"""Marker-last verified product revision storage for P2-012B."""

from __future__ import annotations

import os
import re
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import ValidationError

from poker_deliberation.approval_canonical import (
    approval_security_audit_event_sha256,
    approval_security_audit_pointer_sha256,
)
from poker_deliberation.approval_canonical import (
    canonical_json_bytes as canonical_approval_json_bytes,
)
from poker_deliberation.approval_canonical import (
    parse_canonical_model as parse_approval_model,
)
from poker_deliberation.approval_models import (
    ApprovalDecisionFailureV2,
    ApprovalFailureCode,
    ApprovalSecurityAuditEventV2,
    ApprovalSecurityAuditPointerV2,
    ApprovalSecurityAuditRateStateV2,
)
from poker_deliberation.approvals import ApprovalDecisionValidationError
from poker_deliberation.budgets.contracts import BudgetPolicyV2
from poker_deliberation.budgets.durable_models import (
    DurableBudgetPolicyV1,
    DurableBudgetStateV1,
    ExecutionActivationV1,
    ExecutionLineageV1,
    MutationStatus,
    OwnerKind,
    ResourceAmountsV1,
    SettlementStatus,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetError,
    DurableBudgetStore,
    build_resource_reservation,
    reservation_request_sha256,
)
from poker_deliberation.budgets.retry import FailureCategory
from poker_deliberation.storage.directory_durability import sync_directory
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_domain_sha256,
    canonical_json_bytes,
    domain_sha256,
    parse_canonical_json,
    platform_adapter,
    run_id_sha256,
    validate_logical_name,
    validate_run_id,
)
from poker_deliberation.storage.revision_lock import (
    LockReleaseError,
    verify_directory,
    verify_regular_single_link,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    DurabilityEvidenceV1,
    RootInitializationRequestV1,
    RunStorageError,
)
from poker_deliberation.storage.revision_store import (
    RunRevisionStore,
    initialize_revision_root,
)
from poker_deliberation.storage.terminal_canonical import (
    UnsupportedTerminalVersion,
    budget_binding_sha256,
    canonical_terminal_bytes,
    completion_marker_sha256,
    current_pointer_sha256,
    lifecycle_audit_sha256,
    manifest_sha256,
    parse_completion_marker,
    parse_current_pointer,
    parse_run_manifest,
    product_payload_commitments,
    required_inventory_sha256,
    terminal_inventory_sha256,
    verify_payload_inventory,
)
from poker_deliberation.storage.terminal_models import (
    BudgetSettlementBindingV2,
    CompletionMarkerV2,
    LegacySourceBindingV2,
    ProductRunError,
    ProductRunFailureCode,
    ProductRunFailureV2,
    RunCurrentPointerV2,
    RunManifestV2,
    RunReadStatus,
    ToolContractVersionV2,
    VerifiedPayloadV2,
    VerifiedRunReadV2,
)

PRODUCT_PRODUCER_ID = "poker-deliberation"
PRODUCT_PRODUCER_VERSION = "0.1.0"
PRODUCT_ROOT_DOMAIN = "poker-product-revision-root-v2"
PRODUCT_TRANSACTION_DOMAIN = "poker-product-transaction-v2"
PRODUCT_BUDGET_ID_DOMAIN = "poker-product-budget-id-v2"
_CURRENT_TEMP = re.compile(r"^current\.txn-[0-9a-f]{32}\.tmp$")
_APPROVAL_AUDIT_EVENT = re.compile(r"^(?P<sequence>[0-9]{4})-(?P<hash>[0-9a-f]{64})\.json$")
APPROVAL_AUDIT_MAX_EVENTS = 1024
APPROVAL_AUDIT_MAX_EVENT_BYTES = 16_384
APPROVAL_AUDIT_MAX_TOTAL_BYTES = 1_048_576
APPROVAL_AUDIT_MAX_FAILED_EVENTS = 32
APPROVAL_AUDIT_WINDOW_SECONDS = 60

Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]
FaultInjector = Callable[[str], None]


def _clock() -> datetime:
    return datetime.now(UTC)


def _id_factory(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"


def _fault(injector: FaultInjector | None, hook: str) -> None:
    if injector is not None:
        injector(hook)


def _idle_durability() -> DurabilityEvidenceV1:
    return DurabilityEvidenceV1(
        platform_adapter=cast(Any, platform_adapter()),
        file_sync="not_attempted",
        directory_sync="not_attempted",
        pointer_replace="not_attempted",
        reconciliation="confirmed",
    )


def _published_durability(directory_sync: str) -> DurabilityEvidenceV1:
    return DurabilityEvidenceV1(
        platform_adapter=cast(Any, platform_adapter()),
        file_sync="confirmed",
        directory_sync=cast(Any, directory_sync),
        pointer_replace="confirmed",
        reconciliation="confirmed",
    )


def _reconciliation_durability(
    *,
    file_sync: Literal["not_attempted", "confirmed", "failed"] = "confirmed",
    directory_sync: Literal[
        "not_attempted", "confirmed", "unavailable", "failed"
    ] = "not_attempted",
    pointer_replace: Literal[
        "not_attempted", "attempted_unconfirmed", "confirmed"
    ] = "not_attempted",
) -> DurabilityEvidenceV1:
    return DurabilityEvidenceV1(
        platform_adapter=cast(Any, platform_adapter()),
        file_sync=file_sync,
        directory_sync=directory_sync,
        pointer_replace=pointer_replace,
        reconciliation="required",
    )


def _failure(
    run_id: str,
    code: ProductRunFailureCode,
    *,
    stage: str,
    read_status: RunReadStatus | None = None,
    transaction_id: str | None = None,
    observed_revision: int | None = None,
    observed_pointer_sha256: str | None = None,
    reconciliation_required: bool = False,
    filesystem_effect: Literal[
        "none",
        "control_only",
        "staging_orphan",
        "unreferenced_revision",
        "current_replace_attempted",
        "current_advanced",
    ] = "none",
    domain_effect: Literal[
        "not_started",
        "current_unchanged",
        "current_may_have_advanced",
        "current_advanced",
    ] = "current_unchanged",
    previous_revision_effect: Literal[
        "not_applicable", "unchanged", "unconfirmed"
    ] = "not_applicable",
    durability_evidence: DurabilityEvidenceV1 | None = None,
) -> ProductRunError:
    return ProductRunError(
        ProductRunFailureV2(
            code=code,
            stage=stage,
            read_status=read_status,
            message_code=code.value,
            retryable=code is ProductRunFailureCode.RUN_LOCKED,
            reconciliation_required=reconciliation_required,
            filesystem_effect=filesystem_effect,
            domain_effect=domain_effect,
            previous_revision_effect=previous_revision_effect,
            run_id_sha256=run_id_sha256(run_id),
            transaction_id=transaction_id,
            observed_revision=observed_revision,
            observed_pointer_sha256=observed_pointer_sha256,
            durability_evidence=durability_evidence,
        )
    )


def _directory_sync(path: Path, injector: FaultInjector | None, hook: str) -> str:
    _fault(injector, f"{hook}.before_directory_sync")
    if os.name == "nt":
        _fault(injector, f"{hook}.directory_sync_unavailable")
        return "unavailable"
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fault(injector, f"{hook}.after_directory_sync")
    return "confirmed"


def _write_exclusive_verified(
    path: Path,
    data: bytes,
    *,
    injector: FaultInjector | None,
    hook: str,
) -> None:
    _fault(injector, f"{hook}.before_open")
    with path.open("xb") as stream:
        _fault(injector, f"{hook}.before_write")
        stream.write(data)
        _fault(injector, f"{hook}.after_write")
        stream.flush()
        _fault(injector, f"{hook}.before_fsync")
        os.fsync(stream.fileno())
        _fault(injector, f"{hook}.after_fsync")
    info = verify_regular_single_link(path)
    if info.st_size != len(data):
        raise CanonicalStorageError("written control size mismatch")
    _fault(injector, f"{hook}.before_reread")
    if path.read_bytes() != data:
        raise CanonicalStorageError("written control bytes mismatch")
    _fault(injector, f"{hook}.after_reread")


def _read_bounded(path: Path, *, max_bytes: int) -> bytes:
    info = verify_regular_single_link(path)
    if info.st_size > max_bytes:
        raise CanonicalStorageError("terminal artifact exceeds configured byte limit")
    data = path.read_bytes()
    if len(data) != info.st_size:
        raise CanonicalStorageError("terminal artifact changed during read")
    return data


def _recognized_directory(path: Path) -> None:
    if not path.exists():
        path.mkdir()
    verify_directory(path)


def _root_id(revision_root: Path, legacy_runs_root: Path) -> str:
    digest = canonical_domain_sha256(
        PRODUCT_ROOT_DOMAIN,
        {
            "revision_root": os.path.normcase(str(revision_root.resolve(strict=False))),
            "legacy_runs_root": os.path.normcase(str(legacy_runs_root.resolve(strict=True))),
        },
    )
    return f"root-{digest[:32]}"


def initialize_terminal_root(
    revision_root: Path,
    legacy_runs_root: Path,
    *,
    initialized_at: datetime,
    root_id: str | None = None,
) -> None:
    """Explicitly initialize the dedicated product terminal root."""

    initialize_revision_root(
        RootInitializationRequestV1(
            revision_root=revision_root,
            legacy_runs_root=legacy_runs_root,
            root_id=root_id or _root_id(revision_root, legacy_runs_root),
            initialized_at=initialized_at,
            producer_id=PRODUCT_PRODUCER_ID,
            producer_version=PRODUCT_PRODUCER_VERSION,
        )
    )


@dataclass(frozen=True, slots=True)
class TerminalPublishRequest:
    run_id: str
    transaction_id: str
    publication_kind: Literal[
        "product_checkpoint",
        "product_terminal",
        "legacy_copy",
    ]
    status: Literal[
        "in_progress",
        "approval_required",
        "succeeded",
        "failed",
        "cancelled",
        "cancel_unconfirmed",
        "legacy_unverified",
    ]
    proposed_revision: int
    expected_revision: int | None
    expected_manifest_sha256: str | None
    expected_pointer_sha256: str | None
    created_at: datetime
    updated_at: datetime
    published_at: datetime
    framework_version: str
    source_commit_id: str
    tool_contract_versions: tuple[ToolContractVersionV2, ...]
    canonical_input_sha256: str
    config_sha256: str
    budget_binding: BudgetSettlementBindingV2
    redaction_policy_sha256: str
    state_checkpoint_sha256: str
    event_head_sha256: str
    approval_lineage_head_sha256: str
    context_lineage_head_sha256: str
    execution_lineage_head_sha256: str
    legacy_source: LegacySourceBindingV2 | None
    lifecycle_audit_sha256: str | None
    payloads: tuple[VerifiedPayloadV2, ...]


@dataclass(frozen=True, slots=True)
class TerminalPublishOutcome:
    outcome_kind: Literal["published", "current_committed"]
    run_id_sha256: str
    transaction_id: str
    revision: int
    manifest_sha256: str
    pointer_sha256: str
    completion_marker_sha256: str | None
    durability_evidence: DurabilityEvidenceV1


@dataclass(frozen=True, slots=True)
class ApprovalFailureAuditRequest:
    run_id: str
    actor_sha256: str
    decision_id_sha256: str
    idempotency_key_sha256: str
    batch_sha256: str | None
    failure_code: ApprovalFailureCode
    observed_run_revision: int | None
    observed_ledger_revision: int | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalFailureAuditOutcome:
    event: ApprovalSecurityAuditEventV2
    pointer: ApprovalSecurityAuditPointerV2
    pointer_sha256: str
    directory_sync: Literal["confirmed", "unavailable"]


class ApprovalFailureAuditError(ValueError):
    def __init__(self, failure: ApprovalDecisionFailureV2) -> None:
        self.failure = failure
        super().__init__(failure.code.value)


class SettlementVerifier(Protocol):
    def reserve(
        self,
        request: TerminalPublishRequest,
        *,
        artifact_bytes: int,
        run_bytes: int,
    ) -> None: ...

    def settle(
        self,
        request: TerminalPublishRequest,
        *,
        pointer_sha256: str,
        effect_evidence_sha256: str,
        artifact_bytes: int,
        run_bytes: int,
    ) -> None: ...

    def release_no_effect(
        self,
        request: TerminalPublishRequest,
        *,
        evidence_sha256: str,
    ) -> None: ...

    def verify(
        self,
        run_id: str,
        binding: BudgetSettlementBindingV2,
        *,
        pointer_sha256: str,
        effect_evidence_sha256: str,
    ) -> bool: ...


class DurableBudgetCoordinator:
    """Compose the completed P2-011B API without changing its schemas."""

    def __init__(
        self,
        store: DurableBudgetStore,
        policy: BudgetPolicyV2,
    ) -> None:
        self.store = store
        self.policy = DurableBudgetPolicyV1(
            base_policy=policy,
            activation=ExecutionActivationV1(
                max_concurrent_agents=1,
                max_automatic_retries=0,
            ),
        )

    @staticmethod
    def _ids(request: TerminalPublishRequest) -> dict[str, str]:
        digest = domain_sha256(
            PRODUCT_BUDGET_ID_DOMAIN,
            (
                request.run_id
                + "\0"
                + request.transaction_id
                + "\0"
                + str(request.proposed_revision)
            ).encode("utf-8"),
        )
        return {
            "initialize": f"initialize-{digest[:24]}",
            "reservation": request.budget_binding.reservation_operation_id,
            "permit": request.budget_binding.permit_id,
            "start": f"start-{digest[:24]}",
            "settlement": request.budget_binding.settlement_operation_id,
            "settlement_id": request.budget_binding.settlement_id,
        }

    def _state(self, request: TerminalPublishRequest) -> DurableBudgetStateV1:
        ids = self._ids(request)
        try:
            self.store.create(
                request.run_id,
                self.policy,
                operation_id=ids["initialize"],
            )
        except DurableBudgetError as exc:
            if exc.failure.code.value != "idempotency_conflict":
                raise
        state = self.store.load(request.run_id)
        if (
            state.policy_sha256 != request.budget_binding.budget_policy_sha256
            or run_id_sha256(state.run_id) != request.budget_binding.budget_run_id_sha256
        ):
            raise ValueError("durable budget binding identity mismatch")
        return state

    def reserve(
        self,
        request: TerminalPublishRequest,
        *,
        artifact_bytes: int,
        run_bytes: int,
    ) -> None:
        state = self._state(request)
        self.store.rebase_monotonic_clock(request.run_id)
        ids = self._ids(request)
        requested = ResourceAmountsV1(
            active_runtime_ns=state.active_runtime_remaining_ns,
            artifact_bytes=artifact_bytes,
            run_bytes=run_bytes,
            concurrency_slots=1,
        )
        reservation = build_resource_reservation(
            reservation_id=ids["reservation"],
            requested=requested,
        )
        if reservation.request_sha256 != request.budget_binding.reservation_request_sha256:
            raise ValueError("durable reservation request hash mismatch")
        seed = canonical_domain_sha256(
            PRODUCT_BUDGET_ID_DOMAIN,
            {
                "run_id": request.run_id,
                "transaction_id": request.transaction_id,
                "revision": request.proposed_revision,
            },
        )
        lineage = ExecutionLineageV1(
            owner_kind=OwnerKind.ORCHESTRATOR,
            owner_id="product-terminal-store",
            role="orchestrator",
            phase_id="terminal_publish",
            root_attempt_id=f"attempt-{seed[:24]}",
            attempt_id=f"attempt-{seed[:24]}",
            root_context_id=f"context-{seed[24:48]}",
            context_id=f"context-{seed[24:48]}",
            context_source_sha256=request.canonical_input_sha256,
            context_policy_sha256=request.redaction_policy_sha256,
            context_integrity_sha256=request.state_checkpoint_sha256,
            execution_ordinal=request.proposed_revision - 1,
            idempotency_key=request.transaction_id,
            idempotency_request_sha256=canonical_domain_sha256(
                PRODUCT_TRANSACTION_DOMAIN,
                {
                    "run_id": request.run_id,
                    "transaction_id": request.transaction_id,
                    "inventory": tuple(payload.inventory for payload in request.payloads),
                },
            ),
        )
        self.store.reserve(
            request.run_id,
            operation_id=ids["reservation"],
            permit_id=ids["permit"],
            reservation=reservation,
            lineage=lineage,
        )
        self.store.rebase_monotonic_clock(request.run_id)
        self.store.start(
            request.run_id,
            operation_id=ids["start"],
            permit_id=ids["permit"],
        )

    def freeze_binding(
        self,
        request: TerminalPublishRequest,
        *,
        artifact_bytes: int,
        run_bytes: int,
    ) -> BudgetSettlementBindingV2:
        """Freeze the exact reservation hash before product-root mutation."""

        state = self._state(request)
        requested = ResourceAmountsV1(
            active_runtime_ns=state.active_runtime_remaining_ns,
            artifact_bytes=artifact_bytes,
            run_bytes=run_bytes,
            concurrency_slots=1,
        )
        binding = default_budget_binding(
            request.run_id,
            request.transaction_id,
            self.policy.base_policy,
            requested=requested,
        )
        self.store.rebase_monotonic_clock(request.run_id)
        return binding

    def settle(
        self,
        request: TerminalPublishRequest,
        *,
        pointer_sha256: str,
        effect_evidence_sha256: str,
        artifact_bytes: int,
        run_bytes: int,
    ) -> None:
        ids = self._ids(request)
        status = {
            "succeeded": SettlementStatus.SUCCEEDED,
            "in_progress": SettlementStatus.SUCCEEDED,
            "approval_required": SettlementStatus.SUCCEEDED,
            "failed": SettlementStatus.FAILED,
            "cancelled": SettlementStatus.CANCELLED,
            "cancel_unconfirmed": SettlementStatus.FAILED,
            "legacy_unverified": SettlementStatus.FAILED,
        }[request.status]
        failure_category = (
            None if status is SettlementStatus.SUCCEEDED else FailureCategory.INTERNAL
        )
        self.store.settle(
            request.run_id,
            operation_id=ids["settlement"],
            settlement_id=ids["settlement_id"],
            permit_id=ids["permit"],
            actual=ResourceAmountsV1(
                artifact_bytes=artifact_bytes,
                run_bytes=run_bytes,
                concurrency_slots=1,
            ),
            status=status,
            result_sha256=pointer_sha256,
            effect_evidence_sha256=effect_evidence_sha256,
            failure_category=failure_category,
            observed_peak_concurrency=1,
        )

    def release_no_effect(
        self,
        request: TerminalPublishRequest,
        *,
        evidence_sha256: str,
    ) -> None:
        ids = self._ids(request)
        try:
            self.store.release_no_effect(
                request.run_id,
                operation_id=ids["settlement"],
                settlement_id=ids["settlement_id"],
                permit_id=ids["permit"],
                evidence_sha256=evidence_sha256,
            )
        except DurableBudgetError:
            raise

    def verify(
        self,
        run_id: str,
        binding: BudgetSettlementBindingV2,
        *,
        pointer_sha256: str,
        effect_evidence_sha256: str,
    ) -> bool:
        try:
            state = self.store.load(run_id)
            if (
                state.policy_sha256 != binding.budget_policy_sha256
                or run_id_sha256(run_id) != binding.budget_run_id_sha256
            ):
                return False
            settlement = next(
                item for item in state.settlements if item.settlement_id == binding.settlement_id
            )
            reserve_operation = next(
                item
                for item in state.operations
                if item.operation_id == binding.reservation_operation_id
            )
            settle_operation = next(
                item
                for item in state.operations
                if item.operation_id == binding.settlement_operation_id
            )
            request_hash = reservation_request_sha256(
                reservation_id=binding.reservation_operation_id,
                requested=settlement.reserved,
            )
            return (
                request_hash == binding.reservation_request_sha256
                and reserve_operation.subject_id == binding.permit_id
                and settle_operation.subject_id == binding.settlement_id
                and settlement.permit_id == binding.permit_id
                and settlement.operation_id == binding.settlement_operation_id
                and settlement.result_sha256 == pointer_sha256
                and settlement.effect_evidence_sha256 == effect_evidence_sha256
                and settlement.status
                in {
                    SettlementStatus.SUCCEEDED,
                    SettlementStatus.FAILED,
                    SettlementStatus.CANCELLED,
                }
                and not state.failure_latch
                and reserve_operation.outcome.value in {MutationStatus.APPLIED.value, "applied"}
            )
        except (DurableBudgetError, StopIteration, ValueError):
            return False


@dataclass(frozen=True, slots=True)
class _Prepared:
    request: TerminalPublishRequest
    manifest: RunManifestV2
    manifest_bytes: bytes
    manifest_sha256: str
    marker: CompletionMarkerV2 | None
    marker_bytes: bytes | None
    marker_sha256: str | None
    pointer: RunCurrentPointerV2
    pointer_bytes: bytes
    pointer_sha256: str
    transaction_bytes: bytes
    artifact_bytes: int
    persistent_delta: int


class TerminalRunStore:
    """Side-effect-free handle until ``initialize`` or ``publish`` is called."""

    def __init__(
        self,
        revision_root: Path,
        legacy_runs_root: Path,
        *,
        budget: SettlementVerifier,
        max_artifact_bytes: int = 1_000_000,
        max_run_bytes: int = 10_000_000,
        clock: Clock = _clock,
        id_factory: IdFactory = _id_factory,
        fault_injector: FaultInjector | None = None,
        framework_version: str = "0.1.0",
        source_commit_id: str = "0" * 64,
    ) -> None:
        self.foundation = RunRevisionStore(
            revision_root,
            legacy_runs_root,
            max_artifact_bytes=max_artifact_bytes,
            max_run_bytes=max_run_bytes,
            clock=clock,
            id_factory=id_factory,
            fault_injector=fault_injector,
            producer_id=PRODUCT_PRODUCER_ID,
            producer_version=PRODUCT_PRODUCER_VERSION,
        )
        self.revision_root = self.foundation.revision_root
        self.legacy_runs_root = self.foundation.legacy_runs_root
        self.budget = budget
        self.max_artifact_bytes = max_artifact_bytes
        self.max_run_bytes = max_run_bytes
        self.clock = clock
        self.id_factory = id_factory
        self.fault_injector = fault_injector
        self.framework_version = framework_version
        self.source_commit_id = source_commit_id

    @property
    def runs_root(self) -> Path:
        return self.revision_root / "runs"

    def _base_budget_policy(self) -> BudgetPolicyV2 | None:
        durable_policy = getattr(self.budget, "policy", None)
        base_policy = getattr(durable_policy, "base_policy", None)
        if base_policy is None:
            return None
        return BudgetPolicyV2.model_validate(base_policy.model_dump(mode="python"), strict=True)

    def planned_payload_path(
        self,
        run_id: str,
        *,
        revision: int,
        transaction_id: str,
        logical_name: str,
    ) -> Path:
        """Return the confined immutable payload path for a frozen publication."""

        validate_run_id(run_id)
        if revision < 1:
            raise ValueError("revision must be positive")
        if re.fullmatch(r"txn-[0-9a-f]{32}", transaction_id) is None:
            raise ValueError("invalid terminal transaction ID")
        logical = validate_logical_name(logical_name)
        return self._paths(run_id)[3] / f"r{revision}-{transaction_id}" / "payload" / Path(logical)

    def initialize(self, *, initialized_at: datetime | None = None) -> None:
        initialize_terminal_root(
            self.revision_root,
            self.legacy_runs_root,
            initialized_at=initialized_at or self.clock(),
        )

    def freeze_budget_binding(
        self,
        request: TerminalPublishRequest,
    ) -> TerminalPublishRequest:
        """Resolve the self-size-stable P2-011B binding before publication."""

        if not isinstance(self.budget, DurableBudgetCoordinator):
            return request
        provisional = self._prepare(request)
        try:
            binding = self.budget.freeze_binding(
                request,
                artifact_bytes=provisional.artifact_bytes,
                run_bytes=provisional.persistent_delta,
            )
        except DurableBudgetError as exc:
            code = (
                ProductRunFailureCode.RUN_LOCKED
                if exc.failure.code.value in {"run_locked", "concurrency_exceeded", "cas_conflict"}
                else ProductRunFailureCode.BUDGET_RESERVATION_FAILED
            )
            raise _failure(
                request.run_id,
                code,
                stage="budget_freeze",
                transaction_id=request.transaction_id,
                previous_revision_effect=(
                    "not_applicable" if request.proposed_revision == 1 else "unchanged"
                ),
            ) from exc
        frozen = replace(request, budget_binding=binding)
        final = self._prepare(frozen)
        if (
            final.artifact_bytes != provisional.artifact_bytes
            or final.persistent_delta != provisional.persistent_delta
        ):
            raise _failure(
                request.run_id,
                ProductRunFailureCode.INTERNAL_INVARIANT_ERROR,
                stage="budget_freeze",
                transaction_id=request.transaction_id,
                previous_revision_effect=(
                    "not_applicable" if request.proposed_revision == 1 else "unchanged"
                ),
            )
        return frozen

    def _paths(self, run_id: str) -> tuple[Path, Path, Path, Path]:
        run = self.runs_root / run_id
        control = run / ".terminal-store"
        return (
            run,
            control,
            control / "transactions",
            control / "revisions",
        )

    def _scan_aliases(self, run_id: str) -> None:
        expected = run_id.lower()
        for root in (self.runs_root, self.legacy_runs_root):
            verify_directory(root)
            for sibling in root.iterdir():
                if sibling.name.lower() != expected:
                    continue
                if root == self.legacy_runs_root or sibling.name != run_id:
                    raise _failure(
                        run_id,
                        ProductRunFailureCode.CROSS_RUN_MISMATCH,
                        stage="namespace",
                    )

    def _verify_namespace(self, run_id: str) -> None:
        run, control, transactions, revisions = self._paths(run_id)
        if not run.exists():
            return
        verify_directory(run)
        entries = {item.name: item for item in run.iterdir()}
        if set(entries) != {".terminal-store"}:
            raise CanonicalStorageError("mixed or unknown product run namespace")
        verify_directory(control)
        control_entries = {item.name: item for item in control.iterdir()}
        unknown = {
            name
            for name in control_entries
            if name
            not in {
                "transactions",
                "revisions",
                "current.json",
                "approval-failure-audit",
            }
            and _CURRENT_TEMP.fullmatch(name) is None
        }
        if unknown:
            raise CanonicalStorageError("unknown terminal control entry")
        for name, item in control_entries.items():
            if _CURRENT_TEMP.fullmatch(name) is not None:
                verify_regular_single_link(item)
        if transactions.exists():
            verify_directory(transactions)
            for item in transactions.iterdir():
                verify_directory(item)
        if revisions.exists():
            verify_directory(revisions)
            for item in revisions.iterdir():
                verify_directory(item)
        current = control / "current.json"
        if current.exists():
            verify_regular_single_link(current)
        approval_audit = control / "approval-failure-audit"
        if approval_audit.exists():
            verify_directory(approval_audit)
            audit_entries = {item.name: item for item in approval_audit.iterdir()}
            if set(audit_entries) - {"events", "current.json", "current.tmp"}:
                raise CanonicalStorageError("unknown approval failure audit entry")
            audit_events_root = audit_entries.get("events")
            if audit_events_root is not None:
                verify_directory(audit_events_root)
                for item in audit_events_root.iterdir():
                    if _APPROVAL_AUDIT_EVENT.fullmatch(item.name) is None:
                        raise CanonicalStorageError("unknown approval failure audit event")
                    verify_regular_single_link(item)
            for name in ("current.json", "current.tmp"):
                audit_item = audit_entries.get(name)
                if audit_item is not None:
                    verify_regular_single_link(audit_item)

    def _bootstrap_namespace(self, run_id: str) -> tuple[Path, Path, Path, Path]:
        run, control, transactions, revisions = self._paths(run_id)
        if not run.exists():
            run.mkdir()
        verify_directory(run)
        if any(run.iterdir()) and not control.exists():
            raise CanonicalStorageError("product run namespace is nonempty and unowned")
        _recognized_directory(control)
        _recognized_directory(transactions)
        _recognized_directory(revisions)
        self._verify_namespace(run_id)
        return run, control, transactions, revisions

    def _sync_bootstrap_namespace(self, run_id: str) -> None:
        """Durably commit the complete empty product namespace before tool execution."""

        run, control, transactions, revisions = self._paths(run_id)
        self._verify_namespace(run_id)
        for label, path in (
            ("transactions", transactions),
            ("revisions", revisions),
            ("control", control),
            ("run", run),
            ("runs_root", self.runs_root),
            ("revision_root", self.revision_root),
            ("revision_root_parent", self.revision_root.parent),
        ):
            sync_directory(
                path,
                injector=self.fault_injector,
                hook=f"bootstrap_namespace.{label}",
            )

    def _prepare(self, request: TerminalPublishRequest) -> _Prepared:
        try:
            validate_run_id(request.run_id)
            if not request.payloads:
                raise CanonicalStorageError("terminal publish requires payloads")
            if request.proposed_revision < 1:
                raise CanonicalStorageError("invalid proposed revision")
            if request.proposed_revision == 1:
                if any(
                    value is not None
                    for value in (
                        request.expected_revision,
                        request.expected_manifest_sha256,
                        request.expected_pointer_sha256,
                    )
                ):
                    raise CanonicalStorageError("initial revision expectations mismatch")
            elif (
                request.expected_revision != request.proposed_revision - 1
                or request.expected_manifest_sha256 is None
                or request.expected_pointer_sha256 is None
            ):
                raise CanonicalStorageError("successor revision expectations mismatch")
            payload_map = {
                payload.inventory.logical_name: payload.exact_bytes for payload in request.payloads
            }
            if len(payload_map) != len(request.payloads):
                raise CanonicalStorageError("duplicate terminal payload")
            allow_opaque = request.publication_kind == "legacy_copy"
            inventory = verify_payload_inventory(
                tuple(payload.inventory for payload in request.payloads),
                payload_map,
                allow_opaque=allow_opaque,
            )
            if request.publication_kind != "legacy_copy":
                commitments = product_payload_commitments(
                    payload_map,
                    run_id=request.run_id,
                    status=request.status,
                    revision=request.proposed_revision,
                    revision_root=self.revision_root,
                    transaction_id=request.transaction_id,
                    previous_manifest_sha256=request.expected_manifest_sha256,
                    previous_pointer_sha256=request.expected_pointer_sha256,
                    maximum_admission_record_bytes=self.max_artifact_bytes,
                    budget_policy=self._base_budget_policy(),
                )
                if commitments != (
                    request.canonical_input_sha256,
                    request.state_checkpoint_sha256,
                    request.event_head_sha256,
                    request.approval_lineage_head_sha256,
                    request.context_lineage_head_sha256,
                    request.execution_lineage_head_sha256,
                ):
                    raise CanonicalStorageError("product payload commitment mismatch")
            lifecycle_bytes = payload_map.get("lifecycle_audit.json")
            if request.publication_kind == "product_terminal":
                if (
                    lifecycle_bytes is None
                    or request.lifecycle_audit_sha256 != lifecycle_audit_sha256(lifecycle_bytes)
                ):
                    raise CanonicalStorageError("terminal lifecycle audit payload mismatch")
            elif lifecycle_bytes is not None or request.lifecycle_audit_sha256 is not None:
                raise CanonicalStorageError(
                    "nonterminal revision cannot carry terminal lifecycle audit"
                )
            inventory_digest = terminal_inventory_sha256(inventory)
            manifest = RunManifestV2(
                publication_kind=request.publication_kind,
                run_id=request.run_id,
                revision=request.proposed_revision,
                transaction_id=request.transaction_id,
                previous_revision=request.expected_revision,
                previous_manifest_sha256=request.expected_manifest_sha256,
                expected_pointer_sha256=request.expected_pointer_sha256,
                created_at=request.created_at,
                updated_at=request.updated_at,
                producer_id=PRODUCT_PRODUCER_ID,
                producer_version=PRODUCT_PRODUCER_VERSION,
                framework_version=request.framework_version,
                source_commit_id=request.source_commit_id,
                tool_contract_versions=request.tool_contract_versions,
                status=request.status,
                canonical_input_sha256=request.canonical_input_sha256,
                config_sha256=request.config_sha256,
                budget_policy_sha256=request.budget_binding.budget_policy_sha256,
                budget_binding=request.budget_binding,
                redaction_policy_sha256=request.redaction_policy_sha256,
                local_data_policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
                state_checkpoint_sha256=request.state_checkpoint_sha256,
                event_head_sha256=request.event_head_sha256,
                approval_lineage_head_sha256=request.approval_lineage_head_sha256,
                context_lineage_head_sha256=request.context_lineage_head_sha256,
                execution_lineage_head_sha256=request.execution_lineage_head_sha256,
                legacy_source=request.legacy_source,
                inventory_sha256=inventory_digest,
                lifecycle_audit_sha256=request.lifecycle_audit_sha256,
                artifacts=inventory,
            )
            manifest_bytes = canonical_terminal_bytes(manifest)
            manifest_digest = manifest_sha256(manifest_bytes)
            marker: CompletionMarkerV2 | None = None
            marker_bytes: bytes | None = None
            marker_digest: str | None = None
            if request.publication_kind == "product_terminal":
                if request.lifecycle_audit_sha256 is None:
                    raise CanonicalStorageError("terminal publication lacks lifecycle audit")
                marker = CompletionMarkerV2(
                    run_id=request.run_id,
                    terminal_revision=request.proposed_revision,
                    terminal_transaction_id=request.transaction_id,
                    terminal_status=cast(Any, request.status),
                    terminal_manifest_sha256=manifest_digest,
                    required_inventory_sha256=required_inventory_sha256(inventory),
                    budget_binding_sha256=budget_binding_sha256(request.budget_binding),
                    lifecycle_audit_sha256=request.lifecycle_audit_sha256,
                    published_at=request.published_at,
                )
                marker_bytes = canonical_terminal_bytes(marker)
                marker_digest = completion_marker_sha256(marker_bytes)
            pointer = RunCurrentPointerV2(
                publication_kind=request.publication_kind,
                run_id=request.run_id,
                revision=request.proposed_revision,
                transaction_id=request.transaction_id,
                revision_relative_path=(
                    f"revisions/r{request.proposed_revision}-{request.transaction_id}"
                ),
                status=request.status,
                manifest_sha256=manifest_digest,
                inventory_sha256=inventory_digest,
                completion_marker_sha256=marker_digest,
                published_at=request.published_at,
            )
            pointer_bytes = canonical_terminal_bytes(pointer)
            pointer_digest = current_pointer_sha256(pointer_bytes)
            transaction = {
                "schema_version": "2.0.0",
                "storage_protocol": "poker-run-terminal-v2",
                "canonicalization": "poker-run-storage-json-v1",
                "hash_algorithm": "sha256",
                "run_id_sha256": run_id_sha256(request.run_id),
                "transaction_id": request.transaction_id,
                "proposed_revision": request.proposed_revision,
                "expected_revision": request.expected_revision,
                "expected_manifest_sha256": request.expected_manifest_sha256,
                "expected_pointer_sha256": request.expected_pointer_sha256,
                "inventory_sha256": inventory_digest,
                "manifest_sha256": manifest_digest,
                "completion_marker_sha256": marker_digest,
            }
            transaction["request_sha256"] = canonical_domain_sha256(
                PRODUCT_TRANSACTION_DOMAIN, transaction
            )
            transaction_bytes = canonical_json_bytes(transaction)
            sizes = [
                len(transaction_bytes),
                len(manifest_bytes),
                len(pointer_bytes),
                *(len(payload.exact_bytes) for payload in request.payloads),
            ]
            if marker_bytes is not None:
                sizes.append(len(marker_bytes))
            if any(size > self.max_artifact_bytes for size in sizes):
                raise CanonicalStorageError("terminal artifact exceeds configured limit")
            persistent_delta = (
                len(transaction_bytes)
                + len(manifest_bytes)
                + len(pointer_bytes)
                + sum(len(payload.exact_bytes) for payload in request.payloads)
                + (0 if marker_bytes is None else len(marker_bytes))
            )
            if persistent_delta > self.max_run_bytes:
                raise CanonicalStorageError("terminal revision exceeds run byte limit")
            return _Prepared(
                request=request,
                manifest=manifest,
                manifest_bytes=manifest_bytes,
                manifest_sha256=manifest_digest,
                marker=marker,
                marker_bytes=marker_bytes,
                marker_sha256=marker_digest,
                pointer=pointer,
                pointer_bytes=pointer_bytes,
                pointer_sha256=pointer_digest,
                transaction_bytes=transaction_bytes,
                artifact_bytes=max(sizes),
                persistent_delta=persistent_delta,
            )
        except (CanonicalStorageError, ValidationError, ValueError) as exc:
            raise _failure(
                request.run_id,
                ProductRunFailureCode.ARTIFACT_SCHEMA_ERROR,
                stage="preflight",
                transaction_id=request.transaction_id,
                previous_revision_effect=(
                    "not_applicable" if request.proposed_revision == 1 else "unchanged"
                ),
            ) from exc

    def _read_revision(
        self,
        run_id: str,
        pointer: RunCurrentPointerV2,
        *,
        verify_budget: bool,
    ) -> tuple[
        RunManifestV2,
        bytes,
        CompletionMarkerV2 | None,
        bytes | None,
        tuple[VerifiedPayloadV2, ...],
    ]:
        _run, control, _transactions, _revisions = self._paths(run_id)
        revision = control / pointer.revision_relative_path
        verify_directory(revision)
        allowed = {"transaction.json", "manifest.json", "payload"}
        if pointer.publication_kind == "product_terminal":
            allowed.add("completion.json")
        if set(item.name for item in revision.iterdir()) != allowed:
            raise CanonicalStorageError("terminal revision membership mismatch")
        transaction_bytes = _read_bounded(
            revision / "transaction.json", max_bytes=self.max_artifact_bytes
        )
        transaction = cast(dict[str, object], parse_canonical_json(transaction_bytes))
        request_sha = transaction.pop("request_sha256", None)
        if (
            not isinstance(request_sha, str)
            or request_sha != canonical_domain_sha256(PRODUCT_TRANSACTION_DOMAIN, transaction)
            or transaction.get("run_id_sha256") != run_id_sha256(run_id)
            or transaction.get("transaction_id") != pointer.transaction_id
            or transaction.get("proposed_revision") != pointer.revision
        ):
            raise CanonicalStorageError("terminal transaction mismatch")
        manifest_bytes = _read_bounded(
            revision / "manifest.json", max_bytes=self.max_artifact_bytes
        )
        if manifest_sha256(manifest_bytes) != pointer.manifest_sha256:
            raise CanonicalStorageError("terminal manifest hash mismatch")
        manifest = parse_run_manifest(manifest_bytes)
        if (
            manifest.run_id != run_id
            or manifest.revision != pointer.revision
            or manifest.transaction_id != pointer.transaction_id
            or manifest.publication_kind != pointer.publication_kind
            or manifest.status != pointer.status
            or manifest.inventory_sha256 != pointer.inventory_sha256
        ):
            raise CanonicalStorageError("terminal pointer/manifest mismatch")
        payload_root = revision / "payload"
        verify_directory(payload_root)
        payloads: dict[str, bytes] = {}
        expected_paths = {
            entry.revision_relative_path.removeprefix("payload/"): entry
            for entry in manifest.artifacts
        }
        for item in payload_root.rglob("*"):
            if item.is_dir():
                verify_directory(item)
                continue
            verify_regular_single_link(item)
            relative = item.relative_to(payload_root).as_posix()
            if relative not in expected_paths:
                raise CanonicalStorageError("unexpected terminal payload")
            payloads[relative] = _read_bounded(item, max_bytes=self.max_artifact_bytes)
        entries = verify_payload_inventory(
            manifest.artifacts,
            payloads,
            allow_opaque=manifest.publication_kind == "legacy_copy",
        )
        if terminal_inventory_sha256(entries) != manifest.inventory_sha256:
            raise CanonicalStorageError("terminal inventory hash mismatch")
        if manifest.publication_kind != "legacy_copy":
            commitments = product_payload_commitments(
                payloads,
                run_id=run_id,
                status=manifest.status,
                revision=manifest.revision,
                revision_root=self.revision_root,
                transaction_id=manifest.transaction_id,
                previous_manifest_sha256=manifest.previous_manifest_sha256,
                previous_pointer_sha256=manifest.expected_pointer_sha256,
                maximum_admission_record_bytes=self.max_artifact_bytes,
                budget_policy=self._base_budget_policy(),
            )
            if commitments != (
                manifest.canonical_input_sha256,
                manifest.state_checkpoint_sha256,
                manifest.event_head_sha256,
                manifest.approval_lineage_head_sha256,
                manifest.context_lineage_head_sha256,
                manifest.execution_lineage_head_sha256,
            ):
                raise CanonicalStorageError("terminal payload commitment mismatch")
        marker: CompletionMarkerV2 | None = None
        marker_bytes: bytes | None = None
        if pointer.publication_kind == "product_terminal":
            marker_bytes = _read_bounded(
                revision / "completion.json", max_bytes=self.max_artifact_bytes
            )
            if (
                pointer.completion_marker_sha256 is None
                or completion_marker_sha256(marker_bytes) != pointer.completion_marker_sha256
            ):
                raise CanonicalStorageError("completion marker hash mismatch")
            marker = parse_completion_marker(marker_bytes)
            if (
                marker.run_id != run_id
                or marker.terminal_revision != pointer.revision
                or marker.terminal_transaction_id != pointer.transaction_id
                or marker.terminal_status != pointer.status
                or marker.terminal_manifest_sha256 != pointer.manifest_sha256
                or marker.required_inventory_sha256 != required_inventory_sha256(entries)
                or marker.budget_binding_sha256 != budget_binding_sha256(manifest.budget_binding)
                or marker.lifecycle_audit_sha256 != manifest.lifecycle_audit_sha256
                or "lifecycle_audit.json" not in payloads
                or marker.lifecycle_audit_sha256
                != lifecycle_audit_sha256(payloads["lifecycle_audit.json"])
            ):
                raise CanonicalStorageError("completion marker correlation mismatch")
        evidence_sha = (
            completion_marker_sha256(marker_bytes)
            if marker_bytes is not None
            else manifest_sha256(manifest_bytes)
        )
        if verify_budget and not self.budget.verify(
            run_id,
            manifest.budget_binding,
            pointer_sha256=current_pointer_sha256(pointer),
            effect_evidence_sha256=evidence_sha,
        ):
            raise _failure(
                run_id,
                ProductRunFailureCode.BUDGET_SETTLEMENT_FAILED,
                stage="budget_verify",
                read_status=RunReadStatus.INCOMPLETE,
                observed_revision=pointer.revision,
                observed_pointer_sha256=current_pointer_sha256(pointer),
                reconciliation_required=True,
                filesystem_effect="current_advanced",
                domain_effect="current_advanced",
                previous_revision_effect=(
                    "not_applicable" if pointer.revision == 1 else "unchanged"
                ),
            )
        verified_payloads = tuple(
            VerifiedPayloadV2(
                inventory=entry,
                exact_bytes=payloads[entry.logical_name],
            )
            for entry in entries
        )
        return manifest, manifest_bytes, marker, marker_bytes, verified_payloads

    def read_current(
        self,
        run_id: str,
        *,
        verify_budget: bool = True,
    ) -> VerifiedRunReadV2:
        try:
            validate_run_id(run_id)
            self.foundation._ownership(run_id)
            self._scan_aliases(run_id)
            self._verify_namespace(run_id)
            _run, control, transactions, revisions = self._paths(run_id)
            current = control / "current.json"
            if not current.exists():
                if (revisions.exists() and any(revisions.iterdir())) or (
                    transactions.exists() and any(transactions.iterdir())
                ):
                    raise _failure(
                        run_id,
                        ProductRunFailureCode.RUN_INCOMPLETE,
                        stage="read_pointer",
                        read_status=RunReadStatus.INCOMPLETE,
                        reconciliation_required=True,
                    )
                raise _failure(
                    run_id,
                    ProductRunFailureCode.RUN_NOT_FOUND,
                    stage="read_pointer",
                )
            first_pointer_bytes = _read_bounded(current, max_bytes=self.max_artifact_bytes)
            pointer = parse_current_pointer(first_pointer_bytes)
            if pointer.run_id != run_id:
                raise CanonicalStorageError("current pointer cross-run mismatch")
            (
                manifest,
                manifest_bytes,
                marker,
                marker_bytes,
                payloads,
            ) = self._read_revision(
                run_id,
                pointer,
                verify_budget=verify_budget,
            )
            reachable = [pointer.revision]
            child_manifest = manifest
            for revision_number in range(pointer.revision - 1, 0, -1):
                transaction_id = None
                for candidate in revisions.iterdir():
                    prefix = f"r{revision_number}-"
                    if candidate.name.startswith(prefix):
                        if transaction_id is not None:
                            raise CanonicalStorageError(
                                "multiple terminal revisions share one ordinal"
                            )
                        transaction_id = candidate.name[len(prefix) :]
                if transaction_id is None:
                    raise CanonicalStorageError("terminal lineage revision missing")
                previous_manifest_path = (
                    revisions / f"r{revision_number}-{transaction_id}" / "manifest.json"
                )
                previous_bytes = _read_bounded(
                    previous_manifest_path, max_bytes=self.max_artifact_bytes
                )
                previous = parse_run_manifest(previous_bytes)
                if (
                    previous.run_id != run_id
                    or previous.revision != revision_number
                    or previous.transaction_id != transaction_id
                    or manifest_sha256(previous_bytes) != child_manifest.previous_manifest_sha256
                ):
                    raise CanonicalStorageError("terminal lineage manifest mismatch")
                previous_marker_path = previous_manifest_path.with_name("completion.json")
                previous_marker_sha: str | None = None
                if previous.publication_kind == "product_terminal":
                    previous_marker_bytes = _read_bounded(
                        previous_marker_path, max_bytes=self.max_artifact_bytes
                    )
                    previous_marker_sha = completion_marker_sha256(previous_marker_bytes)
                reconstructed = RunCurrentPointerV2(
                    publication_kind=previous.publication_kind,
                    run_id=run_id,
                    revision=previous.revision,
                    transaction_id=previous.transaction_id,
                    revision_relative_path=(
                        f"revisions/r{previous.revision}-{previous.transaction_id}"
                    ),
                    status=previous.status,
                    manifest_sha256=manifest_sha256(previous_bytes),
                    inventory_sha256=previous.inventory_sha256,
                    completion_marker_sha256=previous_marker_sha,
                    published_at=previous.updated_at,
                )
                if (
                    current_pointer_sha256(reconstructed) != child_manifest.expected_pointer_sha256
                    or child_manifest.previous_revision != revision_number
                ):
                    raise CanonicalStorageError("terminal pointer lineage mismatch")
                (
                    verified_previous,
                    _verified_previous_bytes,
                    _verified_previous_marker,
                    _verified_previous_marker_bytes,
                    _verified_previous_payloads,
                ) = self._read_revision(
                    run_id,
                    reconstructed,
                    verify_budget=verify_budget,
                )
                if verified_previous != previous:
                    raise CanonicalStorageError(
                        "terminal lineage verification changed manifest identity"
                    )
                child_manifest = verified_previous
                reachable.append(revision_number)
            second_pointer_bytes = _read_bounded(current, max_bytes=self.max_artifact_bytes)
            if second_pointer_bytes != first_pointer_bytes:
                raise _failure(
                    run_id,
                    ProductRunFailureCode.RUN_CONFLICT,
                    stage="read_stability",
                    observed_revision=pointer.revision,
                    observed_pointer_sha256=current_pointer_sha256(pointer),
                )
            marker_digest = None if marker_bytes is None else completion_marker_sha256(marker_bytes)
            return VerifiedRunReadV2(
                read_status=RunReadStatus(manifest.status),
                run_id=run_id,
                revision=pointer.revision,
                transaction_id=pointer.transaction_id,
                current_pointer_sha256=current_pointer_sha256(first_pointer_bytes),
                manifest_sha256=manifest_sha256(manifest_bytes),
                inventory_sha256=manifest.inventory_sha256,
                completion_marker_sha256=marker_digest,
                resume_eligible=manifest.status in {"in_progress", "approval_required"},
                lifecycle_verified=manifest.publication_kind == "product_terminal",
                reachable_revisions=tuple(reachable),
                pointer=pointer,
                manifest=manifest,
                completion_marker=marker,
                payloads=payloads,
            )
        except ProductRunError:
            raise
        except UnsupportedTerminalVersion as exc:
            raise _failure(
                run_id,
                ProductRunFailureCode.UNSUPPORTED_RUN_VERSION,
                stage="read_version",
                read_status=RunReadStatus.UNSUPPORTED_VERSION,
            ) from exc
        except (CanonicalStorageError, OSError, ValidationError) as exc:
            raise _failure(
                run_id,
                ProductRunFailureCode.RUN_CORRUPT,
                stage="read_verify",
                read_status=RunReadStatus.CORRUPT,
                reconciliation_required=True,
            ) from exc
        except RunStorageError as exc:
            raise _failure(
                run_id,
                ProductRunFailureCode.RUN_INCOMPLETE,
                stage="root_verify",
                read_status=RunReadStatus.INCOMPLETE,
                reconciliation_required=exc.failure.reconciliation_required,
            ) from exc

    def _admit_current(
        self,
        prepared: _Prepared,
    ) -> VerifiedRunReadV2 | None:
        request = prepared.request
        current_path = self._paths(request.run_id)[1] / "current.json"
        if not current_path.exists():
            if request.expected_revision is not None:
                raise _failure(
                    request.run_id,
                    ProductRunFailureCode.RUN_CONFLICT,
                    stage="locked_admission",
                    transaction_id=request.transaction_id,
                )
            return None
        current = self.read_current(request.run_id)
        if (
            current.transaction_id == request.transaction_id
            and current.revision == request.proposed_revision
        ):
            if (
                current.manifest_sha256 == prepared.manifest_sha256
                and current.current_pointer_sha256 == prepared.pointer_sha256
            ):
                return current
            raise _failure(
                request.run_id,
                ProductRunFailureCode.IDEMPOTENCY_CONFLICT,
                stage="locked_admission",
                transaction_id=request.transaction_id,
                observed_revision=current.revision,
                observed_pointer_sha256=current.current_pointer_sha256,
            )
        if (
            current.revision != request.expected_revision
            or current.manifest_sha256 != request.expected_manifest_sha256
            or current.current_pointer_sha256 != request.expected_pointer_sha256
        ):
            raise _failure(
                request.run_id,
                ProductRunFailureCode.RUN_CONFLICT,
                stage="locked_admission",
                transaction_id=request.transaction_id,
                observed_revision=current.revision,
                observed_pointer_sha256=current.current_pointer_sha256,
                previous_revision_effect=(
                    "not_applicable" if request.proposed_revision == 1 else "unchanged"
                ),
            )
        return None

    def publish(
        self,
        request: TerminalPublishRequest,
        *,
        pre_manifest_verifier: Callable[[], None] | None = None,
    ) -> TerminalPublishOutcome:
        prepared = self._prepare(request)
        previous_effect: Literal["not_applicable", "unchanged"] = (
            "not_applicable" if request.proposed_revision == 1 else "unchanged"
        )
        try:
            self.budget.reserve(
                request,
                artifact_bytes=prepared.artifact_bytes,
                run_bytes=prepared.persistent_delta,
            )
        except DurableBudgetError as exc:
            code = (
                ProductRunFailureCode.RUN_LOCKED
                if exc.failure.code.value in {"run_locked", "concurrency_exceeded", "cas_conflict"}
                else ProductRunFailureCode.BUDGET_RESERVATION_FAILED
            )
            raise _failure(
                request.run_id,
                code,
                stage="budget_reservation",
                transaction_id=request.transaction_id,
                previous_revision_effect=previous_effect,
            ) from exc
        except Exception as exc:
            raise _failure(
                request.run_id,
                ProductRunFailureCode.BUDGET_RESERVATION_FAILED,
                stage="budget_reservation",
                transaction_id=request.transaction_id,
                previous_revision_effect=previous_effect,
            ) from exc
        marker_sha = None
        lease = None
        pointer_published = False
        revision_published = False
        try:
            _marker, marker_sha = self.foundation._ownership(request.run_id)
            self._scan_aliases(request.run_id)
            lease = self.foundation._authority(
                request.run_id,
                marker_sha,
                bootstrap=True,
            )
            self._scan_aliases(request.run_id)
            self._verify_namespace(request.run_id)
            replay = self._admit_current(prepared)
            if replay is not None:
                return TerminalPublishOutcome(
                    outcome_kind="current_committed",
                    run_id_sha256=run_id_sha256(request.run_id),
                    transaction_id=request.transaction_id,
                    revision=request.proposed_revision,
                    manifest_sha256=prepared.manifest_sha256,
                    pointer_sha256=prepared.pointer_sha256,
                    completion_marker_sha256=prepared.marker_sha256,
                    durability_evidence=_idle_durability(),
                )
            if pre_manifest_verifier is not None:
                _fault(self.fault_injector, "pre_manifest_verifier.before")
                pre_manifest_verifier()
                _fault(self.fault_injector, "pre_manifest_verifier.after")
            _run, control, transactions, revisions = self._bootstrap_namespace(request.run_id)
            staging = transactions / request.transaction_id
            revision = revisions / f"r{request.proposed_revision}-{request.transaction_id}"
            if staging.exists() or revision.exists():
                raise CanonicalStorageError("transaction namespace collision")
            _fault(self.fault_injector, "staging.before_mkdir")
            staging.mkdir()
            _fault(self.fault_injector, "staging.after_mkdir")
            _write_exclusive_verified(
                staging / "transaction.json",
                prepared.transaction_bytes,
                injector=self.fault_injector,
                hook="transaction",
            )
            payload_root = staging / "payload"
            payload_root.mkdir()
            payload_map = {
                payload.inventory.logical_name: payload.exact_bytes for payload in request.payloads
            }
            for entry in prepared.manifest.artifacts:
                destination = payload_root / Path(entry.logical_name)
                destination.parent.mkdir(parents=True, exist_ok=True)
                _write_exclusive_verified(
                    destination,
                    payload_map[entry.logical_name],
                    injector=self.fault_injector,
                    hook=f"payload.{entry.logical_name.replace('/', '_')}",
                )
            _write_exclusive_verified(
                staging / "manifest.json",
                prepared.manifest_bytes,
                injector=self.fault_injector,
                hook="manifest",
            )
            verify_payload_inventory(
                prepared.manifest.artifacts,
                payload_map,
                allow_opaque=request.publication_kind == "legacy_copy",
            )
            if prepared.marker_bytes is not None:
                _write_exclusive_verified(
                    staging / "completion.json",
                    prepared.marker_bytes,
                    injector=self.fault_injector,
                    hook="completion",
                )
            _fault(self.fault_injector, "revision.before_rename")
            staging.replace(revision)
            revision_published = True
            _fault(self.fault_injector, "revision.after_rename")
            self._read_revision(
                request.run_id,
                prepared.pointer,
                verify_budget=False,
            )
            current_path = control / "current.json"
            if request.expected_pointer_sha256 is None:
                if current_path.exists():
                    raise _failure(
                        request.run_id,
                        ProductRunFailureCode.RUN_CONFLICT,
                        stage="final_cas",
                        transaction_id=request.transaction_id,
                        filesystem_effect="unreferenced_revision",
                        reconciliation_required=True,
                    )
            else:
                current_bytes = _read_bounded(current_path, max_bytes=self.max_artifact_bytes)
                if current_pointer_sha256(current_bytes) != request.expected_pointer_sha256:
                    raise _failure(
                        request.run_id,
                        ProductRunFailureCode.RUN_CONFLICT,
                        stage="final_cas",
                        transaction_id=request.transaction_id,
                        filesystem_effect="unreferenced_revision",
                        reconciliation_required=True,
                        previous_revision_effect=previous_effect,
                    )
            temporary = control / f"current.{request.transaction_id}.tmp"
            _write_exclusive_verified(
                temporary,
                prepared.pointer_bytes,
                injector=self.fault_injector,
                hook="pointer",
            )
            _fault(self.fault_injector, "current.before_replace")
            try:
                os.replace(temporary, current_path)
            except Exception:
                _fault(self.fault_injector, "current.replace_failed")
                raise
            pointer_published = True
            _fault(self.fault_injector, "current.after_replace")
            if (
                _read_bounded(current_path, max_bytes=self.max_artifact_bytes)
                != prepared.pointer_bytes
            ):
                raise CanonicalStorageError("published current pointer mismatch")
            directory_state = _directory_sync(
                control,
                self.fault_injector,
                "current",
            )
            effect_sha = prepared.marker_sha256 or prepared.manifest_sha256
            self.budget.settle(
                request,
                pointer_sha256=prepared.pointer_sha256,
                effect_evidence_sha256=effect_sha,
                artifact_bytes=prepared.artifact_bytes,
                run_bytes=prepared.persistent_delta,
            )
            self.read_current(request.run_id)
            return TerminalPublishOutcome(
                outcome_kind="published",
                run_id_sha256=run_id_sha256(request.run_id),
                transaction_id=request.transaction_id,
                revision=request.proposed_revision,
                manifest_sha256=prepared.manifest_sha256,
                pointer_sha256=prepared.pointer_sha256,
                completion_marker_sha256=prepared.marker_sha256,
                durability_evidence=_published_durability(directory_state),
            )
        except ApprovalDecisionValidationError:
            if not pointer_published:
                with suppress(Exception):
                    self.budget.release_no_effect(
                        request,
                        evidence_sha256=canonical_domain_sha256(
                            PRODUCT_TRANSACTION_DOMAIN,
                            {"transaction_id": request.transaction_id, "effect": "none"},
                        ),
                    )
            raise
        except ProductRunError:
            if not pointer_published:
                with suppress(Exception):
                    self.budget.release_no_effect(
                        request,
                        evidence_sha256=canonical_domain_sha256(
                            PRODUCT_TRANSACTION_DOMAIN,
                            {"transaction_id": request.transaction_id, "effect": "none"},
                        ),
                    )
            raise
        except RunStorageError as exc:
            if not pointer_published:
                with suppress(Exception):
                    self.budget.release_no_effect(
                        request,
                        evidence_sha256=canonical_domain_sha256(
                            PRODUCT_TRANSACTION_DOMAIN,
                            {
                                "transaction_id": request.transaction_id,
                                "effect": "none",
                            },
                        ),
                    )
            code = {
                "run_locked": ProductRunFailureCode.RUN_LOCKED,
                "lock_unavailable": ProductRunFailureCode.LOCK_UNAVAILABLE,
            }.get(exc.failure.code.value, ProductRunFailureCode.EFFECT_UNKNOWN)
            raise _failure(
                request.run_id,
                code,
                stage="lock_acquire",
                transaction_id=request.transaction_id,
                reconciliation_required=exc.failure.reconciliation_required,
                filesystem_effect=(
                    "control_only" if exc.failure.filesystem_effect == "control_only" else "none"
                ),
                previous_revision_effect=previous_effect,
                durability_evidence=exc.failure.durability_evidence,
            ) from exc
        except Exception as exc:
            effect: Literal[
                "staging_orphan",
                "unreferenced_revision",
                "current_advanced",
            ]
            domain: Literal["current_unchanged", "current_advanced"]
            if pointer_published:
                code = ProductRunFailureCode.BUDGET_SETTLEMENT_FAILED
                effect = "current_advanced"
                domain = "current_advanced"
                durability = _reconciliation_durability(pointer_replace="confirmed")
            elif revision_published:
                code = ProductRunFailureCode.DURABILITY_UNCONFIRMED
                effect = "unreferenced_revision"
                domain = "current_unchanged"
                durability = _reconciliation_durability()
            else:
                code = ProductRunFailureCode.EFFECT_UNKNOWN
                effect = "staging_orphan"
                domain = "current_unchanged"
                durability = _reconciliation_durability(file_sync="not_attempted")
            if not pointer_published:
                try:
                    self.budget.release_no_effect(
                        request,
                        evidence_sha256=canonical_domain_sha256(
                            PRODUCT_TRANSACTION_DOMAIN,
                            {
                                "transaction_id": request.transaction_id,
                                "effect": effect,
                            },
                        ),
                    )
                except Exception:
                    code = ProductRunFailureCode.EFFECT_UNKNOWN
            raise _failure(
                request.run_id,
                code,
                stage="publish",
                read_status=(RunReadStatus.INCOMPLETE if pointer_published else None),
                transaction_id=request.transaction_id,
                observed_revision=(request.proposed_revision if pointer_published else None),
                observed_pointer_sha256=(prepared.pointer_sha256 if pointer_published else None),
                reconciliation_required=True,
                filesystem_effect=effect,
                domain_effect=domain,
                previous_revision_effect=previous_effect,
                durability_evidence=durability,
            ) from exc
        finally:
            if lease is not None:
                try:
                    lease.release()
                except LockReleaseError as exc:
                    if not pointer_published:
                        raise _failure(
                            request.run_id,
                            ProductRunFailureCode.EFFECT_UNKNOWN,
                            stage="lock_release",
                            transaction_id=request.transaction_id,
                            reconciliation_required=True,
                            filesystem_effect=(
                                "unreferenced_revision" if revision_published else "control_only"
                            ),
                            domain_effect="current_unchanged",
                            previous_revision_effect=previous_effect,
                            durability_evidence=_reconciliation_durability(),
                        ) from exc

    def publish_approval_decision(
        self,
        request: TerminalPublishRequest,
        *,
        authority_verifier: Callable[[], None],
    ) -> TerminalPublishOutcome:
        """Publish one approval successor with authority reverified in-lock."""

        if (
            request.expected_revision is None
            or request.proposed_revision != request.expected_revision + 1
        ):
            raise ValueError("approval decision requires an exact successor revision")
        return self.publish(
            request,
            pre_manifest_verifier=authority_verifier,
        )

    @staticmethod
    def _empty_approval_audit_pointer(
        run_id: str,
    ) -> ApprovalSecurityAuditPointerV2:
        return ApprovalSecurityAuditPointerV2(
            run_id_sha256=run_id_sha256(run_id),
            audit_sequence=0,
            head_event_sha256=None,
            total_event_bytes=0,
            rate_states=(),
            updated_at=None,
        )

    def _read_approval_failure_audit(
        self,
        run_id: str,
    ) -> tuple[
        ApprovalSecurityAuditPointerV2,
        tuple[ApprovalSecurityAuditEventV2, ...],
    ]:
        _run, control, _transactions, _revisions = self._paths(run_id)
        root = control / "approval-failure-audit"
        if not root.exists():
            return self._empty_approval_audit_pointer(run_id), ()
        verify_directory(root)
        entries = {item.name: item for item in root.iterdir()}
        if set(entries) - {"events", "current.json", "current.tmp"}:
            raise CanonicalStorageError("unknown approval failure audit entry")
        if "current.tmp" in entries:
            verify_regular_single_link(entries["current.tmp"])
            raise CanonicalStorageError("approval failure audit pointer is incomplete")
        if not entries:
            return self._empty_approval_audit_pointer(run_id), ()
        if set(entries) != {"events", "current.json"}:
            raise CanonicalStorageError("approval failure audit namespace is incomplete")
        events_root = entries["events"]
        verify_directory(events_root)
        pointer_bytes = _read_bounded(
            entries["current.json"],
            max_bytes=self.max_artifact_bytes,
        )
        pointer = parse_approval_model(
            pointer_bytes,
            ApprovalSecurityAuditPointerV2,
        )
        if pointer.run_id_sha256 != run_id_sha256(run_id):
            raise CanonicalStorageError("approval failure audit run mismatch")
        event_files = sorted(
            events_root.iterdir(),
            key=lambda item: item.name.encode("ascii"),
        )
        if len(event_files) != pointer.audit_sequence:
            raise CanonicalStorageError("approval failure audit is truncated")
        events: list[ApprovalSecurityAuditEventV2] = []
        expected_previous: str | None = None
        total_bytes = 0
        prior_time: datetime | None = None
        for sequence, path in enumerate(event_files, start=1):
            match = _APPROVAL_AUDIT_EVENT.fullmatch(path.name)
            if match is None or int(match.group("sequence")) != sequence:
                raise CanonicalStorageError("approval failure audit event path mismatch")
            data = _read_bounded(
                path,
                max_bytes=APPROVAL_AUDIT_MAX_EVENT_BYTES,
            )
            event = parse_approval_model(data, ApprovalSecurityAuditEventV2)
            if (
                event.sequence != sequence
                or event.previous_event_sha256 != expected_previous
                or event.run_id_sha256 != pointer.run_id_sha256
                or event.event_sha256 != match.group("hash")
            ):
                raise CanonicalStorageError("approval failure audit event lineage mismatch")
            if prior_time is not None and event.occurred_at < prior_time:
                raise CanonicalStorageError("approval failure audit clock rollback")
            events.append(event)
            expected_previous = event.event_sha256
            prior_time = event.occurred_at
            total_bytes += len(data)
        expected_states = self._approval_audit_rate_states(tuple(events))
        if (
            pointer.head_event_sha256 != expected_previous
            or pointer.total_event_bytes != total_bytes
            or pointer.rate_states != expected_states
            or pointer.updated_at != prior_time
        ):
            raise CanonicalStorageError("approval failure audit pointer mismatch")
        return pointer, tuple(events)

    @staticmethod
    def _approval_audit_rate_states(
        events: tuple[ApprovalSecurityAuditEventV2, ...],
    ) -> tuple[ApprovalSecurityAuditRateStateV2, ...]:
        states: dict[str, ApprovalSecurityAuditRateStateV2] = {}
        failures: dict[str, list[datetime]] = {}
        for event in events:
            prior = states.get(event.actor_sha256)
            window_floor = event.occurred_at - timedelta(seconds=APPROVAL_AUDIT_WINDOW_SECONDS)
            recent = [
                occurred_at
                for occurred_at in failures.get(event.actor_sha256, [])
                if occurred_at > window_floor
            ]
            marker_active = bool(
                prior is not None
                and prior.rate_limit_marker_recorded
                and len(recent) == APPROVAL_AUDIT_MAX_FAILED_EVENTS
            )
            if event.event_kind == "failure":
                if len(recent) >= APPROVAL_AUDIT_MAX_FAILED_EVENTS:
                    raise CanonicalStorageError("approval audit failure count exceeds its window")
                recent.append(event.occurred_at)
                marker_active = False
            else:
                if len(recent) != APPROVAL_AUDIT_MAX_FAILED_EVENTS or marker_active:
                    raise CanonicalStorageError("approval audit rate-limit marker mismatch")
                marker_active = True
            expected_window_start = recent[0]
            if event.rate_window_started_at != expected_window_start:
                raise CanonicalStorageError("approval audit rolling window mismatch")
            failures[event.actor_sha256] = recent
            states[event.actor_sha256] = ApprovalSecurityAuditRateStateV2(
                actor_sha256=event.actor_sha256,
                window_started_at=expected_window_start,
                failed_event_count=len(recent),
                rate_limit_marker_recorded=marker_active,
            )
        return tuple(states[key] for key in sorted(states, key=lambda item: item.encode("ascii")))

    def read_approval_failure_audit(
        self,
        run_id: str,
    ) -> tuple[
        ApprovalSecurityAuditPointerV2,
        tuple[ApprovalSecurityAuditEventV2, ...],
    ]:
        validate_run_id(run_id)
        self._scan_aliases(run_id)
        self._verify_namespace(run_id)
        return self._read_approval_failure_audit(run_id)

    def append_approval_failure_audit(
        self,
        request: ApprovalFailureAuditRequest,
        *,
        max_events: int = APPROVAL_AUDIT_MAX_EVENTS,
        max_event_bytes: int = APPROVAL_AUDIT_MAX_EVENT_BYTES,
        max_total_bytes: int = APPROVAL_AUDIT_MAX_TOTAL_BYTES,
        max_failed_events: int = APPROVAL_AUDIT_MAX_FAILED_EVENTS,
        window_seconds: int = APPROVAL_AUDIT_WINDOW_SECONDS,
    ) -> ApprovalFailureAuditOutcome:
        """Append one failure event under the same per-run RM-012 authority."""

        if (
            not 1 <= max_events <= APPROVAL_AUDIT_MAX_EVENTS
            or not 1 <= max_event_bytes <= APPROVAL_AUDIT_MAX_EVENT_BYTES
            or not 1 <= max_total_bytes <= APPROVAL_AUDIT_MAX_TOTAL_BYTES
            or max_failed_events != APPROVAL_AUDIT_MAX_FAILED_EVENTS
            or window_seconds != APPROVAL_AUDIT_WINDOW_SECONDS
        ):
            raise ValueError("approval failure audit limits exceed the contract")
        validate_run_id(request.run_id)
        occurred_at_offset = request.occurred_at.utcoffset()
        if (
            request.occurred_at.tzinfo is None
            or occurred_at_offset is None
            or occurred_at_offset.total_seconds() != 0
        ):
            raise ValueError("approval failure audit time must be UTC")
        lease = None
        pointer_published = False
        try:
            _marker, marker_sha = self.foundation._ownership(request.run_id)
            self._scan_aliases(request.run_id)
            lease = self.foundation._authority(
                request.run_id,
                marker_sha,
                bootstrap=True,
            )
            self._scan_aliases(request.run_id)
            self._verify_namespace(request.run_id)
            _run, control, _transactions, _revisions = self._paths(request.run_id)
            if not (control / "current.json").exists():
                raise CanonicalStorageError(
                    "approval failure audit requires an existing product run"
                )
            pointer, events = self._read_approval_failure_audit(request.run_id)
            if pointer.updated_at is not None and request.occurred_at < pointer.updated_at:
                raise CanonicalStorageError("approval failure audit clock rollback")
            if len(events) >= max_events:
                raise self._approval_audit_error(
                    request,
                    ApprovalFailureCode.AUDIT_CAPACITY_EXCEEDED,
                    "Approval failure audit event capacity is exhausted.",
                )
            rate_by_actor = {state.actor_sha256: state for state in pointer.rate_states}
            prior_rate = rate_by_actor.get(request.actor_sha256)
            window_floor = request.occurred_at - timedelta(seconds=window_seconds)
            recent_failures = [
                event.occurred_at
                for event in events
                if event.actor_sha256 == request.actor_sha256
                and event.event_kind == "failure"
                and event.occurred_at > window_floor
            ]
            marker_active = bool(
                prior_rate is not None
                and prior_rate.rate_limit_marker_recorded
                and len(recent_failures) == max_failed_events
            )
            event_kind: Literal["failure", "rate_limit"]
            failure_code = request.failure_code
            if len(recent_failures) >= max_failed_events:
                if marker_active:
                    raise self._approval_audit_error(
                        request,
                        ApprovalFailureCode.AUDIT_RATE_LIMITED,
                        "Approval failure audit rate limit is active.",
                        audit_confirmed=True,
                    )
                event_kind = "rate_limit"
                failure_code = ApprovalFailureCode.AUDIT_RATE_LIMITED
            else:
                event_kind = "failure"
            rate_window_started_at = recent_failures[0] if recent_failures else request.occurred_at
            partial = ApprovalSecurityAuditEventV2.model_construct(
                sequence=pointer.audit_sequence + 1,
                previous_event_sha256=pointer.head_event_sha256,
                event_kind=event_kind,
                run_id_sha256=run_id_sha256(request.run_id),
                actor_sha256=request.actor_sha256,
                decision_id_sha256=request.decision_id_sha256,
                idempotency_key_sha256=request.idempotency_key_sha256,
                batch_sha256=request.batch_sha256,
                failure_code=failure_code,
                observed_run_revision=request.observed_run_revision,
                observed_ledger_revision=request.observed_ledger_revision,
                occurred_at=request.occurred_at,
                rate_window_started_at=rate_window_started_at,
                event_sha256="0" * 64,
            )
            event = ApprovalSecurityAuditEventV2(
                **(
                    partial.model_dump()
                    | {"event_sha256": approval_security_audit_event_sha256(partial)}
                )
            )
            event_bytes = canonical_approval_json_bytes(event)
            if len(event_bytes) > max_event_bytes:
                raise self._approval_audit_error(
                    request,
                    ApprovalFailureCode.AUDIT_CAPACITY_EXCEEDED,
                    "Approval failure audit event exceeds its byte limit.",
                )
            if pointer.total_event_bytes + len(event_bytes) > max_total_bytes:
                raise self._approval_audit_error(
                    request,
                    ApprovalFailureCode.AUDIT_CAPACITY_EXCEEDED,
                    "Approval failure audit byte capacity is exhausted.",
                )
            next_events = (*events, event)
            next_pointer = ApprovalSecurityAuditPointerV2(
                run_id_sha256=run_id_sha256(request.run_id),
                audit_sequence=event.sequence,
                head_event_sha256=event.event_sha256,
                total_event_bytes=pointer.total_event_bytes + len(event_bytes),
                rate_states=self._approval_audit_rate_states(next_events),
                updated_at=request.occurred_at,
            )
            pointer_bytes = canonical_approval_json_bytes(next_pointer)
            if len(pointer_bytes) > self.max_artifact_bytes:
                raise self._approval_audit_error(
                    request,
                    ApprovalFailureCode.AUDIT_CAPACITY_EXCEEDED,
                    "Approval failure audit pointer exceeds its byte limit.",
                )
            audit_root = control / "approval-failure-audit"
            events_root = audit_root / "events"
            _fault(self.fault_injector, "approval_audit.before_mkdir")
            _recognized_directory(audit_root)
            _recognized_directory(events_root)
            _fault(self.fault_injector, "approval_audit.after_mkdir")
            event_path = events_root / (f"{event.sequence:04d}-{event.event_sha256}.json")
            _write_exclusive_verified(
                event_path,
                event_bytes,
                injector=self.fault_injector,
                hook="approval_audit.event",
            )
            _directory_sync(
                events_root,
                self.fault_injector,
                "approval_audit.events",
            )
            temporary = audit_root / "current.tmp"
            _write_exclusive_verified(
                temporary,
                pointer_bytes,
                injector=self.fault_injector,
                hook="approval_audit.pointer",
            )
            _fault(self.fault_injector, "approval_audit.current.before_replace")
            os.replace(temporary, audit_root / "current.json")
            pointer_published = True
            _fault(self.fault_injector, "approval_audit.current.after_replace")
            if (
                _read_bounded(
                    audit_root / "current.json",
                    max_bytes=self.max_artifact_bytes,
                )
                != pointer_bytes
            ):
                raise CanonicalStorageError("approval failure audit pointer reread mismatch")
            directory_state = cast(
                Literal["confirmed", "unavailable"],
                _directory_sync(
                    audit_root,
                    self.fault_injector,
                    "approval_audit.current",
                ),
            )
            verified_pointer, verified_events = self._read_approval_failure_audit(request.run_id)
            if verified_pointer != next_pointer or verified_events[-1] != event:
                raise CanonicalStorageError("approval failure audit verification mismatch")
            outcome = ApprovalFailureAuditOutcome(
                event=event,
                pointer=next_pointer,
                pointer_sha256=approval_security_audit_pointer_sha256(next_pointer),
                directory_sync=directory_state,
            )
            if event_kind == "rate_limit":
                raise self._approval_audit_error(
                    request,
                    ApprovalFailureCode.AUDIT_RATE_LIMITED,
                    "Approval failure audit rate limit is active.",
                    audit_confirmed=True,
                )
            return outcome
        except ApprovalFailureAuditError:
            raise
        except (CanonicalStorageError, ValidationError) as exc:
            code = (
                ApprovalFailureCode.APPROVAL_LEDGER_CORRUPT
                if not pointer_published
                else ApprovalFailureCode.AUDIT_UNCONFIRMED
            )
            raise self._approval_audit_error(
                request,
                code,
                "Approval failure audit could not be verified.",
            ) from exc
        except (RunStorageError, OSError) as exc:
            raise self._approval_audit_error(
                request,
                ApprovalFailureCode.AUDIT_UNCONFIRMED,
                "Approval failure audit could not be confirmed.",
            ) from exc
        except Exception as exc:
            raise self._approval_audit_error(
                request,
                ApprovalFailureCode.AUDIT_UNCONFIRMED,
                "Approval failure audit failed closed.",
            ) from exc
        finally:
            if lease is not None:
                try:
                    lease.release()
                except LockReleaseError as exc:
                    raise self._approval_audit_error(
                        request,
                        ApprovalFailureCode.AUDIT_UNCONFIRMED,
                        "Approval failure audit authority release is unconfirmed.",
                    ) from exc

    @staticmethod
    def _approval_audit_error(
        request: ApprovalFailureAuditRequest,
        code: ApprovalFailureCode,
        message: str,
        *,
        audit_confirmed: bool = False,
    ) -> ApprovalFailureAuditError:
        return ApprovalFailureAuditError(
            ApprovalDecisionFailureV2(
                code=code,
                message=message,
                retryable=False,
                run_id=request.run_id,
                idempotency_key_sha256=request.idempotency_key_sha256,
                observed_run_revision=request.observed_run_revision,
                observed_ledger_revision=request.observed_ledger_revision,
                audit_confirmed=audit_confirmed,
                reconciliation_required=False,
            )
        )

    def report_path(self, read: VerifiedRunReadV2, format_name: str) -> Path:
        name = "final_report.json" if format_name == "json" else "final_report.md"
        if not any(item.inventory.logical_name == name for item in read.payloads):
            raise _failure(
                read.run_id,
                ProductRunFailureCode.ARTIFACT_MISSING,
                stage="report_path",
                observed_revision=read.revision,
                observed_pointer_sha256=read.current_pointer_sha256,
            )
        _run, control, _transactions, _revisions = self._paths(read.run_id)
        path = control / read.pointer.revision_relative_path / "payload" / name
        verify_regular_single_link(path)
        return path


def default_budget_binding(
    run_id: str,
    transaction_id: str,
    policy: BudgetPolicyV2,
    *,
    requested: ResourceAmountsV1,
) -> BudgetSettlementBindingV2:
    """Freeze all P2-011B correlation IDs before product filesystem mutation."""

    digest = domain_sha256(
        PRODUCT_BUDGET_ID_DOMAIN,
        (run_id + "\0" + transaction_id).encode("utf-8"),
    )
    reservation_id = f"reserve-{digest[:24]}"
    return BudgetSettlementBindingV2(
        budget_run_id_sha256=run_id_sha256(run_id),
        budget_policy_sha256=policy.canonical_sha256,
        reservation_operation_id=reservation_id,
        reservation_request_sha256=reservation_request_sha256(
            reservation_id=reservation_id,
            requested=requested,
        ),
        permit_id=f"permit-{digest[8:32]}",
        settlement_operation_id=f"settle-{digest[16:40]}",
        settlement_id=f"settlement-{digest[24:48]}",
    )


def provisional_budget_binding(
    run_id: str,
    transaction_id: str,
    policy: BudgetPolicyV2,
) -> BudgetSettlementBindingV2:
    """Build a fixed-width placeholder that must be frozen before publish."""

    return default_budget_binding(
        run_id,
        transaction_id,
        policy,
        requested=ResourceAmountsV1(
            active_runtime_ns=policy.runtime_limit_ns,
            artifact_bytes=1,
            run_bytes=1,
            concurrency_slots=1,
        ),
    )


__all__ = [
    "PRODUCT_PRODUCER_ID",
    "PRODUCT_PRODUCER_VERSION",
    "DurableBudgetCoordinator",
    "TerminalPublishOutcome",
    "TerminalPublishRequest",
    "TerminalRunStore",
    "default_budget_binding",
    "initialize_terminal_root",
    "provisional_budget_binding",
]
