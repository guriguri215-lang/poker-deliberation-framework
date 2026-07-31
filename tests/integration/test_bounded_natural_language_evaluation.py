from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from poker_deliberation.bounded_natural_language_evaluation import (
    _valid_extraction_evidence,
    load_bounded_natural_language_evaluation_fixture,
    load_bounded_natural_language_evaluation_result,
    run_bounded_natural_language_evaluation,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from tests.bounded_natural_language_support import SOURCE_BYTES, ready_bounded_preparation

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


def test_bounded_evaluation_fails_closed_when_repository_source_changes(tmp_path) -> None:
    fixture = load_bounded_natural_language_evaluation_fixture(FIXTURE)
    baseline = run_bounded_natural_language_evaluation(
        fixture,
        source_path=SOURCE,
        work_root=tmp_path / "baseline",
    )
    changed_source = tmp_path / "changed-source.txt"
    changed_source.write_bytes(SOURCE_BYTES.replace(b"1/2", "1\uff0f2".encode()))

    changed = run_bounded_natural_language_evaluation(
        fixture,
        source_path=changed_source,
        work_root=tmp_path / "changed",
    )

    assert changed.passed is False
    assert changed.overall_score == "0.0"
    assert all(not item.passed and item.score == "0.0" for item in changed.case_results)
    assert changed.source_sha256 != fixture.source_sha256
    assert changed.source_sha256 != baseline.source_sha256
    assert changed.result_sha256 != baseline.result_sha256


def test_fixed_extraction_oracle_rejects_field_and_span_tamper() -> None:
    fixture = load_bounded_natural_language_evaluation_fixture(FIXTURE)
    prepared = ready_bounded_preparation(intake_id="intake-evaluation-oracle")
    assert prepared.candidate is not None
    projection = prepared.candidate.projection
    first = projection.source_bindings[0]
    tampered_binding = first.model_copy(update={"start_byte": 0, "end_byte": 1})
    span_projection = projection.model_copy(
        update={"source_bindings": (tampered_binding, *projection.source_bindings[1:])}
    )
    span_prepared = prepared.model_copy(
        update={"candidate": prepared.candidate.model_copy(update={"projection": span_projection})}
    )
    field_projection = projection.model_copy(
        update={"hand": projection.hand.model_copy(update={"table_size": 3})}
    )
    field_prepared = prepared.model_copy(
        update={"candidate": prepared.candidate.model_copy(update={"projection": field_projection})}
    )

    assert _valid_extraction_evidence(prepared, SOURCE_BYTES, fixture) == (
        "exact-field-extraction",
        "exact-source-span-binding",
    )
    assert "exact-source-span-binding" not in _valid_extraction_evidence(
        span_prepared, SOURCE_BYTES, fixture
    )
    assert "exact-field-extraction" not in _valid_extraction_evidence(
        field_prepared, SOURCE_BYTES, fixture
    )
