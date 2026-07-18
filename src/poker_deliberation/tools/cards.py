"""Small, dependency-free Texas Hold'em card evaluator."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from itertools import combinations, pairwise

RANKS = "23456789TJQKA"
SUITS = "cdhs"
RANK_VALUE = {rank: index + 2 for index, rank in enumerate(RANKS)}
DECK = tuple(f"{rank}{suit}" for rank in RANKS for suit in SUITS)


def normalize_card(card: str) -> str:
    value = card.strip()
    if len(value) != 2:
        raise ValueError(f"invalid card: {card!r}")
    rank = value[0].upper()
    suit = value[1].lower()
    if rank not in RANKS or suit not in SUITS:
        raise ValueError(f"invalid card: {card!r}")
    return rank + suit


def normalize_cards(cards: Iterable[str], *, unique: bool = True) -> tuple[str, ...]:
    normalized = tuple(normalize_card(card) for card in cards)
    if unique and len(normalized) != len(set(normalized)):
        raise ValueError("duplicate cards are not allowed")
    return normalized


def _straight_high(ranks: set[int]) -> int | None:
    if 14 in ranks:
        ranks = ranks | {1}
    ordered = sorted(ranks)
    run = 1
    best: int | None = None
    for previous, current in pairwise(ordered):
        if current == previous + 1:
            run += 1
            if run >= 5:
                best = current
        elif current != previous:
            run = 1
    return best


def evaluate_five(cards: Iterable[str]) -> tuple[int, ...]:
    normalized = normalize_cards(cards)
    if len(normalized) != 5:
        raise ValueError("evaluate_five requires exactly five cards")
    ranks = [RANK_VALUE[card[0]] for card in normalized]
    suits = [card[1] for card in normalized]
    counts = Counter(ranks)
    groups = sorted(((count, rank) for rank, count in counts.items()), reverse=True)
    is_flush = len(set(suits)) == 1
    straight_high = _straight_high(set(ranks))
    if is_flush and straight_high is not None:
        return (8, straight_high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        kicker = max(rank for rank in ranks if rank != quad)
        return (7, quad, kicker)
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5, *sorted(ranks, reverse=True))
    if straight_high is not None:
        return (4, straight_high)
    if groups[0][0] == 3:
        trip = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != trip), reverse=True)
        return (3, trip, *kickers)
    pairs = sorted((rank for rank, count in counts.items() if count == 2), reverse=True)
    if len(pairs) == 2:
        kicker = max(rank for rank in ranks if rank not in pairs)
        return (2, pairs[0], pairs[1], kicker)
    if len(pairs) == 1:
        pair = pairs[0]
        kickers = sorted((rank for rank in ranks if rank != pair), reverse=True)
        return (1, pair, *kickers)
    return (0, *sorted(ranks, reverse=True))


def evaluate_holdem(hole_cards: Iterable[str], board: Iterable[str]) -> tuple[int, ...]:
    hole = normalize_cards(hole_cards)
    community = normalize_cards(board)
    all_cards = normalize_cards((*hole, *community))
    if len(hole) != 2 or len(community) != 5:
        raise ValueError("Texas Hold'em evaluation requires two hole cards and five board cards")
    return max(evaluate_five(combo) for combo in combinations(all_cards, 5))
