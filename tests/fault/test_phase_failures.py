from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.phases import PhaseContractError
from poker_deliberation.phases.services import NormalizationService, RoutingService
from poker_deliberation.providers.base import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    AgentReport,
    CaseInput,
    Claim,
    EpistemicLabel,
)
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
)


class FailingNormalizationService(NormalizationService):
    def run(self, request):  # type: ignore[no-untyped-def]
        del request
        raise RuntimeError("forced pure compute failure")


def test_pure_phase_failure_keeps_only_the_preexecution_namespace_reservation(
    tmp_path: Path,
) -> None:
    run_id = "run-normalization-failure"
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        normalization_service=FailingNormalizationService(),
    )
    with pytest.raises(RuntimeError, match="forced pure compute failure"):
        orchestrator.run(CaseInput(kind="calculation", raw_text="review"), run_id=run_id)
    assert orchestrator.store.read_json(run_id, "input.json")["raw_text"] == "review"
    for logical_name in (
        "normalized_case.json",
        "assignments.json",
        "final_report.json",
    ):
        with pytest.raises(FileNotFoundError):
            orchestrator.store.read_json(run_id, logical_name)
    with pytest.raises(ProductRunError) as failure:
        orchestrator.product_store.read_current(run_id)
    assert failure.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND
    product_run = orchestrator.product_store.runs_root / run_id
    assert {item.name for item in product_run.iterdir()} == {".terminal-store"}
    assert not (product_run / ".terminal-store" / "current.json").exists()


class CorruptRoutingService(RoutingService):
    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        return outcome.model_copy(update={"attempt_id": "phase-forged"})


def test_malformed_routing_preserves_pre_routing_assignment_ledger(tmp_path: Path) -> None:
    run_id = "run-corrupt-routing"
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        routing_service=CorruptRoutingService(),
    )
    with pytest.raises(PhaseContractError, match="correlation mismatch"):
        orchestrator.run(
            CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
            run_id=run_id,
        )
    assignments = [
        AgentAssignment.model_validate(item)
        for item in orchestrator.store.read_json(run_id, "assignments.json")
    ]
    assert [assignment.agent_role for assignment in assignments] == [
        "strategy-analyst",
        "math-auditor",
        "skeptic",
        "adjudicator",
    ]
    assert all(not assignment.context_keys for assignment in assignments)
    assert not any(
        payload.inventory.logical_name.startswith("agent_reports/")
        for payload in orchestrator.store.verified_payloads(run_id)
    )
    with pytest.raises(ProductRunError) as failure:
        orchestrator.product_store.read_current(run_id)
    assert failure.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


class TimeoutProvider:
    def __init__(self) -> None:
        self.analyze_calls = 0

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            status=ProviderStatus.AVAILABLE,
            available=True,
            provider="timeout",
            reason="test",
        )

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, assignment, control
        self.analyze_calls += 1
        raise TimeoutError("forced provider timeout")


def test_provider_timeout_stops_remaining_analysis_and_tool_work(tmp_path: Path) -> None:
    provider = TimeoutProvider()
    report = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"), provider=provider).run(
        CaseInput(
            kind="strategy",
            raw_text="review",
            analysis_scope="retrospective",
            requested_tools=["pot_odds"],
            metadata={
                "tool_inputs": {
                    "pot_odds": {
                        "pot_before_bet": 100,
                        "opponent_bet": 50,
                        "call_cost": 50,
                    }
                }
            },
        ),
        run_id="run-timeout",
    )
    assert provider.analyze_calls == 1
    assert report.run_status == "failed_with_limitations"
    assert report.tool_results == []
    assert len(report.agent_execution_records) == 1
    assert report.agent_execution_records[0].status.value == "failed"


class ObjectionThenTimeoutProvider(TimeoutProvider):
    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, control
        self.analyze_calls += 1
        if self.analyze_calls == 1:
            return AgentReport(
                report_id="report-with-objection",
                agent_role=assignment.agent_role,
                task=assignment.task,
                objections=["first specialist objection"],
            )
        raise TimeoutError("forced provider timeout")


def test_early_timeout_preserves_objections_from_completed_reports(tmp_path: Path) -> None:
    provider = ObjectionThenTimeoutProvider()
    report = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"), provider=provider).run(
        CaseInput(
            kind="strategy",
            raw_text="review",
            analysis_scope="retrospective",
            claims=[
                Claim(
                    claim_id="claim-1",
                    text="decision claim",
                    label=EpistemicLabel.USER_CLAIM,
                )
            ],
        ),
        run_id="run-timeout-after-objection",
    )
    assert report.run_status == "failed_with_limitations"
    assert [dispute.issue for dispute in report.disputes] == ["first specialist objection"]


def test_final_write_fault_keeps_known_p2_010b_atomicity_limitation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "run-final-write-fault"
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))
    original = orchestrator.store.write_json

    def fail_final(run_id_value: str, relative_path: str, value: object) -> None:
        if relative_path == "final_report.json":
            raise OSError("forced final report write failure")
        original(run_id_value, relative_path, value)

    monkeypatch.setattr(orchestrator.store, "write_json", fail_final)
    with pytest.raises(OSError, match="forced final report write failure"):
        orchestrator.run(
            CaseInput(kind="calculation", raw_text="review", analysis_scope="retrospective"),
            run_id=run_id,
        )
    state = orchestrator.store.read_json(run_id, "state.json")
    assert state["state"] == "COMPLETED"
    with pytest.raises(FileNotFoundError):
        orchestrator.store.read_json(run_id, "final_report.json")
    with pytest.raises(ProductRunError) as failure:
        orchestrator.product_store.read_current(run_id)
    assert failure.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND
    product_run = orchestrator.product_store.runs_root / run_id
    assert {item.name for item in product_run.iterdir()} == {".terminal-store"}
    assert not (product_run / ".terminal-store" / "current.json").exists()
