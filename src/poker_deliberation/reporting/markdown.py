"""Faithful Japanese rendering of an already-adjudicated FinalReport."""

from __future__ import annotations

import json

from poker_deliberation.schemas import FinalReport


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _bullets(values: list[str], empty: str = "なし") -> list[str]:
    return [f"- {value}" for value in values] if values else [f"- {empty}"]


def _inline_json(value: object) -> str:
    return f"`{json.dumps(value, ensure_ascii=False, sort_keys=True)}`"


def render_markdown(report: FinalReport) -> str:
    lines = [
        "# ポーカー検討レポート",
        "",
        f"Run ID: `{report.run_id}`",
        f"Run status: `{report.run_status}`",
        "",
        "## 1. 結論",
        "",
        report.conclusion,
        "",
        "## 2. 入力の再構成",
        "",
        "```json",
        _json(report.reconstructed_input),
        "```",
        "",
        "## 3. データ品質と不足情報",
        "",
        *_bullets(report.data_quality),
        "",
        "### セキュリティイベント",
        "",
        *(
            [
                f"- `{event.category}` / `{event.rule_id}`: {event.action}"
                for event in report.security_events
            ]
            or ["- なし"]
        ),
        "",
        "### Agent実行監査",
        "",
        *(
            [
                f"- `{record.agent_role}`: {record.provider} / {record.status.value} / "
                f"context `{record.context_sha256}`"
                for record in report.agent_execution_records
            ]
            or ["- なし"]
        ),
        "",
        "## 4. ユーザー主張の判定",
        "",
    ]
    if report.claim_assessments:
        for claim in report.claim_assessments:
            lines.extend(
                [
                    f"- **{claim.label.value} / 信頼度{claim.confidence.value}** "
                    f"`{claim.claim_id}`: {claim.text}"
                ]
            )
    else:
        lines.append("- 判定対象の構造化主張はありません。")
    lines.extend(["", "## 5. 論点別分析", ""])
    if report.analysis_sections:
        for section in report.analysis_sections:
            title = str(section.get("title", "分析"))
            lines.extend([f"### {title}", "", "```json", _json(section), "```", ""])
    else:
        lines.extend(["- 追加の文章分析はありません。", ""])
    lines.extend(["## 6. 数学的計算", ""])
    if report.tool_results:
        for result in report.tool_results:
            lines.extend(
                [
                    f"### `{result.tool_name}`",
                    "",
                    f"- 状態: `{result.status.value}`",
                    f"- 区分: `{result.exactness.value}`",
                    f"- バージョン: {_inline_json(result.version)}",
                    f"- 実行時間(秒): {_inline_json(result.duration_seconds)}",
                    f"- 仮定: {_inline_json(result.assumptions)}",
                    f"- 警告: {_inline_json(result.warnings)}",
                    f"- seed: {_inline_json(result.seed)}",
                    f"- samples: {_inline_json(result.samples)}",
                    f"- 信頼区間: {_inline_json(result.confidence_interval)}",
                    f"- エラー: {_inline_json(result.error)}",
                    f"- 再現コマンド: {_inline_json(result.reproduce_command)}",
                    "- 出力:",
                    "",
                    "```json",
                    _json(result.output),
                    "```",
                    "",
                ]
            )
    else:
        lines.extend(["- 計算ツールは実行されていません。", ""])
    lines.extend(
        [
            "## 7. 使用したツール",
            "",
            *(
                [
                    f"- `{item.tool_name}` `{item.version}` ({item.exactness.value})"
                    for item in report.tool_results
                ]
                or ["- なし"]
            ),
            "",
            "## 8. GTOベースラインとexploitative adjustment",
            "",
            "ソルバー条件・レンジ・収束が確認されない限り、GTOまたは均衡とは断定しません。",
            "",
            "## 9. 代替戦略",
            "",
            *_bullets(report.alternatives),
            "",
            "## 10. 感度分析",
            "",
        ]
    )
    if report.sensitivity:
        lines.extend(["```json", _json(report.sensitivity), "```"])
    else:
        lines.append("- 感度分析はありません。")
    lines.extend(["", "## 11. 反証と未解決争点", ""])
    if report.disputes:
        for dispute in report.disputes:
            status = "未解決" if dispute.unresolved else "解決"
            lines.append(f"- **{status}** `{dispute.dispute_id}`: {dispute.issue}")
    else:
        lines.append("- 構造化された争点はありません。")
    lines.extend(["", "## 12. 出典", ""])
    if report.evidence:
        for evidence in report.evidence:
            locator = str(evidence.url or evidence.identifier or "不明")
            lines.append(f"- [{evidence.source_tier}] {evidence.source_title}: {locator}")
    else:
        lines.append("- 外部一次資料は使用していません。")
    lines.extend(
        [
            "",
            "## 13. 再現手順",
            "",
            *_bullets(report.reproduction_steps),
            "",
            "## 14. 人間に必要な質問または承認",
            "",
        ]
    )
    if report.approvals:
        for approval in report.approvals:
            lines.extend(
                [
                    f"- `{approval.approval_id}` {approval.status.value}: "
                    f"{approval.requested_action}",
                    f"  - category: `{approval.action_category}`",
                    f"  - reason: {approval.reason}",
                    f"  - expected benefit: {approval.expected_benefit}",
                    f"  - risks: {', '.join(approval.risks) or 'なし'}",
                    f"  - data to be sent: {', '.join(approval.data_to_be_sent) or 'なし'}",
                    f"  - cost/resources: {approval.cost_or_resource_estimate}",
                    f"  - alternatives: {', '.join(approval.alternatives) or 'なし'}",
                    f"  - effect of declining: {approval.effect_of_declining}",
                    f"  - exact command/tool: {approval.exact_command_or_tool_call or 'なし'}",
                ]
            )
    else:
        lines.append("- なし")
    lines.extend(
        [
            "",
            "## 15. 信頼度",
            "",
            f"**{report.confidence.value}**",
            "",
            "### 主要な制限",
            "",
            *_bullets(report.limitations),
            "",
        ]
    )
    return "\n".join(lines)
