from __future__ import annotations

from copy import deepcopy
from typing import Any


def profile(*, chip_unit: str = "1") -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "profile_id": "generic_nlhe_cash_no_rake_v1",
        "profile_version": "1.0.0",
        "supported_site": "none",
        "chip_unit": chip_unit,
    }


def request(
    hand: dict[str, Any],
    *,
    chip_unit: str = "1",
) -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "rule_profile": profile(chip_unit=chip_unit),
        "hand": deepcopy(hand),
    }


def heads_up_hand() -> dict[str, Any]:
    return {
        "game_type": "NLHE",
        "format": "cash",
        "table_size": 2,
        "small_blind": 1,
        "big_blind": 2,
        "ante": 0,
        "rake": 0,
        "players": [
            {"player_id": "a", "position": "SB", "starting_stack": 100},
            {"player_id": "b", "position": "BB", "starting_stack": 100},
        ],
        "actions": [
            {"street": "preflop", "actor": "a", "action": "post_blind", "amount": 1},
            {"street": "preflop", "actor": "b", "action": "post_blind", "amount": 2},
            {
                "street": "preflop",
                "actor": "a",
                "action": "call",
                "amount": 1,
                "to_amount": 2,
            },
            {"street": "preflop", "actor": "b", "action": "check", "amount": 0},
        ],
    }


def side_pot_hand() -> dict[str, Any]:
    return {
        "game_type": "NLHE",
        "format": "cash",
        "table_size": 3,
        "small_blind": 1,
        "big_blind": 2,
        "ante": 0,
        "rake": 0,
        "players": [
            {"player_id": "a", "position": "BTN", "starting_stack": 100},
            {"player_id": "b", "position": "SB", "starting_stack": 50},
            {"player_id": "c", "position": "BB", "starting_stack": 20},
        ],
        "actions": [
            {"street": "preflop", "actor": "b", "action": "post_blind", "amount": 1},
            {"street": "preflop", "actor": "c", "action": "post_blind", "amount": 2},
            {
                "street": "preflop",
                "actor": "a",
                "action": "raise",
                "amount": 20,
                "to_amount": 20,
            },
            {
                "street": "preflop",
                "actor": "b",
                "action": "all_in",
                "amount": 49,
                "to_amount": 50,
            },
            {
                "street": "preflop",
                "actor": "c",
                "action": "all_in",
                "amount": 18,
                "to_amount": 20,
            },
            {
                "street": "preflop",
                "actor": "a",
                "action": "call",
                "amount": 30,
                "to_amount": 50,
            },
        ],
    }


def uncalled_return_hand() -> dict[str, Any]:
    return {
        "game_type": "NLHE",
        "format": "cash",
        "table_size": 3,
        "small_blind": 1,
        "big_blind": 2,
        "ante": 0,
        "rake": 0,
        "board": ["2c", "3d", "4h"],
        "players": [
            {"player_id": "a", "position": "BTN", "starting_stack": 100},
            {"player_id": "b", "position": "SB", "starting_stack": 20},
            {"player_id": "c", "position": "BB", "starting_stack": 50},
        ],
        "actions": [
            {"street": "preflop", "actor": "b", "action": "post_blind", "amount": 1},
            {"street": "preflop", "actor": "c", "action": "post_blind", "amount": 2},
            {
                "street": "preflop",
                "actor": "a",
                "action": "raise",
                "amount": 20,
                "to_amount": 20,
            },
            {
                "street": "preflop",
                "actor": "b",
                "action": "all_in",
                "amount": 19,
                "to_amount": 20,
            },
            {
                "street": "preflop",
                "actor": "c",
                "action": "call",
                "amount": 18,
                "to_amount": 20,
            },
            {
                "street": "flop",
                "actor": "a",
                "action": "bet",
                "amount": 30,
                "to_amount": 30,
            },
            {"street": "flop", "actor": "c", "action": "fold", "amount": 0},
        ],
    }


def short_reopen_hand() -> dict[str, Any]:
    return {
        "game_type": "NLHE",
        "format": "cash",
        "table_size": 3,
        "small_blind": 1,
        "big_blind": 2,
        "ante": 0,
        "rake": 0,
        "players": [
            {"player_id": "a", "position": "BTN", "starting_stack": 100},
            {"player_id": "b", "position": "SB", "starting_stack": 100},
            {"player_id": "c", "position": "BB", "starting_stack": 15},
        ],
        "actions": [
            {"street": "preflop", "actor": "b", "action": "post_blind", "amount": 1},
            {"street": "preflop", "actor": "c", "action": "post_blind", "amount": 2},
            {
                "street": "preflop",
                "actor": "a",
                "action": "raise",
                "amount": 10,
                "to_amount": 10,
            },
            {
                "street": "preflop",
                "actor": "b",
                "action": "call",
                "amount": 9,
                "to_amount": 10,
            },
            {
                "street": "preflop",
                "actor": "c",
                "action": "all_in",
                "amount": 13,
                "to_amount": 15,
            },
            {
                "street": "preflop",
                "actor": "a",
                "action": "raise",
                "amount": 13,
                "to_amount": 23,
            },
        ],
    }


def cumulative_short_reopen_hand() -> dict[str, Any]:
    return {
        "game_type": "NLHE",
        "format": "cash",
        "table_size": 4,
        "small_blind": 1,
        "big_blind": 2,
        "ante": 0,
        "rake": 0,
        "players": [
            {"player_id": "a", "position": "BTN", "starting_stack": 100},
            {"player_id": "b", "position": "CO", "starting_stack": 100},
            {"player_id": "c", "position": "SB", "starting_stack": 15},
            {"player_id": "d", "position": "BB", "starting_stack": 18},
        ],
        "actions": [
            {"street": "preflop", "actor": "c", "action": "post_blind", "amount": 1},
            {"street": "preflop", "actor": "d", "action": "post_blind", "amount": 2},
            {
                "street": "preflop",
                "actor": "a",
                "action": "raise",
                "amount": 10,
                "to_amount": 10,
            },
            {
                "street": "preflop",
                "actor": "b",
                "action": "call",
                "amount": 10,
                "to_amount": 10,
            },
            {
                "street": "preflop",
                "actor": "c",
                "action": "all_in",
                "amount": 14,
                "to_amount": 15,
            },
            {
                "street": "preflop",
                "actor": "d",
                "action": "all_in",
                "amount": 16,
                "to_amount": 18,
            },
            {
                "street": "preflop",
                "actor": "a",
                "action": "raise",
                "amount": 16,
                "to_amount": 26,
            },
            {
                "street": "preflop",
                "actor": "b",
                "action": "call",
                "amount": 16,
                "to_amount": 26,
            },
        ],
    }
