"""Calculator registry that always emits auditable ToolResult objects."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from poker_deliberation.budgets import (
    BudgetFailure,
    BudgetFailureCode,
    BudgetLimitError,
    MonotonicClock,
    SystemMonotonicClock,
    canonical_json_utf8_size,
)
from poker_deliberation.schemas import (
    CanonicalHand,
    Exactness,
    NumericalErrorMetadata,
    NumericalExactness,
    ToolResult,
    ToolStatus,
    VerificationMetadata,
)
from poker_deliberation.tools.best_response import best_response_to_fixed_strategy
from poker_deliberation.tools.combinations import combo_summary, parse_weighted_range
from poker_deliberation.tools.contracts import (
    VERSIONED_RANGE_BRIDGE_TOOL_NAMES,
    RangeValidateInput,
    ToolContract,
    contract_by_name,
    versioned_range_bridge_failure_error,
    versioned_range_bridge_failure_input_matches,
)
from poker_deliberation.tools.equity import holdem_equity
from poker_deliberation.tools.ev_tree import evaluate_ev_tree
from poker_deliberation.tools.hand_pot_ledger import calculate_hand_pot_ledger
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


def _failure_error(tool_name: str, diagnostic: str, *, bind_versioned_range: bool) -> str:
    if not bind_versioned_range or tool_name not in VERSIONED_RANGE_BRIDGE_TOOL_NAMES:
        return diagnostic
    return versioned_range_bridge_failure_error(tool_name)


class ToolByteLimitError(RuntimeError):
    """Typed internal signal used by the phase boundary without changing public results."""

    def __init__(self, resource: str, *, limit: int, observed: int) -> None:
        super().__init__(f"{resource} exceeds hard limit {limit} bytes")
        self.resource = resource
        self.limit = limit
        self.observed = observed


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    purpose: str
    exact_or_approximate: str
    supported_games: tuple[str, ...]
    function: ToolFunction
    assumptions: tuple[str, ...] = ()
    version: str = "1.0.0"
    contract: ToolContract | None = None


class ToolRegistry:
    def __init__(
        self,
        *,
        max_payload_bytes: int = 1_000_000,
        max_output_bytes: int = 1_000_000,
        max_duration_seconds: float = 30.0,
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self.max_payload_bytes = max_payload_bytes
        self.max_output_bytes = max_output_bytes
        if isinstance(max_duration_seconds, bool) or not isinstance(
            max_duration_seconds, (int, float)
        ):
            raise TypeError("max_duration_seconds must be numeric")
        if not math.isfinite(float(max_duration_seconds)) or max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be finite and positive")
        self.max_duration_seconds = max_duration_seconds
        self.monotonic_clock = monotonic_clock or SystemMonotonicClock()

    def _read_clock(self) -> int:
        value = self.monotonic_clock.now_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("monotonic clock must return non-negative integer nanoseconds")
        return value

    def _duration_seconds(self, started_ns: int) -> float:
        completed_ns = self._read_clock()
        if completed_ns < started_ns:
            raise ValueError("monotonic clock moved backwards during tool execution")
        return (completed_ns - started_ns) / 1_000_000_000

    def _failure_duration_seconds(self, started_ns: int) -> float:
        try:
            return self._duration_seconds(started_ns)
        except (ValueError, TypeError):
            return 0.0

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool name: {definition.name}")
        if definition.contract is not None and definition.contract.name != definition.name:
            raise ValueError("tool definition name must match its typed contract")
        self._tools[definition.name] = definition

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, object]]:
        descriptions: list[dict[str, object]] = []
        for tool in sorted(self._tools.values(), key=lambda item: item.name):
            if tool.contract is not None:
                descriptions.append(tool.contract.manifest_entry())
                continue
            descriptions.append(
                {
                    "name": tool.name,
                    "purpose": tool.purpose,
                    "exact_or_approximate": tool.exact_or_approximate,
                    "supported_games": list(tool.supported_games),
                    "assumptions": list(tool.assumptions),
                    "version": tool.version,
                }
            )
        return descriptions

    def runtime_identity_snapshot(
        self,
    ) -> tuple[tuple[str, ToolDefinition, ToolFunction, ToolContract | None], ...]:
        """Return in-process identities that the serial runtime actually invokes."""

        return tuple(
            (name, definition, definition.function, definition.contract)
            for name, definition in sorted(self._tools.items())
        )

    def execute(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
        _bind_versioned_range_failure: bool = False,
        _raise_on_byte_limit: bool = False,
        _budget_observed_at_ns: int | None = None,
        _run_deadline_ns: int | None = None,
        _runtime_limit_ns: int | None = None,
        _active_runtime_ns: int | None = None,
        _runtime_not_before_ns: int | None = None,
        _observation_sink: list[int] | None = None,
    ) -> ToolResult:
        runtime_values = (
            _budget_observed_at_ns,
            _run_deadline_ns,
            _runtime_limit_ns,
            _active_runtime_ns,
            _runtime_not_before_ns,
        )
        if any(item is not None for item in runtime_values) and any(
            item is None for item in runtime_values
        ):
            raise ValueError("tool runtime boundary values must be provided together")

        def read_phase_clock(not_before_ns: int) -> int:
            try:
                value = self._read_clock()
            except Exception as exc:
                raise BudgetLimitError(
                    BudgetFailure(
                        code=BudgetFailureCode.USAGE_MALFORMED,
                        resource="clock",
                        message=f"monotonic clock read failed: {type(exc).__name__}",
                    )
                ) from exc
            if _observation_sink is not None:
                _observation_sink.append(value)
            if value < not_before_ns:
                raise BudgetLimitError(
                    BudgetFailure(
                        code=BudgetFailureCode.CLOCK_ROLLBACK,
                        resource="active_runtime_ns",
                        message="monotonic clock moved backwards before or during tool execution",
                        observed=not_before_ns - value,
                    )
                )
            if _run_deadline_ns is not None and value >= _run_deadline_ns:
                raise BudgetLimitError(
                    BudgetFailure(
                        code=BudgetFailureCode.RUNTIME_EXCEEDED,
                        resource="active_runtime_ns",
                        message="active runtime expired before or during tool execution",
                        limit=_runtime_limit_ns,
                        observed=(
                            (_active_runtime_ns or 0) + value - (_budget_observed_at_ns or 0)
                        ),
                    )
                )
            return value

        known_definition = self._tools.get(name)
        known_contract = known_definition.contract if known_definition is not None else None
        payload_size = canonical_json_utf8_size(payload)
        _bind_versioned_range_failure = (
            _bind_versioned_range_failure
            and payload_size <= self.max_payload_bytes
            and known_contract is not None
            and contract_version == known_contract.contract_version
            and versioned_range_bridge_failure_input_matches(
                name,
                payload,
                contract_version,
            )
        )
        if payload_size > self.max_payload_bytes:
            if _raise_on_byte_limit:
                raise ToolByteLimitError(
                    "tool_input_bytes",
                    limit=self.max_payload_bytes,
                    observed=payload_size,
                )
            return ToolResult(
                tool_name=name,
                input={},
                status=ToolStatus.FAILED,
                exactness=Exactness.UNAVAILABLE,
                numeric_exactness=NumericalExactness.UNAVAILABLE,
                contract_version=(known_contract.contract_version if known_contract else "1.0.0"),
                error=_failure_error(
                    name,
                    f"tool input exceeds hard limit {self.max_payload_bytes} bytes",
                    bind_versioned_range=_bind_versioned_range_failure,
                ),
            )
        if known_definition is None:
            return ToolResult(
                tool_name=name,
                input=payload,
                status=ToolStatus.UNAVAILABLE,
                exactness=Exactness.UNAVAILABLE,
                numeric_exactness=NumericalExactness.UNAVAILABLE,
                error=f"unknown tool: {name}",
                reproduce_command=None,
            )
        definition = known_definition
        started = 0
        try:
            started = (
                read_phase_clock(_runtime_not_before_ns or 0)
                if _run_deadline_ns is not None
                else self._read_clock()
            )
            contract = definition.contract
            if (
                contract is not None
                and contract_version is not None
                and contract_version != contract.contract_version
            ):
                raise ValueError(
                    f"contract version mismatch: requested {contract_version}, "
                    f"supported {contract.contract_version}"
                )

            normalized_payload = payload
            if contract is not None:
                validated_input = contract.input_model.model_validate(payload)
                normalized_payload = validated_input.model_dump(mode="python", exclude_unset=True)
            effect_started_ns = (
                read_phase_clock(max(started, _runtime_not_before_ns or 0))
                if _run_deadline_ns is not None
                else started
            )
            output = definition.function(normalized_payload)
            if contract is not None:
                contract.output_model.model_validate(output)
            if _run_deadline_ns is not None:
                effect_completed_ns = read_phase_clock(effect_started_ns)
                duration = (effect_completed_ns - started) / 1_000_000_000
            else:
                effect_completed_ns = started
                duration = self._duration_seconds(started)
            output_size = canonical_json_utf8_size(output)
            if output_size > self.max_output_bytes:
                if _raise_on_byte_limit:
                    raise ToolByteLimitError(
                        "tool_output_bytes",
                        limit=self.max_output_bytes,
                        observed=output_size,
                    )
                return ToolResult(
                    tool_name=name,
                    input=payload,
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    numeric_exactness=NumericalExactness.UNAVAILABLE,
                    contract_version=(
                        definition.contract.contract_version
                        if definition.contract is not None
                        else "1.0.0"
                    ),
                    assumptions=list(definition.assumptions),
                    version=definition.version,
                    duration_seconds=duration,
                    error=_failure_error(
                        name,
                        f"tool output exceeds hard limit {self.max_output_bytes} bytes",
                        bind_versioned_range=_bind_versioned_range_failure,
                    ),
                    reproduce_command=(
                        f"poker-deliberate calculate {name} --analysis-scope retrospective "
                        "--input <input.json>"
                    ),
                )
            unavailable = bool(output.get("unavailable", False))
            numeric_exactness = (
                NumericalExactness.UNAVAILABLE
                if unavailable
                else (
                    contract.resolve_numeric_exactness(output)
                    if contract is not None
                    else _legacy_numeric_exactness(output, definition.exact_or_approximate)
                )
            )
            exactness = _legacy_exactness_projection(numeric_exactness)
            warnings = _extract_warnings(output)
            if numeric_exactness in {
                NumericalExactness.EXACT_UNDER_MODEL,
                NumericalExactness.FLOATING_VERIFIED,
            }:
                warnings.append(
                    "legacy exactness='exact' is only a compatibility projection; "
                    f"use numeric_exactness='{numeric_exactness.value}'"
                )
            status = ToolStatus.UNAVAILABLE if unavailable else ToolStatus.SUCCESS
            error = str(output.get("error")) if unavailable and output.get("error") else None
            confidence_interval = output.get("confidence_interval_95")
            approximate_metadata = _approximate_metadata(name, output, numeric_exactness)
            verification = _verification_metadata(
                contract,
                numeric_exactness,
                normalized_payload,
                output,
            )
            if _run_deadline_ns is not None:
                verified_ns = read_phase_clock(effect_completed_ns)
                duration = (verified_ns - started) / 1_000_000_000
            else:
                duration = self._duration_seconds(started)
            if duration > self.max_duration_seconds:
                return ToolResult(
                    tool_name=name,
                    input=payload,
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    numeric_exactness=NumericalExactness.UNAVAILABLE,
                    contract_version=(
                        definition.contract.contract_version
                        if definition.contract is not None
                        else "1.0.0"
                    ),
                    assumptions=list(definition.assumptions),
                    version=definition.version,
                    duration_seconds=duration,
                    error=_failure_error(
                        name,
                        (
                            "tool plus verification exceeded post-execution runtime limit "
                            f"{self.max_duration_seconds} seconds"
                        ),
                        bind_versioned_range=_bind_versioned_range_failure,
                    ),
                    reproduce_command=(
                        f"poker-deliberate calculate {name} --analysis-scope retrospective "
                        "--input <input.json>"
                    ),
                )
            return ToolResult(
                tool_name=name,
                input=payload,
                output=output,
                status=status,
                exactness=exactness,
                numeric_exactness=numeric_exactness,
                contract_version=contract.contract_version if contract is not None else "1.0.0",
                assumptions=list(definition.assumptions),
                version=definition.version,
                model_qualifier=contract.model_qualifier if contract is not None else None,
                method=str(output["method"]) if output.get("method") is not None else None,
                stochastic=approximate_metadata.get("stochastic"),
                seed=int(output["seed"]) if output.get("seed") is not None else None,
                samples=int(output["samples"]) if output.get("samples") is not None else None,
                iterations=(
                    int(output["iterations"]) if output.get("iterations") is not None else None
                ),
                confidence_interval=(
                    (float(confidence_interval[0]), float(confidence_interval[1]))
                    if isinstance(confidence_interval, list) and len(confidence_interval) == 2
                    else None
                ),
                confidence_level=approximate_metadata.get("confidence_level"),
                error_metadata=approximate_metadata.get("error_metadata"),
                stopping_condition=approximate_metadata.get("stopping_condition"),
                verification=verification,
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
                numeric_exactness=NumericalExactness.UNAVAILABLE,
                contract_version=(
                    definition.contract.contract_version
                    if definition.contract is not None
                    else "1.0.0"
                ),
                assumptions=list(definition.assumptions),
                version=definition.version,
                duration_seconds=self._failure_duration_seconds(started),
                error=_failure_error(
                    name,
                    f"{type(exc).__name__}: {exc}",
                    bind_versioned_range=_bind_versioned_range_failure,
                ),
                reproduce_command=(
                    f"poker-deliberate calculate {name} --analysis-scope retrospective "
                    "--input <input.json>"
                ),
            )

    def execute_for_phase(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
        budget_observed_at_ns: int | None = None,
        run_deadline_ns: int | None = None,
        runtime_limit_ns: int | None = None,
        active_runtime_ns: int | None = None,
        runtime_not_before_ns: int | None = None,
        observation_sink: list[int] | None = None,
        _bind_versioned_range_failure: bool = False,
    ) -> ToolResult:
        """Execute with typed byte-limit signaling for the orchestrated phase boundary."""

        return self.execute(
            name,
            payload,
            contract_version=contract_version,
            _bind_versioned_range_failure=_bind_versioned_range_failure,
            _raise_on_byte_limit=True,
            _budget_observed_at_ns=budget_observed_at_ns,
            _run_deadline_ns=run_deadline_ns,
            _runtime_limit_ns=runtime_limit_ns,
            _active_runtime_ns=active_runtime_ns,
            _runtime_not_before_ns=runtime_not_before_ns,
            _observation_sink=observation_sink,
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


def _legacy_numeric_exactness(output: dict[str, Any], declared: str) -> NumericalExactness:
    if output.get("exact") is False or output.get("exact_algorithm") is False:
        return NumericalExactness.APPROXIMATE
    if declared == "approximate":
        return NumericalExactness.APPROXIMATE
    if declared == "unavailable":
        return NumericalExactness.UNAVAILABLE
    return NumericalExactness.EXACT


def _legacy_exactness_projection(numeric: NumericalExactness) -> Exactness:
    if numeric is NumericalExactness.APPROXIMATE:
        return Exactness.APPROXIMATE
    if numeric is NumericalExactness.UNAVAILABLE:
        return Exactness.UNAVAILABLE
    return Exactness.EXACT


def _verification_metadata(
    contract: ToolContract | None,
    numeric: NumericalExactness,
    payload: dict[str, Any],
    output: dict[str, Any],
) -> VerificationMetadata | None:
    if numeric is not NumericalExactness.FLOATING_VERIFIED:
        return None
    if contract is None:
        raise ValueError("floating-verified result lacks a typed verification policy")
    evidence = contract.verify_floating(payload, output)
    return VerificationMetadata(
        method="executed tool-specific invariant checks",
        checks=list(evidence.checks),
        observations=list(evidence.observations),
        tolerance=evidence.tolerance,
        passed=True,
    )


def _approximate_metadata(
    name: str,
    output: dict[str, Any],
    numeric: NumericalExactness,
) -> dict[str, Any]:
    if numeric is not NumericalExactness.APPROXIMATE:
        return {}
    method = str(output.get("method", ""))
    if name == "holdem_equity" and method == "monte_carlo":
        return {
            "stochastic": True,
            "confidence_level": 0.95,
            "stopping_condition": "fixed requested sample count",
        }
    if name == "matrix_game" and method == "fictitious_play_fallback":
        return {
            "stochastic": False,
            "error_metadata": NumericalErrorMetadata(
                metric="duality_gap",
                value=float(output["duality_gap"]),
                unit="caller payoff unit",
            ),
            "stopping_condition": "fixed fictitious-play iteration count",
        }
    raise ValueError(f"{name} approximate output lacks a registered metadata adapter")


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


def _range_validate_tool(payload: dict[str, Any]) -> dict[str, Any]:
    from poker_deliberation.range_grammar import validate_versioned_range

    request = RangeValidateInput.model_validate(payload)
    return validate_versioned_range(
        request.hand,
        request.range_definition,
    ).model_dump(mode="python")


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


def _hand_validator_tool(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    tolerance = normalized.pop("tolerance", None)
    hand = CanonicalHand.model_validate(normalized)
    return validate_hand(hand, tolerance=float(tolerance) if tolerance is not None else None)


def default_registry(
    *,
    max_payload_bytes: int = 1_000_000,
    max_output_bytes: int = 1_000_000,
    max_duration_seconds: float = 30.0,
    monotonic_clock: MonotonicClock | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(
        max_payload_bytes=max_payload_bytes,
        max_output_bytes=max_output_bytes,
        max_duration_seconds=max_duration_seconds,
        monotonic_clock=monotonic_clock,
    )
    definitions = [
        ToolDefinition(
            "pot_odds",
            "Pot odds and required equity after a bet and optional rake.",
            "floating-verified",
            ("NLHE", "PLO", "generic"),
            lambda p: pot_odds(**p),
        ),
        ToolDefinition(
            "break_even_fold",
            "Break-even fold frequency for a zero-equity bluff.",
            "floating-verified",
            ("generic",),
            lambda p: break_even_fold_frequency(**p),
            ("Called branch has zero equity unless represented elsewhere in an EV tree.",),
        ),
        ToolDefinition(
            "mdf",
            "Minimum defense frequency against one bet in the zero-equity-bluff toy model.",
            "floating-verified",
            ("generic",),
            lambda p: minimum_defense_frequency(**p),
            ("Single bet; no future action; MDF is not a complete strategy prescription.",),
        ),
        ToolDefinition(
            "spr",
            "Stack-to-pot ratio from a supplied effective stack and pot.",
            "floating-verified",
            ("NLHE", "PLO", "generic"),
            lambda p: stack_to_pot_ratio(**p),
        ),
        ToolDefinition(
            "effective_stack",
            "Effective stack as the minimum supplied remaining stack.",
            "floating-verified",
            ("NLHE", "PLO", "generic"),
            lambda p: effective_stack(**p),
        ),
        ToolDefinition(
            "rake_amount",
            "Declared percentage rake with an optional cap.",
            "floating-verified",
            ("cash", "generic"),
            lambda p: rake_amount(**p),
            ("rake_percent is expressed as percentage points, for example 5 for 5%.",),
        ),
        ToolDefinition(
            "raked_call_ev",
            "Call EV with declared final-pot rake in a no-future-betting model.",
            "floating-verified",
            ("cash", "generic"),
            lambda p: raked_call_ev(**p),
            ("No future betting; equity is supplied; rake is taken from the final pot.",),
        ),
        ToolDefinition(
            "bluff_ev",
            "Bet EV against a supplied fold frequency and called-branch equity.",
            "floating-verified",
            ("generic",),
            lambda p: bluff_ev(**p),
            ("Single street; opponent calls or folds; no rake or future betting.",),
        ),
        ToolDefinition(
            "polar_river_bluff_fraction",
            "Indifference bluff fraction for a polarized river toy model.",
            "floating-verified",
            ("NLHE", "PLO", "generic"),
            lambda p: polar_river_bluff_fraction(**p),
            ("River only; polarized value/bluff range versus a bluff-catcher; no rake.",),
        ),
        ToolDefinition(
            "bayes_update",
            "Bayesian posterior from a supplied prior and likelihoods.",
            "floating-verified",
            ("generic",),
            lambda p: bayes_update(**p),
            ("The supplied prior and likelihoods are assumptions, not inferred population data.",),
        ),
        ToolDefinition(
            "pot_reconstruction",
            "Reconstruct a pot from incremental contributions.",
            "floating-verified",
            ("generic",),
            lambda p: reconstruct_pot(**p),
        ),
        ToolDefinition(
            "range_validate",
            "Validate and canonicalize one provenance-qualified versioned NLHE range.",
            "exact",
            ("NLHE",),
            _range_validate_tool,
        ),
        ToolDefinition(
            "combos",
            "Expand pairs, suited, offsuit, and weighted Hold'em ranges with blockers.",
            "mixed",
            ("NLHE",),
            _combo_tool,
        ),
        ToolDefinition(
            "holdem_equity",
            "Heads-up Hold'em equity by complete enumeration or seeded Monte Carlo.",
            "mixed",
            ("NLHE",),
            _equity_tool,
            ("Heads-up only in the MVP.",),
        ),
        ToolDefinition(
            "ev_tree",
            "Expected value of a finite tree with supplied branch probabilities.",
            "floating-verified",
            ("generic",),
            evaluate_ev_tree,
        ),
        ToolDefinition(
            "icm",
            "Independent Chip Model expected payouts.",
            "floating-verified",
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
            "Exhaustive small-game best response with shared information-set actions.",
            "floating-verified",
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
            "floating-verified",
            ("NLHE", "PLO"),
            _hand_validator_tool,
        ),
        ToolDefinition(
            "hand_pot_ledger",
            "Exact profiled NLHE contribution, return, side-pot, and eligibility ledger.",
            "exact-under-model",
            ("NLHE cash",),
            calculate_hand_pot_ledger,
        ),
        ToolDefinition(
            "sensitivity",
            "Bounds and influence ranking over a supplied scenario grid.",
            "floating-verified",
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
    contracts = contract_by_name()
    if {definition.name for definition in definitions} != set(contracts):
        raise RuntimeError("registry function map and canonical tool contracts differ")
    for definition in definitions:
        contract = contracts[definition.name]
        registry.register(
            ToolDefinition(
                name=contract.name,
                purpose=contract.purpose,
                exact_or_approximate="/".join(
                    item.value for item in contract.numeric_exactness_modes
                ),
                supported_games=contract.supported_games,
                function=definition.function,
                assumptions=contract.assumptions,
                version=contract.version,
                contract=contract,
            )
        )
    return registry
