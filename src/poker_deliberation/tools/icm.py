"""Independent Chip Model expected-payout calculator."""

from __future__ import annotations

import math
from functools import cache
from itertools import pairwise


def calculate_icm(stacks: list[float], payouts: list[float]) -> dict[str, object]:
    if len(stacks) < 2:
        raise ValueError("ICM requires at least two players")
    if len(stacks) > 100:
        raise ValueError("ICM input is limited to 100 listed players")
    if not payouts or len(payouts) > len(stacks):
        raise ValueError("payouts must contain between one and N entries")
    if any(not math.isfinite(stack) or stack < 0 for stack in stacks):
        raise ValueError("stacks must be finite and non-negative")
    if any(not math.isfinite(payout) or payout < 0 for payout in payouts):
        raise ValueError("payouts must be finite and non-negative")
    if any(first < second for first, second in pairwise(payouts)):
        raise ValueError("payouts must be ordered highest to lowest")
    active = tuple(index for index, stack in enumerate(stacks) if stack > 0)
    if not active:
        raise ValueError("at least one positive stack is required")
    if len(active) > 12:
        raise ValueError("complete ICM enumeration is limited to 12 active players")
    if any(payout > 0 for payout in payouts[len(active) :]):
        raise ValueError("non-zero payouts cannot be assigned beyond the active player count")
    effective_payouts = tuple(payouts[: len(active)])
    player_count = len(stacks)

    @cache
    def recurse(remaining: tuple[int, ...], place: int) -> tuple[float, ...]:
        result = [0.0] * player_count
        if not remaining or place >= len(effective_payouts):
            return tuple(result)
        total = sum(stacks[index] for index in remaining)
        if total <= 0:
            return tuple(result)
        for winner in remaining:
            probability = stacks[winner] / total
            result[winner] += probability * effective_payouts[place]
            next_remaining = tuple(index for index in remaining if index != winner)
            continuation = recurse(next_remaining, place + 1)
            for index, value in enumerate(continuation):
                result[index] += probability * value
        return tuple(result)

    equities = list(recurse(active, 0))
    expected_total = sum(equities)
    payable_total = sum(effective_payouts)
    operation_upper_bound = max(1, math.factorial(len(active)) * max(1, len(effective_payouts)))
    verification_tolerance = math.ulp(max(1.0, abs(payable_total))) * 64 * operation_upper_bound
    sum_error = expected_total - payable_total
    return {
        "stacks": stacks,
        "payouts": payouts,
        "equities": equities,
        "equity_sum": expected_total,
        "payable_prize_sum": payable_total,
        "sum_error": sum_error,
        "verification_tolerance": verification_tolerance,
        "conservation_verified": abs(sum_error) <= verification_tolerance,
        "zero_stack_players": [index for index, stack in enumerate(stacks) if stack == 0],
        "warning": None,
        "model": "Independent Chip Model; future-game simulation and risk preferences excluded",
    }
