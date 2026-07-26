from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import UTC, datetime, timedelta

from poker_deliberation.agents import select_roles
from poker_deliberation.phases import PhaseId, make_phase_request
from poker_deliberation.phases.models import (
    AdjudicationInput,
    ContextBuildInput,
    CritiqueInput,
    IntakeValidationInput,
    NormalizationInput,
    RoutingInput,
)
from poker_deliberation.phases.services import (
    AdjudicationService,
    ContextBuildService,
    CritiqueService,
    IntakeValidationService,
    NormalizationService,
    RoutingService,
)
from poker_deliberation.schemas import (
    AgentReport,
    CaseInput,
    Claim,
    ConfidenceGrade,
    EpistemicLabel,
)
from poker_deliberation.tools import default_registry

POLICY_HASH = "a" * 64


def _request(phase_id: PhaseId, input_value: object):  # type: ignore[no-untyped-def]
    return make_phase_request(
        run_id="run-pure",
        phase_id=phase_id,
        attempt_id=f"phase-{phase_id.value}-1",
        policy_snapshot_hash=POLICY_HASH,
        input_value=input_value,
        context_ids=("context-1",) if phase_id is PhaseId.CONTEXT_BUILD else (),
    )


def test_intake_and_normalization_are_deterministic_and_do_not_mutate_nested_input() -> None:
    case = CaseInput(
        kind="strategy",
        raw_text="review",
        metadata={"normalization_warnings": ["source warning"], "nested": {"items": [1]}},
    )
    original = deepcopy(case.model_dump(mode="python"))
    intake_input = IntakeValidationInput(
        case=case,
        record_sensitive_data=False,
        sensitive_action_categories=(),
    )
    request = _request(PhaseId.INTAKE_VALIDATION, intake_input)

    first = IntakeValidationService().run(request)
    second = IntakeValidationService().run(request)
    assert first.output_hash == second.output_hash
    assert case.model_dump(mode="python") == original
    assert first.output is not None
    assert first.output.data_quality == ("source warning",)

    normalization_input = NormalizationInput(
        safe_case=first.output.safe_case,
        assumptions=({"name": "declared"},),
        warnings=("kept",),
    )
    normalized = NormalizationService().run(_request(PhaseId.NORMALIZATION, normalization_input))
    assert normalized.output is not None
    assert normalized.output.normalized_case == first.output.safe_case
    assert normalization_input.assumptions == ({"name": "declared"},)


def test_routing_preserves_exact_role_order_and_calculation_assignments() -> None:
    service = RoutingService()
    expected = {
        "hand": ("intake", "strategy-analyst", "math-auditor", "skeptic", "adjudicator"),
        "strategy": ("strategy-analyst", "math-auditor", "skeptic", "adjudicator"),
        "claim": ("math-auditor", "evidence-researcher", "skeptic", "adjudicator"),
        "calculation": ("math-auditor", "report-writer"),
    }
    for kind, roles in expected.items():
        case = CaseInput(kind=kind, raw_text="review")
        routing_input = RoutingInput(
            case_kind=kind,
            role_snapshot=tuple(select_roles(case)),
            registered_tools=tuple(default_registry().names()),
        )
        outcome = service.run(_request(PhaseId.ROUTING, routing_input))
        assert outcome.output is not None
        assert tuple(item.agent_role for item in outcome.output.assignments) == roles
        if kind == "calculation":
            assert all(not item.context_keys for item in outcome.output.assignments)


def test_context_build_uses_only_injected_time_ids_and_preserves_lineage() -> None:
    case = CaseInput(kind="strategy", raw_text="review")
    assignment = select_roles(case)[0]
    created_at = datetime(2026, 7, 20, 1, 2, tzinfo=UTC)
    value = ContextBuildInput(
        case=case,
        assignment=assignment,
        registered_tools=tuple(default_registry().names()),
        created_at=created_at,
        expires_at=created_at + timedelta(seconds=30),
        context_id="context-1",
        context_attempt_id="attempt-1",
    )
    outcome = ContextBuildService().run(_request(PhaseId.CONTEXT_BUILD, value))
    assert outcome.output is not None
    dispatch = outcome.output.dispatches[0]
    assert dispatch.envelope.created_at == created_at
    assert dispatch.envelope.lineage.context_id == "context-1"
    assert dispatch.envelope.lineage.attempt_id == "attempt-1"
    assert dispatch.assignment.context_keys == sorted(
        dispatch.context.model_dump(mode="json", exclude_none=True, exclude_defaults=True)
    )


def test_critique_rejects_unadjudicated_provider_claim_as_unknown() -> None:
    provider_claim = Claim(
        claim_id="provider-claim",
        text="This is GTO.",
        label=EpistemicLabel.FACT,
        confidence=ConfidenceGrade.A,
    )
    report = AgentReport(
        report_id="report-1",
        agent_role="skeptic",
        task="check",
        claims=[provider_claim],
    )
    value = CritiqueInput(
        case=CaseInput(kind="strategy", raw_text="review"),
        reports=(report,),
        tool_results=(),
        evidence_ids=(),
    )
    outcome = CritiqueService().run(_request(PhaseId.CRITIQUE, value))
    assert outcome.output is not None
    dispute = outcome.output.disputes[0]
    assert dispute.unresolved is False
    assert "UNKNOWN" in str(dispute.resolution)
    assert provider_claim.label is EpistemicLabel.FACT


def test_adjudication_uses_complete_tool_result_without_mutating_inputs() -> None:
    claim = Claim(
        claim_id="claim-odds",
        text="required equity is 0.5",
        label=EpistemicLabel.USER_CLAIM,
    )
    case = CaseInput(
        kind="calculation",
        claims=[claim],
        requested_tools=["pot_odds"],
        metadata={
            "claim_checks": [
                {
                    "claim_id": claim.claim_id,
                    "tool_name": "pot_odds",
                    "output_path": "required_equity",
                    "claimed_value": 0.5,
                }
            ]
        },
    )
    result = default_registry().execute(
        "pot_odds", {"pot_before_bet": 100, "opponent_bet": 50, "call_cost": 50}
    )
    before = result.model_dump(mode="python")
    outcome = AdjudicationService().run(
        _request(
            PhaseId.ADJUDICATION,
            AdjudicationInput(case=case, tool_results=(result,)),
        )
    )
    assert outcome.output is not None
    correction = outcome.output.claim_assessments[-1]
    assert correction.label is EpistemicLabel.CALCULATED
    assert "CALCULATED=0.25" in correction.text
    assert result.model_dump(mode="python") == before


def test_pure_service_module_has_no_effect_owner_imports() -> None:
    import poker_deliberation.phases.services as services

    source = inspect.getsource(services)
    forbidden = (
        "from pathlib import Path",
        "RunStore",
        "WorkflowStateMachine",
        "AgentProvider",
        "ToolRegistry",
        "poker_deliberation.approvals",
        "requires_human_approval",
        "datetime.now",
        "uuid4(",
        "secrets.",
    )
    assert not {token for token in forbidden if token in source}
