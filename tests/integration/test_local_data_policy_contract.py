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


def test_cleanup_capability_is_promoted_after_completion_gates() -> None:
    states = {item.capability_id: item.state for item in CAPABILITIES}
    doctor_states = {item["capability_id"]: item["state"] for item in doctor()["capabilities"]}

    assert states["local_data_lifecycle_policy"] == "implemented"
    assert doctor_states["local_data_lifecycle_policy"] == "implemented"
    assert states["local_data_cleanup_executor"] == "implemented"
    assert doctor_states["local_data_cleanup_executor"] == "implemented"


def test_p2_027b_public_status_is_completed_without_management_ledger() -> None:
    document = load_roadmap()
    milestones = {item["id"]: item for item in document["implementation_milestones"]}

    assert milestones["P2-027A"]["status"] == "completed"
    assert milestones["P2-027B"]["status"] == "completed"
    assert set(document) == {
        "schema_version",
        "source_policy",
        "status_vocabulary",
        "legal_transitions",
        "milestone_status_vocabulary",
        "milestone_legal_transitions",
        "implementation_milestones",
        "items",
    }


def test_cleanup_remains_additive_without_cli_or_runstore_integration() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    policy_doc = (ROOT / "docs" / "local-data-policy.md").read_text(encoding="utf-8")

    assert "P2-027B" in policy_doc
    assert "additive Python API" in policy_doc
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
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "local_data_policy" not in source
        assert "local_data_cleanup" not in source
