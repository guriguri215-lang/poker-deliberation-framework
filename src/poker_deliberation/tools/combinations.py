"""Hold'em combo expansion, blocker removal, and weighted range parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass

from poker_deliberation.tools.cards import RANKS, SUITS, normalize_card, normalize_cards

HAND_CLASS_RE = re.compile(r"^([2-9TJQKA])([2-9TJQKA])(s|o)?$", re.IGNORECASE)
SPECIFIC_COMBO_RE = re.compile(r"^([2-9TJQKA][cdhs])([2-9TJQKA][cdhs])$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class WeightedCombo:
    cards: tuple[str, str]
    weight: float = 1.0


def _canonical_combo(first: str, second: str) -> tuple[str, str]:
    if first == second:
        raise ValueError("a combo cannot contain the same card twice")
    return tuple(sorted((first, second)))  # type: ignore[return-value]


def expand_hand_class(hand_class: str, dead_cards: tuple[str, ...] = ()) -> list[tuple[str, str]]:
    token = hand_class.strip()
    specific = SPECIFIC_COMBO_RE.fullmatch(token)
    dead = set(normalize_cards(dead_cards))
    if specific:
        combo = _canonical_combo(normalize_card(specific[1]), normalize_card(specific[2]))
        return [] if dead.intersection(combo) else [combo]
    match = HAND_CLASS_RE.fullmatch(token)
    if not match:
        raise ValueError(f"unsupported range token: {hand_class!r}")
    first_rank, second_rank, suffix = match[1].upper(), match[2].upper(), match[3]
    suffix = suffix.lower() if suffix else None
    if first_rank == second_rank:
        if suffix is not None:
            raise ValueError("pairs cannot use suited/offsuit suffix")
        combos = [
            _canonical_combo(first_rank + first_suit, first_rank + second_suit)
            for index, first_suit in enumerate(SUITS)
            for second_suit in SUITS[index + 1 :]
        ]
    else:
        if suffix is None:
            raise ValueError("non-pairs must specify 's' or 'o'")
        if suffix == "s":
            combos = [_canonical_combo(first_rank + suit, second_rank + suit) for suit in SUITS]
        else:
            combos = [
                _canonical_combo(first_rank + first_suit, second_rank + second_suit)
                for first_suit in SUITS
                for second_suit in SUITS
                if first_suit != second_suit
            ]
    return sorted(combo for combo in combos if not dead.intersection(combo))


def parse_weighted_range(notation: str, dead_cards: tuple[str, ...] = ()) -> list[WeightedCombo]:
    raw_tokens = [token for token in re.split(r"[\s,]+", notation.strip()) if token]
    if not raw_tokens:
        raise ValueError("range notation is empty")
    weights: dict[tuple[str, str], float] = {}
    for raw in raw_tokens:
        token, separator, weight_text = raw.partition("@")
        if not separator:
            token, separator, weight_text = raw.partition(":")
        weight = float(weight_text) if separator else 1.0
        if not 0 < weight <= 1:
            raise ValueError("combo weights must be in (0, 1]")
        for combo in expand_hand_class(token, dead_cards):
            if combo in weights:
                raise ValueError(f"overlapping range combo: {combo}")
            weights[combo] = weight
    if not weights:
        raise ValueError("range contains no legal combos after blocker removal")
    return [WeightedCombo(cards=combo, weight=weight) for combo, weight in sorted(weights.items())]


def combo_summary(hand_class: str, dead_cards: tuple[str, ...] = ()) -> dict[str, object]:
    combos = expand_hand_class(hand_class, dead_cards)
    return {"hand_class": hand_class, "count": len(combos), "combos": [list(c) for c in combos]}


def rank_order_key(rank: str) -> int:
    return RANKS.index(rank)
