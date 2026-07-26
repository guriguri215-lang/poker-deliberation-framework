"""P2-029A ordinary offline product-path dogfood contract."""

from __future__ import annotations

import json
from pathlib import Path

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.reporting import render_summary
from poker_deliberation.schemas import CanonicalHand, CaseInput, FinalReport, ToolStatus
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from poker_deliberation.storage.terminal_models import RunReadStatus

ROOT = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _case(name: str) -> CaseInput:
    payload = json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))
    return CaseInput.model_validate(payload)


def _verify_terminal_round_trip(
    config: AppConfig,
    report_run_id: str,
) -> tuple[Orchestrator, FinalReport]:
    reader = Orchestrator(config)
    report = reader.load_report(report_run_id)
    verified = reader.product_store.read_current(report_run_id)

    assert report.run_status == "completed"
    assert verified.read_status is RunReadStatus.SUCCEEDED
    assert verified.reachable_revisions == (1,)
    assert verified.payload_bytes("final_report.json") == canonical_json_bytes(report)
    assert reader.load_report(report_run_id) == report
    assert reader.report_path(report_run_id, "json").read_bytes() == (
        verified.payload_bytes("final_report.json")
    )
    return reader, report


def test_offline_product_path_dogfood_covers_correction_validation_and_limitation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    correction = Orchestrator(config).run(
        _case("wrong_pot_odds_case.json"),
        run_id="p2-029a-pot-odds",
    )
    _, loaded_correction = _verify_terminal_round_trip(config, correction.run_id)
    assert loaded_correction == correction
    assert correction.tool_results[0].output["required_equity"] == 0.25
    assert any("訂正が必要" in claim.text for claim in correction.claim_assessments)
    correction_summary = render_summary(correction)
    assert "CALCULATED=0.25" in correction_summary
    assert '"required_equity":0.25' in correction_summary

    hand_payload = json.loads((ROOT / "examples" / "valid_hand.json").read_text(encoding="utf-8"))
    hand = Orchestrator(config).run(
        CaseInput(
            kind="hand",
            hand=CanonicalHand.model_validate(hand_payload),
            analysis_scope="retrospective",
        ),
        run_id="p2-029a-hand-validation",
    )
    _, loaded_hand = _verify_terminal_round_trip(config, hand.run_id)
    assert loaded_hand == hand
    hand_result = next(
        result for result in hand.tool_results if result.tool_name == "hand_validator"
    )
    assert hand_result.status is ToolStatus.SUCCESS
    assert hand_result.output["valid"] is True
    hand_summary = render_summary(hand)
    assert "**CALCULATED** `hand_validator`" in hand_summary
    assert '"valid":true' in hand_summary

    limitation = Orchestrator(config).run(
        _case("full_nlhe_limitations_case.json"),
        run_id="p2-029a-strategy-limitation",
    )
    _, loaded_limitation = _verify_terminal_round_trip(config, limitation.run_id)
    assert loaded_limitation == limitation
    solver = next(
        result for result in limitation.tool_results if result.tool_name == "solver_status"
    )
    assert solver.status is ToolStatus.UNAVAILABLE
    limitation_summary = render_summary(limitation)
    assert "solver_statusは`unavailable`" in limitation_summary
    assert "no equilibrium or strategy result was generated" in limitation_summary
    assert "GTOまたは均衡を主張していません" in limitation_summary
    assert "正確なGTOレンジ" not in limitation_summary

    for report in (correction, hand, limitation):
        assert report.reproduction_steps
        for step in report.reproduction_steps:
            argv = json.loads(step.removeprefix("argv-json: "))
            assert argv[:2] == ["poker-deliberate", "calculate"]
            assert argv[2] in {
                "pot_odds",
                "hand_validator",
                "solver_status",
                "sensitivity",
            }
            assert argv[3] == "--analysis-scope"
            assert argv[4] == "retrospective"
            assert argv[5] == "--input"
            assert Path(argv[-1]).is_file()
