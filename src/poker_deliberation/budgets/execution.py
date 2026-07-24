"""Internal bounded retry, concurrency, cancellation, and RM-028 boundary."""

from __future__ import annotations

import hashlib
import math
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation.budgets.durable_models import (
    DURABLE_BUDGET_SCHEMA_VERSION,
    CancellationState,
    DeterministicToolEvidenceV1,
    DurableBudgetFailureV1,
    DurableFailureCode,
    ExecutionLineageV1,
    MutationStatus,
    ResourceAmountsV1,
    ResourceReservationV1,
    SettlementStatus,
    canonical_durable_sha256,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetError,
    DurableBudgetStore,
    build_resource_reservation,
)
from poker_deliberation.budgets.retry import (
    FailureCategory,
    IdempotencyStatus,
)
from poker_deliberation.context_lifecycle import (
    ContextEnvelope,
    ContextLifecycleError,
    build_retry_context_envelope,
    validate_context_envelope,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext

_SHA256 = r"^[0-9a-f]{64}$"
_ID = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class _ExecutionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
    )


class EffectStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EFFECT_UNKNOWN = "effect_unknown"


RetryReason = Literal[
    "admitted",
    "activation_disabled",
    "attempt_limit",
    "category_forbidden",
    "idempotency_unconfirmed",
    "reconciliation_unconfirmed",
    "fresh_context_factory_missing",
]


class RetryAdmissionV1(_ExecutionModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    admitted: bool
    completed_retries: int = Field(ge=0)
    max_automatic_retries: int = Field(ge=0, le=10)
    max_attempts: int = Field(ge=1, le=11)
    category: FailureCategory
    idempotency: IdempotencyStatus
    reason_code: RetryReason

    @model_validator(mode="after")
    def retry_count_is_bounded(self) -> RetryAdmissionV1:
        if self.max_attempts != self.max_automatic_retries + 1:
            raise ValueError("retry admission max attempts must be N+1")
        if self.admitted != (self.reason_code == "admitted"):
            raise ValueError("retry admission status and reason disagree")
        return self


def admit_automatic_retry(
    *,
    category: FailureCategory,
    idempotency: IdempotencyStatus,
    completed_retries: int,
    max_automatic_retries: int,
    authoritative_reconciliation_confirmed: bool = False,
    fresh_context_factory_available: bool = True,
) -> RetryAdmissionV1:
    """Pure retry admission; it performs no retry or external effect."""

    if isinstance(completed_retries, bool) or not isinstance(completed_retries, int):
        raise TypeError("completed_retries must be an integer")
    if (
        isinstance(max_automatic_retries, bool)
        or not isinstance(max_automatic_retries, int)
        or completed_retries < 0
        or max_automatic_retries < 0
        or max_automatic_retries > 10
    ):
        raise ValueError("retry counts are outside the approved bounds")
    transient = category in {
        FailureCategory.PROVIDER_TRANSIENT,
        FailureCategory.TOOL_TRANSIENT,
    }
    if max_automatic_retries == 0:
        reason: RetryReason = "activation_disabled"
    elif completed_retries >= max_automatic_retries:
        reason = "attempt_limit"
    elif not transient:
        reason = "category_forbidden"
    elif idempotency not in {
        IdempotencyStatus.IDEMPOTENT,
        IdempotencyStatus.RECONCILABLE,
    }:
        reason = "idempotency_unconfirmed"
    elif (
        idempotency is IdempotencyStatus.RECONCILABLE and not authoritative_reconciliation_confirmed
    ):
        reason = "reconciliation_unconfirmed"
    elif not fresh_context_factory_available:
        reason = "fresh_context_factory_missing"
    else:
        reason = "admitted"
    return RetryAdmissionV1(
        admitted=reason == "admitted",
        completed_retries=completed_retries,
        max_automatic_retries=max_automatic_retries,
        max_attempts=max_automatic_retries + 1,
        category=category,
        idempotency=idempotency,
        reason_code=reason,
    )


class EffectResultV1(_ExecutionModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    status: EffectStatus
    actual: ResourceAmountsV1
    result_sha256: str | None = Field(default=None, pattern=_SHA256)
    effect_evidence_sha256: str | None = Field(default=None, pattern=_SHA256)
    cancellation_evidence_sha256: str | None = Field(default=None, pattern=_SHA256)
    failure_category: FailureCategory | None = None
    idempotency: IdempotencyStatus = IdempotencyStatus.UNKNOWN
    authoritative_reconciliation_confirmed: bool = False
    external_cost_actual_authenticated: bool = False
    deterministic_tool_evidence: DeterministicToolEvidenceV1 | None = None

    @model_validator(mode="after")
    def evidence_matches_status(self) -> EffectResultV1:
        if self.actual.concurrency_slots != 1:
            raise ValueError("an executed effect must report exactly one used slot")
        if self.status is EffectStatus.SUCCEEDED and (
            self.result_sha256 is None or self.effect_evidence_sha256 is None
        ):
            raise ValueError("successful effect requires result and effect evidence")
        if self.status is EffectStatus.FAILED and self.failure_category is None:
            raise ValueError("failed effect requires a typed failure category")
        if self.status is EffectStatus.CANCELLED and (self.cancellation_evidence_sha256 is None):
            raise ValueError("cancelled effect requires cooperative cancellation evidence")
        if (
            self.authoritative_reconciliation_confirmed
            and self.idempotency is not IdempotencyStatus.RECONCILABLE
        ):
            raise ValueError("authoritative reconciliation requires reconcilable idempotency")
        return self


class IsolationRequirementV1(_ExecutionModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    process_tree_termination: bool = False
    remote_cancellation_guarantee: bool = False
    os_resource_isolation: bool = False
    external_code_isolation: bool = False

    @property
    def required(self) -> bool:
        return any(
            (
                self.process_tree_termination,
                self.remote_cancellation_guarantee,
                self.os_resource_isolation,
                self.external_code_isolation,
            )
        )

    @property
    def request_sha256(self) -> str:
        return canonical_durable_sha256(self)


class RM028IsolationEvidenceV1(_ExecutionModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    requirement_sha256: str = Field(pattern=_SHA256)
    boundary_id: str = Field(pattern=_ID)
    isolation_evidence_sha256: str = Field(pattern=_SHA256)
    process_tree_termination_confirmed: bool = False
    remote_cancellation_confirmed: bool = False
    os_resource_isolation_confirmed: bool = False
    external_code_isolation_confirmed: bool = False

    def satisfies(self, requirement: IsolationRequirementV1) -> bool:
        return (
            self.requirement_sha256 == requirement.request_sha256
            and (
                not requirement.process_tree_termination or self.process_tree_termination_confirmed
            )
            and (
                not requirement.remote_cancellation_guarantee or self.remote_cancellation_confirmed
            )
            and (not requirement.os_resource_isolation or self.os_resource_isolation_confirmed)
            and (not requirement.external_code_isolation or self.external_code_isolation_confirmed)
        )


class RM028IsolationBoundary(Protocol):
    """Evidence-only interface.  P2-011B ships no implementation."""

    def inspect(self, requirement: IsolationRequirementV1) -> RM028IsolationEvidenceV1: ...


class CooperativeCancellationToken:
    """Thread-safe cooperative signal; it is not a hard-stop primitive."""

    def __init__(self) -> None:
        self._requested = threading.Event()
        self._acknowledged = threading.Event()
        self._lock = threading.Lock()
        self._evidence_sha256: str | None = None

    @property
    def requested(self) -> bool:
        return self._requested.is_set()

    @property
    def acknowledged(self) -> bool:
        return self._acknowledged.is_set()

    @property
    def evidence_sha256(self) -> str | None:
        with self._lock:
            return self._evidence_sha256

    def request(self) -> None:
        self._requested.set()

    def acknowledge(self, evidence_sha256: str) -> None:
        if (
            not isinstance(evidence_sha256, str)
            or len(evidence_sha256) != 64
            or any(character not in "0123456789abcdef" for character in evidence_sha256)
        ):
            raise ValueError("cancellation evidence must be lowercase SHA-256")
        if not self.requested:
            raise ValueError("cancellation cannot be acknowledged before request")
        with self._lock:
            self._evidence_sha256 = evidence_sha256
            self._acknowledged.set()


EffectCallable = Callable[
    [CooperativeCancellationToken, ExecutionLineageV1],
    EffectResultV1,
]


class DurableRetryContextV1(_ExecutionModel):
    """Attempt-memory-only retry context evidence; never part of durable state."""

    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    lineage: ExecutionLineageV1
    envelope: ContextEnvelope
    assignment: AgentAssignment

    @model_validator(mode="after")
    def envelope_matches_durable_lineage(self) -> DurableRetryContextV1:
        envelope_lineage = self.envelope.lineage
        if (
            self.lineage.assignment_id != self.assignment.assignment_id
            or self.lineage.role != self.assignment.agent_role
            or self.lineage.assignment_id != envelope_lineage.assignment_id
            or self.lineage.attempt_id != envelope_lineage.attempt_id
            or self.lineage.context_id != envelope_lineage.context_id
            or self.lineage.parent_context_id != envelope_lineage.parent_context_id
            or self.lineage.context_source_sha256 != envelope_lineage.source_sha256
            or self.lineage.context_policy_sha256 != self.envelope.policy_sha256
            or self.lineage.context_integrity_sha256 != self.envelope.integrity_sha256
        ):
            raise ValueError("retry envelope and durable lineage differ")
        return self


RetryLineageFactory = Callable[[ExecutionLineageV1, int], DurableRetryContextV1]


@dataclass(frozen=True)
class DurableExecutionTask:
    task_id: str
    execution_ordinal: int
    reservation: ResourceReservationV1
    lineage: ExecutionLineageV1
    effect: EffectCallable
    retry_lineage_factory: RetryLineageFactory | None = None
    isolation_requirement: IsolationRequirementV1 = dataclass_field(
        default_factory=IsolationRequirementV1
    )

    def __post_init__(self) -> None:
        if (
            not self.task_id
            or len(self.task_id) > 64
            or not all(character.isalnum() or character in "._:-" for character in self.task_id)
        ):
            raise ValueError("task_id must use the portable correlation format")
        if self.execution_ordinal != self.lineage.execution_ordinal:
            raise ValueError("task and lineage execution ordinals differ")


class ExecutionAttemptResultV1(_ExecutionModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    attempt_index: int = Field(ge=0, le=10)
    attempt_id: str = Field(pattern=_ID)
    context_id: str = Field(pattern=_ID)
    permit_id: str = Field(pattern=_ID)
    settlement_id: str | None = Field(default=None, pattern=_ID)
    status: EffectStatus
    result_sha256: str | None = Field(default=None, pattern=_SHA256)
    effect_evidence_sha256: str | None = Field(default=None, pattern=_SHA256)
    failure_category: FailureCategory | None = None


class DurableExecutionRecordV1(_ExecutionModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    task_id: str = Field(pattern=_ID)
    execution_ordinal: int = Field(ge=0)
    attempts: tuple[ExecutionAttemptResultV1, ...]
    final_status: EffectStatus
    final_result_sha256: str | None = Field(default=None, pattern=_SHA256)

    @model_validator(mode="after")
    def attempt_sequence_is_exact(self) -> DurableExecutionRecordV1:
        if not self.attempts:
            raise ValueError("execution record requires at least one attempt")
        if tuple(item.attempt_index for item in self.attempts) != tuple(range(len(self.attempts))):
            raise ValueError("execution attempts must use contiguous indices")
        if self.attempts[-1].status is not self.final_status:
            raise ValueError("final execution status differs from the last attempt")
        return self


class DurableExecutionResultV1(_ExecutionModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    records: tuple[DurableExecutionRecordV1, ...]
    peak_concurrency: int = Field(ge=0, le=32)
    cancellation_state: CancellationState
    isolation_evidence: tuple[RM028IsolationEvidenceV1, ...] = ()

    @model_validator(mode="after")
    def records_use_stable_ordinal_order(self) -> DurableExecutionResultV1:
        if tuple(item.execution_ordinal for item in self.records) != tuple(
            sorted(item.execution_ordinal for item in self.records)
        ):
            raise ValueError("execution records must use stable ordinal order")
        return self


def build_durable_retry_lineage(
    parent_lineage: ExecutionLineageV1,
    parent_envelope: ContextEnvelope,
    context: AgentContext,
    assignment: AgentAssignment,
    *,
    run_id: str,
    expires_at: datetime,
    clock: Callable[[], datetime],
    context_id: str,
    attempt_id: str,
    idempotency_key: str,
    idempotency_request_sha256: str,
) -> DurableRetryContextV1:
    """Build and bind one fresh retry ContextEnvelope without persisting payload."""

    if (
        parent_lineage.attempt_id != parent_envelope.lineage.attempt_id
        or parent_lineage.context_id != parent_envelope.lineage.context_id
        or parent_lineage.context_source_sha256 != parent_envelope.lineage.source_sha256
        or parent_lineage.context_policy_sha256 != parent_envelope.policy_sha256
        or parent_lineage.context_integrity_sha256 != parent_envelope.integrity_sha256
    ):
        raise ValueError("parent durable/context lineage mismatch")
    retry = build_retry_context_envelope(
        parent_envelope,
        context,
        assignment,
        run_id=run_id,
        expires_at=expires_at,
        clock=clock,
        context_id=context_id,
        attempt_id=attempt_id,
    )
    lineage = ExecutionLineageV1(
        owner_kind=parent_lineage.owner_kind,
        owner_id=parent_lineage.owner_id,
        role=parent_lineage.role,
        phase_id=parent_lineage.phase_id,
        assignment_id=retry.lineage.assignment_id,
        root_attempt_id=parent_lineage.root_attempt_id,
        parent_attempt_id=parent_lineage.attempt_id,
        attempt_id=retry.lineage.attempt_id,
        root_context_id=parent_lineage.root_context_id,
        parent_context_id=parent_lineage.context_id,
        context_id=retry.lineage.context_id,
        context_source_sha256=retry.lineage.source_sha256,
        context_policy_sha256=retry.policy_sha256,
        context_integrity_sha256=retry.integrity_sha256,
        execution_ordinal=parent_lineage.execution_ordinal,
        idempotency_key=idempotency_key,
        idempotency_request_sha256=idempotency_request_sha256,
    )
    return DurableRetryContextV1(
        lineage=lineage,
        envelope=retry,
        assignment=assignment,
    )


def _constant_evidence(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


@dataclass
class _ConcurrentPeak:
    lock: threading.Lock
    current: int = 0
    peak: int = 0

    def enter(self) -> None:
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)

    def exit(self) -> None:
        with self.lock:
            self.current -= 1


class DurableBoundedExecutor:
    """Opt-in local callable adapter; it is not connected to the product path."""

    def __init__(
        self,
        store: DurableBudgetStore,
        run_id: str,
        *,
        isolation_boundary: RM028IsolationBoundary | None = None,
    ) -> None:
        self.store = store
        self.run_id = run_id
        self.isolation_boundary = isolation_boundary

    def _isolation_evidence(
        self,
        tasks: Sequence[DurableExecutionTask],
    ) -> tuple[RM028IsolationEvidenceV1, ...]:
        evidence: list[RM028IsolationEvidenceV1] = []
        for task in tasks:
            requirement = task.isolation_requirement
            if not requirement.required:
                continue
            if self.isolation_boundary is None:
                raise DurableBudgetError(failure=self._isolation_failure(task.task_id))
            item = self.isolation_boundary.inspect(requirement)
            if not item.satisfies(requirement):
                raise DurableBudgetError(failure=self._isolation_failure(task.task_id))
            evidence.append(item)
        return tuple(evidence)

    def _isolation_failure(self, operation_id: str) -> DurableBudgetFailureV1:
        return DurableBudgetFailureV1(
            code=DurableFailureCode.ISOLATION_REQUIRED,
            operation_id=operation_id,
        )

    def _attempt_ids(
        self,
        task: DurableExecutionTask,
        attempt_index: int,
    ) -> tuple[str, str, str, str]:
        stem = f"{task.task_id}.attempt-{attempt_index}"
        return (
            f"permit-{stem}",
            f"{stem}.reserve",
            f"{stem}.start",
            f"{stem}.settle",
        )

    def _retry_reservation(
        self,
        original: ResourceReservationV1,
        task: DurableExecutionTask,
        attempt_index: int,
    ) -> ResourceReservationV1:
        requested = ResourceAmountsV1.model_validate(
            {
                **original.requested.model_dump(mode="python"),
                "retry_attempts": 1,
            },
            strict=True,
        )
        return build_resource_reservation(
            reservation_id=f"{task.task_id}.reservation-{attempt_index}",
            requested=requested,
            execution_class=original.execution_class,
            external_cost_estimate_authenticated=(original.external_cost_estimate_authenticated),
        )

    def _run_effect(
        self,
        effect: EffectCallable,
        token: CooperativeCancellationToken,
        lineage: ExecutionLineageV1,
        peak: _ConcurrentPeak,
        conservative_actual: ResourceAmountsV1,
    ) -> EffectResultV1:
        peak.enter()
        try:
            result = effect(token, lineage)
            return EffectResultV1.model_validate(result, strict=True)
        except Exception:
            return EffectResultV1(
                status=EffectStatus.EFFECT_UNKNOWN,
                actual=conservative_actual,
                effect_evidence_sha256=_constant_evidence("callable-raised-after-durable-start"),
                failure_category=FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
                idempotency=IdempotencyStatus.UNKNOWN,
            )
        finally:
            peak.exit()

    def _settle_result(
        self,
        task: DurableExecutionTask,
        *,
        attempt_index: int,
        lineage: ExecutionLineageV1,
        permit_id: str,
        settlement_operation_id: str,
        result: EffectResultV1,
        observed_peak: int,
    ) -> ExecutionAttemptResultV1:
        settlement_id = f"{task.task_id}.settlement-{attempt_index}"
        status = {
            EffectStatus.SUCCEEDED: SettlementStatus.SUCCEEDED,
            EffectStatus.FAILED: SettlementStatus.FAILED,
            EffectStatus.CANCELLED: SettlementStatus.CANCELLED,
            EffectStatus.EFFECT_UNKNOWN: SettlementStatus.EFFECT_UNKNOWN,
        }[result.status]
        self.store.settle(
            self.run_id,
            operation_id=settlement_operation_id,
            settlement_id=settlement_id,
            permit_id=permit_id,
            actual=result.actual,
            status=status,
            result_sha256=result.result_sha256,
            effect_evidence_sha256=result.effect_evidence_sha256,
            cancellation_evidence_sha256=result.cancellation_evidence_sha256,
            failure_category=result.failure_category,
            deterministic_tool_evidence=result.deterministic_tool_evidence,
            external_cost_actual_authenticated=(result.external_cost_actual_authenticated),
            observed_peak_concurrency=observed_peak,
        )
        return ExecutionAttemptResultV1(
            attempt_index=attempt_index,
            attempt_id=lineage.attempt_id,
            context_id=lineage.context_id,
            permit_id=permit_id,
            settlement_id=settlement_id,
            status=result.status,
            result_sha256=result.result_sha256,
            effect_evidence_sha256=result.effect_evidence_sha256,
            failure_category=result.failure_category,
        )

    def _replayed_attempts(
        self,
        task: DurableExecutionTask,
    ) -> tuple[ExecutionAttemptResultV1, ...] | None:
        state = self.store.load(self.run_id)
        prefix = f"permit-{task.task_id}.attempt-"
        indexed_attempts = []
        for attempt in state.attempts:
            if not attempt.permit_id.startswith(prefix):
                continue
            suffix = attempt.permit_id.removeprefix(prefix)
            if not suffix.isdigit():
                raise DurableBudgetError(self._reconciliation_failure(f"{task.task_id}.replay"))
            indexed_attempts.append((int(suffix), attempt))
        if not indexed_attempts:
            return None
        indexed_attempts.sort(key=lambda item: item[0])
        if tuple(index for index, _attempt in indexed_attempts) != tuple(
            range(len(indexed_attempts))
        ):
            raise DurableBudgetError(self._reconciliation_failure(f"{task.task_id}.replay"))
        settlements = {settlement.permit_id: settlement for settlement in state.settlements}
        if any(attempt.permit_id not in settlements for _index, attempt in indexed_attempts):
            raise DurableBudgetError(self._reconciliation_failure(f"{task.task_id}.replay"))
        results = []
        for index, attempt in indexed_attempts:
            settlement = settlements[attempt.permit_id]
            effect_status = {
                SettlementStatus.SUCCEEDED: EffectStatus.SUCCEEDED,
                SettlementStatus.FAILED: EffectStatus.FAILED,
                SettlementStatus.CANCELLED: EffectStatus.CANCELLED,
                SettlementStatus.EFFECT_UNKNOWN: EffectStatus.EFFECT_UNKNOWN,
                SettlementStatus.OVERRUN: EffectStatus.FAILED,
                SettlementStatus.RELEASED_NO_EFFECT: EffectStatus.CANCELLED,
            }[settlement.status]
            results.append(
                ExecutionAttemptResultV1(
                    attempt_index=index,
                    attempt_id=attempt.lineage.attempt_id,
                    context_id=attempt.lineage.context_id,
                    permit_id=attempt.permit_id,
                    settlement_id=settlement.settlement_id,
                    status=effect_status,
                    result_sha256=settlement.result_sha256,
                    effect_evidence_sha256=settlement.effect_evidence_sha256,
                    failure_category=(
                        None if attempt.failure_category is None else attempt.failure_category
                    ),
                )
            )
        return tuple(results)

    def execute(
        self,
        tasks: Sequence[DurableExecutionTask],
        *,
        max_workers: int | None = None,
        cancel_event: threading.Event | None = None,
        cancellation_grace_seconds: float = 0.0,
    ) -> DurableExecutionResultV1:
        if not tasks:
            return DurableExecutionResultV1(
                records=(),
                peak_concurrency=0,
                cancellation_state=CancellationState.NOT_REQUESTED,
            )
        if (
            isinstance(cancellation_grace_seconds, bool)
            or not isinstance(cancellation_grace_seconds, (int, float))
            or not math.isfinite(float(cancellation_grace_seconds))
            or cancellation_grace_seconds < 0
            or cancellation_grace_seconds > 60
        ):
            raise ValueError("cancellation grace must be finite and between 0 and 60 seconds")
        ordered = tuple(sorted(tasks, key=lambda item: item.execution_ordinal))
        if len({task.task_id for task in ordered}) != len(ordered):
            raise ValueError("bounded execution task IDs must be unique")
        if tuple(task.execution_ordinal for task in ordered) != tuple(range(len(ordered))):
            raise ValueError("bounded execution ordinals must be contiguous")
        state = self.store.load(self.run_id)
        workers = (
            state.policy.activation.max_concurrent_agents if max_workers is None else max_workers
        )
        if (
            isinstance(workers, bool)
            or not isinstance(workers, int)
            or workers < 1
            or workers > state.policy.activation.max_concurrent_agents
        ):
            raise ValueError("max_workers exceeds the durable execution activation")
        isolation_evidence = self._isolation_evidence(ordered)

        prepared: dict[
            str,
            tuple[
                DurableExecutionTask,
                ExecutionLineageV1,
                ResourceReservationV1,
                str,
                str,
                str,
                str,
                CooperativeCancellationToken,
            ],
        ] = {}
        reserved: list[tuple[str, str]] = []
        replay_candidates: set[str] = set()
        try:
            for task in ordered:
                permit_id, reserve_id, start_id, settle_id = self._attempt_ids(
                    task,
                    0,
                )
                reservation_result = self.store.reserve(
                    self.run_id,
                    operation_id=reserve_id,
                    permit_id=permit_id,
                    reservation=task.reservation,
                    lineage=task.lineage,
                )
                active = any(
                    item.permit_id == permit_id for item in reservation_result.state.active_permits
                )
                if reservation_result.status is MutationStatus.EXACT_REPLAY and active:
                    raise DurableBudgetError(self._reconciliation_failure(f"{task.task_id}.replay"))
                if reservation_result.status is MutationStatus.EXACT_REPLAY:
                    replay_candidates.add(task.task_id)
                if active:
                    reserved.append((task.task_id, permit_id))
                prepared[task.task_id] = (
                    task,
                    task.lineage,
                    task.reservation,
                    permit_id,
                    reserve_id,
                    start_id,
                    settle_id,
                    CooperativeCancellationToken(),
                )
        except Exception:
            for task_id, permit_id in reserved:
                self.store.release_no_effect(
                    self.run_id,
                    operation_id=f"{task_id}.admission-release",
                    settlement_id=f"{task_id}.admission-release",
                    permit_id=permit_id,
                    evidence_sha256=_constant_evidence(
                        "bounded-admission-failed-before-effect-start"
                    ),
                )
            raise
        replayed_attempts: dict[str, tuple[ExecutionAttemptResultV1, ...]] = {}
        for task_id, (
            task,
            _lineage,
            _reservation,
            permit_id,
            _reserve_id,
            start_id,
            _settle_id,
            _token,
        ) in prepared.items():
            replay = self._replayed_attempts(task) if task_id in replay_candidates else None
            if replay is not None:
                replayed_attempts[task_id] = replay
                continue
            self.store.start(
                self.run_id,
                operation_id=start_id,
                permit_id=permit_id,
            )

        peak = _ConcurrentPeak(lock=threading.Lock())
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="p2-011b")
        futures: dict[Future[EffectResultV1], str] = {}
        for task_id, item in prepared.items():
            if task_id in replayed_attempts:
                continue
            task, lineage, reservation, _permit, *_rest, token = item
            future = pool.submit(
                self._run_effect,
                task.effect,
                token,
                lineage,
                peak,
                reservation.requested,
            )
            futures[future] = task_id

        pending = set(futures)
        completed: dict[str, EffectResultV1] = {}
        cancellation_state = CancellationState.NOT_REQUESTED
        while pending:
            done, pending = wait(pending, timeout=0.01, return_when=FIRST_COMPLETED)
            for future in done:
                completed[futures[future]] = future.result()
            if cancel_event is not None and cancel_event.is_set() and pending:
                cancellation_state = CancellationState.REQUESTED
                for future in pending:
                    task_id = futures[future]
                    token = prepared[task_id][-1]
                    token.request()
                    self.store.request_cancellation(
                        self.run_id,
                        operation_id=f"{task_id}.cancel-request",
                        permit_id=prepared[task_id][3],
                    )
                finished, pending = wait(
                    pending,
                    timeout=float(cancellation_grace_seconds),
                )
                for future in finished:
                    completed[futures[future]] = future.result()
                for future in pending:
                    task_id = futures[future]
                    self.store.record_cancellation(
                        self.run_id,
                        operation_id=f"{task_id}.cancel-unconfirmed",
                        permit_id=prepared[task_id][3],
                        state_value=CancellationState.UNCONFIRMED,
                        evidence_sha256=_constant_evidence("cooperative-cancellation-unconfirmed"),
                        worker_live=True,
                    )
                cancellation_state = (
                    CancellationState.UNCONFIRMED if pending else CancellationState.ACKNOWLEDGED
                )
                break

        pool.shutdown(wait=not pending, cancel_futures=bool(pending))
        attempt_results: dict[str, list[ExecutionAttemptResultV1]] = {
            task.task_id: list(replayed_attempts.get(task.task_id, ())) for task in ordered
        }
        for task in ordered:
            if task.task_id in replayed_attempts:
                continue
            if task.task_id not in completed:
                attempt_results[task.task_id].append(
                    ExecutionAttemptResultV1(
                        attempt_index=0,
                        attempt_id=task.lineage.attempt_id,
                        context_id=task.lineage.context_id,
                        permit_id=prepared[task.task_id][3],
                        status=EffectStatus.EFFECT_UNKNOWN,
                        effect_evidence_sha256=_constant_evidence(
                            "cooperative-cancellation-unconfirmed"
                        ),
                        failure_category=FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
                    )
                )
                continue
            result = completed[task.task_id]
            token = prepared[task.task_id][-1]
            if token.requested:
                if result.status is EffectStatus.CANCELLED and token.acknowledged:
                    self.store.record_cancellation(
                        self.run_id,
                        operation_id=f"{task.task_id}.cancel-acknowledged",
                        permit_id=prepared[task.task_id][3],
                        state_value=CancellationState.ACKNOWLEDGED,
                        evidence_sha256=token.evidence_sha256,
                        worker_live=False,
                    )
                    self.store.record_cancellation(
                        self.run_id,
                        operation_id=f"{task.task_id}.cancel-completed",
                        permit_id=prepared[task.task_id][3],
                        state_value=CancellationState.CANCELLED,
                        evidence_sha256=token.evidence_sha256,
                        worker_live=False,
                    )
                    cancellation_state = CancellationState.CANCELLED
                elif result.status is not EffectStatus.CANCELLED:
                    self.store.record_cancellation(
                        self.run_id,
                        operation_id=f"{task.task_id}.cancel-effect-unknown",
                        permit_id=prepared[task.task_id][3],
                        state_value=CancellationState.EFFECT_UNKNOWN,
                        evidence_sha256=result.effect_evidence_sha256,
                        worker_live=False,
                    )
                    cancellation_state = CancellationState.EFFECT_UNKNOWN
                    result = EffectResultV1(
                        status=EffectStatus.EFFECT_UNKNOWN,
                        actual=result.actual,
                        effect_evidence_sha256=result.effect_evidence_sha256,
                        failure_category=FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
                        idempotency=IdempotencyStatus.UNKNOWN,
                        external_cost_actual_authenticated=(
                            result.external_cost_actual_authenticated
                        ),
                    )
            attempt_results[task.task_id].append(
                self._settle_result(
                    task,
                    attempt_index=0,
                    lineage=task.lineage,
                    permit_id=prepared[task.task_id][3],
                    settlement_operation_id=prepared[task.task_id][6],
                    result=result,
                    observed_peak=max(1, peak.peak),
                )
            )

            completed_retries = 0
            current_lineage = task.lineage
            current_result = result
            while current_result.status is EffectStatus.FAILED:
                decision = admit_automatic_retry(
                    category=(current_result.failure_category or FailureCategory.INTERNAL),
                    idempotency=current_result.idempotency,
                    completed_retries=completed_retries,
                    max_automatic_retries=(state.policy.activation.max_automatic_retries),
                    authoritative_reconciliation_confirmed=(
                        current_result.authoritative_reconciliation_confirmed
                    ),
                    fresh_context_factory_available=(task.retry_lineage_factory is not None),
                )
                if not decision.admitted:
                    break
                completed_retries += 1
                assert task.retry_lineage_factory is not None
                try:
                    retry_context = DurableRetryContextV1.model_validate(
                        task.retry_lineage_factory(
                            current_lineage,
                            completed_retries,
                        ),
                        strict=True,
                    )
                    retry_lineage = retry_context.lineage
                    validate_context_envelope(
                        retry_context.envelope,
                        retry_context.assignment,
                        run_id=self.run_id,
                        expected_context_id=retry_lineage.context_id,
                        attempt_id=retry_lineage.attempt_id,
                        now=self.store.wall_clock(),
                        expected_parent_context_id=current_lineage.context_id,
                        expected_source_sha256=current_lineage.context_source_sha256,
                    )
                except (ContextLifecycleError, TypeError, ValueError):
                    raise DurableBudgetError(
                        self._retry_failure(f"{task.task_id}.retry-{completed_retries}")
                    ) from None
                if (
                    retry_lineage.attempt_id == current_lineage.attempt_id
                    or retry_lineage.context_id == current_lineage.context_id
                    or retry_lineage.root_attempt_id != current_lineage.root_attempt_id
                    or retry_lineage.root_context_id != current_lineage.root_context_id
                    or retry_lineage.context_source_sha256 != current_lineage.context_source_sha256
                ):
                    raise DurableBudgetError(
                        self._retry_failure(f"{task.task_id}.retry-{completed_retries}")
                    )
                permit_id, reserve_id, start_id, settle_id = self._attempt_ids(
                    task,
                    completed_retries,
                )
                retry_reservation = self._retry_reservation(
                    task.reservation,
                    task,
                    completed_retries,
                )
                self.store.reserve(
                    self.run_id,
                    operation_id=reserve_id,
                    permit_id=permit_id,
                    reservation=retry_reservation,
                    lineage=retry_lineage,
                )
                self.store.start(
                    self.run_id,
                    operation_id=start_id,
                    permit_id=permit_id,
                )
                retry_token = CooperativeCancellationToken()
                current_result = self._run_effect(
                    task.effect,
                    retry_token,
                    retry_lineage,
                    peak,
                    retry_reservation.requested,
                )
                attempt_results[task.task_id].append(
                    self._settle_result(
                        task,
                        attempt_index=completed_retries,
                        lineage=retry_lineage,
                        permit_id=permit_id,
                        settlement_operation_id=settle_id,
                        result=current_result,
                        observed_peak=max(1, peak.peak),
                    )
                )
                current_lineage = retry_lineage

        records = tuple(
            DurableExecutionRecordV1(
                task_id=task.task_id,
                execution_ordinal=task.execution_ordinal,
                attempts=tuple(attempt_results[task.task_id]),
                final_status=attempt_results[task.task_id][-1].status,
                final_result_sha256=attempt_results[task.task_id][-1].result_sha256,
            )
            for task in ordered
        )
        return DurableExecutionResultV1(
            records=records,
            peak_concurrency=max(
                peak.peak,
                min(
                    self.store.load(self.run_id).usage.peak_concurrency,
                    len(replayed_attempts),
                ),
            ),
            cancellation_state=cancellation_state,
            isolation_evidence=isolation_evidence,
        )

    def _retry_failure(self, operation_id: str) -> DurableBudgetFailureV1:
        return DurableBudgetFailureV1(
            code=DurableFailureCode.RETRY_FORBIDDEN,
            operation_id=operation_id,
        )

    def _reconciliation_failure(
        self,
        operation_id: str,
    ) -> DurableBudgetFailureV1:
        return DurableBudgetFailureV1(
            code=DurableFailureCode.RECONCILIATION_REQUIRED,
            operation_id=operation_id,
            reconciliation_required=True,
        )


__all__ = [
    "CooperativeCancellationToken",
    "DurableBoundedExecutor",
    "DurableExecutionRecordV1",
    "DurableExecutionResultV1",
    "DurableExecutionTask",
    "DurableRetryContextV1",
    "EffectResultV1",
    "EffectStatus",
    "ExecutionAttemptResultV1",
    "IsolationRequirementV1",
    "RM028IsolationBoundary",
    "RM028IsolationEvidenceV1",
    "RetryAdmissionV1",
    "admit_automatic_retry",
    "build_durable_retry_lineage",
]
