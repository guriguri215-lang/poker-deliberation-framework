"""P2-010B phase revision coordinator unit tests."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

import poker_deliberation.orchestrator as orchestrator_module
from poker_deliberation.budgets import BudgetFailure, BudgetFailureCode
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
from poker_deliberation.tools.contracts import contract_by_name


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


def test_revision_coordinator_reexecutes_floating_verification_metadata() -> None:
    actual = default_registry().execute(
        "pot_odds",
        {
            "pot_before_bet": 100.0,
            "opponent_bet": 50.0,
            "call_cost": 50.0,
            "expected_rake": 0.0,
        },
        contract_version="2.0.0",
    )
    _validate_tool_result_contract(actual)
    assert actual.verification is not None
    forged = actual.model_copy(
        update={
            "verification": actual.verification.model_copy(
                update={"observations": [*actual.verification.observations, "self-attested"]}
            )
        }
    )

    with pytest.raises(ValueError, match="canonical replay"):
        _validate_tool_result_contract(forged)


def test_revision_coordinator_does_not_accept_phase_self_attested_budget_failure() -> None:
    contract = contract_by_name()["combos"]
    forged = ToolResult(
        result_id="tool-result-phase-budget",
        tool_name="combos",
        input={"hand_class": "AA"},
        status=ToolStatus.FAILED,
        exactness=Exactness.UNAVAILABLE,
        numeric_exactness=NumericalExactness.UNAVAILABLE,
        contract_version=contract.contract_version,
        error="strict budget failure: tool_input_exceeded",
    )
    phase_self_attestation = BudgetFailure(
        code=BudgetFailureCode.TOOL_INPUT_EXCEEDED,
        resource="tool_input_bytes",
        message="phase claims its own tool input exceeded",
        limit=10,
        observed=11,
    )

    # The value can be typed and internally correlated while still not being
    # independent publication authority.  The coordinator exposes no seam for
    # passing it to durable verification.
    assert phase_self_attestation.code.value in (forged.error or "")
    with pytest.raises(ValueError, match="storage authority"):
        _validate_tool_result_contract(forged)


def test_revision_coordinator_seam_is_not_a_public_orchestrator_api() -> None:
    assert not hasattr(orchestrator_module, "PhaseRevisionCoordinator")
    assert not hasattr(orchestrator_module, "PhaseRevisionBundleV1")
    assert not hasattr(Orchestrator, "prepare_revision_bundle")
    assert not hasattr(Orchestrator, "apply_revision_transition")
