import math

import pytest

from poker_deliberation.tools.combinations import (
    combo_summary,
    expand_hand_class,
    parse_weighted_range,
)


def test_base_combo_counts() -> None:
    assert combo_summary("QQ")["count"] == 6
    assert combo_summary("AKs")["count"] == 4
    assert combo_summary("AKo")["count"] == 12


def test_blocker_removes_pair_combos() -> None:
    assert len(expand_hand_class("QQ", ("Qs",))) == 3


def test_weighted_range_and_normalization_input() -> None:
    combos = parse_weighted_range("AKs@0.5,QQ")
    assert len(combos) == 10
    assert math.isclose(sum(combo.weight for combo in combos), 8.0)


def test_overlapping_range_is_rejected() -> None:
    with pytest.raises(ValueError, match="overlapping"):
        parse_weighted_range("AKs,AsKs")


def test_non_pair_requires_suitedness() -> None:
    with pytest.raises(ValueError):
        expand_hand_class("AK")
