"""Strict P2-012B contracts for verified product run revisions."""

from __future__ import annotations

import hashlib
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

from poker_deliberation.storage.revision_models import (
    DurabilityEvidenceV1,
    PayloadInventoryEntryV1,
)

TERMINAL_SCHEMA_VERSION: Final[Literal["2.0.0"]] = "2.0.0"
TERMINAL_STORAGE_PROTOCOL: Final[Literal["poker-run-terminal-v2"]] = "poker-run-terminal-v2"
TERMINAL_CANONICALIZATION: Final[Literal["poker-run-storage-json-v1"]] = "poker-run-storage-json-v1"
TERMINAL_HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$")
_TRANSACTION_ID = re.compile(r"^txn-[0-9a-f]{32}$")
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_STAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SECRET_SHAPE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|"
    r"(?:api[_-]?key|password|passwd|secret|token)\s*[:=])",
    re.IGNORECASE,
)


def _safe_control(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("terminal control metadata must be NFC")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("terminal control metadata cannot contain control characters")
    if _SECRET_SHAPE.search(value):
        raise ValueError("terminal control metadata must not contain a secret shape")
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
    AfterValidator(_safe_control),
]
Version = Annotated[
    str,
    Field(pattern=_VERSION.pattern),
    AfterValidator(_safe_control),
]
TransactionId = Annotated[str, Field(pattern=_TRANSACTION_ID.pattern)]
CorrelationId = Annotated[
    str,
    Field(pattern=_CORRELATION_ID.pattern),
    AfterValidator(_safe_control),
]


class _TerminalModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class RunReadStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    APPROVAL_REQUIRED = "approval_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CANCEL_UNCONFIRMED = "cancel_unconfirmed"
    INCOMPLETE = "incomplete"
    CORRUPT = "corrupt"
    UNSUPPORTED_VERSION = "unsupported_version"
    LEGACY_UNVERIFIED = "legacy_unverified"


PublicationKind: TypeAlias = Literal[
    "product_checkpoint",
    "product_terminal",
    "legacy_copy",
]
ManifestStatus: TypeAlias = Literal[
    "in_progress",
    "approval_required",
    "succeeded",
    "failed",
    "cancelled",
    "cancel_unconfirmed",
    "legacy_unverified",
]
TerminalStatus: TypeAlias = Literal[
    "succeeded",
    "failed",
    "cancelled",
    "cancel_unconfirmed",
]


class ToolContractVersionV2(_TerminalModel):
    tool_name: PortableId
    tool_version: Version
    contract_version: Version


class LegacySourceBindingV2(_TerminalModel):
    adapter_version: Version
    source_root_identity_sha256: Sha256
    source_run_id_sha256: Sha256
    source_inventory_sha256: Sha256
    source_quiescence_acknowledged: Literal[True]
    missing_guarantees: tuple[str, ...]

    @field_validator("missing_guarantees")
    @classmethod
    def canonical_missing_guarantees(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("legacy source must state its missing guarantees")
        if any(not item or len(item) > 128 or _safe_control(item) != item for item in value):
            raise ValueError("invalid legacy missing guarantee")
        expected = tuple(sorted(set(value), key=lambda item: item.encode("utf-8")))
        if value != expected:
            raise ValueError("legacy missing guarantees must be UTF-8 sorted and unique")
        return value


class BudgetSettlementBindingV2(_TerminalModel):
    budget_run_id_sha256: Sha256
    budget_policy_sha256: Sha256
    reservation_operation_id: CorrelationId
    reservation_request_sha256: Sha256
    permit_id: CorrelationId
    settlement_operation_id: CorrelationId
    settlement_id: CorrelationId


class RunManifestV2(_TerminalModel):
    run_schema_version: Literal["2.0.0"] = TERMINAL_SCHEMA_VERSION
    storage_protocol: Literal["poker-run-terminal-v2"] = TERMINAL_STORAGE_PROTOCOL
    canonicalization: Literal["poker-run-storage-json-v1"] = TERMINAL_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = TERMINAL_HASH_ALGORITHM
    publication_kind: PublicationKind
    run_id: PortableId
    revision: int = Field(ge=1)
    transaction_id: TransactionId
    previous_revision: int | None = Field(default=None, ge=1)
    previous_manifest_sha256: Sha256 | None = None
    expected_pointer_sha256: Sha256 | None = None
    created_at: datetime
    updated_at: datetime
    producer_id: PortableId
    producer_version: Version
    framework_version: Version
    source_commit_id: Sha256
    tool_contract_versions: tuple[ToolContractVersionV2, ...]
    status: ManifestStatus
    canonical_input_sha256: Sha256
    config_sha256: Sha256
    budget_policy_sha256: Sha256
    budget_binding: BudgetSettlementBindingV2
    redaction_policy_sha256: Sha256
    local_data_policy_sha256: Sha256
    state_checkpoint_sha256: Sha256
    event_head_sha256: Sha256
    approval_lineage_head_sha256: Sha256
    context_lineage_head_sha256: Sha256
    execution_lineage_head_sha256: Sha256
    legacy_source: LegacySourceBindingV2 | None = None
    inventory_sha256: Sha256
    lifecycle_audit_sha256: Sha256 | None = None
    artifacts: tuple[PayloadInventoryEntryV1, ...]

    _created_utc = field_validator("created_at")(lambda value: _utc(value, "created_at"))
    _updated_utc = field_validator("updated_at")(lambda value: _utc(value, "updated_at"))

    @field_validator("tool_contract_versions")
    @classmethod
    def canonical_tool_versions(
        cls, value: tuple[ToolContractVersionV2, ...]
    ) -> tuple[ToolContractVersionV2, ...]:
        names = tuple(item.tool_name for item in value)
        if len(names) != len(set(names)):
            raise ValueError("tool contract names must be unique")
        if names != tuple(sorted(names, key=lambda item: item.encode("utf-8"))):
            raise ValueError("tool contract versions must be UTF-8 tool-name ordered")
        return value

    @field_validator("artifacts")
    @classmethod
    def canonical_inventory(
        cls, value: tuple[PayloadInventoryEntryV1, ...]
    ) -> tuple[PayloadInventoryEntryV1, ...]:
        if not value:
            raise ValueError("terminal manifest requires payload artifacts")
        paths = tuple(item.revision_relative_path for item in value)
        if len(paths) != len(set(paths)):
            raise ValueError("terminal inventory paths must be unique")
        if paths != tuple(sorted(paths, key=lambda item: item.encode("utf-8"))):
            raise ValueError("terminal inventory must be UTF-8 path ordered")
        return value

    @model_validator(mode="after")
    def closed_manifest_matrix(self) -> RunManifestV2:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.budget_policy_sha256 != self.budget_binding.budget_policy_sha256:
            raise ValueError("manifest and budget binding policy hashes differ")
        if self.revision == 1:
            if any(
                value is not None
                for value in (
                    self.previous_revision,
                    self.previous_manifest_sha256,
                    self.expected_pointer_sha256,
                )
            ):
                raise ValueError("initial terminal revision requires null lineage")
        elif (
            self.previous_revision != self.revision - 1
            or self.previous_manifest_sha256 is None
            or self.expected_pointer_sha256 is None
        ):
            raise ValueError("successor terminal revision requires exact prior lineage")
        status = self.status
        if self.publication_kind == "product_checkpoint":
            valid = (
                status in {"in_progress", "approval_required"}
                and self.legacy_source is None
                and self.lifecycle_audit_sha256 is None
            )
        elif self.publication_kind == "product_terminal":
            valid = (
                status in {"succeeded", "failed", "cancelled", "cancel_unconfirmed"}
                and self.legacy_source is None
                and self.lifecycle_audit_sha256 is not None
            )
        else:
            valid = (
                status == "legacy_unverified"
                and self.legacy_source is not None
                and self.lifecycle_audit_sha256 is None
            )
        if not valid:
            raise ValueError("manifest publication/status matrix mismatch")
        return self


class CompletionMarkerV2(_TerminalModel):
    schema_version: Literal["2.0.0"] = TERMINAL_SCHEMA_VERSION
    storage_protocol: Literal["poker-run-terminal-v2"] = TERMINAL_STORAGE_PROTOCOL
    canonicalization: Literal["poker-run-storage-json-v1"] = TERMINAL_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = TERMINAL_HASH_ALGORITHM
    run_id: PortableId
    terminal_revision: int = Field(ge=1)
    terminal_transaction_id: TransactionId
    terminal_status: TerminalStatus
    terminal_manifest_sha256: Sha256
    required_inventory_sha256: Sha256
    budget_binding_sha256: Sha256
    lifecycle_audit_sha256: Sha256
    published_at: datetime

    _published_utc = field_validator("published_at")(lambda value: _utc(value, "published_at"))


class RunCurrentPointerV2(_TerminalModel):
    schema_version: Literal["2.0.0"] = TERMINAL_SCHEMA_VERSION
    storage_protocol: Literal["poker-run-terminal-v2"] = TERMINAL_STORAGE_PROTOCOL
    canonicalization: Literal["poker-run-storage-json-v1"] = TERMINAL_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = TERMINAL_HASH_ALGORITHM
    publication_kind: PublicationKind
    run_id: PortableId
    revision: int = Field(ge=1)
    transaction_id: TransactionId
    revision_relative_path: str = Field(min_length=1, max_length=192)
    status: ManifestStatus
    manifest_sha256: Sha256
    inventory_sha256: Sha256
    completion_marker_sha256: Sha256 | None = None
    published_at: datetime

    _published_utc = field_validator("published_at")(lambda value: _utc(value, "published_at"))

    @model_validator(mode="after")
    def closed_pointer_matrix(self) -> RunCurrentPointerV2:
        expected = f"revisions/r{self.revision}-{self.transaction_id}"
        if self.revision_relative_path != expected:
            raise ValueError("pointer revision_relative_path mismatch")
        status = self.status
        if self.publication_kind == "product_checkpoint":
            valid = (
                status in {"in_progress", "approval_required"}
                and self.completion_marker_sha256 is None
            )
        elif self.publication_kind == "product_terminal":
            valid = (
                status in {"succeeded", "failed", "cancelled", "cancel_unconfirmed"}
                and self.completion_marker_sha256 is not None
            )
        else:
            valid = status == "legacy_unverified" and self.completion_marker_sha256 is None
        if not valid:
            raise ValueError("pointer publication/status matrix mismatch")
        return self


class VerifiedPayloadV2(_TerminalModel):
    inventory: PayloadInventoryEntryV1
    exact_bytes: bytes

    @field_validator("exact_bytes")
    @classmethod
    def own_bytes(cls, value: bytes) -> bytes:
        return bytes(bytearray(value))

    @model_validator(mode="after")
    def exact_payload_identity(self) -> VerifiedPayloadV2:
        if len(self.exact_bytes) != self.inventory.size_bytes:
            raise ValueError("verified payload size mismatch")
        if hashlib.sha256(self.exact_bytes).hexdigest() != self.inventory.sha256:
            raise ValueError("verified payload hash mismatch")
        return self


class VerifiedRunReadV2(_TerminalModel):
    schema_version: Literal["2.0.0"] = TERMINAL_SCHEMA_VERSION
    read_status: RunReadStatus
    run_id: PortableId
    revision: int = Field(ge=1)
    transaction_id: TransactionId
    current_pointer_sha256: Sha256
    manifest_sha256: Sha256
    inventory_sha256: Sha256
    completion_marker_sha256: Sha256 | None = None
    resume_eligible: bool
    budget_settlement_verified: Literal[True] = True
    lifecycle_verified: bool
    reachable_revisions: tuple[int, ...]
    pointer: RunCurrentPointerV2
    manifest: RunManifestV2
    completion_marker: CompletionMarkerV2 | None = None
    payloads: tuple[VerifiedPayloadV2, ...]

    @model_validator(mode="after")
    def verified_projection_is_exact(self) -> VerifiedRunReadV2:
        if (
            self.read_status.value != self.manifest.status
            or self.pointer.status != self.manifest.status
            or self.pointer.publication_kind != self.manifest.publication_kind
            or self.run_id != self.pointer.run_id
            or self.run_id != self.manifest.run_id
            or self.revision != self.pointer.revision
            or self.revision != self.manifest.revision
            or self.transaction_id != self.pointer.transaction_id
            or self.transaction_id != self.manifest.transaction_id
            or self.manifest_sha256 != self.pointer.manifest_sha256
            or self.inventory_sha256 != self.pointer.inventory_sha256
            or self.inventory_sha256 != self.manifest.inventory_sha256
            or tuple(item.inventory for item in self.payloads) != self.manifest.artifacts
        ):
            raise ValueError("verified read identity mismatch")
        expected_revisions = tuple(range(self.revision, 0, -1))
        if self.reachable_revisions != expected_revisions:
            raise ValueError("verified history must be current-to-genesis")
        expected_resume = self.read_status in {
            RunReadStatus.IN_PROGRESS,
            RunReadStatus.APPROVAL_REQUIRED,
        }
        if self.resume_eligible != expected_resume:
            raise ValueError("verified read resume eligibility mismatch")
        terminal = self.pointer.publication_kind == "product_terminal"
        if terminal != (self.completion_marker is not None):
            raise ValueError("verified terminal marker presence mismatch")
        if terminal != (self.completion_marker_sha256 is not None):
            raise ValueError("verified terminal marker hash presence mismatch")
        if terminal != self.lifecycle_verified:
            raise ValueError("verified lifecycle state mismatch")
        if self.completion_marker is not None and (
            self.completion_marker.terminal_status != self.manifest.status
            or self.completion_marker.terminal_revision != self.revision
            or self.completion_marker.terminal_transaction_id != self.transaction_id
            or self.completion_marker.terminal_manifest_sha256 != self.manifest_sha256
            or self.completion_marker.lifecycle_audit_sha256 != self.manifest.lifecycle_audit_sha256
        ):
            raise ValueError("verified completion marker identity mismatch")
        return self

    def payload_bytes(self, logical_name: str) -> bytes:
        matches = tuple(
            item.exact_bytes
            for item in self.payloads
            if item.inventory.logical_name == logical_name
        )
        if len(matches) != 1:
            raise KeyError(logical_name)
        return matches[0]


class ProductRunFailureCode(StrEnum):
    RUN_NOT_FOUND = "run_not_found"
    RUN_LOCKED = "run_locked"
    LOCK_UNAVAILABLE = "lock_unavailable"
    RUN_CONFLICT = "run_conflict"
    RUN_INCOMPLETE = "run_incomplete"
    RUN_CORRUPT = "run_corrupt"
    UNSUPPORTED_RUN_VERSION = "unsupported_run_version"
    LEGACY_RUN_UNVERIFIED = "legacy_run_unverified"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
    ARTIFACT_SCHEMA_ERROR = "artifact_schema_error"
    PATH_CONFINEMENT_FAILED = "path_confinement_failed"
    LINK_OR_REPARSE_DETECTED = "link_or_reparse_detected"
    CROSS_RUN_MISMATCH = "cross_run_mismatch"
    HASH_ALGORITHM_MISMATCH = "hash_algorithm_mismatch"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    MIGRATION_SOURCE_CHANGED = "migration_source_changed"
    MIGRATION_SOURCE_BUSY = "migration_source_busy"
    MIGRATION_CONFLICT = "migration_conflict"
    BUDGET_RESERVATION_FAILED = "budget_reservation_failed"
    BUDGET_SETTLEMENT_FAILED = "budget_settlement_failed"
    DURABILITY_UNCONFIRMED = "durability_unconfirmed"
    EFFECT_UNKNOWN = "effect_unknown"
    LIFECYCLE_POLICY_FAILED = "lifecycle_policy_failed"
    INTERNAL_INVARIANT_ERROR = "internal_invariant_error"


class ProductRunFailureV2(_TerminalModel):
    schema_version: Literal["2.0.0"] = TERMINAL_SCHEMA_VERSION
    code: ProductRunFailureCode
    stage: Annotated[str, Field(pattern=_STAGE.pattern)]
    read_status: RunReadStatus | None = None
    message_code: str
    automatic_retry_allowed: Literal[False] = False
    retryable: bool
    reconciliation_required: bool
    filesystem_effect: Literal[
        "none",
        "control_only",
        "staging_orphan",
        "unreferenced_revision",
        "current_replace_attempted",
        "current_advanced",
    ]
    domain_effect: Literal[
        "not_started",
        "current_unchanged",
        "current_may_have_advanced",
        "current_advanced",
    ]
    previous_revision_effect: Literal["not_applicable", "unchanged", "unconfirmed"]
    run_id_sha256: Sha256
    transaction_id: TransactionId | None = None
    observed_revision: int | None = Field(default=None, ge=1)
    observed_pointer_sha256: Sha256 | None = None
    durability_evidence: DurabilityEvidenceV1 | None = None

    @model_validator(mode="after")
    def redacted_failure_is_closed(self) -> ProductRunFailureV2:
        if self.message_code != self.code.value:
            raise ValueError("failure message_code must equal the redacted code")
        if self.retryable != (self.code is ProductRunFailureCode.RUN_LOCKED):
            raise ValueError("only run_locked is semantically retryable")
        expected_status = {
            ProductRunFailureCode.RUN_INCOMPLETE: RunReadStatus.INCOMPLETE,
            ProductRunFailureCode.RUN_CORRUPT: RunReadStatus.CORRUPT,
            ProductRunFailureCode.UNSUPPORTED_RUN_VERSION: (RunReadStatus.UNSUPPORTED_VERSION),
            ProductRunFailureCode.LEGACY_RUN_UNVERIFIED: (RunReadStatus.LEGACY_UNVERIFIED),
        }.get(self.code)
        if expected_status is not None and self.read_status is not expected_status:
            raise ValueError("failure read_status mismatch")
        return self


class ProductRunError(ValueError):
    """One redacted typed terminal-run failure."""

    def __init__(self, failure: ProductRunFailureV2):
        self.failure = failure
        super().__init__(failure.code.value)


__all__ = [
    "TERMINAL_CANONICALIZATION",
    "TERMINAL_HASH_ALGORITHM",
    "TERMINAL_SCHEMA_VERSION",
    "TERMINAL_STORAGE_PROTOCOL",
    "BudgetSettlementBindingV2",
    "CompletionMarkerV2",
    "LegacySourceBindingV2",
    "ProductRunError",
    "ProductRunFailureCode",
    "ProductRunFailureV2",
    "RunCurrentPointerV2",
    "RunManifestV2",
    "RunReadStatus",
    "ToolContractVersionV2",
    "VerifiedPayloadV2",
    "VerifiedRunReadV2",
]
