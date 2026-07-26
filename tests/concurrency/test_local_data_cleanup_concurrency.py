from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from poker_deliberation.local_data_cleanup import LocalDataCleanupExecutor
from poker_deliberation.local_data_cleanup_canonical import run_id_sha256
from tests.integration.test_local_data_cleanup_executor import (
    EVALUATED,
    NoLegalHold,
    _approve_cleanup,
    _case,
    _orchestrator,
)


def _executor(tmp_path, orchestrator) -> LocalDataCleanupExecutor:
    return LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )


def test_two_exact_writers_have_one_physical_winner_and_equivalent_results(
    tmp_path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-concurrent-replay")
    initializer = _executor(tmp_path, orchestrator)
    initializer.initialize_cleanup_root(
        existing_run_id="cleanup-concurrent-replay",
        root_id="cleanup-root-" + "d" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    dry_run = initializer.dry_run_quarantine(
        "cleanup-concurrent-replay",
        execution_id="cleanup-concurrent-execution",
        idempotency_key="cleanup-concurrent-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="cleanup-concurrent-approval",
    )
    start = threading.Event()

    def execute():
        executor = _executor(tmp_path, orchestrator)
        start.wait()
        return executor.execute_quarantine(
            dry_run.plan,
            approval_run_id="cleanup-concurrent-approval",
            approval_request_id=request_id,
            authority_provider=provider,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (pool.submit(execute), pool.submit(execute))
        start.set()
        results = tuple(future.result(timeout=30) for future in futures)

    committed = tuple(result for result in results if result.outcome_kind == "committed")
    assert committed
    if len(committed) == 2:
        assert committed[0] == committed[1]
    current = initializer.store.read_current(run_id_sha256("cleanup-concurrent-replay"))
    assert current is not None
    assert current[0].revision == 1
    assert not (tmp_path / "product" / "runs" / "cleanup-concurrent-replay").exists()
    assert (tmp_path / "cleanup" / "quarantine" / "cleanup-concurrent-replay").is_dir()


def test_two_different_plans_cannot_both_win_same_product_run(tmp_path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-concurrent-conflict")
    initializer = _executor(tmp_path, orchestrator)
    initializer.initialize_cleanup_root(
        existing_run_id="cleanup-concurrent-conflict",
        root_id="cleanup-root-" + "e" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    first = initializer.dry_run_quarantine(
        "cleanup-concurrent-conflict",
        execution_id="cleanup-concurrent-first",
        idempotency_key="cleanup-concurrent-first-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    second = initializer.dry_run_quarantine(
        "cleanup-concurrent-conflict",
        execution_id="cleanup-concurrent-second",
        idempotency_key="cleanup-concurrent-second-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert first.plan is not None
    assert second.plan is not None
    first_request, first_provider = _approve_cleanup(
        orchestrator,
        first.plan,
        approval_run_id="cleanup-concurrent-first-approval",
    )
    second_request, second_provider = _approve_cleanup(
        orchestrator,
        second.plan,
        approval_run_id="cleanup-concurrent-second-approval",
    )
    start = threading.Event()

    def execute(plan, approval_run_id, request_id, provider):
        executor = _executor(tmp_path, orchestrator)
        start.wait()
        return executor.execute_quarantine(
            plan,
            approval_run_id=approval_run_id,
            approval_request_id=request_id,
            authority_provider=provider,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(
                execute,
                first.plan,
                "cleanup-concurrent-first-approval",
                first_request,
                first_provider,
            ),
            pool.submit(
                execute,
                second.plan,
                "cleanup-concurrent-second-approval",
                second_request,
                second_provider,
            ),
        )
        start.set()
        results = tuple(future.result(timeout=30) for future in futures)

    assert sum(result.outcome_kind == "committed" for result in results) == 1
    assert sum(result.outcome_kind == "failed" for result in results) == 1
    current = initializer.store.read_current(run_id_sha256("cleanup-concurrent-conflict"))
    assert current is not None
    assert current[0].revision == 1
