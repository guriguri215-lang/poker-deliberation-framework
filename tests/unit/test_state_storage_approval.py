from pathlib import Path

import pytest

from poker_deliberation.approvals import ApprovalLedger, requires_human_approval
from poker_deliberation.config import BudgetConfig
from poker_deliberation.schemas import ApprovalRequest, ApprovalStatus, CaseInput
from poker_deliberation.state_machine import RunState, WorkflowStateMachine
from poker_deliberation.storage import RunStore


def _approval() -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="approval-test",
        requested_action="install package",
        reason="test",
        expected_benefit="test tool",
        risks=["supply chain"],
        cost_or_resource_estimate="small",
        alternatives=["stdlib"],
        effect_of_declining="reduced coverage",
    )


def test_state_machine_rejects_illegal_transition() -> None:
    machine = WorkflowStateMachine(BudgetConfig())
    with pytest.raises(ValueError):
        machine.transition(RunState.COMPLETED, "skip")


def test_state_machine_bounded_rounds_and_retries() -> None:
    machine = WorkflowStateMachine(BudgetConfig(max_deliberation_rounds=1, max_tool_retries=1))
    assert machine.start_deliberation_round()
    assert not machine.start_deliberation_round()
    assert machine.allow_tool_retry("x")
    assert not machine.allow_tool_retry("x")


def test_approval_is_decided_once() -> None:
    ledger = ApprovalLedger([_approval()])
    decided = ledger.decide("approval-test", False, "declined")
    assert decided.status is ApprovalStatus.REJECTED
    with pytest.raises(ValueError):
        ledger.decide("approval-test", True, "too late")
    assert requires_human_approval("package_install")


def test_run_store_confines_paths_and_serializes_models(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    store.write_json("run-ok", "input.json", CaseInput(kind="calculation", requested_tools=["x"]))
    assert store.read_json("run-ok", "input.json")["kind"] == "calculation"
    with pytest.raises(ValueError):
        store.write_json("../escape", "x.json", {})
    with pytest.raises(ValueError):
        store.write_json("run-ok", "../escape.json", {})


def test_run_store_enforces_per_artifact_and_whole_run_byte_budgets(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs", max_artifact_bytes=80, max_run_bytes=120)
    store.create_run("bounded")
    store.write_text("bounded", "small.txt", "x" * 50)
    with pytest.raises(ValueError, match="artifact exceeds"):
        store.write_text("bounded", "large.txt", "x" * 81)
    with pytest.raises(ValueError, match="run artifacts exceed"):
        store.write_text("bounded", "second.txt", "x" * 80)
