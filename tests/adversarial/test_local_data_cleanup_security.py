from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.local_data_cleanup import LocalDataCleanupExecutor
from poker_deliberation.local_data_cleanup_canonical import run_id_sha256
from poker_deliberation.local_data_cleanup_models import (
    CleanupFailureCode,
    LegalHoldSnapshotV1,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.local_data_cleanup_store import (
    CleanupStorageError,
    initialize_cleanup_root,
    scan_cleanup_tree,
)

NOW = datetime(2026, 7, 25, 12, tzinfo=UTC)


class NoHold:
    def resolve(
        self,
        run_id_sha256: str,
        *,
        evaluated_at: datetime,
    ) -> LegalHoldSnapshotV1:
        return LegalHoldSnapshotV1(
            provider_id="security-hold",
            provider_version="1.0.0",
            run_id_sha256=run_id_sha256,
            legal_hold=False,
            snapshot_reference_sha256=hashlib.sha256(run_id_sha256.encode()).hexdigest(),
            resolved_at=evaluated_at,
        )


def _orchestrator(tmp_path: Path) -> Orchestrator:
    config = AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )
    orchestrator = Orchestrator(
        config,
        terminal_clock=lambda: NOW,
        context_clock=lambda: NOW,
    )
    orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="disposable security fixture",
            analysis_scope="retrospective",
        ),
        run_id="security-run",
    )
    return orchestrator


def test_cleanup_root_cannot_overlap_product_root(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)

    with pytest.raises(CleanupStorageError) as caught:
        initialize_cleanup_root(
            tmp_path / "product" / "cleanup",
            orchestrator.product_store,
            existing_run_id="security-run",
            root_id="cleanup-root-" + "1" * 32,
            initialized_at=NOW,
        )

    assert caught.value.failure.code is CleanupFailureCode.PATH_CONFINEMENT_FAILED


def test_symlink_tree_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "payload.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = target / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available")

    with pytest.raises(CleanupStorageError) as caught:
        scan_cleanup_tree(target, run_id_sha256=run_id_sha256("security-run"))

    assert caught.value.failure.code is CleanupFailureCode.LINK_OR_REPARSE_DETECTED
    assert outside.read_text(encoding="utf-8") == "outside"


def test_hardlink_tree_is_rejected_without_unlinking_either_name(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    first = target / "first.json"
    second = target / "second.json"
    first.write_text("{}", encoding="utf-8")
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("hardlink creation is not available")

    with pytest.raises(CleanupStorageError) as caught:
        scan_cleanup_tree(target, run_id_sha256=run_id_sha256("security-run"))

    assert caught.value.failure.code is CleanupFailureCode.HARDLINK_DETECTED
    assert first.exists() and second.exists()


@pytest.mark.parametrize("run_id", ("../escape", "CON", "run:name", "run\\name"))
def test_unsafe_run_id_has_no_cleanup_effect(tmp_path: Path, run_id: str) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: NOW + timedelta(days=400),
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "2" * 32,
        initialized_at=NOW,
    )
    before = tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*"))

    result = executor.dry_run_quarantine(
        run_id,
        execution_id="security-execution",
        idempotency_key="security-key",
        expires_at=NOW + timedelta(days=401),
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.INVALID_PLAN
    assert tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*")) == before
