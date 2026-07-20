"""Canonical typed contracts for every registered local tool.

The registry consumes these definitions directly.  ``tools/manifest.yaml`` and
``docs/tool-contracts.md`` are deterministic projections generated from the same
objects; they are not independent sources of truth.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from poker_deliberation.schemas import (
    CanonicalHand,
    NumericalExactness,
    StrictModel,
    TolerancePolicy,
)
from poker_deliberation.tools.best_response import (
    HARD_MAX_NODES as BEST_RESPONSE_MAX_NODES,
)
from poker_deliberation.tools.best_response import (
    HARD_MAX_POLICY_NODE_EVALUATIONS,
    HARD_MAX_PURE_POLICIES,
)
from poker_deliberation.tools.equity import (
    HARD_MAX_EXACT_EVALUATIONS,
    HARD_MAX_MONTE_CARLO_SAMPLES,
)
from poker_deliberation.tools.ev_tree import HARD_MAX_DEPTH as EV_TREE_MAX_DEPTH
from poker_deliberation.tools.ev_tree import HARD_MAX_NODES as EV_TREE_MAX_NODES
from poker_deliberation.tools.matrix_game import (
    HARD_MAX_DIMENSION,
    HARD_MAX_FALLBACK_ITERATIONS,
    HARD_MAX_FICTITIOUS_WORK,
    HARD_MAX_SUPPORT_CANDIDATES,
    HARD_MAX_SUPPORT_SIZE,
)
from poker_deliberation.tools.numeric import close_ulps
from poker_deliberation.tools.verification import VerificationEvidence, verify_floating_result

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(ge=1)]


def _close_ulps(actual: float, expected: float, *, ulps: int) -> bool:
    return close_ulps(actual, expected, ulps=ulps)


class PotOddsInput(StrictModel):
    pot_before_bet: NonNegativeFloat
    opponent_bet: NonNegativeFloat
    call_cost: PositiveFloat
    expected_rake: NonNegativeFloat = 0.0


class PotOddsOutput(StrictModel):
    pot_after_opponent_bet: NonNegativeFloat
    final_pot_before_rake: PositiveFloat
    expected_rake: NonNegativeFloat
    final_pot_after_rake: PositiveFloat
    required_equity: PositiveFloat
    required_equity_percent: PositiveFloat
    pot_odds_against: NonNegativeFloat

    @model_validator(mode="after")
    def identities_hold(self) -> PotOddsOutput:
        if not _close_ulps(self.required_equity_percent, self.required_equity * 100.0, ulps=8):
            raise ValueError("required_equity_percent invariant failed")
        if not _close_ulps(
            self.final_pot_after_rake,
            self.final_pot_before_rake - self.expected_rake,
            ulps=8,
        ):
            raise ValueError("final-pot rake invariant failed")
        return self


class BreakEvenFoldInput(StrictModel):
    risk: PositiveFloat
    reward: PositiveFloat


class BreakEvenFoldOutput(StrictModel):
    risk: PositiveFloat
    reward: PositiveFloat
    break_even_fold_frequency: Probability
    break_even_fold_percent: NonNegativeFloat

    @model_validator(mode="after")
    def percent_holds(self) -> BreakEvenFoldOutput:
        if not _close_ulps(
            self.break_even_fold_percent,
            self.break_even_fold_frequency * 100.0,
            ulps=8,
        ):
            raise ValueError("break-even percent invariant failed")
        return self


class MDFInput(StrictModel):
    pot_before_bet: PositiveFloat
    bet: PositiveFloat


class MDFOutput(StrictModel):
    minimum_defense_frequency: Probability
    minimum_defense_percent: NonNegativeFloat
    formula: str


class SPRInput(StrictModel):
    effective_stack: NonNegativeFloat
    pot: PositiveFloat


class SPROutput(StrictModel):
    spr: NonNegativeFloat
    effective_stack: NonNegativeFloat
    pot: PositiveFloat
    formula: str


class EffectiveStackInput(StrictModel):
    stacks: list[NonNegativeFloat] = Field(min_length=2)


class EffectiveStackOutput(StrictModel):
    effective_stack: NonNegativeFloat
    stacks: list[NonNegativeFloat] = Field(min_length=2)
    formula: str

    @model_validator(mode="after")
    def minimum_holds(self) -> EffectiveStackOutput:
        if self.effective_stack != min(self.stacks):
            raise ValueError("effective stack must equal min(stacks)")
        return self


class RakeAmountInput(StrictModel):
    pot_total: NonNegativeFloat
    rake_percent: Annotated[float, Field(ge=0, lt=100, allow_inf_nan=False)]
    rake_cap: NonNegativeFloat | None = None


class RakeAmountOutput(StrictModel):
    rake_amount: NonNegativeFloat
    raw_rake: NonNegativeFloat
    rake_cap: NonNegativeFloat | None = None
    formula: str


class RakedCallEVInput(StrictModel):
    equity: Probability
    pot_after_bet: NonNegativeFloat
    call_cost: PositiveFloat
    rake_percent: Annotated[float, Field(ge=0, lt=100, allow_inf_nan=False)]
    rake_cap: NonNegativeFloat | None = None


class RakedCallEVOutput(StrictModel):
    ev: FiniteFloat
    rake_amount: NonNegativeFloat
    final_pot_after_rake: NonNegativeFloat
    formula: str
    model: str


class BluffEVInput(StrictModel):
    fold_frequency: Probability
    pot_before_bet: PositiveFloat
    bet: PositiveFloat
    equity_when_called: Probability = 0.0


class BluffEVOutput(StrictModel):
    ev: FiniteFloat
    called_branch_ev: FiniteFloat
    formula: str
    model: str


class PolarRiverInput(StrictModel):
    pot_before_bet: PositiveFloat
    bet: PositiveFloat


class PolarRiverOutput(StrictModel):
    bluff_fraction: Probability
    bluff_percent: NonNegativeFloat
    formula: str
    model: str


class BayesInput(StrictModel):
    prior: Probability
    likelihood_given_h: Probability
    likelihood_given_not_h: Probability


class BayesOutput(StrictModel):
    posterior: Probability
    evidence_probability: Probability
    formula: str


class PotReconstructionInput(StrictModel):
    starting_pot: NonNegativeFloat
    contributions: list[NonNegativeFloat]


class PotReconstructionOutput(StrictModel):
    starting_pot: NonNegativeFloat
    pots_after_each_contribution: list[NonNegativeFloat]
    final_pot: NonNegativeFloat


class CombosInput(StrictModel):
    hand_class: str | None = None
    range: str | None = None
    dead_cards: list[str] = Field(default_factory=list, max_length=52)

    @model_validator(mode="after")
    def exactly_one_notation(self) -> CombosInput:
        if (self.hand_class is None) == (self.range is None):
            raise ValueError("exactly one of hand_class or range is required")
        return self


class CombosOutput(StrictModel):
    hand_class: str | None = None
    count: int | None = Field(default=None, ge=0)
    combos: list[list[str]] | None = None
    range: str | None = None
    combo_count: int | None = Field(default=None, ge=1)
    total_combo_weight: PositiveFloat | None = None
    normalized_weights: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def shape_matches_mode(self) -> CombosOutput:
        hand_class_shape = self.hand_class is not None and self.count is not None
        range_shape = self.range is not None and self.combo_count is not None
        if hand_class_shape == range_shape:
            raise ValueError("combo output must contain exactly one supported shape")
        return self


class HoldemEquityInput(StrictModel):
    hero_range: str
    villain_range: str
    board: list[str] = Field(default_factory=list, max_length=5)
    dead_cards: list[str] = Field(default_factory=list, max_length=52)
    game_type: str = "NLHE"
    mode: Literal["auto", "exact", "monte_carlo"] = "auto"
    max_exact_evaluations: int = Field(default=250_000, ge=1, le=HARD_MAX_EXACT_EVALUATIONS)
    samples: int = Field(default=50_000, ge=1, le=HARD_MAX_MONTE_CARLO_SAMPLES)
    seed: int = 0
    opponent_ranges: list[str] | None = None
    villain_ranges: list[str] | None = None

    @model_validator(mode="after")
    def nlhe_only(self) -> HoldemEquityInput:
        if self.game_type.upper() != "NLHE":
            raise ValueError("holdem_equity supports NLHE only")
        if self.opponent_ranges is not None or self.villain_ranges is not None:
            raise ValueError("holdem_equity supports exactly one villain")
        return self


class HoldemEquityOutput(StrictModel):
    method: Literal["exact_enumeration", "monte_carlo"]
    exact: bool
    hero_equity: Probability
    evaluations: int | None = Field(default=None, ge=1)
    unweighted_wins: int | None = Field(default=None, ge=0)
    unweighted_ties: int | None = Field(default=None, ge=0)
    unweighted_losses: int | None = Field(default=None, ge=0)
    range_pair_count: int = Field(ge=1)
    cards_to_come: int = Field(ge=0, le=5)
    confidence_interval_95: tuple[Probability, Probability] | None = None
    confidence_interval_method: str | None = None
    samples: int | None = Field(default=None, ge=1)
    seed: int | None = None
    wins: int | None = Field(default=None, ge=0)
    ties: int | None = Field(default=None, ge=0)
    losses: int | None = Field(default=None, ge=0)
    estimated_exact_evaluations: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def method_metadata_matches(self) -> HoldemEquityOutput:
        if self.exact:
            if self.method != "exact_enumeration" or self.evaluations is None:
                raise ValueError("exact enumeration metadata is incomplete")
            counts = (self.unweighted_wins, self.unweighted_ties, self.unweighted_losses)
            if (
                any(value is None for value in counts)
                or sum(value or 0 for value in counts) != self.evaluations
            ):
                raise ValueError("exact enumeration counts do not match evaluations")
        else:
            if (
                self.method != "monte_carlo"
                or self.samples is None
                or self.seed is None
                or self.confidence_interval_95 is None
            ):
                raise ValueError("Monte Carlo metadata is incomplete")
            if sum((self.wins or 0, self.ties or 0, self.losses or 0)) != self.samples:
                raise ValueError("Monte Carlo counts do not match samples")
        return self


class EVBranchInput(StrictModel):
    probability: Probability
    child: str = Field(min_length=1)
    label: str | None = None


class EVNodeInput(StrictModel):
    payoff: FiniteFloat | None = None
    branches: list[EVBranchInput] | None = None

    @model_validator(mode="after")
    def terminal_or_branch(self) -> EVNodeInput:
        if (self.payoff is None) == (self.branches is None):
            raise ValueError("EV node requires exactly one of payoff or branches")
        if self.branches is not None and not self.branches:
            raise ValueError("EV decision node requires at least one branch")
        return self


class EVTreeInput(StrictModel):
    root: str = "root"
    nodes: dict[str, EVNodeInput] = Field(min_length=1, max_length=EV_TREE_MAX_NODES)


class EVTreeOutput(StrictModel):
    root: str
    expected_value: FiniteFloat
    node_values: dict[str, FiniteFloat]
    branches: dict[str, list[dict[str, Any]]]


class ICMInput(StrictModel):
    stacks: list[NonNegativeFloat] = Field(min_length=2, max_length=100)
    payouts: list[NonNegativeFloat] = Field(min_length=1, max_length=100)


class ICMOutput(StrictModel):
    stacks: list[NonNegativeFloat]
    payouts: list[NonNegativeFloat]
    equities: list[NonNegativeFloat]
    equity_sum: NonNegativeFloat
    payable_prize_sum: NonNegativeFloat
    sum_error: FiniteFloat
    verification_tolerance: NonNegativeFloat
    conservation_verified: Literal[True]
    zero_stack_players: list[int]
    warning: str | None = None
    model: str

    @model_validator(mode="after")
    def conservation_metadata_matches(self) -> ICMOutput:
        if not _close_ulps(self.equity_sum, sum(self.equities), ulps=32):
            raise ValueError("ICM equity_sum does not match equities")
        if not _close_ulps(
            self.sum_error,
            self.equity_sum - self.payable_prize_sum,
            ulps=32,
        ):
            raise ValueError("ICM sum_error metadata is inconsistent")
        return self


class MatrixGameInput(StrictModel):
    matrix: list[list[FiniteFloat]] = Field(min_length=1, max_length=HARD_MAX_DIMENSION)
    tolerance: NonNegativeFloat = 1e-9
    max_support_size: int = Field(default=8, ge=1, le=HARD_MAX_SUPPORT_SIZE)
    fallback_iterations: int = Field(default=50_000, ge=1, le=HARD_MAX_FALLBACK_ITERATIONS)

    @model_validator(mode="after")
    def rectangular_and_bounded(self) -> MatrixGameInput:
        if not self.matrix[0]:
            raise ValueError("payoff matrix must be non-empty")
        columns = len(self.matrix[0])
        if columns > HARD_MAX_DIMENSION or any(len(row) != columns for row in self.matrix):
            raise ValueError("payoff matrix must be rectangular and within hard dimensions")
        return self


class MatrixGameOutput(StrictModel):
    row_strategy: list[Probability]
    column_strategy: list[Probability]
    value: FiniteFloat | None = None
    value_estimate: FiniteFloat | None = None
    duality_gap: NonNegativeFloat
    row_support: list[int] | None = None
    column_support: list[int] | None = None
    row_best_response: int = Field(ge=0)
    column_best_response: int = Field(ge=0)
    method: Literal["verified_support_enumeration", "fictitious_play_fallback"]
    exact_algorithm: bool
    verification_tolerance: NonNegativeFloat | None = None
    support_candidates_upper_bound: int = Field(ge=1)
    iterations: int | None = Field(default=None, ge=1)
    fallback_work_estimate: int | None = Field(default=None, ge=1)
    warning: str | None = None

    @model_validator(mode="after")
    def method_metadata_matches(self) -> MatrixGameOutput:
        if not _close_ulps(sum(self.row_strategy), 1.0, ulps=64):
            raise ValueError("row strategy probabilities must sum to one")
        if not _close_ulps(sum(self.column_strategy), 1.0, ulps=64):
            raise ValueError("column strategy probabilities must sum to one")
        if self.exact_algorithm:
            if (
                self.method != "verified_support_enumeration"
                or self.value is None
                or self.verification_tolerance is None
            ):
                raise ValueError("support-enumeration verification metadata is incomplete")
        elif (
            self.method != "fictitious_play_fallback"
            or self.value_estimate is None
            or self.iterations is None
            or self.warning is None
        ):
            raise ValueError("fictitious-play approximation metadata is incomplete")
        return self


class TerminalGameNode(StrictModel):
    type: Literal["terminal"]
    payoff: FiniteFloat


class ChanceAction(StrictModel):
    probability: Probability
    child: str


class ChanceGameNode(StrictModel):
    type: Literal["chance"]
    actions: dict[str, ChanceAction] = Field(min_length=1)


class PlayerGameNode(StrictModel):
    type: Literal["player"]
    player: Literal[0, 1]
    information_set: str = Field(min_length=1)
    actions: dict[str, str] = Field(min_length=1)


GameNode = Annotated[
    TerminalGameNode | ChanceGameNode | PlayerGameNode,
    Field(discriminator="type"),
]


class BestResponseGame(StrictModel):
    root: str = "root"
    nodes: dict[str, GameNode] = Field(min_length=1, max_length=BEST_RESPONSE_MAX_NODES)


class BestResponseInput(StrictModel):
    game: BestResponseGame
    fixed_strategy: dict[str, dict[str, Probability]]
    best_responder: Literal[0, 1] = 0
    max_pure_policies: int = Field(default=1_000_000, ge=1, le=HARD_MAX_PURE_POLICIES)


class BestResponseOutput(StrictModel):
    best_responder: Literal[0, 1]
    value: FiniteFloat
    best_responder_value: FiniteFloat
    player0_value: FiniteFloat
    payoff_convention: str
    pure_policy: dict[str, str]
    evaluated_policies: int = Field(ge=1)
    policy_node_work_upper_bound: int = Field(ge=1)
    information_set_constraint_enforced: Literal[True]
    opponent_strategy_fixed: Literal[True]
    equilibrium_claim: Literal[False]


class HandValidatorInput(CanonicalHand):
    tolerance: NonNegativeFloat | None = None


class HandValidatorOutput(StrictModel):
    valid: bool
    verification_tolerance: NonNegativeFloat
    errors: list[str]
    warnings: list[str]
    final_pot: NonNegativeFloat
    remaining_stacks: dict[str, NonNegativeFloat]
    reconstructed_actions: list[dict[str, Any]]
    decision_snapshots: list[dict[str, Any]]
    limitations: list[str] = Field(min_length=1)


class SensitivityScenario(StrictModel):
    name: str | None = None
    parameters: dict[str, Any]
    value: FiniteFloat


class SensitivityInput(StrictModel):
    scenarios: list[SensitivityScenario] = Field(min_length=1)
    decision_threshold: FiniteFloat = 0.0


class SensitivityOutput(StrictModel):
    lower_bound: FiniteFloat
    upper_bound: FiniteFloat
    decision_threshold: FiniteFloat
    scenarios_at_or_above_threshold: list[dict[str, Any]]
    influence_ranking: list[dict[str, Any]]
    scenario_count: int = Field(ge=1)
    warning: str

    @model_validator(mode="after")
    def ordered_bounds(self) -> SensitivityOutput:
        if self.lower_bound > self.upper_bound:
            raise ValueError("sensitivity lower bound exceeds upper bound")
        return self


class SolverStatusInput(StrictModel):
    pass


class SolverStatusOutput(StrictModel):
    status: Literal["unavailable"]
    operation: str
    result: dict[str, Any]
    error: str
    capability: dict[str, Any]
    unavailable: Literal[True]


NumericResolver = Callable[[dict[str, Any]], NumericalExactness]


@dataclass(frozen=True, slots=True)
class ToolContract:
    name: str
    purpose: str
    supported_games: tuple[str, ...]
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    numeric_exactness_modes: tuple[NumericalExactness, ...]
    assumptions: tuple[str, ...]
    preconditions: tuple[str, ...]
    limits: dict[str, object]
    units: dict[str, str]
    failure_modes: tuple[str, ...]
    verification_checks: tuple[str, ...] = ()
    tolerance: TolerancePolicy | None = None
    model_qualifier: str | None = None
    version: str = "1.0.0"
    contract_version: str = "2.0.0"
    resolver: NumericResolver | None = field(default=None, repr=False, compare=False)

    def resolve_numeric_exactness(self, output: dict[str, Any]) -> NumericalExactness:
        resolved = (
            self.resolver(output) if self.resolver is not None else self.numeric_exactness_modes[0]
        )
        if resolved not in self.numeric_exactness_modes:
            raise ValueError(f"{self.name} resolved an undeclared numeric exactness: {resolved}")
        return resolved

    def verify_floating(
        self,
        payload: dict[str, Any],
        output: dict[str, Any],
    ) -> VerificationEvidence:
        if self.tolerance is None or not self.verification_checks:
            raise ValueError("floating-verified result lacks a typed verification policy")
        return verify_floating_result(
            self.name,
            payload,
            output,
            self.tolerance,
            self.verification_checks,
        )

    def manifest_entry(self) -> dict[str, object]:
        return {
            "name": self.name,
            "purpose": self.purpose,
            "supported_games": list(self.supported_games),
            "input_schema": self.input_model.model_json_schema(),
            "output_schema": self.output_model.model_json_schema(),
            "numeric_exactness_modes": [item.value for item in self.numeric_exactness_modes],
            "legacy_exactness_projection": {
                "exact": "exact",
                "exact-under-model": "exact",
                "floating-verified": "exact",
                "approximate": "approximate",
                "unavailable": "unavailable",
            },
            "assumptions": list(self.assumptions),
            "preconditions": list(self.preconditions),
            "limits": self.limits,
            "units": self.units,
            "failure_modes": list(self.failure_modes),
            "verification_checks": list(self.verification_checks),
            "tolerance": self.tolerance.model_dump(mode="json") if self.tolerance else None,
            "model_qualifier": self.model_qualifier,
            "version": self.version,
            "contract_version": self.contract_version,
            "command": (
                f"poker-deliberate calculate {self.name} --analysis-scope retrospective "
                "--input INPUT.json"
            ),
        }


def _ulp_policy(
    *fields: str,
    ulps: int,
    rationale: str,
    formula: str | None = None,
    unit: str = "output field unit",
) -> TolerancePolicy:
    return TolerancePolicy(
        fields=list(fields),
        kind="ulp",
        ulps=ulps,
        formula=formula,
        unit=unit,
        rationale=rationale,
    )


COMMON_FAILURES = (
    "strict input schema rejection (including extra or missing fields)",
    "documented precondition violation",
    "hard resource limit violation",
    "strict output schema or invariant failure",
)


def _combos_exactness(output: dict[str, Any]) -> NumericalExactness:
    return (
        NumericalExactness.EXACT
        if output.get("hand_class") is not None
        else NumericalExactness.FLOATING_VERIFIED
    )


def _equity_exactness(output: dict[str, Any]) -> NumericalExactness:
    return (
        NumericalExactness.FLOATING_VERIFIED
        if output.get("exact") is True
        else NumericalExactness.APPROXIMATE
    )


def _matrix_exactness(output: dict[str, Any]) -> NumericalExactness:
    return (
        NumericalExactness.FLOATING_VERIFIED
        if output.get("exact_algorithm") is True
        else NumericalExactness.APPROXIMATE
    )


def tool_contracts() -> tuple[ToolContract, ...]:
    """Return the stable 20-tool canonical inventory in registry order."""

    contracts = (
        ToolContract(
            "pot_odds",
            "Pot odds and required equity after a bet and optional declared rake.",
            ("NLHE", "PLO", "generic"),
            PotOddsInput,
            PotOddsOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("All amounts use one currency or chip unit.",),
            ("call_cost > 0", "expected_rake < final pot before rake"),
            {"time_complexity": "O(1)"},
            {"amounts": "one caller-declared chip/currency unit", "required_equity": "fraction"},
            COMMON_FAILURES,
            ("formula identities", "finite typed output"),
            _ulp_policy(
                "pot_after_opponent_bet",
                "final_pot_before_rake",
                "expected_rake",
                "required_equity",
                "required_equity_percent",
                "final_pot_after_rake",
                "pot_odds_against",
                ulps=16,
                rationale=(
                    "Bounded O(1) IEEE-754 arithmetic; the bound scales with result magnitude."
                ),
            ),
        ),
        ToolContract(
            "break_even_fold",
            "Zero-equity bluff break-even fold frequency.",
            ("generic",),
            BreakEvenFoldInput,
            BreakEvenFoldOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Called branch has zero equity; risk and reward are incremental.",),
            ("risk > 0", "reward > 0"),
            {"time_complexity": "O(1)"},
            {"risk": "one value unit", "reward": "same value unit", "frequency": "fraction"},
            COMMON_FAILURES,
            ("frequency/percent identity", "frequency lies in [0,1]"),
            _ulp_policy(
                "risk",
                "reward",
                "break_even_fold_frequency",
                "break_even_fold_percent",
                ulps=16,
                rationale="One division and one addition in IEEE-754 binary64.",
            ),
        ),
        ToolContract(
            "mdf",
            "One-bet toy-model minimum defense frequency.",
            ("generic",),
            MDFInput,
            MDFOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Single bet, no future action, zero-equity bluff indifference model.",),
            ("pot_before_bet > 0", "bet > 0"),
            {"time_complexity": "O(1)"},
            {"amounts": "one caller-declared unit", "frequency": "fraction"},
            COMMON_FAILURES,
            ("frequency/percent identity", "frequency domain and formula metadata"),
            _ulp_policy(
                "minimum_defense_frequency",
                "minimum_defense_percent",
                ulps=16,
                rationale="One division and one addition in IEEE-754 binary64.",
            ),
            model_qualifier="one-bet zero-equity-bluff indifference model",
        ),
        ToolContract(
            "spr",
            "Stack-to-pot ratio at one supplied decision point.",
            ("NLHE", "PLO", "generic"),
            SPRInput,
            SPROutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Stack and pot refer to the same decision point and unit.",),
            ("pot > 0", "effective_stack >= 0"),
            {"time_complexity": "O(1)"},
            {"effective_stack": "caller unit", "pot": "same unit", "spr": "ratio"},
            COMMON_FAILURES,
            ("ratio identity", "formula metadata"),
            _ulp_policy("spr", ulps=8, rationale="One IEEE-754 binary64 division."),
        ),
        ToolContract(
            "effective_stack",
            "Minimum of supplied remaining stacks.",
            ("NLHE", "PLO", "generic"),
            EffectiveStackInput,
            EffectiveStackOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Stacks use one unit and identify the relevant active players.",),
            ("at least two non-negative finite stacks",),
            {"time_complexity": "O(players)"},
            {"stacks": "one caller-declared chip/currency unit"},
            COMMON_FAILURES,
            ("effective_stack equals min(stacks)",),
            _ulp_policy(
                "effective_stack",
                ulps=1,
                rationale="The validated binary64 input value is selected without arithmetic.",
            ),
        ),
        ToolContract(
            "rake_amount",
            "Declared percentage rake with an optional cap.",
            ("cash", "generic"),
            RakeAmountInput,
            RakeAmountOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("rake_percent uses percentage points and the declared rake timing applies.",),
            ("0 <= rake_percent < 100", "pot and cap are non-negative"),
            {"time_complexity": "O(1)"},
            {"pot/rake/cap": "one caller-declared unit", "rake_percent": "percentage points"},
            COMMON_FAILURES,
            ("rake/cap identities", "formula metadata"),
            _ulp_policy(
                "rake_amount",
                "raw_rake",
                ulps=16,
                rationale="One multiplication and one division plus an exact minimum selection.",
            ),
        ),
        ToolContract(
            "raked_call_ev",
            "Call EV with declared final-pot rake and no future betting.",
            ("cash", "generic"),
            RakedCallEVInput,
            RakedCallEVOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("No future betting; supplied equity; rake is taken from the final pot.",),
            ("equity in [0,1]", "call_cost > 0", "0 <= rake_percent < 100"),
            {"time_complexity": "O(1)"},
            {"EV/amounts": "one caller-declared unit", "equity": "fraction"},
            COMMON_FAILURES,
            ("EV/rake identities", "model and formula metadata"),
            _ulp_policy(
                "ev",
                "rake_amount",
                "final_pot_after_rake",
                ulps=32,
                rationale="Bounded straight-line binary64 arithmetic with declared inputs.",
            ),
            model_qualifier="single decision, no future betting, declared final-pot rake",
        ),
        ToolContract(
            "bluff_ev",
            "Single-street bluff or semi-bluff EV.",
            ("generic",),
            BluffEVInput,
            BluffEVOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Call-or-fold response; no rake or future betting.",),
            ("frequencies in [0,1]", "pot and bet > 0"),
            {"time_complexity": "O(1)"},
            {"EV/amounts": "one caller-declared unit", "frequencies": "fraction"},
            COMMON_FAILURES,
            ("EV branch identities", "model and formula metadata"),
            _ulp_policy(
                "ev",
                "called_branch_ev",
                ulps=32,
                rationale="Bounded straight-line binary64 arithmetic.",
            ),
            model_qualifier="single-street call-or-fold response",
        ),
        ToolContract(
            "polar_river_bluff_fraction",
            "Polarized river bluff fraction against a bluff-catcher.",
            ("NLHE", "PLO", "generic"),
            PolarRiverInput,
            PolarRiverOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("River; polarized range; bluff-catcher; no rake.",),
            ("pot_before_bet > 0", "bet > 0"),
            {"time_complexity": "O(1)"},
            {"amounts": "one caller-declared unit", "bluff_fraction": "fraction"},
            COMMON_FAILURES,
            ("fraction/percent identity", "model and formula metadata"),
            _ulp_policy(
                "bluff_fraction",
                "bluff_percent",
                ulps=16,
                rationale="One division and two bounded additions/multiplications.",
            ),
            model_qualifier="polarized river bettor versus bluff-catcher",
        ),
        ToolContract(
            "bayes_update",
            "Bayesian posterior from supplied prior and likelihoods.",
            ("generic",),
            BayesInput,
            BayesOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Prior and likelihoods are supplied assumptions, not inferred population data.",),
            ("all probabilities in [0,1]", "evidence probability > 0"),
            {"time_complexity": "O(1)"},
            {"probabilities": "fraction"},
            COMMON_FAILURES,
            ("posterior/evidence identity", "probability domain and formula metadata"),
            _ulp_policy(
                "posterior",
                "evidence_probability",
                ulps=32,
                rationale="Bounded straight-line binary64 probability arithmetic.",
            ),
            model_qualifier="Bayes rule conditional on supplied prior and likelihoods",
        ),
        ToolContract(
            "pot_reconstruction",
            "Pot reconstruction from incremental contributions.",
            ("generic",),
            PotReconstructionInput,
            PotReconstructionOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Contributions are incremental and use one unit.",),
            ("starting pot and every contribution are non-negative and finite",),
            {"time_complexity": "O(contributions)", "payload_bytes": 1_000_000},
            {"pot/contributions": "one caller-declared unit"},
            COMMON_FAILURES,
            ("running-pot length and ordering", "final-pot sum invariant"),
            _ulp_policy(
                "pots_after_each_contribution",
                "final_pot",
                ulps=16,
                formula=(
                    "ULP bound is applied per addition and checked by exact Decimal oracle tests."
                ),
                rationale="Accumulated binary64 additions; error grows with contribution count.",
            ),
        ),
        ToolContract(
            "combos",
            "Hold'em combo expansion, weights, and blocker removal.",
            ("NLHE",),
            CombosInput,
            CombosOutput,
            (NumericalExactness.EXACT, NumericalExactness.FLOATING_VERIFIED),
            ("Cards use canonical two-character notation.",),
            ("exactly one of hand_class or range", "range retains at least one legal combo"),
            {"time_complexity": "O(range combos)", "dead_cards": 52},
            {"combo_count": "count", "weights": "fraction"},
            COMMON_FAILURES,
            ("combo count matches list", "normalized weights sum to one"),
            _ulp_policy(
                "normalized_weights",
                ulps=32,
                rationale=(
                    "Only weighted-range normalization uses binary64; pure combo expansion "
                    "is exact."
                ),
            ),
            resolver=_combos_exactness,
        ),
        ToolContract(
            "holdem_equity",
            "Heads-up Hold'em equity by bounded complete enumeration or seeded Monte Carlo.",
            ("NLHE",),
            HoldemEquityInput,
            HoldemEquityOutput,
            (NumericalExactness.FLOATING_VERIFIED, NumericalExactness.APPROXIMATE),
            ("Heads-up only; weighted combo independence before overlap filtering.",),
            ("legal non-overlapping ranges", "board has 0, 3, 4, or 5 cards"),
            {
                "exact_evaluations": HARD_MAX_EXACT_EVALUATIONS,
                "monte_carlo_samples": HARD_MAX_MONTE_CARLO_SAMPLES,
            },
            {"hero_equity": "fraction", "confidence_interval_95": "fraction"},
            COMMON_FAILURES,
            (
                "outcome counts equal evaluations/samples",
                "equity and interval lie in [0,1]",
                "seeded Monte Carlo metadata",
            ),
            _ulp_policy(
                "hero_equity",
                ulps=128,
                formula=(
                    "Enumeration bound scales with weighted accumulation length; Monte Carlo "
                    "uses its interval."
                ),
                rationale=(
                    "Weighted binary64 accumulation is verified separately from sampling error."
                ),
            ),
            resolver=_equity_exactness,
        ),
        ToolContract(
            "ev_tree",
            "Expected value of a fully supplied finite probability tree.",
            ("generic",),
            EVTreeInput,
            EVTreeOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Branch probabilities are supplied and sum to one.",),
            ("finite acyclic tree", "each decision node has a normalized probability distribution"),
            {"nodes": EV_TREE_MAX_NODES, "depth": EV_TREE_MAX_DEPTH},
            {"probability": "fraction", "payoff/expected_value": "caller value unit"},
            COMMON_FAILURES,
            ("acyclic traversal", "probability normalization", "node value identities"),
            _ulp_policy(
                "branch probability sums",
                "expected_value",
                "node_values",
                ulps=64,
                formula=(
                    "Bound scales with tree depth and branch accumulation; Fraction oracle "
                    "cases are tested."
                ),
                rationale="Finite binary64 weighted sums over a bounded tree.",
            ),
        ),
        ToolContract(
            "icm",
            "Independent Chip Model expected payouts.",
            ("tournament",),
            ICMInput,
            ICMOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("ICM independence assumption; no future-game, skill, bounty, or risk model.",),
            ("at least one positive stack", "payouts non-increasing and within active places"),
            {"listed_players": 100, "active_players": 12, "time_complexity": "O(n * 2^n)"},
            {"stacks/equities/payouts": "one caller-declared prize unit"},
            COMMON_FAILURES,
            ("prize conservation", "player symmetry", "zero-stack boundary", "finite recursion"),
            _ulp_policy(
                "equities",
                "equity_sum",
                "sum_error",
                ulps=64,
                formula=(
                    "Conservation bound scales with active-player recursion and payable prize "
                    "magnitude."
                ),
                rationale=(
                    "ICM is mathematically model-conditional but implemented by binary64 recursion."
                ),
            ),
            model_qualifier="Independent Chip Model",
        ),
        ToolContract(
            "matrix_game",
            "Small two-player zero-sum matrix solution with a verified duality gap.",
            ("matrix",),
            MatrixGameInput,
            MatrixGameOutput,
            (NumericalExactness.FLOATING_VERIFIED, NumericalExactness.APPROXIMATE),
            ("Two players and zero-sum payoffs.",),
            ("finite non-empty rectangular matrix",),
            {
                "dimension": HARD_MAX_DIMENSION,
                "support_size": HARD_MAX_SUPPORT_SIZE,
                "support_candidates": HARD_MAX_SUPPORT_CANDIDATES,
                "fallback_iterations": HARD_MAX_FALLBACK_ITERATIONS,
                "fictitious_work": HARD_MAX_FICTITIOUS_WORK,
            },
            {"payoff/value/duality_gap": "caller payoff unit", "strategy": "probability"},
            COMMON_FAILURES,
            (
                "strategy normalization",
                "support feasibility",
                "duality gap",
                "best-response bounds",
            ),
            TolerancePolicy(
                fields=[
                    "strategy normalization",
                    "support feasibility",
                    "value",
                    "duality_gap",
                ],
                kind="caller-supplied",
                formula=(
                    "max(input tolerance, 64 ULPs at matrix magnitude); support residual "
                    "checks use documented bounded multiples and report the applied value"
                ),
                unit="caller payoff unit",
                rationale="Matrix conditioning is input-dependent; a universal epsilon is unsound.",
            ),
            model_qualifier="finite two-player zero-sum normal-form game",
            resolver=_matrix_exactness,
        ),
        ToolContract(
            "fixed_strategy_best_response",
            "Best response to a fully fixed opponent strategy in a bounded finite game.",
            ("finite_extensive_form",),
            BestResponseInput,
            BestResponseOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            (
                "Perfect recall; finite acyclic tree; opponent strategy fixed at every "
                "information set.",
            ),
            ("two-player zero-sum payoff convention", "consistent information-set actions"),
            {
                "pure_policies": HARD_MAX_PURE_POLICIES,
                "nodes": BEST_RESPONSE_MAX_NODES,
                "policy_node_work": HARD_MAX_POLICY_NODE_EVALUATIONS,
                "depth": 256,
            },
            {"value/payoff": "caller payoff unit", "probability": "fraction"},
            COMMON_FAILURES,
            (
                "one responder action per information set",
                "chance and fixed opponent distributions",
                "reported policy value",
                "explicit non-equilibrium flag",
            ),
            _ulp_policy(
                "chance probability sums",
                "fixed strategy probability sums",
                "value",
                "player0_value",
                ulps=64,
                formula="Bound scales with chance/opponent weighted-sum depth.",
                rationale=(
                    "Exhaustive policy selection is discrete; policy values use binary64 "
                    "arithmetic."
                ),
            ),
            model_qualifier="best response to one fully fixed opponent strategy",
        ),
        ToolContract(
            "hand_validator",
            "Canonical card, action, stack, and pot validation for declared rules.",
            ("NLHE", "PLO"),
            HandValidatorInput,
            HandValidatorOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Action amounts are incremental; unsupported site rules remain limitations.",),
            ("canonical hand schema", "finite non-negative amounts"),
            {"table_size": 10, "board_cards": 5, "hero_cards": 4},
            {"pots/stacks/actions": "one caller-declared chip unit"},
            COMMON_FAILURES,
            (
                "card uniqueness",
                "stack/pot reconstruction",
                "action legality",
                "limitation disclosure",
            ),
            _ulp_policy(
                "pot and stack comparisons",
                ulps=32,
                formula=(
                    "default applied ULP count is max(32, 4*(actions+players)); caller "
                    "override is recorded as an absolute bound"
                ),
                rationale=(
                    "Chip comparison precision must scale with the supplied hand rather than "
                    "a global epsilon."
                ),
                unit="caller chip unit",
            ),
            model_qualifier="declared canonical hand rules profile",
        ),
        ToolContract(
            "sensitivity",
            "Bounds and influence ranking over a supplied scenario grid.",
            ("generic",),
            SensitivityInput,
            SensitivityOutput,
            (NumericalExactness.FLOATING_VERIFIED,),
            ("Bounds and associations apply only to the supplied grid.",),
            ("at least one finite-valued scenario", "parameters are JSON-serializable"),
            {"payload_bytes": 1_000_000},
            {"values/bounds/threshold": "caller value unit", "impact": "same unit"},
            COMMON_FAILURES,
            ("ordered bounds", "scenario count", "descending influence ranking"),
            _ulp_policy(
                "setting means",
                "impact",
                ulps=64,
                formula="Mean and spread bound scales with observations per setting.",
                rationale=(
                    "Grid values are finite binary64 and aggregation depth is bounded by "
                    "payload size."
                ),
            ),
            model_qualifier="caller-supplied finite scenario grid",
        ),
        ToolContract(
            "solver_status",
            "External solver capability discovery without a strategy result.",
            ("NLHE",),
            SolverStatusInput,
            SolverStatusOutput,
            (NumericalExactness.UNAVAILABLE,),
            (),
            ("no input fields are accepted",),
            {"external_execution": "not performed"},
            {},
            COMMON_FAILURES,
            ("unavailable status", "empty result", "capability.available=false"),
        ),
    )
    names = [contract.name for contract in contracts]
    if len(contracts) != 20 or len(names) != len(set(names)):
        raise RuntimeError("canonical tool inventory must contain exactly 20 unique tools")
    return contracts


def contract_by_name() -> dict[str, ToolContract]:
    return {contract.name: contract for contract in tool_contracts()}
