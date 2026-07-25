from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from poker_deliberation.local_data_policy import (
    LifecycleDisposition,
    SubjectState,
)
from poker_deliberation.schemas import FinalReport
from poker_deliberation.storage.lifecycle_hooks import (
    build_terminal_lifecycle_audit,
    evaluate_reader_candidate,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from poker_deliberation.storage.terminal_canonical import inventory_entry

NOW = datetime(2026, 7, 25, 12, tzinfo=UTC)


def test_terminal_hook_uses_published_at_and_performs_no_filesystem_action(
    tmp_path: Path,
) -> None:
    before = tuple(tmp_path.iterdir())
    state_bytes = canonical_json_bytes(
        {
            "state": "COMPLETED",
            "events": [],
            "deliberation_rounds": 0,
            "tool_retries": {},
            "elapsed_seconds": 0.0,
        }
    )
    report_bytes = canonical_json_bytes(FinalReport(run_id="run-lifecycle", conclusion="verified"))
    inventory = (
        inventory_entry(
            logical_name="state.json",
            data=state_bytes,
            media_type="application/json",
            artifact_schema_version="poker-workflow-state-artifact-v1",
            serialization="poker-run-storage-json-v1",
        ),
        inventory_entry(
            logical_name="final_report.json",
            data=report_bytes,
            media_type="application/json",
            artifact_schema_version="poker-final-report-artifact-v2",
            serialization="poker-run-storage-json-v1",
        ),
    )

    bundle = build_terminal_lifecycle_audit(
        run_id="run-lifecycle",
        revision=1,
        published_at=NOW,
        inventory=inventory,
    )

    assert len(bundle.audits) == 2
    assert all(item.retention_started_at == NOW for item in bundle.audits)
    assert all(item.subject_state is SubjectState.VERIFIED_TERMINAL for item in bundle.audits)
    assert all(item.proposed_disposition is LifecycleDisposition.RETAIN for item in bundle.audits)
    assert len(bundle.sha256) == 64
    assert tuple(tmp_path.iterdir()) == before


def test_corrupt_reader_subject_is_only_a_quarantine_candidate() -> None:
    result = evaluate_reader_candidate(
        run_id="run-corrupt",
        logical_name="final_report.json",
        subject_sha256="1" * 64,
        source_sha256="2" * 64,
        state=SubjectState.CORRUPT,
        evaluated_at=NOW,
    )

    assert result.status == "evaluated"
    assert result.audit is not None
    assert result.audit.proposed_disposition is LifecycleDisposition.QUARANTINE_CANDIDATE
    assert result.audit.action_digest is None
