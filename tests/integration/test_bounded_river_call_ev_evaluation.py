from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.bounded_river_call_ev_evaluation import (
    EVALUATION_FAMILY_ID,
    REQUIRED_CASE_IDS,
    REQUIRED_METRICS,
    BoundedRiverCallEvEvaluationFixtureV1,
    BoundedRiverCallEvEvaluationResultV1,
    load_bounded_river_call_ev_evaluation_fixture,
    run_bounded_river_call_ev_evaluation,
)
from poker_deliberation.range_equity_models import canonical_domain_sha256

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "bounded_river_call_ev" / "v1" / "scenarios.json"
COMMIT_ID = "1" * 40
TREE_ID = "2" * 40


def test_bounded_river_call_ev_evaluation_scores_all_metrics(tmp_path: Path) -> None:
    result = run_bounded_river_call_ev_evaluation(
        load_bounded_river_call_ev_evaluation_fixture(FIXTURE),
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
    assert all(item.score == "1.0" for item in result.metrics)
    assert result.source_commit_id == COMMIT_ID
    assert result.source_tree_id == TREE_ID


def test_evaluation_fixture_and_result_tamper_fail_closed(tmp_path: Path) -> None:
    fixture = load_bounded_river_call_ev_evaluation_fixture(FIXTURE)
    fixture_payload = fixture.model_dump(mode="python")
    fixture_payload["cases"] = fixture_payload["cases"][:-1]
    with pytest.raises(ValueError, match="inventory mismatch"):
        BoundedRiverCallEvEvaluationFixtureV1.model_validate(fixture_payload)

    result = run_bounded_river_call_ev_evaluation(
        fixture,
        work_root=tmp_path / "run",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    payload = result.model_dump(mode="python")
    payload["case_results"][0]["observed_evidence"] = ()
    payload.pop("result_sha256")
    payload["result_sha256"] = canonical_domain_sha256(EVALUATION_FAMILY_ID, payload)
    with pytest.raises(ValueError, match="case score mismatch"):
        BoundedRiverCallEvEvaluationResultV1.model_validate(payload, strict=True)


def test_evaluation_is_deterministic_and_source_bound(tmp_path: Path) -> None:
    fixture = load_bounded_river_call_ev_evaluation_fixture(FIXTURE)
    first = run_bounded_river_call_ev_evaluation(
        fixture,
        work_root=tmp_path / "first",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    second = run_bounded_river_call_ev_evaluation(
        fixture,
        work_root=tmp_path / "second",
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    rebound = run_bounded_river_call_ev_evaluation(
        fixture,
        work_root=tmp_path / "rebound",
        source_commit_id="3" * 40,
        source_tree_id=TREE_ID,
    )

    assert first == second
    assert first.result_sha256 != rebound.result_sha256
