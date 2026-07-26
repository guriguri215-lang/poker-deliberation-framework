from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from poker_deliberation.phases import (
    ArtifactIntent,
    ArtifactKind,
    PhaseContractError,
    PhaseFailure,
    PhaseFailureCode,
    PhaseId,
    PhaseOutcome,
    PhaseRequest,
    PhaseStatus,
    canonical_sha256,
    make_phase_request,
    revalidate_outcome,
)
from poker_deliberation.phases.contracts import failed_outcome, successful_outcome
from poker_deliberation.phases.models import (
    IntakeValidationInput,
    NormalizationInput,
    NormalizationOutput,
)
from poker_deliberation.schemas import CaseInput


def _request() -> PhaseRequest[NormalizationInput]:
    value = NormalizationInput(
        safe_case=CaseInput(kind="strategy", raw_text="review"),
    )
    return make_phase_request(
        run_id="run-contract",
        phase_id=PhaseId.NORMALIZATION,
        attempt_id="phase-normalization-1",
        policy_snapshot_hash="a" * 64,
        input_value=value,
    )


def test_phase_request_rejects_missing_extra_hash_and_version_changes() -> None:
    request = _request()
    payload = request.model_dump(mode="python")

    for field in ("phase_schema_version", "run_id", "phase_id", "attempt_id", "input_hash"):
        missing = dict(payload)
        missing.pop(field)
        with pytest.raises(ValidationError):
            PhaseRequest[NormalizationInput].model_validate(missing)

    with pytest.raises(ValidationError, match="Extra inputs"):
        PhaseRequest[NormalizationInput].model_validate({**payload, "state": "COMPLETED"})
    with pytest.raises(ValidationError, match=r"Input should be '1\.0\.0'"):
        PhaseRequest[NormalizationInput].model_validate(
            {**payload, "phase_schema_version": "9.0.0"}
        )
    with pytest.raises(ValidationError, match="input hash mismatch"):
        PhaseRequest[NormalizationInput].model_validate({**payload, "input_hash": "b" * 64})


def test_phase_models_reject_type_coercion() -> None:
    request = _request()
    payload = request.model_dump(mode="python")
    with pytest.raises(ValidationError):
        PhaseRequest[NormalizationInput].model_validate({**payload, "context_ids": []})
    with pytest.raises(ValidationError):
        IntakeValidationInput(
            case=CaseInput(kind="strategy", raw_text="review"),
            record_sensitive_data=1,  # type: ignore[arg-type]
            sensitive_action_categories=(),
        )


def test_phase_outcome_requires_exact_shape_hash_and_request_correlation() -> None:
    request = _request()
    output = NormalizationOutput(normalized_case=request.input.safe_case)
    valid = successful_outcome(request, output)
    assert revalidate_outcome(request, valid, output_type=NormalizationOutput).output == output

    forged = valid.model_copy(update={"run_id": "run-forged"})
    with pytest.raises(PhaseContractError, match="correlation mismatch"):
        revalidate_outcome(request, forged, output_type=NormalizationOutput)

    payload = valid.model_dump(mode="python")
    with pytest.raises(ValidationError, match="output hash mismatch"):
        PhaseOutcome[NormalizationOutput].model_validate({**payload, "output_hash": "c" * 64})
    with pytest.raises(ValidationError, match="top-level failure"):
        PhaseOutcome[NormalizationOutput].model_validate(
            {
                **payload,
                "status": PhaseStatus.FAILED,
                "failure": None,
            }
        )


def test_failed_outcome_has_sanitized_typed_failure_and_no_output() -> None:
    request = _request()
    failure = PhaseFailure(
        code=PhaseFailureCode.PURE_COMPUTE,
        phase_id=request.phase_id,
        attempt_id=request.attempt_id,
        retryable=False,
        message="deterministic normalization failure",
    )
    outcome = failed_outcome(request, failure)
    assert outcome.status is PhaseStatus.FAILED
    assert outcome.output is None
    assert outcome.failure == failure


@pytest.mark.parametrize(
    "path",
    ["../state.json", "/state.json", "C:/state.json", "a\\b.json", "a//b.json", "x://y"],
)
def test_artifact_intent_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError, match="artifact path"):
        ArtifactIntent(
            kind=ArtifactKind.STATE,
            relative_path=path,
            media_type="application/json",
        )


def test_canonical_hash_is_stable_for_key_order_and_rejects_nonfinite_numbers() -> None:
    assert canonical_sha256({"b": [2, 1], "a": {"y": 2, "x": 1}}) == canonical_sha256(
        {"a": {"x": 1, "y": 2}, "b": [2, 1]}
    )
    with pytest.raises(PhaseContractError, match="canonical JSON"):
        canonical_sha256({"bad": float("nan")})


def test_provider_style_fields_cannot_be_smuggled_into_phase_models() -> None:
    request = _request()
    payload: dict[str, Any] = request.model_dump(mode="python")
    payload["artifact_path"] = "state.json"
    with pytest.raises(ValidationError, match="Extra inputs"):
        PhaseRequest[NormalizationInput].model_validate(payload)
