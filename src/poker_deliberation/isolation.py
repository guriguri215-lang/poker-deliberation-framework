"""Mechanical decision-time isolation for blind baseline analysis."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from poker_deliberation.schemas import (
    BlindDecisionContext,
    CaseInput,
    DecisionSnapshot,
    FocalDecision,
)
from poker_deliberation.tools.hand_validator import validate_hand

FORBIDDEN_KEYS = frozenset(
    {
        "claim",
        "claims",
        "conclusion",
        "hypothesis",
        "preferred_action",
        "realized_result",
        "result",
        "showdown",
        "user_claim",
        "verdict",
        "winner",
        "winner_player_id",
    }
)
VOLUNTARY_ACTIONS = frozenset({"fold", "check", "call", "bet", "raise", "all_in"})


class IsolationError(RuntimeError):
    """Raised when excluded information reaches a blind payload."""


def resolve_focal_decision(case: CaseInput) -> FocalDecision | None:
    """Use the explicit locator, or conservatively select the last Hero decision."""

    if case.focal_decision is not None:
        return case.focal_decision
    if case.hand is None or case.hand.hero_player_id is None:
        return None
    for index in range(len(case.hand.actions) - 1, -1, -1):
        action = case.hand.actions[index]
        if action.actor == case.hand.hero_player_id and action.action in VOLUNTARY_ACTIONS:
            return FocalDecision(street=action.street, action_index=index, actor=action.actor)
    return None


def build_blind_decision_context(case: CaseInput) -> BlindDecisionContext | None:
    """Build a payload containing only facts available before the focal action."""

    if case.hand is None:
        return None
    focal = resolve_focal_decision(case)
    if focal is None:
        return None
    replay = validate_hand(case.hand)
    if not replay.get("valid", False):
        return None
    raw_snapshots = replay.get("decision_snapshots", [])
    if not isinstance(raw_snapshots, list) or focal.action_index >= len(raw_snapshots):
        raise IsolationError("focal decision snapshot is unavailable")
    snapshot = DecisionSnapshot.model_validate(raw_snapshots[focal.action_index])
    if snapshot.street is not focal.street or snapshot.actor != focal.actor:
        raise IsolationError("focal decision does not match its reconstructed snapshot")
    hand = case.hand
    context = BlindDecisionContext(
        game={
            "game_type": hand.game_type,
            "format": hand.format,
            "table_size": hand.table_size,
            "small_blind": hand.small_blind,
            "big_blind": hand.big_blind,
            "ante": hand.ante,
            "rake": hand.rake,
        },
        players=hand.players,
        hero_player_id=hand.hero_player_id,
        hero_cards=hand.hero_cards,
        focal=snapshot,
    )
    verify_blind_payload(case, context)
    return context


def _walk_keys(value: Any) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key).casefold()
            yield from _walk_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_keys(item)


def verify_blind_payload(case: CaseInput, payload: BlindDecisionContext) -> str:
    """Serialize and reject forbidden keys or post-decision shown cards."""

    dumped = payload.model_dump(mode="json")
    serialized = json.dumps(dumped, ensure_ascii=False, sort_keys=True, allow_nan=False)
    forbidden = FORBIDDEN_KEYS.intersection(_walk_keys(dumped))
    if forbidden:
        raise IsolationError(f"forbidden blind-context keys: {sorted(forbidden)}")
    result = case.realized_result
    if result is not None:
        visible_cards = set(payload.hero_cards) | set(payload.focal.board)
        for cards in result.shown_cards.values():
            for card in cards:
                if card not in visible_cards and json.dumps(card) in serialized:
                    raise IsolationError("a post-decision shown card leaked into blind context")
    return serialized
