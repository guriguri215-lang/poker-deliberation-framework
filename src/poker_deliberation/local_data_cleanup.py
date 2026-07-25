"""Pure P2-027B eligibility and exact cleanup-plan construction."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

from pydantic import TypeAdapter, ValidationError

from poker_deliberation.approvals import (
    ApprovalLedgerCorruptError,
    read_approval_state_v2,
)
from poker_deliberation.local_data_cleanup_canonical import (
    canonical_cleanup_sha256,
    cleanup_plan_sha256,
    run_id_sha256,
    tree_inventory_sha256,
)
from poker_deliberation.local_data_cleanup_models import (
    ApprovalRetentionEvidenceV1,
    CleanupActionKind,
    CleanupActionV1,
    CleanupCandidateEvidenceV1,
    CleanupDryRunResultV1,
    CleanupFailureCode,
    CleanupFailureV1,
    CleanupPlanV1,
    CleanupRootInitializationOutcomeV1,
    CleanupRootInspectionV1,
    LegalHoldSnapshotV1,
    LifecycleEligibilityV1,
    ProductRunSourceV1,
    cleanup_failure,
)
from poker_deliberation.local_data_policy import (
    DEFAULT_LOCAL_DATA_POLICY,
    LifecycleAuditMetadata,
    LifecycleDisposition,
    LifecycleSubject,
    ProtectionReason,
    evaluate_local_data,
)
from poker_deliberation.schemas import ApprovalRequest, ApprovalStatus
from poker_deliberation.storage.local_data_cleanup_store import (
    CleanupStorageError,
    LocalDataCleanupStore,
    initialize_cleanup_root,
    inspect_cleanup_root,
    scan_cleanup_tree,
)
from poker_deliberation.storage.revision_canonical import validate_run_id
from poker_deliberation.storage.revision_lock import LockReleaseError
from poker_deliberation.storage.revision_store import ExistingRunAuthorityV1
from poker_deliberation.storage.terminal_models import ProductRunError, VerifiedRunReadV2
from poker_deliberation.storage.terminal_store import TerminalRunStore

Clock = Callable[[], datetime]


def _clock() -> datetime:
    return datetime.now(UTC)


class LegalHoldProvider(Protocol):
    """Injected local authority for one versioned legal-hold snapshot."""

    def resolve(
        self,
        run_id_sha256: str,
        *,
        evaluated_at: datetime,
    ) -> LegalHoldSnapshotV1:
        """Return the exact current hold state for one hashed run identity."""


class UnavailableLegalHoldProvider:
    def resolve(
        self,
        run_id_sha256: str,
        *,
        evaluated_at: datetime,
    ) -> LegalHoldSnapshotV1:
        del run_id_sha256, evaluated_at
        raise RuntimeError("legal hold provider unavailable")


def _executor_inventory_sha256() -> str:
    package = Path(__file__).resolve().parent
    paths = (
        package / "local_data_cleanup_models.py",
        package / "local_data_cleanup_canonical.py",
        package / "local_data_cleanup.py",
        package / "storage" / "local_data_cleanup_store.py",
    )
    inventory = []
    for path in paths:
        data = path.read_bytes()
        inventory.append(
            {
                "logical_name": path.relative_to(package).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    return canonical_cleanup_sha256(
        "poker-local-data-cleanup-executor-inventory-v1",
        inventory,
    )


def _audit_subject(audit: LifecycleAuditMetadata) -> LifecycleSubject:
    return LifecycleSubject(
        subject_kind=audit.subject_kind,
        subject_id=audit.subject_id,
        logical_name=audit.logical_name,
        classification=audit.classification,
        classification_source=audit.classification_source,
        classification_evidence=audit.classification_evidence,
        encryption_state=audit.subject_encryption_state,
        state=audit.subject_state,
        retention_anchor_kind=audit.retention_anchor_kind,
        retention_started_at=audit.retention_started_at,
        run_id=audit.run_id,
        revision=audit.revision,
        subject_sha256=audit.subject_sha256,
        source_sha256=audit.source_sha256,
        run_verification_basis=audit.run_verification_basis,
        ownership_provenance=audit.ownership_provenance,
        integrity_state=audit.integrity_state,
        lineage_state=audit.lineage_state,
        legal_hold=ProtectionReason.LEGAL_HOLD in audit.protection_reasons,
    )


def _lifecycle_evidence(
    lifecycle_bytes: bytes,
    *,
    lifecycle_sha256: str,
    evaluated_at: datetime,
) -> LifecycleEligibilityV1:
    try:
        audits = TypeAdapter(tuple[LifecycleAuditMetadata, ...]).validate_json(
            lifecycle_bytes,
            strict=False,
        )
    except ValidationError as exc:
        raise ValueError("lifecycle audit list is invalid") from exc
    if not audits:
        raise ValueError("lifecycle audit list is empty")
    expiries: list[datetime] = []
    delete_count = 0
    for audit in audits:
        result = evaluate_local_data(
            _audit_subject(audit),
            clock=lambda: evaluated_at,
            expected_policy_sha256=DEFAULT_LOCAL_DATA_POLICY.canonical_sha256,
            quarantine_reasons=audit.quarantine_reasons,
        )
        if result.status != "evaluated" or result.audit is None:
            raise ValueError("lifecycle evidence could not be reevaluated")
        if result.audit.proposed_disposition is LifecycleDisposition.DELETE_CANDIDATE:
            delete_count += 1
        if result.audit.retention_expires_at is None:
            raise ValueError("lifecycle evidence lacks a retention expiry")
        expiries.append(result.audit.retention_expires_at)
    return LifecycleEligibilityV1(
        local_data_policy_id=DEFAULT_LOCAL_DATA_POLICY.policy_id,
        local_data_policy_sha256=DEFAULT_LOCAL_DATA_POLICY.canonical_sha256,
        lifecycle_audit_sha256=lifecycle_sha256,
        audited_subject_count=len(audits),
        delete_candidate_count=delete_count,
        latest_retention_expires_at=max(expiries),
        evaluated_at=evaluated_at,
    )


def _payload_map(read: VerifiedRunReadV2) -> dict[str, bytes]:
    return {item.inventory.logical_name: item.exact_bytes for item in read.payloads}


def _approval_retention(
    terminal_store: TerminalRunStore,
    run_id: str,
    payloads: dict[str, bytes],
    *,
    evaluated_at: datetime,
) -> ApprovalRetentionEvidenceV1:
    try:
        approvals = TypeAdapter(tuple[ApprovalRequest, ...]).validate_json(
            payloads["approvals.json"],
            strict=False,
        )
    except (KeyError, ValidationError) as exc:
        raise ValueError("V1 approval projection is invalid") from exc
    v1_pending = sum(item.status is ApprovalStatus.PENDING for item in approvals)
    v2_names = {
        "approval_ledger_v2.json",
        "approval_decisions_v2.jsonl",
        "approval_audit_v2.jsonl",
    }
    present = v2_names & set(payloads)
    if present and present != v2_names:
        raise ValueError("authoritative V2 approval artifacts are incomplete")
    if present:
        state = read_approval_state_v2(
            payloads["approval_ledger_v2.json"],
            payloads["approval_decisions_v2.jsonl"],
            payloads["approval_audit_v2.jsonl"],
        )
        v2_pending = sum(request.state == "pending" for request in state.ledger.requests)
        ledger_sha = state.ledger_sha256
    else:
        v2_pending = 0
        ledger_sha = canonical_cleanup_sha256(
            "poker-local-data-cleanup-v1-approval-projection-v1",
            {"approvals_sha256": hashlib.sha256(payloads["approvals.json"]).hexdigest()},
        )
    pointer, events = terminal_store.read_approval_failure_audit(run_id)
    latest = max((event.occurred_at for event in events), default=None)
    return ApprovalRetentionEvidenceV1(
        approval_ledger_sha256=ledger_sha,
        v1_pending_count=v1_pending,
        v2_pending_count=v2_pending,
        failure_audit_head_sha256=pointer.head_event_sha256,
        failure_audit_retention_expires_at=(
            None if latest is None else latest + timedelta(days=365)
        ),
        evaluated_at=evaluated_at,
    )


def _ineligible(
    evidence: CleanupCandidateEvidenceV1,
    code: CleanupFailureCode,
) -> CleanupDryRunResultV1:
    return CleanupDryRunResultV1(
        outcome_kind="ineligible",
        run_id_sha256=evidence.source.run_id_sha256,
        failure=cleanup_failure(code, run_id_sha256=evidence.source.run_id_sha256),
    )


def _approval_retention_expired(evidence: ApprovalRetentionEvidenceV1) -> bool:
    expiry = evidence.failure_audit_retention_expires_at
    return expiry is None or evidence.evaluated_at >= expiry


def evaluate_cleanup_candidate(
    evidence: CleanupCandidateEvidenceV1,
) -> CleanupDryRunResultV1:
    """Return one exact plan or a typed mutation-zero failure.

    This function is deliberately pure.  A storage adapter must construct the
    evidence from one bounded verified run read before calling it.
    """

    source = evidence.source
    if not evidence.ownership_verified:
        return _ineligible(evidence, CleanupFailureCode.OWNERSHIP_UNVERIFIED)
    if not evidence.path_confinement_verified:
        return _ineligible(evidence, CleanupFailureCode.PATH_CONFINEMENT_FAILED)
    if not evidence.integrity_verified or not evidence.lineage_verified:
        return _ineligible(evidence, CleanupFailureCode.CANDIDATE_INELIGIBLE)
    if evidence.product_active:
        return _ineligible(evidence, CleanupFailureCode.ACTIVE_OR_PENDING)
    if evidence.approval_retention.v1_pending_count or evidence.approval_retention.v2_pending_count:
        return _ineligible(evidence, CleanupFailureCode.ACTIVE_OR_PENDING)
    if evidence.legal_hold.legal_hold:
        return _ineligible(evidence, CleanupFailureCode.LEGAL_HOLD)
    if (
        evidence.lifecycle.local_data_policy_id != DEFAULT_LOCAL_DATA_POLICY.policy_id
        or evidence.lifecycle.local_data_policy_sha256 != DEFAULT_LOCAL_DATA_POLICY.canonical_sha256
    ):
        return _ineligible(evidence, CleanupFailureCode.POLICY_MISMATCH)
    if (
        not evidence.lifecycle.all_delete_candidates
        or evidence.generated_at < evidence.lifecycle.latest_retention_expires_at
    ):
        return _ineligible(evidence, CleanupFailureCode.CANDIDATE_INELIGIBLE)
    if not _approval_retention_expired(evidence.approval_retention):
        return _ineligible(evidence, CleanupFailureCode.CANDIDATE_INELIGIBLE)
    if not evidence.cleanup_capacity_reserved:
        return _ineligible(evidence, CleanupFailureCode.CAPACITY_EXCEEDED)
    if evidence.generated_at >= evidence.expires_at:
        return _ineligible(evidence, CleanupFailureCode.PLAN_EXPIRED)
    if evidence.expires_at - evidence.generated_at > timedelta(
        seconds=evidence.limits.maximum_plan_lifetime_seconds
    ):
        return _ineligible(evidence, CleanupFailureCode.INVALID_PLAN)

    if isinstance(source, ProductRunSourceV1):
        action = CleanupActionV1(
            action_kind=CleanupActionKind.QUARANTINE_PRODUCT_RUN,
            source_relative_path=f"runs/{source.run_id}",
            destination_relative_path=f"quarantine/{source.run_id}",
        )
    elif evidence.generated_at < source.delete_eligible_at:
        return _ineligible(evidence, CleanupFailureCode.CANDIDATE_INELIGIBLE)
    else:
        action = CleanupActionV1(
            action_kind=CleanupActionKind.DELETE_QUARANTINE_PAYLOAD,
            source_relative_path=f"quarantine/{source.run_id}",
            destination_relative_path=f"deleting/{source.run_id}",
        )

    plan = CleanupPlanV1(
        executor_sha256=evidence.executor_sha256,
        cleanup_root_id=evidence.cleanup_root_id,
        cleanup_root_marker_sha256=evidence.cleanup_root_marker_sha256,
        source=source,
        tree_inventory_sha256=tree_inventory_sha256(evidence.tree_inventory),
        lifecycle=evidence.lifecycle,
        approval_retention=evidence.approval_retention,
        legal_hold=evidence.legal_hold,
        expected_cleanup_revision=evidence.expected_cleanup_revision,
        expected_cleanup_pointer_sha256=evidence.expected_cleanup_pointer_sha256,
        actions=(action,),
        limits=evidence.limits,
        generated_at=evidence.generated_at,
        expires_at=evidence.expires_at,
        execution_id=evidence.execution_id,
        idempotency_key=evidence.idempotency_key,
    )
    return CleanupDryRunResultV1(
        outcome_kind="eligible",
        run_id_sha256=source.run_id_sha256,
        plan=plan,
        plan_sha256=cleanup_plan_sha256(plan),
    )


def _dry_failure(run_hash: str, code: CleanupFailureCode) -> CleanupDryRunResultV1:
    return CleanupDryRunResultV1(
        outcome_kind="ineligible",
        run_id_sha256=run_hash,
        failure=cleanup_failure(code, run_id_sha256=run_hash),
    )


class LocalDataCleanupExecutor:
    """Additive local Python API for explicit one-run cleanup actions."""

    def __init__(
        self,
        cleanup_root: Path,
        terminal_store: TerminalRunStore,
        *,
        legal_hold_provider: LegalHoldProvider | None = None,
        clock: Clock = _clock,
    ) -> None:
        self.terminal_store = terminal_store
        self.store = LocalDataCleanupStore(cleanup_root, terminal_store)
        self.legal_hold_provider = legal_hold_provider or UnavailableLegalHoldProvider()
        self.clock = clock

    def inspect_cleanup_root(self) -> CleanupRootInspectionV1:
        return inspect_cleanup_root(self.store.cleanup_root)

    def initialize_cleanup_root(
        self,
        *,
        existing_run_id: str,
        root_id: str,
        initialized_at: datetime | None = None,
    ) -> CleanupRootInitializationOutcomeV1:
        return initialize_cleanup_root(
            self.store.cleanup_root,
            self.terminal_store,
            existing_run_id=existing_run_id,
            root_id=root_id,
            initialized_at=initialized_at or self.clock(),
        )

    def dry_run_quarantine(
        self,
        run_id: str,
        *,
        execution_id: str,
        idempotency_key: str,
        expires_at: datetime,
    ) -> CleanupDryRunResultV1:
        """Read one explicit product run and return one plan with write zero."""

        try:
            validate_run_id(run_id)
            run_hash = run_id_sha256(run_id)
        except Exception:
            return _dry_failure("0" * 64, CleanupFailureCode.INVALID_PLAN)
        authority: ExistingRunAuthorityV1 | None = None
        try:
            evaluated_at = self.clock()
            marker, marker_sha = self.store.marker()
            authority = self.terminal_store.foundation.acquire_existing_run_authority(run_id)
            if (
                marker.product_root_identity_sha256 != authority.revision_root_identity_sha256
                or marker.product_ownership_marker_sha256 != authority.ownership_marker_sha256
            ):
                return _dry_failure(run_hash, CleanupFailureCode.OWNERSHIP_UNVERIFIED)
            current = self.terminal_store.read_current(run_id)
            if (
                current.pointer.publication_kind != "product_terminal"
                or current.completion_marker is None
                or current.completion_marker_sha256 is None
                or current.manifest.lifecycle_audit_sha256 is None
                or not current.lifecycle_verified
            ):
                return _dry_failure(run_hash, CleanupFailureCode.ACTIVE_OR_PENDING)
            if self.store.read_current(run_hash) is not None:
                return _dry_failure(run_hash, CleanupFailureCode.STALE_CLEANUP_REVISION)
            inventory = scan_cleanup_tree(
                authority.run_path,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            stable = self.terminal_store.read_current(run_id)
            if (
                stable.current_pointer_sha256 != current.current_pointer_sha256
                or stable.manifest_sha256 != current.manifest_sha256
                or stable.inventory_sha256 != current.inventory_sha256
            ):
                return _dry_failure(run_hash, CleanupFailureCode.STALE_SOURCE)
            payloads = _payload_map(current)
            lifecycle_bytes = payloads.get("lifecycle_audit.json")
            if lifecycle_bytes is None:
                return _dry_failure(run_hash, CleanupFailureCode.CANDIDATE_INELIGIBLE)
            lifecycle = _lifecycle_evidence(
                lifecycle_bytes,
                lifecycle_sha256=current.manifest.lifecycle_audit_sha256,
                evaluated_at=evaluated_at,
            )
            approvals = _approval_retention(
                self.terminal_store,
                run_id,
                payloads,
                evaluated_at=evaluated_at,
            )
            hold = self.legal_hold_provider.resolve(
                run_hash,
                evaluated_at=evaluated_at,
            )
            if hold.run_id_sha256 != run_hash or hold.resolved_at != evaluated_at:
                return _dry_failure(run_hash, CleanupFailureCode.LEGAL_HOLD)
            free_bytes = shutil.disk_usage(self.store.cleanup_root).free
            source = ProductRunSourceV1(
                run_id=run_id,
                run_id_sha256=run_hash,
                product_root_identity_sha256=authority.revision_root_identity_sha256,
                product_ownership_marker_sha256=authority.ownership_marker_sha256,
                current_revision=current.revision,
                current_transaction_id=current.transaction_id,
                current_pointer_sha256=current.current_pointer_sha256,
                manifest_sha256=current.manifest_sha256,
                inventory_sha256=current.inventory_sha256,
                completion_marker_sha256=current.completion_marker_sha256,
                terminal_status=cast(
                    Any,
                    current.completion_marker.terminal_status,
                ),
                terminal_published_at=current.completion_marker.published_at,
            )
            evidence = CleanupCandidateEvidenceV1(
                cleanup_root_id=marker.root_id,
                cleanup_root_marker_sha256=marker_sha,
                executor_sha256=_executor_inventory_sha256(),
                source=source,
                tree_inventory=inventory,
                lifecycle=lifecycle,
                approval_retention=approvals,
                legal_hold=hold,
                expected_cleanup_revision=0,
                expected_cleanup_pointer_sha256=None,
                product_active=current.resume_eligible,
                ownership_verified=True,
                path_confinement_verified=True,
                integrity_verified=True,
                lineage_verified=True,
                cleanup_capacity_reserved=(
                    free_bytes >= marker.limits.maximum_control_bytes_per_run
                ),
                generated_at=evaluated_at,
                expires_at=expires_at,
                execution_id=execution_id,
                idempotency_key=idempotency_key,
                limits=marker.limits,
            )
            return evaluate_cleanup_candidate(evidence)
        except CleanupStorageError as exc:
            failure = exc.failure
            if isinstance(failure, CleanupFailureV1):
                return CleanupDryRunResultV1(
                    outcome_kind="ineligible",
                    run_id_sha256=run_hash,
                    failure=failure,
                )
            return _dry_failure(run_hash, CleanupFailureCode.INTERNAL_INVARIANT_ERROR)
        except (ApprovalLedgerCorruptError, ProductRunError, ValidationError, ValueError):
            return _dry_failure(run_hash, CleanupFailureCode.CANDIDATE_INELIGIBLE)
        except Exception:
            return _dry_failure(run_hash, CleanupFailureCode.INTERNAL_INVARIANT_ERROR)
        finally:
            if authority is not None:
                with suppress(LockReleaseError):
                    authority.release()


__all__ = [
    "LegalHoldProvider",
    "LocalDataCleanupExecutor",
    "UnavailableLegalHoldProvider",
    "evaluate_cleanup_candidate",
]
