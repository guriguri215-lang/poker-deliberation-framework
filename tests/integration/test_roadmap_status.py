from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from importlib import resources
from pathlib import Path

import pytest

from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.cli import doctor
from poker_deliberation.roadmap import (
    APPROVAL_SCOPE_SCHEMA_VERSION,
    EXPECTED_PHASE_2_MILESTONES,
    EXPECTED_RM_IDS,
    IMMUTABLE_ITEM_CONTRACT_FIELDS,
    ROADMAP_RESOURCE,
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


def _by_id() -> dict[str, dict[str, object]]:
    return {str(item["id"]): item for item in roadmap_items()}


def _scoped_approval_record(
    document: dict[str, object], item: dict[str, object], topics: list[str]
) -> dict[str, object]:
    milestone = next(
        milestone
        for milestone in document["implementation_milestones"]
        if milestone["id"] == item["entry_milestone"]
    )
    scope = {
        "schema_version": APPROVAL_SCOPE_SCHEMA_VERSION,
        "rm_id": item["id"],
        "milestone_contract": deepcopy(milestone),
        "item_contract": {
            field: deepcopy(item.get(field)) for field in sorted(IMMUTABLE_ITEM_CONTRACT_FIELDS)
        },
        "milestone_implementation_scope": {
            "targets": deepcopy(item["targets"]),
            "tests": deepcopy(item["tests"]),
            "acceptance_criteria": deepcopy(item["acceptance_criteria"]),
        },
        "policy_decisions": topics,
    }
    canonical = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return {
        "source_label": "test external approval",
        "topics": topics,
        "scope": scope,
        "scope_digest": hashlib.sha256(canonical).hexdigest(),
    }


def _scope_digest(scope: object) -> str:
    canonical = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(canonical).hexdigest()


def test_packaged_roadmap_loads_outside_repository_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    document = load_roadmap()

    assert document["schema_version"] == "1.1.0"
    assert resources.files("poker_deliberation").joinpath(ROADMAP_RESOURCE).is_file()
    assert not (tmp_path / "user_materials").exists()


def test_rm_ids_statuses_dependencies_and_evidence_are_canonical() -> None:
    items = _by_id()
    assert set(items) == EXPECTED_RM_IDS

    completed = {rm_id for rm_id, item in items.items() if item["status"] == "completed"}
    assert completed == {f"RM-{number:03d}" for number in range(1, 10)} | {
        "RM-023",
        "RM-024",
    }
    assert items["RM-010"]["status"] == "in_progress"
    assert items["RM-011"]["status"] == "in_progress"
    assert items["RM-012"]["status"] == "in_progress"
    assert all(items[f"RM-{number:03d}"]["status"] == "planned" for number in range(13, 18))
    assert items["RM-024"]["status"] == "completed"
    assert all(items[f"RM-{number:03d}"]["status"] == "proposed" for number in (25, 26, 28))
    assert items["RM-027"]["status"] == "in_progress"
    assert items["RM-023"]["completion_evidence"]
    assert items["RM-024"]["completion_evidence"]["commits"] == [
        "fc2e41dd4fbde2962373ff7ea29019bff2999505"
    ]


def test_status_vocabulary_and_legal_transitions_are_explicit() -> None:
    document = load_roadmap()
    assert set(document["status_vocabulary"]) == {
        "proposed",
        "planned",
        "in_progress",
        "blocked",
        "completed",
        "superseded",
    }
    assert document["legal_transitions"]["completed"] == ["in_progress", "blocked"]
    assert document["legal_transitions"]["superseded"] == []
    validate_transition("proposed", "planned", document["legal_transitions"])
    with pytest.raises(ValueError, match="illegal status transition"):
        validate_transition("proposed", "completed", document["legal_transitions"])


def test_status_history_prevents_status_only_completion_counterexample() -> None:
    counterexample = deepcopy(load_roadmap())
    item = next(item for item in counterexample["items"] if item["id"] == "RM-024")
    item["status"] = "completed"
    item["completion_evidence"] = {
        "commits": ["a" * 40],
        "paths": ["README.md"],
        "tests": ["tests/integration/test_roadmap_status.py"],
    }
    item["human_approval"] = {"required": True, "state": "not_required", "topics": ["fake"]}

    with pytest.raises(ValueError, match=r"not bound|required approval|status history"):
        validate_roadmap(counterexample)


def test_update_validation_rejects_rewritten_history_and_approval_rebinding() -> None:
    previous = load_roadmap()
    rewritten = deepcopy(previous)
    rewritten["status_history"]["RM-001"] = [
        "proposed",
        "planned",
        "in_progress",
        "completed",
    ]
    validate_roadmap(rewritten)
    with pytest.raises(ValueError, match="status history is not append-only"):
        validate_roadmap_update(previous, rewritten)

    weakened = deepcopy(previous)
    item = next(item for item in weakened["items"] if item["id"] == "RM-024")
    item["human_approval"] = {"required": False, "state": "not_required", "topics": []}
    with pytest.raises(ValueError, match="approval was deleted or rewritten"):
        validate_roadmap_update(previous, weakened)

    rebound = deepcopy(previous)
    rebound["approval_records"]["fake-self-declared"] = {
        "source_label": "self declared",
        "topics": ["tracked packaged JSON as canonical RM status"],
        "scope_digest": "bab1b56a1cf48cd2ec67dd710119038a09ce4bdcffc036cc46a50b54aea6f976",
    }
    completed = next(item for item in rebound["items"] if item["id"] == "RM-023")
    completed["human_approval"]["approval_reference"] = "fake-self-declared"
    validate_roadmap(rebound)
    with pytest.raises(ValueError, match="approval was deleted or rewritten"):
        validate_roadmap_update(previous, rebound)
    with pytest.raises(ValueError, match="approval was deleted or rewritten"):
        validate_roadmap_update(previous, rebound, {"goal-objective-2026-07-20"})

    changed_scope = deepcopy(previous)
    changed = next(item for item in changed_scope["items"] if item["id"] == "RM-023")
    changed["objective"] = "Broaden the completed scope without reopening it."
    validate_roadmap(changed_scope)
    with pytest.raises(ValueError, match="RM contract field requires a schema amendment"):
        validate_roadmap_update(previous, changed_scope)

    remapped = deepcopy(previous)
    active = next(item for item in remapped["items"] if item["id"] == "RM-010")
    active["completion_milestone"] = "P2-010A"
    with pytest.raises(ValueError, match="approval scope contract does not match item"):
        validate_roadmap(remapped)


def test_scoped_proposed_item_can_be_frozen_exactly_once() -> None:
    previous = load_roadmap()
    planned = deepcopy(previous)
    rm_028 = next(item for item in planned["items"] if item["id"] == "RM-028")
    rm_028["status"] = "planned"
    rm_028["targets"] = [
        "src/poker_deliberation/isolation.py",
        "src/poker_deliberation/providers",
    ]
    rm_028["tests"] = [
        "tests/unit/test_isolation.py",
        "tests/integration/test_provider_isolation.py",
    ]
    planned["status_history"]["RM-028"].append("planned")
    topics = ["test-approved P2-028A scope"]
    reference = "test-rm028-scope-freeze"
    planned["approval_records"][reference] = _scoped_approval_record(planned, rm_028, topics)
    planned["milestone_approvals"]["P2-028A"] = reference
    rm_028["human_approval"] = {
        "required": True,
        "state": "approved_scope",
        "topics": topics,
        "approval_reference": reference,
        "scope_digest": planned["approval_records"][reference]["scope_digest"],
    }

    validate_roadmap_update(previous, planned, {reference})
    validate_roadmap_update(previous, planned)
    validate_roadmap_update(previous, planned, {"irrelevant-compatibility-value"})

    mutated_after_freeze = deepcopy(planned)
    mutated = next(item for item in mutated_after_freeze["items"] if item["id"] == "RM-028")
    mutated["targets"].append("src/poker_deliberation/security.py")
    with pytest.raises(ValueError, match="approval scope contract does not match item"):
        validate_roadmap(mutated_after_freeze)


def test_p2_027a_scope_freeze_binds_every_approved_policy_dimension() -> None:
    document = load_roadmap()
    reference = "goal-rm027-p2-027a-2026-07-23"
    record = document["approval_records"][reference]
    rm_027 = next(item for item in document["items"] if item["id"] == "RM-027")
    scope = record["scope"]
    decisions = "\n".join(record["topics"])

    assert record["scope_digest"] == (
        "c5636cff29547bf40ce800e63776a7de77b234ee3acb68b17b4647f5d5b5e96d"
    )
    assert document["milestone_approvals"]["P2-027A"] == reference
    assert document["milestone_approvals"]["P2-027B"] is None
    assert document["milestone_progress"]["P2-027A"]["state"] == "completed"
    assert document["milestone_progress"]["P2-027B"]["state"] == "not_started"
    assert rm_027["status"] == "in_progress"
    assert rm_027["status_reason"] == (
        "P2-027A pure policy/schema is completed; RM-027 remains in progress because "
        "P2-027B is separately unapproved and not started."
    )
    assert rm_027["human_approval"]["topics"] == record["topics"]
    assert scope["policy_decisions"] == record["topics"]
    assert scope["item_contract"]["capabilities"] == [
        "local_data_lifecycle_policy",
        "local_data_cleanup_executor",
    ]
    required_terms = {
        "public run 365",
        "internal run 90",
        "sensitive run 30",
        "restricted 0",
        "quarantine_candidate",
        "delete_candidate",
        "require encryption before sensitive persistence",
        "retention_started_at",
        "retention_expires_at",
        "typed non-retryable lifecycle failure taxonomy",
        "filesystem discovery, read, write, scan, move, rename, quarantine, delete",
    }
    assert not {term for term in required_terms if term not in decisions}


def test_p2_012a_scope_freeze_binds_the_exact_approved_proposal() -> None:
    document = load_roadmap()
    reference = "goal-rm012-p2-012a-2026-07-24"
    record = document["approval_records"][reference]
    scope = record["scope"]
    rm_012 = next(item for item in document["items"] if item["id"] == "RM-012")
    canonical = json.dumps(scope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )

    assert hashlib.sha256(canonical).hexdigest() == (
        "abb67567163a58640f693e46c8f5ad43acec24ac6bbf00f2d309a8f8a58698fa"
    )
    assert record["scope_digest"] == rm_012["human_approval"]["scope_digest"]
    assert record["topics"] == scope["policy_decisions"]
    assert record["topics"] == rm_012["human_approval"]["topics"]
    assert len(scope["milestone_implementation_scope"]["targets"]) == 17
    assert len(scope["milestone_implementation_scope"]["tests"]) == 41
    assert len(scope["milestone_implementation_scope"]["acceptance_criteria"]) == 18
    assert len(scope["policy_decisions"]) == 65
    assert document["milestone_approvals"]["P2-012A"] == reference
    assert document["milestone_approvals"]["P2-012B"] is None
    assert document["milestone_progress"]["P2-012A"]["state"] == "completed"
    assert document["milestone_progress"]["P2-012B"]["state"] == "not_started"
    assert rm_012["status"] == "in_progress"


def test_reopened_item_requires_reason_and_new_recompletion_evidence() -> None:
    previous = load_roadmap()
    reopened = deepcopy(previous)
    rm_009 = next(item for item in reopened["items"] if item["id"] == "RM-009")
    rm_009["status"] = "in_progress"
    prior_evidence = json.dumps(
        rm_009["completion_evidence"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    rm_009["reopen_events"] = [
        {
            "transition_index": 3,
            "to_status": "in_progress",
            "reason": "Regression investigation.",
            "prior_evidence_sha256": hashlib.sha256(prior_evidence).hexdigest(),
        }
    ]
    reopened["status_history"]["RM-009"].append("in_progress")
    validate_roadmap_update(previous, reopened)

    stripped_between_transitions = deepcopy(reopened)
    stripped_rm_009 = next(
        item for item in stripped_between_transitions["items"] if item["id"] == "RM-009"
    )
    stripped_rm_009["completion_evidence"]["commits"] = stripped_rm_009["completion_evidence"][
        "commits"
    ][1:]
    with pytest.raises(ValueError, match="completed evidence was removed"):
        validate_roadmap_update(reopened, stripped_between_transitions)

    recompleted = deepcopy(reopened)
    rm_009 = next(item for item in recompleted["items"] if item["id"] == "RM-009")
    rm_009["status"] = "completed"
    recompleted["status_history"]["RM-009"].append("completed")
    with pytest.raises(ValueError, match="re-completed item requires new evidence"):
        validate_roadmap_update(reopened, recompleted)

    swapped = deepcopy(recompleted)
    rm_009 = next(item for item in swapped["items"] if item["id"] == "RM-009")
    rm_009["completion_evidence"]["commits"] = rm_009["completion_evidence"]["commits"][1:]
    rm_009["completion_evidence"]["tests"].append(
        "tests/golden/test_markdown_tool_results.py::test_exact_tool_result_metadata_golden"
    )
    with pytest.raises(
        ValueError, match=r"completed evidence was removed|re-completed item removed evidence"
    ):
        validate_roadmap_update(reopened, swapped)


def test_in_progress_item_requires_completed_item_dependencies() -> None:
    counterexample = deepcopy(load_roadmap())
    rm_014 = next(item for item in counterexample["items"] if item["id"] == "RM-014")
    rm_014["status"] = "in_progress"
    rm_014["human_approval"] = {"required": False, "state": "not_required", "topics": []}
    counterexample["status_history"]["RM-014"].append("in_progress")
    with pytest.raises(ValueError, match="active item has incomplete dependency"):
        validate_roadmap(counterexample)


def test_exact_rm_and_milestone_sets_are_required() -> None:
    missing_rm = deepcopy(load_roadmap())
    missing_rm["items"] = [item for item in missing_rm["items"] if item["id"] != "RM-018A"]
    missing_rm["status_history"].pop("RM-018A")
    with pytest.raises(ValueError, match="RM set mismatch"):
        validate_roadmap(missing_rm)

    document = load_roadmap()
    assert {item["id"] for item in document["implementation_milestones"]} == (
        EXPECTED_PHASE_2_MILESTONES
    )
    cycle = deepcopy(document)
    cycle["implementation_milestones"][0]["dependencies"] = ["P2-028A"]
    with pytest.raises(ValueError, match="implementation dependency cycle"):
        validate_roadmap(cycle)

    duplicate_dependency = deepcopy(document)
    duplicate_dependency["implementation_milestones"][0]["dependencies"] = [
        "RM-006",
        "RM-006",
    ]
    with pytest.raises(ValueError, match="duplicate milestone dependency"):
        validate_roadmap(duplicate_dependency)

    status_on_ordering_node = deepcopy(document)
    status_on_ordering_node["implementation_milestones"][0]["status"] = "completed"
    with pytest.raises(ValueError, match="status-free ordering nodes"):
        validate_roadmap(status_on_ordering_node)


def test_milestone_approval_is_digest_bound_and_does_not_unlock_p2_010b() -> None:
    document = load_roadmap()
    reference = "goal-rm010-p2-010a-2026-07-20"
    record = document["approval_records"][reference]
    canonical = json.dumps(
        record["scope"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")

    assert record["scope_digest"] == hashlib.sha256(canonical).hexdigest()
    assert document["milestone_approvals"]["P2-010A"] == reference
    assert document["milestone_approvals"]["P2-010B"] is None
    assert "P2-010B" not in roadmap_summary(document)["milestone_ready_ids"]

    digest_mismatch = deepcopy(document)
    item = next(item for item in digest_mismatch["items"] if item["id"] == "RM-010")
    item["human_approval"]["scope_digest"] = "0" * 64
    with pytest.raises(ValueError, match="scope digest is not bound"):
        validate_roadmap(digest_mismatch)

    rebound = deepcopy(document)
    rebound["milestone_approvals"]["P2-010B"] = reference
    with pytest.raises(ValueError, match="approval scope contract does not match milestone"):
        validate_roadmap(rebound)

    activated = deepcopy(document)
    progress = activated["milestone_progress"]["P2-010B"]
    progress["state"] = "in_progress"
    progress["history"].append("in_progress")
    with pytest.raises(ValueError, match="active milestone lacks scoped approval"):
        validate_roadmap(activated)


def test_milestone_approval_binding_is_append_only() -> None:
    previous = load_roadmap()
    current = deepcopy(previous)
    old_reference = current["milestone_approvals"]["P2-024A"]
    new_reference = "test-p2-024a-reapproval"
    current["approval_records"][new_reference] = deepcopy(
        current["approval_records"][old_reference]
    )
    current["milestone_approvals"]["P2-024A"] = new_reference

    validate_roadmap(current)
    with pytest.raises(ValueError, match="milestone approval was deleted or rewritten"):
        validate_roadmap_update(previous, current, {new_reference})


def test_scoped_approval_projection_correction_is_strict_and_append_only() -> None:
    current = deepcopy(load_roadmap())
    current["milestone_progress"]["P2-012A"] = {
        "state": "not_started",
        "history": ["not_started"],
        "completion_evidence": {"commits": [], "paths": [], "tests": []},
    }
    current_rm_012 = next(item for item in current["items"] if item["id"] == "RM-012")
    current_rm_012["status"] = "planned"
    current["status_history"]["RM-012"] = ["proposed", "planned"]
    previous = deepcopy(current)
    old_reference = "goal-rm011-p2-011a-2026-07-20"
    new_reference = f"{old_reference}-correction-1"
    previous["approval_records"].pop(new_reference)
    previous["milestone_approvals"]["P2-011A"] = old_reference
    rm_011 = next(item for item in previous["items"] if item["id"] == "RM-011")
    rm_011["human_approval"]["approval_reference"] = old_reference
    rm_011["human_approval"]["scope_digest"] = previous["approval_records"][old_reference][
        "scope_digest"
    ]
    previous["milestone_progress"]["P2-011A"] = {
        "state": "in_progress",
        "history": ["not_started", "in_progress"],
        "completion_evidence": {"commits": [], "paths": [], "tests": []},
    }
    validate_roadmap_update(previous, current)
    validate_roadmap_update(previous, current, {"irrelevant-compatibility-value"})

    changed_topics = deepcopy(current)
    changed_topics["approval_records"][new_reference]["topics"].append("new authority")
    changed_topics["approval_records"][new_reference]["scope"]["policy_decisions"].append(
        "new authority"
    )
    changed_topics["approval_records"][new_reference]["scope_digest"] = _scope_digest(
        changed_topics["approval_records"][new_reference]["scope"]
    )
    changed_topics_rm_011 = next(item for item in changed_topics["items"] if item["id"] == "RM-011")
    changed_topics_rm_011["human_approval"]["topics"].append("new authority")
    changed_topics_rm_011["human_approval"]["scope_digest"] = changed_topics["approval_records"][
        new_reference
    ]["scope_digest"]
    with pytest.raises(ValueError, match="milestone approval was deleted or rewritten"):
        validate_roadmap_update(previous, changed_topics)

    changed_tests = deepcopy(current)
    changed_tests["approval_records"][new_reference]["scope"]["milestone_implementation_scope"][
        "tests"
    ].append("tests/new_scope.py")
    changed_tests["milestone_progress"]["P2-011A"]["completion_evidence"]["tests"].append(
        "tests/new_scope.py"
    )
    changed_tests["approval_records"][new_reference]["scope_digest"] = _scope_digest(
        changed_tests["approval_records"][new_reference]["scope"]
    )
    changed_rm_011 = next(item for item in changed_tests["items"] if item["id"] == "RM-011")
    changed_rm_011["human_approval"]["scope_digest"] = changed_tests["approval_records"][
        new_reference
    ]["scope_digest"]
    with pytest.raises(ValueError, match="milestone approval was deleted or rewritten"):
        validate_roadmap_update(previous, changed_tests)

    changed_targets = deepcopy(current)
    changed_targets["approval_records"][new_reference]["scope"]["milestone_implementation_scope"][
        "targets"
    ] = ["README.md"]
    changed_targets["milestone_progress"]["P2-011A"]["completion_evidence"]["paths"] = ["README.md"]
    changed_targets["approval_records"][new_reference]["scope_digest"] = _scope_digest(
        changed_targets["approval_records"][new_reference]["scope"]
    )
    changed_targets_rm_011 = next(
        item for item in changed_targets["items"] if item["id"] == "RM-011"
    )
    changed_targets_rm_011["human_approval"]["scope_digest"] = changed_targets["approval_records"][
        new_reference
    ]["scope_digest"]
    with pytest.raises(ValueError, match="milestone approval was deleted or rewritten"):
        validate_roadmap_update(previous, changed_targets)


def test_completed_milestone_evidence_binds_to_approved_implementation_scope() -> None:
    counterexample = deepcopy(load_roadmap())
    progress = counterexample["milestone_progress"]["P2-010A"]
    progress["completion_evidence"] = {
        "commits": ["a" * 40],
        "paths": ["future phase service modules"],
        "tests": ["phase unit/property tests"],
    }
    with pytest.raises(ValueError, match="milestone evidence does not cover approved scope"):
        validate_roadmap(counterexample)

    approved_scope = counterexample["approval_records"]["goal-rm010-p2-010a-2026-07-20"]["scope"][
        "milestone_implementation_scope"
    ]
    counterexample["milestone_progress"]["P2-010A"]["completion_evidence"] = {
        "commits": ["a" * 40],
        "paths": approved_scope["targets"],
        "tests": approved_scope["tests"],
    }
    validate_roadmap(counterexample)


def test_completed_milestone_requires_parent_dependency_and_evidence_consistency() -> None:
    counterexample = deepcopy(load_roadmap())
    parent = next(item for item in counterexample["items"] if item["id"] == "RM-028")
    parent["status"] = "in_progress"
    parent["human_approval"] = {"required": False, "state": "not_required", "topics": []}
    counterexample["status_history"]["RM-028"] = ["proposed", "planned", "in_progress"]
    progress = counterexample["milestone_progress"]["P2-028A"]
    progress["state"] = "completed"
    progress["history"] = ["not_started", "in_progress", "completed"]
    progress["completion_evidence"] = {
        "commits": ["a" * 40],
        "paths": ["future isolated job runner"],
        "tests": ["hung child and process tree"],
    }
    with pytest.raises(ValueError, match="incomplete dependency"):
        validate_roadmap(counterexample)

    pending_parent = deepcopy(load_roadmap())
    pending_parent_rm_012 = next(item for item in pending_parent["items"] if item["id"] == "RM-012")
    pending_parent_rm_012["status"] = "planned"
    pending_parent["status_history"]["RM-012"] = ["proposed", "planned"]
    with pytest.raises(ValueError, match="active milestone has inactive parent RM"):
        validate_roadmap(pending_parent)

    blocked_under_active_parent = deepcopy(load_roadmap())
    blocked_progress = blocked_under_active_parent["milestone_progress"]["P2-011A"]
    blocked_progress["state"] = "blocked"
    blocked_progress["history"] = ["not_started", "blocked"]
    blocked_progress["blockers"] = ["RM-024 incomplete"]
    with pytest.raises(
        ValueError, match=r"active item has incomplete dependency|blocked milestone requires"
    ):
        validate_roadmap(blocked_under_active_parent)

    pending_item = deepcopy(load_roadmap())
    pending_item["milestone_progress"]["P2-012A"] = {
        "state": "not_started",
        "history": ["not_started"],
        "completion_evidence": {"commits": [], "paths": [], "tests": []},
    }
    with pytest.raises(ValueError, match="in-progress RM lacks active milestone"):
        validate_roadmap(pending_item)


def test_dependency_cycle_and_completed_evidence_fail_closed() -> None:
    cycle = deepcopy(load_roadmap())
    cycle["items"][0]["dependencies"] = ["RM-023"]
    with pytest.raises(ValueError, match="cycle"):
        validate_roadmap(cycle)

    no_evidence = deepcopy(load_roadmap())
    no_evidence["items"][0]["completion_evidence"] = {
        "commits": [],
        "paths": [],
        "tests": [],
    }
    with pytest.raises(ValueError, match="lacks path/test evidence"):
        validate_roadmap(no_evidence)


def test_completed_evidence_is_repository_validated_separately() -> None:
    document = load_roadmap()
    validate_repository_evidence(document, ROOT)

    fake = deepcopy(document)
    fake["items"][0]["completion_evidence"]["paths"] = [
        "src/poker_deliberation/capabilities.py/missing"
    ]
    with pytest.raises(ValueError, match="does not exist"):
        validate_repository_evidence(fake, ROOT)

    all_references = (
        {
            reference.split("::", 1)[0]
            for item in document["items"]
            if item["status"] == "completed"
            for field in ("paths", "tests")
            for reference in item["completion_evidence"][field]
        }
        | {
            reference.split("::", 1)[0]
            for progress in document["milestone_progress"].values()
            if progress["state"] == "completed"
            for field in ("paths", "tests")
            for reference in progress["completion_evidence"][field]
        }
        | {
            reference
            for approval_reference in document["milestone_approvals"].values()
            if approval_reference is not None
            for scope in [document["approval_records"][approval_reference].get("scope")]
            if scope is not None and scope.get("schema_version") == APPROVAL_SCOPE_SCHEMA_VERSION
            for field in ("targets", "tests")
            for reference in scope["milestone_implementation_scope"][field]
        }
    )
    validate_repository_evidence(document, ROOT, tracked_paths=all_references)
    directory_prefix_paths = set(all_references)
    directory_prefix_paths.remove("tests/fixtures/phase1")
    directory_prefix_paths.add("tests/fixtures/phase1/solver_status.json")
    validate_repository_evidence(document, ROOT, tracked_paths=directory_prefix_paths)
    with pytest.raises(ValueError, match="not tracked"):
        validate_repository_evidence(document, ROOT, tracked_paths=set())

    invalid_scope = deepcopy(document)
    approval_reference = invalid_scope["milestone_approvals"]["P2-011A"]
    approval = invalid_scope["approval_records"][approval_reference]
    approval["scope"]["milestone_implementation_scope"]["targets"].append(
        "src/poker_deliberation/missing-budget-scope.py"
    )
    invalid_scope["milestone_progress"]["P2-011A"]["completion_evidence"]["paths"].append(
        "src/poker_deliberation/missing-budget-scope.py"
    )
    approval["scope_digest"] = _scope_digest(approval["scope"])
    rm_011 = next(item for item in invalid_scope["items"] if item["id"] == "RM-011")
    rm_011["human_approval"]["scope_digest"] = approval["scope_digest"]
    with pytest.raises(ValueError, match="approval scope path does not exist"):
        validate_repository_evidence(invalid_scope, ROOT)

    known_commits = {
        commit for item in document["items"] for commit in item["completion_evidence"]["commits"]
    } | {
        commit
        for progress in document["milestone_progress"].values()
        for commit in progress["completion_evidence"]["commits"]
    }
    validate_repository_evidence(document, ROOT, known_commits=known_commits)
    with pytest.raises(ValueError, match="commit does not exist"):
        validate_repository_evidence(document, ROOT, known_commits=set())

    all_commit_paths = {commit: set(all_references) for commit in known_commits}
    validate_repository_evidence(
        document,
        ROOT,
        known_commits=known_commits,
        commit_paths=all_commit_paths,
        changed_paths=all_commit_paths,
    )
    with pytest.raises(ValueError, match="absent from cited commits"):
        validate_repository_evidence(
            document,
            ROOT,
            known_commits=known_commits,
            commit_paths={commit: set() for commit in known_commits},
        )
    with pytest.raises(ValueError, match="did not change cited scope"):
        validate_repository_evidence(
            document,
            ROOT,
            known_commits=known_commits,
            commit_paths=all_commit_paths,
            changed_paths={commit: set() for commit in known_commits},
        )


def test_capability_references_match_the_capability_catalog() -> None:
    capability_ids = {item.capability_id for item in CAPABILITIES}
    referenced = {
        capability
        for item in roadmap_items()
        for capability in item["capabilities"]
        if isinstance(capability, str)
    }
    assert referenced <= capability_ids


def test_doctor_and_generated_document_are_canonical_projections() -> None:
    document = load_roadmap()
    generated_path = ROOT / str(document["source_policy"]["generated_document"])

    assert doctor()["roadmap"] == roadmap_summary(document)
    assert len(doctor()["roadmap"]["source_sha256"]) == 64
    assert doctor()["roadmap"]["milestone_state_counts"] == {
        "completed": 5,
        "not_started": 7,
    }
    assert doctor()["roadmap"]["milestone_ready_ids"] == []
    assert doctor()["roadmap"]["implementation_ready_ids"] == []
    assert doctor()["project_files_scope"] == "current_working_directory"
    assert generated_path.read_text(encoding="utf-8") == render_roadmap_markdown(document)
    assert generate_roadmap_status(["--check"]) == 0


def test_readme_and_ignore_policy_reference_canonical_and_local_planning_files() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert "docs/roadmap-status.md" in readme
    assert "/PLAN.md" in ignore
    assert "/PROGRESS.md" in ignore


def test_release_split_and_external_execution_dependencies_are_frozen() -> None:
    items = _by_id()
    pre_release = items["RM-018A"]
    stable = items["RM-018B"]

    assert pre_release["phase"] == "pre-release"
    assert stable["phase"] == "stable-release"
    assert {"RM-010", "RM-011", "RM-012", "RM-013", "RM-024", "RM-027"} <= set(
        stable["dependencies"]
    )
    assert "RM-028" in items["RM-019"]["dependencies"]
    assert "RM-028" in items["RM-020"]["dependencies"]
    assert roadmap_summary()["release_readiness"]["pre_release"]["candidate_evidence"] == (
        "not_evaluated"
    )


def test_release_checklist_matches_machine_readable_gates() -> None:
    checklist = (ROOT / "docs" / "public-release-checklist.md").read_text(encoding="utf-8")
    items = _by_id()
    for rm_id in ("RM-018A", "RM-018B"):
        assert rm_id in checklist
        assert items[rm_id]["status"] == "planned"
        assert items[rm_id]["completion_evidence"] == {"commits": [], "paths": [], "tests": []}
    assert "candidate固有" in checklist
    assert "not_evaluated" in str(roadmap_summary()["release_readiness"])


@pytest.mark.parametrize("rm_id", ["RM-010", "RM-011", "RM-012", "RM-013"])
def test_phase_2_preimplementation_contract_has_every_required_dimension(rm_id: str) -> None:
    text = (ROOT / "docs" / "phase2-readiness-contracts.md").read_text(encoding="utf-8")
    start = text.index(f"## {rm_id}")
    remaining = text[start + len(f"## {rm_id}") :]
    next_section = remaining.find("\n---\n")
    section = remaining if next_section == -1 else remaining[:next_section]
    lowered = section.lower()

    required_terms = {
        "目的と非目標",
        "対象",
        "public / backward compatibility",
        "typed",
        "precondition",
        "postcondition",
        "failure",
        "artifact",
        "idempotency",
        "cancellation",
        "timeout",
        "concurrency",
        "migration",
        "security",
        "acceptance",
        "unit",
        "property",
        "integration",
        "adversarial",
        "fault injection",
        "dependencies",
        "human approval",
        "safe commit units",
    }
    assert not {term for term in required_terms if term not in lowered}


def test_phase_2_cross_contracts_freeze_reviewed_counterexamples() -> None:
    text = (ROOT / "docs" / "phase2-readiness-contracts.md").read_text(encoding="utf-8")
    required_phrases = {
        "P2-010A",
        "P2-012A",
        "P2-027A",
        "P2-027B",
        "immutable revision",
        "current` pointer",
        "idempotency key lookup",
        "domain mutation 0",
        "audit_sequence",
        "effect_unknown / manual_reconciliation_required",
        "historical_only/stale",
        "default deny",
        "transitive identity",
        "authenticity証明ではない",
        "RunReadStatus",
    }
    assert not {phrase for phrase in required_phrases if phrase not in text}

    milestones = {item["id"]: item for item in load_roadmap()["implementation_milestones"]}
    assert milestones["P2-012A"]["dependencies"] == [
        "RM-023",
        "P2-010A",
        "P2-011A",
        "P2-027A",
    ]
    assert set(milestones["P2-027B"]["dependencies"]) == {"P2-012B", "P2-013A"}
    assert "filesystem read/write allowlists" in _by_id()["RM-028"]["acceptance_criteria"][0]


def test_distribution_claim_stays_unknown_until_rm_018a() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    generated = (ROOT / "docs" / "roadmap-status.md").read_text(encoding="utf-8")
    assert "wheel/sdist同梱はRM-018Aまで`UNKNOWN`" in readme
    assert "wheel/sdist同梱はRM-018A" in generated
