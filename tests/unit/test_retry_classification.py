from __future__ import annotations

import pytest

from poker_deliberation.budgets import (
    FailureCategory,
    IdempotencyStatus,
    RetryDisposition,
    classify_retry,
)


@pytest.mark.parametrize(
    "category",
    [
        FailureCategory.VALIDATION,
        FailureCategory.UNSUPPORTED,
        FailureCategory.UNAVAILABLE,
        FailureCategory.BUDGET,
        FailureCategory.DEADLINE,
        FailureCategory.CANCEL,
        FailureCategory.POLICY,
        FailureCategory.TOOL_DETERMINISTIC,
        FailureCategory.VERIFICATION,
        FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
    ],
)
def test_declared_non_retryable_categories_are_never_retryable(
    category: FailureCategory,
) -> None:
    result = classify_retry(
        category,
        idempotency=IdempotencyStatus.IDEMPOTENT,
        max_retries=3,
    )

    assert result.disposition is RetryDisposition.NON_RETRYABLE
    assert not result.retryable
    assert not result.automatic_retry
    assert result.max_attempt_candidates == 4


def test_transient_failure_requires_idempotency_or_reconciliation() -> None:
    unknown = classify_retry(
        FailureCategory.PROVIDER_TRANSIENT,
        idempotency=IdempotencyStatus.UNKNOWN,
    )
    safe = classify_retry(
        FailureCategory.PROVIDER_TRANSIENT,
        idempotency=IdempotencyStatus.RECONCILABLE,
        max_retries=2,
    )

    assert not unknown.retryable
    assert safe.retryable
    assert safe.max_attempt_candidates == 3
    assert not safe.automatic_retry


def test_retry_count_is_strict_and_non_negative() -> None:
    with pytest.raises(TypeError):
        classify_retry(FailureCategory.PROVIDER_TRANSIENT, max_retries=True)
    with pytest.raises(ValueError):
        classify_retry(FailureCategory.PROVIDER_TRANSIENT, max_retries=-1)
