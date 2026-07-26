from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.approval_models import (
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
    ApprovalReissueBatchV2,
    ApprovalReissueItemV2,
    ApprovalReissueSuccessorV2,
)
from poker_deliberation.approvals import LocalCliAuthorityProvider, read_approval_state_v2
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_domain_sha256,
)
from poker_deliberation.storage.terminal_canonical import product_payload_commitments
from poker_deliberation.storage.terminal_models import RunReadStatus

NOW = datetime(2026, 7, 26, 0, 0, tzinfo=UTC)
REISSUED_AT = NOW + timedelta(hours=2)
HASH_A = "a" * 64


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _proposal() -> dict[str, object]:
    return {
        "schema_version": "2.0.0",
        "stable_proposal_id": "proposal-lifecycle-source",
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
            "execution_id": "execution-lifecycle-source",
            "remote_idempotency_key": "remote-lifecycle-source",
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
    names = {payload.inventory.logical_name for payload in read.payloads}
    state = read_approval_state_v2(
        read.payload_bytes("approval_ledger_v2.json"),
        read.payload_bytes("approval_decisions_v2.jsonl"),
        read.payload_bytes("approval_audit_v2.jsonl"),
        (
            read.payload_bytes("approval_reissues_v2.jsonl")
            if "approval_reissues_v2.jsonl" in names
            else b""
        ),
    )
    return read, state


def test_reissue_resume_is_atomic_replayable_and_lifecycle_bound(tmp_path: Path) -> None:
    config = _config(tmp_path)
    creator = Orchestrator(config, terminal_clock=lambda: NOW)
    report = creator.run(
        CaseInput(
            kind="strategy",
            raw_text="review",
            analysis_scope="retrospective",
            metadata={"approval_requests": [_proposal()]},
        ),
        run_id="run-approval-lifecycle",
    )
    checkpoint, source_state = _state(creator, report.run_id)
    source = source_state.ledger.requests[0]
    assert checkpoint.manifest.approval_lineage_head_sha256 == canonical_domain_sha256(
        "poker-product-approval-authority-lineage-v2",
        {
            "ledger_sha256": source_state.ledger_sha256,
            "decision_count": 0,
            "decision_log_head_sha256": None,
            "domain_audit_count": 0,
            "domain_audit_log_head_sha256": None,
        },
    )
    successor_plan = source.action_plan.model_copy(
        update={
            "execution_id": "execution-lifecycle-successor",
            "remote_idempotency_key": "remote-lifecycle-successor",
            "expires_at": REISSUED_AT + timedelta(hours=1),
        }
    )
    batch = ApprovalReissueBatchV2(
        run_id=report.run_id,
        expected_run_revision=checkpoint.revision,
        expected_ledger_revision=source_state.ledger.ledger_revision,
        reissue_id="reissue-lifecycle",
        idempotency_key="reissue-key-lifecycle",
        items=(
            ApprovalReissueItemV2(
                source_kind="approval_v2",
                source_request_id=source.request_id,
                expected_source_request_revision=source.request_revision,
                source_action_digest_sha256=source.action_digest_sha256,
                successor=ApprovalReissueSuccessorV2(
                    stable_proposal_id="proposal-lifecycle-successor",
                    action_plan=successor_plan,
                    display=source.display.model_copy(
                        update={"requested_action": "Submit the reissued redacted request."}
                    ),
                    source_phase_id="resume",
                    source_attempt_id="attempt-lifecycle-reissue",
                ),
            ),
        ),
        reason="Replace the exact expired request.",
        reissued_at=REISSUED_AT,
    )
    reissuer = Orchestrator(config, terminal_clock=lambda: REISSUED_AT)

    resumed = reissuer.resume(report.run_id, reissue_batch=batch)
    after_reissue, reissued_state = _state(reissuer, report.run_id)
    replay = Orchestrator(
        config,
        terminal_clock=lambda: REISSUED_AT,
    ).reissue_approvals(batch)
    after_replay = reissuer.product_store.read_current(report.run_id)
    successor = next(
        request for request in reissued_state.ledger.requests if request.state == "pending"
    )

    assert resumed.run_status == "approval_required"
    assert after_reissue.revision == 2
    assert after_reissue.read_status is RunReadStatus.APPROVAL_REQUIRED
    assert after_reissue.resume_eligible is True
    assert replay.current_run_revision == 2
    assert after_replay.current_pointer_sha256 == after_reissue.current_pointer_sha256
    assert len(reissued_state.reissue_records) == 1
    with pytest.raises(CanonicalStorageError, match="previous terminal lineage"):
        product_payload_commitments(
            {
                payload.inventory.logical_name: payload.exact_bytes
                for payload in after_reissue.payloads
            },
            run_id=report.run_id,
            status=after_reissue.manifest.status,
            revision=after_reissue.revision,
            previous_manifest_sha256="f" * 64,
            previous_pointer_sha256=checkpoint.current_pointer_sha256,
        )
    assert (
        next(
            request
            for request in reissued_state.ledger.requests
            if request.request_id == source.request_id
        ).state
        == "superseded"
    )

    provider = LocalCliAuthorityProvider("reviewer")
    decision = ApprovalDecisionBatch(
        run_id=report.run_id,
        expected_run_revision=after_reissue.revision,
        expected_ledger_revision=reissued_state.ledger.ledger_revision,
        actor=provider.actor(),
        decision_id="decision-lifecycle-successor",
        idempotency_key="decision-key-lifecycle-successor",
        items=(
            ApprovalDecisionItemV2(
                request_id=successor.request_id,
                expected_request_revision=successor.request_revision,
                action_digest_sha256=successor.action_digest_sha256,
                decision="rejected",
            ),
        ),
        reason="Reject safely.",
        decision_at=REISSUED_AT,
    )
    decider = Orchestrator(
        config,
        terminal_clock=lambda: REISSUED_AT,
        decision_authority_provider=provider,
    )
    outcome = decider.decide_approvals(decision)
    terminal, terminal_state = _state(decider, report.run_id)

    assert outcome.run_status == "completed"
    assert terminal.revision == 3
    assert terminal.read_status is RunReadStatus.SUCCEEDED
    assert terminal.resume_eligible is False
    assert len(terminal_state.reissue_records) == 1
    assert len(terminal_state.decision_records) == 1
