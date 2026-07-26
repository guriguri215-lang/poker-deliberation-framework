from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from poker_deliberation.approval_canonical import (
    approval_actor_sha256,
    approval_execution_recheck_binding_sha256,
)
from poker_deliberation.approval_models import (
    ApprovalActor,
    ApprovalAuthoritySnapshotV2,
    ApprovalDecisionBatch,
    ApprovalDecisionItemV2,
    ApprovalDisplayV2,
    ApprovalExecutionFailureCode,
    ApprovalReissueBatchV2,
    ApprovalReissueItemV2,
    ApprovalReissueSuccessorV2,
    CanonicalActionPlanV2,
)
from poker_deliberation.approvals import (
    ApprovalDecisionValidationError,
    ApprovalExecutionValidationError,
    add_approval_request_v2,
    build_approval_decision_update,
    build_approval_reissue_update,
    build_approval_request_v2,
    empty_approval_ledger_v2,
    encode_approval_reissue_log_v2,
    encode_approval_state_v2,
    project_v1_approvals,
    read_approval_state_v2,
    recheck_approval_for_execution,
    validate_approval_decision,
    validate_approval_reissue,
)
from poker_deliberation.schemas import ApprovalRequest, ApprovalStatus

NOW = datetime(2026, 7, 26, 0, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _plan(*, expires_at: datetime, suffix: str) -> CanonicalActionPlanV2:
    return CanonicalActionPlanV2(
        operation=f"Submit redacted request {suffix}.",
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
        maximum_runtime_ms=1000,
        maximum_memory_bytes=1024,
        maximum_output_bytes=1024,
        maximum_processes=1,
        working_directory=None,
        environment_name_allowlist=(),
        expected_result_type="none",
        execution_id=f"execution-{suffix}",
        remote_idempotency_key=f"remote-{suffix}",
        expires_at=expires_at,
    )


def _display(suffix: str) -> ApprovalDisplayV2:
    return ApprovalDisplayV2(
        requested_action=f"Submit redacted request {suffix}.",
        reason="A human decision is required.",
        expected_benefit="Record one exact decision.",
        risks=("External disclosure.",),
        data_to_be_sent=(),
        cost_or_resource_estimate="No charge.",
        alternatives=("Reject the action.",),
        effect_of_declining="No external action is performed.",
        exact_command_or_tool_call=None,
    )


def _state_with_request(*, expires_at: datetime):
    request = build_approval_request_v2(
        run_id="run-lifecycle",
        created_run_revision=1,
        ledger_revision=1,
        stable_proposal_id="proposal-source",
        action_plan=_plan(expires_at=expires_at, suffix="source"),
        display=_display("source"),
        source_phase_id="intake",
        source_attempt_id="attempt-source",
        created_at=NOW - timedelta(hours=2),
    )
    ledger, request, _ = add_approval_request_v2(
        empty_approval_ledger_v2("run-lifecycle"),
        request,
    )
    state = read_approval_state_v2(*encode_approval_state_v2(ledger, (), ()))
    return state, request


def _reissue_batch(request, *, reissued_at: datetime = NOW) -> ApprovalReissueBatchV2:
    return ApprovalReissueBatchV2(
        run_id="run-lifecycle",
        expected_run_revision=1,
        expected_ledger_revision=1,
        reissue_id="reissue-source",
        idempotency_key="reissue-key-source",
        items=(
            ApprovalReissueItemV2(
                source_kind="approval_v2",
                source_request_id=request.request_id,
                expected_source_request_revision=request.request_revision,
                source_action_digest_sha256=request.action_digest_sha256,
                successor=ApprovalReissueSuccessorV2(
                    stable_proposal_id="proposal-successor",
                    action_plan=_plan(
                        expires_at=reissued_at + timedelta(hours=2),
                        suffix="successor",
                    ),
                    display=_display("successor"),
                    source_phase_id="resume",
                    source_attempt_id="attempt-reissue",
                ),
            ),
        ),
        reason="Replace the exact expired request.",
        reissued_at=reissued_at,
    )


def test_expired_v2_reissue_supersedes_exact_source_and_replays_write_zero() -> None:
    state, request = _state_with_request(expires_at=NOW - timedelta(minutes=1))
    batch = _reissue_batch(request)
    admission = validate_approval_reissue(
        state,
        (),
        batch,
        observed_run_revision=1,
        previous_manifest_sha256=HASH_A,
        previous_pointer_sha256=HASH_B,
    )

    update = build_approval_reissue_update(admission)
    next_state = read_approval_state_v2(
        *encode_approval_state_v2(
            update.ledger,
            update.decision_records,
            update.domain_audit_events,
        ),
        encode_approval_reissue_log_v2(update.reissue_records),
    )
    source = next(
        item for item in next_state.ledger.requests if item.request_id == request.request_id
    )
    successor = next(
        item for item in next_state.ledger.requests if item.request_id != request.request_id
    )
    replay = validate_approval_reissue(
        next_state,
        (),
        batch,
        observed_run_revision=2,
        previous_manifest_sha256="c" * 64,
        previous_pointer_sha256="d" * 64,
    )

    assert source.state == "superseded"
    assert source.supersession_reference == successor.request_id
    assert successor.state == "pending"
    assert successor.created_run_revision == 2
    assert update.outcome.current_run_revision == 2
    assert replay.kind == "replay"
    assert replay.replay_outcome == update.outcome
    projected = {item.approval_id: item.status for item in project_v1_approvals(next_state)}
    assert projected[source.request_id] is ApprovalStatus.REJECTED
    assert projected[successor.request_id] is ApprovalStatus.PENDING


def test_live_or_digest_mismatched_v2_request_cannot_be_reissued() -> None:
    state, request = _state_with_request(expires_at=NOW + timedelta(minutes=1))
    with pytest.raises(ApprovalDecisionValidationError) as live:
        validate_approval_reissue(
            state,
            (),
            _reissue_batch(request),
            observed_run_revision=1,
            previous_manifest_sha256=HASH_A,
            previous_pointer_sha256=HASH_B,
        )
    expired_state, expired_request = _state_with_request(expires_at=NOW - timedelta(minutes=1))
    mismatched = _reissue_batch(expired_request).model_copy(
        update={
            "items": (
                _reissue_batch(expired_request)
                .items[0]
                .model_copy(update={"source_action_digest_sha256": "f" * 64}),
            )
        }
    )
    with pytest.raises(ApprovalDecisionValidationError) as digest:
        validate_approval_reissue(
            expired_state,
            (),
            mismatched,
            observed_run_revision=1,
            previous_manifest_sha256=HASH_A,
            previous_pointer_sha256=HASH_B,
        )

    assert live.value.failure.code.value == "reissue_not_eligible"
    assert digest.value.failure.code.value == "reissue_not_eligible"


def test_historical_v1_reissue_requires_explicit_full_pending_projection() -> None:
    legacy = ApprovalRequest(
        approval_id="approval-legacy",
        action_category="external_service",
        requested_action="Historical request.",
        reason="Historical input.",
        expected_benefit="Historical benefit.",
        risks=["Historical risk."],
        cost_or_resource_estimate="Unknown.",
        alternatives=["Do nothing."],
        effect_of_declining="No action.",
        created_at=NOW - timedelta(days=1),
    )
    batch = ApprovalReissueBatchV2(
        run_id="run-lifecycle",
        expected_run_revision=1,
        expected_ledger_revision=0,
        reissue_id="reissue-legacy",
        idempotency_key="reissue-key-legacy",
        items=(
            ApprovalReissueItemV2(
                source_kind="historical_v1",
                source_request_id=legacy.approval_id,
                successor=ApprovalReissueSuccessorV2(
                    stable_proposal_id="proposal-legacy-successor",
                    action_plan=_plan(
                        expires_at=NOW + timedelta(hours=2),
                        suffix="legacy-successor",
                    ),
                    display=_display("legacy-successor"),
                    source_phase_id="resume",
                    source_attempt_id="attempt-legacy-reissue",
                ),
            ),
        ),
        reason="Explicitly replace the historical request.",
        reissued_at=NOW,
    )

    admission = validate_approval_reissue(
        None,
        (legacy,),
        batch,
        observed_run_revision=1,
        previous_manifest_sha256=HASH_A,
        previous_pointer_sha256=HASH_B,
    )
    update = build_approval_reissue_update(admission)
    state = read_approval_state_v2(
        *encode_approval_state_v2(
            update.ledger,
            update.decision_records,
            update.domain_audit_events,
        ),
        encode_approval_reissue_log_v2(update.reissue_records),
    )

    assert update.record.previous_ledger_sha256 is None
    assert update.record.legacy_projection[0].request == legacy
    assert [item.status for item in project_v1_approvals(state)] == [
        ApprovalStatus.REJECTED,
        ApprovalStatus.PENDING,
    ]


class _VerifiedProvider:
    def __init__(self, *, revoked: bool = False) -> None:
        self.actor = ApprovalActor(
            actor_id="reviewer",
            actor_type="human",
            authority_source="test-authority",
            authority_scopes=("approve:external_service", "reject:any"),
            verification_status="verified",
            verification_reference_sha256=HASH_A,
            session_reference_sha256=HASH_B,
            credential_reference_sha256=HASH_A,
            verified_at=NOW - timedelta(minutes=1),
            authority_expires_at=NOW + timedelta(hours=3),
            revocation_status=("revoked" if revoked else "not_revoked"),
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


def _approved_state():
    state, request = _state_with_request(expires_at=NOW + timedelta(hours=2))
    provider = _VerifiedProvider()
    batch = ApprovalDecisionBatch(
        run_id="run-lifecycle",
        expected_run_revision=1,
        expected_ledger_revision=1,
        actor=provider.actor,
        decision_id="decision-approve",
        idempotency_key="decision-key-approve",
        items=(
            ApprovalDecisionItemV2(
                request_id=request.request_id,
                expected_request_revision=request.request_revision,
                action_digest_sha256=request.action_digest_sha256,
                decision="approved",
            ),
        ),
        reason="Approve the exact request.",
        decision_at=NOW,
    )
    admission = validate_approval_decision(
        state,
        batch,
        provider,
        observed_run_revision=1,
        evaluated_at=NOW,
    )
    update = build_approval_decision_update(admission)
    approved = read_approval_state_v2(
        *encode_approval_state_v2(
            update.ledger,
            update.decision_records,
            update.domain_audit_events,
        )
    )
    return approved, update.ledger.requests[0], provider


def test_pre_execution_recheck_binds_exact_revision_and_live_authority() -> None:
    state, request, provider = _approved_state()
    binding = recheck_approval_for_execution(
        state,
        approval_run_id="run-lifecycle",
        approval_run_revision=2,
        approval_pointer_sha256=HASH_A,
        approval_manifest_sha256=HASH_B,
        request_id=request.request_id,
        expected_action_plan=request.action_plan,
        authority_provider=provider,
        evaluated_at=NOW + timedelta(minutes=1),
    )

    assert binding.request_revision == request.request_revision
    assert binding.action_digest_sha256 == request.action_digest_sha256
    assert binding.valid_until == request.expires_at
    assert binding.binding_sha256 == approval_execution_recheck_binding_sha256(binding)


def test_pre_execution_recheck_rejects_expiry_revocation_and_stale_run_revision() -> None:
    state, request, _provider = _approved_state()
    with pytest.raises(ApprovalExecutionValidationError) as expired:
        recheck_approval_for_execution(
            state,
            approval_run_id="run-lifecycle",
            approval_run_revision=2,
            approval_pointer_sha256=HASH_A,
            approval_manifest_sha256=HASH_B,
            request_id=request.request_id,
            expected_action_plan=request.action_plan,
            authority_provider=_VerifiedProvider(),
            evaluated_at=request.expires_at,
        )
    with pytest.raises(ApprovalExecutionValidationError) as revoked:
        recheck_approval_for_execution(
            state,
            approval_run_id="run-lifecycle",
            approval_run_revision=2,
            approval_pointer_sha256=HASH_A,
            approval_manifest_sha256=HASH_B,
            request_id=request.request_id,
            expected_action_plan=request.action_plan,
            authority_provider=_VerifiedProvider(revoked=True),
            evaluated_at=NOW + timedelta(minutes=1),
        )
    with pytest.raises(ApprovalExecutionValidationError) as stale:
        recheck_approval_for_execution(
            state,
            approval_run_id="run-lifecycle",
            approval_run_revision=3,
            approval_pointer_sha256=HASH_A,
            approval_manifest_sha256=HASH_B,
            request_id=request.request_id,
            expected_action_plan=request.action_plan,
            authority_provider=_VerifiedProvider(),
            evaluated_at=NOW + timedelta(minutes=1),
        )

    assert expired.value.failure.code is ApprovalExecutionFailureCode.APPROVAL_EXPIRED
    assert revoked.value.failure.code is ApprovalExecutionFailureCode.AUTHORITY_REVOKED
    assert stale.value.failure.code is ApprovalExecutionFailureCode.APPROVAL_MISMATCH


def test_pre_execution_recheck_does_not_trust_a_fabricated_verified_state() -> None:
    state, request, provider = _approved_state()
    fabricated = replace(state, ledger_sha256="f" * 64)

    with pytest.raises(ApprovalExecutionValidationError) as rejected:
        recheck_approval_for_execution(
            fabricated,
            approval_run_id="run-lifecycle",
            approval_run_revision=2,
            approval_pointer_sha256=HASH_A,
            approval_manifest_sha256=HASH_B,
            request_id=request.request_id,
            expected_action_plan=request.action_plan,
            authority_provider=provider,
            evaluated_at=NOW + timedelta(minutes=1),
        )

    assert rejected.value.failure.code is ApprovalExecutionFailureCode.APPROVAL_MISMATCH
