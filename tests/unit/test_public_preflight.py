from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from poker_deliberation import public_preflight
from poker_deliberation.public_preflight import (
    GitCommandError,
    _capability_docs_check,
    _decode_scannable,
    _identity_findings,
    _range_grammar_artifacts_check,
    _safe_worktree_path,
    _scan_history,
    _scan_tag_metadata,
    _scan_worktree,
    _validate_worktree_resolution,
    _validated_output_path,
    scan_text,
)

ROOT = Path(__file__).resolve().parents[2]


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


def _copy_preflight_contract_surface(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    public_documents = sorted(
        {
            path.relative_to(ROOT).as_posix()
            for path in (*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md"))
            if path.is_file() and path.name != "AGENTS.md"
        }
    )
    for relative in (
        *public_documents,
        "src/poker_deliberation/roadmap.py",
        "src/poker_deliberation/roadmap_status.json",
        "tests/fixtures/range/v1/cases.json",
        "evals/datasets/p3_016a/v1/cases.json",
        "tests/fixtures/range_equity/v1/scenarios.json",
        "scripts/run_range_equity_evaluation.py",
        "tools/manifest.yaml",
    ):
        source = ROOT / relative
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return repo


def test_public_document_inventory_excludes_removed_local_planning_docs() -> None:
    paths, digest = public_preflight._public_document_inventory(ROOT)

    assert "PLAN.md" not in paths
    assert "PROGRESS.md" not in paths
    assert digest == public_preflight.PUBLIC_DOCUMENT_INVENTORY_SHA256


def test_capability_preflight_detects_roadmap_schema_and_bridge_drift(
    tmp_path: Path,
) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    assert _capability_docs_check(repo).status == "pass"

    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8").replace("schema 14.0.0", "schema 10.0"),
        encoding="utf-8",
    )
    assert _capability_docs_check(repo).status == "fail"

    shutil.copyfile(ROOT / "README.md", readme)
    bridge = repo / "docs/range-equity-bridge.md"
    bridge.write_text(
        bridge.read_text(encoding="utf-8").replace("P3-030C", "future milestone"),
        encoding="utf-8",
    )
    assert _capability_docs_check(repo).status == "fail"

    shutil.copyfile(ROOT / "docs/range-equity-bridge.md", bridge)
    bridge.write_text(
        bridge.read_text(encoding="utf-8").replace(
            "P3-016B自身のcontractや通常経路は",
            "P3-016B自身のcontractや通常経路も",
        ),
        encoding="utf-8",
    )
    assert _capability_docs_check(repo).status == "fail"


def test_capability_preflight_rejects_schema_value_drift_hidden_by_comments(
    tmp_path: Path,
) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    replacements = {
        "README.md": ("schema 14.0.0", "schema 10.0.0\n<!-- schema 14.0.0 -->"),
        "docs/roadmap-status.md": (
            "schema version: `14.0.0`",
            "schema version: `10.0.0`\n<!-- schema version: `14.0.0` -->",
        ),
        "src/poker_deliberation/roadmap.py": (
            'ROADMAP_SCHEMA_VERSION = "14.0.0"',
            'ROADMAP_SCHEMA_VERSION = "10.0.0"\n# ROADMAP_SCHEMA_VERSION = "14.0.0"',
        ),
        "src/poker_deliberation/roadmap_status.json": (
            '"schema_version": "14.0.0"',
            '"schema_version": "10.0.0"',
        ),
    }
    for relative, (old, new) in replacements.items():
        path = repo / relative
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")

    assert _capability_docs_check(repo).status == "fail"


def test_capability_preflight_rejects_computed_schema_reassignment(
    tmp_path: Path,
) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    roadmap_module = repo / "src/poker_deliberation/roadmap.py"
    roadmap_module.write_text(
        roadmap_module.read_text(encoding="utf-8")
        + '\nROADMAP_SCHEMA_VERSION = str(10) + ".0.0"\n',
        encoding="utf-8",
    )

    assert _capability_docs_check(repo).status == "fail"


def test_capability_preflight_rejects_globals_schema_reassignment(
    tmp_path: Path,
) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    roadmap_module = repo / "src/poker_deliberation/roadmap.py"
    roadmap_module.write_text(
        roadmap_module.read_text(encoding="utf-8")
        + '\nglobals()["ROADMAP_SCHEMA_VERSION"] = "10.0.0"\n',
        encoding="utf-8",
    )

    assert _capability_docs_check(repo).status == "fail"


@pytest.mark.parametrize(
    "payload",
    (
        "\nfrom os import name as ROADMAP_SCHEMA_VERSION\n",
        '\nkey = "ROADMAP_SCHEMA_" + "VERSION"\nsys.modules[__name__].__dict__[key] = "10.0.0"\n',
        '\nkey = "ROADMAP_SCHEMA_VERSION"\ngetter = globals.__call__\ngetter()[key] = "10.0.0"\n',
    ),
)
def test_capability_preflight_rejects_indirect_schema_mutation(
    tmp_path: Path,
    payload: str,
) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    roadmap_module = repo / "src/poker_deliberation/roadmap.py"
    roadmap_module.write_text(
        roadmap_module.read_text(encoding="utf-8") + payload,
        encoding="utf-8",
    )
    assert _capability_docs_check(repo).status == "fail"


def test_capability_preflight_rejects_contradictory_p3_030c_bridge_sentence(
    tmp_path: Path,
) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    bridge = repo / "docs/range-equity-bridge.md"
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        + "\nP3-030CはP3-016B自身のcontractと通常経路を変更する。\n",
        encoding="utf-8",
    )

    assert _capability_docs_check(repo).status == "fail"


def test_capability_preflight_rejects_untracked_public_claim(tmp_path: Path) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    bridge = repo / "docs/range-equity-bridge.md"
    bridge.write_text(
        bridge.read_text(encoding="utf-8")
        + "\nThis bridge now supports general multiway strategy.\n",
        encoding="utf-8",
    )
    assert _capability_docs_check(repo).status == "fail"


def test_capability_preflight_rejects_modified_other_public_document(
    tmp_path: Path,
) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    readme = repo / "README.md"
    readme.write_text(
        readme.read_text(encoding="utf-8") + "\nP3-030C provides GTO strategy.\n",
        encoding="utf-8",
    )

    assert _capability_docs_check(repo).status == "fail"


def test_capability_preflight_rejects_additional_public_document(tmp_path: Path) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    extra = repo / "docs/other-public.md"
    extra.write_text("P3-030C supports general natural language.\n", encoding="utf-8")

    result = _capability_docs_check(repo)

    assert result.status == "fail"
    assert "public_documents:canonical_inventory_identity" in result.details["missing_markers"]


def test_range_artifact_preflight_detects_bridge_evaluation_removal(tmp_path: Path) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    assert _range_grammar_artifacts_check(repo).status == "pass"

    (repo / "tests/fixtures/range_equity/v1/scenarios.json").unlink()
    result = _range_grammar_artifacts_check(repo)

    assert result.status == "fail"
    assert "range_equity_evaluation:fixture_invalid" in result.details["failures"]


@pytest.mark.parametrize(
    "mutation",
    (
        "evidence",
        "license",
        "runner",
        "runner_exit",
        "manifest_truncate",
        "manifest_command",
        "manifest_schema_float",
        "manifest_bool_int",
        "manifest_duplicate_command",
        "range_validate",
        "combos",
        "holdem_equity",
    ),
)
def test_range_artifact_preflight_rejects_same_size_bridge_contract_tamper(
    tmp_path: Path,
    mutation: str,
) -> None:
    repo = _copy_preflight_contract_surface(tmp_path)
    fixture_path = repo / "tests/fixtures/range_equity/v1/scenarios.json"
    runner_path = repo / "scripts/run_range_equity_evaluation.py"
    manifest_path = repo / "tools/manifest.yaml"
    if mutation in {"evidence", "license"}:
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        if mutation == "evidence":
            fixture["cases"][0]["expected_evidence"] = ["forged-evidence"]
        else:
            fixture["license_classification"] = "proprietary"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    elif mutation == "runner":
        runner_path.write_text("", encoding="utf-8")
    elif mutation == "runner_exit":
        runner_path.write_text(
            runner_path.read_text(encoding="utf-8").replace(
                "return 0 if result.passed else 2",
                "return 0",
            ),
            encoding="utf-8",
        )
    elif mutation == "manifest_truncate":
        manifest_text = manifest_path.read_text(encoding="utf-8")
        marker = "- name: holdem_equity"
        manifest_path.write_text(
            manifest_text[: manifest_text.index(marker) + len(marker)] + "\n",
            encoding="utf-8",
        )
    elif mutation == "manifest_command":
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                "poker-deliberate calculate holdem_equity",
                "poker-deliberate calculate range_validate",
            ),
            encoding="utf-8",
        )
    elif mutation == "manifest_schema_float":
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                "schema_version: 2",
                "schema_version: 2.0",
                1,
            ),
            encoding="utf-8",
        )
    elif mutation == "manifest_bool_int":
        manifest_text = manifest_path.read_text(encoding="utf-8")
        start = manifest_text.index("- name: range_validate")
        end = manifest_text.index("\n- name: ", start + 1)
        block = manifest_text[start:end].replace(
            "additionalProperties: false",
            "additionalProperties: 0",
            1,
        )
        manifest_path.write_text(
            manifest_text[:start] + block + manifest_text[end:],
            encoding="utf-8",
        )
    elif mutation == "manifest_duplicate_command":
        command = (
            "  command: poker-deliberate calculate holdem_equity "
            "--analysis-scope retrospective --input INPUT.json"
        )
        forged = (
            "  command: poker-deliberate calculate range_validate "
            "--analysis-scope retrospective --input INPUT.json\n" + command
        )
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(command, forged, 1),
            encoding="utf-8",
        )
    else:
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8").replace(
                f"- name: {mutation}", "- name: removed"
            ),
            encoding="utf-8",
        )

    result = _range_grammar_artifacts_check(repo)

    assert result.status == "fail"


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


def test_worktree_scan_rejects_external_hardlink_without_reading(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("external-fixture@example.invalid\n", encoding="utf-8")
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    linked = repo / "public.txt"
    try:
        os.link(outside, linked)
    except OSError as exc:
        pytest.skip(f"hard links unavailable: {exc}")

    with pytest.raises(ValueError, match="multiple hard links"):
        _safe_worktree_path(repo, "public.txt")
    findings, large_files, skipped = _scan_worktree(repo, ["public.txt"])

    assert findings == []
    assert large_files == []
    assert skipped == ["public.txt"]


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


def test_legacy_bridge_manifest_is_not_sealed_live_evidence() -> None:
    from poker_deliberation.codex_bridge.canonical import parse_canonical_model
    from poker_deliberation.codex_bridge.qualification import (
        SanitizedLiveQualificationManifestV2,
    )

    with pytest.raises(ValueError):
        parse_canonical_model(
            b'{"schema_version":"1.0.0"}',
            SanitizedLiveQualificationManifestV2,
        )


def test_public_preflight_accepts_sealed_live_manifest() -> None:
    result = public_preflight._codex_bridge_public_artifacts_check(ROOT)

    assert result.status == "pass"
    assert isinstance(result.details, dict)
    assert result.details["failures"] == []
    assert result.details["missing_public_evidence"] == []
    assert result.details["subscription_live_qualified"] is True
    assert result.details["api_live_qualified"] is False
    assert result.details["subscription_live_evidence_authority"] == (
        "qualifications/p2-025b-codex-subscription-v1.json"
    )


def test_bridge_documentation_uses_manifest_api_qualification_field_names() -> None:
    text = (ROOT / "docs" / "bounded-codex-river-review-bridge.md").read_text(encoding="utf-8")

    assert "`api_live_executed=false`" in text
    assert "`api_production_qualified=false`" in text
    assert "`live_api_executed=false`" not in text
    assert "`production_qualified=false`" not in text


def test_bridge_documentation_matches_subscription_auth_probe_stream_contract() -> None:
    text = (ROOT / "docs" / "bounded-codex-river-review-bridge.md").read_text(encoding="utf-8")

    assert "stdout\nまたはstderrの正確に一方だけ" in text
    assert "もう一方が空" in text
    assert "両streamへの重複出力" in text


def test_bridge_documentation_requires_fresh_sealed_live_evidence() -> None:
    text = (ROOT / "docs" / "bounded-codex-river-review-bridge.md").read_text(encoding="utf-8")

    assert "qualifications/p2-025b-codex-subscription-v1.json" in text
    assert "strict canonical V2 sealed live manifest" in text
    assert "legacy V1 manifestしかない場合" in text
    assert "fresh live" in text
    assert "same-privilege caller" in text
    assert "p25-live-" not in text
    assert "subscription利用量は合計input" not in text
    assert "manifest hashは" not in text


def test_public_docs_separate_runtime_qualification_states() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    capabilities = (ROOT / "docs" / "capabilities.md").read_text(encoding="utf-8")
    limitations = (ROOT / "docs" / "limitations.md").read_text(encoding="utf-8")

    for text in (readme, capabilities):
        assert "sealed actual-live qualified" in text
        assert "local_only" in text
        assert "networkなし" in text
        assert "live-unqualified" in text
    assert "strict canonical V2 manifest" in limitations
    assert "caller-controlled label" in limitations


def test_public_docs_limit_no_bridge_claim_to_ordinary_surfaces() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    limitations = (ROOT / "docs" / "limitations.md").read_text(encoding="utf-8")

    assert "ordinary manual surface; no general runtime bridge" in readme
    assert "manual use; no runtime bridge" not in readme
    assert "Ordinary Python runs do not launch `.codex/agents/*.toml`" in limitations
    assert "P2-025B is a separately named exception" in limitations
    assert "dedicated\n  additive bridge artifact family" in limitations
    assert "Python does not launch Codex agents" not in limitations


def test_readme_distinguishes_local_build_evidence_from_release_unknowns() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "ローカルwheel/sdist build" in text
    assert "`--no-index --no-deps` default" in text
    assert "API keyなしの`local_only` smokeが成功" in text
    assert "remote CI、未実行のOS/Python matrix、GitHub上の公開release artifact" in text
    assert "wheel/sdist、clean-install matrix" not in text
