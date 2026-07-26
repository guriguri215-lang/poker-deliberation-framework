"""Fault tests for durable budget transitions."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.budgets.durable_models import (
    DurableBudgetPolicyV1,
    DurableFailureCode,
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
RUN_ID = "Run-durable-fault"


@pytest.fixture
def store() -> Generator[DurableBudgetStore, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2bf-", dir=parent) as directory:
        base = Path(directory)
        legacy = base / "legacy"
        legacy.mkdir()
        revision = base / "revision"
        initialize_durable_budget_root(
            revision,
            legacy,
            root_id="root-" + "4" * 32,
            initialized_at=NOW,
        )
        durable = DurableBudgetStore(
            revision,
            legacy,
            clock=lambda: 100,
            wall_clock=lambda: NOW,
        )
        durable.create(
            RUN_ID,
            DurableBudgetPolicyV1(
                base_policy=BudgetPolicyV2(
                    max_deliberation_rounds=2,
                    max_tool_retries=1,
                    max_tool_input_bytes=2_000,
                    max_tool_output_bytes=2_000,
                    max_artifact_bytes=2_000,
                    max_run_bytes=20_000,
                )
            ),
            operation_id="initialize-fault",
        )
        yield durable


def _lineage() -> ExecutionLineageV1:
    return ExecutionLineageV1(
        owner_kind=OwnerKind.TOOL,
        owner_id="owner-fault",
        role="calculator",
        phase_id="tool_research",
        assignment_id="assignment-fault",
        root_attempt_id="attempt-fault",
        attempt_id="attempt-fault",
        root_context_id="context-fault",
        context_id="context-fault",
        context_source_sha256=HASH,
        context_policy_sha256=HASH,
        context_integrity_sha256=HASH,
        execution_ordinal=0,
        idempotency_key="effect-fault",
        idempotency_request_sha256=HASH,
    )


def _reserve(store: DurableBudgetStore) -> None:
    store.reserve(
        RUN_ID,
        operation_id="reserve-fault",
        permit_id="permit-fault",
        reservation=build_resource_reservation(
            reservation_id="reservation-fault",
            requested=ResourceAmountsV1(
                tool_attempts=1,
                tool_input_bytes=100,
                tool_output_bytes=100,
                run_bytes=100,
                concurrency_slots=1,
            ),
        ),
        lineage=_lineage(),
    )


def _fail_once(store: DurableBudgetStore, target: str) -> None:
    fired = False

    def inject(hook: str) -> None:
        nonlocal fired
        if hook == target and not fired:
            fired = True
            raise OSError(f"synthetic {target}")

    store.revisions.fault_injector = inject


def test_fault_before_reservation_commit_keeps_prior_domain_state(
    store: DurableBudgetStore,
) -> None:
    _fail_once(store, "current.before_replace")

    with pytest.raises(DurableBudgetError) as error:
        _reserve(store)

    assert error.value.failure.code is DurableFailureCode.RECONCILIATION_REQUIRED
    assert error.value.failure.reconciliation_required is True
    state = DurableBudgetStore(
        store.revisions.revision_root,
        store.revisions.legacy_runs_root,
        clock=lambda: 200,
        wall_clock=lambda: NOW,
    ).load(RUN_ID)
    assert state.active_permits == ()
    assert state.generation == 1


def test_fault_after_reservation_commit_replays_without_duplicate(
    store: DurableBudgetStore,
) -> None:
    _fail_once(store, "current.after_replace")

    with pytest.raises(DurableBudgetError) as error:
        _reserve(store)

    assert error.value.failure.code is DurableFailureCode.DURABILITY_UNCERTAIN
    assert error.value.failure.reconciliation_required is True
    assert error.value.failure.effect_unknown is True
    store.revisions.fault_injector = None
    state = store.load(RUN_ID)
    assert [permit.permit_id for permit in state.active_permits] == ["permit-fault"]

    _reserve(store)
    replay = store.reserve(
        RUN_ID,
        operation_id="reserve-fault",
        permit_id="permit-fault",
        reservation=state.active_permits[0].reservation,
        lineage=_lineage(),
    )
    assert replay.status is MutationStatus.EXACT_REPLAY
    assert len(replay.state.active_permits) == 1


def test_fault_after_start_commit_is_effect_unknown_on_resume(
    store: DurableBudgetStore,
) -> None:
    _reserve(store)
    _fail_once(store, "current.after_replace")

    with pytest.raises(DurableBudgetError) as error:
        store.start(
            RUN_ID,
            operation_id="start-fault",
            permit_id="permit-fault",
        )

    assert error.value.failure.code is DurableFailureCode.DURABILITY_UNCERTAIN
    store.revisions.fault_injector = None
    state = store.load(RUN_ID)
    assert state.active_permits[0].status.value == "started"
    with pytest.raises(DurableBudgetError) as resume:
        store.resume(RUN_ID)
    assert resume.value.failure.code is DurableFailureCode.EFFECT_UNKNOWN
    assert resume.value.failure.effect_unknown is True


def test_fault_before_settlement_commit_keeps_started_permit(
    store: DurableBudgetStore,
) -> None:
    _reserve(store)
    store.start(RUN_ID, operation_id="start-fault", permit_id="permit-fault")
    _fail_once(store, "current.before_replace")

    with pytest.raises(DurableBudgetError) as error:
        store.settle(
            RUN_ID,
            operation_id="settle-fault",
            settlement_id="settlement-fault",
            permit_id="permit-fault",
            actual=ResourceAmountsV1(
                tool_attempts=1,
                tool_input_bytes=80,
                tool_output_bytes=90,
                run_bytes=90,
                concurrency_slots=1,
            ),
            status=SettlementStatus.SUCCEEDED,
            result_sha256=HASH,
            effect_evidence_sha256=HASH,
        )

    assert error.value.failure.code is DurableFailureCode.RECONCILIATION_REQUIRED
    clean = DurableBudgetStore(
        store.revisions.revision_root,
        store.revisions.legacy_runs_root,
        clock=lambda: 200,
        wall_clock=lambda: NOW,
    )
    state = clean.load(RUN_ID)
    assert state.settlements == ()
    assert state.active_permits[0].status.value == "started"


def test_fault_after_settlement_commit_replays_unique_settlement(
    store: DurableBudgetStore,
) -> None:
    _reserve(store)
    store.start(RUN_ID, operation_id="start-fault", permit_id="permit-fault")
    actual = ResourceAmountsV1(
        tool_attempts=1,
        tool_input_bytes=80,
        tool_output_bytes=90,
        run_bytes=90,
        concurrency_slots=1,
    )
    _fail_once(store, "current.after_replace")

    with pytest.raises(DurableBudgetError) as error:
        store.settle(
            RUN_ID,
            operation_id="settle-fault",
            settlement_id="settlement-fault",
            permit_id="permit-fault",
            actual=actual,
            status=SettlementStatus.SUCCEEDED,
            result_sha256=HASH,
            effect_evidence_sha256=HASH,
        )

    assert error.value.failure.code is DurableFailureCode.DURABILITY_UNCERTAIN
    store.revisions.fault_injector = None
    replay = store.settle(
        RUN_ID,
        operation_id="settle-fault",
        settlement_id="settlement-fault",
        permit_id="permit-fault",
        actual=actual,
        status=SettlementStatus.SUCCEEDED,
        result_sha256=HASH,
        effect_evidence_sha256=HASH,
    )
    assert replay.status is MutationStatus.EXACT_REPLAY
    assert len(replay.state.settlements) == 1


def test_fault_after_no_effect_release_replays_unique_release(
    store: DurableBudgetStore,
) -> None:
    _reserve(store)
    _fail_once(store, "current.after_replace")

    with pytest.raises(DurableBudgetError) as error:
        store.release_no_effect(
            RUN_ID,
            operation_id="release-fault",
            settlement_id="release-settlement-fault",
            permit_id="permit-fault",
            evidence_sha256=HASH,
        )

    assert error.value.failure.code is DurableFailureCode.DURABILITY_UNCERTAIN
    store.revisions.fault_injector = None
    replay = store.release_no_effect(
        RUN_ID,
        operation_id="release-fault",
        settlement_id="release-settlement-fault",
        permit_id="permit-fault",
        evidence_sha256=HASH,
    )
    assert replay.status is MutationStatus.EXACT_REPLAY
    assert replay.state.active_permits == ()
    assert len(replay.state.settlements) == 1
