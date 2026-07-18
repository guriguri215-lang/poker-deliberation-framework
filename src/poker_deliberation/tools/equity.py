"""Heads-up Hold'em exact enumeration with bounded Monte Carlo fallback."""

from __future__ import annotations

import math
import random
from itertools import combinations

from poker_deliberation.tools.cards import DECK, evaluate_holdem, normalize_cards
from poker_deliberation.tools.combinations import WeightedCombo, parse_weighted_range

HARD_MAX_EXACT_EVALUATIONS = 2_000_000
HARD_MAX_MONTE_CARLO_SAMPLES = 1_000_000


def _valid_pairs(
    hero: list[WeightedCombo], villain: list[WeightedCombo], dead: set[str]
) -> list[tuple[WeightedCombo, WeightedCombo, float]]:
    pairs: list[tuple[WeightedCombo, WeightedCombo, float]] = []
    for hero_combo in hero:
        if dead.intersection(hero_combo.cards):
            continue
        for villain_combo in villain:
            if set(hero_combo.cards).intersection(villain_combo.cards):
                continue
            if dead.intersection(villain_combo.cards):
                continue
            pairs.append((hero_combo, villain_combo, hero_combo.weight * villain_combo.weight))
    if not pairs:
        raise ValueError("hero and villain ranges have no non-overlapping legal combo pairs")
    return pairs


def _score(
    hero_cards: tuple[str, str], villain_cards: tuple[str, str], board: tuple[str, ...]
) -> float:
    hero_rank = evaluate_holdem(hero_cards, board)
    villain_rank = evaluate_holdem(villain_cards, board)
    if hero_rank > villain_rank:
        return 1.0
    if hero_rank == villain_rank:
        return 0.5
    return 0.0


def holdem_equity(
    *,
    hero_range: str,
    villain_range: str,
    board: tuple[str, ...] = (),
    dead_cards: tuple[str, ...] = (),
    mode: str = "auto",
    max_exact_evaluations: int = 250_000,
    samples: int = 50_000,
    seed: int = 0,
) -> dict[str, object]:
    normalized_board = normalize_cards(board)
    normalized_dead = normalize_cards(dead_cards)
    if len(normalized_board) not in {0, 3, 4, 5}:
        raise ValueError("board must contain 0, 3, 4, or 5 cards")
    known = set(normalize_cards((*normalized_board, *normalized_dead)))
    hero = parse_weighted_range(hero_range, tuple(known))
    villain = parse_weighted_range(villain_range, tuple(known))
    pairs = _valid_pairs(hero, villain, known)
    cards_to_come = 5 - len(normalized_board)
    if cards_to_come < 0:
        raise ValueError("board contains too many cards")
    available_count = 52 - len(known) - 4
    if available_count < cards_to_come:
        raise ValueError("not enough undealt cards remain to complete the board")
    boards_per_pair = math.comb(available_count, cards_to_come)
    estimated_evaluations = len(pairs) * boards_per_pair
    if mode not in {"auto", "exact", "monte_carlo"}:
        raise ValueError("mode must be auto, exact, or monte_carlo")
    if not 1 <= max_exact_evaluations <= HARD_MAX_EXACT_EVALUATIONS:
        raise ValueError(
            f"max_exact_evaluations must be between 1 and {HARD_MAX_EXACT_EVALUATIONS}"
        )
    use_exact = mode == "exact" or (
        mode == "auto" and estimated_evaluations <= max_exact_evaluations
    )
    if use_exact and estimated_evaluations > max_exact_evaluations:
        raise ValueError(
            f"exact enumeration requires {estimated_evaluations} evaluations, "
            f"above limit {max_exact_evaluations}"
        )
    if use_exact:
        weighted_score = 0.0
        weighted_total = 0.0
        wins = ties = losses = 0
        evaluations = 0
        for hero_combo, villain_combo, pair_weight in pairs:
            excluded = known | set(hero_combo.cards) | set(villain_combo.cards)
            exact_remaining = tuple(card for card in DECK if card not in excluded)
            for future_cards in combinations(exact_remaining, cards_to_come):
                full_board = (*normalized_board, *future_cards)
                score = _score(hero_combo.cards, villain_combo.cards, full_board)
                weighted_score += score * pair_weight
                weighted_total += pair_weight
                wins += score == 1.0
                ties += score == 0.5
                losses += score == 0.0
                evaluations += 1
        return {
            "method": "exact_enumeration",
            "exact": True,
            "hero_equity": weighted_score / weighted_total,
            "evaluations": evaluations,
            "unweighted_wins": wins,
            "unweighted_ties": ties,
            "unweighted_losses": losses,
            "range_pair_count": len(pairs),
            "cards_to_come": cards_to_come,
        }
    if not 1 <= samples <= HARD_MAX_MONTE_CARLO_SAMPLES:
        raise ValueError(f"samples must be between 1 and {HARD_MAX_MONTE_CARLO_SAMPLES}")
    rng = random.Random(seed)
    weights = [pair[2] for pair in pairs]
    values: list[float] = []
    wins = ties = losses = 0
    for _ in range(samples):
        hero_combo, villain_combo, _weight = rng.choices(pairs, weights=weights, k=1)[0]
        excluded = known | set(hero_combo.cards) | set(villain_combo.cards)
        sampled_remaining = [card for card in DECK if card not in excluded]
        sampled_future = rng.sample(sampled_remaining, cards_to_come)
        score = _score(
            hero_combo.cards,
            villain_combo.cards,
            (*normalized_board, *sampled_future),
        )
        values.append(score)
        wins += score == 1.0
        ties += score == 0.5
        losses += score == 0.0
    mean = sum(values) / samples
    margin = math.sqrt(math.log(2 / 0.05) / (2 * samples))
    return {
        "method": "monte_carlo",
        "exact": False,
        "hero_equity": mean,
        "confidence_interval_95": [max(0.0, mean - margin), min(1.0, mean + margin)],
        "confidence_interval_method": "two-sided Hoeffding bound for independent [0,1] scores",
        "samples": samples,
        "seed": seed,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "range_pair_count": len(pairs),
        "estimated_exact_evaluations": estimated_evaluations,
        "cards_to_come": cards_to_come,
    }
