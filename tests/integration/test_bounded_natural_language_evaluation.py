from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from poker_deliberation.bounded_natural_language_evaluation import (
    load_bounded_natural_language_evaluation_fixture,
    load_bounded_natural_language_evaluation_result,
    run_bounded_natural_language_evaluation,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "bounded_natural_language" / "v1" / "scenarios.json"
SOURCE = ROOT / "tests" / "fixtures" / "bounded_natural_language" / "v1" / "valid-ja.txt"


def test_bounded_evaluation_scores_every_declared_case_and_metric_exactly(tmp_path) -> None:
    fixture = load_bounded_natural_language_evaluation_fixture(FIXTURE)
    result = run_bounded_natural_language_evaluation(
        fixture,
        source_path=SOURCE,
        work_root=tmp_path / f"e-{uuid4().hex[:8]}",
    )

    assert result.passed is True
    assert result.overall_score == "1.0"
    assert result.interpretation == "bounded_grammar_contract_only"
    assert all(item.passed and item.score == "1.0" for item in result.case_results)
    assert all(item.score == "1.0" for item in result.metrics)
    assert [item.metric for item in result.metrics] == [
        "exact_field_extraction",
        "exact_source_span_binding",
        "exact_diagnostic",
        "end_to_end_tool_evidence",
        "storage_replay_evidence",
    ]

    result_path = tmp_path / "result.json"
    result_path.write_bytes(canonical_json_bytes(result))
    assert load_bounded_natural_language_evaluation_result(result_path) == result
