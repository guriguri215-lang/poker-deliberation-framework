import math

import pytest

from poker_deliberation.tools.ev_tree import evaluate_ev_tree
from poker_deliberation.tools.sensitivity import analyze_scenarios
from poker_deliberation.tools.solver_adapter import UnavailableSolverAdapter


def test_ev_tree() -> None:
    result = evaluate_ev_tree(
        {
            "root": "root",
            "nodes": {
                "root": {
                    "branches": [
                        {"label": "fold", "probability": 0.25, "child": "win"},
                        {"label": "call", "probability": 0.75, "child": "lose"},
                    ]
                },
                "win": {"payoff": 10},
                "lose": {"payoff": -2},
            },
        }
    )
    assert math.isclose(result["expected_value"], 1.0)


def test_ev_tree_rejects_probability_error() -> None:
    with pytest.raises(ValueError):
        evaluate_ev_tree(
            {
                "root": "root",
                "nodes": {
                    "root": {"branches": [{"probability": 0.5, "child": "end"}]},
                    "end": {"payoff": 1},
                },
            }
        )


def test_sensitivity_bounds_and_ranking() -> None:
    result = analyze_scenarios(
        [
            {"name": "a", "parameters": {"range": "tight"}, "value": -1},
            {"name": "b", "parameters": {"range": "loose"}, "value": 2},
        ]
    )
    assert result["lower_bound"] == -1
    assert result["upper_bound"] == 2
    assert result["influence_ranking"][0]["parameter"] == "range"


def test_sensitivity_canonicalizes_equivalent_json_settings() -> None:
    result = analyze_scenarios(
        [
            {"name": "a", "parameters": {"p": {"a": 1, "b": 2}}, "value": 0},
            {"name": "b", "parameters": {"p": {"b": 2, "a": 1}}, "value": 10},
        ]
    )
    assert result["influence_ranking"][0]["impact"] == 0
    assert "not a causal effect" in result["warning"]


def test_solver_unavailable_is_not_fake_success() -> None:
    result = UnavailableSolverAdapter().solve({})
    assert result.status == "unavailable"
    assert result.result == {}
    assert "no equilibrium" in (result.error or "")
