"""Unit contracts for P2-025A runtime conformance."""

from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from poker_deliberation.runtime_conformance import (
    ApprovalBindingV1,
    AssignmentV1,
    ConformanceRecordV1,
    ContextProvenanceV1,
    ContextReferenceV1,
    ExecutionState,
    ResultStatus,
    ResultV1,
    RoleRelationship,
    RuntimeId,
    StructuredErrorV1,
    ToolCapabilityAllowlistV1,
    ToolResultReferenceV1,
    canonical_json_bytes,
    compare_records,
    parse_conformance_record,
    runtime_inventory_sha256,
    validate_record,
)
from poker_deliberation.runtime_conformance.canonical import (
    APPROVAL_BINDING_DOMAIN,
    CONTEXT_REFERENCE_DOMAIN,
    CanonicalConformanceError,
    canonical_domain_sha256,
)
from poker_deliberation.schemas import EpistemicLabel
from tests.runtime_conformance_support import HASH_A, NOW, inventories, record_pair


def _codes(check: object) -> set[str]:
    return {item.code for item in check.violations}  # type: ignore[attr-defined]


def test_mechanical_role_inventory_and_explicit_mappings_are_complete() -> None:
    codex, python = inventories()

    assert len(codex.roles) == 9
    assert len(python.roles) == 8
    assert len(codex.role_mappings) == 9
    assert codex.role_mappings == python.role_mappings
    calculator = next(
        item for item in codex.role_mappings if item.codex_role_id == "calculator-builder"
    )
    assert calculator.relationship is RoleRelationship.INTENTIONALLY_UNMAPPED
    assert calculator.python_role_id is None
    native_roles = {item.runtime_role_id: item for item in codex.roles}
    assert native_roles["calculator-builder"].read_only is False
    assert native_roles["poker-orchestrator"].read_only is True
    assert all(item.declared_tools is None for item in codex.roles)
    assert all(len(item.source_definition_sha256) == 64 for item in codex.roles)
    assert codex.tool_catalog is None


def test_canonical_record_round_trip_is_byte_exact() -> None:
    source, _, _, _ = record_pair()
    encoded = canonical_json_bytes(source)

    assert parse_conformance_record(encoded) == source
    assert canonical_json_bytes(parse_conformance_record(encoded)) == encoded
    with pytest.raises(CanonicalConformanceError):
        parse_conformance_record(encoded + b"\n")
    with pytest.raises(CanonicalConformanceError):
        parse_conformance_record(b"\xef\xbb\xbf" + encoded)


def test_valid_records_and_cross_runtime_pair_are_conformant() -> None:
    source, target, codex, python = record_pair()

    assert validate_record(source, codex, now=NOW).status == "conformant"
    assert validate_record(target, python, now=NOW).status == "conformant"
    assert (
        compare_records(
            source,
            target,
            codex,
            python,
            now=NOW,
        ).status
        == "conformant"
    )
    assert runtime_inventory_sha256(codex) == source.assignment.role_inventory_sha256


def test_unknown_role_capability_and_allowlist_expansion_are_rejected() -> None:
    source, target, codex, python = record_pair()
    unknown_role = source.model_copy(
        update={
            "assignment": source.assignment.model_copy(update={"runtime_role_id": "unknown-role"})
        }
    )
    assert "unknown-runtime-role" in _codes(validate_record(unknown_role, codex, now=NOW))

    expanded_allowlist = ToolCapabilityAllowlistV1(
        policy_version="1.0.0",
        allowed_tools=("pot_odds",),
        allowed_capabilities=("deterministic-calculation",),
        catalog_status="declared",
        policy_source="fixture",
    )
    expanded = target.model_copy(
        update={
            "assignment": target.assignment.model_copy(update={"allowlist": expanded_allowlist})
        }
    )
    check = compare_records(source, expanded, codex, python, now=NOW)
    assert "allowlist-semantic-mismatch" in _codes(check)

    unknown_allowlist = ToolCapabilityAllowlistV1(
        policy_version="1.0.0",
        allowed_tools=(),
        allowed_capabilities=("unknown-capability",),
        catalog_status="declared",
        policy_source="fixture",
    )
    unknown = target.model_copy(
        update={"assignment": target.assignment.model_copy(update={"allowlist": unknown_allowlist})}
    )
    assert "unknown-capability" in _codes(validate_record(unknown, python, now=NOW))


def test_context_and_approval_mismatches_are_explicit() -> None:
    source, target, codex, python = record_pair()
    changed_context = target.assignment.context.model_copy(update={"classification": "public"})
    changed = target.model_copy(
        update={"assignment": target.assignment.model_copy(update={"context": changed_context})}
    )
    check = compare_records(source, changed, codex, python, now=NOW)
    assert "context-semantic-mismatch" in _codes(check)
    assert "execution-audit-hash-mismatch" in _codes(check)

    approval = ApprovalBindingV1(
        requirement="required",
        request_id="approval-request",
        action_digest_sha256=HASH_A,
        decision="pending",
        expires_at=NOW + timedelta(minutes=5),
    )
    pending_assignment = source.assignment.model_copy(update={"approval": approval})
    pending_result = ResultV1(
        result_id="approval-result",
        status=ResultStatus.APPROVAL_REQUIRED,
        summary="Execution is paused for a bound approval decision.",
        epistemic_label=EpistemicLabel.UNKNOWN,
    )
    pending = ConformanceRecordV1(
        producer_runtime=RuntimeId.CODEX_NATIVE,
        assignment=pending_assignment,
        result=pending_result,
        error=None,
        execution_state=ExecutionState.NOT_EXECUTED,
        execution_audit=None,
    )
    assert validate_record(pending, codex, now=NOW).status == "conformant"
    assert "approval-expired" in _codes(
        validate_record(pending, codex, now=NOW + timedelta(minutes=6))
    )


def test_context_expiry_and_approval_weakening_fail_closed() -> None:
    source, target, codex, python = record_pair()
    expiring_context = ContextReferenceV1(
        reference_kind="context-envelope",
        context_id="expiring-context",
        context_schema_version="1.0.0",
        classification="internal",
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        payload_sha256=HASH_A,
        policy_sha256="2" * 64,
        envelope_sha256="3" * 64,
        provenance=ContextProvenanceV1(
            source_kind="context-envelope",
            source_sha256="4" * 64,
            producer_runtime=RuntimeId.CODEX_NATIVE,
            consumer_runtime=RuntimeId.CODEX_NATIVE,
            parent_context_id="fixture-parent",
        ),
        budget=source.assignment.context.budget,
    )
    context_assignment = source.assignment.model_copy(update={"context": expiring_context})
    context_audit = source.execution_audit.model_copy(
        update={
            "context_sha256": canonical_domain_sha256(
                CONTEXT_REFERENCE_DOMAIN,
                expiring_context,
            )
        }
    )
    expiring = source.model_copy(
        update={
            "assignment": context_assignment,
            "execution_audit": context_audit,
        }
    )
    assert validate_record(expiring, codex, now=NOW).status == "conformant"
    assert "context-expired" in _codes(
        validate_record(expiring, codex, now=NOW + timedelta(minutes=5))
    )

    approved = ApprovalBindingV1(
        requirement="required",
        request_id="approved-action",
        action_digest_sha256=HASH_A,
        decision="approved",
        decision_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
        authority_snapshot_sha256="e" * 64,
    )
    approved_assignment = source.assignment.model_copy(update={"approval": approved})
    approved_audit = source.execution_audit.model_copy(
        update={
            "approval_binding_sha256": canonical_domain_sha256(
                APPROVAL_BINDING_DOMAIN,
                approved,
            )
        }
    )
    approved_source = source.model_copy(
        update={
            "assignment": approved_assignment,
            "execution_audit": approved_audit,
        }
    )
    check = compare_records(
        approved_source,
        target,
        codex,
        python,
        now=NOW,
    )
    assert "approval-binding-mismatch" in _codes(check)


def test_epistemic_and_terminal_claims_require_structured_evidence() -> None:
    with pytest.raises(ValidationError, match="successful tool"):
        ResultV1(
            result_id="unsupported-calculation",
            status=ResultStatus.SUCCEEDED,
            summary="No tool supports this attempted calculation.",
            epistemic_label=EpistemicLabel.CALCULATED,
        )
    with pytest.raises(ValidationError, match="solver evidence"):
        ResultV1(
            result_id="unsupported-equilibrium",
            status=ResultStatus.SUCCEEDED,
            summary="No qualified solver run supports this strategy claim.",
            epistemic_label=EpistemicLabel.UNKNOWN,
            strategy_claim="equilibrium",
        )
    with pytest.raises(ValidationError, match="error/status"):
        ConformanceRecordV1(
            producer_runtime=RuntimeId.CODEX_NATIVE,
            assignment=record_pair()[0].assignment,
            result=ResultV1(
                result_id="failed-result",
                status=ResultStatus.FAILED,
                summary="The fixture failed.",
                epistemic_label=EpistemicLabel.UNKNOWN,
            ),
            error=None,
            execution_state=ExecutionState.NOT_EXECUTED,
            execution_audit=None,
        )


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (ResultStatus.TIMED_OUT, "timeout"),
        (ResultStatus.CANCELLED, "cancellation"),
    ],
)
def test_timeout_and_cancellation_have_structured_errors(
    status: ResultStatus,
    category: str,
) -> None:
    source, _, _, _ = record_pair()
    result = ResultV1(
        result_id=f"{status.value}-result",
        status=status,
        summary="The fixture ended without a semantic result.",
        epistemic_label=EpistemicLabel.UNKNOWN,
    )
    error = StructuredErrorV1(
        code=f"{category}-fixture",
        category=category,  # type: ignore[arg-type]
        retryable=status is ResultStatus.TIMED_OUT,
        message="The fixture records the terminal condition without provider prose.",
    )
    terminal_status = "failed" if status is ResultStatus.TIMED_OUT else "cancelled"
    audit = source.execution_audit.model_copy(update={"terminal_status": terminal_status})
    record = ConformanceRecordV1.model_validate(
        source.model_copy(
            update={"result": result, "error": error, "execution_audit": audit}
        ).model_dump()
    )

    assert record.error == error
    assert validate_record(record, inventories()[0], now=NOW).status == "conformant"


def test_missing_execution_evidence_is_rejected() -> None:
    source, _, _, _ = record_pair()
    raw = source.model_dump()
    raw["execution_audit"] = None

    with pytest.raises(ValidationError, match="execution audit"):
        ConformanceRecordV1.model_validate(raw)


def test_model_copy_does_not_bypass_revalidation_at_public_boundaries() -> None:
    source, _, _, _ = record_pair()
    forged_assignment = source.assignment.model_dump()
    forged_assignment["objective_sha256"] = "f" * 64
    forged = source.model_copy(
        update={"assignment": AssignmentV1.model_construct(**forged_assignment)}
    )

    with pytest.raises(ValidationError):
        ConformanceRecordV1.model_validate(forged.model_dump())


def test_successful_calculation_reference_supports_calculated_label() -> None:
    reference = ToolResultReferenceV1(
        result_id="tool-result",
        tool_name="pot_odds",
        contract_version="2.0.0",
        status="success",
        exactness="exact",
        result_sha256=HASH_A,
    )
    result = ResultV1(
        result_id="calculated-result",
        status=ResultStatus.SUCCEEDED,
        summary="The deterministic tool result is hash-bound.",
        epistemic_label=EpistemicLabel.CALCULATED,
        tool_results=(reference,),
    )

    assert result.epistemic_label is EpistemicLabel.CALCULATED
