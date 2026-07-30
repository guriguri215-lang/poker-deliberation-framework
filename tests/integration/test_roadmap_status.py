from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from copy import deepcopy
from importlib import resources
from pathlib import Path

import pytest

import scripts.generate_roadmap_status as roadmap_generator
from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.cli import doctor
from poker_deliberation.roadmap import (
    EXPECTED_IMPLEMENTATION_MILESTONES,
    EXPECTED_RM_IDS,
    ITEM_FIELDS,
    MILESTONE_FIELDS,
    ROADMAP_RESOURCE,
    ROADMAP_SCHEMA_VERSION,
    TOP_LEVEL_FIELDS,
    load_roadmap,
    render_roadmap_markdown,
    roadmap_items,
    roadmap_summary,
    validate_repository_evidence,
    validate_roadmap,
    validate_roadmap_update,
    validate_transition,
)
from scripts.generate_roadmap_status import main as generate_roadmap_status

ROOT = Path(__file__).resolve().parents[2]


def _by_id(document: dict[str, object] | None = None) -> dict[str, dict[str, object]]:
    raw_items = roadmap_items() if document is None else document["items"]
    return {str(item["id"]): item for item in raw_items}  # type: ignore[union-attr]


def _milestones(document: dict[str, object]) -> dict[str, dict[str, object]]:
    return {
        str(item["id"]): item
        for item in document["implementation_milestones"]  # type: ignore[union-attr]
    }


def _tracked_paths() -> set[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return set(result.stdout.splitlines())


def _fake_roadmap_git(
    documents: dict[str, dict[str, object]],
    revisions: list[str],
    *,
    shallow: bool = False,
) -> Callable[..., subprocess.CompletedProcess[str]]:
    def fake_git(*args: str) -> subprocess.CompletedProcess[str]:
        if args[0] == "rev-parse":
            assert args == ("rev-parse", "--is-shallow-repository")
            return subprocess.CompletedProcess(
                ["git", *args],
                0,
                stdout=f"{str(shallow).lower()}\n",
                stderr="",
            )
        if args[0] == "rev-list":
            assert args == (
                "rev-list",
                "--first-parent",
                "HEAD",
                "--",
                roadmap_generator.SOURCE_RELATIVE.as_posix(),
            )
            return subprocess.CompletedProcess(
                ["git", *args],
                0,
                stdout="\n".join(revisions) + "\n",
                stderr="",
            )
        assert args[0] == "show"
        revision, separator, source_path = args[1].partition(":")
        assert separator == ":"
        assert source_path == roadmap_generator.SOURCE_RELATIVE.as_posix()
        return subprocess.CompletedProcess(
            ["git", *args],
            0,
            stdout=json.dumps(documents[revision]),
            stderr="",
        )

    return fake_git


def test_packaged_public_roadmap_loads_outside_repository_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    document = load_roadmap()

    assert document["schema_version"] == ROADMAP_SCHEMA_VERSION == "9.0.0"
    assert resources.files("poker_deliberation").joinpath(ROADMAP_RESOURCE).is_file()
    assert not (tmp_path / "docs").exists()


def test_public_projection_has_exact_schema_and_complete_item_sets() -> None:
    document = load_roadmap()

    assert set(document) == TOP_LEVEL_FIELDS
    assert {str(item["id"]) for item in document["items"]} == EXPECTED_RM_IDS
    assert all(set(item) == ITEM_FIELDS for item in document["items"])
    assert {
        str(item["id"]) for item in document["implementation_milestones"]
    } == EXPECTED_IMPLEMENTATION_MILESTONES
    assert all(set(item) == MILESTONE_FIELDS for item in document["implementation_milestones"])
    assert document["source_policy"] == {
        "canonical": True,
        "projection": "public",
        "description": (
            "Tracked public roadmap projection for implementation status, dependencies, "
            "capability scope, acceptance criteria, milestones, and decision rationale."
        ),
        "generated_document": "docs/roadmap-status.md",
    }


def test_public_projection_preserves_status_scope_and_decision_rationale() -> None:
    items = _by_id()

    assert {
        rm_id: items[rm_id]["status"]
        for rm_id in ("RM-010", "RM-011", "RM-012", "RM-024", "RM-027")
    } == {
        "RM-010": "completed",
        "RM-011": "completed",
        "RM-012": "completed",
        "RM-024": "completed",
        "RM-027": "completed",
    }
    assert items["RM-013"]["status"] == "completed"
    assert items["RM-028"]["status"] == "proposed"
    assert items["RM-029"]["status"] == "completed"
    assert items["RM-025"]["priority"] == "P1"
    assert items["RM-018A"]["status"] == "planned"
    assert items["RM-018B"]["status"] == "planned"
    assert items["RM-010"]["milestones"] == {
        "entry": "P2-010A",
        "completion": "P2-010B",
    }
    assert items["RM-010"]["decision_gate"]["required"] is True  # type: ignore[index]
    assert items["RM-010"]["decision_gate"]["rationale"]  # type: ignore[index]
    assert items["RM-001"]["decision_gate"] == {"required": False, "rationale": []}
    for item in items.values():
        assert item["objective"]
        assert item["targets"]
        assert item["acceptance_criteria"]
        assert item["tests"]
        assert item["status_reason"]


def test_public_milestone_projection_keeps_only_current_state() -> None:
    milestones = _milestones(load_roadmap())

    completed = {
        "P2-024A",
        "P2-010A",
        "P2-010B",
        "P2-011A",
        "P2-011B",
        "P2-012A",
        "P2-012B",
        "P2-013A",
        "P2-013B",
        "P2-027A",
        "P2-027B",
        "P2-029A",
        "P2-025A",
        "P3-014A",
        "P3-015A",
        "P3-016A",
        "P3-017A",
        "P3-030A",
    }
    assert {item_id for item_id, item in milestones.items() if item["status"] == "completed"} == (
        completed
    )
    assert {item_id for item_id, item in milestones.items() if item["status"] == "not_started"} == {
        "P2-028A",
    }
    assert not {item_id for item_id, item in milestones.items() if item["status"] == "in_progress"}
    assert milestones["P2-011A"]["dependencies"] == ["RM-023", "P2-010A"]
    assert milestones["P2-029A"]["dependencies"] == [
        "P2-012B",
        "P2-013B",
        "P2-024A",
        "P2-027B",
    ]
    assert all(item["status_reason"] for item in milestones.values())


def test_p3_017a_registration_is_offline_and_bounded() -> None:
    items = _by_id()
    milestones = _milestones(load_roadmap())

    assert items["RM-017"]["status"] == "in_progress"
    assert items["RM-017"]["capabilities"] == ["offline_evaluation_harness"]
    assert items["RM-017"]["milestones"] == {
        "entry": "P3-017A",
        "completion": None,
    }
    assert milestones["P3-017A"] == {
        "id": "P3-017A",
        "rm_id": "RM-017",
        "status": "completed",
        "status_reason": (
            "The canonical synthetic fixture, deterministic runner and scorer, "
            "provenance-bound result, documentation, and declared tests are implemented."
        ),
        "dependencies": [
            "RM-006",
            "RM-007",
            "RM-012",
            "P2-025A",
        ],
        "scope": (
            "Strict versioned offline dataset, scorer, provenance, runtime-inventory, "
            "per-case outcome, structured-failure, and summary contracts with a "
            "repository-owned synthetic MIT fixture and deterministic exact-evidence "
            "scoring; no provider, solver, bridge, or external dataset execution."
        ),
    }
    assert items["RM-017"]["decision_gate"]["required"] is True  # type: ignore[index]
    assert "subjective strategy metrics" in items["RM-017"]["decision_gate"]["rationale"][-1]  # type: ignore[index]


def test_p3_030a_registration_is_confirmed_local_and_bounded() -> None:
    items = _by_id()
    milestones = _milestones(load_roadmap())

    assert items["RM-030"]["status"] == "in_progress"
    assert items["RM-030"]["capabilities"] == [
        "confirmed_natural_language_review_intake",
        "natural_language_or_site_parser",
        "versioned_nlhe_range_grammar",
    ]
    assert items["RM-030"]["milestones"] == {
        "entry": "P3-030A",
        "completion": None,
    }
    assert items["RM-030"]["dependencies"] == ["RM-014"]
    assert milestones["P3-030A"]["status"] == "completed"
    assert milestones["P3-030A"]["dependencies"] == [
        "P3-014A",
        "P3-016A",
        "P3-017A",
    ]
    scope = milestones["P3-030A"]["scope"]
    assert "LocalProvider-only adjudication" in scope
    assert "no general natural-language or site parser" in scope
    assert items["RM-030"]["decision_gate"]["required"] is True  # type: ignore[index]
    rationale = items["RM-030"]["decision_gate"]["rationale"]  # type: ignore[index]
    assert any("P3-030B" in item for item in rationale)
    assert any("P2-025B" in item for item in rationale)
    assert any("P3-030C" in item for item in rationale)


def test_p3_014a_registration_is_versioned_bounded_and_site_independent() -> None:
    items = _by_id()
    milestones = _milestones(load_roadmap())

    assert items["RM-014"]["status"] == "completed"
    assert items["RM-014"]["milestones"] == {
        "entry": "P3-014A",
        "completion": "P3-014A",
    }
    assert milestones["P3-014A"]["status"] == "completed"
    assert milestones["P3-014A"]["dependencies"] == ["RM-006", "RM-012"]
    assert "supported site none" in milestones["P3-014A"]["scope"]
    assert items["RM-015"]["status"] == "in_progress"
    assert items["RM-016"]["status"] == "in_progress"


def test_p3_015a_registration_is_profiled_exact_and_bounded() -> None:
    items = _by_id()
    milestones = _milestones(load_roadmap())

    assert items["RM-015"]["status"] == "in_progress"
    assert items["RM-015"]["capabilities"] == ["profiled_nlhe_side_pot_ledger"]
    assert items["RM-015"]["milestones"] == {
        "entry": "P3-015A",
        "completion": None,
    }
    assert milestones["P3-015A"]["status"] == "completed"
    assert milestones["P3-015A"]["dependencies"] == ["P3-014A"]
    assert "generic_nlhe_cash_no_rake_v1" in milestones["P3-015A"]["scope"]
    assert "supported site none" in milestones["P3-015A"]["scope"]
    assert "independent oracle" in milestones["P3-015A"]["scope"]
    assert items["RM-015"]["decision_gate"]["required"] is True  # type: ignore[index]


def test_p3_016a_registration_is_versioned_provenance_bound_and_additive() -> None:
    items = _by_id()
    milestones = _milestones(load_roadmap())

    assert items["RM-016"]["status"] == "in_progress"
    assert items["RM-016"]["capabilities"] == ["versioned_nlhe_range_grammar"]
    assert items["RM-016"]["milestones"] == {
        "entry": "P3-016A",
        "completion": None,
    }
    assert milestones["P3-016A"]["status"] == "completed"
    assert milestones["P3-016A"]["dependencies"] == ["RM-006", "P3-014A"]
    assert "poker-deliberation.nlhe-range grammar version 1.0.0" in (milestones["P3-016A"]["scope"])
    assert "no plus, intervals, exclusions" in milestones["P3-016A"]["scope"]
    assert items["RM-016"]["decision_gate"]["required"] is True  # type: ignore[index]
    assert items["RM-030"]["status"] == "in_progress"
    assert items["RM-030"]["dependencies"] == ["RM-014"]
    assert "natural-language" in str(items["RM-030"]["objective"])


def test_p2_025a_registration_preserves_external_execution_boundaries() -> None:
    items = _by_id()
    milestones = _milestones(load_roadmap())

    assert items["RM-025"]["status"] == "in_progress"
    assert items["RM-025"]["decision_gate"] == {
        "required": True,
        "rationale": [
            "whether a future actual bridge candidate should be registered after the "
            "conformance-only contract",
        ],
    }
    assert milestones["P2-025A"] == {
        "id": "P2-025A",
        "rm_id": "RM-025",
        "status": "completed",
        "status_reason": (
            "The strict conformance-only contract, versioned fixtures, and verified offline Python "
            "product projection are implemented without a runtime bridge."
        ),
        "dependencies": [
            "P2-012B",
            "P2-013B",
            "P2-024A",
            "P2-029A",
        ],
        "scope": (
            "Versioned cross-runtime role, assignment, context, tool allowlist, approval, "
            "result, error, execution-audit, canonical fixture, and offline projection "
            "conformance without an execution bridge."
        ),
    }
    assert items["RM-028"]["status"] == "proposed"
    assert milestones["P2-028A"]["status"] == "not_started"
    assert items["RM-019"]["dependencies"] == [
        "RM-010",
        "RM-011",
        "RM-012",
        "RM-013",
        "RM-024",
        "RM-028",
    ]
    assert items["RM-020"]["dependencies"] == [
        "RM-011",
        "RM-012",
        "RM-013",
        "RM-017",
        "RM-028",
    ]
    assert items["RM-019"]["status"] == "planned"
    assert items["RM-020"]["status"] == "planned"


def test_unknown_projection_fields_fail_closed() -> None:
    top_level = deepcopy(load_roadmap())
    top_level["private_extension"] = {}
    with pytest.raises(ValueError, match="roadmap fields mismatch"):
        validate_roadmap(top_level)

    item_level = deepcopy(load_roadmap())
    item_level["items"][0]["private_extension"] = {}  # type: ignore[index]
    with pytest.raises(ValueError, match="invalid roadmap item fields"):
        validate_roadmap(item_level)

    milestone_level = deepcopy(load_roadmap())
    milestone_level["implementation_milestones"][0]["private_extension"] = {}  # type: ignore[index]
    with pytest.raises(ValueError, match="invalid milestone fields"):
        validate_roadmap(milestone_level)

    item_status = deepcopy(load_roadmap())
    item_status["status_vocabulary"]["private"] = "Undeclared extension state."  # type: ignore[index]
    item_status["legal_transitions"]["private"] = []  # type: ignore[index]
    _by_id(item_status)["RM-028"]["status"] = "private"
    with pytest.raises(ValueError, match="status vocabulary mismatch"):
        validate_roadmap(item_status)

    milestone_status = deepcopy(load_roadmap())
    milestone_status["milestone_status_vocabulary"]["private"] = (  # type: ignore[index]
        "Undeclared extension state."
    )
    milestone_status["milestone_legal_transitions"]["private"] = []  # type: ignore[index]
    _milestones(milestone_status)["P2-028A"]["status"] = "private"
    with pytest.raises(ValueError, match="milestone status vocabulary mismatch"):
        validate_roadmap(milestone_status)

    output_path = deepcopy(load_roadmap())
    output_path["source_policy"]["generated_document"] = (  # type: ignore[index]
        "src/poker_deliberation/approvals.py"
    )
    with pytest.raises(ValueError, match="generated document must be"):
        validate_roadmap(output_path)

    backslash_output_path = deepcopy(load_roadmap())
    backslash_output_path["source_policy"]["generated_document"] = (  # type: ignore[index]
        "docs\\roadmap-status.md"
    )
    with pytest.raises(ValueError, match="generated document must be"):
        validate_roadmap(backslash_output_path)


def test_item_dependency_cycle_and_active_dependency_fail_closed() -> None:
    cycle = deepcopy(load_roadmap())
    cycle["items"][0]["dependencies"] = ["RM-028"]  # type: ignore[index]
    with pytest.raises(ValueError, match="dependency cycle"):
        validate_roadmap(cycle)

    duplicate = deepcopy(load_roadmap())
    duplicate["items"][1]["dependencies"] = ["RM-001", "RM-001"]  # type: ignore[index]
    with pytest.raises(ValueError, match="contains duplicates"):
        validate_roadmap(duplicate)

    incomplete = deepcopy(load_roadmap())
    items = _by_id(incomplete)
    items["RM-010"]["dependencies"].append("RM-028")  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="active item has incomplete dependency"):
        validate_roadmap(incomplete)


def test_milestone_cycle_owner_and_status_rules_fail_closed() -> None:
    cycle = deepcopy(load_roadmap())
    milestones = _milestones(cycle)
    milestones["P2-024A"]["dependencies"] = ["P2-028A"]
    with pytest.raises(ValueError, match="implementation dependency cycle"):
        validate_roadmap(cycle)

    owner = deepcopy(load_roadmap())
    milestones = _milestones(owner)
    milestones["P2-024A"]["rm_id"] = "RM-010"
    with pytest.raises(ValueError, match="milestone owner mismatch"):
        validate_roadmap(owner)

    inactive_parent = deepcopy(load_roadmap())
    items = _by_id(inactive_parent)
    items["RM-013"]["status"] = "planned"
    with pytest.raises(ValueError, match="completed milestone has inactive parent"):
        validate_roadmap(inactive_parent)


def test_public_update_validation_preserves_contracts_and_legal_transitions() -> None:
    previous = load_roadmap()
    unchanged = deepcopy(previous)
    validate_roadmap_update(previous, unchanged)
    validate_roadmap_update(previous, unchanged, set())

    status_change = deepcopy(previous)
    items = _by_id(status_change)
    items["RM-002"]["status"] = "in_progress"
    items["RM-002"]["status_reason"] = "A published follow-up is in progress."
    validate_roadmap_update(previous, status_change)

    unchanged_reason = deepcopy(previous)
    _by_id(unchanged_reason)["RM-002"]["status"] = "in_progress"
    with pytest.raises(ValueError, match="status transition requires a new reason"):
        validate_roadmap_update(previous, unchanged_reason)

    whitespace_only_reason = deepcopy(previous)
    _by_id(whitespace_only_reason)["RM-002"]["status"] = "in_progress"
    _by_id(whitespace_only_reason)["RM-002"]["status_reason"] = (
        str(_by_id(previous)["RM-002"]["status_reason"]) + " "
    )
    with pytest.raises(ValueError, match="status transition requires a new reason"):
        validate_roadmap_update(previous, whitespace_only_reason)

    illegal = deepcopy(previous)
    _by_id(illegal)["RM-018A"]["status"] = "proposed"
    with pytest.raises(ValueError, match="illegal status transition"):
        validate_roadmap_update(previous, illegal)

    contract_change = deepcopy(previous)
    _by_id(contract_change)["RM-011"]["targets"].append("new public target")  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="public item contract changed"):
        validate_roadmap_update(previous, contract_change)

    transition_table_change = deepcopy(previous)
    transition_table_change["legal_transitions"]["planned"].append("proposed")  # type: ignore[index,union-attr]
    _by_id(transition_table_change)["RM-018A"]["status"] = "proposed"
    with pytest.raises(ValueError, match="public top-level contract changed"):
        validate_roadmap_update(previous, transition_table_change)

    decision_gate_change = deepcopy(previous)
    _by_id(decision_gate_change)["RM-010"]["decision_gate"] = {
        "required": False,
        "rationale": [],
    }
    with pytest.raises(ValueError, match="public item contract changed"):
        validate_roadmap_update(previous, decision_gate_change)

    output_path_change = deepcopy(previous)
    output_path_change["source_policy"]["generated_document"] = (  # type: ignore[index]
        "src/poker_deliberation/approvals.py"
    )
    with pytest.raises(ValueError, match="generated document must be"):
        validate_roadmap_update(previous, output_path_change)


def test_transition_validator_rejects_unknown_and_illegal_targets() -> None:
    transitions = load_roadmap()["legal_transitions"]
    validate_transition("planned", "blocked", transitions)
    with pytest.raises(ValueError, match="unknown transition source"):
        validate_transition("missing", "planned", transitions)
    with pytest.raises(ValueError, match="illegal status transition"):
        validate_transition("planned", "proposed", transitions)


def test_completed_public_claim_paths_exist_and_are_tracked() -> None:
    document = load_roadmap()
    tracked = _tracked_paths()

    validate_repository_evidence(document, ROOT, tracked_paths=tracked)

    missing = deepcopy(document)
    _by_id(missing)["RM-001"]["targets"] = ["src/poker_deliberation/missing.py"]
    with pytest.raises(ValueError, match="does not exist"):
        validate_repository_evidence(missing, ROOT, tracked_paths=tracked)

    untracked = deepcopy(document)
    target = "src/poker_deliberation/capabilities.py"
    with pytest.raises(ValueError, match="is not tracked"):
        validate_repository_evidence(untracked, ROOT, tracked_paths=tracked - {target})

    escaping = deepcopy(document)
    _by_id(escaping)["RM-001"]["targets"] = ["src/../../outside.py"]
    with pytest.raises(ValueError, match="escapes repository"):
        validate_repository_evidence(escaping, ROOT, tracked_paths=tracked)


def test_summary_is_public_dependency_projection_without_release_overclaim() -> None:
    summary = roadmap_summary()

    assert summary["schema_version"] == "9.0.0"
    assert summary["total_items"] == 31
    assert summary["status_counts"] == {
        "completed": 18,
        "in_progress": 5,
        "planned": 6,
        "proposed": 2,
    }
    assert summary["milestone_state_counts"] == {
        "completed": 18,
        "not_started": 1,
    }
    assert summary["milestone_ready_ids"] == []
    assert summary["implementation_ready_ids"] == [
        "RM-018A",
        "RM-021",
    ]
    assert summary["release_readiness"]["pre_release"]["candidate_evidence"] == ("not_evaluated")
    assert "dependency-only" in summary["note"]
    assert len(summary["source_sha256"]) == 64


def test_doctor_and_generated_document_use_the_public_projection() -> None:
    document = load_roadmap()
    generated_path = ROOT / str(document["source_policy"]["generated_document"])

    assert doctor()["roadmap"] == roadmap_summary(document)
    assert generated_path.read_text(encoding="utf-8") == render_roadmap_markdown(document)
    assert generate_roadmap_status(["--check", "--require-tracked"]) == 0


def test_generator_rejects_same_schema_contract_and_transition_rewrites(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = load_roadmap()
    current = deepcopy(previous)
    current["legal_transitions"]["planned"].append("proposed")  # type: ignore[index,union-attr]
    _by_id(current)["RM-018A"]["status"] = "proposed"
    source = tmp_path / ROADMAP_RESOURCE
    source.write_text(json.dumps(current), encoding="utf-8")

    monkeypatch.setattr(roadmap_generator, "SOURCE_PATH", source)
    monkeypatch.setattr(
        roadmap_generator,
        "_git",
        _fake_roadmap_git({"HEAD": previous, "base": previous}, ["base"]),
    )

    with pytest.raises(ValueError, match="public top-level contract changed"):
        generate_roadmap_status(["--check"])


def test_generator_compares_a_clean_committed_candidate_with_its_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = load_roadmap()
    candidate = deepcopy(previous)
    candidate["legal_transitions"]["planned"].append("proposed")  # type: ignore[index,union-attr]
    _by_id(candidate)["RM-018A"]["status"] = "proposed"
    source = tmp_path / ROADMAP_RESOURCE
    source.write_text(json.dumps(candidate), encoding="utf-8")
    documents: dict[str, dict[str, object]] = {
        "HEAD": candidate,
        "candidate": candidate,
        "base": previous,
    }

    monkeypatch.setattr(roadmap_generator, "SOURCE_PATH", source)
    monkeypatch.setattr(
        roadmap_generator,
        "_git",
        _fake_roadmap_git(documents, ["candidate", "base"]),
    )

    with pytest.raises(ValueError, match="public top-level contract changed"):
        generate_roadmap_status(["--check"])


def test_generator_audits_roadmap_changes_before_a_no_op_followup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    previous = load_roadmap()
    illegal = deepcopy(previous)
    illegal["legal_transitions"]["planned"].append("proposed")  # type: ignore[index,union-attr]
    _by_id(illegal)["RM-018A"]["status"] = "proposed"
    source = tmp_path / ROADMAP_RESOURCE
    source.write_text(json.dumps(illegal), encoding="utf-8")
    documents: dict[str, dict[str, object]] = {
        "HEAD": illegal,
        "illegal": illegal,
        "base": previous,
    }

    monkeypatch.setattr(roadmap_generator, "SOURCE_PATH", source)
    monkeypatch.setattr(
        roadmap_generator,
        "_git",
        _fake_roadmap_git(documents, ["illegal", "base"]),
    )

    with pytest.raises(ValueError, match="public top-level contract changed"):
        generate_roadmap_status(["--check"])


def test_generator_rejects_invalid_schema_before_the_history_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = load_roadmap()
    source = tmp_path / ROADMAP_RESOURCE
    source.write_text(json.dumps(candidate), encoding="utf-8")
    documents: dict[str, dict[str, object]] = {
        "HEAD": candidate,
        "candidate": candidate,
        "invalid": {},
    }

    monkeypatch.setattr(roadmap_generator, "SOURCE_PATH", source)
    monkeypatch.setattr(
        roadmap_generator,
        "_git",
        _fake_roadmap_git(documents, ["candidate", "invalid"]),
    )

    with pytest.raises(ValueError, match="roadmap schema_version is invalid"):
        generate_roadmap_status(["--check"])


def test_generator_rejects_shallow_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = load_roadmap()
    source = tmp_path / ROADMAP_RESOURCE
    source.write_text(json.dumps(candidate), encoding="utf-8")

    monkeypatch.setattr(roadmap_generator, "SOURCE_PATH", source)
    monkeypatch.setattr(
        roadmap_generator,
        "_git",
        _fake_roadmap_git({"HEAD": candidate}, ["HEAD"], shallow=True),
    )

    with pytest.raises(ValueError, match="requires a non-shallow repository"):
        generate_roadmap_status(["--check"])


def test_generator_rejects_shallow_history_at_a_dirty_schema_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head_document = load_roadmap()
    monkeypatch.setattr(
        roadmap_generator,
        "_git",
        _fake_roadmap_git({"HEAD": head_document}, ["HEAD"], shallow=True),
    )

    with pytest.raises(ValueError, match="requires a non-shallow repository"):
        roadmap_generator._validate_committed_history(head_document, "3.0.0")


def test_generator_help_exposes_only_public_projection_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        generate_roadmap_status(["--help"])
    output = capsys.readouterr().out

    assert exc_info.value.code == 0
    assert "--check" in output
    assert "--require-tracked" in output
    public_options = {
        line.strip().split()[0] for line in output.splitlines() if line.startswith("  --")
    }
    assert public_options == {"--check", "--require-tracked"}


def test_public_surfaces_exclude_management_ledger_markers() -> None:
    forbidden = (
        "approval" + "_records",
        "milestone" + "_approvals",
        "milestone" + "_progress",
        "status" + "_history",
        "scope" + "_digest",
        "completion" + "_evidence",
        "goal" + "-rm",
        "review" + "er",
        "work" + "_ledger",
        "work" + "-ledger",
        "work" + " ledger",
    )
    public_surfaces = (
        ROOT / "README.md",
        ROOT / "docs" / "capabilities.md",
        ROOT / "docs" / "phase2-readiness-contracts.md",
        ROOT / "docs" / "public-release-checklist.md",
        ROOT / "docs" / "review-remediation.md",
        ROOT / "docs" / "roadmap-status.md",
        ROOT / "scripts" / "generate_roadmap_status.py",
        ROOT / "src" / "poker_deliberation" / "roadmap.py",
        ROOT / "src" / "poker_deliberation" / ROADMAP_RESOURCE,
    )

    for path in public_surfaces:
        text = path.read_text(encoding="utf-8").casefold()
        assert not {marker for marker in forbidden if marker.casefold() in text}, path

    tracked = _tracked_paths()
    management_documents = {"plan.md", "progress.md"}
    assert not {path for path in tracked if Path(path).name.casefold() in management_documents}


def test_roadmap_capability_ids_exist_in_the_public_capability_catalog() -> None:
    capability_ids = {item.capability_id for item in CAPABILITIES}
    referenced = {
        capability
        for item in roadmap_items()
        for capability in item["capabilities"]
        if isinstance(capability, str)
    }

    assert referenced <= capability_ids


def test_distribution_configuration_includes_only_the_public_roadmap_resource() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'poker_deliberation = ["roadmap_status.json"]' in pyproject
    json.loads(
        resources.files("poker_deliberation").joinpath(ROADMAP_RESOURCE).read_text(encoding="utf-8")
    )
