from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.cli import build_parser, doctor
from poker_deliberation.context_lifecycle import (
    ATTEMPT_MEMORY_ONLY_RETENTION_POLICY,
    CONTEXT_SCHEMA_VERSION,
    ContextPolicy,
)
from poker_deliberation.local_data_policy import (
    EvidenceVerificationState,
    LifecycleDisposition,
    LifecycleSubject,
    OwnershipProvenance,
    RetentionAnchorKind,
    RunVerificationBasis,
    SubjectEncryptionState,
    SubjectKind,
    SubjectState,
    evaluate_local_data,
)
from poker_deliberation.roadmap import load_roadmap

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def test_context_use_expiry_is_separate_from_storage_retention() -> None:
    context_policy = ContextPolicy(
        expires_at=NOW + timedelta(seconds=30),
        allowed_fields=("kind", "objective"),
    )
    subject = LifecycleSubject(
        subject_kind=SubjectKind.RUN_REPORT,
        subject_id="report-1",
        logical_name="final_report.json",
        state=SubjectState.VERIFIED_TERMINAL,
        retention_anchor_kind=RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
        retention_started_at=NOW - timedelta(days=90),
        run_id="run-report-1",
        revision=1,
        subject_sha256="a" * 64,
        source_sha256="b" * 64,
        run_verification_basis=RunVerificationBasis.FUTURE_VERIFIED_REVISION_V1,
        encryption_state=SubjectEncryptionState.UNKNOWN_OR_UNENCRYPTED,
        ownership_provenance=OwnershipProvenance.FUTURE_VERIFIED_MANIFEST_V1,
        integrity_state=EvidenceVerificationState.VERIFIED,
        lineage_state=EvidenceVerificationState.VERIFIED,
        legal_hold=False,
    )
    result = evaluate_local_data(subject, clock=lambda: NOW)

    assert context_policy.schema_version == CONTEXT_SCHEMA_VERSION == "1.0.0"
    assert context_policy.retention_policy_id == ATTEMPT_MEMORY_ONLY_RETENTION_POLICY
    assert context_policy.expires_at == NOW + timedelta(seconds=30)
    assert result.audit is not None
    assert result.audit.retention_expires_at == NOW
    assert result.audit.proposed_disposition is LifecycleDisposition.DELETE_CANDIDATE


def test_capability_and_doctor_expose_policy_without_cleanup_executor() -> None:
    states = {item.capability_id: item.state for item in CAPABILITIES}
    doctor_states = {item["capability_id"]: item["state"] for item in doctor()["capabilities"]}

    assert states["local_data_lifecycle_policy"] == "implemented"
    assert doctor_states["local_data_lifecycle_policy"] == "implemented"
    assert states["local_data_cleanup_executor"] == "unavailable"
    assert doctor_states["local_data_cleanup_executor"] == "unavailable"


def test_p2_027a_is_active_without_authorizing_p2_027b() -> None:
    document = load_roadmap()

    assert document["milestone_progress"]["P2-027A"]["state"] in {
        "in_progress",
        "completed",
    }
    assert document["milestone_approvals"]["P2-027A"] == ("goal-rm027-p2-027a-2026-07-23")
    assert document["milestone_progress"]["P2-027B"]["state"] == "not_started"
    assert document["milestone_approvals"]["P2-027B"] is None


def test_no_cleanup_command_or_runstore_integration_is_documented() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    policy_doc = (ROOT / "docs" / "local-data-policy.md").read_text(encoding="utf-8")

    assert "local_data_cleanup_executor" in policy_doc
    assert "unavailable" in policy_doc
    assert "cleanup CLI" in policy_doc
    assert "poker-deliberate cleanup" not in readme

    parser = build_parser()
    subparser_action = next(
        action for action in parser._actions if getattr(action, "choices", None)
    )
    assert "cleanup" not in subparser_action.choices
    for relative in (
        "src/poker_deliberation/cli.py",
        "src/poker_deliberation/orchestrator.py",
        "src/poker_deliberation/storage/run_store.py",
    ):
        assert "local_data_policy" not in (ROOT / relative).read_text(encoding="utf-8")
