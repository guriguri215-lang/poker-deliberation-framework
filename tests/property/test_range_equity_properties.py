from __future__ import annotations

from fractions import Fraction

from hypothesis import given, settings
from hypothesis import strategies as st

from poker_deliberation.range_equity import (
    admit_versioned_range_river_equity,
    build_versioned_range_river_equity_result,
    expected_versioned_range_equity_input,
)
from poker_deliberation.range_models import RangeValidationResultV1, VersionedRangeDefinitionV1
from poker_deliberation.tools import default_registry
from tests.range_support import versioned_river_equity_case


def _weighted_token(cards: str, weight: int) -> str:
    if weight == 1_000_000:
        return cards
    return f"{cards}@0.{weight:06d}"


def _bridge_for_weights(loss_weight: int, win_weight: int, *, reverse: bool = False):
    tokens = (
        _weighted_token("6c6d", loss_weight),
        _weighted_token("QcQd", win_weight),
    )
    notation = ",".join(reversed(tokens) if reverse else tokens)
    admission = admit_versioned_range_river_equity(versioned_river_equity_case(notation))
    assert admission.case.hand is not None
    definition = admission.case.hand.known_ranges[0]
    assert isinstance(definition, VersionedRangeDefinitionV1)
    registry = default_registry()
    validation_result = registry.execute(
        "range_validate",
        {
            "schema_version": "1.0.0",
            "hand": admission.case.hand.model_dump(mode="json"),
            "range_definition": definition.model_dump(mode="json"),
        },
        contract_version="2.0.0",
    )
    validation = RangeValidationResultV1.model_validate(validation_result.output)
    combos_result = registry.execute(
        "combos",
        {"range": validation.canonical_notation, "dead_cards": []},
        contract_version="2.0.0",
    )
    equity_result = registry.execute(
        "holdem_equity",
        expected_versioned_range_equity_input(admission.case, validation),
        contract_version="2.0.0",
    )
    return admission, build_versioned_range_river_equity_result(
        admission.case,
        [validation_result, combos_result, equity_result],
    )


@settings(max_examples=40, deadline=None)
@given(
    loss_weight=st.integers(min_value=1, max_value=1_000_000),
    win_weight=st.integers(min_value=1, max_value=1_000_000),
)
def test_integer_weights_project_to_the_exact_reduced_fraction(
    loss_weight: int,
    win_weight: int,
) -> None:
    _admission, result = _bridge_for_weights(loss_weight, win_weight)
    expected = Fraction(win_weight, loss_weight + win_weight)

    assert result.win_weight_millionths == win_weight
    assert result.loss_weight_millionths == loss_weight
    assert (result.equity_numerator, result.equity_denominator) == (
        expected.numerator,
        expected.denominator,
    )


@settings(max_examples=20, deadline=None)
@given(
    loss_weight=st.integers(min_value=1, max_value=1_000_000),
    win_weight=st.integers(min_value=1, max_value=1_000_000),
)
def test_source_order_changes_provenance_but_not_exact_equity(
    loss_weight: int,
    win_weight: int,
) -> None:
    first_admission, first = _bridge_for_weights(loss_weight, win_weight)
    second_admission, second = _bridge_for_weights(loss_weight, win_weight, reverse=True)

    assert first_admission.binding.source_range_sha256 != (
        second_admission.binding.source_range_sha256
    )
    assert (first.equity_numerator, first.equity_denominator) == (
        second.equity_numerator,
        second.equity_denominator,
    )
