"""Mechanical reuse of the P2-025A role inventory and semantic mappings."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.codex_bridge.canonical import domain_sha256
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BridgeRoleConformanceBindingV1,
    BridgeSemanticRole,
)
from poker_deliberation.runtime_conformance import (
    build_runtime_inventories,
    runtime_inventory_sha256,
)
from poker_deliberation.runtime_conformance.canonical import canonical_json_bytes
from poker_deliberation.runtime_conformance.models import RoleRelationship, SemanticRole
from poker_deliberation.tools.registry import default_registry


class BridgeConformanceError(ValueError):
    """Raised when the tracked P2-025A role authority cannot bind the bridge."""


def build_bridge_role_conformance(
    repository_root: Path,
    *,
    repository_commit_id: str,
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
            )
        )
    return tuple(bindings)


__all__ = ["BridgeConformanceError", "build_bridge_role_conformance"]
