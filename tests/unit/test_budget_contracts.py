from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from poker_deliberation.budgets import (
    BudgetFailureCode,
    BudgetLimitError,
    BudgetPolicyV2,
    CancellationStatus,
    DeadlineStatus,
    FakeMonotonicClock,
    canonical_budget_sha256,
    decimal_usd_to_micro_usd,
)
from poker_deliberation.config import BudgetConfig, migrate_budget_config
from poker_deliberation.providers import ProviderControl, ProviderControlError


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_deliberation_rounds", True),
        ("max_tool_retries", "0"),
        ("max_concurrent_agents", 2),
        ("max_runtime_seconds", "1"),
        ("max_runtime_seconds", float("nan")),
        ("max_runtime_seconds", float("inf")),
        ("max_runtime_seconds", -1.0),
        ("max_external_cost_micro_usd", True),
        ("max_provider_output_bytes", "1024"),
        ("max_tool_input_bytes", -1),
    ],
)
def test_v2_policy_rejects_coercion_nonfinite_and_invalid_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises((ValidationError, TypeError)):
        BudgetPolicyV2.model_validate({**BudgetPolicyV2().model_dump(), field: value})


def test_v2_policy_rejects_unknown_fields_and_caller_mutation() -> None:
    with pytest.raises(ValidationError):
        BudgetPolicyV2.model_validate({**BudgetPolicyV2().model_dump(), "unknown": 1})

    policy = BudgetPolicyV2()
    with pytest.raises(ValidationError):
        policy.max_tool_retries = 1  # type: ignore[misc]


def test_v1_migration_preserves_effective_serial_baseline_and_splits_bytes() -> None:
    migration = migrate_budget_config(
        BudgetConfig(
            max_deliberation_rounds=2,
            max_tool_retries=2,
            max_concurrent_agents=5,
            max_agent_depth=1,
            max_external_cost_usd=1.234567,
            max_output_bytes=4096,
            max_run_bytes=16384,
        )
    )

    assert migration.policy.max_deliberation_rounds == 1
    assert migration.policy.max_tool_retries == 0
    assert migration.policy.max_concurrent_agents == 1
    assert migration.policy.max_external_cost_micro_usd == 1_234_567
    assert {
        migration.policy.max_provider_output_bytes,
        migration.policy.max_tool_input_bytes,
        migration.policy.max_tool_output_bytes,
        migration.policy.max_artifact_bytes,
    } == {4096}
    assert "max_agent_depth" in migration.ignored_legacy_fields


def test_v1_migration_rejects_unsupported_active_claims() -> None:
    with pytest.raises(BudgetLimitError) as depth:
        migrate_budget_config(BudgetConfig(max_agent_depth=2))
    assert depth.value.failure.code is BudgetFailureCode.UNSUPPORTED_LEGACY_FIELD

    with pytest.raises(BudgetLimitError) as concurrency:
        migrate_budget_config(BudgetConfig(max_concurrent_agents=2))
    assert concurrency.value.failure.code is BudgetFailureCode.UNSUPPORTED_CONCURRENCY


def test_decimal_cost_requires_exact_integer_micro_usd() -> None:
    assert decimal_usd_to_micro_usd(Decimal("0")) == 0
    assert decimal_usd_to_micro_usd(Decimal("1.000001")) == 1_000_001
    with pytest.raises(ValueError, match="micro-USD"):
        decimal_usd_to_micro_usd(Decimal("0.0000001"))
    with pytest.raises(TypeError):
        decimal_usd_to_micro_usd(1.0)  # type: ignore[arg-type]


def test_canonical_hash_is_deterministic() -> None:
    first = BudgetPolicyV2(max_runtime_seconds=12.5)
    second = BudgetPolicyV2.model_validate(dict(reversed(list(first.model_dump().items()))))

    assert first.canonical_sha256 == second.canonical_sha256
    assert canonical_budget_sha256(first) == canonical_budget_sha256(second)


def test_provider_control_distinguishes_request_ack_unconfirmed_and_deadline() -> None:
    clock = FakeMonotonicClock()
    requested = ProviderControl(timeout_seconds=1.0, clock=clock)
    requested.request_cancel()
    assert requested.cancellation_status is CancellationStatus.CANCEL_REQUESTED
    requested.mark_cancel_unconfirmed()
    assert requested.cancellation_status is CancellationStatus.CANCEL_UNCONFIRMED
    requested.acknowledge_cancel()
    assert requested.cancellation_status is CancellationStatus.CANCELLED

    deadline = ProviderControl(timeout_seconds=1.0, clock=clock)
    clock.advance_ns(1_000_000_000)
    assert deadline.deadline_status is DeadlineStatus.TIMED_OUT
    with pytest.raises(ProviderControlError) as error:
        deadline.raise_if_cancelled()
    assert error.value.deadline_status is DeadlineStatus.TIMED_OUT
    assert error.value.cancellation_status is CancellationStatus.CANCELLED


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), -1.0, 0.0])
def test_provider_control_rejects_nonfinite_or_nonpositive_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="finite and positive"):
        ProviderControl(timeout_seconds=timeout)


def test_provider_control_rejects_monotonic_clock_rollback() -> None:
    clock = FakeMonotonicClock(current_ns=10)
    control = ProviderControl(timeout_seconds=1.0, clock=clock)
    clock.set_ns(9)

    with pytest.raises(ValueError, match="moved backwards"):
        _ = control.deadline_status
