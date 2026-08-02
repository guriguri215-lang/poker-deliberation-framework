"""Terminal provenance and context replay for the P3-030C product slice."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, NoReturn, cast

from pydantic import BaseModel

from poker_deliberation.agents import select_roles
from poker_deliberation.bounded_river_call_ev import (
    BoundedRiverCallEvAdmission,
    BoundedRiverCallEvError,
    _admit_at,
    bounded_river_terminal_revision_root_sha256,
    build_bounded_river_call_ev_result,
    verify_bounded_river_call_ev_tool_chain,
)
from poker_deliberation.bounded_river_call_ev_models import (
    BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
    PROVENANCE_HASH_DOMAIN,
    BoundedRiverCallEvDiagnosticCode,
    BoundedRiverCallEvProvenanceV1,
    BoundedRiverCallEvResultV1,
)
from poker_deliberation.confirmed_review import _expected_agent_context_fields
from poker_deliberation.context_lifecycle import context_payload
from poker_deliberation.phases.services import build_agent_context
from poker_deliberation.range_equity_models import canonical_domain_sha256
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentReport,
    FinalReport,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from poker_deliberation.tools import default_registry

_ASSIGNMENT_ID = re.compile(r"assignment-[0-9a-f]{12}")
_EXECUTION_ID = re.compile(r"execution-[0-9a-f]{24}")
_CONTEXT_ID = re.compile(r"context-[0-9a-f]{24}")
_ATTEMPT_ID = re.compile(r"attempt-[0-9a-f]{24}")
_CONTEXT_MAX_DURATION_SECONDS = 30.0


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
    if (
        [item.agent_role for item in assignments] != expected_roles
        or len(assignments) != len(templates)
        or len({item.assignment_id for item in assignments}) != len(assignments)
        or any(
            _ASSIGNMENT_ID.fullmatch(item.assignment_id) is None
            or item.agent_role != template.agent_role
            or item.task != template.task
            or item.read_only is not True
            for item, template in zip(assignments, templates, strict=True)
        )
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "assignments.json")
    records = report.agent_execution_records
    if [item.agent_role for item in records] != expected_roles:
        _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "report.agent_execution_records")
    if [item.agent_role for item in agent_reports] != expected_roles:
        _fail(BoundedRiverCallEvDiagnosticCode.CONTEXT, "agent_reports")
    assignment_by_role = {item.agent_role: item for item in assignments}
    template_by_role = {item.agent_role: item for item in templates}
    registered_tools = frozenset(default_registry().names())
    seen_execution_ids: set[str] = set()
    seen_context_ids: set[str] = set()
    seen_attempt_ids: set[str] = set()
    previous_completed_at = None
    for record in records:
        assignment = assignment_by_role[record.agent_role]
        template = template_by_role[record.agent_role]
        expected_tools = (
            list(BOUNDED_RIVER_CALL_EV_TOOL_ORDER) if record.agent_role == "math-auditor" else []
        )
        if (
            record.execution_id in seen_execution_ids
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
        seen_execution_ids.add(record.execution_id)
        seen_context_ids.add(record.context_id)
        seen_attempt_ids.add(record.context_attempt_id)
        previous_completed_at = record.completed_at


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
    if report.reconstructed_input != admission.case.model_dump(mode="json"):
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "report.reconstructed_input")
    expected_result = build_bounded_river_call_ev_result(admission, report.tool_results)
    if expected_result != result:
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "bounded_river_call_ev_result.json")
    _validate_assignments_and_contexts(admission, report, assignments, agent_reports)
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


def verify_bounded_river_call_ev_structural_provenance(**kwargs: Any) -> None:
    verify_bounded_river_call_ev_provenance(**kwargs)


__all__ = [
    "build_bounded_river_call_ev_provenance",
    "verify_bounded_river_call_ev_provenance",
    "verify_bounded_river_call_ev_structural_provenance",
]
