"""Attempt-scoped context policy, integrity, and provider handoff boundary."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from poker_deliberation.schemas import AgentAssignment, AgentContext

CONTEXT_SCHEMA_VERSION = "1.0.0"
CANONICALIZATION_VERSION = "poker-context-json-v1"
HASH_ALGORITHM = "sha256"
ATTEMPT_MEMORY_ONLY_RETENTION_POLICY = "attempt-memory-only-v1"

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|bearer|cookie|password|passwd|secret|"
    r"token|private[_-]?key|client[_-]?(?:secret|credential)|credential)",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|password|secret|token)\s*[:=]\s*[^\s,;]+",
        re.IGNORECASE,
    ),
)

Clock = Callable[[], datetime]


class ContextLifecycleError(ValueError):
    """A context failed a lifecycle contract check."""


class ContextHandoffRefused(ContextLifecycleError):
    """A valid context may not cross the provider boundary."""


class ContextClassification(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SENSITIVE = "sensitive"
    RESTRICTED = "restricted"


class RuntimeIdentity(StrEnum):
    PYTHON_LOCAL = "python-local"


class _LifecycleModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _require_utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


class ContextPolicy(_LifecycleModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    classification: ContextClassification = ContextClassification.INTERNAL
    retention_policy_id: Literal["attempt-memory-only-v1"] = "attempt-memory-only-v1"
    expires_at: datetime
    allowed_fields: tuple[str, ...]

    @field_validator("expires_at")
    @classmethod
    def validate_expiry_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "expires_at")

    @field_validator("allowed_fields")
    @classmethod
    def validate_allowed_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("allowed_fields must not be empty")
        if any(not _IDENTIFIER.fullmatch(name) for name in value):
            raise ValueError("allowed_fields must contain top-level identifiers only")
        if len(set(value)) != len(value):
            raise ValueError("allowed_fields must be unique")
        if value != tuple(sorted(value)):
            raise ValueError("allowed_fields must use canonical sorted order")
        return value


class ContextLineage(_LifecycleModel):
    context_id: str
    run_id: str
    assignment_id: str
    assignment_sha256: str
    attempt_id: str
    parent_context_id: str | None = None
    source_sha256: str
    producer_runtime: RuntimeIdentity = RuntimeIdentity.PYTHON_LOCAL
    consumer_runtime: RuntimeIdentity = RuntimeIdentity.PYTHON_LOCAL

    @field_validator(
        "context_id",
        "run_id",
        "assignment_id",
        "attempt_id",
        "parent_context_id",
    )
    @classmethod
    def validate_correlation_id(cls, value: str | None) -> str | None:
        if value is not None and not _CORRELATION_ID.fullmatch(value):
            raise ValueError("lineage correlation IDs must use the portable ID format")
        return value

    @field_validator("assignment_sha256", "source_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("lineage hashes must be lowercase SHA-256")
        return value


class ContextEnvelope(_LifecycleModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    canonicalization_version: Literal["poker-context-json-v1"] = "poker-context-json-v1"
    hash_algorithm: Literal["sha256"] = "sha256"
    created_at: datetime
    policy: ContextPolicy
    lineage: ContextLineage
    canonical_payload: str
    payload_sha256: str
    policy_sha256: str
    integrity_sha256: str

    @field_validator("created_at")
    @classmethod
    def validate_created_at_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, "created_at")

    @field_validator("payload_sha256", "policy_sha256", "integrity_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("envelope hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def expiry_follows_creation(self) -> ContextEnvelope:
        if self.policy.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        return self


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ContextLifecycleError("context value is not canonical JSON") from exc


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _model_payload(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


def context_payload(context: AgentContext) -> dict[str, Any]:
    """Return the meaningful top-level provider payload for exact allowlisting."""

    return context.model_dump(
        mode="json",
        exclude_none=True,
        exclude_defaults=True,
    )


def legacy_context_sha256(context: AgentContext) -> str:
    """Preserve the original full-AgentContext audit hash representation."""

    return _sha256_text(_canonical_json(context.model_dump(mode="json")))


def _assignment_payload(assignment: AgentAssignment) -> dict[str, Any]:
    return {
        "assignment_id": assignment.assignment_id,
        "agent_role": assignment.agent_role,
        "task": assignment.task,
        "context_keys": assignment.context_keys,
        "read_only": assignment.read_only,
    }


def assignment_sha256(assignment: AgentAssignment) -> str:
    return _sha256_text(_canonical_json(_assignment_payload(assignment)))


def _contains_restricted_secret(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _SENSITIVE_KEY.search(str(key)) or _contains_restricted_secret(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_restricted_secret(item) for item in value)
    if isinstance(value, str):
        return any(pattern.search(value) for pattern in _SECRET_PATTERNS)
    return False


def classify_context_payload(
    payload: Mapping[str, Any],
    requested: ContextClassification = ContextClassification.INTERNAL,
) -> ContextClassification:
    """Classify detected credentials as restricted regardless of caller intent."""

    if _contains_restricted_secret(payload):
        return ContextClassification.RESTRICTED
    return requested


def _policy_sha256(policy: ContextPolicy) -> str:
    return _sha256_text(_canonical_json(_model_payload(policy)))


def _integrity_payload(envelope: ContextEnvelope | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(envelope, ContextEnvelope):
        payload = envelope.model_dump(mode="json")
    else:
        payload = dict(envelope)
    payload.pop("integrity_sha256", None)
    return payload


def _integrity_sha256(envelope: ContextEnvelope | Mapping[str, Any]) -> str:
    return _sha256_text(_canonical_json(_integrity_payload(envelope)))


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def new_context_id() -> str:
    return _new_id("context")


def new_attempt_id() -> str:
    return _new_id("attempt")


def _build_context_envelope(
    context: AgentContext,
    assignment: AgentAssignment,
    *,
    run_id: str,
    expires_at: datetime,
    clock: Clock,
    classification: ContextClassification = ContextClassification.INTERNAL,
    context_id: str | None = None,
    attempt_id: str | None = None,
    parent_context_id: str | None = None,
    source_sha256: str | None = None,
) -> ContextEnvelope:
    """Build a fresh immutable envelope without persisting the context payload."""

    created_at = _require_utc(clock(), "clock result")
    expires_at = _require_utc(expires_at, "expires_at")
    payload = context_payload(context)
    allowed_fields = tuple(sorted(payload))
    if tuple(assignment.context_keys) != allowed_fields:
        raise ContextLifecycleError("assignment context_keys do not match the payload allowlist")
    canonical_payload = _canonical_json(payload)
    payload_sha256 = _sha256_text(canonical_payload)
    effective_classification = classify_context_payload(payload, classification)
    policy = ContextPolicy(
        classification=effective_classification,
        expires_at=expires_at,
        allowed_fields=allowed_fields,
    )
    lineage = ContextLineage(
        context_id=context_id or new_context_id(),
        run_id=run_id,
        assignment_id=assignment.assignment_id,
        assignment_sha256=assignment_sha256(assignment),
        attempt_id=attempt_id or new_attempt_id(),
        parent_context_id=parent_context_id,
        source_sha256=source_sha256 or payload_sha256,
    )
    unsigned_json: dict[str, Any] = {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "canonicalization_version": CANONICALIZATION_VERSION,
        "hash_algorithm": HASH_ALGORITHM,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
        "policy": policy.model_dump(mode="json"),
        "lineage": lineage.model_dump(mode="json"),
        "canonical_payload": canonical_payload,
        "payload_sha256": payload_sha256,
        "policy_sha256": _policy_sha256(policy),
    }
    return ContextEnvelope.model_validate(
        {
            **unsigned_json,
            "created_at": created_at,
            "policy": policy,
            "lineage": lineage,
            "integrity_sha256": _integrity_sha256(unsigned_json),
        }
    )


def build_context_envelope(
    context: AgentContext,
    assignment: AgentAssignment,
    *,
    run_id: str,
    expires_at: datetime,
    clock: Clock,
    classification: ContextClassification = ContextClassification.INTERNAL,
    context_id: str | None = None,
    attempt_id: str | None = None,
) -> ContextEnvelope:
    """Build an initial envelope; parent/source lineage is retry-only."""

    return _build_context_envelope(
        context,
        assignment,
        run_id=run_id,
        expires_at=expires_at,
        clock=clock,
        classification=classification,
        context_id=context_id,
        attempt_id=attempt_id,
    )


def build_retry_context_envelope(
    parent: ContextEnvelope,
    context: AgentContext,
    assignment: AgentAssignment,
    *,
    run_id: str,
    expires_at: datetime,
    clock: Clock,
    context_id: str | None = None,
    attempt_id: str | None = None,
) -> ContextEnvelope:
    """Build retry lineage only; this module does not execute automatic retries."""

    try:
        parent = ContextEnvelope.model_validate(parent)
    except (TypeError, ValueError) as exc:
        raise ContextLifecycleError("parent context envelope schema validation failed") from exc
    if parent.lineage.run_id != run_id:
        raise ContextLifecycleError("retry run must match parent lineage")
    if parent.lineage.assignment_id != assignment.assignment_id:
        raise ContextLifecycleError("retry assignment must match parent lineage")
    if parent.lineage.assignment_sha256 != assignment_sha256(assignment):
        raise ContextLifecycleError("retry assignment integrity must match parent lineage")
    retry_now = _require_utc(clock(), "clock result")
    validate_context_envelope(
        parent,
        assignment,
        run_id=run_id,
        expected_context_id=parent.lineage.context_id,
        attempt_id=parent.lineage.attempt_id,
        now=retry_now,
        expected_parent_context_id=parent.lineage.parent_context_id,
        expected_source_sha256=parent.lineage.source_sha256,
    )

    def retry_clock() -> datetime:
        return retry_now

    retry = _build_context_envelope(
        context,
        assignment,
        run_id=run_id,
        expires_at=expires_at,
        clock=retry_clock,
        classification=parent.policy.classification,
        context_id=context_id,
        attempt_id=attempt_id,
        parent_context_id=parent.lineage.context_id,
        source_sha256=parent.lineage.source_sha256,
    )
    if retry.lineage.context_id == parent.lineage.context_id:
        raise ContextLifecycleError("retry context_id must be fresh")
    if retry.lineage.attempt_id == parent.lineage.attempt_id:
        raise ContextLifecycleError("retry attempt_id must be fresh")
    return retry


def validate_context_envelope(
    envelope: ContextEnvelope,
    assignment: AgentAssignment,
    *,
    run_id: str,
    expected_context_id: str,
    attempt_id: str,
    now: datetime,
    expected_parent_context_id: str | None = None,
    expected_source_sha256: str | None = None,
) -> AgentContext:
    """Validate policy, integrity, correlation, and return a fresh provider copy."""

    now = _require_utc(now, "now")
    try:
        candidate = ContextEnvelope.model_validate(envelope)
    except (TypeError, ValueError) as exc:
        raise ContextLifecycleError("context envelope schema validation failed") from exc
    if now < candidate.created_at:
        raise ContextLifecycleError("context envelope was created in the future")
    if now >= candidate.policy.expires_at:
        raise ContextLifecycleError("context envelope has expired")
    if candidate.lineage.run_id != run_id:
        raise ContextLifecycleError("context run correlation mismatch")
    if candidate.lineage.context_id != expected_context_id:
        raise ContextLifecycleError("context ID correlation mismatch")
    if candidate.lineage.assignment_id != assignment.assignment_id:
        raise ContextLifecycleError("context assignment correlation mismatch")
    if candidate.lineage.assignment_sha256 != assignment_sha256(assignment):
        raise ContextLifecycleError("context assignment integrity mismatch")
    if candidate.lineage.attempt_id != attempt_id:
        raise ContextLifecycleError("context attempt correlation mismatch")
    if candidate.lineage.parent_context_id != expected_parent_context_id:
        raise ContextLifecycleError("context parent lineage mismatch")
    if expected_parent_context_id is not None and expected_source_sha256 is None:
        raise ContextLifecycleError("retry context requires expected source lineage")
    if (
        expected_source_sha256 is not None
        and candidate.lineage.source_sha256 != expected_source_sha256
    ):
        raise ContextLifecycleError("context source lineage mismatch")
    if expected_parent_context_id is None and (
        candidate.lineage.source_sha256 != candidate.payload_sha256
    ):
        raise ContextLifecycleError("initial context source hash mismatch")
    if candidate.lineage.producer_runtime is not RuntimeIdentity.PYTHON_LOCAL:
        raise ContextLifecycleError("unsupported context producer runtime")
    if candidate.lineage.consumer_runtime is not RuntimeIdentity.PYTHON_LOCAL:
        raise ContextLifecycleError("unsupported context consumer runtime")
    if candidate.policy_sha256 != _policy_sha256(candidate.policy):
        raise ContextLifecycleError("context policy integrity mismatch")
    if candidate.payload_sha256 != _sha256_text(candidate.canonical_payload):
        raise ContextLifecycleError("context payload integrity mismatch")
    if candidate.integrity_sha256 != _integrity_sha256(candidate):
        raise ContextLifecycleError("context envelope integrity mismatch")
    try:
        raw_payload = json.loads(candidate.canonical_payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContextLifecycleError("context payload is not valid JSON") from exc
    if not isinstance(raw_payload, dict):
        raise ContextLifecycleError("context payload must be a JSON object")
    if _canonical_json(raw_payload) != candidate.canonical_payload:
        raise ContextLifecycleError("context payload is not in canonical form")
    payload_fields = tuple(sorted(raw_payload))
    if payload_fields != candidate.policy.allowed_fields:
        raise ContextLifecycleError("context payload does not match the policy allowlist")
    if tuple(assignment.context_keys) != candidate.policy.allowed_fields:
        raise ContextLifecycleError("assignment context_keys do not match the policy allowlist")
    if (
        candidate.policy.classification is ContextClassification.RESTRICTED
        or _contains_restricted_secret(raw_payload)
    ):
        raise ContextHandoffRefused("restricted context cannot cross the provider boundary")
    try:
        return AgentContext.model_validate(raw_payload)
    except ValueError as exc:
        raise ContextLifecycleError("context payload schema validation failed") from exc
