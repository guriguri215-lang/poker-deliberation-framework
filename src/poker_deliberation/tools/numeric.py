"""Shared binary64 comparison primitives for calculator contracts."""

from __future__ import annotations

import math
from collections.abc import Iterable

EV_TREE_VERIFICATION_ULPS = 64
BEST_RESPONSE_VERIFICATION_ULPS = 64
MATRIX_NORMALIZATION_ULPS = 64


def ulp_bound(actual: float, expected: float, *, ulps: int) -> float:
    """Return a magnitude-scaled ULP bound without an implicit relative tolerance."""

    scale = max(abs(actual), abs(expected), 1.0)
    return math.ulp(scale) * ulps


def close_ulps(actual: float, expected: float, *, ulps: int) -> bool:
    return abs(actual - expected) <= ulp_bound(actual, expected, ulps=ulps)


def close_absolute(actual: float, expected: float, *, absolute: float) -> bool:
    """Compare in one caller-declared unit; relative tolerance is deliberately zero."""

    return math.isclose(actual, expected, rel_tol=0.0, abs_tol=absolute)


def normalized_probability_sum(probabilities: Iterable[float], *, ulps: int) -> bool:
    values = list(probabilities)
    total = sum(values)
    return (
        bool(values)
        and all(math.isfinite(value) and value >= 0 for value in values)
        and close_ulps(
            total,
            1.0,
            ulps=ulps,
        )
    )


def effective_matrix_tolerance(matrix: list[list[float]], caller_tolerance: float) -> float:
    """Resolve a payoff-unit tolerance with a magnitude-scaled binary64 floor."""

    magnitude = max(1.0, *(abs(float(value)) for row in matrix for value in row))
    floating_floor = math.ulp(magnitude) * MATRIX_NORMALIZATION_ULPS
    return max(float(caller_tolerance), floating_floor)
