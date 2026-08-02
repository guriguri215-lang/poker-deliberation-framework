"""Run the deterministic P3-030C exact-evidence evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path

from poker_deliberation.bounded_river_call_ev_evaluation import (
    load_bounded_river_call_ev_evaluation_fixture,
    run_bounded_river_call_ev_evaluation,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "bounded_river_call_ev" / "v1" / "scenarios.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = load_bounded_river_call_ev_evaluation_fixture(args.fixture)
    result = run_bounded_river_call_ev_evaluation(
        fixture,
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
