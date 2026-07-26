from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.context_lifecycle import (
    ContextLifecycleError,
    build_context_envelope,
    build_retry_context_envelope,
    context_payload,
    validate_context_envelope,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext

pytestmark = pytest.mark.property

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
SAFE_VALUES = st.dictionaries(
    st.from_regex(r"[a-z]{1,8}", fullmatch=True),
    st.integers(min_value=-1_000_000, max_value=1_000_000),
    max_size=12,
)


def _assignment(context: AgentContext) -> AgentAssignment:
    return AgentAssignment(
        assignment_id="assignment-property",
        agent_role="math-auditor",
        task="verify",
        context_keys=sorted(context_payload(context)),
    )


@given(SAFE_VALUES)
def test_mapping_order_does_not_change_payload_hash(values: dict[str, int]) -> None:
    reverse_values = dict(reversed(list(values.items())))
    first_context = AgentContext(
        kind="calculation", objective="verify", tool_inputs={"values": values}
    )
    second_context = AgentContext(
        kind="calculation", objective="verify", tool_inputs={"values": reverse_values}
    )
    first_assignment = _assignment(first_context)
    second_assignment = _assignment(second_context)

    first = build_context_envelope(
        first_context,
        first_assignment,
        run_id="run-property",
        expires_at=NOW + timedelta(seconds=1),
        clock=lambda: NOW,
        context_id="context-property",
        attempt_id="attempt-property",
    )
    second = build_context_envelope(
        second_context,
        second_assignment,
        run_id="run-property",
        expires_at=NOW + timedelta(seconds=1),
        clock=lambda: NOW,
        context_id="context-property",
        attempt_id="attempt-property",
    )

    assert first.payload_sha256 == second.payload_sha256
    assert first.integrity_sha256 == second.integrity_sha256


@given(SAFE_VALUES)
def test_round_trip_preserves_payload_and_stale_hash_rejects_change(
    values: dict[str, int],
) -> None:
    context = AgentContext(kind="calculation", objective="verify", tool_inputs={"values": values})
    assignment = _assignment(context)
    envelope = build_context_envelope(
        context,
        assignment,
        run_id="run-property",
        expires_at=NOW + timedelta(seconds=1),
        clock=lambda: NOW,
        context_id="context-property",
        attempt_id="attempt-property",
    )

    delivered = validate_context_envelope(
        envelope,
        assignment,
        run_id="run-property",
        expected_context_id="context-property",
        attempt_id="attempt-property",
        now=NOW,
    )
    assert context_payload(delivered) == context_payload(context)

    tampered = envelope.model_copy(update={"canonical_payload": envelope.canonical_payload + " "})
    with pytest.raises(ContextLifecycleError, match="payload integrity"):
        validate_context_envelope(
            tampered,
            assignment,
            run_id="run-property",
            expected_context_id="context-property",
            attempt_id="attempt-property",
            now=NOW,
        )


@given(SAFE_VALUES)
def test_retry_has_fresh_ids_and_preserves_root_source(values: dict[str, int]) -> None:
    context = AgentContext(kind="calculation", objective="verify", tool_inputs={"values": values})
    assignment = _assignment(context)
    parent = build_context_envelope(
        context,
        assignment,
        run_id="run-property",
        expires_at=NOW + timedelta(seconds=1),
        clock=lambda: NOW,
    )
    retry = build_retry_context_envelope(
        parent,
        context,
        assignment,
        run_id="run-property",
        expires_at=NOW + timedelta(seconds=2),
        clock=lambda: NOW + timedelta(milliseconds=500),
    )

    assert retry.lineage.context_id != parent.lineage.context_id
    assert retry.lineage.attempt_id != parent.lineage.attempt_id
    assert retry.lineage.parent_context_id == parent.lineage.context_id
    assert retry.lineage.source_sha256 == parent.lineage.source_sha256
    validate_context_envelope(
        retry,
        assignment,
        run_id="run-property",
        expected_context_id=retry.lineage.context_id,
        attempt_id=retry.lineage.attempt_id,
        now=NOW + timedelta(milliseconds=500),
        expected_parent_context_id=parent.lineage.context_id,
        expected_source_sha256=parent.lineage.source_sha256,
    )
