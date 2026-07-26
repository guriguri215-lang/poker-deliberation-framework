from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from poker_deliberation.context_lifecycle import (
    ATTEMPT_MEMORY_ONLY_RETENTION_POLICY,
    ContextClassification,
    ContextEnvelope,
    ContextLifecycleError,
    ContextPolicy,
    RuntimeIdentity,
    build_context_envelope,
    context_payload,
    validate_context_envelope,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)


def _context() -> AgentContext:
    return AgentContext(
        kind="strategy",
        objective="review",
        strategy_text="compare lines",
        tool_inputs={"scenario": {"values": [1, 2]}},
    )


def _assignment(context: AgentContext) -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assignment-lifecycle",
        agent_role="skeptic",
        task="find counterexamples",
        context_keys=sorted(context_payload(context)),
    )


def _envelope(
    context: AgentContext | None = None,
    assignment: AgentAssignment | None = None,
) -> tuple[ContextEnvelope, AgentAssignment]:
    actual_context = context or _context()
    actual_assignment = assignment or _assignment(actual_context)
    return (
        build_context_envelope(
            actual_context,
            actual_assignment,
            run_id="run-lifecycle",
            expires_at=NOW + timedelta(seconds=30),
            clock=lambda: NOW,
            context_id="context-lifecycle",
            attempt_id="attempt-lifecycle",
        ),
        actual_assignment,
    )


def test_policy_is_strict_frozen_versioned_and_internal_by_default() -> None:
    envelope, _ = _envelope()

    assert envelope.schema_version == "1.0.0"
    assert envelope.policy.classification is ContextClassification.INTERNAL
    assert envelope.policy.retention_policy_id == ATTEMPT_MEMORY_ONLY_RETENTION_POLICY
    assert envelope.lineage.producer_runtime is RuntimeIdentity.PYTHON_LOCAL
    assert envelope.lineage.consumer_runtime is RuntimeIdentity.PYTHON_LOCAL
    with pytest.raises(ValidationError):
        envelope.policy.classification = ContextClassification.PUBLIC
    with pytest.raises(ValidationError):
        ContextPolicy.model_validate(
            {
                "expires_at": NOW + timedelta(seconds=1),
                "allowed_fields": ("kind", "objective"),
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    "classification",
    [
        ContextClassification.PUBLIC,
        ContextClassification.INTERNAL,
        ContextClassification.SENSITIVE,
    ],
)
def test_nonrestricted_classifications_allow_python_local_handoff(
    classification: ContextClassification,
) -> None:
    context = _context()
    assignment = _assignment(context)
    envelope = build_context_envelope(
        context,
        assignment,
        run_id="run-lifecycle",
        expires_at=NOW + timedelta(seconds=1),
        clock=lambda: NOW,
        classification=classification,
        context_id="context-lifecycle",
        attempt_id="attempt-lifecycle",
    )

    delivered = validate_context_envelope(
        envelope,
        assignment,
        run_id="run-lifecycle",
        expected_context_id="context-lifecycle",
        attempt_id="attempt-lifecycle",
        now=NOW,
    )
    assert envelope.policy.classification is classification
    assert delivered == context


def test_unknown_retention_policy_id_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ContextPolicy.model_validate(
            {
                "expires_at": NOW + timedelta(seconds=1),
                "allowed_fields": ("kind", "objective"),
                "retention_policy_id": "invented-duration-v1",
            }
        )


@pytest.mark.parametrize("field", ["a.b", "a-b", "", "a b"])
def test_allowlist_accepts_top_level_identifiers_only(field: str) -> None:
    with pytest.raises(ValidationError):
        ContextPolicy(
            expires_at=NOW + timedelta(seconds=1),
            allowed_fields=(field,),
        )


def test_builder_requires_exact_assignment_allowlist() -> None:
    context = _context()
    assignment = _assignment(context).model_copy(update={"context_keys": ["kind"]})

    with pytest.raises(ContextLifecycleError, match="allowlist"):
        build_context_envelope(
            context,
            assignment,
            run_id="run-lifecycle",
            expires_at=NOW + timedelta(seconds=1),
            clock=lambda: NOW,
        )


def test_payload_is_deeply_isolated_and_each_delivery_is_fresh() -> None:
    context = _context()
    envelope, assignment = _envelope(context)
    context.tool_inputs["scenario"]["values"].append(99)

    delivered = validate_context_envelope(
        envelope,
        assignment,
        run_id="run-lifecycle",
        expected_context_id="context-lifecycle",
        attempt_id="attempt-lifecycle",
        now=NOW,
    )
    assert delivered.tool_inputs == {"scenario": {"values": [1, 2]}}
    delivered.tool_inputs["scenario"]["values"].append(77)

    delivered_again = validate_context_envelope(
        envelope,
        assignment,
        run_id="run-lifecycle",
        expected_context_id="context-lifecycle",
        attempt_id="attempt-lifecycle",
        now=NOW,
    )
    assert delivered_again.tool_inputs == {"scenario": {"values": [1, 2]}}
    assert delivered_again is not delivered


def test_hashes_are_deterministic_for_identical_versioned_input() -> None:
    first, assignment = _envelope()
    second, _ = _envelope(assignment=assignment)

    assert first.payload_sha256 == second.payload_sha256
    assert first.policy_sha256 == second.policy_sha256
    assert first.integrity_sha256 == second.integrity_sha256


def test_expiry_uses_injected_utc_clock_and_rejects_boundary() -> None:
    envelope, assignment = _envelope()

    with pytest.raises(ContextLifecycleError, match="expired"):
        validate_context_envelope(
            envelope,
            assignment,
            run_id="run-lifecycle",
            expected_context_id="context-lifecycle",
            attempt_id="attempt-lifecycle",
            now=envelope.policy.expires_at,
        )
    with pytest.raises(ValueError, match="UTC"):
        build_context_envelope(
            _context(),
            _assignment(_context()),
            run_id="run-lifecycle",
            expires_at=(NOW + timedelta(seconds=1)).replace(tzinfo=None),
            clock=lambda: NOW,
        )


def test_unknown_schema_and_runtime_fail_strict_validation() -> None:
    envelope, _ = _envelope()
    unknown_schema = envelope.model_dump(mode="python")
    unknown_schema["schema_version"] = "2.0.0"
    with pytest.raises(ValidationError):
        ContextEnvelope.model_validate(unknown_schema)

    unknown_runtime = envelope.model_dump(mode="python")
    unknown_runtime["lineage"]["consumer_runtime"] = "codex-bridge"
    with pytest.raises(ValidationError):
        ContextEnvelope.model_validate(unknown_runtime)
