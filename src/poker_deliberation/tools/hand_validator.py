"""Canonical hand consistency checks without inventing missing data."""

from __future__ import annotations

import math

from poker_deliberation.schemas import CanonicalHand, Street
from poker_deliberation.tools.cards import normalize_cards
from poker_deliberation.tools.numeric import close_absolute

STREET_ORDER = {
    Street.PREFLOP: 0,
    Street.FLOP: 1,
    Street.TURN: 2,
    Street.RIVER: 3,
    Street.SHOWDOWN: 4,
}


def _visible_board(hand: CanonicalHand, street: Street) -> list[str]:
    if street is Street.PREFLOP:
        return []
    if street is Street.FLOP:
        return list(hand.board[:3])
    if street is Street.TURN:
        return list(hand.board[:4])
    return list(hand.board[:5])


def _contestable_pot_before_short_call(
    pot: float,
    contributions: dict[str, float],
    actor: str,
    actual_call: float,
) -> float:
    """Cap every current-street contribution at the short caller's final level."""

    caller_cap = contributions[actor] + actual_call
    prior_street_and_antes = max(0.0, pot - sum(contributions.values()))
    # A folded player's chips stay in the pot, but any layer above caller_cap belongs to
    # side pots for players who contributed to those layers; the short caller cannot win it.
    current_street_contestable = sum(
        min(contribution, caller_cap) for contribution in contributions.values()
    )
    return prior_street_and_antes + current_street_contestable


def _derived_chip_tolerance(hand: CanonicalHand) -> float:
    magnitudes = [
        hand.small_blind,
        hand.big_blind,
        hand.ante,
        *(player.starting_stack for player in hand.players),
    ]
    for action in hand.actions:
        magnitudes.extend(
            value
            for value in (action.amount, action.to_amount, action.pot_before, action.pot_after)
            if value is not None
        )
    scale = max(1.0, *(abs(value) for value in magnitudes))
    operation_bound = max(32, 4 * (len(hand.actions) + len(hand.players)))
    return math.ulp(scale) * operation_bound


def validate_hand(hand: CanonicalHand, *, tolerance: float | None = None) -> dict[str, object]:
    if tolerance is not None and (not math.isfinite(tolerance) or tolerance < 0):
        raise ValueError("tolerance must be finite and non-negative")
    tolerance = _derived_chip_tolerance(hand) if tolerance is None else tolerance
    errors: list[str] = []
    warnings: list[str] = []
    try:
        normalize_cards((*hand.hero_cards, *hand.board))
    except ValueError as exc:
        errors.append(str(exc))
    expected_board_lengths = {0, 3, 4, 5}
    if len(hand.board) not in expected_board_lengths:
        errors.append("board must contain 0, 3, 4, or 5 cards")
    stacks = {player.player_id: player.starting_stack for player in hand.players}
    active = set(stacks)
    all_in_players: set[str] = set()
    contributions = {player_id: 0.0 for player_id in stacks}
    pot = 0.0
    current_street: Street | None = None
    current_bet = 0.0
    last_full_raise = hand.big_blind
    acted_since_full_raise: set[str] = set()
    reconstructed: list[dict[str, object]] = []
    decision_snapshots: list[dict[str, object]] = []
    history: list[str] = []
    for index, action in enumerate(hand.actions):
        prefix = f"action[{index}]"
        if action.actor not in stacks:
            errors.append(f"{prefix}: unknown actor {action.actor!r}")
            continue
        if (
            current_street is not None
            and STREET_ORDER[action.street] < STREET_ORDER[current_street]
        ):
            errors.append(f"{prefix}: street order moves backwards")
        if action.street != current_street:
            if current_street is not None:
                eligible = active - all_in_players
                unsettled = sorted(
                    player_id
                    for player_id in eligible
                    if current_bet - contributions[player_id] > tolerance
                )
                if unsettled:
                    errors.append(
                        f"{prefix}: previous street {current_street.value} is incomplete; "
                        f"players facing an outstanding bet: {unsettled}"
                    )
                missing_action = sorted(eligible - acted_since_full_raise)
                if len(active) > 1 and missing_action:
                    errors.append(
                        f"{prefix}: previous street {current_street.value} is incomplete; "
                        f"players have not acted since the last full bet/raise: {missing_action}"
                    )
                if len(active) <= 1:
                    errors.append(f"{prefix}: a new street cannot start after the hand has ended")
            current_street = action.street
            contributions = {player_id: 0.0 for player_id in stacks}
            current_bet = 0.0
            last_full_raise = hand.big_blind
            acted_since_full_raise = set()
        if action.actor not in active:
            errors.append(f"{prefix}: folded player acts again")
        if action.actor in all_in_players:
            errors.append(f"{prefix}: all-in player acts again")
        if action.pot_before is not None and not close_absolute(
            action.pot_before,
            pot,
            absolute=tolerance,
        ):
            errors.append(f"{prefix}: pot_before={action.pot_before} but reconstructed pot={pot}")
        if action.amount > stacks[action.actor] + tolerance:
            errors.append(f"{prefix}: stack underflow")
        outstanding = max(0.0, current_bet - contributions[action.actor])
        stack_before = max(0.0, stacks[action.actor])
        actual_call: float | None = None
        contestable_pot = pot
        side_pot_risk = False
        if action.action == "call":
            actual_call = min(outstanding, stack_before)
        elif action.action == "all_in" and outstanding > tolerance:
            actual_call = min(action.amount, outstanding)
        if actual_call is not None and actual_call + tolerance < outstanding:
            contestable_pot = _contestable_pot_before_short_call(
                pot,
                contributions,
                action.actor,
                actual_call,
            )
            side_pot_risk = len(active) > 2
        decision_snapshots.append(
            {
                "street": action.street.value,
                "action_index": index,
                "actor": action.actor,
                "board": _visible_board(hand, action.street),
                "pot_before": pot,
                "to_call": outstanding,
                "actual_call": actual_call,
                "contestable_pot": contestable_pot,
                "current_bet": current_bet,
                "actor_invested": contributions[action.actor],
                "stack_behind": stack_before,
                "history_before": list(history),
                "facing_action": (
                    f"facing bet/raise, to_call={outstanding:g}"
                    if outstanding > tolerance
                    else "unopened"
                ),
                "side_pot_risk": side_pot_risk,
            }
        )
        if action.action == "check":
            if action.amount != 0:
                errors.append(f"{prefix}: check amount must be zero")
            if outstanding > tolerance:
                errors.append(f"{prefix}: cannot check while facing an outstanding bet")
            acted_since_full_raise.add(action.actor)
        if action.action == "fold":
            if action.amount != 0:
                errors.append(f"{prefix}: fold amount must be zero")
            active.discard(action.actor)
        if action.action in {"bet", "raise"}:
            if action.to_amount is None:
                errors.append(f"{prefix}: bet/raise requires to_amount")
            else:
                expected_increment = action.to_amount - contributions[action.actor]
                if not close_absolute(expected_increment, action.amount, absolute=tolerance):
                    errors.append(
                        f"{prefix}: amount does not match to_amount minus prior contribution"
                    )
                if action.action == "bet" and current_bet > tolerance:
                    errors.append(f"{prefix}: use raise, not bet, when a bet is outstanding")
                if action.action == "raise" and current_bet <= tolerance:
                    errors.append(f"{prefix}: use bet, not raise, when no bet is outstanding")
                raise_size = action.to_amount - current_bet
                if raise_size <= tolerance:
                    errors.append(f"{prefix}: bet/raise must increase the current bet")
                if action.action == "bet" and raise_size + tolerance < hand.big_blind:
                    errors.append(
                        f"{prefix}: bet is below the minimum opening bet of {hand.big_blind}"
                    )
                if action.action == "raise" and raise_size + tolerance < last_full_raise:
                    errors.append(f"{prefix}: raise is below the known minimum full raise")
                if raise_size > 0:
                    minimum_full_raise = (
                        hand.big_blind if action.action == "bet" else last_full_raise
                    )
                    if raise_size + tolerance >= minimum_full_raise:
                        last_full_raise = raise_size
                        acted_since_full_raise = {action.actor}
                    else:
                        acted_since_full_raise.add(action.actor)
                    current_bet = action.to_amount
        if action.action == "call":
            required = outstanding
            if not close_absolute(
                action.amount,
                min(required, stacks[action.actor]),
                absolute=tolerance,
            ):
                errors.append(f"{prefix}: call amount does not match the outstanding bet")
            acted_since_full_raise.add(action.actor)
        if action.action == "all_in":
            if not close_absolute(action.amount, stacks[action.actor], absolute=tolerance):
                errors.append(f"{prefix}: all_in must commit the actor's remaining stack")
            all_in_total = contributions[action.actor] + action.amount
            if action.to_amount is not None and not close_absolute(
                action.to_amount,
                all_in_total,
                absolute=tolerance,
            ):
                errors.append(f"{prefix}: all_in to_amount does not match total contribution")
            if all_in_total > current_bet + tolerance:
                raise_size = all_in_total - current_bet
                if raise_size + tolerance >= last_full_raise:
                    last_full_raise = raise_size
                    acted_since_full_raise = {action.actor}
                else:
                    warnings.append("short all-in raise does not reopen betting")
                    acted_since_full_raise.add(action.actor)
                current_bet = all_in_total
            else:
                acted_since_full_raise.add(action.actor)
        if action.action in {"post_blind", "post_ante", "call", "bet", "raise", "all_in"}:
            stacks[action.actor] -= action.amount
            if action.action != "post_ante":
                contributions[action.actor] += action.amount
            pot += action.amount
            if action.action == "post_blind":
                current_bet = max(current_bet, contributions[action.actor])
            if stacks[action.actor] <= tolerance:
                stacks[action.actor] = 0.0
                all_in_players.add(action.actor)
            if action.action == "all_in":
                warnings.append(
                    "side-pot consistency requires explicit side-pot data and is not fully verified"
                )
        if action.pot_after is not None and not close_absolute(
            action.pot_after,
            pot,
            absolute=tolerance,
        ):
            errors.append(f"{prefix}: pot_after={action.pot_after} but reconstructed pot={pot}")
        reconstructed.append(
            {
                "index": index,
                "pot_after": pot,
                "stacks_after": dict(stacks),
                "active_players": sorted(active),
                "all_in_players": sorted(all_in_players),
            }
        )
        amount = f" amount={action.amount:g}" if action.amount else ""
        to_amount = f" to={action.to_amount:g}" if action.to_amount is not None else ""
        history.append(f"{action.street.value}: {action.actor} {action.action}{amount}{to_amount}")
    return {
        "valid": not errors,
        "verification_tolerance": tolerance,
        "errors": errors,
        "warnings": sorted(set(warnings)),
        "final_pot": pot,
        "remaining_stacks": stacks,
        "reconstructed_actions": reconstructed,
        "decision_snapshots": decision_snapshots,
        "limitations": [
            "Site-specific rules, rake timing, straddles, returned uncalled bets, "
            "and side pots need explicit data."
        ],
    }
