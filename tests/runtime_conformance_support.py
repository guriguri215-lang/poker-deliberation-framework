"""Deterministic test builders for the version-1 runtime conformance contract."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from poker_deliberation.runtime_conformance import (
    ApprovalBindingV1,
    AssignmentV1,
    BudgetReferenceV1,
    ConformanceRecordV1,
    ContextProvenanceV1,
    ContextReferenceV1,
    ExecutionAuditV1,
    ExecutionState,
    ReproductionMetadataV1,
    ResultStatus,
    ResultV1,
    RuntimeId,
    RuntimeInventoryV1,
    SemanticRole,
    ToolCapabilityAllowlistV1,
    build_runtime_inventories,
    runtime_inventory_sha256,
)
from poker_deliberation.runtime_conformance.canonical import (
    ALLOWLIST_DOMAIN,
    APPROVAL_BINDING_DOMAIN,
    CONTEXT_REFERENCE_DOMAIN,
    canonical_domain_sha256,
    domain_sha256,
)
from poker_deliberation.schemas import EpistemicLabel

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
HASH_A = "1" * 64
HASH_B = "2" * 64
HASH_C = "3" * 64
HASH_D = "4" * 64


def inventories() -> tuple[RuntimeInventoryV1, RuntimeInventoryV1]:
    return build_runtime_inventories(
        ROOT,
        source_revision=HASH_A,
        python_tool_catalog=("hand_validator", "pot_odds", "solver_status"),
        python_capability_catalog=("deterministic-calculation",),
    )


def _context(
    *,
    producer_runtime: RuntimeId,
    consumer_runtime: RuntimeId,
) -> ContextReferenceV1:
    return ContextReferenceV1(
        reference_kind="fixture",
        context_id="fixture-context",
        context_schema_version="1.0.0",
        classification="internal",
        created_at=NOW,
        expires_at=None,
        payload_sha256=HASH_A,
        policy_sha256=HASH_B,
        envelope_sha256=None,
        provenance=ContextProvenanceV1(
            source_kind="fixture",
            source_sha256=HASH_C,
            producer_runtime=producer_runtime,
            consumer_runtime=consumer_runtime,
            parent_context_id="fixture-parent",
        ),
        budget=BudgetReferenceV1(
            policy_schema_version="1.0.0",
            policy_sha256=HASH_D,
            maximum_runtime_ms=5_000,
            maximum_output_bytes=16_384,
            reference_kind="exact-policy",
        ),
    )


def _record(
    inventory: RuntimeInventoryV1,
    *,
    producer_runtime: RuntimeId,
    runtime_role_id: str,
    context_producer: RuntimeId,
    context_consumer: RuntimeId,
    catalog_status: str,
) -> ConformanceRecordV1:
    context = _context(
        producer_runtime=context_producer,
        consumer_runtime=context_consumer,
    )
    allowlist = ToolCapabilityAllowlistV1(
        policy_version="1.0.0",
        allowed_tools=(),
        allowed_capabilities=(),
        catalog_status=catalog_status,
        policy_source="fixture",
    )
    approval = ApprovalBindingV1(
        requirement="not-required",
        decision="not-applicable",
    )
    objective = "Normalize the same retrospective hand-history facts."
    assignment = AssignmentV1(
        assignment_id=f"{producer_runtime.value}-assignment",
        producer_runtime=producer_runtime,
        runtime_role_id=runtime_role_id,
        semantic_role=SemanticRole.INTAKE,
        objective=objective,
        objective_sha256=domain_sha256(
            "poker-runtime-conformance-objective-v1",
            objective.encode("utf-8"),
        ),
        parent_assignment_id="root-assignment",
        context=context,
        allowlist=allowlist,
        approval=approval,
        role_inventory_sha256=runtime_inventory_sha256(inventory),
    )
    result = ResultV1(
        result_id=f"{producer_runtime.value}-result",
        status=ResultStatus.LIMITED,
        summary="The fixture preserves structured semantics without asserting a calculation.",
        epistemic_label=EpistemicLabel.UNKNOWN,
    )
    audit = ExecutionAuditV1(
        execution_id=f"{producer_runtime.value}-execution",
        producer_runtime=producer_runtime,
        execution_kind="fixture",
        terminal_status="succeeded",
        external_effect=False,
        started_at=NOW,
        completed_at=NOW,
        timing_evidence="complete",
        context_sha256=canonical_domain_sha256(CONTEXT_REFERENCE_DOMAIN, context),
        allowlist_sha256=canonical_domain_sha256(ALLOWLIST_DOMAIN, allowlist),
        approval_binding_sha256=canonical_domain_sha256(
            APPROVAL_BINDING_DOMAIN,
            approval,
        ),
        reproduction=ReproductionMetadataV1(
            framework_version="0.1.0",
            source_commit_id=HASH_A,
            source_commit_status="known",
            tool_contract_versions=(),
        ),
    )
    return ConformanceRecordV1(
        producer_runtime=producer_runtime,
        assignment=assignment,
        result=result,
        error=None,
        execution_state=ExecutionState.EXECUTED,
        execution_audit=audit,
    )


def record_pair() -> tuple[
    ConformanceRecordV1,
    ConformanceRecordV1,
    RuntimeInventoryV1,
    RuntimeInventoryV1,
]:
    codex, python = inventories()
    source = _record(
        codex,
        producer_runtime=RuntimeId.CODEX_NATIVE,
        runtime_role_id="intake-reconstructor",
        context_producer=RuntimeId.CODEX_NATIVE,
        context_consumer=RuntimeId.CODEX_NATIVE,
        catalog_status="undeclared",
    )
    target = _record(
        python,
        producer_runtime=RuntimeId.PYTHON_ORCHESTRATOR,
        runtime_role_id="intake",
        context_producer=RuntimeId.CODEX_NATIVE,
        context_consumer=RuntimeId.PYTHON_ORCHESTRATOR,
        catalog_status="declared",
    )
    return source, target, codex, python


__all__ = [
    "HASH_A",
    "HASH_B",
    "HASH_C",
    "HASH_D",
    "NOW",
    "ROOT",
    "inventories",
    "record_pair",
]
