"""Phase-specific immutable payload models for the internal P2-010A API."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.budgets import (
    BudgetFailure,
    BudgetPolicyV2,
    BudgetSnapshot,
    CancellationStatus,
    DeadlineStatus,
    RetryClassification,
    UsageDelta,
)
from poker_deliberation.context_lifecycle import (
    ContextEnvelope,
    assignment_sha256,
    context_payload,
)
from poker_deliberation.phases.contracts import canonical_sha256
from poker_deliberation.providers.base import ProviderAvailability
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
        strict=True,
    )


class IntakeValidationInput(PhasePayload):
    case: CaseInput
    record_sensitive_data: bool
    sensitive_action_categories: tuple[str, ...]
    security_events: tuple[SecurityEvent, ...] = ()
    fallback_approval_ids: tuple[str, ...] = ()

    @field_validator("sensitive_action_categories")
    @classmethod
    def sorted_unique_categories(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("sensitive_action_categories must be sorted and unique")
        return value


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
        if self.envelope.lineage.assignment_sha256 != assignment_sha256(self.assignment):
            raise ValueError("context assignment hash mismatch")
        payload = context_payload(self.context)
        if tuple(sorted(payload)) != tuple(self.assignment.context_keys):
            raise ValueError("context allowlist does not match assignment context keys")
        canonical_payload = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        if self.envelope.canonical_payload != canonical_payload:
            raise ValueError("dispatch context does not match its canonical envelope payload")
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
    provider_availability: ProviderAvailability
    legacy_provider_contract: bool = False
    budget_policy: BudgetPolicyV2
    budget_snapshot: BudgetSnapshot
    budget_observed_at_ns: int = Field(ge=0)
    run_deadline_ns: int = Field(ge=1)

    @model_validator(mode="after")
    def budget_snapshot_matches_policy(self) -> AnalysisInput:
        if self.budget_snapshot.policy_sha256 != self.budget_policy.canonical_sha256:
            raise ValueError("analysis budget snapshot policy mismatch")
        if self.max_output_bytes != self.budget_policy.max_provider_output_bytes:
            raise ValueError("compatibility output cap must match budget policy")
        if self.run_deadline_ns <= self.budget_observed_at_ns:
            raise ValueError("analysis requires a positive absolute runtime window")
        return self


class AnalysisOutput(PhasePayload):
    assignment: AgentAssignment
    context: AgentContext
    envelope: ContextEnvelope
    report: AgentReport
    execution_record: AgentExecutionRecord
    data_quality: tuple[str, ...] = ()
    timed_out: bool = False
    usage_delta: UsageDelta = Field(default_factory=UsageDelta)
    budget_failure: BudgetFailure | None = None
    retry_classification: RetryClassification | None = None
    deadline_status: DeadlineStatus = DeadlineStatus.ACTIVE
    cancellation_status: CancellationStatus = CancellationStatus.NOT_REQUESTED

    @model_validator(mode="after")
    def control_and_budget_status_are_consistent(self) -> AnalysisOutput:
        if self.timed_out != (self.deadline_status is DeadlineStatus.TIMED_OUT):
            raise ValueError("timed_out must match deadline_status")
        if (
            self.deadline_status is DeadlineStatus.TIMED_OUT
            and self.cancellation_status is CancellationStatus.NOT_REQUESTED
        ):
            raise ValueError("timed out analysis must record cancellation state")
        if self.budget_failure is not None and (
            self.retry_classification is None or self.retry_classification.retryable
        ):
            raise ValueError("budget failures require non-retryable classification")
        return self


class ToolResearchInput(PhasePayload):
    requests: tuple[ToolRequest, ...]
    start_ordinal: int = Field(default=0, ge=0)
    existing_result_ids: tuple[str, ...] = ()
    fallback_result_ids: tuple[str, ...] = ()
    budget_policy: BudgetPolicyV2 | None = None
    budget_snapshot: BudgetSnapshot | None = None
    budget_observed_at_ns: int | None = Field(default=None, ge=0)
    run_deadline_ns: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def fallback_count_matches_requests(self) -> ToolResearchInput:
        if len(self.requests) != len(self.fallback_result_ids):
            raise ValueError("one fallback result ID is required per tool request")
        request_ids = [request.request_id for request in self.requests]
        if len(request_ids) != len(set(request_ids)):
            raise ValueError("tool request IDs must be unique")
        if (self.budget_policy is None) != (self.budget_snapshot is None):
            raise ValueError("tool budget policy and snapshot must be provided together")
        budget_values = (
            self.budget_policy,
            self.budget_snapshot,
            self.budget_observed_at_ns,
            self.run_deadline_ns,
        )
        if any(item is not None for item in budget_values) and any(
            item is None for item in budget_values
        ):
            raise ValueError("tool budget policy, snapshot, observation, and deadline are atomic")
        if (
            self.budget_policy is not None
            and self.budget_snapshot is not None
            and self.budget_snapshot.policy_sha256 != self.budget_policy.canonical_sha256
        ):
            raise ValueError("tool budget snapshot policy mismatch")
        if (
            self.budget_observed_at_ns is not None
            and self.run_deadline_ns is not None
            and self.run_deadline_ns <= self.budget_observed_at_ns
        ):
            raise ValueError("tool execution requires a positive absolute runtime window")
        return self


class ToolExecutionBinding(PhasePayload):
    run_id: str
    phase_attempt_id: str
    ordinal: int = Field(ge=0)
    request: ToolRequest
    request_input_sha256: str
    validated_result_input_sha256: str
    materialized_result_input_sha256: str
    requested_contract_version: str | None = None
    supported_contract_version: str
    result_contract_version: str
    result: ToolResult

    @model_validator(mode="after")
    def tool_and_input_match(self) -> ToolExecutionBinding:
        if self.result.tool_name != self.request.tool_name:
            raise ValueError("tool result name does not match its request")
        request_hash = canonical_sha256(self.request.input)
        if self.request_input_sha256 != request_hash:
            raise ValueError("tool request input hash mismatch")
        if self.validated_result_input_sha256 != request_hash:
            raise ValueError("validated tool result input correlation mismatch")
        if self.materialized_result_input_sha256 != canonical_sha256(self.result.input):
            raise ValueError("materialized tool result input hash mismatch")
        if self.result_contract_version != self.result.contract_version:
            raise ValueError("tool result contract version binding mismatch")
        if self.supported_contract_version != self.result_contract_version:
            raise ValueError("tool supported contract version binding mismatch")
        if (
            self.result.status.value == "success"
            and self.requested_contract_version is not None
            and self.result_contract_version != self.requested_contract_version
        ):
            raise ValueError("successful tool result contract version mismatch")
        return self


class ToolResearchOutput(PhasePayload):
    bindings: tuple[ToolExecutionBinding, ...]
    data_quality: tuple[str, ...] = ()
    usage_delta: UsageDelta = Field(default_factory=UsageDelta)
    budget_failure: BudgetFailure | None = None
    retry_classifications: tuple[RetryClassification | None, ...] = ()

    @model_validator(mode="after")
    def retry_classification_count_matches_bindings(self) -> ToolResearchOutput:
        if len(self.retry_classifications) != len(self.bindings):
            raise ValueError("one retry classification is required per tool binding")
        return self


class CritiqueInput(PhasePayload):
    case: CaseInput
    reports: tuple[AgentReport, ...]
    tool_results: tuple[ToolResult, ...]
    evidence_ids: tuple[str, ...]
    existing_disputes: tuple[Dispute, ...] = ()
    include_objections: bool = True
    include_provider_claims: bool = True
    include_auxiliary_findings: bool = True


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
