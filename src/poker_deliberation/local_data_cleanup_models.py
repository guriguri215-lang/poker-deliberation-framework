"""Strict P2-027B cleanup contracts.

The values in this module are immutable control metadata.  They never contain
payload bytes, credentials, exception text, or unrestricted absolute paths.
"""

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

CLEANUP_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
CLEANUP_CANONICALIZATION: Final[Literal["poker-local-data-cleanup-json-v1"]] = (
    "poker-local-data-cleanup-json-v1"
)
CLEANUP_HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"
CLEANUP_POLICY_ID: Final[Literal["p2-027b-local-data-cleanup-v1"]] = "p2-027b-local-data-cleanup-v1"
CLEANUP_EXECUTOR_ID: Final[Literal["poker-deliberation-local-data-cleanup"]] = (
    "poker-deliberation-local-data-cleanup"
)
CLEANUP_EXECUTOR_VERSION: Final[Literal["1.0.0"]] = "1.0.0"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$")
_ROOT_ID = re.compile(r"^cleanup-root-[0-9a-f]{32}$")
_TRANSACTION_ID = re.compile(r"^cleanup-txn-[0-9a-f]{32}$")
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_SECRET_SHAPE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|"
    r"(?:api[_-]?key|password|passwd|secret|token)\s*[:=])",
    re.IGNORECASE,
)


def _safe_control(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("cleanup control metadata must be NFC")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("cleanup control metadata cannot contain control characters")
    if _SECRET_SHAPE.search(value):
        raise ValueError("cleanup control metadata must not contain a secret shape")
    return value


def _portable_id(value: str) -> str:
    _safe_control(value)
    if not _PORTABLE_ID.fullmatch(value) or value.endswith((".", " ")):
        raise ValueError("cleanup identifier must be one portable segment")
    if value.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED:
        raise ValueError("cleanup identifier uses a reserved device stem")
    return value


def _relative_path(value: str) -> str:
    _safe_control(value)
    if (
        not value
        or len(value) > 384
        or "\\" in value
        or value.startswith("/")
        or value.endswith("/")
        or ":" in value
        or "://" in value
    ):
        raise ValueError("cleanup relative path is not portable")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or _portable_id(part) != part for part in parts):
        raise ValueError("cleanup relative path contains an invalid segment")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


Sha256 = Annotated[str, Field(pattern=_SHA256.pattern)]
PortableId = Annotated[str, AfterValidator(_portable_id)]
Version = Annotated[str, Field(pattern=_VERSION.pattern), AfterValidator(_safe_control)]
RootId = Annotated[str, Field(pattern=_ROOT_ID.pattern)]
TransactionId = Annotated[str, Field(pattern=_TRANSACTION_ID.pattern)]
RelativePath = Annotated[str, AfterValidator(_relative_path)]


class _CleanupModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class CleanupActionKind(StrEnum):
    QUARANTINE_PRODUCT_RUN = "quarantine_product_run"
    DELETE_QUARANTINE_PAYLOAD = "delete_quarantine_payload"


class CleanupState(StrEnum):
    QUARANTINED = "quarantined"
    DELETE_PREPARED = "delete_prepared"
    DELETED = "deleted"


class CleanupFailureCode(StrEnum):
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    INVALID_PLAN = "invalid_plan"
    PLAN_EXPIRED = "plan_expired"
    POLICY_MISMATCH = "policy_mismatch"
    CANDIDATE_INELIGIBLE = "candidate_ineligible"
    OWNERSHIP_UNVERIFIED = "ownership_unverified"
    ACTIVE_OR_PENDING = "active_or_pending"
    LEGAL_HOLD = "legal_hold"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_MISMATCH = "approval_mismatch"
    ACTOR_SPOOF = "actor_spoof"
    UNAUTHORIZED_EXECUTION = "unauthorized_execution"
    AUTHORITY_REVOKED = "authority_revoked"
    STALE_SOURCE = "stale_source"
    STALE_CLEANUP_REVISION = "stale_cleanup_revision"
    RUN_LOCKED = "run_locked"
    LOCK_UNAVAILABLE = "lock_unavailable"
    PATH_CONFINEMENT_FAILED = "path_confinement_failed"
    LINK_OR_REPARSE_DETECTED = "link_or_reparse_detected"
    HARDLINK_DETECTED = "hardlink_detected"
    ALIAS_CONFLICT = "alias_conflict"
    CROSS_VOLUME = "cross_volume"
    CAPACITY_EXCEEDED = "capacity_exceeded"
    AUDIT_CAPACITY_EXCEEDED = "audit_capacity_exceeded"
    CANCELLED = "cancelled"
    DURABILITY_UNCONFIRMED = "durability_unconfirmed"
    EFFECT_UNKNOWN = "effect_unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INTERNAL_INVARIANT_ERROR = "internal_invariant_error"


_FAILURE_MESSAGES: Final[dict[CleanupFailureCode, str]] = {
    code: code.value.replace("_", " ") for code in CleanupFailureCode
}


class CleanupLimitsV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    maximum_actions: Literal[1] = 1
    maximum_tree_entries: int = Field(default=10_000, ge=1, le=10_000)
    maximum_target_bytes: int = Field(default=100_000_000, ge=1, le=100_000_000)
    maximum_control_artifact_bytes: int = Field(default=1_000_000, ge=1, le=1_000_000)
    maximum_control_bytes_per_run: int = Field(
        default=10_000_000,
        ge=1,
        le=10_000_000,
    )
    maximum_plan_lifetime_seconds: int = Field(default=86_400, ge=1, le=86_400)

    @model_validator(mode="after")
    def artifact_limit_fits_run_limit(self) -> CleanupLimitsV1:
        if self.maximum_control_artifact_bytes > self.maximum_control_bytes_per_run:
            raise ValueError("control artifact limit exceeds per-run control limit")
        return self


DEFAULT_CLEANUP_LIMITS = CleanupLimitsV1()


class CleanupFailureV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    code: CleanupFailureCode
    message: str = Field(min_length=1, max_length=64)
    retryable: bool
    automatic_retry_allowed: Literal[False] = False
    run_id_sha256: Sha256 | None = None
    plan_sha256: Sha256 | None = None
    transaction_id: TransactionId | None = None
    filesystem_effect: Literal[
        "none",
        "journal_only",
        "source_moved",
        "delete_staging_moved",
        "partial_delete",
        "control_published",
    ] = "none"
    domain_effect: Literal[
        "none",
        "current_unchanged",
        "current_may_have_advanced",
        "current_advanced",
    ] = "none"
    reconciliation_required: bool = False

    @model_validator(mode="after")
    def closed_failure_matrix(self) -> CleanupFailureV1:
        if self.message != _FAILURE_MESSAGES[self.code]:
            raise ValueError("cleanup failure message is not the fixed value")
        if self.retryable != (self.code is CleanupFailureCode.RUN_LOCKED):
            raise ValueError("only run_locked may be semantically retryable")
        uncertain = self.code in {
            CleanupFailureCode.DURABILITY_UNCONFIRMED,
            CleanupFailureCode.EFFECT_UNKNOWN,
            CleanupFailureCode.RECONCILIATION_REQUIRED,
        }
        if uncertain != self.reconciliation_required:
            raise ValueError("cleanup reconciliation flag does not match failure code")
        if self.filesystem_effect != "none" and not self.reconciliation_required:
            raise ValueError("nonzero failed filesystem effect requires reconciliation")
        return self


class CleanupRootMarkerV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    cleanup_protocol: Literal["poker-local-data-cleanup-v1"] = "poker-local-data-cleanup-v1"
    canonicalization: Literal["poker-local-data-cleanup-json-v1"] = CLEANUP_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = CLEANUP_HASH_ALGORITHM
    root_id: RootId
    cleanup_root_identity_sha256: Sha256
    product_root_identity_sha256: Sha256
    product_ownership_marker_sha256: Sha256
    producer_id: Literal["poker-deliberation-local-data-cleanup"] = CLEANUP_EXECUTOR_ID
    producer_version: Literal["1.0.0"] = CLEANUP_EXECUTOR_VERSION
    limits: CleanupLimitsV1 = DEFAULT_CLEANUP_LIMITS
    initialized_at: datetime

    _initialized_utc = field_validator("initialized_at")(
        lambda value: _utc(value, "initialized_at")
    )


class TreeInventoryEntryV1(_CleanupModel):
    relative_path: RelativePath
    entry_kind: Literal["directory", "file"]
    size_bytes: int = Field(ge=0, le=100_000_000)
    content_sha256: Sha256 | None = None
    identity_sha256: Sha256

    @model_validator(mode="after")
    def closed_entry_matrix(self) -> TreeInventoryEntryV1:
        if (self.entry_kind == "file") != (self.content_sha256 is not None):
            raise ValueError("only file entries have content hashes")
        if self.entry_kind == "directory" and self.size_bytes != 0:
            raise ValueError("directory entries have zero payload bytes")
        return self


class TreeInventoryV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    run_id_sha256: Sha256
    entries: tuple[TreeInventoryEntryV1, ...] = Field(min_length=1, max_length=10_000)
    entry_count: int = Field(ge=1, le=10_000)
    total_bytes: int = Field(ge=0, le=100_000_000)

    @model_validator(mode="after")
    def canonical_tree(self) -> TreeInventoryV1:
        paths = tuple(entry.relative_path for entry in self.entries)
        if paths != tuple(sorted(set(paths), key=lambda item: item.encode("utf-8"))):
            raise ValueError("tree entries must be UTF-8 path ordered and unique")
        if self.entry_count != len(self.entries):
            raise ValueError("tree entry count mismatch")
        if self.total_bytes != sum(entry.size_bytes for entry in self.entries):
            raise ValueError("tree byte count mismatch")
        return self


class ProductRunSourceV1(_CleanupModel):
    run_id: PortableId
    run_id_sha256: Sha256
    product_root_identity_sha256: Sha256
    product_ownership_marker_sha256: Sha256
    current_revision: int = Field(ge=1)
    current_transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{32}$")
    current_pointer_sha256: Sha256
    manifest_sha256: Sha256
    inventory_sha256: Sha256
    completion_marker_sha256: Sha256
    terminal_status: Literal["succeeded", "failed", "cancelled", "cancel_unconfirmed"]
    terminal_published_at: datetime

    _published_utc = field_validator("terminal_published_at")(
        lambda value: _utc(value, "terminal_published_at")
    )


class QuarantineSourceV1(_CleanupModel):
    run_id: PortableId
    run_id_sha256: Sha256
    cleanup_root_identity_sha256: Sha256
    cleanup_revision: int = Field(ge=1)
    cleanup_pointer_sha256: Sha256
    tombstone_sha256: Sha256
    quarantine_tree_sha256: Sha256
    quarantine_entered_at: datetime
    delete_eligible_at: datetime

    @field_validator("quarantine_entered_at", "delete_eligible_at")
    @classmethod
    def cleanup_utc(cls, value: datetime) -> datetime:
        return _utc(value, "quarantine timestamp")

    @model_validator(mode="after")
    def review_window_is_positive(self) -> QuarantineSourceV1:
        if self.delete_eligible_at <= self.quarantine_entered_at:
            raise ValueError("delete eligibility must follow quarantine entry")
        return self


class LifecycleEligibilityV1(_CleanupModel):
    local_data_policy_id: Literal["p2-027a-local-data-policy-v1"]
    local_data_policy_sha256: Sha256
    lifecycle_audit_sha256: Sha256
    audited_subject_count: int = Field(ge=1, le=10_000)
    delete_candidate_count: int = Field(ge=0, le=10_000)
    latest_retention_expires_at: datetime
    evaluated_at: datetime

    @field_validator("latest_retention_expires_at", "evaluated_at")
    @classmethod
    def lifecycle_utc(cls, value: datetime) -> datetime:
        return _utc(value, "lifecycle timestamp")

    @property
    def all_delete_candidates(self) -> bool:
        return self.audited_subject_count == self.delete_candidate_count


class ApprovalRetentionEvidenceV1(_CleanupModel):
    approval_ledger_sha256: Sha256
    v1_pending_count: int = Field(ge=0, le=1024)
    v2_pending_count: int = Field(ge=0, le=1024)
    failure_audit_head_sha256: Sha256 | None = None
    failure_audit_retention_expires_at: datetime | None = None
    evaluated_at: datetime

    @field_validator("failure_audit_retention_expires_at", "evaluated_at")
    @classmethod
    def approval_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value, "approval retention timestamp")

    @model_validator(mode="after")
    def audit_retention_binding(self) -> ApprovalRetentionEvidenceV1:
        if (self.failure_audit_head_sha256 is None) != (
            self.failure_audit_retention_expires_at is None
        ):
            raise ValueError("failure audit head and retention expiry must be paired")
        return self


class LegalHoldSnapshotV1(_CleanupModel):
    provider_id: PortableId
    provider_version: Version
    run_id_sha256: Sha256
    legal_hold: bool
    snapshot_reference_sha256: Sha256
    resolved_at: datetime

    _resolved_utc = field_validator("resolved_at")(lambda value: _utc(value, "resolved_at"))


class CleanupActionV1(_CleanupModel):
    action_kind: CleanupActionKind
    source_relative_path: RelativePath
    destination_relative_path: RelativePath

    @model_validator(mode="after")
    def exact_action_paths(self) -> CleanupActionV1:
        if self.action_kind is CleanupActionKind.QUARANTINE_PRODUCT_RUN:
            if not self.source_relative_path.startswith("runs/"):
                raise ValueError("quarantine source must be a product run namespace")
            if not self.destination_relative_path.startswith("quarantine/"):
                raise ValueError("quarantine destination must use cleanup quarantine")
        elif not self.source_relative_path.startswith(
            "quarantine/"
        ) or not self.destination_relative_path.startswith("deleting/"):
            raise ValueError("delete transition must move quarantine to deleting staging")
        return self


CleanupSource: TypeAlias = ProductRunSourceV1 | QuarantineSourceV1


class CleanupPlanV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    cleanup_policy_id: Literal["p2-027b-local-data-cleanup-v1"] = CLEANUP_POLICY_ID
    canonicalization: Literal["poker-local-data-cleanup-json-v1"] = CLEANUP_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = CLEANUP_HASH_ALGORITHM
    executor_id: Literal["poker-deliberation-local-data-cleanup"] = CLEANUP_EXECUTOR_ID
    executor_version: Literal["1.0.0"] = CLEANUP_EXECUTOR_VERSION
    executor_sha256: Sha256
    cleanup_root_id: RootId
    cleanup_root_marker_sha256: Sha256
    source: CleanupSource
    tree_inventory_sha256: Sha256
    lifecycle: LifecycleEligibilityV1
    approval_retention: ApprovalRetentionEvidenceV1
    legal_hold: LegalHoldSnapshotV1
    expected_cleanup_revision: int = Field(ge=0)
    expected_cleanup_pointer_sha256: Sha256 | None = None
    actions: tuple[CleanupActionV1, ...] = Field(min_length=1, max_length=1)
    limits: CleanupLimitsV1 = DEFAULT_CLEANUP_LIMITS
    generated_at: datetime
    expires_at: datetime
    execution_id: PortableId
    idempotency_key: PortableId

    @field_validator("generated_at", "expires_at")
    @classmethod
    def plan_utc(cls, value: datetime) -> datetime:
        return _utc(value, "cleanup plan timestamp")

    @model_validator(mode="after")
    def closed_plan_matrix(self) -> CleanupPlanV1:
        action = self.actions[0]
        if isinstance(self.source, ProductRunSourceV1):
            if action.action_kind is not CleanupActionKind.QUARANTINE_PRODUCT_RUN:
                raise ValueError("product source requires quarantine action")
            if self.source.run_id_sha256 != self.legal_hold.run_id_sha256:
                raise ValueError("product plan run identity mismatch")
        else:
            if action.action_kind is not CleanupActionKind.DELETE_QUARANTINE_PAYLOAD:
                raise ValueError("quarantine source requires delete action")
            if self.source.run_id_sha256 != self.legal_hold.run_id_sha256:
                raise ValueError("delete plan run identity mismatch")
        if self.expires_at <= self.generated_at:
            raise ValueError("cleanup plan expiry must follow generation")
        lifetime = (self.expires_at - self.generated_at).total_seconds()
        if lifetime > self.limits.maximum_plan_lifetime_seconds:
            raise ValueError("cleanup plan exceeds approved lifetime")
        if (self.expected_cleanup_revision == 0) != (self.expected_cleanup_pointer_sha256 is None):
            raise ValueError("cleanup pointer expectation does not match revision")
        return self


class CleanupCandidateEvidenceV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    cleanup_root_id: RootId
    cleanup_root_marker_sha256: Sha256
    executor_sha256: Sha256
    source: CleanupSource
    tree_inventory: TreeInventoryV1
    lifecycle: LifecycleEligibilityV1
    approval_retention: ApprovalRetentionEvidenceV1
    legal_hold: LegalHoldSnapshotV1
    expected_cleanup_revision: int = Field(ge=0)
    expected_cleanup_pointer_sha256: Sha256 | None = None
    product_active: bool = False
    ownership_verified: bool
    path_confinement_verified: bool
    integrity_verified: bool
    lineage_verified: bool
    cleanup_capacity_reserved: bool
    generated_at: datetime
    expires_at: datetime
    execution_id: PortableId
    idempotency_key: PortableId
    limits: CleanupLimitsV1 = DEFAULT_CLEANUP_LIMITS

    @field_validator("generated_at", "expires_at")
    @classmethod
    def evidence_utc(cls, value: datetime) -> datetime:
        return _utc(value, "cleanup evidence timestamp")

    @model_validator(mode="after")
    def evidence_identity(self) -> CleanupCandidateEvidenceV1:
        if self.tree_inventory.run_id_sha256 != self.source.run_id_sha256:
            raise ValueError("tree and source run identities differ")
        if self.legal_hold.run_id_sha256 != self.source.run_id_sha256:
            raise ValueError("hold and source run identities differ")
        if self.tree_inventory.entry_count > self.limits.maximum_tree_entries:
            raise ValueError("tree exceeds entry limit")
        if self.tree_inventory.total_bytes > self.limits.maximum_target_bytes:
            raise ValueError("tree exceeds target byte limit")
        if (self.expected_cleanup_revision == 0) != (self.expected_cleanup_pointer_sha256 is None):
            raise ValueError("cleanup pointer expectation does not match revision")
        return self


class CleanupDryRunResultV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    outcome_kind: Literal["eligible", "ineligible"]
    run_id_sha256: Sha256
    plan: CleanupPlanV1 | None = None
    plan_sha256: Sha256 | None = None
    failure: CleanupFailureV1 | None = None
    filesystem_mutation: Literal[False] = False
    domain_mutation: Literal[False] = False

    @model_validator(mode="after")
    def closed_result_matrix(self) -> CleanupDryRunResultV1:
        eligible = self.outcome_kind == "eligible"
        if eligible != (self.plan is not None and self.plan_sha256 is not None):
            raise ValueError("eligible dry-run requires plan and digest")
        if eligible == (self.failure is not None):
            raise ValueError("dry-run result must have exactly one plan or failure")
        return self


class CleanupApprovalBindingV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    approval_run_id_sha256: Sha256
    approval_run_revision: int = Field(ge=1)
    approval_pointer_sha256: Sha256
    approval_ledger_sha256: Sha256
    request_id: PortableId
    request_revision: int = Field(ge=1)
    action_digest_sha256: Sha256
    decision_id: PortableId
    decision_record_sha256: Sha256
    decision_outcome_sha256: Sha256
    actor_sha256: Sha256
    authority_snapshot_sha256: Sha256
    authority_provider_id: PortableId
    authority_provider_version: Version


class CleanupDurabilityEvidenceV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    platform_adapter: Literal["windows_msvcrt", "posix_fcntl"]
    journal_file_sync: Literal["not_attempted", "confirmed", "failed"]
    effect_rename: Literal["not_attempted", "confirmed", "attempted_unconfirmed"]
    control_file_sync: Literal["not_attempted", "confirmed", "failed"]
    directory_sync: Literal["not_attempted", "confirmed", "unavailable", "failed"]
    pointer_replace: Literal["not_attempted", "confirmed", "attempted_unconfirmed"]
    reconciliation: Literal["confirmed", "required"]


class CleanupReceiptV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    cleanup_policy_id: Literal["p2-027b-local-data-cleanup-v1"] = CLEANUP_POLICY_ID
    action_kind: CleanupActionKind
    run_id_sha256: Sha256
    transaction_id: TransactionId
    plan_sha256: Sha256
    approval_binding_sha256: Sha256
    source_tree_sha256: Sha256
    result_state: CleanupState
    effect_started_at: datetime
    committed_at: datetime
    durability: CleanupDurabilityEvidenceV1

    @field_validator("effect_started_at", "committed_at")
    @classmethod
    def receipt_utc(cls, value: datetime) -> datetime:
        return _utc(value, "cleanup receipt timestamp")


class CleanupTombstoneV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    run_id_sha256: Sha256
    source_pointer_sha256: Sha256
    source_manifest_sha256: Sha256
    quarantine_tree_sha256: Sha256
    state: CleanupState
    receipt_sha256: Sha256
    quarantine_entered_at: datetime
    receipt_retain_until: datetime

    @field_validator("quarantine_entered_at", "receipt_retain_until")
    @classmethod
    def tombstone_utc(cls, value: datetime) -> datetime:
        return _utc(value, "cleanup tombstone timestamp")

    @model_validator(mode="after")
    def retention_follows_entry(self) -> CleanupTombstoneV1:
        if self.receipt_retain_until <= self.quarantine_entered_at:
            raise ValueError("cleanup receipt retention must follow quarantine entry")
        return self


class CleanupExecutionResultV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    outcome_kind: Literal["committed", "failed"]
    run_id_sha256: Sha256
    execution_id: PortableId
    idempotency_key: PortableId
    transaction_id: TransactionId
    plan_sha256: Sha256
    cleanup_revision: int = Field(ge=0)
    cleanup_pointer_sha256: Sha256 | None = None
    receipt: CleanupReceiptV1 | None = None
    receipt_sha256: Sha256 | None = None
    tombstone: CleanupTombstoneV1 | None = None
    tombstone_sha256: Sha256 | None = None
    failure: CleanupFailureV1 | None = None

    @model_validator(mode="after")
    def closed_execution_matrix(self) -> CleanupExecutionResultV1:
        committed = self.outcome_kind == "committed"
        committed_values = (
            self.cleanup_pointer_sha256,
            self.receipt,
            self.receipt_sha256,
            self.tombstone,
            self.tombstone_sha256,
        )
        if committed:
            if self.cleanup_revision < 1 or any(value is None for value in committed_values):
                raise ValueError("committed cleanup result lacks durable evidence")
            if self.failure is not None:
                raise ValueError("committed cleanup result cannot contain a failure")
        elif self.failure is None:
            raise ValueError("failed cleanup result requires a failure")
        return self


class CleanupReconciliationReportV1(_CleanupModel):
    schema_version: Literal["1.0.0"] = CLEANUP_SCHEMA_VERSION
    run_id_sha256: Sha256
    transaction_id: TransactionId
    plan_sha256: Sha256
    observed_source: Literal["absent", "exact", "mismatch", "unreadable"]
    observed_destination: Literal["absent", "exact", "mismatch", "unreadable"]
    observed_staging: Literal["absent", "exact", "partial", "unreadable"]
    observed_current: Literal["absent", "prior", "committed", "mismatch", "unreadable"]
    observed_receipt: Literal["absent", "exact", "mismatch", "unreadable"]
    observed_tombstone: Literal["absent", "exact", "mismatch", "unreadable"]
    classification: Literal[
        "no_effect",
        "committed",
        "reconciliation_required",
        "effect_unknown",
    ]
    automatic_retry_allowed: Literal[False] = False


def cleanup_failure(
    code: CleanupFailureCode,
    *,
    run_id_sha256: str | None = None,
    plan_sha256: str | None = None,
    transaction_id: str | None = None,
    filesystem_effect: Literal[
        "none",
        "journal_only",
        "source_moved",
        "delete_staging_moved",
        "partial_delete",
        "control_published",
    ] = "none",
    domain_effect: Literal[
        "none",
        "current_unchanged",
        "current_may_have_advanced",
        "current_advanced",
    ] = "none",
) -> CleanupFailureV1:
    """Build a fixed, redacted cleanup failure."""

    return CleanupFailureV1(
        code=code,
        message=_FAILURE_MESSAGES[code],
        retryable=code is CleanupFailureCode.RUN_LOCKED,
        run_id_sha256=run_id_sha256,
        plan_sha256=plan_sha256,
        transaction_id=transaction_id,
        filesystem_effect=filesystem_effect,
        domain_effect=domain_effect,
        reconciliation_required=code
        in {
            CleanupFailureCode.DURABILITY_UNCONFIRMED,
            CleanupFailureCode.EFFECT_UNKNOWN,
            CleanupFailureCode.RECONCILIATION_REQUIRED,
        },
    )


__all__ = [
    "CLEANUP_CANONICALIZATION",
    "CLEANUP_EXECUTOR_ID",
    "CLEANUP_EXECUTOR_VERSION",
    "CLEANUP_HASH_ALGORITHM",
    "CLEANUP_POLICY_ID",
    "CLEANUP_SCHEMA_VERSION",
    "DEFAULT_CLEANUP_LIMITS",
    "ApprovalRetentionEvidenceV1",
    "CleanupActionKind",
    "CleanupActionV1",
    "CleanupApprovalBindingV1",
    "CleanupCandidateEvidenceV1",
    "CleanupDryRunResultV1",
    "CleanupDurabilityEvidenceV1",
    "CleanupExecutionResultV1",
    "CleanupFailureCode",
    "CleanupFailureV1",
    "CleanupLimitsV1",
    "CleanupPlanV1",
    "CleanupReceiptV1",
    "CleanupReconciliationReportV1",
    "CleanupRootMarkerV1",
    "CleanupState",
    "CleanupTombstoneV1",
    "LegalHoldSnapshotV1",
    "LifecycleEligibilityV1",
    "ProductRunSourceV1",
    "QuarantineSourceV1",
    "TreeInventoryEntryV1",
    "TreeInventoryV1",
    "cleanup_failure",
]
