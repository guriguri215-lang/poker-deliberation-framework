from __future__ import annotations

import hashlib
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
from poker_deliberation.budgets.durable_models import ExecutionLineageV1
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
from poker_deliberation.isolated_jobs.windows_backend import WindowsJobBackend
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

    assert state.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert state.failure_code is JobFailureCode.EFFECT_UNKNOWN
    assert state.process_id is not None
    assert budget_state.settlements[-1].status.value == "effect_unknown"
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


def test_wait_accounting_exception_terminates_and_returns_effect_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = request(
        SyntheticOperation.HANG,
        suffix="accounting-exception",
        arguments=SyntheticArgumentsV1(duration_ms=5_000),
    )
    prepared = WindowsJobBackend().prepare(
        value,
        policy_for(tmp_path, job_limits=limits(wall_clock_ms=2_000)),
    )
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
    prepared = WindowsJobBackend().prepare(
        value,
        policy_for(tmp_path, job_limits=limits(wall_clock_ms=2_000)),
    )
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
    prepared = WindowsJobBackend().prepare(value, policy_for(tmp_path))

    def fail_identity(_identity: object) -> None:
        raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH)

    monkeypatch.setattr(backend_module, "verify_execution_identity", fail_identity)
    with pytest.raises(IsolatedJobError) as rejected:
        prepared.resume()
    assert rejected.value.code is JobFailureCode.IDENTITY_MISMATCH
    assert prepared._resumed is False
    assert prepared._closed is True


def test_budget_settlement_mutation_fault_cannot_leave_running_job(
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

    assert result.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert state.status is IsolatedJobStatus.EFFECT_UNKNOWN
    assert not budget_state.active_permits
    assert budget_state.settlements[-1].status.value == "succeeded"


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
    prepared = WindowsJobBackend().prepare(value, fixture.policy)
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
