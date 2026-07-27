"""Generate or verify the canonical P3-014A normalization fixture."""

# ruff: noqa: E402 -- insert the repository src path before importing package code.

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from poker_deliberation.normalization import (
    NORMALIZATION_PARSER_ID,
    NORMALIZATION_PARSER_VERSION,
    normalize_hand_bytes,
)

FIXTURE_RELATIVE = Path("tests/fixtures/normalization/v1/cases.json")

_VALID_LINES = (
    "game_type: NLHE",
    "format: cash",
    "table_size: 2",
    "small_blind: 1",
    "big_blind: 2",
    "player: hero, SB, 100",
    "player: villain, BB, 100",
    "hero_player_id: hero",
    "hero_cards: As Kh",
    "action: preflop, hero, post_blind, 1",
    "action: preflop, villain, post_blind, 2",
    "action: preflop, hero, call, 1",
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def fixture_document() -> dict[str, object]:
    lf = ("\n".join(_VALID_LINES) + "\n").encode()
    sources = (
        ("valid-lf", lf),
        ("valid-crlf", ("\r\n".join(_VALID_LINES) + "\r\n").encode()),
        ("bom", b"\xef\xbb\xbf" + lf),
        ("mixed-newline", lf.replace(b"\n", b"\r\n", 1)),
        ("non-nfc", b"# cafe\xcc\x81\n" + lf),
        ("duplicate-scalar", lf + b"table_size: 2\n"),
        ("unknown-key", lf + b"site_name: unsupported\n"),
        ("numeric-exponent", lf.replace(b"small_blind: 1", b"small_blind: 1e0")),
    )
    return {
        "fixture_version": "1.0.0",
        "parser_id": NORMALIZATION_PARSER_ID,
        "parser_version": NORMALIZATION_PARSER_VERSION,
        "cases": [
            {
                "case_id": case_id,
                "source_base64": base64.b64encode(source).decode("ascii"),
                "expected_result": normalize_hand_bytes(source).model_dump(mode="json"),
            }
            for case_id, source in sources
        ],
    }


def fixture_bytes() -> bytes:
    return _canonical_bytes(fixture_document())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    path = ROOT / FIXTURE_RELATIVE
    expected = fixture_bytes()
    if args.check:
        if not path.is_file() or path.read_bytes() != expected:
            print(f"out of date: {path}")
            return 1
        print(f"up to date: {path}")
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(expected)
    print(f"wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
