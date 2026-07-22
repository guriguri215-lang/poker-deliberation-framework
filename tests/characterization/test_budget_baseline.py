from __future__ import annotations

from pathlib import Path

from poker_deliberation.config import AppConfig, BudgetConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.state_machine import WorkflowStateMachine


def test_legacy_budget_defaults_remain_a_public_input_baseline() -> None:
    budgets = BudgetConfig()

    assert budgets.model_dump() == {
        "max_deliberation_rounds": 2,
        "max_tool_retries": 2,
        "max_concurrent_agents": 5,
        "max_agent_depth": 1,
        "max_runtime_seconds": 300.0,
        "max_external_cost_usd": 0.0,
        "max_output_bytes": 1_000_000,
        "max_run_bytes": 10_000_000,
    }


def test_legacy_state_snapshot_shape_is_preserved() -> None:
    machine = WorkflowStateMachine(BudgetConfig())

    snapshot = machine.snapshot()

    assert set(snapshot) == {
        "state",
        "events",
        "deliberation_rounds",
        "tool_retries",
        "elapsed_seconds",
    }
    assert snapshot["state"] == "INTAKE"
    assert snapshot["deliberation_rounds"] == 0
    assert snapshot["tool_retries"] == {}


def test_local_calculation_succeeds_with_legacy_external_cost_cap_zero(
    tmp_path: Path,
) -> None:
    report = Orchestrator(
        AppConfig(
            runs_dir=tmp_path / "runs",
            budgets=BudgetConfig(max_external_cost_usd=0.0),
        )
    ).run(
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
        run_id="run-local-zero-cost",
    )

    assert report.run_status == "completed"
    assert report.tool_results[0].status.value == "success"

