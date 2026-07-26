"""Mechanical Codex/Python role inventory and explicit semantic mappings."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal, cast

from poker_deliberation.agents import ROLE_CATALOG
from poker_deliberation.runtime_conformance.canonical import (
    canonical_json_bytes,
    domain_sha256,
)
from poker_deliberation.runtime_conformance.models import (
    RoleInventoryEntryV1,
    RoleKind,
    RoleMappingV1,
    RoleRelationship,
    RuntimeId,
    RuntimeInventoryV1,
    SemanticRole,
)

_CODEX_FIELDS = {
    "name",
    "description",
    "model",
    "model_reasoning_effort",
    "sandbox_mode",
    "nickname_candidates",
    "developer_instructions",
}

_CODEX_ROLE_SEMANTICS = {
    "adjudicator": SemanticRole.ADJUDICATION,
    "calculator-builder": SemanticRole.CALCULATOR_DEVELOPMENT,
    "evidence-researcher": SemanticRole.EVIDENCE_RESEARCH,
    "intake-reconstructor": SemanticRole.INTAKE,
    "math-tool-auditor": SemanticRole.MATH_AUDIT,
    "poker-orchestrator": SemanticRole.ORCHESTRATION,
    "report-writer": SemanticRole.REPORT_WRITING,
    "skeptic-falsifier": SemanticRole.SKEPTICISM,
    "strategy-analyst": SemanticRole.STRATEGY_ANALYSIS,
}

_PYTHON_ROLE_SEMANTICS = {
    "adjudicator": SemanticRole.ADJUDICATION,
    "evidence-researcher": SemanticRole.EVIDENCE_RESEARCH,
    "intake": SemanticRole.INTAKE,
    "math-auditor": SemanticRole.MATH_AUDIT,
    "report-writer": SemanticRole.REPORT_WRITING,
    "skeptic": SemanticRole.SKEPTICISM,
    "strategy-analyst": SemanticRole.STRATEGY_ANALYSIS,
}


class RuntimeInventoryError(ValueError):
    """A runtime role source could not be inventoried without inference."""


def _require_string(data: dict[str, Any], field_name: str, source: Path) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeInventoryError(f"{source.name} has invalid {field_name}")
    return value


def _role_kind(role_id: str) -> RoleKind:
    if role_id in {"poker-orchestrator", "python-orchestrator"}:
        return RoleKind.ORCHESTRATOR
    if role_id == "calculator-builder":
        return RoleKind.DEVELOPMENT
    return RoleKind.ANALYSIS


def _definition_hash(value: object) -> str:
    return domain_sha256(
        "poker-runtime-conformance-role-definition-v1",
        canonical_json_bytes(value),
    )


def semantic_role_mappings() -> tuple[RoleMappingV1, ...]:
    pairs = (
        (
            SemanticRole.ADJUDICATION,
            RoleRelationship.SEMANTIC_PEER,
            "adjudicator",
            "adjudicator",
            "Both roles adjudicate disputed claims from evidence and calculations.",
        ),
        (
            SemanticRole.CALCULATOR_DEVELOPMENT,
            RoleRelationship.INTENTIONALLY_UNMAPPED,
            "calculator-builder",
            None,
            "Calculator implementation is a Codex development role, not a Python analysis role.",
        ),
        (
            SemanticRole.EVIDENCE_RESEARCH,
            RoleRelationship.SEMANTIC_PEER,
            "evidence-researcher",
            "evidence-researcher",
            "Both roles map material claims to evidence without deciding by vote.",
        ),
        (
            SemanticRole.INTAKE,
            RoleRelationship.SEMANTIC_PEER,
            "intake-reconstructor",
            "intake",
            "Both roles normalize and validate decision-relevant input.",
        ),
        (
            SemanticRole.MATH_AUDIT,
            RoleRelationship.SEMANTIC_PEER,
            "math-tool-auditor",
            "math-auditor",
            "Both roles verify formulas and deterministic calculator evidence.",
        ),
        (
            SemanticRole.ORCHESTRATION,
            RoleRelationship.RUNTIME_SPECIFIC,
            "poker-orchestrator",
            "python-orchestrator",
            "Both own orchestration semantics but run on separate surfaces and are not executions "
            "of one another.",
        ),
        (
            SemanticRole.REPORT_WRITING,
            RoleRelationship.SEMANTIC_PEER,
            "report-writer",
            "report-writer",
            "Both roles render an adjudicated record without adding claims.",
        ),
        (
            SemanticRole.SKEPTICISM,
            RoleRelationship.SEMANTIC_PEER,
            "skeptic-falsifier",
            "skeptic",
            "Both roles search for concrete counterexamples and missing premises.",
        ),
        (
            SemanticRole.STRATEGY_ANALYSIS,
            RoleRelationship.SEMANTIC_PEER,
            "strategy-analyst",
            "strategy-analyst",
            "Both roles compare poker objectives and lines without unsupported solver claims.",
        ),
    )
    values = tuple(
        RoleMappingV1(
            semantic_role=semantic_role,
            relationship=relationship,
            codex_role_id=codex_role_id,
            python_role_id=python_role_id,
            rationale=rationale,
        )
        for semantic_role, relationship, codex_role_id, python_role_id, rationale in pairs
    )
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item.semantic_role.value.encode("utf-8"),
                (item.codex_role_id or "").encode("utf-8"),
                (item.python_role_id or "").encode("utf-8"),
            ),
        )
    )


def load_codex_role_inventory(agent_directory: Path) -> tuple[RoleInventoryEntryV1, ...]:
    """Parse tracked native role TOML without importing or executing it."""

    if not agent_directory.is_dir():
        raise RuntimeInventoryError("Codex agent directory is missing")
    entries: list[RoleInventoryEntryV1] = []
    sources = sorted(
        agent_directory.glob("*.toml"),
        key=lambda path: path.name.encode("utf-8"),
    )
    for source in sources:
        try:
            raw = tomllib.loads(source.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise RuntimeInventoryError(f"{source.name} could not be parsed") from exc
        if set(raw) != _CODEX_FIELDS:
            raise RuntimeInventoryError(
                f"{source.name} fields differ from the tracked role contract"
            )
        role_id = _require_string(raw, "name", source)
        if source.stem != role_id or role_id not in _CODEX_ROLE_SEMANTICS:
            raise RuntimeInventoryError(f"{source.name} role identity is unknown")
        sandbox_mode = _require_string(raw, "sandbox_mode", source)
        if sandbox_mode not in {"read-only", "workspace-write"}:
            raise RuntimeInventoryError(f"{source.name} has unsupported sandbox_mode")
        typed_sandbox_mode = cast(
            Literal["read-only", "workspace-write"],
            sandbox_mode,
        )
        development = role_id == "calculator-builder"
        entries.append(
            RoleInventoryEntryV1(
                runtime=RuntimeId.CODEX_NATIVE,
                runtime_role_id=role_id,
                semantic_role=_CODEX_ROLE_SEMANTICS[role_id],
                role_kind=_role_kind(role_id),
                purpose=_require_string(raw, "description", source),
                read_only=sandbox_mode == "read-only",
                source_path=f".codex/agents/{source.name}",
                source_definition_sha256=_definition_hash(raw),
                catalog_member=True,
                sandbox_mode=typed_sandbox_mode,
                declared_tools=None,
                tool_policy_source="ambient-runtime-undeclared",
                approval_policy_source=(
                    "development-approval-contract" if development else "codex-runtime-policy"
                ),
                expected_result_kind=(
                    "repository-change" if development else "specialist-conclusion"
                ),
                execution_audit_requirement="runtime-native-audit",
            )
        )
    if set(item.runtime_role_id for item in entries) != set(_CODEX_ROLE_SEMANTICS):
        raise RuntimeInventoryError("Codex role inventory is incomplete")
    return tuple(entries)


def load_python_role_inventory() -> tuple[RoleInventoryEntryV1, ...]:
    """Inventory ROLE_CATALOG plus the runtime owner that is intentionally outside it."""

    if set(ROLE_CATALOG) != set(_PYTHON_ROLE_SEMANTICS):
        raise RuntimeInventoryError("Python ROLE_CATALOG differs from the conformance mapping")
    entries = [
        RoleInventoryEntryV1(
            runtime=RuntimeId.PYTHON_ORCHESTRATOR,
            runtime_role_id=role_id,
            semantic_role=_PYTHON_ROLE_SEMANTICS[role_id],
            role_kind=RoleKind.ANALYSIS,
            purpose=definition.purpose,
            read_only=definition.read_only,
            source_path="src/poker_deliberation/agents/roles.py",
            source_definition_sha256=_definition_hash(
                {
                    "name": definition.name,
                    "purpose": definition.purpose,
                    "read_only": definition.read_only,
                }
            ),
            catalog_member=True,
            sandbox_mode=None,
            declared_tools=None,
            tool_policy_source="assignment-and-registry",
            approval_policy_source="python-approval-contract",
            expected_result_kind="agent-report",
            execution_audit_requirement="python-agent-execution-record",
        )
        for role_id, definition in ROLE_CATALOG.items()
    ]
    entries.append(
        RoleInventoryEntryV1(
            runtime=RuntimeId.PYTHON_ORCHESTRATOR,
            runtime_role_id="python-orchestrator",
            semantic_role=SemanticRole.ORCHESTRATION,
            role_kind=RoleKind.ORCHESTRATOR,
            purpose="Own deterministic routing, effects, state, storage, and final synthesis.",
            read_only=False,
            source_path="src/poker_deliberation/orchestrator.py",
            source_definition_sha256=_definition_hash(
                {
                    "name": "python-orchestrator",
                    "purpose": (
                        "Own deterministic routing, effects, state, storage, and final synthesis."
                    ),
                    "read_only": False,
                }
            ),
            catalog_member=False,
            sandbox_mode=None,
            declared_tools=None,
            tool_policy_source="assignment-and-registry",
            approval_policy_source="python-approval-contract",
            expected_result_kind="verified-product-run",
            execution_audit_requirement="python-product-run-audit",
        )
    )
    return tuple(sorted(entries, key=lambda item: item.runtime_role_id.encode("utf-8")))


def build_runtime_inventories(
    repository_root: Path,
    *,
    source_revision: str,
    python_tool_catalog: tuple[str, ...],
    python_capability_catalog: tuple[str, ...],
) -> tuple[RuntimeInventoryV1, RuntimeInventoryV1]:
    mappings = semantic_role_mappings()
    codex = RuntimeInventoryV1(
        runtime=RuntimeId.CODEX_NATIVE,
        roles=load_codex_role_inventory(repository_root / ".codex" / "agents"),
        role_mappings=mappings,
        tool_catalog=None,
        capability_catalog=(),
        source_revision=source_revision,
    )
    python = RuntimeInventoryV1(
        runtime=RuntimeId.PYTHON_ORCHESTRATOR,
        roles=load_python_role_inventory(),
        role_mappings=mappings,
        tool_catalog=python_tool_catalog,
        capability_catalog=python_capability_catalog,
        source_revision=source_revision,
    )
    return codex, python


__all__ = [
    "RuntimeInventoryError",
    "build_runtime_inventories",
    "load_codex_role_inventory",
    "load_python_role_inventory",
    "semantic_role_mappings",
]
