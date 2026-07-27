from __future__ import annotations

import base64
import json
from pathlib import Path

import scripts.generate_normalization_fixtures as fixture_generator
from poker_deliberation.normalization import NormalizationResultV1, normalize_hand_bytes

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "normalization" / "v1" / "cases.json"


def test_generated_normalization_fixture_is_current_and_reproducible() -> None:
    assert fixture_generator.main(["--check"]) == 0
    document = json.loads(FIXTURE.read_bytes())
    assert document["fixture_version"] == "1.0.0"
    assert document["parser_version"] == "1.0.0"
    assert len(document["cases"]) == 8

    for case in document["cases"]:
        source = base64.b64decode(case["source_base64"], validate=True)
        expected = NormalizationResultV1.model_validate_json(json.dumps(case["expected_result"]))
        assert normalize_hand_bytes(source) == expected
