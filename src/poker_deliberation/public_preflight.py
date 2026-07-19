"""Offline, redacted public-release preflight for the tracked repository surface."""

from __future__ import annotations

import argparse
import codecs
import hashlib
import importlib.metadata
import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal

CheckStatus = Literal["pass", "review", "fail", "unknown"]
EvidenceLabel = Literal["FACT", "UNKNOWN"]
FindingClassification = Literal["candidate", "synthetic_canary"]
ScanCategory = Literal["secret", "pii"]

MAX_HISTORY_COMMITS = 10_000
MAX_BLOB_SCAN_BYTES = 2_000_000
LARGE_FILE_BYTES = 1_000_000


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
        ["git", *args],
        cwd=repo,
        check=False,
        capture_output=True,
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


def _capability_docs_check(repo: Path) -> CheckResult:
    required = {
        "README.md": ["docs/capabilities.md", "LocalProvider", "OpenAIAgentsProvider"],
        "docs/capabilities.md": [
            "implemented",
            "disabled",
            "unavailable",
            "planned",
            "full_nlhe_equilibrium",
        ],
        "docs/limitations.md": [
            "outbound analyze",
            "heads-up NLHE",
            "multiway",
            "site-specific parser",
            "OS-level",
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
    return CheckResult(
        "capability_documentation",
        "pass" if not missing else "fail",
        "FACT",
        "Tracked capability statements were checked for required implementation-boundary markers.",
        {"missing_markers": missing},
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
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "repository": ".",
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
