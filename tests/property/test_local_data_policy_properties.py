from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    DEFAULT_LOCAL_DATA_POLICY,
    LifecycleDisposition,
    LifecycleSubject,
    RetentionAnchorKind,
    SubjectKind,
    SubjectState,
    canonical_local_data_sha256,
    classify_artifact,
    evaluate_local_data,
)

pytestmark = pytest.mark.property

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
CLASSIFICATIONS = tuple(ContextClassification)
CLASSIFICATION_RANK = {value: index for index, value in enumerate(CLASSIFICATIONS)}


@given(st.permutations(CLASSIFICATIONS))
def test_source_order_does_not_change_maximum_classification(
    values: list[ContextClassification],
) -> None:
    forward = classify_artifact("input.json", source_classifications=values)
    reverse = classify_artifact("input.json", source_classifications=reversed(values))

    assert forward == reverse
    assert forward.classification is ContextClassification.RESTRICTED


@given(
    st.lists(
        st.sampled_from(CLASSIFICATIONS),
        min_size=0,
        max_size=12,
    )
)
def test_classification_never_falls_below_internal_or_any_source(
    values: list[ContextClassification],
) -> None:
    classified = classify_artifact("final_report.md", source_classifications=values)
    floor = max(
        (ContextClassification.INTERNAL, *values),
        key=CLASSIFICATION_RANK.__getitem__,
    )

    assert CLASSIFICATION_RANK[classified.classification] >= CLASSIFICATION_RANK[floor]


@given(st.integers(min_value=-86_400, max_value=86_400))
def test_expiry_boundary_is_monotone_without_mutation(offset_seconds: int) -> None:
    anchor = NOW - timedelta(days=90)
    subject = LifecycleSubject(
        subject_kind=SubjectKind.RUN_PAYLOAD,
        subject_id="subject-property",
        logical_name="input.json",
        state=SubjectState.VERIFIED_TERMINAL,
        retention_anchor_kind=RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
        retention_started_at=anchor,
        owned_by_application=True,
        integrity_verified=True,
        lineage_verified=True,
        legal_hold=False,
    )

    result = evaluate_local_data(
        subject,
        clock=lambda: NOW + timedelta(seconds=offset_seconds),
    )

    assert result.audit is not None
    expected = (
        LifecycleDisposition.DELETE_CANDIDATE
        if offset_seconds >= 0
        else LifecycleDisposition.RETAIN
    )
    assert result.audit.proposed_disposition is expected


@given(
    st.dictionaries(
        st.from_regex(r"[a-z]{1,8}", fullmatch=True),
        st.integers(min_value=-1_000_000, max_value=1_000_000),
        max_size=12,
    )
)
def test_canonical_hash_is_stable_across_mapping_order(values: dict[str, int]) -> None:
    reverse_values = dict(reversed(list(values.items())))

    assert canonical_local_data_sha256(values) == canonical_local_data_sha256(reverse_values)


def test_policy_and_audit_json_round_trip_preserve_digest() -> None:
    policy_json = DEFAULT_LOCAL_DATA_POLICY.model_dump_json()
    restored_policy = type(DEFAULT_LOCAL_DATA_POLICY).model_validate_json(policy_json)
    subject = LifecycleSubject(
        subject_kind=SubjectKind.RUN_REPORT,
        subject_id="subject-roundtrip",
        logical_name="final_report.json",
        state=SubjectState.VERIFIED_TERMINAL,
        retention_anchor_kind=RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
        retention_started_at=NOW,
        owned_by_application=True,
        integrity_verified=True,
        lineage_verified=True,
        legal_hold=False,
    )
    result = evaluate_local_data(subject, clock=lambda: NOW)

    assert restored_policy.canonical_sha256 == DEFAULT_LOCAL_DATA_POLICY.canonical_sha256
    assert result.audit is not None
    audit_type = type(result.audit)
    restored_audit = audit_type.model_validate_json(result.audit.model_dump_json())
    assert restored_audit.canonical_sha256 == result.audit.canonical_sha256
