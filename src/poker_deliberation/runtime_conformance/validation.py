"""Pure validation for one runtime record and cross-runtime semantic preservation."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from poker_deliberation.runtime_conformance.canonical import (
    ALLOWLIST_DOMAIN,
    APPROVAL_BINDING_DOMAIN,
    CONTEXT_REFERENCE_DOMAIN,
    canonical_domain_sha256,
    runtime_inventory_sha256,
)
from poker_deliberation.runtime_conformance.models import (
    ConformanceCheckV1,
    ConformanceRecordV1,
    ConformanceViolationV1,
    ResultStatus,
    RoleRelationship,
    RuntimeInventoryV1,
)


def _violation(code: str, path: str, summary: str) -> ConformanceViolationV1:
    return ConformanceViolationV1(code=code, path=path, summary=summary)


def _check(violations: Iterable[ConformanceViolationV1]) -> ConformanceCheckV1:
    ordered = tuple(
        sorted(
            violations,
            key=lambda item: (
                item.path.encode("utf-8"),
                item.code.encode("utf-8"),
                item.summary.encode("utf-8"),
            ),
        )
    )
    return ConformanceCheckV1(
        status="nonconformant" if ordered else "conformant",
        violations=ordered,
    )


def _require_utc(now: datetime) -> None:
    offset = now.utcoffset()
    if now.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError("conformance evaluation time must be timezone-aware UTC")


def validate_record(
    record: ConformanceRecordV1,
    inventory: RuntimeInventoryV1,
    *,
    now: datetime,
) -> ConformanceCheckV1:
    """Validate a record against a mechanically derived runtime inventory."""

    _require_utc(now)
    violations: list[ConformanceViolationV1] = []
    assignment = record.assignment
    if inventory.runtime is not record.producer_runtime:
        violations.append(
            _violation(
                "runtime-inventory-mismatch",
                "producer_runtime",
                "The supplied inventory belongs to another runtime.",
            )
        )

    inventory_hash = runtime_inventory_sha256(inventory)
    if assignment.role_inventory_sha256 != inventory_hash:
        violations.append(
            _violation(
                "role-inventory-hash-mismatch",
                "assignment.role_inventory_sha256",
                "The assignment is not bound to the supplied role inventory.",
            )
        )

    role = next(
        (item for item in inventory.roles if item.runtime_role_id == assignment.runtime_role_id),
        None,
    )
    if role is None:
        violations.append(
            _violation(
                "unknown-runtime-role",
                "assignment.runtime_role_id",
                "The assignment role is absent from the runtime inventory.",
            )
        )
    elif role.semantic_role is not assignment.semantic_role:
        violations.append(
            _violation(
                "semantic-role-mismatch",
                "assignment.semantic_role",
                "The assignment semantic role differs from the inventoried role.",
            )
        )

    allowlist = assignment.allowlist
    if inventory.tool_catalog is None:
        if allowlist.catalog_status != "undeclared":
            violations.append(
                _violation(
                    "tool-catalog-undeclared",
                    "assignment.allowlist.catalog_status",
                    "The runtime has no declared tool catalog.",
                )
            )
    else:
        if allowlist.catalog_status != "declared":
            violations.append(
                _violation(
                    "tool-catalog-status-mismatch",
                    "assignment.allowlist.catalog_status",
                    "A declared runtime catalog cannot be treated as undeclared.",
                )
            )
        unknown_tools = tuple(
            tool for tool in allowlist.allowed_tools if tool not in inventory.tool_catalog
        )
        for tool in unknown_tools:
            violations.append(
                _violation(
                    "unknown-tool",
                    f"assignment.allowlist.allowed_tools.{tool}",
                    "The tool is absent from the bound runtime catalog.",
                )
            )

    unknown_capabilities = tuple(
        capability
        for capability in allowlist.allowed_capabilities
        if capability not in inventory.capability_catalog
    )
    for capability in unknown_capabilities:
        violations.append(
            _violation(
                "unknown-capability",
                f"assignment.allowlist.allowed_capabilities.{capability}",
                "The capability is absent from the bound runtime catalog.",
            )
        )

    context = assignment.context
    if context.provenance.consumer_runtime is not record.producer_runtime:
        violations.append(
            _violation(
                "context-consumer-runtime-mismatch",
                "assignment.context.provenance.consumer_runtime",
                "The context consumer differs from the record producer runtime.",
            )
        )
    if context.expires_at is not None and now >= context.expires_at:
        violations.append(
            _violation(
                "context-expired",
                "assignment.context.expires_at",
                "The referenced context has expired.",
            )
        )

    approval = assignment.approval
    if (
        approval.requirement == "required"
        and approval.expires_at is not None
        and now >= approval.expires_at
    ):
        violations.append(
            _violation(
                "approval-expired",
                "assignment.approval.expires_at",
                "The approval binding has expired.",
            )
        )
    if (
        record.execution_audit is not None
        and approval.requirement == "required"
        and approval.decision != "approved"
    ):
        violations.append(
            _violation(
                "execution-without-approval",
                "execution_audit",
                "Execution occurred without a current approved binding.",
            )
        )

    audit = record.execution_audit
    if audit is not None:
        expected_hashes = {
            "context_sha256": canonical_domain_sha256(CONTEXT_REFERENCE_DOMAIN, context),
            "allowlist_sha256": canonical_domain_sha256(ALLOWLIST_DOMAIN, allowlist),
            "approval_binding_sha256": canonical_domain_sha256(
                APPROVAL_BINDING_DOMAIN,
                approval,
            ),
        }
        for field_name, expected_hash in expected_hashes.items():
            if getattr(audit, field_name) != expected_hash:
                violations.append(
                    _violation(
                        "execution-audit-hash-mismatch",
                        f"execution_audit.{field_name}",
                        "Execution audit binding differs from the assignment.",
                    )
                )

        expected_tool_ids = tuple(item.result_id for item in record.result.tool_results)
        if audit.tool_result_ids != expected_tool_ids:
            violations.append(
                _violation(
                    "execution-tool-lineage-mismatch",
                    "execution_audit.tool_result_ids",
                    "Execution tool lineage differs from the structured result.",
                )
            )

        acceptable_terminal_statuses = {
            ResultStatus.SUCCEEDED: {"succeeded"},
            ResultStatus.LIMITED: {"succeeded"},
            ResultStatus.FAILED: {"failed"},
            ResultStatus.REFUSED: {"refused"},
            ResultStatus.TIMED_OUT: {"failed"},
            ResultStatus.CANCELLED: {"cancelled", "cancel-unconfirmed"},
        }.get(record.result.status, set())
        if audit.terminal_status not in acceptable_terminal_statuses:
            violations.append(
                _violation(
                    "terminal-status-mismatch",
                    "execution_audit.terminal_status",
                    "Execution audit terminal status differs from the result.",
                )
            )
    return _check(violations)


def compare_records(
    source: ConformanceRecordV1,
    target: ConformanceRecordV1,
    source_inventory: RuntimeInventoryV1,
    target_inventory: RuntimeInventoryV1,
    *,
    now: datetime,
) -> ConformanceCheckV1:
    """Check whether a target runtime preserved a source record's semantics."""

    _require_utc(now)
    violations = list(validate_record(source, source_inventory, now=now).violations)
    violations.extend(validate_record(target, target_inventory, now=now).violations)

    source_assignment = source.assignment
    target_assignment = target.assignment
    if source.producer_runtime is target.producer_runtime:
        violations.append(
            _violation(
                "cross-runtime-required",
                "producer_runtime",
                "A cross-runtime comparison requires distinct producer runtimes.",
            )
        )

    if source_inventory.role_mappings != target_inventory.role_mappings:
        violations.append(
            _violation(
                "role-mapping-inventory-mismatch",
                "inventory.role_mappings",
                "The runtime inventories do not share one explicit role mapping table.",
            )
        )

    mapping = next(
        (
            item
            for item in source_inventory.role_mappings
            if item.semantic_role is source_assignment.semantic_role
            and (
                item.codex_role_id == source_assignment.runtime_role_id
                or item.python_role_id == source_assignment.runtime_role_id
            )
        ),
        None,
    )
    if (
        mapping is None
        or mapping.relationship is RoleRelationship.INTENTIONALLY_UNMAPPED
        or target_assignment.runtime_role_id not in {mapping.codex_role_id, mapping.python_role_id}
    ):
        violations.append(
            _violation(
                "role-not-cross-runtime",
                "assignment.runtime_role_id",
                "The selected roles do not form an explicit cross-runtime mapping.",
            )
        )

    exact_assignment_fields = (
        "semantic_role",
        "objective",
        "objective_sha256",
        "parent_assignment_id",
    )
    for field_name in exact_assignment_fields:
        if getattr(source_assignment, field_name) != getattr(target_assignment, field_name):
            violations.append(
                _violation(
                    "assignment-semantic-mismatch",
                    f"assignment.{field_name}",
                    "The target assignment changed source semantics.",
                )
            )

    source_context = source_assignment.context
    target_context = target_assignment.context
    context_fields = (
        "reference_kind",
        "context_id",
        "context_schema_version",
        "classification",
        "created_at",
        "expires_at",
        "payload_sha256",
        "policy_sha256",
        "envelope_sha256",
        "budget",
    )
    for field_name in context_fields:
        if getattr(source_context, field_name) != getattr(target_context, field_name):
            violations.append(
                _violation(
                    "context-semantic-mismatch",
                    f"assignment.context.{field_name}",
                    "The target context changed source provenance or policy semantics.",
                )
            )
    if (
        target_context.provenance.source_sha256 != source_context.provenance.source_sha256
        or target_context.provenance.source_kind != source_context.provenance.source_kind
        or target_context.provenance.parent_context_id
        != source_context.provenance.parent_context_id
        or target_context.provenance.producer_runtime is not source.producer_runtime
        or target_context.provenance.consumer_runtime is not target.producer_runtime
    ):
        violations.append(
            _violation(
                "context-provenance-mismatch",
                "assignment.context.provenance",
                "The target context lacks an exact source-to-target provenance binding.",
            )
        )

    source_allowlist = source_assignment.allowlist
    target_allowlist = target_assignment.allowlist
    for field_name in (
        "policy_version",
        "allowed_tools",
        "allowed_capabilities",
        "policy_source",
        "interpretation",
    ):
        if getattr(source_allowlist, field_name) != getattr(target_allowlist, field_name):
            violations.append(
                _violation(
                    "allowlist-semantic-mismatch",
                    f"assignment.allowlist.{field_name}",
                    "The target changed the exact tool or capability allowlist.",
                )
            )

    if source_assignment.approval != target_assignment.approval:
        violations.append(
            _violation(
                "approval-binding-mismatch",
                "assignment.approval",
                "The target changed the approval decision or action digest binding.",
            )
        )

    source_result = source.result
    target_result = target.result
    result_fields = (
        "summary",
        "status",
        "epistemic_label",
        "strategy_claim",
        "tool_results",
        "evidence",
        "provider_conclusions",
        "solver_evidence",
        "limitations",
    )
    for field_name in result_fields:
        if getattr(source_result, field_name) != getattr(target_result, field_name):
            violations.append(
                _violation(
                    "result-semantic-mismatch",
                    f"result.{field_name}",
                    "The target changed a structured result semantic.",
                )
            )
    if source.error != target.error:
        violations.append(
            _violation(
                "structured-error-mismatch",
                "error",
                "The target changed the structured terminal error.",
            )
        )
    return _check(violations)


__all__ = ["compare_records", "validate_record"]
