"""Canonical JSON, strict readers, and domain-separated P2-013A digests."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import datetime
from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from poker_deliberation.approval_models import (
    ApprovalActor,
    ApprovalDecisionBatch,
    ApprovalDecisionOutcome,
    ApprovalDecisionRecordV2,
    ApprovalDomainAuditEventV2,
    ApprovalLedgerV2,
    ApprovalRequestV2,
    CanonicalActionPlanV2,
    ExternalExecutionBindingV2,
    HistoricalApprovalV1Binding,
)

ACTION_PLAN_DOMAIN = "poker-approval-action-plan-v2"
REQUEST_DOMAIN = "poker-approval-request-v2"
REQUEST_IDEMPOTENCY_DOMAIN = "poker-approval-request-idempotency-v2"
ACTOR_DOMAIN = "poker-approval-actor-v2"
AUTHORITY_SNAPSHOT_DOMAIN = "poker-approval-authority-snapshot-v2"
DECISION_BATCH_DOMAIN = "poker-approval-decision-batch-v2"
DECISION_OUTCOME_DOMAIN = "poker-approval-decision-outcome-v2"
DECISION_RECORD_DOMAIN = "poker-approval-decision-record-v2"
DOMAIN_AUDIT_EVENT_DOMAIN = "poker-approval-domain-audit-event-v2"
LEDGER_DOMAIN = "poker-approval-ledger-v2"
EXTERNAL_BINDING_DOMAIN = "poker-approval-external-binding-v2"
HISTORICAL_V1_BINDING_DOMAIN = "poker-approval-historical-v1-binding-v2"

T = TypeVar("T", bound=BaseModel)


class CanonicalApprovalError(ValueError):
    """A stable, non-secret canonical approval validation failure."""


def _require_nfc(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise CanonicalApprovalError("canonical approval text must be NFC")
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="json"))
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalApprovalError("canonical approval keys must be strings")
            result[_require_nfc(key)] = _jsonable(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, str):
        return _require_nfc(value)
    if isinstance(value, float) and not (-float("inf") < value < float("inf")):
        raise CanonicalApprovalError("canonical approval numbers must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise CanonicalApprovalError("value is not canonical approval JSON")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CanonicalApprovalError):
            raise
        raise CanonicalApprovalError("value is not canonical approval JSON") from exc


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    normalized: set[str] = set()
    for key, value in pairs:
        normalized_key = _require_nfc(key)
        if normalized_key in normalized:
            raise CanonicalApprovalError("duplicate canonical approval JSON key")
        normalized.add(normalized_key)
        result[key] = value
    return result


def parse_canonical_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise CanonicalApprovalError("canonical approval JSON cannot contain a BOM")
    if data.endswith((b"\n", b"\r")):
        raise CanonicalApprovalError("canonical approval JSON cannot contain a trailing newline")
    try:
        text = data.decode("utf-8", errors="strict")
        _require_nfc(text)
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CanonicalApprovalError(f"non-finite canonical approval number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalApprovalError("invalid canonical approval JSON") from exc
    if canonical_json_bytes(value) != data:
        raise CanonicalApprovalError("approval JSON bytes are not canonical")
    return value


def parse_canonical_model(data: bytes, model: type[T]) -> T:
    parse_canonical_json(data)
    adapter = TypeAdapter(model)
    try:
        value = adapter.validate_json(data, strict=True)
    except ValidationError as exc:
        if not exc.errors() or any(error["type"] != "datetime_type" for error in exc.errors()):
            raise CanonicalApprovalError(
                "canonical approval JSON violates its strict schema"
            ) from exc
        try:
            value = adapter.validate_json(data, strict=False)
        except ValidationError as fallback_exc:
            raise CanonicalApprovalError(
                "canonical approval JSON violates its strict datetime schema"
            ) from fallback_exc
    if canonical_json_bytes(value) != data:
        raise CanonicalApprovalError("strict approval model bytes mismatch")
    return value


def parse_canonical_jsonl(data: bytes, model: type[T]) -> tuple[T, ...]:
    if data == b"":
        return ()
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data or not data.endswith(b"\n"):
        raise CanonicalApprovalError(
            "approval JSONL requires UTF-8, LF, and a terminated final record"
        )
    lines = data.split(b"\n")[:-1]
    if any(line == b"" for line in lines):
        raise CanonicalApprovalError("approval JSONL cannot contain blank records")
    return tuple(parse_canonical_model(line, model) for line in lines)


def canonical_jsonl_bytes(values: tuple[BaseModel, ...]) -> bytes:
    if not values:
        return b""
    return b"".join(canonical_json_bytes(value) + b"\n" for value in values)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def domain_sha256(domain: str, data: bytes) -> str:
    try:
        prefix = domain.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CanonicalApprovalError("approval hash domain must be ASCII") from exc
    return sha256_bytes(prefix + b"\0" + data)


def canonical_domain_sha256(domain: str, value: Any) -> str:
    return domain_sha256(domain, canonical_json_bytes(value))


def _without_derived_hash(value: BaseModel, field_name: str) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    payload.pop(field_name)
    return payload


def action_digest_sha256(plan: CanonicalActionPlanV2) -> str:
    return canonical_domain_sha256(ACTION_PLAN_DOMAIN, plan)


def approval_request_sha256(request: ApprovalRequestV2) -> str:
    return canonical_domain_sha256(REQUEST_DOMAIN, request)


def approval_request_idempotency_key(
    *,
    run_id: str,
    phase_id: str,
    stable_proposal_id: str,
    action_category: str,
    action_digest_sha256: str,
) -> str:
    return canonical_domain_sha256(
        REQUEST_IDEMPOTENCY_DOMAIN,
        {
            "run_id": run_id,
            "phase_id": phase_id,
            "stable_proposal_id": stable_proposal_id,
            "action_category": action_category,
            "action_digest_sha256": action_digest_sha256,
        },
    )


def approval_actor_sha256(actor: ApprovalActor) -> str:
    return canonical_domain_sha256(ACTOR_DOMAIN, actor)


def approval_authority_snapshot_sha256(snapshot: BaseModel) -> str:
    return canonical_domain_sha256(AUTHORITY_SNAPSHOT_DOMAIN, snapshot)


def approval_decision_batch_sha256(batch: ApprovalDecisionBatch) -> str:
    return canonical_domain_sha256(DECISION_BATCH_DOMAIN, batch)


def approval_decision_outcome_sha256(outcome: ApprovalDecisionOutcome) -> str:
    return canonical_domain_sha256(DECISION_OUTCOME_DOMAIN, outcome)


def approval_decision_record_sha256(record: ApprovalDecisionRecordV2) -> str:
    return canonical_domain_sha256(
        DECISION_RECORD_DOMAIN,
        _without_derived_hash(record, "record_sha256"),
    )


def approval_domain_audit_event_sha256(
    event: ApprovalDomainAuditEventV2,
) -> str:
    return canonical_domain_sha256(
        DOMAIN_AUDIT_EVENT_DOMAIN,
        _without_derived_hash(event, "event_sha256"),
    )


def approval_ledger_sha256(ledger: ApprovalLedgerV2) -> str:
    return canonical_domain_sha256(LEDGER_DOMAIN, ledger)


def external_execution_binding_sha256(
    binding: ExternalExecutionBindingV2,
) -> str:
    return canonical_domain_sha256(EXTERNAL_BINDING_DOMAIN, binding)


def historical_approval_v1_binding_sha256(
    binding: HistoricalApprovalV1Binding,
) -> str:
    return canonical_domain_sha256(HISTORICAL_V1_BINDING_DOMAIN, binding)
