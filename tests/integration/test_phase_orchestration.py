from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

import poker_deliberation.orchestrator as orchestrator_module
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.tools import default_registry


def _record_phase_calls(
    orchestrator: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> list[str]:
    calls: list[str] = []
    bindings = (
        ("intake", orchestrator.intake_service),
        ("normalization", orchestrator.normalization_service),
        ("routing", orchestrator.routing_service),
        ("context", orchestrator.context_build_service),
        ("analysis", orchestrator.analysis_executor),
        ("tool", orchestrator.tool_research_executor),
        ("critique", orchestrator.critique_service),
        ("adjudication", orchestrator.adjudication_service),
        ("synthesis", orchestrator.synthesis_service),
    )
    for name, target in bindings:
        original = target.run

        def wrapper(request: Any, *, _name: str = name, _run=original):  # type: ignore[no-untyped-def]
            calls.append(_name)
            return _run(request)

        monkeypatch.setattr(target, "run", wrapper)
    return calls


def test_actual_strategy_run_uses_every_applicable_phase_serially(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))
    calls = _record_phase_calls(orchestrator, monkeypatch)

    report = orchestrator.run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-phase",
    )

    assert report.run_status == "completed"
    assert calls == [
        "intake",
        "normalization",
        "routing",
        "context",
        "analysis",
        "critique",
        "context",
        "analysis",
        "critique",
        "context",
        "analysis",
        "critique",
        "context",
        "analysis",
        "critique",
        "tool",
        "critique",
        "adjudication",
        "synthesis",
    ]


def test_calculation_keeps_report_writer_assignment_unexecuted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))
    calls = _record_phase_calls(orchestrator, monkeypatch)
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
        run_id="run-calculation-phases",
    )

    assignments = json.loads(
        (tmp_path / "runs" / report.run_id / "assignments.json").read_text(encoding="utf-8")
    )
    assert [item["agent_role"] for item in assignments] == ["math-auditor", "report-writer"]
    assert all(item["context_keys"] == [] for item in assignments)
    assert "analysis" not in calls and "context" not in calls
    assert calls == [
        "intake",
        "normalization",
        "routing",
        "tool",
        "critique",
        "adjudication",
        "synthesis",
    ]


def test_tool_result_metadata_and_artifact_names_remain_compatible(tmp_path: Path) -> None:
    payload = {"pot_before_bet": 100, "opponent_bet": 50, "call_cost": 50}
    expected = default_registry().execute("pot_odds", payload)
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["pot_odds"],
            metadata={"tool_inputs": {"pot_odds": payload}},
        ),
        run_id="run-artifact-parity",
    )
    actual = report.tool_results[0]
    for field in (
        "tool_name",
        "input",
        "output",
        "status",
        "exactness",
        "numeric_exactness",
        "contract_version",
        "assumptions",
        "version",
        "model_qualifier",
        "method",
        "stochastic",
        "confidence_interval",
        "error_metadata",
        "stopping_condition",
        "verification",
        "warnings",
        "error",
        "reproduce_command",
    ):
        assert getattr(actual, field) == getattr(expected, field)

    run_dir = tmp_path / "runs" / report.run_id
    assert {path.name for path in run_dir.iterdir()} == {
        ".poker-deliberation-run",
        "agent_reports",
        "tool_results",
        "input.json",
        "evidence.jsonl",
        "normalized_case.json",
        "assumptions.json",
        "security_events.json",
        "assignments.json",
        "agent_execution_records.json",
        "state.json",
        "approvals.json",
        "disputes.json",
        "final_report.json",
        "final_report.md",
    }
    assert not any(path.name.startswith("phase") for path in run_dir.iterdir())


def test_orchestrator_public_methods_and_positional_constructor_remain_compatible() -> None:
    signature = inspect.signature(Orchestrator)
    assert list(signature.parameters)[:4] == [
        "config",
        "registry",
        "provider",
        "context_clock",
    ]
    for method in ("run", "resume", "load_report", "report_path"):
        assert callable(getattr(Orchestrator, method))


def test_sensitive_action_policy_uses_one_init_time_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))
    assert "external_service" in orchestrator.sensitive_action_categories
    monkeypatch.setattr(orchestrator_module, "SENSITIVE_ACTIONS", set())
    report = orchestrator.run(
        CaseInput(
            kind="strategy",
            raw_text="external solver",
            analysis_scope="retrospective",
            metadata={
                "approval_requests": [
                    {
                        "approval_id": "approval-snapshot",
                        "requested_action": "external solver",
                        "reason": "verify immutable policy snapshot",
                        "expected_benefit": "test",
                        "risks": ["external execution"],
                        "cost_or_resource_estimate": "unknown",
                        "alternatives": ["decline"],
                        "effect_of_declining": "no external action",
                    }
                ]
            },
        ),
        run_id="run-policy-snapshot",
    )
    assert report.run_status == "approval_required"
