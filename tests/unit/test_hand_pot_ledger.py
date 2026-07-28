from __future__ import annotations

from copy import deepcopy
from typing import Any

from poker_deliberation.schemas import NumericalExactness, ToolResult, ToolStatus
from poker_deliberation.tools import default_registry
from tests.hand_pot_ledger_support import (
    cumulative_short_reopen_hand,
    heads_up_hand,
    request,
    short_reopen_hand,
    side_pot_hand,
    uncalled_return_hand,
)


def _execute(hand: dict[str, Any], *, chip_unit: str = "1") -> ToolResult:
    return default_registry().execute(
        "hand_pot_ledger",
        request(hand, chip_unit=chip_unit),
    )


def test_heads_up_ledger_is_exact_under_explicit_model() -> None:
    result = _execute(heads_up_hand())

    assert result.status is ToolStatus.SUCCESS
    assert result.numeric_exactness is NumericalExactness.EXACT_UNDER_MODEL
    assert result.verification is None
    assert result.output["profile_id"] == "generic_nlhe_cash_no_rake_v1"
    assert result.output["final_pot_units"] == 4
    assert result.output["gross_committed_units"] == 4
    assert result.output["total_returned_units"] == 0
    assert result.output["net_contributions_units"] == {"a": 2, "b": 2}
    assert result.output["remaining_stacks_units"] == {"a": 98, "b": 98}
    assert result.output["conservation_verified"] is True
    assert result.output["oracle_verified"] is True


def test_multiway_all_in_builds_main_and_side_pot_with_action_evidence() -> None:
    result = _execute(side_pot_hand())

    assert result.status is ToolStatus.SUCCESS
    layers = result.output["pot_layers"]
    assert [layer["kind"] for layer in layers] == ["main", "side"]
    assert [layer["amount_units"] for layer in layers] == [60, 60]
    assert layers[0]["contributors"] == ("a", "b", "c")
    assert layers[0]["eligible_players"] == ("a", "b", "c")
    assert layers[1]["contributors"] == ("a", "b")
    assert layers[1]["eligible_players"] == ("a", "b")
    assert layers[0]["evidence_action_indexes"] == (0, 1, 2, 3, 4)
    assert layers[1]["evidence_action_indexes"] == (3, 5)
    assert sum(layer["amount_units"] for layer in layers) == 120


def test_folded_contribution_stays_while_uncalled_excess_returns_once() -> None:
    result = _execute(uncalled_return_hand())

    assert result.status is ToolStatus.SUCCESS
    assert result.output["gross_committed_units"] == 90
    assert result.output["total_returned_units"] == 30
    assert result.output["final_pot_units"] == 60
    assert result.output["uncalled_returns"] == (
        {
            "schema_version": "1.0.0",
            "return_id": "return-0",
            "street": "flop",
            "player_id": "a",
            "amount_units": 30,
            "source_action_indexes": (5,),
        },
    )
    assert result.output["net_contributions_units"] == {"a": 20, "b": 20, "c": 20}
    assert result.output["pot_layers"][0]["contributors"] == ("a", "b", "c")
    assert result.output["pot_layers"][0]["eligible_players"] == ("a", "b")
    eligibility = {
        item["player_id"]: item["eligible_for_contested_pots"]
        for item in result.output["player_eligibility"]
    }
    assert eligibility == {"a": True, "b": True, "c": False}


def test_one_short_all_in_does_not_reopen_betting_for_prior_actor() -> None:
    result = _execute(short_reopen_hand())

    assert result.status is ToolStatus.FAILED
    assert result.output == {}
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert result.error == "HandPotLedgerError: betting-not-reopened"


def test_cumulative_short_all_ins_reopen_after_a_full_raise_increment() -> None:
    result = _execute(cumulative_short_reopen_hand())

    assert result.status is ToolStatus.SUCCESS
    short_first = result.output["ledger_actions"][4]
    short_second = result.output["ledger_actions"][5]
    reopened_raise = result.output["ledger_actions"][6]
    assert short_first["full_raise"] is False
    assert short_second["full_raise"] is False
    assert reopened_raise["raise_rights_before"] is True
    assert reopened_raise["full_raise"] is True
    assert reopened_raise["minimum_full_raise_units_after"] == 8
    assert [layer["amount_units"] for layer in result.output["pot_layers"]] == [60, 9, 16]


def test_decimal_chip_unit_avoids_binary64_pot_drift() -> None:
    hand = deepcopy(heads_up_hand())
    hand["small_blind"] = 0.1
    hand["big_blind"] = 0.2
    for player in hand["players"]:
        player["starting_stack"] = 10
    hand["actions"][0]["amount"] = 0.1
    hand["actions"][1]["amount"] = 0.2
    hand["actions"][2]["amount"] = 0.1
    hand["actions"][2]["to_amount"] = 0.2

    result = _execute(hand, chip_unit="0.1")

    assert result.status is ToolStatus.SUCCESS
    assert result.output["chip_unit"] == "0.1"
    assert result.output["final_pot_units"] == 4
    assert result.output["net_contributions_units"] == {"a": 2, "b": 2}
