from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from poker_deliberation import public_preflight
from poker_deliberation.public_preflight import (
    GitCommandError,
    render_preflight_markdown,
    run_preflight,
)


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**os.environ, **(env or {})},
    )


def _check(report: dict[str, object], check_id: str) -> dict[str, object]:
    checks = report["checks"]
    assert isinstance(checks, list)
    return next(
        item for item in checks if isinstance(item, dict) and item.get("check_id") == check_id
    )


def test_preflight_scans_removed_history_but_not_ignored_content(tmp_path: Path) -> None:
    repo = tmp_path / "fixture-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Fixture Author")
    _git(repo, "config", "user.email", "fixture" + "@" + "example.invalid")

    (repo / ".gitignore").write_text(
        "private/\nuser_materials/*\n!user_materials/.gitignore\n"
        "!user_materials/README.md\nruns/*\n!runs/.gitkeep\n.pytest-tmp/\n",
        encoding="utf-8",
    )
    (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    (repo / "requirements.lock").write_text("", encoding="utf-8")
    utf16_email = "utf16-person" + "@" + "example.invalid"
    (repo / "utf16.txt").write_bytes(utf16_email.encode("utf-16"))
    (repo / "unsupported.bin").write_bytes(b"binary\0payload")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial fixture")

    ignored_secret = "sk-" + "ignoredfixture123456789"
    ignored_path = repo / "private" / "local.txt"
    ignored_path.parent.mkdir()
    ignored_path.write_text(ignored_secret, encoding="utf-8")

    history_secret = "sk-" + "historyfixture123456789"
    leaked_path = repo / "removed-secret.txt"
    leaked_path.write_text(history_secret, encoding="utf-8")
    _git(repo, "add", "removed-secret.txt")
    _git(repo, "commit", "-m", "seed history boundary")
    leaked_path.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "remove history fixture")

    report = run_preflight(repo)
    assert report["repository"] == "."
    secret_check = _check(report, "tracked_and_history_secret_scan")
    assert secret_check["status"] == "unknown"
    assert secret_check["evidence_label"] == "UNKNOWN"
    details = secret_check["details"]
    assert isinstance(details, dict)
    candidates = details["candidates"]
    assert isinstance(candidates, list)
    assert any(
        item.get("path") == "removed-secret.txt" and item.get("source") == "history"
        for item in candidates
        if isinstance(item, dict)
    )
    serialized = str(report)
    assert history_secret not in serialized
    assert ignored_secret not in serialized
    assert "private/local.txt" not in serialized
    assert utf16_email not in serialized

    pii_check = _check(report, "tracked_and_history_pii_scan")
    assert pii_check["status"] == "unknown"
    assert pii_check["evidence_label"] == "UNKNOWN"
    pii_details = pii_check["details"]
    assert isinstance(pii_details, dict)
    pii_candidates = pii_details["candidates"]
    assert isinstance(pii_candidates, list)
    assert any(
        item.get("path") == "utf16.txt" and item.get("rule_id") == "email_address"
        for item in pii_candidates
        if isinstance(item, dict)
    )
    skipped = pii_details["skipped"]
    assert isinstance(skipped, list)
    assert "unsupported.bin" in skipped
    assert any(str(item).endswith(":unsupported.bin") for item in skipped)
    assert report["scope"] == {
        "tracked_worktree": True,
        "untracked_non_ignored_worktree": True,
        "git_history": True,
        "ignored_user_materials": False,
        "ignored_runs": False,
    }
    inventory = _check(report, "git_history_tags_and_large_files")
    inventory_details = inventory["details"]
    assert isinstance(inventory_details, dict)
    assert inventory_details["tag_count"] == 0
    assert inventory_details["tags"] == []


def _create_metadata_fixture(repo: Path) -> dict[str, str]:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Fallback Fixture")
    _git(repo, "config", "user.email", "fallback" + "@" + "example.invalid")
    (repo / ".gitignore").write_text(
        "user_materials/*\n!user_materials/.gitignore\n!user_materials/README.md\n"
        "runs/*\n!runs/.gitkeep\n.pytest-tmp/\n",
        encoding="utf-8",
    )
    (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (repo / "README.md").write_text("metadata fixture\n", encoding="utf-8")
    (repo / "requirements.lock").write_text("", encoding="utf-8")

    values = {
        "commit_secret": "sk-" + "commitfixturecandidate123456789",
        "author_name": "Synthetic " + "Author Candidate",
        "author_email": "author-fixture" + "@" + "example.invalid",
        "committer_name": "Synthetic " + "Committer Candidate",
        "committer_email": "committer-fixture" + "@" + "example.invalid",
        "tag_secret": "sk-" + "tagfixturecandidate123456789",
        "tagger_name": "Synthetic " + "Tagger Candidate",
        "tagger_email": "tagger-fixture" + "@" + "example.invalid",
        "ref_secret": "sk-" + "reffixturecandidate123456789",
        "ref_email": "ref-fixture" + "@" + "example.invalid",
    }
    _git(repo, "add", ".")
    _git(
        repo,
        "commit",
        "-m",
        "metadata message " + values["commit_secret"],
        env={
            "GIT_AUTHOR_NAME": values["author_name"],
            "GIT_AUTHOR_EMAIL": values["author_email"],
            "GIT_COMMITTER_NAME": values["committer_name"],
            "GIT_COMMITTER_EMAIL": values["committer_email"],
        },
    )
    _git(
        repo,
        "tag",
        "-a",
        "release/" + values["ref_secret"],
        "-m",
        "annotated metadata " + values["tag_secret"],
        env={
            "GIT_COMMITTER_NAME": values["tagger_name"],
            "GIT_COMMITTER_EMAIL": values["tagger_email"],
        },
    )
    _git(repo, "tag", "v0.0.1")
    _git(repo, "branch", "review/" + values["ref_email"])
    return values


def _create_identity_fixture(
    repo: Path,
    *,
    identity_field: str | None = None,
    identity_value: str | None = None,
) -> None:
    repo.mkdir()
    _git(repo, "init")
    (repo / ".gitignore").write_text(
        "user_materials/*\n!user_materials/.gitignore\n!user_materials/README.md\n"
        "runs/*\n!runs/.gitkeep\n.pytest-tmp/\n",
        encoding="utf-8",
    )
    (repo / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (repo / "README.md").write_text("identity fixture\n", encoding="utf-8")
    (repo / "requirements.lock").write_text("", encoding="utf-8")

    identities = {
        "author_name": "Benign Author Fixture",
        "author_email": "author-benign" + "@" + "example.invalid",
        "committer_name": "Benign Committer Fixture",
        "committer_email": "committer-benign" + "@" + "example.invalid",
        "tagger_name": "Benign Tagger Fixture",
        "tagger_email": "tagger-benign" + "@" + "example.invalid",
    }
    if identity_field is not None:
        assert identity_value is not None
        identities[identity_field] = identity_value

    _git(repo, "add", ".")
    _git(
        repo,
        "commit",
        "-m",
        "benign identity fixture commit",
        env={
            "GIT_AUTHOR_NAME": identities["author_name"],
            "GIT_AUTHOR_EMAIL": identities["author_email"],
            "GIT_COMMITTER_NAME": identities["committer_name"],
            "GIT_COMMITTER_EMAIL": identities["committer_email"],
        },
    )
    _git(
        repo,
        "tag",
        "-a",
        "identity-fixture",
        "-m",
        "benign identity fixture tag",
        env={
            "GIT_COMMITTER_NAME": identities["tagger_name"],
            "GIT_COMMITTER_EMAIL": identities["tagger_email"],
        },
    )


@pytest.mark.parametrize(
    ("identity_field", "expected_source"),
    [
        ("author_name", "commit_metadata"),
        ("author_email", "commit_metadata"),
        ("committer_name", "commit_metadata"),
        ("committer_email", "commit_metadata"),
        ("tagger_name", "tag_metadata"),
        ("tagger_email", "tag_metadata"),
    ],
)
def test_preflight_scans_secret_patterns_in_git_identity_fields(
    tmp_path: Path,
    identity_field: str,
    expected_source: str,
) -> None:
    synthetic_value = "sk-" + "identityfixturecandidate123456789"
    repo = tmp_path / f"identity-{identity_field}"
    _create_identity_fixture(
        repo,
        identity_field=identity_field,
        identity_value=synthetic_value,
    )

    report = run_preflight(repo)
    secret_check = _check(report, "tracked_and_history_secret_scan")
    pii_check = _check(report, "tracked_and_history_pii_scan")
    assert secret_check["status"] == "review"
    assert secret_check["evidence_label"] == "FACT"
    assert pii_check["status"] == "review"
    assert pii_check["evidence_label"] == "FACT"

    secret_details = secret_check["details"]
    pii_details = pii_check["details"]
    assert isinstance(secret_details, dict)
    assert isinstance(pii_details, dict)
    assert secret_details["metadata_complete"] is True
    assert pii_details["metadata_complete"] is True
    assert secret_details["skipped"] == []
    assert pii_details["skipped"] == []

    secret_candidates = secret_details["candidates"]
    pii_candidates = pii_details["candidates"]
    assert isinstance(secret_candidates, list)
    assert isinstance(pii_candidates, list)
    assert secret_details["candidate_count"] == 1
    assert any(
        item["rule_id"] == "openai_style_key"
        and item["source"] == expected_source
        and item["metadata_kind"] == identity_field
        and item["path"] == "<git-identity>"
        for item in secret_candidates
    )
    assert any(
        item["rule_id"] == f"git_{identity_field}"
        and item["source"] == expected_source
        and item["metadata_kind"] == identity_field
        for item in pii_candidates
    )

    all_candidates = [*secret_candidates, *pii_candidates]
    assert all(item["value"] == "[REDACTED]" for item in all_candidates)
    assert all(
        len(item["fingerprint"]) == 12 and set(item["fingerprint"]) <= set("0123456789abcdef")
        for item in all_candidates
    )
    serialized_json = json.dumps(report, ensure_ascii=False)
    serialized_markdown = render_preflight_markdown(report)
    assert synthetic_value not in serialized_json
    assert synthetic_value not in serialized_markdown


def test_benign_git_identities_do_not_add_secret_candidates(tmp_path: Path) -> None:
    repo = tmp_path / "benign-identities"
    _create_identity_fixture(repo)

    report = run_preflight(repo)
    secret_check = _check(report, "tracked_and_history_secret_scan")
    secret_details = secret_check["details"]
    assert isinstance(secret_details, dict)
    assert secret_check["status"] == "pass"
    assert secret_check["evidence_label"] == "FACT"
    assert secret_details["candidate_count"] == 0
    assert secret_details["metadata_complete"] is True
    assert secret_details["skipped"] == []


def test_preflight_scans_and_redacts_commit_tag_and_ref_metadata(tmp_path: Path) -> None:
    repo = tmp_path / "metadata-repo"
    values = _create_metadata_fixture(repo)

    report = run_preflight(repo)
    secret_check = _check(report, "tracked_and_history_secret_scan")
    pii_check = _check(report, "tracked_and_history_pii_scan")
    assert secret_check["status"] == "review"
    assert secret_check["evidence_label"] == "FACT"
    assert pii_check["status"] == "review"
    assert pii_check["evidence_label"] == "FACT"

    secret_details = secret_check["details"]
    pii_details = pii_check["details"]
    assert isinstance(secret_details, dict)
    assert isinstance(pii_details, dict)
    assert secret_details["metadata_complete"] is True
    assert pii_details["metadata_complete"] is True
    assert secret_details["skipped"] == []
    secret_candidates = secret_details["candidates"]
    pii_candidates = pii_details["candidates"]
    assert isinstance(secret_candidates, list)
    assert isinstance(pii_candidates, list)
    assert {item["metadata_kind"] for item in secret_candidates} >= {
        "commit_message",
        "tag_message",
        "tag_name",
        "ref_name",
    }
    assert {item["rule_id"] for item in pii_candidates} >= {
        "git_author_name",
        "git_author_email",
        "git_committer_name",
        "git_committer_email",
        "git_tagger_name",
        "git_tagger_email",
        "email_address",
    }

    inventory = _check(report, "git_history_tags_and_large_files")
    inventory_details = inventory["details"]
    assert isinstance(inventory_details, dict)
    tags = inventory_details["tags"]
    refs = inventory_details["refs"]
    assert isinstance(tags, list)
    assert isinstance(refs, list)
    assert "v0.0.1" in tags
    assert any(str(item).startswith("[REDACTED]") for item in tags)
    assert any(str(item).startswith("[REDACTED]") for item in refs)

    serialized_json = json.dumps(report, ensure_ascii=False)
    serialized_markdown = render_preflight_markdown(report)
    for value in values.values():
        assert value not in serialized_json
        assert value not in serialized_markdown
    for check in (secret_check, pii_check):
        details = check["details"]
        assert isinstance(details, dict)
        candidates = details["candidates"]
        assert isinstance(candidates, list)
        assert all(item["value"] == "[REDACTED]" for item in candidates)


@pytest.mark.parametrize(
    ("failure", "expected_source"),
    [
        ("commit", "commit_metadata"),
        ("tag", "tag_metadata"),
        ("ref", "ref_metadata"),
    ],
)
def test_incomplete_metadata_scan_keeps_secret_and_pii_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_source: str,
) -> None:
    repo = tmp_path / f"incomplete-{failure}"
    _create_metadata_fixture(repo)
    real_git = public_preflight._git

    def failing_git(git_repo: Path, *args: str, **kwargs: object) -> bytes:
        if failure == "commit" and args[:2] == ("cat-file", "commit"):
            raise GitCommandError("synthetic commit read failure")
        if failure == "tag" and args[:2] == ("cat-file", "tag"):
            return b"object invalid\n\n\xff"
        if failure == "ref" and args and args[0] == "for-each-ref":
            raise GitCommandError("synthetic ref enumeration failure")
        return real_git(git_repo, *args, **kwargs)

    monkeypatch.setattr(public_preflight, "_git", failing_git)
    report = run_preflight(repo)

    for check_id in ("tracked_and_history_secret_scan", "tracked_and_history_pii_scan"):
        check = _check(report, check_id)
        assert check["status"] == "unknown"
        assert check["evidence_label"] == "UNKNOWN"
        details = check["details"]
        assert isinstance(details, dict)
        assert details["metadata_complete"] is False
        skipped = details["skipped"]
        assert isinstance(skipped, list)
        assert any(
            isinstance(item, dict) and item.get("source") == expected_source for item in skipped
        )
