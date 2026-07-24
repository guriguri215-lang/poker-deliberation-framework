"""Strict internal P2-011B durable budget contracts.

The models in this module are additive.  In particular they do not change the
P2-011A ``BudgetPolicyV2`` or in-memory ``SerialUsageLedger`` contracts.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.budgets.contracts import BudgetPolicyV2, ExecutionClass

DURABLE_BUDGET_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
DURABLE_BUDGET_ARTIFACT_SCHEMA: Final[Literal["poker-durable-budget-state-artifact-v1"]] = (
    "poker-durable-budget-state-artifact-v1"
)
DURABLE_BUDGET_PRODUCER_ID: Final[Literal["p2-011b-durable-budget"]] = "p2-011b-durable-budget"
DURABLE_BUDGET_PRODUCER_VERSION: Final[Literal["0.1.0"]] = "0.1.0"
DURABLE_BUDGET_CANONICALIZATION: Final[Literal["poker-durable-budget-json-v1"]] = (
    "poker-durable-budget-json-v1"
)
DURABLE_BUDGET_HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"

RESOURCE_ORDER: Final[tuple[str, ...]] = (
    "active_runtime_ns",
    "provider_attempts",
    "tool_attempts",
    "retry_attempts",
    "external_cost_micro_usd",
    "provider_output_bytes",
    "tool_input_bytes",
    "tool_output_bytes",
    "artifact_bytes",
    "run_bytes",
    "concurrency_slots",
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SECRET_SHAPE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|"
    r"(?:api[_-]?key|password|passwd|secret|token)\s*[:=])",
    re.IGNORECASE,
)


class _DurableModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_durable_json(value: Any) -> str:
    """Return the one canonical JSON representation used by durable hashes."""

    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("durable budget value is not canonical JSON") from exc


def canonical_durable_bytes(value: Any) -> bytes:
    return canonical_durable_json(value).encode("utf-8")


def canonical_durable_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_durable_bytes(value)).hexdigest()


def _validate_identifier(value: str) -> str:
    if not _PORTABLE_ID.fullmatch(value):
        raise ValueError("identifier must use the portable correlation format")
    if _SECRET_SHAPE.search(value):
        raise ValueError("identifier must not contain a secret shape")
    return value


class OwnerKind(StrEnum):
    ORCHESTRATOR = "orchestrator"
    AGENT = "agent"
    TOOL = "tool"
    PROVIDER = "provider"
    INTERNAL = "internal"


class PermitStatus(StrEnum):
    RESERVED = "reserved"
    STARTED = "started"


class AttemptStatus(StrEnum):
    RESERVED = "reserved"
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EFFECT_UNKNOWN = "effect_unknown"


class SettlementStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RELEASED_NO_EFFECT = "released_no_effect"
    EFFECT_UNKNOWN = "effect_unknown"
    OVERRUN = "settlement_overrun"


class CancellationState(StrEnum):
    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    ACKNOWLEDGED = "acknowledged"
    CANCELLED = "cancelled"
    UNCONFIRMED = "unconfirmed"
    EFFECT_UNKNOWN = "effect_unknown"


class OperationKind(StrEnum):
    INITIALIZE = "initialize"
    RESERVE = "reserve"
    START = "start"
    SETTLE = "settle"
    RELEASE_NO_EFFECT = "release_no_effect"
    REQUEST_CANCEL = "request_cancel"
    ACKNOWLEDGE_CANCEL = "acknowledge_cancel"
    RECONCILE = "reconcile"
    TIGHTEN_POLICY = "tighten_policy"


class OperationOutcome(StrEnum):
    APPLIED = "applied"
    EXACT_REPLAY = "exact_replay"
    REFUSED = "refused"
    RECONCILIATION_REQUIRED = "reconciliation_required"


class MutationStatus(StrEnum):
    APPLIED = "applied"
    EXACT_REPLAY = "exact_replay"


class DurableFailureCode(StrEnum):
    INVALID_INPUT = "invalid_input"
    POLICY_MISMATCH = "policy_mismatch"
    ACTIVATION_MISMATCH = "activation_mismatch"
    BUDGET_EXCEEDED = "budget_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    CLOCK_ROLLBACK = "clock_rollback"
    CONCURRENCY_EXCEEDED = "concurrency_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    CAS_CONFLICT = "cas_conflict"
    RUN_LOCKED = "run_locked"
    DURABILITY_UNCERTAIN = "durability_uncertain"
    EFFECT_UNKNOWN = "effect_unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    SETTLEMENT_OVERRUN = "settlement_overrun"
    CANCEL_UNCONFIRMED = "cancel_unconfirmed"
    ISOLATION_REQUIRED = "isolation_required"
    RETRY_FORBIDDEN = "retry_forbidden"
    FAILURE_LATCHED = "failure_latched"


class ExecutionActivationV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    max_concurrent_agents: int = Field(default=1, ge=1, le=32)
    max_automatic_retries: int = Field(default=0, ge=0, le=10)


class DurableBudgetPolicyV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    base_policy: BudgetPolicyV2 = Field(default_factory=BudgetPolicyV2)
    activation: ExecutionActivationV1 = Field(default_factory=ExecutionActivationV1)

    @model_validator(mode="after")
    def activation_is_bounded_by_the_base_policy(self) -> DurableBudgetPolicyV1:
        if self.activation.max_automatic_retries > self.base_policy.max_tool_retries:
            raise ValueError("automatic retries exceed the approved base policy")
        return self

    @property
    def policy_sha256(self) -> str:
        return canonical_durable_sha256(self.base_policy)

    @property
    def activation_sha256(self) -> str:
        return canonical_durable_sha256(self.activation)

    @property
    def canonical_sha256(self) -> str:
        return canonical_durable_sha256(self)


class ResourceAmountsV1(_DurableModel):
    """Non-negative units in the normative canonical resource order."""

    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    active_runtime_ns: int = Field(default=0, ge=0)
    provider_attempts: int = Field(default=0, ge=0)
    tool_attempts: int = Field(default=0, ge=0)
    retry_attempts: int = Field(default=0, ge=0)
    external_cost_micro_usd: int = Field(default=0, ge=0)
    provider_output_bytes: int = Field(default=0, ge=0)
    tool_input_bytes: int = Field(default=0, ge=0)
    tool_output_bytes: int = Field(default=0, ge=0)
    artifact_bytes: int = Field(default=0, ge=0)
    run_bytes: int = Field(default=0, ge=0)
    concurrency_slots: int = Field(default=0, ge=0)

    def ordered_items(self) -> tuple[tuple[str, int], ...]:
        return tuple((name, int(getattr(self, name))) for name in RESOURCE_ORDER)

    def add_cumulative(self, other: ResourceAmountsV1) -> ResourceAmountsV1:
        values = {
            name: int(getattr(self, name)) + int(getattr(other, name)) for name in RESOURCE_ORDER
        }
        return ResourceAmountsV1.model_validate(values)


class DurableUsageV1(ResourceAmountsV1):
    """Settled usage; per-value byte fields retain their observed maximum."""

    peak_concurrency: int = Field(default=0, ge=0, le=32)

    def apply_actual(self, actual: ResourceAmountsV1) -> DurableUsageV1:
        cumulative = {
            "active_runtime_ns",
            "provider_attempts",
            "tool_attempts",
            "retry_attempts",
            "external_cost_micro_usd",
            "run_bytes",
        }
        values = {
            name: (
                int(getattr(self, name)) + int(getattr(actual, name))
                if name in cumulative
                else max(int(getattr(self, name)), int(getattr(actual, name)))
            )
            for name in RESOURCE_ORDER
        }
        values["concurrency_slots"] = 0
        values["peak_concurrency"] = max(
            self.peak_concurrency,
            actual.concurrency_slots,
        )
        return DurableUsageV1.model_validate(values)


class ExecutionLineageV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    owner_kind: OwnerKind
    owner_id: str
    role: str
    phase_id: str
    assignment_id: str | None = None
    root_attempt_id: str
    parent_attempt_id: str | None = None
    attempt_id: str
    root_context_id: str
    parent_context_id: str | None = None
    context_id: str
    context_source_sha256: str
    context_policy_sha256: str
    context_integrity_sha256: str
    execution_ordinal: int = Field(ge=0)
    idempotency_key: str
    idempotency_request_sha256: str

    _ids = field_validator(
        "owner_id",
        "phase_id",
        "assignment_id",
        "root_attempt_id",
        "parent_attempt_id",
        "attempt_id",
        "root_context_id",
        "parent_context_id",
        "context_id",
        "idempotency_key",
    )(lambda value: None if value is None else _validate_identifier(value))

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if not _ROLE.fullmatch(value):
            raise ValueError("role must use the canonical lower-case format")
        return value

    @field_validator(
        "context_source_sha256",
        "context_policy_sha256",
        "context_integrity_sha256",
        "idempotency_request_sha256",
    )
    @classmethod
    def validate_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("lineage hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_root_and_parent_lineage(self) -> ExecutionLineageV1:
        if (self.parent_attempt_id is None) != (self.parent_context_id is None):
            raise ValueError("attempt and context parent lineage must be paired")
        if self.parent_attempt_id is None:
            if self.attempt_id != self.root_attempt_id:
                raise ValueError("initial attempt must equal the root attempt")
            if self.context_id != self.root_context_id:
                raise ValueError("initial context must equal the root context")
        else:
            if self.attempt_id in {self.root_attempt_id, self.parent_attempt_id}:
                raise ValueError("retry attempt ID must be fresh")
            if self.context_id in {self.root_context_id, self.parent_context_id}:
                raise ValueError("retry context ID must be fresh")
        return self


class ResourceReservationV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    reservation_id: str
    requested: ResourceAmountsV1
    execution_class: ExecutionClass = ExecutionClass.LOCAL_FREE
    external_cost_estimate_authenticated: bool = False
    request_sha256: str

    _reservation_id = field_validator("reservation_id")(_validate_identifier)

    @field_validator("request_sha256")
    @classmethod
    def valid_request_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("reservation request hash must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def reserve_exactly_one_slot(self) -> ResourceReservationV1:
        if self.requested.concurrency_slots != 1:
            raise ValueError("each permit must reserve exactly one concurrency slot")
        if self.execution_class is ExecutionClass.UNKNOWN:
            raise ValueError("unknown execution class cannot be reserved")
        if self.execution_class is ExecutionClass.LOCAL_FREE and (
            self.requested.external_cost_micro_usd != 0 or self.external_cost_estimate_authenticated
        ):
            raise ValueError("local-free execution must reserve zero external cost")
        if self.execution_class is ExecutionClass.EXTERNAL and (
            self.requested.external_cost_micro_usd <= 0
            or not self.external_cost_estimate_authenticated
        ):
            raise ValueError("external execution requires an authenticated positive estimate")
        return self


class DurablePermitV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    permit_id: str
    reservation: ResourceReservationV1
    lineage: ExecutionLineageV1
    status: PermitStatus = PermitStatus.RESERVED
    reserved_active_runtime_ns: int = Field(ge=0)
    started_active_runtime_ns: int | None = Field(default=None, ge=0)

    _permit_id = field_validator("permit_id")(_validate_identifier)

    @model_validator(mode="after")
    def started_state_has_a_clock_observation(self) -> DurablePermitV1:
        if (self.status is PermitStatus.STARTED) != (self.started_active_runtime_ns is not None):
            raise ValueError("started permit must have exactly one start observation")
        if (
            self.started_active_runtime_ns is not None
            and self.started_active_runtime_ns < self.reserved_active_runtime_ns
        ):
            raise ValueError("permit start observation precedes reservation")
        return self


class AttemptRecordV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    permit_id: str
    lineage: ExecutionLineageV1
    status: AttemptStatus
    failure_category: str | None = Field(default=None, min_length=1, max_length=64)
    effect_evidence_sha256: str | None = None

    _permit_id = field_validator("permit_id")(_validate_identifier)

    @field_validator("effect_evidence_sha256")
    @classmethod
    def validate_optional_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("attempt evidence hash must be lowercase SHA-256")
        return value


class DeterministicToolEvidenceV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    tool_request_bytes_sha256: str
    tool_result_bytes_sha256: str
    contract_version: str = Field(min_length=1, max_length=64)
    reproduction_metadata_sha256: str
    execution_ordinal: int = Field(ge=0)

    @field_validator(
        "tool_request_bytes_sha256",
        "tool_result_bytes_sha256",
        "reproduction_metadata_sha256",
    )
    @classmethod
    def validate_tool_hashes(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("deterministic tool evidence requires lowercase SHA-256")
        return value


class DurableSettlementV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    settlement_id: str
    permit_id: str
    operation_id: str
    operation_request_sha256: str
    reserved: ResourceAmountsV1
    actual: ResourceAmountsV1
    released: ResourceAmountsV1
    status: SettlementStatus
    result_sha256: str | None = None
    effect_evidence_sha256: str | None = None
    cancellation_evidence_sha256: str | None = None
    deterministic_tool_evidence: DeterministicToolEvidenceV1 | None = None
    settled_active_runtime_ns: int = Field(ge=0)

    _ids = field_validator("settlement_id", "permit_id", "operation_id")(_validate_identifier)

    @field_validator(
        "operation_request_sha256",
        "result_sha256",
        "effect_evidence_sha256",
        "cancellation_evidence_sha256",
    )
    @classmethod
    def validate_settlement_hashes(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("settlement hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def release_is_exact_and_overrun_is_explicit(self) -> DurableSettlementV1:
        overrun = False
        for name in RESOURCE_ORDER:
            reserved = int(getattr(self.reserved, name))
            actual = int(getattr(self.actual, name))
            released = int(getattr(self.released, name))
            if actual <= reserved:
                if released != reserved - actual:
                    raise ValueError("settlement released resources are not exact")
            else:
                overrun = True
                if released != 0:
                    raise ValueError("overrun cannot release the exceeded resource")
        if overrun != (self.status is SettlementStatus.OVERRUN):
            raise ValueError("settlement overrun status does not match actual usage")
        if self.status is SettlementStatus.RELEASED_NO_EFFECT and any(
            value != 0 for _name, value in self.actual.ordered_items()
        ):
            raise ValueError("no-effect release cannot settle actual usage")
        if self.deterministic_tool_evidence is not None and (
            self.deterministic_tool_evidence.execution_ordinal < 0
        ):
            raise ValueError("invalid deterministic tool execution ordinal")
        return self


class DurableCancellationV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    permit_id: str
    state: CancellationState = CancellationState.NOT_REQUESTED
    requested_operation_id: str | None = None
    evidence_sha256: str | None = None
    worker_live: bool = False
    observed_active_runtime_ns: int = Field(ge=0)

    _permit_id = field_validator("permit_id")(_validate_identifier)

    @field_validator("requested_operation_id")
    @classmethod
    def validate_operation_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator("evidence_sha256")
    @classmethod
    def validate_evidence_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("cancellation evidence must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def cancellation_evidence_is_conservative(self) -> DurableCancellationV1:
        if self.state is CancellationState.NOT_REQUESTED:
            if self.requested_operation_id is not None or self.evidence_sha256 is not None:
                raise ValueError("not-requested cancellation cannot carry request evidence")
        elif self.requested_operation_id is None:
            raise ValueError("requested cancellation requires its operation ID")
        if self.state is CancellationState.CANCELLED and (
            self.worker_live or self.evidence_sha256 is None
        ):
            raise ValueError("cancelled requires acknowledged evidence and no live worker")
        if self.worker_live and self.state not in {
            CancellationState.REQUESTED,
            CancellationState.UNCONFIRMED,
            CancellationState.EFFECT_UNKNOWN,
        }:
            raise ValueError("a live worker cannot be interpreted as cancelled")
        return self


class IdempotencyRecordV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    operation_id: str
    kind: OperationKind
    request_sha256: str
    outcome: OperationOutcome
    result_sha256: str
    subject_id: str | None = None

    _operation_id = field_validator("operation_id")(_validate_identifier)

    @field_validator("subject_id")
    @classmethod
    def validate_subject_id(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator("request_sha256", "result_sha256")
    @classmethod
    def validate_operation_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("operation hashes must be lowercase SHA-256")
        return value


class DurableBudgetFailureV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    code: DurableFailureCode
    operation_id: str | None = None
    resource: str | None = None
    limit: int | None = Field(default=None, ge=0)
    observed: int | None = Field(default=None, ge=0)
    reconciliation_required: bool = False
    effect_unknown: bool = False
    evidence_sha256: str | None = None

    @field_validator("operation_id")
    @classmethod
    def validate_failure_operation(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator("resource")
    @classmethod
    def validate_resource(cls, value: str | None) -> str | None:
        if value is not None and value not in RESOURCE_ORDER:
            raise ValueError("failure resource is outside the canonical resource order")
        return value

    @field_validator("evidence_sha256")
    @classmethod
    def validate_failure_hash(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("failure evidence must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def failure_flags_match_the_code(self) -> DurableBudgetFailureV1:
        if self.code is DurableFailureCode.EFFECT_UNKNOWN and not self.effect_unknown:
            raise ValueError("effect_unknown failure must set its effect flag")
        if self.effect_unknown and not self.reconciliation_required:
            raise ValueError("unknown effects always require reconciliation")
        return self


class DurableEventV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    ordinal: int = Field(ge=0)
    kind: OperationKind
    operation_id: str
    subject_id: str | None = None
    event_sha256: str

    _operation_id = field_validator("operation_id")(_validate_identifier)

    @field_validator("subject_id")
    @classmethod
    def validate_event_subject(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator("event_sha256")
    @classmethod
    def validate_event_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("event hash must be lowercase SHA-256")
        return value


class DurableBudgetStateV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    artifact_schema: Literal["poker-durable-budget-state-artifact-v1"] = (
        DURABLE_BUDGET_ARTIFACT_SCHEMA
    )
    canonicalization: Literal["poker-durable-budget-json-v1"] = DURABLE_BUDGET_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = DURABLE_BUDGET_HASH_ALGORITHM
    producer_id: Literal["p2-011b-durable-budget"] = DURABLE_BUDGET_PRODUCER_ID
    producer_version: Literal["0.1.0"] = DURABLE_BUDGET_PRODUCER_VERSION
    run_id: str
    generation: int = Field(ge=1)
    previous_state_sha256: str | None = None
    policy: DurableBudgetPolicyV1
    policy_sha256: str
    activation_sha256: str
    usage: DurableUsageV1 = Field(default_factory=DurableUsageV1)
    active_permits: tuple[DurablePermitV1, ...] = ()
    settlements: tuple[DurableSettlementV1, ...] = ()
    attempts: tuple[AttemptRecordV1, ...] = ()
    cancellations: tuple[DurableCancellationV1, ...] = ()
    operations: tuple[IdempotencyRecordV1, ...] = ()
    events: tuple[DurableEventV1, ...] = ()
    failure_latch: DurableBudgetFailureV1 | None = None
    active_runtime_remaining_ns: int = Field(ge=0)

    _run_id = field_validator("run_id")(_validate_identifier)

    @field_validator(
        "previous_state_sha256",
        "policy_sha256",
        "activation_sha256",
    )
    @classmethod
    def validate_state_hashes(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("state hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> DurableBudgetStateV1:
        if (self.generation == 1) != (self.previous_state_sha256 is None):
            raise ValueError("only generation one may omit the previous state hash")
        if self.policy_sha256 != self.policy.policy_sha256:
            raise ValueError("durable state policy hash mismatch")
        if self.activation_sha256 != self.policy.activation_sha256:
            raise ValueError("durable state activation hash mismatch")
        if self.active_runtime_remaining_ns != max(
            0,
            self.policy.base_policy.runtime_limit_ns - self.usage.active_runtime_ns,
        ):
            raise ValueError("remaining runtime does not match settled active runtime")

        permit_ids = [permit.permit_id for permit in self.active_permits]
        settlement_ids = [settlement.settlement_id for settlement in self.settlements]
        settled_permits = [settlement.permit_id for settlement in self.settlements]
        attempt_permits = [attempt.permit_id for attempt in self.attempts]
        attempt_ids = [attempt.lineage.attempt_id for attempt in self.attempts]
        context_ids = [attempt.lineage.context_id for attempt in self.attempts]
        operation_ids = [operation.operation_id for operation in self.operations]
        cancellation_permits = [item.permit_id for item in self.cancellations]
        for name, values in (
            ("permit", permit_ids),
            ("settlement", settlement_ids),
            ("settled permit", settled_permits),
            ("attempt permit", attempt_permits),
            ("attempt", attempt_ids),
            ("context", context_ids),
            ("operation", operation_ids),
            ("cancellation", cancellation_permits),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate durable {name} identity")
        root_ordinals: dict[str, int] = {}
        ordinal_roots: dict[int, str] = {}
        attempts_by_id = {attempt.lineage.attempt_id: attempt.lineage for attempt in self.attempts}
        for attempt in self.attempts:
            lineage = attempt.lineage
            prior_ordinal = root_ordinals.setdefault(
                lineage.root_attempt_id,
                lineage.execution_ordinal,
            )
            if prior_ordinal != lineage.execution_ordinal:
                raise ValueError("one root attempt changed execution ordinal")
            prior_root = ordinal_roots.setdefault(
                lineage.execution_ordinal,
                lineage.root_attempt_id,
            )
            if prior_root != lineage.root_attempt_id:
                raise ValueError("duplicate durable execution ordinal")
            if lineage.parent_attempt_id is None:
                continue
            parent = attempts_by_id.get(lineage.parent_attempt_id)
            if parent is None or parent.context_id != lineage.parent_context_id:
                raise ValueError("retry parent lineage is missing or mismatched")
            stable_fields = (
                "owner_kind",
                "owner_id",
                "role",
                "phase_id",
                "assignment_id",
                "root_attempt_id",
                "root_context_id",
                "context_source_sha256",
                "execution_ordinal",
            )
            if any(getattr(lineage, field) != getattr(parent, field) for field in stable_fields):
                raise ValueError("retry ownership or root lineage was substituted")
        if set(permit_ids) & set(settled_permits):
            raise ValueError("a permit cannot be both active and settled")
        if set(attempt_permits) != set(permit_ids) | set(settled_permits):
            raise ValueError("every durable permit requires exactly one attempt")
        attempts_by_permit = {attempt.permit_id: attempt for attempt in self.attempts}
        cancellations_by_permit = {
            cancellation.permit_id: cancellation for cancellation in self.cancellations
        }
        for permit in self.active_permits:
            cancellation = cancellations_by_permit.get(permit.permit_id)
            expected_status = (
                AttemptStatus.EFFECT_UNKNOWN
                if cancellation is not None
                and cancellation.state
                in {
                    CancellationState.UNCONFIRMED,
                    CancellationState.EFFECT_UNKNOWN,
                }
                else (
                    AttemptStatus.RESERVED
                    if permit.status is PermitStatus.RESERVED
                    else AttemptStatus.STARTED
                )
            )
            if attempts_by_permit[permit.permit_id].status is not expected_status:
                raise ValueError("active permit and attempt status mismatch")
        settlement_attempt_status = {
            SettlementStatus.SUCCEEDED: AttemptStatus.SUCCEEDED,
            SettlementStatus.FAILED: AttemptStatus.FAILED,
            SettlementStatus.CANCELLED: AttemptStatus.CANCELLED,
            SettlementStatus.RELEASED_NO_EFFECT: AttemptStatus.CANCELLED,
            SettlementStatus.EFFECT_UNKNOWN: AttemptStatus.EFFECT_UNKNOWN,
            SettlementStatus.OVERRUN: AttemptStatus.FAILED,
        }
        for settlement in self.settlements:
            if (
                attempts_by_permit[settlement.permit_id].status
                is not settlement_attempt_status[settlement.status]
            ):
                raise ValueError("settlement and attempt status mismatch")
        if not set(cancellation_permits).issubset(set(permit_ids) | set(settled_permits)):
            raise ValueError("cancellation references an unknown permit")
        if tuple(event.ordinal for event in self.events) != tuple(range(len(self.events))):
            raise ValueError("durable event ordinals must be contiguous")
        if len(self.operations) != self.generation or len(self.events) != self.generation:
            raise ValueError("each durable generation requires exactly one operation and event")
        if self.operations[0].kind is not OperationKind.INITIALIZE or any(
            operation.kind is OperationKind.INITIALIZE for operation in self.operations[1:]
        ):
            raise ValueError("durable initialization must occur exactly at generation one")
        for operation, event in zip(self.operations, self.events, strict=True):
            if (
                operation.outcome is not OperationOutcome.APPLIED
                or event.kind is not operation.kind
                or event.operation_id != operation.operation_id
                or event.subject_id != operation.subject_id
                or event.event_sha256
                != canonical_durable_sha256(
                    {
                        "ordinal": event.ordinal,
                        "kind": event.kind.value,
                        "operation_id": event.operation_id,
                        "subject_id": event.subject_id,
                        "result_sha256": operation.result_sha256,
                    }
                )
            ):
                raise ValueError("durable operation and event lineage mismatch")
        active_slots = sum(
            permit.reservation.requested.concurrency_slots for permit in self.active_permits
        )
        if active_slots > self.policy.activation.max_concurrent_agents:
            raise ValueError("active permits exceed activation concurrency")
        if self.usage.peak_concurrency > self.policy.activation.max_concurrent_agents:
            raise ValueError("recorded peak concurrency exceeds activation")
        return self

    @property
    def canonical_sha256(self) -> str:
        return canonical_durable_sha256(self)

    def canonical_bytes(self) -> bytes:
        return canonical_durable_bytes(self)


class DurableMutationResultV1(_DurableModel):
    schema_version: Literal["1.0.0"] = DURABLE_BUDGET_SCHEMA_VERSION
    status: MutationStatus
    operation_id: str
    operation_request_sha256: str
    subject_id: str | None = None
    storage_outcome: Literal["published", "current_committed", "historical_committed"]
    state: DurableBudgetStateV1

    _operation_id = field_validator("operation_id")(_validate_identifier)

    @field_validator("subject_id")
    @classmethod
    def validate_mutation_subject(cls, value: str | None) -> str | None:
        return None if value is None else _validate_identifier(value)

    @field_validator("operation_request_sha256")
    @classmethod
    def validate_mutation_hash(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("mutation request hash must be lowercase SHA-256")
        return value


__all__ = [
    "DURABLE_BUDGET_ARTIFACT_SCHEMA",
    "DURABLE_BUDGET_CANONICALIZATION",
    "DURABLE_BUDGET_HASH_ALGORITHM",
    "DURABLE_BUDGET_PRODUCER_ID",
    "DURABLE_BUDGET_PRODUCER_VERSION",
    "DURABLE_BUDGET_SCHEMA_VERSION",
    "RESOURCE_ORDER",
    "AttemptRecordV1",
    "AttemptStatus",
    "CancellationState",
    "DeterministicToolEvidenceV1",
    "DurableBudgetFailureV1",
    "DurableBudgetPolicyV1",
    "DurableBudgetStateV1",
    "DurableCancellationV1",
    "DurableEventV1",
    "DurableFailureCode",
    "DurableMutationResultV1",
    "DurablePermitV1",
    "DurableSettlementV1",
    "DurableUsageV1",
    "ExecutionActivationV1",
    "ExecutionLineageV1",
    "IdempotencyRecordV1",
    "MutationStatus",
    "OperationKind",
    "OperationOutcome",
    "OwnerKind",
    "PermitStatus",
    "ResourceAmountsV1",
    "ResourceReservationV1",
    "SettlementStatus",
    "canonical_durable_bytes",
    "canonical_durable_json",
    "canonical_durable_sha256",
]
