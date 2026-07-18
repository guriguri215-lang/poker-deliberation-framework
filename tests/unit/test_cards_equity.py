import math

import pytest

from poker_deliberation.tools.cards import DECK, evaluate_five, evaluate_holdem
from poker_deliberation.tools.equity import holdem_equity


def test_wheel_straight_and_royal_flush_ordering() -> None:
    wheel = evaluate_five(["As", "2d", "3c", "4h", "5s"])
    royal = evaluate_five(["Ts", "Js", "Qs", "Ks", "As"])
    assert wheel == (4, 5)
    assert royal == (8, 14)
    assert royal > wheel


def test_seven_card_evaluator_known_showdown() -> None:
    board = ["2c", "3d", "4h", "5s", "9c"]
    assert evaluate_holdem(["As", "Ah"], board) > evaluate_holdem(["Kc", "Kd"], board)


def test_exact_equity_on_complete_board() -> None:
    result = holdem_equity(
        hero_range="AsAh",
        villain_range="KcKd",
        board=("2c", "3d", "4h", "5s", "9c"),
        mode="exact",
    )
    assert result["exact"] is True
    assert result["hero_equity"] == 1.0


def test_monte_carlo_seed_is_reproducible() -> None:
    kwargs = {
        "hero_range": "AKs",
        "villain_range": "QQ",
        "mode": "monte_carlo",
        "samples": 250,
        "seed": 19,
    }
    first = holdem_equity(**kwargs)
    second = holdem_equity(**kwargs)
    assert first == second
    assert math.isclose(first["hero_equity"], second["hero_equity"])


def test_duplicate_known_card_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        holdem_equity(
            hero_range="AsAh",
            villain_range="KcKd",
            board=("2c", "3d", "4h"),
            dead_cards=("2c",),
        )


def test_one_sample_monte_carlo_interval_does_not_claim_certainty() -> None:
    result = holdem_equity(
        hero_range="AsAh",
        villain_range="KcKd",
        board=("2c", "3d", "4h", "8s"),
        mode="monte_carlo",
        samples=1,
        seed=0,
    )
    assert result["confidence_interval_95"] == [0.0, 1.0]
    assert "Hoeffding" in result["confidence_interval_method"]


def test_equity_rejects_impossible_dead_card_state() -> None:
    fixed = {"As", "Ah", "Kc", "Kd", "2c", "3d", "4h"}
    dead = tuple(card for card in DECK if card not in fixed)[:44]
    with pytest.raises(ValueError, match="not enough undealt cards"):
        holdem_equity(
            hero_range="AsAh",
            villain_range="KcKd",
            board=("2c", "3d", "4h"),
            dead_cards=dead,
            mode="exact",
        )
