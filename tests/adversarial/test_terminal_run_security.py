"""Adversarial tests for P2-012B terminal-run storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput, FinalReport
from poker_deliberation.storage.run_store import RunStore
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
)


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _case() -> CaseInput:
    return CaseInput(
        kind="calculation",
        raw_text="security fixture",
        analysis_scope="retrospective",
    )


def test_mixed_v1_v2_namespace_is_never_trusted(tmp_path: Path) -> None:
    config = _config(tmp_path)
    Orchestrator(config).run(_case(), run_id="run-mixed")
    legacy = RunStore(config.runs_dir)
    legacy.create_run("run-mixed")
    legacy.write_json(
        "run-mixed",
        "final_report.json",
        FinalReport(run_id="run-mixed", conclusion="legacy collision"),
    )

    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).load_report("run-mixed")

    assert caught.value.failure.code is ProductRunFailureCode.CROSS_RUN_MISMATCH


def test_case_alias_in_legacy_root_blocks_new_product_run(tmp_path: Path) -> None:
    config = _config(tmp_path)
    RunStore(config.runs_dir).create_run("Run-Alias")

    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).run(_case(), run_id="run-alias")

    assert caught.value.failure.code is ProductRunFailureCode.CROSS_RUN_MISMATCH
    assert not (config.revision_runs_dir / "runs" / "run-alias").exists()


def test_future_pointer_version_is_not_downgraded_to_corruption_or_success(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    Orchestrator(config).run(_case(), run_id="run-future")
    current = config.revision_runs_dir / "runs" / "run-future" / ".terminal-store" / "current.json"
    data = current.read_bytes().replace(
        b'"schema_version":"2.0.0"',
        b'"schema_version":"9.0.0"',
    )
    current.write_bytes(data)

    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).load_report("run-future")

    assert caught.value.failure.code is ProductRunFailureCode.UNSUPPORTED_RUN_VERSION
    assert caught.value.failure.read_status is RunReadStatus.UNSUPPORTED_VERSION


def test_duplicate_key_legacy_json_is_refused_without_product_write(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    source = RunStore(config.runs_dir).create_run("run-duplicate")
    (source / "final_report.json").write_bytes(b'{"run_id":"run-duplicate","run_id":"run-other"}\n')

    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).load_report("run-duplicate")

    assert caught.value.failure.code is ProductRunFailureCode.LEGACY_RUN_UNVERIFIED
    assert not config.revision_runs_dir.exists()


@pytest.mark.parametrize(
    "run_id",
    ("../escape", "CON", "run:name", "run\\name", "run-name."),
)
def test_unsafe_run_identity_has_no_product_filesystem_effect(
    tmp_path: Path,
    run_id: str,
) -> None:
    config = _config(tmp_path)

    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).run(_case(), run_id=run_id)

    assert caught.value.failure.code is ProductRunFailureCode.PATH_CONFINEMENT_FAILED
    assert not config.revision_runs_dir.exists()
    assert not config.durable_budget_runs_dir.exists()
