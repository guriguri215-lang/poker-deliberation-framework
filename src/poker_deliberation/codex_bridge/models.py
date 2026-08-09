"""Strict additive contracts for the bounded actual Codex/Python bridge."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.codex_bridge.canonical import domain_sha256, without_field

BRIDGE_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
BRIDGE_CONTRACT_ID: Final[Literal["poker-bounded-codex-review-bridge"]] = (
    "poker-bounded-codex-review-bridge"
)
BRIDGE_CONTRACT_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
BRIDGE_CANONICALIZATION_ID: Final[Literal["poker-bounded-codex-bridge-json-v1"]] = (
    "poker-bounded-codex-bridge-json-v1"
)
AUTH_MODE_CONTRACT_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
BRIDGE_LOCAL_RUNTIME_ID: Final = "poker-deliberation-local/0.1.0"
BRIDGE_SUBSCRIPTION_RUNTIME_ID: Final = "openai-codex-cli/0.144.4"
BRIDGE_OPENAI_API_RUNTIME_ID: Final = "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4"
# Compatibility name for callers that need the standard remote runtime.  It is
# deliberately the subscription runtime, never an API fallback.
BRIDGE_RUNTIME_ID: Final = BRIDGE_SUBSCRIPTION_RUNTIME_ID
BRIDGE_RUNTIME_BINARY_SHA256: Final = (
    "51398051c2332b6afe08dc3b9dbb4056085c197f35ca57a307ee303d450cada5"
)
BRIDGE_MODEL_ID: Final = "gpt-5.6-terra"
BRIDGE_LOCAL_PROVIDER_ID: Final = "local_provider"
BRIDGE_SUBSCRIPTION_PROVIDER_ID: Final = "openai"
BRIDGE_OPENAI_API_PROVIDER_ID: Final = "openai_responses_api_no_retry"
BRIDGE_MODEL_PROVIDER_ID: Final = BRIDGE_SUBSCRIPTION_PROVIDER_ID
BRIDGE_REASONING_EFFORT: Final = "medium"
BRIDGE_SERVICE_TIER: Final = "default"
BRIDGE_LOCAL_CREDENTIAL_REFERENCE: Final = "none"
BRIDGE_SUBSCRIPTION_CREDENTIAL_REFERENCE: Final = "codex_home:saved_chatgpt_login"
BRIDGE_OPENAI_API_CREDENTIAL_REFERENCE: Final = "env:OPENAI_API_KEY"
BRIDGE_CREDENTIAL_REFERENCE: Final = BRIDGE_SUBSCRIPTION_CREDENTIAL_REFERENCE

REQUEST_HASH_DOMAIN: Final = "poker-bounded-codex-bridge-request-v1"
CONTEXT_HASH_DOMAIN: Final = "poker-bounded-codex-bridge-context-v1"
RESULT_HASH_DOMAIN: Final = "poker-bounded-codex-bridge-role-result-v1"
EXECUTION_AUDIT_HASH_DOMAIN: Final = "poker-bounded-codex-bridge-execution-audit-v2"
EXECUTION_AUDIT_SCHEMA_VERSION: Final[Literal["2.0.0"]] = "2.0.0"
CONFIRMATION_HASH_DOMAIN: Final = "poker-bounded-codex-bridge-confirmation-v1"
ADMISSION_HASH_DOMAIN: Final = "poker-bounded-codex-bridge-admission-v1"
RUN_PLAN_HASH_DOMAIN: Final = "poker-bounded-codex-bridge-run-plan-v1"
TERMINAL_MANIFEST_HASH_DOMAIN: Final = "poker-bounded-codex-bridge-terminal-manifest-v1"
EXECUTION_IDENTITY_CLAIM_HASH_DOMAIN: Final = (
    "poker-bounded-codex-bridge-execution-identity-claim-v1"
)
CONFIRMATION_IDENTIFIER_CLAIM_HASH_DOMAIN: Final = (
    "poker-bounded-codex-bridge-confirmation-identifier-claim-v1"
)
OBSERVED_TRANSPORT_IDENTITY_HASH_DOMAIN: Final = (
    "poker-bounded-codex-bridge-observed-transport-identity-v1"
)
SUBSCRIPTION_EXECUTION_RUNTIME_HASH_DOMAIN: Final = (
    "poker-bounded-codex-subscription-execution-runtime-v1"
)
SUBSCRIPTION_SEALED_LIVE_ATTESTATION_HASH_DOMAIN: Final = (
    "poker-bounded-codex-subscription-sealed-live-execution-attestation-v1"
)
SUBSCRIPTION_USAGE_HASH_DOMAIN: Final = "poker-bounded-codex-subscription-usage-v1"

MAX_CONTEXT_BYTES: Final = 65_536
MAX_RESPONSE_BYTES: Final = 32_768
MAX_STREAM_BYTES: Final = 262_144
MAX_ROLE_RUNTIME_MS: Final = 120_000
MAX_RUN_RUNTIME_MS: Final = 600_000
MAX_INPUT_TOKENS_PER_ROLE: Final = 24_000
MAX_OUTPUT_TOKENS_PER_ROLE: Final = 6_000
MAX_TOTAL_INPUT_TOKENS: Final = 120_000
MAX_TOTAL_OUTPUT_TOKENS: Final = 30_000
MAX_RESERVED_COST_MICRO_USD: Final = 1_020_000
MAX_ASSIGNMENT_LIFETIME_SECONDS: Final = 604_800
MAX_CONFIRMATION_LIFETIME_SECONDS: Final = 900

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CLAIM_ID = re.compile(r"^claim-(?:0[1-9]|1[0-6])$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$")
_CARD = re.compile(r"\b[2-9TJQKA][cdhs]\b")
_RANGE_CLASS = re.compile(r"\b[2-9TJQKA]{2}(?:s|o|\+)?\b")
_NARRATIVE_NUMBER = re.compile(
    r"[0-9]|%|[$€£¥]|\b(?:one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.I,
)
_CITATION = re.compile(r"https?://|doi:|\[[0-9]+\]", re.I)
_SOLVER_CLAIM = re.compile(r"\bsolver\b", re.I)
_UNSUPPORTED_STRATEGY = re.compile(
    r"\b(?:gto|equilibrium|solver-derived|always|must|unconditionally)\b|必ず|常に|無条件|均衡",
    re.I,
)
_SECRET = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|"
    r"(?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*[^\s,;]+)",
    re.I,
)
_PROMPT_INJECTION_ID = re.compile(
    r"(?:ignore|override|bypass)[._:-]*(?:previous|prior|all)?[._:-]*"
    r"(?:instruction|prompt|policy)|(?:system|developer)[._:-]*(?:prompt|message)|jailbreak",
    re.I,
)


def _safe_control(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("bridge text must be NFC")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("bridge text cannot contain control characters")
    if _SECRET.search(value):
        raise ValueError("bridge text cannot contain a secret-shaped value")
    return value


def _safe_identifier(value: str) -> str:
    _safe_control(value)
    if _PROMPT_INJECTION_ID.search(value):
        raise ValueError("bridge identifier contains a prompt-injection-shaped value")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


def _sorted_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    expected = tuple(sorted(set(values), key=lambda item: item.encode("utf-8")))
    if values != expected:
        raise ValueError(f"{field_name} must be UTF-8 sorted and unique")
    return values


PortableId = Annotated[str, Field(pattern=_PORTABLE_ID.pattern), AfterValidator(_safe_identifier)]
ClaimId = Annotated[str, Field(pattern=_CLAIM_ID.pattern)]
Sha256 = Annotated[str, Field(pattern=_SHA256.pattern)]
GitObjectId = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
Version = Annotated[str, Field(pattern=_VERSION.pattern), AfterValidator(_safe_control)]
BoundedText = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_safe_control),
]
SAFE_INFERENCE_NARRATIVE: Final = (
    "The supplied evidence supports only the bounded comparison under its stated assumption."
)
SAFE_UNKNOWN_NARRATIVE: Final = (
    "Practical applicability remains unknown under the supplied assumption."
)
Narrative = Literal[
    "The supplied evidence supports only the bounded comparison under its stated assumption.",
    "Practical applicability remains unknown under the supplied assumption.",
]


class _BridgeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class RuntimeAuthModeV1(StrEnum):
    """Explicit runtime/auth selection; environment variables never select it."""

    LOCAL_ONLY = "local_only"
    CODEX_SUBSCRIPTION = "codex_subscription"
    OPENAI_API = "openai_api"


def _mode_core_identity(mode: RuntimeAuthModeV1) -> tuple[str, str | None, str, str]:
    return {
        RuntimeAuthModeV1.LOCAL_ONLY: (
            BRIDGE_LOCAL_RUNTIME_ID,
            None,
            BRIDGE_LOCAL_PROVIDER_ID,
            BRIDGE_LOCAL_CREDENTIAL_REFERENCE,
        ),
        RuntimeAuthModeV1.CODEX_SUBSCRIPTION: (
            BRIDGE_SUBSCRIPTION_RUNTIME_ID,
            BRIDGE_MODEL_ID,
            BRIDGE_SUBSCRIPTION_PROVIDER_ID,
            BRIDGE_SUBSCRIPTION_CREDENTIAL_REFERENCE,
        ),
        RuntimeAuthModeV1.OPENAI_API: (
            BRIDGE_OPENAI_API_RUNTIME_ID,
            BRIDGE_MODEL_ID,
            BRIDGE_OPENAI_API_PROVIDER_ID,
            BRIDGE_OPENAI_API_CREDENTIAL_REFERENCE,
        ),
    }[mode]


class BridgeRole(StrEnum):
    STRATEGY_ANALYST = "strategy-analyst"
    MATH_TOOL_AUDITOR = "math-tool-auditor"
    SKEPTIC_FALSIFIER = "skeptic-falsifier"
    ADJUDICATOR = "adjudicator"
    REPORT_WRITER = "report-writer"


BRIDGE_ROLE_ORDER: Final = (
    BridgeRole.STRATEGY_ANALYST,
    BridgeRole.MATH_TOOL_AUDITOR,
    BridgeRole.SKEPTIC_FALSIFIER,
    BridgeRole.ADJUDICATOR,
    BridgeRole.REPORT_WRITER,
)


class BridgeEffectState(StrEnum):
    NOT_LAUNCHED = "not_launched"
    LAUNCHED = "launched"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    CANCEL_UNCONFIRMED = "cancel_unconfirmed"
    EFFECT_UNKNOWN = "effect_unknown"


class BridgeEpistemicLabel(StrEnum):
    INFERENCE = "INFERENCE"
    UNKNOWN = "UNKNOWN"


class BridgeConclusionCode(StrEnum):
    STRATEGY_OBSERVATION = "strategy_observation"
    NO_UNCONDITIONAL_RECOMMENDATION = "no_unconditional_recommendation"
    MATH_CONSISTENT = "math_consistent"
    MATH_INCONSISTENCY = "math_inconsistency"
    NUMERIC_CLAIM_REFUSED = "numeric_claim_refused"
    COUNTEREXAMPLE = "counterexample"
    MISSING_PREMISE = "missing_premise"
    RANGE_SENSITIVITY_UNKNOWN = "range_sensitivity_unknown"
    ADJUDICATED_SUPPORT = "adjudicated_support"
    ADJUDICATED_LIMITED = "adjudicated_limited"
    ADJUDICATED_UNKNOWN = "adjudicated_unknown"
    REPORT_BOUND = "report_bound"
    REPORT_LIMITED = "report_limited"


_ROLE_CODES: Final[dict[BridgeRole, frozenset[BridgeConclusionCode]]] = {
    BridgeRole.STRATEGY_ANALYST: frozenset(
        {
            BridgeConclusionCode.STRATEGY_OBSERVATION,
            BridgeConclusionCode.NO_UNCONDITIONAL_RECOMMENDATION,
        }
    ),
    BridgeRole.MATH_TOOL_AUDITOR: frozenset(
        {
            BridgeConclusionCode.MATH_CONSISTENT,
            BridgeConclusionCode.MATH_INCONSISTENCY,
            BridgeConclusionCode.NUMERIC_CLAIM_REFUSED,
        }
    ),
    BridgeRole.SKEPTIC_FALSIFIER: frozenset(
        {
            BridgeConclusionCode.COUNTEREXAMPLE,
            BridgeConclusionCode.MISSING_PREMISE,
            BridgeConclusionCode.RANGE_SENSITIVITY_UNKNOWN,
        }
    ),
    BridgeRole.ADJUDICATOR: frozenset(
        {
            BridgeConclusionCode.ADJUDICATED_SUPPORT,
            BridgeConclusionCode.ADJUDICATED_LIMITED,
            BridgeConclusionCode.ADJUDICATED_UNKNOWN,
        }
    ),
    BridgeRole.REPORT_WRITER: frozenset(
        {
            BridgeConclusionCode.REPORT_BOUND,
            BridgeConclusionCode.REPORT_LIMITED,
        }
    ),
}

_ROLE_EVIDENCE_RULES: Final[
    dict[
        BridgeRole,
        Literal[
            "any_required_evidence",
            "all_three_parent_role_results",
            "adjudication_parent_only",
        ],
    ]
] = {
    BridgeRole.STRATEGY_ANALYST: "any_required_evidence",
    BridgeRole.MATH_TOOL_AUDITOR: "any_required_evidence",
    BridgeRole.SKEPTIC_FALSIFIER: "any_required_evidence",
    BridgeRole.ADJUDICATOR: "all_three_parent_role_results",
    BridgeRole.REPORT_WRITER: "adjudication_parent_only",
}


def allowed_conclusion_codes(role: BridgeRole) -> tuple[BridgeConclusionCode, ...]:
    return tuple(sorted(_ROLE_CODES[role], key=lambda item: item.value))


def claim_evidence_rule(
    role: BridgeRole,
) -> Literal[
    "any_required_evidence",
    "all_three_parent_role_results",
    "adjudication_parent_only",
]:
    return _ROLE_EVIDENCE_RULES[role]


class ExactRationalV1(_BridgeModel):
    numerator: int
    denominator: int = Field(gt=0)

    @model_validator(mode="after")
    def reduced(self) -> ExactRationalV1:
        from fractions import Fraction

        value = Fraction(self.numerator, self.denominator)
        if (value.numerator, value.denominator) != (self.numerator, self.denominator):
            raise ValueError("bridge rational must be reduced")
        return self


class BridgePlayerV1(_BridgeModel):
    player_id: PortableId
    position: PortableId
    starting_stack: ExactRationalV1


class BridgeActionV1(_BridgeModel):
    street: Literal["preflop", "flop", "turn", "river", "showdown"]
    actor: PortableId
    action: Literal["post_blind", "post_ante", "fold", "check", "call", "bet", "raise", "all_in"]
    amount: ExactRationalV1
    to_amount: ExactRationalV1 | None
    pot_before: ExactRationalV1 | None
    pot_after: ExactRationalV1 | None


class BridgeHandV1(_BridgeModel):
    game_type: Literal["NLHE"]
    format: Literal["cash"]
    table_size: int = Field(ge=2, le=10)
    small_blind: ExactRationalV1
    big_blind: ExactRationalV1
    ante: ExactRationalV1
    rake: ExactRationalV1 | None
    players: tuple[BridgePlayerV1, ...] = Field(min_length=2, max_length=10)
    hero_player_id: PortableId
    hero_cards: tuple[str, str]
    board: tuple[str, str, str, str, str]
    actions: tuple[BridgeActionV1, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def closed_hand(self) -> BridgeHandV1:
        from fractions import Fraction

        identifiers = tuple(player.player_id for player in self.players)
        if len(identifiers) != len(set(identifiers)) or self.hero_player_id not in identifiers:
            raise ValueError("bridge hand player identity mismatch")
        if self.table_size != len(self.players):
            raise ValueError("bridge hand table size mismatch")
        cards = (*self.hero_cards, *self.board)
        if len(cards) != len(set(cards)) or any(_CARD.fullmatch(card) is None for card in cards):
            raise ValueError("bridge hand cards are invalid or duplicated")
        ante = Fraction(self.ante.numerator, self.ante.denominator)
        rake = (
            Fraction(self.rake.numerator, self.rake.denominator)
            if self.rake is not None
            else Fraction(0)
        )
        if ante != 0 or rake != 0 or any(item.action == "all_in" for item in self.actions):
            raise ValueError("bridge hand exceeds the no-ante/no-rake/non-all-in scope")
        if any(
            Fraction(item.starting_stack.numerator, item.starting_stack.denominator) <= 0
            for item in self.players
        ) or any(
            Fraction(item.amount.numerator, item.amount.denominator) < 0 for item in self.actions
        ):
            raise ValueError("bridge hand contains an invalid chip amount")
        return self


class BridgeFocalDecisionV1(_BridgeModel):
    selector_street: Literal["river"]
    selector_actor: PortableId
    selector_action: Literal["bet", "raise"]
    selector_amount: ExactRationalV1
    facing_action_index: int = Field(ge=0, le=511)
    hero_action_index: int = Field(ge=1, le=512)
    hero_response: Literal["fold"]
    focal_sha256: Sha256

    @model_validator(mode="after")
    def adjacent(self) -> BridgeFocalDecisionV1:
        if self.hero_action_index != self.facing_action_index + 1:
            raise ValueError("focal actions must be adjacent")
        return self


class BridgeRangeProvenanceV1(_BridgeModel):
    schema_version: Literal["1.0.0"]
    grammar_id: Literal["poker-deliberation.nlhe-range"]
    grammar_version: Literal["1.0.0"]
    range_id: PortableId
    target_player_id: PortableId
    notation: BoundedText
    source_id: PortableId
    source_kind: Literal["repository_fixture"]
    license_classification: Literal["repository_owned_mit"]
    usage_classification: Literal["redistribution_allowed"]
    content_status: Literal["USER_CLAIM", "ASSUMPTION"]
    content_sha256: Sha256
    table_size: int = Field(ge=2, le=10)
    target_position: PortableId
    street: Literal["river"]
    starting_stack_min_bb_milli: int = Field(ge=0)
    starting_stack_max_bb_milli: int = Field(ge=0)
    as_of_action_index: int = Field(ge=1, le=512)
    action_prefix_sha256: Sha256
    range_definition_sha256: Sha256
    range_equity_binding_sha256: Sha256

    @model_validator(mode="after")
    def rights_and_interval(self) -> BridgeRangeProvenanceV1:
        if self.starting_stack_min_bb_milli > self.starting_stack_max_bb_milli:
            raise ValueError("range stack interval is reversed")
        return self


class BridgeToolEvidenceV1(_BridgeModel):
    evidence_id: PortableId
    tool_name: Literal[
        "hand_validator",
        "hand_pot_ledger",
        "pot_odds",
        "range_validate",
        "combos",
        "holdem_equity",
        "raked_call_ev",
    ]
    status: Literal["success"]
    result_sha256: Sha256


class BridgeMathEvidenceV1(_BridgeModel):
    equity_model_id: Literal["explicit-single-range-heads-up-river-exact-v1"]
    exact_only: Literal[True]
    exact_evaluation_cap: Literal[990]
    equity: ExactRationalV1
    call_ev_model_id: Literal["single-river-decision-no-future-betting-no-rake-v1"]
    fold_ev_reference: Literal["focal-decision-zero"]
    chip_unit: ExactRationalV1
    pot_before_bet_units: int = Field(ge=0)
    opponent_bet_units: int = Field(gt=0)
    pot_after_bet_units: int = Field(gt=0)
    call_cost_units: int = Field(gt=0)
    contestable_pot_units: int = Field(gt=0)
    required_equity: ExactRationalV1
    call_ev_units: ExactRationalV1
    call_ev_amount: ExactRationalV1
    fold_ev_units: ExactRationalV1
    call_minus_fold_ev_units: ExactRationalV1
    action_comparison: Literal["call", "fold", "tie"]
    rake_percent_numerator: Literal[0] = 0
    rake_percent_denominator: Literal[1] = 1
    rake_cap: Literal[None] = None
    no_future_betting: Literal[True] = True
    comparison_epistemic_label: Literal["CALCULATED"] = "CALCULATED"
    strategic_interpretation_label: Literal["INFERENCE"] = "INFERENCE"
    practical_range_accuracy: Literal["UNKNOWN"] = "UNKNOWN"
    range_source_status: Literal["USER_CLAIM", "ASSUMPTION"]
    solver_status: Literal["unavailable"] = "unavailable"
    call_ev_model_sha256: Sha256
    source_result_sha256: Sha256
    tool_support: tuple[BridgeToolEvidenceV1, ...] = Field(min_length=7, max_length=7)

    @model_validator(mode="after")
    def exact_math_and_tool_order(self) -> BridgeMathEvidenceV1:
        from fractions import Fraction

        expected = (
            "hand_validator",
            "hand_pot_ledger",
            "pot_odds",
            "range_validate",
            "combos",
            "holdem_equity",
            "raked_call_ev",
        )
        if (
            tuple(item.tool_name for item in self.tool_support) != expected
            or len({item.evidence_id for item in self.tool_support}) != len(expected)
            or len({item.result_sha256 for item in self.tool_support}) != len(expected)
        ):
            raise ValueError("bridge tool evidence order mismatch")
        equity = Fraction(self.equity.numerator, self.equity.denominator)
        required = Fraction(
            self.required_equity.numerator,
            self.required_equity.denominator,
        )
        call_ev = Fraction(self.call_ev_units.numerator, self.call_ev_units.denominator)
        call_amount = Fraction(
            self.call_ev_amount.numerator,
            self.call_ev_amount.denominator,
        )
        fold_ev = Fraction(self.fold_ev_units.numerator, self.fold_ev_units.denominator)
        delta = Fraction(
            self.call_minus_fold_ev_units.numerator,
            self.call_minus_fold_ev_units.denominator,
        )
        chip_unit = Fraction(self.chip_unit.numerator, self.chip_unit.denominator)
        expected_required = Fraction(self.call_cost_units, self.contestable_pot_units)
        expected_call_ev = equity * self.contestable_pot_units - self.call_cost_units
        expected_comparison = (
            "call" if expected_call_ev > 0 else "fold" if expected_call_ev < 0 else "tie"
        )
        if (
            not Fraction(0) <= equity <= Fraction(1)
            or chip_unit <= 0
            or self.pot_after_bet_units != self.pot_before_bet_units + self.opponent_bet_units
            or self.contestable_pot_units != self.pot_after_bet_units + self.call_cost_units
            or required != expected_required
            or call_ev != expected_call_ev
            or fold_ev != 0
            or delta != call_ev - fold_ev
            or call_amount != call_ev * chip_unit
            or self.action_comparison != expected_comparison
        ):
            raise ValueError("bridge exact math evidence is internally inconsistent")
        return self


class BridgeSourceBindingV1(_BridgeModel):
    source_terminal_run_id: PortableId
    source_terminal_revision: int = Field(ge=1)
    source_terminal_transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{32}$")
    source_terminal_revision_root_sha256: Sha256
    source_terminal_manifest_sha256: Sha256
    source_terminal_inventory_sha256: Sha256
    source_candidate_sha256: Sha256
    source_binding_sha256: Sha256
    source_result_sha256: Sha256
    source_provenance_sha256: Sha256


class BridgeSourceContextV1(_BridgeModel):
    source: BridgeSourceBindingV1
    hand: BridgeHandV1
    focal_decision: BridgeFocalDecisionV1
    range: BridgeRangeProvenanceV1
    math: BridgeMathEvidenceV1
    context_payload_sha256: Sha256

    @model_validator(mode="after")
    def payload_hash_matches(self) -> BridgeSourceContextV1:
        focal = self.focal_decision
        if focal.hero_action_index >= len(self.hand.actions):
            raise ValueError("bridge focal action index is outside the hand")
        facing = self.hand.actions[focal.facing_action_index]
        response = self.hand.actions[focal.hero_action_index]
        players = {item.player_id: item for item in self.hand.players}
        if (
            facing.street != focal.selector_street
            or facing.actor != focal.selector_actor
            or facing.action != focal.selector_action
            or facing.amount != focal.selector_amount
            or response.street != "river"
            or response.actor != self.hand.hero_player_id
            or response.action != focal.hero_response
            or self.range.target_player_id != focal.selector_actor
            or self.range.target_player_id not in players
            or self.range.target_position != players[self.range.target_player_id].position
            or self.range.table_size != self.hand.table_size
            or self.range.as_of_action_index != focal.facing_action_index + 1
            or self.math.source_result_sha256 != self.source.source_result_sha256
        ):
            raise ValueError("bridge source context components are not correlated")
        if self.context_payload_sha256 != domain_sha256(
            "poker-bounded-codex-bridge-source-context-v1",
            without_field(self, "context_payload_sha256"),
        ):
            raise ValueError("source context hash mismatch")
        return self


class BridgeBudgetV1(_BridgeModel):
    auth_mode: RuntimeAuthModeV1
    max_context_bytes: Literal[65536] = MAX_CONTEXT_BYTES
    max_response_bytes: Literal[32768] = MAX_RESPONSE_BYTES
    max_stream_bytes: Literal[262144] = MAX_STREAM_BYTES
    max_turns: int = Field(ge=0, le=1)
    max_runtime_ms: int = Field(ge=0, le=MAX_ROLE_RUNTIME_MS)
    max_input_tokens: int = Field(ge=0, le=MAX_INPUT_TOKENS_PER_ROLE)
    max_output_tokens: int = Field(ge=0, le=MAX_OUTPUT_TOKENS_PER_ROLE)
    cost_budget_kind: Literal["not_applicable", "subscription_usage", "api_explicit_cap"]
    max_cost_micro_usd: int | None = Field(default=None, ge=0, le=MAX_RESERVED_COST_MICRO_USD)
    hard_provider_token_stop: Literal[False] = False
    hard_provider_cost_stop: Literal[False] = False

    @model_validator(mode="after")
    def mode_budget_matches(self) -> BridgeBudgetV1:
        expected: tuple[int, int, int, int, str, int | None]
        if self.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY:
            expected = (0, 0, 0, 0, "not_applicable", None)
        elif self.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION:
            expected = (
                1,
                MAX_ROLE_RUNTIME_MS,
                MAX_INPUT_TOKENS_PER_ROLE,
                MAX_OUTPUT_TOKENS_PER_ROLE,
                "subscription_usage",
                None,
            )
        else:
            if self.max_cost_micro_usd is None or self.max_cost_micro_usd <= 0:
                raise ValueError("openai_api requires an explicit positive cost cap")
            expected = (
                1,
                MAX_ROLE_RUNTIME_MS,
                MAX_INPUT_TOKENS_PER_ROLE,
                MAX_OUTPUT_TOKENS_PER_ROLE,
                "api_explicit_cap",
                self.max_cost_micro_usd,
            )
        actual = (
            self.max_turns,
            self.max_runtime_ms,
            self.max_input_tokens,
            self.max_output_tokens,
            self.cost_budget_kind,
            self.max_cost_micro_usd,
        )
        if actual != expected:
            raise ValueError("runtime/auth mode budget mismatch")
        return self


class BridgeRuntimePolicyV1(_BridgeModel):
    auth_mode_contract_version: Literal["1.0.0"] = AUTH_MODE_CONTRACT_VERSION
    auth_mode: RuntimeAuthModeV1
    provider_selection_source: Literal["explicit_auth_mode"] = "explicit_auth_mode"
    api_key_presence_selects_mode: Literal[False] = False
    provider_fallback_allowed: Literal[False] = False
    model_fallback_allowed: Literal[False] = False
    interface: Literal["local_provider", "codex_exec_json", "codex_sdk_responses"]
    runtime_identity: Literal[
        "poker-deliberation-local/0.1.0",
        "openai-codex-cli/0.144.4",
        "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    ]
    runtime_binary_sha256: Sha256 | None
    model: Literal["gpt-5.6-terra"] | None
    model_provider: Literal[
        "local_provider",
        "openai",
        "openai_responses_api_no_retry",
    ]
    reasoning_effort: Literal["medium"] | None
    service_tier: Literal["default"] | None
    credential_reference: Literal[
        "none",
        "codex_home:saved_chatgpt_login",
        "env:OPENAI_API_KEY",
    ]
    credential_value_access: Literal[
        "none",
        "codex_status_probe_only",
        "official_runtime_only",
    ]
    classification: Literal["public"] = "public"
    usage_classification: Literal["redistribution_allowed"] = "redistribution_allowed"
    model_processing_authorized: bool
    trace_policy: Literal["validated_typed_public_raw_local_only"] = (
        "validated_typed_public_raw_local_only"
    )
    remote_retention_policy: Literal[
        "none_local_only",
        "chatgpt_workspace_policy_unknown",
        "openai_api_org_policy_no_zdr_claim",
    ]
    tool_allowlist: tuple[()] = ()
    shell_enabled: Literal[False] = False
    web_enabled: Literal[False] = False
    mcp_enabled: Literal[False] = False
    apps_enabled: Literal[False] = False
    nested_agents_enabled: Literal[False] = False
    file_write_enabled: Literal[False] = False
    approval_policy: Literal["never"] = "never"
    sandbox: Literal["read-only"] = "read-only"
    serial_execution: Literal[True] = True
    automatic_product_retry: Literal[False] = False
    provider_internal_retry_status: Literal["not_applicable", "UNKNOWN", "disabled"]
    cooperative_cancellation_only: Literal[True] = True
    hard_process_tree_stop: Literal[False] = False
    remote_cancel_finality: Literal["not_applicable", "UNKNOWN"]
    network_allowed: bool
    budget: BridgeBudgetV1
    policy_sha256: Sha256

    @model_validator(mode="after")
    def policy_matches_mode_and_hash(self) -> BridgeRuntimePolicyV1:
        expected: dict[RuntimeAuthModeV1, tuple[object, ...]] = {
            RuntimeAuthModeV1.LOCAL_ONLY: (
                "local_provider",
                BRIDGE_LOCAL_RUNTIME_ID,
                None,
                None,
                BRIDGE_LOCAL_PROVIDER_ID,
                None,
                None,
                BRIDGE_LOCAL_CREDENTIAL_REFERENCE,
                "none",
                False,
                "none_local_only",
                "not_applicable",
                "not_applicable",
                False,
            ),
            RuntimeAuthModeV1.CODEX_SUBSCRIPTION: (
                "codex_exec_json",
                BRIDGE_SUBSCRIPTION_RUNTIME_ID,
                BRIDGE_RUNTIME_BINARY_SHA256,
                BRIDGE_MODEL_ID,
                BRIDGE_SUBSCRIPTION_PROVIDER_ID,
                BRIDGE_REASONING_EFFORT,
                BRIDGE_SERVICE_TIER,
                BRIDGE_SUBSCRIPTION_CREDENTIAL_REFERENCE,
                "codex_status_probe_only",
                True,
                "chatgpt_workspace_policy_unknown",
                "UNKNOWN",
                "UNKNOWN",
                True,
            ),
            RuntimeAuthModeV1.OPENAI_API: (
                "codex_sdk_responses",
                BRIDGE_OPENAI_API_RUNTIME_ID,
                BRIDGE_RUNTIME_BINARY_SHA256,
                BRIDGE_MODEL_ID,
                BRIDGE_OPENAI_API_PROVIDER_ID,
                BRIDGE_REASONING_EFFORT,
                BRIDGE_SERVICE_TIER,
                BRIDGE_OPENAI_API_CREDENTIAL_REFERENCE,
                "official_runtime_only",
                True,
                "openai_api_org_policy_no_zdr_claim",
                "disabled",
                "UNKNOWN",
                True,
            ),
        }
        actual = (
            self.interface,
            self.runtime_identity,
            self.runtime_binary_sha256,
            self.model,
            self.model_provider,
            self.reasoning_effort,
            self.service_tier,
            self.credential_reference,
            self.credential_value_access,
            self.model_processing_authorized,
            self.remote_retention_policy,
            self.provider_internal_retry_status,
            self.remote_cancel_finality,
            self.network_allowed,
        )
        if actual != expected[self.auth_mode] or self.budget.auth_mode is not self.auth_mode:
            raise ValueError("runtime policy does not match the explicit auth mode")
        if self.policy_sha256 != domain_sha256(
            "poker-bounded-codex-bridge-runtime-policy-v1",
            without_field(self, "policy_sha256"),
        ):
            raise ValueError("runtime policy hash mismatch")
        return self


class BridgeEvidenceReferenceV1(_BridgeModel):
    evidence_id: PortableId
    evidence_kind: Literal[
        "source_terminal",
        "source_candidate",
        "source_binding",
        "source_result",
        "source_provenance",
        "tool_result",
        "role_result",
        "adjudication",
    ]
    evidence_sha256: Sha256


class BridgeParentResultV1(_BridgeModel):
    """A complete, secret-free parent result suitable for exact outbound replay."""

    output: BridgeRoleOutputV1
    response_bytes_sha256: Sha256
    result_sha256: Sha256

    @property
    def auth_mode(self) -> RuntimeAuthModeV1:
        return self.output.auth_mode

    @property
    def role(self) -> BridgeRole:
        return self.output.role

    @property
    def assignment_id(self) -> str:
        return self.output.assignment_id

    @property
    def attempt_id(self) -> str:
        return self.output.attempt_id

    @model_validator(mode="after")
    def parent_result_hash_matches(self) -> BridgeParentResultV1:
        if self.result_sha256 != domain_sha256(
            RESULT_HASH_DOMAIN,
            without_field(self, "result_sha256"),
        ):
            raise ValueError("parent role result hash mismatch")
        return self


BridgeSemanticRole: TypeAlias = Literal[
    "strategy-analysis",
    "math-audit",
    "skepticism",
    "adjudication",
    "report-writing",
]

_BRIDGE_ROLE_SEMANTICS: Final[dict[BridgeRole, str]] = {
    BridgeRole.STRATEGY_ANALYST: "strategy-analysis",
    BridgeRole.MATH_TOOL_AUDITOR: "math-audit",
    BridgeRole.SKEPTIC_FALSIFIER: "skepticism",
    BridgeRole.ADJUDICATOR: "adjudication",
    BridgeRole.REPORT_WRITER: "report-writing",
}


class BridgeRoleConformanceBindingV1(_BridgeModel):
    conformance_schema_version: Literal["1.0.0"] = "1.0.0"
    role: BridgeRole
    semantic_role: BridgeSemanticRole
    runtime_role_definition_sha256: Sha256
    codex_runtime_inventory_sha256: Sha256
    python_runtime_inventory_sha256: Sha256
    semantic_mapping_sha256: Sha256
    source_path: str = Field(pattern=r"^\.codex/agents/[a-z0-9-]+\.toml$")
    role_read_only: Literal[True] = True
    declared_tool_allowlist: tuple[()] = ()

    @model_validator(mode="after")
    def role_semantics_match(self) -> BridgeRoleConformanceBindingV1:
        if self.semantic_role != _BRIDGE_ROLE_SEMANTICS[self.role]:
            raise ValueError("bridge role differs from the P2-025A semantic mapping")
        if self.source_path != f".codex/agents/{self.role.value}.toml":
            raise ValueError("bridge role definition source path mismatch")
        return self


class BridgeRoleAssignmentV1(_BridgeModel):
    auth_mode: RuntimeAuthModeV1
    bridge_run_id: PortableId
    role: BridgeRole
    assignment_id: PortableId
    attempt_id: PortableId
    parent_assignment_ids: tuple[PortableId, ...]
    parent_result_sha256s: tuple[Sha256, ...]
    ordinal: int = Field(ge=0, le=4)
    expires_at: datetime
    conformance: BridgeRoleConformanceBindingV1

    _expiry_utc = field_validator("expires_at")(lambda value: _utc(value, "expires_at"))

    @model_validator(mode="after")
    def lineage_matches_role(self) -> BridgeRoleAssignmentV1:
        if self.role is not BRIDGE_ROLE_ORDER[self.ordinal]:
            raise ValueError("role ordinal mismatch")
        if self.conformance.role is not self.role:
            raise ValueError("role conformance binding mismatch")
        mode_token = self.auth_mode.value
        if mode_token not in self.assignment_id or mode_token not in self.attempt_id:
            raise ValueError("assignment and attempt IDs must be auth-mode scoped")
        if self.role in {
            BridgeRole.STRATEGY_ANALYST,
            BridgeRole.MATH_TOOL_AUDITOR,
            BridgeRole.SKEPTIC_FALSIFIER,
        }:
            expected_parents = 0
        elif self.role is BridgeRole.ADJUDICATOR:
            expected_parents = 3
        else:
            expected_parents = 1
        if (
            len(self.parent_assignment_ids) != expected_parents
            or len(self.parent_result_sha256s) != expected_parents
            or len(set(self.parent_assignment_ids)) != expected_parents
            or len(set(self.parent_result_sha256s)) != expected_parents
        ):
            raise ValueError("role parent lineage mismatch")
        return self


class BridgeContextEnvelopeV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-codex-review-bridge"] = BRIDGE_CONTRACT_ID
    contract_version: Literal["1.0.0"] = BRIDGE_CONTRACT_VERSION
    canonicalization_id: Literal["poker-bounded-codex-bridge-json-v1"] = BRIDGE_CANONICALIZATION_ID
    producer_runtime: Literal["python-orchestrator"] = "python-orchestrator"
    consumer_runtime: Literal["local", "codex-native"]
    assignment: BridgeRoleAssignmentV1
    runtime_policy: BridgeRuntimePolicyV1
    source_context: BridgeSourceContextV1
    parent_results: tuple[BridgeParentResultV1, ...]
    envelope_sha256: Sha256

    @model_validator(mode="after")
    def envelope_correlates(self) -> BridgeContextEnvelopeV1:
        if (
            self.assignment.auth_mode is not self.runtime_policy.auth_mode
            or any(
                item.auth_mode is not self.runtime_policy.auth_mode for item in self.parent_results
            )
            or self.consumer_runtime
            != (
                "local"
                if self.runtime_policy.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY
                else "codex-native"
            )
        ):
            raise ValueError("context runtime/auth mode binding mismatch")
        if tuple(item.assignment_id for item in self.parent_results) != (
            self.assignment.parent_assignment_ids
        ) or tuple(item.result_sha256 for item in self.parent_results) != (
            self.assignment.parent_result_sha256s
        ):
            raise ValueError("context parent result mismatch")
        expected_parent_roles = {
            BridgeRole.STRATEGY_ANALYST: (),
            BridgeRole.MATH_TOOL_AUDITOR: (),
            BridgeRole.SKEPTIC_FALSIFIER: (),
            BridgeRole.ADJUDICATOR: BRIDGE_ROLE_ORDER[:3],
            BridgeRole.REPORT_WRITER: (BridgeRole.ADJUDICATOR,),
        }[self.assignment.role]
        if tuple(item.role for item in self.parent_results) != expected_parent_roles:
            raise ValueError("context parent role order mismatch")
        if any(
            item.output.bridge_run_id != self.assignment.bridge_run_id
            or item.output.auth_mode is not self.assignment.auth_mode
            for item in self.parent_results
        ):
            raise ValueError("context parent run or auth mode mismatch")
        if self.envelope_sha256 != domain_sha256(
            CONTEXT_HASH_DOMAIN,
            without_field(self, "envelope_sha256"),
        ):
            raise ValueError("context envelope hash mismatch")
        return self


class BoundedCodexBridgeRequestV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-codex-review-bridge"] = BRIDGE_CONTRACT_ID
    request_kind: Literal["bounded_read_only_role_review"] = "bounded_read_only_role_review"
    auth_mode: RuntimeAuthModeV1
    developer_instructions: BoundedText
    output_schema_sha256: Sha256
    allowed_conclusion_codes: tuple[BridgeConclusionCode, ...] = Field(
        min_length=1,
        max_length=3,
    )
    allowed_conclusion_labels: tuple[BridgeEpistemicLabel, ...] = Field(
        min_length=2,
        max_length=2,
    )
    required_uncertainty_label: Literal[BridgeEpistemicLabel.UNKNOWN]
    required_evidence_references: tuple[BridgeEvidenceReferenceV1, ...] = Field(
        min_length=1,
        max_length=64,
    )
    claim_evidence_rule: Literal[
        "any_required_evidence",
        "all_three_parent_role_results",
        "adjudication_parent_only",
    ]
    narrative_numbers_allowed: Literal[False] = False
    narrative_ranges_allowed: Literal[False] = False
    narrative_citations_allowed: Literal[False] = False
    calculated_labels_allowed: Literal[False] = False
    context: BridgeContextEnvelopeV1
    request_bytes_sha256: Sha256
    request_sha256: Sha256

    @model_validator(mode="after")
    def request_hash_matches(self) -> BoundedCodexBridgeRequestV1:
        from poker_deliberation.codex_bridge.canonical import canonical_json_bytes, sha256_bytes

        if (
            self.auth_mode is not self.context.runtime_policy.auth_mode
            or self.auth_mode is not self.context.assignment.auth_mode
        ):
            raise ValueError("request auth mode binding mismatch")
        expected_codes = allowed_conclusion_codes(self.context.assignment.role)
        expected_rule = claim_evidence_rule(self.context.assignment.role)
        if (
            self.allowed_conclusion_codes != expected_codes
            or self.allowed_conclusion_labels
            != (BridgeEpistemicLabel.INFERENCE, BridgeEpistemicLabel.UNKNOWN)
            or self.claim_evidence_rule != expected_rule
            or tuple(item.evidence_id for item in self.required_evidence_references)
            != tuple(
                sorted(
                    (item.evidence_id for item in self.required_evidence_references),
                    key=lambda item: item.encode("utf-8"),
                )
            )
            or len({item.evidence_id for item in self.required_evidence_references})
            != len(self.required_evidence_references)
        ):
            raise ValueError("request response contract mismatch")
        projection = without_field(self, "request_sha256")
        raw_projection = dict(projection)
        raw_projection.pop("request_bytes_sha256")
        if self.request_bytes_sha256 != sha256_bytes(canonical_json_bytes(raw_projection)):
            raise ValueError("request bytes hash mismatch")
        if self.request_sha256 != domain_sha256(REQUEST_HASH_DOMAIN, projection):
            raise ValueError("request hash mismatch")
        return self


class BridgeRunPlanV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-codex-review-bridge"] = BRIDGE_CONTRACT_ID
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    source: BridgeSourceBindingV1
    role_order: tuple[BridgeRole, ...] = BRIDGE_ROLE_ORDER
    role_conformance: tuple[BridgeRoleConformanceBindingV1, ...]
    codex_runtime_inventory_sha256: Sha256
    python_runtime_inventory_sha256: Sha256
    semantic_mapping_sha256: Sha256
    runtime_policy_sha256: Sha256
    runtime_identity: Literal[
        "poker-deliberation-local/0.1.0",
        "openai-codex-cli/0.144.4",
        "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    ]
    model_provider: Literal[
        "local_provider",
        "openai",
        "openai_responses_api_no_retry",
    ]
    model: Literal["gpt-5.6-terra"] | None
    credential_reference: Literal[
        "none",
        "codex_home:saved_chatgpt_login",
        "env:OPENAI_API_KEY",
    ]
    remote_retention_policy: Literal[
        "none_local_only",
        "chatgpt_workspace_policy_unknown",
        "openai_api_org_policy_no_zdr_claim",
    ]
    repository_commit_id: GitObjectId
    repository_tree_id: GitObjectId
    created_at: datetime
    total_max_turns: int = Field(ge=0, le=5)
    total_max_runtime_ms: int = Field(ge=0, le=MAX_RUN_RUNTIME_MS)
    total_max_input_tokens: int = Field(ge=0, le=MAX_TOTAL_INPUT_TOKENS)
    total_max_output_tokens: int = Field(ge=0, le=MAX_TOTAL_OUTPUT_TOKENS)
    total_max_cost_micro_usd: int | None = Field(
        default=None,
        ge=0,
        le=MAX_RESERVED_COST_MICRO_USD,
    )
    plan_sha256: Sha256

    _created_utc = field_validator("created_at")(lambda value: _utc(value, "created_at"))

    @model_validator(mode="after")
    def plan_hash_matches(self) -> BridgeRunPlanV1:
        if self.role_order != BRIDGE_ROLE_ORDER:
            raise ValueError("bridge role order mismatch")
        if tuple(item.role for item in self.role_conformance) != BRIDGE_ROLE_ORDER:
            raise ValueError("bridge conformance role order mismatch")
        if any(
            item.codex_runtime_inventory_sha256 != self.codex_runtime_inventory_sha256
            or item.python_runtime_inventory_sha256 != self.python_runtime_inventory_sha256
            or item.semantic_mapping_sha256 != self.semantic_mapping_sha256
            for item in self.role_conformance
        ):
            raise ValueError("bridge conformance inventory binding mismatch")
        if self.auth_mode is RuntimeAuthModeV1.OPENAI_API and (
            self.total_max_cost_micro_usd is None or self.total_max_cost_micro_usd <= 0
        ):
            raise ValueError("openai_api run plan requires an explicit positive cost cap")
        expected_budget = (
            (0, 0, 0, 0, None)
            if self.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY
            else (
                5,
                MAX_RUN_RUNTIME_MS,
                MAX_TOTAL_INPUT_TOKENS,
                MAX_TOTAL_OUTPUT_TOKENS,
                None
                if self.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
                else self.total_max_cost_micro_usd,
            )
        )
        actual_budget = (
            self.total_max_turns,
            self.total_max_runtime_ms,
            self.total_max_input_tokens,
            self.total_max_output_tokens,
            self.total_max_cost_micro_usd,
        )
        if actual_budget != expected_budget:
            raise ValueError("run plan budget does not match auth mode")
        expected_identity = {
            RuntimeAuthModeV1.LOCAL_ONLY: (
                BRIDGE_LOCAL_RUNTIME_ID,
                BRIDGE_LOCAL_PROVIDER_ID,
                None,
                BRIDGE_LOCAL_CREDENTIAL_REFERENCE,
                "none_local_only",
            ),
            RuntimeAuthModeV1.CODEX_SUBSCRIPTION: (
                BRIDGE_SUBSCRIPTION_RUNTIME_ID,
                BRIDGE_SUBSCRIPTION_PROVIDER_ID,
                BRIDGE_MODEL_ID,
                BRIDGE_SUBSCRIPTION_CREDENTIAL_REFERENCE,
                "chatgpt_workspace_policy_unknown",
            ),
            RuntimeAuthModeV1.OPENAI_API: (
                BRIDGE_OPENAI_API_RUNTIME_ID,
                BRIDGE_OPENAI_API_PROVIDER_ID,
                BRIDGE_MODEL_ID,
                BRIDGE_OPENAI_API_CREDENTIAL_REFERENCE,
                "openai_api_org_policy_no_zdr_claim",
            ),
        }[self.auth_mode]
        if (
            self.runtime_identity,
            self.model_provider,
            self.model,
            self.credential_reference,
            self.remote_retention_policy,
        ) != expected_identity:
            raise ValueError("run plan runtime identity does not match auth mode")
        if self.plan_sha256 != domain_sha256(
            RUN_PLAN_HASH_DOMAIN,
            without_field(self, "plan_sha256"),
        ):
            raise ValueError("bridge run plan hash mismatch")
        return self


class BridgeConfirmationAuthorityV1(_BridgeModel):
    authority_id: PortableId
    authority_kind: Literal["local_user", "verified_application"]
    authentication: Literal["self_asserted", "verified"]
    scope: Literal["confirm_exact_bounded_codex_role_request"] = (
        "confirm_exact_bounded_codex_role_request"
    )

    @model_validator(mode="after")
    def kind_matches_authentication(self) -> BridgeConfirmationAuthorityV1:
        expected = {"local_user": "self_asserted", "verified_application": "verified"}[
            self.authority_kind
        ]
        if self.authentication != expected:
            raise ValueError("confirmation authority mismatch")
        return self


class BridgeRoleConfirmationV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-codex-review-bridge"] = BRIDGE_CONTRACT_ID
    confirmation_id: PortableId
    idempotency_key: PortableId
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    role: BridgeRole
    assignment_id: PortableId
    attempt_id: PortableId
    request_sha256: Sha256
    request_bytes_sha256: Sha256
    envelope_sha256: Sha256
    runtime_policy_sha256: Sha256
    runtime_identity: Literal[
        "poker-deliberation-local/0.1.0",
        "openai-codex-cli/0.144.4",
        "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    ]
    model_provider: Literal[
        "local_provider",
        "openai",
        "openai_responses_api_no_retry",
    ]
    model: Literal["gpt-5.6-terra"] | None
    credential_reference: Literal[
        "none",
        "codex_home:saved_chatgpt_login",
        "env:OPENAI_API_KEY",
    ]
    authority: BridgeConfirmationAuthorityV1
    confirmed_at: datetime
    expires_at: datetime
    confirmed: Literal[True] = True
    confirmation_sha256: Sha256

    @field_validator("confirmed_at", "expires_at")
    @classmethod
    def confirmation_times_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "confirmation_time"))

    @model_validator(mode="after")
    def confirmation_hash_matches(self) -> BridgeRoleConfirmationV1:
        lifetime = (self.expires_at - self.confirmed_at).total_seconds()
        if lifetime <= 0 or lifetime > MAX_CONFIRMATION_LIFETIME_SECONDS:
            raise ValueError("confirmation lifetime is unsupported")
        if (
            self.runtime_identity,
            self.model,
            self.model_provider,
            self.credential_reference,
        ) != _mode_core_identity(self.auth_mode):
            raise ValueError("confirmation runtime/auth mode mismatch")
        if self.confirmation_sha256 != domain_sha256(
            CONFIRMATION_HASH_DOMAIN,
            without_field(self, "confirmation_sha256"),
        ):
            raise ValueError("confirmation hash mismatch")
        return self


class BridgePreExecutionAdmissionV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    role: BridgeRole
    assignment_id: PortableId
    attempt_id: PortableId
    request_sha256: Sha256
    confirmation_sha256: Sha256
    runtime_policy_sha256: Sha256
    runtime_identity: Literal[
        "poker-deliberation-local/0.1.0",
        "openai-codex-cli/0.144.4",
        "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    ]
    model_provider: Literal[
        "local_provider",
        "openai",
        "openai_responses_api_no_retry",
    ]
    model: Literal["gpt-5.6-terra"] | None
    credential_reference: Literal[
        "none",
        "codex_home:saved_chatgpt_login",
        "env:OPENAI_API_KEY",
    ]
    source_terminal_manifest_sha256: Sha256
    admitted_at: datetime
    expires_at: datetime
    effect_state: Literal[BridgeEffectState.NOT_LAUNCHED] = BridgeEffectState.NOT_LAUNCHED
    admission_sha256: Sha256

    @field_validator("admitted_at", "expires_at")
    @classmethod
    def admission_times_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "admission_time"))

    @model_validator(mode="after")
    def admission_hash_matches(self) -> BridgePreExecutionAdmissionV1:
        if self.admitted_at >= self.expires_at:
            raise ValueError("admission is expired")
        if (
            self.runtime_identity,
            self.model,
            self.model_provider,
            self.credential_reference,
        ) != _mode_core_identity(self.auth_mode):
            raise ValueError("admission runtime/auth mode mismatch")
        if self.admission_sha256 != domain_sha256(
            ADMISSION_HASH_DOMAIN,
            without_field(self, "admission_sha256"),
        ):
            raise ValueError("admission hash mismatch")
        return self


class BridgeClaimV1(_BridgeModel):
    claim_id: ClaimId
    conclusion_code: BridgeConclusionCode
    label: BridgeEpistemicLabel
    narrative: Narrative
    evidence_ids: tuple[PortableId, ...] = Field(min_length=1, max_length=16)

    @field_validator("evidence_ids")
    @classmethod
    def canonical_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, "claim evidence IDs")

    @model_validator(mode="after")
    def narrative_matches_epistemic_label(self) -> BridgeClaimV1:
        expected = (
            SAFE_INFERENCE_NARRATIVE
            if self.label is BridgeEpistemicLabel.INFERENCE
            else SAFE_UNKNOWN_NARRATIVE
        )
        if self.narrative != expected:
            raise ValueError("claim narrative does not match its epistemic label")
        return self


class BridgeRoleOutputV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-codex-review-bridge"] = BRIDGE_CONTRACT_ID
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    role: BridgeRole
    assignment_id: PortableId
    attempt_id: PortableId
    model: Literal["gpt-5.6-terra"] | None
    model_provider: Literal[
        "local_provider",
        "openai",
        "openai_responses_api_no_retry",
    ]
    runtime_identity: Literal[
        "poker-deliberation-local/0.1.0",
        "openai-codex-cli/0.144.4",
        "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    ]
    conclusions: tuple[BridgeClaimV1, ...] = Field(min_length=1, max_length=16)
    uncertainties: tuple[BridgeClaimV1, ...] = Field(default=(), max_length=16)
    evidence_references: tuple[BridgeEvidenceReferenceV1, ...] = Field(
        min_length=1,
        max_length=64,
    )
    model_usage_authority: Literal["transport_audit_only"] = "transport_audit_only"
    termination_declaration: Literal["completed"] = "completed"

    @model_validator(mode="after")
    def output_is_role_bound(self) -> BridgeRoleOutputV1:
        runtime, model, provider, _credential = _mode_core_identity(self.auth_mode)
        if (self.runtime_identity, self.model, self.model_provider) != (
            runtime,
            model,
            provider,
        ):
            raise ValueError("role output runtime/auth mode mismatch")
        claims = (*self.conclusions, *self.uncertainties)
        expected_claim_ids = tuple(f"claim-{index:02d}" for index in range(1, len(claims) + 1))
        if tuple(claim.claim_id for claim in claims) != expected_claim_ids:
            raise ValueError("claim IDs must be content-free, ordered, and gapless")
        if any(claim.conclusion_code not in _ROLE_CODES[self.role] for claim in claims):
            raise ValueError("role used an unauthorized conclusion code")
        if any(claim.label is not BridgeEpistemicLabel.UNKNOWN for claim in self.uncertainties):
            raise ValueError("uncertainties must be UNKNOWN")
        refs = {item.evidence_id: item.evidence_sha256 for item in self.evidence_references}
        if len(refs) != len(self.evidence_references):
            raise ValueError("evidence references must be unique")
        used = tuple(
            evidence_id
            for claim in (*self.conclusions, *self.uncertainties)
            for evidence_id in claim.evidence_ids
        )
        if any(evidence_id not in refs for evidence_id in used):
            raise ValueError("claim references unbound evidence")
        parent_refs = {
            item.evidence_id
            for item in self.evidence_references
            if item.evidence_kind in {"role_result", "adjudication"}
        }
        used_refs = set(used)
        if self.role is BridgeRole.ADJUDICATOR:
            if len(parent_refs) != 3 or any(
                not parent_refs.issubset(set(claim.evidence_ids)) for claim in claims
            ):
                raise ValueError("each adjudicator claim must bind all independent parent results")
        elif self.role is BridgeRole.REPORT_WRITER:
            if len(parent_refs) != 1 or used_refs != parent_refs:
                raise ValueError("report writer must use only the adjudicated parent result")
        elif parent_refs:
            raise ValueError("independent role cannot contain a parent result")
        return self


class BridgeRoleResultV1(_BridgeModel):
    output: BridgeRoleOutputV1
    response_bytes_sha256: Sha256
    result_sha256: Sha256

    @model_validator(mode="after")
    def result_hash_matches(self) -> BridgeRoleResultV1:
        if self.result_sha256 != domain_sha256(
            RESULT_HASH_DOMAIN,
            without_field(self, "result_sha256"),
        ):
            raise ValueError("role result hash mismatch")
        return self


class BridgeTransportUsageV1(_BridgeModel):
    input_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    reasoning_output_tokens: int = Field(ge=0)
    estimated_cost_micro_usd: int | None = Field(default=None, ge=0)
    cost_authority: Literal["not_applicable", "estimate", "unavailable"]
    invoice_authority: Literal[False] = False


class CodexSubscriptionLiveExecutionEvidenceV1(_BridgeModel):
    """Secret-free evidence minted by one sealed default subscription execution."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    evidence_kind: Literal["codex_subscription_sealed_default_execution"]
    transport_type: Literal[
        "poker_deliberation.codex_bridge.subscription_transport.CodexSubscriptionCliTransport"
    ]
    sealed_default_process: Literal[True]
    default_auth_status_probe: Literal[True]
    default_command_factory: Literal[True]
    default_isolation_root: Literal[True]
    default_credential_codex_home: Literal[True]
    interface: Literal["codex_exec_json"]
    auth_mode: Literal[RuntimeAuthModeV1.CODEX_SUBSCRIPTION]
    auth_boundary: Literal["codex_home_saved_chatgpt_login"]
    auth_enforcement: Literal["codex_cli_login_status_exact_chatgpt"]
    credential_values_included: Literal[False]
    provider_model_fallback_allowed: Literal[False]
    model_fallback_allowed: Literal[False]
    process_fallback_allowed: Literal[False]
    runtime_identity: Literal["openai-codex-cli/0.144.4"]
    runtime_binary_sha256: Sha256
    runtime_source_inventory_sha256: Sha256
    runtime_configuration_sha256: Sha256
    request_sha256: Sha256
    request_bytes_sha256: Sha256
    output_schema_sha256: Sha256
    command_contract_sha256: Sha256
    launch_intent_sha256: Sha256
    response_bytes_sha256: Sha256
    event_stream_sha256: Sha256
    usage_sha256: Sha256
    process_returncode: Literal[0]
    thread_id_sha256: Sha256
    turn_id_sha256: Sha256
    execution_runtime_sha256: Sha256
    attestation_sha256: Sha256

    @model_validator(mode="after")
    def exact_sealed_execution(self) -> CodexSubscriptionLiveExecutionEvidenceV1:
        if self.runtime_binary_sha256 != BRIDGE_RUNTIME_BINARY_SHA256:
            raise ValueError("subscription attestation runtime binary mismatch")
        runtime_payload = {
            "runtime_identity": self.runtime_identity,
            "runtime_binary_sha256": self.runtime_binary_sha256,
            "runtime_source_inventory_sha256": self.runtime_source_inventory_sha256,
            "runtime_configuration_sha256": self.runtime_configuration_sha256,
            "request_sha256": self.request_sha256,
            "request_bytes_sha256": self.request_bytes_sha256,
            "output_schema_sha256": self.output_schema_sha256,
            "command_contract_sha256": self.command_contract_sha256,
            "launch_intent_sha256": self.launch_intent_sha256,
            "response_bytes_sha256": self.response_bytes_sha256,
            "event_stream_sha256": self.event_stream_sha256,
            "usage_sha256": self.usage_sha256,
            "process_returncode": self.process_returncode,
            "thread_id_sha256": self.thread_id_sha256,
            "turn_id_sha256": self.turn_id_sha256,
        }
        if self.execution_runtime_sha256 != domain_sha256(
            SUBSCRIPTION_EXECUTION_RUNTIME_HASH_DOMAIN,
            runtime_payload,
        ):
            raise ValueError("subscription execution runtime hash mismatch")
        if self.attestation_sha256 != domain_sha256(
            SUBSCRIPTION_SEALED_LIVE_ATTESTATION_HASH_DOMAIN,
            without_field(self, "attestation_sha256"),
        ):
            raise ValueError("subscription execution attestation hash mismatch")
        return self


class BridgeExecutionAuditV1(_BridgeModel):
    schema_version: Literal["2.0.0"] = EXECUTION_AUDIT_SCHEMA_VERSION
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    role: BridgeRole
    assignment_id: PortableId
    attempt_id: PortableId
    request_sha256: Sha256
    confirmation_sha256: Sha256
    admission_sha256: Sha256
    runtime_policy_sha256: Sha256
    transport_qualification: Literal["deterministic_fixture", "actual_live"]
    live_execution_evidence: CodexSubscriptionLiveExecutionEvidenceV1 | None
    interface: Literal["local_provider", "codex_exec_json", "codex_sdk_responses"]
    credential_reference: Literal[
        "none",
        "codex_home:saved_chatgpt_login",
        "env:OPENAI_API_KEY",
    ]
    remote_retention_policy: Literal[
        "none_local_only",
        "chatgpt_workspace_policy_unknown",
        "openai_api_org_policy_no_zdr_claim",
    ]
    runtime_identity: Literal[
        "poker-deliberation-local/0.1.0",
        "openai-codex-cli/0.144.4",
        "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    ]
    model_identity_evidence: Literal[
        "direct_observation",
        "requested_pinned_no_fallback_no_reroute",
        "unavailable",
    ]
    requested_model: Literal["gpt-5.6-terra"] | None
    observed_model: Literal["gpt-5.6-terra"] | None
    requested_model_provider: Literal[
        "local_provider",
        "openai",
        "openai_responses_api_no_retry",
    ]
    observed_model_provider: (
        Literal[
            "local_provider",
            "openai",
            "openai_responses_api_no_retry",
        ]
        | None
    )
    reasoning_effort: Literal["medium"] | None
    observed_reasoning_effort: Literal["medium"] | None
    service_tier: Literal["default"] | None
    observed_service_tier: Literal["default"] | None
    observed_identity_sha256: Sha256 | None
    effect_state: BridgeEffectState
    # A transport can durably observe thread.started before turn.started. In that
    # narrow effect-unknown state the thread hash is retained without inventing a turn.
    thread_id_sha256: Sha256 | None
    turn_id_sha256: Sha256 | None
    launched_at: datetime | None
    completed_at: datetime | None
    duration_ms: int | None = Field(default=None, ge=0)
    usage: BridgeTransportUsageV1 | None
    # This is an integer observation, not retained response content. It must be able to
    # record an over-cap response so a rejected transport does not erase known usage.
    response_bytes: int | None = Field(default=None, ge=0)
    stream_bytes: int | None = Field(default=None, ge=0)
    unexpected_item_types: tuple[PortableId, ...] = ()
    cancellation_kind: Literal["not_requested", "cooperative", "unconfirmed"]
    automatic_retry_count: Literal[0] = 0
    result_sha256: Sha256 | None
    failure_reason_code: PortableId | None
    audit_sha256: Sha256

    @field_validator("launched_at", "completed_at")
    @classmethod
    def optional_audit_times_utc(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return _utc(value, getattr(info, "field_name", "audit_time"))

    @field_validator("unexpected_item_types")
    @classmethod
    def canonical_unexpected_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _sorted_unique(value, "unexpected item types")

    @model_validator(mode="after")
    def audit_state_is_closed(self) -> BridgeExecutionAuditV1:
        runtime, model, provider, credential = _mode_core_identity(self.auth_mode)
        if (
            self.runtime_identity,
            self.requested_model,
            self.requested_model_provider,
            self.credential_reference,
        ) != (runtime, model, provider, credential):
            raise ValueError("execution audit runtime/auth mode mismatch")
        evidence = self.live_execution_evidence
        if self.transport_qualification == "actual_live":
            if (
                evidence is None
                or self.auth_mode is not RuntimeAuthModeV1.CODEX_SUBSCRIPTION
                or evidence.request_sha256 != self.request_sha256
                or evidence.runtime_identity != self.runtime_identity
                or evidence.interface != self.interface
                or evidence.thread_id_sha256 != self.thread_id_sha256
                or evidence.turn_id_sha256 != self.turn_id_sha256
            ):
                raise ValueError("actual-live audit lacks sealed execution evidence")
        elif evidence is not None:
            raise ValueError("deterministic audit cannot contain live execution evidence")
        if self.effect_state is BridgeEffectState.SUCCEEDED:
            if self.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION:
                if (
                    self.model_identity_evidence != "requested_pinned_no_fallback_no_reroute"
                    or self.observed_model is not None
                    or self.observed_model_provider is not None
                    or self.observed_reasoning_effort is not None
                    or self.observed_service_tier is not None
                    or self.observed_identity_sha256 is not None
                ):
                    raise ValueError(
                        "subscription audit must keep effective model identity unknown"
                    )
            elif (
                self.model_identity_evidence != "direct_observation"
                or self.observed_model != model
                or self.observed_model_provider != provider
                or self.observed_reasoning_effort != self.reasoning_effort
                or self.observed_service_tier != self.service_tier
                or self.observed_identity_sha256 is None
            ):
                raise ValueError("successful audit lacks direct runtime identity evidence")
        definitely_launched = self.effect_state in {
            BridgeEffectState.LAUNCHED,
            BridgeEffectState.SUCCEEDED,
            BridgeEffectState.FAILED,
            BridgeEffectState.TIMED_OUT,
            BridgeEffectState.CANCELLED,
            BridgeEffectState.CANCEL_UNCONFIRMED,
        }
        succeeded = self.effect_state is BridgeEffectState.SUCCEEDED
        expected_cancellation_kind = {
            BridgeEffectState.CANCELLED: "cooperative",
            BridgeEffectState.CANCEL_UNCONFIRMED: "unconfirmed",
        }.get(self.effect_state, "not_requested")
        if self.cancellation_kind != expected_cancellation_kind:
            raise ValueError("execution cancellation kind does not match effect state")
        thread_present = self.thread_id_sha256 is not None
        turn_present = self.turn_id_sha256 is not None
        identifiers_present = thread_present and turn_present
        thread_only = thread_present and not turn_present
        if turn_present and not thread_present:
            raise ValueError("execution turn identity requires its thread identity")
        if thread_only and (
            self.effect_state is not BridgeEffectState.EFFECT_UNKNOWN
            or self.launched_at is not None
        ):
            raise ValueError("partial thread lifecycle evidence is not effect-unknown")
        if definitely_launched and (self.launched_at is None or not identifiers_present):
            raise ValueError("execution launch state mismatch")
        if self.effect_state is BridgeEffectState.NOT_LAUNCHED and (
            self.launched_at is not None or thread_present or turn_present
        ):
            raise ValueError("not-launched audit contains runtime identity")
        if succeeded != (self.result_sha256 is not None):
            raise ValueError("execution result state mismatch")
        if self.completed_at is None:
            raise ValueError("terminal execution audit requires completion time")
        if succeeded and self.usage is None:
            raise ValueError("successful execution requires completion and usage")
        if succeeded == (self.failure_reason_code is not None):
            raise ValueError("execution failure reason state mismatch")
        if self.audit_sha256 != domain_sha256(
            EXECUTION_AUDIT_HASH_DOMAIN,
            without_field(self, "audit_sha256"),
        ):
            raise ValueError("execution audit hash mismatch")
        return self


class BridgeExecutionIdentityClaimV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    identity_kind: Literal["thread", "turn"]
    identity_sha256: Sha256
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    role: BridgeRole
    assignment_id: PortableId
    attempt_id: PortableId
    request_sha256: Sha256
    execution_audit_sha256: Sha256
    claim_sha256: Sha256

    @model_validator(mode="after")
    def claim_hash_matches(self) -> BridgeExecutionIdentityClaimV1:
        if self.claim_sha256 != domain_sha256(
            EXECUTION_IDENTITY_CLAIM_HASH_DOMAIN,
            without_field(self, "claim_sha256"),
        ):
            raise ValueError("execution identity claim hash mismatch")
        return self


class BridgeConfirmationIdentifierClaimV1(_BridgeModel):
    """Store-wide reservation for a confirmation or idempotency identifier hash."""

    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    identifier_kind: Literal["confirmation", "idempotency"]
    identifier_sha256: Sha256
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    role: BridgeRole
    request_sha256: Sha256
    claim_sha256: Sha256

    @model_validator(mode="after")
    def claim_hash_matches(self) -> BridgeConfirmationIdentifierClaimV1:
        if self.claim_sha256 != domain_sha256(
            CONFIRMATION_IDENTIFIER_CLAIM_HASH_DOMAIN,
            without_field(self, "claim_sha256"),
        ):
            raise ValueError("confirmation identifier claim hash mismatch")
        return self


BridgeArtifactKind: TypeAlias = Literal[
    "run_plan",
    "source_context",
    "request",
    "confirmation",
    "admission",
    "role_result",
    "execution_audit",
]


class BridgeArtifactInventoryV1(_BridgeModel):
    logical_name: str = Field(pattern=r"^[a-z0-9][a-z0-9_./-]{0,191}$")
    artifact_kind: BridgeArtifactKind
    sha256: Sha256
    size_bytes: int = Field(ge=1, le=1_000_000)


BridgeTerminalStatus: TypeAlias = Literal[
    "approval_required",
    "in_progress",
    "succeeded",
    "failed",
    "timed_out",
    "cancelled",
    "cancel_unconfirmed",
    "effect_unknown",
]


class BridgeTerminalManifestV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    storage_protocol: Literal["poker-bounded-codex-bridge-terminal-v1"] = (
        "poker-bounded-codex-bridge-terminal-v1"
    )
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    runtime_policy_sha256: Sha256
    revision: int = Field(ge=1)
    transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{32}$")
    previous_manifest_sha256: Sha256 | None
    expected_pointer_sha256: Sha256 | None
    status: BridgeTerminalStatus
    source_terminal_manifest_sha256: Sha256
    run_plan_sha256: Sha256
    created_at: datetime
    published_at: datetime
    inventory: tuple[BridgeArtifactInventoryV1, ...] = Field(min_length=1, max_length=64)
    inventory_sha256: Sha256
    manifest_sha256: Sha256

    @field_validator("created_at", "published_at")
    @classmethod
    def manifest_times_utc(cls, value: datetime, info: object) -> datetime:
        return _utc(value, getattr(info, "field_name", "manifest_time"))

    @model_validator(mode="after")
    def manifest_hash_matches(self) -> BridgeTerminalManifestV1:
        names = tuple(item.logical_name for item in self.inventory)
        if names != tuple(sorted(set(names), key=lambda item: item.encode("utf-8"))):
            raise ValueError("terminal inventory must be sorted and unique")
        expected_inventory = domain_sha256(
            "poker-bounded-codex-bridge-inventory-v1",
            [item.model_dump(mode="json") for item in self.inventory],
        )
        if self.inventory_sha256 != expected_inventory:
            raise ValueError("terminal inventory hash mismatch")
        if self.manifest_sha256 != domain_sha256(
            TERMINAL_MANIFEST_HASH_DOMAIN,
            without_field(self, "manifest_sha256"),
        ):
            raise ValueError("terminal manifest hash mismatch")
        return self


class BridgeCompletionMarkerV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    terminal_revision: int = Field(ge=1)
    terminal_transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{32}$")
    terminal_status: Literal[
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "cancel_unconfirmed",
        "effect_unknown",
    ]
    terminal_manifest_sha256: Sha256
    inventory_sha256: Sha256
    published_at: datetime

    _published_utc = field_validator("published_at")(lambda value: _utc(value, "published_at"))


class BridgeCurrentPointerV1(_BridgeModel):
    schema_version: Literal["1.0.0"] = BRIDGE_SCHEMA_VERSION
    bridge_run_id: PortableId
    auth_mode: RuntimeAuthModeV1
    revision: int = Field(ge=1)
    transaction_id: str = Field(pattern=r"^txn-[0-9a-f]{32}$")
    status: BridgeTerminalStatus
    manifest_sha256: Sha256
    inventory_sha256: Sha256
    completion_marker_sha256: Sha256 | None
    published_at: datetime

    _pointer_utc = field_validator("published_at")(lambda value: _utc(value, "published_at"))


__all__ = [name for name in globals() if name.startswith("BRIDGE_") or name.startswith("MAX_")]
__all__ += [
    "ADMISSION_HASH_DOMAIN",
    "AUTH_MODE_CONTRACT_VERSION",
    "CONFIRMATION_IDENTIFIER_CLAIM_HASH_DOMAIN",
    "CONTEXT_HASH_DOMAIN",
    "EXECUTION_AUDIT_HASH_DOMAIN",
    "EXECUTION_AUDIT_SCHEMA_VERSION",
    "OBSERVED_TRANSPORT_IDENTITY_HASH_DOMAIN",
    "REQUEST_HASH_DOMAIN",
    "RESULT_HASH_DOMAIN",
    "RUN_PLAN_HASH_DOMAIN",
    "SUBSCRIPTION_EXECUTION_RUNTIME_HASH_DOMAIN",
    "SUBSCRIPTION_SEALED_LIVE_ATTESTATION_HASH_DOMAIN",
    "SUBSCRIPTION_USAGE_HASH_DOMAIN",
    "TERMINAL_MANIFEST_HASH_DOMAIN",
    "BoundedCodexBridgeRequestV1",
    "BridgeActionV1",
    "BridgeArtifactInventoryV1",
    "BridgeBudgetV1",
    "BridgeClaimV1",
    "BridgeCompletionMarkerV1",
    "BridgeConclusionCode",
    "BridgeConfirmationAuthorityV1",
    "BridgeConfirmationIdentifierClaimV1",
    "BridgeContextEnvelopeV1",
    "BridgeCurrentPointerV1",
    "BridgeEffectState",
    "BridgeEpistemicLabel",
    "BridgeEvidenceReferenceV1",
    "BridgeExecutionAuditV1",
    "BridgeExecutionIdentityClaimV1",
    "BridgeFocalDecisionV1",
    "BridgeHandV1",
    "BridgeMathEvidenceV1",
    "BridgeParentResultV1",
    "BridgePlayerV1",
    "BridgePreExecutionAdmissionV1",
    "BridgeRangeProvenanceV1",
    "BridgeRole",
    "BridgeRoleAssignmentV1",
    "BridgeRoleConfirmationV1",
    "BridgeRoleConformanceBindingV1",
    "BridgeRoleOutputV1",
    "BridgeRoleResultV1",
    "BridgeRunPlanV1",
    "BridgeRuntimePolicyV1",
    "BridgeSourceBindingV1",
    "BridgeSourceContextV1",
    "BridgeTerminalManifestV1",
    "BridgeToolEvidenceV1",
    "BridgeTransportUsageV1",
    "CodexSubscriptionLiveExecutionEvidenceV1",
    "ExactRationalV1",
    "RuntimeAuthModeV1",
]
