"""Deterministic binary64 poker formulas with explicit toy-model assumptions."""

from __future__ import annotations

import math


def _finite_non_negative(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and non-negative")
    return number


def _finite_positive(name: str, value: float) -> float:
    number = _finite_non_negative(name, value)
    if number == 0:
        raise ValueError(f"{name} must be positive")
    return number


def _fraction(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or not 0 <= number <= 1:
        raise ValueError(f"{name} must be in [0, 1]")
    return number


def effective_stack(*, stacks: list[float]) -> dict[str, object]:
    if len(stacks) < 2:
        raise ValueError("stacks requires at least two players")
    checked = [
        _finite_non_negative(f"stacks[{index}]", value) for index, value in enumerate(stacks)
    ]
    return {
        "effective_stack": min(checked),
        "stacks": checked,
        "formula": "min(stacks)",
    }


def stack_to_pot_ratio(*, effective_stack: float, pot: float) -> dict[str, float | str]:
    stack = _finite_non_negative("effective_stack", effective_stack)
    checked_pot = _finite_positive("pot", pot)
    return {
        "spr": stack / checked_pot,
        "effective_stack": stack,
        "pot": checked_pot,
        "formula": "effective_stack / pot",
    }


def minimum_defense_frequency(*, pot_before_bet: float, bet: float) -> dict[str, float | str]:
    pot = _finite_positive("pot_before_bet", pot_before_bet)
    checked_bet = _finite_positive("bet", bet)
    frequency = pot / (pot + checked_bet)
    return {
        "minimum_defense_frequency": frequency,
        "minimum_defense_percent": frequency * 100,
        "formula": "pot_before_bet / (pot_before_bet + bet)",
    }


def rake_amount(
    *, pot_total: float, rake_percent: float, rake_cap: float | None = None
) -> dict[str, float | str | None]:
    pot = _finite_non_negative("pot_total", pot_total)
    percent = _finite_non_negative("rake_percent", rake_percent)
    if percent >= 100:
        raise ValueError("rake_percent must be smaller than 100")
    cap = None if rake_cap is None else _finite_non_negative("rake_cap", rake_cap)
    raw = pot * percent / 100
    rake = min(raw, cap) if cap is not None else raw
    return {
        "rake_amount": rake,
        "raw_rake": raw,
        "rake_cap": cap,
        "formula": "min(pot_total * rake_percent / 100, rake_cap) when capped",
    }


def raked_call_ev(
    *,
    equity: float,
    pot_after_bet: float,
    call_cost: float,
    rake_percent: float,
    rake_cap: float | None = None,
) -> dict[str, float | str | None]:
    checked_equity = _fraction("equity", equity)
    pot = _finite_non_negative("pot_after_bet", pot_after_bet)
    call = _finite_positive("call_cost", call_cost)
    total = pot + call
    rake_output = rake_amount(
        pot_total=total,
        rake_percent=rake_percent,
        rake_cap=rake_cap,
    )
    rake_value = rake_output["rake_amount"]
    if not isinstance(rake_value, float):  # internal contract guard
        raise TypeError("rake_amount returned a non-numeric value")
    rake = rake_value
    value = checked_equity * (total - rake) - call
    return {
        "ev": value,
        "rake_amount": rake,
        "final_pot_after_rake": total - rake,
        "formula": "equity * (pot_after_bet + call_cost - rake) - call_cost",
        "model": "single decision, no future betting, declared final-pot rake",
    }


def bluff_ev(
    *,
    fold_frequency: float,
    pot_before_bet: float,
    bet: float,
    equity_when_called: float = 0.0,
) -> dict[str, float | str]:
    folds = _fraction("fold_frequency", fold_frequency)
    equity = _fraction("equity_when_called", equity_when_called)
    pot = _finite_positive("pot_before_bet", pot_before_bet)
    checked_bet = _finite_positive("bet", bet)
    called_ev = equity * (pot + 2 * checked_bet) - checked_bet
    value = folds * pot + (1 - folds) * called_ev
    return {
        "ev": value,
        "called_branch_ev": called_ev,
        "formula": "F*pot + (1-F)*(equity*(pot+2*bet)-bet)",
        "model": "single street, call-or-fold response, no rake or future betting",
    }


def polar_river_bluff_fraction(*, pot_before_bet: float, bet: float) -> dict[str, float | str]:
    pot = _finite_positive("pot_before_bet", pot_before_bet)
    checked_bet = _finite_positive("bet", bet)
    frequency = checked_bet / (pot + 2 * checked_bet)
    return {
        "bluff_fraction": frequency,
        "bluff_percent": frequency * 100,
        "formula": "bet / (pot_before_bet + 2*bet)",
        "model": "polarized river bettor versus bluff-catcher, no rake",
    }


def bayes_update(
    *, prior: float, likelihood_given_h: float, likelihood_given_not_h: float
) -> dict[str, float | str]:
    checked_prior = _fraction("prior", prior)
    likelihood_h = _fraction("likelihood_given_h", likelihood_given_h)
    likelihood_not_h = _fraction("likelihood_given_not_h", likelihood_given_not_h)
    denominator = checked_prior * likelihood_h + (1 - checked_prior) * likelihood_not_h
    if denominator == 0:
        raise ValueError("the evidence has zero total probability")
    posterior = checked_prior * likelihood_h / denominator
    return {
        "posterior": posterior,
        "evidence_probability": denominator,
        "formula": "P(E|H)P(H) / (P(E|H)P(H) + P(E|not H)P(not H))",
    }
