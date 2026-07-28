from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.range_grammar import validate_versioned_range
from tests.range_support import versioned_range_hand


@given(weight=st.integers(min_value=1, max_value=1_000_000))
def test_integer_millionth_weight_round_trips_without_binary_float_semantics(
    weight: int,
) -> None:
    notation = "QcQd" if weight == 1_000_000 else f"QcQd@0.{weight:06d}"
    hand, definition = versioned_range_hand(notation)

    result = validate_versioned_range(hand, definition)

    assert result.status == "success"
    assert result.combo_count == 1
    assert result.combos[0].weight_millionths == weight
    expected_suffix = "" if weight == 1_000_000 else f"@0.{weight:06d}".rstrip("0")
    assert result.canonical_notation == f"QcQd{expected_suffix}"


@given(order=st.permutations(("QcQd", "JhJc", "9s8s")))
def test_canonical_combo_projection_is_independent_of_token_order(
    order: tuple[str, ...],
) -> None:
    hand, definition = versioned_range_hand(",".join(order))

    result = validate_versioned_range(hand, definition)

    assert result.status == "success"
    assert result.canonical_notation == "QcQd,JcJh,9s8s"
