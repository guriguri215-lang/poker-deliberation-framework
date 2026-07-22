"""Packaged roadmap status loading, validation, summaries, and documentation rendering."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from importlib import resources
from itertools import pairwise
from pathlib import Path
from typing import Any

ROADMAP_RESOURCE = "roadmap_status.json"
ROADMAP_SCHEMA_VERSION = "1.1.0"
LEGACY_ROADMAP_SCHEMA_VERSION = "1.0.0"
SUPPORTED_ROADMAP_SCHEMA_VERSIONS = {
    LEGACY_ROADMAP_SCHEMA_VERSION,
    ROADMAP_SCHEMA_VERSION,
}
APPROVAL_SCOPE_SCHEMA_VERSION = "1.1.0"
LEGACY_APPROVAL_SCOPE_SCHEMA_VERSION = "1.0.0"
SUPPORTED_APPROVAL_SCOPE_SCHEMA_VERSIONS = {
    LEGACY_APPROVAL_SCOPE_SCHEMA_VERSION,
    APPROVAL_SCOPE_SCHEMA_VERSION,
}
ROADMAP_SCHEMA_AMENDMENT_REFERENCE = "goal-rm010-p2-010a-governance-amendment-2026-07-20"
RM_ID_PATTERN = re.compile(r"^RM-[0-9]{3}[AB]?$")
MILESTONE_ID_PATTERN = re.compile(r"^P2-[0-9]{3}[AB]$")
COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
ALLOWED_PRIORITIES = {"P0", "P1", "P2", "P3"}
ALLOWED_PHASES = {
    "readiness",
    "phase-0",
    "phase-1",
    "phase-2",
    "phase-3",
    "phase-4",
    "phase-5",
    "post-phase-2",
    "pre-release",
    "stable-release",
}
ALLOWED_APPROVAL_STATES = {"not_required", "approved_scope", "pending"}
EXPECTED_RM_IDS = (
    {f"RM-{number:03d}" for number in range(1, 18)}
    | {"RM-018A", "RM-018B"}
    | {f"RM-{number:03d}" for number in range(19, 29)}
)
EXPECTED_PHASE_2_MILESTONES = {
    "P2-024A",
    "P2-010A",
    "P2-011A",
    "P2-027A",
    "P2-012A",
    "P2-010B",
    "P2-011B",
    "P2-012B",
    "P2-013A",
    "P2-027B",
    "P2-013B",
    "P2-028A",
}
MILESTONE_TRANSITIONS = {
    "not_started": ["in_progress", "blocked"],
    "in_progress": ["not_started", "blocked", "completed"],
    "blocked": ["not_started", "in_progress"],
    "completed": [],
}
IMMUTABLE_ITEM_CONTRACT_FIELDS = {
    "id",
    "title",
    "phase",
    "priority",
    "dependencies",
    "capabilities",
    "objective",
    "targets",
    "acceptance_criteria",
    "tests",
    "entry_milestone",
    "completion_milestone",
    "evidence_mode",
    "relations",
}


def _require_dict(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _require_string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} must be a list of strings")
    return list(value)


def _document_digest(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _topic_digest(topics: list[str]) -> str:
    canonical = json.dumps(
        topics, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _item_contract_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    return {field: item.get(field) for field in sorted(IMMUTABLE_ITEM_CONTRACT_FIELDS)}


def _milestone_contract_snapshot(milestone: dict[str, Any]) -> dict[str, Any]:
    return {field: milestone.get(field) for field in ("id", "rm_id", "dependencies", "scope")}


def _approval_milestone_id(record: dict[str, Any]) -> str | None:
    if "scope" not in record:
        return None
    scope = _require_dict(record["scope"], "approval scope")
    if scope.get("schema_version") == LEGACY_APPROVAL_SCOPE_SCHEMA_VERSION:
        value = scope.get("milestone_id")
    else:
        value = _require_dict(scope.get("milestone_contract"), "approval milestone contract").get(
            "id"
        )
    return value if isinstance(value, str) else None


def _validate_scoped_approval_record(
    reference: str,
    record: dict[str, Any],
    *,
    rm_id: str | None = None,
    item: dict[str, Any] | None = None,
    milestone: dict[str, Any] | None = None,
) -> None:
    scope = _require_dict(record.get("scope"), f"approval_records.{reference}.scope")
    schema_version = scope.get("schema_version")
    if schema_version not in SUPPORTED_APPROVAL_SCOPE_SCHEMA_VERSIONS:
        raise ValueError(f"unsupported approval scope schema: {reference}")
    expected_fields = (
        {
            "schema_version",
            "rm_id",
            "milestone_id",
            "item_contract",
            "policy_decisions",
        }
        if schema_version == LEGACY_APPROVAL_SCOPE_SCHEMA_VERSION
        else {
            "schema_version",
            "rm_id",
            "milestone_contract",
            "item_contract",
            "milestone_implementation_scope",
            "policy_decisions",
        }
    )
    if set(scope) != expected_fields:
        raise ValueError(f"invalid approval scope fields: {reference}")
    if not isinstance(scope.get("rm_id"), str) or not RM_ID_PATTERN.fullmatch(scope["rm_id"]):
        raise ValueError(f"invalid approval scope RM: {reference}")
    milestone_id = _approval_milestone_id(record)
    if not isinstance(milestone_id, str) or not MILESTONE_ID_PATTERN.fullmatch(milestone_id):
        raise ValueError(f"invalid approval scope milestone: {reference}")
    decisions = _require_string_list(
        scope.get("policy_decisions"), f"approval_records.{reference}.policy_decisions"
    )
    if decisions != _require_string_list(record["topics"], f"{reference}.topics"):
        raise ValueError(f"approval scope decisions do not match topics: {reference}")
    contract = _require_dict(
        scope.get("item_contract"), f"approval_records.{reference}.item_contract"
    )
    if set(contract) != IMMUTABLE_ITEM_CONTRACT_FIELDS:
        raise ValueError(f"approval scope item contract is incomplete: {reference}")
    if schema_version == APPROVAL_SCOPE_SCHEMA_VERSION:
        milestone_contract = _require_dict(
            scope.get("milestone_contract"),
            f"approval_records.{reference}.milestone_contract",
        )
        if set(milestone_contract) != {"id", "rm_id", "dependencies", "scope"}:
            raise ValueError(f"invalid approval milestone contract: {reference}")
        if milestone_contract.get("rm_id") != scope["rm_id"]:
            raise ValueError(f"approval milestone owner mismatch: {reference}")
        dependencies = _require_string_list(
            milestone_contract.get("dependencies"),
            f"approval_records.{reference}.milestone_contract.dependencies",
        )
        if len(dependencies) != len(set(dependencies)):
            raise ValueError(f"duplicate approval milestone dependency: {reference}")
        if (
            not isinstance(milestone_contract.get("scope"), str)
            or not milestone_contract["scope"].strip()
        ):
            raise ValueError(f"empty approval milestone scope: {reference}")
        implementation_scope = _require_dict(
            scope.get("milestone_implementation_scope"),
            f"approval_records.{reference}.milestone_implementation_scope",
        )
        if set(implementation_scope) != {"targets", "tests", "acceptance_criteria"}:
            raise ValueError(f"invalid milestone implementation scope: {reference}")
        for field in ("targets", "tests", "acceptance_criteria"):
            values = _require_string_list(
                implementation_scope.get(field),
                f"approval_records.{reference}.milestone_implementation_scope.{field}",
            )
            if not values or len(values) != len(set(values)):
                raise ValueError(f"invalid milestone implementation scope {field}: {reference}")
    if rm_id is None or item is None:
        if milestone is not None:
            raise ValueError("approval milestone validation requires its parent item")
    else:
        if scope["rm_id"] != rm_id:
            raise ValueError(f"approval scope RM does not match item: {rm_id}")
        if milestone_id not in {
            item.get("entry_milestone"),
            item.get("completion_milestone"),
        }:
            raise ValueError(f"approval scope milestone does not match item: {rm_id}")
        if contract != _item_contract_snapshot(item):
            raise ValueError(f"approval scope contract does not match item: {rm_id}")
    if milestone is not None:
        if schema_version != APPROVAL_SCOPE_SCHEMA_VERSION:
            expected_id = milestone.get("id")
            if milestone_id != expected_id or scope["rm_id"] != milestone.get("rm_id"):
                raise ValueError(f"legacy approval scope does not match milestone: {expected_id}")
        elif _require_dict(scope["milestone_contract"], "milestone_contract") != (
            _milestone_contract_snapshot(milestone)
        ):
            raise ValueError(f"approval scope contract does not match milestone: {milestone_id}")


def _milestone_approval_map(
    document: dict[str, Any],
    items: dict[str, dict[str, Any]],
    milestones: dict[str, dict[str, Any]],
    approval_records: dict[str, dict[str, Any]],
) -> dict[str, str | None]:
    if document.get("schema_version") == LEGACY_ROADMAP_SCHEMA_VERSION:
        derived: dict[str, str | None] = {}
        for milestone_id, milestone in milestones.items():
            parent = items[str(milestone["rm_id"])]
            approval = _require_dict(parent["human_approval"], "human_approval")
            reference = approval.get("approval_reference")
            record = approval_records.get(reference) if isinstance(reference, str) else None
            derived[milestone_id] = (
                reference
                if record is not None and _approval_milestone_id(record) == milestone_id
                else None
            )
        return derived

    raw_bindings = _require_dict(document.get("milestone_approvals"), "milestone_approvals")
    if set(raw_bindings) != set(milestones):
        raise ValueError("milestone approval IDs must exactly match milestone IDs")
    bindings: dict[str, str | None] = {}
    for milestone_id, raw_reference in raw_bindings.items():
        if raw_reference is None:
            bindings[milestone_id] = None
            continue
        if not isinstance(raw_reference, str) or raw_reference not in approval_records:
            raise ValueError(f"unknown milestone approval reference: {milestone_id}")
        record = approval_records[raw_reference]
        if "scope" not in record:
            raise ValueError(f"milestone approval is not scoped: {milestone_id}")
        milestone = milestones[milestone_id]
        parent = items[str(milestone["rm_id"])]
        _validate_scoped_approval_record(
            raw_reference,
            record,
            rm_id=str(milestone["rm_id"]),
            item=parent,
            milestone=milestone,
        )
        if _approval_milestone_id(record) != milestone_id:
            raise ValueError(f"milestone approval does not match milestone: {milestone_id}")
        bindings[milestone_id] = raw_reference
    return bindings


def _evidence_is_contract_bound(reference: str, declared: list[str]) -> bool:
    path = reference.split("::", 1)[0].replace("\\", "/").rstrip("/")
    for raw_declaration in declared:
        declaration = raw_declaration.split("::", 1)[0].replace("\\", "/").rstrip("/")
        if path == declaration or path.startswith(f"{declaration}/"):
            return True
    return False


def load_roadmap() -> dict[str, Any]:
    """Load and validate the packaged canonical roadmap resource from any CWD."""

    raw = (
        resources.files("poker_deliberation").joinpath(ROADMAP_RESOURCE).read_text(encoding="utf-8")
    )
    document = _require_dict(json.loads(raw), "roadmap")
    validate_roadmap(document)
    return document


def validate_transition(source: str, target: str, transitions: dict[str, Any]) -> None:
    """Validate one recorded status transition."""

    if source not in transitions:
        raise ValueError(f"unknown transition source: {source}")
    allowed = _require_string_list(transitions[source], f"legal_transitions.{source}")
    if target not in allowed:
        raise ValueError(f"illegal status transition: {source} -> {target}")


def _validate_milestones(
    document: dict[str, Any],
    items: dict[str, dict[str, Any]],
    approval_records: dict[str, dict[str, Any]],
) -> None:
    raw_milestones = document.get("implementation_milestones")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        raise ValueError("implementation_milestones must be a non-empty list")
    milestones: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_milestones):
        milestone = _require_dict(raw, f"implementation_milestones[{index}]")
        if set(milestone) != {"id", "rm_id", "dependencies", "scope"}:
            raise ValueError("implementation milestones are status-free ordering nodes")
        milestone_id = milestone.get("id")
        if not isinstance(milestone_id, str) or not MILESTONE_ID_PATTERN.fullmatch(milestone_id):
            raise ValueError(f"invalid implementation milestone id: {milestone_id}")
        if milestone_id in milestones:
            raise ValueError(f"duplicate implementation milestone id: {milestone_id}")
        if milestone.get("rm_id") not in items:
            raise ValueError(f"unknown RM for implementation milestone: {milestone_id}")
        if not isinstance(milestone.get("scope"), str) or not milestone["scope"].strip():
            raise ValueError(f"empty milestone scope: {milestone_id}")
        milestone_dependencies = _require_string_list(
            milestone.get("dependencies"), f"{milestone_id}.dependencies"
        )
        if len(milestone_dependencies) != len(set(milestone_dependencies)):
            raise ValueError(f"duplicate milestone dependency: {milestone_id}")
        milestones[milestone_id] = milestone
    if set(milestones) != EXPECTED_PHASE_2_MILESTONES:
        missing = sorted(EXPECTED_PHASE_2_MILESTONES - set(milestones))
        extra = sorted(set(milestones) - EXPECTED_PHASE_2_MILESTONES)
        raise ValueError(f"implementation milestone set mismatch; missing={missing}, extra={extra}")

    nodes = set(items) | set(milestones)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError(f"implementation dependency cycle includes {node_id}")
        if node_id in visited or node_id in items:
            return
        visiting.add(node_id)
        dependencies = _require_string_list(milestones[node_id]["dependencies"], "dependencies")
        for dependency in dependencies:
            if dependency not in nodes:
                raise ValueError(f"unknown milestone dependency {dependency} for {node_id}")
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for milestone_id in milestones:
        visit(milestone_id)

    milestone_approvals = _milestone_approval_map(document, items, milestones, approval_records)

    raw_progress = _require_dict(document.get("milestone_progress"), "milestone_progress")
    if set(raw_progress) != set(milestones):
        raise ValueError("milestone progress IDs must exactly match milestone IDs")
    progress: dict[str, dict[str, Any]] = {}
    for milestone_id, raw in raw_progress.items():
        entry = _require_dict(raw, f"milestone_progress.{milestone_id}")
        required_fields = {"state", "history", "completion_evidence"}
        optional_fields = {"blockers"}
        if not required_fields <= set(entry) or not set(entry) <= required_fields | optional_fields:
            raise ValueError(f"invalid milestone progress fields: {milestone_id}")
        state = entry.get("state")
        if state not in MILESTONE_TRANSITIONS:
            raise ValueError(f"invalid milestone state: {milestone_id}")
        history = _require_string_list(entry.get("history"), f"{milestone_id}.history")
        if not history or history[-1] != state:
            raise ValueError(f"milestone history does not match state: {milestone_id}")
        for source, target in pairwise(history):
            if target not in MILESTONE_TRANSITIONS.get(source, []):
                raise ValueError(f"illegal milestone transition: {source} -> {target}")
        evidence = _require_dict(entry.get("completion_evidence"), f"{milestone_id}.evidence")
        evidence_lists = {
            field: _require_string_list(evidence.get(field), f"{milestone_id}.evidence.{field}")
            for field in ("commits", "paths", "tests")
        }
        if any(not COMMIT_PATTERN.fullmatch(commit) for commit in evidence_lists["commits"]):
            raise ValueError(f"milestone evidence must use full Git object IDs: {milestone_id}")
        if state == "completed" and any(not values for values in evidence_lists.values()):
            raise ValueError(f"completed milestone lacks evidence: {milestone_id}")
        if state == "completed":
            parent = items[str(milestones[milestone_id]["rm_id"])]
            approval_reference = milestone_approvals[milestone_id]
            approval_record = (
                approval_records[approval_reference] if approval_reference is not None else None
            )
            if (
                approval_record is not None
                and "scope" in approval_record
                and _require_dict(approval_record["scope"], "approval scope").get("schema_version")
                == APPROVAL_SCOPE_SCHEMA_VERSION
            ):
                implementation_scope = _require_dict(
                    _require_dict(approval_record["scope"], "approval scope")[
                        "milestone_implementation_scope"
                    ],
                    "milestone implementation scope",
                )
                declared_targets = _require_string_list(implementation_scope["targets"], "targets")
                declared_tests = _require_string_list(implementation_scope["tests"], "tests")
                if (
                    len(evidence_lists["paths"]) != len(declared_targets)
                    or set(evidence_lists["paths"]) != set(declared_targets)
                    or len(evidence_lists["tests"]) != len(declared_tests)
                    or set(evidence_lists["tests"]) != set(declared_tests)
                ):
                    raise ValueError(
                        f"milestone evidence does not cover approved scope: {milestone_id}"
                    )
                declared = declared_targets + declared_tests
            else:
                declared = _require_string_list(
                    parent["targets"], "targets"
                ) + _require_string_list(parent["tests"], "tests")
            if any(
                not _evidence_is_contract_bound(reference, declared)
                for reference in evidence_lists["paths"] + evidence_lists["tests"]
            ):
                raise ValueError(f"milestone evidence is not contract-bound: {milestone_id}")
        if state == "blocked" and not _require_string_list(
            entry.get("blockers"), f"{milestone_id}.blockers"
        ):
            raise ValueError(f"blocked milestone lacks blockers: {milestone_id}")
        progress[milestone_id] = entry

    for milestone_id, milestone in milestones.items():
        state = progress[milestone_id]["state"]
        parent_item = items[str(milestone["rm_id"])]
        if state == "blocked" and parent_item["status"] != "blocked":
            raise ValueError(f"blocked milestone requires blocked parent RM: {milestone_id}")
        if state not in {"in_progress", "completed"}:
            continue
        if parent_item["status"] not in {"in_progress", "completed"}:
            raise ValueError(f"active milestone has inactive parent RM: {milestone_id}")
        parent_approval = _require_dict(parent_item["human_approval"], "human_approval")
        if parent_approval["required"] and parent_approval["state"] != "approved_scope":
            raise ValueError(f"active milestone lacks parent approval: {milestone_id}")
        if parent_approval["required"] and milestone_approvals[milestone_id] is None:
            raise ValueError(f"active milestone lacks scoped approval: {milestone_id}")
        for dependency in _require_string_list(milestone["dependencies"], "dependencies"):
            dependency_completed = (
                items[dependency]["status"] == "completed"
                if dependency in items
                else progress[dependency]["state"] == "completed"
            )
            if not dependency_completed:
                raise ValueError(f"active milestone has incomplete dependency: {milestone_id}")

    completion_targets: set[str] = set()
    for rm_id in ("RM-010", "RM-011", "RM-012", "RM-013", "RM-024", "RM-027", "RM-028"):
        entry_milestone = items[rm_id].get("entry_milestone")
        completion_milestone = items[rm_id].get("completion_milestone")
        if not isinstance(entry_milestone, str) or entry_milestone not in milestones:
            raise ValueError(f"invalid entry milestone for {rm_id}")
        if milestones[entry_milestone]["rm_id"] != rm_id:
            raise ValueError(f"invalid entry milestone owner for {rm_id}")
        if not isinstance(completion_milestone, str) or completion_milestone not in milestones:
            raise ValueError(f"invalid completion milestone for {rm_id}")
        if milestones[completion_milestone]["rm_id"] != rm_id:
            raise ValueError(f"invalid completion milestone owner for {rm_id}")
        if completion_milestone in completion_targets:
            raise ValueError(f"duplicate completion milestone: {completion_milestone}")
        completion_targets.add(completion_milestone)
        if (
            items[rm_id]["status"] == "completed"
            and progress[completion_milestone]["state"] != "completed"
        ):
            raise ValueError(f"completed RM lacks completed gate milestone: {rm_id}")
        if items[rm_id]["status"] == "completed" and entry_milestone == completion_milestone:
            parent_evidence = _require_dict(
                items[rm_id]["completion_evidence"], f"{rm_id}.completion_evidence"
            )
            milestone_evidence = _require_dict(
                progress[completion_milestone]["completion_evidence"],
                f"{completion_milestone}.completion_evidence",
            )
            if parent_evidence != milestone_evidence:
                raise ValueError(f"single-milestone RM evidence differs from milestone: {rm_id}")
        owned_states = [
            progress[milestone_id]["state"]
            for milestone_id, milestone in milestones.items()
            if milestone["rm_id"] == rm_id
        ]
        if items[rm_id]["status"] == "planned" and any(
            state != "not_started" for state in owned_states
        ):
            raise ValueError(f"planned RM has active milestone: {rm_id}")
        if items[rm_id]["status"] == "in_progress" and all(
            state == "not_started" for state in owned_states
        ):
            raise ValueError(f"in-progress RM lacks active milestone: {rm_id}")
        if items[rm_id]["status"] == "in_progress" and "blocked" in owned_states:
            raise ValueError(f"in-progress RM has blocked milestone: {rm_id}")
        if items[rm_id]["status"] == "blocked" and "blocked" not in owned_states:
            raise ValueError(f"blocked RM lacks blocked milestone: {rm_id}")


def validate_roadmap(document: dict[str, Any]) -> None:
    """Fail closed on malformed status, evidence, approval, references, or dependency cycles."""

    if document.get("schema_version") not in SUPPORTED_ROADMAP_SCHEMA_VERSIONS:
        raise ValueError("unsupported roadmap schema version")

    source_policy = _require_dict(document.get("source_policy"), "source_policy")
    if source_policy.get("canonical") is not True:
        raise ValueError("roadmap source must declare itself canonical")
    if not isinstance(source_policy.get("generated_document"), str):
        raise ValueError("source_policy.generated_document must be a string")
    if (
        not isinstance(source_policy.get("history_baseline"), str)
        or not source_policy["history_baseline"].strip()
    ):
        raise ValueError("source_policy.history_baseline must be a non-empty string")

    raw_approval_records = _require_dict(document.get("approval_records"), "approval_records")
    approval_records: dict[str, dict[str, Any]] = {}
    for reference, raw_record in raw_approval_records.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9._:-]+", reference):
            raise ValueError(f"invalid approval record reference: {reference}")
        record = _require_dict(raw_record, f"approval_records.{reference}")
        legacy_fields = {"source_label", "topics", "scope_digest"}
        scoped_fields = legacy_fields | {"scope"}
        if frozenset(record) not in {frozenset(legacy_fields), frozenset(scoped_fields)}:
            raise ValueError(f"invalid approval record fields: {reference}")
        if not isinstance(record["source_label"], str) or not record["source_label"].strip():
            raise ValueError(f"invalid approval source label: {reference}")
        record_topics = _require_string_list(record["topics"], f"{reference}.topics")
        if not record_topics:
            raise ValueError(f"approval record topics are empty: {reference}")
        expected_digest = (
            _document_digest(_require_dict(record["scope"], f"{reference}.scope"))
            if "scope" in record
            else _topic_digest(record_topics)
        )
        if record["scope_digest"] != expected_digest:
            raise ValueError(f"approval record digest mismatch: {reference}")
        if "scope" in record:
            _validate_scoped_approval_record(reference, record)
        approval_records[reference] = record

    vocabulary = _require_dict(document.get("status_vocabulary"), "status_vocabulary")
    transitions = _require_dict(document.get("legal_transitions"), "legal_transitions")
    if set(vocabulary) != set(transitions):
        raise ValueError("status vocabulary and transition keys must match")
    for source, raw_targets in transitions.items():
        targets = _require_string_list(raw_targets, f"legal_transitions.{source}")
        if not set(targets) <= set(vocabulary):
            raise ValueError(f"unknown transition target from {source}")
        if source in targets:
            raise ValueError(f"self transition is not legal for {source}")

    raw_items = document.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty list")
    items: dict[str, dict[str, Any]] = {}
    for index, raw_item in enumerate(raw_items):
        item = _require_dict(raw_item, f"items[{index}]")
        for field in ("id", "title", "phase", "priority", "status", "objective", "status_reason"):
            if not isinstance(item.get(field), str) or not str(item[field]).strip():
                raise ValueError(f"items[{index}].{field} must be a non-empty string")
        for field in ("dependencies", "capabilities", "targets", "acceptance_criteria", "tests"):
            values = _require_string_list(item.get(field), f"items[{index}].{field}")
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate value in {item['id']}.{field}")

        rm_id = str(item["id"])
        if not RM_ID_PATTERN.fullmatch(rm_id):
            raise ValueError(f"invalid RM id: {rm_id}")
        if rm_id in items:
            raise ValueError(f"duplicate RM id: {rm_id}")
        if item["phase"] not in ALLOWED_PHASES:
            raise ValueError(f"invalid phase for {rm_id}")
        if item["priority"] not in ALLOWED_PRIORITIES:
            raise ValueError(f"invalid priority for {rm_id}")
        if item["status"] not in vocabulary:
            raise ValueError(f"invalid status for {rm_id}")

        evidence = _require_dict(item.get("completion_evidence"), f"{rm_id}.completion_evidence")
        evidence_lists = {
            field: _require_string_list(evidence.get(field), f"{rm_id}.completion_evidence.{field}")
            for field in ("commits", "paths", "tests")
        }
        if any(not COMMIT_PATTERN.fullmatch(commit) for commit in evidence_lists["commits"]):
            raise ValueError(f"completion evidence must use full Git object IDs: {rm_id}")
        if item["status"] == "completed":
            if not evidence_lists["paths"] or not evidence_lists["tests"]:
                raise ValueError(f"completed item lacks path/test evidence: {rm_id}")
            if not evidence_lists["commits"] and item.get("evidence_mode") != "enclosing_commit":
                raise ValueError(f"completed item lacks commit evidence mode: {rm_id}")
            declared_evidence = _require_string_list(item["targets"], "targets") + (
                _require_string_list(item["tests"], "tests")
            )
            if any(
                not _evidence_is_contract_bound(reference, declared_evidence)
                for reference in evidence_lists["paths"] + evidence_lists["tests"]
            ):
                raise ValueError(f"completion evidence is not bound to targets/tests: {rm_id}")

        approval = _require_dict(item.get("human_approval"), f"{rm_id}.human_approval")
        if not isinstance(approval.get("required"), bool):
            raise ValueError(f"{rm_id}.human_approval.required must be boolean")
        if approval.get("state") not in ALLOWED_APPROVAL_STATES:
            raise ValueError(f"invalid approval state for {rm_id}")
        topics = _require_string_list(approval.get("topics"), f"{rm_id}.human_approval.topics")
        if approval["required"] and (not topics or approval["state"] == "not_required"):
            raise ValueError(f"required approval has invalid state/topics: {rm_id}")
        if not approval["required"] and approval["state"] != "not_required":
            raise ValueError(f"non-required approval must use not_required: {rm_id}")
        if approval["state"] == "approved_scope" and not any(
            isinstance(approval.get(field), str) and approval[field].strip()
            for field in ("approval_reference", "scope_digest")
        ):
            raise ValueError(f"approved scope lacks reference or digest: {rm_id}")
        if approval["state"] == "approved_scope":
            approval_reference = approval.get("approval_reference")
            if (
                not isinstance(approval_reference, str)
                or approval_reference not in approval_records
            ):
                raise ValueError(f"approved scope lacks tracked approval record: {rm_id}")
            approved_topics = _require_string_list(
                approval_records[approval_reference]["topics"], f"{approval_reference}.topics"
            )
            approval_record = approval_records[approval_reference]
            if not set(topics) <= set(approved_topics):
                raise ValueError(f"approval topics exceed tracked scope: {rm_id}")
            if "scope" in approval_record:
                if topics != approved_topics:
                    raise ValueError(f"scoped approval topics must match exactly: {rm_id}")
                approval_scope = _require_dict(approval_record["scope"], "approval scope")
                if (
                    approval_scope.get("schema_version") == APPROVAL_SCOPE_SCHEMA_VERSION
                    and approval.get("scope_digest") != approval_record["scope_digest"]
                ):
                    raise ValueError(f"approved scope digest is not bound to record: {rm_id}")
                _validate_scoped_approval_record(
                    approval_reference,
                    approval_record,
                    rm_id=rm_id,
                    item=item,
                )
        if (
            item["status"] == "completed"
            and approval["required"]
            and approval["state"] != "approved_scope"
        ):
            raise ValueError(f"completed item lacks required approval: {rm_id}")
        if (
            item["status"] == "in_progress"
            and approval["required"]
            and approval["state"] != "approved_scope"
        ):
            raise ValueError(f"in-progress item lacks required approval: {rm_id}")
        if item["status"] == "blocked" and not _require_string_list(
            item.get("blockers"), f"{rm_id}.blockers"
        ):
            raise ValueError(f"blocked item lacks blockers: {rm_id}")
        items[rm_id] = item

    if set(items) != EXPECTED_RM_IDS:
        missing = sorted(EXPECTED_RM_IDS - set(items))
        extra = sorted(set(items) - EXPECTED_RM_IDS)
        raise ValueError(f"RM set mismatch; missing={missing}, extra={extra}")

    raw_history = _require_dict(document.get("status_history"), "status_history")
    if set(raw_history) != set(items):
        raise ValueError("status history IDs must exactly match RM IDs")
    for rm_id, item in items.items():
        history = _require_string_list(raw_history[rm_id], f"status_history.{rm_id}")
        if not history or history[-1] != item["status"]:
            raise ValueError(f"status history does not match current status: {rm_id}")
        if any(status not in vocabulary for status in history):
            raise ValueError(f"unknown status in history: {rm_id}")
        for source, target in pairwise(history):
            validate_transition(source, target, transitions)
        expected_reopens = [
            (index, target)
            for index, (source, target) in enumerate(pairwise(history), start=1)
            if source == "completed" and target in {"in_progress", "blocked"}
        ]
        raw_reopen_events = item.get("reopen_events", [])
        if not isinstance(raw_reopen_events, list):
            raise ValueError(f"reopen_events must be a list: {rm_id}")
        if len(raw_reopen_events) != len(expected_reopens):
            raise ValueError(f"reopen events do not match status history: {rm_id}")
        for raw_event, (transition_index, target) in zip(
            raw_reopen_events, expected_reopens, strict=True
        ):
            event = _require_dict(raw_event, f"{rm_id}.reopen_event")
            if set(event) != {"transition_index", "to_status", "reason", "prior_evidence_sha256"}:
                raise ValueError(f"invalid reopen event fields: {rm_id}")
            if event["transition_index"] != transition_index or event["to_status"] != target:
                raise ValueError(f"reopen event does not match transition: {rm_id}")
            if not isinstance(event["reason"], str) or not event["reason"].strip():
                raise ValueError(f"reopen event lacks reason: {rm_id}")
            if not isinstance(event["prior_evidence_sha256"], str) or not re.fullmatch(
                r"[0-9a-f]{64}", event["prior_evidence_sha256"]
            ):
                raise ValueError(f"reopen event lacks evidence digest: {rm_id}")

    for rm_id, item in items.items():
        dependencies = _require_string_list(item["dependencies"], f"{rm_id}.dependencies")
        for dependency in dependencies:
            if dependency == rm_id:
                raise ValueError(f"self dependency for {rm_id}")
            if dependency not in items:
                raise ValueError(f"unknown dependency {dependency} for {rm_id}")
        if item["status"] in {"in_progress", "completed"} and any(
            items[dependency]["status"] != "completed" for dependency in dependencies
        ):
            raise ValueError(f"active item has incomplete dependency: {rm_id}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(rm_id: str) -> None:
        if rm_id in visiting:
            raise ValueError(f"roadmap dependency cycle includes {rm_id}")
        if rm_id in visited:
            return
        visiting.add(rm_id)
        for dependency in _require_string_list(items[rm_id]["dependencies"], "dependencies"):
            visit(dependency)
        visiting.remove(rm_id)
        visited.add(rm_id)

    for rm_id in items:
        visit(rm_id)
    _validate_milestones(document, items, approval_records)


def validate_roadmap_update(
    previous: dict[str, Any],
    current: dict[str, Any],
    newly_approved_references: set[str] | None = None,
) -> None:
    """Validate append-only history; the third argument is compatibility-only."""

    validate_roadmap(previous)
    validate_roadmap(current)
    del newly_approved_references
    previous_schema = previous.get("schema_version")
    current_schema = current.get("schema_version")
    if previous_schema != current_schema:
        migration_is_bound = (
            previous_schema == LEGACY_ROADMAP_SCHEMA_VERSION
            and current_schema == ROADMAP_SCHEMA_VERSION
            and ROADMAP_SCHEMA_AMENDMENT_REFERENCE
            in _require_dict(current.get("approval_records"), "approval_records")
            and ROADMAP_SCHEMA_AMENDMENT_REFERENCE
            not in _require_dict(previous.get("approval_records"), "approval_records")
        )
        if not migration_is_bound:
            raise ValueError("roadmap schema migration lacks its append-only amendment record")
    for field in (
        "source_policy",
        "status_vocabulary",
        "legal_transitions",
        "implementation_milestones",
    ):
        if previous.get(field) != current.get(field):
            raise ValueError(f"canonical contract field requires a schema amendment: {field}")
    previous_history = _require_dict(previous["status_history"], "previous.status_history")
    current_history = _require_dict(current["status_history"], "current.status_history")
    previous_items = {str(item["id"]): item for item in roadmap_items(previous)}
    current_items = {str(item["id"]): item for item in roadmap_items(current)}

    old_records = _require_dict(previous["approval_records"], "previous.approval_records")
    new_records = _require_dict(current["approval_records"], "current.approval_records")
    for reference, record in old_records.items():
        if new_records.get(reference) != record:
            raise ValueError(f"approval record was deleted or rewritten: {reference}")
    appended_records = set(new_records) - set(old_records)

    old_milestones = {
        str(milestone["id"]): _require_dict(milestone, "milestone")
        for milestone in previous["implementation_milestones"]
    }
    new_milestones = {
        str(milestone["id"]): _require_dict(milestone, "milestone")
        for milestone in current["implementation_milestones"]
    }
    old_bindings = _milestone_approval_map(previous, previous_items, old_milestones, old_records)
    new_bindings = _milestone_approval_map(current, current_items, new_milestones, new_records)
    for milestone_id, old_reference in old_bindings.items():
        new_reference = new_bindings[milestone_id]
        if old_reference is not None and new_reference != old_reference:
            raise ValueError(f"milestone approval was deleted or rewritten: {milestone_id}")
        if (
            old_reference is None
            and new_reference is not None
            and new_reference not in appended_records
        ):
            raise ValueError(f"milestone approval was not appended with binding: {milestone_id}")

    for rm_id in sorted(EXPECTED_RM_IDS):
        changed_contract_fields = {
            field
            for field in IMMUTABLE_ITEM_CONTRACT_FIELDS
            if previous_items[rm_id].get(field) != current_items[rm_id].get(field)
        }
        if changed_contract_fields:
            new_approval = _require_dict(current_items[rm_id]["human_approval"], "approval")
            new_reference = new_approval.get("approval_reference")
            scope_freeze = (
                previous_items[rm_id]["status"] == "proposed"
                and current_items[rm_id]["status"] == "planned"
                and new_approval.get("state") == "approved_scope"
                and isinstance(new_reference, str)
                and new_reference in appended_records
                and "scope" in _require_dict(new_records[new_reference], new_reference)
            )
            if not scope_freeze:
                field = sorted(changed_contract_fields)[0]
                raise ValueError(f"RM contract field requires a schema amendment: {rm_id}.{field}")
            if not isinstance(new_reference, str):
                raise ValueError(f"approved scope lacks reference: {rm_id}")
            _validate_scoped_approval_record(
                new_reference,
                _require_dict(new_records[new_reference], new_reference),
                rm_id=rm_id,
                item=current_items[rm_id],
            )
        old_history = _require_string_list(previous_history[rm_id], f"previous.{rm_id}")
        new_history = _require_string_list(current_history[rm_id], f"current.{rm_id}")
        if new_history[: len(old_history)] != old_history:
            raise ValueError(f"status history is not append-only: {rm_id}")
        added = len(new_history) - len(old_history)
        status_changed = previous_items[rm_id]["status"] != current_items[rm_id]["status"]
        if added > 1 or (status_changed and added != 1) or (not status_changed and added != 0):
            raise ValueError(f"status update must append exactly one transition: {rm_id}")

        old_reopen_events = previous_items[rm_id].get("reopen_events", [])
        new_reopen_events = current_items[rm_id].get("reopen_events", [])
        if not isinstance(old_reopen_events, list) or not isinstance(new_reopen_events, list):
            raise ValueError(f"reopen events must be lists: {rm_id}")
        if new_reopen_events[: len(old_reopen_events)] != old_reopen_events:
            raise ValueError(f"reopen events are not append-only: {rm_id}")
        reopened_now = previous_items[rm_id]["status"] == "completed" and current_items[rm_id][
            "status"
        ] in {"in_progress", "blocked"}
        appended_reopens = len(new_reopen_events) - len(old_reopen_events)
        if appended_reopens != int(reopened_now):
            raise ValueError(f"reopen event must be bound to its transition: {rm_id}")
        if reopened_now:
            old_evidence = _require_dict(
                previous_items[rm_id]["completion_evidence"], "completion_evidence"
            )
            appended_event = _require_dict(new_reopen_events[-1], "reopen_event")
            if appended_event["prior_evidence_sha256"] != _document_digest(old_evidence):
                raise ValueError(f"reopen event evidence digest mismatch: {rm_id}")

        old_approval = _require_dict(previous_items[rm_id]["human_approval"], "approval")
        new_approval = _require_dict(current_items[rm_id]["human_approval"], "approval")
        if old_approval != new_approval:
            new_reference = new_approval.get("approval_reference")
            if old_approval.get("state") == "approved_scope":
                raise ValueError(f"approval was deleted or rewritten: {rm_id}")
            if (
                new_approval.get("state") != "approved_scope"
                or not isinstance(new_reference, str)
                or new_reference not in appended_records
            ):
                raise ValueError(f"approval was not appended with exact scope: {rm_id}")

        if "completed" in old_history:
            old_evidence = _require_dict(
                previous_items[rm_id]["completion_evidence"], "completion_evidence"
            )
            new_evidence = _require_dict(
                current_items[rm_id]["completion_evidence"], "completion_evidence"
            )
            for field in ("commits", "paths", "tests"):
                old_values = set(_require_string_list(old_evidence[field], field))
                new_values = set(_require_string_list(new_evidence[field], field))
                if not old_values <= new_values:
                    raise ValueError(f"completed evidence was removed: {rm_id}.{field}")
        if (
            previous_items[rm_id]["status"] != "completed"
            and current_items[rm_id]["status"] == "completed"
            and "completed" in old_history
        ):
            old_evidence = _require_dict(
                previous_items[rm_id]["completion_evidence"], "completion_evidence"
            )
            new_evidence = _require_dict(
                current_items[rm_id]["completion_evidence"], "completion_evidence"
            )
            old_sets = {
                field: set(_require_string_list(old_evidence[field], field))
                for field in ("commits", "paths", "tests")
            }
            new_sets = {
                field: set(_require_string_list(new_evidence[field], field))
                for field in ("commits", "paths", "tests")
            }
            if any(not old_sets[field] <= new_sets[field] for field in old_sets):
                raise ValueError(f"re-completed item removed evidence: {rm_id}")
            evidence_grew = any(old_sets[field] < new_sets[field] for field in old_sets)
            if not evidence_grew:
                raise ValueError(f"re-completed item requires new evidence: {rm_id}")

    old_progress = _require_dict(previous["milestone_progress"], "previous.milestone_progress")
    new_progress = _require_dict(current["milestone_progress"], "current.milestone_progress")
    for milestone_id in sorted(EXPECTED_PHASE_2_MILESTONES):
        old_entry = _require_dict(old_progress[milestone_id], f"previous.{milestone_id}")
        new_entry = _require_dict(new_progress[milestone_id], f"current.{milestone_id}")
        old_history = _require_string_list(old_entry["history"], "history")
        new_history = _require_string_list(new_entry["history"], "history")
        if new_history[: len(old_history)] != old_history:
            raise ValueError(f"milestone history is not append-only: {milestone_id}")
        added = len(new_history) - len(old_history)
        state_changed = old_entry["state"] != new_entry["state"]
        if added > 1 or (state_changed and added != 1) or (not state_changed and added != 0):
            raise ValueError(f"milestone update must append exactly one transition: {milestone_id}")
        if old_entry["state"] == "completed":
            old_evidence = _require_dict(old_entry["completion_evidence"], "completion_evidence")
            new_evidence = _require_dict(new_entry["completion_evidence"], "completion_evidence")
            for field in ("commits", "paths", "tests"):
                if not set(_require_string_list(old_evidence[field], field)) <= set(
                    _require_string_list(new_evidence[field], field)
                ):
                    raise ValueError(f"milestone evidence was removed: {milestone_id}.{field}")


def validate_repository_evidence(
    document: dict[str, Any],
    repository_root: Path,
    tracked_paths: set[str] | None = None,
    known_commits: set[str] | None = None,
    commit_paths: dict[str, set[str]] | None = None,
    changed_paths: dict[str, set[str]] | None = None,
) -> None:
    """Validate completed evidence against a repository checkout, optionally including tracking."""

    validate_roadmap(document)
    root = repository_root.resolve()
    evidence_records: list[tuple[str, dict[str, Any]]] = []
    for item in roadmap_items(document):
        if item["status"] == "completed":
            evidence_records.append(
                (
                    str(item["id"]),
                    _require_dict(item["completion_evidence"], "completion_evidence"),
                )
            )
    progress = _require_dict(document["milestone_progress"], "milestone_progress")
    for milestone_id, raw_entry in progress.items():
        entry = _require_dict(raw_entry, f"milestone_progress.{milestone_id}")
        if entry["state"] == "completed":
            evidence_records.append(
                (
                    milestone_id,
                    _require_dict(entry["completion_evidence"], "completion_evidence"),
                )
            )

    for evidence_id, evidence in evidence_records:
        commits = _require_string_list(evidence["commits"], "commits")
        if known_commits is not None and any(commit not in known_commits for commit in commits):
            raise ValueError(f"completion evidence commit does not exist: {evidence_id}")
        references = _require_string_list(evidence["paths"], "paths") + _require_string_list(
            evidence["tests"], "tests"
        )
        for reference in references:
            relative = reference.split("::", 1)[0].replace("\\", "/")
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"evidence path escapes repository: {reference}") from exc
            if not candidate.exists():
                raise ValueError(f"evidence path does not exist: {reference}")
            if tracked_paths is not None:
                tracked = relative in tracked_paths
                if candidate.is_dir():
                    prefix = f"{relative.rstrip('/')}/"
                    tracked = tracked or any(path.startswith(prefix) for path in tracked_paths)
                if not tracked:
                    raise ValueError(f"evidence path is not tracked: {reference}")
            if "::" in reference:
                node = reference.split("::", 1)[1]
                if candidate.is_dir() or node not in candidate.read_text(encoding="utf-8"):
                    raise ValueError(f"evidence test node does not exist: {reference}")
            if commit_paths is not None and commits:
                present_in_tree = any(
                    relative in commit_paths.get(commit, set())
                    or any(
                        path.startswith(f"{relative.rstrip('/')}/")
                        for path in commit_paths.get(commit, set())
                    )
                    for commit in commits
                )
                if not present_in_tree:
                    raise ValueError(
                        f"completion evidence path is absent from cited commits: {evidence_id}"
                    )
        if changed_paths is not None and commits and references:
            normalized_references = [
                reference.split("::", 1)[0].replace("\\", "/").rstrip("/")
                for reference in references
            ]
            changed = set().union(*(changed_paths.get(commit, set()) for commit in commits))
            if not any(
                path == reference or path.startswith(f"{reference}/")
                for reference in normalized_references
                for path in changed
            ):
                raise ValueError(
                    f"completion evidence commit did not change cited scope: {evidence_id}"
                )


def roadmap_items(document: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    source = document if document is not None else load_roadmap()
    raw_items = source["items"]
    if not isinstance(raw_items, list):
        raise ValueError("items must be a list")
    return [_require_dict(item, "item") for item in raw_items]


def roadmap_summary(document: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a derived doctor-friendly status summary without release overclaiming."""

    source = document if document is not None else load_roadmap()
    items = roadmap_items(source)
    counts = Counter(str(item["status"]) for item in items)
    by_id = {str(item["id"]): item for item in items}
    milestones = {
        str(item["id"]): _require_dict(item, "milestone")
        for item in source["implementation_milestones"]
    }
    approval_records = {
        str(reference): _require_dict(record, f"approval_records.{reference}")
        for reference, record in _require_dict(
            source["approval_records"], "approval_records"
        ).items()
    }
    milestone_approvals = _milestone_approval_map(source, by_id, milestones, approval_records)
    progress = _require_dict(source["milestone_progress"], "milestone_progress")
    milestone_ready: list[str] = []
    for milestone_id, milestone in milestones.items():
        milestone_progress = _require_dict(progress[milestone_id], milestone_id)
        parent = by_id[str(milestone["rm_id"])]
        approval = _require_dict(parent["human_approval"], "human_approval")
        dependencies = _require_string_list(milestone["dependencies"], "dependencies")
        dependencies_complete = all(
            by_id[dependency]["status"] == "completed"
            if dependency in by_id
            else _require_dict(progress[dependency], dependency)["state"] == "completed"
            for dependency in dependencies
        )
        if (
            milestone_progress["state"] == "not_started"
            and parent["status"] in {"planned", "in_progress"}
            and (not approval["required"] or approval["state"] == "approved_scope")
            and (not approval["required"] or milestone_approvals[milestone_id] is not None)
            and dependencies_complete
        ):
            milestone_ready.append(milestone_id)

    implementation_ready = []
    for item in items:
        if item["status"] != "planned":
            continue
        entry_milestone = item.get("entry_milestone")
        if entry_milestone is not None:
            if entry_milestone in milestone_ready:
                implementation_ready.append(str(item["id"]))
            continue
        dependencies = _require_string_list(item["dependencies"], "dependencies")
        approval = _require_dict(item["human_approval"], "human_approval")
        if all(by_id[dependency]["status"] == "completed" for dependency in dependencies) and (
            not approval["required"] or approval["state"] == "approved_scope"
        ):
            implementation_ready.append(str(item["id"]))

    def release_milestone(rm_id: str) -> dict[str, str]:
        item = by_id.get(rm_id)
        if item is None:
            raise ValueError(f"required release milestone is missing: {rm_id}")
        return {
            "rm_id": rm_id,
            "implementation_status": str(item["status"]),
            "candidate_evidence": "not_evaluated",
        }

    return {
        "schema_version": source["schema_version"],
        "source": f"poker_deliberation/{ROADMAP_RESOURCE}",
        "source_sha256": _document_digest(source),
        "total_items": len(items),
        "status_counts": dict(sorted(counts.items())),
        "completed_ids": [str(item["id"]) for item in items if item["status"] == "completed"],
        "implementation_ready_ids": implementation_ready,
        "milestone_state_counts": dict(
            sorted(
                Counter(
                    str(_require_dict(entry, "milestone_progress")["state"])
                    for entry in progress.values()
                ).items()
            )
        ),
        "milestone_ready_ids": milestone_ready,
        "release_readiness": {
            "pre_release": release_milestone("RM-018A"),
            "stable_release": release_milestone("RM-018B"),
        },
        "note": (
            "Release readiness requires candidate-specific evidence and is not inferred "
            "from RM counts."
        ),
    }


def _markdown_cell(value: object) -> str:
    return (
        str(value).replace("`", "&#96;").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")
    )


def render_roadmap_markdown(document: dict[str, Any] | None = None) -> str:
    """Render the tracked human-readable projection checked by contract tests."""

    source = document if document is not None else load_roadmap()
    vocabulary = _require_dict(source["status_vocabulary"], "status_vocabulary")
    transitions = _require_dict(source["legal_transitions"], "legal_transitions")
    lines = [
        "# RM status",
        "",
        "この文書は`src/poker_deliberation/roadmap_status.json`から生成する追跡済みprojectionです。",
        "RM実装状態の正はJSONであり、`user_materials/ROADMAP.md`やPROGRESS履歴ではありません。",
        "",
        f"- schema version: `{source['schema_version']}`",
        f"- source SHA-256: `{_document_digest(source)}`",
        f"- history baseline: {_markdown_cell(source['source_policy']['history_baseline'])}",
        "- `ready`は保存statusではなく、依存関係と人間承認から計算する派生表示です。",
        (
            "- release readinessはRM件数から推定せず、candidate固有のbuild/hash/matrix証拠を"
            "別途要求します。"
        ),
        "",
        "## Status vocabulary",
        "",
        "| status | meaning | legal next status |",
        "|---|---|---|",
    ]
    for status, description in vocabulary.items():
        targets = ", ".join(
            f"`{item}`" for item in _require_string_list(transitions[status], status)
        )
        lines.append(f"| `{status}` | {_markdown_cell(description)} | {targets or 'terminal'} |")
    lines.extend(
        [
            "",
            (
                "Completedの再openは`in_progress`または`blocked`への遷移とし、理由と直前"
                "evidence digestをappend-only eventで同じ変更に記録します。再completedには"
                "全旧evidenceを保持したうえで"
                "新しいcommit/test/artifact evidenceを要求します。"
                "scope変更はschema amendmentを要します。`superseded`はgovernance amendmentなしには"
                "terminalです。"
            ),
            "",
            "## Phase 2 implementation milestones",
            "",
            (
                "RM-010〜013/024/027/028の実装順はitem-level依存ではなく、次の非循環"
                "milestone DAGを正とします。"
            ),
            "",
            "| milestone | RM | state | dependencies | scope |",
            "|---|---|---|---|---|",
        ]
    )
    for raw in source["implementation_milestones"]:
        milestone = _require_dict(raw, "milestone")
        dependencies = ", ".join(f"`{item}`" for item in milestone["dependencies"])
        milestone_progress = _require_dict(
            source["milestone_progress"][str(milestone["id"])], "milestone_progress"
        )
        lines.append(
            f"| `{milestone['id']}` | `{milestone['rm_id']}` | `{milestone_progress['state']}` | "
            f"{dependencies or 'none'} | {_markdown_cell(milestone['scope'])} |"
        )
    lines.extend(
        [
            "",
            "## Current RM state",
            "",
            (
                "| RM | title | phase | priority | status | dependencies | completion milestone | "
                "human approval |"
            ),
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for item in roadmap_items(source):
        dependencies = ", ".join(f"`{dependency}`" for dependency in item["dependencies"])
        approval = _require_dict(item["human_approval"], "human_approval")
        completion = item.get("completion_milestone", "n/a")
        lines.append(
            f"| `{item['id']}` | {_markdown_cell(item['title'])} | `{item['phase']}` | "
            f"`{item['priority']}` | `{item['status']}` | {dependencies or 'none'} | "
            f"`{completion}` | `{approval['state']}` |"
        )
    lines.extend(
        [
            "",
            "## Synchronization contract",
            "",
            (
                "- `poker-deliberate doctor --format json`の`roadmap`はpackage resourceとして"
                "設定したJSONから計算します。source/editable checkoutは検証済みですが、"
                "wheel/sdist同梱はRM-018Aで候補ごとに検証します。"
            ),
            (
                "- `scripts/generate_roadmap_status.py --check`とcontract testがこのprojectionの"
                "driftを検出します。"
            ),
            (
                "- PLANは現在の実行scopeを示し、PROGRESSは履歴だけを記録し、RM statusを"
                "再定義しません。"
            ),
            (
                "- ignoredの`user_materials/ROADMAP.md`は承認方針・背景説明でありruntime入力"
                "ではありません。"
            ),
            "",
        ]
    )
    return "\n".join(lines)
