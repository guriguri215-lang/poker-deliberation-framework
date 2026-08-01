from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

import poker_deliberation.storage.range_equity_admission_store as admission_store
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_equity import admit_versioned_range_river_equity
from poker_deliberation.storage.revision_canonical import CanonicalStorageError
from poker_deliberation.storage.terminal_models import ProductRunError
from poker_deliberation.tools import default_registry
from tests.range_support import versioned_river_equity_case


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _orchestrator_with_counted_tools(
    tmp_path: Path,
) -> tuple[Orchestrator, list[str]]:
    calls: list[str] = []
    registry = default_registry()
    original = registry._tools["range_validate"]

    def counted(payload: dict[str, object]) -> dict[str, object]:
        calls.append("range_validate")
        return original.function(payload)

    registry._tools["range_validate"] = replace(original, function=counted)
    return Orchestrator(_config(tmp_path), registry=registry), calls


def test_product_namespace_directory_sync_failure_stops_before_tools_and_buffer(
    tmp_path: Path,
) -> None:
    orchestrator, calls = _orchestrator_with_counted_tools(tmp_path)
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    run_id = "p3-016b-namespace-sync-fault"

    def fail_at_first_namespace_sync(hook: str) -> None:
        if hook == "bootstrap_namespace.transactions.before_open":
            raise OSError("synthetic directory sync failure")

    orchestrator.product_store.fault_injector = fail_at_first_namespace_sync

    with pytest.raises(ProductRunError) as error:
        orchestrator.run_versioned_range_river_equity(admission, run_id=run_id)

    assert error.value.failure.stage == "new_run_reservation"
    assert calls == []
    assert not orchestrator.store.exists(run_id)


def test_admission_record_directory_sync_failure_stops_before_tools_and_buffer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator, calls = _orchestrator_with_counted_tools(tmp_path)
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    run_id = "p3-016b-admission-sync-fault"
    original_sync = admission_store.sync_directory

    def fail_record_parent(
        path: Path,
        *,
        injector: Callable[[str], None] | None = None,
        hook: str = "directory_sync",
    ) -> None:
        if hook == "range_equity_admission.record_parent":
            raise CanonicalStorageError("synthetic admission directory sync failure")
        original_sync(path, injector=injector, hook=hook)

    monkeypatch.setattr(admission_store, "sync_directory", fail_record_parent)

    with pytest.raises(ProductRunError) as error:
        orchestrator.run_versioned_range_river_equity(admission, run_id=run_id)

    assert error.value.failure.stage == "new_run_reservation"
    assert calls == []
    assert not orchestrator.store.exists(run_id)
    assert (
        admission_store.read_range_equity_admission_record(
            orchestrator.revision_runs_root,
            run_id,
            maximum_bytes=orchestrator.budget_policy.max_artifact_bytes,
        )
        is not None
    )
