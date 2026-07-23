"""Strict pure P2-027A local-data policy, evaluation, and audit values.

This module intentionally has no filesystem, storage, CLI, approval-ledger, or
orchestrator integration. It returns policy decisions; it never performs them.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from poker_deliberation.context_lifecycle import ContextClassification

LOCAL_DATA_POLICY_SCHEMA_VERSION: Final = "1.0.0"
LOCAL_DATA_POLICY_ID: Final = "p2-027a-local-data-policy-v1"
LOCAL_DATA_POLICY_VERSION: Final = "1.0.0"
LOCAL_DATA_CANONICALIZATION_VERSION: Final = "poker-local-data-policy-json-v1"
LOCAL_DATA_HASH_ALGORITHM: Final = "sha256"
LOCAL_DATA_EVALUATOR_VERSION: Final = "p2-027a-pure-evaluator-v1"

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_REPORT_ARTIFACT = re.compile(r"^agent_reports/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")
_TOOL_INPUT_ARTIFACT = re.compile(r"^tool_results/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.input\.json$")
_TOOL_RESULT_ARTIFACT = re.compile(r"^tool_results/[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$")

Clock = Callable[[], datetime]


class SubjectKind(StrEnum):
    ATTEMPT_CONTEXT = "attempt_context"
    RUN_PAYLOAD = "run_payload"
    RUN_AUDIT = "run_audit"
    RUN_REPORT = "run_report"
    APPLICATION_CACHE = "application_cache"
    APPLICATION_TEMP = "application_temp"
    LIFECYCLE_AUDIT = "lifecycle_audit"
    QUARANTINE_PAYLOAD = "quarantine_payload"
    DISPOSITION_RECEIPT = "disposition_receipt"


ArtifactSubjectKind = Literal[
    SubjectKind.RUN_PAYLOAD,
    SubjectKind.RUN_AUDIT,
    SubjectKind.RUN_REPORT,
]


class RetentionAnchorKind(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    VERIFIED_TERMINAL_PUBLISHED = "verified_terminal_published_at"
    APPLICATION_CREATED = "application_created_at"
    DECISION_COMMITTED = "decision_committed_at"
    QUARANTINE_ENTERED = "quarantine_entered_at"


class SubjectState(StrEnum):
    ACTIVE = "active"
    APPROVAL_PENDING = "approval_pending"
    VERIFIED_TERMINAL = "verified_terminal"
    INCOMPLETE = "incomplete"
    CORRUPT = "corrupt"
    ORPHAN_TRANSACTION = "orphan_transaction"
    QUARANTINED = "quarantined"
    UNSUPPORTED_FUTURE_VERSION = "unsupported_future_version"
    LEGACY_UNVERIFIED = "legacy_unverified"


class LifecycleDisposition(StrEnum):
    DENY_PERSISTENCE = "deny_persistence"
    RETAIN = "retain"
    PROTECTED = "protected"
    MANUAL_REVIEW = "manual_review"
    QUARANTINE_CANDIDATE = "quarantine_candidate"
    DELETE_CANDIDATE = "delete_candidate"


class EncryptionRequirement(StrEnum):
    DEFERRED_NO_CLAIM = "deferred_no_claim"
    REQUIRED_BEFORE_PERSISTENCE = "required_before_persistence"
    PERSISTENCE_FORBIDDEN = "persistence_forbidden"


class ClassificationSource(StrEnum):
    DEFAULT_INTERNAL = "default_internal"
    EXPLICIT_TRUSTED = "explicit_trusted"
    SOURCE_INHERITANCE = "source_inheritance"
    CREDENTIAL_DETECTION = "credential_detection"


class ProtectionReason(StrEnum):
    ACTIVE_RUN = "active_run"
    APPROVAL_PENDING = "approval_pending"
    LEGAL_HOLD = "legal_hold"
    OWNERSHIP_UNVERIFIED = "ownership_unverified"
    INTEGRITY_UNVERIFIED = "integrity_unverified"
    LINEAGE_UNVERIFIED = "lineage_unverified"
    UNSUPPORTED_FUTURE_VERSION = "unsupported_future_version"
    LEGACY_UNVERIFIED = "legacy_unverified"


class QuarantineReason(StrEnum):
    INCOMPLETE = "incomplete"
    CORRUPT = "corrupt"
    ORPHAN_TRANSACTION = "orphan_transaction"
    INTEGRITY_MISMATCH = "integrity_mismatch"
    LINEAGE_MISMATCH = "lineage_mismatch"
    PATH_CONFINEMENT_FAILURE = "path_confinement_failure"
    ENCRYPTION_REQUIREMENT_MISMATCH = "encryption_requirement_mismatch"


class LifecyclePolicyFailureCode(StrEnum):
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INVALID_POLICY = "invalid_policy"
    UNKNOWN_POLICY = "unknown_policy"
    UNKNOWN_CLASSIFICATION = "unknown_classification"
    UNKNOWN_ARTIFACT_KIND = "unknown_artifact_kind"
    INVALID_UTC = "invalid_utc"
    INVALID_RETENTION_TIME = "invalid_retention_time"
    CLOCK_ROLLBACK = "clock_rollback"
    POLICY_HASH_MISMATCH = "policy_hash_mismatch"
    CLASSIFICATION_DOWNGRADE_DENIED = "classification_downgrade_denied"
    OWNERSHIP_UNVERIFIED = "ownership_unverified"
    INTEGRITY_UNVERIFIED = "integrity_unverified"
    LINEAGE_UNVERIFIED = "lineage_unverified"
    SUBJECT_PROTECTED = "subject_protected"
    ENCRYPTION_REQUIRED = "encryption_required"
    PERSISTENCE_FORBIDDEN = "persistence_forbidden"
    DISPOSITION_DENIED = "disposition_denied"


class _LocalDataModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_local_data_json(value: Any) -> str:
    """Return the approved deterministic UTF-8 JSON domain as text."""

    try:
        return json.dumps(
            _canonical_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("local-data value is not canonical JSON") from exc


def canonical_local_data_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_local_data_json(value).encode("utf-8")).hexdigest()


def _require_utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class RetentionProfile(_LocalDataModel):
    policy_id: str = Field(min_length=1, max_length=96, pattern=r"^[a-z0-9-]+$")
    classification: ContextClassification
    retention_days: int = Field(ge=0, le=3650)
    encryption: EncryptionRequirement


class LocalDataPolicy(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    policy_id: Literal["p2-027a-local-data-policy-v1"] = LOCAL_DATA_POLICY_ID
    policy_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_VERSION
    canonicalization_version: Literal["poker-local-data-policy-json-v1"] = (
        LOCAL_DATA_CANONICALIZATION_VERSION
    )
    hash_algorithm: Literal["sha256"] = LOCAL_DATA_HASH_ALGORITHM
    profiles: tuple[RetentionProfile, ...]
    cache_max_days: Literal[7] = 7
    temp_max_days: Literal[1] = 1
    lifecycle_audit_days: Literal[365] = 365
    disposition_receipt_days: Literal[365] = 365
    quarantine_review_days: Literal[30] = 30

    @model_validator(mode="after")
    def validate_exact_profiles(self) -> LocalDataPolicy:
        expected = (
            (
                ContextClassification.PUBLIC,
                "public-run-365d-v1",
                365,
                EncryptionRequirement.DEFERRED_NO_CLAIM,
            ),
            (
                ContextClassification.INTERNAL,
                "internal-run-90d-v1",
                90,
                EncryptionRequirement.DEFERRED_NO_CLAIM,
            ),
            (
                ContextClassification.SENSITIVE,
                "sensitive-run-30d-v1",
                30,
                EncryptionRequirement.REQUIRED_BEFORE_PERSISTENCE,
            ),
            (
                ContextClassification.RESTRICTED,
                "restricted-no-persist-v1",
                0,
                EncryptionRequirement.PERSISTENCE_FORBIDDEN,
            ),
        )
        actual = tuple(
            (
                profile.classification,
                profile.policy_id,
                profile.retention_days,
                profile.encryption,
            )
            for profile in self.profiles
        )
        if actual != expected:
            raise ValueError("retention profiles do not match the approved canonical matrix")
        return self

    def profile_for(self, classification: ContextClassification) -> RetentionProfile:
        for profile in self.profiles:
            if profile.classification is classification:
                return profile
        raise ValueError("classification has no approved retention profile")

    @property
    def canonical_sha256(self) -> str:
        return canonical_local_data_sha256(self)


DEFAULT_LOCAL_DATA_POLICY = LocalDataPolicy(
    profiles=(
        RetentionProfile(
            policy_id="public-run-365d-v1",
            classification=ContextClassification.PUBLIC,
            retention_days=365,
            encryption=EncryptionRequirement.DEFERRED_NO_CLAIM,
        ),
        RetentionProfile(
            policy_id="internal-run-90d-v1",
            classification=ContextClassification.INTERNAL,
            retention_days=90,
            encryption=EncryptionRequirement.DEFERRED_NO_CLAIM,
        ),
        RetentionProfile(
            policy_id="sensitive-run-30d-v1",
            classification=ContextClassification.SENSITIVE,
            retention_days=30,
            encryption=EncryptionRequirement.REQUIRED_BEFORE_PERSISTENCE,
        ),
        RetentionProfile(
            policy_id="restricted-no-persist-v1",
            classification=ContextClassification.RESTRICTED,
            retention_days=0,
            encryption=EncryptionRequirement.PERSISTENCE_FORBIDDEN,
        ),
    )
)


class ArtifactClassification(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    logical_name: str = Field(min_length=1, max_length=256)
    subject_kind: ArtifactSubjectKind
    classification: ContextClassification
    classification_source: ClassificationSource


class LifecycleSubject(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    subject_kind: SubjectKind
    subject_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    logical_name: str = Field(min_length=1, max_length=256)
    classification: ContextClassification = ContextClassification.INTERNAL
    classification_source: ClassificationSource = ClassificationSource.DEFAULT_INTERNAL
    state: SubjectState
    retention_anchor_kind: RetentionAnchorKind = RetentionAnchorKind.NOT_APPLICABLE
    retention_started_at: datetime | None = None
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    revision: int | None = Field(default=None, ge=0)
    subject_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    owned_by_application: bool
    integrity_verified: bool
    lineage_verified: bool
    legal_hold: bool

    @field_validator("retention_started_at")
    @classmethod
    def validate_retention_started_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value, "retention_started_at")

    @model_validator(mode="after")
    def validate_anchor_contract(self) -> LifecycleSubject:
        if (
            self.classification_source is ClassificationSource.DEFAULT_INTERNAL
            and self.classification is not ContextClassification.INTERNAL
        ):
            raise ValueError("default classification source is internal only")
        if (
            self.classification_source is ClassificationSource.SOURCE_INHERITANCE
            and self.classification is ContextClassification.PUBLIC
        ):
            raise ValueError("source inheritance cannot lower the internal classification floor")
        if (
            self.classification_source is ClassificationSource.CREDENTIAL_DETECTION
            and self.classification is not ContextClassification.RESTRICTED
        ):
            raise ValueError("credential detection requires restricted classification")
        if (
            self.subject_kind
            in {
                SubjectKind.LIFECYCLE_AUDIT,
                SubjectKind.DISPOSITION_RECEIPT,
            }
            and self.classification is not ContextClassification.INTERNAL
        ):
            raise ValueError("lifecycle metadata subject kinds must be internal")
        expected = {
            SubjectKind.ATTEMPT_CONTEXT: RetentionAnchorKind.NOT_APPLICABLE,
            SubjectKind.APPLICATION_CACHE: RetentionAnchorKind.APPLICATION_CREATED,
            SubjectKind.APPLICATION_TEMP: RetentionAnchorKind.APPLICATION_CREATED,
            SubjectKind.LIFECYCLE_AUDIT: RetentionAnchorKind.DECISION_COMMITTED,
            SubjectKind.DISPOSITION_RECEIPT: RetentionAnchorKind.DECISION_COMMITTED,
            SubjectKind.QUARANTINE_PAYLOAD: RetentionAnchorKind.QUARANTINE_ENTERED,
        }
        required = expected.get(self.subject_kind)
        if required is not None and self.retention_anchor_kind is not required:
            raise ValueError("subject kind uses an invalid retention anchor")
        if self.subject_kind is SubjectKind.ATTEMPT_CONTEXT:
            if self.retention_started_at is not None:
                raise ValueError("attempt context has no durable retention start")
        elif required is not None and self.retention_started_at is None:
            raise ValueError("subject kind requires a retention start")
        if self.subject_kind in {
            SubjectKind.RUN_PAYLOAD,
            SubjectKind.RUN_AUDIT,
            SubjectKind.RUN_REPORT,
        }:
            if _artifact_kind(self.logical_name) is not self.subject_kind:
                raise ValueError("run subject logical name does not match its approved kind")
            if self.state is SubjectState.VERIFIED_TERMINAL and (
                self.retention_anchor_kind is not RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED
                or self.retention_started_at is None
            ):
                raise ValueError("verified terminal run data requires its publish anchor")
            if self.state is not SubjectState.VERIFIED_TERMINAL and (
                self.retention_anchor_kind
                not in {
                    RetentionAnchorKind.NOT_APPLICABLE,
                    RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
                }
            ):
                raise ValueError("run data uses an invalid retention anchor")
        return self


class LifecycleFailure(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    code: LifecyclePolicyFailureCode
    message: str = Field(min_length=1, max_length=256)
    retryable: Literal[False] = False
    filesystem_mutation: Literal[False] = False
    domain_mutation: Literal[False] = False
    manual_review_required: bool = False
    policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    subject_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )


class LifecycleAuditMetadata(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    policy_id: Literal["p2-027a-local-data-policy-v1"]
    policy_version: Literal["1.0.0"]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonicalization_version: Literal["poker-local-data-policy-json-v1"]
    hash_algorithm: Literal["sha256"]
    subject_kind: SubjectKind
    subject_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    logical_name: str = Field(min_length=1, max_length=256)
    run_id: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    revision: int | None = Field(default=None, ge=0)
    subject_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    classification: ContextClassification
    classification_source: ClassificationSource
    retention_anchor_kind: RetentionAnchorKind
    retention_started_at: datetime | None
    retention_expires_at: datetime | None
    evaluated_at: datetime
    subject_state: SubjectState
    proposed_disposition: LifecycleDisposition
    protection_reasons: tuple[ProtectionReason, ...] = ()
    quarantine_reasons: tuple[QuarantineReason, ...] = ()
    manual_review_required: bool = False
    failure_code: LifecyclePolicyFailureCode | None = None
    evaluator_version: Literal["p2-027a-pure-evaluator-v1"] = LOCAL_DATA_EVALUATOR_VERSION
    approval_reference: str | None = Field(
        default=None,
        min_length=1,
        max_length=192,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$",
    )
    action_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("retention_started_at", "retention_expires_at", "evaluated_at")
    @classmethod
    def validate_audit_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value, "audit timestamp")

    @field_validator("protection_reasons")
    @classmethod
    def canonical_protection_reasons(
        cls, value: tuple[ProtectionReason, ...]
    ) -> tuple[ProtectionReason, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.value)):
            raise ValueError("protection reasons must be sorted and unique")
        return value

    @field_validator("quarantine_reasons")
    @classmethod
    def canonical_quarantine_reasons(
        cls, value: tuple[QuarantineReason, ...]
    ) -> tuple[QuarantineReason, ...]:
        if value != tuple(sorted(set(value), key=lambda item: item.value)):
            raise ValueError("quarantine reasons must be sorted and unique")
        return value

    @property
    def canonical_sha256(self) -> str:
        return canonical_local_data_sha256(self)


class LifecycleEvaluationResult(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    status: Literal["evaluated", "failed"]
    audit: LifecycleAuditMetadata | None = None
    failure: LifecycleFailure | None = None

    @model_validator(mode="after")
    def exactly_one_payload(self) -> LifecycleEvaluationResult:
        if self.status == "evaluated" and (self.audit is None or self.failure is not None):
            raise ValueError("evaluated result requires only audit metadata")
        if self.status == "failed" and (self.failure is None or self.audit is not None):
            raise ValueError("failed result requires only a typed failure")
        return self


class LifecyclePolicyError(ValueError):
    def __init__(self, failure: LifecycleFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


def _failure(
    code: LifecyclePolicyFailureCode,
    message: str,
    *,
    policy: LocalDataPolicy | None = None,
    subject_id: str | None = None,
    manual_review_required: bool = False,
) -> LifecycleFailure:
    return LifecycleFailure(
        code=code,
        message=message,
        policy_sha256=policy.canonical_sha256 if policy is not None else None,
        manual_review_required=manual_review_required,
        subject_id=(
            subject_id if subject_id is not None and _PORTABLE_ID.fullmatch(subject_id) else None
        ),
    )


def _artifact_kind(logical_name: str) -> ArtifactSubjectKind | None:
    if logical_name in {
        "input.json",
        "normalized_case.json",
        "assumptions.json",
        "evidence.jsonl",
        "approvals.json",
    } or _REPORT_ARTIFACT.fullmatch(logical_name):
        return SubjectKind.RUN_PAYLOAD
    if _TOOL_INPUT_ARTIFACT.fullmatch(logical_name):
        return SubjectKind.RUN_PAYLOAD
    if logical_name in {
        ".poker-deliberation-run",
        "state.json",
        "assignments.json",
        "agent_execution_records.json",
        "security_events.json",
        "disputes.json",
    } or _TOOL_RESULT_ARTIFACT.fullmatch(logical_name):
        return SubjectKind.RUN_AUDIT
    if logical_name in {"final_report.json", "final_report.md"}:
        return SubjectKind.RUN_REPORT
    return None


_CLASSIFICATION_RANK = {
    ContextClassification.PUBLIC: 0,
    ContextClassification.INTERNAL: 1,
    ContextClassification.SENSITIVE: 2,
    ContextClassification.RESTRICTED: 3,
}


def classify_artifact(
    logical_name: str,
    *,
    source_classifications: Iterable[ContextClassification] = (),
    explicit_classification: ContextClassification | None = None,
    explicit_source_trusted: bool = False,
    contains_restricted_secret: bool = False,
    policy: LocalDataPolicy = DEFAULT_LOCAL_DATA_POLICY,
) -> ArtifactClassification:
    """Classify one known logical artifact without touching a path."""

    kind = _artifact_kind(logical_name)
    if kind is None:
        raise LifecyclePolicyError(
            _failure(
                LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND,
                "logical artifact kind is not approved",
                policy=policy,
            )
        )
    try:
        sources = tuple(ContextClassification(item) for item in source_classifications)
    except (TypeError, ValueError) as exc:
        raise LifecyclePolicyError(
            _failure(
                LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION,
                "source classification is not approved",
                policy=policy,
                manual_review_required=True,
            )
        ) from exc

    inherited = max(
        (ContextClassification.INTERNAL, *sources),
        key=lambda item: _CLASSIFICATION_RANK[item],
    )
    classification = inherited
    source = (
        ClassificationSource.SOURCE_INHERITANCE
        if sources
        else ClassificationSource.DEFAULT_INTERNAL
    )
    if explicit_classification is not None:
        try:
            requested = ContextClassification(explicit_classification)
        except (TypeError, ValueError) as exc:
            raise LifecyclePolicyError(
                _failure(
                    LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION,
                    "explicit classification is not approved",
                    policy=policy,
                    manual_review_required=True,
                )
            ) from exc
        if requested is ContextClassification.PUBLIC and not explicit_source_trusted:
            raise LifecyclePolicyError(
                _failure(
                    LifecyclePolicyFailureCode.CLASSIFICATION_DOWNGRADE_DENIED,
                    "public classification requires a trusted explicit source",
                    policy=policy,
                )
            )
        source_floor = (
            max(sources, key=lambda item: _CLASSIFICATION_RANK[item])
            if sources
            else ContextClassification.PUBLIC
        )
        if _CLASSIFICATION_RANK[requested] < _CLASSIFICATION_RANK[source_floor]:
            raise LifecyclePolicyError(
                _failure(
                    LifecyclePolicyFailureCode.CLASSIFICATION_DOWNGRADE_DENIED,
                    "classification downgrade is denied",
                    policy=policy,
                )
            )
        classification = requested
        source = ClassificationSource.EXPLICIT_TRUSTED
    if contains_restricted_secret:
        classification = ContextClassification.RESTRICTED
        source = ClassificationSource.CREDENTIAL_DETECTION
    return ArtifactClassification(
        logical_name=logical_name,
        subject_kind=kind,
        classification=classification,
        classification_source=source,
    )


def _validation_failure_code(exc: ValidationError) -> LifecyclePolicyFailureCode:
    locations = {str(part) for error in exc.errors() for part in error["loc"]}
    if "schema_version" in locations:
        return LifecyclePolicyFailureCode.UNSUPPORTED_SCHEMA
    if "classification" in locations:
        return LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION
    if "policy_id" in locations:
        return LifecyclePolicyFailureCode.UNKNOWN_POLICY
    if "retention_started_at" in locations:
        return LifecyclePolicyFailureCode.INVALID_UTC
    return LifecyclePolicyFailureCode.INVALID_POLICY


def _retention_days(
    subject: LifecycleSubject,
    profile: RetentionProfile,
    policy: LocalDataPolicy,
) -> int:
    if subject.subject_kind is SubjectKind.ATTEMPT_CONTEXT:
        return 0
    if subject.subject_kind is SubjectKind.APPLICATION_CACHE:
        return min(profile.retention_days, policy.cache_max_days)
    if subject.subject_kind is SubjectKind.APPLICATION_TEMP:
        return min(profile.retention_days, policy.temp_max_days)
    if subject.subject_kind is SubjectKind.LIFECYCLE_AUDIT:
        return policy.lifecycle_audit_days
    if subject.subject_kind is SubjectKind.DISPOSITION_RECEIPT:
        return policy.disposition_receipt_days
    if subject.subject_kind is SubjectKind.QUARANTINE_PAYLOAD:
        return policy.quarantine_review_days
    return profile.retention_days


def _protection_reasons(subject: LifecycleSubject) -> tuple[ProtectionReason, ...]:
    reasons: set[ProtectionReason] = set()
    if subject.state is SubjectState.ACTIVE:
        reasons.add(ProtectionReason.ACTIVE_RUN)
    if subject.state is SubjectState.APPROVAL_PENDING:
        reasons.add(ProtectionReason.APPROVAL_PENDING)
    if subject.legal_hold:
        reasons.add(ProtectionReason.LEGAL_HOLD)
    if not subject.owned_by_application:
        reasons.add(ProtectionReason.OWNERSHIP_UNVERIFIED)
    if not subject.integrity_verified:
        reasons.add(ProtectionReason.INTEGRITY_UNVERIFIED)
    if not subject.lineage_verified:
        reasons.add(ProtectionReason.LINEAGE_UNVERIFIED)
    if subject.state is SubjectState.UNSUPPORTED_FUTURE_VERSION:
        reasons.add(ProtectionReason.UNSUPPORTED_FUTURE_VERSION)
    if subject.state is SubjectState.LEGACY_UNVERIFIED:
        reasons.add(ProtectionReason.LEGACY_UNVERIFIED)
    return tuple(sorted(reasons, key=lambda item: item.value))


def _state_quarantine_reasons(subject: LifecycleSubject) -> set[QuarantineReason]:
    reasons: set[QuarantineReason] = set()
    if subject.state is SubjectState.INCOMPLETE:
        reasons.add(QuarantineReason.INCOMPLETE)
    if subject.state is SubjectState.CORRUPT:
        reasons.add(QuarantineReason.CORRUPT)
    if subject.state is SubjectState.ORPHAN_TRANSACTION:
        reasons.add(QuarantineReason.ORPHAN_TRANSACTION)
    return reasons


def _failed_result(failure: LifecycleFailure) -> LifecycleEvaluationResult:
    return LifecycleEvaluationResult(status="failed", failure=failure)


def evaluate_local_data(
    subject: LifecycleSubject | Mapping[str, Any],
    *,
    clock: Clock,
    policy: LocalDataPolicy | Mapping[str, Any] = DEFAULT_LOCAL_DATA_POLICY,
    expected_policy_sha256: str | None = None,
    encryption_available: bool = False,
    quarantine_reasons: Iterable[QuarantineReason] = (),
    approval_reference: str | None = None,
    action_digest: str | None = None,
) -> LifecycleEvaluationResult:
    """Evaluate one subject as a pure value with mutation fixed to zero."""

    try:
        effective_policy = LocalDataPolicy.model_validate(policy)
    except ValidationError as exc:
        code = _validation_failure_code(exc)
        return _failed_result(
            _failure(
                code,
                "local-data policy validation failed",
                manual_review_required=code
                in {
                    LifecyclePolicyFailureCode.UNKNOWN_POLICY,
                    LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION,
                },
            )
        )
    try:
        candidate = LifecycleSubject.model_validate(subject)
    except ValidationError as exc:
        code = _validation_failure_code(exc)
        return _failed_result(
            _failure(
                code,
                "local-data subject validation failed",
                policy=effective_policy,
                manual_review_required=code is LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION,
            )
        )
    if expected_policy_sha256 is not None and (
        not _SHA256.fullmatch(expected_policy_sha256)
        or expected_policy_sha256 != effective_policy.canonical_sha256
    ):
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.POLICY_HASH_MISMATCH,
                "local-data policy hash mismatch",
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )
    try:
        evaluated_at = _require_utc(clock(), "clock result")
    except Exception:
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.INVALID_UTC,
                "lifecycle clock did not return timezone-aware UTC",
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )
    if candidate.retention_started_at is not None and evaluated_at < candidate.retention_started_at:
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.CLOCK_ROLLBACK,
                "lifecycle clock precedes the retention anchor",
                policy=effective_policy,
                subject_id=candidate.subject_id,
                manual_review_required=True,
            )
        )

    profile = effective_policy.profile_for(candidate.classification)
    days = _retention_days(candidate, profile, effective_policy)
    try:
        retention_expires_at = (
            candidate.retention_started_at + timedelta(days=days)
            if candidate.retention_started_at is not None
            else None
        )
    except (OverflowError, ValueError):
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.INVALID_RETENTION_TIME,
                "retention expiry cannot be represented",
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )

    try:
        explicit_quarantine = {QuarantineReason(item) for item in quarantine_reasons}
    except (TypeError, ValueError):
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.INVALID_POLICY,
                "quarantine reason is not approved",
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )
    all_quarantine = _state_quarantine_reasons(candidate) | explicit_quarantine
    canonical_quarantine = tuple(sorted(all_quarantine, key=lambda item: item.value))
    protections = _protection_reasons(candidate)
    manual_review = (
        candidate.state
        in {
            SubjectState.UNSUPPORTED_FUTURE_VERSION,
            SubjectState.LEGACY_UNVERIFIED,
        }
        or ProtectionReason.OWNERSHIP_UNVERIFIED in protections
    )

    failure_code: LifecyclePolicyFailureCode | None = None
    if (
        candidate.classification is ContextClassification.RESTRICTED
        or candidate.subject_kind is SubjectKind.ATTEMPT_CONTEXT
    ):
        disposition = LifecycleDisposition.DENY_PERSISTENCE
        failure_code = LifecyclePolicyFailureCode.PERSISTENCE_FORBIDDEN
    elif (
        profile.encryption is EncryptionRequirement.REQUIRED_BEFORE_PERSISTENCE
        and not encryption_available
    ):
        disposition = LifecycleDisposition.DENY_PERSISTENCE
        failure_code = LifecyclePolicyFailureCode.ENCRYPTION_REQUIRED
    elif protections:
        disposition = LifecycleDisposition.PROTECTED
        if ProtectionReason.OWNERSHIP_UNVERIFIED in protections:
            failure_code = LifecyclePolicyFailureCode.OWNERSHIP_UNVERIFIED
        elif ProtectionReason.INTEGRITY_UNVERIFIED in protections:
            failure_code = LifecyclePolicyFailureCode.INTEGRITY_UNVERIFIED
        elif ProtectionReason.LINEAGE_UNVERIFIED in protections:
            failure_code = LifecyclePolicyFailureCode.LINEAGE_UNVERIFIED
        else:
            failure_code = LifecyclePolicyFailureCode.SUBJECT_PROTECTED
    elif canonical_quarantine:
        disposition = LifecycleDisposition.QUARANTINE_CANDIDATE
    elif (
        candidate.state in {SubjectState.VERIFIED_TERMINAL, SubjectState.QUARANTINED}
        and retention_expires_at is not None
        and evaluated_at >= retention_expires_at
    ):
        disposition = LifecycleDisposition.DELETE_CANDIDATE
    else:
        disposition = LifecycleDisposition.RETAIN

    try:
        audit = LifecycleAuditMetadata(
            policy_id=effective_policy.policy_id,
            policy_version=effective_policy.policy_version,
            policy_sha256=effective_policy.canonical_sha256,
            canonicalization_version=effective_policy.canonicalization_version,
            hash_algorithm=effective_policy.hash_algorithm,
            subject_kind=candidate.subject_kind,
            subject_id=candidate.subject_id,
            logical_name=candidate.logical_name,
            run_id=candidate.run_id,
            revision=candidate.revision,
            subject_sha256=candidate.subject_sha256,
            source_sha256=candidate.source_sha256,
            classification=candidate.classification,
            classification_source=candidate.classification_source,
            retention_anchor_kind=candidate.retention_anchor_kind,
            retention_started_at=candidate.retention_started_at,
            retention_expires_at=retention_expires_at,
            evaluated_at=evaluated_at,
            subject_state=candidate.state,
            proposed_disposition=disposition,
            protection_reasons=protections,
            quarantine_reasons=canonical_quarantine,
            manual_review_required=manual_review,
            failure_code=failure_code,
            approval_reference=approval_reference,
            action_digest=action_digest,
        )
    except ValidationError:
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.INVALID_POLICY,
                "lifecycle audit metadata validation failed",
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )
    return LifecycleEvaluationResult(status="evaluated", audit=audit)


def utc_datetime(year: int, month: int, day: int) -> datetime:
    """Small test/helper constructor that makes the UTC requirement explicit."""

    return datetime(year, month, day, tzinfo=UTC)


__all__ = [
    "DEFAULT_LOCAL_DATA_POLICY",
    "ArtifactClassification",
    "ClassificationSource",
    "EncryptionRequirement",
    "LifecycleAuditMetadata",
    "LifecycleDisposition",
    "LifecycleEvaluationResult",
    "LifecycleFailure",
    "LifecyclePolicyError",
    "LifecyclePolicyFailureCode",
    "LifecycleSubject",
    "LocalDataPolicy",
    "ProtectionReason",
    "QuarantineReason",
    "RetentionAnchorKind",
    "RetentionProfile",
    "SubjectKind",
    "SubjectState",
    "canonical_local_data_json",
    "canonical_local_data_sha256",
    "classify_artifact",
    "evaluate_local_data",
    "utc_datetime",
]
