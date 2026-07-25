from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from poker_deliberation.approval_canonical import (
    action_digest_sha256,
    approval_actor_sha256,
    approval_decision_outcome_sha256,
    approval_decision_record_sha256,
    approval_domain_audit_event_sha256,
    approval_request_idempotency_key,
)
from poker_deliberation.approval_models import (
    ApprovalActor,
    ApprovalAuthoritySnapshotV2,
    ApprovalDecisionBatch,
    ApprovalDecisionFailureV2,
    ApprovalDecisionItemV2,
    ApprovalDecisionOutcome,
    ApprovalDecisionRecordV2,
    ApprovalDecisionResultV2,
    ApprovalDisplayV2,
    ApprovalDomainAuditEventV2,
    ApprovalFailureCode,
    ApprovalLedgerV2,
    ApprovalRequestV2,
    CanonicalActionPlanV2,
    OutboundFieldBindingV2,
)
from poker_deliberation.approvals import (
    ApprovalDecisionValidationError,
    LocalCliAuthorityProvider,
    add_approval_request_v2,
    build_approval_decision_update,
    empty_approval_ledger_v2,
    encode_approval_state_v2,
    read_approval_state_v2,
    validate_approval_decision,
)

NOW = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _plan(**changes: object) -> CanonicalActionPlanV2:
    values: dict[str, object] = {
        "operation": "Send the reviewed payload to the provider.",
        "action_category": "external_service",
        "executor_kind": "provider",
        "executor_identifier": "provider.example",
        "executor_version": "1.2.3",
        "executor_sha256": HASH_A,
        "executor_availability": "unavailable",
        "outbound_fields": (
            OutboundFieldBindingV2(
                field_name="case.summary",
                classification="internal",
                content_sha256=HASH_B,
            ),
        ),
        "destination_kind": "provider",
        "destination_identifier": "provider.example/review",
        "retention_policy_id": "retention-30d",
        "trace_policy_id": "trace-redacted-v1",
        "maximum_cost_microunits": 1_000_000,
        "maximum_runtime_ms": 60_000,
        "maximum_memory_bytes": 268_435_456,
        "maximum_output_bytes": 1_048_576,
        "maximum_processes": 1,
        "working_directory": "workspace",
        "environment_name_allowlist": ("LANG",),
        "expected_result_type": "review-result",
        "execution_id": "execution-1",
        "remote_idempotency_key": "remote-1",
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return CanonicalActionPlanV2(**values)


def _local_actor() -> ApprovalActor:
    return ApprovalActor(
        actor_id="local-user",
        actor_type="human",
        authority_source="local_cli",
        authority_scopes=("reject:any",),
        verification_status="unverified",
        revocation_status="unknown",
        session_reference_sha256=HASH_A,
    )


def _verified_actor() -> ApprovalActor:
    return ApprovalActor(
        actor_id="verified-user",
        actor_type="human",
        authority_source="test_provider",
        authority_scopes=("approve:external_service", "reject:any"),
        verification_status="verified",
        verification_reference_sha256=HASH_A,
        session_reference_sha256=HASH_B,
        credential_reference_sha256=HASH_C,
        verified_at=NOW,
        authority_expires_at=NOW + timedelta(hours=2),
        revocation_status="not_revoked",
    )


def _request(**changes: object) -> ApprovalRequestV2:
    plan = _plan()
    values: dict[str, object] = {
        "request_id": "request-1",
        "request_revision": 1,
        "ledger_revision": 1,
        "created_run_revision": 2,
        "stable_proposal_id": "proposal-1",
        "action_plan": plan,
        "action_digest_sha256": action_digest_sha256(plan),
        "display": ApprovalDisplayV2(
            requested_action="Send the reviewed payload.",
            reason="External review requires an explicit decision.",
            expected_benefit="Obtain a reviewed response.",
            risks=("External disclosure.",),
            data_to_be_sent=("case.summary",),
            cost_or_resource_estimate="At most one cost unit.",
            alternatives=("Keep the run local.",),
            effect_of_declining="The external review is not performed.",
        ),
        "required_authority_scope": "approve:external_service",
        "created_at": NOW,
        "expires_at": plan.expires_at,
        "source_phase_id": "synthesis",
        "source_attempt_id": "attempt-1",
        "request_idempotency_key": approval_request_idempotency_key(
            run_id="run-1",
            phase_id="synthesis",
            stable_proposal_id="proposal-1",
            action_category="external_service",
            action_digest_sha256=action_digest_sha256(plan),
        ),
    }
    values.update(changes)
    return ApprovalRequestV2(**values)


def _state() -> tuple[ApprovalRequestV2, object]:
    request = _request()
    ledger, _, created = add_approval_request_v2(
        empty_approval_ledger_v2("run-1"),
        request,
    )
    assert created
    encoded = encode_approval_state_v2(ledger, (), ())
    return request, read_approval_state_v2(*encoded)


def _batch(
    *,
    actor: ApprovalActor | None = None,
    decision: str = "rejected",
    items: tuple[ApprovalDecisionItemV2, ...] | None = None,
    **changes: object,
) -> ApprovalDecisionBatch:
    request = _request()
    values: dict[str, object] = {
        "run_id": "run-1",
        "expected_run_revision": 2,
        "expected_ledger_revision": 1,
        "actor": actor or _local_actor(),
        "decision_id": "decision-1",
        "idempotency_key": "decision-key-1",
        "items": items
        or (
            ApprovalDecisionItemV2(
                request_id=request.request_id,
                expected_request_revision=request.request_revision,
                action_digest_sha256=request.action_digest_sha256,
                decision=decision,
            ),
        ),
        "reason": "Decide the exact pending request.",
        "decision_at": NOW + timedelta(minutes=1),
    }
    values.update(changes)
    return ApprovalDecisionBatch(**values)


class _StaticAuthorityProvider:
    def __init__(self, actor: ApprovalActor) -> None:
        self.actor = actor

    def resolve_actor(
        self,
        actor_id: str,
        *,
        decision_at: datetime,
    ) -> ApprovalAuthoritySnapshotV2:
        del actor_id
        return ApprovalAuthoritySnapshotV2(
            provider_id="test-provider",
            provider_version="1.0.0",
            resolved_at=decision_at,
            actor=self.actor,
            actor_sha256=approval_actor_sha256(self.actor),
        )


def test_actor_trust_matrix_is_closed() -> None:
    assert _local_actor().authority_scopes == ("reject:any",)
    assert _verified_actor().revocation_status == "not_revoked"

    with pytest.raises(ValidationError, match="unverified local actor trust matrix"):
        ApprovalActor(
            actor_id="spoofed-user",
            actor_type="human",
            authority_source="local_cli",
            authority_scopes=("approve:external_service",),
            verification_status="unverified",
            revocation_status="unknown",
        )
    with pytest.raises(ValidationError, match="verified actor trust matrix"):
        ApprovalActor(
            actor_id="expired-shape",
            actor_type="human",
            authority_source="test_provider",
            authority_scopes=("approve:external_service",),
            verification_status="verified",
            verification_reference_sha256=HASH_A,
            verified_at=NOW,
            authority_expires_at=NOW,
            revocation_status="not_revoked",
        )


def test_actor_scopes_and_action_bindings_are_canonically_ordered() -> None:
    with pytest.raises(ValidationError, match="UTF-8 ordered"):
        ApprovalActor(
            actor_id="verified-user",
            actor_type="human",
            authority_source="test_provider",
            authority_scopes=("reject:any", "approve:external_service"),
            verification_status="verified",
            verification_reference_sha256=HASH_A,
            verified_at=NOW,
            authority_expires_at=NOW + timedelta(hours=1),
            revocation_status="not_revoked",
        )
    with pytest.raises(ValidationError, match="outbound fields"):
        _plan(
            outbound_fields=(
                OutboundFieldBindingV2(
                    field_name="z",
                    classification="internal",
                    content_sha256=HASH_A,
                ),
                OutboundFieldBindingV2(
                    field_name="a",
                    classification="internal",
                    content_sha256=HASH_B,
                ),
            )
        )
    with pytest.raises(ValidationError, match="environment names"):
        _plan(environment_name_allowlist=("Z_VAR", "A_VAR"))


def test_request_binds_action_scope_expiry_and_attempt_independent_key() -> None:
    request = _request()
    assert request.action_digest_sha256 == action_digest_sha256(request.action_plan)
    assert request.required_authority_scope == "approve:external_service"

    first = approval_request_idempotency_key(
        run_id="run-1",
        phase_id="synthesis",
        stable_proposal_id="proposal-1",
        action_category="external_service",
        action_digest_sha256=request.action_digest_sha256,
    )
    assert first == request.request_idempotency_key

    with pytest.raises(ValidationError, match="action digest mismatch"):
        _request(action_digest_sha256=HASH_C)
    with pytest.raises(ValidationError, match="authority scope mismatch"):
        _request(required_authority_scope="reject:any")
    with pytest.raises(ValidationError, match="expiry mismatch"):
        _request(expires_at=NOW + timedelta(minutes=1))


def test_ledger_rejects_duplicate_lookup_identities_before_dict_construction() -> None:
    request = _request()
    with pytest.raises(ValidationError, match="request IDs must be unique"):
        ApprovalLedgerV2(
            run_id="run-1",
            ledger_revision=1,
            requests=(request, request),
            decision_count=0,
            domain_audit_count=0,
        )


def test_batch_order_is_canonical_but_duplicate_identity_reaches_transaction_validation() -> None:
    item = ApprovalDecisionItemV2(
        request_id="request-1",
        expected_request_revision=1,
        action_digest_sha256=HASH_A,
        decision="rejected",
    )
    batch = ApprovalDecisionBatch(
        run_id="run-1",
        expected_run_revision=2,
        expected_ledger_revision=1,
        actor=_local_actor(),
        decision_id="decision-1",
        idempotency_key="decision-key-1",
        items=(item, item),
        reason="Reject safely.",
        decision_at=NOW,
    )
    assert len(batch.items) == 2

    with pytest.raises(ValidationError, match="canonically ordered"):
        ApprovalDecisionBatch(
            run_id="run-1",
            expected_run_revision=2,
            expected_ledger_revision=1,
            actor=_verified_actor(),
            decision_id="decision-1",
            idempotency_key="decision-key-1",
            items=(
                item,
                ApprovalDecisionItemV2(
                    request_id="request-0",
                    expected_request_revision=1,
                    action_digest_sha256=HASH_A,
                    decision="approved",
                ),
            ),
            reason="Decide.",
            decision_at=NOW,
        )


def test_outcome_matrix_distinguishes_safe_reject_and_unavailable_approval() -> None:
    rejected = ApprovalDecisionOutcome(
        outcome_kind="committed",
        run_id="run-1",
        decision_id="decision-1",
        idempotency_key="decision-key-1",
        actor_sha256=approval_actor_sha256(_local_actor()),
        batch_sha256=HASH_A,
        previous_run_revision=2,
        current_run_revision=3,
        previous_ledger_revision=1,
        current_ledger_revision=2,
        request_results=(
            ApprovalDecisionResultV2(
                request_id="request-1",
                request_revision=2,
                action_digest_sha256=HASH_B,
                decision="rejected",
            ),
        ),
        remaining_pending_count=0,
        run_status="completed",
        committed_at=NOW,
    )
    assert rejected.limitation is None

    limitation = ApprovalDecisionFailureV2(
        code=ApprovalFailureCode.EXTERNAL_EXECUTOR_UNAVAILABLE,
        message="The external executor is unavailable.",
        run_id="run-1",
        decision_id="decision-2",
        audit_confirmed=True,
    )
    approved = rejected.model_copy(
        update={
            "decision_id": "decision-2",
            "request_results": (
                rejected.request_results[0].model_copy(update={"decision": "approved"}),
            ),
            "run_status": "failed_with_limitations",
            "limitation": limitation,
        }
    )
    assert ApprovalDecisionOutcome.model_validate(approved).limitation == limitation

    with pytest.raises(ValidationError, match="unavailable limitation"):
        ApprovalDecisionOutcome.model_validate(approved.model_copy(update={"limitation": None}))


def test_decision_and_audit_chain_hashes_exclude_only_the_derived_hash() -> None:
    outcome = ApprovalDecisionOutcome(
        outcome_kind="committed",
        run_id="run-1",
        decision_id="decision-1",
        idempotency_key="decision-key-1",
        actor_sha256=HASH_A,
        batch_sha256=HASH_B,
        previous_run_revision=2,
        current_run_revision=3,
        previous_ledger_revision=1,
        current_ledger_revision=2,
        request_results=(
            ApprovalDecisionResultV2(
                request_id="request-1",
                request_revision=2,
                action_digest_sha256=HASH_C,
                decision="rejected",
            ),
        ),
        remaining_pending_count=0,
        run_status="completed",
        committed_at=NOW,
    )
    partial_record = ApprovalDecisionRecordV2.model_construct(
        sequence=1,
        previous_record_sha256=None,
        run_id="run-1",
        decision_id="decision-1",
        idempotency_key="decision-key-1",
        actor_sha256=HASH_A,
        batch_sha256=HASH_B,
        outcome=outcome,
        outcome_sha256=approval_decision_outcome_sha256(outcome),
        committed_at=NOW,
        record_sha256="0" * 64,
    )
    record = ApprovalDecisionRecordV2(
        **(
            partial_record.model_dump()
            | {"record_sha256": approval_decision_record_sha256(partial_record)}
        )
    )
    assert record.record_sha256 == approval_decision_record_sha256(record)

    partial_event = ApprovalDomainAuditEventV2.model_construct(
        sequence=1,
        previous_event_sha256=None,
        event_kind="decision_committed",
        run_id="run-1",
        run_revision=3,
        ledger_revision=2,
        decision_id="decision-1",
        actor_sha256=HASH_A,
        batch_sha256=HASH_B,
        decision_record_sha256=record.record_sha256,
        outcome_sha256=approval_decision_outcome_sha256(outcome),
        occurred_at=NOW,
        event_sha256="0" * 64,
    )
    event = ApprovalDomainAuditEventV2(
        **(
            partial_event.model_dump()
            | {"event_sha256": approval_domain_audit_event_sha256(partial_event)}
        )
    )
    assert event.event_sha256 == approval_domain_audit_event_sha256(event)


def test_only_run_locked_is_retryable() -> None:
    with pytest.raises(ValidationError, match="only run_locked"):
        ApprovalDecisionFailureV2(
            code=ApprovalFailureCode.APPROVAL_UNKNOWN,
            message="Unknown approval.",
            retryable=True,
            audit_confirmed=True,
        )


def test_pure_reject_update_round_trips_and_exact_replay_precedes_stale_revision() -> None:
    request, state = _state()
    batch = _batch()
    provider = LocalCliAuthorityProvider(
        "local-user",
        session_reference_sha256=HASH_A,
    )
    admission = validate_approval_decision(
        state,
        batch,
        provider,
        observed_run_revision=2,
        evaluated_at=batch.decision_at,
    )
    update = build_approval_decision_update(admission)

    assert update.outcome.run_status == "completed"
    assert update.ledger.requests[0].state == "rejected"
    assert update.ledger.requests[0].request_revision == request.request_revision + 1
    verified = read_approval_state_v2(
        *encode_approval_state_v2(
            update.ledger,
            update.decision_records,
            update.domain_audit_events,
        )
    )

    replay = validate_approval_decision(
        verified,
        batch,
        provider,
        observed_run_revision=3,
        evaluated_at=batch.decision_at,
    )
    assert replay.kind == "replay"
    assert replay.replay_outcome == update.outcome


def test_same_idempotency_key_with_different_payload_fails_before_stale_revision() -> None:
    _, state = _state()
    provider = LocalCliAuthorityProvider(
        "local-user",
        session_reference_sha256=HASH_A,
    )
    batch = _batch()
    update = build_approval_decision_update(
        validate_approval_decision(
            state,
            batch,
            provider,
            observed_run_revision=2,
            evaluated_at=batch.decision_at,
        )
    )
    verified = read_approval_state_v2(
        *encode_approval_state_v2(
            update.ledger,
            update.decision_records,
            update.domain_audit_events,
        )
    )
    changed = _batch(reason="Different canonical reason.")

    with pytest.raises(ApprovalDecisionValidationError) as captured:
        validate_approval_decision(
            verified,
            changed,
            provider,
            observed_run_revision=99,
            evaluated_at=changed.decision_at,
        )
    assert captured.value.failure.code is ApprovalFailureCode.IDEMPOTENCY_CONFLICT


def test_local_actor_can_reject_but_cannot_approve() -> None:
    _, state = _state()
    provider = LocalCliAuthorityProvider(
        "local-user",
        session_reference_sha256=HASH_A,
    )
    approved = _batch(decision="approved")

    with pytest.raises(ApprovalDecisionValidationError) as captured:
        validate_approval_decision(
            state,
            approved,
            provider,
            observed_run_revision=2,
            evaluated_at=approved.decision_at,
        )
    assert captured.value.failure.code is ApprovalFailureCode.UNAUTHORIZED_DECISION


def test_verified_exact_scope_approval_commits_unavailable_limitation() -> None:
    _, state = _state()
    actor = _verified_actor()
    batch = _batch(actor=actor, decision="approved")
    admission = validate_approval_decision(
        state,
        batch,
        _StaticAuthorityProvider(actor),
        observed_run_revision=2,
        evaluated_at=batch.decision_at,
    )
    update = build_approval_decision_update(admission)

    assert update.outcome.run_status == "failed_with_limitations"
    assert update.outcome.limitation is not None
    assert update.outcome.limitation.code is ApprovalFailureCode.EXTERNAL_EXECUTOR_UNAVAILABLE


def test_request_idempotency_is_exact_and_conflicting_payload_is_rejected() -> None:
    request = _request()
    ledger, prior, created = add_approval_request_v2(
        empty_approval_ledger_v2("run-1"),
        request,
    )
    assert created
    same_ledger, same_request, repeated = add_approval_request_v2(ledger, request)
    assert (same_ledger, same_request, repeated) == (ledger, prior, False)

    with pytest.raises(ApprovalDecisionValidationError) as captured:
        add_approval_request_v2(
            ledger,
            request.model_copy(update={"request_id": "request-changed"}),
        )
    assert captured.value.failure.code is ApprovalFailureCode.IDEMPOTENCY_CONFLICT
