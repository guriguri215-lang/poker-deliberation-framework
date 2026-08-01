"""Run the deterministic confirmed-review contract evaluation family."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from poker_deliberation.confirmed_review_evaluation import (
    load_confirmed_review_evaluation_fixture,
    run_confirmed_review_evaluation,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "confirmed_review" / "v1" / "scenarios.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    args = parser.parse_args()
    fixture = load_confirmed_review_evaluation_fixture(args.fixture)
    result = run_confirmed_review_evaluation(
        fixture,
        work_root=args.work_root.resolve(),
        source_commit_id=args.source_commit,
        source_tree_id=args.source_tree,
    )
    if args.output is not None:
        args.output.write_bytes(canonical_json_bytes(result))
    print(json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
