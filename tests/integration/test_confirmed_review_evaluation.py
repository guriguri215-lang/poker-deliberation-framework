from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from poker_deliberation.confirmed_review_evaluation import (
    EVALUATION_FAMILY_ID,
    REQUIRED_CASE_IDS,
    ConfirmedReviewEvaluationFixtureV1,
    load_confirmed_review_evaluation_fixture,
    run_confirmed_review_evaluation,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "confirmed_review" / "v1" / "scenarios.json"


def test_confirmed_review_evaluation_scores_all_declared_cases_exactly(tmp_path) -> None:
    fixture = load_confirmed_review_evaluation_fixture(FIXTURE)
    result = run_confirmed_review_evaluation(
        fixture,
        work_root=tmp_path,
    )
    assert fixture.family_id == EVALUATION_FAMILY_ID
    assert tuple(item.case_id for item in result.case_results) == REQUIRED_CASE_IDS
    assert all(item.score == "1.0" and item.passed for item in result.case_results)
    assert result.overall_score == "1.0"
    assert result.passed is True
    assert len(result.result_sha256) == 64


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "unexpected"])
def test_evaluation_case_inventory_fails_closed(mutation: str) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if mutation == "missing":
        value["cases"].pop()
    elif mutation == "duplicate":
        value["cases"][-1] = value["cases"][0]
    else:
        value["cases"][-1]["case_id"] = "undeclared-case"
    with pytest.raises(ValidationError):
        ConfirmedReviewEvaluationFixtureV1.model_validate_json(json.dumps(value))


def test_unexpected_or_missing_evidence_scores_zero(tmp_path) -> None:
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["cases"][0]["expected_evidence"] = ["undeclared-evidence"]
    fixture = ConfirmedReviewEvaluationFixtureV1.model_validate_json(json.dumps(value))
    result = run_confirmed_review_evaluation(fixture, work_root=tmp_path)
    assert result.case_results[0].score == "0.0"
    assert result.overall_score == "0.0"
    assert result.passed is False
