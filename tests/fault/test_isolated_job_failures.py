from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from poker_deliberation.approval_canonical import action_digest_sha256
from poker_deliberation.approval_models import ApprovalExecutionRecheckBindingV2
from poker_deliberation.budgets.durable_models import ExecutionLineageV1
from poker_deliberation.budgets.durable_store import (
    DurableBudgetStore,
    initialize_durable_budget_root,
)
from poker_deliberation.context_lifecycle import ContextEnvelope
from poker_deliberation.isolated_jobs.coordinator import IsolatedJobCoordinator
from poker_deliberation.isolated_jobs.models import (
    ApprovalJobReferenceV1,
    IsolatedJobError,
    IsolatedJobPolicyV1,
    IsolatedJobRequestV1,
    IsolatedJobStatus,
    JobFailureCode,
    ReconciliationReferenceV1,
)
from poker_deliberation.isolated_jobs.store import (
    IsolatedJobStore,
    initialize_isolated_job_root,
)
from poker_deliberation.isolated_jobs.windows_backend import WindowsJobBackend
from poker_deliberation.schemas import AgentAssignment
from tests.isolated_job_support import (
    NOW,
    JobAuthority,
    context_for,
    durable_policy,
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
            ApprovalExecutionRecheckBindingV2.model_construct(binding_sha256="3" * 64),
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
    value = request(suffix=suffix)
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


def _prepared(tmp_path: Path, *, suffix: str) -> _PreparedFixture:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    budget_root = tmp_path / "budget"
    job_root = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
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
    value = request(suffix=suffix)
    budget.create(
        value.budget_run_id,
        durable_policy(),
        operation_id=f"initialize-{suffix}",
    )
    assignment, envelope = context_for(value)
    lineage = lineage_for(value, envelope)
    policy = policy_for(workspace)
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
        effect_admission_recheck_binding_sha256="8" * 64,
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
        effect_admission_recheck_binding_sha256="9" * 64,
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
    )

    recovered = fixture.coordinator.recover_after_restart(value, fixture.policy)
    budget_state = fixture.budget.load(value.budget_run_id)

    assert recovered.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert budget_state.settlements[-1].status.value == "effect_unknown"
    assert not budget_state.active_permits


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
    assert state.process_id is None
    assert budget_state.settlements[-1].status.value == "released_no_effect"
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
    prepared = WindowsJobBackend().prepare(value, fixture.policy)
    prepared.resume()
    outcome = prepared.wait()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=outcome.evidence.process_id,
        process_creation_time_100ns=outcome.evidence.process_creation_time_100ns,
        effect_admission_recheck_binding_sha256="a" * 64,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
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
    prepared = WindowsJobBackend().prepare(value, fixture.policy)
    prepared.resume()
    outcome = prepared.wait()
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.LAUNCH_COMMITTED,
        reason_code="suspended_child_assigned",
        process_id=outcome.evidence.process_id,
        process_creation_time_100ns=outcome.evidence.process_creation_time_100ns,
        effect_admission_recheck_binding_sha256="b" * 64,
    )
    fixture.job_store.transition(
        value.execution_id,
        status=IsolatedJobStatus.RUNNING,
        reason_code="primary_thread_resumed",
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
