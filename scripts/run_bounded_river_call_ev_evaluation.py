"""Run the deterministic P3-030C exact-evidence evaluation."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
source_root_text = str(SOURCE_ROOT)
sys.path[:] = [source_root_text, *(item for item in sys.path if item != source_root_text)]

from poker_deliberation.bounded_river_call_ev import BoundedRiverCallEvError  # noqa: E402
from poker_deliberation.bounded_river_call_ev_evaluation import (  # noqa: E402
    BOUNDED_RIVER_CALL_EV_EVALUATION_PATH_BUDGET,
    load_bounded_river_call_ev_evaluation_fixture,
    normalize_bounded_river_call_ev_evaluation_root,
    run_bounded_river_call_ev_evaluation,
)
from poker_deliberation.bounded_river_call_ev_models import (  # noqa: E402
    BoundedRiverCallEvDiagnosticCode,
)
from poker_deliberation.storage.revision_canonical import (  # noqa: E402
    CanonicalStorageError,
    canonical_json_bytes,
    check_path_lengths,
)

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "bounded_river_call_ev" / "v1" / "scenarios.json"


def _validate_runner_paths(*, work_root: Path, output: Path) -> tuple[Path, Path]:
    """Resolve and bound all evaluator paths before the first filesystem mutation."""

    try:
        resolved_work = normalize_bounded_river_call_ev_evaluation_root(work_root)
        resolved_output = output.resolve(strict=False)
        check_path_lengths(
            (
                resolved_work,
                resolved_output,
                resolved_work / BOUNDED_RIVER_CALL_EV_EVALUATION_PATH_BUDGET,
            )
        )
    except (CanonicalStorageError, OSError) as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.STORAGE,
            "evaluation.paths",
        ) from exc
    return resolved_work, resolved_output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    work_root, output = _validate_runner_paths(
        work_root=args.work_root,
        output=args.output,
    )
    fixture = load_bounded_river_call_ev_evaluation_fixture(args.fixture)
    result = run_bounded_river_call_ev_evaluation(
        fixture,
        repository_root=ROOT,
        work_root=work_root,
        source_commit_id=args.source_commit,
        source_tree_id=args.source_tree,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical_json_bytes(result))
    print(result.model_dump_json(indent=2))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
