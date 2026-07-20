from __future__ import annotations

import json
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
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport, CaseInput


class FailingNormalizationService(NormalizationService):
    def run(self, request):  # type: ignore[no-untyped-def]
        del request
        raise RuntimeError("forced pure compute failure")


def test_pure_phase_failure_does_not_write_its_or_later_artifacts(tmp_path: Path) -> None:
    run_id = "run-normalization-failure"
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        normalization_service=FailingNormalizationService(),
    )
    with pytest.raises(RuntimeError, match="forced pure compute failure"):
        orchestrator.run(CaseInput(kind="calculation", raw_text="review"), run_id=run_id)
    run_dir = tmp_path / "runs" / run_id
    assert (run_dir / "input.json").is_file()
    assert not (run_dir / "normalized_case.json").exists()
    assert not (run_dir / "assignments.json").exists()
    assert not (run_dir / "final_report.json").exists()


class CorruptRoutingService(RoutingService):
    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        return outcome.model_copy(update={"attempt_id": "phase-forged"})


def test_malformed_phase_outcome_fails_before_assignment_materialization(tmp_path: Path) -> None:
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
    run_dir = tmp_path / "runs" / run_id
    assert not (run_dir / "assignments.json").exists()
    assert not list((run_dir / "agent_reports").glob("*.json"))


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
    state = json.loads((tmp_path / "runs" / run_id / "state.json").read_text(encoding="utf-8"))
    assert state["state"] == "COMPLETED"
    assert not (tmp_path / "runs" / run_id / "final_report.json").exists()
