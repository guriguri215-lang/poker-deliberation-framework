from __future__ import annotations

import json
from pathlib import Path

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.reporting import render_summary
from poker_deliberation.schemas import CanonicalHand, CaseInput, NumericalExactness, ToolStatus
from poker_deliberation.storage.terminal_models import RunReadStatus
from tests.hand_pot_ledger_support import heads_up_hand, profile, side_pot_hand


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def test_profiled_ledger_uses_bound_case_hand_and_paired_terminal_artifacts(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    hand = CanonicalHand.model_validate(side_pot_hand())
    report = orchestrator.run(
        CaseInput(
            kind="hand",
            hand=hand,
            analysis_scope="retrospective",
            requested_tools=["hand_pot_ledger"],
            metadata={
                "tool_inputs": {
                    "hand_pot_ledger": {
                        "schema_version": "1.0.0",
                        "rule_profile": profile(),
                    }
                }
            },
        ),
        run_id="p3-015a-product-ledger",
    )

    ledger = next(item for item in report.tool_results if item.tool_name == "hand_pot_ledger")
    assert report.run_status == "completed"
    assert ledger.status is ToolStatus.SUCCESS
    assert ledger.numeric_exactness is NumericalExactness.EXACT_UNDER_MODEL
    assert ledger.input["hand"] == hand.model_dump(mode="json")
    assert ledger.output["final_pot_units"] == 120
    assert ledger.output["conservation_verified"] is True
    assert ledger.output["oracle_verified"] is True

    verified = orchestrator.product_store.read_current(report.run_id)
    assert verified.read_status is RunReadStatus.SUCCEEDED
    input_name = f"tool_results/{ledger.result_id}.input.json"
    result_name = f"tool_results/{ledger.result_id}.json"
    assert json.loads(verified.payload_bytes(input_name)) == ledger.input
    assert json.loads(verified.payload_bytes(result_name)) == ledger.model_dump(mode="json")
    assert Orchestrator(config).load_report(report.run_id) == report

    summary = render_summary(report)
    assert "**CALCULATED** `hand_pot_ledger` (`exact-under-model`)" in summary
    assert '"final_pot_units":120' in summary
    assert '"conservation_verified":true' in summary
    assert '"oracle_verified":true' in summary
    assert "gross_contributions_units" not in summary
    assert "remaining_stacks_units" not in summary


def test_product_path_rejects_metadata_hand_substitution(tmp_path: Path) -> None:
    case_hand = CanonicalHand.model_validate(heads_up_hand())
    orchestrator = Orchestrator(_config(tmp_path))
    report = orchestrator.run(
        CaseInput(
            kind="hand",
            hand=case_hand,
            analysis_scope="retrospective",
            requested_tools=["hand_pot_ledger"],
            metadata={
                "tool_inputs": {
                    "hand_pot_ledger": {
                        "schema_version": "1.0.0",
                        "rule_profile": profile(),
                        "hand": side_pot_hand(),
                    }
                }
            },
        ),
        run_id="p3-015a-hand-substitution",
    )

    ledger = next(item for item in report.tool_results if item.tool_name == "hand_pot_ledger")
    assert ledger.status is ToolStatus.FAILED
    assert ledger.input == {}
    assert any("hand_pot_ledger input hand does not match" in item for item in report.data_quality)
