"""Run the fixed bounded-Japanese intake evaluation into an ignored artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from poker_deliberation.bounded_natural_language_evaluation import (
    load_bounded_natural_language_evaluation_fixture,
    run_bounded_natural_language_evaluation,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = load_bounded_natural_language_evaluation_fixture(args.fixture)
    result = run_bounded_natural_language_evaluation(
        fixture,
        source_path=args.source,
        work_root=args.work_root,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(result))
    print(result.model_dump_json(indent=2))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
