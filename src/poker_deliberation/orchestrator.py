"""Deterministic workflow owner for state, artifacts, budgets, and synthesis."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from poker_deliberation.agents import select_roles
from poker_deliberation.approvals import ApprovalLedger
from poker_deliberation.config import AppConfig
from poker_deliberation.context_lifecycle import (
    new_attempt_id,
    new_context_id,
)
from poker_deliberation.isolation import IsolationError, build_blind_decision_context
from poker_deliberation.phases import (
    AdjudicationService,
    AnalysisExecutor,
    ContextBuildService,
    CritiqueService,
    IntakeValidationService,
    NormalizationService,
    PhaseContractError,
    PhaseId,
    RoutingService,
    SynthesisService,
    ToolResearchExecutor,
    canonical_sha256,
    make_phase_request,
    revalidate_outcome,
)
from poker_deliberation.phases.models import (
    AdjudicationInput,
    AdjudicationOutput,
    AnalysisInput,
    AnalysisOutput,
    ContextBuildInput,
    ContextBuildOutput,
    CritiqueInput,
    CritiqueOutput,
    IntakeValidationInput,
    IntakeValidationOutput,
    NormalizationInput,
    NormalizationOutput,
    ProviderSnapshot,
    RoutingInput,
    RoutingOutput,
    SynthesisInput,
    SynthesisOutput,
    ToolResearchInput,
    ToolResearchOutput,
)
from poker_deliberation.providers import AgentProvider, LocalProvider
from poker_deliberation.reporting import render_markdown
from poker_deliberation.research import EvidenceLedger
from poker_deliberation.schemas import (
    AgentExecutionRecord,
    AgentReport,
    ApprovalRequest,
    ApprovalStatus,
    CaseInput,
    Claim,
    Dispute,
    EvidenceRecord,
    FinalReport,
    SecurityEvent,
    ToolRequest,
    ToolResult,
)
from poker_deliberation.security import redact_sensitive, screen_case
from poker_deliberation.state_machine import RunState, WorkflowStateMachine
from poker_deliberation.storage import RunStore
from poker_deliberation.tools import ToolRegistry, default_registry


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(4)}"


def _new_phase_attempt_id(phase_id: PhaseId) -> str:
    return f"phase-{phase_id.value}-{secrets.token_hex(8)}"


def _new_internal_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


class Orchestrator:
    def __init__(
        self,
        config: AppConfig | None = None,
        registry: ToolRegistry | None = None,
        provider: AgentProvider | None = None,
        context_clock: Callable[[], datetime] | None = None,
        *,
        intake_service: IntakeValidationService | None = None,
        normalization_service: NormalizationService | None = None,
        routing_service: RoutingService | None = None,
        context_build_service: ContextBuildService | None = None,
        analysis_executor: AnalysisExecutor | None = None,
        tool_research_executor: ToolResearchExecutor | None = None,
        critique_service: CritiqueService | None = None,
        adjudication_service: AdjudicationService | None = None,
        synthesis_service: SynthesisService | None = None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.registry = registry or default_registry(
            max_payload_bytes=self.config.budgets.max_output_bytes,
            max_output_bytes=self.config.budgets.max_output_bytes,
            max_duration_seconds=min(30.0, self.config.budgets.max_runtime_seconds),
        )
        self.provider = provider or LocalProvider()
        self.context_clock = context_clock or (lambda: datetime.now(UTC))
        self.intake_service = intake_service or IntakeValidationService()
        self.normalization_service = normalization_service or NormalizationService()
        self.routing_service = routing_service or RoutingService()
        self.context_build_service = context_build_service or ContextBuildService(
            blind_context_builder=build_blind_decision_context
        )
        self.analysis_executor = analysis_executor or AnalysisExecutor(
            self.provider,
            context_clock=self.context_clock,
            record_clock=lambda: datetime.now(UTC),
        )
        self.tool_research_executor = tool_research_executor or ToolResearchExecutor(
            self.registry,
            record_sensitive_data=self.config.record_sensitive_data,
        )
        self.critique_service = critique_service or CritiqueService()
        self.adjudication_service = adjudication_service or AdjudicationService()
        self.synthesis_service = synthesis_service or SynthesisService()
        self.phase_policy_snapshot_hash = canonical_sha256(
            {
                "record_sensitive_data": self.config.record_sensitive_data,
                "registered_tools": self.registry.names(),
                "context_retention_policy": "attempt-memory-only-v1",
                "execution": "serial",
            }
        )
        self.store = RunStore(
            self.config.runs_dir,
            max_artifact_bytes=self.config.budgets.max_output_bytes,
            max_run_bytes=self.config.budgets.max_run_bytes,
        )

    def run(self, case: CaseInput, *, run_id: str | None = None) -> FinalReport:
        case = CaseInput.model_validate(case.model_dump(mode="python"))
        actual_run_id = run_id or new_run_id()
        self.store.create_run(actual_run_id)
        machine = WorkflowStateMachine(self.config.budgets)
        approvals = ApprovalLedger()
        disputes: list[Dispute] = []
        tool_results: list[ToolResult] = []
        data_quality: list[str] = []
        execution_records: list[AgentExecutionRecord] = []
        security_events: list[SecurityEvent] = []
        evidence = EvidenceLedger()
        self.store.ensure_directory(actual_run_id, "agent_reports")
        self.store.ensure_directory(actual_run_id, "tool_results")
        raw_approvals = case.metadata.get("approval_requests", [])
        fallback_approval_ids = (
            tuple(_new_internal_id("approval") for _ in raw_approvals)
            if isinstance(raw_approvals, list)
            else ()
        )
        intake_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.INTAKE_VALIDATION,
            attempt_id=_new_phase_attempt_id(PhaseId.INTAKE_VALIDATION),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=IntakeValidationInput(
                case=case,
                record_sensitive_data=self.config.record_sensitive_data,
                fallback_approval_ids=fallback_approval_ids,
            ),
        )
        intake_outcome = revalidate_outcome(
            intake_request,
            self.intake_service.run(intake_request),
            output_type=IntakeValidationOutput,
        )
        if intake_outcome.output is None:
            raise PhaseContractError("intake validation returned no output")
        intake = intake_outcome.output
        case = intake.case
        safe_case = intake.safe_case
        data_quality.extend(intake.data_quality)
        self.store.write_json(actual_run_id, "input.json", safe_case)
        self.store.write_text(actual_run_id, "evidence.jsonl", "")
        for record in intake.accepted_evidence:
            evidence.add(record)
            self.store.append_jsonl(
                actual_run_id,
                "evidence.jsonl",
                redact_sensitive(record, enabled=not self.config.record_sensitive_data),
            )
        for proposal in intake.approval_proposals:
            approval_request = ApprovalRequest.model_validate(proposal.model_dump())
            approvals.add(
                ApprovalRequest.model_validate(
                    redact_sensitive(
                        approval_request,
                        enabled=not self.config.record_sensitive_data,
                    )
                )
            )

        machine.transition(RunState.NORMALIZE, "input parsed into CaseInput")
        normalization_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.NORMALIZATION,
            attempt_id=_new_phase_attempt_id(PhaseId.NORMALIZATION),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=NormalizationInput(
                safe_case=safe_case,
                assumptions=tuple(
                    assumption.model_dump(mode="json") for assumption in case.assumptions
                ),
            ),
        )
        normalization_outcome = revalidate_outcome(
            normalization_request,
            self.normalization_service.run(normalization_request),
            output_type=NormalizationOutput,
        )
        if normalization_outcome.output is None:
            raise PhaseContractError("normalization returned no output")
        normalized = normalization_outcome.output.normalized_case
        self.store.write_json(actual_run_id, "normalized_case.json", normalized)
        self.store.write_json(
            actual_run_id,
            "assumptions.json",
            redact_sensitive(case.assumptions, enabled=not self.config.record_sensitive_data),
        )

        machine.transition(RunState.DATA_VALIDATION, "canonical schema validation completed")
        security_events = screen_case(case)
        self.store.write_json(actual_run_id, "security_events.json", security_events)
        if any(event.category == "prompt_injection" for event in security_events):
            data_quality.append(
                "プロンプトインジェクションらしき文字列を無害な入力として記録しました。"
            )
        if any(event.blocked for event in security_events):
            data_quality.append(
                "事後検討専用の範囲外です。リアルタイム支援、非公開カード取得、共謀、"
                "自動プレイ、検出回避には対応しません。"
            )
            machine.transition(RunState.FAILED_WITH_LIMITATIONS, "prohibited use refused")
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                list(case.claims),
                [],
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
            )
        if case.kind == "hand":
            if case.hand is None:
                data_quality.append(
                    "自由文だけでは正確なポット・スタック・合法性を確定できません。CanonicalHandが必要です。"
                )
            else:
                hand_request = ToolRequest(
                    request_id=_new_internal_id("tool-request"),
                    tool_name="hand_validator",
                    input=case.hand.model_dump(mode="json"),
                )
                tool_phase_request = make_phase_request(
                    run_id=actual_run_id,
                    phase_id=PhaseId.TOOL_RESEARCH,
                    attempt_id=_new_phase_attempt_id(PhaseId.TOOL_RESEARCH),
                    policy_snapshot_hash=self.phase_policy_snapshot_hash,
                    input_value=ToolResearchInput(
                        requests=(hand_request,),
                        fallback_result_ids=(_new_internal_id("tool-result"),),
                    ),
                )
                tool_phase_outcome = revalidate_outcome(
                    tool_phase_request,
                    self.tool_research_executor.run(tool_phase_request),
                    output_type=ToolResearchOutput,
                )
                if tool_phase_outcome.output is None:
                    raise PhaseContractError("hand validation returned no output")
                data_quality.extend(tool_phase_outcome.output.data_quality)
                validation = tool_phase_outcome.output.bindings[0].result
                tool_results.append(validation)
                if not validation.output.get("valid", False):
                    data_quality.extend(map(str, validation.output.get("errors", [])))
                data_quality.extend(map(str, validation.output.get("warnings", [])))
        if case.raw_text and case.hand is None and case.kind == "hand":
            data_quality.append("不足情報を捏造せず、自由文を未正規化入力として保存しました。")

        machine.transition(RunState.TASK_ROUTING, "roles selected by case kind")
        registered_tools = tuple(self.registry.names())
        routing_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.ROUTING,
            attempt_id=_new_phase_attempt_id(PhaseId.ROUTING),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=RoutingInput(
                case_kind=case.kind,
                role_snapshot=tuple(select_roles(case)),
                registered_tools=registered_tools,
            ),
        )
        routing_outcome = revalidate_outcome(
            routing_request,
            self.routing_service.run(routing_request),
            output_type=RoutingOutput,
        )
        if routing_outcome.output is None:
            raise PhaseContractError("routing returned no output")
        assignments = list(routing_outcome.output.assignments)
        self.store.write_json(actual_run_id, "assignments.json", assignments)
        reports: list[AgentReport] = []
        if case.kind != "calculation":
            machine.transition(RunState.INDEPENDENT_ANALYSIS, "selected roles run independently")
            report_ids: set[str] = set()
            for index, assignment in enumerate(assignments):
                remaining_runtime = max(
                    0.001,
                    self.config.budgets.max_runtime_seconds - machine.elapsed_seconds,
                )
                started_at = datetime.now(UTC)
                provider_timeout = min(30.0, remaining_runtime)
                lifecycle_now = self.context_clock()
                expected_context_id = new_context_id()
                expected_attempt_id = new_attempt_id()
                context_request = make_phase_request(
                    run_id=actual_run_id,
                    phase_id=PhaseId.CONTEXT_BUILD,
                    attempt_id=_new_phase_attempt_id(PhaseId.CONTEXT_BUILD),
                    policy_snapshot_hash=self.phase_policy_snapshot_hash,
                    context_ids=(expected_context_id,),
                    input_value=ContextBuildInput(
                        case=case,
                        assignment=assignment,
                        registered_tools=registered_tools,
                        created_at=lifecycle_now,
                        expires_at=lifecycle_now + timedelta(seconds=provider_timeout),
                        context_id=expected_context_id,
                        context_attempt_id=expected_attempt_id,
                    ),
                )
                try:
                    context_outcome = revalidate_outcome(
                        context_request,
                        self.context_build_service.run(context_request),
                        output_type=ContextBuildOutput,
                    )
                except IsolationError as exc:
                    data_quality.append(f"blind decision isolation failed: {exc}")
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "blind decision isolation failed",
                    )
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                if context_outcome.output is None or len(context_outcome.output.dispatches) != 1:
                    raise PhaseContractError("context build returned an invalid dispatch batch")
                dispatch = context_outcome.output.dispatches[0]
                assignments[index] = dispatch.assignment
                self.store.write_json(actual_run_id, "assignments.json", assignments)
                analysis_request = make_phase_request(
                    run_id=actual_run_id,
                    phase_id=PhaseId.ANALYSIS,
                    attempt_id=_new_phase_attempt_id(PhaseId.ANALYSIS),
                    policy_snapshot_hash=self.phase_policy_snapshot_hash,
                    context_ids=(expected_context_id,),
                    input_value=AnalysisInput(
                        dispatch=dispatch,
                        provider_timeout_seconds=provider_timeout,
                        registered_tools=registered_tools,
                        max_output_bytes=self.config.budgets.max_output_bytes,
                        record_sensitive_data=self.config.record_sensitive_data,
                        started_at=started_at,
                        execution_id=_new_internal_id("execution"),
                        fallback_report_id=_new_internal_id("report"),
                        existing_report_ids=tuple(sorted(report_ids)),
                    ),
                )
                analysis_outcome = revalidate_outcome(
                    analysis_request,
                    self.analysis_executor.run(analysis_request),
                    output_type=AnalysisOutput,
                )
                if analysis_outcome.output is None:
                    raise PhaseContractError("analysis returned no output")
                analysis = analysis_outcome.output
                execution_records.append(analysis.execution_record)
                data_quality.extend(analysis.data_quality)
                if analysis.timed_out:
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "provider deadline exceeded",
                    )
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                reports.append(analysis.report)
                report_ids.add(analysis.report.report_id)
                self.store.write_json(
                    actual_run_id,
                    f"agent_reports/{analysis.report.report_id}.json",
                    analysis.report,
                )
            if not machine.enforce_runtime():
                data_quality.append("maximum runtime exceeded after provider analysis")
                return self._synthesize(
                    actual_run_id,
                    case,
                    data_quality,
                    list(case.claims),
                    reports,
                    execution_records,
                    tool_results,
                    disputes,
                    evidence.all(),
                    approvals,
                    security_events,
                    completed=False,
                    machine=machine,
                )
            machine.transition(RunState.TOOL_AND_RESEARCH, "independent reports collected")
        else:
            machine.transition(
                RunState.TOOL_AND_RESEARCH, "calculation case routes directly to tools"
            )
        tool_inputs = case.metadata.get("tool_inputs", {})
        if not isinstance(tool_inputs, dict):
            data_quality.append(
                "metadata.tool_inputs must be an object; requested tools were not run"
            )
            tool_inputs = {}
        already_run = {result.tool_name for result in tool_results}
        requested_tool_calls: list[ToolRequest] = []
        for tool_name in case.requested_tools:
            if tool_name in already_run and tool_name == "hand_validator":
                continue
            payload = tool_inputs.get(tool_name, {})
            if not isinstance(payload, dict):
                payload = {}
            requested_tool_calls.append(
                ToolRequest(
                    request_id=_new_internal_id("tool-request"),
                    tool_name=tool_name,
                    input=payload,
                )
            )
        requested_tools_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.TOOL_RESEARCH,
            attempt_id=_new_phase_attempt_id(PhaseId.TOOL_RESEARCH),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=ToolResearchInput(
                requests=tuple(requested_tool_calls),
                start_ordinal=len(tool_results),
                existing_result_ids=tuple(result.result_id for result in tool_results),
                fallback_result_ids=tuple(
                    _new_internal_id("tool-result") for _ in requested_tool_calls
                ),
            ),
        )
        requested_tools_outcome = revalidate_outcome(
            requested_tools_request,
            self.tool_research_executor.run(requested_tools_request),
            output_type=ToolResearchOutput,
        )
        if requested_tools_outcome.output is None:
            raise PhaseContractError("tool research returned no output")
        data_quality.extend(requested_tools_outcome.output.data_quality)
        tool_results.extend(binding.result for binding in requested_tools_outcome.output.bindings)
        for result in tool_results:
            self.store.write_json(actual_run_id, f"tool_results/{result.result_id}.json", result)
            self.store.write_json(
                actual_run_id, f"tool_results/{result.result_id}.input.json", result.input
            )
        if not machine.enforce_runtime():
            data_quality.append("maximum runtime exceeded after tool execution")
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                list(case.claims),
                reports,
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
            )

        machine.transition(RunState.CRITIQUE, "tool failures and unsupported claims checked")
        critique_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.CRITIQUE,
            attempt_id=_new_phase_attempt_id(PhaseId.CRITIQUE),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=CritiqueInput(
                case=case,
                reports=tuple(reports),
                tool_results=tuple(tool_results),
                evidence_ids=tuple(record.evidence_id for record in evidence.all()),
                existing_disputes=tuple(disputes),
            ),
        )
        critique_outcome = revalidate_outcome(
            critique_request,
            self.critique_service.run(critique_request),
            output_type=CritiqueOutput,
        )
        if critique_outcome.output is None:
            raise PhaseContractError("critique returned no output")
        disputes = list(critique_outcome.output.disputes)
        data_quality.extend(critique_outcome.output.data_quality)

        machine.transition(RunState.ADJUDICATION, "evidence strength, not vote count, used")
        adjudication_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.ADJUDICATION,
            attempt_id=_new_phase_attempt_id(PhaseId.ADJUDICATION),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=AdjudicationInput(
                case=case,
                tool_results=tuple(tool_results),
            ),
        )
        adjudication_outcome = revalidate_outcome(
            adjudication_request,
            self.adjudication_service.run(adjudication_request),
            output_type=AdjudicationOutput,
        )
        if adjudication_outcome.output is None:
            raise PhaseContractError("adjudication returned no output")
        claim_assessments = list(adjudication_outcome.output.claim_assessments)
        data_quality.extend(adjudication_outcome.output.data_quality)
        known_evidence_ids = {record.evidence_id for record in evidence.all()}
        for claim in case.claims:
            missing_evidence = set(claim.evidence_ids) - known_evidence_ids
            if missing_evidence:
                data_quality.append(
                    f"{claim.claim_id}: unknown evidence IDs: {sorted(missing_evidence)}"
                )

        if approvals.pending():
            machine.transition(RunState.HUMAN_REVIEW_REQUIRED, "sensitive action needs approval")
            self._write_common_artifacts(actual_run_id, machine, approvals, disputes)
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                claim_assessments,
                reports,
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
            )
        machine.transition(RunState.FINAL_SYNTHESIS, "no pending approval blocks synthesis")
        final_report = self._synthesize(
            actual_run_id,
            case,
            data_quality,
            claim_assessments,
            reports,
            execution_records,
            tool_results,
            disputes,
            evidence.all(),
            approvals,
            security_events,
            completed=True,
            machine=machine,
        )
        return final_report

    def _write_common_artifacts(
        self,
        run_id: str,
        machine: WorkflowStateMachine,
        approvals: ApprovalLedger,
        disputes: list[Dispute],
    ) -> None:
        self.store.write_json(run_id, "state.json", machine.snapshot())
        self.store.write_json(
            run_id,
            "approvals.json",
            redact_sensitive(approvals.all(), enabled=not self.config.record_sensitive_data),
        )
        self.store.write_json(run_id, "disputes.json", disputes)

    def _synthesize(
        self,
        run_id: str,
        case: CaseInput,
        data_quality: list[str],
        claim_assessments: list[Claim],
        reports: list[AgentReport],
        execution_records: list[AgentExecutionRecord],
        tool_results: list[ToolResult],
        disputes: list[Dispute],
        evidence_records: list[EvidenceRecord],
        approvals: ApprovalLedger,
        security_events: list[SecurityEvent],
        *,
        completed: bool,
        machine: WorkflowStateMachine,
    ) -> FinalReport:
        provider_info = self.provider.availability()
        provider_reason = (
            self.provider.availability().reason
            if not provider_info.available
            else provider_info.reason
        )
        synthesis_request = make_phase_request(
            run_id=run_id,
            phase_id=PhaseId.SYNTHESIS,
            attempt_id=_new_phase_attempt_id(PhaseId.SYNTHESIS),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=SynthesisInput(
                run_id=run_id,
                machine_state=machine.state.value,
                completed=completed,
                case=case,
                data_quality=tuple(data_quality),
                claim_assessments=tuple(claim_assessments),
                reports=tuple(reports),
                execution_records=tuple(execution_records),
                tool_results=tuple(tool_results),
                disputes=tuple(disputes),
                evidence_records=tuple(evidence_records),
                approvals=tuple(ApprovalRequest.model_validate(item) for item in approvals.all()),
                security_events=tuple(security_events),
                provider_snapshot=ProviderSnapshot(
                    available=provider_info.available,
                    reason=provider_reason,
                ),
                tool_input_artifact_paths=tuple(
                    str(
                        self.store.run_dir(run_id)
                        / "tool_results"
                        / f"{result.result_id}.input.json"
                    )
                    for result in tool_results
                ),
                record_sensitive_data=self.config.record_sensitive_data,
                generated_at=datetime.now(UTC),
            ),
        )
        synthesis_outcome = revalidate_outcome(
            synthesis_request,
            self.synthesis_service.run(synthesis_request),
            output_type=SynthesisOutput,
        )
        if synthesis_outcome.output is None:
            raise PhaseContractError("synthesis returned no output")
        expected_intents = (
            ("agent_execution_records", "agent_execution_records.json", "application/json"),
            ("security_events", "security_events.json", "application/json"),
            ("state", "state.json", "application/json"),
            ("approvals", "approvals.json", "application/json"),
            ("disputes", "disputes.json", "application/json"),
            ("final_report_json", "final_report.json", "application/json"),
            ("final_report_markdown", "final_report.md", "text/markdown"),
        )
        actual_intents = tuple(
            (intent.kind.value, intent.relative_path, intent.media_type)
            for intent in synthesis_outcome.artifact_intents
        )
        if actual_intents != expected_intents:
            raise PhaseContractError("synthesis artifact intent allowlist mismatch")
        expected_next_state = "completed" if completed else None
        if synthesis_outcome.requested_next_state != expected_next_state:
            raise PhaseContractError("synthesis requested an illegal next state")
        report = synthesis_outcome.output.report
        self.store.write_json(run_id, "agent_execution_records.json", execution_records)
        self.store.write_json(run_id, "security_events.json", security_events)
        if completed and not machine.terminal:
            machine.transition(RunState.COMPLETED, "final report artifacts written")
        self._write_common_artifacts(run_id, machine, approvals, disputes)
        self.store.write_json(run_id, "final_report.json", report)
        self.store.write_text(run_id, "final_report.md", render_markdown(report))
        return report

    def load_report(self, run_id: str) -> FinalReport:
        return FinalReport.model_validate(self.store.read_json(run_id, "final_report.json"))

    def resume(
        self,
        run_id: str,
        *,
        approve_ids: list[str] | None = None,
        reject_ids: list[str] | None = None,
        reason: str = "human decision recorded by CLI",
    ) -> FinalReport:
        snapshot = self.store.read_json(run_id, "state.json")
        machine = WorkflowStateMachine.from_snapshot(self.config.budgets, snapshot)
        if machine.state is not RunState.HUMAN_REVIEW_REQUIRED:
            return self.load_report(run_id)
        requests = [
            ApprovalRequest.model_validate(item)
            for item in self.store.read_json(run_id, "approvals.json")
        ]
        ledger = ApprovalLedger(requests)
        for approval_id in approve_ids or []:
            ledger.decide(
                approval_id,
                True,
                str(redact_sensitive(reason, enabled=not self.config.record_sensitive_data)),
            )
        for approval_id in reject_ids or []:
            ledger.decide(
                approval_id,
                False,
                str(redact_sensitive(reason, enabled=not self.config.record_sensitive_data)),
            )
        report = self.load_report(run_id)
        report.approvals = ledger.all()
        report.generated_at = datetime.now(UTC)
        if ledger.pending():
            report.run_status = "approval_required"
            self.store.write_json(run_id, "approvals.json", ledger.all())
            self.store.write_json(run_id, "final_report.json", report)
            self.store.write_text(run_id, "final_report.md", render_markdown(report))
            return report
        rejected = [item for item in ledger.all() if item.status is ApprovalStatus.REJECTED]
        approved = [item for item in ledger.all() if item.status is ApprovalStatus.APPROVED]
        if approved:
            machine.transition(
                RunState.FAILED_WITH_LIMITATIONS,
                "approval recorded but no external action executor is configured in the MVP",
            )
            report.conclusion = (
                "承認は記録しましたが、MVPは外部操作を自動実行しないため制限付きで終了します。"
            )
            report.run_status = "failed_with_limitations"
            report.limitations.append(
                "承認済み外部操作は、人間が再現コマンドを確認して別途実行する必要があります。"
            )
        else:
            machine.transition(
                RunState.FINAL_SYNTHESIS, "rejected actions replaced by the safe no-action path"
            )
            machine.transition(RunState.COMPLETED, "safe alternative report finalized")
            report.conclusion = (
                "承認が拒否されたため、外部操作を行わない安全な代替結果を確定しました。"
            )
            report.run_status = "completed"
            if rejected:
                report.limitations.append("拒否された外部操作に依存する分析は未実行です。")
        self.store.write_json(run_id, "state.json", machine.snapshot())
        self.store.write_json(run_id, "approvals.json", ledger.all())
        self.store.write_json(run_id, "final_report.json", report)
        self.store.write_text(run_id, "final_report.md", render_markdown(report))
        return report

    def report_path(self, run_id: str, format_name: str) -> Path:
        suffix = "json" if format_name == "json" else "md"
        return self.store.run_dir(run_id) / f"final_report.{suffix}"
