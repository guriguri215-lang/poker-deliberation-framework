"""Canonical bytes, digests, argv, and approval plans for P2-028A."""

from __future__ import annotations

import hashlib
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from poker_deliberation.approval_models import (
    CanonicalActionPlanV2,
    OutboundFieldBindingV2,
)
from poker_deliberation.isolated_jobs.models import (
    BudgetJobBindingV1,
    ContextJobBindingV1,
    IsolatedJobPolicyV1,
    IsolatedJobRequestV1,
    SyntheticOperation,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes


def isolated_job_json(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    return isolated_job_bytes(value).decode("utf-8")


def isolated_job_bytes(value: BaseModel | dict[str, Any] | list[Any]) -> bytes:
    try:
        return canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("isolated-job value is not canonical JSON") from exc


def isolated_job_sha256(value: BaseModel | dict[str, Any] | list[Any]) -> str:
    return hashlib.sha256(isolated_job_bytes(value)).hexdigest()


def secret_reference_set_sha256(request: IsolatedJobRequestV1) -> str:
    return isolated_job_sha256([item.model_dump(mode="json") for item in request.secret_references])


def run_lineage_sha256(request: IsolatedJobRequestV1) -> str:
    return isolated_job_sha256(
        {
            "run_id": request.run_id,
            "execution_id": request.execution_id,
            "attempt_id": request.attempt_id,
            "context_id": request.context_id,
            "execution_ordinal": request.execution_ordinal,
        }
    )


def canonical_child_argv(
    request: IsolatedJobRequestV1,
    policy: IsolatedJobPolicyV1,
    *,
    input_handle: int | None = None,
) -> tuple[str, ...]:
    """Return the complete closed argv for the repository helper."""

    if request.operation is SyntheticOperation.COPY_HANDLES:
        if input_handle is None or input_handle < 0:
            raise ValueError("copy_handles requires one explicit numeric input handle")
    elif input_handle is not None:
        raise ValueError("only copy_handles may receive an explicit input handle")
    arguments = request.arguments
    argv = [
        policy.execution_identity.interpreter.absolute_path,
        "-I",
        "-S",
        "-B",
        "-u",
        "-X",
        "utf8",
        policy.execution_identity.synthetic_helper.absolute_path,
        "--protocol",
        "1.0.0",
        "--operation",
        request.operation.value,
    ]
    for name, flag in (
        ("duration_ms", "--duration-ms"),
        ("output_bytes", "--output-bytes"),
        ("memory_bytes", "--memory-bytes"),
        ("child_count", "--child-count"),
        ("exit_code", "--exit-code"),
    ):
        value = getattr(arguments, name)
        if value is not None:
            argv.extend((flag, str(value)))
    if input_handle is not None:
        argv.extend(("--input-handle", str(input_handle)))
    return tuple(argv)


def canonical_windows_command_line(argv: tuple[str, ...]) -> str:
    if not argv or any(not isinstance(item, str) or "\x00" in item for item in argv):
        raise ValueError("invalid Windows argv")
    return subprocess.list2cmdline(list(argv))


def command_line_sha256(command_line: str) -> str:
    return hashlib.sha256(command_line.encode("utf-16-le")).hexdigest()


def build_action_plan(
    request: IsolatedJobRequestV1,
    policy: IsolatedJobPolicyV1,
    context: ContextJobBindingV1,
    budget: BudgetJobBindingV1,
    *,
    expires_at: datetime,
) -> CanonicalActionPlanV2:
    fields = {
        "budget_activation_sha256": budget.activation_sha256,
        "budget_lineage_sha256": budget.lineage_sha256,
        "budget_policy_sha256": budget.policy_sha256,
        "budget_reservation_sha256": budget.reservation_sha256,
        "context_integrity_sha256": context.integrity_sha256,
        "execution_identity_sha256": policy.execution_identity.identity_sha256,
        "isolation_boundary_id": isolated_job_sha256(
            {"isolation_boundary_id": budget.isolation_boundary_id}
        ),
        "isolation_evidence_sha256": budget.isolation_evidence_sha256,
        "isolation_requirement_sha256": budget.isolation_requirement_sha256,
        "policy_sha256": isolated_job_sha256(policy),
        "request_sha256": isolated_job_sha256(request),
        "run_lineage_sha256": run_lineage_sha256(request),
        "secret_reference_set_sha256": secret_reference_set_sha256(request),
    }
    outbound = tuple(
        OutboundFieldBindingV2(
            field_name=name,
            classification="internal",
            content_sha256=value,
        )
        for name, value in sorted(fields.items(), key=lambda item: item[0].encode("utf-8"))
    )
    limits = policy.limits
    return CanonicalActionPlanV2(
        operation="repository_synthetic_isolated_job",
        action_category="external_code",
        executor_kind="local_process",
        executor_identifier="p2-028a-repository-synthetic-helper",
        executor_version="1.0.0",
        executor_sha256=policy.execution_identity.identity_sha256,
        executor_availability="available",
        outbound_fields=outbound,
        destination_kind="workspace",
        destination_identifier="repository-owned-synthetic-job-workspace",
        retention_policy_id="p2-027a-local-data-policy-v1",
        trace_policy_id="p2-028a-isolated-job-trace-v1",
        maximum_cost_microunits=0,
        maximum_runtime_ms=limits.wall_clock_ms,
        maximum_memory_bytes=limits.job_memory_bytes,
        maximum_output_bytes=limits.combined_output_bytes,
        maximum_processes=limits.maximum_processes,
        working_directory=str(Path(policy.filesystem.workspace_root.absolute_path)),
        environment_name_allowlist=(),
        expected_result_type="repository-synthetic-isolated-job-result-v1",
        execution_id=request.execution_id,
        remote_idempotency_key=request.execution_id,
        expires_at=expires_at,
    )
