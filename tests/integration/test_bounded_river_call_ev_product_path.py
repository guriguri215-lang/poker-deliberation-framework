from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path

from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.storage.terminal_models import RunReadStatus
from tests.bounded_river_call_ev_support import admission, app_config


def test_product_path_persists_and_replays_exact_call_positive_result(tmp_path: Path) -> None:
    admitted = admission(run_id="run-river-product-call")
    orchestrator = Orchestrator(config=app_config(tmp_path), provider=LocalProvider())

    report = orchestrator.run_bounded_river_call_ev_review(admitted)

    assert report.run_status == "completed"
    assert [result.tool_name for result in report.tool_results] == [
        "hand_validator",
        "hand_pot_ledger",
        "pot_odds",
        "range_validate",
        "combos",
        "holdem_equity",
        "raked_call_ev",
    ]
    result = json.loads(
        orchestrator.product_store.read_current(report.run_id)
        .payload_bytes("bounded_river_call_ev_result.json")
        .decode("utf-8")
    )
    assert Fraction(
        result["required_equity"]["numerator"],
        result["required_equity"]["denominator"],
    ) == Fraction(5, 24)
    assert result["action_comparison"] == "call"

    replay = orchestrator.run_bounded_river_call_ev_review(admitted)
    assert replay == report


def test_tool_input_budget_refusal_publishes_exact_attempted_prefix(tmp_path: Path) -> None:
    admitted = admission(run_id="run-river-product-input-budget")
    orchestrator = Orchestrator(
        config=app_config(tmp_path),
        provider=LocalProvider(),
        budget_policy=BudgetPolicyV2(max_tool_input_bytes=2_600),
    )

    report = orchestrator.run_bounded_river_call_ev_review(admitted)

    assert report.run_status == "failed_with_limitations"
    assert [(item.tool_name, item.status.value) for item in report.tool_results] == [
        ("hand_validator", "success"),
        ("hand_pot_ledger", "failed"),
    ]
    assert (
        orchestrator.product_store.read_current(report.run_id).read_status is RunReadStatus.FAILED
    )
    assert orchestrator.run_bounded_river_call_ev_review(admitted) == report
