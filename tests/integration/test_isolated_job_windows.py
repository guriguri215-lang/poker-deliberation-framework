from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from poker_deliberation.approval_models import (
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
)
from poker_deliberation.approvals import read_approval_state_v2
from poker_deliberation.budgets.durable_store import (
    DurableBudgetStore,
    initialize_durable_budget_root,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.isolated_jobs.coordinator import IsolatedJobCoordinator
from poker_deliberation.isolated_jobs.models import (
    JobFailureCode,
    JobLimitsV1,
    SyntheticArgumentsV1,
    SyntheticOperation,
)
from poker_deliberation.isolated_jobs.store import (
    IsolatedJobStore,
    initialize_isolated_job_root,
)
from poker_deliberation.isolated_jobs.windows_backend import WindowsJobBackend
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
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


def _run_backend(value, policy):
    prepared = WindowsJobBackend().prepare(value, policy)
    prepared.resume()
    return prepared.wait()


def test_windows_job_normal_exit_closed_stdin_and_module_inventory(tmp_path: Path) -> None:
    success = request(suffix="backend-success")
    policy = policy_for(tmp_path)

    normal = _run_backend(success, policy)
    eof = _run_backend(
        request(SyntheticOperation.STDIN_EOF, suffix="backend-stdin"),
        policy_for(tmp_path),
    )
    inventory = _run_backend(
        request(SyntheticOperation.MODULE_INVENTORY, suffix="backend-modules"),
        policy_for(tmp_path),
    )

    assert normal.failure_code is None
    assert normal.stdout == b"ok\n"
    assert normal.evidence.exit_code == 0
    assert normal.evidence.active_processes == 0
    assert normal.evidence.inherited_handle_count == 3
    assert normal.evidence.job_limits_requeried is True
    assert normal.evidence.executable_identity_rechecked is True
    assert eof.failure_code is None
    assert eof.stdout == b"eof\n"
    non_frozen_modules = set(inventory.stdout.splitlines())
    assert non_frozen_modules == {
        b"encodings",
        b"encodings.aliases",
        b"encodings.utf_8",
    }
    encoding_identities = policy.execution_identity.encoding_files
    assert {Path(item.absolute_path).name for item in encoding_identities} == {
        "__init__.py",
        "aliases.py",
        "utf_8.py",
    }


@pytest.mark.parametrize(
    ("operation", "arguments", "expected"),
    [
        (
            SyntheticOperation.HANG,
            SyntheticArgumentsV1(duration_ms=5_000),
            JobFailureCode.WALL_CLOCK_LIMIT,
        ),
        (
            SyntheticOperation.STDOUT_FLOOD,
            SyntheticArgumentsV1(output_bytes=1_000_000),
            JobFailureCode.STDOUT_LIMIT,
        ),
        (
            SyntheticOperation.STDERR_FLOOD,
            SyntheticArgumentsV1(output_bytes=1_000_000),
            JobFailureCode.STDERR_LIMIT,
        ),
    ],
)
def test_windows_job_hard_stops_and_bounds_output(
    tmp_path: Path,
    operation: SyntheticOperation,
    arguments: SyntheticArgumentsV1,
    expected: JobFailureCode,
) -> None:
    value = request(operation, suffix=operation.value, arguments=arguments)
    job_limits = limits(
        wall_clock_ms=250,
        stdout_bytes=4_096,
        stderr_bytes=4_096,
        combined_output_bytes=8_192,
    )

    outcome = _run_backend(value, policy_for(tmp_path, job_limits=job_limits))

    assert outcome.failure_code is expected
    assert outcome.evidence.active_processes == 0
    assert outcome.evidence.process_tree_termination_confirmed is True
    assert len(outcome.stdout) <= job_limits.stdout_bytes
    assert len(outcome.stderr) <= job_limits.stderr_bytes
    assert len(outcome.stdout) + len(outcome.stderr) <= job_limits.combined_output_bytes


@pytest.mark.parametrize(
    ("operation", "arguments", "job_limits", "expected"),
    [
        (
            SyntheticOperation.CPU_SPIN,
            SyntheticArgumentsV1(duration_ms=5_000),
            limits(
                wall_clock_ms=3_000,
                process_cpu_time_ms=100,
                job_cpu_time_ms=100,
            ),
            JobFailureCode.CPU_LIMIT,
        ),
        (
            SyntheticOperation.MEMORY_PRESSURE,
            SyntheticArgumentsV1(memory_bytes=256 * 1024 * 1024, duration_ms=5_000),
            limits(
                wall_clock_ms=3_000,
                process_memory_bytes=32 * 1024 * 1024,
                job_memory_bytes=32 * 1024 * 1024,
            ),
            JobFailureCode.MEMORY_LIMIT,
        ),
        (
            SyntheticOperation.SPAWN_TREE,
            SyntheticArgumentsV1(duration_ms=5_000, child_count=2),
            limits(
                wall_clock_ms=3_000,
                maximum_processes=2,
            ),
            JobFailureCode.PROCESS_LIMIT,
        ),
    ],
)
def test_windows_job_enforces_cpu_memory_and_process_caps(
    tmp_path: Path,
    operation: SyntheticOperation,
    arguments: SyntheticArgumentsV1,
    job_limits: JobLimitsV1,
    expected: JobFailureCode,
) -> None:
    outcome = _run_backend(
        request(operation, suffix=f"resource-{operation.value}", arguments=arguments),
        policy_for(tmp_path, job_limits=job_limits),
    )

    assert outcome.failure_code is expected
    assert outcome.evidence.active_processes == 0
    assert outcome.evidence.process_tree_termination_confirmed is True
    assert outcome.evidence.job_limits_requeried is True


def test_windows_job_terminates_descendant_tree(tmp_path: Path) -> None:
    value = request(
        SyntheticOperation.SPAWN_TREE,
        suffix="backend-tree",
        arguments=SyntheticArgumentsV1(duration_ms=5_000, child_count=2),
    )
    policy = policy_for(
        tmp_path,
        job_limits=limits(wall_clock_ms=350, maximum_processes=3),
    )

    outcome = _run_backend(value, policy)

    assert outcome.failure_code is JobFailureCode.WALL_CLOCK_LIMIT
    assert outcome.evidence.total_processes >= 2
    assert outcome.evidence.active_processes == 0
    assert outcome.evidence.process_tree_termination_confirmed is True


def test_windows_job_passes_only_approved_input_handle(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture.txt"
    fixture.write_bytes(b"fixture-data\n")
    value = request(SyntheticOperation.COPY_HANDLES, suffix="backend-handle")
    policy = policy_for(tmp_path, approved_input=fixture)

    outcome = _run_backend(value, policy)

    assert outcome.failure_code is None
    assert outcome.stdout == b"fixture-data\n"
    assert outcome.evidence.inherited_handle_count == 4


def test_full_approval_context_budget_storage_execution_and_exact_replay(
    tmp_path: Path,
) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    product_root = tmp_path / "product"
    budget_root = tmp_path / "budget"
    job_root = tmp_path / "jobs"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    initialize_durable_budget_root(
        budget_root,
        legacy,
        root_id="root-" + "3" * 32,
        initialized_at=NOW,
    )
    initialize_isolated_job_root(
        job_root,
        legacy,
        root_id="root-" + "4" * 32,
        initialized_at=NOW,
    )
    budget = DurableBudgetStore(
        budget_root,
        legacy,
        wall_clock=lambda: NOW,
    )
    value = request(suffix="full")
    budget.create(
        value.budget_run_id,
        durable_policy(),
        operation_id="initialize-full-job-budget",
    )
    config = AppConfig(
        runs_dir=legacy,
        revision_runs_dir=product_root,
        durable_budget_runs_dir=budget_root,
    )
    creator = Orchestrator(
        config,
        budget_store=budget,
        terminal_clock=lambda: NOW,
        context_clock=lambda: NOW,
    )
    job_store = IsolatedJobStore(job_root, legacy, clock=lambda: NOW)
    coordinator = IsolatedJobCoordinator(
        job_store,
        budget,
        creator.product_store,
        clock=lambda: NOW,
    )
    assignment, envelope = context_for(value)
    lineage = lineage_for(value, envelope)
    policy = policy_for(workspace)
    action_expires_at = NOW + timedelta(minutes=30)
    preview = coordinator.preview(
        value,
        policy,
        context_envelope=envelope,
        assignment=assignment,
        budget_lineage=lineage,
        action_expires_at=action_expires_at,
    )
    approval_run_id = "Approval-isolated-full"
    report = creator.run(
        CaseInput(
            kind="strategy",
            raw_text="authorize one fixed repository synthetic helper",
            analysis_scope="retrospective",
            metadata={
                "approval_requests": [
                    {
                        "schema_version": "2.0.0",
                        "stable_proposal_id": "isolated-job-full-proposal",
                        "action_plan": preview.action_plan.model_dump(mode="json"),
                        "display": {
                            "requested_action": preview.action_plan.operation,
                            "reason": "Authorize the exact fixed synthetic job.",
                            "expected_benefit": "Verify the P2-028A vertical slice.",
                            "risks": ["A bounded local synthetic child will run."],
                            "data_to_be_sent": [],
                            "cost_or_resource_estimate": "Bounded local resources only.",
                            "alternatives": ["Decline and perform no process effect."],
                            "effect_of_declining": "No isolated job is launched.",
                            "exact_command_or_tool_call": None,
                        },
                    }
                ]
            },
        ),
        run_id=approval_run_id,
    )
    assert report.run_status == "approval_required"
    checkpoint = creator.product_store.read_current(approval_run_id)
    state = read_approval_state_v2(
        checkpoint.payload_bytes("approval_ledger_v2.json"),
        checkpoint.payload_bytes("approval_decisions_v2.jsonl"),
        checkpoint.payload_bytes("approval_audit_v2.jsonl"),
    )
    approval_request = state.ledger.requests[0]
    authority = JobAuthority()
    decision_at = NOW + timedelta(minutes=1)
    batch = ApprovalDecisionBatch(
        run_id=approval_run_id,
        expected_run_revision=checkpoint.revision,
        expected_ledger_revision=state.ledger.ledger_revision,
        actor=authority.actor,
        decision_id="isolated-job-full-decision",
        idempotency_key="isolated-job-full-decision-key",
        items=(
            ApprovalDecisionItemV2(
                request_id=approval_request.request_id,
                expected_request_revision=approval_request.request_revision,
                action_digest_sha256=approval_request.action_digest_sha256,
                decision="approved",
            ),
        ),
        reason="Approve the exact repository synthetic job.",
        decision_at=decision_at,
    )
    decider = Orchestrator(
        config,
        budget_store=budget,
        terminal_clock=lambda: decision_at,
        context_clock=lambda: decision_at,
        decision_authority_provider=authority,
    )
    decision = decider.decide_approvals(batch)
    assert decision.run_status == "failed_with_limitations"
    executor = IsolatedJobCoordinator(
        job_store,
        budget,
        decider.product_store,
        clock=lambda: NOW + timedelta(minutes=2),
    )

    altered_policy = policy.model_copy(update={"limits": limits(wall_clock_ms=1_900)})
    with pytest.raises(ValueError, match=JobFailureCode.APPROVAL_MISMATCH.value):
        executor.execute(
            value,
            altered_policy,
            context_envelope=envelope,
            assignment=assignment,
            budget_lineage=lineage,
            action_expires_at=action_expires_at,
            approval_run_id=approval_run_id,
            approval_request_id=approval_request.request_id,
            authority_provider=authority,
        )
    assert not (job_store.revisions.runs_root / value.execution_id).exists()
    assert not budget.load(value.budget_run_id).active_permits

    result = executor.execute(
        value,
        policy,
        context_envelope=envelope,
        assignment=assignment,
        budget_lineage=lineage,
        action_expires_at=action_expires_at,
        approval_run_id=approval_run_id,
        approval_request_id=approval_request.request_id,
        authority_provider=authority,
    )
    revision = job_store.revisions.read_current(value.execution_id)
    replay = executor.execute(
        value,
        policy,
        context_envelope=envelope,
        assignment=assignment,
        budget_lineage=lineage,
        action_expires_at=action_expires_at,
        approval_run_id=approval_run_id,
        approval_request_id=approval_request.request_id,
        authority_provider=authority,
    )
    after_replay = job_store.revisions.read_current(value.execution_id)
    budget_state = budget.load(value.budget_run_id)

    assert result.status.value == "completed"
    assert result.stdout == b"ok\n"
    assert replay == result
    assert after_replay.current_pointer_sha256 == revision.current_pointer_sha256
    assert budget_state.settlements[-1].status.value == "succeeded"
    assert (
        budget_state.settlements[-1].actual.active_runtime_ns
        >= job_store.load(value.execution_id).evidence.wall_clock_ms * 1_000_000
    )
    assert not budget_state.active_permits
