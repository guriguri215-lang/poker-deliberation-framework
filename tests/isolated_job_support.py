from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from poker_deliberation.approval_canonical import approval_actor_sha256
from poker_deliberation.approval_models import ApprovalActor, ApprovalAuthoritySnapshotV2
from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.budgets.durable_models import (
    DurableBudgetPolicyV1,
    ExecutionLineageV1,
    OwnerKind,
)
from poker_deliberation.context_lifecycle import ContextEnvelope, build_context_envelope
from poker_deliberation.isolated_jobs.canonical import isolated_job_sha256
from poker_deliberation.isolated_jobs.coordinator import qualify_isolated_job_policy
from poker_deliberation.isolated_jobs.models import (
    IsolatedJobRequestV1,
    JobLimitsV1,
    SyntheticArgumentsV1,
    SyntheticOperation,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext

NOW = datetime(2026, 7, 31, 1, 0, tzinfo=UTC)


class JobAuthority:
    def __init__(self, *, now: datetime = NOW) -> None:
        self.actor = ApprovalActor(
            actor_id="isolated-job-reviewer",
            actor_type="human",
            authority_source="test-isolated-job-authority",
            authority_scopes=("approve:external_code", "reject:any"),
            verification_status="verified",
            verification_reference_sha256="1" * 64,
            session_reference_sha256="2" * 64,
            credential_reference_sha256="3" * 64,
            verified_at=now - timedelta(minutes=1),
            authority_expires_at=now + timedelta(hours=2),
            revocation_status="not_revoked",
        )

    def resolve_actor(
        self,
        actor_id: str,
        *,
        decision_at: datetime,
    ) -> ApprovalAuthoritySnapshotV2:
        assert actor_id == self.actor.actor_id
        return ApprovalAuthoritySnapshotV2(
            provider_id="test-isolated-job-authority",
            provider_version="1.0.0",
            resolved_at=decision_at,
            actor=self.actor,
            actor_sha256=approval_actor_sha256(self.actor),
        )


def limits(
    *,
    wall_clock_ms: int = 2_000,
    process_cpu_time_ms: int = 1_000,
    job_cpu_time_ms: int = 1_000,
    process_memory_bytes: int = 128 * 1024 * 1024,
    job_memory_bytes: int = 128 * 1024 * 1024,
    maximum_processes: int = 2,
    stdout_bytes: int = 64 * 1024,
    stderr_bytes: int = 64 * 1024,
    combined_output_bytes: int = 128 * 1024,
) -> JobLimitsV1:
    return JobLimitsV1(
        wall_clock_ms=wall_clock_ms,
        process_cpu_time_ms=process_cpu_time_ms,
        job_cpu_time_ms=job_cpu_time_ms,
        process_memory_bytes=process_memory_bytes,
        job_memory_bytes=job_memory_bytes,
        maximum_processes=maximum_processes,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
        combined_output_bytes=combined_output_bytes,
    )


def request(
    operation: SyntheticOperation = SyntheticOperation.SUCCESS,
    *,
    suffix: str = "success",
    arguments: SyntheticArgumentsV1 | None = None,
) -> IsolatedJobRequestV1:
    return IsolatedJobRequestV1(
        run_id=f"Run-isolated-{suffix}",
        execution_id=f"job-{suffix}",
        attempt_id=f"attempt-{suffix}",
        context_id=f"context-{suffix}",
        budget_run_id=f"Budget-isolated-{suffix}",
        budget_permit_id=f"permit-{suffix}",
        operation=operation,
        arguments=arguments or SyntheticArgumentsV1(),
    )


def context_for(
    value: IsolatedJobRequestV1,
    *,
    now: datetime = NOW,
):
    assignment = AgentAssignment(
        assignment_id=f"assignment-{value.execution_id}",
        agent_role="isolated_job",
        task="Run the fixed repository synthetic helper.",
        context_keys=["kind", "objective"],
    )
    envelope = build_context_envelope(
        AgentContext(kind="calculation", objective="isolated_job_qualification"),
        assignment,
        run_id=value.run_id,
        expires_at=now + timedelta(hours=1),
        clock=lambda: now,
        context_id=value.context_id,
        attempt_id=value.attempt_id,
    )
    return assignment, envelope


def lineage_for(
    value: IsolatedJobRequestV1,
    envelope: ContextEnvelope | None = None,
) -> ExecutionLineageV1:
    request_hash = isolated_job_sha256(value)
    source_sha256 = (
        hashlib.sha256(b"context-source").hexdigest()
        if envelope is None
        else envelope.lineage.source_sha256
    )
    policy_sha256 = (
        hashlib.sha256(b"context-policy").hexdigest()
        if envelope is None
        else envelope.policy_sha256
    )
    integrity_sha256 = (
        hashlib.sha256(b"context-integrity").hexdigest()
        if envelope is None
        else envelope.integrity_sha256
    )
    return ExecutionLineageV1(
        owner_kind=OwnerKind.INTERNAL,
        owner_id="p2-028a",
        role="isolated_job",
        phase_id="isolated_job",
        assignment_id=f"assignment-{value.execution_id}",
        root_attempt_id=value.attempt_id,
        attempt_id=value.attempt_id,
        root_context_id=value.context_id,
        context_id=value.context_id,
        context_source_sha256=source_sha256,
        context_policy_sha256=policy_sha256,
        context_integrity_sha256=integrity_sha256,
        execution_ordinal=0,
        idempotency_key=value.execution_id,
        idempotency_request_sha256=request_hash,
    )


def durable_policy() -> DurableBudgetPolicyV1:
    return DurableBudgetPolicyV1(
        base_policy=BudgetPolicyV2(
            max_deliberation_rounds=3,
            max_tool_retries=0,
            max_runtime_seconds=30,
            max_tool_input_bytes=2 * 1024 * 1024,
            max_tool_output_bytes=2 * 1024 * 1024,
            max_artifact_bytes=2 * 1024 * 1024,
            max_run_bytes=8 * 1024 * 1024,
        )
    )


def policy_for(
    workspace: Path,
    *,
    job_limits: JobLimitsV1 | None = None,
    approved_input: Path | None = None,
):
    return qualify_isolated_job_policy(
        job_limits or limits(),
        workspace_root=workspace,
        approved_input=approved_input,
    )
