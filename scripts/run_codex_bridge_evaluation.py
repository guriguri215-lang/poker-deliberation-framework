"""Run the deterministic no-network P2-025B bridge evaluation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
source_root_text = str(SOURCE_ROOT)
sys.path[:] = [source_root_text, *(item for item in sys.path if item != source_root_text)]

from poker_deliberation.codex_bridge.canonical import canonical_json_bytes  # noqa: E402
from poker_deliberation.codex_bridge.evaluation import (  # noqa: E402
    load_bounded_codex_bridge_evaluation_fixture,
    run_bounded_codex_bridge_evaluation,
)

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "codex_bridge" / "v1" / "cases.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture = load_bounded_codex_bridge_evaluation_fixture(args.fixture)
    args.work_root.mkdir(parents=True, exist_ok=True)
    result = run_bounded_codex_bridge_evaluation(
        fixture,
        repository_root=ROOT,
        work_root=args.work_root,
        source_commit_id=args.source_commit,
        source_tree_id=args.source_tree,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json_bytes(result))
    print(result.model_dump_json(indent=2))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
