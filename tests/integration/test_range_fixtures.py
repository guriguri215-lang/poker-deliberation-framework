from __future__ import annotations

import json
from pathlib import Path

import scripts.generate_range_fixtures as fixture_generator
from poker_deliberation.public_preflight import _range_grammar_artifacts_check
from poker_deliberation.range_grammar import validate_versioned_range

ROOT = Path(__file__).resolve().parents[2]


def _projection(notation: str) -> dict[str, object]:
    result = validate_versioned_range(*fixture_generator._range_case(notation))
    return {
        "status": result.status,
        "diagnostic_codes": [diagnostic.code.value for diagnostic in result.diagnostics],
        "canonical_notation": result.canonical_notation,
        "canonical_combo_sha256": result.canonical_combo_sha256,
        "combo_count": result.combo_count,
        "total_weight_millionths": result.total_weight_millionths,
    }


def test_canonical_range_fixture_and_evaluation_dataset_are_current() -> None:
    fixture_path = ROOT / fixture_generator.FIXTURE_RELATIVE
    evaluation_path = ROOT / fixture_generator.EVALUATION_RELATIVE

    assert fixture_path.read_bytes() == fixture_generator.fixture_bytes()
    assert evaluation_path.read_bytes() == fixture_generator.evaluation_bytes()
    assert fixture_generator.main(["--check"]) == 0
    assert _range_grammar_artifacts_check(ROOT).status == "pass"


def test_declared_range_conformance_cases_match_deterministic_results() -> None:
    document = json.loads(
        (ROOT / fixture_generator.EVALUATION_RELATIVE).read_text(encoding="utf-8")
    )

    assert document["license"] == "MIT"
    assert document["aggregation"] == "all declared cases"
    assert len(document["cases"]) == 4
    for case in document["cases"]:
        assert _projection(case["notation"]) == case["expected"]
