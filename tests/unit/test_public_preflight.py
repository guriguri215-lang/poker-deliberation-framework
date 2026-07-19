from __future__ import annotations

import json
from pathlib import Path

import pytest

from poker_deliberation import public_preflight
from poker_deliberation.public_preflight import (
    GitCommandError,
    _decode_scannable,
    _identity_findings,
    _safe_worktree_path,
    _scan_history,
    _scan_tag_metadata,
    _scan_worktree,
    _validate_worktree_resolution,
    _validated_output_path,
    scan_text,
)


def test_secret_candidate_is_redacted_and_fingerprinted() -> None:
    synthetic_value = "sk-" + "fixturecandidate123456789"
    findings = scan_text(
        f"token={synthetic_value}",
        path="src/example.py",
        source="worktree",
    )

    secret = next(item for item in findings if item.category == "secret")
    serialized = json.dumps(secret.as_report_dict())
    assert secret.classification == "candidate"
    assert secret.value == "[REDACTED]"
    assert synthetic_value not in serialized
    assert len(secret.fingerprint) == 12


def test_known_security_fixture_is_classified_as_synthetic_canary() -> None:
    synthetic_value = "sk-" + "supersecret123456789"
    findings = scan_text(
        f'canary = "{synthetic_value}"',
        path="tests/adversarial/test_review_regressions.py",
        source="worktree",
    )

    secret = next(item for item in findings if item.category == "secret")
    assert secret.classification == "synthetic_canary"


def test_placeholders_and_empty_assignments_are_not_secret_candidates() -> None:
    text = "OPENAI_API_KEY=\nexample=sk-<REDACTED>\npassword=<set-me>"
    findings = scan_text(text, path=".env.example", source="worktree")
    assert not [item for item in findings if item.category == "secret"]


def test_scanner_pattern_text_is_not_a_unix_home_path_candidate() -> None:
    pattern_documentation = r"unix pattern: /home/[^/\s]+"
    findings = scan_text(pattern_documentation, path="scanner.py", source="worktree")
    assert not [item for item in findings if item.rule_id == "unix_home_path"]


def test_pii_candidate_is_redacted() -> None:
    email = "person" + "@" + "example.com"
    findings = scan_text(email, path="example.txt", source="worktree")
    pii = next(item for item in findings if item.category == "pii")
    assert pii.rule_id == "email_address"
    assert email not in json.dumps(pii.as_report_dict())


def test_git_identity_email_is_not_duplicated_by_general_pii_scan() -> None:
    email = "identity-person" + "@" + "example.invalid"
    findings = _identity_findings(
        name="Benign Identity",
        email=email,
        actor="author",
        source="commit_metadata",
        revision="a" * 12,
    )

    assert sum(item.rule_id == "git_author_email" for item in findings) == 1
    assert not [item for item in findings if item.rule_id == "email_address"]
    assert email not in json.dumps([item.as_report_dict() for item in findings])


def test_preflight_output_cannot_escape_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _validated_output_path(repo, repo / "report.json") == repo / "report.json"
    with pytest.raises(ValueError, match="inside the repository"):
        _validated_output_path(repo, tmp_path / "outside.json")


def test_safe_worktree_path_allows_only_an_unredirected_repository_file(
    tmp_path: Path,
) -> None:
    repo = (tmp_path / "repo").resolve()
    path = repo / "src" / "normal.txt"
    path.parent.mkdir(parents=True)
    path.write_text("normal", encoding="utf-8")

    assert _safe_worktree_path(repo, "src/normal.txt") == path
    with pytest.raises(ValueError, match="escapes repository"):
        _safe_worktree_path(repo, "../outside.txt")
    with pytest.raises(ValueError, match="escapes repository"):
        _safe_worktree_path(repo, str(path.resolve()))


@pytest.mark.parametrize(
    ("candidate", "resolved"),
    [
        ("linked-file.txt", "actual-file.txt"),
        ("linked-parent/file.txt", "actual-parent/file.txt"),
        ("tracked/file.txt", ".ignored/file.txt"),
    ],
    ids=("final-symlink", "parent-junction-or-symlink", "ignored-target"),
)
def test_worktree_resolution_rejects_every_redirect(
    tmp_path: Path,
    candidate: str,
    resolved: str,
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    with pytest.raises(ValueError, match="redirected"):
        _validate_worktree_resolution(repo, repo / candidate, repo / resolved)


def test_worktree_resolution_rejects_repository_escape(tmp_path: Path) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    with pytest.raises(ValueError, match="outside repository"):
        _validate_worktree_resolution(repo, repo / "tracked.txt", tmp_path / "outside.txt")


def test_rejected_worktree_path_is_skipped_without_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()

    def reject(_repo: Path, _git_path: str) -> Path:
        raise ValueError("redirected fixture")

    monkeypatch.setattr(public_preflight, "_safe_worktree_path", reject)
    findings, large_files, skipped = _scan_worktree(repo, ["linked/file.txt"])

    assert findings == []
    assert large_files == []
    assert skipped == ["linked/file.txt"]


def test_scannable_decode_supports_utf8_and_bom_utf16_but_rejects_binary() -> None:
    assert _decode_scannable(b"plain text") == "plain text"
    assert _decode_scannable("person@example.invalid".encode("utf-16")) == (
        "person@example.invalid"
    )
    assert _decode_scannable(b"binary\0payload") is None
    assert _decode_scannable(b"\xff\xfe\x00\x00unsupported utf32") is None


def test_unreadable_history_blob_is_reported_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commit = "c" * 40
    oid = "d" * 40

    def fake_git(_repo: Path, *args: str, **_kwargs: object) -> bytes:
        if args == ("rev-list", "--all"):
            return f"{commit}\n".encode()
        if args == ("ls-tree", "-r", "-z", "--full-tree", commit):
            return f"100644 blob {oid}\tunreadable.txt\0".encode()
        if args == ("cat-file", "-s", oid):
            return b"10\n"
        if args == ("cat-file", "blob", oid):
            raise GitCommandError("fixture read failure")
        raise AssertionError(args)

    monkeypatch.setattr(public_preflight, "_git", fake_git)
    findings, large_files, skipped, commit_count, history_complete = _scan_history(tmp_path)

    assert findings == []
    assert large_files == []
    assert skipped == [f"{commit[:12]}:unreadable.txt"]
    assert commit_count == 1
    assert history_complete is True


def test_tag_target_failure_keeps_findings_and_marks_metadata_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tag_oid = "a" * 40
    target_oid = "b" * 40
    message_secret = "sk-" + "tagtargetfixture123456789"
    tagger_email = "tagger-target" + "@" + "example.invalid"
    tag_object = (
        f"object {target_oid}\n"
        "type commit\n"
        "tag safe-name\n"
        f"tagger Synthetic Tagger <{tagger_email}> 1 +0000\n\n"
        f"message {message_secret}\n"
    ).encode()

    def fake_git(_repo: Path, *args: str, **_kwargs: object) -> bytes:
        if args == ("cat-file", "tag", tag_oid):
            return tag_object
        if args == ("cat-file", "-t", target_oid):
            raise GitCommandError("synthetic target failure")
        raise AssertionError(args)

    monkeypatch.setattr(public_preflight, "_git", fake_git)
    findings, skipped, complete = _scan_tag_metadata(tmp_path, [tag_oid])

    assert complete is False
    assert any(item.metadata_kind == "tag_message" for item in findings)
    assert any(item.rule_id == "git_tagger_email" for item in findings)
    assert any(item["metadata_kind"] == "tag_target" for item in skipped)
    serialized = json.dumps(
        {
            "findings": [item.as_report_dict() for item in findings],
            "skipped": skipped,
        }
    )
    assert message_secret not in serialized
    assert tagger_email not in serialized


def test_nested_tag_objects_are_visited_once_without_looping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer_oid = "c" * 40
    inner_oid = "d" * 40
    commit_oid = "e" * 40
    outer_email = "outer" + "@" + "example.invalid"
    inner_email = "inner" + "@" + "example.invalid"
    objects = {
        outer_oid: (
            f"object {inner_oid}\n"
            "type tag\n"
            "tag outer\n"
            f"tagger Outer Fixture <{outer_email}> 1 +0000\n\nouter\n"
        ).encode(),
        inner_oid: (
            f"object {commit_oid}\n"
            "type commit\n"
            "tag inner\n"
            f"tagger Inner Fixture <{inner_email}> 1 +0000\n\ninner\n"
        ).encode(),
    }
    reads: list[str] = []

    def fake_git(_repo: Path, *args: str, **_kwargs: object) -> bytes:
        if args[:2] == ("cat-file", "tag"):
            oid = args[2]
            reads.append(oid)
            return objects[oid]
        if args == ("cat-file", "-t", inner_oid):
            return b"tag\n"
        if args == ("cat-file", "-t", commit_oid):
            return b"commit\n"
        raise AssertionError(args)

    monkeypatch.setattr(public_preflight, "_git", fake_git)
    findings, skipped, complete = _scan_tag_metadata(tmp_path, [outer_oid, outer_oid])

    assert complete is True
    assert skipped == []
    assert sorted(reads) == sorted([outer_oid, inner_oid])
    assert sum(item.rule_id == "git_tagger_email" for item in findings) == 2
