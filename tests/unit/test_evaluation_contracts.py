from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from poker_deliberation.evaluation.canonical import (
    CASE_INPUT_DOMAIN,
    DATASET_CONTENT_DOMAIN,
    CanonicalEvaluationError,
    canonical_domain_sha256,
    canonical_json_bytes,
    parse_canonical_model,
)
from poker_deliberation.evaluation.models import (
    EvaluationCaseInputV1,
    EvaluationDatasetV1,
    EvaluationSummaryV1,
    ExpectedEvidenceV1,
)
from poker_deliberation.evaluation.runner import load_evaluation_suite

ROOT = Path(__file__).resolve().parents[2]
SUITE = "evals/suites/p3_017a_v1.json"


def test_repository_owned_fixture_has_strict_license_hashes_and_counts() -> None:
    loaded = load_evaluation_suite(ROOT, SUITE)

    assert loaded.manifest.ownership == "repository-owned"
    assert loaded.manifest.license_spdx == "MIT"
    assert loaded.manifest.case_count == len(loaded.dataset.cases) == 10
    assert loaded.manifest.content_sha256 == canonical_domain_sha256(
        DATASET_CONTENT_DOMAIN,
        loaded.dataset,
    )
    assert loaded.scorer.aggregation == "micro-mean"
    assert loaded.scorer.denominator_policy == "all-declared-cases"
    assert loaded.scorer.invalid_or_missing_count_policy == "fail-closed"
    assert loaded.scorer.threshold == "1.0"
    assert loaded.scorer.human_review_rubric is None
    assert all(
        case.input_sha256 == canonical_domain_sha256(CASE_INPUT_DOMAIN, case.input)
        for case in loaded.dataset.cases
    )


def test_canonical_dataset_round_trip_is_byte_exact_and_versioned() -> None:
    loaded = load_evaluation_suite(ROOT, SUITE)
    encoded = canonical_json_bytes(loaded.dataset)

    assert parse_canonical_model(encoded, EvaluationDatasetV1) == loaded.dataset
    assert canonical_json_bytes(parse_canonical_model(encoded, EvaluationDatasetV1)) == encoded
    with pytest.raises(CanonicalEvaluationError):
        parse_canonical_model(encoded + b"\n", EvaluationDatasetV1)
    with pytest.raises(CanonicalEvaluationError):
        parse_canonical_model(b"\xef\xbb\xbf" + encoded, EvaluationDatasetV1)


def test_canonical_dataset_rejects_unknown_fields_and_duplicate_keys() -> None:
    loaded = load_evaluation_suite(ROOT, SUITE)
    raw = loaded.dataset.model_dump(mode="python")
    raw["unexpected"] = True
    with pytest.raises(ValidationError, match="unexpected"):
        EvaluationDatasetV1.model_validate(raw, strict=True)

    duplicate = (
        b'{"canonicalization":"poker-offline-evaluation-json-v1",'
        b'"canonicalization":"poker-offline-evaluation-json-v1"}'
    )
    with pytest.raises(CanonicalEvaluationError, match="duplicate"):
        parse_canonical_model(duplicate, EvaluationDatasetV1)


def test_evidence_tokens_are_unique_and_utf8_sorted() -> None:
    assert ExpectedEvidenceV1(tokens=("a:first", "b:second")).tokens == (
        "a:first",
        "b:second",
    )
    with pytest.raises(ValidationError, match="UTF-8 sorted"):
        ExpectedEvidenceV1(tokens=("b:second", "a:first"))
    with pytest.raises(ValidationError, match="unique"):
        ExpectedEvidenceV1(tokens=("a:first", "a:first"))


def test_case_scenario_and_mutation_are_exactly_bound() -> None:
    loaded = load_evaluation_suite(ROOT, SUITE)

    for case in loaded.dataset.cases:
        mismatched_mutation = "change-oracle" if case.input.mutation == "none" else "none"
        raw = case.input.model_dump(mode="python")
        raw["mutation"] = mismatched_mutation
        with pytest.raises(ValidationError, match="scenario/mutation mismatch"):
            EvaluationCaseInputV1.model_validate(raw, strict=True)


def test_exact_summary_threshold_boundary_and_count_failures() -> None:
    passed = EvaluationSummaryV1(
        declared_case_count=10,
        observed_case_count=10,
        matched_case_count=10,
        mismatched_case_count=0,
        numerator=10,
        denominator=10,
        score="1.0",
        threshold="1.0",
        decision="pass",
    )
    failed = EvaluationSummaryV1(
        declared_case_count=10,
        observed_case_count=10,
        matched_case_count=9,
        mismatched_case_count=1,
        numerator=9,
        denominator=10,
        score="0.9",
        threshold="1.0",
        decision="fail",
    )

    assert passed.decision == "pass"
    assert failed.decision == "fail"
    invalid = deepcopy(failed.model_dump(mode="python"))
    invalid["denominator"] = 9
    with pytest.raises(ValidationError, match="all declared cases"):
        EvaluationSummaryV1.model_validate(invalid, strict=True)
    missing = failed.model_dump(mode="python")
    missing.pop("denominator")
    with pytest.raises(ValidationError, match="denominator"):
        EvaluationSummaryV1.model_validate(missing, strict=True)
