from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from poker_deliberation.schemas import ToolStatus
from poker_deliberation.tools import default_registry
from tests.hand_pot_ledger_support import heads_up_hand, request, side_pot_hand


def _successful_output(hand: dict[str, Any], *, chip_unit: str = "1") -> dict[str, Any]:
    result = default_registry().execute(
        "hand_pot_ledger",
        request(hand, chip_unit=chip_unit),
    )
    assert result.status is ToolStatus.SUCCESS, result.error
    return result.output


def _scale_money(hand: dict[str, Any], factor: int) -> dict[str, Any]:
    scaled = deepcopy(hand)
    for field in ("small_blind", "big_blind", "ante", "rake"):
        if scaled.get(field) is not None:
            scaled[field] *= factor
    for player in scaled["players"]:
        player["starting_stack"] *= factor
    for action in scaled["actions"]:
        for field in ("amount", "to_amount", "pot_before", "pot_after"):
            if action.get(field) is not None:
                action[field] *= factor
    return scaled


@pytest.mark.property
@settings(max_examples=30, deadline=None)
@given(
    small_blind=st.integers(min_value=1, max_value=100),
    stack_multiple=st.integers(min_value=2, max_value=200),
)
def test_heads_up_conservation_for_bounded_integer_blinds(
    small_blind: int,
    stack_multiple: int,
) -> None:
    hand = heads_up_hand()
    big_blind = small_blind * 2
    starting_stack = big_blind * stack_multiple
    hand["small_blind"] = small_blind
    hand["big_blind"] = big_blind
    for player in hand["players"]:
        player["starting_stack"] = starting_stack
    hand["actions"][0]["amount"] = small_blind
    hand["actions"][1]["amount"] = big_blind
    hand["actions"][2]["amount"] = small_blind
    hand["actions"][2]["to_amount"] = big_blind

    output = _successful_output(hand)

    assert output["final_pot_units"] == big_blind * 2
    assert output["starting_chips_units"] == starting_stack * 2
    assert (
        sum(output["remaining_stacks_units"].values()) + output["final_pot_units"]
        == (output["starting_chips_units"])
    )
    assert (
        sum(layer["amount_units"] for layer in output["pot_layers"]) == (output["final_pot_units"])
    )
    assert output["conservation_verified"] is True
    assert output["oracle_verified"] is True


@pytest.mark.property
@settings(max_examples=12, deadline=None)
@given(factor=st.integers(min_value=1, max_value=50))
def test_scaling_values_and_declared_unit_preserves_integer_ledger(factor: int) -> None:
    base = _successful_output(side_pot_hand())
    scaled = _successful_output(_scale_money(side_pot_hand(), factor), chip_unit=str(factor))

    for field in (
        "gross_contributions_units",
        "net_contributions_units",
        "remaining_stacks_units",
        "gross_committed_units",
        "total_returned_units",
        "final_pot_units",
        "starting_chips_units",
        "pot_layers",
        "uncalled_returns",
    ):
        assert scaled[field] == base[field]


@pytest.mark.property
@given(order=st.permutations([0, 1, 2]))
def test_player_declaration_order_does_not_change_canonical_accounting(
    order: list[int],
) -> None:
    hand = side_pot_hand()
    expected = _successful_output(hand)
    hand["players"] = [hand["players"][index] for index in order]

    actual = _successful_output(hand)

    for field in (
        "gross_contributions_units",
        "net_contributions_units",
        "remaining_stacks_units",
        "pot_layers",
        "player_eligibility",
        "final_pot_units",
    ):
        assert actual[field] == expected[field]
