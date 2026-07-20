from __future__ import annotations

from copy import deepcopy

from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.phases import PhaseId, canonical_sha256, make_phase_request
from poker_deliberation.phases.models import IntakeValidationInput
from poker_deliberation.phases.services import IntakeValidationService
from poker_deliberation.schemas import CaseInput


@given(st.dictionaries(st.text(min_size=1, max_size=8), st.integers(), max_size=12))
def test_canonical_hash_is_independent_of_mapping_insertion_order(
    value: dict[str, int],
) -> None:
    assert canonical_sha256(value) == canonical_sha256(dict(reversed(list(value.items()))))


@given(st.text(min_size=1, max_size=30), st.text(min_size=1, max_size=30))
def test_one_input_field_delta_changes_phase_hash(first: str, second: str) -> None:
    if first == second:
        return
    assert canonical_sha256({"value": first}) != canonical_sha256({"value": second})


@given(
    st.lists(st.integers(min_value=-100, max_value=100), max_size=20),
    st.booleans(),
)
def test_intake_phase_is_stable_and_nested_input_isolation_holds(
    items: list[int],
    record_sensitive_data: bool,
) -> None:
    case = CaseInput(
        kind="strategy",
        raw_text="review",
        metadata={"nested": {"items": list(items)}},
    )
    original = deepcopy(case.model_dump(mode="python"))
    value = IntakeValidationInput(
        case=case,
        record_sensitive_data=record_sensitive_data,
    )
    request = make_phase_request(
        run_id="run-property",
        phase_id=PhaseId.INTAKE_VALIDATION,
        attempt_id="phase-intake-property",
        policy_snapshot_hash="a" * 64,
        input_value=value,
    )
    service = IntakeValidationService()
    first = service.run(request)
    second = service.run(request)

    assert first.output_hash == second.output_hash
    assert case.model_dump(mode="python") == original
    assert request.input.case.metadata["nested"]["items"] == items
