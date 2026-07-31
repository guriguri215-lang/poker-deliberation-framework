"""Durable provenance for the bounded Japanese natural-language intake."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal, NoReturn, cast

from pydantic import ValidationError

from poker_deliberation.agents import select_roles
from poker_deliberation.bounded_natural_language import (
    BoundedNaturalLanguageAdmission,
    BoundedNaturalLanguageError,
    _admit_bounded_at,
    bounded_provenance_sha256,
    bounded_terminal_revision_root_sha256,
)
from poker_deliberation.bounded_natural_language_models import (
    BOUNDED_NL_PROVENANCE_CANONICALIZATION_ID,
    BOUNDED_NL_TOOL_ALLOWLIST,
    BOUNDED_NL_TOOL_ORDER,
    MAX_BOUNDED_NL_ARTIFACT_BYTES,
    BoundedAgentSupportV1,
    BoundedNaturalLanguageDiagnosticCode,
    BoundedNaturalLanguageProvenanceV1,
    BoundedToolSupportV1,
)
from poker_deliberation.confirmed_review import (
    _expected_agent_context_fields,
    _tool_result_semantic_projection,
    _validate_confirmed_report_projection,
)
from poker_deliberation.context_lifecycle import context_payload
from poker_deliberation.phases.services import build_agent_context
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentReport,
    ConfidenceGrade,
    EpistemicLabel,
    FinalReport,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_domain_sha256,
    canonical_json_bytes,
)
from poker_deliberation.tools import default_registry

_ASSIGNMENT_ID = re.compile(r"assignment-[0-9a-f]{12}")
_EXECUTION_ID = re.compile(r"execution-[0-9a-f]{24}")
_CONTEXT_ID = re.compile(r"context-[0-9a-f]{24}")
_ATTEMPT_ID = re.compile(r"attempt-[0-9a-f]{24}")
_TOOL_MAX_DURATION_SECONDS = 30.0
_CONTEXT_MAX_DURATION_SECONDS = 30.0


def _fail(code: BoundedNaturalLanguageDiagnosticCode, field_path: str) -> NoReturn:
    raise BoundedNaturalLanguageError(code, field_path)


def _domain_sha256(suffix: str, value: object) -> str:
    return canonical_domain_sha256(
        BOUNDED_NL_PROVENANCE_CANONICALIZATION_ID + suffix,
        value,
    )


def _strict_provenance(
    provenance: BoundedNaturalLanguageProvenanceV1,
) -> BoundedNaturalLanguageProvenanceV1:
    try:
        payload = canonical_json_bytes(provenance)
        if len(payload) > MAX_BOUNDED_NL_ARTIFACT_BYTES:
            _fail(BoundedNaturalLanguageDiagnosticCode.STORAGE, "bounded_nl_provenance.json")
        return BoundedNaturalLanguageProvenanceV1.model_validate_json(payload, strict=True)
    except (CanonicalStorageError, ValidationError):
        _fail(BoundedNaturalLanguageDiagnosticCode.STORAGE, "bounded_nl_provenance.json")


def _tool_label(result: ToolResult) -> EpistemicLabel:
    if result.status is not ToolStatus.SUCCESS:
        return EpistemicLabel.UNKNOWN
    if result.numeric_exactness in {
        NumericalExactness.EXACT,
        NumericalExactness.EXACT_UNDER_MODEL,
        NumericalExactness.FLOATING_VERIFIED,
    }:
        return EpistemicLabel.CALCULATED
    if result.numeric_exactness is NumericalExactness.APPROXIMATE:
        return EpistemicLabel.ESTIMATE
    return EpistemicLabel.UNKNOWN


def _tool_support(result: ToolResult) -> BoundedToolSupportV1:
    if result.tool_name not in BOUNDED_NL_TOOL_ALLOWLIST:
        _fail(BoundedNaturalLanguageDiagnosticCode.TOOL, "report.tool_results")
    if (
        result.numeric_exactness is NumericalExactness.EXACT_UNDER_MODEL
        and not result.model_qualifier
    ) or (
        result.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
        and (result.verification is None or not result.verification.passed)
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.tool_results")
    return BoundedToolSupportV1(
        result_id=result.result_id,
        tool_name=result.tool_name,
        tool_version=result.version,
        contract_version=result.contract_version,
        status=result.status.value,
        epistemic_label=cast(
            Literal["CALCULATED", "ESTIMATE", "UNKNOWN"],
            _tool_label(result).value,
        ),
        input_sha256=_domain_sha256(":tool-input", result.input),
        output_sha256=_domain_sha256(":tool-output", result.output),
        result_sha256=_domain_sha256(":tool-result", result.model_dump(mode="json")),
    )


def _expected_tool_results(
    admission: BoundedNaturalLanguageAdmission,
) -> dict[str, ToolResult]:
    hand = admission.case.hand
    if hand is None:
        _fail(BoundedNaturalLanguageDiagnosticCode.MISSING, "candidate.hand")
    hand_payload = hand.model_dump(mode="json")
    raw_inputs = admission.case.metadata.get("tool_inputs")
    if not isinstance(raw_inputs, dict):
        _fail(BoundedNaturalLanguageDiagnosticCode.TOOL, "candidate.tool_plan")
    raw_ledger = raw_inputs.get("hand_pot_ledger")
    raw_pot_odds = raw_inputs.get("pot_odds")
    if not isinstance(raw_ledger, dict) or not isinstance(raw_pot_odds, dict):
        _fail(BoundedNaturalLanguageDiagnosticCode.TOOL, "candidate.tool_plan")
    inputs = {
        "hand_validator": hand_payload,
        "hand_pot_ledger": {**raw_ledger, "hand": hand_payload},
        "pot_odds": raw_pot_odds,
    }
    registry = default_registry()
    expected: dict[str, ToolResult] = {}
    for tool_name in BOUNDED_NL_TOOL_ORDER:
        result = registry.execute(tool_name, inputs[tool_name])
        if result.status is not ToolStatus.SUCCESS:
            _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.tool_results")
        expected[tool_name] = result
    return expected


def _validate_assignments_and_agents(
    admission: BoundedNaturalLanguageAdmission,
    report: FinalReport,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
) -> tuple[BoundedAgentSupportV1, ...]:
    templates = tuple(select_roles(admission.case))
    expected_roles = [item.agent_role for item in templates]
    ledger = tuple(assignments)
    reports = tuple(agent_reports)
    if (
        len(ledger) != len(templates)
        or [item.agent_role for item in ledger] != expected_roles
        or len({item.assignment_id for item in ledger}) != len(ledger)
        or any(
            _ASSIGNMENT_ID.fullmatch(item.assignment_id) is None
            or item.agent_role != template.agent_role
            or item.task != template.task
            or item.read_only != template.read_only
            for item, template in zip(ledger, templates, strict=True)
        )
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "assignments.json")
    actual_roles = [record.agent_role for record in report.agent_execution_records]
    if (report.run_status == "completed" and actual_roles != expected_roles) or (
        report.run_status == "failed_with_limitations"
        and actual_roles != expected_roles[: len(actual_roles)]
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.agent_execution_records")
    if (
        len(reports) != len(report.agent_execution_records)
        or [item.agent_role for item in reports] != actual_roles
        or len({item.report_id for item in reports}) != len(reports)
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "agent_reports")
    assignment_by_id = {item.assignment_id: item for item in ledger}
    template_by_role = {item.agent_role: item for item in templates}
    registered_tools = frozenset(default_registry().names())
    seen_execution_ids: set[str] = set()
    seen_context_ids: set[str] = set()
    seen_attempt_ids: set[str] = set()
    previous_completed_at = None
    support: list[BoundedAgentSupportV1] = []
    for index, (record, agent_report) in enumerate(
        zip(report.agent_execution_records, reports, strict=True)
    ):
        assignment = assignment_by_id.get(record.assignment_id)
        template = template_by_role.get(record.agent_role)
        expected_allowed_tools = (
            list(BOUNDED_NL_TOOL_ORDER) if record.agent_role == "math-auditor" else []
        )
        if (
            assignment is None
            or template is None
            or record.execution_id in seen_execution_ids
            or record.context_id is None
            or record.context_id in seen_context_ids
            or record.context_attempt_id is None
            or record.context_attempt_id in seen_attempt_ids
            or _EXECUTION_ID.fullmatch(record.execution_id) is None
            or _CONTEXT_ID.fullmatch(record.context_id) is None
            or _ATTEMPT_ID.fullmatch(record.context_attempt_id) is None
            or record.provider != "local"
            or record.provider_version != "1.0.0"
            or record.model is not None
            or record.reasoning_effort is not None
            or record.allowed_tools != expected_allowed_tools
            or record.context_schema_version != "1.0.0"
            or record.context_classification != "internal"
            or record.context_producer_runtime != "python-local"
            or record.context_consumer_runtime != "python-local"
            or record.context_expires_at is None
        ):
            _fail(
                BoundedNaturalLanguageDiagnosticCode.LOCAL_PROVIDER,
                "report.agent_execution_records",
            )
        assert assignment is not None
        assert template is not None
        assert record.context_id is not None
        assert record.context_attempt_id is not None
        assert record.context_expires_at is not None
        if (
            record.started_at.tzinfo is None
            or record.completed_at.tzinfo is None
            or record.context_expires_at.tzinfo is None
            or record.started_at < admission.admitted_at
            or record.completed_at < record.started_at
            or record.completed_at > report.generated_at
            or record.started_at > record.context_expires_at
            or record.context_expires_at - record.started_at
            > timedelta(seconds=_CONTEXT_MAX_DURATION_SECONDS)
            or (
                record.status.value == "completed"
                and record.completed_at > record.context_expires_at
            )
            or (previous_completed_at is not None and record.started_at < previous_completed_at)
        ):
            _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.agent_execution_records")
        expected_context = _expected_agent_context_fields(
            admission=cast(Any, admission),
            report=report,
            record=record,
            assignment=assignment,
            assignment_template=template,
            assignment_is_authoritative=True,
            registered_tools=registered_tools,
        )
        if any(getattr(record, name) != value for name, value in expected_context.items()):
            _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.agent_execution_records")
        expected_assignment = template.model_copy(
            update={
                "assignment_id": assignment.assignment_id,
                "context_keys": sorted(
                    context_payload(
                        build_agent_context(admission.case, record.agent_role, registered_tools)
                    )
                ),
            },
            deep=True,
        )
        if assignment != AgentAssignment.model_validate(
            expected_assignment.model_dump(mode="python")
        ):
            _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "assignments.json")
        if (
            agent_report.agent_role != assignment.agent_role
            or agent_report.task != assignment.task
            or agent_report.conclusions
            or agent_report.claims
            or agent_report.assumptions
            or agent_report.evidence_ids
            or agent_report.tool_result_ids
            or agent_report.formulas
            or agent_report.objections
            or agent_report.falsification_conditions
            or (
                record.status.value == "completed"
                and agent_report.confidence is not ConfidenceGrade.C
            )
            or (
                record.status.value != "completed"
                and agent_report.confidence is not ConfidenceGrade.D
            )
        ):
            _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "agent_reports")
        support.append(
            BoundedAgentSupportV1(
                execution_id=record.execution_id,
                agent_role=record.agent_role,
                provider="local",
                provider_version="1.0.0",
                status=record.status.value,
                record_sha256=_domain_sha256(
                    ":agent-record",
                    {
                        "execution_record": record.model_dump(mode="json"),
                        "agent_report": agent_report.model_dump(mode="json"),
                    },
                ),
            )
        )
        seen_execution_ids.add(record.execution_id)
        seen_context_ids.add(record.context_id)
        seen_attempt_ids.add(record.context_attempt_id)
        previous_completed_at = record.completed_at
        if report.run_status == "completed" and record.status.value != "completed":
            _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.agent_execution_records")
        if index >= len(templates):
            _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.agent_execution_records")
    return tuple(support)


def _build_provenance(
    admission: BoundedNaturalLanguageAdmission,
    report: FinalReport,
    *,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
    storage_root: Path | str | None = None,
    storage_root_sha256: str | None = None,
    storage_revision: int,
    storage_transaction_id: str,
) -> BoundedNaturalLanguageProvenanceV1:
    verified = _admit_bounded_at(
        admission.source_bytes,
        admission.candidate,
        admission.confirmation,
        admitted_at=admission.admitted_at,
    )
    if verified != admission or report.run_id != admission.confirmation.run_id:
        _fail(BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING, "admission")
    if report.reconstructed_input != admission.case.model_dump(mode="json"):
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.reconstructed_input")
    report_metadata = report.reconstructed_input.get("metadata")
    expected_marker = admission.case.metadata.get("bounded_natural_language_review")
    if (
        not isinstance(report_metadata, dict)
        or report_metadata.get("bounded_natural_language_review") != expected_marker
        or admission.case.raw_text is not None
        or report.claim_assessments != admission.case.claims
        or report.generated_at.tzinfo is None
        or report.generated_at < admission.admitted_at
        or report.generated_at > admission.confirmation.expires_at
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report")
    expected_tools = _expected_tool_results(admission)
    expected_names = list(expected_tools)
    actual_names = [item.tool_name for item in report.tool_results]
    if (report.run_status == "completed" and actual_names != expected_names) or (
        report.run_status == "failed_with_limitations"
        and actual_names != expected_names[: len(actual_names)]
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.TOOL, "report.tool_results")
    _validate_confirmed_report_projection(
        report,
        expected_agent_count=len(select_roles(admission.case)),
        expected_tool_names=expected_names,
        storage_root=storage_root,
        storage_revision=storage_revision,
        storage_transaction_id=storage_transaction_id,
        require_storage_authority=storage_root is not None,
    )
    if len({item.result_id for item in report.tool_results}) != len(report.tool_results):
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.tool_results")
    for result in report.tool_results:
        expected = expected_tools.get(result.tool_name)
        if (
            expected is None
            or result.status is not ToolStatus.SUCCESS
            or result.duration_seconds > _TOOL_MAX_DURATION_SECONDS
            or result.created_at.tzinfo is None
            or result.created_at < admission.admitted_at
            or result.created_at > report.generated_at
            or _tool_result_semantic_projection(result)
            != _tool_result_semantic_projection(expected)
        ):
            _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.tool_results")
    validators = [item for item in report.tool_results if item.tool_name == "hand_validator"]
    if report.tool_results and (
        len(validators) != 1
        or validators[0].status is not ToolStatus.SUCCESS
        or validators[0].output.get("valid") is not True
    ):
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "report.hand_validator")
    agent_support = _validate_assignments_and_agents(admission, report, assignments, agent_reports)
    if storage_root is not None and storage_root_sha256 is not None:
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "storage_authority.revision_root")
    if storage_root is not None:
        root_sha256 = bounded_terminal_revision_root_sha256(storage_root)
    elif storage_root_sha256 is not None and re.fullmatch(r"[0-9a-f]{64}", storage_root_sha256):
        root_sha256 = storage_root_sha256
    else:
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "storage_authority.revision_root")
    if storage_revision < 1 or re.fullmatch(r"txn-[0-9a-f]{32}", storage_transaction_id) is None:
        _fail(BoundedNaturalLanguageDiagnosticCode.REPORT, "storage_authority.revision")
    projection = admission.candidate.projection
    provisional = BoundedNaturalLanguageProvenanceV1(
        run_id=report.run_id,
        intake_id=projection.intake_id,
        admitted_at=admission.admitted_at,
        source_sha256=projection.source.content_sha256,
        candidate_sha256=admission.candidate.candidate_sha256,
        source_bindings_sha256=projection.source_bindings_sha256,
        focal_sha256=projection.focal_decision.focal_sha256,
        tool_plan_sha256=projection.tool_plan.tool_plan_sha256,
        extractor_sha256=projection.extractor_sha256,
        confirmation_sha256=admission.confirmation.confirmation_sha256,
        case_input_sha256=_domain_sha256(":case-input", admission.case.model_dump(mode="json")),
        assignments_sha256=_domain_sha256(
            ":assignments", [item.model_dump(mode="json") for item in assignments]
        ),
        agent_reports_sha256=_domain_sha256(
            ":agent-reports", [item.model_dump(mode="json") for item in agent_reports]
        ),
        final_report_sha256=_domain_sha256(":final-report", report.model_dump(mode="json")),
        terminal_revision_root_sha256=root_sha256,
        terminal_revision=storage_revision,
        terminal_transaction_id=storage_transaction_id,
        agent_support=agent_support,
        tool_support=tuple(_tool_support(item) for item in report.tool_results),
        terminal_status=cast(Literal["completed", "failed_with_limitations"], report.run_status),
        provenance_sha256="0" * 64,
    )
    return provisional.model_copy(
        update={"provenance_sha256": bounded_provenance_sha256(provisional)}
    )


def build_bounded_natural_language_provenance(
    admission: BoundedNaturalLanguageAdmission,
    report: FinalReport,
    *,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
    storage_root: Path | str,
    storage_revision: int,
    storage_transaction_id: str,
) -> BoundedNaturalLanguageProvenanceV1:
    """Build a provenance artifact bound to planned immutable storage."""

    return _build_provenance(
        admission,
        report,
        assignments=assignments,
        agent_reports=agent_reports,
        storage_root=storage_root,
        storage_revision=storage_revision,
        storage_transaction_id=storage_transaction_id,
    )


def verify_bounded_natural_language_provenance(
    *,
    source_bytes: bytes,
    candidate: Any,
    confirmation: Any,
    case: Any,
    report: FinalReport,
    provenance: BoundedNaturalLanguageProvenanceV1,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
    storage_root: Path | str,
    storage_revision: int,
    storage_transaction_id: str,
) -> None:
    """Replay every bounded source-to-terminal binding without a provider call."""

    provenance = _strict_provenance(provenance)
    admission = _admit_bounded_at(
        source_bytes, candidate, confirmation, admitted_at=provenance.admitted_at
    )
    if admission.case != case:
        _fail(BoundedNaturalLanguageDiagnosticCode.STORAGE, "input.json")
    expected = build_bounded_natural_language_provenance(
        admission,
        report,
        assignments=assignments,
        agent_reports=agent_reports,
        storage_root=storage_root,
        storage_revision=storage_revision,
        storage_transaction_id=storage_transaction_id,
    )
    if provenance != expected:
        _fail(BoundedNaturalLanguageDiagnosticCode.STORAGE, "bounded_nl_provenance.json")


def verify_bounded_natural_language_structural_provenance(
    *,
    source_bytes: bytes,
    candidate: Any,
    confirmation: Any,
    case: Any,
    report: FinalReport,
    provenance: BoundedNaturalLanguageProvenanceV1,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
) -> None:
    """Replay a nonterminal buffer without making its path authoritative."""

    provenance = _strict_provenance(provenance)
    admission = _admit_bounded_at(
        source_bytes, candidate, confirmation, admitted_at=provenance.admitted_at
    )
    if admission.case != case:
        _fail(BoundedNaturalLanguageDiagnosticCode.STORAGE, "input.json")
    expected = _build_provenance(
        admission,
        report,
        assignments=assignments,
        agent_reports=agent_reports,
        storage_root_sha256=provenance.terminal_revision_root_sha256,
        storage_revision=provenance.terminal_revision,
        storage_transaction_id=provenance.terminal_transaction_id,
    )
    if provenance != expected:
        _fail(BoundedNaturalLanguageDiagnosticCode.STORAGE, "bounded_nl_provenance.json")
