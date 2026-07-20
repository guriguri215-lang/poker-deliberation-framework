from __future__ import annotations

import importlib.metadata
import math

import pytest

from poker_deliberation.normalization import normalize_hand_text
from poker_deliberation.providers import (
    OpenAIAgentsProvider,
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.providers import openai_agents as openai_agents_module
from poker_deliberation.research import EvidenceLedger
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    Claim,
    EpistemicLabel,
    EvidenceRecord,
)
from poker_deliberation.tools import matrix_game as matrix_game_module
from poker_deliberation.tools.matrix_game import solve_zero_sum_matrix
from poker_deliberation.tools.pot_odds import (
    break_even_fold_frequency,
    pot_odds,
    reconstruct_pot,
)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("pot_before_bet", -1.0),
        ("opponent_bet", math.inf),
        ("call_cost", math.nan),
        ("expected_rake", -0.1),
    ],
)
def test_pot_odds_rejects_each_non_finite_or_negative_input(field: str, value: float) -> None:
    payload = {
        "pot_before_bet": 100.0,
        "opponent_bet": 50.0,
        "call_cost": 50.0,
        "expected_rake": 0.0,
    }
    payload[field] = value
    with pytest.raises(ValueError, match=field):
        pot_odds(**payload)


def test_pot_odds_rejects_zero_call_and_exhaustive_rake_boundary() -> None:
    with pytest.raises(ValueError, match="call_cost must be positive"):
        pot_odds(pot_before_bet=100, opponent_bet=50, call_cost=0)
    with pytest.raises(ValueError, match="smaller than the final pot"):
        pot_odds(pot_before_bet=100, opponent_bet=50, call_cost=50, expected_rake=200)


@pytest.mark.parametrize(("risk", "reward"), [(0, 1), (1, 0)])
def test_break_even_fold_requires_both_positive(risk: float, reward: float) -> None:
    with pytest.raises(ValueError, match="risk and reward must be positive"):
        break_even_fold_frequency(risk=risk, reward=reward)


def test_pot_reconstruction_rejects_bad_start_and_indexed_contribution() -> None:
    with pytest.raises(ValueError, match="starting_pot"):
        reconstruct_pot(starting_pot=-1, contributions=[])
    with pytest.raises(ValueError, match=r"contributions\[1\]"):
        reconstruct_pot(starting_pot=0, contributions=[1, math.inf])


def test_pot_reconstruction_success_tracks_each_increment() -> None:
    assert reconstruct_pot(starting_pot=2, contributions=[3, 5]) == {
        "starting_pot": 2,
        "pots_after_each_contribution": [5, 10],
        "final_pot": 10,
    }


@pytest.mark.parametrize(
    ("matrix", "message"),
    [
        ([], "non-empty"),
        ([[1], [1, 2]], "rectangular"),
        ([[math.inf]], "finite"),
    ],
)
def test_matrix_validation_boundaries(matrix: list[list[float]], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        solve_zero_sum_matrix(matrix)


def test_matrix_dimension_support_iteration_and_work_limits() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        solve_zero_sum_matrix([[0.0] * 33])
    with pytest.raises(ValueError, match="max_support_size"):
        solve_zero_sum_matrix([[0.0]], max_support_size=0)
    with pytest.raises(ValueError, match="fallback_iterations"):
        solve_zero_sum_matrix([[0.0]], fallback_iterations=0)
    with pytest.raises(ValueError, match="work estimate"):
        solve_zero_sum_matrix([[0.0] * 32 for _ in range(32)])


def test_matrix_fallback_is_explicit_and_deterministic() -> None:
    payload = {
        "matrix": [[0, -1, 1], [1, 0, -1], [-1, 1, 0]],
        "max_support_size": 1,
        "fallback_iterations": 100,
    }
    first = solve_zero_sum_matrix(**payload)
    second = solve_zero_sum_matrix(**payload)
    assert first == second
    assert first["method"] == "fictitious_play_fallback"
    assert first["exact_algorithm"] is False
    assert first["iterations"] == 100
    assert first["duality_gap"] >= 0
    assert "approximate" in str(first["warning"])


@pytest.mark.parametrize(
    "solutions",
    [
        [[1.0, 0.0], None],
        [[0.0, 0.0], [1.0, 0.0]],
        [[1.0, 0.0], [1.0, 1.0]],
    ],
    ids=("singular-column-system", "non-positive-probability", "inconsistent-values"),
)
def test_matrix_rejects_unverified_support_candidates(
    monkeypatch: pytest.MonkeyPatch,
    solutions: list[list[float] | None],
) -> None:
    queued = iter(solutions)
    monkeypatch.setattr(matrix_game_module, "_solve_linear", lambda *_args: next(queued))
    assert matrix_game_module._support_candidate([[0.0]], (0,), (0,), 1e-9) is None


def test_normalization_reports_every_warning_class_without_guessing() -> None:
    result = normalize_hand_text(
        "\n".join(
            [
                "",
                "# comment",
                "not a key value line",
                "unknown_key: value",
                "table_size: not-an-int",
                "player: only,two",
                "action: flop,hero,bet",
            ]
        )
    )
    assert result.hand is None
    assert any("ignored unrecognized free text" in item for item in result.warnings)
    assert any("ignored unknown key" in item for item in result.warnings)
    assert any("invalid literal" in item for item in result.warnings)
    assert any("player requires" in item for item in result.warnings)
    assert any("action requires" in item for item in result.warnings)
    assert any("canonical validation failed" in item for item in result.warnings)


def test_normalization_parses_optional_action_to_amount() -> None:
    result = normalize_hand_text(
        "\n".join(
            [
                "format: cash",
                "table_size: 2",
                "small_blind: 1",
                "big_blind: 2",
                "player: hero, SB, 100",
                "player: villain, BB, 100",
                "action: preflop, hero, raise, 6, 6",
            ]
        )
    )
    assert result.hand is not None
    assert result.hand.actions[0].to_amount == 6


def _evidence(evidence_id: str, *claim_ids: str) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        source_title="fixture",
        organization_or_author="fixture",
        source_type="input",
        identifier=f"fixture:{evidence_id}",
        accessed_date="2026-07-19",
        supported_claim_ids=list(claim_ids),
        summary="fixture",
        source_tier=6,
    )


def test_evidence_ledger_duplicate_locator_and_claim_branches() -> None:
    ledger = EvidenceLedger([_evidence("e1", "c1")])
    assert [item.evidence_id for item in ledger.all()] == ["e1"]
    assert [item.evidence_id for item in ledger.for_claim("c1")] == ["e1"]
    claims = [
        Claim(claim_id="c1", text="supported", label=EpistemicLabel.USER_CLAIM),
        Claim(claim_id="c2", text="unsupported", label=EpistemicLabel.USER_CLAIM),
    ]
    assert ledger.unsupported_claims(claims) == ["c2"]
    with pytest.raises(ValueError, match="duplicate"):
        ledger.add(_evidence("e1"))
    invalid = EvidenceRecord.model_construct(
        evidence_id="invalid",
        source_title="fixture",
        organization_or_author="fixture",
        source_type="input",
        url=None,
        identifier=None,
        accessed_date="2026-07-19",
        supported_claim_ids=[],
        summary="fixture",
        source_tier=6,
        limitations=[],
    )
    with pytest.raises(ValueError, match="URL or identifier"):
        ledger.add(invalid)


def test_openai_provider_missing_distribution_and_cancelled_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(openai_agents_module.importlib.metadata, "version", missing)
    assert OpenAIAgentsProvider._version() is None

    control = ProviderControl(timeout_seconds=1)
    control.cancel()
    with pytest.raises(TimeoutError, match="deadline/cancellation"):
        OpenAIAgentsProvider().analyze(
            AgentContext(kind="claim", objective="fixture"),
            AgentAssignment(agent_role="skeptic", task="fixture"),
            control,
        )


@pytest.mark.parametrize(
    ("status", "available"),
    [
        (ProviderStatus.AVAILABLE, False),
        (ProviderStatus.UNAVAILABLE, True),
        (ProviderStatus.DISABLED, True),
    ],
)
def test_provider_availability_rejects_status_boolean_contradictions(
    status: ProviderStatus,
    available: bool,
) -> None:
    with pytest.raises(ValueError, match="available must be true exactly"):
        ProviderAvailability(
            status=status,
            available=available,
            provider="fixture",
            reason="known contradiction fixture",
        )
