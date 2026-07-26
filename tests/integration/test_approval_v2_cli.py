from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from poker_deliberation.approval_canonical import canonical_json_bytes
from poker_deliberation.approval_models import (
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
    ApprovalReissueBatchV2,
    ApprovalReissueItemV2,
    ApprovalReissueSuccessorV2,
)
from poker_deliberation.approvals import LocalCliAuthorityProvider, read_approval_state_v2
from poker_deliberation.cli import main
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput

HASH_A = "a" * 64


def _set_roots(monkeypatch, tmp_path: Path) -> AppConfig:  # type: ignore[no-untyped-def]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POKER_DELIBERATION_RUNS_DIR", str(tmp_path / "legacy"))
    monkeypatch.setenv("POKER_DELIBERATION_REVISION_RUNS_DIR", str(tmp_path / "product"))
    monkeypatch.setenv(
        "POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR",
        str(tmp_path / "budget"),
    )
    return AppConfig.from_env()


def _proposal() -> dict[str, object]:
    return {
        "schema_version": "2.0.0",
        "stable_proposal_id": "proposal-cli",
        "action_plan": {
            "schema_version": "2.0.0",
            "operation": "Review one unavailable provider action.",
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
            "maximum_runtime_ms": 1,
            "maximum_memory_bytes": 1,
            "maximum_output_bytes": 1,
            "maximum_processes": 1,
            "working_directory": None,
            "environment_name_allowlist": [],
            "expected_result_type": "none",
            "execution_id": "execution-cli",
            "remote_idempotency_key": "remote-cli",
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        },
        "display": {
            "requested_action": "Review the action.",
            "reason": "Approval is required.",
            "expected_benefit": "An exact decision.",
            "risks": ["External disclosure."],
            "data_to_be_sent": [],
            "cost_or_resource_estimate": "None.",
            "alternatives": ["Reject."],
            "effect_of_declining": "No external action.",
            "exact_command_or_tool_call": None,
        },
    }


def _checkpoint(config: AppConfig, run_id: str):
    orchestrator = Orchestrator(config)
    report = orchestrator.run(
        CaseInput(
            kind="strategy",
            raw_text="review",
            analysis_scope="retrospective",
            metadata={"approval_requests": [_proposal()]},
        ),
        run_id=run_id,
    )
    read = orchestrator.product_store.read_current(run_id)
    state = read_approval_state_v2(
        read.payload_bytes("approval_ledger_v2.json"),
        read.payload_bytes("approval_decisions_v2.jsonl"),
        read.payload_bytes("approval_audit_v2.jsonl"),
    )
    return report, read, state


def test_cli_legacy_reject_flags_build_a_v2_batch_and_exit_zero(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    config = _set_roots(monkeypatch, tmp_path)
    report, read, state = _checkpoint(config, "run-approval-cli-flags")
    request = state.ledger.requests[0]

    exit_code = main(
        [
            "resume",
            report.run_id,
            "--reject",
            request.request_id,
            "--actor-id",
            "cli-reviewer",
            "--decision-id",
            "decision-cli-reject",
            "--idempotency-key",
            "decision-key-cli-reject",
            "--expected-run-revision",
            str(read.revision),
            "--expected-ledger-revision",
            str(state.ledger.ledger_revision),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["run_status"] == "completed"
    assert payload["approvals"][0]["status"] == "rejected"


def test_cli_decision_file_is_versioned_and_local_approve_fails_structured(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    config = _set_roots(monkeypatch, tmp_path)
    report, read, state = _checkpoint(config, "run-approval-cli-file")
    request = state.ledger.requests[0]
    provider = LocalCliAuthorityProvider("cli-reviewer")
    batch = ApprovalDecisionBatch(
        run_id=report.run_id,
        expected_run_revision=read.revision,
        expected_ledger_revision=state.ledger.ledger_revision,
        actor=provider.actor(),
        decision_id="decision-cli-approve",
        idempotency_key="decision-key-cli-approve",
        items=(
            ApprovalDecisionItemV2(
                request_id=request.request_id,
                expected_request_revision=request.request_revision,
                action_digest_sha256=request.action_digest_sha256,
                decision="approved",
            ),
        ),
        reason="Attempt local approval.",
        decision_at=datetime.now(UTC),
    )
    decision_file = tmp_path / "decision.json"
    decision_file.write_bytes(canonical_json_bytes(batch))

    exit_code = main(
        [
            "resume",
            report.run_id,
            "--decision-file",
            str(decision_file),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert payload["code"] == "unauthorized_decision"
    assert payload["audit_confirmed"] is True
    assert Orchestrator(config).product_store.read_current(report.run_id).revision == 1


def test_cli_reissue_file_publishes_explicit_expired_successor(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:  # type: ignore[no-untyped-def]
    config = _set_roots(monkeypatch, tmp_path)
    report, read, state = _checkpoint(config, "run-approval-cli-reissue")
    request = state.ledger.requests[0]
    reissued_at = request.expires_at
    batch = ApprovalReissueBatchV2(
        run_id=report.run_id,
        expected_run_revision=read.revision,
        expected_ledger_revision=state.ledger.ledger_revision,
        reissue_id="reissue-cli",
        idempotency_key="reissue-key-cli",
        items=(
            ApprovalReissueItemV2(
                source_kind="approval_v2",
                source_request_id=request.request_id,
                expected_source_request_revision=request.request_revision,
                source_action_digest_sha256=request.action_digest_sha256,
                successor=ApprovalReissueSuccessorV2(
                    stable_proposal_id="proposal-cli-successor",
                    action_plan=request.action_plan.model_copy(
                        update={
                            "execution_id": "execution-cli-successor",
                            "remote_idempotency_key": "remote-cli-successor",
                            "expires_at": reissued_at + timedelta(days=1),
                        }
                    ),
                    display=request.display,
                    source_phase_id="resume",
                    source_attempt_id="attempt-cli-reissue",
                ),
            ),
        ),
        reason="Replace the exact expired request.",
        reissued_at=reissued_at,
    )
    reissue_file = tmp_path / "reissue.json"
    reissue_file.write_bytes(canonical_json_bytes(batch))
    monkeypatch.setattr(
        "poker_deliberation.cli.Orchestrator",
        lambda *args, **kwargs: Orchestrator(
            *args,
            terminal_clock=lambda: reissued_at,
            **kwargs,
        ),
    )

    exit_code = main(
        [
            "resume",
            report.run_id,
            "--reissue-file",
            str(reissue_file),
            "--format",
            "json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    current = Orchestrator(config).product_store.read_current(report.run_id)
    committed = read_approval_state_v2(
        current.payload_bytes("approval_ledger_v2.json"),
        current.payload_bytes("approval_decisions_v2.jsonl"),
        current.payload_bytes("approval_audit_v2.jsonl"),
        current.payload_bytes("approval_reissues_v2.jsonl"),
    )

    assert exit_code == 3
    assert payload["run_status"] == "approval_required"
    assert current.revision == 2
    assert len(committed.reissue_records) == 1
    assert sum(request.state == "pending" for request in committed.ledger.requests) == 1
