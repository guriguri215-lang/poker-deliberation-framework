"""P2-010B phase revision coordinator unit tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

import poker_deliberation.orchestrator as orchestrator_module
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionFailureCode,
    PhaseRevisionFailureV1,
    PhaseTransitionPlanV1,
    _is_issued_plan,
    _issue_transition_plan,
    _validate_tool_result_contract,
)
from poker_deliberation.schemas import (
    Exactness,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.tools import default_registry


def _digest(domain: str, value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\0" + encoded).hexdigest()


def test_transition_plan_has_exact_fields_and_domain_hashes() -> None:
    events = ({"source": "ADJUDICATION", "target": "FINAL_SYNTHESIS", "reason": "ready"},)
    owner = object()

    plan = _issue_transition_plan(run_id="run-plan", events=events, owner=owner)

    projection = {
        "schema_version": "1.0.0",
        "run_id": "run-plan",
        "source": "FINAL_SYNTHESIS",
        "target": "COMPLETED",
        "reason": "durable synthesis revision committed",
        "event_count": 1,
        "event_prefix_sha256": _digest(
            "poker-phase-transition-event-prefix-v1",
            events,
        ),
    }
    assert plan.model_dump(mode="json") == {
        **projection,
        "plan_sha256": _digest("poker-phase-transition-plan-v1", projection),
    }
    assert _is_issued_plan(plan)
    assert _is_issued_plan(plan, owner=owner)
    assert not _is_issued_plan(plan, owner=object())


def test_reparsed_or_wrong_hash_plan_is_not_factory_authority() -> None:
    plan = _issue_transition_plan(run_id="run-plan", events=(), owner=object())

    reparsed = PhaseTransitionPlanV1.model_validate(plan.model_dump(mode="python"))
    assert reparsed == plan
    assert reparsed is not plan
    assert not _is_issued_plan(reparsed)

    with pytest.raises(ValidationError):
        PhaseTransitionPlanV1.model_validate({**plan.model_dump(mode="python"), "event_count": 1})


def test_failure_value_is_closed_frozen_data() -> None:
    failure = PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)

    assert failure.model_dump(mode="json") == {
        "schema_version": "1.0.0",
        "code": "publish_uncertain",
    }
    assert set(type(failure).model_fields) == {"schema_version", "code"}
    with pytest.raises(ValidationError):
        PhaseRevisionFailureV1.model_validate(
            {
                "schema_version": "1.0.0",
                "code": "publish_uncertain",
                "exception": "forbidden",
            }
        )


def test_solver_status_result_requires_canonical_unavailable_contract() -> None:
    actual = default_registry().execute(
        "solver_status",
        {},
        contract_version="2.0.0",
    )
    _validate_tool_result_contract(actual)
    forged = ToolResult(
        result_id="forged-solver-result",
        tool_name="solver_status",
        input={},
        output={"available": False},
        status=ToolStatus.SUCCESS,
        exactness=Exactness.EXACT,
        numeric_exactness=NumericalExactness.EXACT,
        contract_version="2.0.0",
    )

    with pytest.raises(ValueError):
        _validate_tool_result_contract(forged)


def test_revision_coordinator_seam_is_not_a_public_orchestrator_api() -> None:
    assert not hasattr(orchestrator_module, "PhaseRevisionCoordinator")
    assert not hasattr(orchestrator_module, "PhaseRevisionBundleV1")
    assert not hasattr(Orchestrator, "prepare_revision_bundle")
    assert not hasattr(Orchestrator, "apply_revision_transition")
