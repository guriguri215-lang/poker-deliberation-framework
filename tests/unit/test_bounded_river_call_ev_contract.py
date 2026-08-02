from __future__ import annotations

from fractions import Fraction

import pytest

from poker_deliberation.bounded_river_call_ev import (
    prepare_bounded_river_call_ev_intake,
    verify_bounded_river_call_ev_candidate,
)
from tests.bounded_river_call_ev_support import (
    admission,
    multiplayer_river_source,
    range_definition,
    ready_preparation,
    river_source,
)


def test_preparation_binds_exact_candidate_and_no_rake_oracle() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    candidate = verify_bounded_river_call_ev_candidate(prepared.candidate)
    model = candidate.projection.call_ev_model

    assert model.rake_percent == 0
    assert model.rake_cap is None
    assert model.no_future_betting is True
    assert Fraction(model.required_equity.numerator, model.required_equity.denominator) == (
        Fraction(5, 24)
    )
    assert model.action_comparison == "call"


def test_admission_distributes_only_canonical_case_without_raw_text() -> None:
    admitted = admission()

    assert admitted.case.raw_text is None
    assert admitted.case.hand is not None
    assert admitted.case.requested_tools == [
        "hand_validator",
        "hand_pot_ledger",
        "pot_odds",
        "range_validate",
        "combos",
        "holdem_equity",
        "raked_call_ev",
    ]


@pytest.mark.parametrize(
    ("notation", "expected_equity", "comparison", "call_ev"),
    [
        ("QcJc", Fraction(1), "call", Fraction(38)),
        ("9c9d", Fraction(0), "fold", Fraction(-10)),
        ("QcJc@0.05,9c9d@0.19", Fraction(5, 24), "tie", Fraction(0)),
    ],
)
def test_exact_call_fold_and_zero_delta_oracles(
    notation: str,
    expected_equity: Fraction,
    comparison: str,
    call_ev: Fraction,
) -> None:
    prepared = ready_preparation(notation=notation)
    assert prepared.candidate is not None
    model = prepared.candidate.projection.call_ev_model

    assert Fraction(model.equity.numerator, model.equity.denominator) == expected_equity
    assert Fraction(model.call_ev_units.numerator, model.call_ev_units.denominator) == call_ev
    assert model.action_comparison == comparison


def test_actual_fold_binds_counterfactual_call_ev() -> None:
    folded = ready_preparation(source_bytes=river_source())
    assert folded.candidate is not None

    focal = folded.candidate.projection.bounded_candidate.projection.focal_decision
    assert focal.hero_response == "fold"
    assert folded.candidate.projection.call_ev_model.action_comparison == "call"
    assert folded.candidate.projection.call_ev_model.call_minus_fold_ev_units == (
        folded.candidate.projection.call_ev_model.call_ev_units
    )


@pytest.mark.parametrize("table_size", [3, 6])
def test_folded_historical_players_are_allowed_but_not_equity_eligible(
    table_size: int,
) -> None:
    prepared = ready_preparation(source_bytes=multiplayer_river_source(table_size))
    assert prepared.candidate is not None
    target = prepared.candidate.projection.range_target

    assert target.eligible_player_ids == ("Hero", "Villain")
    assert prepared.candidate.projection.bounded_candidate.projection.hand.table_size == table_size


def test_blockers_reduce_class_range_and_empty_post_blocker_fails_closed() -> None:
    reduced = ready_preparation(notation="AKs")
    assert reduced.candidate is not None
    assert reduced.candidate.projection.range_equity_binding.combo_count < 4

    source = river_source()
    blocked = prepare_bounded_river_call_ev_intake(
        source,
        range_definition(source, "AsKd"),
        intake_id="intake-empty-blocker",
        source_id="fixture-empty-blocker",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    assert blocked.status == "blocked"
    assert blocked.diagnostics[0].code.value == "BRC_E_RANGE"
