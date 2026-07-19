"""Calculator registry that always emits auditable ToolResult objects."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from poker_deliberation.schemas import CanonicalHand, Exactness, ToolResult, ToolStatus
from poker_deliberation.tools.best_response import best_response_to_fixed_strategy
from poker_deliberation.tools.combinations import combo_summary, parse_weighted_range
from poker_deliberation.tools.equity import holdem_equity
from poker_deliberation.tools.ev_tree import evaluate_ev_tree
from poker_deliberation.tools.hand_validator import validate_hand
from poker_deliberation.tools.icm import calculate_icm
from poker_deliberation.tools.matrix_game import solve_zero_sum_matrix
from poker_deliberation.tools.pot_odds import (
    break_even_fold_frequency,
    pot_odds,
    reconstruct_pot,
)
from poker_deliberation.tools.sensitivity import analyze_scenarios
from poker_deliberation.tools.solver_adapter import UnavailableSolverAdapter
from poker_deliberation.tools.strategy_math import (
    bayes_update,
    bluff_ev,
    effective_stack,
    minimum_defense_frequency,
    polar_river_bluff_fraction,
    rake_amount,
    raked_call_ev,
    stack_to_pot_ratio,
)

ToolFunction = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    purpose: str
    exact_or_approximate: str
    supported_games: tuple[str, ...]
    function: ToolFunction
    assumptions: tuple[str, ...] = ()
    version: str = "1.0.0"


class ToolRegistry:
    def __init__(
        self,
        *,
        max_payload_bytes: int = 1_000_000,
        max_output_bytes: int = 1_000_000,
        max_duration_seconds: float = 30.0,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self.max_payload_bytes = max_payload_bytes
        self.max_output_bytes = max_output_bytes
        self.max_duration_seconds = max_duration_seconds

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool name: {definition.name}")
        self._tools[definition.name] = definition

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, object]]:
        return [
            {
                "name": tool.name,
                "purpose": tool.purpose,
                "exact_or_approximate": tool.exact_or_approximate,
                "supported_games": list(tool.supported_games),
                "assumptions": list(tool.assumptions),
                "version": tool.version,
            }
            for tool in sorted(self._tools.values(), key=lambda item: item.name)
        ]

    def execute(self, name: str, payload: dict[str, Any]) -> ToolResult:
        payload_size = len(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        if payload_size > self.max_payload_bytes:
            return ToolResult(
                tool_name=name,
                input={},
                status=ToolStatus.FAILED,
                exactness=Exactness.UNAVAILABLE,
                error=f"tool input exceeds hard limit {self.max_payload_bytes} bytes",
            )
        if name not in self._tools:
            return ToolResult(
                tool_name=name,
                input=payload,
                status=ToolStatus.UNAVAILABLE,
                exactness=Exactness.UNAVAILABLE,
                error=f"unknown tool: {name}",
                reproduce_command=None,
            )
        definition = self._tools[name]
        started = perf_counter()
        try:
            output = definition.function(payload)
            duration = perf_counter() - started
            if duration > self.max_duration_seconds:
                return ToolResult(
                    tool_name=name,
                    input=payload,
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    assumptions=list(definition.assumptions),
                    version=definition.version,
                    duration_seconds=duration,
                    error=(
                        "tool exceeded post-execution runtime limit "
                        f"{self.max_duration_seconds} seconds"
                    ),
                )
            output_size = len(
                json.dumps(output, ensure_ascii=False, sort_keys=True).encode("utf-8")
            )
            if output_size > self.max_output_bytes:
                return ToolResult(
                    tool_name=name,
                    input=payload,
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    assumptions=list(definition.assumptions),
                    version=definition.version,
                    duration_seconds=duration,
                    error=f"tool output exceeds hard limit {self.max_output_bytes} bytes",
                )
            unavailable = bool(output.get("unavailable", False))
            exactness = _infer_exactness(output, definition.exact_or_approximate)
            warnings = _extract_warnings(output)
            status = ToolStatus.UNAVAILABLE if unavailable else ToolStatus.SUCCESS
            error = str(output.get("error")) if unavailable and output.get("error") else None
            confidence_interval = output.get("confidence_interval_95")
            return ToolResult(
                tool_name=name,
                input=payload,
                output=output,
                status=status,
                exactness=Exactness.UNAVAILABLE if unavailable else exactness,
                assumptions=list(definition.assumptions),
                version=definition.version,
                seed=int(output["seed"]) if output.get("seed") is not None else None,
                samples=int(output["samples"]) if output.get("samples") is not None else None,
                confidence_interval=(
                    (float(confidence_interval[0]), float(confidence_interval[1]))
                    if isinstance(confidence_interval, list) and len(confidence_interval) == 2
                    else None
                ),
                duration_seconds=duration,
                warnings=warnings,
                error=error,
                reproduce_command=(
                    f"poker-deliberate calculate {name} --analysis-scope retrospective "
                    "--input <input.json>"
                ),
            )
        except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
            return ToolResult(
                tool_name=name,
                input=payload,
                status=ToolStatus.FAILED,
                exactness=Exactness.UNAVAILABLE,
                assumptions=list(definition.assumptions),
                version=definition.version,
                duration_seconds=perf_counter() - started,
                error=f"{type(exc).__name__}: {exc}",
                reproduce_command=(
                    f"poker-deliberate calculate {name} --analysis-scope retrospective "
                    "--input <input.json>"
                ),
            )


def _extract_warnings(output: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    warning = output.get("warning")
    if warning:
        warnings.append(str(warning))
    many = output.get("warnings")
    if isinstance(many, list):
        warnings.extend(str(item) for item in many)
    return warnings


def _infer_exactness(output: dict[str, Any], declared: str) -> Exactness:
    if output.get("exact") is False or output.get("exact_algorithm") is False:
        return Exactness.APPROXIMATE
    if declared == "approximate":
        return Exactness.APPROXIMATE
    return Exactness.EXACT


def _combo_tool(payload: dict[str, Any]) -> dict[str, Any]:
    if "range" in payload:
        combos = parse_weighted_range(
            str(payload["range"]), tuple(map(str, payload.get("dead_cards", [])))
        )
        total_weight = sum(combo.weight for combo in combos)
        return {
            "range": payload["range"],
            "combo_count": len(combos),
            "total_combo_weight": total_weight,
            "normalized_weights": [
                {"cards": list(combo.cards), "weight": combo.weight / total_weight}
                for combo in combos
            ],
        }
    return combo_summary(str(payload["hand_class"]), tuple(map(str, payload.get("dead_cards", []))))


def _equity_tool(payload: dict[str, Any]) -> dict[str, Any]:
    game_type = str(payload.get("game_type", "NLHE")).upper()
    if game_type != "NLHE":
        raise ValueError("holdem_equity supports NLHE only")
    if "opponent_ranges" in payload or "villain_ranges" in payload:
        raise ValueError("holdem_equity supports exactly one villain")
    return holdem_equity(
        hero_range=str(payload["hero_range"]),
        villain_range=str(payload["villain_range"]),
        board=tuple(map(str, payload.get("board", []))),
        dead_cards=tuple(map(str, payload.get("dead_cards", []))),
        mode=str(payload.get("mode", "auto")),
        max_exact_evaluations=int(payload.get("max_exact_evaluations", 250_000)),
        samples=int(payload.get("samples", 50_000)),
        seed=int(payload.get("seed", 0)),
    )


def _solver_status(_payload: dict[str, Any]) -> dict[str, Any]:
    response = UnavailableSolverAdapter().health_check().model_dump(mode="json")
    return {**response, "unavailable": True}


def default_registry(
    *,
    max_payload_bytes: int = 1_000_000,
    max_output_bytes: int = 1_000_000,
    max_duration_seconds: float = 30.0,
) -> ToolRegistry:
    registry = ToolRegistry(
        max_payload_bytes=max_payload_bytes,
        max_output_bytes=max_output_bytes,
        max_duration_seconds=max_duration_seconds,
    )
    definitions = [
        ToolDefinition(
            "pot_odds",
            "Pot odds and required equity after a bet and optional rake.",
            "exact",
            ("NLHE", "PLO", "generic"),
            lambda p: pot_odds(**p),
        ),
        ToolDefinition(
            "break_even_fold",
            "Break-even fold frequency for a zero-equity bluff.",
            "exact",
            ("generic",),
            lambda p: break_even_fold_frequency(**p),
            ("Called branch has zero equity unless represented elsewhere in an EV tree.",),
        ),
        ToolDefinition(
            "mdf",
            "Minimum defense frequency against one bet in the zero-equity-bluff toy model.",
            "exact",
            ("generic",),
            lambda p: minimum_defense_frequency(**p),
            ("Single bet; no future action; MDF is not a complete strategy prescription.",),
        ),
        ToolDefinition(
            "spr",
            "Stack-to-pot ratio from a supplied effective stack and pot.",
            "exact",
            ("NLHE", "PLO", "generic"),
            lambda p: stack_to_pot_ratio(**p),
        ),
        ToolDefinition(
            "effective_stack",
            "Effective stack as the minimum supplied remaining stack.",
            "exact",
            ("NLHE", "PLO", "generic"),
            lambda p: effective_stack(**p),
        ),
        ToolDefinition(
            "rake_amount",
            "Declared percentage rake with an optional cap.",
            "exact",
            ("cash", "generic"),
            lambda p: rake_amount(**p),
            ("rake_percent is expressed as percentage points, for example 5 for 5%.",),
        ),
        ToolDefinition(
            "raked_call_ev",
            "Call EV with declared final-pot rake in a no-future-betting model.",
            "exact",
            ("cash", "generic"),
            lambda p: raked_call_ev(**p),
            ("No future betting; equity is supplied; rake is taken from the final pot.",),
        ),
        ToolDefinition(
            "bluff_ev",
            "Bet EV against a supplied fold frequency and called-branch equity.",
            "exact",
            ("generic",),
            lambda p: bluff_ev(**p),
            ("Single street; opponent calls or folds; no rake or future betting.",),
        ),
        ToolDefinition(
            "polar_river_bluff_fraction",
            "Indifference bluff fraction for a polarized river toy model.",
            "exact",
            ("NLHE", "PLO", "generic"),
            lambda p: polar_river_bluff_fraction(**p),
            ("River only; polarized value/bluff range versus a bluff-catcher; no rake.",),
        ),
        ToolDefinition(
            "bayes_update",
            "Bayesian posterior from a supplied prior and likelihoods.",
            "exact",
            ("generic",),
            lambda p: bayes_update(**p),
            ("The supplied prior and likelihoods are assumptions, not inferred population data.",),
        ),
        ToolDefinition(
            "pot_reconstruction",
            "Reconstruct a pot from incremental contributions.",
            "exact",
            ("generic",),
            lambda p: reconstruct_pot(**p),
        ),
        ToolDefinition(
            "combos",
            "Expand pairs, suited, offsuit, and weighted Hold'em ranges with blockers.",
            "exact",
            ("NLHE",),
            _combo_tool,
        ),
        ToolDefinition(
            "holdem_equity",
            "Heads-up Hold'em equity by exact enumeration or seeded Monte Carlo.",
            "mixed",
            ("NLHE",),
            _equity_tool,
            ("Heads-up only in the MVP.",),
        ),
        ToolDefinition(
            "ev_tree",
            "Expected value of a finite tree with supplied branch probabilities.",
            "exact",
            ("generic",),
            evaluate_ev_tree,
        ),
        ToolDefinition(
            "icm",
            "Independent Chip Model expected payouts.",
            "exact",
            ("tournament",),
            lambda p: calculate_icm(list(map(float, p["stacks"])), list(map(float, p["payouts"]))),
            ("ICM independence assumption; no future-game simulation.",),
        ),
        ToolDefinition(
            "matrix_game",
            "Small two-player zero-sum matrix equilibrium and exploitability gap.",
            "mixed",
            ("matrix",),
            lambda p: solve_zero_sum_matrix(
                [[float(value) for value in row] for row in p["matrix"]],
                tolerance=float(p.get("tolerance", 1e-9)),
                max_support_size=int(p.get("max_support_size", 8)),
                fallback_iterations=int(p.get("fallback_iterations", 50_000)),
            ),
        ),
        ToolDefinition(
            "fixed_strategy_best_response",
            "Exact small-game best response with shared information-set actions.",
            "exact",
            ("finite_extensive_form",),
            lambda p: best_response_to_fixed_strategy(
                p["game"],
                p["fixed_strategy"],
                best_responder=int(p.get("best_responder", 0)),
                max_pure_policies=int(p.get("max_pure_policies", 1_000_000)),
            ),
            ("Opponent strategy is fixed at every opponent information set.",),
        ),
        ToolDefinition(
            "hand_validator",
            "Validate canonical hand cards, action order, stacks, and pots.",
            "exact",
            ("NLHE", "PLO"),
            lambda p: validate_hand(CanonicalHand.model_validate(p)),
        ),
        ToolDefinition(
            "sensitivity",
            "Bounds and influence ranking over a supplied scenario grid.",
            "exact",
            ("generic",),
            lambda p: analyze_scenarios(
                p["scenarios"], decision_threshold=float(p.get("decision_threshold", 0.0))
            ),
            ("Bounds apply only to the supplied scenario grid.",),
        ),
        ToolDefinition(
            "solver_status",
            "Discover external solver availability without fabricating output.",
            "unavailable",
            ("NLHE",),
            _solver_status,
        ),
    ]
    for definition in definitions:
        registry.register(definition)
    return registry
