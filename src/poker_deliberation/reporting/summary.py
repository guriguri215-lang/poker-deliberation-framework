"""Concise Japanese projection of an already-adjudicated ``FinalReport``."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from poker_deliberation.schemas import (
    ConfidenceGrade,
    EpistemicLabel,
    FinalReport,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)

_IMPORTANT_OUTPUT_FIELDS: dict[str, tuple[str, ...]] = {
    "pot_odds": (
        "required_equity",
        "required_equity_percent",
        "final_pot_after_rake",
        "pot_odds_against",
    ),
    "break_even_fold": ("break_even_fold_frequency", "break_even_fold_percent"),
    "mdf": ("minimum_defense_frequency", "minimum_defense_percent"),
    "spr": ("spr",),
    "effective_stack": ("effective_stack",),
    "rake_amount": ("rake_amount",),
    "raked_call_ev": ("ev", "rake_amount", "final_pot_after_rake"),
    "bluff_ev": ("ev", "called_branch_ev"),
    "polar_river_bluff_fraction": ("bluff_fraction", "bluff_percent"),
    "bayes_update": ("posterior", "evidence_probability"),
    "pot_reconstruction": ("final_pot",),
    "combos": ("count", "combo_count", "total_combo_weight"),
    "holdem_equity": (
        "hero_equity",
        "method",
        "evaluations",
        "samples",
        "confidence_interval_95",
    ),
    "ev_tree": ("expected_value",),
    "icm": (
        "equities",
        "equity_sum",
        "payable_prize_sum",
        "conservation_verified",
    ),
    "matrix_game": ("value", "value_estimate", "duality_gap", "method", "exact_algorithm"),
    "fixed_strategy_best_response": (
        "value",
        "best_responder",
        "opponent_strategy_fixed",
        "equilibrium_claim",
    ),
    "hand_validator": ("valid", "final_pot", "errors", "warnings"),
    "sensitivity": ("lower_bound", "upper_bound", "scenario_count", "warning"),
}


def _deduplicate(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _project_value(value: Any) -> Any:
    if isinstance(value, list) and len(value) > 5:
        return {"preview": value[:5], "total_items": len(value)}
    if isinstance(value, dict) and len(value) > 5:
        keys = sorted(map(str, value))[:5]
        return {
            "preview": {key: value[key] for key in keys},
            "total_items": len(value),
        }
    return value


def _inline(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _calculation_label(result: ToolResult) -> str | None:
    if result.status is not ToolStatus.SUCCESS:
        return None
    if result.numeric_exactness is NumericalExactness.FLOATING_VERIFIED:
        if result.verification is None or not result.verification.passed:
            return None
        return EpistemicLabel.CALCULATED.value
    if result.numeric_exactness in {
        NumericalExactness.EXACT,
        NumericalExactness.EXACT_UNDER_MODEL,
    }:
        return EpistemicLabel.CALCULATED.value
    if result.numeric_exactness is NumericalExactness.APPROXIMATE:
        if not result.stopping_condition or (
            result.confidence_interval is None and result.error_metadata is None
        ):
            return None
        return EpistemicLabel.ESTIMATE.value
    return None


def _important_output(result: ToolResult) -> dict[str, Any]:
    fields = _IMPORTANT_OUTPUT_FIELDS.get(result.tool_name, ())
    return {
        field: _project_value(result.output[field])
        for field in fields
        if field in result.output and result.output[field] is not None
    }


def _verified_corrections(report: FinalReport) -> list[str]:
    return [
        claim.text
        for claim in report.claim_assessments
        if claim.claim_id.startswith("adjudication-")
        and claim.label is EpistemicLabel.CALCULATED
        and claim.confidence is ConfidenceGrade.A
        and "訂正が必要" in claim.text
    ]


def _major_assumptions(report: FinalReport) -> list[str]:
    assumptions: list[str] = []
    raw = report.reconstructed_input.get("assumptions")
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                text = item.get("text")
                reason = item.get("reason")
                if isinstance(text, str) and text:
                    assumptions.append(
                        f"{text} (理由: {reason})" if isinstance(reason, str) and reason else text
                    )
            elif isinstance(item, str) and item:
                assumptions.append(item)
    for result in report.tool_results:
        if _calculation_label(result) is not None:
            assumptions.extend(result.assumptions)
            if result.model_qualifier:
                assumptions.append(f"{result.tool_name}: {result.model_qualifier}")
    return _deduplicate(assumptions)[:8]


def _limitations(report: FinalReport) -> list[str]:
    limitations = [*report.limitations]
    for result in report.tool_results:
        if result.status in {ToolStatus.FAILED, ToolStatus.UNAVAILABLE}:
            detail = result.error or "結果は利用できません。"
            limitations.append(f"{result.tool_name}は`{result.status.value}`です: {detail}")
    if report.analysis_sections:
        limitations.append(
            f"未検証のagent文章{len(report.analysis_sections)}節は結論へ昇格せず、"
            "完全JSONにのみ保持しています。"
        )
    return _deduplicate(limitations)


def render_summary(report: FinalReport) -> str:
    """Render a compact view without reproducing full input or execution audit details."""

    corrections = _verified_corrections(report)
    calculations = [
        (result, label, _important_output(result))
        for result in report.tool_results
        if (label := _calculation_label(result)) is not None
    ]
    assumptions = _major_assumptions(report)
    limitations = _limitations(report)

    lines = [
        "# ポーカー検討サマリー",
        "",
        f"- Run ID: `{report.run_id}`",
        f"- Status: `{report.run_status}`",
        f"- Confidence: `{report.confidence.value}`",
        "",
        "## 結論",
        "",
        report.conclusion,
        "",
        "## 訂正",
        "",
        *([f"- {item}" for item in corrections] or ["- 訂正対象はありません。"]),
        "",
        "## 重要な計算結果",
        "",
    ]
    if calculations:
        for result, label, output in calculations:
            verification = (
                " / verification passed"
                if result.verification is not None and result.verification.passed
                else ""
            )
            lines.append(
                f"- **{label}** `{result.tool_name}` "
                f"(`{result.numeric_exactness.value}`{verification}): {_inline(output)}"
            )
    else:
        lines.append("- 利用可能な検証済み計算結果はありません。")
    lines.extend(
        [
            "",
            "## 主要な仮定",
            "",
            *([f"- {item}" for item in assumptions] or ["- 明示された主要仮定はありません。"]),
            "",
            "## 制限",
            "",
            *([f"- {item}" for item in limitations] or ["- 追加の制限はありません。"]),
            "",
            "## 詳細と再現",
            "",
            "- 完全入力、claim判定、verification observations、agent実行監査は"
            "`final_report.json`を参照してください。",
            "- 完全な利用者向けレポートは`final_report.md`、個別計算の入力・出力は"
            "`tool_results/*.input.json`と`tool_results/*.json`を参照してください。",
            f"- 記録済み再現手順: `{len(report.reproduction_steps)}`件。",
            "",
        ]
    )
    return "\n".join(lines)
