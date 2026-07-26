"""Independent Chip Model expected-payout calculator."""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import cache
from itertools import pairwise

_BINARY64_UNIT_ROUNDOFF = 2.0**-53
_FORWARD_ERROR_SAFETY_FACTOR = 4


@dataclass(frozen=True, slots=True)
class ICMFloatingErrorBound:
    operation_upper_bound: int
    ulps: int
    absolute: float


def icm_cached_subset_operation_bound(
    active_player_count: int,
    listed_player_count: int,
    payout_count: int,
) -> int:
    """Bound binary64 operations in the cached subset DP and conservation check.

    At removal depth ``k``, ``C(n, k)`` distinct non-base cache states contain
    ``n-k`` possible winners.  A state performs at most ``(n-k) * (2*L+4)``
    operations: its stack sum, one division per winner, the current-prize
    multiply/add, and one multiply/add for each of the ``L`` listed outputs.
    The final term covers the two output sums and their subtraction.
    """

    if (
        active_player_count < 1
        or listed_player_count < active_player_count
        or payout_count < 1
        or payout_count > active_player_count
    ):
        raise ValueError("invalid ICM operation-bound dimensions")
    state_operations = (2 * listed_player_count + 4) * sum(
        (active_player_count - removed) * math.comb(active_player_count, removed)
        for removed in range(payout_count)
    )
    final_operations = listed_player_count + payout_count + 1
    return state_operations + final_operations


def icm_floating_error_bound(
    active_player_count: int,
    listed_player_count: int,
    payout_count: int,
    payable_prize_sum: float,
) -> ICMFloatingErrorBound:
    """Return a rounded-up binary64 conservation bound for the cached subset DP."""

    if not math.isfinite(payable_prize_sum) or payable_prize_sum < 0:
        raise ValueError("payable prize sum must be finite and non-negative")
    operation_upper_bound = icm_cached_subset_operation_bound(
        active_player_count,
        listed_player_count,
        payout_count,
    )
    accumulated_roundoff = operation_upper_bound * _BINARY64_UNIT_ROUNDOFF
    if accumulated_roundoff >= 1:
        raise ValueError("ICM floating-operation bound is outside the gamma model")
    gamma = accumulated_roundoff / (1.0 - accumulated_roundoff)
    scale = max(1.0, abs(payable_prize_sum))
    scale_ulp = math.ulp(scale)
    raw_absolute = _FORWARD_ERROR_SAFETY_FACTOR * gamma * scale
    ulps = max(1, math.ceil(raw_absolute / scale_ulp))
    absolute = scale_ulp * ulps
    if not math.isfinite(absolute):
        raise ValueError("ICM verification tolerance is not finite")
    return ICMFloatingErrorBound(
        operation_upper_bound=operation_upper_bound,
        ulps=ulps,
        absolute=absolute,
    )


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
    if not math.isfinite(sum(stacks)):
        raise ValueError("aggregate active stacks must remain finite")
    payable_total = sum(effective_payouts)
    if not math.isfinite(payable_total):
        raise ValueError("aggregate payable prizes must remain finite")

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
    error_bound = icm_floating_error_bound(
        len(active),
        player_count,
        len(effective_payouts),
        payable_total,
    )
    verification_tolerance = error_bound.absolute
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
