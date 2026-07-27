from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import scripts.generate_offline_evaluation_fixtures as fixture_generator
from poker_deliberation.evaluation.canonical import (
    canonical_json_bytes,
    parse_canonical_model,
    sha256_bytes,
)
from poker_deliberation.evaluation.models import EvaluationResultV1
from poker_deliberation.evaluation.runner import (
    EvaluationLoadError,
    load_evaluation_suite,
    result_bytes,
    run_evaluation,
)

ROOT = Path(__file__).resolve().parents[2]
SUITE = "evals/suites/p3_017a_v1.json"
COMMIT_ID = "a" * 40
TREE_ID = "b" * 40


def _copy_fixture_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repository"
    shutil.copytree(ROOT / "evals", root / "evals")
    shutil.copy2(ROOT / "LICENSE", root / "LICENSE")
    return root


def test_offline_integrated_suite_passes_exactly_and_binds_runtime_and_tools() -> None:
    result = run_evaluation(
        ROOT,
        SUITE,
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )

    assert result.summary.model_dump(mode="json") == {
        "declared_case_count": 10,
        "observed_case_count": 10,
        "matched_case_count": 10,
        "mismatched_case_count": 0,
        "numerator": 10,
        "denominator": 10,
        "score": "1.0",
        "threshold": "1.0",
        "decision": "pass",
    }
    normal = result.outcomes[0]
    assert normal.case_id == "case-01-normal"
    assert normal.observed_status == "succeeded"
    assert normal.tool_evidence is not None
    assert normal.tool_evidence.tool_name == "pot_odds"
    assert normal.tool_evidence.contract_version == "2.0.0"
    assert normal.tool_evidence.numeric_exactness == "floating-verified"
    assert normal.tool_evidence.verification_passed is True
    assert "context:semantics-preserved" in normal.actual_evidence
    assert "routing:python-orchestrator" in normal.actual_evidence
    assert "runtime-bridge:false" in normal.actual_evidence
    assert result.source.source_commit_id == COMMIT_ID
    assert result.source.source_tree_id == TREE_ID
    assert len(result.source.config_sha256) == 64
    assert len(result.source.tool_contract_versions) == 20
    assert len(result.source.codex_runtime_inventory_sha256) == 64
    assert len(result.source.python_runtime_inventory_sha256) == 64


def test_negative_cases_are_expected_structured_failures_not_missing_denominator() -> None:
    result = run_evaluation(
        ROOT,
        SUITE,
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    negative = result.outcomes[1:]

    assert all(item.failure is not None for item in negative)
    assert all(item.exact_match for item in negative)
    assert result.summary.denominator == len(result.outcomes)
    assert {item.failure.category for item in negative if item.failure is not None} >= {
        "configuration",
        "integrity",
        "security",
        "timeout",
        "tool",
        "unsupported",
    }
    timeout = next(item for item in negative if item.case_kind == "structured-timeout")
    assert timeout.observed_status == "timed-out"
    assert timeout.failure is not None and timeout.failure.retryable is False


def test_result_is_canonical_and_deterministic_for_one_source_binding() -> None:
    first = run_evaluation(
        ROOT,
        SUITE,
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )
    second = run_evaluation(
        ROOT,
        SUITE,
        source_commit_id=COMMIT_ID,
        source_tree_id=TREE_ID,
    )

    assert result_bytes(first) == result_bytes(second)
    assert parse_canonical_model(result_bytes(first), EvaluationResultV1) == first

    drifted = run_evaluation(
        ROOT,
        SUITE,
        source_commit_id="c" * 40,
        source_tree_id=TREE_ID,
    )
    assert result_bytes(first) != result_bytes(drifted)
    assert first.outcomes == drifted.outcomes
    assert first.summary == drifted.summary


def test_generated_fixture_is_current_and_canonical() -> None:
    assert fixture_generator.main(["--check"]) == 0
    loaded = load_evaluation_suite(ROOT, SUITE)
    assert (
        canonical_json_bytes(loaded.dataset)
        == (ROOT / "evals" / "datasets" / "p3_017a" / "v1" / "cases.json").read_bytes()
    )


def test_suite_loader_rejects_missing_input_before_case_execution(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    suite = load_evaluation_suite(ROOT, SUITE).suite.model_copy(
        update={"scorer_path": "evals/scorers/missing.json"}
    )
    suite_path = root / "suite.json"
    suite_path.write_bytes(canonical_json_bytes(suite))

    with pytest.raises(EvaluationLoadError) as error:
        load_evaluation_suite(root, "suite.json")
    assert error.value.code == "evaluation-input-missing"
    assert error.value.path == "dataset_manifest_path"


def test_suite_loader_rejects_count_hash_and_metric_drift(tmp_path: Path) -> None:
    count_root = _copy_fixture_repository(tmp_path / "count")
    loaded = load_evaluation_suite(count_root, SUITE)
    manifest = loaded.manifest.model_copy(update={"case_count": 11})
    manifest_bytes = canonical_json_bytes(manifest)
    (count_root / loaded.suite.dataset_manifest_path).write_bytes(manifest_bytes)
    suite = loaded.suite.model_copy(
        update={"dataset_manifest_sha256": sha256_bytes(manifest_bytes)}
    )
    (count_root / SUITE).write_bytes(canonical_json_bytes(suite))
    with pytest.raises(EvaluationLoadError) as count_error:
        load_evaluation_suite(count_root, SUITE)
    assert count_error.value.code == "dataset-case-count-mismatch"

    hash_root = _copy_fixture_repository(tmp_path / "hash")
    hash_loaded = load_evaluation_suite(hash_root, SUITE)
    changed_scorer = hash_loaded.scorer.model_copy(update={"threshold": "0.9"})
    (hash_root / hash_loaded.suite.scorer_path).write_bytes(canonical_json_bytes(changed_scorer))
    with pytest.raises(EvaluationLoadError) as hash_error:
        load_evaluation_suite(hash_root, SUITE)
    assert hash_error.value.code == "scorer-hash-mismatch"

    metric_root = _copy_fixture_repository(tmp_path / "metric")
    metrics_path = metric_root / "evals" / "metrics.json"
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics["metrics"].remove("reproducibility")
    metrics_path.write_text(json.dumps(metrics), encoding="utf-8")
    with pytest.raises(EvaluationLoadError) as metric_error:
        load_evaluation_suite(metric_root, SUITE)
    assert metric_error.value.code == "metric-not-registered"
