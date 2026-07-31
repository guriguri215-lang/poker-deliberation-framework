"""Strict contracts for the bounded Japanese NLHE cash intake slice.

This contract is additive to P3-030A.  It describes a deterministic parser,
source-byte bindings, one focal call/fold decision, and a hash-bound tool plan.
It deliberately does not widen the caller-supplied P3-030A v1 contract.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.schemas import CanonicalHand
from poker_deliberation.tools.hand_pot_ledger import HandRuleProfileV1

BOUNDED_NL_CONTRACT_ID: Literal["poker-deliberation.bounded-japanese-nlhe-cash"] = (
    "poker-deliberation.bounded-japanese-nlhe-cash"
)
BOUNDED_NL_CONTRACT_VERSION: Literal["1.0.0"] = "1.0.0"
BOUNDED_NL_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
BOUNDED_NL_RESULT_VERSION: Literal["1.0.0"] = "1.0.0"
BOUNDED_NL_EXTRACTOR_ID: Literal["poker-deliberation.bounded-japanese-nlhe-cash-parser"] = (
    "poker-deliberation.bounded-japanese-nlhe-cash-parser"
)
BOUNDED_NL_EXTRACTOR_VERSION: Literal["1.0.0"] = "1.0.0"
BOUNDED_NL_PRODUCER_KIND: Literal["deterministic_bounded_parser"] = "deterministic_bounded_parser"

BOUNDED_NL_SOURCE_CANONICALIZATION_ID: Literal["poker-bounded-nl-source-bytes-v1"] = (
    "poker-bounded-nl-source-bytes-v1"
)
BOUNDED_NL_BINDINGS_CANONICALIZATION_ID: Literal["poker-bounded-nl-source-bindings-json-v1"] = (
    "poker-bounded-nl-source-bindings-json-v1"
)
BOUNDED_NL_FOCAL_CANONICALIZATION_ID: Literal["poker-bounded-nl-focal-json-v1"] = (
    "poker-bounded-nl-focal-json-v1"
)
BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID: Literal["poker-bounded-nl-tool-plan-json-v1"] = (
    "poker-bounded-nl-tool-plan-json-v1"
)
BOUNDED_NL_EXTRACTOR_CANONICALIZATION_ID: Literal["poker-bounded-nl-extractor-json-v1"] = (
    "poker-bounded-nl-extractor-json-v1"
)
BOUNDED_NL_CANDIDATE_CANONICALIZATION_ID: Literal["poker-bounded-nl-candidate-json-v1"] = (
    "poker-bounded-nl-candidate-json-v1"
)
BOUNDED_NL_CONFIRMATION_CANONICALIZATION_ID: Literal["poker-bounded-nl-confirmation-json-v1"] = (
    "poker-bounded-nl-confirmation-json-v1"
)
BOUNDED_NL_PROVENANCE_CANONICALIZATION_ID: Literal["poker-bounded-nl-provenance-json-v1"] = (
    "poker-bounded-nl-provenance-json-v1"
)

BOUNDED_NL_SOURCE_ARTIFACT_SCHEMA = "poker-bounded-nl-source-artifact-v1"
BOUNDED_NL_CANDIDATE_ARTIFACT_SCHEMA = "poker-bounded-nl-candidate-artifact-v1"
BOUNDED_NL_CONFIRMATION_ARTIFACT_SCHEMA = "poker-bounded-nl-confirmation-artifact-v1"
BOUNDED_NL_PROVENANCE_ARTIFACT_SCHEMA = "poker-bounded-nl-provenance-artifact-v1"

MAX_BOUNDED_NL_SOURCE_BYTES = 65_536
MAX_BOUNDED_NL_ARTIFACT_BYTES = 1_000_000
MAX_BOUNDED_NL_RUN_BYTES = 10_000_000
MAX_BOUNDED_NL_PLAYERS = 6
MAX_BOUNDED_NL_ACTIONS = 64
MAX_BOUNDED_NL_DIAGNOSTICS = 64
MAX_BOUNDED_NL_BINDINGS = 512
MAX_BOUNDED_NL_CONFIRMATION_LIFETIME_SECONDS = 86_400

BOUNDED_NL_TOOL_ORDER: tuple[Literal["hand_validator", "hand_pot_ledger", "pot_odds"], ...] = (
    "hand_validator",
    "hand_pot_ledger",
    "pot_odds",
)
BOUNDED_NL_TOOL_ALLOWLIST = frozenset(BOUNDED_NL_TOOL_ORDER)

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PORTABLE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_FIELD_PATH_PATTERN = r"^[A-Za-z][A-Za-z0-9_.\[\]-]{0,255}$"


class _BoundedModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class BoundedNaturalLanguageDiagnosticCode(StrEnum):
    SOURCE_SIZE = "BNL_E_SOURCE_SIZE"
    SOURCE_UTF8 = "BNL_E_SOURCE_UTF8"
    SOURCE_BOM = "BNL_E_SOURCE_BOM"
    SOURCE_NEWLINE = "BNL_E_SOURCE_NEWLINE"
    SOURCE_NFC = "BNL_E_SOURCE_NFC"
    SOURCE_CONTROL = "BNL_E_SOURCE_CONTROL"
    SOURCE_SECRET = "BNL_E_SOURCE_SECRET"
    SOURCE_RIGHTS = "BNL_E_SOURCE_RIGHTS"
    CONTROL = "BNL_E_CONTROL"
    CONTROL_SECRET = "BNL_E_CONTROL_SECRET"
    SYNTAX = "BNL_E_SYNTAX"
    UNSUPPORTED = "BNL_E_UNSUPPORTED"
    MISSING = "BNL_E_MISSING"
    DUPLICATE = "BNL_E_DUPLICATE"
    CONFLICT = "BNL_E_CONFLICT"
    LIMIT = "BNL_E_LIMIT"
    PLAYER = "BNL_E_PLAYER"
    CARD = "BNL_E_CARD"
    AMOUNT = "BNL_E_AMOUNT"
    STREET = "BNL_E_STREET"
    ACTION = "BNL_E_ACTION"
    RAISE_AMBIGUITY = "BNL_E_RAISE_AMBIGUITY"
    FOCAL_MISSING = "BNL_E_FOCAL_MISSING"
    FOCAL_MULTIPLE = "BNL_E_FOCAL_MULTIPLE"
    FOCAL_MISMATCH = "BNL_E_FOCAL_MISMATCH"
    LEDGER = "BNL_E_LEDGER"
    POT_MISMATCH = "BNL_E_POT_MISMATCH"
    TOOL = "BNL_E_TOOL"
    CONFIRMATION_MISSING = "BNL_E_CONFIRMATION_MISSING"
    CONFIRMATION_BINDING = "BNL_E_CONFIRMATION_BINDING"
    CONFIRMATION_AUTHORITY = "BNL_E_CONFIRMATION_AUTHORITY"
    CONFIRMATION_EXPIRED = "BNL_E_CONFIRMATION_EXPIRED"
    CONFIRMATION_REPLAY = "BNL_E_CONFIRMATION_REPLAY"
    LOCAL_PROVIDER = "BNL_E_LOCAL_PROVIDER"
    RUNTIME_BUDGET = "BNL_E_RUNTIME_BUDGET"
    REPORT = "BNL_E_REPORT"
    STORAGE = "BNL_E_STORAGE"


class BoundedNaturalLanguageDiagnosticV1(_BoundedModel):
    code: BoundedNaturalLanguageDiagnosticCode
    field_path: str = Field(pattern=_FIELD_PATH_PATTERN)
    start_byte: int | None = Field(default=None, ge=0, le=MAX_BOUNDED_NL_SOURCE_BYTES)
    end_byte: int | None = Field(default=None, ge=0, le=MAX_BOUNDED_NL_SOURCE_BYTES)

    @model_validator(mode="after")
    def span_is_half_open(self) -> BoundedNaturalLanguageDiagnosticV1:
        if (self.start_byte is None) != (self.end_byte is None):
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFLICT.value)
        if (
            self.start_byte is not None
            and self.end_byte is not None
            and self.end_byte <= self.start_byte
        ):
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFLICT.value)
        return self


class BoundedSourceProvenanceV1(_BoundedModel):
    source_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_kind: Literal["user_supplied", "repository_fixture"]
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ]
    usage_classification: Literal["local_analysis_only", "redistribution_allowed"]
    classification: Literal["internal", "public"]
    content_status: Literal["USER_CLAIM"] = "USER_CLAIM"
    encoding: Literal["utf-8"] = "utf-8"
    newline: Literal["lf"] = "lf"
    normalization: Literal["NFC"] = "NFC"
    bytes_length: int = Field(ge=1, le=MAX_BOUNDED_NL_SOURCE_BYTES)
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def rights_match_source_kind(self) -> BoundedSourceProvenanceV1:
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
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.SOURCE_RIGHTS.value)
        return self


class BoundedSourceBindingV1(_BoundedModel):
    field_path: str = Field(pattern=_FIELD_PATH_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    start_byte: int = Field(ge=0, le=MAX_BOUNDED_NL_SOURCE_BYTES)
    end_byte: int = Field(gt=0, le=MAX_BOUNDED_NL_SOURCE_BYTES)
    lexeme_sha256: str = Field(pattern=_SHA256_PATTERN)
    canonical_value_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def span_is_half_open(self) -> BoundedSourceBindingV1:
        if self.end_byte <= self.start_byte:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFLICT.value)
        return self


class BoundedPartialExtractionV1(_BoundedModel):
    field_path: str = Field(pattern=_FIELD_PATH_PATTERN)
    start_byte: int = Field(ge=0, le=MAX_BOUNDED_NL_SOURCE_BYTES)
    end_byte: int = Field(gt=0, le=MAX_BOUNDED_NL_SOURCE_BYTES)
    canonical_value_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def span_is_half_open(self) -> BoundedPartialExtractionV1:
        if self.end_byte <= self.start_byte:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFLICT.value)
        return self


class BoundedFocalDecisionV1(_BoundedModel):
    selector_street: Literal["preflop", "flop", "turn", "river"]
    selector_actor: str = Field(pattern=_PORTABLE_ID_PATTERN)
    selector_action: Literal["bet", "raise"]
    selector_amount: float = Field(gt=0)
    facing_action_index: int = Field(ge=0, lt=MAX_BOUNDED_NL_ACTIONS)
    hero_action_index: int = Field(ge=1, lt=MAX_BOUNDED_NL_ACTIONS)
    hero_response: Literal["call", "fold"]
    focal_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def actions_are_adjacent(self) -> BoundedFocalDecisionV1:
        if self.hero_action_index != self.facing_action_index + 1:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.FOCAL_MISMATCH.value)
        return self


class BoundedPotOddsInputV1(_BoundedModel):
    pot_before_bet: float = Field(ge=0)
    opponent_bet: float = Field(ge=0)
    call_cost: float = Field(gt=0)
    expected_rake: float = Field(default=0.0, ge=0, le=0)


class BoundedDeclaredPotAssertionsV1(_BoundedModel):
    pot_before_bet: float | None = Field(default=None, ge=0)
    call_cost: float | None = Field(default=None, gt=0)
    contestable_pot: float | None = Field(default=None, gt=0)


class BoundedToolPlanV1(_BoundedModel):
    ordered_tools: tuple[Literal["hand_validator", "hand_pot_ledger", "pot_odds"], ...]
    ledger_profile: HandRuleProfileV1
    facing_action_index: int = Field(ge=0, lt=MAX_BOUNDED_NL_ACTIONS)
    hero_action_index: int = Field(ge=1, lt=MAX_BOUNDED_NL_ACTIONS)
    pot_before_bet_units: int = Field(ge=0)
    opponent_bet_units: int = Field(gt=0)
    call_cost_units: int = Field(gt=0)
    contestable_pot_units: int = Field(gt=0)
    ledger_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    ledger_output_sha256: str = Field(pattern=_SHA256_PATTERN)
    pot_odds_input: BoundedPotOddsInputV1
    pot_odds_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def plan_is_ordered_and_correlated(self) -> BoundedToolPlanV1:
        if self.ordered_tools != BOUNDED_NL_TOOL_ORDER:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.TOOL.value)
        if self.hero_action_index != self.facing_action_index + 1:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.FOCAL_MISMATCH.value)
        expected = self.pot_before_bet_units + self.opponent_bet_units + self.call_cost_units
        if self.contestable_pot_units != expected:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.LEDGER.value)
        return self


class BoundedCandidateProjectionV1(_BoundedModel):
    schema_version: Literal["1.0.0"] = BOUNDED_NL_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.bounded-japanese-nlhe-cash"] = BOUNDED_NL_CONTRACT_ID
    contract_version: Literal["1.0.0"] = BOUNDED_NL_CONTRACT_VERSION
    extractor_id: Literal["poker-deliberation.bounded-japanese-nlhe-cash-parser"] = (
        BOUNDED_NL_EXTRACTOR_ID
    )
    extractor_version: Literal["1.0.0"] = BOUNDED_NL_EXTRACTOR_VERSION
    producer_kind: Literal["deterministic_bounded_parser"] = BOUNDED_NL_PRODUCER_KIND
    external_execution: Literal[False] = False
    analysis_scope: Literal["retrospective"] = "retrospective"
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source: BoundedSourceProvenanceV1
    hand: CanonicalHand
    focal_decision: BoundedFocalDecisionV1
    source_bindings: tuple[BoundedSourceBindingV1, ...] = Field(
        min_length=1,
        max_length=MAX_BOUNDED_NL_BINDINGS,
    )
    source_bindings_sha256: str = Field(pattern=_SHA256_PATTERN)
    extractor_sha256: str = Field(pattern=_SHA256_PATTERN)
    declared_pot_assertions: BoundedDeclaredPotAssertionsV1
    tool_plan: BoundedToolPlanV1

    @model_validator(mode="after")
    def binding_paths_are_unique(self) -> BoundedCandidateProjectionV1:
        paths = [binding.field_path for binding in self.source_bindings]
        if len(paths) != len(set(paths)):
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.DUPLICATE.value)
        if any(
            binding.source_sha256 != self.source.content_sha256 for binding in self.source_bindings
        ):
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFLICT.value)
        return self


class BoundedIntakeCandidateV1(_BoundedModel):
    schema_version: Literal["1.0.0"] = BOUNDED_NL_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.bounded-japanese-nlhe-cash"] = BOUNDED_NL_CONTRACT_ID
    result_version: Literal["1.0.0"] = BOUNDED_NL_RESULT_VERSION
    canonicalization_id: Literal["poker-bounded-nl-candidate-json-v1"] = (
        BOUNDED_NL_CANDIDATE_CANONICALIZATION_ID
    )
    projection: BoundedCandidateProjectionV1
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)


class BoundedIntakePreparationResultV1(_BoundedModel):
    schema_version: Literal["1.0.0"] = BOUNDED_NL_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.bounded-japanese-nlhe-cash"] = BOUNDED_NL_CONTRACT_ID
    result_version: Literal["1.0.0"] = BOUNDED_NL_RESULT_VERSION
    status: Literal["ready", "blocked"]
    source: BoundedSourceProvenanceV1 | None = None
    candidate: BoundedIntakeCandidateV1 | None = None
    partial_extractions: tuple[BoundedPartialExtractionV1, ...] = Field(
        default=(), max_length=MAX_BOUNDED_NL_BINDINGS
    )
    diagnostics: tuple[BoundedNaturalLanguageDiagnosticV1, ...] = Field(
        default=(), max_length=MAX_BOUNDED_NL_DIAGNOSTICS
    )

    @model_validator(mode="after")
    def result_is_closed(self) -> BoundedIntakePreparationResultV1:
        if self.status == "ready":
            if self.source is None or self.candidate is None or self.diagnostics:
                raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFLICT.value)
            if self.source != self.candidate.projection.source:
                raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFLICT.value)
        elif not self.diagnostics or self.candidate is not None:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFLICT.value)
        return self


class BoundedConfirmationAuthorityV1(_BoundedModel):
    authority_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    authority_kind: Literal["local_user", "verified_application"]
    authentication: Literal["self_asserted", "verified"]
    scope: Literal["confirm_bounded_natural_language_projection"] = (
        "confirm_bounded_natural_language_projection"
    )

    @model_validator(mode="after")
    def authentication_matches_kind(self) -> BoundedConfirmationAuthorityV1:
        expected = {
            "local_user": "self_asserted",
            "verified_application": "verified",
        }[self.authority_kind]
        if self.authentication != expected:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_AUTHORITY.value)
        return self


class BoundedIntakeConfirmationV1(_BoundedModel):
    schema_version: Literal["1.0.0"] = BOUNDED_NL_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.bounded-japanese-nlhe-cash"] = BOUNDED_NL_CONTRACT_ID
    contract_version: Literal["1.0.0"] = BOUNDED_NL_CONTRACT_VERSION
    canonicalization_id: Literal["poker-bounded-nl-confirmation-json-v1"] = (
        BOUNDED_NL_CONFIRMATION_CANONICALIZATION_ID
    )
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    confirmation_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    idempotency_key: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_bindings_sha256: str = Field(pattern=_SHA256_PATTERN)
    focal_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    extractor_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_schema_version: Literal["1.0.0"] = BOUNDED_NL_SCHEMA_VERSION
    extractor_id: Literal["poker-deliberation.bounded-japanese-nlhe-cash-parser"] = (
        BOUNDED_NL_EXTRACTOR_ID
    )
    extractor_version: Literal["1.0.0"] = BOUNDED_NL_EXTRACTOR_VERSION
    authority: BoundedConfirmationAuthorityV1
    authority_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmed: Literal[True] = True
    confirmed_at: datetime
    expires_at: datetime
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("confirmed_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING.value)
        return value

    @model_validator(mode="after")
    def time_window_is_supported(self) -> BoundedIntakeConfirmationV1:
        lifetime = (self.expires_at - self.confirmed_at).total_seconds()
        if lifetime <= 0 or lifetime > MAX_BOUNDED_NL_CONFIRMATION_LIFETIME_SECONDS:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_EXPIRED.value)
        return self


class BoundedToolSupportV1(_BoundedModel):
    result_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    tool_name: Literal["hand_validator", "hand_pot_ledger", "pot_odds"]
    tool_version: str = Field(min_length=1, max_length=32)
    contract_version: str = Field(min_length=1, max_length=32)
    status: Literal["success", "failed", "unavailable"]
    epistemic_label: Literal["CALCULATED", "ESTIMATE", "UNKNOWN"]
    input_sha256: str = Field(pattern=_SHA256_PATTERN)
    output_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)


class BoundedAgentSupportV1(_BoundedModel):
    execution_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    agent_role: str = Field(min_length=1, max_length=128)
    provider: Literal["local"]
    provider_version: Literal["1.0.0"]
    status: Literal["completed", "failed", "refused", "fallback"]
    record_sha256: str = Field(pattern=_SHA256_PATTERN)


class BoundedNaturalLanguageProvenanceV1(_BoundedModel):
    schema_version: Literal["1.0.0"] = BOUNDED_NL_SCHEMA_VERSION
    contract_id: Literal["poker-deliberation.bounded-japanese-nlhe-cash"] = BOUNDED_NL_CONTRACT_ID
    result_version: Literal["1.0.0"] = BOUNDED_NL_RESULT_VERSION
    canonicalization_id: Literal["poker-bounded-nl-provenance-json-v1"] = (
        BOUNDED_NL_PROVENANCE_CANONICALIZATION_ID
    )
    report_claim_policy_id: Literal["poker-bounded-nl-claim-policy-v1"] = (
        "poker-bounded-nl-claim-policy-v1"
    )
    provider_narrative_epistemic_label: Literal["UNKNOWN"] = "UNKNOWN"
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    admitted_at: datetime
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_bindings_sha256: str = Field(pattern=_SHA256_PATTERN)
    focal_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    extractor_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    case_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    assignments_sha256: str = Field(pattern=_SHA256_PATTERN)
    agent_reports_sha256: str = Field(pattern=_SHA256_PATTERN)
    final_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_revision_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_revision: int = Field(ge=1)
    terminal_transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{32}$")
    agent_support: tuple[BoundedAgentSupportV1, ...]
    tool_support: tuple[BoundedToolSupportV1, ...]
    terminal_status: Literal["completed", "failed_with_limitations"]
    provenance_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("admitted_at")
    @classmethod
    def admitted_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING.value)
        return value
