"""Deterministic binary64 pot-odds and break-even calculations."""

from __future__ import annotations

import math


def _non_negative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")


def pot_odds(
    *,
    pot_before_bet: float,
    opponent_bet: float,
    call_cost: float,
    expected_rake: float = 0.0,
) -> dict[str, float]:
    for name, value in {
        "pot_before_bet": pot_before_bet,
        "opponent_bet": opponent_bet,
        "call_cost": call_cost,
        "expected_rake": expected_rake,
    }.items():
        _non_negative(name, value)
    final_pot_before_rake = pot_before_bet + opponent_bet + call_cost
    if call_cost == 0:
        raise ValueError("call_cost must be positive")
    if expected_rake >= final_pot_before_rake:
        raise ValueError("expected_rake must be smaller than the final pot")
    final_pot = final_pot_before_rake - expected_rake
    required_equity = call_cost / final_pot
    return {
        "pot_after_opponent_bet": pot_before_bet + opponent_bet,
        "final_pot_before_rake": final_pot_before_rake,
        "expected_rake": expected_rake,
        "final_pot_after_rake": final_pot,
        "required_equity": required_equity,
        "required_equity_percent": required_equity * 100,
        "pot_odds_against": (final_pot - call_cost) / call_cost,
    }


def break_even_fold_frequency(*, risk: float, reward: float) -> dict[str, float]:
    for name, value in {
        "risk": risk,
        "reward": reward,
    }.items():
        _non_negative(name, value)
    if risk <= 0 or reward <= 0:
        raise ValueError("risk and reward must be positive")
    frequency = risk / (risk + reward)
    return {
        "risk": risk,
        "reward": reward,
        "break_even_fold_frequency": frequency,
        "break_even_fold_percent": frequency * 100,
    }


def reconstruct_pot(*, starting_pot: float, contributions: list[float]) -> dict[str, object]:
    _non_negative("starting_pot", starting_pot)
    running = starting_pot
    pots = [running]
    for index, contribution in enumerate(contributions):
        _non_negative(f"contributions[{index}]", contribution)
        running += contribution
        pots.append(running)
    return {
        "starting_pot": starting_pot,
        "pots_after_each_contribution": pots[1:],
        "final_pot": running,
    }
