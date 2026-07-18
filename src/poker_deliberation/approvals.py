"""Application-owned human approval ledger."""

from __future__ import annotations

from datetime import UTC, datetime

from poker_deliberation.schemas import ApprovalRequest, ApprovalStatus

SENSITIVE_ACTIONS = {
    "external_code",
    "package_install",
    "external_service",
    "long_running_compute",
    "outside_workspace_write",
    "destructive_change",
    "secret_access",
    "paid_data",
    "objective_change",
}


def requires_human_approval(action_category: str) -> bool:
    return action_category in SENSITIVE_ACTIONS


class ApprovalLedger:
    def __init__(self, requests: list[ApprovalRequest] | None = None) -> None:
        self._requests = {request.approval_id: request for request in requests or []}

    def add(self, request: ApprovalRequest) -> ApprovalRequest:
        if request.approval_id in self._requests:
            raise ValueError(f"duplicate approval id: {request.approval_id}")
        self._requests[request.approval_id] = request
        return request

    def decide(self, approval_id: str, approved: bool, reason: str) -> ApprovalRequest:
        request = self._requests[approval_id]
        if request.status is not ApprovalStatus.PENDING:
            raise ValueError("approval request has already been decided")
        request.status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
        request.decision_reason = reason
        request.decided_at = datetime.now(UTC)
        return request

    def pending(self) -> list[ApprovalRequest]:
        return [r for r in self._requests.values() if r.status is ApprovalStatus.PENDING]

    def all(self) -> list[ApprovalRequest]:
        return list(self._requests.values())
