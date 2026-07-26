import math
from fractions import Fraction
from functools import cache

import pytest

from poker_deliberation.tools.icm import (
    calculate_icm,
    icm_cached_subset_operation_bound,
    icm_floating_error_bound,
)


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


def _fraction_icm(stacks: tuple[int, ...], payouts: tuple[int, ...]) -> tuple[Fraction, ...]:
    @cache
    def recurse(remaining: tuple[int, ...], place: int) -> tuple[Fraction, ...]:
        result = [Fraction(0) for _ in stacks]
        if not remaining or place >= len(payouts):
            return tuple(result)
        total = sum(Fraction(stacks[index]) for index in remaining)
        for winner in remaining:
            probability = Fraction(stacks[winner]) / total
            result[winner] += probability * payouts[place]
            continuation = recurse(
                tuple(index for index in remaining if index != winner),
                place + 1,
            )
            for index, value in enumerate(continuation):
                result[index] += probability * value
        return tuple(result)

    return recurse(tuple(range(len(stacks))), 0)


@pytest.mark.parametrize(
    ("stacks", "payouts"),
    [
        ((3, 2), (7,)),
        ((5, 3, 2), (9, 4)),
        ((7, 5, 3, 1), (10, 6, 3, 1)),
    ],
)
def test_small_icm_matches_independent_fraction_oracle(
    stacks: tuple[int, ...],
    payouts: tuple[int, ...],
) -> None:
    result = calculate_icm(list(map(float, stacks)), list(map(float, payouts)))
    oracle = _fraction_icm(stacks, payouts)

    assert all(
        abs(actual - float(expected)) <= result["verification_tolerance"]
        for actual, expected in zip(result["equities"], oracle, strict=True)
    )


def test_cached_subset_bound_covers_twelve_active_players_and_listed_zero_stacks() -> None:
    stacks = [float(index + 1) for index in range(12)] + [0.0] * 88
    payouts = [float(12 - index) for index in range(12)]
    result = calculate_icm(stacks, payouts)
    expected_operations = (
        (2 * len(stacks) + 4)
        * sum((12 - removed) * math.comb(12, removed) for removed in range(12))
        + len(stacks)
        + len(payouts)
        + 1
    )
    bound = icm_floating_error_bound(12, len(stacks), len(payouts), sum(payouts))

    assert icm_cached_subset_operation_bound(12, len(stacks), len(payouts)) == (expected_operations)
    assert bound.operation_upper_bound == expected_operations
    assert result["verification_tolerance"] == bound.absolute
    assert result["conservation_verified"] is True
    assert result["zero_stack_players"] == list(range(12, 100))


def test_icm_tolerance_scales_with_binary_prize_unit() -> None:
    scale = float(2**40)
    base = calculate_icm([7.0, 5.0, 3.0], [10.0, 6.0, 2.0])
    scaled = calculate_icm(
        [7.0, 5.0, 3.0],
        [10.0 * scale, 6.0 * scale, 2.0 * scale],
    )

    assert scaled["verification_tolerance"] == base["verification_tolerance"] * scale
    assert all(
        abs(actual * scale - scaled_actual) <= scaled["verification_tolerance"]
        for actual, scaled_actual in zip(
            base["equities"],
            scaled["equities"],
            strict=True,
        )
    )


def test_icm_rejects_nonfinite_aggregate_stacks_and_prizes() -> None:
    with pytest.raises(ValueError, match="aggregate active stacks"):
        calculate_icm([1e308, 1e308], [1.0])
    with pytest.raises(ValueError, match="aggregate payable prizes"):
        calculate_icm([1.0, 1.0], [1e308, 1e308])
