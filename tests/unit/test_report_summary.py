from __future__ import annotations

from poker_deliberation.reporting import render_summary
from poker_deliberation.schemas import (
    Claim,
    ConfidenceGrade,
    EpistemicLabel,
    FinalReport,
)
from poker_deliberation.tools import default_registry


def test_summary_leads_with_adjudicated_correction_and_verified_calculation() -> None:
    result = default_registry().execute(
        "pot_odds",
        {
            "pot_before_bet": 100,
            "opponent_bet": 50,
            "call_cost": 50,
            "expected_rake": 0,
        },
    )
    report = FinalReport(
        run_id="summary-correction",
        conclusion="再現可能なローカル計算に基づく訂正が必要です。",
        reconstructed_input={
            "raw_text": "完全入力には表示してはいけない詳細があります。",
            "assumptions": [
                {
                    "text": "追加レーキは0とする",
                    "reason": "入力で明示された",
                }
            ],
        },
        claim_assessments=[
            Claim(
                claim_id="claim-user",
                text="必要エクイティは33.333%",
                label=EpistemicLabel.USER_CLAIM,
            ),
            Claim(
                claim_id="adjudication-claim-user",
                text=("USER_CLAIM=0.3333333333 は CALCULATED=0.25 と一致せず、訂正が必要です。"),
                label=EpistemicLabel.CALCULATED,
                confidence=ConfidenceGrade.A,
            ),
        ],
        tool_results=[result],
        analysis_sections=[
            {
                "title": "local-provider",
                "epistemic_status": "UNKNOWN",
                "unverified_conclusions": ["未検証文章を結論に入れてはいけない"],
                "uncertainties": ["内容のない専門家節"],
            }
        ],
        limitations=["入力で指定されていない将来アクションは評価していません。"],
        reproduction_steps=["argv-json: fixture"],
    )

    summary = render_summary(report)

    assert summary.index("## 結論") < summary.index("## 訂正")
    assert summary.index("## 訂正") < summary.index("## 重要な計算結果")
    assert "CALCULATED=0.25" in summary
    assert '"required_equity":0.25' in summary
    assert "verification passed" in summary
    assert "追加レーキは0とする" in summary
    assert "未検証のagent文章1節は結論へ昇格せず" in summary
    assert "未検証文章を結論に入れてはいけない" not in summary
    assert "完全入力には表示してはいけない詳細" not in summary
    assert "verification observations" in summary
    assert "記録済み再現手順: `1`件" in summary
    assert render_summary(report) == summary


def test_summary_keeps_unavailable_solver_as_limitation_not_calculation() -> None:
    solver = default_registry().execute("solver_status", {})
    report = FinalReport(
        run_id="summary-unavailable",
        conclusion="外部ソルバー結果はありません。",
        tool_results=[solver],
        limitations=["ソルバー実行と収束確認を行っていません。"],
    )

    summary = render_summary(report)

    assert "利用可能な検証済み計算結果はありません" in summary
    assert "solver_statusは`unavailable`" in summary
    assert "no equilibrium or strategy result was generated" in summary
    assert "**CALCULATED** `solver_status`" not in summary
