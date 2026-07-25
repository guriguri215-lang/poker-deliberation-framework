from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from poker_deliberation.approval_canonical import (
    action_digest_sha256,
    approval_actor_sha256,
    approval_decision_record_sha256,
    approval_domain_audit_event_sha256,
    approval_request_idempotency_key,
)
from poker_deliberation.approval_models import (
    ApprovalActor,
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
        run_status="completed",
        decision_record_sha256=HASH_B,
        domain_audit_event_sha256=HASH_C,
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
    partial_record = ApprovalDecisionRecordV2.model_construct(
        sequence=1,
        previous_record_sha256=None,
        run_id="run-1",
        decision_id="decision-1",
        idempotency_key="decision-key-1",
        actor_sha256=HASH_A,
        batch_sha256=HASH_B,
        outcome_sha256=HASH_C,
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
        outcome_sha256=HASH_C,
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
