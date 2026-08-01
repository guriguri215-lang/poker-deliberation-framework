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

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_APPROVAL_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_SECRET_METADATA = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\bAIza[A-Za-z0-9_-]{20,}\b|"
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|\bnpm_[A-Za-z0-9]{20,}\b|"
    r"\b(?:rk|sk)_(?:live|test)_[A-Za-z0-9]{10,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|"
    r"(?:api[_-]?key|password|passwd|secret|token)\s*[:=])",
    re.IGNORECASE,
)
_WINDOWS_RESERVED_STEMS = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_REPORT_ARTIFACT = re.compile(
    r"^agent_reports/(?P<identifier>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$"
)
_TOOL_INPUT_ARTIFACT = re.compile(
    r"^tool_results/(?P<identifier>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.input\.json$"
)
_TOOL_RESULT_ARTIFACT = re.compile(
    r"^tool_results/(?P<identifier>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$"
)

Clock = Callable[[], datetime]


def contains_restricted_secret_shape(value: str) -> bool:
    """Return whether bounded control or payload text matches a known secret shape."""

    return _SECRET_METADATA.search(value) is not None


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


class EncryptionCapabilityState(StrEnum):
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"


class SubjectEncryptionState(StrEnum):
    UNKNOWN_OR_UNENCRYPTED = "unknown_or_unencrypted"
    ENCRYPTED_VERIFIED = "encrypted_verified"
    REQUIREMENT_MISMATCH = "requirement_mismatch"


class RunVerificationBasis(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    FUTURE_VERIFIED_REVISION_V1 = "future_verified_revision_v1"
    LEGACY_V1_UNVERIFIED = "legacy_v1_unverified"


class OwnershipProvenance(StrEnum):
    RUN_CONTRACT_V1 = "run_contract_v1"
    TYPED_APPLICATION_METADATA_V1 = "typed_application_metadata_v1"
    FUTURE_VERIFIED_MANIFEST_V1 = "future_verified_manifest_v1"
    UNVERIFIED = "unverified"
    EXCLUDED_USER_MATERIAL = "excluded_user_material"
    EXCLUDED_GOAL_MANAGEMENT = "excluded_goal_management"
    EXCLUDED_REVIEW_TEST_OUTPUT = "excluded_review_test_output"
    EXCLUDED_PYTEST_SESSION = "excluded_pytest_session"
    EXCLUDED_TRACKED_SOURCE = "excluded_tracked_source"


class EvidenceVerificationState(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    MISMATCH = "mismatch"


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


_FAILURE_MESSAGES: Final[dict[LifecyclePolicyFailureCode, str]] = {
    LifecyclePolicyFailureCode.UNSUPPORTED_SCHEMA: "unsupported local-data schema",
    LifecyclePolicyFailureCode.INVALID_POLICY: "invalid local-data policy input",
    LifecyclePolicyFailureCode.UNKNOWN_POLICY: "unknown local-data policy",
    LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION: "unknown local-data classification",
    LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND: "unknown local-data artifact kind",
    LifecyclePolicyFailureCode.INVALID_UTC: "invalid lifecycle UTC timestamp",
    LifecyclePolicyFailureCode.INVALID_RETENTION_TIME: "invalid lifecycle retention time",
    LifecyclePolicyFailureCode.CLOCK_ROLLBACK: "lifecycle clock rollback",
    LifecyclePolicyFailureCode.POLICY_HASH_MISMATCH: "local-data policy hash mismatch",
    LifecyclePolicyFailureCode.CLASSIFICATION_DOWNGRADE_DENIED: (
        "local-data classification downgrade denied"
    ),
    LifecyclePolicyFailureCode.OWNERSHIP_UNVERIFIED: "local-data ownership unverified",
    LifecyclePolicyFailureCode.INTEGRITY_UNVERIFIED: "local-data integrity unverified",
    LifecyclePolicyFailureCode.LINEAGE_UNVERIFIED: "local-data lineage unverified",
    LifecyclePolicyFailureCode.SUBJECT_PROTECTED: "local-data subject protected",
    LifecyclePolicyFailureCode.ENCRYPTION_REQUIRED: "local-data encryption required",
    LifecyclePolicyFailureCode.PERSISTENCE_FORBIDDEN: "local-data persistence forbidden",
    LifecyclePolicyFailureCode.DISPOSITION_DENIED: "local-data disposition denied",
}


class _LocalDataModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _is_opaque_portable_id(value: str) -> bool:
    if not _PORTABLE_ID.fullmatch(value) or value.endswith("."):
        return False
    windows_stem = value.split(".", maxsplit=1)[0].upper()
    return windows_stem not in _WINDOWS_RESERVED_STEMS


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


def _metadata_identifier_digest(domain: str, value: str) -> str:
    prefix = f"p2-027a:{domain}:v1\0".encode()
    return hashlib.sha256(prefix + value.encode("utf-8")).hexdigest()


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


class ClassificationEvidence(_LocalDataModel):
    source_classifications: tuple[ContextClassification, ...] = ()
    explicit_classification: ContextClassification | None = None
    explicit_source_trusted: bool = False
    restricted_secret_check_completed: bool = False
    contains_restricted_secret: bool = False

    @field_validator("source_classifications")
    @classmethod
    def canonical_source_classifications(
        cls,
        value: tuple[ContextClassification, ...],
    ) -> tuple[ContextClassification, ...]:
        if len(value) != len(set(value)):
            raise ValueError("source classifications must be unique")
        if value != tuple(sorted(value, key=lambda item: _CLASSIFICATION_RANK[item])):
            raise ValueError("source classifications must use canonical sensitivity order")
        return value


class ArtifactClassification(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    logical_name: str = Field(min_length=1, max_length=256)
    subject_kind: ArtifactSubjectKind
    classification: ContextClassification
    classification_source: ClassificationSource
    classification_evidence: ClassificationEvidence

    @model_validator(mode="after")
    def validate_artifact_mapping(self) -> ArtifactClassification:
        if _artifact_kind(self.logical_name) is not self.subject_kind:
            raise ValueError("logical artifact name does not match its approved kind")
        expected = _classification_outcome(self.classification_evidence)
        if expected != (self.classification, self.classification_source):
            raise ValueError("artifact classification does not match its evidence")
        return self


class LifecycleSubject(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    subject_kind: SubjectKind
    subject_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    logical_name: str = Field(min_length=1, max_length=256)
    classification: ContextClassification = ContextClassification.INTERNAL
    classification_source: ClassificationSource = ClassificationSource.DEFAULT_INTERNAL
    classification_evidence: ClassificationEvidence = Field(default_factory=ClassificationEvidence)
    encryption_state: SubjectEncryptionState
    state: SubjectState
    retention_anchor_kind: RetentionAnchorKind = RetentionAnchorKind.NOT_APPLICABLE
    retention_started_at: datetime | None = None
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    revision: int | None = Field(default=None, ge=0)
    subject_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    run_verification_basis: RunVerificationBasis = RunVerificationBasis.NOT_APPLICABLE
    ownership_provenance: OwnershipProvenance
    integrity_state: EvidenceVerificationState
    lineage_state: EvidenceVerificationState
    legal_hold: bool

    @field_validator("subject_id", "logical_name", "run_id")
    @classmethod
    def reject_secret_metadata(cls, value: str | None) -> str | None:
        if value is not None and _SECRET_METADATA.search(value):
            raise ValueError("subject metadata must not contain a secret shape")
        return value

    @field_validator("subject_id", "run_id")
    @classmethod
    def validate_subject_opaque_identifiers(cls, value: str | None) -> str | None:
        if value is not None and not _is_opaque_portable_id(value):
            raise ValueError("subject identifier must be an opaque portable identifier")
        return value

    @field_validator("retention_started_at")
    @classmethod
    def validate_retention_started_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value, "retention_started_at")

    @model_validator(mode="after")
    def validate_anchor_contract(self) -> LifecycleSubject:
        expected_classification = _classification_outcome(self.classification_evidence)
        if expected_classification != (self.classification, self.classification_source):
            raise ValueError("subject classification does not match its evidence")
        if (
            self.subject_kind
            in {
                SubjectKind.LIFECYCLE_AUDIT,
                SubjectKind.DISPOSITION_RECEIPT,
            }
            and self.classification is not ContextClassification.INTERNAL
        ):
            raise ValueError("lifecycle metadata subject kinds must be internal")
        if self.subject_kind not in {
            SubjectKind.RUN_PAYLOAD,
            SubjectKind.RUN_AUDIT,
            SubjectKind.RUN_REPORT,
        } and not _is_opaque_portable_id(self.logical_name):
            raise ValueError("non-run logical name must be an opaque portable identifier")
        if self.state is SubjectState.QUARANTINED and (
            self.subject_kind is not SubjectKind.QUARANTINE_PAYLOAD
        ):
            raise ValueError("quarantined state requires a quarantine payload")
        if self.subject_kind is SubjectKind.QUARANTINE_PAYLOAD and (
            self.state is not SubjectState.QUARANTINED
        ):
            raise ValueError("quarantine payload requires quarantined state")
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
            if self.state is SubjectState.VERIFIED_TERMINAL:
                if (
                    self.retention_anchor_kind
                    is not RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED
                    or self.retention_started_at is None
                ):
                    raise ValueError("verified terminal run data requires its publish anchor")
                if (
                    self.run_verification_basis
                    is not RunVerificationBasis.FUTURE_VERIFIED_REVISION_V1
                    or self.run_id is None
                    or self.revision is None
                    or self.subject_sha256 is None
                    or self.source_sha256 is None
                ):
                    raise ValueError(
                        "verified terminal run data requires revision identity and hashes"
                    )
            elif self.state is SubjectState.LEGACY_UNVERIFIED:
                if (
                    self.run_verification_basis is not RunVerificationBasis.LEGACY_V1_UNVERIFIED
                    or self.encryption_state is not SubjectEncryptionState.UNKNOWN_OR_UNENCRYPTED
                    or self.ownership_provenance is not OwnershipProvenance.RUN_CONTRACT_V1
                ):
                    raise ValueError(
                        "legacy run data requires its unverified basis, ownership, "
                        "and encryption label"
                    )
            elif self.run_verification_basis is not RunVerificationBasis.NOT_APPLICABLE:
                raise ValueError("nonterminal run data cannot claim a verification basis")
            elif self.ownership_provenance is OwnershipProvenance.FUTURE_VERIFIED_MANIFEST_V1:
                raise ValueError("nonterminal run data cannot claim verified manifest ownership")
            if self.state is not SubjectState.VERIFIED_TERMINAL and (
                self.retention_anchor_kind
                not in {
                    RetentionAnchorKind.NOT_APPLICABLE,
                    RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
                }
            ):
                raise ValueError("run data uses an invalid retention anchor")
        elif self.run_verification_basis is not RunVerificationBasis.NOT_APPLICABLE:
            raise ValueError("non-run subject cannot claim a run verification basis")
        if self.subject_kind is SubjectKind.QUARANTINE_PAYLOAD and (
            self.subject_sha256 is None or self.source_sha256 is None
        ):
            raise ValueError("quarantine payload requires subject and source hashes")
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
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_fixed_message(self) -> LifecycleFailure:
        if self.message != _FAILURE_MESSAGES[self.code]:
            raise ValueError("failure message must be the fixed message for its code")
        return self


class LifecycleAuditMetadata(_LocalDataModel):
    schema_version: Literal["1.0.0"] = LOCAL_DATA_POLICY_SCHEMA_VERSION
    policy_id: Literal["p2-027a-local-data-policy-v1"]
    policy_version: Literal["1.0.0"]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    canonicalization_version: Literal["poker-local-data-policy-json-v1"]
    hash_algorithm: Literal["sha256"]
    subject_kind: SubjectKind
    subject_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_name: str = Field(min_length=1, max_length=256)
    run_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    revision: int | None = Field(default=None, ge=0)
    subject_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    classification: ContextClassification
    classification_source: ClassificationSource
    classification_evidence: ClassificationEvidence
    encryption_requirement: EncryptionRequirement
    encryption_capability: EncryptionCapabilityState
    subject_encryption_state: SubjectEncryptionState
    run_verification_basis: RunVerificationBasis
    ownership_provenance: OwnershipProvenance
    integrity_state: EvidenceVerificationState
    lineage_state: EvidenceVerificationState
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
        pattern=r"^[0-9a-f]{64}$",
    )
    action_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("retention_started_at", "retention_expires_at", "evaluated_at")
    @classmethod
    def validate_audit_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value, "audit timestamp")

    @field_validator("logical_name")
    @classmethod
    def reject_audit_secret_metadata(cls, value: str | None) -> str | None:
        if value is not None and _SECRET_METADATA.search(value):
            raise ValueError("audit metadata must not contain a secret shape")
        return value

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

    @model_validator(mode="after")
    def validate_audit_consistency(self) -> LifecycleAuditMetadata:
        if self.policy_sha256 != DEFAULT_LOCAL_DATA_POLICY.canonical_sha256:
            raise ValueError("audit policy hash does not match the approved policy")
        if (self.retention_started_at is None) != (self.retention_expires_at is None):
            raise ValueError("retention timestamps must either both be present or both be absent")
        if (
            self.retention_started_at is not None
            and self.retention_expires_at is not None
            and (
                self.retention_expires_at < self.retention_started_at
                or self.evaluated_at < self.retention_started_at
            )
        ):
            raise ValueError("audit retention timestamps are inconsistent")
        if self.subject_kind in {
            SubjectKind.RUN_PAYLOAD,
            SubjectKind.RUN_AUDIT,
            SubjectKind.RUN_REPORT,
        } and (
            _artifact_kind(self.logical_name) is not self.subject_kind
            or not _audit_run_logical_name_is_bounded(self.logical_name)
        ):
            raise ValueError("audit run logical name is not a bounded approved artifact")
        if self.subject_kind not in {
            SubjectKind.RUN_PAYLOAD,
            SubjectKind.RUN_AUDIT,
            SubjectKind.RUN_REPORT,
        } and not _SHA256.fullmatch(self.logical_name):
            raise ValueError("audit non-run logical name must be a domain-separated digest")
        if _classification_outcome(self.classification_evidence) != (
            self.classification,
            self.classification_source,
        ):
            raise ValueError("audit classification does not match its evidence")
        try:
            audit_subject = LifecycleSubject(
                subject_kind=self.subject_kind,
                subject_id=self.subject_id,
                logical_name=self.logical_name,
                classification=self.classification,
                classification_source=self.classification_source,
                classification_evidence=self.classification_evidence,
                encryption_state=self.subject_encryption_state,
                state=self.subject_state,
                retention_anchor_kind=self.retention_anchor_kind,
                retention_started_at=self.retention_started_at,
                run_id=self.run_id,
                revision=self.revision,
                subject_sha256=self.subject_sha256,
                source_sha256=self.source_sha256,
                run_verification_basis=self.run_verification_basis,
                ownership_provenance=self.ownership_provenance,
                integrity_state=self.integrity_state,
                lineage_state=self.lineage_state,
                legal_hold=ProtectionReason.LEGAL_HOLD in self.protection_reasons,
            )
        except ValidationError as exc:
            raise ValueError("audit subject metadata is inconsistent") from exc
        if self.protection_reasons != _protection_reasons(audit_subject):
            raise ValueError("audit protection reasons do not match subject evidence")
        state_quarantine = _state_quarantine_reasons(audit_subject)
        if not state_quarantine <= set(self.quarantine_reasons):
            raise ValueError("audit quarantine reasons omit subject evidence")
        profile = DEFAULT_LOCAL_DATA_POLICY.profile_for(self.classification)
        if self.encryption_requirement is not profile.encryption:
            raise ValueError("audit encryption requirement does not match the policy")
        retention_days = _retention_days(
            audit_subject,
            profile,
            DEFAULT_LOCAL_DATA_POLICY,
        )
        try:
            expected_expiry = (
                self.retention_started_at + timedelta(days=retention_days)
                if self.retention_started_at is not None
                else None
            )
        except (OverflowError, ValueError) as exc:
            raise ValueError("audit retention expiry cannot be represented") from exc
        if self.retention_expires_at != expected_expiry:
            raise ValueError("audit retention expiry does not match the approved policy")
        (
            expected_disposition,
            expected_failure_code,
            expected_manual_review,
        ) = _derive_disposition(
            audit_subject,
            encryption_requirement=self.encryption_requirement,
            encryption_capability=self.encryption_capability,
            protection_reasons=self.protection_reasons,
            quarantine_reasons=self.quarantine_reasons,
            retention_expires_at=self.retention_expires_at,
            evaluated_at=self.evaluated_at,
        )
        if (
            self.proposed_disposition,
            self.failure_code,
            self.manual_review_required,
        ) != (
            expected_disposition,
            expected_failure_code,
            expected_manual_review,
        ):
            raise ValueError("audit disposition does not match lifecycle evidence")
        if self.proposed_disposition is LifecycleDisposition.DELETE_CANDIDATE:
            run_subject = self.subject_kind in {
                SubjectKind.RUN_PAYLOAD,
                SubjectKind.RUN_AUDIT,
                SubjectKind.RUN_REPORT,
            }
            if (
                self.protection_reasons
                or self.quarantine_reasons
                or self.failure_code is not None
                or self.retention_expires_at is None
                or self.evaluated_at < self.retention_expires_at
                or self.subject_sha256 is None
                or self.source_sha256 is None
            ):
                raise ValueError("delete candidate lacks clean expiry and hash evidence")
            if self.subject_state is SubjectState.VERIFIED_TERMINAL:
                if run_subject and (
                    self.run_verification_basis
                    is not RunVerificationBasis.FUTURE_VERIFIED_REVISION_V1
                    or self.run_id is None
                    or self.revision is None
                ):
                    raise ValueError("verified run delete candidate lacks revision evidence")
            elif not (
                self.subject_kind is SubjectKind.QUARANTINE_PAYLOAD
                and self.subject_state is SubjectState.QUARANTINED
            ):
                raise ValueError("delete candidate uses an ineligible subject state")
            expected_ownership = (
                OwnershipProvenance.FUTURE_VERIFIED_MANIFEST_V1
                if run_subject
                else OwnershipProvenance.TYPED_APPLICATION_METADATA_V1
            )
            if (
                self.ownership_provenance is not expected_ownership
                or self.integrity_state is not EvidenceVerificationState.VERIFIED
                or self.lineage_state is not EvidenceVerificationState.VERIFIED
            ):
                raise ValueError("delete candidate lacks verified provenance and evidence")
        elif self.proposed_disposition is LifecycleDisposition.PROTECTED:
            if not self.protection_reasons or self.failure_code is None:
                raise ValueError("protected audit requires protection reasons and a failure code")
            if (
                self.subject_state
                in {
                    SubjectState.LEGACY_UNVERIFIED,
                    SubjectState.UNSUPPORTED_FUTURE_VERSION,
                }
                or ProtectionReason.OWNERSHIP_UNVERIFIED in self.protection_reasons
            ) and not self.manual_review_required:
                raise ValueError("protected audit omits required manual review")
        elif self.proposed_disposition is LifecycleDisposition.QUARANTINE_CANDIDATE:
            if (
                not self.quarantine_reasons
                or self.protection_reasons
                or self.failure_code is not None
            ):
                raise ValueError("quarantine candidate requires only quarantine reasons")
        elif self.proposed_disposition is LifecycleDisposition.DENY_PERSISTENCE:
            if self.failure_code not in {
                LifecyclePolicyFailureCode.ENCRYPTION_REQUIRED,
                LifecyclePolicyFailureCode.PERSISTENCE_FORBIDDEN,
            }:
                raise ValueError("persistence denial requires its typed failure code")
            if (
                self.failure_code is LifecyclePolicyFailureCode.PERSISTENCE_FORBIDDEN
                and self.classification is not ContextClassification.RESTRICTED
                and self.subject_kind is not SubjectKind.ATTEMPT_CONTEXT
            ):
                raise ValueError("persistence-forbidden audit lacks a forbidden subject")
            if self.failure_code is LifecyclePolicyFailureCode.ENCRYPTION_REQUIRED and (
                self.classification is not ContextClassification.SENSITIVE
                or self.encryption_requirement
                is not EncryptionRequirement.REQUIRED_BEFORE_PERSISTENCE
                or (
                    self.encryption_capability is EncryptionCapabilityState.AVAILABLE
                    and self.subject_encryption_state is SubjectEncryptionState.ENCRYPTED_VERIFIED
                )
            ):
                raise ValueError("encryption denial conflicts with encryption evidence")
        elif self.proposed_disposition is LifecycleDisposition.MANUAL_REVIEW:
            if (
                not self.manual_review_required
                or not self.protection_reasons
                or self.failure_code is None
            ):
                raise ValueError("manual review requires protection evidence")
        elif self.proposed_disposition is LifecycleDisposition.RETAIN:
            if self.protection_reasons or self.quarantine_reasons or self.failure_code is not None:
                raise ValueError("retain audit conflicts with lifecycle evidence")
        if self.manual_review_required and self.proposed_disposition not in {
            LifecycleDisposition.DENY_PERSISTENCE,
            LifecycleDisposition.PROTECTED,
            LifecycleDisposition.MANUAL_REVIEW,
        }:
            raise ValueError("manual review flag requires a protected disposition")
        if (
            self.classification is ContextClassification.SENSITIVE
            and self.proposed_disposition
            not in {
                LifecycleDisposition.DENY_PERSISTENCE,
                LifecycleDisposition.PROTECTED,
                LifecycleDisposition.QUARANTINE_CANDIDATE,
            }
            and (
                self.encryption_capability is not EncryptionCapabilityState.AVAILABLE
                or self.subject_encryption_state is not SubjectEncryptionState.ENCRYPTED_VERIFIED
            )
        ):
            raise ValueError("sensitive audit lacks verified encryption evidence")
        return self

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
    *,
    policy: LocalDataPolicy | None = None,
    subject_id: str | None = None,
    manual_review_required: bool = False,
) -> LifecycleFailure:
    return LifecycleFailure(
        code=code,
        message=_FAILURE_MESSAGES[code],
        policy_sha256=policy.canonical_sha256 if policy is not None else None,
        manual_review_required=manual_review_required,
        subject_id=(
            _metadata_identifier_digest("subject-id", subject_id)
            if subject_id is not None
            else None
        ),
    )


_FIXED_RUN_PAYLOAD_ARTIFACTS = frozenset(
    {
        "confirmed_review_source.txt",
        "confirmed_review_candidate.json",
        "confirmed_review_confirmation.json",
        "bounded_nl_source.txt",
        "bounded_nl_candidate.json",
        "bounded_nl_confirmation.json",
        "input.json",
        "range_equity_binding.json",
        "normalization.json",
        "normalized_case.json",
        "assumptions.json",
        "evidence.jsonl",
        "approvals.json",
        "stdout.txt",
        "stderr.txt",
    }
)
_FIXED_RUN_AUDIT_ARTIFACTS = frozenset(
    {
        ".poker-deliberation-run",
        "state.json",
        "assignments.json",
        "agent_execution_records.json",
        "budget_state.json",
        "isolated_job_state.json",
        "security_events.json",
        "disputes.json",
    }
)
_FIXED_RUN_REPORT_ARTIFACTS = frozenset(
    {
        "final_report.json",
        "final_report.md",
        "confirmed_review_provenance.json",
        "bounded_nl_provenance.json",
    }
)


def _approved_variable_artifact(
    pattern: re.Pattern[str],
    logical_name: str,
) -> re.Match[str] | None:
    match = pattern.fullmatch(logical_name)
    if match is None or not _is_opaque_portable_id(match.group("identifier")):
        return None
    return match


def _artifact_kind(logical_name: str) -> ArtifactSubjectKind | None:
    if logical_name in _FIXED_RUN_PAYLOAD_ARTIFACTS or _approved_variable_artifact(
        _REPORT_ARTIFACT,
        logical_name,
    ):
        return SubjectKind.RUN_PAYLOAD
    if _approved_variable_artifact(_TOOL_INPUT_ARTIFACT, logical_name):
        return SubjectKind.RUN_PAYLOAD
    if logical_name in _FIXED_RUN_AUDIT_ARTIFACTS or _approved_variable_artifact(
        _TOOL_RESULT_ARTIFACT,
        logical_name,
    ):
        return SubjectKind.RUN_AUDIT
    if logical_name in _FIXED_RUN_REPORT_ARTIFACTS:
        return SubjectKind.RUN_REPORT
    return None


def _audit_run_logical_name(logical_name: str) -> str:
    report_match = _approved_variable_artifact(_REPORT_ARTIFACT, logical_name)
    if report_match is not None:
        digest = _metadata_identifier_digest(
            "agent-report-id",
            report_match.group("identifier"),
        )
        return f"agent_reports/{digest}.json"
    tool_input_match = _approved_variable_artifact(_TOOL_INPUT_ARTIFACT, logical_name)
    if tool_input_match is not None:
        digest = _metadata_identifier_digest(
            "tool-input-id",
            tool_input_match.group("identifier"),
        )
        return f"tool_results/{digest}.input.json"
    tool_result_match = _approved_variable_artifact(_TOOL_RESULT_ARTIFACT, logical_name)
    if tool_result_match is not None:
        digest = _metadata_identifier_digest(
            "tool-result-id",
            tool_result_match.group("identifier"),
        )
        return f"tool_results/{digest}.json"
    return logical_name


def _audit_run_logical_name_is_bounded(logical_name: str) -> bool:
    if logical_name in (
        _FIXED_RUN_PAYLOAD_ARTIFACTS | _FIXED_RUN_AUDIT_ARTIFACTS | _FIXED_RUN_REPORT_ARTIFACTS
    ):
        return True
    for pattern in (_REPORT_ARTIFACT, _TOOL_INPUT_ARTIFACT, _TOOL_RESULT_ARTIFACT):
        match = pattern.fullmatch(logical_name)
        if match is not None:
            return _SHA256.fullmatch(match.group("identifier")) is not None
    return False


_CLASSIFICATION_RANK = {
    ContextClassification.PUBLIC: 0,
    ContextClassification.INTERNAL: 1,
    ContextClassification.SENSITIVE: 2,
    ContextClassification.RESTRICTED: 3,
}


def _classification_outcome(
    evidence: ClassificationEvidence,
) -> tuple[ContextClassification, ClassificationSource]:
    sources = evidence.source_classifications
    inherited = max(
        (ContextClassification.INTERNAL, *sources),
        key=lambda item: _CLASSIFICATION_RANK[item],
    )
    if evidence.contains_restricted_secret:
        return (
            ContextClassification.RESTRICTED,
            ClassificationSource.CREDENTIAL_DETECTION,
        )
    requested = evidence.explicit_classification
    if requested is None:
        return (
            inherited,
            ClassificationSource.SOURCE_INHERITANCE
            if sources
            else ClassificationSource.DEFAULT_INTERNAL,
        )
    if not evidence.explicit_source_trusted:
        raise ValueError("explicit classification requires a trusted source")
    if requested is ContextClassification.PUBLIC and not evidence.restricted_secret_check_completed:
        raise ValueError("public classification requires a completed clean restricted-secret check")
    source_floor = (
        max(sources, key=lambda item: _CLASSIFICATION_RANK[item])
        if sources
        else ContextClassification.PUBLIC
    )
    if _CLASSIFICATION_RANK[requested] < _CLASSIFICATION_RANK[source_floor]:
        raise ValueError("classification downgrade is denied")
    return requested, ClassificationSource.EXPLICIT_TRUSTED


def classify_artifact(
    logical_name: str,
    *,
    source_classifications: Iterable[ContextClassification] = (),
    explicit_classification: ContextClassification | None = None,
    explicit_source_trusted: bool = False,
    restricted_secret_check_completed: bool = False,
    contains_restricted_secret: bool = False,
    policy: LocalDataPolicy | Mapping[str, Any] = DEFAULT_LOCAL_DATA_POLICY,
) -> ArtifactClassification:
    """Classify one known logical artifact without touching a path."""

    try:
        effective_policy = LocalDataPolicy.model_validate(policy)
    except ValidationError as exc:
        code = _validation_failure_code(exc)
        raise LifecyclePolicyError(
            _failure(
                code,
                manual_review_required=code
                in {
                    LifecyclePolicyFailureCode.UNKNOWN_POLICY,
                    LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION,
                    LifecyclePolicyFailureCode.UNSUPPORTED_SCHEMA,
                },
            )
        ) from exc
    if not isinstance(logical_name, str):
        raise LifecyclePolicyError(
            _failure(
                LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND,
                policy=effective_policy,
                manual_review_required=True,
            )
        )
    kind = _artifact_kind(logical_name)
    if kind is None:
        raise LifecyclePolicyError(
            _failure(
                LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND,
                policy=effective_policy,
                manual_review_required=True,
            )
        )
    try:
        raw_sources = tuple(source_classifications)
        if any(not isinstance(item, ContextClassification) for item in raw_sources):
            raise TypeError
        canonical_sources = tuple(
            sorted(set(raw_sources), key=lambda item: _CLASSIFICATION_RANK[item])
        )
        evidence = ClassificationEvidence(
            source_classifications=canonical_sources,
            explicit_classification=explicit_classification,
            explicit_source_trusted=explicit_source_trusted,
            restricted_secret_check_completed=restricted_secret_check_completed,
            contains_restricted_secret=contains_restricted_secret,
        )
    except Exception as exc:
        code = (
            LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION
            if isinstance(exc, TypeError)
            or (
                isinstance(exc, ValidationError)
                and any(
                    "classification" in str(part) for error in exc.errors() for part in error["loc"]
                )
            )
            else LifecyclePolicyFailureCode.INVALID_POLICY
        )
        raise LifecyclePolicyError(
            _failure(
                code,
                policy=effective_policy,
                manual_review_required=code is LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION,
            )
        ) from exc
    try:
        classification, source = _classification_outcome(evidence)
    except ValueError as exc:
        raise LifecyclePolicyError(
            _failure(
                LifecyclePolicyFailureCode.CLASSIFICATION_DOWNGRADE_DENIED,
                policy=effective_policy,
            )
        ) from exc
    return ArtifactClassification(
        logical_name=logical_name,
        subject_kind=kind,
        classification=classification,
        classification_source=source,
        classification_evidence=evidence,
    )


def _validation_failure_code(exc: ValidationError) -> LifecyclePolicyFailureCode:
    locations = {str(part) for error in exc.errors() for part in error["loc"]}
    messages = " ".join(str(error["msg"]) for error in exc.errors())
    if "schema_version" in locations:
        return LifecyclePolicyFailureCode.UNSUPPORTED_SCHEMA
    if any("classification" in location for location in locations):
        return LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION
    if "policy_id" in locations:
        return LifecyclePolicyFailureCode.UNKNOWN_POLICY
    if "retention_started_at" in locations:
        return LifecyclePolicyFailureCode.INVALID_UTC
    if "logical artifact" in messages or "logical name" in messages:
        return LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND
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
    run_subject = subject.subject_kind in {
        SubjectKind.RUN_PAYLOAD,
        SubjectKind.RUN_AUDIT,
        SubjectKind.RUN_REPORT,
    }
    if run_subject and subject.state is SubjectState.VERIFIED_TERMINAL:
        verified_ownership = (
            subject.ownership_provenance is OwnershipProvenance.FUTURE_VERIFIED_MANIFEST_V1
        )
    elif run_subject:
        verified_ownership = subject.ownership_provenance in {
            OwnershipProvenance.RUN_CONTRACT_V1,
            OwnershipProvenance.FUTURE_VERIFIED_MANIFEST_V1,
        }
    else:
        verified_ownership = (
            subject.ownership_provenance is OwnershipProvenance.TYPED_APPLICATION_METADATA_V1
        )
    if not verified_ownership:
        reasons.add(ProtectionReason.OWNERSHIP_UNVERIFIED)
    if subject.integrity_state is EvidenceVerificationState.UNVERIFIED:
        reasons.add(ProtectionReason.INTEGRITY_UNVERIFIED)
    if subject.lineage_state is EvidenceVerificationState.UNVERIFIED:
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
    if subject.integrity_state is EvidenceVerificationState.MISMATCH:
        reasons.add(QuarantineReason.INTEGRITY_MISMATCH)
    if subject.lineage_state is EvidenceVerificationState.MISMATCH:
        reasons.add(QuarantineReason.LINEAGE_MISMATCH)
    if subject.encryption_state is SubjectEncryptionState.REQUIREMENT_MISMATCH:
        reasons.add(QuarantineReason.ENCRYPTION_REQUIREMENT_MISMATCH)
    return reasons


def _delete_evidence_complete(subject: LifecycleSubject) -> bool:
    if (
        subject.subject_sha256 is None
        or subject.source_sha256 is None
        or subject.integrity_state is not EvidenceVerificationState.VERIFIED
        or subject.lineage_state is not EvidenceVerificationState.VERIFIED
    ):
        return False
    run_subject = subject.subject_kind in {
        SubjectKind.RUN_PAYLOAD,
        SubjectKind.RUN_AUDIT,
        SubjectKind.RUN_REPORT,
    }
    if run_subject:
        return (
            subject.state is SubjectState.VERIFIED_TERMINAL
            and subject.ownership_provenance is OwnershipProvenance.FUTURE_VERIFIED_MANIFEST_V1
            and subject.run_verification_basis is RunVerificationBasis.FUTURE_VERIFIED_REVISION_V1
            and subject.run_id is not None
            and subject.revision is not None
        )
    return subject.ownership_provenance is OwnershipProvenance.TYPED_APPLICATION_METADATA_V1 and (
        subject.state is SubjectState.VERIFIED_TERMINAL
        or (
            subject.subject_kind is SubjectKind.QUARANTINE_PAYLOAD
            and subject.state is SubjectState.QUARANTINED
        )
    )


def _derive_disposition(
    subject: LifecycleSubject,
    *,
    encryption_requirement: EncryptionRequirement,
    encryption_capability: EncryptionCapabilityState,
    protection_reasons: tuple[ProtectionReason, ...],
    quarantine_reasons: tuple[QuarantineReason, ...],
    retention_expires_at: datetime | None,
    evaluated_at: datetime,
) -> tuple[LifecycleDisposition, LifecyclePolicyFailureCode | None, bool]:
    manual_review_required = (
        subject.state
        in {
            SubjectState.UNSUPPORTED_FUTURE_VERSION,
            SubjectState.LEGACY_UNVERIFIED,
        }
        or ProtectionReason.OWNERSHIP_UNVERIFIED in protection_reasons
    )
    if subject.subject_kind is SubjectKind.ATTEMPT_CONTEXT:
        return (
            LifecycleDisposition.DENY_PERSISTENCE,
            LifecyclePolicyFailureCode.PERSISTENCE_FORBIDDEN,
            manual_review_required,
        )
    if subject.classification is ContextClassification.RESTRICTED:
        return (
            LifecycleDisposition.DENY_PERSISTENCE,
            LifecyclePolicyFailureCode.PERSISTENCE_FORBIDDEN,
            manual_review_required,
        )
    if (
        encryption_requirement is EncryptionRequirement.REQUIRED_BEFORE_PERSISTENCE
        and subject.encryption_state is not SubjectEncryptionState.REQUIREMENT_MISMATCH
        and (
            encryption_capability is not EncryptionCapabilityState.AVAILABLE
            or subject.encryption_state is not SubjectEncryptionState.ENCRYPTED_VERIFIED
        )
    ):
        return (
            LifecycleDisposition.DENY_PERSISTENCE,
            LifecyclePolicyFailureCode.ENCRYPTION_REQUIRED,
            manual_review_required,
        )
    if protection_reasons:
        if ProtectionReason.OWNERSHIP_UNVERIFIED in protection_reasons:
            failure_code = LifecyclePolicyFailureCode.OWNERSHIP_UNVERIFIED
        elif ProtectionReason.INTEGRITY_UNVERIFIED in protection_reasons:
            failure_code = LifecyclePolicyFailureCode.INTEGRITY_UNVERIFIED
        elif ProtectionReason.LINEAGE_UNVERIFIED in protection_reasons:
            failure_code = LifecyclePolicyFailureCode.LINEAGE_UNVERIFIED
        else:
            failure_code = LifecyclePolicyFailureCode.SUBJECT_PROTECTED
        return (
            LifecycleDisposition.PROTECTED,
            failure_code,
            manual_review_required,
        )
    if subject.encryption_state is SubjectEncryptionState.REQUIREMENT_MISMATCH:
        return (
            LifecycleDisposition.QUARANTINE_CANDIDATE,
            None,
            manual_review_required,
        )
    if quarantine_reasons:
        return (
            LifecycleDisposition.QUARANTINE_CANDIDATE,
            None,
            manual_review_required,
        )
    if (
        (
            subject.state is SubjectState.VERIFIED_TERMINAL
            or (
                subject.subject_kind is SubjectKind.QUARANTINE_PAYLOAD
                and subject.state is SubjectState.QUARANTINED
            )
        )
        and retention_expires_at is not None
        and evaluated_at >= retention_expires_at
        and _delete_evidence_complete(subject)
    ):
        return (
            LifecycleDisposition.DELETE_CANDIDATE,
            None,
            manual_review_required,
        )
    return LifecycleDisposition.RETAIN, None, manual_review_required


def _failed_result(failure: LifecycleFailure) -> LifecycleEvaluationResult:
    return LifecycleEvaluationResult(status="failed", failure=failure)


def evaluate_local_data(
    subject: LifecycleSubject | Mapping[str, Any],
    *,
    clock: Clock,
    policy: LocalDataPolicy | Mapping[str, Any] = DEFAULT_LOCAL_DATA_POLICY,
    expected_policy_sha256: str | None = None,
    encryption_capability: EncryptionCapabilityState = EncryptionCapabilityState.UNAVAILABLE,
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
                manual_review_required=code
                in {
                    LifecyclePolicyFailureCode.UNKNOWN_POLICY,
                    LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION,
                    LifecyclePolicyFailureCode.UNSUPPORTED_SCHEMA,
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
                policy=effective_policy,
                manual_review_required=code
                in {
                    LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND,
                    LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION,
                    LifecyclePolicyFailureCode.UNKNOWN_POLICY,
                    LifecyclePolicyFailureCode.UNSUPPORTED_SCHEMA,
                },
            )
        )
    if expected_policy_sha256 is not None and (
        not isinstance(expected_policy_sha256, str)
        or not _SHA256.fullmatch(expected_policy_sha256)
        or expected_policy_sha256 != effective_policy.canonical_sha256
    ):
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.POLICY_HASH_MISMATCH,
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )
    if approval_reference is not None and (
        not isinstance(approval_reference, str)
        or not _APPROVAL_REFERENCE.fullmatch(approval_reference)
        or _SECRET_METADATA.search(approval_reference)
    ):
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.INVALID_POLICY,
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
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )
    if candidate.retention_started_at is not None and evaluated_at < candidate.retention_started_at:
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.CLOCK_ROLLBACK,
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
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )

    try:
        explicit_quarantine_items = tuple(quarantine_reasons)
        if any(not isinstance(item, QuarantineReason) for item in explicit_quarantine_items):
            raise TypeError
        explicit_quarantine = set(explicit_quarantine_items)
    except Exception:
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.INVALID_POLICY,
                policy=effective_policy,
                subject_id=candidate.subject_id,
            )
        )
    all_quarantine = _state_quarantine_reasons(candidate) | explicit_quarantine
    canonical_quarantine = tuple(sorted(all_quarantine, key=lambda item: item.value))
    protections = _protection_reasons(candidate)
    disposition, failure_code, manual_review = _derive_disposition(
        candidate,
        encryption_requirement=profile.encryption,
        encryption_capability=encryption_capability,
        protection_reasons=protections,
        quarantine_reasons=canonical_quarantine,
        retention_expires_at=retention_expires_at,
        evaluated_at=evaluated_at,
    )

    try:
        audit = LifecycleAuditMetadata(
            policy_id=effective_policy.policy_id,
            policy_version=effective_policy.policy_version,
            policy_sha256=effective_policy.canonical_sha256,
            canonicalization_version=effective_policy.canonicalization_version,
            hash_algorithm=effective_policy.hash_algorithm,
            subject_kind=candidate.subject_kind,
            subject_id=_metadata_identifier_digest("subject-id", candidate.subject_id),
            logical_name=(
                _audit_run_logical_name(candidate.logical_name)
                if candidate.subject_kind
                in {
                    SubjectKind.RUN_PAYLOAD,
                    SubjectKind.RUN_AUDIT,
                    SubjectKind.RUN_REPORT,
                }
                else _metadata_identifier_digest("logical-name", candidate.logical_name)
            ),
            run_id=(
                _metadata_identifier_digest("run-id", candidate.run_id)
                if candidate.run_id is not None
                else None
            ),
            revision=candidate.revision,
            subject_sha256=candidate.subject_sha256,
            source_sha256=candidate.source_sha256,
            classification=candidate.classification,
            classification_source=candidate.classification_source,
            classification_evidence=candidate.classification_evidence,
            encryption_requirement=profile.encryption,
            encryption_capability=encryption_capability,
            subject_encryption_state=candidate.encryption_state,
            run_verification_basis=candidate.run_verification_basis,
            ownership_provenance=candidate.ownership_provenance,
            integrity_state=candidate.integrity_state,
            lineage_state=candidate.lineage_state,
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
            approval_reference=(
                _metadata_identifier_digest("approval-reference", approval_reference)
                if approval_reference is not None
                else None
            ),
            action_digest=action_digest,
        )
    except ValidationError:
        return _failed_result(
            _failure(
                LifecyclePolicyFailureCode.INVALID_POLICY,
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
    "ClassificationEvidence",
    "ClassificationSource",
    "EncryptionCapabilityState",
    "EncryptionRequirement",
    "EvidenceVerificationState",
    "LifecycleAuditMetadata",
    "LifecycleDisposition",
    "LifecycleEvaluationResult",
    "LifecycleFailure",
    "LifecyclePolicyError",
    "LifecyclePolicyFailureCode",
    "LifecycleSubject",
    "LocalDataPolicy",
    "OwnershipProvenance",
    "ProtectionReason",
    "QuarantineReason",
    "RetentionAnchorKind",
    "RetentionProfile",
    "RunVerificationBasis",
    "SubjectEncryptionState",
    "SubjectKind",
    "SubjectState",
    "canonical_local_data_json",
    "canonical_local_data_sha256",
    "classify_artifact",
    "evaluate_local_data",
    "utc_datetime",
]
