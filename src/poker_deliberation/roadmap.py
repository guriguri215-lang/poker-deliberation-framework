"""Public roadmap projection loading, validation, summaries, and documentation rendering."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any

ROADMAP_RESOURCE = "roadmap_status.json"
ROADMAP_SCHEMA_VERSION = "12.0.0"
__all__ = [
    "ROADMAP_RESOURCE",
    "ROADMAP_SCHEMA_VERSION",
    "load_roadmap",
    "render_roadmap_markdown",
    "roadmap_items",
    "roadmap_summary",
    "validate_repository_evidence",
    "validate_roadmap",
    "validate_roadmap_update",
    "validate_transition",
]
RM_ID_PATTERN = re.compile(r"^RM-[0-9]{3}[AB]?$")
MILESTONE_ID_PATTERN = re.compile(r"^P[2-5]-[0-9]{3}[A-Z]$")
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
ITEM_STATUS_VALUES = {
    "proposed",
    "planned",
    "in_progress",
    "blocked",
    "completed",
    "superseded",
}
MILESTONE_STATUS_VALUES = {
    "not_started",
    "in_progress",
    "blocked",
    "completed",
}
GENERATED_ROADMAP_DOCUMENT = "docs/roadmap-status.md"
EXPECTED_RM_IDS = (
    {f"RM-{number:03d}" for number in range(1, 18)}
    | {"RM-018A", "RM-018B"}
    | {f"RM-{number:03d}" for number in range(19, 31)}
)
EXPECTED_IMPLEMENTATION_MILESTONES = {
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
    "P2-029A",
    "P2-025A",
    "P3-014A",
    "P3-015A",
    "P3-016A",
    "P3-016B",
    "P3-017A",
    "P3-030A",
    "P3-030B",
    "P3-030C",
}
# Compatibility alias for callers that imported the schema 4.x name.
EXPECTED_PHASE_2_MILESTONES = EXPECTED_IMPLEMENTATION_MILESTONES
TOP_LEVEL_FIELDS = {
    "schema_version",
    "source_policy",
    "status_vocabulary",
    "legal_transitions",
    "milestone_status_vocabulary",
    "milestone_legal_transitions",
    "implementation_milestones",
    "items",
}
ITEM_FIELDS = {
    "id",
    "title",
    "phase",
    "priority",
    "status",
    "status_reason",
    "dependencies",
    "capabilities",
    "objective",
    "targets",
    "acceptance_criteria",
    "tests",
    "milestones",
    "relations",
    "decision_gate",
}
IMMUTABLE_ITEM_FIELDS = ITEM_FIELDS - {"status", "status_reason"}
_RM030_STALE_P2_028A_RELATION = (
    "P2-028A remains not started and is not activated by this local-only path."
)
_RM030_HISTORICAL_P2_028A_RELATION = (
    "At P3-030A completion, P2-028A had not started and was not activated by that local-only path."
)
MILESTONE_FIELDS = {
    "id",
    "rm_id",
    "status",
    "status_reason",
    "dependencies",
    "scope",
}
IMMUTABLE_MILESTONE_FIELDS = MILESTONE_FIELDS - {"status", "status_reason"}


def _require_dict(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def _require_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _require_string_list(
    value: object,
    name: str,
    *,
    allow_empty: bool = True,
) -> list[str]:
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item.strip() for item in value
    ):
        raise ValueError(f"{name} must be a list of non-empty strings")
    values = list(value)
    if not allow_empty and not values:
        raise ValueError(f"{name} must not be empty")
    if len(values) != len(set(values)):
        raise ValueError(f"{name} contains duplicates")
    return values


def _document_digest(document: dict[str, Any]) -> str:
    canonical = json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_roadmap() -> dict[str, Any]:
    """Load and validate the packaged public roadmap resource from any CWD."""

    raw = (
        resources.files("poker_deliberation").joinpath(ROADMAP_RESOURCE).read_text(encoding="utf-8")
    )
    document = _require_dict(json.loads(raw), "roadmap")
    validate_roadmap(document)
    return document


def validate_transition(source: str, target: str, transitions: dict[str, Any]) -> None:
    """Validate one public status transition."""

    if source not in transitions:
        raise ValueError(f"unknown transition source: {source}")
    allowed = _require_string_list(transitions[source], f"legal_transitions.{source}")
    if target not in allowed:
        raise ValueError(f"illegal status transition: {source} -> {target}")


def _validate_transition_table(
    vocabulary: dict[str, Any],
    transitions: dict[str, Any],
    name: str,
) -> None:
    if set(vocabulary) != set(transitions):
        raise ValueError(f"{name} transition sources must match its vocabulary")
    for status, description in vocabulary.items():
        _require_string(description, f"{name}.{status}")
        targets = _require_string_list(transitions[status], f"{name}_transitions.{status}")
        unknown = set(targets) - set(vocabulary)
        if unknown:
            raise ValueError(f"{name} transition has unknown targets: {sorted(unknown)}")


def _validate_item_dependencies(items: dict[str, dict[str, Any]]) -> None:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visiting:
            raise ValueError(f"roadmap dependency cycle includes {item_id}")
        if item_id in visited:
            return
        visiting.add(item_id)
        for dependency in _require_string_list(items[item_id]["dependencies"], "dependencies"):
            if dependency not in items:
                raise ValueError(f"unknown dependency {dependency} for {item_id}")
            if dependency == item_id:
                raise ValueError(f"self dependency is not allowed: {item_id}")
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in items:
        visit(item_id)


def _validate_milestones(
    document: dict[str, Any],
    items: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    raw_milestones = document.get("implementation_milestones")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        raise ValueError("implementation_milestones must be a non-empty list")

    milestone_vocabulary = _require_dict(
        document["milestone_status_vocabulary"],
        "milestone_status_vocabulary",
    )
    milestones: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_milestones):
        milestone = _require_dict(raw, f"implementation_milestones[{index}]")
        if set(milestone) != MILESTONE_FIELDS:
            raise ValueError(f"invalid milestone fields at index {index}")
        milestone_id = milestone.get("id")
        if not isinstance(milestone_id, str) or not MILESTONE_ID_PATTERN.fullmatch(milestone_id):
            raise ValueError(f"invalid implementation milestone id: {milestone_id}")
        if milestone_id in milestones:
            raise ValueError(f"duplicate implementation milestone id: {milestone_id}")
        rm_id = milestone.get("rm_id")
        if not isinstance(rm_id, str) or rm_id not in items:
            raise ValueError(f"unknown RM for implementation milestone: {milestone_id}")
        if milestone.get("status") not in milestone_vocabulary:
            raise ValueError(f"invalid milestone status: {milestone_id}")
        _require_string(milestone.get("status_reason"), f"{milestone_id}.status_reason")
        _require_string(milestone.get("scope"), f"{milestone_id}.scope")
        _require_string_list(milestone.get("dependencies"), f"{milestone_id}.dependencies")
        milestones[milestone_id] = milestone

    if set(milestones) != EXPECTED_IMPLEMENTATION_MILESTONES:
        missing = sorted(EXPECTED_IMPLEMENTATION_MILESTONES - set(milestones))
        extra = sorted(set(milestones) - EXPECTED_IMPLEMENTATION_MILESTONES)
        raise ValueError(f"implementation milestone set mismatch; missing={missing}, extra={extra}")

    nodes = set(items) | set(milestones)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in items or node_id in visited:
            return
        if node_id in visiting:
            raise ValueError(f"implementation dependency cycle includes {node_id}")
        visiting.add(node_id)
        for dependency in _require_string_list(
            milestones[node_id]["dependencies"],
            f"{node_id}.dependencies",
        ):
            if dependency not in nodes:
                raise ValueError(f"unknown milestone dependency {dependency} for {node_id}")
            visit(dependency)
        visiting.remove(node_id)
        visited.add(node_id)

    for milestone_id in milestones:
        visit(milestone_id)

    for item_id, item in items.items():
        references = _require_dict(item["milestones"], f"{item_id}.milestones")
        if set(references) != {"entry", "completion"}:
            raise ValueError(f"invalid milestone reference fields: {item_id}")
        for field, reference in references.items():
            if reference is None:
                continue
            if not isinstance(reference, str) or reference not in milestones:
                raise ValueError(f"unknown {field} milestone for {item_id}")
            if milestones[reference]["rm_id"] != item_id:
                raise ValueError(f"milestone owner mismatch for {item_id}.{field}")

    def dependency_complete(dependency: str) -> bool:
        if dependency in items:
            return str(items[dependency]["status"]) == "completed"
        return str(milestones[dependency]["status"]) == "completed"

    for milestone_id, milestone in milestones.items():
        status = milestone["status"]
        parent = items[str(milestone["rm_id"])]
        if status in {"in_progress", "completed"} and not all(
            dependency_complete(dependency)
            for dependency in _require_string_list(
                milestone["dependencies"],
                f"{milestone_id}.dependencies",
            )
        ):
            raise ValueError(f"active milestone has incomplete dependency: {milestone_id}")
        if status == "in_progress" and parent["status"] != "in_progress":
            raise ValueError(f"in-progress milestone requires in-progress parent: {milestone_id}")
        if status == "completed" and parent["status"] not in {
            "in_progress",
            "blocked",
            "completed",
        }:
            raise ValueError(f"completed milestone has inactive parent: {milestone_id}")
        if status == "blocked" and parent["status"] != "blocked":
            raise ValueError(f"blocked milestone requires blocked parent: {milestone_id}")

    return milestones


def validate_roadmap(document: dict[str, Any]) -> None:
    """Validate the packaged public projection without accepting management-only fields."""

    if set(document) != TOP_LEVEL_FIELDS:
        missing = sorted(TOP_LEVEL_FIELDS - set(document))
        extra = sorted(set(document) - TOP_LEVEL_FIELDS)
        raise ValueError(f"roadmap fields mismatch; missing={missing}, extra={extra}")
    if document.get("schema_version") != ROADMAP_SCHEMA_VERSION:
        raise ValueError(f"unsupported roadmap schema: {document.get('schema_version')}")

    policy = _require_dict(document["source_policy"], "source_policy")
    expected_policy_fields = {"canonical", "projection", "description", "generated_document"}
    if set(policy) != expected_policy_fields:
        raise ValueError("invalid source_policy fields")
    if policy.get("canonical") is not True or policy.get("projection") != "public":
        raise ValueError("source_policy must identify the canonical public projection")
    _require_string(policy.get("description"), "source_policy.description")
    generated_document = _require_string(
        policy.get("generated_document"),
        "source_policy.generated_document",
    )
    generated_path = PurePosixPath(generated_document)
    if generated_path.is_absolute() or ".." in generated_path.parts:
        raise ValueError("generated document must be repository-relative")
    if generated_document != GENERATED_ROADMAP_DOCUMENT:
        raise ValueError(f"generated document must be {GENERATED_ROADMAP_DOCUMENT}")

    vocabulary = _require_dict(document["status_vocabulary"], "status_vocabulary")
    transitions = _require_dict(document["legal_transitions"], "legal_transitions")
    if set(vocabulary) != ITEM_STATUS_VALUES:
        raise ValueError("status vocabulary mismatch")
    _validate_transition_table(vocabulary, transitions, "status")
    milestone_vocabulary = _require_dict(
        document["milestone_status_vocabulary"],
        "milestone_status_vocabulary",
    )
    milestone_transitions = _require_dict(
        document["milestone_legal_transitions"],
        "milestone_legal_transitions",
    )
    if set(milestone_vocabulary) != MILESTONE_STATUS_VALUES:
        raise ValueError("milestone status vocabulary mismatch")
    _validate_transition_table(
        milestone_vocabulary,
        milestone_transitions,
        "milestone_status",
    )

    raw_items = document.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ValueError("items must be a non-empty list")
    items: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_items):
        item = _require_dict(raw, f"items[{index}]")
        if set(item) != ITEM_FIELDS:
            raise ValueError(f"invalid roadmap item fields at index {index}")
        item_id = item.get("id")
        if not isinstance(item_id, str) or not RM_ID_PATTERN.fullmatch(item_id):
            raise ValueError(f"invalid roadmap id: {item_id}")
        if item_id in items:
            raise ValueError(f"duplicate roadmap id: {item_id}")
        _require_string(item.get("title"), f"{item_id}.title")
        _require_string(item.get("objective"), f"{item_id}.objective")
        _require_string(item.get("status_reason"), f"{item_id}.status_reason")
        if item.get("phase") not in ALLOWED_PHASES:
            raise ValueError(f"invalid phase for {item_id}")
        if item.get("priority") not in ALLOWED_PRIORITIES:
            raise ValueError(f"invalid priority for {item_id}")
        if item.get("status") not in vocabulary:
            raise ValueError(f"invalid status for {item_id}")
        _require_string_list(item.get("dependencies"), f"{item_id}.dependencies")
        _require_string_list(item.get("capabilities"), f"{item_id}.capabilities")
        _require_string_list(item.get("targets"), f"{item_id}.targets", allow_empty=False)
        _require_string_list(
            item.get("acceptance_criteria"),
            f"{item_id}.acceptance_criteria",
            allow_empty=False,
        )
        _require_string_list(item.get("tests"), f"{item_id}.tests", allow_empty=False)
        _require_string_list(item.get("relations"), f"{item_id}.relations")
        gate = _require_dict(item.get("decision_gate"), f"{item_id}.decision_gate")
        if set(gate) != {"required", "rationale"} or not isinstance(gate.get("required"), bool):
            raise ValueError(f"invalid decision gate for {item_id}")
        rationale = _require_string_list(
            gate.get("rationale"),
            f"{item_id}.decision_gate.rationale",
        )
        if gate["required"] and not rationale:
            raise ValueError(f"required decision gate lacks rationale: {item_id}")
        if not gate["required"] and rationale:
            raise ValueError(f"unneeded decision gate has rationale: {item_id}")
        items[item_id] = item

    if set(items) != EXPECTED_RM_IDS:
        missing = sorted(EXPECTED_RM_IDS - set(items))
        extra = sorted(set(items) - EXPECTED_RM_IDS)
        raise ValueError(f"RM set mismatch; missing={missing}, extra={extra}")
    _validate_item_dependencies(items)
    milestones = _validate_milestones(document, items)

    for item_id, item in items.items():
        if item["status"] in {"in_progress", "completed"} and any(
            items[dependency]["status"] != "completed"
            for dependency in _require_string_list(item["dependencies"], "dependencies")
        ):
            raise ValueError(f"active item has incomplete dependency: {item_id}")
        references = _require_dict(item["milestones"], f"{item_id}.milestones")
        entry = references["entry"]
        completion = references["completion"]
        if (
            item["status"] == "in_progress"
            and entry is not None
            and milestones[str(entry)]["status"] not in {"in_progress", "completed"}
        ):
            raise ValueError(f"in-progress item has inactive entry milestone: {item_id}")
        if (
            item["status"] == "completed"
            and completion is not None
            and milestones[str(completion)]["status"] != "completed"
        ):
            raise ValueError(f"completed item has incomplete milestone: {item_id}")


def validate_roadmap_update(
    previous: dict[str, Any],
    current: dict[str, Any],
    newly_approved_references: set[str] | None = None,
) -> None:
    """Validate public contract stability and legal current-status changes.

    The optional third argument is retained as a no-op compatibility shim for
    callers of schema 1.x. Public projection validation discards it.
    """

    del newly_approved_references

    validate_roadmap(previous)
    validate_roadmap(current)
    if previous["schema_version"] != current["schema_version"]:
        raise ValueError("schema change requires an explicit compatibility release")
    for field in TOP_LEVEL_FIELDS - {"items", "implementation_milestones"}:
        if previous[field] != current[field]:
            raise ValueError(f"public top-level contract changed: {field}")

    old_items = {str(item["id"]): item for item in roadmap_items(previous)}
    new_items = {str(item["id"]): item for item in roadmap_items(current)}
    if set(old_items) != set(new_items):
        raise ValueError("roadmap item IDs cannot change within a schema version")
    transitions = _require_dict(previous["legal_transitions"], "legal_transitions")
    for item_id, old_item in old_items.items():
        new_item = new_items[item_id]
        for field in IMMUTABLE_ITEM_FIELDS:
            if old_item[field] != new_item[field]:
                if (
                    item_id == "RM-030"
                    and field == "relations"
                    and old_item[field].count(_RM030_STALE_P2_028A_RELATION) == 1
                ):
                    corrected = list(old_item[field])
                    corrected[corrected.index(_RM030_STALE_P2_028A_RELATION)] = (
                        _RM030_HISTORICAL_P2_028A_RELATION
                    )
                    if new_item[field] == corrected:
                        continue
                raise ValueError(f"public item contract changed: {item_id}.{field}")
        if old_item["status"] != new_item["status"]:
            validate_transition(str(old_item["status"]), str(new_item["status"]), transitions)
            if str(old_item["status_reason"]).strip() == str(new_item["status_reason"]).strip():
                raise ValueError(f"status transition requires a new reason: {item_id}")

    old_milestones = {
        str(item["id"]): _require_dict(item, "milestone")
        for item in previous["implementation_milestones"]
    }
    new_milestones = {
        str(item["id"]): _require_dict(item, "milestone")
        for item in current["implementation_milestones"]
    }
    if set(old_milestones) != set(new_milestones):
        raise ValueError("milestone IDs cannot change within a schema version")
    milestone_transitions = _require_dict(
        previous["milestone_legal_transitions"],
        "milestone_legal_transitions",
    )
    for milestone_id, old_milestone in old_milestones.items():
        new_milestone = new_milestones[milestone_id]
        for field in IMMUTABLE_MILESTONE_FIELDS:
            if old_milestone[field] != new_milestone[field]:
                raise ValueError(f"public milestone contract changed: {milestone_id}.{field}")
        if old_milestone["status"] != new_milestone["status"]:
            validate_transition(
                str(old_milestone["status"]),
                str(new_milestone["status"]),
                milestone_transitions,
            )
            if (
                str(old_milestone["status_reason"]).strip()
                == str(new_milestone["status_reason"]).strip()
            ):
                raise ValueError(f"milestone transition requires a new reason: {milestone_id}")


def _repository_reference(reference: str) -> str | None:
    raw = reference.split("::", 1)[0].replace("\\", "/").rstrip("/")
    path_prefixes = ("docs/", "evals/", "scripts/", "src/", "tests/", "tools/")
    if raw == "pyproject.toml" or raw.startswith(path_prefixes):
        return raw
    return None


def validate_repository_evidence(
    document: dict[str, Any],
    repository_root: Path,
    tracked_paths: set[str] | None = None,
    known_commits: set[str] | None = None,
    commit_paths: dict[str, set[str]] | None = None,
    changed_paths: dict[str, set[str]] | None = None,
) -> None:
    """Validate current-tree paths supporting completed public roadmap claims."""

    del known_commits, commit_paths, changed_paths
    validate_roadmap(document)
    root = repository_root.resolve()
    for item in roadmap_items(document):
        if item["status"] != "completed":
            continue
        references = _require_string_list(item["targets"], "targets") + _require_string_list(
            item["tests"], "tests"
        )
        for reference in references:
            relative = _repository_reference(reference)
            if relative is None:
                continue
            candidate = (root / relative).resolve()
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"public roadmap path escapes repository: {reference}") from exc
            if not candidate.exists():
                raise ValueError(f"public roadmap path does not exist: {reference}")
            if tracked_paths is not None:
                tracked = relative in tracked_paths
                if candidate.is_dir():
                    prefix = f"{relative}/"
                    tracked = tracked or any(path.startswith(prefix) for path in tracked_paths)
                if not tracked:
                    raise ValueError(f"public roadmap path is not tracked: {reference}")
            if "::" in reference:
                node = reference.split("::", 1)[1]
                if candidate.is_dir() or node not in candidate.read_text(encoding="utf-8"):
                    raise ValueError(f"public roadmap test node does not exist: {reference}")


def roadmap_items(document: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    source = document if document is not None else load_roadmap()
    raw_items = source["items"]
    if not isinstance(raw_items, list):
        raise ValueError("items must be a list")
    return [_require_dict(item, "item") for item in raw_items]


def roadmap_summary(document: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a doctor-friendly public status summary without release overclaiming."""

    source = document if document is not None else load_roadmap()
    validate_roadmap(source)
    items = roadmap_items(source)
    by_id = {str(item["id"]): item for item in items}
    milestones = {
        str(item["id"]): _require_dict(item, "milestone")
        for item in source["implementation_milestones"]
    }

    def dependency_complete(dependency: str) -> bool:
        if dependency in by_id:
            return str(by_id[dependency]["status"]) == "completed"
        return str(milestones[dependency]["status"]) == "completed"

    milestone_ready = [
        milestone_id
        for milestone_id, milestone in milestones.items()
        if milestone["status"] == "not_started"
        and by_id[str(milestone["rm_id"])]["status"] in {"planned", "in_progress"}
        and all(
            dependency_complete(dependency)
            for dependency in _require_string_list(milestone["dependencies"], "dependencies")
        )
    ]
    implementation_ready: list[str] = []
    for item in items:
        if item["status"] != "planned":
            continue
        references = _require_dict(item["milestones"], "milestones")
        entry = references["entry"]
        if entry is not None:
            if entry in milestone_ready or milestones[str(entry)]["status"] == "completed":
                implementation_ready.append(str(item["id"]))
            continue
        if all(
            by_id[dependency]["status"] == "completed"
            for dependency in _require_string_list(item["dependencies"], "dependencies")
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
        "status_counts": dict(sorted(Counter(str(item["status"]) for item in items).items())),
        "completed_ids": [str(item["id"]) for item in items if item["status"] == "completed"],
        "implementation_ready_ids": implementation_ready,
        "milestone_state_counts": dict(
            sorted(Counter(str(item["status"]) for item in milestones.values()).items())
        ),
        "milestone_ready_ids": milestone_ready,
        "release_readiness": {
            "pre_release": release_milestone("RM-018A"),
            "stable_release": release_milestone("RM-018B"),
        },
        "note": (
            "Ready IDs are dependency-only projections. Decision gates and candidate-specific "
            "release evidence remain separate."
        ),
    }


def _markdown_cell(value: object) -> str:
    return (
        str(value).replace("`", "&#96;").replace("|", "\\|").replace("\r", "").replace("\n", "<br>")
    )


def _append_detail_list(lines: list[str], label: str, values: list[str]) -> None:
    lines.append(f"- {label}:")
    if not values:
        lines.append("  - none")
        return
    lines.extend(f"  - {_markdown_cell(value)}" for value in values)


def render_roadmap_markdown(document: dict[str, Any] | None = None) -> str:
    """Render the tracked human-readable public roadmap projection."""

    source = document if document is not None else load_roadmap()
    validate_roadmap(source)
    vocabulary = _require_dict(source["status_vocabulary"], "status_vocabulary")
    transitions = _require_dict(source["legal_transitions"], "legal_transitions")
    milestone_vocabulary = _require_dict(
        source["milestone_status_vocabulary"],
        "milestone_status_vocabulary",
    )
    milestone_transitions = _require_dict(
        source["milestone_legal_transitions"],
        "milestone_legal_transitions",
    )
    lines = [
        "# Public roadmap status",
        "",
        "この文書は`src/poker_deliberation/roadmap_status.json`から生成する公開projectionです。",
        (
            "公開中の実装状態、依存関係、能力scope、受入条件、milestone、"
            "decision rationaleを示します。"
        ),
        "",
        f"- schema version: `{source['schema_version']}`",
        f"- source SHA-256: `{_document_digest(source)}`",
        "- `ready`は依存関係だけから計算し、decision gateの完了を意味しません。",
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
            "## Milestone status vocabulary",
            "",
            "| status | meaning | legal next status |",
            "|---|---|---|",
        ]
    )
    for status, description in milestone_vocabulary.items():
        targets = ", ".join(
            f"`{item}`" for item in _require_string_list(milestone_transitions[status], status)
        )
        lines.append(f"| `{status}` | {_markdown_cell(description)} | {targets or 'terminal'} |")

    lines.extend(
        [
            "",
            "## Implementation milestones",
            "",
            "| milestone | RM | status | dependencies | scope | status reason |",
            "|---|---|---|---|---|---|",
        ]
    )
    for raw in source["implementation_milestones"]:
        milestone = _require_dict(raw, "milestone")
        dependencies = ", ".join(f"`{item}`" for item in milestone["dependencies"])
        lines.append(
            f"| `{milestone['id']}` | `{milestone['rm_id']}` | `{milestone['status']}` | "
            f"{dependencies or 'none'} | {_markdown_cell(milestone['scope'])} | "
            f"{_markdown_cell(milestone['status_reason'])} |"
        )

    lines.extend(
        [
            "",
            "## Current RM state",
            "",
            (
                "| RM | title | phase | priority | status | dependencies | completion milestone | "
                "decision gate |"
            ),
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for item in roadmap_items(source):
        dependencies = ", ".join(f"`{dependency}`" for dependency in item["dependencies"])
        milestones = _require_dict(item["milestones"], "milestones")
        completion = milestones["completion"] or "n/a"
        gate = _require_dict(item["decision_gate"], "decision_gate")
        gate_label = "required" if gate["required"] else "none"
        lines.append(
            f"| `{item['id']}` | {_markdown_cell(item['title'])} | `{item['phase']}` | "
            f"`{item['priority']}` | `{item['status']}` | {dependencies or 'none'} | "
            f"`{completion}` | `{gate_label}` |"
        )

    lines.extend(["", "## Public item contracts", ""])
    for item in roadmap_items(source):
        lines.extend(
            [
                f"### {item['id']} — {_markdown_cell(item['title'])}",
                "",
                f"- Status: `{item['status']}`",
                f"- Status reason: {_markdown_cell(item['status_reason'])}",
                f"- Objective: {_markdown_cell(item['objective'])}",
            ]
        )
        _append_detail_list(lines, "Capabilities", list(item["capabilities"]))
        _append_detail_list(lines, "Targets", list(item["targets"]))
        _append_detail_list(lines, "Acceptance criteria", list(item["acceptance_criteria"]))
        _append_detail_list(lines, "Tests", list(item["tests"]))
        gate = _require_dict(item["decision_gate"], "decision_gate")
        if gate["required"]:
            _append_detail_list(lines, "Decision gate rationale", list(gate["rationale"]))
        if item["relations"]:
            _append_detail_list(
                lines,
                (
                    "Completion-time relations (historical; not current status assertions)"
                    if item["id"] == "RM-030"
                    else "Relations"
                ),
                list(item["relations"]),
            )
        lines.append("")

    lines.extend(
        [
            "## Synchronization contract",
            "",
            (
                "- `poker-deliberate doctor --format json`の`roadmap`はpackage resourceの公開JSON"
                "から計算します。"
            ),
            (
                "- 公開projection自体はcandidate固有のcommitやtest実行を証明しません。status更新は"
                "同一schema更新検証、参照path/testのtracked検証、repository gateを別途要求します。"
            ),
            (
                "- `scripts/generate_roadmap_status.py --check`とcontract testがこのprojectionの"
                "driftを検出します。"
            ),
            (
                "- wheel/sdistのpackage-dataはartifact smokeで候補ごとに別途検証します。"
                "この検証だけではrelease candidate判定とせず、RM-018Aのmatrix・license・"
                "artifact条件を別途要求します。"
            ),
            "",
        ]
    )
    return "\n".join(lines)
