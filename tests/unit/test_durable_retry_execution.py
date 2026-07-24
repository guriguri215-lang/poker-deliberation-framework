"""Unit tests for durable retry and execution contracts."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.budgets.durable_models import (
    CancellationState,
    DurableBudgetPolicyV1,
    DurableFailureCode,
    ExecutionActivationV1,
    ExecutionLineageV1,
    OwnerKind,
    ResourceAmountsV1,
    ResourceReservationV1,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetError,
    DurableBudgetStore,
    build_resource_reservation,
    initialize_durable_budget_root,
)
from poker_deliberation.budgets.execution import (
    CooperativeCancellationToken,
    DurableBoundedExecutor,
    DurableExecutionTask,
    EffectResultV1,
    EffectStatus,
    IsolationRequirementV1,
    admit_automatic_retry,
    build_durable_retry_lineage,
)
from poker_deliberation.budgets.retry import (
    FailureCategory,
    IdempotencyStatus,
)
from poker_deliberation.context_lifecycle import (
    build_context_envelope,
    context_payload,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64


@pytest.fixture
def store() -> Generator[DurableBudgetStore, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2be-", dir=parent) as directory:
        base = Path(directory)
        legacy = base / "legacy"
        legacy.mkdir()
        revision = base / "revision"
        initialize_durable_budget_root(
            revision,
            legacy,
            root_id="root-" + "d" * 32,
            initialized_at=NOW,
        )
        tick = iter(range(100, 10_000))
        value = DurableBudgetStore(
            revision,
            legacy,
            clock=lambda: next(tick),
            wall_clock=lambda: NOW,
        )
        value.create(
            "Run-executor",
            DurableBudgetPolicyV1(
                base_policy=BudgetPolicyV2(
                    max_deliberation_rounds=4,
                    max_tool_retries=1,
                    max_provider_output_bytes=4_000,
                    max_tool_input_bytes=4_000,
                    max_tool_output_bytes=4_000,
                    max_artifact_bytes=4_000,
                    max_run_bytes=40_000,
                ),
                activation=ExecutionActivationV1(
                    max_concurrent_agents=2,
                    max_automatic_retries=1,
                ),
            ),
            operation_id="initialize-executor",
        )
        yield value


def _lineage(ordinal: int) -> ExecutionLineageV1:
    return ExecutionLineageV1(
        owner_kind=OwnerKind.TOOL,
        owner_id=f"owner-{ordinal}",
        role="calculator",
        phase_id="tool_research",
        assignment_id=f"assignment-{ordinal}",
        root_attempt_id=f"attempt-{ordinal}-0",
        attempt_id=f"attempt-{ordinal}-0",
        root_context_id=f"context-{ordinal}-0",
        context_id=f"context-{ordinal}-0",
        context_source_sha256=HASH,
        context_policy_sha256=HASH,
        context_integrity_sha256=HASH,
        execution_ordinal=ordinal,
        idempotency_key=f"effect-{ordinal}-0",
        idempotency_request_sha256=HASH,
    )


def _reservation(task_id: str) -> ResourceReservationV1:
    return build_resource_reservation(
        reservation_id=f"{task_id}.reservation-0",
        requested=ResourceAmountsV1(
            active_runtime_ns=100,
            tool_attempts=1,
            tool_input_bytes=100,
            tool_output_bytes=100,
            run_bytes=100,
            concurrency_slots=1,
        ),
    )


def _success(lineage: ExecutionLineageV1) -> EffectResultV1:
    return EffectResultV1(
        status=EffectStatus.SUCCEEDED,
        actual=ResourceAmountsV1(
            active_runtime_ns=80,
            tool_attempts=1,
            retry_attempts=1 if lineage.parent_attempt_id is not None else 0,
            tool_input_bytes=80,
            tool_output_bytes=80,
            run_bytes=80,
            concurrency_slots=1,
        ),
        result_sha256=hashlib_sha(f"result-{lineage.execution_ordinal}"),
        effect_evidence_sha256=hashlib_sha(f"effect-{lineage.execution_ordinal}"),
    )


def hashlib_sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_retry_admission_is_separate_strict_and_n_plus_one() -> None:
    admitted = admit_automatic_retry(
        category=FailureCategory.TOOL_TRANSIENT,
        idempotency=IdempotencyStatus.IDEMPOTENT,
        completed_retries=0,
        max_automatic_retries=1,
    )
    assert admitted.admitted
    assert admitted.max_attempts == 2

    for category in (
        FailureCategory.VALIDATION,
        FailureCategory.BUDGET,
        FailureCategory.DEADLINE,
        FailureCategory.CANCEL,
        FailureCategory.TOOL_DETERMINISTIC,
        FailureCategory.VERIFICATION,
        FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
    ):
        assert not admit_automatic_retry(
            category=category,
            idempotency=IdempotencyStatus.IDEMPOTENT,
            completed_retries=0,
            max_automatic_retries=1,
        ).admitted
    assert not admit_automatic_retry(
        category=FailureCategory.PROVIDER_TRANSIENT,
        idempotency=IdempotencyStatus.RECONCILABLE,
        completed_retries=0,
        max_automatic_retries=1,
    ).admitted


def test_bounded_executor_reduces_by_ordinal_and_records_peak_two(
    store: DurableBudgetStore,
) -> None:
    barrier = threading.Barrier(2)

    def effect(_token, lineage: ExecutionLineageV1) -> EffectResultV1:
        barrier.wait(timeout=2)
        return _success(lineage)

    tasks = tuple(
        DurableExecutionTask(
            task_id=f"task-{ordinal}",
            execution_ordinal=ordinal,
            reservation=_reservation(f"task-{ordinal}"),
            lineage=_lineage(ordinal),
            effect=effect,
        )
        for ordinal in (1, 0)
    )
    result = DurableBoundedExecutor(store, "Run-executor").execute(
        tasks,
        max_workers=2,
    )

    assert [record.execution_ordinal for record in result.records] == [0, 1]
    assert result.peak_concurrency == 2
    assert store.load("Run-executor").usage.peak_concurrency == 2
    assert not store.load("Run-executor").active_permits


def test_transient_idempotent_failure_retries_once_with_fresh_lineage(
    store: DurableBudgetStore,
) -> None:
    calls: list[str] = []

    def effect(_token, lineage: ExecutionLineageV1) -> EffectResultV1:
        calls.append(lineage.attempt_id)
        if len(calls) == 1:
            return EffectResultV1(
                status=EffectStatus.FAILED,
                actual=ResourceAmountsV1(
                    active_runtime_ns=40,
                    tool_attempts=1,
                    tool_input_bytes=40,
                    tool_output_bytes=40,
                    run_bytes=40,
                    concurrency_slots=1,
                ),
                effect_evidence_sha256=hashlib_sha("transient"),
                failure_category=FailureCategory.TOOL_TRANSIENT,
                idempotency=IdempotencyStatus.IDEMPOTENT,
            )
        return _success(lineage)

    def retry_lineage(
        parent: ExecutionLineageV1,
        attempt_index: int,
    ) -> ExecutionLineageV1:
        return ExecutionLineageV1(
            owner_kind=parent.owner_kind,
            owner_id=parent.owner_id,
            role=parent.role,
            phase_id=parent.phase_id,
            assignment_id=parent.assignment_id,
            root_attempt_id=parent.root_attempt_id,
            parent_attempt_id=parent.attempt_id,
            attempt_id=f"attempt-0-{attempt_index}",
            root_context_id=parent.root_context_id,
            parent_context_id=parent.context_id,
            context_id=f"context-0-{attempt_index}",
            context_source_sha256=parent.context_source_sha256,
            context_policy_sha256=parent.context_policy_sha256,
            context_integrity_sha256=hashlib_sha(f"integrity-{attempt_index}"),
            execution_ordinal=parent.execution_ordinal,
            idempotency_key=f"effect-0-{attempt_index}",
            idempotency_request_sha256=parent.idempotency_request_sha256,
        )

    task = DurableExecutionTask(
        task_id="retry-task",
        execution_ordinal=0,
        reservation=_reservation("retry-task"),
        lineage=_lineage(0),
        effect=effect,
        retry_lineage_factory=retry_lineage,
    )
    executor = DurableBoundedExecutor(store, "Run-executor")
    result = executor.execute((task,))
    replay = executor.execute((task,))

    assert calls == ["attempt-0-0", "attempt-0-1"]
    assert len(result.records[0].attempts) == 2
    assert result.records[0].final_status is EffectStatus.SUCCEEDED
    assert replay.records == result.records
    assert replay.peak_concurrency == result.peak_concurrency
    assert store.load("Run-executor").usage.retry_attempts == 1


def test_unconfirmed_cooperative_cancellation_is_not_success(
    store: DurableBudgetStore,
) -> None:
    release = threading.Event()
    cancel = threading.Event()
    cancel.set()

    def uncooperative(_token, lineage: ExecutionLineageV1) -> EffectResultV1:
        release.wait(timeout=2)
        return _success(lineage)

    task = DurableExecutionTask(
        task_id="cancel-task",
        execution_ordinal=0,
        reservation=_reservation("cancel-task"),
        lineage=_lineage(0),
        effect=uncooperative,
    )
    result = DurableBoundedExecutor(store, "Run-executor").execute(
        (task,),
        cancel_event=cancel,
        cancellation_grace_seconds=0,
    )
    release.set()

    assert result.cancellation_state is CancellationState.UNCONFIRMED
    assert result.records[0].final_status is EffectStatus.EFFECT_UNKNOWN
    state = store.load("Run-executor")
    assert state.failure_latch is not None
    assert state.failure_latch.code is DurableFailureCode.CANCEL_UNCONFIRMED
    assert state.active_permits


def test_acknowledged_cooperative_cancellation_settles_cancelled(
    store: DurableBudgetStore,
) -> None:
    cancel = threading.Event()
    cancel.set()

    def cooperative(
        token: CooperativeCancellationToken,
        lineage: ExecutionLineageV1,
    ) -> EffectResultV1:
        while not token.requested:
            threading.Event().wait(0.001)
        evidence = hashlib_sha("cooperative-cancel")
        token.acknowledge(evidence)
        return EffectResultV1(
            status=EffectStatus.CANCELLED,
            actual=ResourceAmountsV1(
                tool_attempts=1,
                tool_input_bytes=10,
                tool_output_bytes=10,
                run_bytes=10,
                concurrency_slots=1,
            ),
            cancellation_evidence_sha256=evidence,
            idempotency=IdempotencyStatus.IDEMPOTENT,
        )

    task = DurableExecutionTask(
        task_id="cooperative-cancel-task",
        execution_ordinal=0,
        reservation=_reservation("cooperative-cancel-task"),
        lineage=_lineage(0),
        effect=cooperative,
    )
    result = DurableBoundedExecutor(store, "Run-executor").execute(
        (task,),
        cancel_event=cancel,
        cancellation_grace_seconds=1,
    )

    assert result.cancellation_state is CancellationState.CANCELLED
    assert result.records[0].final_status is EffectStatus.CANCELLED
    state = store.load("Run-executor")
    assert state.cancellations[0].state is CancellationState.CANCELLED
    assert state.settlements[0].status.value == "cancelled"
    assert not state.active_permits
    assert state.failure_latch is None
    operation_ids = {operation.operation_id for operation in state.operations}
    assert "cooperative-cancel-task.cancel-acknowledged" in operation_ids
    assert "cooperative-cancel-task.cancel-completed" in operation_ids


def test_success_after_cancel_request_is_settled_effect_unknown(
    store: DurableBudgetStore,
) -> None:
    cancel = threading.Event()
    cancel.set()

    def ignores_cancel_but_finishes(
        token: CooperativeCancellationToken,
        lineage: ExecutionLineageV1,
    ) -> EffectResultV1:
        while not token.requested:
            threading.Event().wait(0.001)
        return _success(lineage)

    task = DurableExecutionTask(
        task_id="cancel-race-task",
        execution_ordinal=0,
        reservation=_reservation("cancel-race-task"),
        lineage=_lineage(0),
        effect=ignores_cancel_but_finishes,
    )
    result = DurableBoundedExecutor(store, "Run-executor").execute(
        (task,),
        cancel_event=cancel,
        cancellation_grace_seconds=1,
    )

    assert result.cancellation_state is CancellationState.EFFECT_UNKNOWN
    assert result.records[0].final_status is EffectStatus.EFFECT_UNKNOWN
    state = store.load("Run-executor")
    assert state.cancellations[0].state is CancellationState.EFFECT_UNKNOWN
    assert state.settlements[0].status.value == "effect_unknown"
    assert state.failure_latch is not None
    assert state.failure_latch.code is DurableFailureCode.EFFECT_UNKNOWN


def test_isolation_requirement_refuses_before_reservation(
    store: DurableBudgetStore,
) -> None:
    called = False

    def effect(_token, lineage: ExecutionLineageV1) -> EffectResultV1:
        nonlocal called
        called = True
        return _success(lineage)

    task = DurableExecutionTask(
        task_id="isolated-task",
        execution_ordinal=0,
        reservation=_reservation("isolated-task"),
        lineage=_lineage(0),
        effect=effect,
        isolation_requirement=IsolationRequirementV1(process_tree_termination=True),
    )
    with pytest.raises(DurableBudgetError) as refused:
        DurableBoundedExecutor(store, "Run-executor").execute((task,))
    assert refused.value.failure.code is DurableFailureCode.ISOLATION_REQUIRED
    assert not called
    assert not store.load("Run-executor").active_permits


def test_retry_context_helper_uses_fresh_existing_lifecycle_contract() -> None:
    context = AgentContext(
        kind="strategy",
        objective="review",
        strategy_text="compare lines",
    )
    assignment = AgentAssignment(
        assignment_id="assignment-context",
        agent_role="strategy_analyst",
        task="review",
        context_keys=sorted(context_payload(context)),
    )
    parent = build_context_envelope(
        context,
        assignment,
        run_id="Run-context",
        expires_at=NOW + timedelta(minutes=5),
        clock=lambda: NOW,
        context_id="context-parent",
        attempt_id="attempt-parent",
    )
    parent_lineage = ExecutionLineageV1(
        owner_kind=OwnerKind.AGENT,
        owner_id="owner-context",
        role="strategy_analyst",
        phase_id="analysis",
        assignment_id=assignment.assignment_id,
        root_attempt_id=parent.lineage.attempt_id,
        attempt_id=parent.lineage.attempt_id,
        root_context_id=parent.lineage.context_id,
        context_id=parent.lineage.context_id,
        context_source_sha256=parent.lineage.source_sha256,
        context_policy_sha256=parent.policy_sha256,
        context_integrity_sha256=parent.integrity_sha256,
        execution_ordinal=0,
        idempotency_key="context-effect-0",
        idempotency_request_sha256=HASH,
    )

    lineage, envelope = build_durable_retry_lineage(
        parent_lineage,
        parent,
        context,
        assignment,
        run_id="Run-context",
        expires_at=NOW + timedelta(minutes=5),
        clock=lambda: NOW + timedelta(seconds=1),
        context_id="context-retry",
        attempt_id="attempt-retry",
        idempotency_key="context-effect-1",
        idempotency_request_sha256=HASH,
    )

    assert lineage.parent_attempt_id == "attempt-parent"
    assert lineage.parent_context_id == "context-parent"
    assert envelope.canonical_payload == parent.canonical_payload
    assert lineage.context_source_sha256 == parent.lineage.source_sha256
