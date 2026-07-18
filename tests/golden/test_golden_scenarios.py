import json
from pathlib import Path

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.tools import default_registry

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.golden


def _load(name: str) -> dict[str, object]:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def _orchestrator(tmp_path: Path) -> Orchestrator:
    return Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))


def test_wrong_pot_odds_claim_is_corrected(tmp_path: Path) -> None:
    report = _orchestrator(tmp_path).run(
        CaseInput.model_validate(_load("wrong_pot_odds_case.json"))
    )
    assert "訂正が必要" in report.conclusion
    correction = [claim for claim in report.claim_assessments if "CALCULATED=0.25" in claim.text]
    assert correction
    assert correction[0].confidence.value == "A"


def test_incomplete_hand_collects_missing_data_without_invention(tmp_path: Path) -> None:
    report = _orchestrator(tmp_path).run(
        CaseInput.model_validate(_load("incomplete_hand_case.json"))
    )
    assert any("CanonicalHand" in item for item in report.data_quality)
    assert report.tool_results == []
    assert report.confidence.value == "C"


def test_icm_tournament_example() -> None:
    result = default_registry().execute("icm", _load("icm_input.json"))
    assert result.status.value == "success"
    assert abs(sum(result.output["equities"]) - 100) < 1e-9


def test_small_game_fixed_strategy_best_response() -> None:
    result = default_registry().execute(
        "fixed_strategy_best_response", _load("best_response_input.json")
    )
    assert result.status.value == "success"
    assert result.output["value"] == 0.5
    assert result.output["equilibrium_claim"] is False


def test_full_nlhe_does_not_fake_equilibrium(tmp_path: Path) -> None:
    report = _orchestrator(tmp_path).run(
        CaseInput.model_validate(_load("full_nlhe_limitations_case.json"))
    )
    solver = next(item for item in report.tool_results if item.tool_name == "solver_status")
    assert solver.status.value == "unavailable"
    assert solver.output.get("result", {}) == {}
    assert any("unavailable" in item for item in report.data_quality)


def test_mismatched_public_range_stays_an_assumption(tmp_path: Path) -> None:
    case = CaseInput.model_validate(_load("public_range_mismatch_case.json"))
    report = _orchestrator(tmp_path).run(case)
    assert case.assumptions[0].text.startswith("公開chartは対象条件と一致しない")
    assert not any(claim.label.value == "FACT" for claim in report.claim_assessments)
