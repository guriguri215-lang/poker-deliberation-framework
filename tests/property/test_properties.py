import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.tools.combinations import parse_weighted_range
from poker_deliberation.tools.icm import calculate_icm
from poker_deliberation.tools.pot_odds import pot_odds

pytestmark = pytest.mark.property


@given(
    pot=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    bet=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
    call=st.floats(min_value=0.01, max_value=1e6, allow_nan=False, allow_infinity=False),
)
def test_required_equity_is_probability(pot: float, bet: float, call: float) -> None:
    result = pot_odds(pot_before_bet=pot, opponent_bet=bet, call_cost=call)
    assert 0 < result["required_equity"] < 1


@given(st.lists(st.integers(min_value=1, max_value=1000), min_size=2, max_size=7))
def test_icm_prize_conservation(stacks: list[int]) -> None:
    payouts = [float(len(stacks) - index) for index in range(len(stacks))]
    result = calculate_icm(list(map(float, stacks)), payouts)
    assert abs(result["equity_sum"] - sum(payouts)) <= result["verification_tolerance"]
    assert result["conservation_verified"] is True
    assert all(0 <= value <= sum(payouts) for value in result["equities"])


def test_range_weights_can_be_normalized() -> None:
    combos = parse_weighted_range("AKs@0.25,QQ@0.5")
    total = sum(combo.weight for combo in combos)
    normalized = [combo.weight / total for combo in combos]
    assert math.isclose(sum(normalized), 1.0)
