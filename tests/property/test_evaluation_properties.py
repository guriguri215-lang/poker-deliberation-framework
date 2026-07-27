from __future__ import annotations

from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from poker_deliberation.evaluation.canonical import canonical_json_bytes
from poker_deliberation.evaluation.models import (
    EvaluationSummaryV1,
    ExpectedEvidenceV1,
    ratio_decimal,
)


@given(
    denominator=st.integers(min_value=1, max_value=10_000),
    numerator=st.integers(min_value=0, max_value=10_000),
)
def test_ratio_rendering_and_summary_are_deterministic(
    denominator: int,
    numerator: int,
) -> None:
    numerator = min(numerator, denominator)
    score = ratio_decimal(numerator, denominator)
    expected = "pass" if numerator == denominator else "fail"
    summary = EvaluationSummaryV1(
        declared_case_count=denominator,
        observed_case_count=denominator,
        matched_case_count=numerator,
        mismatched_case_count=denominator - numerator,
        numerator=numerator,
        denominator=denominator,
        score=score,
        threshold="1.0",
        decision=expected,
    )

    assert Fraction(summary.score) >= 0
    assert Fraction(summary.score) <= 1
    assert canonical_json_bytes(summary) == canonical_json_bytes(summary)


@given(tokens=st.lists(st.sampled_from(("a:x", "b:y", "c:z")), min_size=2, max_size=3))
def test_noncanonical_evidence_collections_fail_closed(tokens: list[str]) -> None:
    candidate = tuple(tokens)
    canonical = len(candidate) == len(set(candidate)) and candidate == tuple(sorted(candidate))
    if canonical:
        assert ExpectedEvidenceV1(tokens=candidate).tokens == candidate
    else:
        with pytest.raises(ValidationError):
            ExpectedEvidenceV1(tokens=candidate)


@given(
    left=st.integers(min_value=-(10**9), max_value=10**9),
    right=st.integers(min_value=-(10**9), max_value=10**9),
)
def test_canonical_dict_order_does_not_change_bytes(left: int, right: int) -> None:
    assert canonical_json_bytes({"a": left, "b": right}) == canonical_json_bytes(
        {"b": right, "a": left}
    )
