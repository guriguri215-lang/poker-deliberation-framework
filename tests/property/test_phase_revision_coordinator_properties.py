"""P2-010B phase revision coordinator property tests."""

from __future__ import annotations

import hashlib
import json

from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.phases.revision_coordinator import (
    PhaseTransitionPlanV1,
    _is_issued_plan,
    _issue_transition_plan,
)

_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=0,
    max_size=24,
)


def _digest(domain: str, value: object) -> str:
    data = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + data).hexdigest()


@given(
    st.lists(
        st.fixed_dictionaries({"source": _TEXT, "target": _TEXT, "reason": _TEXT}),
        max_size=12,
    )
)
def test_plan_hash_is_deterministic_for_every_ordered_event_prefix(
    raw_events: list[dict[str, str]],
) -> None:
    events = tuple(raw_events)
    owner = object()

    first = _issue_transition_plan(run_id="run-property", events=events, owner=owner)
    second = _issue_transition_plan(run_id="run-property", events=events, owner=owner)

    assert first == second
    assert first.event_count == len(events)
    assert first.event_prefix_sha256 == _digest(
        "poker-phase-transition-event-prefix-v1",
        events,
    )
    assert _is_issued_plan(first)
    assert _is_issued_plan(second)


@given(st.integers(min_value=0, max_value=1000))
def test_reconstructed_plan_never_inherits_factory_identity(event_count: int) -> None:
    events = tuple(
        {"source": "A", "target": "B", "reason": str(index)} for index in range(event_count)
    )
    issued = _issue_transition_plan(run_id="run-property", events=events, owner=object())

    reconstructed = PhaseTransitionPlanV1.model_validate(issued.model_dump(mode="python"))

    assert reconstructed == issued
    assert not _is_issued_plan(reconstructed)


@given(st.text(min_size=1, max_size=48))
def test_any_in_place_plan_tamper_invalidates_issued_identity(reason: str) -> None:
    issued = _issue_transition_plan(run_id="run-property", events=(), owner=object())
    if reason == issued.reason:
        return

    object.__setattr__(issued, "reason", reason)

    assert not _is_issued_plan(issued)
