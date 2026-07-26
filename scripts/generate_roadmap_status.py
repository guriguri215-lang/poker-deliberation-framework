"""Generate or verify the tracked roadmap status projection."""

# ruff: noqa: E402 -- insert the repository src path before importing the renderer.

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SOURCE_RELATIVE = Path("src") / "poker_deliberation" / "roadmap_status.json"
SOURCE_PATH = ROOT / SOURCE_RELATIVE
WORKTREE_SOURCE = ROOT / "src"
if str(WORKTREE_SOURCE) not in sys.path:
    sys.path.insert(0, str(WORKTREE_SOURCE))

from poker_deliberation.roadmap import (
    render_roadmap_markdown,
    validate_repository_evidence,
    validate_roadmap,
    validate_roadmap_update,
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def _tracked_paths() -> set[str]:
    tracked_result = _git("ls-files", "-z")
    if tracked_result.returncode != 0:
        raise ValueError("tracked path enumeration failed")
    return {path for path in tracked_result.stdout.split("\0") if path}


def _git_document(revision: str) -> dict[str, Any]:
    result = _git("show", f"{revision}:{SOURCE_RELATIVE.as_posix()}")
    if result.returncode != 0:
        raise ValueError(f"{revision} roadmap source could not be read")
    document = json.loads(result.stdout)
    if not isinstance(document, dict):
        raise ValueError(f"{revision} roadmap source must contain an object")
    return document


def _schema_version(document: dict[str, Any], revision: str) -> str:
    version = document.get("schema_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{revision} roadmap schema_version is invalid")
    return version


def _history_revisions() -> list[str]:
    result = _git(
        "rev-list",
        "--first-parent",
        "HEAD",
        "--",
        SOURCE_RELATIVE.as_posix(),
    )
    if result.returncode != 0:
        raise ValueError("roadmap first-parent history could not be read")
    revisions = [revision for revision in result.stdout.splitlines() if revision]
    if not revisions:
        raise ValueError("roadmap first-parent history is empty")
    return revisions


def _validate_committed_history(
    head_document: dict[str, Any],
    current_schema_version: str,
) -> None:
    if _schema_version(head_document, "HEAD") != current_schema_version:
        return
    revisions = _history_revisions()
    newer = _git_document(revisions[0])
    if newer != head_document:
        raise ValueError("HEAD roadmap differs from its first-parent history")
    for revision in revisions[1:]:
        older = _git_document(revision)
        if _schema_version(older, revision) != current_schema_version:
            break
        validate_roadmap_update(older, newer)
        newer = older


def _working_tree_document(require_tracked: bool = False) -> dict[str, Any]:
    document = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("roadmap source must contain an object")
    validate_roadmap(document)
    current_schema_version = _schema_version(document, "working tree")
    head_document = _git_document("HEAD")
    _validate_committed_history(head_document, current_schema_version)
    if (
        head_document != document
        and _schema_version(
            head_document,
            "HEAD",
        )
        == current_schema_version
    ):
        validate_roadmap_update(head_document, document)
    validate_repository_evidence(
        document,
        ROOT,
        tracked_paths=_tracked_paths() if require_tracked else None,
    )
    return document


def _generated_path(document: dict[str, Any]) -> Path:
    policy = document.get("source_policy")
    if not isinstance(policy, dict) or not isinstance(policy.get("generated_document"), str):
        raise ValueError("source_policy.generated_document must be a string")
    candidate = (ROOT / policy["generated_document"]).resolve()
    try:
        candidate.relative_to(ROOT.resolve())
    except ValueError as exc:
        raise ValueError("generated document escapes repository") from exc
    return candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--require-tracked", action="store_true")
    args = parser.parse_args(argv)
    document = _working_tree_document(args.require_tracked)
    doc_path = _generated_path(document)
    rendered = render_roadmap_markdown(document)
    if args.check:
        if not doc_path.exists() or doc_path.read_text(encoding="utf-8") != rendered:
            print(f"out of date: {doc_path}")
            return 1
        print(f"up to date: {doc_path}")
        return 0
    doc_path.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"wrote: {doc_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
