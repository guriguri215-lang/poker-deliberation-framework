"""Additive projection from a verified Python product run into the conformance contract."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from poker_deliberation.runtime_conformance.canonical import (
    ALLOWLIST_DOMAIN,
    APPROVAL_BINDING_DOMAIN,
    CONTEXT_REFERENCE_DOMAIN,
    canonical_domain_sha256,
    domain_sha256,
    runtime_inventory_sha256,
    sha256_bytes,
)
from poker_deliberation.runtime_conformance.models import (
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
    ToolResultReferenceV1,
)
from poker_deliberation.schemas import EpistemicLabel, FinalReport, ToolStatus
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes as storage_canonical_json_bytes,
)
from poker_deliberation.storage.terminal_models import RunReadStatus, VerifiedRunReadV2


class ProductProjectionError(ValueError):
    """A product run lacked evidence needed for a non-invented projection."""


T = TypeVar("T")


def _portable_id(prefix: str, value: str) -> str:
    candidate = f"{prefix}-{value}"
    if len(candidate) <= 128:
        return candidate
    return f"{prefix}-{domain_sha256('poker-runtime-conformance-id-v1', value.encode())}"


def _only_item(values: Iterable[T], *, description: str) -> T:
    items = tuple(values)
    if len(items) != 1:
        raise ProductProjectionError(f"verified product run has no unique {description}")
    return items[0]


def _tool_reference(
    result: object,
    verified: VerifiedRunReadV2,
) -> ToolResultReferenceV1:
    from poker_deliberation.schemas import ToolResult

    if not isinstance(result, ToolResult):
        raise ProductProjectionError("product result type is unsupported")
    logical_name = f"tool_results/{result.result_id}.json"
    try:
        persisted = verified.payload_bytes(logical_name)
    except KeyError as exc:
        raise ProductProjectionError("verified tool-result payload is missing") from exc
    expected = storage_canonical_json_bytes(result)
    if persisted != expected:
        raise ProductProjectionError("report tool result differs from verified product bytes")
    return ToolResultReferenceV1(
        result_id=result.result_id,
        tool_name=result.tool_name,
        contract_version=result.contract_version,
        status=result.status.value,
        exactness=result.numeric_exactness.value,
        result_sha256=sha256_bytes(persisted),
    )


def project_python_product_run(
    report: FinalReport,
    verified: VerifiedRunReadV2,
    runtime_inventory: RuntimeInventoryV1,
) -> ConformanceRecordV1:
    """Project only byte-verified, terminal, local Python output.

    The function does not modify product artifacts, invoke providers, or claim a
    Codex/Python execution bridge.
    """

    if runtime_inventory.runtime is not RuntimeId.PYTHON_ORCHESTRATOR:
        raise ProductProjectionError("projection requires the Python runtime inventory")
    if verified.read_status is not RunReadStatus.SUCCEEDED:
        raise ProductProjectionError("only a verified succeeded product run can be projected")
    if verified.completion_marker is None:
        raise ProductProjectionError("verified product run lacks a completion marker")
    if report.run_id != verified.run_id or report.run_status != "completed":
        raise ProductProjectionError("report and verified product terminal identity differ")
    try:
        persisted_report = verified.payload_bytes("final_report.json")
    except KeyError as exc:
        raise ProductProjectionError("verified final-report payload is missing") from exc
    if persisted_report != storage_canonical_json_bytes(report):
        raise ProductProjectionError("report differs from verified product bytes")
    if report.approvals:
        raise ProductProjectionError("approval-bearing product runs require a separate binding")
    if any(record.provider != "local" for record in report.agent_execution_records):
        raise ProductProjectionError("external provider conclusions are not projected")

    orchestrator_role = next(
        (role for role in runtime_inventory.roles if role.runtime_role_id == "python-orchestrator"),
        None,
    )
    if (
        orchestrator_role is None
        or orchestrator_role.semantic_role is not SemanticRole.ORCHESTRATION
    ):
        raise ProductProjectionError("Python orchestrator role inventory is incomplete")

    input_entry = _only_item(
        (item for item in verified.manifest.artifacts if item.logical_name == "input.json"),
        description="input artifact",
    )
    tool_references = tuple(
        sorted(
            (_tool_reference(result, verified) for result in report.tool_results),
            key=lambda item: item.result_id.encode("utf-8"),
        )
    )
    tool_names = tuple(
        sorted(
            {item.tool_name for item in tool_references},
            key=lambda item: item.encode("utf-8"),
        )
    )
    if runtime_inventory.tool_catalog is None or any(
        tool not in runtime_inventory.tool_catalog for tool in tool_names
    ):
        raise ProductProjectionError("product tool is absent from the runtime inventory")

    context = ContextReferenceV1(
        reference_kind="verified-product-input",
        context_id=_portable_id("product-input", report.run_id),
        context_schema_version=input_entry.artifact_schema_version,
        classification=input_entry.classification.value,
        created_at=verified.manifest.created_at,
        expires_at=None,
        payload_sha256=verified.manifest.canonical_input_sha256,
        policy_sha256=verified.manifest.local_data_policy_sha256,
        envelope_sha256=None,
        provenance=ContextProvenanceV1(
            source_kind="verified-product-input",
            source_sha256=input_entry.source_sha256,
            producer_runtime=RuntimeId.PYTHON_ORCHESTRATOR,
            consumer_runtime=RuntimeId.PYTHON_ORCHESTRATOR,
            parent_context_id=None,
        ),
        budget=BudgetReferenceV1(
            policy_schema_version=verified.manifest.run_schema_version,
            policy_sha256=verified.manifest.budget_policy_sha256,
            maximum_runtime_ms=None,
            maximum_output_bytes=None,
            reference_kind="verified-policy-hash",
        ),
    )
    allowlist = ToolCapabilityAllowlistV1(
        policy_version=verified.manifest.run_schema_version,
        allowed_tools=tool_names,
        allowed_capabilities=(),
        catalog_status="declared",
        policy_source="verified-product-result-bindings",
    )
    approval = ApprovalBindingV1(
        requirement="not-required",
        decision="not-applicable",
    )
    objective = "Project a verified local Python product run without changing its semantics."
    assignment = AssignmentV1(
        assignment_id=_portable_id("product-projection", report.run_id),
        producer_runtime=RuntimeId.PYTHON_ORCHESTRATOR,
        runtime_role_id=orchestrator_role.runtime_role_id,
        semantic_role=orchestrator_role.semantic_role,
        objective=objective,
        objective_sha256=domain_sha256(
            "poker-runtime-conformance-objective-v1",
            objective.encode("utf-8"),
        ),
        parent_assignment_id=None,
        context=context,
        allowlist=allowlist,
        approval=approval,
        role_inventory_sha256=runtime_inventory_sha256(runtime_inventory),
    )

    successful_tools = tuple(
        item for item in tool_references if item.status == ToolStatus.SUCCESS.value
    )
    limitations = tuple(
        sorted(
            {
                (
                    f"tool-unavailable:{item.tool_name}"
                    if item.status == ToolStatus.UNAVAILABLE.value
                    else f"tool-failed:{item.tool_name}"
                )
                for item in tool_references
                if item.status != ToolStatus.SUCCESS.value
            },
            key=lambda item: item.encode("utf-8"),
        )
    )
    limited = bool(limitations) or not successful_tools
    result = ResultV1(
        result_id=_portable_id("product-result", report.run_id),
        status=ResultStatus.LIMITED if limited else ResultStatus.SUCCEEDED,
        summary=(
            "Verified product projection contains deterministic calculator evidence."
            if successful_tools
            else "Verified product projection contains no successful calculator evidence."
        ),
        epistemic_label=(EpistemicLabel.CALCULATED if successful_tools else EpistemicLabel.UNKNOWN),
        strategy_claim="none",
        tool_results=tool_references,
        limitations=limitations,
    )

    agent_ids = tuple(
        sorted(
            {record.execution_id for record in report.agent_execution_records},
            key=lambda item: item.encode("utf-8"),
        )
    )
    if len(agent_ids) != len(report.agent_execution_records):
        raise ProductProjectionError("product agent execution IDs are not unique")
    starts = tuple(record.started_at for record in report.agent_execution_records)
    completed_at = verified.completion_marker.published_at
    tool_versions = tuple(
        (item.tool_name, item.contract_version) for item in verified.manifest.tool_contract_versions
    )
    audit = ExecutionAuditV1(
        execution_id=_portable_id("product-execution", report.run_id),
        producer_runtime=RuntimeId.PYTHON_ORCHESTRATOR,
        execution_kind="python-product-run",
        terminal_status="succeeded",
        external_effect=False,
        started_at=min(starts) if starts else None,
        completed_at=completed_at,
        timing_evidence="complete" if starts else "completion-only",
        context_sha256=canonical_domain_sha256(CONTEXT_REFERENCE_DOMAIN, context),
        allowlist_sha256=canonical_domain_sha256(ALLOWLIST_DOMAIN, allowlist),
        approval_binding_sha256=canonical_domain_sha256(
            APPROVAL_BINDING_DOMAIN,
            approval,
        ),
        current_pointer_sha256=verified.current_pointer_sha256,
        manifest_sha256=verified.manifest_sha256,
        inventory_sha256=verified.inventory_sha256,
        agent_execution_ids=agent_ids,
        tool_result_ids=tuple(item.result_id for item in tool_references),
        reproduction=ReproductionMetadataV1(
            framework_version=verified.manifest.framework_version,
            source_commit_id=verified.manifest.source_commit_id,
            source_commit_status=(
                "unknown" if verified.manifest.source_commit_id == "0" * 64 else "known"
            ),
            tool_contract_versions=tool_versions,
        ),
    )
    return ConformanceRecordV1(
        producer_runtime=RuntimeId.PYTHON_ORCHESTRATOR,
        assignment=assignment,
        result=result,
        error=None,
        execution_state=ExecutionState.EXECUTED,
        execution_audit=audit,
        runtime_bridge_used=False,
    )


__all__ = ["ProductProjectionError", "project_python_product_run"]
