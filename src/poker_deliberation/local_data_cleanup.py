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

from poker_deliberation.approval_canonical import (
    action_digest_sha256,
    approval_authority_snapshot_sha256,
    approval_decision_outcome_sha256,
    approval_decision_record_sha256,
)
from poker_deliberation.approval_models import (
    ApprovalFailureCode,
    CanonicalActionPlanV2,
    OutboundFieldBindingV2,
)
from poker_deliberation.approvals import (
    ApprovalLedgerCorruptError,
    DecisionAuthorityProvider,
    read_approval_state_v2,
)
from poker_deliberation.local_data_cleanup_canonical import (
    canonical_cleanup_sha256,
    cleanup_approval_binding_sha256,
    cleanup_plan_sha256,
    cleanup_pointer_sha256,
    cleanup_receipt_sha256,
    cleanup_tombstone_sha256,
    run_id_sha256,
    tree_inventory_sha256,
)
from poker_deliberation.local_data_cleanup_models import (
    ApprovalRetentionEvidenceV1,
    CleanupActionKind,
    CleanupActionV1,
    CleanupApprovalBindingV1,
    CleanupCandidateEvidenceV1,
    CleanupDryRunResultV1,
    CleanupExecutionResultV1,
    CleanupFailureCode,
    CleanupFailureV1,
    CleanupPlanV1,
    CleanupReconciliationReportV1,
    CleanupRootInitializationOutcomeV1,
    CleanupRootInspectionV1,
    CleanupState,
    LegalHoldSnapshotV1,
    LifecycleEligibilityV1,
    ProductRunSourceV1,
    QuarantineSourceV1,
    cleanup_failure,
    delete_staging_relative_path,
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
from poker_deliberation.storage.lifecycle_hooks import build_terminal_lifecycle_audit
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


class _ApprovalExecutionError(ValueError):
    def __init__(self, code: CleanupFailureCode) -> None:
        super().__init__(code.value)
        self.code = code


def cleanup_approval_action_plan(plan: CleanupPlanV1) -> CanonicalActionPlanV2:
    """Return the exact P2-013A action plan that may authorize ``plan``."""

    plan_sha = cleanup_plan_sha256(plan)
    action = plan.actions[0].action_kind
    operation = (
        "Quarantine one verified terminal product run."
        if action is CleanupActionKind.QUARANTINE_PRODUCT_RUN
        else "Delete one verified quarantine payload through staging."
    )
    return CanonicalActionPlanV2(
        operation=operation,
        action_category="destructive_change",
        executor_kind="local_process",
        executor_identifier=plan.executor_id,
        executor_version=plan.executor_version,
        executor_sha256=plan.executor_sha256,
        executor_availability="available",
        outbound_fields=(
            OutboundFieldBindingV2(
                field_name="cleanup_plan_sha256",
                classification="internal",
                content_sha256=plan_sha,
            ),
        ),
        destination_kind="filesystem",
        destination_identifier=plan.cleanup_root_id,
        retention_policy_id=plan.lifecycle.local_data_policy_id,
        trace_policy_id="p2-027b-local-data-cleanup-trace-v1",
        maximum_cost_microunits=0,
        maximum_runtime_ms=86_400_000,
        maximum_memory_bytes=plan.limits.maximum_target_bytes
        + plan.limits.maximum_control_bytes_per_run,
        maximum_output_bytes=plan.limits.maximum_control_bytes_per_run,
        maximum_processes=1,
        working_directory=None,
        environment_name_allowlist=(),
        expected_result_type="cleanup-execution-result-v1",
        execution_id=plan.execution_id,
        remote_idempotency_key=plan.idempotency_key,
        expires_at=plan.expires_at,
    )


def _transaction_id(plan: CleanupPlanV1) -> str:
    digest = canonical_cleanup_sha256(
        "poker-local-data-cleanup-transaction-id-v1",
        {
            "run_id_sha256": plan.source.run_id_sha256,
            "execution_id": plan.execution_id,
            "idempotency_key": plan.idempotency_key,
            "plan_sha256": cleanup_plan_sha256(plan),
        },
    )
    return f"cleanup-txn-{digest[:32]}"


def _execution_failure(
    plan: CleanupPlanV1,
    code: CleanupFailureCode,
    *,
    transaction_id: str | None = None,
    cleanup_revision: int | None = None,
    filesystem_effect: str = "none",
    domain_effect: str = "none",
) -> CleanupExecutionResultV1:
    plan_sha = cleanup_plan_sha256(plan)
    transaction_id = transaction_id or _transaction_id(plan)
    return CleanupExecutionResultV1(
        outcome_kind="failed",
        run_id_sha256=plan.source.run_id_sha256,
        execution_id=plan.execution_id,
        idempotency_key=plan.idempotency_key,
        transaction_id=transaction_id,
        plan_sha256=plan_sha,
        cleanup_revision=(
            plan.expected_cleanup_revision if cleanup_revision is None else cleanup_revision
        ),
        failure=cleanup_failure(
            code,
            run_id_sha256=plan.source.run_id_sha256,
            plan_sha256=plan_sha,
            transaction_id=transaction_id,
            filesystem_effect=cast(Any, filesystem_effect),
            domain_effect=cast(Any, domain_effect),
        ),
    )


def _persisted_execution_result(
    plan: CleanupPlanV1,
    persisted: tuple[Any, Any, Any, Any],
    *,
    reconciliation: CleanupReconciliationReportV1 | None = None,
) -> CleanupExecutionResultV1:
    pointer, manifest, receipt, tombstone = persisted
    if pointer.state is CleanupState.DELETE_PREPARED:
        if reconciliation is None:
            raise ValueError("delete-prepared replay requires reconciliation evidence")
        effect_unknown = (
            reconciliation.classification == "effect_unknown"
            or reconciliation.observed_staging == "unreadable"
            or reconciliation.observed_current == "unreadable"
        )
        return _execution_failure(
            plan,
            (
                CleanupFailureCode.EFFECT_UNKNOWN
                if effect_unknown
                else CleanupFailureCode.RECONCILIATION_REQUIRED
            ),
            transaction_id=manifest.transaction_id,
            cleanup_revision=pointer.revision,
            filesystem_effect=(
                "delete_staging_moved"
                if reconciliation.observed_staging == "exact"
                else "partial_delete"
            ),
            domain_effect=("current_may_have_advanced" if effect_unknown else "current_advanced"),
        )
    expected_state = (
        CleanupState.QUARANTINED
        if isinstance(plan.source, ProductRunSourceV1)
        else CleanupState.DELETED
    )
    if pointer.state is not expected_state:
        return _execution_failure(
            plan,
            CleanupFailureCode.IDEMPOTENCY_CONFLICT,
            transaction_id=manifest.transaction_id,
            cleanup_revision=pointer.revision,
        )
    return CleanupExecutionResultV1(
        outcome_kind="committed",
        run_id_sha256=plan.source.run_id_sha256,
        execution_id=plan.execution_id,
        idempotency_key=plan.idempotency_key,
        transaction_id=manifest.transaction_id,
        plan_sha256=manifest.plan_sha256,
        cleanup_revision=pointer.revision,
        cleanup_pointer_sha256=cleanup_pointer_sha256(pointer),
        receipt=receipt,
        receipt_sha256=cleanup_receipt_sha256(receipt),
        tombstone=tombstone,
        tombstone_sha256=cleanup_tombstone_sha256(tombstone),
    )


def _cancelled(callback: Callable[[], bool] | None) -> bool:
    return callback is not None and callback()


def _verify_cleanup_approval(
    terminal_store: TerminalRunStore,
    plan: CleanupPlanV1,
    *,
    approval_run_id: str,
    request_id: str,
    authority_provider: DecisionAuthorityProvider,
    evaluated_at: datetime,
) -> CleanupApprovalBindingV1:
    """Verify immutable approval evidence and live authority for one execution."""

    if (
        evaluated_at.tzinfo is None
        or evaluated_at.utcoffset() is None
        or evaluated_at.utcoffset() != timedelta(0)
        or evaluated_at >= plan.expires_at
    ):
        raise _ApprovalExecutionError(CleanupFailureCode.PLAN_EXPIRED)
    try:
        approval_read = terminal_store.read_current(approval_run_id)
        state = read_approval_state_v2(
            approval_read.payload_bytes("approval_ledger_v2.json"),
            approval_read.payload_bytes("approval_decisions_v2.jsonl"),
            approval_read.payload_bytes("approval_audit_v2.jsonl"),
        )
    except (ApprovalLedgerCorruptError, ProductRunError, KeyError, ValidationError, ValueError):
        raise _ApprovalExecutionError(CleanupFailureCode.APPROVAL_MISSING) from None
    if state.ledger.run_id != approval_run_id:
        raise _ApprovalExecutionError(CleanupFailureCode.APPROVAL_MISMATCH)
    request_matches = tuple(
        request for request in state.ledger.requests if request.request_id == request_id
    )
    if len(request_matches) != 1:
        raise _ApprovalExecutionError(CleanupFailureCode.APPROVAL_MISSING)
    request = request_matches[0]
    expected_action = cleanup_approval_action_plan(plan)
    expected_digest = action_digest_sha256(expected_action)
    if (
        request.state != "approved"
        or request.action_plan != expected_action
        or request.action_digest_sha256 != expected_digest
        or request.required_authority_scope != "approve:destructive_change"
        or evaluated_at >= request.expires_at
    ):
        raise _ApprovalExecutionError(CleanupFailureCode.APPROVAL_MISMATCH)
    record_matches = []
    for record in state.decision_records:
        result = next(
            (
                item
                for item in record.outcome.request_results
                if item.request_id == request.request_id
            ),
            None,
        )
        if result is not None:
            record_matches.append((record, result))
    if len(record_matches) != 1:
        raise _ApprovalExecutionError(CleanupFailureCode.APPROVAL_MISMATCH)
    record, result = record_matches[0]
    limitation = record.outcome.limitation
    if (
        result.decision != "approved"
        or result.request_revision != request.request_revision
        or result.action_digest_sha256 != expected_digest
        or record.outcome.outcome_kind != "committed"
        or record.outcome.run_status != "failed_with_limitations"
        or limitation is None
        or limitation.code is not ApprovalFailureCode.EXTERNAL_EXECUTOR_UNAVAILABLE
        or record.record_sha256 != approval_decision_record_sha256(record)
        or record.outcome_sha256 != approval_decision_outcome_sha256(record.outcome)
    ):
        raise _ApprovalExecutionError(CleanupFailureCode.APPROVAL_MISMATCH)
    saved_snapshot = record.authority_snapshot
    try:
        live_snapshot = authority_provider.resolve_actor(
            saved_snapshot.actor.actor_id,
            decision_at=evaluated_at,
        )
    except Exception:
        raise _ApprovalExecutionError(CleanupFailureCode.UNAUTHORIZED_EXECUTION) from None
    if (
        live_snapshot.resolved_at != evaluated_at
        or live_snapshot.provider_id != saved_snapshot.provider_id
        or live_snapshot.provider_version != saved_snapshot.provider_version
        or live_snapshot.actor != saved_snapshot.actor
    ):
        code = (
            CleanupFailureCode.AUTHORITY_REVOKED
            if live_snapshot.actor.revocation_status == "revoked"
            else CleanupFailureCode.ACTOR_SPOOF
        )
        raise _ApprovalExecutionError(code)
    actor = live_snapshot.actor
    if actor.revocation_status == "revoked":
        raise _ApprovalExecutionError(CleanupFailureCode.AUTHORITY_REVOKED)
    if (
        actor.verification_status != "verified"
        or actor.revocation_status != "not_revoked"
        or actor.authority_expires_at is None
        or evaluated_at >= actor.authority_expires_at
        or "approve:destructive_change" not in actor.authority_scopes
    ):
        raise _ApprovalExecutionError(CleanupFailureCode.UNAUTHORIZED_EXECUTION)
    return CleanupApprovalBindingV1(
        approval_run_id_sha256=run_id_sha256(approval_run_id),
        approval_run_revision=approval_read.revision,
        approval_pointer_sha256=approval_read.current_pointer_sha256,
        approval_ledger_sha256=state.ledger_sha256,
        request_id=request.request_id,
        request_revision=request.request_revision,
        action_digest_sha256=request.action_digest_sha256,
        decision_id=record.decision_id,
        decision_record_sha256=record.record_sha256,
        decision_outcome_sha256=record.outcome_sha256,
        actor_sha256=record.actor_sha256,
        authority_snapshot_sha256=approval_authority_snapshot_sha256(saved_snapshot),
        authority_provider_id=saved_snapshot.provider_id,
        authority_provider_version=saved_snapshot.provider_version,
    )


def _executor_inventory_sha256() -> str:
    package = Path(__file__).resolve().parent
    paths = (
        package / "local_data_cleanup_models.py",
        package / "local_data_cleanup_canonical.py",
        package / "local_data_cleanup.py",
        package / "storage" / "local_data_cleanup_store.py",
        package / "storage" / "revision_store.py",
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
    current: VerifiedRunReadV2,
    *,
    evaluated_at: datetime,
) -> LifecycleEligibilityV1:
    payloads = _payload_map(current)
    lifecycle_bytes = payloads.get("lifecycle_audit.json")
    if (
        lifecycle_bytes is None
        or current.completion_marker is None
        or current.manifest.lifecycle_audit_sha256 is None
    ):
        raise ValueError("terminal lifecycle evidence is incomplete")
    approval_names = {
        "approval_ledger_v2.json",
        "approval_decisions_v2.jsonl",
        "approval_audit_v2.jsonl",
    }
    present_approval_names = approval_names & set(payloads)
    if present_approval_names and present_approval_names != approval_names:
        raise ValueError("terminal approval control evidence is incomplete")
    excluded_names = {"lifecycle_audit.json", *present_approval_names}
    audited_inventory = tuple(
        payload.inventory
        for payload in current.payloads
        if payload.inventory.logical_name not in excluded_names
    )
    expected = build_terminal_lifecycle_audit(
        run_id=current.run_id,
        revision=current.revision,
        published_at=current.completion_marker.published_at,
        inventory=audited_inventory,
    )
    if (
        lifecycle_bytes != expected.canonical_bytes
        or current.manifest.lifecycle_audit_sha256 != expected.sha256
    ):
        raise ValueError("lifecycle audit does not cover the exact product inventory")
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
    control_count = len(excluded_names)
    control_expiry = current.completion_marker.published_at + timedelta(
        days=DEFAULT_LOCAL_DATA_POLICY.lifecycle_audit_days
    )
    control_delete_count = control_count if evaluated_at >= control_expiry else 0
    return LifecycleEligibilityV1(
        local_data_policy_id=DEFAULT_LOCAL_DATA_POLICY.policy_id,
        local_data_policy_sha256=DEFAULT_LOCAL_DATA_POLICY.canonical_sha256,
        lifecycle_audit_sha256=current.manifest.lifecycle_audit_sha256,
        audited_subject_count=len(audits) + control_count,
        delete_candidate_count=delete_count + control_delete_count,
        latest_retention_expires_at=max((*expiries, control_expiry)),
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


def _legal_hold_matches(
    saved: LegalHoldSnapshotV1,
    live: LegalHoldSnapshotV1,
    *,
    evaluated_at: datetime,
) -> bool:
    return (
        not saved.legal_hold
        and not live.legal_hold
        and live.provider_id == saved.provider_id
        and live.provider_version == saved.provider_version
        and live.run_id_sha256 == saved.run_id_sha256
        and live.snapshot_reference_sha256 == saved.snapshot_reference_sha256
        and live.resolved_at == evaluated_at
    )


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
            destination_relative_path=delete_staging_relative_path(
                source,
                evidence.execution_id,
            ),
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

    def execute(
        self,
        plan: CleanupPlanV1,
        *,
        approval_run_id: str,
        approval_request_id: str,
        authority_provider: DecisionAuthorityProvider,
        cancelled: Callable[[], bool] | None = None,
    ) -> CleanupExecutionResultV1:
        """Dispatch one exact cleanup plan to its only permitted action."""

        if isinstance(plan.source, ProductRunSourceV1):
            return self.execute_quarantine(
                plan,
                approval_run_id=approval_run_id,
                approval_request_id=approval_request_id,
                authority_provider=authority_provider,
                cancelled=cancelled,
            )
        return self.execute_delete(
            plan,
            approval_run_id=approval_run_id,
            approval_request_id=approval_request_id,
            authority_provider=authority_provider,
            cancelled=cancelled,
        )

    def inspect_cleanup_root(self) -> CleanupRootInspectionV1:
        inspection = inspect_cleanup_root(self.store.cleanup_root)
        if inspection.status != "initialized":
            return inspection
        try:
            self.store.marker()
        except CleanupStorageError:
            return CleanupRootInspectionV1(
                status="corrupt",
                recognized_relative_paths=inspection.recognized_relative_paths,
            )
        return inspection

    def inspect_reconciliation(
        self,
        plan: CleanupPlanV1,
    ) -> CleanupReconciliationReportV1:
        """Return a read-only manual-reconciliation classification."""

        return self.store.inspect_reconciliation(
            plan,
            transaction_id=_transaction_id(plan),
        )

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

    def _revalidate_quarantine_eligibility(
        self,
        plan: CleanupPlanV1,
        *,
        evaluated_at: datetime,
    ) -> None:
        if (
            not isinstance(plan.source, ProductRunSourceV1)
            or plan.actions[0].action_kind is not CleanupActionKind.QUARANTINE_PRODUCT_RUN
            or plan.executor_sha256 != _executor_inventory_sha256()
            or plan.generated_at > evaluated_at
            or evaluated_at >= plan.expires_at
        ):
            raise _ApprovalExecutionError(
                CleanupFailureCode.PLAN_EXPIRED
                if evaluated_at >= plan.expires_at
                else CleanupFailureCode.INVALID_PLAN
            )
        try:
            current = self.terminal_store.read_current(plan.source.run_id)
            payloads = _payload_map(current)
            if (
                current.pointer.publication_kind != "product_terminal"
                or current.resume_eligible
                or current.completion_marker is None
                or not current.lifecycle_verified
            ):
                raise _ApprovalExecutionError(CleanupFailureCode.ACTIVE_OR_PENDING)
            lifecycle = _lifecycle_evidence(current, evaluated_at=evaluated_at)
            approvals = _approval_retention(
                self.terminal_store,
                plan.source.run_id,
                payloads,
                evaluated_at=evaluated_at,
            )
            try:
                hold = self.legal_hold_provider.resolve(
                    plan.source.run_id_sha256,
                    evaluated_at=evaluated_at,
                )
            except Exception:
                raise _ApprovalExecutionError(CleanupFailureCode.LEGAL_HOLD) from None
        except _ApprovalExecutionError:
            raise
        except (ApprovalLedgerCorruptError, ProductRunError, KeyError, ValidationError, ValueError):
            raise _ApprovalExecutionError(CleanupFailureCode.CANDIDATE_INELIGIBLE) from None
        except Exception:
            raise _ApprovalExecutionError(CleanupFailureCode.LEGAL_HOLD) from None
        if (
            lifecycle.local_data_policy_id != plan.lifecycle.local_data_policy_id
            or lifecycle.local_data_policy_sha256 != plan.lifecycle.local_data_policy_sha256
        ):
            raise _ApprovalExecutionError(CleanupFailureCode.POLICY_MISMATCH)
        if (
            not lifecycle.all_delete_candidates
            or evaluated_at < lifecycle.latest_retention_expires_at
        ):
            raise _ApprovalExecutionError(CleanupFailureCode.CANDIDATE_INELIGIBLE)
        if approvals.v1_pending_count or approvals.v2_pending_count:
            raise _ApprovalExecutionError(CleanupFailureCode.ACTIVE_OR_PENDING)
        if not _approval_retention_expired(approvals):
            raise _ApprovalExecutionError(CleanupFailureCode.CANDIDATE_INELIGIBLE)
        if not _legal_hold_matches(plan.legal_hold, hold, evaluated_at=evaluated_at):
            raise _ApprovalExecutionError(CleanupFailureCode.LEGAL_HOLD)
        if (
            shutil.disk_usage(self.store.cleanup_root).free
            < plan.limits.maximum_control_bytes_per_run
        ):
            raise _ApprovalExecutionError(CleanupFailureCode.CAPACITY_EXCEEDED)

    def dry_run_delete(
        self,
        run_id: str,
        *,
        execution_id: str,
        idempotency_key: str,
        expires_at: datetime,
    ) -> CleanupDryRunResultV1:
        """Read one explicit quarantine current and return one delete plan."""

        try:
            validate_run_id(run_id)
            run_hash = run_id_sha256(run_id)
        except Exception:
            return _dry_failure("0" * 64, CleanupFailureCode.INVALID_PLAN)
        try:
            evaluated_at = self.clock()
            marker, marker_sha = self.store.marker()
            binding = self.terminal_store.foundation.inspect_run_authority_binding(
                run_id,
                detached=True,
            )
            if (
                marker.product_root_identity_sha256 != binding.revision_root_identity_sha256
                or marker.product_ownership_marker_sha256 != binding.ownership_marker_sha256
            ):
                return _dry_failure(run_hash, CleanupFailureCode.OWNERSHIP_UNVERIFIED)
            current = self.store.read_current(run_hash)
            if current is None:
                return _dry_failure(run_hash, CleanupFailureCode.CANDIDATE_INELIGIBLE)
            pointer, manifest, _receipt, tombstone = current
            if (
                pointer.state is not CleanupState.QUARANTINED
                or manifest.action_kind is not CleanupActionKind.QUARANTINE_PRODUCT_RUN
                or not isinstance(manifest.plan.source, ProductRunSourceV1)
                or manifest.plan.source.run_id != run_id
            ):
                return _dry_failure(run_hash, CleanupFailureCode.STALE_CLEANUP_REVISION)
            quarantine = self.store.quarantine_path(run_id)
            inventory = scan_cleanup_tree(
                quarantine,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            tree_sha = tree_inventory_sha256(inventory)
            if (
                tree_sha != tombstone.quarantine_tree_sha256
                or tree_sha != manifest.source_tree_sha256
            ):
                return _dry_failure(run_hash, CleanupFailureCode.STALE_SOURCE)
            stable = self.store.read_current(run_hash)
            if stable is None or cleanup_pointer_sha256(stable[0]) != cleanup_pointer_sha256(
                pointer
            ):
                return _dry_failure(run_hash, CleanupFailureCode.STALE_CLEANUP_REVISION)
            try:
                hold = self.legal_hold_provider.resolve(
                    run_hash,
                    evaluated_at=evaluated_at,
                )
            except Exception:
                return _dry_failure(run_hash, CleanupFailureCode.LEGAL_HOLD)
            if (
                hold.run_id_sha256 != run_hash
                or hold.resolved_at != evaluated_at
                or hold.legal_hold
            ):
                return _dry_failure(run_hash, CleanupFailureCode.LEGAL_HOLD)
            source = QuarantineSourceV1(
                run_id=run_id,
                run_id_sha256=run_hash,
                cleanup_root_identity_sha256=marker.cleanup_root_identity_sha256,
                cleanup_revision=pointer.revision,
                cleanup_pointer_sha256=cleanup_pointer_sha256(pointer),
                tombstone_sha256=cleanup_tombstone_sha256(tombstone),
                quarantine_tree_sha256=tree_sha,
                quarantine_entered_at=tombstone.quarantine_entered_at,
                delete_eligible_at=tombstone.quarantine_entered_at + timedelta(days=30),
            )
            lifecycle = LifecycleEligibilityV1.model_validate(
                manifest.plan.lifecycle.model_dump()
                | {
                    "evaluated_at": evaluated_at,
                }
            )
            approval_retention = ApprovalRetentionEvidenceV1.model_validate(
                manifest.plan.approval_retention.model_dump()
                | {
                    "evaluated_at": evaluated_at,
                }
            )
            evidence = CleanupCandidateEvidenceV1(
                cleanup_root_id=marker.root_id,
                cleanup_root_marker_sha256=marker_sha,
                executor_sha256=_executor_inventory_sha256(),
                source=source,
                tree_inventory=inventory,
                lifecycle=lifecycle,
                approval_retention=approval_retention,
                legal_hold=hold,
                expected_cleanup_revision=pointer.revision,
                expected_cleanup_pointer_sha256=cleanup_pointer_sha256(pointer),
                product_active=False,
                ownership_verified=True,
                path_confinement_verified=True,
                integrity_verified=True,
                lineage_verified=True,
                cleanup_capacity_reserved=(
                    shutil.disk_usage(self.store.cleanup_root).free
                    >= marker.limits.maximum_control_bytes_per_run
                ),
                generated_at=evaluated_at,
                expires_at=expires_at,
                execution_id=execution_id,
                idempotency_key=idempotency_key,
                limits=marker.limits,
            )
            return evaluate_cleanup_candidate(evidence)
        except CleanupStorageError as exc:
            if isinstance(exc.failure, CleanupFailureV1):
                return CleanupDryRunResultV1(
                    outcome_kind="ineligible",
                    run_id_sha256=run_hash,
                    failure=exc.failure,
                )
            return _dry_failure(run_hash, CleanupFailureCode.INTERNAL_INVARIANT_ERROR)
        except (ProductRunError, ValidationError, ValueError):
            return _dry_failure(run_hash, CleanupFailureCode.CANDIDATE_INELIGIBLE)
        except Exception:
            return _dry_failure(run_hash, CleanupFailureCode.INTERNAL_INVARIANT_ERROR)

    def execute_quarantine(
        self,
        plan: CleanupPlanV1,
        *,
        approval_run_id: str,
        approval_request_id: str,
        authority_provider: DecisionAuthorityProvider,
        cancelled: Callable[[], bool] | None = None,
    ) -> CleanupExecutionResultV1:
        """Execute one exact approved quarantine plan or return a typed failure."""

        transaction_id = _transaction_id(plan)
        try:
            persisted = self.store.read_operation(
                plan,
                approval_run_id_sha256=run_id_sha256(approval_run_id),
                approval_request_id=approval_request_id,
            )
            if persisted is not None:
                reconciliation = (
                    self.store.inspect_reconciliation(
                        plan,
                        transaction_id=persisted[1].transaction_id,
                    )
                    if persisted[0].state is CleanupState.DELETE_PREPARED
                    else None
                )
                return _persisted_execution_result(
                    plan,
                    persisted,
                    reconciliation=reconciliation,
                )
            if _cancelled(cancelled):
                raise _ApprovalExecutionError(CleanupFailureCode.CANCELLED)
            evaluated_at = self.clock()
            approval = _verify_cleanup_approval(
                self.terminal_store,
                plan,
                approval_run_id=approval_run_id,
                request_id=approval_request_id,
                authority_provider=authority_provider,
                evaluated_at=evaluated_at,
            )
            approval_sha = cleanup_approval_binding_sha256(approval)
            current = self.store.read_current(plan.source.run_id_sha256)
            if current is not None:
                pointer, manifest, receipt, tombstone = current
                same_identity = (
                    manifest.execution_id == plan.execution_id
                    or manifest.idempotency_key == plan.idempotency_key
                )
                exact = (
                    manifest.action_kind is CleanupActionKind.QUARANTINE_PRODUCT_RUN
                    and manifest.execution_id == plan.execution_id
                    and manifest.idempotency_key == plan.idempotency_key
                    and manifest.plan_sha256 == cleanup_plan_sha256(plan)
                    and manifest.approval_binding_sha256 == approval_sha
                    and receipt.plan_sha256 == manifest.plan_sha256
                    and receipt.approval_binding_sha256 == approval_sha
                )
                if exact:
                    return CleanupExecutionResultV1(
                        outcome_kind="committed",
                        run_id_sha256=plan.source.run_id_sha256,
                        execution_id=plan.execution_id,
                        idempotency_key=plan.idempotency_key,
                        transaction_id=manifest.transaction_id,
                        plan_sha256=manifest.plan_sha256,
                        cleanup_revision=pointer.revision,
                        cleanup_pointer_sha256=cleanup_pointer_sha256(pointer),
                        receipt=receipt,
                        receipt_sha256=cleanup_receipt_sha256(receipt),
                        tombstone=tombstone,
                        tombstone_sha256=cleanup_tombstone_sha256(tombstone),
                    )
                raise _ApprovalExecutionError(
                    CleanupFailureCode.IDEMPOTENCY_CONFLICT
                    if same_identity
                    else CleanupFailureCode.STALE_CLEANUP_REVISION
                )
            self._revalidate_quarantine_eligibility(plan, evaluated_at=evaluated_at)

            def authorize_in_lock() -> CleanupApprovalBindingV1:
                try:
                    if _cancelled(cancelled):
                        raise _ApprovalExecutionError(CleanupFailureCode.CANCELLED)
                    locked_at = self.clock()
                    self._revalidate_quarantine_eligibility(plan, evaluated_at=locked_at)
                    locked = _verify_cleanup_approval(
                        self.terminal_store,
                        plan,
                        approval_run_id=approval_run_id,
                        request_id=approval_request_id,
                        authority_provider=authority_provider,
                        evaluated_at=locked_at,
                    )
                    if locked != approval:
                        raise _ApprovalExecutionError(CleanupFailureCode.APPROVAL_MISMATCH)
                    return locked
                except _ApprovalExecutionError as exc:
                    raise CleanupStorageError(
                        cleanup_failure(
                            exc.code,
                            run_id_sha256=plan.source.run_id_sha256,
                            plan_sha256=cleanup_plan_sha256(plan),
                            transaction_id=transaction_id,
                        )
                    ) from None

            return self.store._publish_quarantine(
                plan,
                transaction_id=transaction_id,
                clock=self.clock,
                authorize=authorize_in_lock,
                cancelled=cancelled,
            )
        except _ApprovalExecutionError as exc:
            return _execution_failure(plan, exc.code, transaction_id=transaction_id)
        except CleanupStorageError as exc:
            failure = exc.failure
            if isinstance(failure, CleanupFailureV1):
                return CleanupExecutionResultV1(
                    outcome_kind="failed",
                    run_id_sha256=plan.source.run_id_sha256,
                    execution_id=plan.execution_id,
                    idempotency_key=plan.idempotency_key,
                    transaction_id=transaction_id,
                    plan_sha256=cleanup_plan_sha256(plan),
                    cleanup_revision=plan.expected_cleanup_revision,
                    failure=failure,
                )
            return _execution_failure(
                plan,
                CleanupFailureCode.INTERNAL_INVARIANT_ERROR,
                transaction_id=transaction_id,
            )
        except Exception:
            return _execution_failure(
                plan,
                CleanupFailureCode.INTERNAL_INVARIANT_ERROR,
                transaction_id=transaction_id,
            )

    def _revalidate_delete_eligibility(
        self,
        plan: CleanupPlanV1,
        *,
        evaluated_at: datetime,
    ) -> None:
        if (
            not isinstance(plan.source, QuarantineSourceV1)
            or plan.actions[0].action_kind is not CleanupActionKind.DELETE_QUARANTINE_PAYLOAD
            or plan.executor_sha256 != _executor_inventory_sha256()
            or plan.generated_at > evaluated_at
            or evaluated_at >= plan.expires_at
        ):
            raise _ApprovalExecutionError(
                CleanupFailureCode.PLAN_EXPIRED
                if evaluated_at >= plan.expires_at
                else CleanupFailureCode.INVALID_PLAN
            )
        if evaluated_at < plan.source.delete_eligible_at:
            raise _ApprovalExecutionError(CleanupFailureCode.CANDIDATE_INELIGIBLE)
        marker, marker_sha = self.store.marker()
        if (
            marker.root_id != plan.cleanup_root_id
            or marker_sha != plan.cleanup_root_marker_sha256
            or marker.cleanup_root_identity_sha256 != plan.source.cleanup_root_identity_sha256
            or plan.lifecycle.local_data_policy_id != DEFAULT_LOCAL_DATA_POLICY.policy_id
            or plan.lifecycle.local_data_policy_sha256 != DEFAULT_LOCAL_DATA_POLICY.canonical_sha256
            or not plan.lifecycle.all_delete_candidates
            or evaluated_at < plan.lifecycle.latest_retention_expires_at
            or plan.approval_retention.v1_pending_count
            or plan.approval_retention.v2_pending_count
            or not _approval_retention_expired(
                ApprovalRetentionEvidenceV1.model_validate(
                    plan.approval_retention.model_dump()
                    | {
                        "evaluated_at": evaluated_at,
                    }
                )
            )
        ):
            raise _ApprovalExecutionError(CleanupFailureCode.POLICY_MISMATCH)
        current = self.store.read_current(plan.source.run_id_sha256)
        if current is None:
            raise _ApprovalExecutionError(CleanupFailureCode.STALE_CLEANUP_REVISION)
        pointer, _manifest, _receipt, tombstone = current
        if (
            pointer.state is not CleanupState.QUARANTINED
            or pointer.revision != plan.source.cleanup_revision
            or cleanup_pointer_sha256(pointer) != plan.source.cleanup_pointer_sha256
            or cleanup_tombstone_sha256(tombstone) != plan.source.tombstone_sha256
        ):
            raise _ApprovalExecutionError(CleanupFailureCode.STALE_CLEANUP_REVISION)
        quarantine = self.store.quarantine_path(plan.source.run_id)
        try:
            inventory = scan_cleanup_tree(
                quarantine,
                run_id_sha256=plan.source.run_id_sha256,
                limits=marker.limits,
            )
            try:
                hold = self.legal_hold_provider.resolve(
                    plan.source.run_id_sha256,
                    evaluated_at=evaluated_at,
                )
            except Exception:
                raise _ApprovalExecutionError(CleanupFailureCode.LEGAL_HOLD) from None
        except CleanupStorageError:
            raise
        except Exception:
            raise _ApprovalExecutionError(CleanupFailureCode.LEGAL_HOLD) from None
        if (
            tree_inventory_sha256(inventory) != plan.tree_inventory_sha256
            or tree_inventory_sha256(inventory) != plan.source.quarantine_tree_sha256
        ):
            raise _ApprovalExecutionError(CleanupFailureCode.STALE_SOURCE)
        if not _legal_hold_matches(plan.legal_hold, hold, evaluated_at=evaluated_at):
            raise _ApprovalExecutionError(CleanupFailureCode.LEGAL_HOLD)
        if (
            shutil.disk_usage(self.store.cleanup_root).free
            < plan.limits.maximum_control_bytes_per_run
        ):
            raise _ApprovalExecutionError(CleanupFailureCode.CAPACITY_EXCEEDED)

    def execute_delete(
        self,
        plan: CleanupPlanV1,
        *,
        approval_run_id: str,
        approval_request_id: str,
        authority_provider: DecisionAuthorityProvider,
        cancelled: Callable[[], bool] | None = None,
    ) -> CleanupExecutionResultV1:
        """Execute one exact approved staged-delete plan."""

        transaction_id = _transaction_id(plan)
        try:
            persisted = self.store.read_operation(
                plan,
                approval_run_id_sha256=run_id_sha256(approval_run_id),
                approval_request_id=approval_request_id,
            )
            if persisted is not None:
                reconciliation = (
                    self.store.inspect_reconciliation(
                        plan,
                        transaction_id=persisted[1].transaction_id,
                    )
                    if persisted[0].state is CleanupState.DELETE_PREPARED
                    else None
                )
                return _persisted_execution_result(
                    plan,
                    persisted,
                    reconciliation=reconciliation,
                )
            if _cancelled(cancelled):
                raise _ApprovalExecutionError(CleanupFailureCode.CANCELLED)
            evaluated_at = self.clock()
            approval = _verify_cleanup_approval(
                self.terminal_store,
                plan,
                approval_run_id=approval_run_id,
                request_id=approval_request_id,
                authority_provider=authority_provider,
                evaluated_at=evaluated_at,
            )
            approval_sha = cleanup_approval_binding_sha256(approval)
            current = self.store.read_current(plan.source.run_id_sha256)
            if current is not None and current[0].state is not CleanupState.QUARANTINED:
                pointer, manifest, receipt, tombstone = current
                same_identity = (
                    manifest.execution_id == plan.execution_id
                    or manifest.idempotency_key == plan.idempotency_key
                )
                exact = (
                    pointer.state is CleanupState.DELETED
                    and manifest.action_kind is CleanupActionKind.DELETE_QUARANTINE_PAYLOAD
                    and manifest.execution_id == plan.execution_id
                    and manifest.idempotency_key == plan.idempotency_key
                    and manifest.plan_sha256 == cleanup_plan_sha256(plan)
                    and manifest.approval_binding_sha256 == approval_sha
                )
                if exact:
                    return CleanupExecutionResultV1(
                        outcome_kind="committed",
                        run_id_sha256=plan.source.run_id_sha256,
                        execution_id=plan.execution_id,
                        idempotency_key=plan.idempotency_key,
                        transaction_id=manifest.transaction_id,
                        plan_sha256=manifest.plan_sha256,
                        cleanup_revision=pointer.revision,
                        cleanup_pointer_sha256=cleanup_pointer_sha256(pointer),
                        receipt=receipt,
                        receipt_sha256=cleanup_receipt_sha256(receipt),
                        tombstone=tombstone,
                        tombstone_sha256=cleanup_tombstone_sha256(tombstone),
                    )
                if pointer.state is CleanupState.DELETE_PREPARED and same_identity:
                    raise _ApprovalExecutionError(CleanupFailureCode.RECONCILIATION_REQUIRED)
                raise _ApprovalExecutionError(
                    CleanupFailureCode.IDEMPOTENCY_CONFLICT
                    if same_identity
                    else CleanupFailureCode.STALE_CLEANUP_REVISION
                )
            self._revalidate_delete_eligibility(plan, evaluated_at=evaluated_at)

            def authorize_in_lock() -> CleanupApprovalBindingV1:
                try:
                    if _cancelled(cancelled):
                        raise _ApprovalExecutionError(CleanupFailureCode.CANCELLED)
                    locked_at = self.clock()
                    self._revalidate_delete_eligibility(plan, evaluated_at=locked_at)
                    locked = _verify_cleanup_approval(
                        self.terminal_store,
                        plan,
                        approval_run_id=approval_run_id,
                        request_id=approval_request_id,
                        authority_provider=authority_provider,
                        evaluated_at=locked_at,
                    )
                    if locked != approval:
                        raise _ApprovalExecutionError(CleanupFailureCode.APPROVAL_MISMATCH)
                    return locked
                except _ApprovalExecutionError as exc:
                    raise CleanupStorageError(
                        cleanup_failure(
                            exc.code,
                            run_id_sha256=plan.source.run_id_sha256,
                            plan_sha256=cleanup_plan_sha256(plan),
                            transaction_id=transaction_id,
                        )
                    ) from None

            return self.store._publish_delete(
                plan,
                transaction_id=transaction_id,
                clock=self.clock,
                authorize=authorize_in_lock,
                cancelled=cancelled,
            )
        except _ApprovalExecutionError as exc:
            return _execution_failure(plan, exc.code, transaction_id=transaction_id)
        except CleanupStorageError as exc:
            if isinstance(exc.failure, CleanupFailureV1):
                return CleanupExecutionResultV1(
                    outcome_kind="failed",
                    run_id_sha256=plan.source.run_id_sha256,
                    execution_id=plan.execution_id,
                    idempotency_key=plan.idempotency_key,
                    transaction_id=transaction_id,
                    plan_sha256=cleanup_plan_sha256(plan),
                    cleanup_revision=plan.expected_cleanup_revision,
                    failure=exc.failure,
                )
            return _execution_failure(
                plan,
                CleanupFailureCode.INTERNAL_INVARIANT_ERROR,
                transaction_id=transaction_id,
            )
        except Exception:
            return _execution_failure(
                plan,
                CleanupFailureCode.INTERNAL_INVARIANT_ERROR,
                transaction_id=transaction_id,
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
            lifecycle = _lifecycle_evidence(current, evaluated_at=evaluated_at)
            approvals = _approval_retention(
                self.terminal_store,
                run_id,
                payloads,
                evaluated_at=evaluated_at,
            )
            try:
                hold = self.legal_hold_provider.resolve(
                    run_hash,
                    evaluated_at=evaluated_at,
                )
            except Exception:
                return _dry_failure(run_hash, CleanupFailureCode.LEGAL_HOLD)
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
    "cleanup_approval_action_plan",
    "evaluate_cleanup_candidate",
]
