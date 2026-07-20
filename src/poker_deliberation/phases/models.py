"""Phase-specific immutable payload models for the internal P2-010A API."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.context_lifecycle import ContextEnvelope, context_payload
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    AgentExecutionRecord,
    AgentReport,
    ApprovalProposal,
    ApprovalRequest,
    CaseInput,
    Claim,
    Dispute,
    EvidenceRecord,
    FinalReport,
    SecurityEvent,
    ToolRequest,
    ToolResult,
)


class PhasePayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
    )


class IntakeValidationInput(PhasePayload):
    case: CaseInput
    record_sensitive_data: bool
    security_events: tuple[SecurityEvent, ...] = ()
    fallback_approval_ids: tuple[str, ...] = ()


class IntakeValidationOutput(PhasePayload):
    case: CaseInput
    safe_case: CaseInput
    accepted_evidence: tuple[EvidenceRecord, ...] = ()
    approval_proposals: tuple[ApprovalProposal, ...] = ()
    security_events: tuple[SecurityEvent, ...] = ()
    data_quality: tuple[str, ...] = ()


class NormalizationInput(PhasePayload):
    safe_case: CaseInput
    assumptions: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


class NormalizationOutput(PhasePayload):
    normalized_case: CaseInput
    assumptions: tuple[dict[str, Any], ...] = ()
    warnings: tuple[str, ...] = ()


class RoutingInput(PhasePayload):
    case_kind: str
    role_snapshot: tuple[AgentAssignment, ...]
    registered_tools: tuple[str, ...]

    @field_validator("registered_tools")
    @classmethod
    def sorted_unique_tools(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("registered_tools must be sorted and unique")
        return value


class RoutingOutput(PhasePayload):
    assignments: tuple[AgentAssignment, ...]


class ContextBuildInput(PhasePayload):
    case: CaseInput
    assignment: AgentAssignment
    registered_tools: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    context_id: str
    context_attempt_id: str


class ContextDispatch(PhasePayload):
    assignment: AgentAssignment
    context: AgentContext
    envelope: ContextEnvelope

    @model_validator(mode="after")
    def lineage_matches_dispatch(self) -> ContextDispatch:
        if self.envelope.lineage.assignment_id != self.assignment.assignment_id:
            raise ValueError("context assignment lineage mismatch")
        if tuple(sorted(context_payload(self.context))) != tuple(self.assignment.context_keys):
            raise ValueError("context allowlist does not match assignment context keys")
        return self


class ContextBuildOutput(PhasePayload):
    dispatches: tuple[ContextDispatch, ...]


class AnalysisInput(PhasePayload):
    dispatch: ContextDispatch
    provider_timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    registered_tools: tuple[str, ...]
    max_output_bytes: int = Field(gt=0)
    record_sensitive_data: bool
    started_at: datetime
    execution_id: str
    fallback_report_id: str
    existing_report_ids: tuple[str, ...] = ()


class AnalysisOutput(PhasePayload):
    assignment: AgentAssignment
    context: AgentContext
    envelope: ContextEnvelope
    report: AgentReport
    execution_record: AgentExecutionRecord
    data_quality: tuple[str, ...] = ()
    timed_out: bool = False


class ToolResearchInput(PhasePayload):
    requests: tuple[ToolRequest, ...]
    start_ordinal: int = Field(default=0, ge=0)
    existing_result_ids: tuple[str, ...] = ()
    fallback_result_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def fallback_count_matches_requests(self) -> ToolResearchInput:
        if len(self.requests) != len(self.fallback_result_ids):
            raise ValueError("one fallback result ID is required per tool request")
        request_ids = [request.request_id for request in self.requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("tool request IDs must be unique")
        return self


class ToolExecutionBinding(PhasePayload):
    run_id: str
    phase_attempt_id: str
    ordinal: int = Field(ge=0)
    request: ToolRequest
    request_input_sha256: str
    requested_contract_version: str | None = None
    result_contract_version: str
    result: ToolResult

    @model_validator(mode="after")
    def tool_and_input_match(self) -> ToolExecutionBinding:
        if self.result.tool_name != self.request.tool_name:
            raise ValueError("tool result name does not match its request")
        return self


class ToolResearchOutput(PhasePayload):
    bindings: tuple[ToolExecutionBinding, ...]
    data_quality: tuple[str, ...] = ()


class CritiqueInput(PhasePayload):
    case: CaseInput
    reports: tuple[AgentReport, ...]
    tool_results: tuple[ToolResult, ...]
    evidence_ids: tuple[str, ...]
    existing_disputes: tuple[Dispute, ...] = ()


class CritiqueOutput(PhasePayload):
    disputes: tuple[Dispute, ...]
    data_quality: tuple[str, ...] = ()


class AdjudicationInput(PhasePayload):
    case: CaseInput
    tool_results: tuple[ToolResult, ...]


class AdjudicationOutput(PhasePayload):
    claim_assessments: tuple[Claim, ...]
    data_quality: tuple[str, ...] = ()


class ProviderSnapshot(PhasePayload):
    available: bool
    reason: str


class SynthesisInput(PhasePayload):
    run_id: str
    machine_state: str
    completed: bool
    case: CaseInput
    data_quality: tuple[str, ...]
    claim_assessments: tuple[Claim, ...]
    reports: tuple[AgentReport, ...]
    execution_records: tuple[AgentExecutionRecord, ...]
    tool_results: tuple[ToolResult, ...]
    disputes: tuple[Dispute, ...]
    evidence_records: tuple[EvidenceRecord, ...]
    approvals: tuple[ApprovalRequest, ...]
    security_events: tuple[SecurityEvent, ...]
    provider_snapshot: ProviderSnapshot
    tool_input_artifact_paths: tuple[str, ...]
    record_sensitive_data: bool
    generated_at: datetime


class SynthesisOutput(PhasePayload):
    report: FinalReport
