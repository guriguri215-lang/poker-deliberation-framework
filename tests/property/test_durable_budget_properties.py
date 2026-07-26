"""Property tests for durable budget invariants."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.budgets.durable_models import (
    DurableUsageV1,
    ResourceAmountsV1,
    canonical_durable_sha256,
)
from poker_deliberation.budgets.durable_store import (
    build_resource_reservation,
    reservation_request_sha256,
)
from poker_deliberation.budgets.execution import admit_automatic_retry
from poker_deliberation.budgets.retry import FailureCategory, IdempotencyStatus


@given(
    runtime=st.integers(min_value=0, max_value=1_000_000),
    run_bytes=st.integers(min_value=0, max_value=1_000_000),
    output_bytes=st.integers(min_value=0, max_value=1_000_000),
)
def test_reservation_builder_binds_exact_units(
    runtime: int,
    run_bytes: int,
    output_bytes: int,
) -> None:
    requested = ResourceAmountsV1(
        active_runtime_ns=runtime,
        tool_output_bytes=output_bytes,
        run_bytes=run_bytes,
        concurrency_slots=1,
    )
    reservation = build_resource_reservation(
        reservation_id="property-reservation",
        requested=requested,
    )

    assert reservation.request_sha256 == reservation_request_sha256(
        reservation_id=reservation.reservation_id,
        requested=requested,
    )


@given(
    first=st.integers(min_value=0, max_value=1_000_000),
    second=st.integers(min_value=0, max_value=1_000_000),
)
def test_usage_is_additive_for_cumulative_and_max_for_per_value_bytes(
    first: int,
    second: int,
) -> None:
    usage = DurableUsageV1().apply_actual(
        ResourceAmountsV1(
            run_bytes=first,
            tool_output_bytes=first,
            concurrency_slots=1,
        )
    )
    combined = usage.apply_actual(
        ResourceAmountsV1(
            run_bytes=second,
            tool_output_bytes=second,
            concurrency_slots=1,
        )
    )

    assert combined.run_bytes == first + second
    assert combined.tool_output_bytes == max(first, second)
    assert combined.peak_concurrency == 1


@given(retries=st.integers(min_value=0, max_value=10))
def test_durable_retry_limit_is_always_n_plus_one(retries: int) -> None:
    decision = admit_automatic_retry(
        category=FailureCategory.PROVIDER_TRANSIENT,
        idempotency=IdempotencyStatus.IDEMPOTENT,
        completed_retries=0,
        max_automatic_retries=retries,
    )

    assert decision.max_attempts == retries + 1


@given(value=st.integers(min_value=0, max_value=10**9))
def test_canonical_hash_is_independent_of_mapping_insertion_order(value: int) -> None:
    forward = {"a": value, "b": {"x": 1, "y": 2}}
    reverse = {"b": {"y": 2, "x": 1}, "a": value}

    assert canonical_durable_sha256(forward) == canonical_durable_sha256(reverse)
