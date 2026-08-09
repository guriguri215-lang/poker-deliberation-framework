from __future__ import annotations

import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from poker_deliberation.bounded_river_review_workflow_evaluation import (
    load_bounded_river_review_workflow_fixture,
    run_bounded_river_review_workflow_evaluation,
)
from poker_deliberation.codex_bridge import product as bridge_product

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "bounded_river_review_workflow" / "v1"


def test_deterministic_workflow_evaluation_scores_all_independent_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work_root = REPOSITORY_ROOT / "tmp" / f"we-{uuid4().hex[:8]}"
    monkeypatch.setattr(bridge_product, "verify_bridge_checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bridge_product,
        "verify_bridge_module_origins",
        lambda *args, **kwargs: None,
    )
    try:
        fixture, fixture_sha256 = load_bounded_river_review_workflow_fixture(
            FIXTURE_ROOT / "scenarios.json"
        )
        result = run_bounded_river_review_workflow_evaluation(
            fixture,
            fixture_sha256=fixture_sha256,
            source_path=FIXTURE_ROOT / "source-ja.txt",
            range_path=FIXTURE_ROOT / "range.json",
            repository_root=REPOSITORY_ROOT,
            work_root=work_root,
            source_commit_id="1" * 40,
            source_tree_id="2" * 40,
        )

        assert result.passed is True
        assert result.score_milli == 1000
        assert len(result.metrics) == 5
        assert all(metric.passed and metric.evidence for metric in result.metrics)
        assert result.result_sha256 != "0" * 64
    finally:
        if work_root.exists():
            shutil.rmtree(work_root)
