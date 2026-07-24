"""Concurrency tests for durable budget state."""

from __future__ import annotations

import threading
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.budgets.durable_models import (
    DurableBudgetPolicyV1,
    DurableFailureCode,
    ExecutionActivationV1,
    ExecutionLineageV1,
    MutationStatus,
    OwnerKind,
    ResourceAmountsV1,
    SettlementStatus,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetError,
    DurableBudgetStore,
    build_resource_reservation,
    initialize_durable_budget_root,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64


@pytest.fixture
def roots() -> Generator[tuple[Path, Path], None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2bc-", dir=parent) as directory:
        base = Path(directory)
        legacy = base / "legacy"
        legacy.mkdir()
        revision = base / "revision"
        initialize_durable_budget_root(
            revision,
            legacy,
            root_id="root-" + "f" * 32,
            initialized_at=NOW,
        )
        DurableBudgetStore(
            revision,
            legacy,
            clock=lambda: 100,
            wall_clock=lambda: NOW,
        ).create(
            "Run-concurrent-budget",
            DurableBudgetPolicyV1(
                base_policy=BudgetPolicyV2(
                    max_deliberation_rounds=2,
                    max_tool_retries=1,
                    max_tool_input_bytes=2_000,
                    max_tool_output_bytes=2_000,
                    max_artifact_bytes=2_000,
                    max_run_bytes=20_000,
                ),
                activation=ExecutionActivationV1(max_concurrent_agents=2),
            ),
            operation_id="initialize-concurrent",
        )
        yield revision, legacy


def _lineage() -> ExecutionLineageV1:
    return ExecutionLineageV1(
        owner_kind=OwnerKind.TOOL,
        owner_id="owner-concurrent",
        role="calculator",
        phase_id="tool_research",
        assignment_id="assignment-concurrent",
        root_attempt_id="attempt-concurrent",
        attempt_id="attempt-concurrent",
        root_context_id="context-concurrent",
        context_id="context-concurrent",
        context_source_sha256=HASH,
        context_policy_sha256=HASH,
        context_integrity_sha256=HASH,
        execution_ordinal=0,
        idempotency_key="effect-concurrent",
        idempotency_request_sha256=HASH,
    )


def test_concurrent_exact_operation_has_one_winner_then_exact_replay(
    roots: tuple[Path, Path],
) -> None:
    revision, legacy = roots
    acquired = threading.Event()
    release = threading.Event()
    winner = DurableBudgetStore(
        revision,
        legacy,
        clock=lambda: 200,
        wall_clock=lambda: NOW,
    )
    loser = DurableBudgetStore(
        revision,
        legacy,
        clock=lambda: 200,
        wall_clock=lambda: NOW,
    )

    def pause_after_lock(hook: str) -> None:
        if hook == "authority.after_kernel_acquire" and not acquired.is_set():
            acquired.set()
            assert release.wait(timeout=10)

    winner.revisions.fault_injector = pause_after_lock
    reservation = build_resource_reservation(
        reservation_id="reservation-concurrent",
        requested=ResourceAmountsV1(
            tool_attempts=1,
            tool_input_bytes=100,
            tool_output_bytes=100,
            run_bytes=100,
            concurrency_slots=1,
        ),
    )
    results = []

    def reserve_winner() -> None:
        results.append(
            winner.reserve(
                "Run-concurrent-budget",
                operation_id="reserve-concurrent",
                permit_id="permit-concurrent",
                reservation=reservation,
                lineage=_lineage(),
            )
        )

    thread = threading.Thread(target=reserve_winner)
    thread.start()
    assert acquired.wait(timeout=10)
    with pytest.raises(DurableBudgetError) as overlap:
        loser.reserve(
            "Run-concurrent-budget",
            operation_id="reserve-concurrent",
            permit_id="permit-concurrent",
            reservation=reservation,
            lineage=_lineage(),
        )
    assert overlap.value.failure.code is DurableFailureCode.RUN_LOCKED
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0].status is MutationStatus.APPLIED

    replay = loser.reserve(
        "Run-concurrent-budget",
        operation_id="reserve-concurrent",
        permit_id="permit-concurrent",
        reservation=reservation,
        lineage=_lineage(),
    )
    state = loser.load("Run-concurrent-budget")
    assert replay.status is MutationStatus.EXACT_REPLAY
    assert len(state.active_permits) == 1
    assert state.active_permits[0].permit_id == "permit-concurrent"


def test_committed_settlement_wins_concurrent_cancellation_race(
    roots: tuple[Path, Path],
) -> None:
    revision, legacy = roots
    settlement_store = DurableBudgetStore(
        revision,
        legacy,
        clock=lambda: 300,
        wall_clock=lambda: NOW,
    )
    cancellation_store = DurableBudgetStore(
        revision,
        legacy,
        clock=lambda: 300,
        wall_clock=lambda: NOW,
    )
    reservation = build_resource_reservation(
        reservation_id="reservation-race",
        requested=ResourceAmountsV1(
            tool_attempts=1,
            tool_input_bytes=100,
            tool_output_bytes=100,
            run_bytes=100,
            concurrency_slots=1,
        ),
    )
    settlement_store.reserve(
        "Run-concurrent-budget",
        operation_id="reserve-race",
        permit_id="permit-race",
        reservation=reservation,
        lineage=_lineage().model_copy(
            update={
                "owner_id": "owner-race",
                "assignment_id": "assignment-race",
                "root_attempt_id": "attempt-race",
                "attempt_id": "attempt-race",
                "root_context_id": "context-race",
                "context_id": "context-race",
                "idempotency_key": "effect-race",
            }
        ),
    )
    settlement_store.start(
        "Run-concurrent-budget",
        operation_id="start-race",
        permit_id="permit-race",
    )
    acquired = threading.Event()
    release = threading.Event()

    def pause_after_lock(hook: str) -> None:
        if hook == "authority.after_kernel_acquire" and not acquired.is_set():
            acquired.set()
            assert release.wait(timeout=10)

    settlement_store.revisions.fault_injector = pause_after_lock
    errors: list[Exception] = []

    def settle() -> None:
        try:
            settlement_store.settle(
                "Run-concurrent-budget",
                operation_id="settle-race",
                settlement_id="settlement-race",
                permit_id="permit-race",
                actual=ResourceAmountsV1(
                    tool_attempts=1,
                    tool_input_bytes=50,
                    tool_output_bytes=50,
                    run_bytes=50,
                    concurrency_slots=1,
                ),
                status=SettlementStatus.SUCCEEDED,
                result_sha256=HASH,
                effect_evidence_sha256=HASH,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    thread = threading.Thread(target=settle)
    thread.start()
    assert acquired.wait(timeout=10)
    with pytest.raises(DurableBudgetError) as overlap:
        cancellation_store.request_cancellation(
            "Run-concurrent-budget",
            operation_id="cancel-race",
            permit_id="permit-race",
        )
    assert overlap.value.failure.code is DurableFailureCode.RUN_LOCKED
    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []

    with pytest.raises(DurableBudgetError) as late:
        cancellation_store.request_cancellation(
            "Run-concurrent-budget",
            operation_id="cancel-race",
            permit_id="permit-race",
        )
    assert late.value.failure.code is DurableFailureCode.INVALID_INPUT
    state = cancellation_store.load("Run-concurrent-budget")
    assert [item.settlement_id for item in state.settlements] == ["settlement-race"]
    assert state.cancellations == ()
