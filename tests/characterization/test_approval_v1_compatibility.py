from __future__ import annotations

from poker_deliberation.approvals import ApprovalLedger
from poker_deliberation.schemas import ApprovalRequest, ApprovalStatus

V1_FIELDS = {
    "approval_id",
    "action_category",
    "requested_action",
    "reason",
    "expected_benefit",
    "risks",
    "data_to_be_sent",
    "cost_or_resource_estimate",
    "alternatives",
    "effect_of_declining",
    "exact_command_or_tool_call",
    "status",
    "decision_reason",
    "created_at",
    "decided_at",
}


def _request(approval_id: str = "approval-v1") -> ApprovalRequest:
    return ApprovalRequest(
        approval_id=approval_id,
        action_category="external_service",
        requested_action="Use an external service.",
        reason="A human decision is required.",
        expected_benefit="Potentially obtain external evidence.",
        risks=["External disclosure."],
        data_to_be_sent=["summary"],
        cost_or_resource_estimate="Bounded.",
        alternatives=["Keep the run local."],
        effect_of_declining="No external action is taken.",
    )


def test_v1_public_projection_fields_remain_exact() -> None:
    request = _request()
    assert set(request.model_dump(mode="json")) == V1_FIELDS
    assert request.status is ApprovalStatus.PENDING
    assert request.decision_reason is None
    assert request.decided_at is None


def test_v1_reject_mutation_and_order_remain_source_compatible() -> None:
    first = _request("approval-1")
    second = _request("approval-2")
    ledger = ApprovalLedger([first, second])

    decided = ledger.decide("approval-1", False, "Rejected safely.")

    assert decided is first
    assert decided.status is ApprovalStatus.REJECTED
    assert decided.decision_reason == "Rejected safely."
    assert decided.decided_at is not None
    assert decided.decided_at.utcoffset() is not None
    assert ledger.all() == [first, second]
    assert ledger.pending() == [second]
