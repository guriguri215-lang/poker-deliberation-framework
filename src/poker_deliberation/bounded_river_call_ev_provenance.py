"""Terminal provenance and context replay for the P3-030C product slice."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast

from pydantic import BaseModel

from poker_deliberation.agents import select_roles
from poker_deliberation.bounded_river_call_ev import (
    BoundedRiverCallEvAdmission,
    BoundedRiverCallEvError,
    _admit_at,
    bounded_river_call_ev_report_projection,
    bounded_river_terminal_revision_root_sha256,
    verify_bounded_river_call_ev_tool_chain,
)
from poker_deliberation.bounded_river_call_ev_models import (
    BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
    PROVENANCE_HASH_DOMAIN,
    BoundedRiverCallEvDiagnosticCode,
    BoundedRiverCallEvProvenanceV1,
    BoundedRiverCallEvResultV1,
)
from poker_deliberation.confirmed_review import (
    _expected_agent_context_fields,
    _validate_reproduction_steps,
)
from poker_deliberation.context_lifecycle import context_payload
from poker_deliberation.phases.services import build_agent_context
from poker_deliberation.providers.local import (
    LOCAL_PROVIDER_MODEL_SUPPORT_QUESTION,
    LOCAL_PROVIDER_UNCERTAINTY,
)
from poker_deliberation.range_equity_models import canonical_domain_sha256
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentReport,
    ConfidenceGrade,
    EpistemicLabel,
    FinalReport,
    ToolStatus,
)
from poker_deliberation.security import redact_sensitive
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from poker_deliberation.tools import default_registry

_ASSIGNMENT_ID = re.compile(r"assignment-[0-9a-f]{12}")
_EXECUTION_ID = re.compile(r"execution-[0-9a-f]{24}")
_CONTEXT_ID = re.compile(r"context-[0-9a-f]{24}")
_ATTEMPT_ID = re.compile(r"attempt-[0-9a-f]{24}")
_CONTEXT_MAX_DURATION_SECONDS = 30.0
_TOOL_MAX_DURATION_SECONDS = 30.0
_SOLVER_LIMITATION = "外部ソルバーの実行・収束確認なしにGTOまたは均衡を主張していません。"
_FAILED_CONCLUSION = "実行予算または安全上の制限に達したため、制限付きで終了しました。"
_FAILED_AGENT_UNCERTAINTIES = frozenset(
    {
        "Provider execution was refused by the strict budget policy.",
        "Provider deadline was exceeded; no output was accepted.",
        "Context handoff was refused by policy.",
        "Context validation failed; no provider output was accepted.",
        "Provider failed; no specialist conclusion was accepted.",
        "Provider report identity became unsafe after redaction.",
        "Oversized provider output was rejected.",
        "Provider context expired before its output could be accepted.",
    }
)


def _fail(code: BoundedRiverCallEvDiagnosticCode, field_path: str) -> NoReturn:
    raise BoundedRiverCallEvError(code, field_path)


def _hash(domain: str, value: object) -> str:
    canonical_value = json.loads(canonical_json_bytes(value).decode("utf-8"))
    return canonical_domain_sha256(domain, canonical_value)


def _json_hash(suffix: str, value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        value = [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item for item in value
        ]
    return _hash(f"poker-bounded-river-call-ev-{suffix}-v1", value)


def _validate_assignments_and_contexts(
    admission: BoundedRiverCallEvAdmission,
    report: FinalReport,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
) -> None:
    templates = tuple(select_roles(admission.case))
    expected_roles = [item.agent_role for item in templates]
    actual_roles = [item.agent_role for item in report.agent_execution_records]
    reports = tuple(agent_reports)
    if (
        [item.agent_role for item in assignments] != expected_roles
        or len(assignments) != len(templates)
        or len({item.assignment_id for item in assignments}) != len(assignments)
        or (report.run_status == "completed" and actual_roles != expected_roles)
        or (
            report.run_status == "failed_with_limitations"
            and actual_roles != expected_roles[: len(actual_roles)]
        )
        or len(reports) != len(actual_roles)
        or [item.agent_role for item in reports] != actual_roles
        or len({item.report_id for item in reports}) != len(reports)
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "assignments.json")
    records = report.agent_execution_records
    registered_tools = frozenset(default_registry().names())
    for index, (assignment, template) in enumerate(zip(assignments, templates, strict=True)):
        context_keys = (
            sorted(
                context_payload(
                    build_agent_context(admission.case, template.agent_role, registered_tools)
                )
            )
            if index < len(records)
            else []
        )
        expected_assignment = AgentAssignment.model_validate(
            template.model_copy(
                update={
                    "assignment_id": assignment.assignment_id,
                    "context_keys": context_keys,
                },
                deep=True,
            ).model_dump(mode="python")
        )
        if (
            _ASSIGNMENT_ID.fullmatch(assignment.assignment_id) is None
            or assignment != expected_assignment
        ):
            _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "assignments.json")
    seen_execution_ids: set[str] = set()
    seen_context_ids: set[str] = set()
    seen_attempt_ids: set[str] = set()
    previous_completed_at = None
    for index, (record, agent_report) in enumerate(zip(records, reports, strict=True)):
        assignment = assignments[index]
        template = templates[index]
        expected_tools = (
            list(BOUNDED_RIVER_CALL_EV_TOOL_ORDER) if record.agent_role == "math-auditor" else []
        )
        if (
            record.assignment_id != assignment.assignment_id
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
            or record.allowed_tools != expected_tools
            or record.context_schema_version != "1.0.0"
            or record.context_classification != "internal"
            or record.context_producer_runtime != "python-local"
            or record.context_consumer_runtime != "python-local"
            or record.context_expires_at is None
            or record.started_at.tzinfo is None
            or record.completed_at.tzinfo is None
            or record.context_expires_at.tzinfo is None
            or record.started_at < admission.admitted_at
            or record.completed_at < record.started_at
            or record.started_at > record.context_expires_at
            or record.completed_at > report.generated_at
            or (
                record.status.value == "completed"
                and record.completed_at > record.context_expires_at
            )
            or record.context_expires_at - record.started_at
            > timedelta(seconds=_CONTEXT_MAX_DURATION_SECONDS)
            or (previous_completed_at is not None and record.started_at < previous_completed_at)
        ):
            _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "report.agent_execution_records")
        expected_context = _expected_agent_context_fields(
            admission=cast(Any, admission),
            report=report,
            record=record,
            assignment=assignment,
            assignment_template=template,
            assignment_is_authoritative=True,
            registered_tools=registered_tools,
        )
        if any(getattr(record, name) != expected for name, expected in expected_context.items()):
            _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "report.agent_execution_records")
        context = build_agent_context(admission.case, record.agent_role, registered_tools)
        if context.raw_text is not None or "raw_text" in context_payload(context):
            _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "agent_context.raw_text")
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
                and agent_report.uncertainties != [LOCAL_PROVIDER_UNCERTAINTY]
            )
            or (
                record.status.value == "completed"
                and agent_report.unresolved_questions != [LOCAL_PROVIDER_MODEL_SUPPORT_QUESTION]
            )
            or (
                record.status.value != "completed"
                and (
                    len(agent_report.uncertainties) != 1
                    or agent_report.uncertainties[0] not in _FAILED_AGENT_UNCERTAINTIES
                    or agent_report.unresolved_questions
                )
            )
            or (
                record.status.value == "completed"
                and agent_report.confidence is not ConfidenceGrade.C
            )
            or (
                record.status.value != "completed"
                and agent_report.confidence is not ConfidenceGrade.D
            )
            or ((record.status.value == "completed") != (record.error is None))
            or (
                record.error is not None
                and (not record.error.strip() or redact_sensitive(record.error) != record.error)
            )
        ):
            _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "agent_reports")
        seen_execution_ids.add(record.execution_id)
        seen_context_ids.add(record.context_id)
        seen_attempt_ids.add(record.context_attempt_id)
        previous_completed_at = record.completed_at


def _validate_report_semantics(
    admission: BoundedRiverCallEvAdmission,
    result: BoundedRiverCallEvResultV1 | None,
    report: FinalReport,
    *,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
    storage_root: Path | str,
    storage_revision: int,
    storage_transaction_id: str,
) -> None:
    if (
        report.run_id != admission.confirmation.run_id
        or report.run_status not in {"completed", "failed_with_limitations"}
        or report.reconstructed_input != admission.case.model_dump(mode="json")
        or admission.case.raw_text is not None
        or report.generated_at.tzinfo is None
        or report.generated_at.utcoffset() is None
        or report.generated_at < admission.admitted_at
        or report.generated_at > admission.confirmation.expires_at
        or report.data_quality != list(dict.fromkeys(report.data_quality))
        or report.limitations != list(dict.fromkeys(report.limitations))
        or report.sensitivity
        or report.disputes
        or report.evidence
        or report.approvals
        or report.security_events
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report")
    metadata = report.reconstructed_input.get("metadata")
    if not isinstance(metadata, dict) or metadata.get(
        "bounded_river_call_ev"
    ) != admission.case.metadata.get("bounded_river_call_ev"):
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report.reconstructed_input")
    expected_sections = [
        {
            "title": item.agent_role,
            "epistemic_status": EpistemicLabel.UNKNOWN.value,
            "unverified_conclusions": item.conclusions,
            "unverified_claims": [claim.text for claim in item.claims],
            "uncertainties": item.uncertainties,
            "objections": item.objections,
            "unresolved_questions": item.unresolved_questions,
        }
        for item in agent_reports
    ]
    if report.analysis_sections != expected_sections:
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report.analysis_sections")
    expected_result = verify_bounded_river_call_ev_tool_chain(
        admission,
        report.tool_results,
        run_status=report.run_status,
    )
    if expected_result != result:
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report.tool_results")
    if len({item.result_id for item in report.tool_results}) != len(report.tool_results):
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report.tool_results")
    if any(
        item.duration_seconds > _TOOL_MAX_DURATION_SECONDS
        or item.created_at.tzinfo is None
        or item.created_at.utcoffset() is None
        or item.created_at < admission.admitted_at
        or item.created_at > report.generated_at
        for item in report.tool_results
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report.tool_results")
    if report.run_status == "completed":
        if result is None:
            _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "bounded_river_call_ev_result.json")
        conclusion, claim, alternatives, added_limitations = (
            bounded_river_call_ev_report_projection(result)
        )
        expected_claims = [*admission.case.claims, claim]
        expected_limitations = list(
            dict.fromkeys([*report.data_quality, _SOLVER_LIMITATION, *added_limitations])
        )
        if (
            report.conclusion != conclusion
            or report.confidence is not ConfidenceGrade.A
            or report.claim_assessments != expected_claims
            or report.alternatives != alternatives
            or report.limitations != expected_limitations
            or any(item.status is not ToolStatus.SUCCESS for item in report.tool_results)
        ):
            _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report.conclusion")
    else:
        expected_limitations = list(dict.fromkeys([*report.data_quality, _SOLVER_LIMITATION]))
        if (
            report.conclusion != _FAILED_CONCLUSION
            or report.confidence is not ConfidenceGrade.C
            or report.claim_assessments != admission.case.claims
            or report.alternatives
            or report.limitations != expected_limitations
        ):
            _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report.conclusion")
    try:
        _validate_reproduction_steps(
            report,
            storage_root=storage_root,
            storage_revision=storage_revision,
            storage_transaction_id=storage_transaction_id,
            require_storage_authority=True,
        )
    except ValueError as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.REPLAY,
            "report.reproduction_steps",
        ) from exc
    _validate_assignments_and_contexts(admission, report, assignments, agent_reports)


def build_bounded_river_call_ev_provenance(
    admission: BoundedRiverCallEvAdmission,
    result: BoundedRiverCallEvResultV1,
    report: FinalReport,
    *,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
    storage_root: Path | str,
    storage_revision: int,
    storage_transaction_id: str,
) -> BoundedRiverCallEvProvenanceV1:
    replay = _admit_at(
        admission.source_bytes,
        admission.candidate,
        admission.confirmation,
        admitted_at=admission.admitted_at,
    )
    if replay != admission or report.run_id != admission.confirmation.run_id:
        _fail(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING, "admission")
    _validate_report_semantics(
        admission,
        result,
        report,
        assignments=assignments,
        agent_reports=agent_reports,
        storage_root=storage_root,
        storage_revision=storage_revision,
        storage_transaction_id=storage_transaction_id,
    )
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "poker-bounded-river-call-ev",
        "result_version": "1.0.0",
        "run_id": report.run_id,
        "intake_id": admission.candidate.projection.intake_id,
        "admitted_at": admission.admitted_at,
        "source_sha256": admission.candidate.projection.source_sha256,
        "candidate_sha256": admission.candidate.candidate_sha256,
        "confirmation_sha256": admission.confirmation.confirmation_sha256,
        "binding_sha256": admission.binding.binding_sha256,
        "range_definition_sha256": admission.candidate.projection.range_definition_sha256,
        "range_equity_binding_sha256": (admission.range_equity_admission.binding.binding_sha256),
        "result_sha256": result.result_sha256,
        "case_input_sha256": _json_hash("case-input-json", admission.case),
        "assignments_sha256": _json_hash("assignments-json", assignments),
        "agent_reports_sha256": _json_hash("agent-reports-json", agent_reports),
        "execution_records_sha256": _json_hash(
            "execution-records-json",
            report.agent_execution_records,
        ),
        "final_report_sha256": _json_hash("final-report-json", report),
        "terminal_revision_root_sha256": bounded_river_terminal_revision_root_sha256(storage_root),
        "terminal_revision": storage_revision,
        "terminal_transaction_id": storage_transaction_id,
        "terminal_status": report.run_status,
    }
    return BoundedRiverCallEvProvenanceV1.model_validate(
        {
            **payload,
            "provenance_sha256": _hash(PROVENANCE_HASH_DOMAIN, payload),
        },
        strict=True,
    )


def verify_bounded_river_call_ev_provenance(
    *,
    source_bytes: bytes,
    candidate: Any,
    confirmation: Any,
    case: Any,
    result: BoundedRiverCallEvResultV1,
    report: FinalReport,
    provenance: BoundedRiverCallEvProvenanceV1,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
    storage_root: Path | str,
    storage_revision: int,
    storage_transaction_id: str,
) -> None:
    admission = _admit_at(
        source_bytes,
        candidate,
        confirmation,
        admitted_at=provenance.admitted_at,
    )
    if admission.case != case:
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "input.json")
    verify_bounded_river_call_ev_tool_chain(
        admission,
        report.tool_results,
        run_status=report.run_status,
    )
    expected = build_bounded_river_call_ev_provenance(
        admission,
        result,
        report,
        assignments=assignments,
        agent_reports=agent_reports,
        storage_root=storage_root,
        storage_revision=storage_revision,
        storage_transaction_id=storage_transaction_id,
    )
    if expected != provenance:
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "bounded_river_call_ev_provenance.json")


def verify_bounded_river_call_ev_structural_provenance(
    *,
    source_bytes: bytes,
    candidate: Any,
    confirmation: Any,
    case: Any,
    result: BoundedRiverCallEvResultV1 | None,
    report: FinalReport,
    admitted_at: datetime,
    assignments: Sequence[AgentAssignment],
    agent_reports: Sequence[AgentReport],
    storage_root: Path | str,
    storage_revision: int,
    storage_transaction_id: str,
) -> None:
    admission = _admit_at(
        source_bytes,
        candidate,
        confirmation,
        admitted_at=admitted_at,
    )
    if admission.case != case:
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "input.json")
    _validate_report_semantics(
        admission,
        result,
        report,
        assignments=assignments,
        agent_reports=agent_reports,
        storage_root=storage_root,
        storage_revision=storage_revision,
        storage_transaction_id=storage_transaction_id,
    )


__all__ = [
    "build_bounded_river_call_ev_provenance",
    "verify_bounded_river_call_ev_provenance",
    "verify_bounded_river_call_ev_structural_provenance",
]
