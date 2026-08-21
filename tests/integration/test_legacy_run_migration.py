"""Integration tests for P2-012B flat-v1 migration."""

from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_equity import admit_versioned_range_river_equity
from poker_deliberation.schemas import (
    CaseInput,
    Exactness,
    FinalReport,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.storage.range_equity_admission_store import (
    read_range_equity_admission_record,
)
from poker_deliberation.storage.run_store import RunStore
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
)
from tests.range_support import versioned_river_equity_case


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _legacy_source(
    config: AppConfig,
    run_id: str = "run-legacy-source",
    *,
    tool_results: list[ToolResult] | None = None,
) -> Path:
    store = RunStore(config.runs_dir)
    store.create_run(run_id)
    store.write_json(
        run_id,
        "input.json",
        CaseInput(
            kind="calculation",
            raw_text="legacy fixture",
            analysis_scope="retrospective",
        ),
    )
    store.write_json(
        run_id,
        "state.json",
        {
            "state": "COMPLETED",
            "events": [],
            "deliberation_rounds": 0,
            "tool_retries": {},
            "elapsed_seconds": 0.0,
        },
    )
    report = FinalReport(
        run_id=run_id,
        run_status="completed",
        conclusion="legacy source conclusion",
        tool_results=tool_results or [],
    )
    store.write_json(run_id, "final_report.json", report)
    store.write_text(run_id, "final_report.md", "legacy source conclusion\n")
    return config.runs_dir / run_id


def _bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def test_flat_v1_show_is_downgraded_and_source_is_never_modified(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = _legacy_source(config)
    before = _bytes(source)

    report = Orchestrator(config).load_report("run-legacy-source")

    assert report.run_status == "failed_with_limitations"
    assert "legacy_unverified_integrity_guarantees_missing" in report.limitations
    assert _bytes(source) == before
    assert not config.revision_runs_dir.exists()
    assert not config.durable_budget_runs_dir.exists()


def test_copy_migration_preserves_exact_bytes_and_replays_idempotently(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = _legacy_source(config)
    before = _bytes(source)
    orchestrator = Orchestrator(config)

    migrated = orchestrator.migrate_legacy_run(
        "run-legacy-source",
        "run-legacy-copy",
        source_quiescence_acknowledged=True,
    )
    replay = Orchestrator(config).migrate_legacy_run(
        "run-legacy-source",
        "run-legacy-copy",
        source_quiescence_acknowledged=True,
    )

    assert migrated.read_status is RunReadStatus.LEGACY_UNVERIFIED
    assert migrated.resume_eligible is False
    assert migrated.completion_marker is None
    assert migrated.lifecycle_verified is False
    assert replay.current_pointer_sha256 == migrated.current_pointer_sha256
    assert {
        payload.inventory.logical_name: payload.exact_bytes for payload in migrated.payloads
    } == {name: data for name, data in before.items() if name != ".poker-deliberation-run"}
    assert "normalization.json" not in {
        payload.inventory.logical_name for payload in migrated.payloads
    }
    assert _bytes(source) == before
    projection = Orchestrator(config).load_report("run-legacy-copy")
    assert projection.run_status == "failed_with_limitations"
    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).report_path("run-legacy-copy", "json")
    assert caught.value.failure.code is ProductRunFailureCode.LEGACY_RUN_UNVERIFIED


def test_migrated_unknown_legacy_tool_is_readable_as_unverified_projection(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    unknown_result = ToolResult(
        tool_name="retired_legacy_tool",
        input={"legacy": True},
        status=ToolStatus.UNAVAILABLE,
        exactness=Exactness.UNAVAILABLE,
        numeric_exactness=NumericalExactness.UNAVAILABLE,
        error="unknown tool: retired_legacy_tool",
    )
    _legacy_source(config, tool_results=[unknown_result])
    Orchestrator(config).migrate_legacy_run(
        "run-legacy-source",
        "run-legacy-copy-unknown-tool",
        source_quiescence_acknowledged=True,
    )

    projection = Orchestrator(config).load_report("run-legacy-copy-unknown-tool")

    assert projection.run_status == "failed_with_limitations"
    assert projection.tool_results == [unknown_result]
    assert "legacy_unverified_integrity_guarantees_missing" in projection.limitations


def test_migration_requires_explicit_source_quiescence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    _legacy_source(config)

    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).migrate_legacy_run(
            "run-legacy-source",
            "run-legacy-copy",
            source_quiescence_acknowledged=False,
        )

    assert caught.value.failure.code is ProductRunFailureCode.MIGRATION_SOURCE_BUSY
    assert not config.revision_runs_dir.exists()
    assert not config.durable_budget_runs_dir.exists()


def test_unknown_legacy_sentinel_is_refused_without_writes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = _legacy_source(config)
    sentinel = source / ".poker-deliberation-run"
    sentinel.write_bytes(b"v9\n")
    before = _bytes(source)

    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).load_report("run-legacy-source")

    assert caught.value.failure.code is ProductRunFailureCode.LEGACY_RUN_UNVERIFIED
    assert _bytes(source) == before
    assert not config.revision_runs_dir.exists()


def test_source_change_during_copy_leaves_no_published_current(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    source = _legacy_source(config)
    orchestrator = Orchestrator(config)
    original = orchestrator.legacy_adapter.inspect
    calls = 0

    def change_before_reread(run_id: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            (source / "final_report.md").write_text(
                "changed by source owner\n",
                encoding="utf-8",
            )
        return original(run_id)

    monkeypatch.setattr(orchestrator.legacy_adapter, "inspect", change_before_reread)
    with pytest.raises(ProductRunError) as caught:
        orchestrator.migrate_legacy_run(
            "run-legacy-source",
            "run-legacy-copy",
            source_quiescence_acknowledged=True,
        )

    assert caught.value.failure.code is ProductRunFailureCode.MIGRATION_SOURCE_CHANGED
    assert caught.value.failure.filesystem_effect == "staging_orphan"
    current = (
        config.revision_runs_dir / "runs" / "run-legacy-copy" / ".terminal-store" / "current.json"
    )
    assert not current.exists()


def test_migration_cannot_publish_into_reserved_range_equity_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _legacy_source(config)
    migration = Orchestrator(config)
    bridge = Orchestrator(config)
    destination = "run-migration-bridge-race"
    migration_ready_to_reserve = Event()
    release_migration = Event()
    bridge_namespace_reserved = Event()
    migration_results: list[object] = []
    bridge_results: list[object] = []
    bridge_binding_sha256: list[str] = []
    original_reserve = migration._reserve_legacy_migration_destination

    def pausing_reserve(run_id: str) -> None:
        migration_ready_to_reserve.set()
        assert release_migration.wait(timeout=20)
        original_reserve(run_id)

    monkeypatch.setattr(migration, "_reserve_legacy_migration_destination", pausing_reserve)

    def migrate() -> None:
        try:
            migration_results.append(
                migration.migrate_legacy_run(
                    "run-legacy-source",
                    destination,
                    source_quiescence_acknowledged=True,
                )
            )
        except ProductRunError as exc:
            migration_results.append(exc)

    def run_bridge() -> None:
        try:
            admission = admit_versioned_range_river_equity(versioned_river_equity_case())
            bridge._reserve_new_run(admission.case, destination)
            bridge_results.append(admission)
            bridge_binding_sha256.append(admission.binding.binding_sha256)
        except ProductRunError as exc:
            bridge_results.append(exc)
        finally:
            bridge_namespace_reserved.set()

    migration_thread = Thread(target=migrate)
    bridge_thread = Thread(target=run_bridge)
    migration_thread.start()
    assert migration_ready_to_reserve.wait(timeout=20)
    bridge_thread.start()
    assert bridge_namespace_reserved.wait(timeout=20)
    release_migration.set()
    migration_thread.join(timeout=30)
    bridge_thread.join(timeout=30)

    assert not migration_thread.is_alive()
    assert not bridge_thread.is_alive()
    assert len(migration_results) == 1
    assert isinstance(migration_results[0], ProductRunError)
    assert migration_results[0].failure.code is ProductRunFailureCode.MIGRATION_CONFLICT
    assert len(bridge_results) == 1
    assert not isinstance(bridge_results[0], ProductRunError)
    assert bridge._namespace_kind(destination) == "product"
    admission_record = read_range_equity_admission_record(
        bridge.revision_runs_root,
        destination,
        maximum_bytes=bridge.budget_policy.max_artifact_bytes,
    )
    assert admission_record is not None
    assert admission_record.binding_sha256 == bridge_binding_sha256[0]
