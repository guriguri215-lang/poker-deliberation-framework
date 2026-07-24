"""Internal P2-011B durable budget state transitions over P2-012A CAS."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from poker_deliberation.budgets.contracts import BudgetPolicyV2, ExecutionClass
from poker_deliberation.budgets.durable_models import (
    DURABLE_BUDGET_ARTIFACT_SCHEMA,
    DURABLE_BUDGET_PRODUCER_ID,
    DURABLE_BUDGET_PRODUCER_VERSION,
    RESOURCE_ORDER,
    AttemptRecordV1,
    AttemptStatus,
    CancellationState,
    DeterministicToolEvidenceV1,
    DurableBudgetFailureV1,
    DurableBudgetPolicyV1,
    DurableBudgetStateV1,
    DurableCancellationV1,
    DurableEventV1,
    DurableFailureCode,
    DurableMutationResultV1,
    DurablePermitV1,
    DurableSettlementV1,
    ExecutionLineageV1,
    IdempotencyRecordV1,
    MutationStatus,
    OperationKind,
    OperationOutcome,
    PermitStatus,
    ResourceAmountsV1,
    ResourceReservationV1,
    SettlementStatus,
    canonical_durable_sha256,
)
from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.storage.revision_canonical import (
    classification_evidence_sha256,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    LocalDataBindingV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    RootInitializationOutcomeV1,
    RootInitializationRequestV1,
    RunStorageError,
    RunStorageFailureCode,
)
from poker_deliberation.storage.revision_store import (
    RunRevisionStore,
    initialize_revision_root,
)

WallClock = Callable[[], datetime]
MonotonicClock = Callable[[], int]
T = TypeVar("T", bound=BaseModel)


def _wall_clock() -> datetime:
    return datetime.now(UTC)


def _monotonic_clock() -> int:
    import time

    return time.monotonic_ns()


class DurableBudgetError(ValueError):
    """One redacted typed durable-budget refusal."""

    def __init__(self, failure: DurableBudgetFailureV1):
        self.failure = failure
        super().__init__(failure.code.value)


def _failure(
    code: DurableFailureCode,
    *,
    operation_id: str | None = None,
    resource: str | None = None,
    limit: int | None = None,
    observed: int | None = None,
    reconciliation_required: bool = False,
    effect_unknown: bool = False,
    evidence_sha256: str | None = None,
) -> DurableBudgetError:
    return DurableBudgetError(
        DurableBudgetFailureV1(
            code=code,
            operation_id=operation_id,
            resource=resource,
            limit=limit,
            observed=observed,
            reconciliation_required=reconciliation_required,
            effect_unknown=effect_unknown,
            evidence_sha256=evidence_sha256,
        )
    )


def _transaction_id(operation_id: str) -> str:
    digest = hashlib.sha256(
        b"poker-durable-budget-operation-v1\0" + operation_id.encode("utf-8")
    ).hexdigest()
    return f"txn-{digest[:32]}"


def _model_without(value: BaseModel, *fields: str) -> dict[str, Any]:
    return value.model_dump(mode="json", exclude=set(fields))


def reservation_request_sha256(
    *,
    reservation_id: str,
    requested: ResourceAmountsV1,
    execution_class: ExecutionClass = ExecutionClass.LOCAL_FREE,
    external_cost_estimate_authenticated: bool = False,
) -> str:
    return canonical_durable_sha256(
        {
            "reservation_id": reservation_id,
            "requested": requested.model_dump(mode="json"),
            "execution_class": execution_class.value,
            "external_cost_estimate_authenticated": (
                external_cost_estimate_authenticated
            ),
        }
    )


def build_resource_reservation(
    *,
    reservation_id: str,
    requested: ResourceAmountsV1,
    execution_class: ExecutionClass = ExecutionClass.LOCAL_FREE,
    external_cost_estimate_authenticated: bool = False,
) -> ResourceReservationV1:
    return ResourceReservationV1(
        reservation_id=reservation_id,
        requested=requested,
        execution_class=execution_class,
        external_cost_estimate_authenticated=external_cost_estimate_authenticated,
        request_sha256=reservation_request_sha256(
            reservation_id=reservation_id,
            requested=requested,
            execution_class=execution_class,
            external_cost_estimate_authenticated=external_cost_estimate_authenticated,
        ),
    )


def initialize_durable_budget_root(
    revision_root: Path,
    legacy_runs_root: Path,
    *,
    root_id: str,
    initialized_at: datetime,
) -> RootInitializationOutcomeV1:
    """Explicitly initialize a dedicated P2-011B structural root."""

    return initialize_revision_root(
        RootInitializationRequestV1(
            revision_root=revision_root,
            legacy_runs_root=legacy_runs_root,
            root_id=root_id,
            initialized_at=initialized_at,
            producer_id=DURABLE_BUDGET_PRODUCER_ID,
            producer_version=DURABLE_BUDGET_PRODUCER_VERSION,
        )
    )


def _artifact(state: DurableBudgetStateV1) -> RevisionArtifactV1:
    evidence = ClassificationEvidence(restricted_secret_check_completed=True)
    local = LocalDataBindingV1(
        logical_name="budget_state.json",
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.DEFAULT_INTERNAL,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
    )
    return RevisionArtifactV1(
        logical_name="budget_state.json",
        media_type="application/json",
        artifact_schema_version=DURABLE_BUDGET_ARTIFACT_SCHEMA,
        serialization="poker-run-storage-json-v1",
        exact_bytes=state.canonical_bytes(),
        required=True,
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.DEFAULT_INTERNAL,
        classification_evidence=evidence,
        policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
        origin_kind="budget_state",
        provenance_bindings=(local,),
    )


def _replace(model: T, **updates: Any) -> T:
    return type(model).model_validate(
        {**model.model_dump(mode="python"), **updates},
        strict=True,
    )


def _is_policy_tightening(
    old: DurableBudgetPolicyV1,
    new: DurableBudgetPolicyV1,
) -> bool:
    old_base = old.base_policy
    new_base = new.base_policy
    nonincreasing = (
        "max_deliberation_rounds",
        "max_tool_retries",
        "max_runtime_seconds",
        "max_external_cost_micro_usd",
        "max_provider_output_bytes",
        "max_tool_input_bytes",
        "max_tool_output_bytes",
        "max_artifact_bytes",
        "max_run_bytes",
    )
    if any(getattr(new_base, field) > getattr(old_base, field) for field in nonincreasing):
        return False
    return (
        new.activation.max_concurrent_agents
        <= old.activation.max_concurrent_agents
        and new.activation.max_automatic_retries
        <= old.activation.max_automatic_retries
    )


def _validate_state_successor(
    older: DurableBudgetStateV1,
    newer: DurableBudgetStateV1,
) -> None:
    if (
        newer.run_id != older.run_id
        or newer.generation != older.generation + 1
        or newer.previous_state_sha256 != older.canonical_sha256
    ):
        raise ValueError("durable state generation lineage mismatch")
    if newer.operations[: len(older.operations)] != older.operations:
        raise ValueError("durable idempotency history was rewritten")
    if newer.events[: len(older.events)] != older.events:
        raise ValueError("durable event history was rewritten")
    if newer.settlements[: len(older.settlements)] != older.settlements:
        raise ValueError("durable settlement history was rewritten")
    if older.failure_latch is not None and newer.failure_latch != older.failure_latch:
        raise ValueError("durable failure latch was cleared or rewritten")
    if newer.policy != older.policy and not _is_policy_tightening(older.policy, newer.policy):
        raise ValueError("durable policy was not monotonically tightened")
    cumulative = {
        "active_runtime_ns",
        "provider_attempts",
        "tool_attempts",
        "retry_attempts",
        "external_cost_micro_usd",
        "run_bytes",
    }
    for resource in RESOURCE_ORDER:
        previous = int(getattr(older.usage, resource))
        current = int(getattr(newer.usage, resource))
        if resource in cumulative and current < previous:
            raise ValueError("durable cumulative usage decreased")
        if resource not in cumulative and resource != "concurrency_slots" and current < previous:
            raise ValueError("durable maximum usage decreased")


class DurableBudgetStore:
    """A caller-visible exact-idempotent durable budget state machine."""

    def __init__(
        self,
        revision_root: Path,
        legacy_runs_root: Path,
        *,
        clock: MonotonicClock = _monotonic_clock,
        wall_clock: WallClock = _wall_clock,
        max_artifact_bytes: int = 1_000_000,
        max_run_bytes: int = 10_000_000,
    ) -> None:
        self.clock = clock
        self.wall_clock = wall_clock
        self._last_monotonic_ns: int | None = None
        self.revisions = RunRevisionStore(
            revision_root,
            legacy_runs_root,
            max_artifact_bytes=max_artifact_bytes,
            max_run_bytes=max_run_bytes,
            clock=wall_clock,
            producer_id=DURABLE_BUDGET_PRODUCER_ID,
            producer_version=DURABLE_BUDGET_PRODUCER_VERSION,
        )

    def _observe_monotonic(self, operation_id: str) -> int:
        try:
            value = self.clock()
        except Exception as exc:
            raise _failure(
                DurableFailureCode.INVALID_INPUT,
                operation_id=operation_id,
            ) from exc
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise _failure(
                DurableFailureCode.INVALID_INPUT,
                operation_id=operation_id,
            )
        if self._last_monotonic_ns is not None and value < self._last_monotonic_ns:
            raise _failure(
                DurableFailureCode.CLOCK_ROLLBACK,
                operation_id=operation_id,
                resource="active_runtime_ns",
                observed=self._last_monotonic_ns - value,
            )
        self._last_monotonic_ns = value
        return value

    def rebase_monotonic_clock(self) -> None:
        """Exclude restart or human-approval wait from active-runtime accounting."""

        self._last_monotonic_ns = None

    def _map_storage_error(
        self,
        error: RunStorageError,
        operation_id: str,
    ) -> DurableBudgetError:
        code = error.failure.code
        if code is RunStorageFailureCode.RUN_LOCKED:
            return _failure(DurableFailureCode.RUN_LOCKED, operation_id=operation_id)
        if code is RunStorageFailureCode.RUN_CONFLICT:
            return _failure(DurableFailureCode.CAS_CONFLICT, operation_id=operation_id)
        if code is RunStorageFailureCode.IDEMPOTENCY_CONFLICT:
            return _failure(
                DurableFailureCode.IDEMPOTENCY_CONFLICT,
                operation_id=operation_id,
            )
        if code in {
            RunStorageFailureCode.EFFECT_UNKNOWN,
            RunStorageFailureCode.DURABILITY_UNCONFIRMED,
        }:
            return _failure(
                DurableFailureCode.DURABILITY_UNCERTAIN,
                operation_id=operation_id,
                reconciliation_required=True,
                effect_unknown=True,
            )
        return _failure(
            DurableFailureCode.RECONCILIATION_REQUIRED,
            operation_id=operation_id,
            reconciliation_required=True,
        )

    def _load_history(self, run_id: str) -> tuple[DurableBudgetStateV1, ...]:
        try:
            history = self.revisions._read_structural_artifact_history(
                run_id,
                "budget_state.json",
                artifact_schema_version=DURABLE_BUDGET_ARTIFACT_SCHEMA,
            )
        except RunStorageError as exc:
            raise self._map_storage_error(exc, "read-state") from exc
        try:
            states = tuple(
                DurableBudgetStateV1.model_validate_json(entry.exact_bytes)
                for entry in history.revisions
            )
            if tuple(state.generation for state in states) != tuple(
                entry.revision for entry in history.revisions
            ):
                raise ValueError("budget state generation does not match storage revision")
            if any(state.run_id != run_id for state in states):
                raise ValueError("budget state cross-run replay")
            for newer, older in pairwise(states):
                _validate_state_successor(older, newer)
        except (TypeError, ValueError, ValidationError) as exc:
            raise _failure(
                DurableFailureCode.RECONCILIATION_REQUIRED,
                operation_id="read-state",
                reconciliation_required=True,
            ) from exc
        return states

    def load(self, run_id: str) -> DurableBudgetStateV1:
        """Reconstruct the exact current state from verified structural history."""

        return self._load_history(run_id)[0]

    def resume(self, run_id: str) -> DurableBudgetStateV1:
        """Resume only when no started effect has an unknown durable outcome."""

        self.rebase_monotonic_clock()
        state = self.load(run_id)
        started = next(
            (
                permit
                for permit in state.active_permits
                if permit.status is PermitStatus.STARTED
            ),
            None,
        )
        if started is not None:
            raise _failure(
                DurableFailureCode.EFFECT_UNKNOWN,
                operation_id=started.lineage.idempotency_key,
                reconciliation_required=True,
                effect_unknown=True,
            )
        return state

    def _existing_operation(
        self,
        state: DurableBudgetStateV1,
        *,
        operation_id: str,
        kind: OperationKind,
        request_sha256: str,
    ) -> DurableMutationResultV1 | None:
        record = next(
            (
                item
                for item in state.operations
                if item.operation_id == operation_id
            ),
            None,
        )
        if record is None:
            return None
        if record.kind is not kind or record.request_sha256 != request_sha256:
            raise _failure(
                DurableFailureCode.IDEMPOTENCY_CONFLICT,
                operation_id=operation_id,
            )
        return DurableMutationResultV1(
            status=MutationStatus.EXACT_REPLAY,
            operation_id=operation_id,
            operation_request_sha256=request_sha256,
            subject_id=record.subject_id,
            storage_outcome="current_committed",
            state=state,
        )

    def _append_operation(
        self,
        state: DurableBudgetStateV1,
        *,
        operation_id: str,
        kind: OperationKind,
        request_sha256: str,
        subject_id: str | None,
        result_payload: Any,
        updates: Mapping[str, Any],
    ) -> DurableBudgetStateV1:
        result_sha256 = canonical_durable_sha256(result_payload)
        operation = IdempotencyRecordV1(
            operation_id=operation_id,
            kind=kind,
            request_sha256=request_sha256,
            outcome=OperationOutcome.APPLIED,
            result_sha256=result_sha256,
            subject_id=subject_id,
        )
        ordinal = len(state.events)
        event = DurableEventV1(
            ordinal=ordinal,
            kind=kind,
            operation_id=operation_id,
            subject_id=subject_id,
            event_sha256=canonical_durable_sha256(
                {
                    "ordinal": ordinal,
                    "kind": kind.value,
                    "operation_id": operation_id,
                    "subject_id": subject_id,
                    "result_sha256": result_sha256,
                }
            ),
        )
        return DurableBudgetStateV1.model_validate(
            {
                **state.model_dump(mode="python"),
                **dict(updates),
                "generation": state.generation + 1,
                "previous_state_sha256": state.canonical_sha256,
                "operations": (*state.operations, operation),
                "events": (*state.events, event),
            },
            strict=True,
        )

    def _publish(
        self,
        previous: DurableBudgetStateV1 | None,
        successor: DurableBudgetStateV1,
        *,
        operation_id: str,
        request_sha256: str,
        subject_id: str | None,
    ) -> DurableMutationResultV1:
        if previous is None:
            expected_revision = None
            expected_manifest_sha256 = None
            expected_pointer_sha256 = None
        else:
            try:
                current = self.revisions.read_current(successor.run_id)
            except RunStorageError as exc:
                raise self._map_storage_error(exc, operation_id) from exc
            if current.current_revision != previous.generation:
                current_state = self.load(successor.run_id)
                operation_kind = next(
                    item.kind
                    for item in successor.operations
                    if item.operation_id == operation_id
                )
                replay = self._existing_operation(
                    current_state,
                    operation_id=operation_id,
                    kind=operation_kind,
                    request_sha256=request_sha256,
                )
                if replay is not None:
                    return replay
                raise _failure(
                    DurableFailureCode.CAS_CONFLICT,
                    operation_id=operation_id,
                )
            expected_revision = current.current_revision
            expected_manifest_sha256 = current.manifest_sha256
            expected_pointer_sha256 = current.current_pointer_sha256
        request = RevisionPublishRequestV1(
            run_id=successor.run_id,
            transaction_id=_transaction_id(operation_id),
            proposed_revision=successor.generation,
            expected_revision=expected_revision,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_pointer_sha256=expected_pointer_sha256,
            created_at=self.wall_clock(),
            producer_id=DURABLE_BUDGET_PRODUCER_ID,
            producer_version=DURABLE_BUDGET_PRODUCER_VERSION,
            artifacts=(_artifact(successor),),
        )
        try:
            outcome = self.revisions.publish(request)
        except RunStorageError as exc:
            raise self._map_storage_error(exc, operation_id) from exc
        if outcome.outcome_kind != "published":
            current_state = self.load(successor.run_id)
            replay = self._existing_operation(
                current_state,
                operation_id=operation_id,
                kind=next(
                    item.kind
                    for item in successor.operations
                    if item.operation_id == operation_id
                ),
                request_sha256=request_sha256,
            )
            if replay is None:
                raise _failure(
                    DurableFailureCode.RECONCILIATION_REQUIRED,
                    operation_id=operation_id,
                    reconciliation_required=True,
                )
            return _replace(replay, storage_outcome=outcome.outcome_kind)
        return DurableMutationResultV1(
            status=MutationStatus.APPLIED,
            operation_id=operation_id,
            operation_request_sha256=request_sha256,
            subject_id=subject_id,
            storage_outcome=outcome.outcome_kind,
            state=successor,
        )

    def create(
        self,
        run_id: str,
        policy: DurableBudgetPolicyV1,
        *,
        operation_id: str,
    ) -> DurableMutationResultV1:
        self._observe_monotonic(operation_id)
        request_sha256 = canonical_durable_sha256(
            {
                "kind": OperationKind.INITIALIZE.value,
                "run_id": run_id,
                "policy": policy.model_dump(mode="json"),
            }
        )
        run_path = self.revisions.runs_root / run_id
        if run_path.exists():
            current = self.load(run_id)
            replay = self._existing_operation(
                current,
                operation_id=operation_id,
                kind=OperationKind.INITIALIZE,
                request_sha256=request_sha256,
            )
            if replay is not None:
                return replay
            raise _failure(
                DurableFailureCode.IDEMPOTENCY_CONFLICT,
                operation_id=operation_id,
            )
        result_sha256 = canonical_durable_sha256(
            {"run_id": run_id, "policy_sha256": policy.policy_sha256}
        )
        operation = IdempotencyRecordV1(
            operation_id=operation_id,
            kind=OperationKind.INITIALIZE,
            request_sha256=request_sha256,
            outcome=OperationOutcome.APPLIED,
            result_sha256=result_sha256,
            subject_id=run_id,
        )
        event = DurableEventV1(
            ordinal=0,
            kind=OperationKind.INITIALIZE,
            operation_id=operation_id,
            subject_id=run_id,
            event_sha256=canonical_durable_sha256(
                {
                    "ordinal": 0,
                    "kind": OperationKind.INITIALIZE.value,
                    "operation_id": operation_id,
                    "subject_id": run_id,
                    "result_sha256": result_sha256,
                }
            ),
        )
        state = DurableBudgetStateV1(
            run_id=run_id,
            generation=1,
            policy=policy,
            policy_sha256=policy.policy_sha256,
            activation_sha256=policy.activation_sha256,
            operations=(operation,),
            events=(event,),
            active_runtime_remaining_ns=policy.base_policy.runtime_limit_ns,
        )
        return self._publish(
            None,
            state,
            operation_id=operation_id,
            request_sha256=request_sha256,
            subject_id=run_id,
        )

    def _resource_limits(self, policy: BudgetPolicyV2) -> dict[str, int]:
        return {
            "active_runtime_ns": policy.runtime_limit_ns,
            "provider_attempts": policy.max_deliberation_rounds,
            "tool_attempts": policy.max_deliberation_rounds
            + policy.max_tool_retries,
            "retry_attempts": policy.max_tool_retries,
            "external_cost_micro_usd": policy.max_external_cost_micro_usd,
            "provider_output_bytes": policy.max_provider_output_bytes,
            "tool_input_bytes": policy.max_tool_input_bytes,
            "tool_output_bytes": policy.max_tool_output_bytes,
            "artifact_bytes": policy.max_artifact_bytes,
            "run_bytes": policy.max_run_bytes,
            "concurrency_slots": 1,
        }

    def _admit_reservation(
        self,
        state: DurableBudgetStateV1,
        reservation: ResourceReservationV1,
        operation_id: str,
    ) -> None:
        if state.failure_latch is not None:
            raise _failure(
                DurableFailureCode.FAILURE_LATCHED,
                operation_id=operation_id,
            )
        if state.active_runtime_remaining_ns == 0:
            raise _failure(
                DurableFailureCode.DEADLINE_EXCEEDED,
                operation_id=operation_id,
                resource="active_runtime_ns",
            )
        if reservation.request_sha256 != reservation_request_sha256(
            reservation_id=reservation.reservation_id,
            requested=reservation.requested,
            execution_class=reservation.execution_class,
            external_cost_estimate_authenticated=(
                reservation.external_cost_estimate_authenticated
            ),
        ):
            raise _failure(
                DurableFailureCode.INVALID_INPUT,
                operation_id=operation_id,
            )
        limits = self._resource_limits(state.policy.base_policy)
        cumulative = {
            "active_runtime_ns",
            "provider_attempts",
            "tool_attempts",
            "retry_attempts",
            "external_cost_micro_usd",
            "run_bytes",
        }
        active = state.active_permits
        for resource in RESOURCE_ORDER:
            requested = int(getattr(reservation.requested, resource))
            if resource == "concurrency_slots":
                observed = len(active) + requested
                limit = state.policy.activation.max_concurrent_agents
            elif resource in cumulative:
                observed = (
                    int(getattr(state.usage, resource))
                    + sum(
                        int(getattr(permit.reservation.requested, resource))
                        for permit in active
                    )
                    + requested
                )
                limit = limits[resource]
            else:
                observed = requested
                limit = limits[resource]
            if observed > limit:
                code = (
                    DurableFailureCode.CONCURRENCY_EXCEEDED
                    if resource == "concurrency_slots"
                    else DurableFailureCode.BUDGET_EXCEEDED
                )
                raise _failure(
                    code,
                    operation_id=operation_id,
                    resource=resource,
                    limit=limit,
                    observed=observed,
                )

    def reserve(
        self,
        run_id: str,
        *,
        operation_id: str,
        permit_id: str,
        reservation: ResourceReservationV1,
        lineage: ExecutionLineageV1,
    ) -> DurableMutationResultV1:
        observed = self._observe_monotonic(operation_id)
        state = self.load(run_id)
        request_payload = {
            "kind": OperationKind.RESERVE.value,
            "permit_id": permit_id,
            "reservation": reservation.model_dump(mode="json"),
            "lineage": lineage.model_dump(mode="json"),
        }
        request_sha256 = canonical_durable_sha256(request_payload)
        replay = self._existing_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.RESERVE,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        self._admit_reservation(state, reservation, operation_id)
        all_permit_ids = {
            *(permit.permit_id for permit in state.active_permits),
            *(settlement.permit_id for settlement in state.settlements),
        }
        if permit_id in all_permit_ids:
            raise _failure(
                DurableFailureCode.IDEMPOTENCY_CONFLICT,
                operation_id=operation_id,
            )
        permit = DurablePermitV1(
            permit_id=permit_id,
            reservation=reservation,
            lineage=lineage,
            reserved_monotonic_ns=observed,
        )
        attempt = AttemptRecordV1(
            permit_id=permit_id,
            lineage=lineage,
            status=AttemptStatus.RESERVED,
        )
        successor = self._append_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.RESERVE,
            request_sha256=request_sha256,
            subject_id=permit_id,
            result_payload=permit,
            updates={
                "active_permits": (*state.active_permits, permit),
                "attempts": (*state.attempts, attempt),
            },
        )
        return self._publish(
            state,
            successor,
            operation_id=operation_id,
            request_sha256=request_sha256,
            subject_id=permit_id,
        )

    def start(
        self,
        run_id: str,
        *,
        operation_id: str,
        permit_id: str,
    ) -> DurableMutationResultV1:
        observed = self._observe_monotonic(operation_id)
        state = self.load(run_id)
        request_sha256 = canonical_durable_sha256(
            {
                "kind": OperationKind.START.value,
                "permit_id": permit_id,
            }
        )
        replay = self._existing_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.START,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        permit = next(
            (item for item in state.active_permits if item.permit_id == permit_id),
            None,
        )
        if permit is None or permit.status is not PermitStatus.RESERVED:
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        cancellation = next(
            (item for item in state.cancellations if item.permit_id == permit_id),
            None,
        )
        if cancellation is not None and cancellation.state is not CancellationState.NOT_REQUESTED:
            raise _failure(DurableFailureCode.CANCEL_UNCONFIRMED, operation_id=operation_id)
        started = _replace(
            permit,
            status=PermitStatus.STARTED,
            started_monotonic_ns=observed,
        )
        permits = tuple(
            started if item.permit_id == permit_id else item
            for item in state.active_permits
        )
        attempts = tuple(
            _replace(item, status=AttemptStatus.STARTED)
            if item.permit_id == permit_id
            else item
            for item in state.attempts
        )
        successor = self._append_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.START,
            request_sha256=request_sha256,
            subject_id=permit_id,
            result_payload=started,
            updates={"active_permits": permits, "attempts": attempts},
        )
        return self._publish(
            state,
            successor,
            operation_id=operation_id,
            request_sha256=request_sha256,
            subject_id=permit_id,
        )

    def _released(
        self,
        reserved: ResourceAmountsV1,
        actual: ResourceAmountsV1,
    ) -> tuple[ResourceAmountsV1, bool]:
        values: dict[str, int] = {}
        overrun = False
        for resource in RESOURCE_ORDER:
            reserved_value = int(getattr(reserved, resource))
            actual_value = int(getattr(actual, resource))
            if actual_value > reserved_value:
                overrun = True
                values[resource] = 0
            else:
                values[resource] = reserved_value - actual_value
        return ResourceAmountsV1.model_validate(values), overrun

    def settle(
        self,
        run_id: str,
        *,
        operation_id: str,
        settlement_id: str,
        permit_id: str,
        actual: ResourceAmountsV1,
        status: SettlementStatus,
        result_sha256: str | None = None,
        effect_evidence_sha256: str | None = None,
        cancellation_evidence_sha256: str | None = None,
        deterministic_tool_evidence: DeterministicToolEvidenceV1 | None = None,
        external_cost_actual_authenticated: bool = False,
    ) -> DurableMutationResultV1:
        observed = self._observe_monotonic(operation_id)
        state = self.load(run_id)
        request_payload = {
            "kind": OperationKind.SETTLE.value,
            "settlement_id": settlement_id,
            "permit_id": permit_id,
            "actual": actual.model_dump(mode="json"),
            "status": status.value,
            "result_sha256": result_sha256,
            "effect_evidence_sha256": effect_evidence_sha256,
            "cancellation_evidence_sha256": cancellation_evidence_sha256,
            "deterministic_tool_evidence": (
                None
                if deterministic_tool_evidence is None
                else deterministic_tool_evidence.model_dump(mode="json")
            ),
            "external_cost_actual_authenticated": external_cost_actual_authenticated,
        }
        request_sha256 = canonical_durable_sha256(request_payload)
        replay = self._existing_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.SETTLE,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        permit = next(
            (item for item in state.active_permits if item.permit_id == permit_id),
            None,
        )
        if permit is None or permit.status is not PermitStatus.STARTED:
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        if status is SettlementStatus.RELEASED_NO_EFFECT:
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        if (
            permit.reservation.execution_class is ExecutionClass.EXTERNAL
            and not external_cost_actual_authenticated
        ):
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        if (
            permit.reservation.execution_class is ExecutionClass.LOCAL_FREE
            and (
                actual.external_cost_micro_usd != 0
                or external_cost_actual_authenticated
            )
        ):
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        if status is SettlementStatus.SUCCEEDED and (
            result_sha256 is None or effect_evidence_sha256 is None
        ):
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        if deterministic_tool_evidence is not None and (
            deterministic_tool_evidence.execution_ordinal
            != permit.lineage.execution_ordinal
            or deterministic_tool_evidence.tool_result_bytes_sha256
            != result_sha256
        ):
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        released, overrun = self._released(permit.reservation.requested, actual)
        effective_status = SettlementStatus.OVERRUN if overrun else status
        settlement = DurableSettlementV1(
            settlement_id=settlement_id,
            permit_id=permit_id,
            operation_id=operation_id,
            operation_request_sha256=request_sha256,
            reserved=permit.reservation.requested,
            actual=actual,
            released=released,
            status=effective_status,
            result_sha256=result_sha256,
            effect_evidence_sha256=effect_evidence_sha256,
            cancellation_evidence_sha256=cancellation_evidence_sha256,
            deterministic_tool_evidence=deterministic_tool_evidence,
            settled_monotonic_ns=observed,
        )
        usage = state.usage.apply_actual(actual)
        failure_latch = state.failure_latch
        attempt_status = {
            SettlementStatus.SUCCEEDED: AttemptStatus.SUCCEEDED,
            SettlementStatus.FAILED: AttemptStatus.FAILED,
            SettlementStatus.CANCELLED: AttemptStatus.CANCELLED,
            SettlementStatus.EFFECT_UNKNOWN: AttemptStatus.EFFECT_UNKNOWN,
            SettlementStatus.OVERRUN: AttemptStatus.FAILED,
        }[effective_status]
        if effective_status is SettlementStatus.OVERRUN:
            exceeded = next(
                resource
                for resource in RESOURCE_ORDER
                if int(getattr(actual, resource))
                > int(getattr(permit.reservation.requested, resource))
            )
            failure_latch = DurableBudgetFailureV1(
                code=DurableFailureCode.SETTLEMENT_OVERRUN,
                operation_id=operation_id,
                resource=exceeded,
                limit=int(getattr(permit.reservation.requested, exceeded)),
                observed=int(getattr(actual, exceeded)),
                reconciliation_required=True,
                evidence_sha256=effect_evidence_sha256,
            )
        elif effective_status is SettlementStatus.EFFECT_UNKNOWN:
            failure_latch = DurableBudgetFailureV1(
                code=DurableFailureCode.EFFECT_UNKNOWN,
                operation_id=operation_id,
                reconciliation_required=True,
                effect_unknown=True,
                evidence_sha256=effect_evidence_sha256,
            )
        attempts = tuple(
            _replace(
                item,
                status=attempt_status,
                effect_evidence_sha256=effect_evidence_sha256,
            )
            if item.permit_id == permit_id
            else item
            for item in state.attempts
        )
        successor = self._append_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.SETTLE,
            request_sha256=request_sha256,
            subject_id=settlement_id,
            result_payload=settlement,
            updates={
                "active_permits": tuple(
                    item for item in state.active_permits if item.permit_id != permit_id
                ),
                "settlements": (*state.settlements, settlement),
                "attempts": attempts,
                "usage": usage,
                "active_runtime_remaining_ns": max(
                    0,
                    state.policy.base_policy.runtime_limit_ns
                    - usage.active_runtime_ns,
                ),
                "failure_latch": failure_latch,
            },
        )
        return self._publish(
            state,
            successor,
            operation_id=operation_id,
            request_sha256=request_sha256,
            subject_id=settlement_id,
        )

    def release_no_effect(
        self,
        run_id: str,
        *,
        operation_id: str,
        settlement_id: str,
        permit_id: str,
        evidence_sha256: str,
    ) -> DurableMutationResultV1:
        observed = self._observe_monotonic(operation_id)
        state = self.load(run_id)
        request_sha256 = canonical_durable_sha256(
            {
                "kind": OperationKind.RELEASE_NO_EFFECT.value,
                "settlement_id": settlement_id,
                "permit_id": permit_id,
                "evidence_sha256": evidence_sha256,
            }
        )
        replay = self._existing_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.RELEASE_NO_EFFECT,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        permit = next(
            (item for item in state.active_permits if item.permit_id == permit_id),
            None,
        )
        if permit is None or permit.status is not PermitStatus.RESERVED:
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        settlement = DurableSettlementV1(
            settlement_id=settlement_id,
            permit_id=permit_id,
            operation_id=operation_id,
            operation_request_sha256=request_sha256,
            reserved=permit.reservation.requested,
            actual=ResourceAmountsV1(),
            released=permit.reservation.requested,
            status=SettlementStatus.RELEASED_NO_EFFECT,
            effect_evidence_sha256=evidence_sha256,
            settled_monotonic_ns=observed,
        )
        attempts = tuple(
            _replace(
                item,
                status=AttemptStatus.CANCELLED,
                effect_evidence_sha256=evidence_sha256,
            )
            if item.permit_id == permit_id
            else item
            for item in state.attempts
        )
        successor = self._append_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.RELEASE_NO_EFFECT,
            request_sha256=request_sha256,
            subject_id=settlement_id,
            result_payload=settlement,
            updates={
                "active_permits": tuple(
                    item for item in state.active_permits if item.permit_id != permit_id
                ),
                "settlements": (*state.settlements, settlement),
                "attempts": attempts,
            },
        )
        return self._publish(
            state,
            successor,
            operation_id=operation_id,
            request_sha256=request_sha256,
            subject_id=settlement_id,
        )

    def request_cancellation(
        self,
        run_id: str,
        *,
        operation_id: str,
        permit_id: str,
    ) -> DurableMutationResultV1:
        observed = self._observe_monotonic(operation_id)
        state = self.load(run_id)
        request_sha256 = canonical_durable_sha256(
            {
                "kind": OperationKind.REQUEST_CANCEL.value,
                "permit_id": permit_id,
            }
        )
        replay = self._existing_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.REQUEST_CANCEL,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        if not any(item.permit_id == permit_id for item in state.active_permits):
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        previous = next(
            (item for item in state.cancellations if item.permit_id == permit_id),
            None,
        )
        if previous is not None and previous.state is not CancellationState.NOT_REQUESTED:
            raise _failure(
                DurableFailureCode.IDEMPOTENCY_CONFLICT,
                operation_id=operation_id,
            )
        cancellation = DurableCancellationV1(
            permit_id=permit_id,
            state=CancellationState.REQUESTED,
            requested_operation_id=operation_id,
            worker_live=True,
            observed_monotonic_ns=observed,
        )
        cancellations = (
            *(
                item
                for item in state.cancellations
                if item.permit_id != permit_id
            ),
            cancellation,
        )
        successor = self._append_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.REQUEST_CANCEL,
            request_sha256=request_sha256,
            subject_id=permit_id,
            result_payload=cancellation,
            updates={"cancellations": cancellations},
        )
        return self._publish(
            state,
            successor,
            operation_id=operation_id,
            request_sha256=request_sha256,
            subject_id=permit_id,
        )

    def record_cancellation(
        self,
        run_id: str,
        *,
        operation_id: str,
        permit_id: str,
        state_value: CancellationState,
        evidence_sha256: str | None,
        worker_live: bool,
    ) -> DurableMutationResultV1:
        observed = self._observe_monotonic(operation_id)
        state = self.load(run_id)
        request_sha256 = canonical_durable_sha256(
            {
                "kind": OperationKind.ACKNOWLEDGE_CANCEL.value,
                "permit_id": permit_id,
                "state": state_value.value,
                "evidence_sha256": evidence_sha256,
                "worker_live": worker_live,
            }
        )
        replay = self._existing_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.ACKNOWLEDGE_CANCEL,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        previous = next(
            (item for item in state.cancellations if item.permit_id == permit_id),
            None,
        )
        if (
            previous is None
            or previous.state is not CancellationState.REQUESTED
            or state_value
            not in {
                CancellationState.ACKNOWLEDGED,
                CancellationState.CANCELLED,
                CancellationState.UNCONFIRMED,
                CancellationState.EFFECT_UNKNOWN,
            }
        ):
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        if state_value in {
            CancellationState.ACKNOWLEDGED,
            CancellationState.CANCELLED,
        } and evidence_sha256 is None:
            raise _failure(DurableFailureCode.INVALID_INPUT, operation_id=operation_id)
        cancellation = DurableCancellationV1(
            permit_id=permit_id,
            state=state_value,
            requested_operation_id=previous.requested_operation_id,
            evidence_sha256=evidence_sha256,
            worker_live=worker_live,
            observed_monotonic_ns=observed,
        )
        failure_latch = state.failure_latch
        attempts = state.attempts
        if state_value in {
            CancellationState.UNCONFIRMED,
            CancellationState.EFFECT_UNKNOWN,
        }:
            failure_latch = DurableBudgetFailureV1(
                code=(
                    DurableFailureCode.CANCEL_UNCONFIRMED
                    if state_value is CancellationState.UNCONFIRMED
                    else DurableFailureCode.EFFECT_UNKNOWN
                ),
                operation_id=operation_id,
                reconciliation_required=True,
                effect_unknown=True,
                evidence_sha256=evidence_sha256,
            )
            attempts = tuple(
                _replace(
                    item,
                    status=AttemptStatus.EFFECT_UNKNOWN,
                    effect_evidence_sha256=evidence_sha256,
                )
                if item.permit_id == permit_id
                else item
                for item in attempts
            )
        cancellations = (
            *(
                item
                for item in state.cancellations
                if item.permit_id != permit_id
            ),
            cancellation,
        )
        successor = self._append_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.ACKNOWLEDGE_CANCEL,
            request_sha256=request_sha256,
            subject_id=permit_id,
            result_payload=cancellation,
            updates={
                "cancellations": cancellations,
                "failure_latch": failure_latch,
                "attempts": attempts,
            },
        )
        return self._publish(
            state,
            successor,
            operation_id=operation_id,
            request_sha256=request_sha256,
            subject_id=permit_id,
        )

    def tighten_policy(
        self,
        run_id: str,
        *,
        operation_id: str,
        new_policy: DurableBudgetPolicyV1,
        reason_sha256: str,
    ) -> DurableMutationResultV1:
        self._observe_monotonic(operation_id)
        state = self.load(run_id)
        request_sha256 = canonical_durable_sha256(
            {
                "kind": OperationKind.TIGHTEN_POLICY.value,
                "old_policy_sha256": state.policy.canonical_sha256,
                "new_policy": new_policy.model_dump(mode="json"),
                "reason_sha256": reason_sha256,
            }
        )
        replay = self._existing_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.TIGHTEN_POLICY,
            request_sha256=request_sha256,
        )
        if replay is not None:
            return replay
        if state.failure_latch is not None or not _is_policy_tightening(
            state.policy,
            new_policy,
        ):
            raise _failure(
                DurableFailureCode.POLICY_MISMATCH,
                operation_id=operation_id,
            )
        limits = self._resource_limits(new_policy.base_policy)
        cumulative = {
            "active_runtime_ns",
            "provider_attempts",
            "tool_attempts",
            "retry_attempts",
            "external_cost_micro_usd",
            "run_bytes",
        }
        for resource in RESOURCE_ORDER:
            if resource == "concurrency_slots":
                observed = len(state.active_permits)
                limit = new_policy.activation.max_concurrent_agents
            elif resource in cumulative:
                observed = int(getattr(state.usage, resource)) + sum(
                    int(getattr(item.reservation.requested, resource))
                    for item in state.active_permits
                )
                limit = limits[resource]
            else:
                observed = max(
                    (
                        int(getattr(state.usage, resource)),
                        *(
                            int(getattr(item.reservation.requested, resource))
                            for item in state.active_permits
                        ),
                    )
                )
                limit = limits[resource]
            if observed > limit:
                raise _failure(
                    DurableFailureCode.POLICY_MISMATCH,
                    operation_id=operation_id,
                    resource=resource,
                    limit=limit,
                    observed=observed,
                )
        successor = self._append_operation(
            state,
            operation_id=operation_id,
            kind=OperationKind.TIGHTEN_POLICY,
            request_sha256=request_sha256,
            subject_id=run_id,
            result_payload={
                "old_policy_sha256": state.policy.canonical_sha256,
                "new_policy_sha256": new_policy.canonical_sha256,
                "reason_sha256": reason_sha256,
            },
            updates={
                "policy": new_policy,
                "policy_sha256": new_policy.policy_sha256,
                "activation_sha256": new_policy.activation_sha256,
                "active_runtime_remaining_ns": (
                    new_policy.base_policy.runtime_limit_ns
                    - state.usage.active_runtime_ns
                ),
            },
        )
        return self._publish(
            state,
            successor,
            operation_id=operation_id,
            request_sha256=request_sha256,
            subject_id=run_id,
        )


__all__ = [
    "DurableBudgetError",
    "DurableBudgetStore",
    "build_resource_reservation",
    "initialize_durable_budget_root",
    "reservation_request_sha256",
]
