from __future__ import annotations

import hashlib
from typing import Any

from poker_deliberation.range_grammar import action_prefix_sha256
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.schemas import CanonicalHand, CaseInput


def versioned_range_hand(
    notation: str = "AKs@0.25,QQ@0.5",
    *,
    source_content_sha256: str | None = None,
    range_id: str = "villain-preflop",
    target_player_id: str = "villain",
    game_condition_updates: dict[str, Any] | None = None,
) -> tuple[CanonicalHand, VersionedRangeDefinitionV1]:
    base = CanonicalHand.model_validate(
        {
            "game_type": "NLHE",
            "format": "cash",
            "table_size": 2,
            "small_blind": 1,
            "big_blind": 2,
            "players": [
                {
                    "player_id": "hero",
                    "position": "SB",
                    "starting_stack": 100,
                },
                {
                    "player_id": "villain",
                    "position": "BB",
                    "starting_stack": 100,
                },
            ],
            "hero_player_id": "hero",
            "hero_cards": ["As", "Kh"],
            "actions": [
                {
                    "street": "preflop",
                    "actor": "hero",
                    "action": "post_blind",
                    "amount": 1,
                },
                {
                    "street": "preflop",
                    "actor": "villain",
                    "action": "post_blind",
                    "amount": 2,
                },
            ],
        }
    )
    conditions: dict[str, Any] = {
        "game_type": "NLHE",
        "format": "cash",
        "table_size": 2,
        "target_position": "BB",
        "street": "preflop",
        "starting_stack_min_bb_milli": 50_000,
        "starting_stack_max_bb_milli": 50_000,
        "as_of_action_index": 2,
        "action_prefix_sha256": action_prefix_sha256(base, 2),
    }
    conditions.update(game_condition_updates or {})
    definition = VersionedRangeDefinitionV1.model_validate(
        {
            "range_id": range_id,
            "target_player_id": target_player_id,
            "notation": notation,
            "source": {
                "source_id": "range-fixture",
                "source_kind": "repository_fixture",
                "license_classification": "repository_owned_mit",
                "usage_classification": "redistribution_allowed",
                "content_status": "ASSUMPTION",
                "content_sha256": (
                    source_content_sha256
                    if source_content_sha256 is not None
                    else hashlib.sha256(notation.encode("utf-8")).hexdigest()
                ),
            },
            "game_conditions": conditions,
        }
    )
    hand_payload = base.model_dump(mode="json")
    hand_payload["known_ranges"] = [definition.model_dump(mode="json")]
    hand = CanonicalHand.model_validate(hand_payload)
    parsed = hand.known_ranges[0]
    assert isinstance(parsed, VersionedRangeDefinitionV1)
    return hand, parsed


def versioned_river_equity_case(
    notation: str = "6c6d@0.25,QcQd@0.75",
    *,
    hero_cards: tuple[str, str] = ("As", "Kd"),
    board: tuple[str, str, str, str, str] = ("2c", "3d", "4h", "5s", "9c"),
    metadata: dict[str, Any] | None = None,
) -> CaseInput:
    """Build a repository-owned three-player river fixture with one folded player."""

    base = CanonicalHand.model_validate(
        {
            "game_type": "NLHE",
            "format": "cash",
            "table_size": 3,
            "small_blind": 1,
            "big_blind": 2,
            "players": [
                {"player_id": "hero", "position": "BTN", "starting_stack": 100},
                {"player_id": "folded", "position": "SB", "starting_stack": 100},
                {"player_id": "villain", "position": "BB", "starting_stack": 100},
            ],
            "hero_player_id": "hero",
            "hero_cards": list(hero_cards),
            "board": list(board),
            "actions": [
                {
                    "street": "preflop",
                    "actor": "folded",
                    "action": "fold",
                    "amount": 0,
                },
                {
                    "street": "river",
                    "actor": "villain",
                    "action": "bet",
                    "amount": 20,
                },
            ],
        }
    )
    definition = VersionedRangeDefinitionV1.model_validate(
        {
            "range_id": "villain-river",
            "target_player_id": "villain",
            "notation": notation,
            "source": {
                "source_id": "river-equity-fixture",
                "source_kind": "repository_fixture",
                "license_classification": "repository_owned_mit",
                "usage_classification": "redistribution_allowed",
                "content_status": "ASSUMPTION",
                "content_sha256": hashlib.sha256(notation.encode("utf-8")).hexdigest(),
            },
            "game_conditions": {
                "game_type": "NLHE",
                "format": "cash",
                "table_size": 3,
                "target_position": "BB",
                "street": "river",
                "starting_stack_min_bb_milli": 50_000,
                "starting_stack_max_bb_milli": 50_000,
                "as_of_action_index": 2,
                "action_prefix_sha256": action_prefix_sha256(base, 2),
            },
        }
    )
    hand_payload = base.model_dump(mode="json")
    hand_payload["known_ranges"] = [definition.model_dump(mode="json")]
    return CaseInput.model_validate(
        {
            "kind": "calculation",
            "hand": hand_payload,
            "analysis_scope": "retrospective",
            "requested_tools": ["combos", "holdem_equity"],
            "metadata": metadata or {},
        }
    )
