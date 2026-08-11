from __future__ import annotations

import pytest

from poker_deliberation.bounded_river_review_workflow import (
    _project_report_writer_evidence,
)
from poker_deliberation.bounded_river_review_workflow_models import (
    BoundedRiverReviewReportViewV1,
)
from poker_deliberation.codex_bridge.canonical import domain_sha256, without_field
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    RESULT_HASH_DOMAIN,
    SAFE_INFERENCE_NARRATIVE,
    BridgeClaimV1,
    BridgeConclusionCode,
    BridgeEpistemicLabel,
    BridgeEvidenceReferenceV1,
    BridgeRole,
    BridgeRoleOutputV1,
    BridgeRoleResultV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.reporting import (
    render_bounded_river_review_markdown,
    render_bounded_river_review_summary,
    render_markdown,
    render_summary,
)
from poker_deliberation.schemas import FinalReport
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes,
    sha256_bytes,
)

pytestmark = pytest.mark.golden


def _report_writer_result() -> BridgeRoleResultV1:
    output = BridgeRoleOutputV1(
        bridge_run_id="bridge-report-view-golden",
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        role=BridgeRole.REPORT_WRITER,
        assignment_id="assignment-report-view-golden",
        attempt_id="attempt-report-view-golden",
        model="gpt-5.6-terra",
        model_provider="openai",
        runtime_identity="openai-codex-cli/0.144.4",
        conclusions=(
            BridgeClaimV1(
                claim_id="claim-01",
                conclusion_code=BridgeConclusionCode.REPORT_BOUND,
                label=BridgeEpistemicLabel.INFERENCE,
                narrative=SAFE_INFERENCE_NARRATIVE,
                evidence_ids=("adjudication-result",),
            ),
        ),
        evidence_references=(
            BridgeEvidenceReferenceV1(
                evidence_id="adjudication-result",
                evidence_kind="adjudication",
                evidence_sha256="f" * 64,
            ),
        ),
    )
    provisional = BridgeRoleResultV1.model_construct(
        output=output,
        response_bytes_sha256="e" * 64,
        result_sha256="0" * 64,
    )
    return BridgeRoleResultV1(
        output=output,
        response_bytes_sha256=provisional.response_bytes_sha256,
        result_sha256=domain_sha256(
            RESULT_HASH_DOMAIN,
            without_field(provisional, "result_sha256"),
        ),
    )


def test_report_view_renderers_preserve_final_report_suffix_and_drop_writer_narrative() -> None:
    result = _report_writer_result()
    evidence = _project_report_writer_evidence(
        result,
        bridge_run_id=result.output.bridge_run_id,
        auth_mode=result.output.auth_mode,
    )
    report = FinalReport(
        run_id="source-report-view-golden",
        conclusion="authoritative FinalReport conclusion",
    )
    view = BoundedRiverReviewReportViewV1(
        workflow_id="workflow-report-view-golden",
        state="completed",
        bridge_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        bridge_status="succeeded",
        completed_roles=BRIDGE_ROLE_ORDER,
        source_run_id=report.run_id,
        bridge_run_id=result.output.bridge_run_id,
        plan_sha256="1" * 64,
        confirmation_sha256="2" * 64,
        linkage_sha256="3" * 64,
        source_terminal_manifest_sha256="4" * 64,
        source_terminal_inventory_sha256="5" * 64,
        bridge_manifest_sha256="6" * 64,
        bridge_inventory_sha256="7" * 64,
        final_report_artifact_sha256=sha256_bytes(canonical_json_bytes(report)),
        report_writer_additive_evidence=evidence,
        final_report=report,
    )

    summary = render_bounded_river_review_summary(view)
    markdown = render_bounded_river_review_markdown(view)

    assert summary.endswith(render_summary(report))
    assert markdown.endswith(render_markdown(report))
    assert result.output.conclusions[0].narrative not in view.model_dump_json()
    assert result.output.conclusions[0].narrative not in summary
    assert result.output.conclusions[0].narrative not in markdown
    assert view.model_dump(mode="json")["report_writer_additive_evidence"] == [
        {
            "conclusion_code": "report_bound",
            "referenced_evidence_sha256": "f" * 64,
        }
    ]
