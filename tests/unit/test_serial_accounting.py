from __future__ import annotations

import pytest

from poker_deliberation.budgets import (
    BudgetFailureCode,
    BudgetLimitError,
    BudgetPolicyV2,
    ExecutionClass,
    FakeMonotonicClock,
    SerialUsageLedger,
    UsageDelta,
)


def test_fake_clock_accepts_exact_runtime_cap_and_rejects_over_cap() -> None:
    clock = FakeMonotonicClock()
    ledger = SerialUsageLedger(BudgetPolicyV2(max_runtime_seconds=1.0), clock=clock)

    clock.advance_ns(1_000_000_000)
    assert ledger.snapshot().active_runtime_ns == 1_000_000_000
    clock.advance_ns(1)
    with pytest.raises(BudgetLimitError) as error:
        ledger.snapshot()
    assert error.value.failure.code is BudgetFailureCode.RUNTIME_EXCEEDED


def test_fake_clock_rollback_is_rejected() -> None:
    clock = FakeMonotonicClock(current_ns=10)
    ledger = SerialUsageLedger(BudgetPolicyV2(), clock=clock)
    clock.set_ns(9)

    with pytest.raises(BudgetLimitError) as error:
        ledger.snapshot()
    assert error.value.failure.code is BudgetFailureCode.CLOCK_ROLLBACK


def test_paused_human_wait_is_not_charged() -> None:
    clock = FakeMonotonicClock()
    ledger = SerialUsageLedger(BudgetPolicyV2(max_runtime_seconds=2.0), clock=clock)
    clock.advance_ns(500_000_000)
    ledger.pause()
    clock.advance_ns(20_000_000_000)
    ledger.resume()
    clock.advance_ns(500_000_000)

    assert ledger.snapshot().active_runtime_ns == 1_000_000_000


def test_usage_combination_is_associative_and_keeps_units_separate() -> None:
    a = UsageDelta(provider_attempts=1, provider_output_bytes=10, peak_concurrency=1)
    b = UsageDelta(tool_attempts=1, tool_input_bytes=20)
    c = UsageDelta(tool_output_bytes=30, external_cost_micro_usd=40)

    assert a.combine(b).combine(c) == a.combine(b.combine(c))
    combined = a.combine(b).combine(c)
    assert combined.provider_output_bytes == 10
    assert combined.tool_input_bytes == 20
    assert combined.tool_output_bytes == 30
    assert combined.external_cost_micro_usd == 40


def test_local_free_execution_works_with_zero_external_cost_cap() -> None:
    ledger = SerialUsageLedger(BudgetPolicyV2(max_external_cost_micro_usd=0), active=False)

    snapshot = ledger.begin_provider_attempt(ExecutionClass.LOCAL_FREE, None)

    assert snapshot.provider_attempts == 1
    assert snapshot.external_cost_micro_usd == 0


def test_external_cost_unknown_zero_cap_and_over_cap_fail_before_attempt() -> None:
    zero = SerialUsageLedger(BudgetPolicyV2(max_external_cost_micro_usd=0), active=False)
    with pytest.raises(BudgetLimitError) as disabled:
        zero.begin_provider_attempt(ExecutionClass.EXTERNAL, 1)
    assert disabled.value.failure.code is BudgetFailureCode.EXTERNAL_COST_DISABLED
    assert zero.snapshot().provider_attempts == 0

    enabled = SerialUsageLedger(BudgetPolicyV2(max_external_cost_micro_usd=10), active=False)
    with pytest.raises(BudgetLimitError) as unknown:
        enabled.begin_provider_attempt(ExecutionClass.EXTERNAL, None)
    assert unknown.value.failure.code is BudgetFailureCode.EXTERNAL_COST_UNKNOWN
    with pytest.raises(BudgetLimitError) as exceeded:
        enabled.begin_provider_attempt(ExecutionClass.EXTERNAL, 11)
    assert exceeded.value.failure.code is BudgetFailureCode.EXTERNAL_COST_EXCEEDED
    assert enabled.snapshot().provider_attempts == 0


def test_peak_concurrency_above_serial_baseline_fails_closed() -> None:
    ledger = SerialUsageLedger(BudgetPolicyV2(), active=False)
    with pytest.raises(BudgetLimitError) as error:
        ledger.apply(UsageDelta(peak_concurrency=2))
    assert error.value.failure.code is BudgetFailureCode.UNSUPPORTED_CONCURRENCY
