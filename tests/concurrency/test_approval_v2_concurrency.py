from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

from poker_deliberation.approval_models import (
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
    ApprovalFailureCode,
)
from poker_deliberation.approvals import (
    ApprovalDecisionValidationError,
    LocalCliAuthorityProvider,
    read_approval_state_v2,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput

NOW = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
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
        "stable_proposal_id": "proposal-concurrency",
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
            "execution_id": "execution-concurrency",
            "remote_idempotency_key": "remote-concurrency",
            "expires_at": (NOW + timedelta(hours=1)).isoformat(),
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


class _CoordinatedProvider(LocalCliAuthorityProvider):
    def __init__(
        self,
        actor_id: str,
        *,
        barrier: Barrier,
        winner_done: Event,
        wait_for_winner: bool,
    ) -> None:
        super().__init__(actor_id)
        self._barrier = barrier
        self._winner_done = winner_done
        self._wait_for_winner = wait_for_winner
        self._calls = 0

    def resolve_actor(self, actor_id: str, *, decision_at: datetime):  # type: ignore[no-untyped-def]
        self._calls += 1
        if self._calls == 1:
            self._barrier.wait(timeout=10)
            if self._wait_for_winner:
                assert self._winner_done.wait(timeout=10)
        return super().resolve_actor(actor_id, decision_at=decision_at)


def test_two_deciders_have_one_cas_winner_and_no_lost_update(
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
        run_id="run-approval-concurrency",
    )
    checkpoint = creator.product_store.read_current(report.run_id)
    state = read_approval_state_v2(
        checkpoint.payload_bytes("approval_ledger_v2.json"),
        checkpoint.payload_bytes("approval_decisions_v2.jsonl"),
        checkpoint.payload_bytes("approval_audit_v2.jsonl"),
    )
    request = state.ledger.requests[0]
    barrier = Barrier(2)
    winner_done = Event()
    fast_provider = _CoordinatedProvider(
        "fast-reviewer",
        barrier=barrier,
        winner_done=winner_done,
        wait_for_winner=False,
    )
    slow_provider = _CoordinatedProvider(
        "slow-reviewer",
        barrier=barrier,
        winner_done=winner_done,
        wait_for_winner=True,
    )

    def decide(
        provider: _CoordinatedProvider,
        decision_id: str,
        idempotency_key: str,
        *,
        winner: bool,
    ):
        batch = ApprovalDecisionBatch(
            run_id=report.run_id,
            expected_run_revision=checkpoint.revision,
            expected_ledger_revision=state.ledger.ledger_revision,
            actor=provider.actor(),
            decision_id=decision_id,
            idempotency_key=idempotency_key,
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
        try:
            return Orchestrator(
                config,
                terminal_clock=lambda: NOW,
                decision_authority_provider=provider,
            ).decide_approvals(batch)
        except ApprovalDecisionValidationError as exc:
            return exc.failure
        finally:
            if winner:
                winner_done.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        fast = executor.submit(
            decide,
            fast_provider,
            "decision-fast",
            "decision-key-fast",
            winner=True,
        )
        slow = executor.submit(
            decide,
            slow_provider,
            "decision-slow",
            "decision-key-slow",
            winner=False,
        )
        results = (fast.result(timeout=30), slow.result(timeout=30))

    current = creator.product_store.read_current(report.run_id)
    committed = read_approval_state_v2(
        current.payload_bytes("approval_ledger_v2.json"),
        current.payload_bytes("approval_decisions_v2.jsonl"),
        current.payload_bytes("approval_audit_v2.jsonl"),
    )
    pointer, events = creator.product_store.read_approval_failure_audit(report.run_id)

    assert results[0].outcome_kind == "committed"
    assert results[1].code is ApprovalFailureCode.STALE_DECISION
    assert results[1].audit_confirmed is True
    assert current.revision == 2
    assert committed.ledger.decision_count == 1
    assert committed.ledger.requests[0].state == "rejected"
    assert pointer.audit_sequence == 1
    assert events[0].failure_code is ApprovalFailureCode.STALE_DECISION
