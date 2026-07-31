from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from poker_deliberation.isolated_jobs.canonical import (
    canonical_child_argv,
    canonical_windows_command_line,
    isolated_job_sha256,
)
from poker_deliberation.isolated_jobs.models import (
    ExecutionIdentityV1,
    IsolatedJobRequestV1,
    SyntheticArgumentsV1,
    SyntheticOperation,
)
from tests.isolated_job_support import NOW, context_for, lineage_for, policy_for, request

pytestmark = pytest.mark.skipif(
    __import__("sys").platform != "win32",
    reason="Windows-qualified isolated-job contracts",
)


def test_request_rejects_unknown_version_field_network_and_argument_shape() -> None:
    baseline = request()
    payload = baseline.model_dump(mode="python")

    with pytest.raises(ValidationError):
        IsolatedJobRequestV1.model_validate(
            payload | {"schema_version": "2.0.0"},
            strict=True,
        )
    with pytest.raises(ValidationError):
        IsolatedJobRequestV1.model_validate(
            payload | {"invented": True},
            strict=True,
        )
    with pytest.raises(ValidationError):
        IsolatedJobRequestV1.model_validate(
            payload | {"requested_network_access": True},
            strict=True,
        )
    with pytest.raises(ValidationError, match="closed operation matrix"):
        IsolatedJobRequestV1.model_validate(
            payload
            | {
                "arguments": SyntheticArgumentsV1(duration_ms=10),
            },
            strict=True,
        )


def test_closed_argv_has_no_shell_code_module_or_free_text(tmp_path) -> None:
    workspace = tmp_path.resolve()
    value = request(
        SyntheticOperation.HANG,
        suffix="argv",
        arguments=SyntheticArgumentsV1(duration_ms=25),
    )
    policy = policy_for(workspace)

    argv = canonical_child_argv(value, policy)
    command_line = canonical_windows_command_line(argv)

    assert argv[1:8] == ("-I", "-S", "-B", "-u", "-X", "utf8", argv[7])
    assert "-c" not in argv
    assert "-m" not in argv
    assert "cmd" not in command_line.lower()
    assert "powershell" not in command_line.lower()
    assert argv[-2:] == ("--duration-ms", "25")


def test_preview_binds_context_budget_identity_and_secret_reference_set(tmp_path) -> None:
    from poker_deliberation.budgets.durable_store import (
        DurableBudgetStore,
        initialize_durable_budget_root,
    )
    from poker_deliberation.isolated_jobs.coordinator import IsolatedJobCoordinator
    from poker_deliberation.isolated_jobs.store import (
        IsolatedJobStore,
        initialize_isolated_job_root,
    )

    value = request(suffix="preview")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    budget_root = tmp_path / "budget"
    jobs_root = tmp_path / "jobs"
    initialize_durable_budget_root(
        budget_root,
        legacy,
        root_id="root-" + "1" * 32,
        initialized_at=NOW,
    )
    initialize_isolated_job_root(
        jobs_root,
        legacy,
        root_id="root-" + "2" * 32,
        initialized_at=NOW,
    )
    budget = DurableBudgetStore(
        budget_root,
        legacy,
        wall_clock=lambda: NOW,
    )
    from tests.isolated_job_support import durable_policy

    budget.create(
        value.budget_run_id,
        durable_policy(),
        operation_id="initialize-preview",
    )
    assignment, envelope = context_for(value)
    policy = policy_for(workspace)
    coordinator = IsolatedJobCoordinator(
        IsolatedJobStore(jobs_root, legacy, clock=lambda: NOW),
        budget,
        terminal_store=object(),  # type: ignore[arg-type]
        clock=lambda: NOW,
    )

    preview = coordinator.preview(
        value,
        policy,
        context_envelope=envelope,
        assignment=assignment,
        budget_lineage=lineage_for(value, envelope),
        action_expires_at=NOW + timedelta(minutes=30),
    )
    outbound = {
        item.field_name: item.content_sha256 for item in preview.action_plan.outbound_fields
    }

    assert preview.action_plan.action_category == "external_code"
    assert preview.action_plan.executor_kind == "local_process"
    assert preview.action_plan.environment_name_allowlist == ()
    assert preview.action_plan.maximum_processes == policy.limits.maximum_processes
    assert outbound["request_sha256"] == isolated_job_sha256(value)
    assert outbound["execution_identity_sha256"] == policy.execution_identity.identity_sha256
    assert outbound["isolation_requirement_sha256"] == preview.isolation_requirement.request_sha256
    assert (
        outbound["isolation_evidence_sha256"]
        == preview.isolation_evidence.isolation_evidence_sha256
    )
    assert preview.isolation_evidence.satisfies(preview.isolation_requirement)
    assert preview.isolation_evidence.remote_cancellation_confirmed is False
    assert preview.isolation_evidence.external_code_isolation_confirmed is False

    with pytest.raises(ValueError, match="budget_mismatch"):
        coordinator.preview(
            value,
            policy,
            context_envelope=envelope,
            assignment=assignment,
            budget_lineage=lineage_for(value, envelope).model_copy(
                update={"context_integrity_sha256": "f" * 64}
            ),
            action_expires_at=NOW + timedelta(minutes=30),
        )

    identity_payload = policy.execution_identity.model_dump(mode="python")
    with pytest.raises(ValidationError, match="identity hash mismatch"):
        ExecutionIdentityV1.model_validate(
            identity_payload | {"identity_sha256": "f" * 64},
            strict=True,
        )
    assert outbound["context_integrity_sha256"] == envelope.integrity_sha256
    assert outbound["budget_policy_sha256"] == preview.budget_binding.policy_sha256
