from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_grammar import verify_versioned_range_tool_chain
from poker_deliberation.reporting import render_summary
from poker_deliberation.schemas import (
    CaseInput,
    NumericalExactness,
    ToolStatus,
)
from poker_deliberation.storage.terminal_models import RunReadStatus
from tests.range_support import versioned_range_hand


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _case(notation: str = "AKs@0.25,QQ@0.5") -> CaseInput:
    hand, _ = versioned_range_hand(notation)
    return CaseInput(
        kind="hand",
        hand=hand,
        analysis_scope="retrospective",
        requested_tools=["combos"],
    )


def test_product_path_auto_validates_then_runs_canonical_combos(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    case = _case()

    report = Orchestrator(config).run(case, run_id="p3-016a-range-product")

    validation = next(
        result for result in report.tool_results if result.tool_name == "range_validate"
    )
    combos = next(result for result in report.tool_results if result.tool_name == "combos")
    assert report.run_status == "completed"
    assert validation.status is ToolStatus.SUCCESS
    assert validation.numeric_exactness is NumericalExactness.EXACT
    assert validation.output["status"] == "success"
    assert combos.status is ToolStatus.SUCCESS
    assert combos.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
    assert combos.input == {
        "range": validation.output["canonical_notation"],
        "dead_cards": [],
    }
    assert combos.output["combo_count"] == validation.output["combo_count"]
    assert combos.verification is not None and combos.verification.passed
    verify_versioned_range_tool_chain(case, report.tool_results)
    summary = render_summary(report)
    assert "**CALCULATED** `range_validate` (`exact`)" in summary
    assert '"combo_count":8' in summary

    reader = Orchestrator(config)
    verified = reader.product_store.read_current(report.run_id)
    assert verified.read_status is RunReadStatus.SUCCEEDED
    assert reader.load_report(report.run_id) == report


def test_invalid_versioned_range_fails_before_combos(tmp_path: Path) -> None:
    report = Orchestrator(_config(tmp_path)).run(
        _case("QQ+"),
        run_id="p3-016a-range-invalid",
    )

    validation = next(
        result for result in report.tool_results if result.tool_name == "range_validate"
    )
    assert validation.status is ToolStatus.SUCCESS
    assert validation.output["status"] == "failed"
    assert validation.output["diagnostics"][0]["code"] == "RNG_E_SYNTAX"
    assert not any(result.tool_name == "combos" for result in report.tool_results)
    assert any("RNG_E_SYNTAX" in item for item in report.data_quality)


def test_conflicting_manual_combos_input_fails_closed(tmp_path: Path) -> None:
    case = _case()
    case.metadata = {
        "tool_inputs": {
            "combos": {
                "range": "AA",
                "dead_cards": [],
            }
        }
    }

    report = Orchestrator(_config(tmp_path)).run(
        case,
        run_id="p3-016a-range-conflict",
    )

    assert any(result.tool_name == "range_validate" for result in report.tool_results)
    assert not any(result.tool_name == "combos" for result in report.tool_results)
    assert any("conflicting combos input" in item for item in report.data_quality)


def test_semantic_replay_rejects_tampered_validation_output(tmp_path: Path) -> None:
    case = _case()
    report = Orchestrator(_config(tmp_path)).run(
        case,
        run_id="p3-016a-range-replay-tamper",
    )
    results = list(report.tool_results)
    index = next(
        index for index, result in enumerate(results) if result.tool_name == "range_validate"
    )
    tampered_output = dict(results[index].output)
    tampered_output["total_weight_millionths"] += 1
    results[index] = results[index].model_copy(update={"output": tampered_output})

    with pytest.raises(ValueError, match=r"incomplete|deterministic replay"):
        verify_versioned_range_tool_chain(case, results)
