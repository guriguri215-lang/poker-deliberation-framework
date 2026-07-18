import math

import pytest

from poker_deliberation.tools.pot_odds import break_even_fold_frequency, pot_odds


def test_known_pot_odds_is_twenty_five_percent() -> None:
    result = pot_odds(pot_before_bet=100, opponent_bet=50, call_cost=50)
    assert result["required_equity"] == 0.25
    assert result["pot_odds_against"] == 3.0


def test_rake_increases_required_equity() -> None:
    no_rake = pot_odds(pot_before_bet=100, opponent_bet=50, call_cost=50)
    rake = pot_odds(pot_before_bet=100, opponent_bet=50, call_cost=50, expected_rake=10)
    assert rake["required_equity"] > no_rake["required_equity"]


def test_break_even_fold_frequency() -> None:
    result = break_even_fold_frequency(risk=50, reward=100)
    assert math.isclose(result["break_even_fold_frequency"], 1 / 3)


def test_called_branch_rake_is_not_silently_applied_to_zero_equity_bluff() -> None:
    with pytest.raises(TypeError):
        break_even_fold_frequency(risk=50, reward=100, expected_rake_if_called=10)  # type: ignore[call-arg]


def test_invalid_amount_rejected() -> None:
    with pytest.raises(ValueError):
        pot_odds(pot_before_bet=-1, opponent_bet=50, call_cost=50)
