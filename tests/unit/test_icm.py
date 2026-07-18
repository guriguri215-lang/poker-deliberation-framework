import math

import pytest

from poker_deliberation.tools.icm import calculate_icm


def test_equal_stacks_are_symmetric_and_prizes_sum() -> None:
    result = calculate_icm([100, 100, 100], [60, 30, 10])
    assert all(math.isclose(value, 100 / 3) for value in result["equities"])
    assert math.isclose(result["equity_sum"], 100)
    assert math.isclose(result["sum_error"], 0, abs_tol=1e-10)


def test_zero_stack_has_zero_equity() -> None:
    result = calculate_icm([100, 50, 0], [70, 30, 0])
    assert result["equities"][2] == 0
    assert math.isclose(sum(result["equities"]), 100)


def test_larger_stack_has_more_equity_in_symmetric_field() -> None:
    result = calculate_icm([200, 100, 100], [60, 30, 10])
    assert result["equities"][0] > result["equities"][1]


def test_invalid_payout_order_rejected() -> None:
    with pytest.raises(ValueError):
        calculate_icm([100, 100], [20, 30])


def test_zero_stack_cannot_silently_drop_nonzero_prize() -> None:
    with pytest.raises(ValueError, match="non-zero payouts"):
        calculate_icm([100, 50, 0], [70, 30, 10])


def test_icm_hard_active_player_cap() -> None:
    with pytest.raises(ValueError, match="12 active"):
        calculate_icm([1] * 13, [1])
