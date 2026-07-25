"""Strict immutable contracts for P2-013A approval authority."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from poker_deliberation.schemas import ApprovalCategory

APPROVAL_SCHEMA_VERSION: Final[Literal["2.0.0"]] = "2.0.0"
APPROVAL_CANONICALIZATION: Final[Literal["poker-approval-json-v2"]] = "poker-approval-json-v2"
APPROVAL_HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$")
_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _safe_text(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("approval text must be NFC")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("approval text cannot contain control characters")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


Sha256 = Annotated[str, Field(pattern=_SHA256.pattern)]
PortableId = Annotated[
    str,
    Field(pattern=_PORTABLE_ID.pattern),
    AfterValidator(_safe_text),
]
Version = Annotated[
    str,
    Field(pattern=_VERSION.pattern),
    AfterValidator(_safe_text),
]
FieldName = Annotated[str, Field(pattern=_FIELD_NAME.pattern)]
EnvironmentName = Annotated[str, Field(pattern=_ENVIRONMENT_NAME.pattern)]
BoundedText = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_safe_text),
]
OptionalBoundedText = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_safe_text),
]

AuthorityScope: TypeAlias = Literal[
    "reject:any",
    "approve:external_code",
    "approve:package_install",
    "approve:external_service",
    "approve:long_running_compute",
    "approve:outside_workspace_write",
    "approve:destructive_change",
    "approve:secret_access",
    "approve:paid_data",
    "approve:objective_change",
]
DecisionValue: TypeAlias = Literal["approved", "rejected"]
RequestState: TypeAlias = Literal["pending", "approved", "rejected", "superseded"]
ReportRunStatus: TypeAlias = Literal[
    "approval_required",
    "completed",
    "failed_with_limitations",
]


class _ApprovalModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class ApprovalFailureCode(StrEnum):
    APPROVAL_UNKNOWN = "approval_unknown"
    APPROVAL_DUPLICATE = "approval_duplicate"
    APPROVAL_ALREADY_DECIDED = "approval_already_decided"
    APPROVAL_EXPIRED = "approval_expired"
    APPROVAL_LEDGER_CORRUPT = "approval_ledger_corrupt"
    LEGACY_APPROVAL_HISTORICAL_ONLY = "legacy_approval_historical_only"
    ACTOR_SPOOF = "actor_spoof"
    UNAUTHORIZED_DECISION = "unauthorized_decision"
    AUTHORITY_REVOKED = "authority_revoked"
    STALE_DECISION = "stale_decision"
    APPROVE_REJECT_CONFLICT = "approve_reject_conflict"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    ACTION_DIGEST_MISMATCH = "action_digest_mismatch"
    RESUME_CONFLICT = "resume_conflict"
    RESUME_TRANSACTION_FAILED = "resume_transaction_failed"
    EXTERNAL_EXECUTOR_UNAVAILABLE = "external_executor_unavailable"
    AUDIT_RATE_LIMITED = "audit_rate_limited"
    AUDIT_CAPACITY_EXCEEDED = "audit_capacity_exceeded"
    AUDIT_UNCONFIRMED = "audit_unconfirmed"
    RUN_LOCKED = "run_locked"


class ApprovalActor(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    actor_id: PortableId
    actor_type: Literal["human", "service"]
    authority_source: PortableId
    authority_scopes: tuple[AuthorityScope, ...]
    verification_status: Literal["unverified", "verified"]
    verification_reference_sha256: Sha256 | None = None
    session_reference_sha256: Sha256 | None = None
    credential_reference_sha256: Sha256 | None = None
    verified_at: datetime | None = None
    authority_expires_at: datetime | None = None
    revocation_status: Literal["unknown", "not_revoked", "revoked"]

    @field_validator("authority_scopes")
    @classmethod
    def canonical_scopes(cls, value: tuple[AuthorityScope, ...]) -> tuple[AuthorityScope, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("authority scopes must be nonempty and unique")
        if value != tuple(sorted(value, key=lambda item: item.encode("utf-8"))):
            raise ValueError("authority scopes must be UTF-8 ordered")
        return value

    @field_validator("verified_at", "authority_expires_at")
    @classmethod
    def utc_authority_time(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "authority time")
        return _utc(value, field_name)

    @model_validator(mode="after")
    def closed_trust_matrix(self) -> ApprovalActor:
        if self.verification_status == "unverified":
            if (
                self.authority_source != "local_cli"
                or self.authority_scopes != ("reject:any",)
                or self.verification_reference_sha256 is not None
                or self.verified_at is not None
                or self.authority_expires_at is not None
                or self.revocation_status != "unknown"
            ):
                raise ValueError("unverified local actor trust matrix mismatch")
        elif (
            self.authority_source == "local_cli"
            or self.verification_reference_sha256 is None
            or self.verified_at is None
            or self.authority_expires_at is None
            or self.authority_expires_at <= self.verified_at
            or self.revocation_status == "unknown"
        ):
            raise ValueError("verified actor trust matrix mismatch")
        return self


ApprovalActorV2 = ApprovalActor


class ApprovalAuthoritySnapshotV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    provider_id: PortableId
    provider_version: Version
    resolved_at: datetime
    actor: ApprovalActor
    actor_sha256: Sha256

    _resolved_utc = field_validator("resolved_at")(lambda value: _utc(value, "resolved_at"))

    @model_validator(mode="after")
    def actor_hash_matches(self) -> ApprovalAuthoritySnapshotV2:
        from poker_deliberation.approval_canonical import approval_actor_sha256

        if self.actor_sha256 != approval_actor_sha256(self.actor):
            raise ValueError("authority snapshot actor hash mismatch")
        return self


class OutboundFieldBindingV2(_ApprovalModel):
    field_name: FieldName
    classification: Literal["public", "internal", "sensitive", "restricted"]
    content_sha256: Sha256


class CanonicalActionPlanV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    operation: BoundedText
    action_category: ApprovalCategory
    executor_kind: Literal["none", "local_process", "external_service", "provider"]
    executor_identifier: PortableId
    executor_version: Version
    executor_sha256: Sha256
    executor_availability: Literal["available", "unavailable"]
    outbound_fields: tuple[OutboundFieldBindingV2, ...] = Field(max_length=128)
    destination_kind: Literal[
        "none",
        "workspace",
        "filesystem",
        "network_service",
        "package_registry",
        "provider",
    ]
    destination_identifier: BoundedText
    retention_policy_id: PortableId
    trace_policy_id: PortableId
    maximum_cost_microunits: int = Field(ge=0, le=10**15)
    maximum_runtime_ms: int = Field(ge=1, le=31_536_000_000)
    maximum_memory_bytes: int = Field(ge=1, le=2**63 - 1)
    maximum_output_bytes: int = Field(ge=1, le=2**63 - 1)
    maximum_processes: int = Field(ge=1, le=1_000_000)
    working_directory: BoundedText | None = None
    environment_name_allowlist: tuple[EnvironmentName, ...] = Field(max_length=128)
    expected_result_type: PortableId
    execution_id: PortableId
    remote_idempotency_key: PortableId
    expires_at: datetime

    _expires_utc = field_validator("expires_at")(lambda value: _utc(value, "expires_at"))

    @field_validator("outbound_fields")
    @classmethod
    def canonical_outbound_fields(
        cls, value: tuple[OutboundFieldBindingV2, ...]
    ) -> tuple[OutboundFieldBindingV2, ...]:
        names = tuple(item.field_name for item in value)
        if len(names) != len(set(names)):
            raise ValueError("outbound field names must be unique")
        if names != tuple(sorted(names, key=lambda item: item.encode("utf-8"))):
            raise ValueError("outbound fields must be UTF-8 field-name ordered")
        return value

    @field_validator("environment_name_allowlist")
    @classmethod
    def canonical_environment_names(
        cls, value: tuple[EnvironmentName, ...]
    ) -> tuple[EnvironmentName, ...]:
        if len(value) != len(set(value)):
            raise ValueError("environment names must be unique")
        if value != tuple(sorted(value, key=lambda item: item.encode("utf-8"))):
            raise ValueError("environment names must be UTF-8 ordered")
        return value


class ApprovalDisplayV2(_ApprovalModel):
    requested_action: BoundedText
    reason: BoundedText
    expected_benefit: BoundedText
    risks: tuple[BoundedText, ...] = Field(min_length=1, max_length=64)
    data_to_be_sent: tuple[BoundedText, ...] = Field(max_length=128)
    cost_or_resource_estimate: BoundedText
    alternatives: tuple[BoundedText, ...] = Field(min_length=1, max_length=64)
    effect_of_declining: BoundedText
    exact_command_or_tool_call: OptionalBoundedText | None = None


class ApprovalRequestV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    request_id: PortableId
    request_revision: int = Field(ge=1)
    ledger_revision: int = Field(ge=1)
    created_run_revision: int = Field(ge=1)
    stable_proposal_id: PortableId
    action_plan: CanonicalActionPlanV2
    action_digest_sha256: Sha256
    display: ApprovalDisplayV2
    required_authority_scope: AuthorityScope
    created_at: datetime
    expires_at: datetime
    source_phase_id: PortableId
    source_attempt_id: PortableId
    request_idempotency_key: Sha256
    state: RequestState = "pending"
    supersession_reference: PortableId | None = None

    _created_utc = field_validator("created_at")(lambda value: _utc(value, "created_at"))
    _expires_utc = field_validator("expires_at")(lambda value: _utc(value, "expires_at"))

    @model_validator(mode="after")
    def closed_request_matrix(self) -> ApprovalRequestV2:
        from poker_deliberation.approval_canonical import action_digest_sha256

        expected_scope = f"approve:{self.action_plan.action_category}"
        if self.action_digest_sha256 != action_digest_sha256(self.action_plan):
            raise ValueError("approval request action digest mismatch")
        if self.required_authority_scope != expected_scope:
            raise ValueError("approval request authority scope mismatch")
        if self.expires_at != self.action_plan.expires_at or self.expires_at <= self.created_at:
            raise ValueError("approval request expiry mismatch")
        if self.state == "superseded" and self.supersession_reference is None:
            raise ValueError("superseded request requires a supersession reference")
        if self.supersession_reference == self.request_id:
            raise ValueError("approval request cannot supersede itself")
        return self


class HistoricalApprovalV1Binding(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    run_id: PortableId
    approval_id: PortableId
    v1_request_sha256: Sha256
    v1_status: Literal["pending", "approved", "rejected"]
    compatibility_mode: Literal["historical_rejection_only"] = "historical_rejection_only"


class ApprovalLedgerV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    canonicalization: Literal["poker-approval-json-v2"] = APPROVAL_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = APPROVAL_HASH_ALGORITHM
    run_id: PortableId
    ledger_revision: int = Field(ge=0)
    requests: tuple[ApprovalRequestV2, ...] = Field(max_length=1024)
    decision_count: int = Field(ge=0, le=1_000_000)
    decision_log_head_sha256: Sha256 | None = None
    domain_audit_count: int = Field(ge=0, le=1_000_000)
    domain_audit_log_head_sha256: Sha256 | None = None

    @model_validator(mode="after")
    def closed_ledger_matrix(self) -> ApprovalLedgerV2:
        request_ids = tuple(item.request_id for item in self.requests)
        idempotency_keys = tuple(item.request_idempotency_key for item in self.requests)
        order = tuple(
            (item.ledger_revision, item.request_id.encode("utf-8")) for item in self.requests
        )
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("approval ledger request IDs must be unique")
        if len(idempotency_keys) != len(set(idempotency_keys)):
            raise ValueError("approval request idempotency keys must be unique")
        if order != tuple(sorted(order)):
            raise ValueError("approval ledger requests must be canonically ordered")
        if any(item.ledger_revision > self.ledger_revision for item in self.requests):
            raise ValueError("request revision exceeds approval ledger revision")
        if self.ledger_revision == 0 and self.requests:
            raise ValueError("initial approval ledger cannot contain requests")
        if (self.decision_count == 0) != (self.decision_log_head_sha256 is None):
            raise ValueError("decision count/head mismatch")
        if (self.domain_audit_count == 0) != (self.domain_audit_log_head_sha256 is None):
            raise ValueError("domain audit count/head mismatch")
        return self


class ApprovalDecisionItemV2(_ApprovalModel):
    request_id: PortableId
    expected_request_revision: int = Field(ge=1)
    action_digest_sha256: Sha256
    decision: DecisionValue


class ApprovalDecisionBatch(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    run_id: PortableId
    expected_run_revision: int = Field(ge=1)
    expected_ledger_revision: int = Field(ge=0)
    actor: ApprovalActor
    decision_id: PortableId
    idempotency_key: PortableId
    items: tuple[ApprovalDecisionItemV2, ...] = Field(
        min_length=1,
        max_length=128,
    )
    reason: BoundedText
    decision_at: datetime

    _decision_utc = field_validator("decision_at")(lambda value: _utc(value, "decision_at"))

    @field_validator("items")
    @classmethod
    def canonical_item_order(
        cls, value: tuple[ApprovalDecisionItemV2, ...]
    ) -> tuple[ApprovalDecisionItemV2, ...]:
        order = tuple(
            (item.request_id.encode("utf-8"), item.decision.encode("ascii")) for item in value
        )
        if order != tuple(sorted(order)):
            raise ValueError("decision items must be canonically ordered")
        return value


ApprovalDecisionBatchV2 = ApprovalDecisionBatch


class ApprovalDecisionResultV2(_ApprovalModel):
    request_id: PortableId
    request_revision: int = Field(ge=1)
    action_digest_sha256: Sha256
    decision: DecisionValue


class ApprovalDecisionFailureV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    code: ApprovalFailureCode
    message: BoundedText
    retryable: bool = False
    run_id: PortableId | None = None
    request_id: PortableId | None = None
    decision_id: PortableId | None = None
    idempotency_key_sha256: Sha256 | None = None
    observed_run_revision: int | None = Field(default=None, ge=1)
    observed_ledger_revision: int | None = Field(default=None, ge=0)
    audit_confirmed: bool
    reconciliation_required: bool = False

    @model_validator(mode="after")
    def retry_matrix(self) -> ApprovalDecisionFailureV2:
        if self.retryable != (self.code is ApprovalFailureCode.RUN_LOCKED):
            raise ValueError("only run_locked may be retryable")
        if self.code is ApprovalFailureCode.AUDIT_UNCONFIRMED and self.audit_confirmed:
            raise ValueError("audit_unconfirmed cannot claim confirmed audit")
        return self


class ApprovalDecisionOutcome(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    outcome_kind: Literal["committed", "failed"]
    run_id: PortableId
    decision_id: PortableId
    idempotency_key: PortableId
    actor_sha256: Sha256
    authority_snapshot_sha256: Sha256
    batch_sha256: Sha256
    previous_run_revision: int = Field(ge=1)
    current_run_revision: int = Field(ge=1)
    previous_ledger_revision: int = Field(ge=0)
    current_ledger_revision: int = Field(ge=0)
    request_results: tuple[ApprovalDecisionResultV2, ...] = Field(max_length=128)
    remaining_pending_count: int = Field(ge=0, le=1024)
    run_status: ReportRunStatus | None = None
    limitation: ApprovalDecisionFailureV2 | None = None
    committed_at: datetime | None = None

    @field_validator("committed_at")
    @classmethod
    def utc_commit_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value, "committed_at")

    @field_validator("request_results")
    @classmethod
    def canonical_results(
        cls, value: tuple[ApprovalDecisionResultV2, ...]
    ) -> tuple[ApprovalDecisionResultV2, ...]:
        ids = tuple(item.request_id for item in value)
        if len(ids) != len(set(ids)):
            raise ValueError("decision outcome request IDs must be unique")
        if ids != tuple(sorted(ids, key=lambda item: item.encode("utf-8"))):
            raise ValueError("decision results must be UTF-8 request-ID ordered")
        return value

    @model_validator(mode="after")
    def closed_outcome_matrix(self) -> ApprovalDecisionOutcome:
        if self.outcome_kind == "committed":
            if (
                not self.request_results
                or self.run_status is None
                or self.current_run_revision != self.previous_run_revision + 1
                or self.current_ledger_revision != self.previous_ledger_revision + 1
                or self.committed_at is None
            ):
                raise ValueError("committed approval outcome matrix mismatch")
            has_approval = any(item.decision == "approved" for item in self.request_results)
            if has_approval:
                if (
                    self.run_status != "failed_with_limitations"
                    or self.limitation is None
                    or self.limitation.code is not ApprovalFailureCode.EXTERNAL_EXECUTOR_UNAVAILABLE
                ):
                    raise ValueError("approved action requires unavailable limitation")
            elif (
                self.run_status
                != ("approval_required" if self.remaining_pending_count else "completed")
                or self.limitation is not None
            ):
                raise ValueError("reject outcome status must match remaining pending requests")
        elif (
            self.request_results
            or self.remaining_pending_count != 0
            or self.run_status is not None
            or self.limitation is not None
            or self.current_run_revision != self.previous_run_revision
            or self.current_ledger_revision != self.previous_ledger_revision
            or self.committed_at is not None
        ):
            raise ValueError("failed approval outcome must be mutation-zero")
        return self


ApprovalDecisionOutcomeV2 = ApprovalDecisionOutcome


class ApprovalDecisionRecordV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    sequence: int = Field(ge=1)
    previous_record_sha256: Sha256 | None = None
    run_id: PortableId
    decision_id: PortableId
    idempotency_key: PortableId
    actor_sha256: Sha256
    authority_snapshot: ApprovalAuthoritySnapshotV2
    authority_snapshot_sha256: Sha256
    batch: ApprovalDecisionBatch
    batch_sha256: Sha256
    outcome: ApprovalDecisionOutcome
    outcome_sha256: Sha256
    committed_at: datetime
    record_sha256: Sha256

    _committed_utc = field_validator("committed_at")(lambda value: _utc(value, "committed_at"))

    @model_validator(mode="after")
    def record_hash_matches(self) -> ApprovalDecisionRecordV2:
        from poker_deliberation.approval_canonical import (
            approval_actor_sha256,
            approval_authority_snapshot_sha256,
            approval_decision_batch_sha256,
            approval_decision_outcome_sha256,
            approval_decision_record_sha256,
        )

        if self.batch_sha256 != approval_decision_batch_sha256(self.batch):
            raise ValueError("approval decision batch hash mismatch")
        if self.outcome_sha256 != approval_decision_outcome_sha256(self.outcome):
            raise ValueError("approval decision outcome hash mismatch")
        batch_results = tuple(
            (
                item.request_id,
                item.expected_request_revision + 1,
                item.action_digest_sha256,
                item.decision,
            )
            for item in self.batch.items
        )
        outcome_results = tuple(
            (
                item.request_id,
                item.request_revision,
                item.action_digest_sha256,
                item.decision,
            )
            for item in self.outcome.request_results
        )
        if (
            self.batch.run_id != self.run_id
            or self.batch.decision_id != self.decision_id
            or self.batch.idempotency_key != self.idempotency_key
            or approval_actor_sha256(self.batch.actor) != self.actor_sha256
            or self.authority_snapshot.actor != self.batch.actor
            or self.authority_snapshot.actor_sha256 != self.actor_sha256
            or approval_authority_snapshot_sha256(self.authority_snapshot)
            != self.authority_snapshot_sha256
            or self.outcome.authority_snapshot_sha256 != self.authority_snapshot_sha256
            or self.batch.expected_run_revision != self.outcome.previous_run_revision
            or self.batch.expected_ledger_revision != self.outcome.previous_ledger_revision
            or self.batch.decision_at != self.committed_at
            or self.outcome.committed_at != self.committed_at
            or batch_results != outcome_results
            or self.outcome.run_id != self.run_id
            or self.outcome.decision_id != self.decision_id
            or self.outcome.idempotency_key != self.idempotency_key
            or self.outcome.actor_sha256 != self.actor_sha256
            or self.outcome.batch_sha256 != self.batch_sha256
        ):
            raise ValueError("approval decision record outcome identity mismatch")
        if self.record_sha256 != approval_decision_record_sha256(self):
            raise ValueError("approval decision record hash mismatch")
        if (self.sequence == 1) != (self.previous_record_sha256 is None):
            raise ValueError("approval decision record lineage mismatch")
        return self


class ApprovalDomainAuditEventV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    sequence: int = Field(ge=1)
    previous_event_sha256: Sha256 | None = None
    event_kind: Literal["decision_committed"] = "decision_committed"
    run_id: PortableId
    run_revision: int = Field(ge=1)
    ledger_revision: int = Field(ge=1)
    decision_id: PortableId
    actor_sha256: Sha256
    authority_snapshot_sha256: Sha256
    batch_sha256: Sha256
    decision_record_sha256: Sha256
    outcome_sha256: Sha256
    occurred_at: datetime
    event_sha256: Sha256

    _occurred_utc = field_validator("occurred_at")(lambda value: _utc(value, "occurred_at"))

    @model_validator(mode="after")
    def event_hash_matches(self) -> ApprovalDomainAuditEventV2:
        from poker_deliberation.approval_canonical import (
            approval_domain_audit_event_sha256,
        )

        if self.event_sha256 != approval_domain_audit_event_sha256(self):
            raise ValueError("approval domain audit event hash mismatch")
        if (self.sequence == 1) != (self.previous_event_sha256 is None):
            raise ValueError("approval domain audit lineage mismatch")
        return self


class ApprovalSecurityAuditEventV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    sequence: int = Field(ge=1, le=1024)
    previous_event_sha256: Sha256 | None = None
    event_kind: Literal["failure", "rate_limit"]
    run_id_sha256: Sha256
    actor_sha256: Sha256
    decision_id_sha256: Sha256
    idempotency_key_sha256: Sha256
    batch_sha256: Sha256 | None = None
    failure_code: ApprovalFailureCode
    observed_run_revision: int | None = Field(default=None, ge=1)
    observed_ledger_revision: int | None = Field(default=None, ge=0)
    occurred_at: datetime
    rate_window_started_at: datetime
    event_sha256: Sha256

    _occurred_utc = field_validator("occurred_at")(lambda value: _utc(value, "occurred_at"))
    _window_utc = field_validator("rate_window_started_at")(
        lambda value: _utc(value, "rate_window_started_at")
    )

    @model_validator(mode="after")
    def security_event_matrix(self) -> ApprovalSecurityAuditEventV2:
        from poker_deliberation.approval_canonical import (
            approval_security_audit_event_sha256,
        )

        if self.rate_window_started_at > self.occurred_at:
            raise ValueError("approval security audit window starts in the future")
        if self.event_kind == "rate_limit":
            if self.failure_code is not ApprovalFailureCode.AUDIT_RATE_LIMITED:
                raise ValueError("rate-limit marker code mismatch")
        elif self.failure_code in {
            ApprovalFailureCode.AUDIT_RATE_LIMITED,
            ApprovalFailureCode.AUDIT_CAPACITY_EXCEEDED,
            ApprovalFailureCode.AUDIT_UNCONFIRMED,
        }:
            raise ValueError("audit control failures are not ordinary failure events")
        if self.event_sha256 != approval_security_audit_event_sha256(self):
            raise ValueError("approval security audit event hash mismatch")
        if (self.sequence == 1) != (self.previous_event_sha256 is None):
            raise ValueError("approval security audit lineage mismatch")
        return self


class ApprovalSecurityAuditRateStateV2(_ApprovalModel):
    actor_sha256: Sha256
    window_started_at: datetime
    failed_event_count: int = Field(ge=0, le=32)
    rate_limit_marker_recorded: bool

    _window_utc = field_validator("window_started_at")(
        lambda value: _utc(value, "window_started_at")
    )

    @model_validator(mode="after")
    def marker_requires_full_window(self) -> ApprovalSecurityAuditRateStateV2:
        if self.rate_limit_marker_recorded and self.failed_event_count != 32:
            raise ValueError("rate-limit marker requires a full failure window")
        return self


class ApprovalSecurityAuditPointerV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    run_id_sha256: Sha256
    audit_sequence: int = Field(ge=0, le=1024)
    head_event_sha256: Sha256 | None = None
    total_event_bytes: int = Field(ge=0, le=1_048_576)
    rate_states: tuple[ApprovalSecurityAuditRateStateV2, ...] = Field(max_length=1024)
    updated_at: datetime | None = None

    @field_validator("updated_at")
    @classmethod
    def utc_pointer_time(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return _utc(value, getattr(info, "field_name", "audit pointer time"))

    @model_validator(mode="after")
    def pointer_matrix(self) -> ApprovalSecurityAuditPointerV2:
        empty = self.audit_sequence == 0
        if empty != (
            self.head_event_sha256 is None
            and self.total_event_bytes == 0
            and not self.rate_states
            and self.updated_at is None
        ):
            raise ValueError("approval security audit pointer empty matrix mismatch")
        if not empty and (self.head_event_sha256 is None or self.updated_at is None):
            raise ValueError("approval security audit pointer active matrix mismatch")
        actors = tuple(item.actor_sha256 for item in self.rate_states)
        if len(actors) != len(set(actors)):
            raise ValueError("approval security audit rate actors must be unique")
        if actors != tuple(sorted(actors, key=lambda item: item.encode("ascii"))):
            raise ValueError("approval security audit rate states must be ordered")
        return self


class ExternalExecutionBindingV2(_ApprovalModel):
    schema_version: Literal["2.0.0"] = APPROVAL_SCHEMA_VERSION
    binding_kind: Literal["external_execution_unavailable"] = "external_execution_unavailable"
    run_id: PortableId
    request_id: PortableId
    request_revision: int = Field(ge=1)
    action_digest_sha256: Sha256
    execution_id: PortableId
    decision_id: PortableId
    outcome_sha256: Sha256
    actor_sha256: Sha256
    authority_snapshot_sha256: Sha256
    executor_status: Literal["unavailable"] = "unavailable"
    failure_code: Literal["external_executor_unavailable"] = "external_executor_unavailable"
