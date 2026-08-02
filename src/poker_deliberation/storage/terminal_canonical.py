"""Canonical bytes, hashes, version dispatch, and inventory checks for P2-012B."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from poker_deliberation.approval_canonical import (
    parse_canonical_jsonl as parse_approval_jsonl,
)
from poker_deliberation.approval_canonical import (
    parse_canonical_model as parse_approval_model,
)
from poker_deliberation.approval_models import (
    ApprovalDecisionRecordV2,
    ApprovalDomainAuditEventV2,
    ApprovalLedgerV2,
    ApprovalReissueRecordV2,
)
from poker_deliberation.approvals import (
    project_v1_approvals,
    read_approval_state_v2,
)
from poker_deliberation.bounded_natural_language_models import (
    BoundedIntakeCandidateV1,
    BoundedIntakeConfirmationV1,
    BoundedNaturalLanguageProvenanceV1,
)
from poker_deliberation.bounded_river_call_ev_models import (
    BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_MARKER,
    BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT,
    BoundedRiverCallEvBindingV1,
    BoundedRiverCallEvBudgetFailureEvidenceV1,
    BoundedRiverCallEvCandidateV1,
    BoundedRiverCallEvConfirmationV1,
    BoundedRiverCallEvProvenanceV1,
    BoundedRiverCallEvResultV1,
)
from poker_deliberation.budgets.contracts import BudgetPolicyV2
from poker_deliberation.confirmed_review_models import (
    ConfirmedReviewProvenanceV1,
    ReviewIntakeCandidateV1,
    ReviewIntakeConfirmationV1,
)
from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
    LifecycleAuditMetadata,
)
from poker_deliberation.normalization import (
    NORMALIZATION_CONTRACT_VERSION,
    NORMALIZATION_RESULT_VERSION,
    NormalizationResultV1,
    normalization_result_json_bytes,
    verify_normalization_binding,
)
from poker_deliberation.range_equity import (
    verify_versioned_range_river_equity_binding_artifact,
    verify_versioned_range_river_equity_case_correlation,
    verify_versioned_range_river_equity_tool_chain,
)
from poker_deliberation.range_equity_models import (
    RANGE_EQUITY_BINDING_ARTIFACT,
    VersionedRangeRiverEquityBindingV1,
)
from poker_deliberation.range_grammar import verify_versioned_range_tool_chain
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionRecord,
    AgentReport,
    ApprovalRequest,
    Assumption,
    CaseInput,
    Dispute,
    EvidenceRecord,
    FinalReport,
    SecurityEvent,
    ToolResult,
)
from poker_deliberation.state_machine import ALLOWED_TRANSITIONS, RunState
from poker_deliberation.storage.bounded_river_call_ev_admission_store import (
    read_bounded_river_call_ev_admission_record,
    verify_bounded_river_call_ev_admission_record,
)
from poker_deliberation.storage.bounded_river_call_ev_failure_store import (
    read_bounded_river_call_ev_budget_failure_evidence,
    verify_bounded_river_call_ev_budget_failure_evidence,
)
from poker_deliberation.storage.range_equity_admission_store import (
    read_range_equity_admission_record,
    verify_range_equity_admission_record,
)
from poker_deliberation.storage.revision_canonical import (
    CONTROL_CANONICALIZATION,
    JSONL_SERIALIZATION,
    TEXT_SERIALIZATION,
    CanonicalStorageError,
    artifact_table_entry,
    canonical_domain_sha256,
    canonical_json_bytes,
    canonicalize_bindings,
    classification_evidence_sha256,
    domain_sha256,
    parse_canonical_json,
    parse_canonical_jsonl,
    parse_canonical_model,
    sha256_bytes,
    upstream_source_sha256,
    validate_assignment_execution_correlation,
    validate_canonical_text,
    validate_logical_name,
)
from poker_deliberation.storage.revision_models import (
    LocalDataBindingV1,
    PayloadInventoryEntryV1,
    ProvenanceBindingV1,
)
from poker_deliberation.storage.terminal_models import (
    TERMINAL_SCHEMA_VERSION,
    BudgetSettlementBindingV2,
    CompletionMarkerV2,
    RunCurrentPointerV2,
    RunManifestV2,
)

TERMINAL_INVENTORY_DOMAIN = "poker-run-terminal-inventory-v2"
REQUIRED_INVENTORY_DOMAIN = "poker-run-required-inventory-v2"
BUDGET_BINDING_DOMAIN = "poker-run-budget-settlement-binding-v2"
EMPTY_HEAD_DOMAIN = "poker-run-terminal-empty-head-v2"
LIFECYCLE_AUDIT_DOMAIN = "poker-run-lifecycle-audit-v2"
LEGACY_SOURCE_INVENTORY_DOMAIN = "poker-run-legacy-source-inventory-v2"
PRODUCT_LINEAGE_DOMAIN_PREFIX = "poker-product"
APPROVAL_AUTHORITY_LINEAGE_DOMAIN = "poker-product-approval-authority-lineage-v2"
APPROVAL_V2_CORE_ARTIFACTS = frozenset(
    {
        "approval_ledger_v2.json",
        "approval_decisions_v2.jsonl",
        "approval_audit_v2.jsonl",
    }
)
APPROVAL_REISSUE_ARTIFACT = "approval_reissues_v2.jsonl"
APPROVAL_V2_ARTIFACTS = APPROVAL_V2_CORE_ARTIFACTS | {APPROVAL_REISSUE_ARTIFACT}
_CONFIRMED_REVIEW_ARTIFACTS = frozenset(
    {
        "confirmed_review_source.txt",
        "confirmed_review_candidate.json",
        "confirmed_review_confirmation.json",
        "confirmed_review_provenance.json",
    }
)
_BOUNDED_NL_ARTIFACTS = frozenset(
    {
        "bounded_nl_source.txt",
        "bounded_nl_candidate.json",
        "bounded_nl_confirmation.json",
        "bounded_nl_provenance.json",
    }
)
_BOUNDED_RIVER_CALL_EV_BASE_ARTIFACTS = frozenset(
    {
        BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT,
    }
)
_BOUNDED_RIVER_CALL_EV_TERMINAL_ARTIFACTS = frozenset(
    {
        BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
        BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT,
    }
)
_BOUNDED_RIVER_CALL_EV_ARTIFACTS = (
    _BOUNDED_RIVER_CALL_EV_BASE_ARTIFACTS | _BOUNDED_RIVER_CALL_EV_TERMINAL_ARTIFACTS
)
_AGENT_REPORT_ARTIFACT = re.compile(
    r"^agent_reports/(?P<identifier>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$"
)
_TERMINAL_ONLY_ARTIFACT_TABLE: dict[str, tuple[str, str, str]] = {
    "state.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-workflow-state-artifact-v1",
    ),
    "lifecycle_audit.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-lifecycle-audit-artifact-v1",
    ),
    "approval_ledger_v2.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-approval-ledger-artifact-v2",
    ),
    "approval_decisions_v2.jsonl": (
        "application/x-ndjson",
        JSONL_SERIALIZATION,
        "poker-approval-decision-log-artifact-v2",
    ),
    "approval_audit_v2.jsonl": (
        "application/x-ndjson",
        JSONL_SERIALIZATION,
        "poker-approval-domain-audit-log-artifact-v2",
    ),
    APPROVAL_REISSUE_ARTIFACT: (
        "application/x-ndjson",
        JSONL_SERIALIZATION,
        "poker-approval-reissue-log-artifact-v2",
    ),
}

_SUPPORTED_VERSION = TERMINAL_SCHEMA_VERSION
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}$")
T = TypeVar("T", bound=BaseModel)


class UnsupportedTerminalVersion(CanonicalStorageError):
    """A syntactically valid terminal control object uses an unknown version."""


def canonical_terminal_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value)


def _version(data: bytes, field: str) -> str:
    value = parse_canonical_json(data)
    if not isinstance(value, dict):
        raise CanonicalStorageError("terminal control value must be an object")
    version = value.get(field)
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise CanonicalStorageError("terminal control version is invalid")
    if version != _SUPPORTED_VERSION:
        raise UnsupportedTerminalVersion("unsupported terminal storage version")
    return version


def _parse_versioned(data: bytes, model: type[T], *, version_field: str) -> T:
    _version(data, version_field)
    try:
        return parse_canonical_model(data, model)
    except (CanonicalStorageError, ValidationError) as exc:
        raise CanonicalStorageError("terminal control schema is invalid") from exc


def parse_run_manifest(data: bytes) -> RunManifestV2:
    return _parse_versioned(data, RunManifestV2, version_field="run_schema_version")


def parse_completion_marker(data: bytes) -> CompletionMarkerV2:
    return _parse_versioned(data, CompletionMarkerV2, version_field="schema_version")


def parse_current_pointer(data: bytes) -> RunCurrentPointerV2:
    return _parse_versioned(data, RunCurrentPointerV2, version_field="schema_version")


def manifest_sha256(manifest: RunManifestV2 | bytes) -> str:
    data = manifest if isinstance(manifest, bytes) else canonical_terminal_bytes(manifest)
    return sha256_bytes(data)


def completion_marker_sha256(marker: CompletionMarkerV2 | bytes) -> str:
    data = marker if isinstance(marker, bytes) else canonical_terminal_bytes(marker)
    return sha256_bytes(data)


def current_pointer_sha256(pointer: RunCurrentPointerV2 | bytes) -> str:
    data = pointer if isinstance(pointer, bytes) else canonical_terminal_bytes(pointer)
    return sha256_bytes(data)


def budget_binding_sha256(binding: BudgetSettlementBindingV2) -> str:
    return canonical_domain_sha256(BUDGET_BINDING_DOMAIN, binding)


def lifecycle_audit_sha256(data: bytes) -> str:
    return domain_sha256(LIFECYCLE_AUDIT_DOMAIN, data)


def empty_lineage_head_sha256(kind: str) -> str:
    try:
        encoded = kind.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CanonicalStorageError("lineage head kind must be ASCII") from exc
    return domain_sha256(EMPTY_HEAD_DOMAIN, encoded + b"\0" + b"[]")


def _lineage_head(kind: str, value: Sequence[object]) -> str:
    if not value:
        return empty_lineage_head_sha256(kind)
    return canonical_domain_sha256(
        f"{PRODUCT_LINEAGE_DOMAIN_PREFIX}-{kind}-lineage-v2",
        value,
    )


def _validate_state_event_chain(
    state: Mapping[str, object], events: list[dict[str, object]]
) -> None:
    current = RunState.INTAKE
    for event in events:
        if set(event) != {"source", "target", "reason"}:
            raise CanonicalStorageError("state checkpoint event shape mismatch")
        try:
            source = RunState(str(event["source"]))
            target = RunState(str(event["target"]))
        except ValueError as exc:
            raise CanonicalStorageError("state checkpoint event state mismatch") from exc
        reason = event["reason"]
        if (
            source is not current
            or target not in ALLOWED_TRANSITIONS[source]
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            raise CanonicalStorageError("state checkpoint event chain mismatch")
        current = target
    if state.get("state") != current.value:
        raise CanonicalStorageError("state checkpoint terminal event mismatch")


def product_payload_commitments(
    payloads: Mapping[str, bytes],
    *,
    run_id: str,
    status: str,
    revision: int | None = None,
    revision_root: Path | str | None = None,
    transaction_id: str | None = None,
    previous_manifest_sha256: str | None = None,
    previous_pointer_sha256: str | None = None,
    maximum_admission_record_bytes: int = 1_000_000,
    budget_policy: BudgetPolicyV2 | None = None,
) -> tuple[str, str, str, str, str, str]:
    """Recompute product input, checkpoint, and scalar lineage commitments."""

    required = {"input.json", "state.json", "final_report.json"}
    if not required <= set(payloads):
        raise CanonicalStorageError("product publication lacks a required core payload")
    input_case = parse_canonical_model(payloads["input.json"], CaseInput)
    normalized_case = input_case
    if "normalized_case.json" in payloads:
        normalized_case = parse_canonical_model(payloads["normalized_case.json"], CaseInput)
    if "normalization.json" in payloads:
        if "normalized_case.json" not in payloads:
            raise CanonicalStorageError("normalization payload lacks the normalized case artifact")
        normalization = _parse_normalization_result(payloads["normalization.json"])
        try:
            verify_normalization_binding(input_case, normalized_case, normalization)
        except ValueError as exc:
            raise CanonicalStorageError("normalization payload binding mismatch") from exc
    report = _parse_json_model(payloads["final_report.json"], FinalReport)
    state = parse_canonical_json(payloads["state.json"])
    if not isinstance(state, dict):
        raise CanonicalStorageError("state checkpoint must be an object")
    events = state.get("events")
    if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
        raise CanonicalStorageError("state checkpoint events must be an object list")
    _validate_state_event_chain(state, events)
    if report.run_id != run_id:
        raise CanonicalStorageError("final report run ID mismatch")

    expected_public_status = {
        "approval_required": "approval_required",
        "succeeded": "completed",
        "failed": "failed_with_limitations",
        "cancelled": "failed_with_limitations",
        "cancel_unconfirmed": "failed_with_limitations",
    }.get(status)
    expected_machine_state = {
        "approval_required": "HUMAN_REVIEW_REQUIRED",
        "succeeded": "COMPLETED",
        "failed": "FAILED_WITH_LIMITATIONS",
        "cancelled": "FAILED_WITH_LIMITATIONS",
        "cancel_unconfirmed": "FAILED_WITH_LIMITATIONS",
    }.get(status)
    if expected_public_status is not None and (
        report.run_status != expected_public_status or state.get("state") != expected_machine_state
    ):
        raise CanonicalStorageError("product state/report/status correlation mismatch")

    approvals = TypeAdapter(list[ApprovalRequest]).validate_json(
        payloads.get("approvals.json", b"[]")
    )
    present_approval_v2 = APPROVAL_V2_ARTIFACTS & set(payloads)
    approval_authority_head: str | None = None
    if present_approval_v2:
        if not present_approval_v2 >= APPROVAL_V2_CORE_ARTIFACTS:
            raise CanonicalStorageError("authoritative approval artifacts must be complete")
        try:
            approval_state = read_approval_state_v2(
                payloads["approval_ledger_v2.json"],
                payloads["approval_decisions_v2.jsonl"],
                payloads["approval_audit_v2.jsonl"],
                payloads.get(APPROVAL_REISSUE_ARTIFACT, b""),
            )
            projected = project_v1_approvals(approval_state)
        except ValueError as exc:
            raise CanonicalStorageError("authoritative approval artifacts are invalid") from exc
        if approval_state.ledger.run_id != run_id or projected != approvals:
            raise CanonicalStorageError(
                "V1 approval projection differs from authoritative V2 state"
            )
        if revision is not None:
            decisions = approval_state.decision_records
            reissues = approval_state.reissue_records
            checkpoint_revision = revision - len(decisions) - len(reissues)
            successor_creation_revisions = {
                result.successor_request_id: result.successor_created_run_revision
                for record in reissues
                for result in record.outcome.results
            }
            if any(
                request.created_run_revision
                != successor_creation_revisions.get(request.request_id, checkpoint_revision)
                for request in approval_state.ledger.requests
            ):
                raise CanonicalStorageError(
                    "approval request creation is not bound to its checkpoint revision"
                )
            transitions = [
                (
                    record.outcome.previous_run_revision,
                    record.outcome.current_run_revision,
                )
                for record in decisions
            ]
            transitions.extend(
                (
                    record.outcome.previous_run_revision,
                    record.outcome.current_run_revision,
                )
                for record in reissues
            )
            transitions.sort()
            if transitions and (
                transitions[0][0] != checkpoint_revision or transitions[-1][1] != revision
            ):
                raise CanonicalStorageError(
                    "approval mutation chain is not bound to the current revision"
                )
        current_reissue = next(
            (
                record
                for record in approval_state.reissue_records
                if revision is not None and record.outcome.current_run_revision == revision
            ),
            None,
        )
        if current_reissue is not None and (
            current_reissue.previous_manifest_sha256 != previous_manifest_sha256
            or current_reissue.previous_pointer_sha256 != previous_pointer_sha256
        ):
            raise CanonicalStorageError(
                "approval reissue is not bound to the previous terminal lineage"
            )
        authority_commitment = {
            "ledger_sha256": approval_state.ledger_sha256,
            "decision_count": approval_state.ledger.decision_count,
            "decision_log_head_sha256": approval_state.ledger.decision_log_head_sha256,
            "domain_audit_count": approval_state.ledger.domain_audit_count,
            "domain_audit_log_head_sha256": (approval_state.ledger.domain_audit_log_head_sha256),
        }
        if approval_state.reissue_records:
            authority_commitment.update(
                {
                    "reissue_count": len(approval_state.reissue_records),
                    "reissue_log_head_sha256": (approval_state.reissue_records[-1].record_sha256),
                }
            )
        approval_authority_head = canonical_domain_sha256(
            APPROVAL_AUTHORITY_LINEAGE_DOMAIN,
            authority_commitment,
        )
    assignments = TypeAdapter(list[AgentAssignment]).validate_json(
        payloads.get("assignments.json", b"[]")
    )
    execution_records = TypeAdapter(list[AgentExecutionRecord]).validate_json(
        payloads.get("agent_execution_records.json", b"[]")
    )
    validate_assignment_execution_correlation(assignments, execution_records)
    agent_reports: list[AgentReport] = []
    for logical_name in sorted(payloads, key=lambda item: item.encode("utf-8")):
        report_match = _AGENT_REPORT_ARTIFACT.fullmatch(logical_name)
        if report_match is None:
            continue
        agent_report = _parse_json_model(payloads[logical_name], AgentReport)
        if agent_report.report_id != report_match.group("identifier"):
            raise CanonicalStorageError("agent report ID does not match its path")
        agent_reports.append(agent_report)
    report_ids = [agent_report.report_id for agent_report in agent_reports]
    report_roles = [agent_report.agent_role for agent_report in agent_reports]
    if len(set(report_ids)) != len(report_ids) or len(set(report_roles)) != len(report_roles):
        raise CanonicalStorageError("agent report IDs and roles must be unique")
    security_events = TypeAdapter(list[SecurityEvent]).validate_json(
        payloads.get("security_events.json", b"[]")
    )
    disputes = TypeAdapter(list[Dispute]).validate_json(payloads.get("disputes.json", b"[]"))
    evidence = parse_canonical_jsonl(payloads.get("evidence.jsonl", b""), EvidenceRecord)
    tool_results = {
        logical_name.removeprefix("tool_results/").removesuffix(".json"): (
            _parse_json_model(data, ToolResult)
        )
        for logical_name, data in payloads.items()
        if logical_name.startswith("tool_results/")
        and logical_name.endswith(".json")
        and not logical_name.endswith(".input.json")
    }
    tool_inputs = {
        logical_name.removeprefix("tool_results/").removesuffix(".input.json")
        for logical_name in payloads
        if logical_name.startswith("tool_results/") and logical_name.endswith(".input.json")
    }
    if tool_inputs != set(tool_results):
        raise CanonicalStorageError("tool input/result inventory pairing mismatch")
    if report.approvals != approvals:
        raise CanonicalStorageError("final report approval payload correlation mismatch")
    if report.agent_execution_records != execution_records:
        raise CanonicalStorageError("final report execution payload correlation mismatch")
    if report.security_events != security_events:
        raise CanonicalStorageError("final report security payload correlation mismatch")
    if report.disputes != disputes:
        raise CanonicalStorageError("final report dispute payload correlation mismatch")
    if report.evidence != list(evidence):
        raise CanonicalStorageError("final report evidence payload correlation mismatch")
    if len(report.tool_results) != len(tool_results) or any(
        tool_results.get(item.result_id) != item for item in report.tool_results
    ):
        raise CanonicalStorageError("final report tool payload correlation mismatch")
    for result in report.tool_results:
        input_name = f"tool_results/{result.result_id}.input.json"
        if input_name not in payloads or parse_canonical_json(payloads[input_name]) != result.input:
            raise CanonicalStorageError("tool input/result correlation mismatch")
    report_metadata = report.reconstructed_input.get("metadata")
    bounded_river_input_marker_present = BOUNDED_RIVER_CALL_EV_MARKER in input_case.metadata
    bounded_river_report_marker_present = isinstance(report_metadata, dict) and (
        BOUNDED_RIVER_CALL_EV_MARKER in report_metadata
    )
    bounded_river_report_marker = (
        report_metadata.get(BOUNDED_RIVER_CALL_EV_MARKER)
        if isinstance(report_metadata, dict)
        else None
    )
    if bounded_river_input_marker_present != bounded_river_report_marker_present or (
        bounded_river_input_marker_present
        and bounded_river_report_marker != input_case.metadata[BOUNDED_RIVER_CALL_EV_MARKER]
    ):
        raise CanonicalStorageError(
            "bounded river call-EV input and report markers must match exactly"
        )
    bounded_river_marker = bounded_river_input_marker_present or bounded_river_report_marker_present
    try:
        verify_versioned_range_river_equity_case_correlation(
            input_case,
            normalized_case,
            report.reconstructed_input,
        )
        binding_artifact = (
            _parse_json_model(
                payloads[RANGE_EQUITY_BINDING_ARTIFACT],
                VersionedRangeRiverEquityBindingV1,
            )
            if RANGE_EQUITY_BINDING_ARTIFACT in payloads
            else None
        )
        verify_versioned_range_river_equity_binding_artifact(
            input_case,
            normalized_case,
            report.reconstructed_input,
            binding_artifact,
        )
        if revision_root is not None:
            admission_record = read_range_equity_admission_record(
                Path(revision_root),
                run_id,
                maximum_bytes=maximum_admission_record_bytes,
            )
            if admission_record is None:
                if binding_artifact is not None:
                    raise CanonicalStorageError(
                        "range-equity payload lacks its pre-execution admission record"
                    )
            elif binding_artifact is None:
                raise CanonicalStorageError(
                    "range-equity admission record requires its durable payload binding"
                )
            else:
                verify_range_equity_admission_record(admission_record, binding_artifact)
    except ValueError as exc:
        raise CanonicalStorageError("range-equity persisted cases do not correlate") from exc
    if not bounded_river_marker:
        try:
            verify_versioned_range_tool_chain(
                input_case,
                report.tool_results,
                run_status=report.run_status,
            )
            verify_versioned_range_river_equity_tool_chain(
                input_case,
                report.tool_results,
                run_status=report.run_status,
            )
        except ValueError as exc:
            raise CanonicalStorageError("versioned range tool chain replay failed") from exc
    confirmed_names = set(payloads) & _CONFIRMED_REVIEW_ARTIFACTS
    bounded_names = set(payloads) & _BOUNDED_NL_ARTIFACTS
    bounded_river_names = set(payloads) & _BOUNDED_RIVER_CALL_EV_ARTIFACTS
    if bounded_river_marker:
        successful_artifacts = _BOUNDED_RIVER_CALL_EV_BASE_ARTIFACTS | {
            BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
            BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT,
        }
        allowed_sets = (
            {successful_artifacts}
            if report.run_status == "completed"
            else {
                _BOUNDED_RIVER_CALL_EV_BASE_ARTIFACTS,
                _BOUNDED_RIVER_CALL_EV_BASE_ARTIFACTS | {BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT},
                _BOUNDED_RIVER_CALL_EV_BASE_ARTIFACTS
                | {BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT},
                _BOUNDED_RIVER_CALL_EV_BASE_ARTIFACTS
                | {
                    BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
                    BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT,
                },
            }
        )
        if frozenset(bounded_river_names) not in allowed_sets:
            raise CanonicalStorageError(
                "bounded river call-EV marker lacks its exact terminal artifact set"
            )
        if not {
            "input.json",
            "final_report.json",
            "assignments.json",
            RANGE_EQUITY_BINDING_ARTIFACT,
        } <= set(payloads):
            raise CanonicalStorageError(
                "bounded river call-EV payload lacks a required canonical ledger"
            )
    elif bounded_river_names:
        raise CanonicalStorageError(
            "bounded river call-EV artifacts require their exact case marker"
        )
    input_marker_present = "confirmed_review" in input_case.metadata
    report_marker_present = isinstance(report_metadata, dict) and (
        "confirmed_review" in report_metadata
    )
    report_marker = (
        report_metadata.get("confirmed_review") if isinstance(report_metadata, dict) else None
    )
    if input_marker_present != report_marker_present or (
        input_marker_present and report_marker != input_case.metadata["confirmed_review"]
    ):
        raise CanonicalStorageError("confirmed-review input and report markers must match exactly")
    confirmed_marker = input_marker_present or report_marker_present
    if confirmed_marker != bool(confirmed_names) or (
        confirmed_names and confirmed_names != _CONFIRMED_REVIEW_ARTIFACTS
    ):
        raise CanonicalStorageError(
            "confirmed-review marker and complete artifact set must appear together"
        )
    if confirmed_marker:
        if "assignments.json" not in payloads:
            raise CanonicalStorageError("confirmed-review payload requires the assignment ledger")
        if revision_root is None or revision is None or transaction_id is None:
            raise CanonicalStorageError(
                "confirmed-review payload requires exact terminal storage authority"
            )
        source_bytes = payloads["confirmed_review_source.txt"]
        candidate = _parse_json_model(
            payloads["confirmed_review_candidate.json"],
            ReviewIntakeCandidateV1,
        )
        confirmation = _parse_json_model(
            payloads["confirmed_review_confirmation.json"],
            ReviewIntakeConfirmationV1,
        )
        provenance = _parse_json_model(
            payloads["confirmed_review_provenance.json"],
            ConfirmedReviewProvenanceV1,
        )
        if confirmation.run_id != run_id or provenance.run_id != run_id:
            raise CanonicalStorageError("confirmed-review run ID correlation mismatch")
        reports_by_role = {agent_report.agent_role: agent_report for agent_report in agent_reports}
        ordered_agent_reports = [
            reports_by_role[record.agent_role]
            for record in execution_records
            if record.agent_role in reports_by_role
        ]
        if len(ordered_agent_reports) != len(execution_records) or len(agent_reports) != len(
            execution_records
        ):
            raise CanonicalStorageError("confirmed-review agent reports do not match executions")
        try:
            # Delayed to preserve the storage package's import direction:
            # confirmed_review uses canonical storage helpers, while terminal
            # replay is the only path that needs the higher-level verifier.
            from poker_deliberation.confirmed_review import (
                verify_confirmed_review_provenance,
            )

            verify_confirmed_review_provenance(
                source_bytes=source_bytes,
                candidate=candidate,
                confirmation=confirmation,
                case=input_case,
                report=report,
                provenance=provenance,
                assignments=assignments,
                agent_reports=ordered_agent_reports,
                storage_root=revision_root,
                storage_revision=revision,
                storage_transaction_id=transaction_id,
            )
        except ValueError as exc:
            raise CanonicalStorageError("confirmed-review source-to-report replay failed") from exc
    bounded_input_marker_present = "bounded_natural_language_review" in input_case.metadata
    bounded_report_marker_present = isinstance(report_metadata, dict) and (
        "bounded_natural_language_review" in report_metadata
    )
    bounded_report_marker = (
        report_metadata.get("bounded_natural_language_review")
        if isinstance(report_metadata, dict)
        else None
    )
    if bounded_input_marker_present != bounded_report_marker_present or (
        bounded_input_marker_present
        and bounded_report_marker != input_case.metadata["bounded_natural_language_review"]
    ):
        raise CanonicalStorageError("bounded-language input and report markers must match exactly")
    bounded_marker = bounded_input_marker_present or bounded_report_marker_present
    if bounded_marker != bool(bounded_names) or (
        bounded_names and bounded_names != _BOUNDED_NL_ARTIFACTS
    ):
        raise CanonicalStorageError(
            "bounded-language marker and complete artifact set must appear together"
        )
    if sum((confirmed_marker, bounded_marker, bounded_river_marker)) > 1:
        raise CanonicalStorageError("confirmed intake artifact contracts are mutually exclusive")
    if bounded_marker:
        if "assignments.json" not in payloads:
            raise CanonicalStorageError("bounded-language payload requires the assignment ledger")
        if revision_root is None or revision is None or transaction_id is None:
            raise CanonicalStorageError(
                "bounded-language payload requires exact terminal storage authority"
            )
        source_bytes = payloads["bounded_nl_source.txt"]
        bounded_candidate = _parse_json_model(
            payloads["bounded_nl_candidate.json"],
            BoundedIntakeCandidateV1,
        )
        bounded_confirmation = _parse_json_model(
            payloads["bounded_nl_confirmation.json"],
            BoundedIntakeConfirmationV1,
        )
        bounded_provenance = _parse_json_model(
            payloads["bounded_nl_provenance.json"],
            BoundedNaturalLanguageProvenanceV1,
        )
        if bounded_confirmation.run_id != run_id or bounded_provenance.run_id != run_id:
            raise CanonicalStorageError("bounded-language run ID correlation mismatch")
        reports_by_role = {agent_report.agent_role: agent_report for agent_report in agent_reports}
        ordered_agent_reports = [
            reports_by_role[record.agent_role]
            for record in execution_records
            if record.agent_role in reports_by_role
        ]
        if len(ordered_agent_reports) != len(execution_records) or len(agent_reports) != len(
            execution_records
        ):
            raise CanonicalStorageError("bounded-language agent reports do not match executions")
        try:
            from poker_deliberation.bounded_natural_language_provenance import (
                verify_bounded_natural_language_provenance,
            )

            verify_bounded_natural_language_provenance(
                source_bytes=source_bytes,
                candidate=bounded_candidate,
                confirmation=bounded_confirmation,
                case=input_case,
                report=report,
                provenance=bounded_provenance,
                assignments=assignments,
                agent_reports=ordered_agent_reports,
                storage_root=revision_root,
                storage_revision=revision,
                storage_transaction_id=transaction_id,
            )
        except ValueError as exc:
            raise CanonicalStorageError("bounded-language source-to-report replay failed") from exc

    if bounded_river_marker:
        if revision_root is None or revision is None or transaction_id is None:
            raise CanonicalStorageError(
                "bounded river call-EV payload requires exact terminal storage authority"
            )
        source_bytes = payloads[BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT]
        bounded_river_range = _parse_json_model(
            payloads[BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT],
            VersionedRangeDefinitionV1,
        )
        bounded_river_candidate = _parse_json_model(
            payloads[BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT],
            BoundedRiverCallEvCandidateV1,
        )
        bounded_river_confirmation = _parse_json_model(
            payloads[BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT],
            BoundedRiverCallEvConfirmationV1,
        )
        bounded_river_binding = _parse_json_model(
            payloads[BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT],
            BoundedRiverCallEvBindingV1,
        )
        bounded_river_result = (
            _parse_json_model(
                payloads[BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT],
                BoundedRiverCallEvResultV1,
            )
            if BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT in payloads
            else None
        )
        bounded_river_provenance = (
            _parse_json_model(
                payloads[BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT],
                BoundedRiverCallEvProvenanceV1,
            )
            if BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT in payloads
            else None
        )
        bounded_river_failure_evidence = (
            _parse_json_model(
                payloads[BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT],
                BoundedRiverCallEvBudgetFailureEvidenceV1,
            )
            if BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT in payloads
            else None
        )
        if (
            bounded_river_confirmation.run_id != run_id
            or bounded_river_binding.run_id != run_id
            or (bounded_river_result is not None and bounded_river_result.run_id != run_id)
            or (bounded_river_provenance is not None and bounded_river_provenance.run_id != run_id)
            or (
                bounded_river_failure_evidence is not None
                and bounded_river_failure_evidence.run_id != run_id
            )
            or bounded_river_candidate.projection.range_definition != bounded_river_range
            or binding_artifact != bounded_river_candidate.projection.range_equity_binding
        ):
            raise CanonicalStorageError("bounded river call-EV artifact correlation mismatch")
        bounded_river_admission_record = read_bounded_river_call_ev_admission_record(
            Path(revision_root),
            run_id,
            maximum_bytes=maximum_admission_record_bytes,
        )
        if bounded_river_admission_record is None:
            raise CanonicalStorageError(
                "bounded river call-EV payload lacks its pre-execution admission record"
            )
        verify_bounded_river_call_ev_admission_record(
            bounded_river_admission_record,
            bounded_river_binding,
        )
        if budget_policy is None:
            raise CanonicalStorageError(
                "bounded river call-EV payload requires its exact budget policy"
            )
        external_failure_records = read_bounded_river_call_ev_budget_failure_evidence(
            Path(revision_root),
            run_id,
            maximum_bytes=maximum_admission_record_bytes,
        )
        budget_failed_results = [
            item
            for item in report.tool_results
            if item.status.value == "failed"
            and (item.error or "").startswith("strict budget failure: ")
        ]
        if budget_failed_results:
            if (
                len(budget_failed_results) != 1
                or len(external_failure_records) != 1
                or bounded_river_failure_evidence is None
                or canonical_json_bytes(bounded_river_failure_evidence)
                != canonical_json_bytes(external_failure_records[0])
            ):
                raise CanonicalStorageError(
                    "bounded river call-EV budget failure lacks independent evidence"
                )
            verify_bounded_river_call_ev_budget_failure_evidence(
                external_failure_records[0],
                binding=bounded_river_binding,
                admission_record=bounded_river_admission_record,
                result=budget_failed_results[0],
                policy=budget_policy,
            )
        elif external_failure_records or bounded_river_failure_evidence is not None:
            raise CanonicalStorageError(
                "bounded river call-EV has uncorrelated budget failure evidence"
            )
        reports_by_role = {agent_report.agent_role: agent_report for agent_report in agent_reports}
        ordered_agent_reports = [
            reports_by_role[record.agent_role]
            for record in execution_records
            if record.agent_role in reports_by_role
        ]
        if len(ordered_agent_reports) != len(execution_records) or len(agent_reports) != len(
            execution_records
        ):
            raise CanonicalStorageError(
                "bounded river call-EV agent reports do not match executions"
            )
        try:
            from poker_deliberation.bounded_river_call_ev import (
                _admit_at as admit_bounded_river_call_ev_at,
            )
            from poker_deliberation.bounded_river_call_ev import (
                verify_bounded_river_call_ev_tool_chain,
            )

            admitted_at = (
                bounded_river_provenance.admitted_at
                if bounded_river_provenance is not None
                else bounded_river_confirmation.confirmed_at
            )
            bounded_river_admission = admit_bounded_river_call_ev_at(
                source_bytes,
                bounded_river_candidate,
                bounded_river_confirmation,
                admitted_at=admitted_at,
            )
            if (
                bounded_river_admission.case != input_case
                or bounded_river_admission.binding != bounded_river_binding
            ):
                raise ValueError("admitted bounded river call-EV case differs")
            expected_result = verify_bounded_river_call_ev_tool_chain(
                bounded_river_admission,
                report.tool_results,
                run_status=report.run_status,
            )
            if expected_result != bounded_river_result:
                raise ValueError("bounded river call-EV result differs from replay")
            from poker_deliberation.bounded_river_call_ev_provenance import (
                verify_bounded_river_call_ev_structural_provenance,
            )

            verify_bounded_river_call_ev_structural_provenance(
                source_bytes=source_bytes,
                candidate=bounded_river_candidate,
                confirmation=bounded_river_confirmation,
                case=input_case,
                result=bounded_river_result,
                report=report,
                admitted_at=admitted_at,
                assignments=assignments,
                agent_reports=ordered_agent_reports,
                storage_root=revision_root,
                storage_revision=revision,
                storage_transaction_id=transaction_id,
            )
            if report.run_status == "completed":
                if bounded_river_result is None or bounded_river_provenance is None:
                    raise ValueError("completed bounded river call-EV payload is incomplete")
                from poker_deliberation.bounded_river_call_ev_provenance import (
                    verify_bounded_river_call_ev_provenance,
                )

                verify_bounded_river_call_ev_provenance(
                    source_bytes=source_bytes,
                    candidate=bounded_river_candidate,
                    confirmation=bounded_river_confirmation,
                    case=input_case,
                    result=bounded_river_result,
                    report=report,
                    provenance=bounded_river_provenance,
                    assignments=assignments,
                    agent_reports=ordered_agent_reports,
                    storage_root=revision_root,
                    storage_revision=revision,
                    storage_transaction_id=transaction_id,
                )
        except ValueError as exc:
            raise CanonicalStorageError(
                "bounded river call-EV source-to-report replay failed"
            ) from exc

    context_bindings = [
        {
            key: value
            for key, value in record.model_dump(mode="json").items()
            if key == "context_sha256" or key.startswith("context_") or key == "parent_context_id"
        }
        for record in execution_records
    ]
    execution_bindings: list[object] = [
        record.model_dump(mode="json") for record in execution_records
    ]
    execution_bindings.extend(
        agent_report.model_dump(mode="json") for agent_report in agent_reports
    )
    execution_bindings.extend(item.model_dump(mode="json") for item in report.tool_results)
    return (
        sha256_bytes(payloads["input.json"]),
        sha256_bytes(payloads["state.json"]),
        _lineage_head("event", events),
        (
            approval_authority_head
            if approval_authority_head is not None
            else _lineage_head(
                "approval",
                [item.model_dump(mode="json") for item in approvals],
            )
        ),
        _lineage_head("context", context_bindings),
        _lineage_head("execution", execution_bindings),
    )


def _canonical_entries(
    inventory: Sequence[PayloadInventoryEntryV1],
) -> tuple[PayloadInventoryEntryV1, ...]:
    entries = tuple(sorted(inventory, key=lambda item: item.revision_relative_path.encode("utf-8")))
    paths: set[str] = set()
    logical_names: set[str] = set()
    aliases: set[str] = set()
    for entry in entries:
        logical_name = validate_logical_name(entry.logical_name)
        expected_path = f"payload/{logical_name}"
        if entry.revision_relative_path != expected_path:
            raise CanonicalStorageError("terminal inventory relative path mismatch")
        alias = logical_name.lower()
        if (
            entry.revision_relative_path in paths
            or logical_name in logical_names
            or alias in aliases
        ):
            raise CanonicalStorageError("duplicate terminal inventory identity")
        paths.add(entry.revision_relative_path)
        logical_names.add(logical_name)
        aliases.add(alias)
        if entry.classification_evidence_sha256 != classification_evidence_sha256(
            entry.classification_evidence
        ):
            raise CanonicalStorageError("classification evidence hash mismatch")
        if entry.provenance_bindings != canonicalize_bindings(entry.provenance_bindings):
            raise CanonicalStorageError("provenance bindings are not canonical")
        if entry.source_sha256 != upstream_source_sha256(entry.provenance_bindings):
            raise CanonicalStorageError("payload source hash mismatch")
    return entries


def terminal_inventory_sha256(
    inventory: Sequence[PayloadInventoryEntryV1],
) -> str:
    return canonical_domain_sha256(TERMINAL_INVENTORY_DOMAIN, _canonical_entries(inventory))


def required_inventory_sha256(
    inventory: Sequence[PayloadInventoryEntryV1],
) -> str:
    required = tuple(item for item in _canonical_entries(inventory) if item.required)
    if not required:
        raise CanonicalStorageError("terminal revision requires at least one required payload")
    return canonical_domain_sha256(REQUIRED_INVENTORY_DOMAIN, required)


def _validate_inventory_contract(entry: PayloadInventoryEntryV1) -> None:
    logical_name = entry.logical_name
    terminal_only = _TERMINAL_ONLY_ARTIFACT_TABLE.get(logical_name)
    if terminal_only is None:
        try:
            media_type, serialization, schema, _origin = artifact_table_entry(
                logical_name,
                (entry.artifact_schema_version if logical_name == "final_report.json" else None),
            )
            expected_tuple = (media_type, serialization, schema)
        except CanonicalStorageError as exc:
            raise CanonicalStorageError(
                "terminal inventory logical artifact is not admitted"
            ) from exc
    else:
        expected_tuple = terminal_only
    actual_tuple = (
        entry.media_type,
        entry.serialization,
        entry.artifact_schema_version,
    )
    legacy_lifecycle_tuple = (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-lifecycle-audit-list-artifact-v1",
    )
    if actual_tuple != expected_tuple and not (
        logical_name == "lifecycle_audit.json" and actual_tuple == legacy_lifecycle_tuple
    ):
        raise CanonicalStorageError("terminal inventory media/schema/serialization mismatch")


def verify_payload_inventory(
    inventory: Sequence[PayloadInventoryEntryV1],
    payloads: Mapping[str, bytes],
    *,
    allow_opaque: bool = False,
) -> tuple[PayloadInventoryEntryV1, ...]:
    entries = _canonical_entries(inventory)
    expected = {entry.logical_name for entry in entries}
    if set(payloads) != expected:
        raise CanonicalStorageError("terminal payload inventory membership mismatch")
    for entry in entries:
        if not allow_opaque:
            _validate_inventory_contract(entry)
        data = payloads[entry.logical_name]
        if (
            not isinstance(data, bytes)
            or len(data) != entry.size_bytes
            or sha256_bytes(data) != entry.sha256
        ):
            raise CanonicalStorageError("terminal payload size or hash mismatch")
        validate_payload_bytes(entry, data, allow_opaque=allow_opaque)
    return entries


def _validate_json_models(data: bytes, model: type[BaseModel]) -> None:
    parse_canonical_json(data)
    adapter = TypeAdapter(list[model])  # type: ignore[valid-type]
    try:
        values = adapter.validate_json(data, strict=True)
    except ValidationError:
        try:
            values = adapter.validate_json(data, strict=False)
        except ValidationError as fallback_exc:
            raise CanonicalStorageError("terminal payload list schema mismatch") from fallback_exc
    if canonical_json_bytes(values) != data:
        raise CanonicalStorageError("terminal payload list canonical bytes mismatch")


def _parse_json_model(data: bytes, model: type[T]) -> T:
    parse_canonical_json(data)
    adapter = TypeAdapter(model)
    try:
        value = adapter.validate_json(data, strict=False)
    except ValidationError as exc:
        raise CanonicalStorageError("terminal payload schema mismatch") from exc
    if canonical_json_bytes(value) != data:
        raise CanonicalStorageError("terminal payload canonical bytes mismatch")
    return value


def _validate_json_model(data: bytes, model: type[BaseModel]) -> None:
    _parse_json_model(data, model)


def _parse_normalization_result(data: bytes) -> NormalizationResultV1:
    value = parse_canonical_json(data)
    if not isinstance(value, dict):
        raise CanonicalStorageError("normalization payload must be an object")
    if (
        value.get("result_version") != NORMALIZATION_RESULT_VERSION
        or value.get("contract_version") != NORMALIZATION_CONTRACT_VERSION
    ):
        raise UnsupportedTerminalVersion("unsupported normalization payload version")
    result = _parse_json_model(data, NormalizationResultV1)
    if normalization_result_json_bytes(result) != data:
        raise CanonicalStorageError("normalization payload canonical bytes mismatch")
    return result


def _validate_json_value(logical_name: str, data: bytes) -> None:
    single_models: dict[str, type[BaseModel]] = {
        BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT: VersionedRangeDefinitionV1,
        BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT: BoundedRiverCallEvCandidateV1,
        BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT: BoundedRiverCallEvConfirmationV1,
        BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT: BoundedRiverCallEvBindingV1,
        BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT: BoundedRiverCallEvResultV1,
        BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT: BoundedRiverCallEvProvenanceV1,
        "confirmed_review_candidate.json": ReviewIntakeCandidateV1,
        "confirmed_review_confirmation.json": ReviewIntakeConfirmationV1,
        "confirmed_review_provenance.json": ConfirmedReviewProvenanceV1,
        "bounded_nl_candidate.json": BoundedIntakeCandidateV1,
        "bounded_nl_confirmation.json": BoundedIntakeConfirmationV1,
        "bounded_nl_provenance.json": BoundedNaturalLanguageProvenanceV1,
        "input.json": CaseInput,
        "normalized_case.json": CaseInput,
        "final_report.json": FinalReport,
    }
    list_models: dict[str, type[BaseModel]] = {
        "assumptions.json": Assumption,
        "approvals.json": ApprovalRequest,
        "assignments.json": AgentAssignment,
        "agent_execution_records.json": AgentExecutionRecord,
        "security_events.json": SecurityEvent,
        "disputes.json": Dispute,
        "lifecycle_audit.json": LifecycleAuditMetadata,
    }
    model = single_models.get(logical_name)
    if model is not None:
        _validate_json_model(data, model)
        return
    if logical_name == "normalization.json":
        _parse_normalization_result(data)
        return
    if logical_name == "approval_ledger_v2.json":
        parse_approval_model(data, ApprovalLedgerV2)
        return
    model = list_models.get(logical_name)
    if model is not None:
        _validate_json_models(data, model)
        return
    if logical_name.startswith("agent_reports/"):
        parse_canonical_model(data, AgentReport)
        return
    if logical_name.startswith("tool_results/") and not logical_name.endswith(".input.json"):
        _validate_json_model(data, ToolResult)
        return
    value = parse_canonical_json(data)
    if logical_name == "state.json" and not isinstance(value, dict):
        raise CanonicalStorageError("state checkpoint must be an object")
    if logical_name.startswith("tool_results/") and not isinstance(value, dict):
        raise CanonicalStorageError("tool input payload must be an object")


def validate_payload_bytes(
    entry: PayloadInventoryEntryV1,
    data: bytes,
    *,
    allow_opaque: bool = False,
) -> None:
    if entry.serialization == CONTROL_CANONICALIZATION:
        _validate_json_value(entry.logical_name, data)
    elif entry.serialization == JSONL_SERIALIZATION:
        if entry.logical_name == "approval_decisions_v2.jsonl":
            parse_approval_jsonl(data, ApprovalDecisionRecordV2)
        elif entry.logical_name == "approval_audit_v2.jsonl":
            parse_approval_jsonl(data, ApprovalDomainAuditEventV2)
        elif entry.logical_name == APPROVAL_REISSUE_ARTIFACT:
            parse_approval_jsonl(data, ApprovalReissueRecordV2)
        elif entry.logical_name == "evidence.jsonl":
            parse_canonical_jsonl(data, EvidenceRecord)
        else:
            raise CanonicalStorageError("unsupported terminal JSONL payload")
    elif entry.serialization == TEXT_SERIALIZATION:
        validate_canonical_text(data)
    elif entry.serialization == "opaque-bytes-v1" and allow_opaque:
        if not isinstance(data, bytes):
            raise CanonicalStorageError("legacy opaque payload must be exact bytes")
    else:
        raise CanonicalStorageError("opaque terminal payloads are not admitted")


def inventory_entry(
    *,
    logical_name: str,
    data: bytes,
    media_type: str,
    artifact_schema_version: str,
    serialization: str,
    required: bool = True,
    classification: ContextClassification = ContextClassification.INTERNAL,
    classification_source: ClassificationSource = ClassificationSource.DEFAULT_INTERNAL,
    classification_evidence: ClassificationEvidence | None = None,
    provenance_bindings: Sequence[ProvenanceBindingV1] = (),
) -> PayloadInventoryEntryV1:
    """Build one exact inventory entry after pure byte/schema admission."""

    validate_logical_name(logical_name)
    evidence = classification_evidence or ClassificationEvidence(
        restricted_secret_check_completed=True
    )
    bindings: tuple[ProvenanceBindingV1, ...]
    if provenance_bindings:
        bindings = canonicalize_bindings(provenance_bindings)
    else:
        local = LocalDataBindingV1(
            logical_name=logical_name,
            classification=classification,
            classification_source=classification_source,
            classification_evidence=evidence,
            classification_evidence_sha256=classification_evidence_sha256(evidence),
        )
        bindings = (local,)
    entry = PayloadInventoryEntryV1(
        logical_name=logical_name,
        revision_relative_path=f"payload/{logical_name}",
        media_type=media_type,  # type: ignore[arg-type]
        artifact_schema_version=artifact_schema_version,
        serialization=serialization,  # type: ignore[arg-type]
        size_bytes=len(data),
        sha256=sha256_bytes(data),
        required=required,
        classification=classification,
        classification_source=classification_source,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
        source_sha256=upstream_source_sha256(bindings),
        provenance_bindings=bindings,
    )
    validate_payload_bytes(entry, data)
    if classification in {
        ContextClassification.SENSITIVE,
        ContextClassification.RESTRICTED,
    }:
        raise CanonicalStorageError("terminal persistence requires separately verified encryption")
    return entry


def legacy_source_inventory_sha256(
    entries: Sequence[Mapping[str, object]],
) -> str:
    ordered = tuple(
        sorted(
            (dict(item) for item in entries),
            key=lambda item: str(item["relative_path"]).encode("utf-8"),
        )
    )
    return canonical_domain_sha256(LEGACY_SOURCE_INVENTORY_DOMAIN, ordered)


__all__ = [
    "UnsupportedTerminalVersion",
    "budget_binding_sha256",
    "canonical_terminal_bytes",
    "completion_marker_sha256",
    "current_pointer_sha256",
    "empty_lineage_head_sha256",
    "inventory_entry",
    "legacy_source_inventory_sha256",
    "lifecycle_audit_sha256",
    "manifest_sha256",
    "parse_completion_marker",
    "parse_current_pointer",
    "parse_run_manifest",
    "product_payload_commitments",
    "required_inventory_sha256",
    "terminal_inventory_sha256",
    "verify_payload_inventory",
]
