from __future__ import annotations

import math
from fractions import Fraction

import pytest

from poker_deliberation.schemas import NumericalExactness, ToolStatus
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.contracts import contract_by_name

pytestmark = pytest.mark.property


def _value(tool: str, payload: dict[str, object], field: str) -> float:
    result = default_registry().execute(tool, payload)
    assert result.status is ToolStatus.SUCCESS, result.error
    assert result.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
    value = result.output[field]
    assert isinstance(value, (int, float))
    return float(value)


def _assert_fraction_oracle(tool: str, actual: float, expected: Fraction) -> None:
    policy = contract_by_name()[tool].tolerance
    assert policy is not None and policy.kind == "ulp" and policy.ulps is not None
    expected_float = float(expected)
    bound = math.ulp(max(abs(actual), abs(expected_float), 1.0)) * policy.ulps
    assert abs(actual - expected_float) <= bound


@pytest.mark.parametrize(
    ("tool", "payload", "field", "expected"),
    [
        (
            "pot_odds",
            {"pot_before_bet": 100, "opponent_bet": 50, "call_cost": 50},
            "required_equity",
            Fraction(1, 4),
        ),
        (
            "break_even_fold",
            {"risk": 50, "reward": 100},
            "break_even_fold_frequency",
            Fraction(1, 3),
        ),
        (
            "mdf",
            {"pot_before_bet": 10, "bet": 5},
            "minimum_defense_frequency",
            Fraction(2, 3),
        ),
        ("spr", {"effective_stack": 15, "pot": 6}, "spr", Fraction(5, 2)),
        (
            "rake_amount",
            {"pot_total": 100, "rake_percent": 5},
            "rake_amount",
            Fraction(5, 1),
        ),
        (
            "raked_call_ev",
            {
                "equity": 0.5,
                "pot_after_bet": 75,
                "call_cost": 25,
                "rake_percent": 5,
                "rake_cap": 3,
            },
            "ev",
            Fraction(47, 2),
        ),
        (
            "bluff_ev",
            {"fold_frequency": 1 / 3, "pot_before_bet": 10, "bet": 5},
            "ev",
            Fraction(0, 1),
        ),
        (
            "polar_river_bluff_fraction",
            {"pot_before_bet": 10, "bet": 10},
            "bluff_fraction",
            Fraction(1, 3),
        ),
        (
            "bayes_update",
            {"prior": 0.5, "likelihood_given_h": 0.8, "likelihood_given_not_h": 0.4},
            "posterior",
            Fraction(2, 3),
        ),
    ],
)
def test_simple_calculators_match_independent_fraction_oracles(
    tool: str,
    payload: dict[str, object],
    field: str,
    expected: Fraction,
) -> None:
    _assert_fraction_oracle(tool, _value(tool, payload, field), expected)


@pytest.mark.parametrize(
    ("tool", "payload", "scaled_payload", "field"),
    [
        (
            "pot_odds",
            {"pot_before_bet": 100, "opponent_bet": 50, "call_cost": 50},
            {"pot_before_bet": 700, "opponent_bet": 350, "call_cost": 350},
            "required_equity",
        ),
        (
            "break_even_fold",
            {"risk": 50, "reward": 100},
            {"risk": 350, "reward": 700},
            "break_even_fold_frequency",
        ),
        (
            "mdf",
            {"pot_before_bet": 10, "bet": 5},
            {"pot_before_bet": 70, "bet": 35},
            "minimum_defense_frequency",
        ),
        (
            "spr",
            {"effective_stack": 15, "pot": 6},
            {"effective_stack": 105, "pot": 42},
            "spr",
        ),
        (
            "polar_river_bluff_fraction",
            {"pot_before_bet": 10, "bet": 10},
            {"pot_before_bet": 70, "bet": 70},
            "bluff_fraction",
        ),
    ],
)
def test_ratio_calculators_are_scale_invariant(
    tool: str,
    payload: dict[str, object],
    scaled_payload: dict[str, object],
    field: str,
) -> None:
    assert _value(tool, payload, field) == _value(tool, scaled_payload, field)


def test_effective_stack_and_pot_reconstruction_ordering_oracles() -> None:
    registry = default_registry()
    effective = registry.execute("effective_stack", {"stacks": [30, 10, 20]})
    reconstructed = registry.execute(
        "pot_reconstruction", {"starting_pot": 7, "contributions": [3, 5, 11]}
    )
    assert effective.output["effective_stack"] == 10
    assert reconstructed.output["pots_after_each_contribution"] == [10, 15, 26]
    assert reconstructed.output["final_pot"] == 26


def test_combo_blocker_and_weight_normalization_oracles() -> None:
    registry = default_registry()
    exact = registry.execute("combos", {"hand_class": "AA", "dead_cards": ["As"]})
    weighted = registry.execute("combos", {"range": "AKs@0.25,QQ@0.5"})
    assert exact.numeric_exactness is NumericalExactness.EXACT
    assert exact.output["count"] == 3
    assert weighted.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
    total = sum(item["weight"] for item in weighted.output["normalized_weights"])
    policy = contract_by_name()["combos"].tolerance
    assert policy is not None and policy.ulps is not None
    assert abs(total - 1.0) <= math.ulp(1.0) * policy.ulps


def test_equity_symmetry_and_seed_reproducibility() -> None:
    registry = default_registry()
    base = {
        "hero_range": "AsAh",
        "villain_range": "KcKd",
        "board": ["2c", "3d", "4h", "5s", "9c"],
        "mode": "exact",
    }
    hero = registry.execute("holdem_equity", base)
    villain = registry.execute(
        "holdem_equity",
        {**base, "hero_range": base["villain_range"], "villain_range": base["hero_range"]},
    )
    assert hero.output["hero_equity"] + villain.output["hero_equity"] == 1.0

    monte_carlo_input = {
        "hero_range": "AKs",
        "villain_range": "QQ",
        "mode": "monte_carlo",
        "samples": 100,
        "seed": 19,
    }
    first = registry.execute("holdem_equity", monte_carlo_input)
    second = registry.execute("holdem_equity", monte_carlo_input)
    assert first.output == second.output
    assert first.confidence_interval == second.confidence_interval


def test_ev_tree_matches_fraction_oracle_and_scales_payoffs() -> None:
    registry = default_registry()
    tree = {
        "root": "root",
        "nodes": {
            "root": {
                "branches": [
                    {"probability": 0.25, "child": "win"},
                    {"probability": 0.75, "child": "lose"},
                ]
            },
            "win": {"payoff": 10},
            "lose": {"payoff": -2},
        },
    }
    result = registry.execute("ev_tree", tree)
    _assert_fraction_oracle("ev_tree", result.output["expected_value"], Fraction(1, 1))
    scaled = registry.execute(
        "ev_tree",
        {
            **tree,
            "nodes": {**tree["nodes"], "win": {"payoff": 50}, "lose": {"payoff": -10}},
        },
    )
    assert scaled.output["expected_value"] == 5 * result.output["expected_value"]


def test_icm_model_invariants_and_scale_relations() -> None:
    registry = default_registry()
    base = registry.execute("icm", {"stacks": [100, 100, 100], "payouts": [60, 30, 10]})
    stack_scaled = registry.execute("icm", {"stacks": [700, 700, 700], "payouts": [60, 30, 10]})
    payout_scaled = registry.execute("icm", {"stacks": [100, 100, 100], "payouts": [420, 210, 70]})
    assert base.model_qualifier == "Independent Chip Model"
    assert base.verification is not None
    assert base.verification.tolerance.absolute == base.output["verification_tolerance"]
    assert base.output["equities"] == stack_scaled.output["equities"]
    assert all(
        abs(7 * value - scaled) <= payout_scaled.output["verification_tolerance"]
        for value, scaled in zip(
            base.output["equities"], payout_scaled.output["equities"], strict=True
        )
    )
    assert abs(base.output["sum_error"]) <= base.output["verification_tolerance"]
    assert (
        max(base.output["equities"]) - min(base.output["equities"])
        <= base.output["verification_tolerance"]
    )


def test_matrix_tolerance_sensitivity_and_payoff_scaling() -> None:
    registry = default_registry()
    matrix = [[0, -1, 1], [1, 0, -1], [-1, 1, 0]]
    tight = registry.execute("matrix_game", {"matrix": matrix, "tolerance": 1e-12})
    default = registry.execute("matrix_game", {"matrix": matrix, "tolerance": 1e-9})
    scaled = registry.execute(
        "matrix_game", {"matrix": [[7 * value for value in row] for row in matrix]}
    )
    for result in (tight, default, scaled):
        assert result.status is ToolStatus.SUCCESS
        assert result.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
        assert result.output["duality_gap"] <= result.input.get("tolerance", 1e-9) * 20
        assert result.verification is not None
        assert result.verification.tolerance.kind == "caller-supplied"
        assert result.verification.tolerance.absolute == result.output["verification_tolerance"]
    row_strategy = list(map(float, default.output["row_strategy"]))
    column_strategy = list(map(float, default.output["column_strategy"]))
    lower = min(
        sum(row_strategy[row] * matrix[row][column] for row in range(len(matrix)))
        for column in range(len(matrix[0]))
    )
    upper = max(
        sum(matrix[row][column] * column_strategy[column] for column in range(len(matrix[0])))
        for row in range(len(matrix))
    )
    assert lower <= default.output["value"] <= upper
    assert upper - lower == default.output["duality_gap"]
    assert tight.output["row_strategy"] == default.output["row_strategy"]
    assert scaled.output["row_strategy"] == default.output["row_strategy"]
    assert scaled.output["value"] == 7 * default.output["value"]

    zero_requested = registry.execute(
        "matrix_game",
        {"matrix": [[1.0]], "tolerance": 0.0},
    )
    assert zero_requested.verification is not None
    assert zero_requested.output["verification_tolerance"] > 0.0
    assert (
        zero_requested.verification.tolerance.absolute
        == zero_requested.output["verification_tolerance"]
    )


def test_best_response_sensitivity_and_non_equilibrium_contract() -> None:
    registry = default_registry()
    game = {
        "root": "decision",
        "nodes": {
            "decision": {
                "type": "player",
                "player": 0,
                "information_set": "hero",
                "actions": {"a": "a", "b": "b"},
            },
            "a": {"type": "terminal", "payoff": 2},
            "b": {"type": "terminal", "payoff": -1},
        },
    }
    base = registry.execute("fixed_strategy_best_response", {"game": game, "fixed_strategy": {}})
    scaled_game = {
        **game,
        "nodes": {
            **game["nodes"],
            "a": {"type": "terminal", "payoff": 10},
            "b": {"type": "terminal", "payoff": -5},
        },
    }
    scaled = registry.execute(
        "fixed_strategy_best_response", {"game": scaled_game, "fixed_strategy": {}}
    )
    assert base.output["pure_policy"] == scaled.output["pure_policy"] == {"hero": "a"}
    assert scaled.output["value"] == 5 * base.output["value"]
    assert base.output["equilibrium_claim"] is False

    fixed_game = {
        "root": "hero",
        "nodes": {
            "hero": {
                "type": "player",
                "player": 0,
                "information_set": "hero",
                "actions": {"safe": "safe", "play": "villain"},
            },
            "villain": {
                "type": "player",
                "player": 1,
                "information_set": "villain",
                "actions": {"win": "win", "lose": "lose"},
            },
            "safe": {"type": "terminal", "payoff": 0},
            "win": {"type": "terminal", "payoff": 2},
            "lose": {"type": "terminal", "payoff": -1},
        },
    }
    versus_fixed = registry.execute(
        "fixed_strategy_best_response",
        {
            "game": fixed_game,
            "fixed_strategy": {"villain": {"win": 0.75, "lose": 0.25}},
        },
    )
    assert versus_fixed.output["pure_policy"] == {"hero": "play"}
    _assert_fraction_oracle(
        "fixed_strategy_best_response",
        versus_fixed.output["value"],
        Fraction(5, 4),
    )
    assert versus_fixed.output["equilibrium_claim"] is False


def test_sensitivity_ordering_and_affine_value_metamorphism() -> None:
    registry = default_registry()
    scenarios = [
        {"name": "low", "parameters": {"range": "tight"}, "value": -1},
        {"name": "high", "parameters": {"range": "loose"}, "value": 2},
    ]
    base = registry.execute("sensitivity", {"scenarios": scenarios})
    shifted = registry.execute(
        "sensitivity",
        {
            "scenarios": [
                {**scenario, "value": 3 * scenario["value"] + 5} for scenario in scenarios
            ],
            "decision_threshold": 5,
        },
    )
    assert shifted.output["lower_bound"] == 3 * base.output["lower_bound"] + 5
    assert shifted.output["upper_bound"] == 3 * base.output["upper_bound"] + 5
    assert shifted.output["influence_ranking"][0]["impact"] == (
        3 * base.output["influence_ranking"][0]["impact"]
    )


def test_solver_unavailable_is_reproducible_and_never_numeric_success() -> None:
    registry = default_registry()
    first = registry.execute("solver_status", {})
    second = registry.execute("solver_status", {})
    assert first.status is second.status is ToolStatus.UNAVAILABLE
    assert first.numeric_exactness is second.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert first.output == second.output
    assert first.output["result"] == {}
