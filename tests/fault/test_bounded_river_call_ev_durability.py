from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

import pytest

import poker_deliberation.storage.bounded_river_call_ev_admission_store as admission_store
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.storage.revision_canonical import CanonicalStorageError
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
)
from tests.bounded_river_call_ev_support import admission, app_config


def test_admission_directory_sync_failure_stops_before_product_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(app_config(tmp_path))
    admitted = admission(run_id="run-river-admission-sync-fault")
    original_sync = admission_store.sync_directory

    def fail_record_parent(
        path: Path,
        *,
        injector: Callable[[str], None] | None = None,
        hook: str = "directory_sync",
    ) -> None:
        if hook == "bounded_river_call_ev_admission.record_parent":
            raise CanonicalStorageError("synthetic admission directory sync failure")
        original_sync(path, injector=injector, hook=hook)

    monkeypatch.setattr(admission_store, "sync_directory", fail_record_parent)

    with pytest.raises(ProductRunError) as caught:
        orchestrator.run_bounded_river_call_ev_review(admitted)

    assert caught.value.failure.stage == "new_run_reservation"
    assert not orchestrator.store.exists(admitted.confirmation.run_id)
    product_run = orchestrator.product_store.runs_root / admitted.confirmation.run_id
    assert not (product_run / ".terminal-store" / "current.json").exists()
    assert (
        admission_store.read_bounded_river_call_ev_admission_record(
            orchestrator.revision_runs_root,
            admitted.confirmation.run_id,
            maximum_bytes=orchestrator.budget_policy.max_artifact_bytes,
        )
        is not None
    )


def test_same_run_reservation_serializes_before_tool_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = Orchestrator(app_config(tmp_path))
    second = Orchestrator(app_config(tmp_path))
    admitted = admission(run_id="run-river-reservation-race")
    entered = threading.Event()
    release = threading.Event()
    original_sync = admission_store.sync_directory

    def blocking_sync(
        path: Path,
        *,
        injector: Callable[[str], None] | None = None,
        hook: str = "directory_sync",
    ) -> None:
        if hook == "bounded_river_call_ev_admission.record_parent":
            entered.set()
            assert release.wait(timeout=10)
        original_sync(path, injector=injector, hook=hook)

    monkeypatch.setattr(admission_store, "sync_directory", blocking_sync)
    reports = []
    failures: list[BaseException] = []

    def run_first() -> None:
        try:
            reports.append(first.run_bounded_river_call_ev_review(admitted))
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert entered.wait(timeout=10)
    try:
        with pytest.raises(ProductRunError) as caught:
            second.run_bounded_river_call_ev_review(admitted)
        assert caught.value.failure.code is ProductRunFailureCode.RUN_LOCKED
    finally:
        release.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert failures == []
    assert len(reports) == 1
    assert reports[0].run_status == "completed"
