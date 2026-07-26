from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.budgets import (
    BudgetFailureCode,
    BudgetLimitError,
    BudgetPolicyV2,
    ExecutionClass,
    FailureCategory,
    FakeMonotonicClock,
    IdempotencyStatus,
    SerialUsageLedger,
    UsageDelta,
    canonical_budget_sha256,
    classify_retry,
)
from poker_deliberation.config import BudgetConfig, migrate_budget_config

usage_values = st.builds(
    UsageDelta,
    provider_attempts=st.integers(min_value=0, max_value=10),
    tool_attempts=st.integers(min_value=0, max_value=10),
    retry_attempts=st.integers(min_value=0, max_value=10),
    active_runtime_ns=st.integers(min_value=0, max_value=10_000),
    external_cost_micro_usd=st.integers(min_value=0, max_value=10_000),
    provider_output_bytes=st.integers(min_value=0, max_value=10_000),
    tool_input_bytes=st.integers(min_value=0, max_value=10_000),
    tool_output_bytes=st.integers(min_value=0, max_value=10_000),
    artifact_bytes=st.integers(min_value=0, max_value=10_000),
    run_bytes=st.integers(min_value=0, max_value=10_000),
    peak_concurrency=st.integers(min_value=0, max_value=1),
)


@given(a=usage_values, b=usage_values, c=usage_values)
def test_usage_addition_is_associative(a: UsageDelta, b: UsageDelta, c: UsageDelta) -> None:
    assert a.combine(b).combine(c) == a.combine(b.combine(c))


@given(seconds=st.integers(min_value=1, max_value=10_000))
def test_fake_clock_forward_progress_is_exact(seconds: int) -> None:
    clock = FakeMonotonicClock()
    policy = BudgetPolicyV2(max_runtime_seconds=float(seconds + 1))
    ledger = SerialUsageLedger(policy, clock=clock)
    clock.advance_ns(seconds * 1_000_000_000)

    assert ledger.snapshot().active_runtime_ns == seconds * 1_000_000_000


@given(retries=st.integers(min_value=0, max_value=10))
def test_retry_candidate_count_is_retries_plus_one(retries: int) -> None:
    result = classify_retry(
        FailureCategory.PROVIDER_TRANSIENT,
        idempotency=IdempotencyStatus.IDEMPOTENT,
        max_retries=retries,
    )

    assert result.max_attempt_candidates == retries + 1
    assert not result.automatic_retry


@given(runtime=st.integers(min_value=1, max_value=10_000))
def test_policy_round_trip_and_dict_order_have_stable_hash(runtime: int) -> None:
    policy = BudgetPolicyV2(max_runtime_seconds=float(runtime))
    reversed_payload = dict(reversed(list(policy.model_dump().items())))
    round_trip = BudgetPolicyV2.model_validate(reversed_payload)

    assert round_trip == policy
    assert canonical_budget_sha256(round_trip) == canonical_budget_sha256(policy)


def test_peak_concurrency_baseline_is_one() -> None:
    assert BudgetPolicyV2().max_concurrent_agents == 1


@given(cap=st.integers(min_value=1, max_value=100_000))
def test_external_cost_cap_accepts_equality_and_rejects_over_cap(cap: int) -> None:
    policy = BudgetPolicyV2(max_external_cost_micro_usd=cap)
    exact = SerialUsageLedger(policy, active=False)
    over = SerialUsageLedger(policy, active=False)

    assert exact.begin_provider_attempt(ExecutionClass.EXTERNAL, cap).external_cost_micro_usd == cap
    try:
        over.begin_provider_attempt(ExecutionClass.EXTERNAL, cap + 1)
    except BudgetLimitError as exc:
        assert exc.failure.code is BudgetFailureCode.EXTERNAL_COST_EXCEEDED
    else:  # pragma: no cover - Hypothesis should never reach this branch
        raise AssertionError("over-cap cost was accepted")


@given(
    runtime=st.integers(min_value=1, max_value=10_000),
    external_dollars=st.integers(min_value=0, max_value=100),
    output_bytes=st.integers(min_value=1024, max_value=10_240),
)
def test_v1_migration_is_deterministic(
    runtime: int,
    external_dollars: int,
    output_bytes: int,
) -> None:
    config = BudgetConfig(
        max_runtime_seconds=float(runtime),
        max_external_cost_usd=float(external_dollars),
        max_output_bytes=output_bytes,
        max_run_bytes=10_240,
    )

    assert migrate_budget_config(config) == migrate_budget_config(config)
    assert (
        migrate_budget_config(config).canonical_sha256
        == migrate_budget_config(config).canonical_sha256
    )
