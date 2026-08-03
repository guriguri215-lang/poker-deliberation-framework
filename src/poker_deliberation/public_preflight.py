"""Offline, redacted public-release preflight for the tracked repository surface."""

from __future__ import annotations

import argparse
import ast
import codecs
import hashlib
import importlib.metadata
import json
import re
import stat
import subprocess
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

import yaml  # type: ignore[import-untyped]

CheckStatus = Literal["pass", "review", "fail", "unknown"]
EvidenceLabel = Literal["FACT", "UNKNOWN"]
FindingClassification = Literal["candidate", "synthetic_canary"]
ScanCategory = Literal["secret", "pii"]

MAX_HISTORY_COMMITS = 10_000
MAX_BLOB_SCAN_BYTES = 2_000_000
LARGE_FILE_BYTES = 1_000_000
RANGE_EQUITY_EVALUATION_RUNNER_SHA256 = (
    "35f76a142e93132fde84f8bc08a2c17537ace3446c135a933dcb36bc373afe5d"
)
EXPECTED_ROADMAP_SCHEMA_VERSION = "12.0.0"
ROADMAP_MODULE_SHA256 = "ce77719249a87348444c4b6419e6fa7ff07d78486eeb9c664a73bc5dd533ea0b"
RANGE_EQUITY_BRIDGE_DOC_SHA256 = "8e1b9e7b6e21a1b11d9f1e33a1067c1012aecb7e6cd1c6ec9986e5f0cb15964b"
CAPABILITY_DOCUMENT_PATHS = (
    "README.md",
    "docs/bounded-river-call-ev.md",
    "docs/capabilities.md",
    "docs/limitations.md",
    "docs/range-grammar.md",
    "docs/range-equity-bridge.md",
    "docs/roadmap-status.md",
)
CAPABILITY_DOCUMENT_SET_SHA256 = "ea36be0df9908d332b8fd56a00ac0e7c2ddabe72680b71854d6144e895eff4ea"
PUBLIC_DOCUMENT_INVENTORY_SHA256 = (
    "13600d7ad58b1ef49de02335ff184517cf9165e80fa21d217b87919af9e98854"
)


class _UniqueKeySafeLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Safe YAML loader that rejects ambiguous duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: Any,
    deep: bool = False,
) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as exc:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise yaml.constructor.ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _strict_tree_equal(left: object, right: object) -> bool:
    """Compare JSON-like trees without Python's bool/int equality collapse."""

    if type(left) is not type(right):
        return False
    if isinstance(left, dict) and isinstance(right, dict):
        if len(left) != len(right) or set(left) != set(right):
            return False
        return all(_strict_tree_equal(left[key], right[key]) for key in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _strict_tree_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return left == right


def _assigned_string_constant(path: Path, name: str) -> str | None:
    try:
        module = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return None
    stores = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store) and node.id == name
    ]
    blocked_dynamic_names = {
        "__import__",
        "eval",
        "exec",
        "getattr",
        "globals",
        "locals",
        "setattr",
        "vars",
    }
    for node in ast.walk(module):
        if isinstance(node, ast.Name) and node.id in blocked_dynamic_names:
            return None
        if isinstance(node, ast.Attribute) and node.attr in {
            *blocked_dynamic_names,
            "__dict__",
            "__setattr__",
            "__setitem__",
            "update",
        }:
            return None
        if isinstance(node, ast.alias) and (node.asname or node.name.split(".")[-1]) == name:
            return None
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == name
        ):
            return None
        if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
            return None
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.slice, ast.Constant)
            and node.slice.value == name
        ):
            return None
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and node.attr == name
        ):
            return None
    matches: list[str] = []
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            matches.append(statement.value.value)
    return matches[0] if len(stores) == 1 and len(matches) == 1 else None


def _head_worktree_blob_binding(repo: Path) -> dict[str, object]:
    raw_tree = _git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD")
    head_blobs: list[tuple[str, str, str]] = []
    unsupported: list[str] = []
    for raw_entry in raw_tree.split(b"\0"):
        if not raw_entry or b"\t" not in raw_entry:
            continue
        metadata, raw_path = raw_entry.split(b"\t", 1)
        parts = metadata.split()
        path = _decode_path(raw_path)
        if len(parts) != 3 or parts[1] != b"blob" or parts[0] not in {b"100644", b"100755"}:
            unsupported.append(path)
            continue
        head_blobs.append((path, parts[0].decode("ascii"), parts[2].decode("ascii")))

    index_paths = _tracked_paths(repo)
    head_paths = [path for path, _mode, _oid in head_blobs]
    unsafe_paths = [path for path in head_paths if "\n" in path or "\r" in path]
    observed_oids: list[str] = []
    hash_error = False
    if not unsafe_paths:
        try:
            raw_observed = _git_input(
                repo,
                ("hash-object", "--stdin-paths"),
                "".join(f"{path}\n" for path in head_paths).encode(
                    "utf-8",
                    errors="surrogateescape",
                ),
            )
            observed_oids = raw_observed.decode("ascii").splitlines()
        except (GitCommandError, UnicodeError):
            hash_error = True
    else:
        hash_error = True

    mismatches = [
        path
        for (path, _mode, expected_oid), observed_oid in zip(
            head_blobs,
            observed_oids,
            strict=False,
        )
        if observed_oid != expected_oid
    ]
    if len(observed_oids) != len(head_blobs):
        hash_error = True
    if set(index_paths) != set(head_paths):
        mismatches.extend(sorted(set(index_paths) ^ set(head_paths)))
    mismatches.extend(unsupported)
    mismatches.extend(unsafe_paths)
    mismatches = sorted(set(mismatches))
    inventory = [
        {"path": path, "mode": mode, "head_blob_oid": oid} for path, mode, oid in head_blobs
    ]
    return {
        "tracked_head_blob_match": not hash_error and not mismatches,
        "tracked_head_blob_count": len(head_blobs),
        "tracked_head_blob_inventory_sha256": hashlib.sha256(
            json.dumps(
                inventory,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8", errors="surrogateescape")
        ).hexdigest(),
        "tracked_head_blob_mismatch_paths": mismatches,
        "tracked_head_blob_hash_error": hash_error,
    }


def _index_flag_binding(repo: Path) -> dict[str, object]:
    raw = _git(repo, "ls-files", "-v", "-z")
    flagged: list[dict[str, str]] = []
    malformed = False
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            malformed = True
            continue
        tag = chr(record[0])
        if tag == "S" or tag.islower():
            flagged.append({"tag": tag, "path": _decode_path(record[2:])})
    return {
        "tracked_index_flags_clean": not malformed and not flagged,
        "tracked_index_flagged": flagged,
        "tracked_index_flags_malformed": malformed,
        "tracked_index_flags_sha256": hashlib.sha256(raw).hexdigest(),
    }


def _repository_binding_snapshot(repo: Path) -> dict[str, object]:
    commit = _git(repo, "rev-parse", "--verify", "HEAD").decode("ascii").strip()
    tree = _git(repo, "rev-parse", "--verify", "HEAD^{tree}").decode("ascii").strip()
    replace_refs = sorted(
        line
        for line in _git(
            repo,
            "for-each-ref",
            "--format=%(refname)",
            "refs/replace",
        )
        .decode("utf-8", errors="surrogateescape")
        .splitlines()
        if line
    )
    tracked_status = _git(repo, "status", "--porcelain=v1", "--untracked-files=no", "-z")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) or not re.fullmatch(
        r"[0-9a-f]{40}|[0-9a-f]{64}", tree
    ):
        raise GitCommandError("repository HEAD binding is not a canonical object ID")
    blob_binding = _head_worktree_blob_binding(repo)
    flag_binding = _index_flag_binding(repo)
    tracked_worktree_clean = (
        not tracked_status
        and not replace_refs
        and bool(blob_binding["tracked_head_blob_match"])
        and bool(flag_binding["tracked_index_flags_clean"])
    )
    return {
        "source_commit_id": commit,
        "source_tree_id": tree,
        "tracked_worktree_clean": tracked_worktree_clean,
        "tracked_status_sha256": hashlib.sha256(tracked_status).hexdigest(),
        "replace_refs_clean": not replace_refs,
        "replace_refs": replace_refs,
        **blob_binding,
        **flag_binding,
    }


@dataclass(frozen=True, slots=True)
class ScanFinding:
    category: str
    rule_id: str
    classification: FindingClassification
    source: str
    path: str
    line: int | None
    fingerprint: str
    revision: str | None = None
    metadata_kind: str | None = None
    value: str = "[REDACTED]"

    def as_report_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CheckResult:
    check_id: str
    status: CheckStatus
    evidence_label: EvidenceLabel
    summary: str
    details: dict[str, object]

    def as_report_dict(self) -> dict[str, object]:
        return asdict(self)


SECRET_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "private_key_header",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)\b"
            r"\s*[:=]\s*['\"]?([A-Za-z0-9_./+=-]{12,})"
        ),
    ),
)

PII_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "email_address",
        re.compile(r"\b[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    ("windows_user_path", re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+")),
    (
        "unix_home_path",
        re.compile(r"(?<![A-Za-z0-9_])/home/[A-Za-z0-9._-]+(?=/|\b)"),
    ),
)


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _is_synthetic_canary(path: str, rule_id: str, value: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        normalized == "tests/adversarial/test_review_regressions.py"
        and rule_id == "openai_style_key"
        and (value.startswith("sk-supersecret") or value.startswith("sk-nestedsecret"))
    )


def scan_text(
    text: str,
    *,
    path: str,
    source: str,
    revision: str | None = None,
    metadata_kind: str | None = None,
    categories: Sequence[ScanCategory] = ("secret", "pii"),
) -> list[ScanFinding]:
    """Return redacted secret and PII findings without retaining matched values."""

    findings: list[ScanFinding] = []
    rules_by_category = {
        "secret": SECRET_RULES,
        "pii": PII_RULES,
    }
    for category in categories:
        rules = rules_by_category[category]
        for rule_id, pattern in rules:
            for match in pattern.finditer(text):
                matched = match.group(1) if match.lastindex else match.group(0)
                classification: FindingClassification = (
                    "synthetic_canary"
                    if category == "secret" and _is_synthetic_canary(path, rule_id, matched)
                    else "candidate"
                )
                findings.append(
                    ScanFinding(
                        category=category,
                        rule_id=rule_id,
                        classification=classification,
                        source=source,
                        path=path,
                        line=text.count("\n", 0, match.start()) + 1,
                        fingerprint=_fingerprint(matched),
                        revision=revision,
                        metadata_kind=metadata_kind,
                    )
                )
    return findings


class GitCommandError(RuntimeError):
    pass


def _git(repo: Path, *args: str, allowed_returncodes: Sequence[int] = (0,)) -> bytes:
    completed = subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    if completed.returncode not in allowed_returncodes:
        command = "git " + " ".join(args)
        raise GitCommandError(f"{command} failed with exit code {completed.returncode}")
    return completed.stdout


def _git_input(
    repo: Path,
    args: Sequence[str],
    input_bytes: bytes,
    *,
    allowed_returncodes: Sequence[int] = (0,),
) -> bytes:
    completed = subprocess.run(
        ["git", "--no-replace-objects", *args],
        cwd=repo,
        check=False,
        capture_output=True,
        input=input_bytes,
    )
    if completed.returncode not in allowed_returncodes:
        command = "git " + " ".join(args)
        raise GitCommandError(f"{command} failed with exit code {completed.returncode}")
    return completed.stdout


def _decode_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape").replace("\\", "/")


def _tracked_paths(repo: Path) -> list[str]:
    raw = _git(repo, "ls-files", "-z")
    return sorted(_decode_path(item) for item in raw.split(b"\0") if item)


def _public_worktree_paths(repo: Path) -> list[str]:
    raw = _git(repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    return sorted(_decode_path(item) for item in raw.split(b"\0") if item)


def _safe_worktree_path(repo: Path, git_path: str) -> Path:
    relative = PurePosixPath(git_path)
    parts = relative.parts
    if (
        not parts
        or relative.is_absolute()
        or PureWindowsPath(git_path).is_absolute()
        or ".." in parts
    ):
        raise ValueError(f"tracked path escapes repository: {git_path!r}")
    candidate = repo.joinpath(*parts)
    resolved = candidate.resolve(strict=True)
    _validate_worktree_resolution(repo, candidate, resolved)
    metadata = candidate.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"worktree path is not a regular file: {git_path!r}")
    if metadata.st_nlink != 1:
        raise ValueError(f"worktree path has multiple hard links: {git_path!r}")
    return candidate


def _validate_worktree_resolution(repo: Path, candidate: Path, resolved: Path) -> None:
    """Reject repository escapes and every symlink/junction redirection."""

    try:
        resolved.relative_to(repo)
    except ValueError as exc:
        raise ValueError(f"worktree path resolves outside repository: {candidate}") from exc
    if resolved != candidate:
        raise ValueError(f"worktree path is redirected: {candidate}")


def _decode_scannable(data: bytes) -> str | None:
    if data.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        return None
    encoding = (
        "utf-16" if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)) else "utf-8-sig"
    )
    if encoding == "utf-8-sig" and b"\0" in data[:8192]:
        return None
    try:
        text = data.decode(encoding, errors="strict")
    except UnicodeDecodeError:
        return None
    return None if "\0" in text else text


def _scan_worktree(
    repo: Path, tracked_paths: Sequence[str]
) -> tuple[list[ScanFinding], list[dict[str, object]], list[str]]:
    findings: list[ScanFinding] = []
    large_files: list[dict[str, object]] = []
    skipped: list[str] = []
    for git_path in tracked_paths:
        try:
            path = _safe_worktree_path(repo, git_path)
            if not path.is_file():
                raise ValueError(f"worktree path is not a regular file: {git_path!r}")
            size = path.stat().st_size
        except (OSError, ValueError):
            skipped.append(git_path)
            continue
        if size >= LARGE_FILE_BYTES:
            large_files.append({"path": git_path, "bytes": size, "source": "worktree"})
        if size > MAX_BLOB_SCAN_BYTES:
            skipped.append(git_path)
            continue
        try:
            text = _decode_scannable(path.read_bytes())
        except OSError:
            text = None
        if text is None:
            skipped.append(git_path)
            continue
        findings.extend(scan_text(text, path=git_path, source="worktree"))
    return findings, large_files, skipped


def _history_entries(repo: Path, commits: Sequence[str]) -> list[tuple[str, str, str]]:
    entries: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for commit in commits:
        tree = _git(repo, "ls-tree", "-r", "-z", "--full-tree", commit)
        for raw_entry in tree.split(b"\0"):
            if not raw_entry or b"\t" not in raw_entry:
                continue
            metadata, raw_path = raw_entry.split(b"\t", 1)
            parts = metadata.split()
            if len(parts) != 3 or parts[1] != b"blob":
                continue
            oid = parts[2].decode("ascii")
            path = _decode_path(raw_path)
            pair = (oid, path)
            if pair not in seen:
                seen.add(pair)
                entries.append((oid, path, commit))
    return entries


def _scan_history(
    repo: Path,
) -> tuple[list[ScanFinding], list[dict[str, object]], list[str], int, bool]:
    commits = [
        line for line in _git(repo, "rev-list", "--all").decode("ascii").splitlines() if line
    ]
    complete = len(commits) <= MAX_HISTORY_COMMITS
    selected_commits = commits if complete else commits[:MAX_HISTORY_COMMITS]
    findings: list[ScanFinding] = []
    large_files: list[dict[str, object]] = []
    skipped: list[str] = []
    blob_cache: dict[str, bytes] = {}
    for oid, path, commit in _history_entries(repo, selected_commits):
        entry_label = f"{commit[:12]}:{path}"
        try:
            size = int(_git(repo, "cat-file", "-s", oid).decode("ascii").strip())
        except (GitCommandError, UnicodeDecodeError, ValueError):
            skipped.append(entry_label)
            continue
        if size >= LARGE_FILE_BYTES:
            large_files.append(
                {"path": path, "bytes": size, "source": "history", "revision": commit[:12]}
            )
        if size > MAX_BLOB_SCAN_BYTES:
            skipped.append(entry_label)
            continue
        try:
            if oid not in blob_cache:
                blob_cache[oid] = _git(repo, "cat-file", "blob", oid)
        except GitCommandError:
            skipped.append(entry_label)
            continue
        data = blob_cache[oid]
        text = _decode_scannable(data)
        if text is None:
            skipped.append(entry_label)
            continue
        findings.extend(scan_text(text, path=path, source="history", revision=commit[:12]))
    return findings, large_files, skipped, len(commits), complete


def _metadata_skip(
    *,
    source: str,
    metadata_kind: str,
    reason: str,
    revision: str | None = None,
) -> dict[str, str | None]:
    return {
        "source": source,
        "revision": revision,
        "metadata_kind": metadata_kind,
        "reason": reason,
    }


def _decode_git_object(data: bytes) -> str:
    header, separator, _message = data.partition(b"\n\n")
    if not separator:
        raise ValueError("Git object has no header/message separator")
    encoding = "utf-8"
    for line in header.splitlines():
        if line.startswith(b"encoding "):
            encoding = line.removeprefix(b"encoding ").decode("ascii", errors="strict")
            break
    return data.decode(encoding, errors="strict")


def _object_parts(data: bytes) -> tuple[list[str], str]:
    text = _decode_git_object(data)
    header, separator, message = text.partition("\n\n")
    if not separator:
        raise ValueError("Git object has no decoded header/message separator")
    return header.splitlines(), message


def _header_values(headers: Sequence[str], name: str) -> list[str]:
    prefix = name + " "
    return [line.removeprefix(prefix) for line in headers if line.startswith(prefix)]


_IDENTITY_PATTERN = re.compile(r"(.*) <([^<>]*)> -?\d+ [+-]\d{4}\Z")
_OBJECT_ID_PATTERN = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")
_GIT_OBJECT_TYPES = frozenset({"blob", "tree", "commit", "tag"})


def _parse_identity(value: str) -> tuple[str, str]:
    match = _IDENTITY_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("malformed Git identity")
    return match.group(1), match.group(2)


def _identity_findings(
    *,
    name: str,
    email: str,
    actor: str,
    source: str,
    revision: str,
) -> list[ScanFinding]:
    findings: list[ScanFinding] = []
    for field, value in (("name", name), ("email", email)):
        if not value:
            continue
        metadata_kind = f"{actor}_{field}"
        findings.append(
            ScanFinding(
                category="pii",
                rule_id=f"git_{actor}_{field}",
                classification="candidate",
                source=source,
                path="<git-identity>",
                line=None,
                fingerprint=_fingerprint(value),
                revision=revision,
                metadata_kind=metadata_kind,
            )
        )
        findings.extend(
            scan_text(
                value,
                path="<git-identity>",
                source=source,
                revision=revision,
                metadata_kind=metadata_kind,
                categories=("secret",),
            )
        )
    return findings


def _history_commits(repo: Path) -> tuple[list[str], int, bool]:
    commits = [
        line for line in _git(repo, "rev-list", "--all").decode("ascii").splitlines() if line
    ]
    complete = len(commits) <= MAX_HISTORY_COMMITS
    return commits[:MAX_HISTORY_COMMITS], len(commits), complete


def _scan_commit_metadata(
    repo: Path,
) -> tuple[list[ScanFinding], list[dict[str, str | None]], int, bool]:
    commits, commit_count, within_limit = _history_commits(repo)
    findings: list[ScanFinding] = []
    skipped: list[dict[str, str | None]] = []
    for commit in commits:
        revision = commit[:12]
        try:
            headers, message = _object_parts(_git(repo, "cat-file", "commit", commit))
        except (GitCommandError, LookupError, UnicodeDecodeError, ValueError):
            skipped.append(
                _metadata_skip(
                    source="commit_metadata",
                    metadata_kind="commit_object",
                    reason="read_parse_or_decode_failed",
                    revision=revision,
                )
            )
            continue
        for actor in ("author", "committer"):
            identities = _header_values(headers, actor)
            if len(identities) != 1:
                skipped.append(
                    _metadata_skip(
                        source="commit_metadata",
                        metadata_kind=f"{actor}_identity",
                        reason="missing_or_duplicate_header",
                        revision=revision,
                    )
                )
                continue
            try:
                name, email = _parse_identity(identities[0])
            except ValueError:
                skipped.append(
                    _metadata_skip(
                        source="commit_metadata",
                        metadata_kind=f"{actor}_identity",
                        reason="parse_failed",
                        revision=revision,
                    )
                )
                continue
            findings.extend(
                _identity_findings(
                    name=name,
                    email=email,
                    actor=actor,
                    source="commit_metadata",
                    revision=revision,
                )
            )
        findings.extend(
            scan_text(
                message,
                path="<commit-message>",
                source="commit_metadata",
                revision=revision,
                metadata_kind="commit_message",
            )
        )
    return findings, skipped, commit_count, within_limit and not skipped


def _redacted_ref_name(name: str, findings: Sequence[ScanFinding]) -> str:
    if not findings:
        return name
    return f"[REDACTED] (fingerprint={_fingerprint(name)})"


def _scan_refs(
    repo: Path,
) -> tuple[
    list[ScanFinding],
    list[dict[str, str | None]],
    list[str],
    list[str],
    list[str],
    bool,
]:
    try:
        raw = _git(
            repo,
            "for-each-ref",
            "--format=%(refname)%00%(objectname)%00%(objecttype)%00",
        )
    except GitCommandError:
        return (
            [],
            [
                _metadata_skip(
                    source="ref_metadata",
                    metadata_kind="ref_enumeration",
                    reason="enumeration_failed",
                )
            ],
            [],
            [],
            [],
            False,
        )

    findings: list[ScanFinding] = []
    skipped: list[dict[str, str | None]] = []
    safe_refs: list[str] = []
    safe_tags: list[str] = []
    annotated_tag_oids: list[str] = []
    for record in raw.splitlines():
        parts = record.split(b"\0")
        if len(parts) != 4 or parts[-1] != b"":
            skipped.append(
                _metadata_skip(
                    source="ref_metadata",
                    metadata_kind="ref_record",
                    reason="parse_failed",
                )
            )
            continue
        try:
            ref_name = parts[0].decode("utf-8", errors="strict")
            oid = parts[1].decode("ascii", errors="strict")
            object_type = parts[2].decode("ascii", errors="strict")
        except UnicodeDecodeError:
            skipped.append(
                _metadata_skip(
                    source="ref_metadata",
                    metadata_kind="ref_record",
                    reason="decode_failed",
                )
            )
            continue
        if (
            not ref_name
            or _OBJECT_ID_PATTERN.fullmatch(oid) is None
            or object_type not in _GIT_OBJECT_TYPES
        ):
            skipped.append(
                _metadata_skip(
                    source="ref_metadata",
                    metadata_kind="ref_record",
                    reason="invalid_object_identity",
                )
            )
            continue
        ref_findings = scan_text(
            ref_name,
            path="<ref-name>",
            source="ref_metadata",
            revision=oid[:12],
            metadata_kind="ref_name",
        )
        findings.extend(ref_findings)
        safe_refs.append(_redacted_ref_name(ref_name, ref_findings))
        if ref_name.startswith("refs/tags/"):
            tag_name = ref_name.removeprefix("refs/tags/")
            safe_tags.append(_redacted_ref_name(tag_name, ref_findings))
        if object_type == "tag":
            annotated_tag_oids.append(oid)
    return (
        findings,
        skipped,
        sorted(safe_refs),
        sorted(safe_tags),
        annotated_tag_oids,
        not skipped,
    )


def _scan_tag_metadata(
    repo: Path, initial_oids: Sequence[str]
) -> tuple[list[ScanFinding], list[dict[str, str | None]], bool]:
    findings: list[ScanFinding] = []
    skipped: list[dict[str, str | None]] = []
    pending = list(dict.fromkeys(initial_oids))
    visited: set[str] = set()
    while pending:
        oid = pending.pop()
        if oid in visited:
            continue
        visited.add(oid)
        revision = oid[:12]
        try:
            headers, message = _object_parts(_git(repo, "cat-file", "tag", oid))
        except (GitCommandError, LookupError, UnicodeDecodeError, ValueError):
            skipped.append(
                _metadata_skip(
                    source="tag_metadata",
                    metadata_kind="tag_object",
                    reason="read_parse_or_decode_failed",
                    revision=revision,
                )
            )
            continue

        targets = _header_values(headers, "object")
        target_types = _header_values(headers, "type")
        tag_names = _header_values(headers, "tag")
        taggers = _header_values(headers, "tagger")
        if len(targets) != 1 or len(target_types) != 1:
            skipped.append(
                _metadata_skip(
                    source="tag_metadata",
                    metadata_kind="tag_target",
                    reason="missing_or_duplicate_header",
                    revision=revision,
                )
            )
        else:
            target_oid = targets[0]
            declared_type = target_types[0]
            if (
                _OBJECT_ID_PATTERN.fullmatch(target_oid) is None
                or declared_type not in _GIT_OBJECT_TYPES
            ):
                skipped.append(
                    _metadata_skip(
                        source="tag_metadata",
                        metadata_kind="tag_target",
                        reason="invalid_target_header",
                        revision=revision,
                    )
                )
            else:
                try:
                    actual_type = (
                        _git(repo, "cat-file", "-t", target_oid)
                        .decode("ascii", errors="strict")
                        .strip()
                    )
                except (GitCommandError, UnicodeDecodeError):
                    skipped.append(
                        _metadata_skip(
                            source="tag_metadata",
                            metadata_kind="tag_target",
                            reason="target_type_read_failed",
                            revision=revision,
                        )
                    )
                else:
                    if actual_type != declared_type:
                        skipped.append(
                            _metadata_skip(
                                source="tag_metadata",
                                metadata_kind="tag_target",
                                reason="target_type_mismatch",
                                revision=revision,
                            )
                        )
                    elif actual_type == "tag":
                        pending.append(target_oid)

        if len(tag_names) != 1:
            skipped.append(
                _metadata_skip(
                    source="tag_metadata",
                    metadata_kind="tag_name",
                    reason="missing_or_duplicate_header",
                    revision=revision,
                )
            )
        else:
            findings.extend(
                scan_text(
                    tag_names[0],
                    path="<annotated-tag-name>",
                    source="tag_metadata",
                    revision=revision,
                    metadata_kind="tag_name",
                )
            )

        if len(taggers) != 1:
            skipped.append(
                _metadata_skip(
                    source="tag_metadata",
                    metadata_kind="tagger_identity",
                    reason="missing_or_duplicate_header",
                    revision=revision,
                )
            )
        else:
            try:
                name, email = _parse_identity(taggers[0])
            except ValueError:
                skipped.append(
                    _metadata_skip(
                        source="tag_metadata",
                        metadata_kind="tagger_identity",
                        reason="parse_failed",
                        revision=revision,
                    )
                )
            else:
                findings.extend(
                    _identity_findings(
                        name=name,
                        email=email,
                        actor="tagger",
                        source="tag_metadata",
                        revision=revision,
                    )
                )
        findings.extend(
            scan_text(
                message,
                path="<annotated-tag-message>",
                source="tag_metadata",
                revision=revision,
                metadata_kind="tag_message",
            )
        )
    return findings, skipped, not skipped


def _parse_locked_dependencies(repo: Path) -> list[str]:
    lock_path = repo / "requirements.lock"
    if not lock_path.is_file():
        return []
    names: list[str] = []
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "==" not in stripped:
            continue
        names.append(stripped.split("==", 1)[0])
    return names


def _dependency_license_check(repo: Path) -> CheckResult:
    dependencies = _parse_locked_dependencies(repo)
    known: dict[str, str] = {}
    unknown: list[str] = []
    for name in dependencies:
        try:
            package = importlib.metadata.metadata(name)
        except importlib.metadata.PackageNotFoundError:
            unknown.append(name)
            continue
        license_value: str | None = package["License-Expression"] or package["License"]
        if not license_value or license_value.strip().upper() == "UNKNOWN":
            classifiers = package.get_all("Classifier", [])
            license_classifiers = [
                item.removeprefix("License :: ")
                for item in classifiers
                if item.startswith("License :: ")
            ]
            license_value = "; ".join(license_classifiers)
        if license_value:
            normalized = " ".join(license_value.split())
            known[name] = normalized if len(normalized) <= 200 else normalized[:197] + "..."
        else:
            unknown.append(name)
    status: CheckStatus = "pass" if dependencies and not unknown else "unknown"
    label: EvidenceLabel = "FACT" if status == "pass" else "UNKNOWN"
    return CheckResult(
        "dependency_licenses",
        status,
        label,
        "Installed package metadata was inspected offline; unknown entries require human review.",
        {
            "locked_dependency_count": len(dependencies),
            "license_metadata_count": len(known),
            "unknown_packages": sorted(unknown),
            "license_metadata_excerpts": dict(sorted(known.items())),
        },
    )


def _ignored_probe(repo: Path, path: str) -> bool:
    completed = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    return completed.returncode == 0


def _public_document_inventory(repo: Path) -> tuple[list[str], str]:
    paths = sorted(
        {
            path.relative_to(repo).as_posix()
            for path in (*repo.glob("*.md"), *(repo / "docs").rglob("*.md"))
            if path.is_file() and path.name != "AGENTS.md"
        }
    )
    digest = hashlib.sha256(
        json.dumps(
            paths,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return paths, digest


def _capability_docs_check(repo: Path) -> CheckResult:
    required = {
        "README.md": [
            "docs/capabilities.md",
            "docs/range-equity-bridge.md",
            "LocalProvider",
            "OpenAIAgentsProvider",
            "P3-016B",
        ],
        "docs/capabilities.md": [
            "implemented",
            "disabled",
            "unavailable",
            "planned",
            "full_nlhe_equilibrium",
            "versioned_nlhe_river_equity_bridge",
            "P3-016B",
            "990",
        ],
        "docs/limitations.md": [
            "outbound analyze",
            "heads-up NLHE",
            "multiway",
            "site-specific parser",
            "OS-level",
            "P3-016B",
            "all-in",
            "990",
        ],
        "docs/range-grammar.md": [
            "poker-deliberation.nlhe-range",
            "RNG_E_PROVENANCE",
            "millionths",
            "自然言語",
            "P3-016B",
            "990",
        ],
        "docs/range-equity-bridge.md": [
            "P3-016B",
            "range_validate",
            "holdem_equity",
            "990",
            "all-in",
            "P3-030C",
        ],
        "docs/bounded-river-call-ev.md": [
            "P3-030C",
            "VersionedRangeDefinitionV1",
            "raked_call_ev",
            "Fraction",
            "LocalProvider",
            "CALCULATED",
            "UNKNOWN",
            "実Codex/Python runtime",
        ],
    }
    missing: list[str] = []
    for relative, markers in required.items():
        path = repo / relative
        if not path.is_file():
            missing.append(relative)
            continue
        text = path.read_text(encoding="utf-8")
        missing.extend(f"{relative}:{marker}" for marker in markers if marker not in text)
    document_hashes = {
        relative: hashlib.sha256((repo / relative).read_bytes()).hexdigest()
        for relative in CAPABILITY_DOCUMENT_PATHS
        if (repo / relative).is_file()
    }
    document_set_sha256 = hashlib.sha256(
        json.dumps(
            document_hashes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    if (
        set(document_hashes) != set(CAPABILITY_DOCUMENT_PATHS)
        or document_set_sha256 != CAPABILITY_DOCUMENT_SET_SHA256
    ):
        missing.append("public_capability_documents:canonical_document_set_identity")
    public_document_paths, public_document_inventory_sha256 = _public_document_inventory(repo)
    if public_document_inventory_sha256 != PUBLIC_DOCUMENT_INVENTORY_SHA256:
        missing.append("public_documents:canonical_inventory_identity")
    bridge_path = repo / "docs/range-equity-bridge.md"
    if (
        not bridge_path.is_file()
        or hashlib.sha256(bridge_path.read_bytes()).hexdigest() != RANGE_EQUITY_BRIDGE_DOC_SHA256
    ):
        missing.append("docs/range-equity-bridge.md:canonical_document_identity")
    bridge_gate_lines_expected = [
        "P3-030Cは別の専用admissionとしてこのbridgeを再利用するが、P3-016B自身のcontractや通常経路は",
        "変更しない。P3-030Cの限定統合は`docs/bounded-river-call-ev.md`に記載する。",
    ]
    bridge_gate_lines = (
        [
            line.strip()
            for line in bridge_path.read_text(encoding="utf-8").splitlines()
            if "P3-030C" in line
        ]
        if bridge_path.is_file()
        else []
    )
    if bridge_gate_lines != bridge_gate_lines_expected:
        missing.append("docs/range-equity-bridge.md:p3_030c_additive_bridge_boundary")
    roadmap_path = repo / "src/poker_deliberation/roadmap_status.json"
    try:
        roadmap = json.loads(roadmap_path.read_text(encoding="utf-8"))
        roadmap_schema = roadmap.get("schema_version") if isinstance(roadmap, dict) else None
    except (OSError, json.JSONDecodeError):
        roadmap_schema = None
    if roadmap_schema != EXPECTED_ROADMAP_SCHEMA_VERSION:
        missing.append("src/poker_deliberation/roadmap_status.json:schema_version")
    else:
        readme = repo / "README.md"
        readme_lines = (
            set(readme.read_text(encoding="utf-8").splitlines()) if readme.is_file() else set()
        )
        if not any(
            re.fullmatch(
                rf"\*\*FACT\*\*: milestone/RMの公開状態と技術契約の正は、schema "
                rf"{re.escape(roadmap_schema)}のpublic projectionである",
                line.strip(),
            )
            for line in readme_lines
        ):
            missing.append("README.md:roadmap_schema_statement")
        roadmap_doc = repo / "docs/roadmap-status.md"
        roadmap_lines = (
            {line.strip() for line in roadmap_doc.read_text(encoding="utf-8").splitlines()}
            if roadmap_doc.is_file()
            else set()
        )
        if f"- schema version: `{roadmap_schema}`" not in roadmap_lines:
            missing.append("docs/roadmap-status.md:schema_version_field")
        roadmap_module = repo / "src/poker_deliberation/roadmap.py"
        if (
            not roadmap_module.is_file()
            or hashlib.sha256(roadmap_module.read_bytes()).hexdigest() != ROADMAP_MODULE_SHA256
            or _assigned_string_constant(roadmap_module, "ROADMAP_SCHEMA_VERSION") != roadmap_schema
        ):
            missing.append("src/poker_deliberation/roadmap.py:ROADMAP_SCHEMA_VERSION")
    return CheckResult(
        "capability_documentation",
        "pass" if not missing else "fail",
        "FACT",
        "Tracked capability statements were checked for required implementation-boundary markers.",
        {
            "missing_markers": missing,
            "document_set_paths": list(CAPABILITY_DOCUMENT_PATHS),
            "document_set_sha256": document_set_sha256,
            "public_document_paths": public_document_paths,
            "public_document_inventory_sha256": public_document_inventory_sha256,
        },
    )


def _range_grammar_artifacts_check(repo: Path) -> CheckResult:
    from poker_deliberation.range_equity_evaluation import (
        load_range_equity_evaluation_fixture,
    )
    from poker_deliberation.tools.contracts import tool_contracts

    fixture_path = repo / "tests/fixtures/range/v1/cases.json"
    evaluation_path = repo / "evals/datasets/p3_016a/v1/cases.json"
    manifest_path = repo / "tools/manifest.yaml"
    bridge_fixture_path = repo / "tests/fixtures/range_equity/v1/scenarios.json"
    bridge_runner_path = repo / "scripts/run_range_equity_evaluation.py"
    failures: list[str] = []
    fixture: dict[str, object] = {}
    evaluation: dict[str, object] = {}
    for label, path in (("fixture", fixture_path), ("evaluation", evaluation_path)):
        if not path.is_file():
            failures.append(f"{label}:missing")
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failures.append(f"{label}:invalid_json")
            continue
        if not isinstance(value, dict):
            failures.append(f"{label}:not_object")
            continue
        if label == "fixture":
            fixture = value
        else:
            evaluation = value
    expected_identity = ("poker-deliberation.nlhe-range", "1.0.0")
    for label, document in (("fixture", fixture), ("evaluation", evaluation)):
        if (
            document
            and (
                document.get("grammar_id"),
                document.get("grammar_version"),
            )
            != expected_identity
        ):
            failures.append(f"{label}:grammar_identity")
        if document and document.get("license") != "MIT":
            failures.append(f"{label}:license")
    if fixture and evaluation and fixture.get("cases") != evaluation.get("cases"):
        failures.append("fixture_evaluation_case_drift")
    manifest_value: object = None
    if manifest_path.is_file():
        with suppress(OSError, yaml.YAMLError):
            manifest_value = yaml.load(
                manifest_path.read_text(encoding="utf-8"),
                Loader=_UniqueKeySafeLoader,
            )
    expected_entries = {contract.name: contract.manifest_entry() for contract in tool_contracts()}
    observed_entries = manifest_value.get("tools") if isinstance(manifest_value, dict) else None
    if (
        not isinstance(manifest_value, dict)
        or not _strict_tree_equal(manifest_value.get("schema_version"), 2)
        or manifest_value.get("canonical_source")
        != "poker_deliberation.tools.contracts.tool_contracts"
        or not isinstance(observed_entries, list)
    ):
        failures.append("tool_manifest:document")
        observed_entries = []
    for tool_name in ("range_validate", "combos", "holdem_equity"):
        matches = [
            entry
            for entry in observed_entries
            if isinstance(entry, dict) and entry.get("name") == tool_name
        ]
        if len(matches) != 1 or not _strict_tree_equal(matches[0], expected_entries[tool_name]):
            failures.append(f"tool_manifest:{tool_name}")
    if not bridge_runner_path.is_file():
        failures.append("range_equity_evaluation:runner_missing")
    elif hashlib.sha256(bridge_runner_path.read_bytes()).hexdigest() != (
        RANGE_EQUITY_EVALUATION_RUNNER_SHA256
    ):
        failures.append("range_equity_evaluation:runner_identity")
    try:
        load_range_equity_evaluation_fixture(bridge_fixture_path)
    except (OSError, ValueError):
        failures.append("range_equity_evaluation:fixture_invalid")
    return CheckResult(
        "versioned_range_grammar_artifacts",
        "pass" if not failures else "fail",
        "FACT",
        "The bounded range grammar and P3-016B evaluation fixtures, runner, license, and tool "
        "manifest identity were checked offline.",
        {"failures": failures},
    )


def _report_summary(checks: Sequence[CheckResult]) -> dict[str, int]:
    return {
        status: sum(check.status == status for check in checks)
        for status in ("pass", "review", "fail", "unknown")
    }


def run_preflight(repo: Path) -> dict[str, object]:
    repo = repo.resolve()
    if not (repo / ".git").exists():
        raise ValueError(f"not a Git repository: {repo}")

    binding_before = _repository_binding_snapshot(repo)
    tracked = _tracked_paths(repo)
    public_worktree = _public_worktree_paths(repo)
    untracked_public = sorted(set(public_worktree) - set(tracked))
    worktree_findings, worktree_large, worktree_skipped = _scan_worktree(repo, public_worktree)
    history_findings, history_large, history_skipped, commit_count, history_complete = (
        _scan_history(repo)
    )
    (
        commit_metadata_findings,
        commit_metadata_skipped,
        metadata_commit_count,
        (commit_metadata_complete),
    ) = _scan_commit_metadata(repo)
    (
        ref_findings,
        ref_skipped,
        refs,
        tags,
        annotated_tag_oids,
        ref_metadata_complete,
    ) = _scan_refs(repo)
    tag_metadata_findings, tag_metadata_skipped, tag_metadata_complete = _scan_tag_metadata(
        repo, annotated_tag_oids
    )
    metadata_skipped = [
        *commit_metadata_skipped,
        *ref_skipped,
        *tag_metadata_skipped,
    ]
    if metadata_commit_count != commit_count:
        metadata_skipped.append(
            _metadata_skip(
                source="commit_metadata",
                metadata_kind="commit_enumeration",
                reason="history_changed_during_scan",
            )
        )
        commit_metadata_complete = False
    metadata_complete = commit_metadata_complete and ref_metadata_complete and tag_metadata_complete
    all_findings = [
        *worktree_findings,
        *history_findings,
        *commit_metadata_findings,
        *ref_findings,
        *tag_metadata_findings,
    ]
    secret_candidates = [
        item
        for item in all_findings
        if item.category == "secret" and item.classification == "candidate"
    ]
    synthetic_canaries = [
        item
        for item in all_findings
        if item.category == "secret" and item.classification == "synthetic_canary"
    ]
    pii_candidates = [item for item in all_findings if item.category == "pii"]
    root_license = repo / "LICENSE"
    license_ok = root_license.is_file() and "MIT License" in root_license.read_text(
        encoding="utf-8"
    )
    tracked_user_materials = [path for path in tracked if path.startswith("user_materials/")]
    tracked_runs = [path for path in tracked if path.startswith("runs/")]
    user_materials_ok = tracked_user_materials == [
        "user_materials/.gitignore",
        "user_materials/README.md",
    ] and _ignored_probe(repo, "user_materials/__public_preflight_probe__.txt")
    runs_ok = tracked_runs == ["runs/.gitkeep"] and _ignored_probe(
        repo, "runs/__public_preflight_probe__.json"
    )
    pytest_ignored = _ignored_probe(repo, ".pytest-tmp/__public_preflight_probe__")
    forbidden_release_prefixes = (
        ".pytest-tmp/",
        ".venv/",
        "dist/",
        "build/",
    )
    tracked_ignored_artifacts = [
        path for path in public_worktree if path.startswith(forbidden_release_prefixes)
    ]
    example_paths = [path for path in tracked if path.startswith("examples/")]
    example_pii = [
        item for item in worktree_findings if item.path in example_paths and item.category == "pii"
    ]
    workflow_paths = [path for path in tracked if path.startswith(".github/workflows/")]
    binding_after = _repository_binding_snapshot(repo)
    binding_stable = binding_before == binding_after
    tracked_worktree_clean = bool(binding_before["tracked_worktree_clean"]) and bool(
        binding_after["tracked_worktree_clean"]
    )

    scan_complete = (
        history_complete and not worktree_skipped and not history_skipped and metadata_complete
    )
    scan_skipped: list[object] = [
        *worktree_skipped,
        *history_skipped,
        *metadata_skipped,
    ]
    secret_status: CheckStatus
    if not scan_complete:
        secret_status = "unknown"
    elif secret_candidates:
        secret_status = "review"
    else:
        secret_status = "pass"
    secret_label: EvidenceLabel = "UNKNOWN" if secret_status == "unknown" else "FACT"
    pii_status: CheckStatus
    if not scan_complete:
        pii_status = "unknown"
    elif pii_candidates:
        pii_status = "review"
    else:
        pii_status = "pass"
    pii_label: EvidenceLabel = "UNKNOWN" if pii_status == "unknown" else "FACT"

    checks: list[CheckResult] = [
        CheckResult(
            "fixed_source_repository_binding",
            "pass" if binding_stable and tracked_worktree_clean else "fail",
            "FACT",
            "HEAD commit/tree and tracked-worktree state were captured before and after the scan.",
            {
                "before": binding_before,
                "after": binding_after,
                "stable": binding_stable,
                "tracked_worktree_clean": tracked_worktree_clean,
            },
        ),
        CheckResult(
            "tracked_and_history_secret_scan",
            secret_status,
            secret_label,
            "Tracked worktree and Git history were scanned offline; matched values are redacted.",
            {
                "candidate_count": len(secret_candidates),
                "synthetic_canary_count": len(synthetic_canaries),
                "candidates": [item.as_report_dict() for item in secret_candidates],
                "synthetic_canaries": [item.as_report_dict() for item in synthetic_canaries],
                "skipped": scan_skipped,
                "history_complete": history_complete,
                "metadata_complete": metadata_complete,
                "commit_metadata_complete": commit_metadata_complete,
                "ref_metadata_complete": ref_metadata_complete,
                "tag_metadata_complete": tag_metadata_complete,
                "max_blob_scan_bytes": MAX_BLOB_SCAN_BYTES,
            },
        ),
        CheckResult(
            "tracked_and_history_pii_scan",
            pii_status,
            pii_label,
            "Pattern matches and Git author/committer/tagger identities are candidates, "
            "not confirmed personal data.",
            {
                "candidate_count": len(pii_candidates),
                "candidates": [item.as_report_dict() for item in pii_candidates],
                "skipped": scan_skipped,
                "history_complete": history_complete,
                "metadata_complete": metadata_complete,
                "commit_metadata_complete": commit_metadata_complete,
                "ref_metadata_complete": ref_metadata_complete,
                "tag_metadata_complete": tag_metadata_complete,
                "max_blob_scan_bytes": MAX_BLOB_SCAN_BYTES,
                "ignored_user_data_scanned": False,
            },
        ),
        CheckResult(
            "root_license",
            "pass" if license_ok else "fail",
            "FACT",
            "Root LICENSE presence and MIT marker were checked.",
            {"tracked": "LICENSE" in tracked, "mit_marker": license_ok},
        ),
        _dependency_license_check(repo),
        CheckResult(
            "git_history_tags_and_large_files",
            "pass"
            if history_complete and metadata_complete and not worktree_large and not history_large
            else "review",
            "FACT" if history_complete and metadata_complete else "UNKNOWN",
            "Git history, tags, and files at or above the declared large-file threshold "
            "were inspected.",
            {
                "commit_count": commit_count,
                "tag_count": len(tags),
                "tags": tags,
                "ref_count": len(refs),
                "refs": refs,
                "large_file_threshold_bytes": LARGE_FILE_BYTES,
                "large_files": [*worktree_large, *history_large],
                "history_complete": history_complete,
                "metadata_complete": metadata_complete,
                "metadata_skipped": metadata_skipped,
            },
        ),
        CheckResult(
            "github_workflows_and_actions_logs",
            "unknown",
            "UNKNOWN",
            "Tracked workflow files are locally visible; remote Actions runs and logs "
            "were not queried.",
            {"tracked_workflows": workflow_paths, "remote_actions_logs_checked": False},
        ),
        CheckResult(
            "examples_anonymity_and_scope",
            "review" if example_pii else "pass",
            "FACT",
            "Only tracked examples were scanned for supported PII patterns; semantic "
            "anonymity still needs human review.",
            {
                "tracked_example_count": len(example_paths),
                "pii_candidate_count": len(example_pii),
                "human_semantic_review_required": True,
            },
        ),
        CheckResult(
            "user_materials_exclusion",
            "pass" if user_materials_ok else "fail",
            "FACT",
            "Only the tracked policy files were inspected; ignored user_materials contents "
            "were not enumerated or read.",
            {
                "tracked_paths": tracked_user_materials,
                "ignored_user_data_scanned": False,
                "probe_ignored": user_materials_ok,
            },
        ),
        CheckResult(
            "run_artifact_exclusion",
            "pass" if runs_ok else "fail",
            "FACT",
            "Tracked run paths and the ignore rule were checked without enumerating ignored runs.",
            {"tracked_paths": tracked_runs, "probe_ignored": runs_ok},
        ),
        _capability_docs_check(repo),
        _range_grammar_artifacts_check(repo),
        CheckResult(
            "tracked_release_candidates",
            "pass" if not tracked_ignored_artifacts and pytest_ignored else "fail",
            "FACT",
            "Tracked source candidates exclude known ignored runtime, temp, build, and "
            "user-data paths.",
            {
                "tracked_ignored_artifacts": tracked_ignored_artifacts,
                "pytest_temp_probe_ignored": pytest_ignored,
            },
        ),
        CheckResult(
            "untracked_public_candidates",
            "review" if untracked_public else "pass",
            "FACT",
            "Non-ignored untracked paths were listed without staging or committing them.",
            {"paths": untracked_public},
        ),
        CheckResult(
            "built_package_contents",
            "unknown",
            "UNKNOWN",
            "No wheel or sdist was built in this preflight, so archive contents were not asserted.",
            {"package_build_executed": False},
        ),
    ]
    return {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": ".",
        "repository_binding": {
            **binding_after,
            "stable_during_scan": binding_stable,
        },
        "offline": True,
        "external_operations": [],
        "scope": {
            "tracked_worktree": True,
            "untracked_non_ignored_worktree": True,
            "git_history": True,
            "ignored_user_materials": False,
            "ignored_runs": False,
        },
        "summary": _report_summary(checks),
        "checks": [check.as_report_dict() for check in checks],
        "publication_decision": "human_review_required",
    }


def render_preflight_markdown(report: dict[str, object]) -> str:
    lines = [
        "# Offline public preflight",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Repository: `{report['repository']}`",
        "- Repository binding: `"
        + json.dumps(report.get("repository_binding", {}), sort_keys=True)
        + "`",
        "- External operations: none",
        "- Publication decision: `human_review_required`",
        "",
    ]
    checks = report.get("checks", [])
    if isinstance(checks, list):
        for raw_check in checks:
            if not isinstance(raw_check, dict):
                continue
            lines.extend(
                [
                    f"## `{raw_check.get('check_id', 'unknown')}`",
                    "",
                    f"- Status: `{raw_check.get('status', 'unknown')}`",
                    f"- Evidence: `{raw_check.get('evidence_label', 'UNKNOWN')}`",
                    f"- Summary: {raw_check.get('summary', '')}",
                    "- Details:",
                    "",
                    "```json",
                    json.dumps(
                        raw_check.get("details", {}),
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                    "",
                ]
            )
    return "\n".join(lines)


def _write_new(path: Path, content: str) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing preflight report: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _validated_output_path(repo: Path, output: Path) -> Path:
    resolved = output.resolve()
    if resolved != repo and repo not in resolved.parents:
        raise ValueError("preflight output must remain inside the repository workspace")
    return resolved


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = run_preflight(args.repo)
    content = (
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
        if args.format == "json"
        else render_preflight_markdown(report) + "\n"
    )
    if args.output is None:
        print(content, end="")
    else:
        output = _validated_output_path(args.repo.resolve(), args.output)
        _write_new(output, content)
        print(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
