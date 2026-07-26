from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.approval_canonical import (
    action_digest_sha256,
    approval_request_idempotency_key,
    canonical_json_bytes,
)
from poker_deliberation.approval_models import (
    ApprovalActor,
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
    ApprovalDisplayV2,
    ApprovalFailureCode,
    ApprovalLedgerV2,
    ApprovalRequestV2,
    CanonicalActionPlanV2,
)
from poker_deliberation.approvals import (
    ApprovalDecisionValidationError,
    ApprovalLedgerCorruptError,
    LocalCliAuthorityProvider,
    add_approval_request_v2,
    empty_approval_ledger_v2,
    encode_approval_state_v2,
    read_approval_state_v2,
    validate_approval_decision,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.terminal_store import (
    ApprovalFailureAuditError,
    ApprovalFailureAuditRequest,
)

NOW = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _state() -> tuple[ApprovalRequestV2, object]:
    plan = CanonicalActionPlanV2(
        operation="Review one exact action.",
        action_category="external_service",
        executor_kind="provider",
        executor_identifier="provider.example",
        executor_version="1.0.0",
        executor_sha256=HASH_A,
        executor_availability="unavailable",
        outbound_fields=(),
        destination_kind="provider",
        destination_identifier="provider.example/review",
        retention_policy_id="retention-none",
        trace_policy_id="trace-redacted-v1",
        maximum_cost_microunits=0,
        maximum_runtime_ms=1,
        maximum_memory_bytes=1,
        maximum_output_bytes=1,
        maximum_processes=1,
        environment_name_allowlist=(),
        expected_result_type="none",
        execution_id="execution-1",
        remote_idempotency_key="remote-1",
        expires_at=NOW + timedelta(hours=1),
    )
    request = ApprovalRequestV2(
        request_id="request-1",
        request_revision=1,
        ledger_revision=1,
        created_run_revision=2,
        stable_proposal_id="proposal-1",
        action_plan=plan,
        action_digest_sha256=action_digest_sha256(plan),
        display=ApprovalDisplayV2(
            requested_action="Review the action.",
            reason="Approval is required.",
            expected_benefit="A reviewed decision.",
            risks=("External disclosure.",),
            data_to_be_sent=(),
            cost_or_resource_estimate="None.",
            alternatives=("Reject.",),
            effect_of_declining="No external action.",
        ),
        required_authority_scope="approve:external_service",
        created_at=NOW,
        expires_at=plan.expires_at,
        source_phase_id="synthesis",
        source_attempt_id="attempt-1",
        request_idempotency_key=approval_request_idempotency_key(
            run_id="run-1",
            phase_id="synthesis",
            stable_proposal_id="proposal-1",
            action_category="external_service",
            action_digest_sha256=action_digest_sha256(plan),
        ),
    )
    ledger, _, _ = add_approval_request_v2(
        empty_approval_ledger_v2("run-1"),
        request,
    )
    return request, read_approval_state_v2(*encode_approval_state_v2(ledger, (), ()))


def _local_actor() -> ApprovalActor:
    return ApprovalActor(
        actor_id="local-user",
        actor_type="human",
        authority_source="local_cli",
        authority_scopes=("reject:any",),
        verification_status="unverified",
        session_reference_sha256=HASH_A,
        revocation_status="unknown",
    )


def _batch(
    request: ApprovalRequestV2,
    items: tuple[ApprovalDecisionItemV2, ...],
    *,
    actor: ApprovalActor | None = None,
) -> ApprovalDecisionBatch:
    return ApprovalDecisionBatch(
        run_id="run-1",
        expected_run_revision=2,
        expected_ledger_revision=1,
        actor=actor or _local_actor(),
        decision_id="decision-1",
        idempotency_key="decision-key-1",
        items=items,
        reason="Decide safely.",
        decision_at=NOW + timedelta(minutes=1),
    )


def _item(
    request: ApprovalRequestV2,
    decision: str = "rejected",
) -> ApprovalDecisionItemV2:
    return ApprovalDecisionItemV2(
        request_id=request.request_id,
        expected_request_revision=request.request_revision,
        action_digest_sha256=request.action_digest_sha256,
        decision=decision,
    )


@pytest.mark.parametrize(
    ("data", "decision_log", "audit_log"),
    [
        (b'{"schema_version":"2.0.0","schema_version":"2.0.0"}', b"", b""),
        (b'{"schema_version":"9.0.0"}', b"", b""),
        (b"\xef\xbb\xbf{}", b"", b""),
    ],
)
def test_strict_state_reader_rejects_duplicate_unknown_and_bom_before_lookup(
    data: bytes,
    decision_log: bytes,
    audit_log: bytes,
) -> None:
    with pytest.raises(ApprovalLedgerCorruptError):
        read_approval_state_v2(data, decision_log, audit_log)


def test_strict_reader_rejects_head_hash_tamper() -> None:
    _, state = _state()
    tampered = ApprovalLedgerV2(
        **(
            state.ledger.model_dump()
            | {
                "decision_count": 1,
                "decision_log_head_sha256": HASH_B,
            }
        )
    )
    with pytest.raises(ApprovalLedgerCorruptError):
        read_approval_state_v2(canonical_json_bytes(tampered), b"", b"")


def test_spoofed_claimed_actor_fails_before_scope_inference() -> None:
    request, state = _state()
    claimed = ApprovalActor(
        actor_id="claimed-user",
        actor_type="human",
        authority_source="test_provider",
        authority_scopes=("approve:external_service",),
        verification_status="verified",
        verification_reference_sha256=HASH_A,
        verified_at=NOW,
        authority_expires_at=NOW + timedelta(hours=1),
        revocation_status="not_revoked",
    )
    batch = _batch(request, (_item(request, "approved"),), actor=claimed)
    provider = LocalCliAuthorityProvider(
        "local-user",
        session_reference_sha256=HASH_A,
    )

    with pytest.raises(ApprovalDecisionValidationError) as captured:
        validate_approval_decision(
            state,
            batch,
            provider,
            observed_run_revision=2,
            evaluated_at=batch.decision_at,
        )
    assert captured.value.failure.code is ApprovalFailureCode.ACTOR_SPOOF


def test_approve_reject_conflict_precedes_duplicate_classification() -> None:
    request, state = _state()
    items = (_item(request, "approved"), _item(request, "rejected"))
    batch = _batch(request, items)

    with pytest.raises(ApprovalDecisionValidationError) as captured:
        validate_approval_decision(
            state,
            batch,
            LocalCliAuthorityProvider(
                "local-user",
                session_reference_sha256=HASH_A,
            ),
            observed_run_revision=2,
            evaluated_at=batch.decision_at,
        )
    assert captured.value.failure.code is ApprovalFailureCode.APPROVE_REJECT_CONFLICT


def test_expiry_and_action_swap_fail_without_mutating_state() -> None:
    request, state = _state()
    swapped = _item(request).model_copy(update={"action_digest_sha256": HASH_B})
    batch = _batch(request, (swapped,))
    provider = LocalCliAuthorityProvider(
        "local-user",
        session_reference_sha256=HASH_A,
    )

    with pytest.raises(ApprovalDecisionValidationError) as mismatch:
        validate_approval_decision(
            state,
            batch,
            provider,
            observed_run_revision=2,
            evaluated_at=batch.decision_at,
        )
    assert mismatch.value.failure.code is ApprovalFailureCode.ACTION_DIGEST_MISMATCH

    expired_batch = _batch(request, (_item(request),))
    with pytest.raises(ApprovalDecisionValidationError) as expired:
        validate_approval_decision(
            state,
            expired_batch,
            provider,
            observed_run_revision=2,
            evaluated_at=request.expires_at,
        )
    assert expired.value.failure.code is ApprovalFailureCode.APPROVAL_EXPIRED
    assert state.ledger.requests[0].state == "pending"


def _audit_orchestrator(tmp_path: Path) -> Orchestrator:
    orchestrator = Orchestrator(
        AppConfig(
            runs_dir=tmp_path / "legacy",
            revision_runs_dir=tmp_path / "product",
            durable_budget_runs_dir=tmp_path / "budget",
        ),
        terminal_clock=lambda: NOW,
    )
    orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="audit fixture",
            analysis_scope="retrospective",
        ),
        run_id="run-approval-audit-limits",
    )
    return orchestrator


def _audit_request(
    sequence: int,
    *,
    occurred_at: datetime | None = None,
) -> ApprovalFailureAuditRequest:
    return ApprovalFailureAuditRequest(
        run_id="run-approval-audit-limits",
        actor_sha256=HASH_A,
        decision_id_sha256=f"{sequence:064x}",
        idempotency_key_sha256=f"{sequence + 100:064x}",
        batch_sha256=HASH_B,
        failure_code=ApprovalFailureCode.STALE_DECISION,
        observed_run_revision=1,
        observed_ledger_revision=0,
        occurred_at=occurred_at or NOW + timedelta(seconds=sequence),
    )


def test_security_audit_records_one_rate_marker_then_fails_write_zero(
    tmp_path: Path,
) -> None:
    orchestrator = _audit_orchestrator(tmp_path)
    for sequence in range(32):
        orchestrator.product_store.append_approval_failure_audit(_audit_request(sequence))

    with pytest.raises(ApprovalFailureAuditError) as marker:
        orchestrator.product_store.append_approval_failure_audit(_audit_request(32))
    pointer, events = orchestrator.product_store.read_approval_failure_audit(
        "run-approval-audit-limits"
    )
    with pytest.raises(ApprovalFailureAuditError) as limited:
        orchestrator.product_store.append_approval_failure_audit(_audit_request(33))
    unchanged, unchanged_events = orchestrator.product_store.read_approval_failure_audit(
        "run-approval-audit-limits"
    )

    assert marker.value.failure.code is ApprovalFailureCode.AUDIT_RATE_LIMITED
    assert marker.value.failure.audit_confirmed is True
    assert limited.value.failure.code is ApprovalFailureCode.AUDIT_RATE_LIMITED
    assert pointer.audit_sequence == 33
    assert events[-1].event_kind == "rate_limit"
    assert unchanged == pointer
    assert unchanged_events == events


def test_security_audit_uses_a_rolling_not_tumbling_sixty_second_window(
    tmp_path: Path,
) -> None:
    orchestrator = _audit_orchestrator(tmp_path)
    for sequence in range(32):
        orchestrator.product_store.append_approval_failure_audit(_audit_request(sequence))
    with pytest.raises(ApprovalFailureAuditError):
        orchestrator.product_store.append_approval_failure_audit(_audit_request(32))

    boundary_time = NOW + timedelta(seconds=60)
    admitted = orchestrator.product_store.append_approval_failure_audit(
        _audit_request(1000, occurred_at=boundary_time)
    )
    assert admitted.event.event_kind == "failure"
    assert admitted.pointer.rate_states[0].failed_event_count == 32
    assert admitted.pointer.rate_states[0].window_started_at == NOW + timedelta(seconds=1)
    assert admitted.pointer.rate_states[0].rate_limit_marker_recorded is False

    with pytest.raises(ApprovalFailureAuditError) as marker:
        orchestrator.product_store.append_approval_failure_audit(
            _audit_request(1001, occurred_at=boundary_time)
        )
    assert marker.value.failure.code is ApprovalFailureCode.AUDIT_RATE_LIMITED
    pointer, events = orchestrator.product_store.read_approval_failure_audit(
        "run-approval-audit-limits"
    )
    assert pointer.rate_states[0].rate_limit_marker_recorded is True
    assert events[-1].event_kind == "rate_limit"


def test_security_audit_capacity_exhaustion_never_overwrites(
    tmp_path: Path,
) -> None:
    orchestrator = _audit_orchestrator(tmp_path)
    first = orchestrator.product_store.append_approval_failure_audit(
        _audit_request(0),
        max_events=1,
    )

    with pytest.raises(ApprovalFailureAuditError) as captured:
        orchestrator.product_store.append_approval_failure_audit(
            _audit_request(1),
            max_events=1,
        )
    pointer, events = orchestrator.product_store.read_approval_failure_audit(
        "run-approval-audit-limits"
    )

    assert captured.value.failure.code is ApprovalFailureCode.AUDIT_CAPACITY_EXCEEDED
    assert pointer == first.pointer
    assert events == (first.event,)
