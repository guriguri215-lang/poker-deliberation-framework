"""Fault tests for P2-012B terminal-run storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
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
        raw_text="fault fixture",
        analysis_scope="retrospective",
    )


@pytest.mark.parametrize(
    "fault_hook",
    (
        "payload.final_report.json.before_write",
        "manifest.before_write",
        "completion.before_write",
        "revision.before_rename",
        "current.before_replace",
        "current.after_replace",
    ),
)
def test_fault_boundaries_never_return_or_read_completed(
    tmp_path: Path,
    fault_hook: str,
) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)

    def inject(hook: str) -> None:
        if hook == fault_hook:
            raise OSError("injected terminal fault")

    orchestrator.product_store.fault_injector = inject
    orchestrator.product_store.foundation.fault_injector = inject
    with pytest.raises(ProductRunError):
        orchestrator.run(_case(), run_id="run-terminal-fault")

    with pytest.raises(ProductRunError) as read_error:
        Orchestrator(config).load_report("run-terminal-fault")
    assert read_error.value.failure.code in {
        ProductRunFailureCode.RUN_INCOMPLETE,
        ProductRunFailureCode.BUDGET_SETTLEMENT_FAILED,
    }
    assert read_error.value.failure.read_status is RunReadStatus.INCOMPLETE


def test_missing_completion_marker_is_corrupt_not_completed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    orchestrator = Orchestrator(config)
    verified = orchestrator.product_store
    report = orchestrator.run(_case(), run_id="run-missing-marker")
    current = verified.read_current(report.run_id)
    marker = (
        config.revision_runs_dir
        / "runs"
        / report.run_id
        / ".terminal-store"
        / current.pointer.revision_relative_path
        / "completion.json"
    )
    marker.unlink()

    with pytest.raises(ProductRunError) as caught:
        Orchestrator(config).load_report(report.run_id)

    assert caught.value.failure.code is ProductRunFailureCode.RUN_CORRUPT
    assert caught.value.failure.read_status is RunReadStatus.CORRUPT
