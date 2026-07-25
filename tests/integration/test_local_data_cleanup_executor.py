from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.approval_canonical import approval_actor_sha256
from poker_deliberation.approval_models import (
    ApprovalActor,
    ApprovalAuthoritySnapshotV2,
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
)
from poker_deliberation.approvals import read_approval_state_v2
from poker_deliberation.config import AppConfig
from poker_deliberation.local_data_cleanup import (
    LocalDataCleanupExecutor,
    cleanup_approval_action_plan,
)
from poker_deliberation.local_data_cleanup_canonical import (
    canonical_cleanup_bytes,
    parse_cleanup_model,
    run_id_sha256,
)
from poker_deliberation.local_data_cleanup_models import (
    CleanupManifestV1,
    CleanupPlanV1,
    LegalHoldSnapshotV1,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.local_data_cleanup_store import CleanupStorageError

PUBLISHED = datetime(2025, 1, 1, 12, tzinfo=UTC)
EVALUATED = PUBLISHED + timedelta(days=400)
DELETE_AT = EVALUATED + timedelta(days=31)


class NoLegalHold:
    def resolve(
        self,
        run_id_sha256: str,
        *,
        evaluated_at: datetime,
    ) -> LegalHoldSnapshotV1:
        return LegalHoldSnapshotV1(
            provider_id="test-legal-hold",
            provider_version="1.0.0",
            run_id_sha256=run_id_sha256,
            legal_hold=False,
            snapshot_reference_sha256=hashlib.sha256(
                f"{run_id_sha256}:{evaluated_at.isoformat()}:false".encode()
            ).hexdigest(),
            resolved_at=evaluated_at,
        )


def _orchestrator(tmp_path: Path) -> Orchestrator:
    config = AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )
    return Orchestrator(
        config,
        terminal_clock=lambda: PUBLISHED,
        context_clock=lambda: PUBLISHED,
    )


def _case() -> CaseInput:
    return CaseInput(
        kind="calculation",
        raw_text="disposable cleanup integration fixture",
        analysis_scope="retrospective",
    )


def _snapshot(root: Path) -> tuple[tuple[str, str, str], ...]:
    values: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            values.append((relative, "directory", ""))
        else:
            values.append((relative, "file", hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(values)


class CleanupAuthority:
    def __init__(self, evaluated_at: datetime = EVALUATED) -> None:
        self.actor = ApprovalActor(
            actor_id="cleanup-reviewer",
            actor_type="human",
            authority_source="test-cleanup-authority",
            authority_scopes=("approve:destructive_change", "reject:any"),
            verification_status="verified",
            verification_reference_sha256="1" * 64,
            session_reference_sha256="2" * 64,
            credential_reference_sha256="3" * 64,
            verified_at=evaluated_at - timedelta(minutes=1),
            authority_expires_at=evaluated_at + timedelta(days=1),
            revocation_status="not_revoked",
        )

    def resolve_actor(
        self,
        actor_id: str,
        *,
        decision_at: datetime,
    ) -> ApprovalAuthoritySnapshotV2:
        assert actor_id == self.actor.actor_id
        return ApprovalAuthoritySnapshotV2(
            provider_id="test-cleanup-authority",
            provider_version="1.0.0",
            resolved_at=decision_at,
            actor=self.actor,
            actor_sha256=approval_actor_sha256(self.actor),
        )


def _approve_cleanup(
    orchestrator: Orchestrator,
    plan: CleanupPlanV1,
    *,
    approval_run_id: str,
    decision_at: datetime = EVALUATED,
) -> tuple[str, CleanupAuthority]:
    action_plan = cleanup_approval_action_plan(plan)
    report = orchestrator.run(
        CaseInput(
            kind="strategy",
            raw_text="authorize one disposable cleanup fixture",
            analysis_scope="retrospective",
            metadata={
                "approval_requests": [
                    {
                        "schema_version": "2.0.0",
                        "stable_proposal_id": "cleanup-proposal",
                        "action_plan": action_plan.model_dump(mode="json"),
                        "display": {
                            "requested_action": action_plan.operation,
                            "reason": "Authorize the exact cleanup plan.",
                            "expected_benefit": "Exercise the cleanup authorization boundary.",
                            "risks": ["Disposable local fixture data will move."],
                            "data_to_be_sent": [],
                            "cost_or_resource_estimate": "Bounded local resources only.",
                            "alternatives": ["Decline and retain the fixture."],
                            "effect_of_declining": "No cleanup action is performed.",
                            "exact_command_or_tool_call": None,
                        },
                    }
                ]
            },
        ),
        run_id=approval_run_id,
    )
    current = orchestrator.product_store.read_current(report.run_id)
    state = read_approval_state_v2(
        current.payload_bytes("approval_ledger_v2.json"),
        current.payload_bytes("approval_decisions_v2.jsonl"),
        current.payload_bytes("approval_audit_v2.jsonl"),
    )
    request = state.ledger.requests[0]
    provider = CleanupAuthority(decision_at)
    batch = ApprovalDecisionBatch(
        run_id=report.run_id,
        expected_run_revision=current.revision,
        expected_ledger_revision=state.ledger.ledger_revision,
        actor=provider.actor,
        decision_id="cleanup-decision",
        idempotency_key="cleanup-decision-key",
        items=(
            ApprovalDecisionItemV2(
                request_id=request.request_id,
                expected_request_revision=request.request_revision,
                action_digest_sha256=request.action_digest_sha256,
                decision="approved",
            ),
        ),
        reason="Approve the exact disposable cleanup plan.",
        decision_at=decision_at,
    )
    Orchestrator(
        orchestrator.config,
        terminal_clock=lambda: decision_at,
        decision_authority_provider=provider,
    ).decide_approvals(batch)
    return request.request_id, provider


def test_dry_run_is_bounded_and_filesystem_mutation_zero(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-run-1")
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    initialized = executor.initialize_cleanup_root(
        existing_run_id="cleanup-run-1",
        root_id="cleanup-root-" + "1" * 32,
        initialized_at=PUBLISHED,
    )
    assert initialized.outcome_kind == "initialized"
    before = _snapshot(tmp_path)

    result = executor.dry_run_quarantine(
        "cleanup-run-1",
        execution_id="cleanup-execution-1",
        idempotency_key="cleanup-key-1",
        expires_at=EVALUATED + timedelta(hours=1),
    )

    assert result.outcome_kind == "eligible", result
    assert result.plan is not None
    assert result.plan.actions[0].source_relative_path == "runs/cleanup-run-1"
    assert result.plan.actions[0].destination_relative_path == "quarantine/cleanup-run-1"
    assert result.filesystem_mutation is False
    assert result.domain_mutation is False
    assert _snapshot(tmp_path) == before


def test_quarantine_uses_single_rename_and_publishes_receipt_and_tombstone(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-run-2")
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id="cleanup-run-2",
        root_id="cleanup-root-" + "2" * 32,
        initialized_at=PUBLISHED,
    )
    dry_run = executor.dry_run_quarantine(
        "cleanup-run-2",
        execution_id="cleanup-execution-2",
        idempotency_key="cleanup-key-2",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="cleanup-approval-run-2",
    )

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-approval-run-2",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    replay = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-approval-run-2",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    run_hash = run_id_sha256("cleanup-run-2")
    assert result.outcome_kind == "committed"
    assert result.cleanup_revision == 1
    assert result.receipt is not None
    assert result.tombstone is not None
    assert result.receipt.result_state == "quarantined"
    assert result.tombstone.state == "quarantined"
    assert replay == result
    assert not (tmp_path / "product" / "runs" / "cleanup-run-2").exists()
    assert (cleanup_root / "quarantine" / "cleanup-run-2").is_dir()
    current = executor.store.read_current(run_hash)
    assert current is not None
    assert current[0].state == "quarantined"
    assert not hasattr(executor.store, "delete_product_run")
    assert not hasattr(executor.store, "publish_quarantine")


def test_delete_requires_second_dry_run_and_approval_then_stages_and_unlinks(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-run-delete")
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id="cleanup-run-delete",
        root_id="cleanup-root-" + "4" * 32,
        initialized_at=PUBLISHED,
    )
    quarantine_dry_run = executor.dry_run_quarantine(
        "cleanup-run-delete",
        execution_id="cleanup-quarantine-delete",
        idempotency_key="cleanup-quarantine-key-delete",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert quarantine_dry_run.plan is not None
    quarantine_request, quarantine_provider = _approve_cleanup(
        orchestrator,
        quarantine_dry_run.plan,
        approval_run_id="cleanup-approval-quarantine-delete",
    )
    quarantined = executor.execute_quarantine(
        quarantine_dry_run.plan,
        approval_run_id="cleanup-approval-quarantine-delete",
        approval_request_id=quarantine_request,
        authority_provider=quarantine_provider,
    )
    assert quarantined.outcome_kind == "committed"

    executor.clock = lambda: DELETE_AT
    delete_dry_run = executor.dry_run_delete(
        "cleanup-run-delete",
        execution_id="cleanup-delete-execution",
        idempotency_key="cleanup-delete-key",
        expires_at=DELETE_AT + timedelta(hours=1),
    )
    assert delete_dry_run.plan is not None
    delete_request, delete_provider = _approve_cleanup(
        orchestrator,
        delete_dry_run.plan,
        approval_run_id="cleanup-approval-delete",
        decision_at=DELETE_AT,
    )
    deleted = executor.execute_delete(
        delete_dry_run.plan,
        approval_run_id="cleanup-approval-delete",
        approval_request_id=delete_request,
        authority_provider=delete_provider,
    )
    replay = executor.execute_delete(
        delete_dry_run.plan,
        approval_run_id="cleanup-approval-delete",
        approval_request_id=delete_request,
        authority_provider=delete_provider,
    )

    run_hash = run_id_sha256("cleanup-run-delete")
    assert deleted.outcome_kind == "committed"
    assert deleted.cleanup_revision == 3
    assert deleted.receipt is not None
    assert deleted.receipt.result_state == "deleted"
    assert replay == deleted
    assert not (cleanup_root / "quarantine" / "cleanup-run-delete").exists()
    assert not any((cleanup_root / "deleting").iterdir())
    current = executor.store.read_current(run_hash)
    assert current is not None
    assert current[0].state == "deleted"
    assert current[3].state == "deleted"
    revisions = cleanup_root / "runs" / run_hash / "revisions"
    revision_one = next(path for path in revisions.iterdir() if path.name.startswith("r1-"))
    manifest_path = revision_one / "manifest.json"
    manifest = parse_cleanup_model(manifest_path.read_bytes(), CleanupManifestV1)
    manifest_path.write_bytes(
        canonical_cleanup_bytes(
            manifest.model_copy(
                update={"created_at": manifest.created_at + timedelta(microseconds=1)}
            )
        )
    )
    with pytest.raises(CleanupStorageError):
        executor.store.read_current(run_hash)
