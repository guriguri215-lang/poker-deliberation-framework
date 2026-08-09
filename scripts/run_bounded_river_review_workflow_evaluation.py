"""Run the deterministic P3-030D workflow evaluation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
source_root_text = str(SOURCE_ROOT)
sys.path[:] = [source_root_text, *(item for item in sys.path if item != source_root_text)]

from poker_deliberation.bounded_river_review_workflow_evaluation import (  # noqa: E402
    load_bounded_river_review_workflow_fixture,
    run_bounded_river_review_workflow_evaluation,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes  # noqa: E402

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "bounded_river_review_workflow" / "v1"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=FIXTURE_ROOT / "scenarios.json")
    parser.add_argument("--source", type=Path, default=FIXTURE_ROOT / "source-ja.txt")
    parser.add_argument(
        "--range",
        dest="range_path",
        type=Path,
        default=FIXTURE_ROOT / "range.json",
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture, fixture_sha256 = load_bounded_river_review_workflow_fixture(args.fixture)
    result = run_bounded_river_review_workflow_evaluation(
        fixture,
        fixture_sha256=fixture_sha256,
        source_path=args.source,
        range_path=args.range_path,
        repository_root=ROOT,
        work_root=args.work_root.resolve(),
        source_commit_id=args.source_commit,
        source_tree_id=args.source_tree,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(result))
    print(result.model_dump_json(indent=2))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
