from __future__ import annotations

from pathlib import Path

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput, NumericalExactness, ToolStatus
from poker_deliberation.tools import default_registry
from tests.range_support import versioned_range_hand


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def test_p3_016a_ordinary_path_still_stops_after_combos(tmp_path: Path) -> None:
    hand, _definition = versioned_range_hand()
    case = CaseInput(
        kind="hand",
        hand=hand,
        analysis_scope="retrospective",
        requested_tools=["combos"],
    )

    report = Orchestrator(_config(tmp_path)).run(
        case,
        run_id="p3-016b-compat-range-v1",
    )

    range_tools = [
        result.tool_name
        for result in report.tool_results
        if result.tool_name in {"range_validate", "combos", "holdem_equity"}
    ]
    assert report.run_status == "completed"
    assert range_tools == ["range_validate", "combos"]
    assert "versioned_range_river_equity" not in report.reconstructed_input["metadata"]


def test_legacy_holdem_equity_contract_and_float_projection_are_unchanged() -> None:
    result = default_registry().execute(
        "holdem_equity",
        {
            "hero_range": "AsKd",
            "villain_range": "6c6d@0.25,QcQd@0.75",
            "board": ["2c", "3d", "4h", "5s", "9c"],
            "dead_cards": [],
            "game_type": "NLHE",
            "mode": "exact",
            "max_exact_evaluations": 2,
        },
        contract_version="2.0.0",
    )

    assert result.status is ToolStatus.SUCCESS
    assert result.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
    assert result.output == {
        "method": "exact_enumeration",
        "exact": True,
        "hero_equity": 0.75,
        "evaluations": 2,
        "unweighted_wins": 1,
        "unweighted_ties": 0,
        "unweighted_losses": 1,
        "range_pair_count": 2,
        "cards_to_come": 0,
    }
