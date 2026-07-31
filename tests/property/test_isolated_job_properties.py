from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from poker_deliberation.isolated_jobs.canonical import isolated_job_sha256
from poker_deliberation.isolated_jobs.models import (
    ApprovalJobReferenceV1,
    BudgetJobBindingV1,
    ContextJobBindingV1,
    DurableIsolatedJobStateV1,
    IsolatedJobError,
    IsolatedJobRequestV1,
    IsolatedJobStatus,
    JobEvidenceV1,
    JobFailureCode,
)
from poker_deliberation.isolated_jobs.store import (
    IsolatedJobStore,
    initialize_isolated_job_root,
)
from tests.isolated_job_support import NOW, policy_for, request

pytestmark = pytest.mark.skipif(
    __import__("sys").platform != "win32",
    reason="Windows-qualified isolated-job policy",
)


@given(
    suffix=st.text(
        alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-",
        min_size=1,
        max_size=24,
    )
)
def test_request_canonical_round_trip_and_hash_are_stable(suffix: str) -> None:
    value = request(suffix=suffix)

    replay = IsolatedJobRequestV1.model_validate_json(
        value.model_dump_json(),
        strict=True,
    )

    assert replay == value
    assert isolated_job_sha256(replay) == isolated_job_sha256(value)


@given(
    path=st.sampled_from(
        (
            ("failed",),
            ("launch_committed", "failed"),
            ("launch_committed", "running", "failed"),
            ("effect_unknown", "reconciled"),
        )
    )
)
@settings(max_examples=8, deadline=None)
def test_durable_state_machine_allows_only_exact_successors_and_latches_terminal(
    path: tuple[str, ...],
) -> None:
    repository_tmp = Path(__file__).resolve().parents[2] / "tmp"
    repository_tmp.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2-028a-property-", dir=repository_tmp) as directory:
        root = Path(directory)
        legacy = root / "legacy"
        workspace = root / "workspace"
        legacy.mkdir()
        workspace.mkdir()
        revision = root / "jobs"
        initialize_isolated_job_root(
            revision,
            legacy,
            root_id="root-" + "a" * 32,
            initialized_at=NOW,
        )
        value = request(suffix="property-state")
        policy = policy_for(workspace)
        context = ContextJobBindingV1(
            context_id=value.context_id,
            attempt_id=value.attempt_id,
            payload_sha256="1" * 64,
            source_sha256="2" * 64,
            policy_sha256="3" * 64,
            integrity_sha256="4" * 64,
            expires_at=NOW + timedelta(hours=1),
        )
        budget = BudgetJobBindingV1(
            budget_run_id=value.budget_run_id,
            permit_id=value.budget_permit_id,
            policy_sha256="5" * 64,
            activation_sha256="6" * 64,
            reservation_sha256="7" * 64,
            lineage_sha256="8" * 64,
            isolation_requirement_sha256="f" * 64,
            isolation_evidence_sha256="0" * 64,
            isolation_boundary_id="windows-job-object-repository-synthetic-v1",
        )
        store = IsolatedJobStore(revision, legacy, clock=lambda: NOW)
        store.create(
            value,
            policy,
            action_digest_sha256="9" * 64,
            context_binding=context,
            budget_binding=budget,
            approval_reference=ApprovalJobReferenceV1(
                approval_run_id="Approval-property",
                approval_revision=2,
                approval_pointer_sha256="a" * 64,
                approval_manifest_sha256="b" * 64,
                request_id="request-property",
            ),
            approval_recheck_binding_sha256="c" * 64,
        )

        for status_name in path:
            status = IsolatedJobStatus(status_name)
            kwargs = {}
            if status is IsolatedJobStatus.LAUNCH_COMMITTED:
                kwargs = {
                    "process_id": 123,
                    "process_creation_time_100ns": 456,
                    "effect_admission_recheck_binding_sha256": "d" * 64,
                }
            elif status in {
                IsolatedJobStatus.FAILED,
                IsolatedJobStatus.EFFECT_UNKNOWN,
            }:
                kwargs = {
                    "failure_code": (
                        JobFailureCode.EFFECT_UNKNOWN
                        if status is IsolatedJobStatus.EFFECT_UNKNOWN
                        else JobFailureCode.CHILD_EXIT_NONZERO
                    )
                }
                if (
                    status is IsolatedJobStatus.FAILED
                    and store.load(value.execution_id).process_id is not None
                ):
                    kwargs["evidence"] = JobEvidenceV1(
                        process_id=123,
                        process_creation_time_100ns=456,
                        exit_code=73,
                        termination_reason=JobFailureCode.CHILD_EXIT_NONZERO.value,
                        stdout_sha256=hashlib.sha256(b"").hexdigest(),
                        stderr_sha256=hashlib.sha256(b"").hexdigest(),
                        command_line_sha256="0" * 64,
                        inherited_handle_count=3,
                        total_processes=1,
                        process_tree_termination_confirmed=True,
                        job_limits_requeried=True,
                        executable_identity_rechecked=True,
                        output_complete=True,
                    )
            elif status is IsolatedJobStatus.RECONCILED:
                kwargs = {
                    "failure_code": JobFailureCode.RECONCILIATION_REQUIRED,
                    "reconciliation_evidence_sha256": "e" * 64,
                }
            store.transition(
                value.execution_id,
                status=status,
                reason_code=f"property_{status.value}",
                **kwargs,
            )

        terminal = store.load(value.execution_id)
        assert terminal.status.value == path[-1]
        assert terminal.generation == len(path) + 1
        forged_payload = terminal.model_dump(mode="python")
        forged_events = list(forged_payload["events"])
        forged_events[-1] = forged_events[-1] | {"state_sha256": "f" * 64}
        with pytest.raises(ValidationError, match="event hash mismatch"):
            DurableIsolatedJobStateV1.model_validate(
                forged_payload | {"events": tuple(forged_events)},
                strict=True,
            )
        with pytest.raises(IsolatedJobError) as replay:
            store.transition(
                value.execution_id,
                status=IsolatedJobStatus.COMPLETED,
                reason_code="forbidden_terminal_promotion",
            )
        assert replay.value.code is JobFailureCode.STALE_REPLAY
