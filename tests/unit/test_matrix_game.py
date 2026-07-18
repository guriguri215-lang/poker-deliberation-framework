import math

import pytest

from poker_deliberation.tools.matrix_game import solve_zero_sum_matrix


def test_rock_paper_scissors_equilibrium() -> None:
    result = solve_zero_sum_matrix([[0, -1, 1], [1, 0, -1], [-1, 1, 0]])
    assert result["exact_algorithm"] is True
    assert all(math.isclose(value, 1 / 3, abs_tol=1e-9) for value in result["row_strategy"])
    assert all(math.isclose(value, 1 / 3, abs_tol=1e-9) for value in result["column_strategy"])
    assert math.isclose(result["value"], 0, abs_tol=1e-9)
    assert result["duality_gap"] <= 1e-9


def test_pure_saddle_point() -> None:
    result = solve_zero_sum_matrix([[3, 1], [2, 0]])
    assert result["row_strategy"] == [1.0, 0.0]
    assert result["column_strategy"] == [0.0, 1.0]
    assert math.isclose(result["value"], 1.0)


@pytest.mark.parametrize("tolerance", [math.nan, math.inf, -1e-9])
def test_invalid_tolerance_is_rejected(tolerance: float) -> None:
    with pytest.raises(ValueError, match="finite and non-negative"):
        solve_zero_sum_matrix([[1, -1], [-1, 1]], tolerance=tolerance)
