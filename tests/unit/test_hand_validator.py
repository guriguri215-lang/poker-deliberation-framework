import math

import pytest

from poker_deliberation.schemas import CanonicalHand
from poker_deliberation.tools.hand_validator import validate_hand


def _hand() -> dict[str, object]:
    return {
        "game_type": "NLHE",
        "format": "cash",
        "table_size": 2,
        "small_blind": 1,
        "big_blind": 2,
        "players": [
            {"player_id": "h", "position": "SB", "starting_stack": 100},
            {"player_id": "v", "position": "BB", "starting_stack": 100},
        ],
        "hero_player_id": "h",
        "hero_cards": ["As", "Kh"],
        "board": [],
        "actions": [
            {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
            {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
            {
                "street": "preflop",
                "actor": "h",
                "action": "raise",
                "amount": 5,
                "to_amount": 6,
            },
            {"street": "preflop", "actor": "v", "action": "fold", "amount": 0},
        ],
    }


def test_valid_hand_reconstructs_pot() -> None:
    result = validate_hand(CanonicalHand.model_validate(_hand()))
    assert result["valid"] is True
    assert result["final_pot"] == 8


def test_stack_underflow_and_duplicate_cards_are_detected() -> None:
    data = _hand()
    data["hero_cards"] = ["As", "As"]
    data["actions"][2]["amount"] = 500  # type: ignore[index]
    data["actions"][2]["to_amount"] = 501  # type: ignore[index]
    result = validate_hand(CanonicalHand.model_validate(data))
    assert result["valid"] is False
    assert any("duplicate" in error for error in result["errors"])
    assert any("underflow" in error for error in result["errors"])


def test_blinds_create_outstanding_bet_and_small_blind_can_call() -> None:
    data = _hand()
    data["actions"] = [
        {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
        {"street": "preflop", "actor": "h", "action": "call", "amount": 1},
    ]
    result = validate_hand(CanonicalHand.model_validate(data))
    assert result["valid"] is True
    assert result["final_pot"] == 4


def test_check_facing_big_blind_is_rejected() -> None:
    data = _hand()
    data["actions"] = [
        {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
        {"street": "preflop", "actor": "h", "action": "check", "amount": 0},
    ]
    result = validate_hand(CanonicalHand.model_validate(data))
    assert result["valid"] is False
    assert any("cannot check" in error for error in result["errors"])


def test_all_in_player_cannot_act_again() -> None:
    data = _hand()
    data["players"][0]["starting_stack"] = 3  # type: ignore[index]
    data["actions"] = [
        {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
        {
            "street": "preflop",
            "actor": "h",
            "action": "all_in",
            "amount": 2,
            "to_amount": 3,
        },
        {"street": "preflop", "actor": "h", "action": "fold", "amount": 0},
    ]
    result = validate_hand(CanonicalHand.model_validate(data))
    assert result["valid"] is False
    assert any("all-in player acts again" in error for error in result["errors"])


def test_non_finite_hand_amounts_are_rejected_by_schema() -> None:
    invalid_hands: list[dict[str, object]] = []

    blind = _hand()
    blind["big_blind"] = math.inf
    invalid_hands.append(blind)

    stack = _hand()
    stack["players"][0]["starting_stack"] = math.inf  # type: ignore[index]
    invalid_hands.append(stack)

    action = _hand()
    action["actions"][2]["amount"] = math.inf  # type: ignore[index]
    invalid_hands.append(action)

    for hand in invalid_hands:
        with pytest.raises(ValueError):
            CanonicalHand.model_validate(hand)


def test_street_cannot_advance_with_an_outstanding_bet() -> None:
    data = _hand()
    data["board"] = ["2c", "3d", "4h"]
    data["actions"] = [
        {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
        {"street": "flop", "actor": "h", "action": "check", "amount": 0},
    ]
    result = validate_hand(CanonicalHand.model_validate(data))
    assert result["valid"] is False
    assert any("previous street preflop is incomplete" in error for error in result["errors"])


def test_street_cannot_advance_before_all_players_respond() -> None:
    data = _hand()
    data["board"] = ["2c", "3d", "4h"]
    data["actions"] = [
        {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
        {"street": "preflop", "actor": "h", "action": "call", "amount": 1},
        {"street": "flop", "actor": "h", "action": "check", "amount": 0},
    ]
    result = validate_hand(CanonicalHand.model_validate(data))
    assert result["valid"] is False
    assert any("players have not acted" in error for error in result["errors"])


def test_bet_below_big_blind_is_rejected() -> None:
    data = _hand()
    data["board"] = ["2c", "3d", "4h"]
    data["actions"] = [
        {"street": "preflop", "actor": "h", "action": "post_blind", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "post_blind", "amount": 2},
        {"street": "preflop", "actor": "h", "action": "call", "amount": 1},
        {"street": "preflop", "actor": "v", "action": "check", "amount": 0},
        {
            "street": "flop",
            "actor": "h",
            "action": "bet",
            "amount": 0.5,
            "to_amount": 0.5,
        },
    ]
    result = validate_hand(CanonicalHand.model_validate(data))
    assert result["valid"] is False
    assert any("below the minimum opening bet" in error for error in result["errors"])


def test_multiway_short_call_caps_each_current_street_contribution() -> None:
    data = _hand()
    data["table_size"] = 3
    data["players"] = [
        {"player_id": "short", "position": "BB", "starting_stack": 30},
        {"player_id": "deep_a", "position": "SB", "starting_stack": 100},
        {"player_id": "deep_b", "position": "BTN", "starting_stack": 100},
    ]
    data["hero_player_id"] = "short"
    data["actions"] = [
        {"street": "preflop", "actor": "deep_a", "action": "post_blind", "amount": 1},
        {"street": "preflop", "actor": "short", "action": "post_blind", "amount": 2},
        {
            "street": "preflop",
            "actor": "deep_b",
            "action": "raise",
            "amount": 50,
            "to_amount": 50,
        },
        {"street": "preflop", "actor": "deep_a", "action": "call", "amount": 49},
        {"street": "preflop", "actor": "short", "action": "call", "amount": 28},
    ]

    result = validate_hand(CanonicalHand.model_validate(data))
    snapshot = result["decision_snapshots"][4]

    assert result["valid"] is True
    assert snapshot["pot_before"] == 102
    assert snapshot["contestable_pot"] == 62
    assert snapshot["side_pot_risk"] is True


def test_folded_contribution_above_caller_cap_stays_in_side_pot() -> None:
    data = _hand()
    data["table_size"] = 4
    data["players"] = [
        {"player_id": "short", "position": "BB", "starting_stack": 30},
        {"player_id": "folded", "position": "UTG", "starting_stack": 200},
        {"player_id": "deep_a", "position": "BTN", "starting_stack": 200},
        {"player_id": "deep_b", "position": "SB", "starting_stack": 200},
    ]
    data["hero_player_id"] = "short"
    data["actions"] = [
        {"street": "preflop", "actor": "short", "action": "post_blind", "amount": 2},
        {
            "street": "preflop",
            "actor": "folded",
            "action": "raise",
            "amount": 50,
            "to_amount": 50,
        },
        {
            "street": "preflop",
            "actor": "deep_a",
            "action": "raise",
            "amount": 100,
            "to_amount": 100,
        },
        {"street": "preflop", "actor": "deep_b", "action": "call", "amount": 100},
        {"street": "preflop", "actor": "folded", "action": "fold", "amount": 0},
        {"street": "preflop", "actor": "short", "action": "call", "amount": 28},
    ]

    result = validate_hand(CanonicalHand.model_validate(data))
    snapshot = result["decision_snapshots"][5]

    assert result["valid"] is True
    assert snapshot["pot_before"] == 252
    assert snapshot["contestable_pot"] == 92
    assert snapshot["side_pot_risk"] is True
