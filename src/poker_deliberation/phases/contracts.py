"""Strict internal contracts shared by the P2-010A phase boundaries."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PHASE_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class PhaseContractError(ValueError):
    """A request or outcome failed its internal phase contract."""


class PhaseId(StrEnum):
    INTAKE_VALIDATION = "intake_validation"
    NORMALIZATION = "normalization"
    ROUTING = "routing"
    CONTEXT_BUILD = "context_build"
    ANALYSIS = "analysis"
    TOOL_RESEARCH = "tool_research"
    CRITIQUE = "critique"
    ADJUDICATION = "adjudication"
    SYNTHESIS = "synthesis"


class PhaseStatus(StrEnum):
    SUCCEEDED = "succeeded"
    COMPLETED_WITH_FAILURES = "completed_with_failures"
    FAILED = "failed"


class PhaseFailureCode(StrEnum):
    VALIDATION = "validation"
    PRECONDITION = "precondition"
    ISOLATION = "isolation"
    CORRELATION = "correlation"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    PURE_COMPUTE = "pure_compute"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_REFUSED = "provider_refused"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_FAILURE = "provider_failure"
    PROVIDER_TRANSIENT = "provider_transient"
    TOOL_FAILURE = "tool_failure"
    TOOL_TRANSIENT = "tool_transient"
    BUDGET_EXCEEDED = "budget_exceeded"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_UNCONFIRMED = "cancel_unconfirmed"
    MALFORMED_OUTCOME = "malformed_outcome"
    ILLEGAL_REQUESTED_TRANSITION = "illegal_requested_transition"
    UNSAFE_ARTIFACT_IDENTITY = "unsafe_artifact_identity"


class ArtifactKind(StrEnum):
    AGENT_EXECUTION_RECORDS = "agent_execution_records"
    SECURITY_EVENTS = "security_events"
    STATE = "state"
    APPROVALS = "approvals"
    DISPUTES = "disputes"
    FINAL_REPORT_JSON = "final_report_json"
    FINAL_REPORT_MARKDOWN = "final_report_markdown"


class _PhaseModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_json(value: Any) -> str:
    """Serialize one phase value deterministically and reject non-JSON numbers."""

    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise PhaseContractError("phase value is not canonical JSON") from exc


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class PhaseFailureCause(_PhaseModel):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=512)

    @field_validator("message")
    @classmethod
    def sanitized_message(cls, value: str) -> str:
        if any(ord(character) < 32 and character not in "\t" for character in value):
            raise ValueError("failure messages must not contain control characters")
        return value


class PhaseFailure(_PhaseModel):
    code: PhaseFailureCode
    phase_id: PhaseId
    attempt_id: str
    retryable: bool
    message: str = Field(min_length=1, max_length=512)
    causes: tuple[PhaseFailureCause, ...] = ()

    @field_validator("attempt_id")
    @classmethod
    def portable_attempt_id(cls, value: str) -> str:
        if not _PORTABLE_ID.fullmatch(value):
            raise ValueError("attempt_id must use the portable ID format")
        return value

    @model_validator(mode="after")
    def retryability_matches_failure_code(self) -> PhaseFailure:
        retryable_codes = {
            PhaseFailureCode.PROVIDER_TRANSIENT,
            PhaseFailureCode.TOOL_TRANSIENT,
        }
        if self.retryable != (self.code in retryable_codes):
            raise ValueError("retryable must match the typed transient failure taxonomy")
        return self


class ArtifactIntent(_PhaseModel):
    kind: ArtifactKind
    relative_path: str = Field(min_length=1, max_length=256)
    media_type: Literal["application/json", "text/markdown"]
    content_sha256: str | None = None

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        if (
            "\\" in value
            or ":" in value
            or value.startswith("/")
            or "://" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("artifact path must be a portable repository-relative path")
        return value

    @field_validator("content_sha256")
    @classmethod
    def valid_optional_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("content_sha256 must be lowercase SHA-256")
        return value


InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")


class PhaseRequest(_PhaseModel, Generic[InputT]):
    run_id: str
    phase_id: PhaseId
    phase_schema_version: Literal["1.0.0"]
    attempt_id: str
    input_hash: str
    policy_snapshot_hash: str
    context_ids: tuple[str, ...] = ()
    input: InputT

    @field_validator("run_id", "attempt_id")
    @classmethod
    def portable_id(cls, value: str) -> str:
        if not _PORTABLE_ID.fullmatch(value):
            raise ValueError("phase correlation IDs must use the portable ID format")
        return value

    @field_validator("input_hash", "policy_snapshot_hash")
    @classmethod
    def valid_sha256(cls, value: str) -> str:
        if not _SHA256.fullmatch(value):
            raise ValueError("phase hashes must be lowercase SHA-256")
        return value

    @field_validator("context_ids")
    @classmethod
    def ordered_unique_context_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("context_ids must be unique")
        if any(not _PORTABLE_ID.fullmatch(item) for item in value):
            raise ValueError("context_ids must use the portable ID format")
        return value

    @model_validator(mode="after")
    def input_hash_matches(self) -> PhaseRequest[InputT]:
        if canonical_sha256(self.input) != self.input_hash:
            raise ValueError("phase input hash mismatch")
        return self


class PhaseOutcome(_PhaseModel, Generic[OutputT]):
    run_id: str
    phase_id: PhaseId
    phase_schema_version: Literal["1.0.0"]
    attempt_id: str
    input_hash: str
    policy_snapshot_hash: str
    context_ids: tuple[str, ...] = ()
    status: PhaseStatus
    output: OutputT | None = None
    output_hash: str | None = None
    failure: PhaseFailure | None = None
    warnings: tuple[str, ...] = ()
    requested_next_state: str | None = None
    artifact_intents: tuple[ArtifactIntent, ...] = ()

    @field_validator("run_id", "attempt_id")
    @classmethod
    def portable_id(cls, value: str) -> str:
        if not _PORTABLE_ID.fullmatch(value):
            raise ValueError("phase correlation IDs must use the portable ID format")
        return value

    @field_validator("input_hash", "policy_snapshot_hash", "output_hash")
    @classmethod
    def valid_sha256(cls, value: str | None) -> str | None:
        if value is not None and not _SHA256.fullmatch(value):
            raise ValueError("phase hashes must be lowercase SHA-256")
        return value

    @model_validator(mode="after")
    def outcome_shape_matches_status(self) -> PhaseOutcome[OutputT]:
        if self.status is PhaseStatus.FAILED:
            if self.output is not None or self.output_hash is not None or self.failure is None:
                raise ValueError("failed phase outcomes require only a top-level failure")
        else:
            if self.output is None or self.failure is not None:
                raise ValueError("output-bearing phase outcomes cannot carry a top-level failure")
            if self.output_hash != canonical_sha256(self.output):
                raise ValueError("phase output hash mismatch")
        return self


def make_phase_request(
    *,
    run_id: str,
    phase_id: PhaseId,
    attempt_id: str,
    policy_snapshot_hash: str,
    input_value: InputT,
    context_ids: tuple[str, ...] = (),
) -> PhaseRequest[InputT]:
    return PhaseRequest[InputT](
        run_id=run_id,
        phase_id=phase_id,
        phase_schema_version=PHASE_SCHEMA_VERSION,
        attempt_id=attempt_id,
        input_hash=canonical_sha256(input_value),
        policy_snapshot_hash=policy_snapshot_hash,
        context_ids=context_ids,
        input=input_value,
    )


def successful_outcome(
    request: PhaseRequest[Any],
    output: OutputT,
    *,
    warnings: tuple[str, ...] = (),
    completed_with_failures: bool = False,
    requested_next_state: str | None = None,
    artifact_intents: tuple[ArtifactIntent, ...] = (),
) -> PhaseOutcome[OutputT]:
    return PhaseOutcome[OutputT](
        run_id=request.run_id,
        phase_id=request.phase_id,
        phase_schema_version=request.phase_schema_version,
        attempt_id=request.attempt_id,
        input_hash=request.input_hash,
        policy_snapshot_hash=request.policy_snapshot_hash,
        context_ids=request.context_ids,
        status=(
            PhaseStatus.COMPLETED_WITH_FAILURES
            if completed_with_failures
            else PhaseStatus.SUCCEEDED
        ),
        output=output,
        output_hash=canonical_sha256(output),
        warnings=warnings,
        requested_next_state=requested_next_state,
        artifact_intents=artifact_intents,
    )


def failed_outcome(
    request: PhaseRequest[Any],
    failure: PhaseFailure,
) -> PhaseOutcome[Any]:
    return PhaseOutcome[Any](
        run_id=request.run_id,
        phase_id=request.phase_id,
        phase_schema_version=request.phase_schema_version,
        attempt_id=request.attempt_id,
        input_hash=request.input_hash,
        policy_snapshot_hash=request.policy_snapshot_hash,
        context_ids=request.context_ids,
        status=PhaseStatus.FAILED,
        failure=failure,
    )


def revalidate_request(
    request: PhaseRequest[Any],
    *,
    phase_id: PhaseId,
    input_type: type[InputT],
) -> PhaseRequest[InputT]:
    try:
        isolated = PhaseRequest[input_type].model_validate(  # type: ignore[valid-type]
            request.model_dump(mode="python")
        )
    except ValueError as exc:
        raise PhaseContractError("malformed phase request") from exc
    if isolated.phase_id is not phase_id:
        raise PhaseContractError(
            f"phase/input mismatch: expected {phase_id.value}, got {isolated.phase_id.value}"
        )
    return cast(PhaseRequest[InputT], isolated)


def revalidate_outcome(
    request: PhaseRequest[Any],
    outcome: PhaseOutcome[Any],
    *,
    output_type: type[OutputT],
) -> PhaseOutcome[OutputT]:
    try:
        isolated = PhaseOutcome[output_type].model_validate(  # type: ignore[valid-type]
            outcome.model_dump(mode="python")
        )
    except ValueError as exc:
        raise PhaseContractError("malformed phase outcome") from exc
    correlation = (
        isolated.run_id,
        isolated.phase_id,
        isolated.phase_schema_version,
        isolated.attempt_id,
        isolated.input_hash,
        isolated.policy_snapshot_hash,
        isolated.context_ids,
    )
    expected = (
        request.run_id,
        request.phase_id,
        request.phase_schema_version,
        request.attempt_id,
        request.input_hash,
        request.policy_snapshot_hash,
        request.context_ids,
    )
    if correlation != expected:
        raise PhaseContractError("phase outcome correlation mismatch")
    return cast(PhaseOutcome[OutputT], isolated)
