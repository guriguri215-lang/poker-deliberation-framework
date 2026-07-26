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
    CancellationState,
    DeterministicToolEvidenceV1,
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
    assert settled.state.usage.active_runtime_ns == 40
    assert settled.state.settlements[0].actual.active_runtime_ns == 30
    assert settled.state.settlements[0].settled_active_runtime_ns == 40
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


def test_mutation_zero_refusals_do_not_reset_active_runtime_baseline(
    store: DurableBudgetStore,
) -> None:
    _create(store)

    for operation_id in ("invalid-start-1", "invalid-start-2"):
        with pytest.raises(DurableBudgetError):
            store.start(
                "Run-budget-store",
                operation_id=operation_id,
                permit_id="missing-permit",
            )

    admitted = store.reserve(
        "Run-budget-store",
        operation_id="reserve-after-refusals",
        permit_id="permit-after-refusals",
        reservation=_reservation(),
        lineage=_lineage(),
    )

    assert admitted.state.usage.active_runtime_ns == 30
    assert admitted.state.active_permits[0].reserved_active_runtime_ns == 30


def test_clock_rollback_is_detected_against_an_uncommitted_observation(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    observations = iter((150, 140))
    store.clock = lambda: next(observations)

    with pytest.raises(DurableBudgetError):
        store.start(
            "Run-budget-store",
            operation_id="invalid-before-rollback",
            permit_id="missing-permit",
        )
    with pytest.raises(DurableBudgetError) as rollback:
        store.start(
            "Run-budget-store",
            operation_id="rollback-after-refusal",
            permit_id="missing-permit",
        )

    assert rollback.value.failure.code is DurableFailureCode.CLOCK_ROLLBACK
    assert rollback.value.failure.observed == 10
    assert store.load("Run-budget-store").generation == 1


def test_active_runtime_baselines_are_isolated_by_run(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.create(
        "Run-budget-store-other",
        _policy(),
        operation_id="initialize-other",
    )

    admitted = store.reserve(
        "Run-budget-store",
        operation_id="reserve-after-other-run",
        permit_id="permit-after-other-run",
        reservation=_reservation(),
        lineage=_lineage(),
    )

    assert admitted.state.usage.active_runtime_ns == 20
    assert admitted.state.active_permits[0].reserved_active_runtime_ns == 20
    assert store.load("Run-budget-store-other").usage.active_runtime_ns == 0


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
    replay = store.tighten_policy(
        "Run-budget-store",
        operation_id="tighten-1",
        new_policy=_policy(max_concurrency=1, max_run_bytes=15_000),
        reason_sha256="f" * 64,
    )
    assert replay.status is MutationStatus.EXACT_REPLAY

    with pytest.raises(DurableBudgetError) as invalid_reason:
        store.tighten_policy(
            "Run-budget-store",
            operation_id="tighten-invalid-reason",
            new_policy=_policy(max_concurrency=1, max_run_bytes=14_000),
            reason_sha256="not-a-sha256",
        )
    assert invalid_reason.value.failure.code is DurableFailureCode.INVALID_INPUT

    with pytest.raises(DurableBudgetError) as loosened:
        store.tighten_policy(
            "Run-budget-store",
            operation_id="tighten-2",
            new_policy=_policy(max_concurrency=2, max_run_bytes=20_000),
            reason_sha256="f" * 64,
        )
    assert loosened.value.failure.code is DurableFailureCode.POLICY_MISMATCH
    assert tightened.state.attempts[0].status is AttemptStatus.RESERVED


def test_cancellation_before_start_blocks_start_and_ack_is_not_settlement(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-cancel",
        permit_id="permit-cancel",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    store.request_cancellation(
        "Run-budget-store",
        operation_id="request-cancel",
        permit_id="permit-cancel",
    )

    with pytest.raises(DurableBudgetError) as blocked:
        store.start(
            "Run-budget-store",
            operation_id="start-cancelled",
            permit_id="permit-cancel",
        )
    assert blocked.value.failure.code is DurableFailureCode.CANCEL_UNCONFIRMED

    acknowledged = store.record_cancellation(
        "Run-budget-store",
        operation_id="ack-cancel",
        permit_id="permit-cancel",
        state_value=CancellationState.ACKNOWLEDGED,
        evidence_sha256="a" * 64,
        worker_live=False,
    )
    assert acknowledged.state.settlements == ()
    assert acknowledged.state.active_permits[0].permit_id == "permit-cancel"
    assert acknowledged.state.cancellations[0].state is CancellationState.ACKNOWLEDGED


def test_live_cancel_request_cannot_release_a_started_permit(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-live-cancel",
        permit_id="permit-live-cancel",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    store.start(
        "Run-budget-store",
        operation_id="start-live-cancel",
        permit_id="permit-live-cancel",
    )
    requested = store.request_cancellation(
        "Run-budget-store",
        operation_id="request-live-cancel",
        permit_id="permit-live-cancel",
    )

    with pytest.raises(DurableBudgetError) as blocked:
        store.settle(
            "Run-budget-store",
            operation_id="settle-live-cancel",
            settlement_id="settlement-live-cancel",
            permit_id="permit-live-cancel",
            actual=ResourceAmountsV1(concurrency_slots=1),
            status=SettlementStatus.EFFECT_UNKNOWN,
            effect_evidence_sha256="b" * 64,
        )

    assert blocked.value.failure.code is DurableFailureCode.CANCEL_UNCONFIRMED
    state = store.load("Run-budget-store")
    assert state.generation == requested.state.generation
    assert state.active_permits[0].permit_id == "permit-live-cancel"
    assert state.settlements == ()


def test_settlement_rejects_non_boolean_authentication_flag(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-auth-type",
        permit_id="permit-auth-type",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    started = store.start(
        "Run-budget-store",
        operation_id="start-auth-type",
        permit_id="permit-auth-type",
    )

    with pytest.raises(DurableBudgetError) as blocked:
        store.settle(
            "Run-budget-store",
            operation_id="settle-auth-type",
            settlement_id="settlement-auth-type",
            permit_id="permit-auth-type",
            actual=ResourceAmountsV1(concurrency_slots=1),
            status=SettlementStatus.FAILED,
            external_cost_actual_authenticated=1,  # type: ignore[arg-type]
        )

    assert blocked.value.failure.code is DurableFailureCode.INVALID_INPUT
    assert store.load("Run-budget-store").generation == started.state.generation

    with pytest.raises(DurableBudgetError) as invalid_category:
        store.settle(
            "Run-budget-store",
            operation_id="settle-failure-category-type",
            settlement_id="settlement-failure-category-type",
            permit_id="permit-auth-type",
            actual=ResourceAmountsV1(concurrency_slots=1),
            status=SettlementStatus.FAILED,
            failure_category="invented-category",  # type: ignore[arg-type]
        )

    assert invalid_category.value.failure.code is DurableFailureCode.INVALID_INPUT
    assert store.load("Run-budget-store").generation == started.state.generation


def test_deterministic_tool_request_hash_must_match_permit_lineage(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-tool-evidence",
        permit_id="permit-tool-evidence",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    started = store.start(
        "Run-budget-store",
        operation_id="start-tool-evidence",
        permit_id="permit-tool-evidence",
    )

    with pytest.raises(DurableBudgetError) as blocked:
        store.settle(
            "Run-budget-store",
            operation_id="settle-tool-evidence",
            settlement_id="settlement-tool-evidence",
            permit_id="permit-tool-evidence",
            actual=ResourceAmountsV1(
                tool_attempts=1,
                tool_input_bytes=10,
                tool_output_bytes=10,
                run_bytes=10,
                concurrency_slots=1,
            ),
            status=SettlementStatus.SUCCEEDED,
            result_sha256="b" * 64,
            effect_evidence_sha256="c" * 64,
            deterministic_tool_evidence=DeterministicToolEvidenceV1(
                tool_request_bytes_sha256="d" * 64,
                tool_result_bytes_sha256="b" * 64,
                contract_version="1.0.0",
                reproduction_metadata_sha256="e" * 64,
                execution_ordinal=0,
            ),
        )

    assert blocked.value.failure.code is DurableFailureCode.INVALID_INPUT
    state = store.load("Run-budget-store")
    assert state.generation == started.state.generation
    assert state.settlements == ()


def test_committed_settlement_remains_authoritative_over_late_cancel(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-race",
        permit_id="permit-race",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    store.start(
        "Run-budget-store",
        operation_id="start-race",
        permit_id="permit-race",
    )
    settled = store.settle(
        "Run-budget-store",
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
        result_sha256="b" * 64,
        effect_evidence_sha256="c" * 64,
    )

    with pytest.raises(DurableBudgetError) as late_cancel:
        store.request_cancellation(
            "Run-budget-store",
            operation_id="cancel-after-settle",
            permit_id="permit-race",
        )
    assert late_cancel.value.failure.code is DurableFailureCode.INVALID_INPUT
    state = store.load("Run-budget-store")
    assert state.settlements == settled.state.settlements
    assert state.cancellations == ()


def test_restart_rebases_process_clock_and_excludes_wall_downtime(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-restart",
        permit_id="permit-restart",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    before = store.load("Run-budget-store").usage.active_runtime_ns

    class ManualClock:
        value = 10**15

        def __call__(self) -> int:
            return self.value

    clock = ManualClock()
    restarted = DurableBudgetStore(
        store.revisions.revision_root,
        store.revisions.legacy_runs_root,
        clock=clock,
        wall_clock=lambda: NOW,
    )
    restarted.resume("Run-budget-store")
    clock.value += 7
    released = restarted.release_no_effect(
        "Run-budget-store",
        operation_id="release-restart",
        settlement_id="settlement-restart",
        permit_id="permit-restart",
        evidence_sha256="d" * 64,
    )

    assert released.state.usage.active_runtime_ns == before + 7
    assert released.state.settlements[0].settled_active_runtime_ns == before + 7


def test_explicit_clock_rebase_excludes_human_wait_window(
    store: DurableBudgetStore,
) -> None:
    _create(store)
    store.reserve(
        "Run-budget-store",
        operation_id="reserve-approval-wait",
        permit_id="permit-approval-wait",
        reservation=_reservation(),
        lineage=_lineage(),
    )
    before = store.load("Run-budget-store").usage.active_runtime_ns
    store.rebase_monotonic_clock()

    released = store.release_no_effect(
        "Run-budget-store",
        operation_id="release-approval-wait",
        settlement_id="settlement-approval-wait",
        permit_id="permit-approval-wait",
        evidence_sha256="e" * 64,
    )

    assert released.state.usage.active_runtime_ns == before
