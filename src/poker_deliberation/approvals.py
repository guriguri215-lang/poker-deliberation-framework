"""Application-owned V1 compatibility and strict P2-013A approval authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, NoReturn, Protocol, cast

from pydantic import ValidationError

from poker_deliberation.approval_canonical import (
    CanonicalApprovalError,
    action_digest_sha256,
    approval_actor_sha256,
    approval_decision_batch_sha256,
    approval_decision_outcome_sha256,
    approval_decision_record_sha256,
    approval_domain_audit_event_sha256,
    approval_ledger_sha256,
    approval_request_idempotency_key,
    canonical_json_bytes,
    domain_sha256,
    parse_canonical_jsonl,
    parse_canonical_model,
)
from poker_deliberation.approval_models import (
    ApprovalActor,
    ApprovalAuthoritySnapshotV2,
    ApprovalDecisionBatch,
    ApprovalDecisionFailureV2,
    ApprovalDecisionOutcome,
    ApprovalDecisionRecordV2,
    ApprovalDecisionResultV2,
    ApprovalDisplayV2,
    ApprovalDomainAuditEventV2,
    ApprovalFailureCode,
    ApprovalLedgerV2,
    ApprovalRequestV2,
    AuthorityScope,
    CanonicalActionPlanV2,
    ExternalExecutionBindingV2,
    ReportRunStatus,
)
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

_IDEMPOTENCY_REFERENCE_DOMAIN = "poker-approval-idempotency-reference-v2"
_DECISION_REFERENCE_DOMAIN = "poker-approval-decision-reference-v2"
_TRANSACTION_ID_DOMAIN = "poker-approval-decision-transaction-v2"


def requires_human_approval(action_category: str) -> bool:
    return action_category in SENSITIVE_ACTIONS


class ApprovalLedger:
    """Mutable V1 compatibility ledger.

    Authoritative V2 state uses :class:`ApprovalLedgerV2` and the pure
    validation functions below. Existing callers keep their V1 behavior.
    """

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


class DecisionAuthorityProvider(Protocol):
    """Injected source of authority; claimed batch fields are never trusted."""

    def resolve_actor(
        self,
        actor_id: str,
        *,
        decision_at: datetime,
    ) -> ApprovalAuthoritySnapshotV2:
        """Return the independently verified canonical actor snapshot."""


class ExternalExecutionBindingProvider(Protocol):
    """Future executor seam; P2-013A only binds an unavailable result."""

    def bind_unavailable(
        self,
        request: ApprovalRequestV2,
        outcome: ApprovalDecisionOutcome,
        authority_snapshot: ApprovalAuthoritySnapshotV2,
    ) -> ExternalExecutionBindingV2:
        """Bind exact approved authority without launching an effect."""


class UnavailableExternalExecutionBindingProvider:
    def bind_unavailable(
        self,
        request: ApprovalRequestV2,
        outcome: ApprovalDecisionOutcome,
        authority_snapshot: ApprovalAuthoritySnapshotV2,
    ) -> ExternalExecutionBindingV2:
        result = next(
            (item for item in outcome.request_results if item.request_id == request.request_id),
            None,
        )
        if (
            outcome.outcome_kind != "committed"
            or result is None
            or result.decision != "approved"
            or result.request_revision != request.request_revision
            or result.action_digest_sha256 != request.action_digest_sha256
            or outcome.actor_sha256 != authority_snapshot.actor_sha256
        ):
            raise ValueError("external binding requires the exact approved request")
        from poker_deliberation.approval_canonical import (
            approval_authority_snapshot_sha256,
            approval_decision_outcome_sha256,
        )

        return ExternalExecutionBindingV2(
            run_id=outcome.run_id,
            request_id=request.request_id,
            request_revision=request.request_revision,
            action_digest_sha256=request.action_digest_sha256,
            execution_id=request.action_plan.execution_id,
            decision_id=outcome.decision_id,
            outcome_sha256=approval_decision_outcome_sha256(outcome),
            actor_sha256=outcome.actor_sha256,
            authority_snapshot_sha256=approval_authority_snapshot_sha256(
                authority_snapshot
            ),
        )


class LocalCliAuthorityProvider:
    """Default local authority: unverified and safe-side rejection only."""

    def __init__(
        self,
        actor_id: str,
        *,
        session_reference_sha256: str | None = None,
        credential_reference_sha256: str | None = None,
        provider_version: str = "1.0.0",
    ) -> None:
        self._actor_id = actor_id
        self._session_reference_sha256 = session_reference_sha256
        self._credential_reference_sha256 = credential_reference_sha256
        self._provider_version = provider_version

    def actor(self) -> ApprovalActor:
        return ApprovalActor(
            actor_id=self._actor_id,
            actor_type="human",
            authority_source="local_cli",
            authority_scopes=("reject:any",),
            verification_status="unverified",
            session_reference_sha256=self._session_reference_sha256,
            credential_reference_sha256=self._credential_reference_sha256,
            revocation_status="unknown",
        )

    def resolve_actor(
        self,
        actor_id: str,
        *,
        decision_at: datetime,
    ) -> ApprovalAuthoritySnapshotV2:
        del actor_id
        actor = self.actor()
        return ApprovalAuthoritySnapshotV2(
            provider_id="local-cli-authority",
            provider_version=self._provider_version,
            resolved_at=decision_at,
            actor=actor,
            actor_sha256=approval_actor_sha256(actor),
        )


class ApprovalLedgerCorruptError(ValueError):
    """Raised before any lookup when authoritative V2 artifacts are invalid."""


class ApprovalDecisionValidationError(ValueError):
    """A redacted structured, mutation-zero decision rejection."""

    def __init__(self, failure: ApprovalDecisionFailureV2) -> None:
        super().__init__(failure.code.value)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class VerifiedApprovalStateV2:
    ledger: ApprovalLedgerV2
    decision_records: tuple[ApprovalDecisionRecordV2, ...]
    domain_audit_events: tuple[ApprovalDomainAuditEventV2, ...]
    ledger_sha256: str


@dataclass(frozen=True, slots=True)
class ApprovalDecisionAdmissionV2:
    kind: Literal["new", "replay"]
    state: VerifiedApprovalStateV2
    batch: ApprovalDecisionBatch
    actor_snapshot: ApprovalAuthoritySnapshotV2 | None
    actor_sha256: str
    batch_sha256: str
    requests: tuple[ApprovalRequestV2, ...]
    replay_outcome: ApprovalDecisionOutcome | None


@dataclass(frozen=True, slots=True)
class ApprovalDecisionUpdateV2:
    ledger: ApprovalLedgerV2
    decision_records: tuple[ApprovalDecisionRecordV2, ...]
    domain_audit_events: tuple[ApprovalDomainAuditEventV2, ...]
    outcome: ApprovalDecisionOutcome
    decision_record: ApprovalDecisionRecordV2
    domain_audit_event: ApprovalDomainAuditEventV2


def empty_approval_ledger_v2(run_id: str) -> ApprovalLedgerV2:
    return ApprovalLedgerV2(
        run_id=run_id,
        ledger_revision=0,
        requests=(),
        decision_count=0,
        domain_audit_count=0,
    )


def build_approval_request_v2(
    *,
    run_id: str,
    created_run_revision: int,
    ledger_revision: int,
    stable_proposal_id: str,
    action_plan: CanonicalActionPlanV2,
    display: ApprovalDisplayV2,
    source_phase_id: str,
    source_attempt_id: str,
    created_at: datetime,
) -> ApprovalRequestV2:
    """Bind an untrusted proposal to application-owned V2 request identity."""

    action_sha256 = action_digest_sha256(action_plan)
    idempotency_key = approval_request_idempotency_key(
        run_id=run_id,
        phase_id=source_phase_id,
        stable_proposal_id=stable_proposal_id,
        action_category=action_plan.action_category,
        action_digest_sha256=action_sha256,
    )
    return ApprovalRequestV2(
        request_id=f"request-{idempotency_key[:32]}",
        request_revision=1,
        ledger_revision=ledger_revision,
        created_run_revision=created_run_revision,
        stable_proposal_id=stable_proposal_id,
        action_plan=action_plan,
        action_digest_sha256=action_sha256,
        display=display,
        required_authority_scope=cast(
            AuthorityScope,
            f"approve:{action_plan.action_category}",
        ),
        created_at=created_at,
        expires_at=action_plan.expires_at,
        source_phase_id=source_phase_id,
        source_attempt_id=source_attempt_id,
        request_idempotency_key=idempotency_key,
    )


def add_approval_request_v2(
    ledger: ApprovalLedgerV2,
    request: ApprovalRequestV2,
) -> tuple[ApprovalLedgerV2, ApprovalRequestV2, bool]:
    """Append one request or return the exact request-idempotent prior value."""

    for existing in ledger.requests:
        if existing.request_idempotency_key == request.request_idempotency_key:
            if canonical_json_bytes(existing) == canonical_json_bytes(request):
                return ledger, existing, False
            raise ApprovalDecisionValidationError(
                _failure(
                    ApprovalFailureCode.IDEMPOTENCY_CONFLICT,
                    "Request idempotency key is bound to different canonical data.",
                    run_id=ledger.run_id,
                    request_id=request.request_id,
                )
            )
        if existing.request_id == request.request_id:
            raise ApprovalDecisionValidationError(
                _failure(
                    ApprovalFailureCode.APPROVAL_DUPLICATE,
                    "Approval request ID is already present.",
                    run_id=ledger.run_id,
                    request_id=request.request_id,
                )
            )
    if request.ledger_revision != ledger.ledger_revision + 1:
        raise ApprovalDecisionValidationError(
            _failure(
                ApprovalFailureCode.STALE_DECISION,
                "Approval request ledger revision is stale.",
                run_id=ledger.run_id,
                request_id=request.request_id,
                observed_ledger_revision=ledger.ledger_revision,
            )
        )
    values = ledger.model_dump()
    values.update(
        ledger_revision=request.ledger_revision,
        requests=(*ledger.requests, request),
    )
    return ApprovalLedgerV2(**values), request, True


def read_approval_state_v2(
    ledger_bytes: bytes,
    decision_log_bytes: bytes,
    domain_audit_log_bytes: bytes,
) -> VerifiedApprovalStateV2:
    """Strictly validate complete ledger and chains before lookup dictionaries."""

    try:
        ledger = parse_canonical_model(ledger_bytes, ApprovalLedgerV2)
        decisions = parse_canonical_jsonl(
            decision_log_bytes,
            ApprovalDecisionRecordV2,
        )
        events = parse_canonical_jsonl(
            domain_audit_log_bytes,
            ApprovalDomainAuditEventV2,
        )
        _validate_decision_chain(ledger, decisions)
        _validate_domain_audit_chain(ledger, decisions, events)
        _validate_request_decision_projection(ledger, decisions)
    except (CanonicalApprovalError, ValidationError, ValueError) as exc:
        if isinstance(exc, ApprovalLedgerCorruptError):
            raise
        raise ApprovalLedgerCorruptError("authoritative approval ledger is corrupt") from exc
    return VerifiedApprovalStateV2(
        ledger=ledger,
        decision_records=decisions,
        domain_audit_events=events,
        ledger_sha256=approval_ledger_sha256(ledger),
    )


def _validate_decision_chain(
    ledger: ApprovalLedgerV2,
    records: tuple[ApprovalDecisionRecordV2, ...],
) -> None:
    if ledger.decision_count != len(records):
        raise ApprovalLedgerCorruptError("approval decision log is truncated")
    expected_previous: str | None = None
    decision_ids: set[str] = set()
    idempotency_keys: set[str] = set()
    prior_time: datetime | None = None
    prior_ledger_revision = -1
    for sequence, record in enumerate(records, start=1):
        if (
            record.sequence != sequence
            or record.previous_record_sha256 != expected_previous
            or record.run_id != ledger.run_id
            or record.outcome.outcome_kind != "committed"
        ):
            raise ApprovalLedgerCorruptError("approval decision chain mismatch")
        if record.decision_id in decision_ids or record.idempotency_key in idempotency_keys:
            raise ApprovalLedgerCorruptError("duplicate approval decision identity")
        if prior_time is not None and record.committed_at < prior_time:
            raise ApprovalLedgerCorruptError("approval decision clock rollback")
        if record.outcome.current_ledger_revision <= prior_ledger_revision:
            raise ApprovalLedgerCorruptError("approval decision ledger order mismatch")
        if record.outcome.current_ledger_revision > ledger.ledger_revision:
            raise ApprovalLedgerCorruptError("approval decision exceeds ledger revision")
        decision_ids.add(record.decision_id)
        idempotency_keys.add(record.idempotency_key)
        expected_previous = record.record_sha256
        prior_time = record.committed_at
        prior_ledger_revision = record.outcome.current_ledger_revision
    if ledger.decision_log_head_sha256 != expected_previous:
        raise ApprovalLedgerCorruptError("approval decision head mismatch")


def _validate_domain_audit_chain(
    ledger: ApprovalLedgerV2,
    records: tuple[ApprovalDecisionRecordV2, ...],
    events: tuple[ApprovalDomainAuditEventV2, ...],
) -> None:
    if ledger.domain_audit_count != len(events) or len(records) != len(events):
        raise ApprovalLedgerCorruptError("approval domain audit log is truncated")
    expected_previous: str | None = None
    for sequence, (record, event) in enumerate(zip(records, events, strict=True), start=1):
        if (
            event.sequence != sequence
            or event.previous_event_sha256 != expected_previous
            or event.run_id != ledger.run_id
            or event.decision_id != record.decision_id
            or event.actor_sha256 != record.actor_sha256
            or event.batch_sha256 != record.batch_sha256
            or event.decision_record_sha256 != record.record_sha256
            or event.outcome_sha256 != record.outcome_sha256
            or event.run_revision != record.outcome.current_run_revision
            or event.ledger_revision != record.outcome.current_ledger_revision
            or event.occurred_at != record.committed_at
        ):
            raise ApprovalLedgerCorruptError("approval domain audit chain mismatch")
        expected_previous = event.event_sha256
    if ledger.domain_audit_log_head_sha256 != expected_previous:
        raise ApprovalLedgerCorruptError("approval domain audit head mismatch")


def _validate_request_decision_projection(
    ledger: ApprovalLedgerV2,
    records: tuple[ApprovalDecisionRecordV2, ...],
) -> None:
    requests = {request.request_id: request for request in ledger.requests}
    decided: dict[str, ApprovalDecisionResultV2] = {}
    for record in records:
        for result in record.outcome.request_results:
            request = requests.get(result.request_id)
            if (
                request is None
                or result.request_id in decided
                or request.action_digest_sha256 != result.action_digest_sha256
                or request.request_revision != result.request_revision
                or request.state != result.decision
            ):
                raise ApprovalLedgerCorruptError("approval decision/request projection mismatch")
            decided[result.request_id] = result
    for request in ledger.requests:
        if request.state in {"approved", "rejected"} and request.request_id not in decided:
            raise ApprovalLedgerCorruptError("decided request lacks decision record")
        if request.state == "pending" and request.request_id in decided:
            raise ApprovalLedgerCorruptError("pending request has a decision record")
    maxima = [
        *(request.ledger_revision for request in ledger.requests),
        *(record.outcome.current_ledger_revision for record in records),
    ]
    if maxima and max(maxima) != ledger.ledger_revision:
        raise ApprovalLedgerCorruptError("approval ledger revision is not reachable")


def validate_approval_decision(
    state: VerifiedApprovalStateV2,
    batch: ApprovalDecisionBatch,
    authority_provider: DecisionAuthorityProvider,
    *,
    observed_run_revision: int,
    evaluated_at: datetime,
) -> ApprovalDecisionAdmissionV2:
    """Apply the approved fixed validation order without mutating state."""

    _require_utc(evaluated_at)
    actor_sha256 = approval_actor_sha256(batch.actor)
    batch_sha256 = approval_decision_batch_sha256(batch)

    replay = next(
        (
            record
            for record in state.decision_records
            if record.idempotency_key == batch.idempotency_key
        ),
        None,
    )
    if replay is not None:
        if replay.actor_sha256 != actor_sha256 or replay.batch_sha256 != batch_sha256:
            _reject(
                ApprovalFailureCode.IDEMPOTENCY_CONFLICT,
                "Decision idempotency key is bound to different canonical data.",
                batch,
                observed_run_revision,
                state.ledger.ledger_revision,
            )
        return ApprovalDecisionAdmissionV2(
            kind="replay",
            state=state,
            batch=batch,
            actor_snapshot=None,
            actor_sha256=actor_sha256,
            batch_sha256=batch_sha256,
            requests=(),
            replay_outcome=replay.outcome,
        )

    if batch.run_id != state.ledger.run_id:
        _reject(
            ApprovalFailureCode.RESUME_CONFLICT,
            "Decision run identity does not match the authoritative ledger.",
            batch,
            observed_run_revision,
            state.ledger.ledger_revision,
        )
    if batch.expected_run_revision != observed_run_revision:
        _reject(
            ApprovalFailureCode.STALE_DECISION,
            "Expected run revision is stale.",
            batch,
            observed_run_revision,
            state.ledger.ledger_revision,
        )
    if batch.expected_ledger_revision != state.ledger.ledger_revision:
        _reject(
            ApprovalFailureCode.STALE_DECISION,
            "Expected approval ledger revision is stale.",
            batch,
            observed_run_revision,
            state.ledger.ledger_revision,
        )
    if any(record.decision_id == batch.decision_id for record in state.decision_records):
        _reject(
            ApprovalFailureCode.IDEMPOTENCY_CONFLICT,
            "Decision ID is already bound to another decision.",
            batch,
            observed_run_revision,
            state.ledger.ledger_revision,
        )

    grouped: dict[str, set[str]] = {}
    counts: dict[str, int] = {}
    for item in batch.items:
        grouped.setdefault(item.request_id, set()).add(item.decision)
        counts[item.request_id] = counts.get(item.request_id, 0) + 1
    if any(len(decisions) > 1 for decisions in grouped.values()):
        _reject(
            ApprovalFailureCode.APPROVE_REJECT_CONFLICT,
            "Batch contains both approve and reject for one request.",
            batch,
            observed_run_revision,
            state.ledger.ledger_revision,
        )
    if any(count > 1 for count in counts.values()):
        _reject(
            ApprovalFailureCode.APPROVAL_DUPLICATE,
            "Batch contains a duplicate approval request.",
            batch,
            observed_run_revision,
            state.ledger.ledger_revision,
        )

    requests_by_id = {request.request_id: request for request in state.ledger.requests}
    selected: list[ApprovalRequestV2] = []
    for item in batch.items:
        request = requests_by_id.get(item.request_id)
        if request is None:
            _reject(
                ApprovalFailureCode.APPROVAL_UNKNOWN,
                "Approval request is unknown.",
                batch,
                observed_run_revision,
                state.ledger.ledger_revision,
                request_id=item.request_id,
            )
        if request.state != "pending":
            _reject(
                ApprovalFailureCode.APPROVAL_ALREADY_DECIDED,
                "Approval request has already been decided.",
                batch,
                observed_run_revision,
                state.ledger.ledger_revision,
                request_id=item.request_id,
            )
        if item.expected_request_revision != request.request_revision:
            _reject(
                ApprovalFailureCode.STALE_DECISION,
                "Expected approval request revision is stale.",
                batch,
                observed_run_revision,
                state.ledger.ledger_revision,
                request_id=item.request_id,
            )
        if item.action_digest_sha256 != request.action_digest_sha256:
            _reject(
                ApprovalFailureCode.ACTION_DIGEST_MISMATCH,
                "Approval action digest does not match.",
                batch,
                observed_run_revision,
                state.ledger.ledger_revision,
                request_id=item.request_id,
            )
        if evaluated_at >= request.expires_at:
            _reject(
                ApprovalFailureCode.APPROVAL_EXPIRED,
                "Approval request has expired.",
                batch,
                observed_run_revision,
                state.ledger.ledger_revision,
                request_id=item.request_id,
            )
        selected.append(request)

    try:
        snapshot = authority_provider.resolve_actor(
            batch.actor.actor_id,
            decision_at=evaluated_at,
        )
    except Exception:
        _reject(
            ApprovalFailureCode.UNAUTHORIZED_DECISION,
            "Authority provider could not verify the actor.",
            batch,
            observed_run_revision,
            state.ledger.ledger_revision,
        )
    if snapshot.actor != batch.actor:
        _reject(
            ApprovalFailureCode.ACTOR_SPOOF,
            "Claimed actor does not match the authority provider.",
            batch,
            observed_run_revision,
            state.ledger.ledger_revision,
        )

    for request, item in zip(selected, batch.items, strict=True):
        actor = snapshot.actor
        if actor.revocation_status == "revoked":
            _reject(
                ApprovalFailureCode.AUTHORITY_REVOKED,
                "Approval authority is revoked.",
                batch,
                observed_run_revision,
                state.ledger.ledger_revision,
                request_id=request.request_id,
            )
        if item.decision == "rejected":
            authorized = "reject:any" in actor.authority_scopes
        else:
            authorized = (
                actor.verification_status == "verified"
                and actor.revocation_status == "not_revoked"
                and actor.authority_expires_at is not None
                and evaluated_at < actor.authority_expires_at
                and request.required_authority_scope in actor.authority_scopes
            )
        if not authorized:
            _reject(
                ApprovalFailureCode.UNAUTHORIZED_DECISION,
                "Actor lacks the exact required authority.",
                batch,
                observed_run_revision,
                state.ledger.ledger_revision,
                request_id=request.request_id,
            )

    return ApprovalDecisionAdmissionV2(
        kind="new",
        state=state,
        batch=batch,
        actor_snapshot=snapshot,
        actor_sha256=actor_sha256,
        batch_sha256=batch_sha256,
        requests=tuple(selected),
        replay_outcome=None,
    )


def build_approval_decision_update(
    admission: ApprovalDecisionAdmissionV2,
) -> ApprovalDecisionUpdateV2:
    """Construct every successor approval payload in memory."""

    if admission.kind != "new" or admission.actor_snapshot is None:
        raise ValueError("a replay admission cannot build a new decision update")
    batch = admission.batch
    ledger = admission.state.ledger
    next_ledger_revision = ledger.ledger_revision + 1
    decisions = {item.request_id: item.decision for item in batch.items}
    updated_requests: list[ApprovalRequestV2] = []
    results: list[ApprovalDecisionResultV2] = []
    for request in ledger.requests:
        decision = decisions.get(request.request_id)
        if decision is None:
            updated_requests.append(request)
            continue
        updated = ApprovalRequestV2(
            **(
                request.model_dump()
                | {
                    "request_revision": request.request_revision + 1,
                    "ledger_revision": next_ledger_revision,
                    "state": decision,
                }
            )
        )
        updated_requests.append(updated)
        results.append(
            ApprovalDecisionResultV2(
                request_id=updated.request_id,
                request_revision=updated.request_revision,
                action_digest_sha256=updated.action_digest_sha256,
                decision=decision,
            )
        )
    results.sort(key=lambda item: item.request_id.encode("utf-8"))
    remaining_pending_count = sum(request.state == "pending" for request in updated_requests)
    has_approval = any(result.decision == "approved" for result in results)
    limitation = (
        ApprovalDecisionFailureV2(
            code=ApprovalFailureCode.EXTERNAL_EXECUTOR_UNAVAILABLE,
            message="Approved action was not executed because no executor is available.",
            run_id=batch.run_id,
            decision_id=batch.decision_id,
            idempotency_key_sha256=_idempotency_reference_sha256(batch.idempotency_key),
            observed_run_revision=batch.expected_run_revision,
            observed_ledger_revision=ledger.ledger_revision,
            audit_confirmed=True,
        )
        if has_approval
        else None
    )
    run_status: ReportRunStatus = (
        "failed_with_limitations"
        if has_approval
        else "approval_required"
        if remaining_pending_count
        else "completed"
    )
    outcome = ApprovalDecisionOutcome(
        outcome_kind="committed",
        run_id=batch.run_id,
        decision_id=batch.decision_id,
        idempotency_key=batch.idempotency_key,
        actor_sha256=admission.actor_sha256,
        batch_sha256=admission.batch_sha256,
        previous_run_revision=batch.expected_run_revision,
        current_run_revision=batch.expected_run_revision + 1,
        previous_ledger_revision=ledger.ledger_revision,
        current_ledger_revision=next_ledger_revision,
        request_results=tuple(results),
        remaining_pending_count=remaining_pending_count,
        run_status=run_status,
        limitation=limitation,
        committed_at=batch.decision_at,
    )
    outcome_sha256 = approval_decision_outcome_sha256(outcome)
    partial_record = ApprovalDecisionRecordV2.model_construct(
        sequence=len(admission.state.decision_records) + 1,
        previous_record_sha256=ledger.decision_log_head_sha256,
        run_id=batch.run_id,
        decision_id=batch.decision_id,
        idempotency_key=batch.idempotency_key,
        actor_sha256=admission.actor_sha256,
        batch=batch,
        batch_sha256=admission.batch_sha256,
        outcome=outcome,
        outcome_sha256=outcome_sha256,
        committed_at=batch.decision_at,
        record_sha256="0" * 64,
    )
    record = ApprovalDecisionRecordV2(
        **(
            partial_record.model_dump()
            | {"record_sha256": approval_decision_record_sha256(partial_record)}
        )
    )
    partial_event = ApprovalDomainAuditEventV2.model_construct(
        sequence=len(admission.state.domain_audit_events) + 1,
        previous_event_sha256=ledger.domain_audit_log_head_sha256,
        event_kind="decision_committed",
        run_id=batch.run_id,
        run_revision=outcome.current_run_revision,
        ledger_revision=next_ledger_revision,
        decision_id=batch.decision_id,
        actor_sha256=admission.actor_sha256,
        batch_sha256=admission.batch_sha256,
        decision_record_sha256=record.record_sha256,
        outcome_sha256=outcome_sha256,
        occurred_at=batch.decision_at,
        event_sha256="0" * 64,
    )
    event = ApprovalDomainAuditEventV2(
        **(
            partial_event.model_dump()
            | {"event_sha256": approval_domain_audit_event_sha256(partial_event)}
        )
    )
    updated_ledger = ApprovalLedgerV2(
        **(
            ledger.model_dump()
            | {
                "ledger_revision": next_ledger_revision,
                "requests": tuple(updated_requests),
                "decision_count": len(admission.state.decision_records) + 1,
                "decision_log_head_sha256": record.record_sha256,
                "domain_audit_count": len(admission.state.domain_audit_events) + 1,
                "domain_audit_log_head_sha256": event.event_sha256,
            }
        )
    )
    return ApprovalDecisionUpdateV2(
        ledger=updated_ledger,
        decision_records=(*admission.state.decision_records, record),
        domain_audit_events=(*admission.state.domain_audit_events, event),
        outcome=outcome,
        decision_record=record,
        domain_audit_event=event,
    )


def reverify_approval_authority(
    admission: ApprovalDecisionAdmissionV2,
    authority_provider: DecisionAuthorityProvider,
    *,
    evaluated_at: datetime,
) -> ApprovalAuthoritySnapshotV2:
    """Repeat actor and scope verification while RM-012 authority is held."""

    if admission.kind != "new" or admission.actor_snapshot is None:
        raise ValueError("only a new admission requires in-lock authority verification")
    _require_utc(evaluated_at)
    batch = admission.batch
    if evaluated_at < batch.decision_at:
        _reject(
            ApprovalFailureCode.RESUME_CONFLICT,
            "Authority verification clock moved backwards.",
            batch,
            batch.expected_run_revision,
            batch.expected_ledger_revision,
        )
    try:
        snapshot = authority_provider.resolve_actor(
            batch.actor.actor_id,
            decision_at=evaluated_at,
        )
    except Exception:
        _reject(
            ApprovalFailureCode.UNAUTHORIZED_DECISION,
            "Authority provider could not reverify the actor.",
            batch,
            batch.expected_run_revision,
            batch.expected_ledger_revision,
        )
    if snapshot.actor != admission.actor_snapshot.actor:
        code = (
            ApprovalFailureCode.AUTHORITY_REVOKED
            if snapshot.actor.revocation_status == "revoked"
            else ApprovalFailureCode.ACTOR_SPOOF
        )
        _reject(
            code,
            "Authority changed before decision publication.",
            batch,
            batch.expected_run_revision,
            batch.expected_ledger_revision,
        )
    for request, item in zip(admission.requests, batch.items, strict=True):
        actor = snapshot.actor
        if evaluated_at >= request.expires_at:
            _reject(
                ApprovalFailureCode.APPROVAL_EXPIRED,
                "Approval request expired before publication.",
                batch,
                batch.expected_run_revision,
                batch.expected_ledger_revision,
                request_id=request.request_id,
            )
        authorized = (
            "reject:any" in actor.authority_scopes
            if item.decision == "rejected"
            else actor.verification_status == "verified"
            and actor.revocation_status == "not_revoked"
            and actor.authority_expires_at is not None
            and evaluated_at < actor.authority_expires_at
            and request.required_authority_scope in actor.authority_scopes
        )
        if not authorized:
            _reject(
                (
                    ApprovalFailureCode.AUTHORITY_REVOKED
                    if actor.revocation_status == "revoked"
                    else ApprovalFailureCode.UNAUTHORIZED_DECISION
                ),
                "Actor no longer has the exact required authority.",
                batch,
                batch.expected_run_revision,
                batch.expected_ledger_revision,
                request_id=request.request_id,
            )
    return snapshot


def approval_transaction_id(
    run_id: str,
    decision_idempotency_key: str,
    batch_sha256: str,
) -> str:
    digest = domain_sha256(
        _TRANSACTION_ID_DOMAIN,
        canonical_json_bytes(
            {
                "run_id": run_id,
                "decision_idempotency_key": decision_idempotency_key,
                "batch_sha256": batch_sha256,
            }
        ),
    )
    return f"txn-{digest[:32]}"


def approval_reference_sha256(
    kind: Literal["decision_id", "idempotency_key"],
    value: str,
) -> str:
    domain = (
        _DECISION_REFERENCE_DOMAIN
        if kind == "decision_id"
        else _IDEMPOTENCY_REFERENCE_DOMAIN
    )
    return domain_sha256(domain, value.encode("utf-8"))


def approval_failure_v2(
    code: ApprovalFailureCode,
    message: str,
    *,
    run_id: str,
    decision_id: str,
    idempotency_key: str,
    observed_run_revision: int | None,
    observed_ledger_revision: int | None,
) -> ApprovalDecisionFailureV2:
    return _failure(
        code,
        message,
        run_id=run_id,
        decision_id=decision_id,
        idempotency_key=idempotency_key,
        observed_run_revision=observed_run_revision,
        observed_ledger_revision=observed_ledger_revision,
    )


def project_v1_approvals(
    state: VerifiedApprovalStateV2,
) -> list[ApprovalRequest]:
    """Derive the exact public V1 projection from authoritative V2 state."""

    decisions: dict[str, ApprovalDecisionRecordV2] = {}
    for record in state.decision_records:
        for result in record.outcome.request_results:
            decisions[result.request_id] = record
    projected: list[ApprovalRequest] = []
    for request in state.ledger.requests:
        if request.state == "superseded":
            raise ApprovalLedgerCorruptError("P2-013A cannot project a superseded approval request")
        decision_record = decisions.get(request.request_id)
        status = {
            "pending": ApprovalStatus.PENDING,
            "approved": ApprovalStatus.APPROVED,
            "rejected": ApprovalStatus.REJECTED,
        }[request.state]
        projected.append(
            ApprovalRequest(
                approval_id=request.request_id,
                action_category=request.action_plan.action_category,
                requested_action=request.display.requested_action,
                reason=request.display.reason,
                expected_benefit=request.display.expected_benefit,
                risks=list(request.display.risks),
                data_to_be_sent=list(request.display.data_to_be_sent),
                cost_or_resource_estimate=request.display.cost_or_resource_estimate,
                alternatives=list(request.display.alternatives),
                effect_of_declining=request.display.effect_of_declining,
                exact_command_or_tool_call=request.display.exact_command_or_tool_call,
                status=status,
                decision_reason=(
                    None if decision_record is None else decision_record.batch.reason
                ),
                created_at=request.created_at,
                decided_at=(
                    None if decision_record is None else decision_record.committed_at
                ),
            )
        )
    return projected


def encode_approval_state_v2(
    ledger: ApprovalLedgerV2,
    decisions: tuple[ApprovalDecisionRecordV2, ...],
    events: tuple[ApprovalDomainAuditEventV2, ...],
) -> tuple[bytes, bytes, bytes]:
    from poker_deliberation.approval_canonical import canonical_jsonl_bytes

    return (
        canonical_json_bytes(ledger),
        canonical_jsonl_bytes(decisions),
        canonical_jsonl_bytes(events),
    )


def _failure(
    code: ApprovalFailureCode,
    message: str,
    *,
    run_id: str | None = None,
    request_id: str | None = None,
    decision_id: str | None = None,
    idempotency_key: str | None = None,
    observed_run_revision: int | None = None,
    observed_ledger_revision: int | None = None,
) -> ApprovalDecisionFailureV2:
    return ApprovalDecisionFailureV2(
        code=code,
        message=message,
        retryable=code is ApprovalFailureCode.RUN_LOCKED,
        run_id=run_id,
        request_id=request_id,
        decision_id=decision_id,
        idempotency_key_sha256=(
            _idempotency_reference_sha256(idempotency_key) if idempotency_key is not None else None
        ),
        observed_run_revision=observed_run_revision,
        observed_ledger_revision=observed_ledger_revision,
        audit_confirmed=False,
        reconciliation_required=False,
    )


def _reject(
    code: ApprovalFailureCode,
    message: str,
    batch: ApprovalDecisionBatch,
    observed_run_revision: int,
    observed_ledger_revision: int,
    *,
    request_id: str | None = None,
) -> NoReturn:
    raise ApprovalDecisionValidationError(
        _failure(
            code,
            message,
            run_id=batch.run_id,
            request_id=request_id,
            decision_id=batch.decision_id,
            idempotency_key=batch.idempotency_key,
            observed_run_revision=observed_run_revision,
            observed_ledger_revision=observed_ledger_revision,
        )
    )


def _idempotency_reference_sha256(value: str) -> str:
    return domain_sha256(_IDEMPOTENCY_REFERENCE_DOMAIN, value.encode("utf-8"))


def _require_utc(value: datetime) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("approval evaluation time must be timezone-aware UTC")
