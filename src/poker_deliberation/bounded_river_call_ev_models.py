"""Strict additive contracts for the P3-030C bounded river call-EV slice."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.bounded_natural_language_models import (
    BoundedIntakeCandidateV1,
    BoundedSourceProvenanceV1,
)
from poker_deliberation.range_equity_models import (
    RANGE_EQUITY_MAX_EVALUATIONS,
    RANGE_EQUITY_TOOL_PLAN,
    VersionedRangeRiverEquityBindingV1,
    VersionedRangeRiverEquityOracleProjectionV1,
    VersionedRangeRiverEquityResultV1,
    canonical_domain_sha256,
)
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.tools.numeric import close_ulps

BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
BOUNDED_RIVER_CALL_EV_CONTRACT_ID: Literal["poker-bounded-river-call-ev"] = (
    "poker-bounded-river-call-ev"
)
BOUNDED_RIVER_CALL_EV_CONTRACT_VERSION: Literal["1.0.0"] = "1.0.0"
BOUNDED_RIVER_CALL_EV_RESULT_VERSION: Literal["1.0.0"] = "1.0.0"
BOUNDED_RIVER_CALL_EV_MARKER = "bounded_river_call_ev"

BOUNDED_RIVER_CALL_EV_TOOL_ORDER: tuple[
    Literal["hand_validator"],
    Literal["hand_pot_ledger"],
    Literal["pot_odds"],
    Literal["range_validate"],
    Literal["combos"],
    Literal["holdem_equity"],
    Literal["raked_call_ev"],
] = (
    "hand_validator",
    "hand_pot_ledger",
    "pot_odds",
    "range_validate",
    "combos",
    "holdem_equity",
    "raked_call_ev",
)
BOUNDED_RIVER_CALL_EV_TOOL_ALLOWLIST = frozenset(BOUNDED_RIVER_CALL_EV_TOOL_ORDER)

SOURCE_HASH_DOMAIN = "poker-bounded-river-call-ev-source-bytes-v1"
BOUNDED_CANDIDATE_HASH_DOMAIN = "poker-bounded-river-call-ev-bounded-candidate-json-v1"
SOURCE_BINDINGS_HASH_DOMAIN = "poker-bounded-river-call-ev-source-bindings-json-v1"
FOCAL_HASH_DOMAIN = "poker-bounded-river-call-ev-focal-json-v1"
EXTRACTOR_HASH_DOMAIN = "poker-bounded-river-call-ev-extractor-json-v1"
TOOL_PLAN_HASH_DOMAIN = "poker-bounded-river-call-ev-tool-plan-json-v1"
RANGE_DEFINITION_HASH_DOMAIN = "poker-bounded-river-call-ev-range-definition-json-v1"
RANGE_TARGET_HASH_DOMAIN = "poker-bounded-river-call-ev-range-target-json-v1"
RANGE_BINDING_HASH_DOMAIN = "poker-bounded-river-call-ev-range-binding-json-v1"
EQUITY_MODEL_HASH_DOMAIN = "poker-bounded-river-call-ev-equity-model-json-v1"
CALL_EV_MODEL_HASH_DOMAIN = "poker-bounded-river-call-ev-call-ev-model-json-v1"
CANDIDATE_HASH_DOMAIN = "poker-bounded-river-call-ev-candidate-json-v1"
AUTHORITY_HASH_DOMAIN = "poker-bounded-river-call-ev-authority-json-v1"
CONFIRMATION_HASH_DOMAIN = "poker-bounded-river-call-ev-confirmation-json-v1"
BINDING_HASH_DOMAIN = "poker-bounded-river-call-ev-binding-json-v1"
RESULT_HASH_DOMAIN = "poker-bounded-river-call-ev-result-json-v1"
PROVENANCE_HASH_DOMAIN = "poker-bounded-river-call-ev-provenance-json-v1"
ADMISSION_RECORD_HASH_DOMAIN = "poker-bounded-river-call-ev-admission-record-json-v1"
TOOL_RESULT_HASH_DOMAIN = "poker-bounded-river-call-ev-tool-result-json-v1"

BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT = "bounded_river_call_ev_source.txt"
BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT = "bounded_river_call_ev_candidate.json"
BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT = "bounded_river_call_ev_confirmation.json"
BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT = "bounded_river_call_ev_range.json"
BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT = "bounded_river_call_ev_binding.json"
BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT = "bounded_river_call_ev_result.json"
BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT = "bounded_river_call_ev_provenance.json"

BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT_SCHEMA = "poker-bounded-river-call-ev-source-artifact-v1"
BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT_SCHEMA = (
    "poker-bounded-river-call-ev-candidate-artifact-v1"
)
BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT_SCHEMA = (
    "poker-bounded-river-call-ev-confirmation-artifact-v1"
)
BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT_SCHEMA = "poker-bounded-river-call-ev-range-artifact-v1"
BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT_SCHEMA = "poker-bounded-river-call-ev-binding-artifact-v1"
BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT_SCHEMA = "poker-bounded-river-call-ev-result-artifact-v1"
BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT_SCHEMA = (
    "poker-bounded-river-call-ev-provenance-artifact-v1"
)
BOUNDED_RIVER_CALL_EV_ADMISSION_RECORD_SCHEMA = "poker-bounded-river-call-ev-admission-record-v1"

MAX_BOUNDED_RIVER_CALL_EV_ARTIFACT_BYTES = 1_500_000
MAX_BOUNDED_RIVER_CALL_EV_RUN_BYTES = 15_000_000
MAX_BOUNDED_RIVER_CALL_EV_CONFIRMATION_LIFETIME_SECONDS = 86_400

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PORTABLE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_FIELD_PATTERN = r"^[A-Za-z][A-Za-z0-9_.\[\]-]{0,255}$"


class _BoundedRiverModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class BoundedRiverCallEvDiagnosticCode(StrEnum):
    SCHEMA = "BRC_E_SCHEMA"
    SOURCE = "BRC_E_SOURCE"
    CANDIDATE = "BRC_E_CANDIDATE"
    FOCAL = "BRC_E_FOCAL"
    UNSUPPORTED = "BRC_E_UNSUPPORTED"
    RANGE = "BRC_E_RANGE"
    TARGET = "BRC_E_TARGET"
    TOOL_PLAN = "BRC_E_TOOL_PLAN"
    LEDGER = "BRC_E_LEDGER"
    ORACLE = "BRC_E_ORACLE"
    NUMERIC = "BRC_E_NUMERIC"
    CONFIRMATION_BINDING = "BRC_E_CONFIRMATION_BINDING"
    CONFIRMATION_AUTHORITY = "BRC_E_CONFIRMATION_AUTHORITY"
    CONFIRMATION_EXPIRED = "BRC_E_CONFIRMATION_EXPIRED"
    CONFIRMATION_REPLAY = "BRC_E_CONFIRMATION_REPLAY"
    CONTEXT = "BRC_E_CONTEXT"
    LOCAL_PROVIDER = "BRC_E_LOCAL_PROVIDER"
    BUDGET = "BRC_E_BUDGET"
    STORAGE = "BRC_E_STORAGE"
    REPLAY = "BRC_E_REPLAY"


class BoundedRiverCallEvDiagnosticV1(_BoundedRiverModel):
    code: BoundedRiverCallEvDiagnosticCode
    field_path: str = Field(pattern=_FIELD_PATTERN)


class ExactRationalV1(_BoundedRiverModel):
    numerator: int
    denominator: int = Field(gt=0)

    @model_validator(mode="after")
    def reduced(self) -> ExactRationalV1:
        value = Fraction(self.numerator, self.denominator)
        if (value.numerator, value.denominator) != (self.numerator, self.denominator):
            raise ValueError(f"{BoundedRiverCallEvDiagnosticCode.ORACLE.value}: unreduced fraction")
        return self


class BoundedRiverRangeTargetBindingV1(_BoundedRiverModel):
    hero_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    facing_actor: str = Field(pattern=_PORTABLE_ID_PATTERN)
    target_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    facing_action_index: int = Field(ge=0, le=511)
    as_of_action_index: int = Field(ge=1, le=512)
    action_prefix_sha256: str = Field(pattern=_SHA256_PATTERN)
    eligible_player_ids: tuple[str, str]

    @model_validator(mode="after")
    def target_and_prefix_match(self) -> BoundedRiverRangeTargetBindingV1:
        if (
            self.facing_actor != self.target_player_id
            or self.as_of_action_index != self.facing_action_index + 1
            or set(self.eligible_player_ids) != {self.hero_player_id, self.target_player_id}
        ):
            raise ValueError(f"{BoundedRiverCallEvDiagnosticCode.TARGET.value}: target mismatch")
        return self


class BoundedRiverEquityModelV1(_BoundedRiverModel):
    model_id: Literal["explicit-single-range-heads-up-river-exact-v1"] = (
        "explicit-single-range-heads-up-river-exact-v1"
    )
    range_equity_contract_id: Literal["poker-versioned-range-river-equity"] = (
        "poker-versioned-range-river-equity"
    )
    range_equity_contract_version: Literal["1.0.0"] = "1.0.0"
    exact_only: Literal[True] = True
    exact_evaluation_cap: Literal[990] = RANGE_EQUITY_MAX_EVALUATIONS
    tool_plan: tuple[
        Literal["range_validate"],
        Literal["combos"],
        Literal["holdem_equity"],
    ] = RANGE_EQUITY_TOOL_PLAN
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    oracle_sha256: str = Field(pattern=_SHA256_PATTERN)
    equity: ExactRationalV1
    source_content_status: Literal["USER_CLAIM", "ASSUMPTION"]
    legacy_equity_max_ulps: Literal[128] = 128


class BoundedRiverCallEvModelV1(_BoundedRiverModel):
    model_id: Literal["single-river-decision-no-future-betting-no-rake-v1"] = (
        "single-river-decision-no-future-betting-no-rake-v1"
    )
    fold_ev_reference: Literal["focal-decision-zero"] = "focal-decision-zero"
    chip_unit: ExactRationalV1
    pot_before_bet_units: int = Field(ge=0)
    opponent_bet_units: int = Field(gt=0)
    pot_after_bet_units: int = Field(gt=0)
    call_cost_units: int = Field(gt=0)
    contestable_pot_units: int = Field(gt=0)
    equity: ExactRationalV1
    required_equity: ExactRationalV1
    call_ev_units: ExactRationalV1
    call_ev_amount: ExactRationalV1
    fold_ev_units: ExactRationalV1
    call_minus_fold_ev_units: ExactRationalV1
    action_comparison: Literal["call", "fold", "tie"]
    rake_percent: float = Field(default=0.0, ge=0.0, le=0.0, allow_inf_nan=False)
    rake_cap: Literal[None] = None
    no_future_betting: Literal[True] = True
    pot_odds_max_ulps: Literal[16] = 16
    raked_call_ev_max_ulps: Literal[32] = 32

    @model_validator(mode="after")
    def exact_formulas_match(self) -> BoundedRiverCallEvModelV1:
        equity = Fraction(self.equity.numerator, self.equity.denominator)
        required = Fraction(self.call_cost_units, self.contestable_pot_units)
        call_ev = equity * self.contestable_pot_units - self.call_cost_units
        chip_unit = Fraction(self.chip_unit.numerator, self.chip_unit.denominator)
        if self.pot_after_bet_units != self.pot_before_bet_units + self.opponent_bet_units:
            raise ValueError(f"{BoundedRiverCallEvDiagnosticCode.LEDGER.value}: pot mismatch")
        if self.contestable_pot_units != self.pot_after_bet_units + self.call_cost_units:
            raise ValueError(f"{BoundedRiverCallEvDiagnosticCode.LEDGER.value}: pot mismatch")
        expected = (
            (required.numerator, required.denominator),
            (call_ev.numerator, call_ev.denominator),
            ((call_ev * chip_unit).numerator, (call_ev * chip_unit).denominator),
            (0, 1),
            (call_ev.numerator, call_ev.denominator),
        )
        actual = tuple(
            (item.numerator, item.denominator)
            for item in (
                self.required_equity,
                self.call_ev_units,
                self.call_ev_amount,
                self.fold_ev_units,
                self.call_minus_fold_ev_units,
            )
        )
        comparison = "call" if call_ev > 0 else "fold" if call_ev < 0 else "tie"
        if actual != expected or self.action_comparison != comparison:
            raise ValueError(f"{BoundedRiverCallEvDiagnosticCode.ORACLE.value}: EV mismatch")
        return self


class BoundedRiverCallEvCandidateProjectionV1(_BoundedRiverModel):
    schema_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-river-call-ev"] = BOUNDED_RIVER_CALL_EV_CONTRACT_ID
    contract_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_CONTRACT_VERSION
    analysis_scope: Literal["retrospective"] = "retrospective"
    external_execution: Literal[False] = False
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    bounded_candidate: BoundedIntakeCandidateV1
    range_definition: VersionedRangeDefinitionV1
    range_target: BoundedRiverRangeTargetBindingV1
    range_equity_binding: VersionedRangeRiverEquityBindingV1
    range_equity_oracle: VersionedRangeRiverEquityOracleProjectionV1
    equity_model: BoundedRiverEquityModelV1
    call_ev_model: BoundedRiverCallEvModelV1
    ordered_tools: tuple[
        Literal["hand_validator"],
        Literal["hand_pot_ledger"],
        Literal["pot_odds"],
        Literal["range_validate"],
        Literal["combos"],
        Literal["holdem_equity"],
        Literal["raked_call_ev"],
    ] = BOUNDED_RIVER_CALL_EV_TOOL_ORDER
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    bounded_candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_bindings_sha256: str = Field(pattern=_SHA256_PATTERN)
    focal_sha256: str = Field(pattern=_SHA256_PATTERN)
    extractor_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_target_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    equity_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    call_ev_model_sha256: str = Field(pattern=_SHA256_PATTERN)


class BoundedRiverCallEvCandidateV1(_BoundedRiverModel):
    schema_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-river-call-ev"] = BOUNDED_RIVER_CALL_EV_CONTRACT_ID
    result_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_RESULT_VERSION
    canonicalization_id: Literal["poker-bounded-river-call-ev-candidate-json-v1"] = (
        "poker-bounded-river-call-ev-candidate-json-v1"
    )
    projection: BoundedRiverCallEvCandidateProjectionV1
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)


class BoundedRiverCallEvPreparationResultV1(_BoundedRiverModel):
    schema_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-river-call-ev"] = BOUNDED_RIVER_CALL_EV_CONTRACT_ID
    result_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_RESULT_VERSION
    status: Literal["ready", "blocked"]
    source: BoundedSourceProvenanceV1 | None = None
    candidate: BoundedRiverCallEvCandidateV1 | None = None
    diagnostics: tuple[BoundedRiverCallEvDiagnosticV1, ...] = ()

    @model_validator(mode="after")
    def closed_shape(self) -> BoundedRiverCallEvPreparationResultV1:
        if self.status == "ready":
            if self.source is None or self.candidate is None or self.diagnostics:
                raise ValueError(BoundedRiverCallEvDiagnosticCode.SCHEMA.value)
        elif self.candidate is not None or not self.diagnostics:
            raise ValueError(BoundedRiverCallEvDiagnosticCode.SCHEMA.value)
        return self


class BoundedRiverCallEvConfirmationAuthorityV1(_BoundedRiverModel):
    authority_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    authority_kind: Literal["local_user", "verified_application"]
    authentication: Literal["self_asserted", "verified"]
    scope: Literal["confirm_bounded_river_call_ev_projection"] = (
        "confirm_bounded_river_call_ev_projection"
    )

    @model_validator(mode="after")
    def authentication_matches_kind(self) -> BoundedRiverCallEvConfirmationAuthorityV1:
        expected = {"local_user": "self_asserted", "verified_application": "verified"}[
            self.authority_kind
        ]
        if self.authentication != expected:
            raise ValueError(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_AUTHORITY.value)
        return self


class BoundedRiverCallEvConfirmationV1(_BoundedRiverModel):
    schema_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-river-call-ev"] = BOUNDED_RIVER_CALL_EV_CONTRACT_ID
    contract_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_CONTRACT_VERSION
    canonicalization_id: Literal["poker-bounded-river-call-ev-confirmation-json-v1"] = (
        "poker-bounded-river-call-ev-confirmation-json-v1"
    )
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    confirmation_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    idempotency_key: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    bounded_candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_bindings_sha256: str = Field(pattern=_SHA256_PATTERN)
    focal_sha256: str = Field(pattern=_SHA256_PATTERN)
    extractor_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_target_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    equity_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    call_ev_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    authority: BoundedRiverCallEvConfirmationAuthorityV1
    authority_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmed: Literal[True] = True
    confirmed_at: datetime
    expires_at: datetime
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("confirmed_at", "expires_at")
    @classmethod
    def timezone_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING.value)
        return value

    @model_validator(mode="after")
    def supported_lifetime(self) -> BoundedRiverCallEvConfirmationV1:
        lifetime = (self.expires_at - self.confirmed_at).total_seconds()
        if lifetime <= 0 or lifetime > MAX_BOUNDED_RIVER_CALL_EV_CONFIRMATION_LIFETIME_SECONDS:
            raise ValueError(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_EXPIRED.value)
        return self


class BoundedRiverCallEvBindingV1(_BoundedRiverModel):
    schema_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-river-call-ev"] = BOUNDED_RIVER_CALL_EV_CONTRACT_ID
    contract_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_CONTRACT_VERSION
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    bounded_candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_bindings_sha256: str = Field(pattern=_SHA256_PATTERN)
    focal_sha256: str = Field(pattern=_SHA256_PATTERN)
    extractor_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_target_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    equity_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    call_ev_model_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    ordered_tools: tuple[
        Literal["hand_validator"],
        Literal["hand_pot_ledger"],
        Literal["pot_odds"],
        Literal["range_validate"],
        Literal["combos"],
        Literal["holdem_equity"],
        Literal["raked_call_ev"],
    ] = BOUNDED_RIVER_CALL_EV_TOOL_ORDER
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def binding_hash_matches(self) -> BoundedRiverCallEvBindingV1:
        payload = self.model_dump(mode="json")
        payload.pop("binding_sha256")
        if self.binding_sha256 != canonical_domain_sha256(BINDING_HASH_DOMAIN, payload):
            raise ValueError(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING.value)
        return self


class BoundedRiverCallEvAdmissionRecordV1(_BoundedRiverModel):
    schema_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION
    record_schema: Literal["poker-bounded-river-call-ev-admission-record-v1"] = (
        "poker-bounded-river-call-ev-admission-record-v1"
    )
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_plan: tuple[
        Literal["hand_validator"],
        Literal["hand_pot_ledger"],
        Literal["pot_odds"],
        Literal["range_validate"],
        Literal["combos"],
        Literal["holdem_equity"],
        Literal["raked_call_ev"],
    ] = BOUNDED_RIVER_CALL_EV_TOOL_ORDER
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def record_hash_matches(self) -> BoundedRiverCallEvAdmissionRecordV1:
        payload = self.model_dump(mode="json")
        payload.pop("record_sha256")
        if self.record_sha256 != canonical_domain_sha256(ADMISSION_RECORD_HASH_DOMAIN, payload):
            raise ValueError(BoundedRiverCallEvDiagnosticCode.REPLAY.value)
        return self


class BoundedRiverToolSupportV1(_BoundedRiverModel):
    result_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    tool_name: Literal[
        "hand_validator",
        "hand_pot_ledger",
        "pot_odds",
        "range_validate",
        "combos",
        "holdem_equity",
        "raked_call_ev",
    ]
    status: Literal["success", "failed", "unavailable"]
    result_sha256: str = Field(pattern=_SHA256_PATTERN)


class BoundedRiverCallEvResultV1(_BoundedRiverModel):
    schema_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-river-call-ev"] = BOUNDED_RIVER_CALL_EV_CONTRACT_ID
    result_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_RESULT_VERSION
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_equity_result: VersionedRangeRiverEquityResultV1
    equity: ExactRationalV1
    required_equity: ExactRationalV1
    call_ev_units: ExactRationalV1
    call_ev_amount: ExactRationalV1
    fold_ev_units: ExactRationalV1
    call_minus_fold_ev_units: ExactRationalV1
    action_comparison: Literal["call", "fold", "tie"]
    equity_binary64: float = Field(ge=0, le=1, allow_inf_nan=False)
    required_equity_binary64: float = Field(ge=0, le=1, allow_inf_nan=False)
    call_ev_binary64: float = Field(allow_inf_nan=False)
    range_source_status: Literal["USER_CLAIM", "ASSUMPTION"]
    comparison_epistemic_label: Literal["CALCULATED"] = "CALCULATED"
    strategic_interpretation_label: Literal["INFERENCE"] = "INFERENCE"
    practical_range_accuracy: Literal["UNKNOWN"] = "UNKNOWN"
    tool_support: tuple[BoundedRiverToolSupportV1, ...]
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def exact_and_binary_projections_match(self) -> BoundedRiverCallEvResultV1:
        exact_equity = Fraction(self.equity.numerator, self.equity.denominator)
        exact_required = Fraction(
            self.required_equity.numerator,
            self.required_equity.denominator,
        )
        exact_ev = Fraction(self.call_ev_amount.numerator, self.call_ev_amount.denominator)
        bridge = self.range_equity_result
        if (
            (bridge.equity_numerator, bridge.equity_denominator)
            != (exact_equity.numerator, exact_equity.denominator)
            or not close_ulps(self.equity_binary64, float(exact_equity), ulps=128)
            or not close_ulps(self.required_equity_binary64, float(exact_required), ulps=16)
            or not close_ulps(self.call_ev_binary64, float(exact_ev), ulps=32)
            or tuple(item.tool_name for item in self.tool_support)
            != BOUNDED_RIVER_CALL_EV_TOOL_ORDER
        ):
            raise ValueError(BoundedRiverCallEvDiagnosticCode.NUMERIC.value)
        payload = self.model_dump(mode="json")
        payload.pop("result_sha256")
        if self.result_sha256 != canonical_domain_sha256(RESULT_HASH_DOMAIN, payload):
            raise ValueError(BoundedRiverCallEvDiagnosticCode.REPLAY.value)
        return self


class BoundedRiverCallEvProvenanceV1(_BoundedRiverModel):
    schema_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-river-call-ev"] = BOUNDED_RIVER_CALL_EV_CONTRACT_ID
    result_version: Literal["1.0.0"] = BOUNDED_RIVER_CALL_EV_RESULT_VERSION
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    admitted_at: datetime
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_equity_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)
    case_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    assignments_sha256: str = Field(pattern=_SHA256_PATTERN)
    agent_reports_sha256: str = Field(pattern=_SHA256_PATTERN)
    execution_records_sha256: str = Field(pattern=_SHA256_PATTERN)
    final_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_revision_root_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_revision: int = Field(ge=1)
    terminal_transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{32}$")
    terminal_status: Literal["completed", "failed_with_limitations"]
    provenance_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("admitted_at")
    @classmethod
    def admitted_at_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(BoundedRiverCallEvDiagnosticCode.REPLAY.value)
        return value

    @model_validator(mode="after")
    def provenance_hash_matches(self) -> BoundedRiverCallEvProvenanceV1:
        payload = self.model_dump(mode="json")
        payload.pop("provenance_sha256")
        if self.provenance_sha256 != canonical_domain_sha256(PROVENANCE_HASH_DOMAIN, payload):
            raise ValueError(BoundedRiverCallEvDiagnosticCode.REPLAY.value)
        return self


__all__ = [name for name in globals() if name.startswith("BOUNDED_RIVER_CALL_EV_")]
__all__ += [
    "ADMISSION_RECORD_HASH_DOMAIN",
    "AUTHORITY_HASH_DOMAIN",
    "BINDING_HASH_DOMAIN",
    "BOUNDED_CANDIDATE_HASH_DOMAIN",
    "CALL_EV_MODEL_HASH_DOMAIN",
    "CANDIDATE_HASH_DOMAIN",
    "CONFIRMATION_HASH_DOMAIN",
    "EQUITY_MODEL_HASH_DOMAIN",
    "EXTRACTOR_HASH_DOMAIN",
    "FOCAL_HASH_DOMAIN",
    "MAX_BOUNDED_RIVER_CALL_EV_ARTIFACT_BYTES",
    "MAX_BOUNDED_RIVER_CALL_EV_CONFIRMATION_LIFETIME_SECONDS",
    "MAX_BOUNDED_RIVER_CALL_EV_RUN_BYTES",
    "PROVENANCE_HASH_DOMAIN",
    "RANGE_BINDING_HASH_DOMAIN",
    "RANGE_DEFINITION_HASH_DOMAIN",
    "RANGE_TARGET_HASH_DOMAIN",
    "RESULT_HASH_DOMAIN",
    "SOURCE_BINDINGS_HASH_DOMAIN",
    "SOURCE_HASH_DOMAIN",
    "TOOL_PLAN_HASH_DOMAIN",
    "TOOL_RESULT_HASH_DOMAIN",
    "BoundedRiverCallEvAdmissionRecordV1",
    "BoundedRiverCallEvBindingV1",
    "BoundedRiverCallEvCandidateProjectionV1",
    "BoundedRiverCallEvCandidateV1",
    "BoundedRiverCallEvConfirmationAuthorityV1",
    "BoundedRiverCallEvConfirmationV1",
    "BoundedRiverCallEvDiagnosticCode",
    "BoundedRiverCallEvDiagnosticV1",
    "BoundedRiverCallEvModelV1",
    "BoundedRiverCallEvPreparationResultV1",
    "BoundedRiverCallEvProvenanceV1",
    "BoundedRiverCallEvResultV1",
    "BoundedRiverEquityModelV1",
    "BoundedRiverRangeTargetBindingV1",
    "BoundedRiverToolSupportV1",
    "ExactRationalV1",
]
