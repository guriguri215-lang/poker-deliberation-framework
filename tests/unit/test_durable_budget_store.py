"""Unit tests for the internal durable budget store."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.budgets.durable_models import (
    AttemptStatus,
    DurableBudgetPolicyV1,
    DurableFailureCode,
    ExecutionActivationV1,
    ExecutionLineageV1,
    MutationStatus,
    OwnerKind,
    ResourceAmountsV1,
    ResourceReservationV1,
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


class FakeClock:
    def __init__(self) -> None:
        self.value = 100

    def __call__(self) -> int:
        self.value += 10
        return self.value


@pytest.fixture
def store() -> Generator[DurableBudgetStore, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2bs-", dir=parent) as directory:
        base = Path(directory)
        legacy = base / "legacy"
        legacy.mkdir()
        revision = base / "revision"
        initialize_durable_budget_root(
            revision,
            legacy,
            root_id="root-" + "c" * 32,
            initialized_at=NOW,
        )
        yield DurableBudgetStore(
            revision,
            legacy,
            clock=FakeClock(),
            wall_clock=lambda: NOW,
        )


def _policy(
    *,
    max_concurrency: int = 2,
    max_run_bytes: int = 20_000,
) -> DurableBudgetPolicyV1:
    return DurableBudgetPolicyV1(
        base_policy=BudgetPolicyV2(
            max_deliberation_rounds=3,
            max_tool_retries=2,
            max_external_cost_micro_usd=100,
            max_provider_output_bytes=2_000,
            max_tool_input_bytes=2_000,
            max_tool_output_bytes=2_000,
            max_artifact_bytes=2_000,
            max_run_bytes=max_run_bytes,
        ),
        activation=ExecutionActivationV1(
            max_concurrent_agents=max_concurrency,
            max_automatic_retries=1,
        ),
    )


def _lineage(ordinal: int = 0) -> ExecutionLineageV1:
    return ExecutionLineageV1(
        owner_kind=OwnerKind.TOOL,
        owner_id=f"tool-owner-{ordinal}",
        role="calculator",
        phase_id="tool_research",
        assignment_id=f"assignment-{ordinal}",
        root_attempt_id=f"attempt-{ordinal}",
        attempt_id=f"attempt-{ordinal}",
        root_context_id=f"context-{ordinal}",
        context_id=f"context-{ordinal}",
        context_source_sha256=HASH,
        context_policy_sha256=HASH,
        context_integrity_sha256=HASH,
        execution_ordinal=ordinal,
        idempotency_key=f"effect-{ordinal}",
        idempotency_request_sha256=HASH,
    )


def _reservation(*, run_bytes: int = 100) -> ResourceReservationV1:
    return build_resource_reservation(
        reservation_id=f"reservation-{run_bytes}",
        requested=ResourceAmountsV1(
            active_runtime_ns=100,
            tool_attempts=1,
            tool_input_bytes=100,
            tool_output_bytes=100,
            run_bytes=run_bytes,
            concurrency_slots=1,
        ),
    )


def _create(store: DurableBudgetStore) -> None:
    created = store.create("Run-budget-store", _policy(), operation_id="initialize-1")
    assert created.status is MutationStatus.APPLIED


def test_reserve_start_settle_and_exact_replay_survive_restart(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    reservation = _reservation()
    admitted = store.reserve(
        "Run-budget-store",
        operation_id="reserve-1",
        permit_id="permit-1",
        reservation=reservation,
        lineage=_lineage(),
    )
    replay = store.reserve(
        "Run-budget-store",
        operation_id="reserve-1",
        permit_id="permit-1",
        reservation=reservation,
        lineage=_lineage(),
    )
    assert admitted.status is MutationStatus.APPLIED
    assert replay.status is MutationStatus.EXACT_REPLAY

    store.start(
        "Run-budget-store",
        operation_id="start-1",
        permit_id="permit-1",
    )
    settled = store.settle(
        "Run-budget-store",
        operation_id="settle-1",
        settlement_id="settlement-1",
        permit_id="permit-1",
        actual=ResourceAmountsV1(
            active_runtime_ns=80,
            tool_attempts=1,
            tool_input_bytes=80,
            tool_output_bytes=80,
            run_bytes=80,
            concurrency_slots=1,
        ),
        status=SettlementStatus.SUCCEEDED,
        result_sha256="b" * 64,
        effect_evidence_sha256="c" * 64,
    )
    settle_replay = store.settle(
        "Run-budget-store",
        operation_id="settle-1",
        settlement_id="settlement-1",
        permit_id="permit-1",
        actual=ResourceAmountsV1(
            active_runtime_ns=80,
            tool_attempts=1,
            tool_input_bytes=80,
            tool_output_bytes=80,
            run_bytes=80,
            concurrency_slots=1,
        ),
        status=SettlementStatus.SUCCEEDED,
        result_sha256="b" * 64,
        effect_evidence_sha256="c" * 64,
    )

    assert settled.state.usage.run_bytes == 80
    assert settled.state.usage.peak_concurrency == 1
    assert not settled.state.active_permits
    assert settle_replay.status is MutationStatus.EXACT_REPLAY
    assert store.load("Run-budget-store") == settled.state


def test_operation_key_reuse_with_different_bytes_is_a_conflict(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-1",
        permit_id="permit-1",
        reservation=_reservation(),
        lineage=_lineage(),
    )

    with pytest.raises(DurableBudgetError) as conflict:
        store.reserve(
            "Run-budget-store",
            operation_id="reserve-1",
            permit_id="permit-other",
            reservation=_reservation(run_bytes=101),
            lineage=_lineage(1),
        )
    assert conflict.value.failure.code is DurableFailureCode.IDEMPOTENCY_CONFLICT
    assert len(store.load("Run-budget-store").active_permits) == 1


def test_no_effect_release_is_explicit_and_started_resume_fails_closed(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-1",
        permit_id="permit-1",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    released = store.release_no_effect(
        "Run-budget-store",
        operation_id="release-1",
        settlement_id="settlement-1",
        permit_id="permit-1",
        evidence_sha256="d" * 64,
    )
    assert released.state.settlements[0].status is SettlementStatus.RELEASED_NO_EFFECT
    assert released.state.usage == released.state.usage.model_copy()

    store.reserve(
        "Run-budget-store",
        operation_id="reserve-2",
        permit_id="permit-2",
        reservation=_reservation(run_bytes=101),
        lineage=_lineage(1),
    )
    store.start(
        "Run-budget-store",
        operation_id="start-2",
        permit_id="permit-2",
    )
    with pytest.raises(DurableBudgetError) as unknown:
        store.resume("Run-budget-store")
    assert unknown.value.failure.code is DurableFailureCode.EFFECT_UNKNOWN
    assert unknown.value.failure.reconciliation_required


def test_settlement_overrun_records_actual_and_latches_failure(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-1",
        permit_id="permit-1",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    store.start(
        "Run-budget-store",
        operation_id="start-1",
        permit_id="permit-1",
    )
    overrun = store.settle(
        "Run-budget-store",
        operation_id="settle-1",
        settlement_id="settlement-1",
        permit_id="permit-1",
        actual=ResourceAmountsV1(
            active_runtime_ns=101,
            tool_attempts=1,
            tool_input_bytes=100,
            tool_output_bytes=101,
            run_bytes=101,
            concurrency_slots=1,
        ),
        status=SettlementStatus.FAILED,
        effect_evidence_sha256="e" * 64,
    )
    assert overrun.state.settlements[0].status is SettlementStatus.OVERRUN
    assert overrun.state.settlements[0].actual.tool_output_bytes == 101
    assert overrun.state.failure_latch is not None
    assert overrun.state.failure_latch.code is DurableFailureCode.SETTLEMENT_OVERRUN

    with pytest.raises(DurableBudgetError) as latched:
        store.reserve(
            "Run-budget-store",
            operation_id="reserve-2",
            permit_id="permit-2",
            reservation=_reservation(run_bytes=101),
            lineage=_lineage(1),
        )
    assert latched.value.failure.code is DurableFailureCode.FAILURE_LATCHED


def test_policy_change_can_only_tighten_above_used_and_reserved(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-1",
        permit_id="permit-1",
        reservation=_reservation(run_bytes=100),
        lineage=_lineage(),
    )
    tightened = store.tighten_policy(
        "Run-budget-store",
        operation_id="tighten-1",
        new_policy=_policy(max_concurrency=1, max_run_bytes=15_000),
        reason_sha256="f" * 64,
    )
    assert tightened.state.policy.activation.max_concurrent_agents == 1

    with pytest.raises(DurableBudgetError) as loosened:
        store.tighten_policy(
            "Run-budget-store",
            operation_id="tighten-2",
            new_policy=_policy(max_concurrency=2, max_run_bytes=20_000),
            reason_sha256="f" * 64,
        )
    assert loosened.value.failure.code is DurableFailureCode.POLICY_MISMATCH
    assert tightened.state.attempts[0].status is AttemptStatus.RESERVED
