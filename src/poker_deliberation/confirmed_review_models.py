"""Versioned contracts for human-confirmed natural-language review intake.

These models describe a deliberately bounded projection workflow.  The caller,
not this package, supplies the structured candidate extracted from natural
language.  Confirmation attests only that the projection matches the source;
it does not promote caller content beyond ``USER_CLAIM``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.schemas import CanonicalHand
from poker_deliberation.tools.hand_pot_ledger import HandRuleProfileV1

CONFIRMED_REVIEW_CONTRACT_ID: Literal["poker-deliberation.confirmed-review-intake"] = (
    "poker-deliberation.confirmed-review-intake"
)
CONFIRMED_REVIEW_CONTRACT_VERSION: Literal["1.0.0"] = "1.0.0"
CONFIRMED_REVIEW_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
CONFIRMED_REVIEW_RESULT_VERSION: Literal["1.0.0"] = "1.0.0"

CONFIRMED_REVIEW_EXTRACTOR_ID: Literal["poker-deliberation.caller-supplied-candidate"] = (
    "poker-deliberation.caller-supplied-candidate"
)
CONFIRMED_REVIEW_EXTRACTOR_VERSION: Literal["1.0.0"] = "1.0.0"
CONFIRMED_REVIEW_PRODUCER_KIND: Literal["human_confirmed_projection"] = "human_confirmed_projection"

SOURCE_CANONICALIZATION_ID: Literal["poker-confirmed-review-source-bytes-v1"] = (
    "poker-confirmed-review-source-bytes-v1"
)
CANDIDATE_CANONICALIZATION_ID: Literal["poker-confirmed-review-candidate-json-v1"] = (
    "poker-confirmed-review-candidate-json-v1"
)
CONFIRMATION_CANONICALIZATION_ID: Literal["poker-confirmed-review-confirmation-json-v1"] = (
    "poker-confirmed-review-confirmation-json-v1"
)
PROVENANCE_CANONICALIZATION_ID: Literal["poker-confirmed-review-provenance-json-v1"] = (
    "poker-confirmed-review-provenance-json-v1"
)

SOURCE_ARTIFACT_SCHEMA = "poker-confirmed-review-source-artifact-v1"
CANDIDATE_ARTIFACT_SCHEMA = "poker-confirmed-review-candidate-artifact-v1"
CONFIRMATION_ARTIFACT_SCHEMA = "poker-confirmed-review-confirmation-artifact-v1"
PROVENANCE_ARTIFACT_SCHEMA = "poker-confirmed-review-provenance-artifact-v1"

MAX_CONFIRMED_REVIEW_SOURCE_BYTES = 1_000_000
MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES = 1_000_000
MAX_CONFIRMED_REVIEW_RUN_BYTES = 10_000_000
MAX_CONFIRMED_REVIEW_ACTIONS = 512
MAX_CONFIRMED_REVIEW_RANGES = 1
MAX_CONFIRMED_REVIEW_AMBIGUITIES = 64
MAX_CONFIRMED_REVIEW_CLAIMS = 64
MAX_CONFIRMATION_LIFETIME_SECONDS = 86_400

CONFIRMED_REVIEW_TOOL_ALLOWLIST = frozenset(
    {"hand_validator", "hand_pot_ledger", "range_validate", "combos"}
)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PORTABLE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class _ConfirmedReviewModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class ConfirmedReviewDiagnosticCode(StrEnum):
    SOURCE_SIZE = "CRI_E_SOURCE_SIZE"
    SOURCE_UTF8 = "CRI_E_SOURCE_UTF8"
    SOURCE_BOM = "CRI_E_SOURCE_BOM"
    SOURCE_NEWLINE = "CRI_E_SOURCE_NEWLINE"
    SOURCE_NFC = "CRI_E_SOURCE_NFC"
    SOURCE_CONTROL = "CRI_E_SOURCE_CONTROL"
    SOURCE_SECRET = "CRI_E_SOURCE_SECRET"
    SOURCE_RIGHTS = "CRI_E_SOURCE_RIGHTS"
    SOURCE_CLASSIFICATION = "CRI_E_SOURCE_CLASSIFICATION"
    CANDIDATE_SCHEMA = "CRI_E_CANDIDATE_SCHEMA"
    CANDIDATE_MISSING = "CRI_E_CANDIDATE_MISSING"
    CANDIDATE_AMBIGUITY = "CRI_E_CANDIDATE_AMBIGUITY"
    CANDIDATE_RANGE_COUNT = "CRI_E_CANDIDATE_RANGE_COUNT"
    CANDIDATE_RANGE_UNSUPPORTED = "CRI_E_CANDIDATE_RANGE_UNSUPPORTED"
    CANDIDATE_SCOPE = "CRI_E_CANDIDATE_SCOPE"
    CANDIDATE_TOOL = "CRI_E_CANDIDATE_TOOL"
    CANDIDATE_SECURITY = "CRI_E_CANDIDATE_SECURITY"
    CONFIRMATION_MISSING = "CRI_E_CONFIRMATION_MISSING"
    CONFIRMATION_BINDING = "CRI_E_CONFIRMATION_BINDING"
    CONFIRMATION_AUTHORITY = "CRI_E_CONFIRMATION_AUTHORITY"
    CONFIRMATION_EXPIRED = "CRI_E_CONFIRMATION_EXPIRED"
    CONFIRMATION_REPLAY = "CRI_E_CONFIRMATION_REPLAY"
    LOCAL_PROVIDER = "CRI_E_LOCAL_PROVIDER"
    RUNTIME_BUDGET = "CRI_E_RUNTIME_BUDGET"
    EXTERNAL_SOLVER = "CRI_E_EXTERNAL_SOLVER"
    REPORT_OVERREACH = "CRI_E_REPORT_OVERREACH"
    STORAGE = "CRI_E_STORAGE"


class ConfirmedReviewDiagnosticV1(_ConfirmedReviewModel):
    code: ConfirmedReviewDiagnosticCode
    field_path: str = Field(min_length=1, max_length=256)


class ReviewSourceProvenanceV1(_ConfirmedReviewModel):
    source_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_kind: Literal["user_supplied", "repository_fixture"]
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ]
    usage_classification: Literal[
        "local_analysis_only",
        "redistribution_allowed",
    ]
    classification: Literal["internal", "public"]
    content_status: Literal["USER_CLAIM"] = "USER_CLAIM"
    encoding: Literal["utf-8"] = "utf-8"
    newline: Literal["lf"] = "lf"
    normalization: Literal["NFC"] = "NFC"
    bytes_length: int = Field(ge=0, le=MAX_CONFIRMED_REVIEW_SOURCE_BYTES)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def rights_match_source_kind(self) -> ReviewSourceProvenanceV1:
        expected = {
            "user_supplied": (
                "user_supplied_private_analysis",
                "local_analysis_only",
                "internal",
            ),
            "repository_fixture": (
                "repository_owned_mit",
                "redistribution_allowed",
                "public",
            ),
        }[self.source_kind]
        if (
            self.license_classification,
            self.usage_classification,
            self.classification,
        ) != expected:
            raise ValueError(ConfirmedReviewDiagnosticCode.SOURCE_RIGHTS.value)
        return self


class ReviewAmbiguityV1(_ConfirmedReviewModel):
    ambiguity_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    field_path: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=2_000)
    status: Literal["unresolved", "resolved", "optional_omitted"]
    candidates: tuple[str, ...] = Field(default=(), max_length=16)
    selected: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def status_is_closed(self) -> ReviewAmbiguityV1:
        if self.status == "unresolved" and self.selected is not None:
            raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_AMBIGUITY.value)
        if self.status == "resolved" and self.selected is None:
            raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_AMBIGUITY.value)
        if self.status == "optional_omitted" and (self.selected is not None or self.candidates):
            raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_AMBIGUITY.value)
        return self


class ReviewUserClaimV1(_ConfirmedReviewModel):
    claim_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    text: str = Field(min_length=1, max_length=10_000)
    label: Literal["USER_CLAIM"] = "USER_CLAIM"
    confidence: Literal["C", "D"] = "C"


class ReviewCandidateInputV1(_ConfirmedReviewModel):
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    hand: CanonicalHand
    ambiguities: tuple[ReviewAmbiguityV1, ...] = Field(
        default=(),
        max_length=MAX_CONFIRMED_REVIEW_AMBIGUITIES,
    )
    claims: tuple[ReviewUserClaimV1, ...] = Field(
        default=(),
        max_length=MAX_CONFIRMED_REVIEW_CLAIMS,
    )
    ledger_profile: HandRuleProfileV1 | None = None

    @field_validator("hand", mode="before")
    @classmethod
    def require_only_versioned_ranges(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        ranges = value.get("known_ranges", [])
        if not isinstance(ranges, list):
            return value
        for item in ranges:
            if not isinstance(item, dict) or "schema_version" not in item:
                raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_UNSUPPORTED.value)
        return value

    @model_validator(mode="after")
    def unique_ids(self) -> ReviewCandidateInputV1:
        ambiguity_ids = [item.ambiguity_id for item in self.ambiguities]
        claim_ids = [item.claim_id for item in self.claims]
        if len(ambiguity_ids) != len(set(ambiguity_ids)):
            raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA.value)
        if len(claim_ids) != len(set(claim_ids)):
            raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA.value)
        return self


class ReviewCandidateProjectionV1(_ConfirmedReviewModel):
    schema_version: Literal["1.0.0"] = CONFIRMED_REVIEW_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.confirmed-review-intake"] = (
        CONFIRMED_REVIEW_CONTRACT_ID
    )
    contract_version: Literal["1.0.0"] = CONFIRMED_REVIEW_CONTRACT_VERSION
    extractor_id: Literal["poker-deliberation.caller-supplied-candidate"] = (
        CONFIRMED_REVIEW_EXTRACTOR_ID
    )
    extractor_version: Literal["1.0.0"] = CONFIRMED_REVIEW_EXTRACTOR_VERSION
    producer_kind: Literal["human_confirmed_projection"] = CONFIRMED_REVIEW_PRODUCER_KIND
    external_execution: Literal[False] = False
    analysis_scope: Literal["retrospective"] = "retrospective"
    source: ReviewSourceProvenanceV1
    candidate_input: ReviewCandidateInputV1


class ReviewIntakeCandidateV1(_ConfirmedReviewModel):
    schema_version: Literal["1.0.0"] = CONFIRMED_REVIEW_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.confirmed-review-intake"] = (
        CONFIRMED_REVIEW_CONTRACT_ID
    )
    result_version: Literal["1.0.0"] = CONFIRMED_REVIEW_RESULT_VERSION
    canonicalization_id: Literal["poker-confirmed-review-candidate-json-v1"] = (
        CANDIDATE_CANONICALIZATION_ID
    )
    projection: ReviewCandidateProjectionV1
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)


class ReviewIntakePreparationResultV1(_ConfirmedReviewModel):
    schema_version: Literal["1.0.0"] = CONFIRMED_REVIEW_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.confirmed-review-intake"] = (
        CONFIRMED_REVIEW_CONTRACT_ID
    )
    result_version: Literal["1.0.0"] = CONFIRMED_REVIEW_RESULT_VERSION
    status: Literal["ready", "blocked"]
    source: ReviewSourceProvenanceV1 | None = None
    candidate: ReviewIntakeCandidateV1 | None = None
    diagnostics: tuple[ConfirmedReviewDiagnosticV1, ...] = ()

    @model_validator(mode="after")
    def result_is_closed(self) -> ReviewIntakePreparationResultV1:
        if self.status == "ready":
            if self.source is None or self.candidate is None or self.diagnostics:
                raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA.value)
            if self.source != self.candidate.projection.source:
                raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA.value)
        elif not self.diagnostics or self.candidate is not None:
            raise ValueError(ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA.value)
        return self


class ReviewConfirmationAuthorityV1(_ConfirmedReviewModel):
    authority_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    authority_kind: Literal["local_user", "verified_application"]
    authentication: Literal["self_asserted", "verified"]
    scope: Literal["confirm_review_projection"] = "confirm_review_projection"

    @model_validator(mode="after")
    def authentication_matches_kind(self) -> ReviewConfirmationAuthorityV1:
        expected = {
            "local_user": "self_asserted",
            "verified_application": "verified",
        }[self.authority_kind]
        if self.authentication != expected:
            raise ValueError(ConfirmedReviewDiagnosticCode.CONFIRMATION_AUTHORITY.value)
        return self


class ReviewIntakeConfirmationV1(_ConfirmedReviewModel):
    schema_version: Literal["1.0.0"] = CONFIRMED_REVIEW_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.confirmed-review-intake"] = (
        CONFIRMED_REVIEW_CONTRACT_ID
    )
    contract_version: Literal["1.0.0"] = CONFIRMED_REVIEW_CONTRACT_VERSION
    canonicalization_id: Literal["poker-confirmed-review-confirmation-json-v1"] = (
        CONFIRMATION_CANONICALIZATION_ID
    )
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    confirmation_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    idempotency_key: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_schema_version: Literal["1.0.0"] = CONFIRMED_REVIEW_SCHEMA_VERSION
    extractor_id: Literal["poker-deliberation.caller-supplied-candidate"] = (
        CONFIRMED_REVIEW_EXTRACTOR_ID
    )
    extractor_version: Literal["1.0.0"] = CONFIRMED_REVIEW_EXTRACTOR_VERSION
    authority: ReviewConfirmationAuthorityV1
    authority_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmed: Literal[True] = True
    confirmed_at: datetime
    expires_at: datetime
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("confirmed_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING.value)
        return value

    @model_validator(mode="after")
    def time_window_is_supported(self) -> ReviewIntakeConfirmationV1:
        lifetime = (self.expires_at - self.confirmed_at).total_seconds()
        if lifetime <= 0 or lifetime > MAX_CONFIRMATION_LIFETIME_SECONDS:
            raise ValueError(ConfirmedReviewDiagnosticCode.CONFIRMATION_EXPIRED.value)
        return self


class ConfirmedReviewToolSupportV1(_ConfirmedReviewModel):
    result_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    tool_name: Literal[
        "hand_validator",
        "hand_pot_ledger",
        "range_validate",
        "combos",
    ]
    tool_version: str = Field(min_length=1, max_length=32)
    contract_version: str = Field(min_length=1, max_length=32)
    status: Literal["success", "failed", "unavailable"]
    epistemic_label: Literal["CALCULATED", "ESTIMATE", "UNKNOWN"]
    input_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)


class ConfirmedReviewAgentSupportV1(_ConfirmedReviewModel):
    execution_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    agent_role: str = Field(min_length=1, max_length=128)
    provider: Literal["local"]
    provider_version: Literal["1.0.0"]
    status: Literal["completed", "failed", "refused", "fallback"]
    record_sha256: str = Field(pattern=_SHA256_PATTERN)


class ConfirmedReviewProvenanceV1(_ConfirmedReviewModel):
    schema_version: Literal["1.0.0"] = CONFIRMED_REVIEW_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.confirmed-review-intake"] = (
        CONFIRMED_REVIEW_CONTRACT_ID
    )
    result_version: Literal["1.0.0"] = CONFIRMED_REVIEW_RESULT_VERSION
    canonicalization_id: Literal["poker-confirmed-review-provenance-json-v1"] = (
        PROVENANCE_CANONICALIZATION_ID
    )
    report_claim_policy_id: Literal["poker-confirmed-review-claim-policy-v1"] = (
        "poker-confirmed-review-claim-policy-v1"
    )
    provider_narrative_epistemic_label: Literal["UNKNOWN"] = "UNKNOWN"
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    admitted_at: datetime
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    case_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    final_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_revision_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_revision: int = Field(ge=1)
    terminal_transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{32}$")
    agent_support: tuple[ConfirmedReviewAgentSupportV1, ...]
    tool_support: tuple[ConfirmedReviewToolSupportV1, ...]
    terminal_status: Literal["completed", "failed_with_limitations"]
    provenance_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("admitted_at")
    @classmethod
    def admitted_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING.value)
        return value
