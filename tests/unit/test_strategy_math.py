import math

from poker_deliberation.tools import default_registry


def _output(name: str, payload: dict[str, object]) -> dict[str, object]:
    result = default_registry().execute(name, payload)
    assert result.status.value == "success", result.error
    assert result.exactness.value == "exact"
    return result.output


def test_effective_stack_spr_and_mdf_known_answers() -> None:
    assert _output("effective_stack", {"stacks": [100, 42.5, 88]})["effective_stack"] == 42.5
    assert math.isclose(
        float(_output("spr", {"effective_stack": 97.5, "pot": 5.5})["spr"]), 97.5 / 5.5
    )
    assert math.isclose(
        float(_output("mdf", {"pot_before_bet": 10, "bet": 5})["minimum_defense_frequency"]),
        10 / 15,
    )


def test_rake_call_bluff_polar_and_bayes_known_answers() -> None:
    rake = _output("rake_amount", {"pot_total": 100, "rake_percent": 5, "rake_cap": 3})
    assert rake["rake_amount"] == 3
    call = _output(
        "raked_call_ev",
        {
            "equity": 0.5,
            "pot_after_bet": 75,
            "call_cost": 25,
            "rake_percent": 5,
            "rake_cap": 3,
        },
    )
    assert call["ev"] == 23.5
    bluff = _output(
        "bluff_ev",
        {"fold_frequency": 1 / 3, "pot_before_bet": 10, "bet": 5},
    )
    assert math.isclose(float(bluff["ev"]), 0, abs_tol=1e-12)
    polar = _output("polar_river_bluff_fraction", {"pot_before_bet": 10, "bet": 10})
    assert math.isclose(float(polar["bluff_fraction"]), 1 / 3)
    bayes = _output(
        "bayes_update",
        {"prior": 0.5, "likelihood_given_h": 0.8, "likelihood_given_not_h": 0.4},
    )
    assert math.isclose(float(bayes["posterior"]), 2 / 3)


def test_strategy_math_invalid_inputs_fail_without_output() -> None:
    result = default_registry().execute("mdf", {"pot_before_bet": 0, "bet": 5})
    assert result.status.value == "failed"
    assert result.output == {}
