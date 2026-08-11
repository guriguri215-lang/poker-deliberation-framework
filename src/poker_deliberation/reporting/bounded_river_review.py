"""Structural renderers for a verified bounded river-review report view."""

from __future__ import annotations

from poker_deliberation.bounded_river_review_workflow_models import (
    BoundedRiverReviewReportViewV1,
)
from poker_deliberation.reporting.markdown import render_markdown
from poker_deliberation.reporting.summary import render_summary


def _structural_header(view: BoundedRiverReviewReportViewV1) -> str:
    completed_roles = ", ".join(role.value for role in view.completed_roles) or "none"
    lines = [
        "# 境界付きリバー・レビュー ワークフロー",
        "",
        f"- Workflow ID: `{view.workflow_id}`",
        f"- Workflow state: `{view.state}`",
        f"- Bridge mode: `{view.bridge_mode.value}`",
        f"- Bridge status: `{view.bridge_status}`",
        f"- Completed roles: `{completed_roles}`",
        f"- Source run ID: `{view.source_run_id}`",
        f"- Bridge run ID: `{view.bridge_run_id}`",
        f"- Plan SHA-256: `{view.plan_sha256}`",
        f"- Confirmation SHA-256: `{view.confirmation_sha256}`",
        f"- Linkage SHA-256: `{view.linkage_sha256}`",
        (f"- Source terminal manifest SHA-256: `{view.source_terminal_manifest_sha256}`"),
        (f"- Source terminal inventory SHA-256: `{view.source_terminal_inventory_sha256}`"),
        f"- Bridge manifest SHA-256: `{view.bridge_manifest_sha256}`",
        f"- Bridge inventory SHA-256: `{view.bridge_inventory_sha256}`",
        f"- FinalReport artifact SHA-256: `{view.final_report_artifact_sha256}`",
    ]
    lines.extend(
        "- Report-writer additive evidence: "
        f"`{item.conclusion_code.value}` / `{item.referenced_evidence_sha256}`"
        for item in view.report_writer_additive_evidence
    )
    return "\n".join(lines)


def render_bounded_river_review_summary(view: BoundedRiverReviewReportViewV1) -> str:
    """Prepend verified workflow structure to the existing summary verbatim."""

    return f"{_structural_header(view)}\n\n{render_summary(view.final_report)}"


def render_bounded_river_review_markdown(view: BoundedRiverReviewReportViewV1) -> str:
    """Prepend verified workflow structure to the existing markdown verbatim."""

    return f"{_structural_header(view)}\n\n{render_markdown(view.final_report)}"


__all__ = [
    "render_bounded_river_review_markdown",
    "render_bounded_river_review_summary",
]
