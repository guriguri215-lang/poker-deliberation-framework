from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from poker_deliberation.range_equity_evaluation import (
    EVALUATION_FAMILY_ID,
    REQUIRED_CASE_IDS,
    REQUIRED_METRICS,
    RangeEquityEvaluationFixtureV1,
    RangeEquityEvaluationResultV1,
    _evaluation_work_root,
    load_range_equity_evaluation_fixture,
    run_range_equity_evaluation,
)
from poker_deliberation.range_equity_models import canonical_domain_sha256

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "range_equity" / "v1" / "scenarios.json"
COMMIT_ID = "1" * 40
TREE_ID = "2" * 40


def test_range_equity_evaluation_scores_every_metric_exactly(tmp_path: Path) -> None:
    fixture = load_range_equity_evaluation_fixture(FIXTURE)

    result = run_range_equity_evaluation(
        fixture,
        work_root=tmp_path / "run",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )

    assert result.passed is True, [
        (item.case_id, item.observed_evidence) for item in result.case_results if not item.passed
    ]
    assert result.overall_score == "1.0"
    assert tuple(item.case_id for item in result.case_results) == REQUIRED_CASE_IDS
    assert tuple(item.metric for item in result.metrics) == REQUIRED_METRICS
    assert all(item.score == "1.0" for item in result.case_results)
    assert all(item.score == "1.0" for item in result.metrics)
    assert all(item.declared_checks == item.passed_checks for item in result.metrics)
    assert result.source_commit_id == COMMIT_ID
    assert result.source_tree_id == TREE_ID


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path regression")
def test_range_equity_evaluation_supports_documented_windows_work_root(
    tmp_path: Path,
) -> None:
    fixture = load_range_equity_evaluation_fixture(FIXTURE)
    work_root = tmp_path / "long-root"
    current_length = len(str(work_root.resolve(strict=False)))
    if current_length >= 99:
        pytest.skip("temporary root is already beyond the targeted Windows boundary")
    work_root /= "x" * (99 - current_length - 1)
    assert len(str(work_root.resolve(strict=False))) == 99

    try:
        result = run_range_equity_evaluation(
            fixture,
            work_root=work_root,
            source_commit_id=COMMIT_ID,
            source_tree_id=TREE_ID,
        )
    finally:
        extended_root = _evaluation_work_root(work_root)
        if extended_root.exists():
            shutil.rmtree(extended_root)

    assert result.passed is True, [
        (item.case_id, item.observed_evidence) for item in result.case_results if not item.passed
    ]
    assert all(item.score == "1.0" for item in result.metrics)


def test_range_equity_evaluation_is_deterministic(tmp_path: Path) -> None:
    fixture = load_range_equity_evaluation_fixture(FIXTURE)

    first = run_range_equity_evaluation(
        fixture,
        work_root=tmp_path / "first",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    second = run_range_equity_evaluation(
        fixture,
        work_root=tmp_path / "second",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )

    assert first == second
    assert first.result_sha256 == second.result_sha256


def test_range_equity_evaluation_fixture_inventory_fails_closed() -> None:
    fixture = load_range_equity_evaluation_fixture(FIXTURE)
    payload = fixture.model_dump(mode="python")
    payload["cases"] = payload["cases"][:-1]

    with pytest.raises(ValueError, match="inventory mismatch"):
        RangeEquityEvaluationFixtureV1.model_validate(payload)


def test_range_equity_evaluation_result_recomputes_evidence_and_metrics(
    tmp_path: Path,
) -> None:
    fixture = load_range_equity_evaluation_fixture(FIXTURE)
    result = run_range_equity_evaluation(
        fixture,
        work_root=tmp_path / "run",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    payload = result.model_dump(mode="python")
    payload["case_results"][0]["observed_evidence"] = ()
    payload["metrics"][0]["declared_checks"] = 1
    payload["metrics"][0]["passed_checks"] = 1
    payload.pop("result_sha256")
    payload["result_sha256"] = canonical_domain_sha256(EVALUATION_FAMILY_ID, payload)

    with pytest.raises(ValueError, match="case result summary mismatch"):
        RangeEquityEvaluationResultV1.model_validate(payload, strict=True)


def test_range_equity_evaluation_source_binding_changes_result_hash(tmp_path: Path) -> None:
    fixture = load_range_equity_evaluation_fixture(FIXTURE)
    first = run_range_equity_evaluation(
        fixture,
        work_root=tmp_path / "first",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    second = run_range_equity_evaluation(
        fixture,
        work_root=tmp_path / "second",
        source_commit_id="3" * 40,
        source_tree_id=TREE_ID,
    )

    assert first.result_sha256 != second.result_sha256
