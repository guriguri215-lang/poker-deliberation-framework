"""Generate or verify the canonical P3-016A range grammar fixture."""

# ruff: noqa: E402 -- insert the repository src path before importing package code.

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from poker_deliberation.range_grammar import (
    action_prefix_sha256,
    validate_versioned_range,
)
from poker_deliberation.range_models import (
    RANGE_GRAMMAR_ID,
    RANGE_GRAMMAR_VERSION,
    VersionedRangeDefinitionV1,
)
from poker_deliberation.schemas import CanonicalHand

FIXTURE_RELATIVE = Path("tests/fixtures/range/v1/cases.json")
EVALUATION_RELATIVE = Path("evals/datasets/p3_016a/v1/cases.json")
CASES = (
    ("valid-weighted-blocked", "AKs@0.25,QQ@0.5"),
    ("valid-millionths", "QcQd@0.123456"),
    ("unsupported-plus", "QQ+"),
    ("overlap-before-blockers", "AKs,AsKs"),
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _range_case(notation: str) -> tuple[CanonicalHand, VersionedRangeDefinitionV1]:
    base = CanonicalHand.model_validate(
        {
            "game_type": "NLHE",
            "format": "cash",
            "table_size": 2,
            "small_blind": 1,
            "big_blind": 2,
            "players": [
                {"player_id": "hero", "position": "SB", "starting_stack": 100},
                {"player_id": "villain", "position": "BB", "starting_stack": 100},
            ],
            "hero_player_id": "hero",
            "hero_cards": ["As", "Kh"],
            "actions": [
                {
                    "street": "preflop",
                    "actor": "hero",
                    "action": "post_blind",
                    "amount": 1,
                },
                {
                    "street": "preflop",
                    "actor": "villain",
                    "action": "post_blind",
                    "amount": 2,
                },
            ],
        }
    )
    definition = VersionedRangeDefinitionV1.model_validate(
        {
            "range_id": "villain-preflop",
            "target_player_id": "villain",
            "notation": notation,
            "source": {
                "source_id": "range-fixture",
                "source_kind": "repository_fixture",
                "license_classification": "repository_owned_mit",
                "usage_classification": "redistribution_allowed",
                "content_status": "ASSUMPTION",
                "content_sha256": hashlib.sha256(notation.encode("utf-8")).hexdigest(),
            },
            "game_conditions": {
                "game_type": "NLHE",
                "format": "cash",
                "table_size": 2,
                "target_position": "BB",
                "street": "preflop",
                "starting_stack_min_bb_milli": 50_000,
                "starting_stack_max_bb_milli": 50_000,
                "as_of_action_index": 2,
                "action_prefix_sha256": action_prefix_sha256(base, 2),
            },
        }
    )
    payload = base.model_dump(mode="json")
    payload["known_ranges"] = [definition.model_dump(mode="json")]
    hand = CanonicalHand.model_validate(payload)
    parsed = hand.known_ranges[0]
    if not isinstance(parsed, VersionedRangeDefinitionV1):
        raise RuntimeError("fixture range did not round-trip through CanonicalHand")
    return hand, parsed


def fixture_document() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for case_id, notation in CASES:
        result = validate_versioned_range(*_range_case(notation))
        cases.append(
            {
                "case_id": case_id,
                "notation": notation,
                "expected": {
                    "status": result.status,
                    "diagnostic_codes": [
                        diagnostic.code.value for diagnostic in result.diagnostics
                    ],
                    "canonical_notation": result.canonical_notation,
                    "canonical_combo_sha256": result.canonical_combo_sha256,
                    "combo_count": result.combo_count,
                    "total_weight_millionths": result.total_weight_millionths,
                },
            }
        )
    return {
        "fixture_version": "1.0.0",
        "grammar_id": RANGE_GRAMMAR_ID,
        "grammar_version": RANGE_GRAMMAR_VERSION,
        "license": "MIT",
        "cases": cases,
    }


def fixture_bytes() -> bytes:
    return _canonical_bytes(fixture_document()) + b"\n"


def evaluation_document() -> dict[str, object]:
    fixture = fixture_document()
    return {
        "dataset_version": "1.0.0",
        "dataset_id": "p3-016a-range-grammar-conformance",
        "grammar_id": fixture["grammar_id"],
        "grammar_version": fixture["grammar_version"],
        "license": fixture["license"],
        "metric": "exact expected-field conformance",
        "aggregation": "all declared cases",
        "cases": fixture["cases"],
    }


def evaluation_bytes() -> bytes:
    return _canonical_bytes(evaluation_document()) + b"\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    outputs = (
        (ROOT / FIXTURE_RELATIVE, fixture_bytes()),
        (ROOT / EVALUATION_RELATIVE, evaluation_bytes()),
    )
    if args.check:
        stale = [
            path
            for path, expected in outputs
            if not path.is_file() or path.read_bytes() != expected
        ]
        if stale:
            print("out of date: " + ", ".join(str(path) for path in stale))
            return 1
        print("up to date: " + ", ".join(str(path) for path, _ in outputs))
        return 0
    for path, expected in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(expected)
        print(f"wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
