"""Compatibility tests for additive durable budget behavior."""

from __future__ import annotations

import inspect

import poker_deliberation.budgets as budgets
import poker_deliberation.storage as storage
from poker_deliberation.budgets import (
    BudgetPolicyV2,
    FakeMonotonicClock,
    SerialUsageLedger,
)
from poker_deliberation.budgets.durable_models import (
    DurableBudgetPolicyV1,
    ExecutionActivationV1,
)
from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.orchestrator import Orchestrator


def test_p2_011a_budget_value_and_serial_accounting_remain_exact() -> None:
    policy = BudgetPolicyV2()
    assert policy.model_dump(mode="json") == {
        "schema_version": "2.0.0",
        "max_deliberation_rounds": 1,
        "max_tool_retries": 0,
        "max_concurrent_agents": 1,
        "max_runtime_seconds": 300.0,
        "max_external_cost_micro_usd": 0,
        "max_provider_output_bytes": 1_000_000,
        "max_tool_input_bytes": 1_000_000,
        "max_tool_output_bytes": 1_000_000,
        "max_artifact_bytes": 1_000_000,
        "max_run_bytes": 10_000_000,
    }
    ledger = SerialUsageLedger(policy, clock=FakeMonotonicClock())
    snapshot = ledger.snapshot()
    assert snapshot.peak_concurrency == 0
    assert snapshot.retry_attempts == 0
    assert ledger.policy == policy


def test_durable_activation_is_separate_internal_and_serial_by_default() -> None:
    durable = DurableBudgetPolicyV1()
    assert durable.base_policy == BudgetPolicyV2()
    assert durable.activation == ExecutionActivationV1(
        max_concurrent_agents=1,
        max_automatic_retries=0,
    )
    assert "DurableBudgetStore" not in budgets.__all__
    assert not hasattr(budgets, "DurableBudgetStore")
    assert not hasattr(storage, "read_structural_artifact_history")


def test_ordinary_orchestrator_signature_does_not_gain_durable_arguments() -> None:
    signature = inspect.signature(Orchestrator)
    assert "durable_budget_store" not in signature.parameters
    assert "durable_executor" not in signature.parameters
    assert "revision_root" not in signature.parameters


def test_capability_truth_remains_ordinary_product_scoped() -> None:
    states = {item.capability_id: item.state for item in CAPABILITIES}
    assert states["parallel_deliberation_and_tool_retry"] == "disabled"
    assert states["process_sandbox"] == "unavailable"
    assert states["immutable_revision_storage_foundation"] == "implemented"
    assert states["product_integrated_durable_run"] == "planned"
