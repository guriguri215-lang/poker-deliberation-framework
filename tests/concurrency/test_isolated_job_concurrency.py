from __future__ import annotations

import sys
import threading
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

if sys.platform != "win32":
    pytest.skip("Windows Job Object concurrency tests", allow_module_level=True)

from poker_deliberation.approval_models import ApprovalExecutionRecheckBindingV2
from poker_deliberation.budgets.durable_store import (
    DurableBudgetStore,
    initialize_durable_budget_root,
)
from poker_deliberation.isolated_jobs.coordinator import IsolatedJobCoordinator
from poker_deliberation.isolated_jobs.models import (
    ApprovalJobReferenceV1,
    IsolatedJobStatus,
    JobFailureCode,
    SyntheticArgumentsV1,
    SyntheticOperation,
)
from poker_deliberation.isolated_jobs.store import (
    IsolatedJobStore,
    initialize_isolated_job_root,
)
from poker_deliberation.isolated_jobs.windows_backend import WindowsJobBackend
from tests.isolated_job_support import (
    NOW,
    JobAuthority,
    context_for,
    durable_policy,
    limits,
    lineage_for,
    policy_for,
    request,
)

pytestmark = pytest.mark.skipif(
    __import__("sys").platform != "win32",
    reason="Windows Job Object integration",
)


class _Approved:
    def verify(
        self, **kwargs: Any
    ) -> tuple[
        ApprovalJobReferenceV1,
        ApprovalExecutionRecheckBindingV2,
    ]:
        request_id = str(kwargs["request_id"])
        return (
            ApprovalJobReferenceV1(
                approval_run_id=str(kwargs["approval_run_id"]),
                approval_revision=2,
                approval_pointer_sha256="1" * 64,
                approval_manifest_sha256="2" * 64,
                request_id=request_id,
            ),
            ApprovalExecutionRecheckBindingV2.model_construct(
                binding_sha256="3" * 64,
                valid_until=NOW + timedelta(minutes=30),
            ),
        )


class _CountingBackend(WindowsJobBackend):
    def __init__(self) -> None:
        self.count = 0
        self.lock = threading.Lock()

    def _prepare_with_lease(self, request_value, policy, lease):
        with self.lock:
            self.count += 1
        return super()._prepare_with_lease(request_value, policy, lease)


def _coordinator_fixture(
    tmp_path: Path,
    *,
    suffix: str,
    backend: WindowsJobBackend | None = None,
    operation: SyntheticOperation = SyntheticOperation.SUCCESS,
    arguments: SyntheticArgumentsV1 | None = None,
    wall_clock_ms: int = 3_000,
    budget_runtime_seconds: float | None = None,
):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    budget_root = tmp_path / "budget"
    job_root = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialize_durable_budget_root(
        budget_root,
        legacy,
        root_id="root-" + "8" * 32,
        initialized_at=NOW,
    )
    initialize_isolated_job_root(
        job_root,
        legacy,
        root_id="root-" + "9" * 32,
        initialized_at=NOW,
    )
    value = request(operation, suffix=suffix, arguments=arguments)
    budget = DurableBudgetStore(budget_root, legacy, wall_clock=lambda: NOW)
    budget_policy = durable_policy()
    if budget_runtime_seconds is not None:
        budget_policy = budget_policy.model_copy(
            update={
                "base_policy": budget_policy.base_policy.model_copy(
                    update={"max_runtime_seconds": budget_runtime_seconds}
                )
            }
        )
    budget.create(
        value.budget_run_id,
        budget_policy,
        operation_id=f"initialize-{suffix}",
    )
    job_store = IsolatedJobStore(job_root, legacy, clock=lambda: NOW)
    coordinator = IsolatedJobCoordinator(
        job_store,
        budget,
        terminal_store=object(),  # type: ignore[arg-type]
        backend=backend,
        clock=lambda: NOW,
    )
    coordinator.approvals = _Approved()  # type: ignore[assignment]
    assignment, envelope = context_for(value)
    lineage = lineage_for(value, envelope)
    policy = policy_for(
        workspace,
        job_limits=limits(wall_clock_ms=wall_clock_ms),
    )
    kwargs = {
        "context_envelope": envelope,
        "assignment": assignment,
        "budget_lineage": lineage,
        "action_expires_at": NOW + timedelta(minutes=30),
        "approval_run_id": f"Approval-{suffix}",
        "approval_request_id": f"request-{suffix}",
        "authority_provider": JobAuthority(),
    }
    return value, policy, coordinator, job_store, budget, kwargs


def test_concurrent_exact_execution_launches_only_one_child(tmp_path: Path) -> None:
    backend = _CountingBackend()
    value, policy, coordinator, _job_store, _budget, kwargs = _coordinator_fixture(
        tmp_path,
        suffix="concurrent",
        backend=backend,
    )
    barrier = threading.Barrier(3)
    results = []
    errors: list[Exception] = []

    def run() -> None:
        barrier.wait()
        try:
            results.append(coordinator.execute(value, policy, **kwargs))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=20)

    assert all(not thread.is_alive() for thread in threads)
    assert backend.count == 1
    assert [result.status for result in results] == [IsolatedJobStatus.COMPLETED]
    assert len(errors) == 1
    assert str(errors[0]) == JobFailureCode.RUN_LOCKED.value


def test_running_exact_duplicate_cannot_abort_first_callers_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _CountingBackend()
    value, policy, coordinator, job_store, _budget, kwargs = _coordinator_fixture(
        tmp_path,
        suffix="running-duplicate",
        backend=backend,
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=60_000),
        wall_clock_ms=30_000,
        budget_runtime_seconds=90.0,
    )
    running = threading.Event()
    cancel_first = threading.Event()
    first_results = []
    first_errors: list[BaseException] = []
    original_transition = job_store.transition

    def observe_running(*args: Any, **transition_kwargs: Any):
        result = original_transition(*args, **transition_kwargs)
        if transition_kwargs.get("status") is IsolatedJobStatus.RUNNING:
            running.set()
        return result

    monkeypatch.setattr(job_store, "transition", observe_running)

    def run_first() -> None:
        try:
            first_results.append(
                coordinator.execute(
                    value,
                    policy,
                    cancelled=cancel_first.is_set,
                    **kwargs,
                )
            )
        except BaseException as exc:
            first_errors.append(exc)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert running.wait(timeout=10)
    state = job_store.load(value.execution_id)
    assert state.process_id is not None
    assert state.process_creation_time_100ns is not None
    assert (
        WindowsJobBackend.process_identity_status(
            state.process_id,
            state.process_creation_time_100ns,
        )
        == "same_live_process"
    )

    with pytest.raises(ValueError, match=JobFailureCode.RUN_LOCKED.value):
        coordinator.execute(value, policy, **kwargs)
    assert (
        WindowsJobBackend.process_identity_status(
            state.process_id,
            state.process_creation_time_100ns,
        )
        == "same_live_process"
    )

    cancel_first.set()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert not first_errors
    assert [result.status for result in first_results] == [IsolatedJobStatus.CANCELLED]
    assert backend.count == 1


def test_cancel_is_durable_tree_wide_and_budget_settled(tmp_path: Path) -> None:
    wall_clock_ms = 30_000
    value, policy, coordinator, job_store, budget, kwargs = _coordinator_fixture(
        tmp_path,
        suffix="cancel",
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=60_000),
        wall_clock_ms=wall_clock_ms,
        budget_runtime_seconds=90.0,
    )

    result = coordinator.execute(
        value,
        policy,
        cancelled=lambda: True,
        **kwargs,
    )
    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)

    assert result.status is IsolatedJobStatus.CANCELLED
    assert result.failure_code is JobFailureCode.CANCELLED
    assert state.evidence is not None
    assert state.evidence.active_processes == 0
    assert state.evidence.process_tree_termination_confirmed is True
    assert [event.status for event in state.events][-2:] == [
        IsolatedJobStatus.CANCEL_REQUESTED,
        IsolatedJobStatus.CANCELLED,
    ]
    assert budget_state.settlements[-1].status.value == "cancelled"
    assert budget_state.cancellations[-1].state.value == "cancelled"


def test_concurrent_cancel_signal_race_reaches_one_closed_terminal_state(
    tmp_path: Path,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _coordinator_fixture(
        tmp_path,
        suffix="cancel-race",
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    cancellation = threading.Event()
    timer = threading.Timer(0.05, cancellation.set)
    timer.start()
    try:
        result = coordinator.execute(
            value,
            policy,
            cancelled=cancellation.is_set,
            **kwargs,
        )
    finally:
        timer.cancel()
        timer.join(timeout=1)

    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)
    assert result.status is IsolatedJobStatus.CANCELLED
    assert state.status is IsolatedJobStatus.CANCELLED
    assert state.evidence is not None
    assert state.evidence.active_processes == 0
    assert not budget_state.active_permits
    assert budget_state.settlements[-1].status.value == "cancelled"
