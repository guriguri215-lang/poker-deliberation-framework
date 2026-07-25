from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from poker_deliberation.config import AppConfig
from poker_deliberation.local_data_cleanup import LocalDataCleanupExecutor
from poker_deliberation.local_data_cleanup_canonical import run_id_sha256
from poker_deliberation.local_data_cleanup_models import (
    CleanupApprovalBindingV1,
    LegalHoldSnapshotV1,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput

PUBLISHED = datetime(2025, 1, 1, 12, tzinfo=UTC)
EVALUATED = PUBLISHED + timedelta(days=400)


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


def _approval(run_id: str) -> CleanupApprovalBindingV1:
    return CleanupApprovalBindingV1(
        approval_run_id_sha256=run_id_sha256("approval-run"),
        approval_run_revision=2,
        approval_pointer_sha256="1" * 64,
        approval_ledger_sha256="2" * 64,
        request_id="cleanup-request-1",
        request_revision=1,
        action_digest_sha256="3" * 64,
        decision_id="cleanup-decision-1",
        decision_record_sha256="4" * 64,
        decision_outcome_sha256="5" * 64,
        actor_sha256="6" * 64,
        authority_snapshot_sha256="7" * 64,
        authority_provider_id="test-authority",
        authority_provider_version="1.0.0",
    )


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

    assert result.outcome_kind == "eligible"
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

    result = executor.store.publish_quarantine(
        dry_run.plan,
        _approval("cleanup-run-2"),
        transaction_id="cleanup-txn-" + "3" * 32,
        effect_at=EVALUATED,
    )

    run_hash = run_id_sha256("cleanup-run-2")
    assert result.outcome_kind == "committed"
    assert result.cleanup_revision == 1
    assert result.receipt is not None
    assert result.tombstone is not None
    assert result.receipt.result_state == "quarantined"
    assert result.tombstone.state == "quarantined"
    assert not (tmp_path / "product" / "runs" / "cleanup-run-2").exists()
    assert (cleanup_root / "quarantine" / "cleanup-run-2").is_dir()
    current = executor.store.read_current(run_hash)
    assert current is not None
    assert current[0].state == "quarantined"
    assert not hasattr(executor.store, "delete_product_run")
