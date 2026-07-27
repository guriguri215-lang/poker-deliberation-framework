"""Run the canonical offline evaluation suite into an ignored tmp artifact."""

# ruff: noqa: E402 -- insert the repository src path before importing package code.

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from poker_deliberation.evaluation.runner import (
    EvaluationLoadError,
    result_bytes,
    run_evaluation,
)

DEFAULT_SUITE = "evals/suites/p3_017a_v1.json"


def _output_path(relative: str) -> Path:
    raw = Path(relative)
    if raw.is_absolute() or not raw.parts or raw.parts[0] != "tmp" or raw.suffix != ".json":
        raise ValueError("output must be a repository-relative JSON path under tmp/")
    candidate = (ROOT / raw).resolve()
    try:
        candidate.relative_to((ROOT / "tmp").resolve())
    except ValueError as exc:
        raise ValueError("output must remain under the repository tmp/ directory") from exc
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--suite", default=DEFAULT_SUITE)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    try:
        output = _output_path(args.output)
        result = run_evaluation(
            ROOT,
            args.suite,
            source_commit_id=args.source_commit,
            source_tree_id=args.source_tree,
        )
    except EvaluationLoadError as exc:
        print(
            json.dumps(
                {"status": "failed", "code": exc.code, "path": exc.path},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except ValueError as exc:
        print(
            json.dumps(
                {"status": "failed", "code": "invalid-invocation", "message": str(exc)},
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result_bytes(result))
    print(
        json.dumps(
            {
                "decision": result.summary.decision,
                "denominator": result.summary.denominator,
                "numerator": result.summary.numerator,
                "output": output.relative_to(ROOT).as_posix(),
                "score": result.summary.score,
                "status": "completed",
                "threshold": result.summary.threshold,
            },
            sort_keys=True,
        )
    )
    return 0 if result.summary.decision == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
