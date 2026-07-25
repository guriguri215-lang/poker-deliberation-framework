from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.approval_canonical import approval_actor_sha256
from poker_deliberation.approval_models import (
    ApprovalActor,
    ApprovalAuthoritySnapshotV2,
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
)
from poker_deliberation.approvals import (
    ApprovalDecisionValidationError,
    LocalCliAuthorityProvider,
    UnavailableExternalExecutionBindingProvider,
    read_approval_state_v2,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.terminal_models import RunReadStatus

NOW = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _proposal() -> dict[str, object]:
    return {
        "schema_version": "2.0.0",
        "stable_proposal_id": "proposal-transaction",
        "action_plan": {
            "schema_version": "2.0.0",
            "operation": "Submit one redacted request.",
            "action_category": "external_service",
            "executor_kind": "provider",
            "executor_identifier": "provider.example",
            "executor_version": "1.0.0",
            "executor_sha256": HASH_A,
            "executor_availability": "unavailable",
            "outbound_fields": [],
            "destination_kind": "provider",
            "destination_identifier": "provider.example/review",
            "retention_policy_id": "retention-none",
            "trace_policy_id": "trace-redacted-v1",
            "maximum_cost_microunits": 0,
            "maximum_runtime_ms": 1000,
            "maximum_memory_bytes": 1024,
            "maximum_output_bytes": 1024,
            "maximum_processes": 1,
            "working_directory": None,
            "environment_name_allowlist": [],
            "expected_result_type": "none",
            "execution_id": "execution-transaction",
            "remote_idempotency_key": "remote-transaction",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
        },
        "display": {
            "requested_action": "Submit one redacted request.",
            "reason": "A human decision is required.",
            "expected_benefit": "Record an exact decision.",
            "risks": ["External disclosure."],
            "data_to_be_sent": [],
            "cost_or_resource_estimate": "No charge.",
            "alternatives": ["Reject the action."],
            "effect_of_declining": "No external action is performed.",
            "exact_command_or_tool_call": None,
        },
    }


def _state(orchestrator: Orchestrator, run_id: str):
    read = orchestrator.product_store.read_current(run_id)
    return read, read_approval_state_v2(
        read.payload_bytes("approval_ledger_v2.json"),
        read.payload_bytes("approval_decisions_v2.jsonl"),
        read.payload_bytes("approval_audit_v2.jsonl"),
    )


def test_reject_transaction_is_atomic_and_exact_replay_is_write_zero(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    creator = Orchestrator(config, terminal_clock=lambda: NOW)
    report = creator.run(
        CaseInput(
            kind="strategy",
            raw_text="review",
            analysis_scope="retrospective",
            metadata={"approval_requests": [_proposal()]},
        ),
        run_id="run-approval-v2-reject",
    )
    checkpoint, state = _state(creator, report.run_id)
    request = state.ledger.requests[0]
    provider = LocalCliAuthorityProvider("reviewer")
    batch = ApprovalDecisionBatch(
        run_id=report.run_id,
        expected_run_revision=checkpoint.revision,
        expected_ledger_revision=state.ledger.ledger_revision,
        actor=provider.actor(),
        decision_id="decision-reject",
        idempotency_key="decision-key-reject",
        items=(
            ApprovalDecisionItemV2(
                request_id=request.request_id,
                expected_request_revision=request.request_revision,
                action_digest_sha256=request.action_digest_sha256,
                decision="rejected",
            ),
        ),
        reason="Reject safely.",
        decision_at=NOW,
    )
    decider = Orchestrator(
        config,
        terminal_clock=lambda: NOW,
        decision_authority_provider=provider,
    )

    outcome = decider.decide_approvals(batch)
    terminal, committed = _state(decider, report.run_id)
    replay = Orchestrator(
        config,
        terminal_clock=lambda: NOW,
        decision_authority_provider=provider,
    ).decide_approvals(batch)
    after_replay = decider.product_store.read_current(report.run_id)

    assert report.run_status == "approval_required"
    assert outcome.run_status == "completed"
    assert terminal.read_status is RunReadStatus.SUCCEEDED
    assert terminal.revision == 2
    assert committed.ledger.requests[0].state == "rejected"
    assert committed.ledger.decision_count == 1
    assert committed.ledger.domain_audit_count == 1
    assert replay == outcome
    assert after_replay.current_pointer_sha256 == terminal.current_pointer_sha256


def test_validation_failure_changes_no_domain_revision_and_is_audited(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    creator = Orchestrator(config, terminal_clock=lambda: NOW)
    report = creator.run(
        CaseInput(
            kind="strategy",
            raw_text="review",
            analysis_scope="retrospective",
            metadata={"approval_requests": [_proposal()]},
        ),
        run_id="run-approval-v2-failure-audit",
    )
    checkpoint, state = _state(creator, report.run_id)
    provider = LocalCliAuthorityProvider("reviewer")
    batch = ApprovalDecisionBatch(
        run_id=report.run_id,
        expected_run_revision=checkpoint.revision,
        expected_ledger_revision=state.ledger.ledger_revision,
        actor=provider.actor(),
        decision_id="decision-unknown",
        idempotency_key="decision-key-unknown",
        items=(
            ApprovalDecisionItemV2(
                request_id="request-unknown",
                expected_request_revision=1,
                action_digest_sha256="0" * 64,
                decision="rejected",
            ),
        ),
        reason="This raw reason must not be in the control audit.",
        decision_at=NOW,
    )
    decider = Orchestrator(
        config,
        terminal_clock=lambda: NOW,
        decision_authority_provider=provider,
    )

    with pytest.raises(ApprovalDecisionValidationError) as captured:
        decider.decide_approvals(batch)

    current = decider.product_store.read_current(report.run_id)
    pointer, events = decider.product_store.read_approval_failure_audit(report.run_id)
    audit_root = (
        config.revision_runs_dir
        / "runs"
        / report.run_id
        / ".terminal-store"
        / "approval-failure-audit"
    )
    audit_bytes = b"".join(path.read_bytes() for path in audit_root.rglob("*.json"))

    assert captured.value.failure.code.value == "approval_unknown"
    assert captured.value.failure.audit_confirmed is True
    assert current.current_pointer_sha256 == checkpoint.current_pointer_sha256
    assert current.revision == checkpoint.revision
    assert pointer.audit_sequence == 1
    assert events[0].failure_code.value == "approval_unknown"
    assert batch.reason.encode() not in audit_bytes


class _VerifiedProvider:
    def __init__(self) -> None:
        self.actor = ApprovalActor(
            actor_id="verified-reviewer",
            actor_type="human",
            authority_source="test-authority",
            authority_scopes=("approve:external_service", "reject:any"),
            verification_status="verified",
            verification_reference_sha256=HASH_A,
            session_reference_sha256=HASH_B,
            credential_reference_sha256=HASH_A,
            verified_at=NOW - timedelta(minutes=1),
            authority_expires_at=NOW + timedelta(hours=1),
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
            provider_id="test-authority",
            provider_version="1.0.0",
            resolved_at=decision_at,
            actor=self.actor,
            actor_sha256=approval_actor_sha256(self.actor),
        )


def test_verified_approve_records_authority_but_never_executes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    creator = Orchestrator(config, terminal_clock=lambda: NOW)
    report = creator.run(
        CaseInput(
            kind="strategy",
            raw_text="review",
            analysis_scope="retrospective",
            metadata={"approval_requests": [_proposal()]},
        ),
        run_id="run-approval-v2-approved-unavailable",
    )
    checkpoint, state = _state(creator, report.run_id)
    request = state.ledger.requests[0]
    provider = _VerifiedProvider()
    batch = ApprovalDecisionBatch(
        run_id=report.run_id,
        expected_run_revision=checkpoint.revision,
        expected_ledger_revision=state.ledger.ledger_revision,
        actor=provider.actor,
        decision_id="decision-approved",
        idempotency_key="decision-key-approved",
        items=(
            ApprovalDecisionItemV2(
                request_id=request.request_id,
                expected_request_revision=request.request_revision,
                action_digest_sha256=request.action_digest_sha256,
                decision="approved",
            ),
        ),
        reason="Approve the exact plan.",
        decision_at=NOW,
    )
    decider = Orchestrator(
        config,
        terminal_clock=lambda: NOW,
        decision_authority_provider=provider,
    )

    outcome = decider.decide_approvals(batch)
    terminal, committed = _state(decider, report.run_id)
    updated_request = committed.ledger.requests[0]
    binding = UnavailableExternalExecutionBindingProvider().bind_unavailable(
        updated_request,
        outcome,
        provider.resolve_actor(provider.actor.actor_id, decision_at=NOW),
    )

    assert outcome.run_status == "failed_with_limitations"
    assert outcome.limitation is not None
    assert outcome.limitation.code.value == "external_executor_unavailable"
    assert terminal.read_status is RunReadStatus.FAILED
    assert updated_request.state == "approved"
    assert binding.executor_status == "unavailable"
    assert binding.action_digest_sha256 == updated_request.action_digest_sha256
