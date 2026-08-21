"""Run the deterministic P3-030G supervised-workflow qualification harness."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src"
source_root_text = str(SOURCE_ROOT)
sys.path[:] = [source_root_text, *(item for item in sys.path if item != source_root_text)]

from poker_deliberation.bounded_river_review_workflow_evaluation import (  # noqa: E402
    BoundedRiverReviewWorkflowEvaluationResultV2,
    bounded_river_review_workflow_evaluation_config,
    load_bounded_river_review_workflow_evaluation_result_v2,
    load_bounded_river_review_workflow_fixture_v2,
    run_bounded_river_review_workflow_evaluation_v2,
    verify_bounded_river_review_workflow_evaluation_result_v2,
)
from poker_deliberation.bounded_river_review_workflow_qualification import (  # noqa: E402
    build_sanitized_bounded_river_review_workflow_qualification_manifest,
    load_sanitized_bounded_river_review_workflow_qualification_manifest,
    write_sanitized_bounded_river_review_workflow_qualification_manifest,
)
from poker_deliberation.codex_bridge.product import (  # noqa: E402
    BridgeProductError,
    confined_runtime_scratch_path,
)
from poker_deliberation.storage.revision_canonical import (  # noqa: E402
    canonical_json_bytes,
    check_path_lengths,
)

FIXTURE_V1_ROOT = ROOT / "tests" / "fixtures" / "bounded_river_review_workflow" / "v1"
FIXTURE_V2_ROOT = ROOT / "tests" / "fixtures" / "bounded_river_review_workflow" / "v2"
_TERMINAL_STORE_PATH_BUDGET = (
    Path("s")
    / "p"
    / "runs"
    / "run-p3-030g-evaluation"
    / ".terminal-store"
    / "transactions"
    / f"txn-{'0' * 32}"
    / "payload"
    / "tool_results"
    / f"tool-result-{'0' * 12}.input.json"
)


def _overlaps(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _git_status(*arguments: str) -> int:
    try:
        return subprocess.run(
            ("git", "-C", str(ROOT), *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=15,
        ).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("BRWE_E_PATH") from exc


def _validate_runner_paths(
    *,
    fixture: Path,
    source: Path,
    range_path: Path,
    work_root: Path,
    output: Path,
    manifest_output: Path | None,
) -> tuple[Path, Path, Path | None]:
    """Reject unsafe or ambiguous mutable paths before any evaluator mutation."""

    try:
        inputs = tuple(path.resolve(strict=True) for path in (fixture, source, range_path))
        if any(not path.is_file() for path in inputs):
            raise ValueError("BRWE_E_PATH")
        resolved_work = work_root.resolve(strict=False)
        resolved_output = output.resolve(strict=False)
        resolved_manifest = (
            manifest_output.resolve(strict=False) if manifest_output is not None else None
        )
        targets = tuple(
            path for path in (resolved_work, resolved_output, resolved_manifest) if path is not None
        )
        if any(path.exists() for path in targets):
            raise ValueError("BRWE_E_PATH")
        repository = ROOT.resolve(strict=True)
        if not resolved_work.is_relative_to(repository):
            raise ValueError("BRWE_E_PATH")
        if any(
            _overlaps(left, right)
            for index, left in enumerate(targets)
            for right in targets[index + 1 :]
        ):
            raise ValueError("BRWE_E_PATH")
        protected = (
            ROOT.resolve(strict=True) / ".git",
            ROOT.resolve(strict=True) / "user_materials",
            ROOT.resolve(strict=True) / "src",
            ROOT.resolve(strict=True) / "scripts",
            ROOT.resolve(strict=True) / "tests",
            *inputs,
        )
        if any(_overlaps(target, item) for target in targets for item in protected):
            raise ValueError("BRWE_E_PATH")
        for target in targets:
            parent = target.parent
            while not parent.exists() and parent != parent.parent:
                parent = parent.parent
            if not parent.is_dir():
                raise ValueError("BRWE_E_PATH")
            if not target.is_relative_to(repository):
                continue
            relative = target.relative_to(repository).as_posix()
            if (
                _git_status("ls-files", "--error-unmatch", "--", relative) == 0
                or _git_status("check-ignore", "--quiet", "--", relative) != 0
            ):
                raise ValueError("BRWE_E_PATH")
        check_path_lengths((*targets, resolved_work / _TERMINAL_STORE_PATH_BUDGET))
        try:
            if confined_runtime_scratch_path(resolved_work, repository) != resolved_work:
                raise ValueError("BRWE_E_PATH")
        except BridgeProductError as exc:
            raise ValueError("BRWE_E_PATH") from exc
        return resolved_work, resolved_output, resolved_manifest
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == "BRWE_E_PATH":
            raise
        raise ValueError("BRWE_E_PATH") from exc


def _write_verified_result(
    path: Path,
    result: BoundedRiverReviewWorkflowEvaluationResultV2,
    *,
    fixture_path: Path,
    source_path: Path,
    range_path: Path,
    source_commit_id: str,
    source_tree_id: str,
) -> None:
    if not verify_bounded_river_review_workflow_evaluation_result_v2(
        result,
        repository_root=ROOT,
        fixture_path=fixture_path,
        source_path=source_path,
        range_path=range_path,
        source_commit_id=source_commit_id,
        source_tree_id=source_tree_id,
    ):
        raise ValueError("BRWE_E_RESULT")
    data = canonical_json_bytes(result)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
    loaded = load_bounded_river_review_workflow_evaluation_result_v2(path)
    if loaded != result or not verify_bounded_river_review_workflow_evaluation_result_v2(
        loaded,
        repository_root=ROOT,
        fixture_path=fixture_path,
        source_path=source_path,
        range_path=range_path,
        source_commit_id=source_commit_id,
        source_tree_id=source_tree_id,
    ):
        raise ValueError("BRWE_E_RESULT")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=FIXTURE_V2_ROOT / "scenarios.json")
    parser.add_argument("--source", type=Path, default=FIXTURE_V1_ROOT / "source-ja.txt")
    parser.add_argument(
        "--range",
        dest="range_path",
        type=Path,
        default=FIXTURE_V1_ROOT / "range.json",
    )
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--source-tree", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument(
        "--qualification-id",
        default="p3-030g-deterministic-workflow-qualification",
    )
    args = parser.parse_args()
    work_root, output, manifest_output = _validate_runner_paths(
        fixture=args.fixture,
        source=args.source,
        range_path=args.range_path,
        work_root=args.work_root,
        output=args.output,
        manifest_output=args.manifest_output,
    )
    fixture, fixture_sha256 = load_bounded_river_review_workflow_fixture_v2(args.fixture)
    result = run_bounded_river_review_workflow_evaluation_v2(
        fixture,
        fixture_sha256=fixture_sha256,
        source_path=args.source,
        range_path=args.range_path,
        repository_root=ROOT,
        work_root=work_root,
        source_commit_id=args.source_commit,
        source_tree_id=args.source_tree,
    )
    manifest = None
    if result.passed and manifest_output is not None:
        manifest = build_sanitized_bounded_river_review_workflow_qualification_manifest(
            config=bounded_river_review_workflow_evaluation_config(work_root),
            repository_root=ROOT,
            workflow_root=work_root / "w",
            workflow_id=result.workflow_id,
            qualification_id=args.qualification_id,
            deterministic_evaluation=result,
        )
    _write_verified_result(
        output,
        result,
        fixture_path=args.fixture,
        source_path=args.source,
        range_path=args.range_path,
        source_commit_id=args.source_commit,
        source_tree_id=args.source_tree,
    )
    if manifest is not None and manifest_output is not None:
        manifest_output.parent.mkdir(parents=True, exist_ok=True)
        write_sanitized_bounded_river_review_workflow_qualification_manifest(
            manifest_output,
            manifest,
        )
        loaded_manifest = load_sanitized_bounded_river_review_workflow_qualification_manifest(
            manifest_output
        )
        if loaded_manifest != manifest:
            raise ValueError("BRWQ_E_MANIFEST_STORAGE")
    print(result.model_dump_json(indent=2))
    return 0 if result.passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
