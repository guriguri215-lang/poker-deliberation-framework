"""Concurrency tests for P2-012B terminal-run storage."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
)


def test_two_writers_cannot_last_write_win_the_same_run(tmp_path: Path) -> None:
    config = AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )
    bootstrap = Orchestrator(config)
    bootstrap._initialize_product_storage("run-bootstrap")
    barrier = Barrier(2)

    def attempt(label: str):  # type: ignore[no-untyped-def]
        orchestrator = Orchestrator(config)
        barrier.wait(timeout=10)
        try:
            return orchestrator.run(
                CaseInput(
                    kind="calculation",
                    raw_text=label,
                    analysis_scope="retrospective",
                ),
                run_id="run-concurrent",
            )
        except ProductRunError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(attempt, ("first", "second")))

    reports = tuple(item for item in results if not isinstance(item, ProductRunError))
    errors = tuple(item for item in results if isinstance(item, ProductRunError))
    assert len(reports) == 1
    assert len(errors) == 1
    assert errors[0].failure.code in {
        ProductRunFailureCode.RUN_LOCKED,
        ProductRunFailureCode.RUN_CONFLICT,
    }
    current = Orchestrator(config).product_store.read_current("run-concurrent")
    assert current.read_status is RunReadStatus.SUCCEEDED
    assert current.reachable_revisions == (1,)
