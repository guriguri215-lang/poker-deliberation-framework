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
SOURCE_PATH = ROOT / "src" / "poker_deliberation" / "roadmap_status.json"
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


def _head_document() -> dict[str, Any] | None:
    result = _git("show", "HEAD:src/poker_deliberation/roadmap_status.json")
    if result.returncode != 0:
        return None
    document = json.loads(result.stdout)
    if not isinstance(document, dict):
        raise ValueError("HEAD roadmap source must contain an object")
    return document


def _git_evidence(
    document: dict[str, Any],
) -> tuple[set[str], set[str], dict[str, set[str]], dict[str, set[str]]]:
    tracked_result = _git("ls-files", "-z")
    commits_result = _git("rev-list", "HEAD")
    if tracked_result.returncode != 0 or commits_result.returncode != 0:
        raise ValueError("Git evidence enumeration failed")
    tracked = {path for path in tracked_result.stdout.split("\0") if path}
    commits = {commit for commit in commits_result.stdout.splitlines() if commit}
    declared_commits = {
        commit for item in document["items"] for commit in item["completion_evidence"]["commits"]
    } | {
        commit
        for progress in document["milestone_progress"].values()
        for commit in progress["completion_evidence"]["commits"]
    }
    commit_paths: dict[str, set[str]] = {}
    changed_paths: dict[str, set[str]] = {}
    for commit in declared_commits:
        tree_result = _git("ls-tree", "-r", "--name-only", commit)
        changed_result = _git("diff-tree", "--root", "--no-commit-id", "--name-only", "-r", commit)
        if tree_result.returncode != 0 or changed_result.returncode != 0:
            raise ValueError(f"Git evidence commit inspection failed: {commit}")
        commit_paths[commit] = set(tree_result.stdout.splitlines())
        changed_paths[commit] = set(changed_result.stdout.splitlines())
    return tracked, commits, commit_paths, changed_paths


def _working_tree_document(
    newly_approved_references: set[str] | None = None, require_tracked: bool = False
) -> dict[str, Any]:
    document = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("roadmap source must contain an object")
    validate_roadmap(document)
    previous = _head_document()
    if previous is not None:
        validate_roadmap_update(previous, document, newly_approved_references)
    tracked, commits, commit_paths, changed_paths = (
        _git_evidence(document) if require_tracked else (None, None, None, None)
    )
    validate_repository_evidence(
        document,
        ROOT,
        tracked_paths=tracked,
        known_commits=commits,
        commit_paths=commit_paths,
        changed_paths=changed_paths,
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
    parser.add_argument("--approve-reference", action="append", default=[])
    args = parser.parse_args(argv)
    document = _working_tree_document(set(args.approve_reference), args.require_tracked)
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
