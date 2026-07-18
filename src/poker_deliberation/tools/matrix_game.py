"""Small two-player zero-sum matrix games via verified support enumeration."""

from __future__ import annotations

import math
from itertools import combinations

HARD_MAX_DIMENSION = 32
HARD_MAX_SUPPORT_SIZE = 8
HARD_MAX_FALLBACK_ITERATIONS = 1_000_000
HARD_MAX_SUPPORT_CANDIDATES = 250_000
HARD_MAX_FICTITIOUS_WORK = 5_000_000


def _validate_matrix(matrix: list[list[float]]) -> tuple[int, int]:
    if not matrix or not matrix[0]:
        raise ValueError("payoff matrix must be non-empty")
    columns = len(matrix[0])
    if any(len(row) != columns for row in matrix):
        raise ValueError("payoff matrix must be rectangular")
    if any(not math.isfinite(float(value)) for row in matrix for value in row):
        raise ValueError("payoff matrix values must be finite")
    return len(matrix), columns


def _solve_linear(
    coefficients: list[list[float]], values: list[float], tolerance: float
) -> list[float] | None:
    size = len(values)
    augmented = [
        [*list(map(float, row)), float(value)]
        for row, value in zip(coefficients, values, strict=True)
    ]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) <= tolerance:
            return None
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            augmented[row] = [
                current - factor * pivot_value
                for current, pivot_value in zip(augmented[row], augmented[column], strict=True)
            ]
    return [augmented[index][-1] for index in range(size)]


def _support_candidate(
    matrix: list[list[float]],
    row_support: tuple[int, ...],
    column_support: tuple[int, ...],
    tolerance: float,
) -> dict[str, object] | None:
    support_size = len(row_support)
    row_equations = [
        [matrix[row][column] for row in row_support] + [-1.0] for column in column_support
    ]
    row_equations.append([1.0] * support_size + [0.0])
    row_solution = _solve_linear(row_equations, [0.0] * support_size + [1.0], tolerance)
    if row_solution is None:
        return None
    column_equations = [
        [matrix[row][column] for column in column_support] + [-1.0] for row in row_support
    ]
    column_equations.append([1.0] * support_size + [0.0])
    column_solution = _solve_linear(column_equations, [0.0] * support_size + [1.0], tolerance)
    if column_solution is None:
        return None
    row_probabilities = row_solution[:-1]
    column_probabilities = column_solution[:-1]
    row_value, column_value = row_solution[-1], column_solution[-1]
    if any(probability <= tolerance for probability in (*row_probabilities, *column_probabilities)):
        return None
    if abs(row_value - column_value) > tolerance * 20:
        return None
    row_strategy = [0.0] * len(matrix)
    column_strategy = [0.0] * len(matrix[0])
    for index, probability in zip(row_support, row_probabilities, strict=True):
        row_strategy[index] = probability
    for index, probability in zip(column_support, column_probabilities, strict=True):
        column_strategy[index] = probability
    payoff_against_columns = [
        sum(row_strategy[row] * matrix[row][column] for row in range(len(matrix)))
        for column in range(len(matrix[0]))
    ]
    payoff_for_rows = [
        sum(matrix[row][column] * column_strategy[column] for column in range(len(matrix[0])))
        for row in range(len(matrix))
    ]
    lower = min(payoff_against_columns)
    upper = max(payoff_for_rows)
    if lower < row_value - tolerance * 20 or upper > row_value + tolerance * 20:
        return None
    return {
        "row_strategy": row_strategy,
        "column_strategy": column_strategy,
        "value": (row_value + column_value) / 2,
        "row_support": list(row_support),
        "column_support": list(column_support),
        "duality_gap": max(0.0, upper - lower),
        "row_best_response": max(range(len(payoff_for_rows)), key=payoff_for_rows.__getitem__),
        "column_best_response": min(
            range(len(payoff_against_columns)), key=payoff_against_columns.__getitem__
        ),
    }


def _fictitious_play(matrix: list[list[float]], iterations: int) -> dict[str, object]:
    rows, columns = len(matrix), len(matrix[0])
    row_counts = [1.0] * rows
    column_counts = [1.0] * columns
    for _ in range(iterations):
        column_total = sum(column_counts)
        column_strategy = [count / column_total for count in column_counts]
        row_payoffs = [
            sum(matrix[row][column] * column_strategy[column] for column in range(columns))
            for row in range(rows)
        ]
        row_choice = max(range(rows), key=row_payoffs.__getitem__)
        row_counts[row_choice] += 1
        row_total = sum(row_counts)
        row_strategy = [count / row_total for count in row_counts]
        column_payoffs = [
            sum(row_strategy[row] * matrix[row][column] for row in range(rows))
            for column in range(columns)
        ]
        column_choice = min(range(columns), key=column_payoffs.__getitem__)
        column_counts[column_choice] += 1
    row_total, column_total = sum(row_counts), sum(column_counts)
    row_strategy = [count / row_total for count in row_counts]
    column_strategy = [count / column_total for count in column_counts]
    payoffs_for_rows = [
        sum(matrix[row][column] * column_strategy[column] for column in range(columns))
        for row in range(rows)
    ]
    payoffs_against_columns = [
        sum(row_strategy[row] * matrix[row][column] for row in range(rows))
        for column in range(columns)
    ]
    upper, lower = max(payoffs_for_rows), min(payoffs_against_columns)
    return {
        "row_strategy": row_strategy,
        "column_strategy": column_strategy,
        "value_estimate": (upper + lower) / 2,
        "duality_gap": upper - lower,
        "iterations": iterations,
        "row_best_response": max(range(rows), key=payoffs_for_rows.__getitem__),
        "column_best_response": min(range(columns), key=payoffs_against_columns.__getitem__),
    }


def solve_zero_sum_matrix(
    matrix: list[list[float]],
    *,
    tolerance: float = 1e-9,
    max_support_size: int = 8,
    fallback_iterations: int = 50_000,
) -> dict[str, object]:
    if not math.isfinite(tolerance) or tolerance < 0:
        raise ValueError("tolerance must be finite and non-negative")
    rows, columns = _validate_matrix(matrix)
    if rows > HARD_MAX_DIMENSION or columns > HARD_MAX_DIMENSION:
        raise ValueError(
            f"matrix dimensions are limited to {HARD_MAX_DIMENSION} x {HARD_MAX_DIMENSION}"
        )
    if not 1 <= max_support_size <= HARD_MAX_SUPPORT_SIZE:
        raise ValueError(f"max_support_size must be between 1 and {HARD_MAX_SUPPORT_SIZE}")
    if not 1 <= fallback_iterations <= HARD_MAX_FALLBACK_ITERATIONS:
        raise ValueError(
            f"fallback_iterations must be between 1 and {HARD_MAX_FALLBACK_ITERATIONS}"
        )
    support_limit = min(rows, columns, max_support_size)
    support_candidates = sum(
        math.comb(rows, size) * math.comb(columns, size) for size in range(1, support_limit + 1)
    )
    fallback_work = fallback_iterations * rows * columns
    if support_candidates <= HARD_MAX_SUPPORT_CANDIDATES:
        for support_size in range(1, support_limit + 1):
            for row_support in combinations(range(rows), support_size):
                for column_support in combinations(range(columns), support_size):
                    candidate = _support_candidate(matrix, row_support, column_support, tolerance)
                    if candidate is not None:
                        return {
                            **candidate,
                            "method": "verified_support_enumeration",
                            "exact_algorithm": True,
                            "verification_tolerance": tolerance,
                            "support_candidates_upper_bound": support_candidates,
                        }
    if fallback_work > HARD_MAX_FICTITIOUS_WORK:
        raise ValueError(
            "matrix work estimate exceeds hard limits: "
            f"support_candidates={support_candidates}, fallback_work={fallback_work}"
        )
    approximate = _fictitious_play(matrix, fallback_iterations)
    return {
        **approximate,
        "method": "fictitious_play_fallback",
        "exact_algorithm": False,
        "support_candidates_upper_bound": support_candidates,
        "fallback_work_estimate": fallback_work,
        "warning": (
            "support enumeration did not cover this degenerate/large game; result is approximate"
        ),
    }
