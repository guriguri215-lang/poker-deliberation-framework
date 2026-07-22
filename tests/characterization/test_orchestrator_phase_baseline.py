from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.agents import ROLE_CATALOG
from poker_deliberation.budgets import ExecutionClass
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers.base import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport, CaseInput


class CountingProvider:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.availability_calls = 0
        self.analyze_roles: list[str] = []

    def availability(self) -> ProviderAvailability:
        self.availability_calls += 1
        return ProviderAvailability(
            status=ProviderStatus.AVAILABLE if self.available else ProviderStatus.UNAVAILABLE,
            available=self.available,
            provider="counting",
            reason="available" if self.available else "unavailable",
            version="1.0.0",
            execution_class=ExecutionClass.LOCAL_FREE,
        )

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context
        control.raise_if_cancelled()
        self.analyze_roles.append(assignment.agent_role)
        return AgentReport(
            report_id=f"report-{assignment.agent_role}",
            agent_role=assignment.agent_role,
            task=assignment.task,
        )


@pytest.mark.parametrize(
    ("kind", "roles"),
    [
        ("hand", ["intake", "strategy-analyst", "math-auditor", "skeptic", "adjudicator"]),
        ("strategy", ["strategy-analyst", "math-auditor", "skeptic", "adjudicator"]),
        ("claim", ["math-auditor", "evidence-researcher", "skeptic", "adjudicator"]),
        ("calculation", []),
    ],
)
def test_role_order_provider_calls_and_availability_count_are_characterized(
    tmp_path: Path,
    kind: str,
    roles: list[str],
) -> None:
    provider = CountingProvider()
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / kind),
        provider=provider,
    ).run(
        CaseInput(kind=kind, raw_text="review", analysis_scope="retrospective"),
        run_id=f"run-{kind}",
    )

    assert provider.analyze_roles == roles
    assert provider.availability_calls == len(roles) + 1
    assert [record.agent_role for record in report.agent_execution_records] == roles


def test_unavailable_provider_checks_each_assignment_and_synthesis_once(
    tmp_path: Path,
) -> None:
    provider = CountingProvider(available=False)
    report = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"), provider=provider).run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-unavailable",
    )

    assert provider.analyze_roles == []
    assert provider.availability_calls == 5
    assert [record.agent_role for record in report.agent_execution_records] == [
        "strategy-analyst",
        "math-auditor",
        "skeptic",
        "adjudicator",
    ]
    assert all(record.status.value == "refused" for record in report.agent_execution_records)


def test_calculation_write_order_and_unexecuted_report_writer_are_characterized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))
    writes: list[str] = []
    original_json = orchestrator.store.write_json
    original_text = orchestrator.store.write_text

    def write_json(run_id: str, relative_path: str, value: object) -> None:
        writes.append(relative_path)
        original_json(run_id, relative_path, value)

    def write_text(run_id: str, relative_path: str, value: str) -> None:
        writes.append(relative_path)
        original_text(run_id, relative_path, value)

    monkeypatch.setattr(orchestrator.store, "write_json", write_json)
    monkeypatch.setattr(orchestrator.store, "write_text", write_text)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
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
        run_id="run-write-order",
    )
    result_id = report.tool_results[0].result_id
    assert writes == [
        "input.json",
        "evidence.jsonl",
        "normalized_case.json",
        "assumptions.json",
        "security_events.json",
        "assignments.json",
        f"tool_results/{result_id}.json",
        f"tool_results/{result_id}.input.json",
        "agent_execution_records.json",
        "security_events.json",
        "state.json",
        "approvals.json",
        "disputes.json",
        "final_report.json",
        "final_report.md",
    ]
    assignments = orchestrator.store.read_json(report.run_id, "assignments.json")
    assert assignments == [
        {
            "assignment_id": assignments[0]["assignment_id"],
            "agent_role": "math-auditor",
            "task": ROLE_CATALOG["math-auditor"].purpose,
            "context_keys": [],
            "read_only": True,
        },
        {
            "assignment_id": assignments[1]["assignment_id"],
            "agent_role": "report-writer",
            "task": ROLE_CATALOG["report-writer"].purpose,
            "context_keys": [],
            "read_only": True,
        },
    ]
    assert report.agent_execution_records == []
