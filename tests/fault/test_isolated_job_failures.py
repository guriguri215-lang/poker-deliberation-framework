from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

if sys.platform != "win32":
    pytest.skip("Windows Job Object fault tests", allow_module_level=True)

from poker_deliberation.approval_canonical import action_digest_sha256
from poker_deliberation.approval_models import ApprovalExecutionRecheckBindingV2
from poker_deliberation.budgets.durable_models import (
    CancellationState,
    ExecutionLineageV1,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetStore,
    initialize_durable_budget_root,
)
from poker_deliberation.context_lifecycle import ContextEnvelope
from poker_deliberation.isolated_jobs import windows_backend as backend_module
from poker_deliberation.isolated_jobs.canonical import isolated_job_sha256
from poker_deliberation.isolated_jobs.coordinator import IsolatedJobCoordinator
from poker_deliberation.isolated_jobs.models import (
    ISOLATED_JOB_ARTIFACT_SCHEMA,
    ISOLATED_JOB_PRODUCER_ID,
    ISOLATED_JOB_PRODUCER_VERSION,
    ApprovalJobReferenceV1,
    DurableIsolatedJobStateV1,
    IsolatedJobError,
    IsolatedJobPolicyV1,
    IsolatedJobRequestV1,
    IsolatedJobStatus,
    JobEvidenceV1,
    JobFailureCode,
    ReconciliationReferenceV1,
    SyntheticArgumentsV1,
    SyntheticOperation,
)
from poker_deliberation.isolated_jobs.store import (
    IsolatedJobStore,
    _validate_successor,
    initialize_isolated_job_root,
)
from poker_deliberation.isolated_jobs.windows_backend import (
    PreparationLease,
    WindowsJobBackend,
    WindowsJobOutcome,
)
from poker_deliberation.schemas import AgentAssignment
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    build_inventory,
)
from poker_deliberation.storage.revision_models import RevisionPublishRequestV1
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
    reason="Windows-qualified isolated-job fault tests",
)


@dataclass(frozen=True)
class _PreparedFixture:
    value: IsolatedJobRequestV1
    policy: IsolatedJobPolicyV1
    job_store: IsolatedJobStore
    budget: DurableBudgetStore
    coordinator: IsolatedJobCoordinator
    assignment: AgentAssignment
    envelope: ContextEnvelope
    lineage: ExecutionLineageV1


class _AbsentBackend(WindowsJobBackend):
    @staticmethod
    def process_identity_status(
        process_id: int,
        creation_time_100ns: int,
    ) -> str:
        del process_id, creation_time_100ns
        return "absent"


class _DifferentProcessBackend(WindowsJobBackend):
    @staticmethod
    def process_identity_status(
        process_id: int,
        creation_time_100ns: int,
    ) -> str:
        del process_id, creation_time_100ns
        return "different_live_process"


class _SameLiveProcessBackend(WindowsJobBackend):
    @staticmethod
    def process_identity_status(
        process_id: int,
        creation_time_100ns: int,
    ) -> str:
        del process_id, creation_time_100ns
        return "same_live_process"


class _Approved:
    def verify(
        self,
        **kwargs: Any,
    ) -> tuple[ApprovalJobReferenceV1, ApprovalExecutionRecheckBindingV2]:
        return (
            ApprovalJobReferenceV1(
                approval_run_id=str(kwargs["approval_run_id"]),
                approval_revision=2,
                approval_pointer_sha256="1" * 64,
                approval_manifest_sha256="2" * 64,
                request_id=str(kwargs["request_id"]),
            ),
            ApprovalExecutionRecheckBindingV2.model_construct(
                binding_sha256="3" * 64,
                valid_until=NOW + timedelta(minutes=30),
            ),
        )


class _ChangingApproval(_Approved):
    def __init__(self) -> None:
        self.calls = 0

    def verify(
        self,
        **kwargs: Any,
    ) -> tuple[ApprovalJobReferenceV1, ApprovalExecutionRecheckBindingV2]:
        reference, binding = super().verify(**kwargs)
        self.calls += 1
        if self.calls == 2:
            reference = reference.model_copy(update={"approval_pointer_sha256": "4" * 64})
        return reference, binding


def _executable_with_publication_fault(
    tmp_path: Path,
    *,
    suffix: str,
    fail_on_current_replace: int,
    operation: SyntheticOperation = SyntheticOperation.SUCCESS,
    arguments: SyntheticArgumentsV1 | None = None,
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
        root_id="root-" + "b" * 32,
        initialized_at=NOW,
    )
    initialize_isolated_job_root(
        job_root,
        legacy,
        root_id="root-" + "c" * 32,
        initialized_at=NOW,
    )
    value = request(operation, suffix=suffix, arguments=arguments)
    budget = DurableBudgetStore(budget_root, legacy, wall_clock=lambda: NOW)
    budget.create(
        value.budget_run_id,
        durable_policy(),
        operation_id=f"initialize-{suffix}",
    )
    current_replace_count = 0
    fired = False

    def fault(hook: str) -> None:
        nonlocal current_replace_count, fired
        if hook != "current.before_replace":
            return
        current_replace_count += 1
        if current_replace_count == fail_on_current_replace and not fired:
            fired = True
            raise OSError("synthetic publication window")

    job_store = IsolatedJobStore(
        job_root,
        legacy,
        clock=lambda: NOW,
        fault_injector=fault,
    )
    coordinator = IsolatedJobCoordinator(
        job_store,
        budget,
        terminal_store=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    coordinator.approvals = _Approved()  # type: ignore[assignment]
    assignment, envelope = context_for(value)
    kwargs = {
        "context_envelope": envelope,
        "assignment": assignment,
        "budget_lineage": lineage_for(value, envelope),
        "action_expires_at": NOW + timedelta(minutes=30),
        "approval_run_id": f"Approval-{suffix}",
        "approval_request_id": f"request-{suffix}",
        "authority_provider": JobAuthority(),
    }
    return (
        value,
        policy_for(workspace),
        coordinator,
        job_store,
        budget,
        kwargs,
    )


def _prepared(
    tmp_path: Path,
    *,
    suffix: str,
    operation: SyntheticOperation = SyntheticOperation.SUCCESS,
    input_bytes: bytes | None = None,
) -> _PreparedFixture:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    budget_root = tmp_path / "budget"
    job_root = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    approved_input = None
    if input_bytes is not None:
        approved_input = workspace / "approved-input.txt"
        approved_input.write_bytes(input_bytes)
    initialize_durable_budget_root(
        budget_root,
        legacy,
        root_id="root-" + "5" * 32,
        initialized_at=NOW,
    )
    initialize_isolated_job_root(
        job_root,
        legacy,
        root_id="root-" + "6" * 32,
        initialized_at=NOW,
    )
    budget = DurableBudgetStore(budget_root, legacy, wall_clock=lambda: NOW)
    value = request(operation, suffix=suffix)
    budget.create(
        value.budget_run_id,
        durable_policy(),
        operation_id=f"initialize-{suffix}",
    )
    assignment, envelope = context_for(value)
    lineage = lineage_for(value, envelope)
    policy = policy_for(workspace, approved_input=approved_input)
    job_store = IsolatedJobStore(job_root, legacy, clock=lambda: NOW)
    coordinator = IsolatedJobCoordinator(
        job_store,
        budget,
        terminal_store=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    preview = coordinator.preview(
        value,
        policy,
        context_envelope=envelope,
        assignment=assignment,
        budget_lineage=lineage,
        action_expires_at=NOW + timedelta(minutes=30),
    )
    budget.reserve(
        value.budget_run_id,
        operation_id=f"reserve-{suffix}",
        permit_id=value.budget_permit_id,
        reservation=preview.reservation,
        lineage=lineage,
    )
    job_store.create(
        value,
        policy,
        action_digest_sha256=action_digest_sha256(preview.action_plan),
        context_binding=preview.context_binding,
        budget_binding=preview.budget_binding,
        approval_reference=ApprovalJobReferenceV1(
            approval_run_id=f"Approval-{suffix}",
            approval_revision=2,
            approval_pointer_sha256="1" * 64,
            approval_manifest_sha256="2" * 64,
            request_id=f"request-{suffix}",
        ),
        approval_recheck_binding_sha256="3" * 64,
    )
    return _PreparedFixture(
        value=value,
        policy=policy,
        job_store=job_store,
        budget=budget,
        coordinator=coordinator,
        assignment=assignment,
        envelope=envelope,
        lineage=lineage,
    )


def test_restart_latches_effect_unknown_then_manual_reconciliation_is_non_success(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="restart")
    value = fixture.value

    recovered = fixture.coordinator.recover_after_restart(
        value,
        fixture.policy,
    )
    reconciled = fixture.coordinator.reconcile(
        value.execution_id,
        reconciliation_reference=ReconciliationReferenceV1(
            reference_id="operator-confirmed-no-live-process",
            evidence_sha256="7" * 64,
        ),
    )
    budget_state = fixture.budget.load(value.budget_run_id)

    assert recovered.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert recovered.failure_code is JobFailureCode.EFFECT_UNKNOWN
    assert reconciled.status is IsolatedJobStatus.RECONCILED
    assert reconciled.failure_code is JobFailureCode.RECONCILIATION_REQUIRED
    assert budget_state.settlements[-1].status.value == "released_no_effect"
    with pytest.raises(IsolatedJobError) as stale:
        fixture.job_store.transition(
            value.execution_id,
            status=IsolatedJobStatus.COMPLETED,
            reason_code="forbidden-promotion",
        )
    assert stale.value.code is JobFailureCode.STALE_REPLAY


def test_restart_after_launch_commit_releases_reserved_no_effect(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="restart-launch")
    value = fixture.value
    fixture.coordinator.backend = _DifferentProcessBackend()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=123,
        process_creation_time_100ns=456,
    )

    recovered = fixture.coordinator.recover_after_restart(value, fixture.policy)
    budget_state = fixture.budget.load(value.budget_run_id)

    assert recovered.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert budget_state.settlements[-1].status.value == "released_no_effect"
    assert not budget_state.active_permits


def test_restart_after_resume_settles_started_permit_as_effect_unknown(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="restart-running")
    value = fixture.value
    fixture.coordinator.backend = _AbsentBackend()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=321,
        process_creation_time_100ns=654,
    )
    fixture.budget.start(
        value.budget_run_id,
        operation_id="start-restart-running",
        permit_id=value.budget_permit_id,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="9" * 64,
    )

    recovered = fixture.coordinator.recover_after_restart(value, fixture.policy)
    budget_state = fixture.budget.load(value.budget_run_id)

    assert recovered.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert budget_state.settlements[-1].status.value == "effect_unknown"
    assert budget_state.settlements[-1].actual.tool_attempts == 1
    assert not budget_state.active_permits


def test_effect_unknown_recovery_refuses_to_close_live_process_permit(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="effect-unknown-live-process")
    value = fixture.value
    fixture.coordinator.backend = _SameLiveProcessBackend()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=321,
        process_creation_time_100ns=654,
    )
    fixture.budget.start(
        value.budget_run_id,
        operation_id="start-effect-unknown-live-process",
        permit_id=value.budget_permit_id,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="9" * 64,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.EFFECT_UNKNOWN,
        reason_code="controller_restart",
        failure_code=JobFailureCode.EFFECT_UNKNOWN,
    )

    with pytest.raises(ValueError, match=JobFailureCode.RUN_LOCKED.value):
        fixture.coordinator.recover_after_restart(value, fixture.policy)

    budget_state = fixture.budget.load(value.budget_run_id)
    assert any(permit.permit_id == value.budget_permit_id for permit in budget_state.active_permits)
    assert not budget_state.settlements


def test_corrupt_current_payload_is_rejected_before_state_use(tmp_path: Path) -> None:
    fixture = _prepared(tmp_path, suffix="tamper")
    value = fixture.value
    current = fixture.job_store.revisions.read_current(value.execution_id)
    revision = current.reachable_history[0]
    payload = (
        fixture.job_store.revisions.runs_root
        / value.execution_id
        / ".revision-store"
        / revision.revision_relative_path
        / "payload"
        / "isolated_job_state.json"
    )
    data = payload.read_bytes()
    payload.write_bytes(data[:-1] + (b" " if data[-1:] != b" " else b"\n"))

    with pytest.raises(IsolatedJobError) as corrupt:
        fixture.job_store.load(value.execution_id)
    assert corrupt.value.code is JobFailureCode.STORAGE_FAILURE


def test_partial_successor_write_does_not_advance_current(tmp_path: Path) -> None:
    fixture = _prepared(tmp_path, suffix="partial")
    value = fixture.value
    fired = False

    def fault(hook: str) -> None:
        nonlocal fired
        if hook == "current.before_replace" and not fired:
            fired = True
            raise OSError("synthetic partial publication")

    failing = IsolatedJobStore(
        fixture.job_store.revisions.revision_root,
        fixture.job_store.revisions.legacy_runs_root,
        clock=lambda: NOW,
        fault_injector=fault,
    )
    with pytest.raises(IsolatedJobError) as failed:
        failing.transition(
            value.execution_id,
            status=IsolatedJobStatus.FAILED,
            reason_code="synthetic-fault",
            failure_code=JobFailureCode.STORAGE_FAILURE,
        )

    assert failed.value.code is JobFailureCode.STORAGE_FAILURE
    assert fixture.job_store.load(value.execution_id).status is IsolatedJobStatus.PREPARED


def test_launch_commit_publication_fault_terminates_suspended_child_and_releases_budget(
    tmp_path: Path,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="fault-launch-commit",
        fail_on_current_replace=2,
    )

    with pytest.raises(ValueError, match=JobFailureCode.STORAGE_FAILURE.value):
        coordinator.execute(value, policy, **kwargs)
    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)

    assert state.status is IsolatedJobStatus.FAILED
    assert state.failure_code is JobFailureCode.STORAGE_FAILURE
    assert budget_state.settlements[-1].status.value == "released_no_effect"
    assert not budget_state.active_permits


def test_effect_admission_approval_change_terminates_before_resume(
    tmp_path: Path,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="approval-change",
        fail_on_current_replace=99,
    )
    coordinator.approvals = _ChangingApproval()  # type: ignore[assignment]

    with pytest.raises(ValueError, match=JobFailureCode.APPROVAL_MISMATCH.value):
        coordinator.execute(value, policy, **kwargs)
    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)

    assert state.status is IsolatedJobStatus.FAILED
    assert state.failure_code is JobFailureCode.APPROVAL_MISMATCH
    assert state.process_id is not None
    assert budget_state.settlements[-1].status.value == "released_no_effect"
    assert not budget_state.active_permits


def test_effect_admission_controller_abort_hard_stops_and_rethrows(
    tmp_path: Path,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="approval-controller-abort",
        fail_on_current_replace=99,
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )

    class InterruptSecondApproval(_Approved):
        def __init__(self) -> None:
            self.calls = 0

        def verify(self, **verify_kwargs: Any):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt("synthetic approval controller abort")
            return super().verify(**verify_kwargs)

    coordinator.approvals = InterruptSecondApproval()  # type: ignore[assignment]
    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(value, policy, **kwargs)

    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)
    assert state.status is IsolatedJobStatus.FAILED
    assert state.failure_code is JobFailureCode.INTERNAL_INVARIANT_ERROR
    assert state.process_id is not None
    assert (
        WindowsJobBackend.process_identity_status(
            state.process_id,
            state.process_creation_time_100ns or 0,
        )
        == "absent"
    )
    assert budget_state.settlements[-1].status.value == "released_no_effect"
    assert budget_state.settlements[-1].actual.tool_attempts == 0
    assert not budget_state.active_permits


def test_prepare_return_handoff_abort_is_closed_by_preexisting_lease(
    tmp_path: Path,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="prepare-return-handoff-abort",
        fail_on_current_replace=99,
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )

    class InterruptReturnBackend(WindowsJobBackend):
        process_id: int | None = None
        creation_time: int | None = None
        lease: PreparationLease | None = None

        def _prepare_with_lease(
            self,
            request_value: IsolatedJobRequestV1,
            policy_value: IsolatedJobPolicyV1,
            lease: PreparationLease,
        ):
            self.lease = lease
            prepared = super()._prepare_with_lease(
                request_value,
                policy_value,
                lease,
            )
            self.process_id = prepared.process_id
            self.creation_time = prepared.creation_time_100ns
            raise KeyboardInterrupt("synthetic prepare return handoff abort")

    backend = InterruptReturnBackend()
    coordinator.backend = backend
    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(value, policy, **kwargs)

    assert backend.process_id is not None
    assert backend.creation_time is not None
    assert (
        WindowsJobBackend.process_identity_status(
            backend.process_id,
            backend.creation_time,
        )
        == "absent"
    )
    assert backend.lease is not None
    assert backend.lease._released is True
    assert backend.lease._prepared is None
    assert job_store.load(value.execution_id).status is IsolatedJobStatus.FAILED
    assert not budget.load(value.budget_run_id).active_permits


def test_kernel_boundary_expiry_reports_approval_mismatch_before_resume(
    tmp_path: Path,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="kernel-boundary-expiry",
        fail_on_current_replace=99,
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    clock_calls = 0

    def expiring_clock():
        nonlocal clock_calls
        clock_calls += 1
        return NOW if clock_calls < 4 else NOW + timedelta(minutes=30)

    coordinator.clock = expiring_clock
    with pytest.raises(ValueError, match=JobFailureCode.APPROVAL_MISMATCH.value):
        coordinator.execute(value, policy, **kwargs)

    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)
    assert clock_calls == 4
    assert state.status is IsolatedJobStatus.FAILED
    assert state.failure_code is JobFailureCode.APPROVAL_MISMATCH
    assert state.process_id is not None
    assert (
        WindowsJobBackend.process_identity_status(
            state.process_id,
            state.process_creation_time_100ns or 0,
        )
        == "absent"
    )
    assert budget_state.settlements[-1].actual.tool_attempts == 1
    assert budget_state.settlements[-1].status.value == "failed"
    assert not budget_state.active_permits
    invalid_payload = state.model_dump(mode="python")
    invalid_payload["evidence"]["output_complete"] = False
    with pytest.raises(ValueError, match="known-no-effect failure"):
        DurableIsolatedJobStateV1.model_validate(invalid_payload)


def test_kernel_expiry_with_incomplete_output_evidence_keeps_started_permit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="kernel-expiry-incomplete-output",
        fail_on_current_replace=99,
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    clock_calls = 0
    original_snapshot = backend_module._OutputBudget.snapshot

    def expiring_clock():
        nonlocal clock_calls
        clock_calls += 1
        return NOW if clock_calls < 4 else NOW + timedelta(minutes=30)

    def incomplete_snapshot(output_budget: object):
        stdout, stderr, overflow, _reader_error = original_snapshot(
            output_budget  # type: ignore[arg-type]
        )
        return stdout, stderr, overflow, True

    coordinator.clock = expiring_clock
    monkeypatch.setattr(backend_module._OutputBudget, "snapshot", incomplete_snapshot)
    with pytest.raises(ValueError, match=JobFailureCode.APPROVAL_MISMATCH.value):
        coordinator.execute(value, policy, **kwargs)

    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)
    assert state.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert state.failure_code is JobFailureCode.EFFECT_UNKNOWN
    assert state.evidence is None
    assert any(permit.permit_id == value.budget_permit_id for permit in budget_state.active_permits)
    assert not budget_state.settlements


def test_resume_rechecks_identity_after_second_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="identity-after-approval",
        fail_on_current_replace=99,
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    original_verify = backend_module.verify_execution_identity
    identity_checks = 0

    def fail_fourth_identity(identity: object) -> None:
        nonlocal identity_checks
        identity_checks += 1
        if identity_checks == 4:
            raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH)
        original_verify(identity)  # type: ignore[arg-type]

    monkeypatch.setattr(backend_module, "verify_execution_identity", fail_fourth_identity)
    with pytest.raises(ValueError, match=JobFailureCode.IDENTITY_MISMATCH.value):
        coordinator.execute(value, policy, **kwargs)

    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)
    assert identity_checks == 4
    assert state.status is IsolatedJobStatus.FAILED
    assert state.failure_code is JobFailureCode.IDENTITY_MISMATCH
    assert state.process_id is not None
    assert (
        WindowsJobBackend.process_identity_status(
            state.process_id,
            state.process_creation_time_100ns or 0,
        )
        == "absent"
    )
    assert budget_state.settlements[-1].actual.tool_attempts == 1
    assert budget_state.settlements[-1].status.value == "failed"
    assert not budget_state.active_permits


def test_running_publication_controller_abort_hard_stops_and_rethrows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="running-controller-abort",
        fail_on_current_replace=99,
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    original_transition = job_store.transition
    fired = False

    def interrupt_running(*args: Any, **transition_kwargs: Any):
        nonlocal fired
        if transition_kwargs.get("status") is IsolatedJobStatus.RUNNING and not fired:
            fired = True
            raise KeyboardInterrupt("synthetic running publication abort")
        return original_transition(*args, **transition_kwargs)

    monkeypatch.setattr(job_store, "transition", interrupt_running)
    with pytest.raises(KeyboardInterrupt):
        coordinator.execute(value, policy, **kwargs)

    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)
    assert fired is True
    assert state.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert state.process_id is not None
    assert (
        WindowsJobBackend.process_identity_status(
            state.process_id,
            state.process_creation_time_100ns or 0,
        )
        == "absent"
    )
    assert budget_state.settlements[-1].actual.tool_attempts == 1
    assert not budget_state.active_permits


def test_running_publication_fault_hard_stops_and_settles_effect_unknown(
    tmp_path: Path,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="fault-running",
        fail_on_current_replace=3,
    )

    with pytest.raises(ValueError, match=JobFailureCode.EFFECT_UNKNOWN.value):
        coordinator.execute(value, policy, **kwargs)
    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)

    assert state.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert state.failure_code is JobFailureCode.EFFECT_UNKNOWN
    assert budget_state.settlements[-1].status.value == "effect_unknown"
    assert not budget_state.active_permits


def test_terminal_publication_fault_never_returns_success(tmp_path: Path) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="fault-terminal",
        fail_on_current_replace=4,
    )

    result = coordinator.execute(value, policy, **kwargs)
    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)

    assert result.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert result.failure_code is JobFailureCode.EFFECT_UNKNOWN
    assert state.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert budget_state.settlements[-1].status.value == "succeeded"


def test_post_replace_effect_unknown_is_returned_only_after_exact_confirmation(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="postreplace-confirmed")
    fired = False

    def fault(hook: str) -> None:
        nonlocal fired
        if hook == "current.after_replace" and not fired:
            fired = True
            raise OSError("synthetic post-replace uncertainty")

    fixture.job_store.revisions.fault_injector = fault
    state = fixture.job_store.transition(
        fixture.value.execution_id,
        status=IsolatedJobStatus.FAILED,
        reason_code="confirmed_postreplace",
        failure_code=JobFailureCode.CHILD_EXIT_NONZERO,
    )

    assert fired is True
    assert state.status is IsolatedJobStatus.FAILED
    assert fixture.job_store.load(fixture.value.execution_id) == state


def test_current_advanced_lineage_read_fault_uses_exact_confirmation(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="lineage-confirmed")
    reread_count = 0
    fired = False

    def fault(hook: str) -> None:
        nonlocal reread_count, fired
        if hook == "reconciliation.lineage.current.before_reread":
            reread_count += 1
            if reread_count == 1 and not fired:
                fired = True
                raise OSError("synthetic lineage reread uncertainty")

    fixture.job_store.revisions.fault_injector = fault
    state = fixture.job_store.transition(
        fixture.value.execution_id,
        status=IsolatedJobStatus.FAILED,
        reason_code="confirmed_lineage_reread",
        failure_code=JobFailureCode.STORAGE_FAILURE,
    )

    assert fired is True
    assert state.status is IsolatedJobStatus.FAILED
    assert fixture.job_store.load(fixture.value.execution_id) == state


def test_corrupt_post_replace_state_is_never_confirmed_as_success(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="postreplace-corrupt")
    current_path = (
        fixture.job_store.revisions.runs_root
        / fixture.value.execution_id
        / ".revision-store"
        / "current.json"
    )

    def fault(hook: str) -> None:
        if hook == "current.after_replace":
            current_path.write_bytes(b"{}\n")
            raise OSError("synthetic corrupt post-replace state")

    fixture.job_store.revisions.fault_injector = fault
    with pytest.raises(IsolatedJobError) as rejected:
        fixture.job_store.transition(
            fixture.value.execution_id,
            status=IsolatedJobStatus.FAILED,
            reason_code="corrupt_postreplace",
            failure_code=JobFailureCode.CHILD_EXIT_NONZERO,
        )
    assert rejected.value.code is JobFailureCode.STORAGE_FAILURE


def test_lower_revision_admission_rejects_output_without_state_evidence(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="lower-output-binding")
    state = fixture.job_store.load(fixture.value.execution_id)
    request_value = RevisionPublishRequestV1(
        run_id=state.execution_id,
        transaction_id="txn-" + "7" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id=ISOLATED_JOB_PRODUCER_ID,
        producer_version=ISOLATED_JOB_PRODUCER_VERSION,
        artifacts=(
            fixture.job_store._artifact(
                state,
                logical_name="isolated_job_state.json",
                data=state.canonical_bytes,
                schema=ISOLATED_JOB_ARTIFACT_SCHEMA,
                origin_kind="isolated_job_state",
                serialization="poker-run-storage-json-v1",
                media_type="application/json",
            ),
            fixture.job_store._artifact(
                state,
                logical_name="stdout.txt",
                data=b"unbound\n",
                schema="poker-isolated-job-stdout-artifact-v1",
                origin_kind="isolated_job_stdout",
                serialization="poker-run-storage-utf8-text-v1",
                media_type="text/plain",
            ),
            fixture.job_store._artifact(
                state,
                logical_name="stderr.txt",
                data=b"",
                schema="poker-isolated-job-stderr-artifact-v1",
                origin_kind="isolated_job_stderr",
                serialization="poker-run-storage-utf8-text-v1",
                media_type="text/plain",
            ),
        ),
    )

    with pytest.raises(CanonicalStorageError, match="requires exact process evidence"):
        build_inventory(request_value, max_artifact_bytes=70 * 1024 * 1024)


@pytest.mark.parametrize("omitted_kind", ["context", "budget_policy"])
def test_lower_revision_requires_exact_isolated_provenance(
    tmp_path: Path,
    omitted_kind: str,
) -> None:
    fixture = _prepared(tmp_path, suffix=f"lower-provenance-{omitted_kind}")
    state = fixture.job_store.load(fixture.value.execution_id)
    artifacts = (
        fixture.job_store._artifact(
            state,
            logical_name="isolated_job_state.json",
            data=state.canonical_bytes,
            schema=ISOLATED_JOB_ARTIFACT_SCHEMA,
            origin_kind="isolated_job_state",
            serialization="poker-run-storage-json-v1",
            media_type="application/json",
        ),
        fixture.job_store._artifact(
            state,
            logical_name="stdout.txt",
            data=b"",
            schema="poker-isolated-job-stdout-artifact-v1",
            origin_kind="isolated_job_stdout",
            serialization="poker-run-storage-utf8-text-v1",
            media_type="text/plain",
        ),
        fixture.job_store._artifact(
            state,
            logical_name="stderr.txt",
            data=b"",
            schema="poker-isolated-job-stderr-artifact-v1",
            origin_kind="isolated_job_stderr",
            serialization="poker-run-storage-utf8-text-v1",
            media_type="text/plain",
        ),
    )
    mutated = tuple(
        artifact.model_copy(
            update={
                "provenance_bindings": tuple(
                    binding
                    for binding in artifact.provenance_bindings
                    if binding.kind != omitted_kind
                )
            }
        )
        for artifact in artifacts
    )
    request_value = RevisionPublishRequestV1(
        run_id=state.execution_id,
        transaction_id="txn-" + ("8" if omitted_kind == "context" else "9") * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id=ISOLATED_JOB_PRODUCER_ID,
        producer_version=ISOLATED_JOB_PRODUCER_VERSION,
        artifacts=mutated,
    )

    with pytest.raises(CanonicalStorageError, match="exact local/context/budget provenance"):
        build_inventory(request_value, max_artifact_bytes=70 * 1024 * 1024)


def test_wait_accounting_exception_terminates_and_returns_effect_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="accounting-exception",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    with WindowsJobBackend().prepare(
        value,
        policy_for(tmp_path, job_limits=limits(wall_clock_ms=2_000)),
    ) as prepared:
        prepared.resume()

        def fail_accounting(_job: int):
            raise OSError("synthetic accounting failure")

        monkeypatch.setattr(backend_module, "_query_accounting", fail_accounting)
        outcome = prepared.wait()

    assert outcome.failure_code is JobFailureCode.EFFECT_UNKNOWN
    assert outcome.evidence.output_complete is False
    assert prepared._closed is True


def test_pipe_read_error_is_not_treated_as_complete_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="pipe-read-error",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    with WindowsJobBackend().prepare(
        value,
        policy_for(tmp_path, job_limits=limits(wall_clock_ms=2_000)),
    ) as prepared:
        target_fds = {prepared.stdout_read_fd, prepared.stderr_read_fd}
        original_read = backend_module.os.read

        def fail_target_read(file_descriptor: int, size: int) -> bytes:
            if file_descriptor in target_fds:
                raise OSError("synthetic pipe read failure")
            return original_read(file_descriptor, size)

        monkeypatch.setattr(backend_module.os, "read", fail_target_read)
        prepared.resume()
        outcome = prepared.wait()

    assert outcome.failure_code is JobFailureCode.EFFECT_UNKNOWN
    assert outcome.evidence.output_complete is False


def test_resume_identity_failure_terminates_without_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="resume-identity",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    with WindowsJobBackend().prepare(value, policy_for(tmp_path)) as prepared:

        def fail_identity(_identity: object) -> None:
            raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH)

        monkeypatch.setattr(backend_module, "verify_execution_identity", fail_identity)
        with pytest.raises(IsolatedJobError) as rejected:
            prepared.resume()
    assert rejected.value.code is JobFailureCode.IDENTITY_MISMATCH
    assert prepared._resumed is False
    assert prepared._closed is True


def test_resume_rechecks_identity_after_reader_start_before_kernel_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="reader-start-identity-order",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    with WindowsJobBackend().prepare(value, policy_for(tmp_path)) as prepared:
        original_start_readers = prepared._start_readers
        original_verify = backend_module.verify_execution_identity
        identity_invalid = False
        resume_called = False

        def mutate_after_reader_start() -> None:
            nonlocal identity_invalid
            original_start_readers()
            identity_invalid = True

        def reject_mutated_identity(identity: object) -> None:
            if identity_invalid:
                raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH)
            original_verify(identity)  # type: ignore[arg-type]

        def observe_resume(_handle: object) -> int:
            nonlocal resume_called
            resume_called = True
            return 0

        monkeypatch.setattr(prepared, "_start_readers", mutate_after_reader_start)
        monkeypatch.setattr(
            backend_module,
            "verify_execution_identity",
            reject_mutated_identity,
        )
        monkeypatch.setattr(backend_module._kernel32, "ResumeThread", observe_resume)

        with pytest.raises(IsolatedJobError) as rejected:
            prepared.resume()

    assert rejected.value.code is JobFailureCode.IDENTITY_MISMATCH
    assert resume_called is False
    assert prepared.resume_effect_possible is False
    assert prepared._resumed is False
    assert prepared._closed is True
    assert (
        WindowsJobBackend.process_identity_status(
            prepared.process_id,
            prepared.creation_time_100ns,
        )
        == "absent"
    )


def test_resume_expiry_at_kernel_boundary_terminates_without_effect(
    tmp_path: Path,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="resume-expiry",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    with WindowsJobBackend().prepare(value, policy_for(tmp_path)) as prepared:
        prepared.verify_identity_before_admission()

        with pytest.raises(IsolatedJobError) as rejected:
            prepared.resume(
                approval_valid_until=NOW,
                clock=lambda: NOW,
            )

    assert rejected.value.code is JobFailureCode.APPROVAL_MISMATCH
    assert prepared._resumed is False
    assert prepared._closed is True
    assert (
        WindowsJobBackend.process_identity_status(
            prepared.process_id,
            prepared.creation_time_100ns,
        )
        == "absent"
    )


def test_prepare_factory_acquires_no_process_before_context_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="prepare-context-factory",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    create_job_called = False
    original_create_job = backend_module._kernel32.CreateJobObjectW

    def observe_create_job(*args: Any) -> object:
        nonlocal create_job_called
        create_job_called = True
        return original_create_job(*args)

    monkeypatch.setattr(
        backend_module._kernel32,
        "CreateJobObjectW",
        observe_create_job,
    )
    try:
        preparation = WindowsJobBackend().prepare(value, policy_for(tmp_path))
        raise KeyboardInterrupt("synthetic caller abort before context entry")
    except KeyboardInterrupt:
        pass

    assert preparation._prepared is None
    assert preparation._lease._worker is None
    assert create_job_called is False


def test_atomic_job_create_controller_abort_terminates_assigned_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="atomic-create-controller-abort",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    observed: dict[str, int] = {}
    original_create_job = backend_module._kernel32.CreateJobObjectW
    original_create_process = backend_module._kernel32.CreateProcessW

    def capture_job(*args: Any) -> object:
        handle = original_create_job(*args)
        observed["job_handle"] = int(handle)
        return handle

    def interrupt_after_create(*args: Any) -> bool:
        created = bool(original_create_process(*args))
        if not created:
            return False
        process_information = backend_module.ctypes.cast(
            args[-1],
            backend_module.ctypes.POINTER(backend_module._PROCESS_INFORMATION),
        ).contents
        observed["process_id"] = int(process_information.dwProcessId)
        observed["creation_time"] = backend_module._process_creation_time(
            int(process_information.hProcess)
        )
        observed["active_processes"] = int(
            backend_module._query_accounting(observed["job_handle"]).BasicInfo.ActiveProcesses
        )
        raise KeyboardInterrupt("synthetic post-kernel-create abort")

    monkeypatch.setattr(backend_module._kernel32, "CreateJobObjectW", capture_job)
    monkeypatch.setattr(backend_module._kernel32, "CreateProcessW", interrupt_after_create)
    with (
        pytest.raises(KeyboardInterrupt),
        WindowsJobBackend().prepare(value, policy_for(tmp_path)),
    ):
        pass

    assert observed["active_processes"] == 1
    assert (
        WindowsJobBackend.process_identity_status(
            observed["process_id"],
            observed["creation_time"],
        )
        == "absent"
    )


@pytest.mark.parametrize("fault_phase", ["before", "after"])
def test_attribute_list_delete_abort_terminates_created_job_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_phase: str,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix=f"attribute-delete-{fault_phase}-controller-abort",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    observed: dict[str, int] = {}
    original_create_process = backend_module._kernel32.CreateProcessW
    original_delete = backend_module._DELETE_PROC_THREAD_ATTRIBUTE_LIST
    delete_calls = 0

    def capture_process(*args: Any) -> bool:
        created = bool(original_create_process(*args))
        if created:
            process_information = backend_module.ctypes.cast(
                args[-1],
                backend_module.ctypes.POINTER(
                    backend_module._PROCESS_INFORMATION,
                ),
            ).contents
            observed["process_id"] = int(process_information.dwProcessId)
            observed["creation_time"] = backend_module._process_creation_time(
                int(process_information.hProcess)
            )
            observed["process_handle"] = int(process_information.hProcess)
            observed["thread_handle"] = int(process_information.hThread)
        return created

    def count_delete(attribute_list: object) -> None:
        nonlocal delete_calls
        delete_calls += 1
        original_delete(attribute_list)

    def interrupt_at_checkpoint(phase: str) -> None:
        if phase == fault_phase:
            raise KeyboardInterrupt(f"synthetic attribute-list {phase} delete abort")

    monkeypatch.setattr(
        backend_module._kernel32,
        "CreateProcessW",
        capture_process,
    )
    monkeypatch.setattr(
        backend_module,
        "_DELETE_PROC_THREAD_ATTRIBUTE_LIST",
        count_delete,
    )
    monkeypatch.setattr(
        backend_module,
        "_attribute_list_delete_checkpoint",
        interrupt_at_checkpoint,
    )
    with (
        pytest.raises(KeyboardInterrupt),
        WindowsJobBackend().prepare(value, policy_for(tmp_path)),
    ):
        pass

    assert delete_calls == 1
    assert (
        backend_module._kernel32.WaitForSingleObject(
            backend_module.wintypes.HANDLE(observed["process_handle"]),
            0,
        )
        == backend_module._WAIT_FAILED
    )
    assert (
        backend_module._kernel32.WaitForSingleObject(
            backend_module.wintypes.HANDLE(observed["thread_handle"]),
            0,
        )
        == backend_module._WAIT_FAILED
    )
    assert (
        WindowsJobBackend.process_identity_status(
            observed["process_id"],
            observed["creation_time"],
        )
        == "absent"
    )


def test_job_list_attribute_failure_refuses_before_process_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="job-list-refusal",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    original_update = backend_module._kernel32.UpdateProcThreadAttribute
    original_delete = backend_module._DELETE_PROC_THREAD_ATTRIBUTE_LIST
    update_calls = 0
    delete_calls = 0
    create_called = False

    def fail_job_list(*args: Any) -> bool:
        nonlocal update_calls
        update_calls += 1
        if update_calls == 2:
            backend_module.ctypes.set_last_error(87)
            return False
        return bool(original_update(*args))

    def observe_create(*_args: Any) -> bool:
        nonlocal create_called
        create_called = True
        return False

    def count_delete(attribute_list: object) -> None:
        nonlocal delete_calls
        delete_calls += 1
        original_delete(attribute_list)

    monkeypatch.setattr(backend_module._kernel32, "UpdateProcThreadAttribute", fail_job_list)
    monkeypatch.setattr(backend_module._kernel32, "CreateProcessW", observe_create)
    monkeypatch.setattr(
        backend_module,
        "_DELETE_PROC_THREAD_ATTRIBUTE_LIST",
        count_delete,
    )
    with (
        pytest.raises(OSError),
        WindowsJobBackend().prepare(
            value,
            policy_for(tmp_path),
        ),
    ):
        pass

    assert update_calls == 2
    assert delete_calls == 1
    assert create_called is False


def test_attribute_list_delete_runs_once_on_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="attribute-delete-success",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    original_delete = backend_module._DELETE_PROC_THREAD_ATTRIBUTE_LIST
    delete_calls = 0

    def count_delete(attribute_list: object) -> None:
        nonlocal delete_calls
        delete_calls += 1
        original_delete(attribute_list)

    monkeypatch.setattr(
        backend_module,
        "_DELETE_PROC_THREAD_ATTRIBUTE_LIST",
        count_delete,
    )
    with WindowsJobBackend().prepare(value, policy_for(tmp_path)) as prepared:
        assert delete_calls == 1
        assert (
            WindowsJobBackend.process_identity_status(
                prepared.process_id,
                prepared.creation_time_100ns,
            )
            == "same_live_process"
        )

    assert delete_calls == 1
    assert prepared._closed is True


def test_resume_controller_abort_after_kernel_resume_kills_job_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.SPAWN_TREE,
        suffix="resume-controller-abort",
        arguments=SyntheticArgumentsV1(duration_ms=5_000, child_count=1),
    )
    with WindowsJobBackend().prepare(value, policy_for(tmp_path)) as prepared:
        original_resume = backend_module._kernel32.ResumeThread

        def interrupt_after_resume(handle: object) -> int:
            original_resume(handle)
            raise KeyboardInterrupt("synthetic controller abort")

        monkeypatch.setattr(backend_module._kernel32, "ResumeThread", interrupt_after_resume)
        with pytest.raises(KeyboardInterrupt):
            prepared.resume()

    assert prepared._closed is True
    assert (
        WindowsJobBackend.process_identity_status(
            prepared.process_id,
            prepared.creation_time_100ns,
        )
        == "absent"
    )


def test_wait_controller_abort_kills_job_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.SPAWN_TREE,
        suffix="wait-controller-abort",
        arguments=SyntheticArgumentsV1(duration_ms=5_000, child_count=1),
    )
    with WindowsJobBackend().prepare(value, policy_for(tmp_path)) as prepared:
        prepared.resume()

        def interrupt_accounting(_job_handle: int):
            raise KeyboardInterrupt("synthetic controller abort")

        monkeypatch.setattr(backend_module, "_query_accounting", interrupt_accounting)
        with pytest.raises(KeyboardInterrupt):
            prepared.wait()

    assert prepared._closed is True
    assert (
        WindowsJobBackend.process_identity_status(
            prepared.process_id,
            prepared.creation_time_100ns,
        )
        == "absent"
    )


def test_partial_reader_start_abort_closes_unstarted_pipe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="partial-reader-start",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    with WindowsJobBackend().prepare(value, policy_for(tmp_path)) as prepared:
        descriptors = (prepared.stdout_read_fd, prepared.stderr_read_fd)
        original_start = backend_module.threading.Thread.start
        start_count = 0

        def interrupt_second_start(thread: object) -> None:
            nonlocal start_count
            start_count += 1
            if start_count == 2:
                raise KeyboardInterrupt("synthetic reader startup abort")
            original_start(thread)  # type: ignore[arg-type]

        monkeypatch.setattr(backend_module.threading.Thread, "start", interrupt_second_start)
        with pytest.raises(KeyboardInterrupt):
            prepared.resume()
        assert prepared._stdout_done.wait(timeout=5)

    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    assert prepared._closed is True


def test_approved_input_base_exception_closes_inheritable_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved_input = tmp_path / "approved.txt"
    approved_input.write_bytes(b"bounded input\n")
    value = request(
        SyntheticOperation.COPY_HANDLES,
        suffix="input-base-exception",
    )
    observed_descriptor: int | None = None
    original_set_inheritable = backend_module.os.set_inheritable

    def interrupt_after_inheritable(file_descriptor: int, inheritable: bool) -> None:
        nonlocal observed_descriptor
        original_set_inheritable(file_descriptor, inheritable)
        if inheritable:
            observed_descriptor = file_descriptor
            raise KeyboardInterrupt("synthetic input admission abort")

    monkeypatch.setattr(backend_module.os, "set_inheritable", interrupt_after_inheritable)
    with (
        pytest.raises(KeyboardInterrupt),
        WindowsJobBackend().prepare(
            value,
            policy_for(tmp_path, approved_input=approved_input),
        ),
    ):
        pass

    assert observed_descriptor is not None
    with pytest.raises(OSError):
        os.fstat(observed_descriptor)


def test_cancel_termination_failure_is_effect_unknown_not_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="cancel-termination-failure",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    with WindowsJobBackend().prepare(value, policy_for(tmp_path)) as prepared:
        prepared.resume()
        monkeypatch.setattr(
            backend_module._kernel32,
            "TerminateJobObject",
            lambda *_args: False,
        )

        outcome = prepared.wait(cancelled=lambda: True)

    assert outcome.failure_code is JobFailureCode.EFFECT_UNKNOWN
    assert outcome.cancelled is False
    assert outcome.evidence.active_processes > 0
    assert outcome.evidence.process_tree_termination_confirmed is False


def test_exit_zero_at_exact_cpu_cap_is_deterministically_cpu_limit(
    tmp_path: Path,
) -> None:
    cap_ms = 200
    value = request(
        SyntheticOperation.CPU_SPIN,
        suffix="cpu-exact-terminal",
        arguments=SyntheticArgumentsV1(duration_ms=1),
    )
    with WindowsJobBackend().prepare(
        value,
        policy_for(
            tmp_path,
            job_limits=limits(
                process_cpu_time_ms=cap_ms,
                job_cpu_time_ms=cap_ms,
            ),
        ),
    ) as prepared:
        accounting = backend_module._JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION()
        accounting.BasicInfo.TotalUserTime.QuadPart = cap_ms * 10_000
        extended = backend_module._JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        assert (
            prepared._infer_exit_failure(
                0,
                accounting,
                extended,
                process_user_time_100ns=0,
            )
            is JobFailureCode.CPU_LIMIT
        )


def test_budget_settlement_postreplace_fault_exactly_confirms_and_closes_permit(
    tmp_path: Path,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix="budget-settle-fault",
        fail_on_current_replace=99,
    )
    after_replace_count = 0

    def fault(hook: str) -> None:
        nonlocal after_replace_count
        if hook == "current.after_replace":
            after_replace_count += 1
            if after_replace_count == 3:
                raise OSError("synthetic budget settlement uncertainty")

    budget.revisions.fault_injector = fault
    result = coordinator.execute(value, policy, **kwargs)
    state = job_store.load(value.execution_id)
    budget_state = budget.load(value.budget_run_id)

    assert result.status is IsolatedJobStatus.COMPLETED
    assert state.status is IsolatedJobStatus.COMPLETED
    assert not budget_state.active_permits
    assert budget_state.settlements[-1].status.value == "succeeded"


@pytest.mark.parametrize(
    ("fault_ordinal", "expected_job_status", "expected_cancellation"),
    [
        (3, IsolatedJobStatus.EFFECT_UNKNOWN, CancellationState.EFFECT_UNKNOWN),
        (4, IsolatedJobStatus.CANCELLED, CancellationState.CANCELLED),
        (5, IsolatedJobStatus.CANCELLED, CancellationState.CANCELLED),
    ],
)
def test_cancel_publication_faults_close_started_permit(
    tmp_path: Path,
    fault_ordinal: int,
    expected_job_status: IsolatedJobStatus,
    expected_cancellation: CancellationState,
) -> None:
    value, policy, coordinator, job_store, budget, kwargs = _executable_with_publication_fault(
        tmp_path,
        suffix=f"cancel-fault-{fault_ordinal}",
        fail_on_current_replace=99,
        operation=SyntheticOperation.HANG,
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    after_replace_count = 0
    fired = False

    def fault(hook: str) -> None:
        nonlocal after_replace_count, fired
        if hook == "current.after_replace":
            after_replace_count += 1
            if after_replace_count == fault_ordinal and not fired:
                fired = True
                raise OSError("synthetic cancellation publication uncertainty")

    budget.revisions.fault_injector = fault
    result = coordinator.execute(value, policy, cancelled=lambda: True, **kwargs)
    budget_state = budget.load(value.budget_run_id)
    cancellation = next(
        item for item in budget_state.cancellations if item.permit_id == value.budget_permit_id
    )

    assert fired is True
    assert result.status is expected_job_status
    assert job_store.load(value.execution_id).status is expected_job_status
    assert cancellation.state is expected_cancellation
    assert cancellation.worker_live is False
    assert not budget_state.active_permits
    assert budget_state.settlements[-1].status.value == (
        "cancelled" if expected_job_status is IsolatedJobStatus.CANCELLED else "effect_unknown"
    )


def test_unconfirmed_tree_death_never_confirms_cancel_or_closes_permit(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="cancel-live-tree")
    value = fixture.value
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=901,
        process_creation_time_100ns=902,
    )
    fixture.budget.start(
        value.budget_run_id,
        operation_id="start-cancel-live-tree",
        permit_id=value.budget_permit_id,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="d" * 64,
    )
    fixture.budget.request_cancellation(
        value.budget_run_id,
        operation_id="request-cancel-live-tree",
        permit_id=value.budget_permit_id,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.CANCEL_REQUESTED,
        reason_code="caller_cancel_requested",
    )
    evidence = JobEvidenceV1(
        process_id=901,
        process_creation_time_100ns=902,
        exit_code=259,
        termination_reason=JobFailureCode.CANCELLED.value,
        total_processes=1,
        active_processes=1,
        stdout_sha256=hashlib.sha256(b"").hexdigest(),
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
        command_line_sha256="b" * 64,
        inherited_handle_count=3,
        process_tree_termination_confirmed=False,
        job_limits_requeried=True,
        executable_identity_rechecked=True,
        output_complete=False,
    )
    outcome = WindowsJobOutcome(
        evidence=evidence,
        stdout=b"",
        stderr=b"",
        failure_code=JobFailureCode.CANCELLED,
        cancelled=True,
    )

    status, code = fixture.coordinator._settle_outcome(value, outcome)
    budget_state = fixture.budget.load(value.budget_run_id)
    cancellation = next(
        item for item in budget_state.cancellations if item.permit_id == value.budget_permit_id
    )

    assert status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert code is JobFailureCode.EFFECT_UNKNOWN
    assert cancellation.state is CancellationState.EFFECT_UNKNOWN
    assert cancellation.worker_live is True
    assert any(permit.permit_id == value.budget_permit_id for permit in budget_state.active_permits)
    assert not budget_state.settlements


def test_recovery_closes_requested_cancellation_after_process_absence(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="recover-requested-cancel")
    value = fixture.value
    fixture.coordinator.backend = _AbsentBackend()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=777,
        process_creation_time_100ns=888,
    )
    fixture.budget.start(
        value.budget_run_id,
        operation_id="start-recover-requested-cancel",
        permit_id=value.budget_permit_id,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="d" * 64,
    )
    fixture.budget.request_cancellation(
        value.budget_run_id,
        operation_id="request-recover-requested-cancel",
        permit_id=value.budget_permit_id,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.EFFECT_UNKNOWN,
        reason_code="controller_restart",
        failure_code=JobFailureCode.EFFECT_UNKNOWN,
    )

    recovered = fixture.coordinator.recover_after_restart(value, fixture.policy)
    budget_state = fixture.budget.load(value.budget_run_id)
    cancellation = next(
        item for item in budget_state.cancellations if item.permit_id == value.budget_permit_id
    )

    assert recovered.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert cancellation.state is CancellationState.EFFECT_UNKNOWN
    assert cancellation.worker_live is False
    assert not budget_state.active_permits
    assert budget_state.settlements[-1].status.value == "effect_unknown"
    assert budget_state.settlements[-1].actual.tool_attempts == 1


def test_acknowledged_cancel_recovery_charges_attempt_and_approved_input(
    tmp_path: Path,
) -> None:
    input_bytes = b"approved bounded input\n"
    fixture = _prepared(
        tmp_path,
        suffix="recover-acknowledged-cancel",
        operation=SyntheticOperation.COPY_HANDLES,
        input_bytes=input_bytes,
    )
    value = fixture.value
    fixture.coordinator.backend = _AbsentBackend()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=777,
        process_creation_time_100ns=888,
    )
    fixture.budget.start(
        value.budget_run_id,
        operation_id="start-recover-acknowledged-cancel",
        permit_id=value.budget_permit_id,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="d" * 64,
    )
    fixture.budget.request_cancellation(
        value.budget_run_id,
        operation_id="request-recover-acknowledged-cancel",
        permit_id=value.budget_permit_id,
    )
    fixture.budget.record_cancellation(
        value.budget_run_id,
        operation_id="ack-recover-acknowledged-cancel",
        permit_id=value.budget_permit_id,
        state_value=CancellationState.ACKNOWLEDGED,
        evidence_sha256="a" * 64,
        worker_live=False,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.EFFECT_UNKNOWN,
        reason_code="controller_restart",
        failure_code=JobFailureCode.EFFECT_UNKNOWN,
    )

    recovered = fixture.coordinator.recover_after_restart(value, fixture.policy)
    budget_state = fixture.budget.load(value.budget_run_id)
    settlement = budget_state.settlements[-1]

    assert recovered.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert settlement.status.value == "cancelled"
    assert settlement.actual.tool_attempts == 1
    assert settlement.actual.tool_input_bytes == len(input_bytes)
    assert not budget_state.active_permits


def test_recovery_retries_budget_closure_and_reconciliation_rejects_active_permit(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="recovery-budget-fault")
    value = fixture.value
    fixture.coordinator.backend = _AbsentBackend()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=777,
        process_creation_time_100ns=888,
    )
    fixture.budget.start(
        value.budget_run_id,
        operation_id="start-recovery-budget-fault",
        permit_id=value.budget_permit_id,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="d" * 64,
    )

    def fault(hook: str) -> None:
        if hook == "staging.before_mkdir":
            raise OSError("synthetic budget closure failure")

    fixture.budget.revisions.fault_injector = fault
    recovered = fixture.coordinator.recover_after_restart(value, fixture.policy)
    assert recovered.status is IsolatedJobStatus.EFFECT_UNKNOWN
    with pytest.raises(ValueError, match=JobFailureCode.RECONCILIATION_REQUIRED.value):
        fixture.coordinator.reconcile(
            value.execution_id,
            reconciliation_reference=ReconciliationReferenceV1(
                reference_id="reconcile-active-budget",
                evidence_sha256="e" * 64,
            ),
        )

    fixture.budget.revisions.fault_injector = None
    fixture.coordinator.recover_after_restart(value, fixture.policy)
    reconciled = fixture.coordinator.reconcile(
        value.execution_id,
        reconciliation_reference=ReconciliationReferenceV1(
            reference_id="reconcile-closed-budget",
            evidence_sha256="f" * 64,
        ),
    )
    assert reconciled.status is IsolatedJobStatus.RECONCILED


def test_successor_process_effect_and_evidence_bindings_are_one_way(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="immutable-successor")
    value = fixture.value
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=901,
        process_creation_time_100ns=902,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="a" * 64,
    )
    evidence = JobEvidenceV1(
        process_id=901,
        process_creation_time_100ns=902,
        exit_code=0,
        termination_reason=JobFailureCode.EFFECT_UNKNOWN.value,
        total_processes=1,
        active_processes=0,
        stdout_sha256=hashlib.sha256(b"").hexdigest(),
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
        command_line_sha256="b" * 64,
        inherited_handle_count=3,
        process_tree_termination_confirmed=True,
        job_limits_requeried=True,
        executable_identity_rechecked=True,
        output_complete=False,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.EFFECT_UNKNOWN,
        reason_code="effect_unknown",
        evidence=evidence,
        failure_code=JobFailureCode.EFFECT_UNKNOWN,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RECONCILED,
        reason_code="manual_reconciliation",
        failure_code=JobFailureCode.RECONCILIATION_REQUIRED,
        reconciliation_evidence_sha256="c" * 64,
    )
    newer, older = fixture.job_store._load_history(value.execution_id)[:2]

    with pytest.raises(ValueError, match="process identity changed"):
        _validate_successor(older, newer.model_copy(update={"process_id": 999}))
    with pytest.raises(ValueError, match="effect-admission identity changed"):
        _validate_successor(
            older,
            newer.model_copy(update={"effect_admission_recheck_binding_sha256": "d" * 64}),
        )
    assert newer.evidence is not None
    with pytest.raises(ValueError, match="process evidence changed"):
        _validate_successor(
            older,
            newer.model_copy(
                update={"evidence": newer.evidence.model_copy(update={"wall_clock_ms": 1})}
            ),
        )


def test_prepared_effect_unknown_cannot_claim_effect_admission_digest(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="premature-effect-digest")

    with pytest.raises(ValueError, match="introduced out of order"):
        fixture.job_store.transition(
            fixture.value.execution_id,
            status=IsolatedJobStatus.EFFECT_UNKNOWN,
            reason_code="pre_effect_storage_unknown",
            effect_admission_recheck_binding_sha256="a" * 64,
            failure_code=JobFailureCode.EFFECT_UNKNOWN,
        )

    state = fixture.job_store.load(fixture.value.execution_id)
    assert state.status is IsolatedJobStatus.PREPARED
    assert state.effect_admission_recheck_binding_sha256 is None


def test_reconciliation_cannot_introduce_late_process_evidence(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="late-reconciliation-evidence")
    value = fixture.value
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=901,
        process_creation_time_100ns=902,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.EFFECT_UNKNOWN,
        reason_code="pre_resume_publication_unknown",
        failure_code=JobFailureCode.EFFECT_UNKNOWN,
    )
    evidence = JobEvidenceV1(
        process_id=901,
        process_creation_time_100ns=902,
        exit_code=0,
        total_processes=1,
        active_processes=0,
        stdout_sha256=hashlib.sha256(b"").hexdigest(),
        stderr_sha256=hashlib.sha256(b"").hexdigest(),
        command_line_sha256="b" * 64,
        inherited_handle_count=3,
        process_tree_termination_confirmed=True,
        job_limits_requeried=True,
        executable_identity_rechecked=True,
        output_complete=True,
    )

    with pytest.raises(ValueError, match="introduced before terminal state"):
        fixture.job_store.transition(
            value.execution_id,
            status=IsolatedJobStatus.RECONCILED,
            reason_code="manual_reconciliation",
            evidence=evidence,
            failure_code=JobFailureCode.RECONCILIATION_REQUIRED,
            reconciliation_evidence_sha256="c" * 64,
        )

    state = fixture.job_store.load(value.execution_id)
    assert state.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert state.evidence is None


def test_secret_shaped_output_is_never_published(tmp_path: Path) -> None:
    fixture = _prepared(tmp_path, suffix="secret-output")
    value = fixture.value

    with pytest.raises(IsolatedJobError) as rejected:
        fixture.job_store.transition(
            value.execution_id,
            status=IsolatedJobStatus.FAILED,
            reason_code="synthetic_output_rejected",
            failure_code=JobFailureCode.CHILD_EXIT_NONZERO,
            stdout=b"ghp_abcdefghijklmnopqrstuvwxyz012345\n",
        )

    assert rejected.value.code is JobFailureCode.SECRET_REJECTED
    assert fixture.job_store.load(value.execution_id).status is IsolatedJobStatus.PREPARED


def test_output_bytes_must_match_process_evidence_before_publication(
    tmp_path: Path,
) -> None:
    fixture = _prepared(tmp_path, suffix="output-binding")
    value = fixture.value
    with WindowsJobBackend().prepare(value, fixture.policy) as prepared:
        prepared.resume()
        outcome = prepared.wait()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=outcome.evidence.process_id,
        process_creation_time_100ns=outcome.evidence.process_creation_time_100ns,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="a" * 64,
    )

    with pytest.raises(IsolatedJobError) as rejected:
        fixture.job_store.transition(
            value.execution_id,
            status=IsolatedJobStatus.COMPLETED,
            reason_code="job_terminal",
            evidence=outcome.evidence,
            stdout=b"tampered\n",
            stderr=outcome.stderr,
        )

    assert rejected.value.code is JobFailureCode.STORAGE_FAILURE
    assert fixture.job_store.load(value.execution_id).status is IsolatedJobStatus.RUNNING


def test_reconciliation_preserves_bound_output_evidence(tmp_path: Path) -> None:
    fixture = _prepared(tmp_path, suffix="reconcile-output")
    value = fixture.value
    with WindowsJobBackend().prepare(value, fixture.policy) as prepared:
        prepared.resume()
        outcome = prepared.wait()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=outcome.evidence.process_id,
        process_creation_time_100ns=outcome.evidence.process_creation_time_100ns,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
        effect_admission_recheck_binding_sha256="b" * 64,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.EFFECT_UNKNOWN,
        reason_code="terminal_publication_unknown",
        evidence=outcome.evidence,
        failure_code=JobFailureCode.EFFECT_UNKNOWN,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
    )
    fixture.budget.start(
        value.budget_run_id,
        operation_id="start-reconcile-output",
        permit_id=value.budget_permit_id,
    )
    fixture.coordinator._settle_ambiguous_resume(
        value,
        evidence_sha256=isolated_job_sha256(outcome.evidence),
    )
    fixture.coordinator.backend = _AbsentBackend()

    reconciled = fixture.coordinator.reconcile(
        value.execution_id,
        reconciliation_reference=ReconciliationReferenceV1(
            reference_id="reconcile-output-evidence",
            evidence_sha256="c" * 64,
        ),
    )

    assert reconciled.status is IsolatedJobStatus.RECONCILED
    assert reconciled.stdout == outcome.stdout
    assert fixture.job_store.load(value.execution_id).evidence == outcome.evidence
