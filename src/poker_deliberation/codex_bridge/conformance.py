"""Mechanical reuse of the P2-025A role inventory and semantic mappings."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal, TypedDict, cast

from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.codex_bridge.canonical import domain_sha256, sha256_bytes
from poker_deliberation.codex_bridge.contracts import role_definition_instructions
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BridgeRole,
    BridgeRoleConformanceBindingV1,
    BridgeSemanticRole,
    BridgeSkillId,
    repository_skill_for_role,
)
from poker_deliberation.runtime_conformance import (
    build_runtime_inventories,
    runtime_inventory_sha256,
)
from poker_deliberation.runtime_conformance.canonical import canonical_json_bytes
from poker_deliberation.runtime_conformance.models import RoleRelationship, SemanticRole
from poker_deliberation.storage.revision_lock import verify_regular_single_link
from poker_deliberation.tools.registry import default_registry


class BridgeConformanceError(ValueError):
    """Raised when the tracked P2-025A role authority cannot bind the bridge."""


class _SkillBinding(TypedDict, total=False):
    repository_skill_id: BridgeSkillId
    repository_skill_source_path: str
    repository_skill_content_sha256: str
    repository_skill_version_kind: Literal["repository_commit"]
    repository_skill_version: str
    repository_skill_instructions: str


def _validated_skill_instructions(text: str, skill_id: BridgeSkillId) -> str:
    lines = text.splitlines()
    if not lines or lines[0] != "---":
        raise BridgeConformanceError("tracked Skill frontmatter is invalid")
    try:
        frontmatter_end = lines.index("---", 1)
    except ValueError as exc:
        raise BridgeConformanceError("tracked Skill frontmatter is invalid") from exc
    name_entries = []
    for line in lines[1:frontmatter_end]:
        key, separator, value = line.partition(":")
        if separator and key.strip() == "name":
            name_entries.append(value.strip())
    if name_entries != [skill_id]:
        raise BridgeConformanceError("tracked Skill name differs from its binding")
    marker = "## Bounded bridge mode"
    marker_indexes = [
        index
        for index, line in enumerate(lines[frontmatter_end + 1 :], frontmatter_end + 1)
        if line == marker
    ]
    if len(marker_indexes) != 1:
        raise BridgeConformanceError("tracked Skill lacks unique bounded bridge mode")
    section_start = marker_indexes[0] + 1
    section_end = next(
        (
            index
            for index, line in enumerate(lines[section_start:], section_start)
            if line.startswith("## ")
        ),
        len(lines),
    )
    if not any(
        line.strip() and not line.lstrip().startswith("#")
        for line in lines[section_start:section_end]
    ):
        raise BridgeConformanceError("tracked Skill bounded bridge mode is empty")
    return " ".join(text.split())


def _normalized_role_instructions(repository_root: Path, role: BridgeRole) -> str:
    source = repository_root / ".codex" / "agents" / f"{role.value}.toml"
    try:
        verify_regular_single_link(source)
        raw = tomllib.loads(source.read_text(encoding="utf-8"))
        value = raw["developer_instructions"]
    except Exception as exc:
        raise BridgeConformanceError("tracked role instructions are unavailable") from exc
    if not isinstance(value, str):
        raise BridgeConformanceError("tracked role instructions are invalid")
    normalized = " ".join(value.split())
    if normalized != role_definition_instructions(role):
        raise BridgeConformanceError("bridge role prompt differs from tracked role instructions")
    return normalized


def _skill_binding(
    repository_root: Path,
    role: BridgeRole,
    *,
    repository_commit_id: str,
) -> _SkillBinding:
    skill_id = repository_skill_for_role(role)
    if skill_id is None:
        return {}
    relative = f".agents/skills/{skill_id}/SKILL.md"
    source = repository_root.joinpath(*relative.split("/"))
    try:
        status = verify_regular_single_link(source)
        resolved = source.resolve(strict=True)
        root = repository_root.resolve(strict=True)
        if not resolved.is_relative_to(root) or status.st_size > 262_144:
            raise ValueError("Skill source is outside its bound")
        data = source.read_bytes()
        text = data.decode("utf-8", errors="strict")
    except Exception as exc:
        raise BridgeConformanceError("tracked Skill source is unavailable") from exc
    instructions = _validated_skill_instructions(text, skill_id)
    return {
        "repository_skill_id": skill_id,
        "repository_skill_source_path": relative,
        "repository_skill_content_sha256": sha256_bytes(data),
        "repository_skill_version_kind": "repository_commit",
        "repository_skill_version": repository_commit_id,
        "repository_skill_instructions": instructions,
    }


def build_bridge_role_conformance(
    repository_root: Path,
    *,
    repository_commit_id: str,
    include_repository_skill_bindings: bool = False,
) -> tuple[BridgeRoleConformanceBindingV1, ...]:
    registry = default_registry()
    tool_names = tuple(sorted(registry.names(), key=lambda item: item.encode("utf-8")))
    capability_ids = tuple(
        sorted(
            (item.capability_id for item in CAPABILITIES),
            key=lambda item: item.encode("utf-8"),
        )
    )
    source_revision = domain_sha256(
        "poker-bounded-codex-bridge-source-revision-v1",
        repository_commit_id,
    )
    codex, python = build_runtime_inventories(
        repository_root.resolve(),
        source_revision=source_revision,
        python_tool_catalog=tool_names,
        python_capability_catalog=capability_ids,
    )
    mapping_sha = domain_sha256(
        "poker-bounded-codex-bridge-semantic-mapping-v1",
        canonical_json_bytes(codex.role_mappings),
    )
    codex_inventory_sha = runtime_inventory_sha256(codex)
    python_inventory_sha = runtime_inventory_sha256(python)
    bindings: list[BridgeRoleConformanceBindingV1] = []
    for role in BRIDGE_ROLE_ORDER:
        _normalized_role_instructions(repository_root, role)
        inventory_role = next(
            (item for item in codex.roles if item.runtime_role_id == role.value),
            None,
        )
        mapping = next(
            (item for item in codex.role_mappings if item.codex_role_id == role.value),
            None,
        )
        if (
            inventory_role is None
            or not inventory_role.read_only
            or inventory_role.declared_tools is not None
            or mapping is None
            or mapping.relationship is not RoleRelationship.SEMANTIC_PEER
            or mapping.semantic_role is not inventory_role.semantic_role
            or inventory_role.semantic_role
            not in {
                SemanticRole.STRATEGY_ANALYSIS,
                SemanticRole.MATH_AUDIT,
                SemanticRole.SKEPTICISM,
                SemanticRole.ADJUDICATION,
                SemanticRole.REPORT_WRITING,
            }
        ):
            raise BridgeConformanceError("P2-025A role inventory cannot authorize bridge role")
        bindings.append(
            BridgeRoleConformanceBindingV1(
                role=role,
                semantic_role=cast(
                    BridgeSemanticRole,
                    inventory_role.semantic_role.value,
                ),
                runtime_role_definition_sha256=(inventory_role.source_definition_sha256),
                codex_runtime_inventory_sha256=codex_inventory_sha,
                python_runtime_inventory_sha256=python_inventory_sha,
                semantic_mapping_sha256=mapping_sha,
                source_path=inventory_role.source_path,
                role_read_only=True,
                declared_tool_allowlist=(),
                **(
                    _skill_binding(
                        repository_root,
                        role,
                        repository_commit_id=repository_commit_id,
                    )
                    if include_repository_skill_bindings
                    else {}
                ),
            )
        )
    return tuple(bindings)


__all__ = ["BridgeConformanceError", "build_bridge_role_conformance"]
