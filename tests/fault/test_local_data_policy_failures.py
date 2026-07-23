from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from poker_deliberation.local_data_policy import (
    LifecyclePolicyFailureCode,
    LifecycleSubject,
    RetentionAnchorKind,
    SubjectKind,
    SubjectState,
    evaluate_local_data,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _subject(*, started_at: datetime | None = None) -> LifecycleSubject:
    return LifecycleSubject(
        subject_kind=SubjectKind.RUN_REPORT,
        subject_id="fault-subject",
        logical_name="final_report.json",
        state=SubjectState.VERIFIED_TERMINAL,
        retention_anchor_kind=RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
        retention_started_at=started_at or NOW,
        owned_by_application=True,
        integrity_verified=True,
        lineage_verified=True,
        legal_hold=False,
    )


@pytest.mark.parametrize(
    "clock",
    [
        lambda: datetime(2026, 7, 23, 12, 0),
        lambda: "not-a-datetime",
        lambda: (_ for _ in ()).throw(RuntimeError("clock unavailable")),
    ],
)
def test_clock_faults_return_typed_non_retryable_failure(clock) -> None:
    result = evaluate_local_data(_subject(), clock=clock)

    assert result.audit is None
    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.INVALID_UTC
    assert result.failure.retryable is False
    assert result.failure.filesystem_mutation is False
    assert result.failure.domain_mutation is False


def test_clock_rollback_returns_no_action() -> None:
    result = evaluate_local_data(
        _subject(started_at=NOW),
        clock=lambda: NOW - timedelta(microseconds=1),
    )

    assert result.audit is None
    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.CLOCK_ROLLBACK
    assert result.failure.manual_review_required is True


def test_retention_overflow_returns_no_action() -> None:
    latest = datetime.max.replace(tzinfo=UTC)
    result = evaluate_local_data(
        _subject(started_at=latest),
        clock=lambda: latest,
    )

    assert result.audit is None
    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.INVALID_RETENTION_TIME


@pytest.mark.parametrize("expected_hash", ["bad", "0" * 64])
def test_policy_hash_faults_return_no_action(expected_hash: str) -> None:
    result = evaluate_local_data(
        _subject(),
        clock=lambda: NOW,
        expected_policy_sha256=expected_hash,
    )

    assert result.audit is None
    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.POLICY_HASH_MISMATCH
    assert result.failure.filesystem_mutation is False
    assert result.failure.domain_mutation is False


def test_invalid_quarantine_reason_and_approval_reference_are_bounded_failures() -> None:
    invalid_reason = evaluate_local_data(
        _subject(),
        clock=lambda: NOW,
        quarantine_reasons=("invented",),
    )
    invalid_reference = evaluate_local_data(
        _subject(),
        clock=lambda: NOW,
        approval_reference="../outside",
    )

    assert invalid_reason.failure is not None
    assert invalid_reason.failure.code is LifecyclePolicyFailureCode.INVALID_POLICY
    assert invalid_reference.failure is not None
    assert invalid_reference.failure.code is LifecyclePolicyFailureCode.INVALID_POLICY
