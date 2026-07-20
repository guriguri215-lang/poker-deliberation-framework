"""Canonical Pydantic schemas shared by the CLI, tools, and artifacts."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class EpistemicLabel(StrEnum):
    FACT = "FACT"
    CALCULATED = "CALCULATED"
    INFERENCE = "INFERENCE"
    ESTIMATE = "ESTIMATE"
    ASSUMPTION = "ASSUMPTION"
    USER_CLAIM = "USER_CLAIM"
    UNKNOWN = "UNKNOWN"


class ConfidenceGrade(StrEnum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


class Exactness(StrEnum):
    """Legacy three-value compatibility projection.

    New results also expose ``numeric_exactness``.  This field remains stable so
    version-1 JSON consumers can migrate without an enum-breaking change.
    """

    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNAVAILABLE = "unavailable"


class NumericalExactness(StrEnum):
    EXACT = "exact"
    EXACT_UNDER_MODEL = "exact-under-model"
    FLOATING_VERIFIED = "floating-verified"
    APPROXIMATE = "approximate"
    UNAVAILABLE = "unavailable"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"


class AgentExecutionStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    REFUSED = "refused"
    FALLBACK = "fallback"


class Street(StrEnum):
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"


NonNegativeFiniteFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveFiniteFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class PlayerStack(StrictModel):
    player_id: str = Field(min_length=1)
    position: str = Field(min_length=1)
    starting_stack: NonNegativeFiniteFloat


class HandAction(StrictModel):
    street: Street
    actor: str = Field(min_length=1)
    action: Literal["post_blind", "post_ante", "fold", "check", "call", "bet", "raise", "all_in"]
    amount: NonNegativeFiniteFloat = Field(default=0, description="Incremental chips committed")
    to_amount: NonNegativeFiniteFloat | None = None
    pot_before: NonNegativeFiniteFloat | None = None
    pot_after: NonNegativeFiniteFloat | None = None


class TournamentContext(StrictModel):
    payouts: list[NonNegativeFiniteFloat] = Field(default_factory=list)
    remaining_players: int | None = Field(default=None, ge=2)
    hero_stack: NonNegativeFiniteFloat | None = None
    bounty_type: Literal["none", "fixed", "progressive", "mystery"] = "none"
    hero_bounty: NonNegativeFiniteFloat | None = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def payouts_non_increasing(self) -> TournamentContext:
        if any(p < 0 for p in self.payouts):
            raise ValueError("payouts must be non-negative")
        if any(a < b for a, b in zip(self.payouts, self.payouts[1:], strict=False)):
            raise ValueError("payouts must be ordered from highest to lowest")
        return self


class RangeDefinition(StrictModel):
    player_id: str
    notation: str = Field(min_length=1)
    source: str | None = None
    game_conditions: dict[str, Any] = Field(default_factory=dict)
    assumed: bool = False


class CanonicalHand(StrictModel):
    game_type: Literal["NLHE", "PLO", "OTHER"] = "NLHE"
    format: Literal["cash", "tournament"]
    table_size: int = Field(ge=2, le=10)
    small_blind: PositiveFiniteFloat
    big_blind: PositiveFiniteFloat
    ante: NonNegativeFiniteFloat = 0
    rake: NonNegativeFiniteFloat | None = None
    players: list[PlayerStack] = Field(min_length=2)
    hero_player_id: str | None = None
    hero_cards: list[str] = Field(default_factory=list, max_length=4)
    board: list[str] = Field(default_factory=list, max_length=5)
    actions: list[HandAction] = Field(default_factory=list)
    tournament: TournamentContext | None = None
    known_ranges: list[RangeDefinition] = Field(default_factory=list)
    opponent_observations: list[str] = Field(default_factory=list)
    analysis_objective: str = "strategy_review"

    @model_validator(mode="after")
    def validate_game_context(self) -> CanonicalHand:
        ids = [player.player_id for player in self.players]
        if len(ids) != len(set(ids)):
            raise ValueError("player_id values must be unique")
        if self.hero_player_id is not None and self.hero_player_id not in ids:
            raise ValueError("hero_player_id must identify a listed player")
        if self.format == "tournament" and self.tournament is None:
            raise ValueError("tournament context is required for tournament hands")
        if self.game_type == "NLHE" and len(self.hero_cards) not in {0, 2}:
            raise ValueError("NLHE hero_cards must be empty or contain two cards")
        return self


class FocalDecision(StrictModel):
    """Stable locator for the decision being reviewed."""

    street: Street
    action_index: int = Field(ge=0)
    actor: str = Field(min_length=1)


class RealizedResult(StrictModel):
    """Post-decision information that blind analysis must never receive."""

    raw_text: str | None = None
    winner_player_id: str | None = None
    shown_cards: dict[str, list[str]] = Field(default_factory=dict)


class DecisionSnapshot(StrictModel):
    street: Street
    action_index: int = Field(ge=0)
    actor: str = Field(min_length=1)
    board: list[str] = Field(default_factory=list, max_length=5)
    pot_before: NonNegativeFiniteFloat
    to_call: NonNegativeFiniteFloat
    actual_call: NonNegativeFiniteFloat | None = None
    contestable_pot: NonNegativeFiniteFloat
    current_bet: NonNegativeFiniteFloat
    actor_invested: NonNegativeFiniteFloat
    stack_behind: NonNegativeFiniteFloat
    history_before: list[str] = Field(default_factory=list)
    facing_action: str
    side_pot_risk: bool = False


class BlindDecisionContext(StrictModel):
    """Decision-time-only payload for a baseline strategy analysis."""

    game: dict[str, Any]
    players: list[PlayerStack]
    hero_player_id: str | None = None
    hero_cards: list[str] = Field(default_factory=list, max_length=4)
    focal: DecisionSnapshot


class Claim(StrictModel):
    claim_id: str = Field(default_factory=lambda: f"claim-{uuid4().hex[:12]}")
    text: str = Field(min_length=1)
    label: EpistemicLabel
    confidence: ConfidenceGrade = ConfidenceGrade.C
    evidence_ids: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class Assumption(StrictModel):
    assumption_id: str = Field(default_factory=lambda: f"assumption-{uuid4().hex[:12]}")
    text: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    sensitivity: str | None = None


class CaseInput(StrictModel):
    case_id: str = Field(default_factory=lambda: f"case-{uuid4().hex[:12]}")
    kind: Literal["hand", "strategy", "claim", "calculation"]
    raw_text: str | None = None
    hand: CanonicalHand | None = None
    focal_decision: FocalDecision | None = None
    realized_result: RealizedResult | None = None
    analysis_scope: Literal["unspecified", "retrospective", "real_time"] = "unspecified"
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    objective: str = "correctness"
    requested_tools: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("requested_tools")
    @classmethod
    def safe_tool_names(cls, values: list[str]) -> list[str]:
        if len(values) > 32:
            raise ValueError("at most 32 requested tools are allowed")
        if len(values) != len(set(values)):
            raise ValueError("requested tool names must be unique")
        for value in values:
            if not value or len(value) > 64 or not value.replace("_", "a").isalnum():
                raise ValueError(
                    "requested tool names may contain only letters, digits, and underscore"
                )
        return values

    @model_validator(mode="after")
    def require_input(self) -> CaseInput:
        if not self.raw_text and self.hand is None and not self.claims and not self.requested_tools:
            raise ValueError("at least one analyzable input is required")
        for claim in self.claims:
            normalized = False
            if claim.label is not EpistemicLabel.USER_CLAIM:
                claim.label = EpistemicLabel.USER_CLAIM
                normalized = True
            if claim.confidence in {ConfidenceGrade.A, ConfidenceGrade.B}:
                claim.confidence = ConfidenceGrade.C
                normalized = True
            if normalized:
                claim.limitations.append(
                    "Input-supplied epistemic labels are untrusted; normalized to USER_CLAIM/C."
                )
        if self.focal_decision is not None:
            if self.hand is None:
                raise ValueError("focal_decision requires a canonical hand")
            if self.focal_decision.action_index >= len(self.hand.actions):
                raise ValueError("focal_decision.action_index is outside hand.actions")
            action = self.hand.actions[self.focal_decision.action_index]
            if (
                action.street is not self.focal_decision.street
                or action.actor != self.focal_decision.actor
            ):
                raise ValueError("focal_decision must match the indexed hand action")
        return self


class AgentAssignment(StrictModel):
    assignment_id: str = Field(default_factory=lambda: f"assignment-{uuid4().hex[:12]}")
    agent_role: str
    task: str
    context_keys: list[str] = Field(default_factory=list)
    read_only: bool = True


class AgentContext(StrictModel):
    kind: Literal["hand", "strategy", "claim", "calculation"]
    objective: str
    raw_text: str | None = None
    strategy_text: str | None = None
    hand: CanonicalHand | None = None
    claims: list[Claim] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    requested_tools: list[str] = Field(default_factory=list)
    tool_inputs: dict[str, Any] = Field(default_factory=dict)
    blind_decision_context: BlindDecisionContext | None = None


class AgentReport(StrictModel):
    report_id: str = Field(default_factory=lambda: f"report-{uuid4().hex[:12]}")
    agent_role: str
    task: str
    conclusions: list[str] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    tool_result_ids: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    falsification_conditions: list[str] = Field(default_factory=list)
    confidence: ConfidenceGrade = ConfidenceGrade.C
    unresolved_questions: list[str] = Field(default_factory=list)


class AgentExecutionRecord(StrictModel):
    execution_id: str = Field(default_factory=lambda: f"execution-{uuid4().hex[:12]}")
    assignment_id: str
    agent_role: str
    provider: str
    provider_version: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_id: str | None = None
    context_attempt_id: str | None = None
    parent_context_id: str | None = None
    context_schema_version: str | None = None
    context_classification: str | None = None
    context_payload_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_source_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_policy_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_envelope_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_expires_at: datetime | None = None
    context_producer_runtime: str | None = None
    context_consumer_runtime: str | None = None
    status: AgentExecutionStatus
    started_at: datetime
    completed_at: datetime
    error: str | None = None


class SecurityEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: f"security-{uuid4().hex[:12]}")
    category: Literal[
        "real_time_assistance",
        "private_cards",
        "collusion",
        "automated_play",
        "detection_evasion",
        "prompt_injection",
    ]
    rule_id: str
    action: Literal["refused", "recorded"]
    blocked: bool
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime = Field(default_factory=utc_now)


class ToolRequest(StrictModel):
    request_id: str = Field(default_factory=lambda: f"tool-request-{uuid4().hex[:12]}")
    tool_name: str
    input: dict[str, Any]
    requested_by: str = "orchestrator"
    requires_approval: bool = False
    contract_version: str | None = None


class TolerancePolicy(StrictModel):
    """Algorithm/field-specific comparison rule; never a repository-wide epsilon.

    ``absolute`` is expressed in the named field unit, ``relative`` scales by
    ``max(abs(actual), abs(expected))``, and ``ulps`` scales by the binary64
    magnitude.  A caller-supplied policy records its resolved per-result bound
    in ``absolute``.  No kind inherits ``math.isclose``'s default relative
    tolerance.
    """

    fields: list[str] = Field(min_length=1)
    kind: Literal["absolute", "relative", "absolute-or-relative", "ulp", "caller-supplied"]
    absolute: NonNegativeFiniteFloat | None = None
    relative: NonNegativeFiniteFloat | None = None
    ulps: int | None = Field(default=None, ge=1)
    formula: str | None = None
    unit: str
    rationale: str

    @model_validator(mode="after")
    def require_bound(self) -> TolerancePolicy:
        if self.kind == "absolute" and self.absolute is None:
            raise ValueError("absolute tolerance requires an absolute bound")
        if self.kind == "relative" and self.relative is None:
            raise ValueError("relative tolerance requires a relative bound")
        if self.kind == "absolute-or-relative" and (self.absolute is None or self.relative is None):
            raise ValueError("absolute-or-relative tolerance requires both bounds")
        if self.kind == "ulp" and self.ulps is None:
            raise ValueError("ulp tolerance requires an ulps bound")
        if self.kind == "caller-supplied" and not self.formula:
            raise ValueError("caller-supplied tolerance requires a formula")
        return self


class VerificationMetadata(StrictModel):
    method: str = Field(min_length=1)
    checks: list[str] = Field(min_length=1)
    observations: list[str] = Field(default_factory=list)
    tolerance: TolerancePolicy
    passed: bool


class NumericalErrorMetadata(StrictModel):
    metric: str = Field(min_length=1)
    value: NonNegativeFiniteFloat
    bound: NonNegativeFiniteFloat | None = None
    unit: str


class ToolResult(StrictModel):
    result_id: str = Field(default_factory=lambda: f"tool-result-{uuid4().hex[:12]}")
    tool_name: str
    input: dict[str, Any]
    output: dict[str, Any] = Field(default_factory=dict)
    status: ToolStatus
    exactness: Exactness
    numeric_exactness: NumericalExactness
    contract_version: str = "1.0.0"
    assumptions: list[str] = Field(default_factory=list)
    version: str = "1.0.0"
    model_qualifier: str | None = None
    method: str | None = None
    stochastic: bool | None = None
    seed: int | None = None
    samples: int | None = Field(default=None, ge=0)
    iterations: int | None = Field(default=None, ge=0)
    confidence_interval: tuple[float, float] | None = None
    confidence_level: float | None = Field(default=None, gt=0, lt=1, allow_inf_nan=False)
    error_metadata: NumericalErrorMetadata | None = None
    stopping_condition: str | None = None
    verification: VerificationMetadata | None = None
    duration_seconds: float = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    reproduce_command: str | None = None
    created_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="before")
    @classmethod
    def migrate_v1_numeric_exactness(cls, data: object) -> object:
        if not isinstance(data, dict) or data.get("numeric_exactness") is not None:
            return data
        raw_exactness = data.get("exactness")
        value = raw_exactness.value if isinstance(raw_exactness, Exactness) else raw_exactness
        migrated = dict(data)
        if isinstance(value, str):
            migrated["numeric_exactness"] = {
                Exactness.EXACT.value: NumericalExactness.EXACT.value,
                Exactness.APPROXIMATE.value: NumericalExactness.APPROXIMATE.value,
                Exactness.UNAVAILABLE.value: NumericalExactness.UNAVAILABLE.value,
            }.get(value)
        return migrated

    @model_validator(mode="after")
    def validate_numeric_contract(self) -> ToolResult:
        projection = {
            NumericalExactness.EXACT: Exactness.EXACT,
            NumericalExactness.EXACT_UNDER_MODEL: Exactness.EXACT,
            NumericalExactness.FLOATING_VERIFIED: Exactness.EXACT,
            NumericalExactness.APPROXIMATE: Exactness.APPROXIMATE,
            NumericalExactness.UNAVAILABLE: Exactness.UNAVAILABLE,
        }
        numeric = self.numeric_exactness
        if projection[numeric] is not self.exactness:
            raise ValueError("exactness must be the compatibility projection of numeric_exactness")

        if self.confidence_interval is not None:
            low, high = self.confidence_interval
            if not math.isfinite(low) or not math.isfinite(high) or low > high:
                raise ValueError("confidence_interval must contain ordered finite bounds")

        is_v2 = self.contract_version.startswith("2.")
        if self.status is ToolStatus.SUCCESS:
            if numeric is NumericalExactness.UNAVAILABLE:
                raise ValueError("successful results cannot have unavailable numeric exactness")
            if self.error is not None:
                raise ValueError("successful results cannot carry an error")
        else:
            if numeric is not NumericalExactness.UNAVAILABLE:
                raise ValueError("failed/unavailable results require unavailable numeric exactness")
            if self.exactness is not Exactness.UNAVAILABLE:
                raise ValueError("failed/unavailable results require unavailable legacy exactness")
            if self.status is ToolStatus.FAILED and self.output:
                raise ValueError("failed results cannot carry output values")

        if not is_v2:
            return self
        if self.status is not ToolStatus.SUCCESS:
            return self
        if numeric is NumericalExactness.EXACT_UNDER_MODEL and not self.model_qualifier:
            raise ValueError("exact-under-model results require model_qualifier")
        if numeric is NumericalExactness.FLOATING_VERIFIED and (
            self.verification is None or not self.verification.passed
        ):
            raise ValueError("floating-verified results require passed verification metadata")
        if numeric is NumericalExactness.APPROXIMATE:
            if not self.method:
                raise ValueError("approximate results require method")
            if self.stochastic is None:
                raise ValueError("approximate results require stochastic=true/false")
            if self.stochastic and self.seed is None:
                raise ValueError("stochastic approximate results require seed")
            if not ((self.samples or 0) > 0 or (self.iterations or 0) > 0):
                raise ValueError("approximate results require positive samples or iterations")
            if self.confidence_interval is None and self.error_metadata is None:
                raise ValueError("approximate results require an interval or error metadata")
            if not self.stopping_condition:
                raise ValueError("approximate results require a stopping condition")
        return self


class EvidenceRecord(StrictModel):
    evidence_id: str = Field(default_factory=lambda: f"evidence-{uuid4().hex[:12]}")
    source_title: str
    organization_or_author: str
    source_type: Literal["official", "paper", "repository", "technical", "community", "input"]
    url: HttpUrl | None = None
    identifier: str | None = None
    publication_date: str | None = None
    accessed_date: str
    supported_claim_ids: list[str] = Field(default_factory=list)
    summary: str
    source_tier: int = Field(ge=1, le=6)
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def requires_locator(self) -> EvidenceRecord:
        if self.url is None and not self.identifier:
            raise ValueError("evidence requires a URL or identifier")
        return self


class ClaimCheck(StrictModel):
    claim_id: str = Field(min_length=1)
    tool_name: str = Field(pattern=r"^[A-Za-z0-9_]{1,64}$")
    output_path: str = Field(pattern=r"^[A-Za-z0-9_.]{1,256}$")
    claimed_value: float = Field(allow_inf_nan=False)
    tolerance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unit: str | None = None


class Dispute(StrictModel):
    dispute_id: str = Field(default_factory=lambda: f"dispute-{uuid4().hex[:12]}")
    claim_ids: list[str] = Field(min_length=1)
    issue: str
    positions: list[str] = Field(default_factory=list)
    resolution: str | None = None
    resolution_basis: list[str] = Field(default_factory=list)
    unresolved: bool = True

    @model_validator(mode="after")
    def resolution_matches_status(self) -> Dispute:
        if self.unresolved and self.resolution is not None:
            raise ValueError("unresolved disputes cannot have a resolution")
        if not self.unresolved and self.resolution is None:
            raise ValueError("resolved disputes require a resolution")
        return self


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


ApprovalCategory = Literal[
    "external_code",
    "package_install",
    "external_service",
    "long_running_compute",
    "outside_workspace_write",
    "destructive_change",
    "secret_access",
    "paid_data",
    "objective_change",
]


class ApprovalProposal(StrictModel):
    approval_id: str = Field(default_factory=lambda: f"approval-{uuid4().hex[:12]}")
    action_category: ApprovalCategory = "external_service"
    requested_action: str
    reason: str
    expected_benefit: str
    risks: list[str]
    data_to_be_sent: list[str] = Field(default_factory=list)
    cost_or_resource_estimate: str
    alternatives: list[str]
    effect_of_declining: str
    exact_command_or_tool_call: str | None = None


class ApprovalRequest(StrictModel):
    approval_id: str = Field(default_factory=lambda: f"approval-{uuid4().hex[:12]}")
    action_category: ApprovalCategory = "external_service"
    requested_action: str
    reason: str
    expected_benefit: str
    risks: list[str]
    data_to_be_sent: list[str] = Field(default_factory=list)
    cost_or_resource_estimate: str
    alternatives: list[str]
    effect_of_declining: str
    exact_command_or_tool_call: str | None = None
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision_reason: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    decided_at: datetime | None = None


class FinalReport(StrictModel):
    run_id: str
    run_status: Literal["completed", "approval_required", "failed_with_limitations"] = "completed"
    conclusion: str
    reconstructed_input: dict[str, Any] = Field(default_factory=dict)
    data_quality: list[str] = Field(default_factory=list)
    claim_assessments: list[Claim] = Field(default_factory=list)
    analysis_sections: list[dict[str, Any]] = Field(default_factory=list)
    agent_execution_records: list[AgentExecutionRecord] = Field(default_factory=list)
    security_events: list[SecurityEvent] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    sensitivity: list[dict[str, Any]] = Field(default_factory=list)
    disputes: list[Dispute] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    confidence: ConfidenceGrade = ConfidenceGrade.C
    limitations: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utc_now)
