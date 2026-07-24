"""Strict internal P2-012A contracts for immutable structural revisions.

These models deliberately are not exported from :mod:`poker_deliberation.storage`.
They describe structural, nonterminal storage only; no model in this module is a
completion marker or a product run-status contract.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import ClassificationEvidence, ClassificationSource

STORAGE_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
STORAGE_PROTOCOL: Final[Literal["poker-run-revision-v1"]] = "poker-run-revision-v1"
STORAGE_CANONICALIZATION: Final[Literal["poker-run-storage-json-v1"]] = "poker-run-storage-json-v1"
STORAGE_HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"
PUBLICATION_KIND: Final[Literal["structural_nonterminal"]] = "structural_nonterminal"
APPROVED_LOCAL_DATA_POLICY_ID: Final[Literal["p2-027a-local-data-policy-v1"]] = (
    "p2-027a-local-data-policy-v1"
)
APPROVED_LOCAL_DATA_POLICY_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
APPROVED_LOCAL_DATA_POLICY_SHA256: Final[
    Literal["508fe62ce72ae54b59a8d71e200309a2046bf807dcf249f0aebf787c910cab60"]
] = "508fe62ce72ae54b59a8d71e200309a2046bf807dcf249f0aebf787c910cab60"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_TRANSACTION_ID = re.compile(r"^txn-[0-9a-f]{32}$")
_OWNER_TOKEN = re.compile(r"^owner-[0-9a-f]{32}$")
_CLAIM_ID = re.compile(r"^claim-[0-9a-f]{32}$")
_ROOT_ID = re.compile(r"^root-[0-9a-f]{32}$")
_SOURCE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,191}$")
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


def validate_control_string(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("control metadata must be NFC")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("control metadata cannot contain control characters")
    if _SECRET_METADATA.search(value):
        raise ValueError("control metadata must not contain a secret shape")
    return value


Sha256 = Annotated[str, Field(pattern=_SHA256.pattern)]
PortableId = Annotated[
    str,
    Field(pattern=_PORTABLE_ID.pattern),
    AfterValidator(validate_control_string),
]
Version = Annotated[str, Field(pattern=_VERSION.pattern), AfterValidator(validate_control_string)]
TransactionId = Annotated[str, Field(pattern=_TRANSACTION_ID.pattern)]
OwnerToken = Annotated[str, Field(pattern=_OWNER_TOKEN.pattern)]
ClaimId = Annotated[str, Field(pattern=_CLAIM_ID.pattern)]
RootId = Annotated[str, Field(pattern=_ROOT_ID.pattern)]
SourceId = Annotated[
    str, Field(pattern=_SOURCE_ID.pattern), AfterValidator(validate_control_string)
]
PlatformAdapter = Literal["windows_msvcrt", "posix_fcntl"]
Serialization = Literal[
    "poker-run-storage-json-v1",
    "poker-run-storage-jsonl-v1",
    "poker-run-storage-utf8-text-v1",
    "opaque-bytes-v1",
]
OriginKind = Literal[
    "case_input",
    "normalization_output",
    "assumption_ledger",
    "evidence_ledger",
    "approval_ledger",
    "assignment_ledger",
    "agent_execution_ledger",
    "security_event_ledger",
    "dispute_ledger",
    "agent_report",
    "tool_input",
    "tool_result",
    "final_report_json",
    "final_report_markdown",
    "budget_state",
]


class _RevisionModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _require_utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _validate_revision_expectations(
    proposed_revision: int,
    expected_revision: int | None,
    expected_manifest_sha256: str | None,
    expected_pointer_sha256: str | None,
) -> None:
    expectations = (
        expected_revision,
        expected_manifest_sha256,
        expected_pointer_sha256,
    )
    if expected_revision is None:
        if proposed_revision != 1 or any(value is not None for value in expectations):
            raise ValueError("initial revision requires revision 1 and null expectations")
    elif (
        proposed_revision != expected_revision + 1
        or expected_manifest_sha256 is None
        or expected_pointer_sha256 is None
    ):
        raise ValueError("successor revision requires exact previous revision and hashes")


class ArtifactIntentSnapshotV1(_RevisionModel):
    kind: Literal[
        "agent_execution_records",
        "security_events",
        "state",
        "approvals",
        "disputes",
        "final_report_json",
        "final_report_markdown",
    ]
    relative_path: str = Field(min_length=1, max_length=256)
    media_type: Literal["application/json", "text/markdown"]
    content_sha256: Sha256 | None = None

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        validate_control_string(value)
        if (
            "\\" in value
            or ":" in value
            or value.startswith("/")
            or "://" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("artifact intent path must be portable and relative")
        return value


class ApprovalDecisionBindingV1(_RevisionModel):
    kind: Literal["approval_decision"] = "approval_decision"
    approval_id: PortableId
    decision: Literal["approved", "rejected"]
    decided_at: datetime
    decision_reason_sha256: Sha256
    external_source_id: SourceId

    _utc_decided_at = field_validator("decided_at")(lambda value: _require_utc(value, "decided_at"))


class ContextBindingV1(_RevisionModel):
    kind: Literal["context"] = "context"
    context_sha256: Sha256
    context_id: PortableId
    attempt_id: PortableId
    parent_context_id: PortableId | None = None
    schema_version: Version
    classification: ContextClassification
    payload_sha256: Sha256
    source_sha256: Sha256
    policy_sha256: Sha256
    envelope_sha256: Sha256
    expires_at: datetime
    producer_runtime: PortableId
    consumer_runtime: PortableId

    _utc_expires_at = field_validator("expires_at")(lambda value: _require_utc(value, "expires_at"))


class PhaseBindingV1(_RevisionModel):
    kind: Literal["phase"] = "phase"
    run_id: PortableId
    phase_id: PortableId
    phase_schema_version: Version
    attempt_id: PortableId
    context_ids: tuple[PortableId, ...] = ()
    input_hash: Sha256
    policy_snapshot_hash: Sha256
    output_hash: Sha256 | None = None
    artifact_intents: tuple[ArtifactIntentSnapshotV1, ...] = ()

    @field_validator("context_ids")
    @classmethod
    def unique_context_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("context_ids must be ordered and unique")
        return value


class BudgetPolicyBindingV1(_RevisionModel):
    kind: Literal["budget_policy"] = "budget_policy"
    policy_schema_version: Literal["2.0.0"]
    policy_sha256: Sha256


class ToolBindingV1(_RevisionModel):
    kind: Literal["tool"] = "tool"
    run_id: PortableId
    phase_attempt_id: PortableId
    ordinal: int = Field(ge=0)
    request_id: PortableId
    request_tool_name: PortableId
    requested_by: PortableId
    requires_approval: bool
    requested_contract_version: Version | None = None
    tool_request_sha256: Sha256
    request_input_artifact_sha256: Sha256
    result_id: PortableId
    result_tool_name: PortableId
    result_artifact_sha256: Sha256
    request_input_sha256: Sha256
    validated_result_input_sha256: Sha256
    materialized_result_input_sha256: Sha256
    supported_contract_version: Version
    result_contract_version: Version


class LocalDataBindingV1(_RevisionModel):
    kind: Literal["local_data"] = "local_data"
    policy_id: Literal["p2-027a-local-data-policy-v1"] = APPROVED_LOCAL_DATA_POLICY_ID
    policy_version: Literal["1.0.0"] = APPROVED_LOCAL_DATA_POLICY_VERSION
    policy_sha256: Literal["508fe62ce72ae54b59a8d71e200309a2046bf807dcf249f0aebf787c910cab60"] = (
        APPROVED_LOCAL_DATA_POLICY_SHA256
    )
    logical_name: str = Field(min_length=1, max_length=256)
    classification: ContextClassification
    classification_source: ClassificationSource
    classification_evidence: ClassificationEvidence
    classification_evidence_sha256: Sha256


class SourceBindingV1(_RevisionModel):
    kind: Literal["source"] = "source"
    source_id: SourceId
    source_kind: Literal["user_input", "payload_artifact", "external_evidence"]
    trust_kind: Literal[
        "trusted_user_input",
        "verified_payload",
        "declared_external_evidence",
    ]
    source_logical_name: str | None = Field(default=None, max_length=256)
    source_schema_version: Version | None = None
    consumer_record_id: PortableId | None = None
    source_sha256: Sha256

    @model_validator(mode="after")
    def source_kind_matches_trust(self) -> SourceBindingV1:
        expected = {
            "user_input": "trusted_user_input",
            "payload_artifact": "verified_payload",
            "external_evidence": "declared_external_evidence",
        }
        if self.trust_kind != expected[self.source_kind]:
            raise ValueError("source kind and trust kind mismatch")
        if self.source_kind == "payload_artifact":
            if self.source_logical_name is None or self.source_schema_version is None:
                raise ValueError("payload source requires logical name and schema version")
            if self.consumer_record_id is not None:
                raise ValueError("payload source cannot identify a consumer record")
        elif self.source_logical_name is not None or self.source_schema_version is not None:
            raise ValueError("non-payload source cannot carry logical name or schema version")
        return self


class ReportBindingV1(_RevisionModel):
    kind: Literal["report"] = "report"
    report_id: PortableId
    report_schema_version: Version
    report_json_sha256: Sha256
    rendered_markdown_sha256: Sha256
    upstream_source_sha256: Sha256


ProvenanceBindingV1: TypeAlias = Annotated[
    ApprovalDecisionBindingV1
    | ContextBindingV1
    | PhaseBindingV1
    | BudgetPolicyBindingV1
    | ToolBindingV1
    | LocalDataBindingV1
    | SourceBindingV1
    | ReportBindingV1,
    Field(discriminator="kind"),
]


class ProvenanceHeadV1(_RevisionModel):
    kind: Literal[
        "approval_decision",
        "context",
        "phase",
        "budget_policy",
        "tool",
        "local_data",
        "source",
        "report",
    ]
    binding_count: int = Field(gt=0)
    bindings_sha256: Sha256


class PayloadInventoryEntryV1(_RevisionModel):
    logical_name: str = Field(min_length=1, max_length=256)
    revision_relative_path: str = Field(min_length=1, max_length=264)
    media_type: Literal["application/json", "application/x-ndjson", "text/markdown"]
    artifact_schema_version: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$",
    )
    serialization: Serialization
    size_bytes: int = Field(ge=0)
    sha256: Sha256
    required: bool
    classification: ContextClassification
    classification_source: ClassificationSource
    classification_evidence: ClassificationEvidence
    classification_evidence_sha256: Sha256
    source_sha256: Sha256
    provenance_bindings: tuple[ProvenanceBindingV1, ...]


class RevisionArtifactV1(_RevisionModel):
    logical_name: str = Field(min_length=1, max_length=256)
    media_type: Literal["application/json", "application/x-ndjson", "text/markdown"]
    artifact_schema_version: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$",
    )
    serialization: Serialization
    exact_bytes: bytes
    required: bool
    classification: ContextClassification
    classification_source: ClassificationSource
    classification_evidence: ClassificationEvidence
    policy_sha256: Literal["508fe62ce72ae54b59a8d71e200309a2046bf807dcf249f0aebf787c910cab60"]
    origin_kind: OriginKind
    provenance_bindings: tuple[ProvenanceBindingV1, ...]

    @field_validator("exact_bytes")
    @classmethod
    def own_exact_bytes(cls, value: bytes) -> bytes:
        return bytes(bytearray(value))


class RevisionPublishRequestV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    run_id: PortableId
    transaction_id: TransactionId
    proposed_revision: int = Field(ge=1)
    expected_revision: int | None = Field(default=None, ge=1)
    expected_manifest_sha256: Sha256 | None = None
    expected_pointer_sha256: Sha256 | None = None
    created_at: datetime
    producer_id: PortableId
    producer_version: Version
    artifacts: tuple[RevisionArtifactV1, ...]

    _utc_created_at = field_validator("created_at")(lambda value: _require_utc(value, "created_at"))

    @model_validator(mode="after")
    def exact_revision_sequence(self) -> RevisionPublishRequestV1:
        _validate_revision_expectations(
            self.proposed_revision,
            self.expected_revision,
            self.expected_manifest_sha256,
            self.expected_pointer_sha256,
        )
        return self


class RevisionTransactionDescriptorV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    storage_protocol: Literal["poker-run-revision-v1"] = STORAGE_PROTOCOL
    canonicalization: Literal["poker-run-storage-json-v1"] = STORAGE_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = STORAGE_HASH_ALGORITHM
    run_id: PortableId
    transaction_id: TransactionId
    proposed_revision: int = Field(ge=1)
    expected_revision: int | None = Field(default=None, ge=1)
    expected_manifest_sha256: Sha256 | None = None
    expected_pointer_sha256: Sha256 | None = None
    created_at: datetime
    producer_id: PortableId
    producer_version: Version
    artifact_plan: tuple[PayloadInventoryEntryV1, ...]
    provenance_heads: tuple[ProvenanceHeadV1, ...]
    transaction_sha256: Sha256

    _utc_created_at = field_validator("created_at")(lambda value: _require_utc(value, "created_at"))

    @model_validator(mode="after")
    def exact_revision_sequence(self) -> RevisionTransactionDescriptorV1:
        _validate_revision_expectations(
            self.proposed_revision,
            self.expected_revision,
            self.expected_manifest_sha256,
            self.expected_pointer_sha256,
        )
        return self


class StorageRevisionManifestV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    storage_protocol: Literal["poker-run-revision-v1"] = STORAGE_PROTOCOL
    canonicalization: Literal["poker-run-storage-json-v1"] = STORAGE_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = STORAGE_HASH_ALGORITHM
    publication_kind: Literal["structural_nonterminal"] = PUBLICATION_KIND
    run_id: PortableId
    revision: int = Field(ge=1)
    transaction_id: TransactionId
    transaction_sha256: Sha256
    previous_revision: int | None = Field(default=None, ge=1)
    previous_manifest_sha256: Sha256 | None = None
    expected_pointer_sha256: Sha256 | None = None
    created_at: datetime
    producer_id: PortableId
    producer_version: Version
    inventory_sha256: Sha256
    provenance_heads: tuple[ProvenanceHeadV1, ...]
    artifacts: tuple[PayloadInventoryEntryV1, ...]

    _utc_created_at = field_validator("created_at")(lambda value: _require_utc(value, "created_at"))

    @model_validator(mode="after")
    def previous_revision_is_exact(self) -> StorageRevisionManifestV1:
        if self.revision == 1:
            if self.previous_revision is not None or self.previous_manifest_sha256 is not None:
                raise ValueError("revision 1 cannot have previous lineage")
        elif self.previous_revision != self.revision - 1 or self.previous_manifest_sha256 is None:
            raise ValueError("successor manifest requires exact previous lineage")
        return self


class StorageRevisionPointerV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    storage_protocol: Literal["poker-run-revision-v1"] = STORAGE_PROTOCOL
    canonicalization: Literal["poker-run-storage-json-v1"] = STORAGE_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = STORAGE_HASH_ALGORITHM
    publication_kind: Literal["structural_nonterminal"] = PUBLICATION_KIND
    run_id: PortableId
    revision: int = Field(ge=1)
    transaction_id: TransactionId
    revision_relative_path: str = Field(min_length=1, max_length=192)
    transaction_sha256: Sha256
    manifest_sha256: Sha256
    inventory_sha256: Sha256
    published_at: datetime

    _utc_published_at = field_validator("published_at")(
        lambda value: _require_utc(value, "published_at")
    )

    @model_validator(mode="after")
    def canonical_revision_path(self) -> StorageRevisionPointerV1:
        expected = f"revisions/r{self.revision}-{self.transaction_id}"
        if self.revision_relative_path != expected:
            raise ValueError("pointer revision_relative_path mismatch")
        return self


class OwnershipMarkerV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    ownership_kind: Literal["poker_revision_root"] = "poker_revision_root"
    storage_protocol: Literal["poker-run-revision-v1"] = STORAGE_PROTOCOL
    canonicalization: Literal["poker-run-storage-json-v1"] = STORAGE_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = STORAGE_HASH_ALGORITHM
    root_id: RootId
    legacy_runs_root_identity_sha256: Sha256
    initialized_at: datetime
    producer_id: PortableId
    producer_version: Version

    _utc_initialized_at = field_validator("initialized_at")(
        lambda value: _require_utc(value, "initialized_at")
    )


class LockMetadataV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    storage_protocol: Literal["poker-run-revision-v1"] = STORAGE_PROTOCOL
    run_id_sha256: Sha256
    ownership_marker_sha256: Sha256
    authority_identity_sha256: Sha256
    owner_token: OwnerToken
    process_id: int = Field(gt=0)
    adapter: PlatformAdapter
    transaction_id: TransactionId | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    acquired_at: datetime

    _utc_acquired_at = field_validator("acquired_at")(
        lambda value: _require_utc(value, "acquired_at")
    )


class RecoveryClaimRequestV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    storage_protocol: Literal["poker-run-revision-v1"] = STORAGE_PROTOCOL
    canonicalization: Literal["poker-run-storage-json-v1"] = STORAGE_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = STORAGE_HASH_ALGORITHM
    run_id_sha256: Sha256
    transaction_id: TransactionId
    transaction_sha256: Sha256
    observed_pointer_sha256: Sha256 | None = None
    orphan_form: Literal["staging", "unreferenced_revision"]
    claim_id: ClaimId
    claimant_token: OwnerToken
    claimed_at: datetime

    _utc_claimed_at = field_validator("claimed_at")(lambda value: _require_utc(value, "claimed_at"))


class RecoveryClaimV1(RecoveryClaimRequestV1):
    claim_sha256: Sha256


class DurabilityEvidenceV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    platform_adapter: PlatformAdapter
    file_sync: Literal["not_attempted", "confirmed", "failed"]
    directory_sync: Literal["not_attempted", "confirmed", "unavailable", "failed"]
    pointer_replace: Literal["not_attempted", "attempted_unconfirmed", "confirmed"]
    reconciliation: Literal["confirmed", "required"]


class RevisionPublishOutcomeV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    outcome_kind: Literal[
        "published",
        "current_committed",
        "historical_committed",
        "reconciliation_required",
    ]
    run_id_sha256: Sha256
    transaction_id: TransactionId
    transaction_sha256: Sha256
    revision: int = Field(ge=1)
    observed_current_revision: int | None = Field(default=None, ge=1)
    manifest_sha256: Sha256 | None = None
    pointer_sha256: Sha256 | None = None
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
    durability_evidence: DurabilityEvidenceV1

    @model_validator(mode="after")
    def closed_outcome_matrix(self) -> RevisionPublishOutcomeV1:
        previous = "not_applicable" if self.revision == 1 else "unchanged"
        durability = self.durability_evidence
        if self.outcome_kind == "published":
            if (
                self.observed_current_revision != self.revision
                or self.manifest_sha256 is None
                or self.pointer_sha256 is None
                or self.filesystem_effect != "current_advanced"
                or self.domain_effect != "current_advanced"
                or self.previous_revision_effect != previous
                or durability.file_sync != "confirmed"
                or durability.directory_sync not in {"confirmed", "unavailable"}
                or durability.pointer_replace != "confirmed"
                or durability.reconciliation != "confirmed"
            ):
                raise ValueError("published outcome violates the closed effect matrix")
        elif self.outcome_kind == "current_committed":
            if (
                self.observed_current_revision != self.revision
                or self.manifest_sha256 is None
                or self.pointer_sha256 is None
                or self.filesystem_effect != "none"
                or self.domain_effect != "current_unchanged"
                or self.previous_revision_effect != previous
                or durability.file_sync != "not_attempted"
                or durability.directory_sync != "not_attempted"
                or durability.pointer_replace != "not_attempted"
                or durability.reconciliation != "confirmed"
            ):
                raise ValueError("current_committed outcome violates the closed effect matrix")
        elif self.outcome_kind == "historical_committed":
            if (
                self.observed_current_revision is None
                or self.observed_current_revision <= self.revision
                or self.manifest_sha256 is None
                or self.pointer_sha256 is None
                or self.filesystem_effect != "none"
                or self.domain_effect != "current_unchanged"
                or self.previous_revision_effect != previous
                or durability.file_sync != "not_attempted"
                or durability.directory_sync != "not_attempted"
                or durability.pointer_replace != "not_attempted"
                or durability.reconciliation != "confirmed"
            ):
                raise ValueError("historical_committed outcome violates the closed effect matrix")
        elif (
            durability.reconciliation != "required"
            or self.filesystem_effect
            not in {
                "staging_orphan",
                "unreferenced_revision",
                "current_replace_attempted",
                "current_advanced",
            }
            or self.domain_effect
            not in {"current_unchanged", "current_may_have_advanced", "current_advanced"}
            or (
                self.revision > 1
                and self.previous_revision_effect not in {"unchanged", "unconfirmed"}
            )
            or (self.revision == 1 and self.previous_revision_effect != "not_applicable")
        ):
            raise ValueError("reconciliation outcome violates the closed effect matrix")
        return self


class RootInitializationRequestV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    revision_root: Path
    legacy_runs_root: Path
    root_id: RootId
    initialized_at: datetime
    producer_id: PortableId
    producer_version: Version

    _utc_initialized_at = field_validator("initialized_at")(
        lambda value: _require_utc(value, "initialized_at")
    )


class RootInitializationInspectionV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    status: Literal["uninitialized", "incomplete", "initialized", "corrupt"]
    root_id: RootId | None = None
    ownership_marker_sha256: Sha256 | None = None
    recognized_relative_paths: tuple[str, ...] = ()

    @field_validator("recognized_relative_paths")
    @classmethod
    def canonical_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(value, key=lambda item: item.encode("utf-8"))):
            raise ValueError("recognized paths must be sorted by UTF-8 bytes")
        if len(value) != len(set(value)):
            raise ValueError("recognized paths must be unique")
        return value


class RootInitializationOutcomeV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    outcome_kind: Literal["initialized", "already_initialized", "reconciliation_required"]
    root_id: RootId
    ownership_marker_sha256: Sha256 | None = None
    filesystem_effect: Literal["none", "control_only"]
    durability_evidence: DurabilityEvidenceV1

    @model_validator(mode="after")
    def closed_root_matrix(self) -> RootInitializationOutcomeV1:
        durability = self.durability_evidence
        if self.outcome_kind == "initialized":
            valid = (
                self.ownership_marker_sha256 is not None
                and self.filesystem_effect == "control_only"
                and durability.file_sync == "confirmed"
                and durability.directory_sync in {"confirmed", "unavailable"}
                and durability.pointer_replace == "not_attempted"
                and durability.reconciliation == "confirmed"
            )
        elif self.outcome_kind == "already_initialized":
            valid = (
                self.ownership_marker_sha256 is not None
                and self.filesystem_effect == "none"
                and durability.file_sync == "not_attempted"
                and durability.directory_sync == "not_attempted"
                and durability.pointer_replace == "not_attempted"
                and durability.reconciliation == "confirmed"
            )
        else:
            valid = (
                self.filesystem_effect == "control_only"
                and durability.pointer_replace == "not_attempted"
                and durability.reconciliation == "required"
            )
        if not valid:
            raise ValueError("root outcome violates the closed effect matrix")
        return self


class ReachableRevisionV1(_RevisionModel):
    revision: int = Field(ge=1)
    transaction_id: TransactionId
    revision_relative_path: str
    transaction_sha256: Sha256
    manifest_sha256: Sha256


class VerifiedStorageRevisionV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    verification_kind: Literal["structural_nonterminal"] = PUBLICATION_KIND
    run_id: PortableId
    current_revision: int = Field(ge=1)
    current_pointer_sha256: Sha256
    manifest_sha256: Sha256
    inventory_sha256: Sha256
    reachable_history: tuple[ReachableRevisionV1, ...]


class StructuralArtifactRevisionV1(_RevisionModel):
    """One verified immutable payload; it carries no product status meaning."""

    revision: int = Field(ge=1)
    transaction_id: TransactionId
    manifest_sha256: Sha256
    logical_name: str = Field(min_length=1, max_length=256)
    artifact_schema_version: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$",
    )
    size_bytes: int = Field(ge=0)
    sha256: Sha256
    exact_bytes: bytes

    @field_validator("exact_bytes")
    @classmethod
    def own_exact_bytes(cls, value: bytes) -> bytes:
        return bytes(bytearray(value))

    @model_validator(mode="after")
    def exact_payload_identity(self) -> StructuralArtifactRevisionV1:
        if len(self.exact_bytes) != self.size_bytes:
            raise ValueError("structural artifact size mismatch")
        if hashlib.sha256(self.exact_bytes).hexdigest() != self.sha256:
            raise ValueError("structural artifact hash mismatch")
        return self


class StructuralArtifactHistoryV1(_RevisionModel):
    """Verified current-to-genesis artifact bytes from a structural root only."""

    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    verification_kind: Literal["structural_artifact_history"] = (
        "structural_artifact_history"
    )
    run_id: PortableId
    logical_name: str = Field(min_length=1, max_length=256)
    current_revision: int = Field(ge=1)
    current_pointer_sha256: Sha256
    revisions: tuple[StructuralArtifactRevisionV1, ...]

    @model_validator(mode="after")
    def current_to_genesis_is_exact(self) -> StructuralArtifactHistoryV1:
        if not self.revisions:
            raise ValueError("structural artifact history cannot be empty")
        expected = tuple(range(self.current_revision, 0, -1))
        if tuple(entry.revision for entry in self.revisions) != expected:
            raise ValueError("structural artifact history must be current-to-genesis")
        if any(entry.logical_name != self.logical_name for entry in self.revisions):
            raise ValueError("structural artifact history logical-name mismatch")
        return self


class OrphanEntryV1(_RevisionModel):
    orphan_form: Literal["staging", "unreferenced_revision"]
    verification_state: Literal["path_only", "descriptor_verified", "manifest_verified"]
    transaction_id: TransactionId
    transaction_sha256: Sha256 | None = None
    revision: int | None = Field(default=None, ge=1)
    relative_path: str


class OrphanInspectionV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    run_id_sha256: Sha256
    current_pointer_sha256: Sha256 | None = None
    reachable_history: tuple[ReachableRevisionV1, ...]
    staging_orphans: tuple[OrphanEntryV1, ...]
    revision_orphans: tuple[OrphanEntryV1, ...]


class RunStorageFailureCode(StrEnum):
    INVALID_STORAGE_INPUT = "invalid_storage_input"
    UNSUPPORTED_STORAGE_VERSION = "unsupported_storage_version"
    LEGACY_RUN_UNVERIFIED = "legacy_run_unverified"
    RUN_NAMESPACE_CONFLICT = "run_namespace_conflict"
    ROOT_INITIALIZATION_INCOMPLETE = "root_initialization_incomplete"
    PATH_CONFINEMENT_FAILED = "path_confinement_failed"
    LINK_OR_REPARSE_DETECTED = "link_or_reparse_detected"
    CROSS_RUN_MISMATCH = "cross_run_mismatch"
    HASH_ALGORITHM_MISMATCH = "hash_algorithm_mismatch"
    ARTIFACT_BUDGET_EXCEEDED = "artifact_budget_exceeded"
    RUN_BUDGET_EXCEEDED = "run_budget_exceeded"
    RUN_LOCKED = "run_locked"
    LOCK_UNAVAILABLE = "lock_unavailable"
    RUN_CONFLICT = "run_conflict"
    RECOVERY_CLAIM_CONFLICT = "recovery_claim_conflict"
    RECOVERY_CLAIM_INCOMPLETE = "recovery_claim_incomplete"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    PERSISTENCE_FORBIDDEN = "persistence_forbidden"
    ENCRYPTION_REQUIRED = "encryption_required"
    TRANSACTION_WRITE_FAILED = "transaction_write_failed"
    TRANSACTION_VERIFICATION_FAILED = "transaction_verification_failed"
    TRANSACTION_PUBLISH_FAILED = "transaction_publish_failed"
    DURABILITY_UNCONFIRMED = "durability_unconfirmed"
    EFFECT_UNKNOWN = "effect_unknown"
    RUN_INCOMPLETE = "run_incomplete"
    RUN_CORRUPT = "run_corrupt"
    ARTIFACT_MISSING = "artifact_missing"
    ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
    ARTIFACT_SCHEMA_ERROR = "artifact_schema_error"
    INTERNAL_INVARIANT_ERROR = "internal_invariant_error"


RootFailureStage = Literal["root_preflight", "root_initialization"]
RunFailureStage = Literal[
    "preflight",
    "lock_bootstrap",
    "initial_read",
    "locked_admission",
    "lock_metadata",
    "namespace_bootstrap",
    "staging",
    "transaction",
    "payload",
    "manifest",
    "revision_publish",
    "pointer",
    "final_cas",
    "current_replace",
    "reconciliation",
    "directory_sync",
    "lock_release",
    "orphan_inspect",
    "recovery_claim",
]


class RunStorageFailureV1(_RevisionModel):
    schema_version: Literal["1.0.0"] = STORAGE_SCHEMA_VERSION
    code: RunStorageFailureCode
    stage: RootFailureStage | RunFailureStage
    message: str
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
    root_id: RootId | None = None
    run_id_sha256: Sha256 | None = None
    transaction_id: TransactionId | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    observed_revision: int | None = Field(default=None, ge=1)
    durability_evidence: DurabilityEvidenceV1 | None = None

    @model_validator(mode="after")
    def closed_failure_contract(self) -> RunStorageFailureV1:
        if self.message != self.code.value:
            raise ValueError("failure message must equal its redacted code")
        if self.retryable != (self.code is RunStorageFailureCode.RUN_LOCKED):
            raise ValueError("only run_locked is semantically retryable")
        if self.stage in {"root_preflight", "root_initialization"}:
            if (
                self.root_id is None
                or self.run_id_sha256 is not None
                or self.transaction_id is not None
                or self.expected_revision is not None
                or self.observed_revision is not None
            ):
                raise ValueError("root failure identity fields mismatch")
        elif self.run_id_sha256 is None or self.root_id is not None:
            raise ValueError("run failure identity fields mismatch")
        return self


class RunStorageError(ValueError):
    """Raised with one redacted typed storage failure."""

    def __init__(self, failure: RunStorageFailureV1):
        self.failure = failure
        super().__init__(failure.code.value)


__all__ = [
    "APPROVED_LOCAL_DATA_POLICY_ID",
    "APPROVED_LOCAL_DATA_POLICY_SHA256",
    "APPROVED_LOCAL_DATA_POLICY_VERSION",
    "ApprovalDecisionBindingV1",
    "ArtifactIntentSnapshotV1",
    "BudgetPolicyBindingV1",
    "ContextBindingV1",
    "DurabilityEvidenceV1",
    "LocalDataBindingV1",
    "LockMetadataV1",
    "OrphanEntryV1",
    "OrphanInspectionV1",
    "OwnershipMarkerV1",
    "PayloadInventoryEntryV1",
    "PhaseBindingV1",
    "ProvenanceBindingV1",
    "ProvenanceHeadV1",
    "ReachableRevisionV1",
    "RecoveryClaimRequestV1",
    "RecoveryClaimV1",
    "ReportBindingV1",
    "RevisionArtifactV1",
    "RevisionPublishOutcomeV1",
    "RevisionPublishRequestV1",
    "RevisionTransactionDescriptorV1",
    "RootInitializationInspectionV1",
    "RootInitializationOutcomeV1",
    "RootInitializationRequestV1",
    "RunStorageError",
    "RunStorageFailureCode",
    "RunStorageFailureV1",
    "SourceBindingV1",
    "StorageRevisionManifestV1",
    "StorageRevisionPointerV1",
    "StructuralArtifactHistoryV1",
    "StructuralArtifactRevisionV1",
    "ToolBindingV1",
    "VerifiedStorageRevisionV1",
]
