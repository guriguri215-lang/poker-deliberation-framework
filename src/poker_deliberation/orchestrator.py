"""Deterministic workflow owner for state, artifacts, budgets, and synthesis."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
from datetime import UTC, datetime
from pathlib import Path
from threading import Thread
from typing import Any

from poker_deliberation.agents import select_roles
from poker_deliberation.approvals import ApprovalLedger, requires_human_approval
from poker_deliberation.config import AppConfig
from poker_deliberation.isolation import IsolationError, build_blind_decision_context
from poker_deliberation.providers import AgentProvider, LocalProvider, ProviderControl
from poker_deliberation.reporting import render_markdown
from poker_deliberation.research import EvidenceLedger
from poker_deliberation.results_orientation import detect_results_orientation
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    AgentExecutionRecord,
    AgentExecutionStatus,
    AgentReport,
    ApprovalProposal,
    ApprovalRequest,
    ApprovalStatus,
    CaseInput,
    Claim,
    ClaimCheck,
    ConfidenceGrade,
    Dispute,
    EpistemicLabel,
    EvidenceRecord,
    Exactness,
    FinalReport,
    SecurityEvent,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.security import isolate_prompt_injection, redact_sensitive, screen_case
from poker_deliberation.state_machine import RunState, WorkflowStateMachine
from poker_deliberation.storage import RunStore
from poker_deliberation.tools import ToolRegistry, default_registry


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(4)}"


def _lookup_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def _agent_context(
    case: CaseInput,
    role: str,
    registered_tools: frozenset[str],
) -> AgentContext:
    common: dict[str, Any] = {"kind": case.kind, "objective": case.objective}
    if role == "intake":
        context = AgentContext(**common, raw_text=case.raw_text, hand=case.hand)
        return AgentContext.model_validate(isolate_prompt_injection(context))
    if role == "math-auditor":
        raw_tool_inputs = case.metadata.get("tool_inputs", {})
        requested_tools = [name for name in case.requested_tools if name in registered_tools]
        tool_inputs = (
            {name: raw_tool_inputs[name] for name in requested_tools if name in raw_tool_inputs}
            if isinstance(raw_tool_inputs, dict)
            else {}
        )
        context = AgentContext(
            **common,
            hand=case.hand,
            claims=case.claims,
            assumptions=case.assumptions,
            requested_tools=requested_tools,
            tool_inputs=tool_inputs,
        )
        return AgentContext.model_validate(isolate_prompt_injection(context))
    if role == "evidence-researcher":
        context = AgentContext(**common, claims=case.claims, evidence=case.evidence)
        return AgentContext.model_validate(isolate_prompt_injection(context))
    if case.kind == "hand" and role == "strategy-analyst":
        context = AgentContext(
            kind=case.kind,
            objective="decision_quality_baseline",
            blind_decision_context=build_blind_decision_context(case),
        )
        return AgentContext.model_validate(isolate_prompt_injection(context))
    strategy_text = None
    if case.kind == "strategy" and role in {"strategy-analyst", "skeptic", "adjudicator"}:
        strategy_text = "\n".join(line.rstrip() for line in (case.raw_text or "").splitlines())
    context = AgentContext(
        **common,
        strategy_text=strategy_text,
        hand=case.hand,
        claims=case.claims,
        assumptions=case.assumptions,
    )
    return AgentContext.model_validate(isolate_prompt_injection(context))


def _context_sha256(context: AgentContext) -> str:
    serialized = json.dumps(
        context.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _analyze_with_timeout(
    provider: AgentProvider,
    context: AgentContext,
    assignment: AgentAssignment,
    timeout_seconds: float,
) -> AgentReport:
    results: list[AgentReport] = []
    errors: list[Exception] = []
    control = ProviderControl(timeout_seconds=timeout_seconds)

    def invoke() -> None:
        try:
            results.append(provider.analyze(context, assignment, control))
        except Exception as exc:  # provider boundary converts failures to structured limitations
            errors.append(exc)

    thread = Thread(target=invoke, daemon=True, name=f"provider-{assignment.agent_role}")
    thread.start()
    thread.join(timeout_seconds)
    if thread.is_alive():
        control.cancel()
        thread.join(min(0.5, max(0.05, timeout_seconds)))
        suffix = " and ignored cancellation" if thread.is_alive() else " and was cancelled"
        raise TimeoutError(f"provider exceeded deadline {timeout_seconds} seconds{suffix}")
    if errors:
        raise errors[0]
    if not results:
        raise RuntimeError("provider returned no report")
    return results[0]


class Orchestrator:
    def __init__(
        self,
        config: AppConfig | None = None,
        registry: ToolRegistry | None = None,
        provider: AgentProvider | None = None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.registry = registry or default_registry(
            max_payload_bytes=self.config.budgets.max_output_bytes,
            max_output_bytes=self.config.budgets.max_output_bytes,
            max_duration_seconds=min(30.0, self.config.budgets.max_runtime_seconds),
        )
        self.provider = provider or LocalProvider()
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
        safe_case = redact_sensitive(case, enabled=not self.config.record_sensitive_data)
        self.store.write_json(actual_run_id, "input.json", safe_case)
        self.store.write_text(actual_run_id, "evidence.jsonl", "")
        known_claim_ids = {claim.claim_id for claim in case.claims}
        for record in case.evidence:
            unknown_claims = set(record.supported_claim_ids) - known_claim_ids
            if unknown_claims:
                data_quality.append(
                    f"{record.evidence_id}: unknown supported claim IDs: {sorted(unknown_claims)}"
                )
                continue
            evidence.add(record)
            self.store.append_jsonl(
                actual_run_id,
                "evidence.jsonl",
                redact_sensitive(record, enabled=not self.config.record_sensitive_data),
            )
        raw_approvals = case.metadata.get("approval_requests", [])
        if isinstance(raw_approvals, list):
            for raw_approval in raw_approvals:
                if isinstance(raw_approval, dict):
                    proposal_fields = ApprovalProposal.model_fields
                    injected_fields = set(raw_approval) - set(proposal_fields)
                    proposal_payload = {
                        key: value for key, value in raw_approval.items() if key in proposal_fields
                    }
                    try:
                        proposal = ApprovalProposal.model_validate(proposal_payload)
                    except ValueError as exc:
                        data_quality.append(f"invalid approval proposal: {exc}")
                        proposal = ApprovalProposal(
                            requested_action="review malformed external-action request",
                            reason="approval metadata was malformed and must fail closed",
                            expected_benefit="preserve approval integrity",
                            risks=["untrusted approval metadata"],
                            cost_or_resource_estimate="unknown",
                            alternatives=["reject the malformed request"],
                            effect_of_declining="no external action is performed",
                        )
                    if injected_fields:
                        data_quality.append(
                            "input-supplied approval decision fields were ignored: "
                            f"{sorted(injected_fields)}"
                        )
                    if not requires_human_approval(proposal.action_category):
                        raise ValueError("approval proposal category is not a sensitive action")
                    approval_request = ApprovalRequest.model_validate(proposal.model_dump())
                    approvals.add(
                        ApprovalRequest.model_validate(
                            redact_sensitive(
                                approval_request,
                                enabled=not self.config.record_sensitive_data,
                            )
                        )
                    )
        elif raw_approvals:
            data_quality.append("metadata.approval_requests must be a list")
        normalization_warnings = case.metadata.get("normalization_warnings", [])
        if isinstance(normalization_warnings, list):
            data_quality.extend(str(item) for item in normalization_warnings)

        machine.transition(RunState.NORMALIZE, "input parsed into CaseInput")
        normalized = safe_case
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
                validation = self.registry.execute(
                    "hand_validator", case.hand.model_dump(mode="json")
                )
                validation = ToolResult.model_validate(
                    redact_sensitive(validation, enabled=not self.config.record_sensitive_data)
                )
                tool_results.append(validation)
                if not validation.output.get("valid", False):
                    data_quality.extend(map(str, validation.output.get("errors", [])))
                data_quality.extend(map(str, validation.output.get("warnings", [])))
        if case.raw_text and case.hand is None and case.kind == "hand":
            data_quality.append("不足情報を捏造せず、自由文を未正規化入力として保存しました。")

        machine.transition(RunState.TASK_ROUTING, "roles selected by case kind")
        assignments = select_roles(case)
        self.store.write_json(actual_run_id, "assignments.json", assignments)
        reports: list[AgentReport] = []
        if case.kind != "calculation":
            machine.transition(RunState.INDEPENDENT_ANALYSIS, "selected roles run independently")
            for assignment in assignments:
                remaining_runtime = max(
                    0.001,
                    self.config.budgets.max_runtime_seconds - machine.elapsed_seconds,
                )
                started_at = datetime.now(UTC)
                try:
                    context = _agent_context(
                        case,
                        assignment.agent_role,
                        frozenset(self.registry.names()),
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
                context_hash = _context_sha256(context)
                provider_info = self.provider.availability()
                execution_status = AgentExecutionStatus.COMPLETED
                execution_error: str | None = None
                try:
                    agent_report = _analyze_with_timeout(
                        self.provider,
                        context,
                        assignment,
                        min(30.0, remaining_runtime),
                    )
                except TimeoutError as exc:
                    execution_records.append(
                        AgentExecutionRecord(
                            assignment_id=assignment.assignment_id,
                            agent_role=assignment.agent_role,
                            provider=provider_info.provider,
                            provider_version=provider_info.version,
                            model=getattr(self.provider, "model", None),
                            reasoning_effort=getattr(self.provider, "reasoning_effort", None),
                            allowed_tools=[
                                name
                                for name in context.requested_tools
                                if name in self.registry.names()
                            ],
                            context_sha256=context_hash,
                            status=AgentExecutionStatus.FAILED,
                            started_at=started_at,
                            completed_at=datetime.now(UTC),
                            error=str(exc),
                        )
                    )
                    data_quality.append(str(exc))
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS, "provider deadline exceeded"
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
                except Exception as exc:
                    execution_status = AgentExecutionStatus.FALLBACK
                    execution_error = f"{type(exc).__name__}: {exc}"
                    data_quality.append(
                        f"provider {assignment.agent_role} failed: {type(exc).__name__}: {exc}"
                    )
                    agent_report = AgentReport(
                        agent_role=assignment.agent_role,
                        task=assignment.task,
                        uncertainties=["Provider failed; no specialist conclusion was accepted."],
                        confidence=ConfidenceGrade.D,
                    )
                agent_report = AgentReport.model_validate(
                    redact_sensitive(agent_report, enabled=not self.config.record_sensitive_data)
                )
                report_size = len(
                    json.dumps(agent_report.model_dump(mode="json"), ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
                if report_size > self.config.budgets.max_output_bytes:
                    data_quality.append(
                        f"provider {assignment.agent_role} output exceeded the hard byte limit"
                    )
                    agent_report = AgentReport(
                        agent_role=assignment.agent_role,
                        task=assignment.task,
                        uncertainties=["Oversized provider output was rejected."],
                        confidence=ConfidenceGrade.D,
                    )
                    execution_status = AgentExecutionStatus.FAILED
                    execution_error = "provider output exceeded the hard byte limit"
                execution_records.append(
                    AgentExecutionRecord(
                        assignment_id=assignment.assignment_id,
                        agent_role=assignment.agent_role,
                        provider=provider_info.provider,
                        provider_version=provider_info.version,
                        model=getattr(self.provider, "model", None),
                        reasoning_effort=getattr(self.provider, "reasoning_effort", None),
                        allowed_tools=[
                            name
                            for name in context.requested_tools
                            if name in self.registry.names()
                        ],
                        context_sha256=context_hash,
                        status=execution_status,
                        started_at=started_at,
                        completed_at=datetime.now(UTC),
                        error=execution_error,
                    )
                )
                reports.append(agent_report)
                self.store.write_json(
                    actual_run_id,
                    f"agent_reports/{agent_report.report_id}.json",
                    agent_report,
                )
                for objection in agent_report.objections:
                    if case.claims:
                        disputes.append(
                            Dispute(
                                claim_ids=[case.claims[0].claim_id],
                                issue=objection,
                                positions=[objection],
                                unresolved=True,
                            )
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
        for tool_name in case.requested_tools:
            if tool_name in already_run and tool_name == "hand_validator":
                continue
            payload = tool_inputs.get(tool_name, {})
            if not isinstance(payload, dict):
                payload = {}
            result = self.registry.execute(tool_name, payload)
            result = ToolResult.model_validate(
                redact_sensitive(result, enabled=not self.config.record_sensitive_data)
            )
            tool_results.append(result)
        for result in tool_results:
            self.store.write_json(actual_run_id, f"tool_results/{result.result_id}.json", result)
            self.store.write_json(
                actual_run_id, f"tool_results/{result.result_id}.input.json", result.input
            )
        valid_evidence_ids = {record.evidence_id for record in evidence.all()}
        valid_tool_result_ids = {
            result.result_id for result in tool_results if result.status is ToolStatus.SUCCESS
        }
        for agent_report in reports:
            report_evidence_ids = set(agent_report.evidence_ids)
            report_tool_ids = set(agent_report.tool_result_ids)
            invalid_refs = (report_evidence_ids - valid_evidence_ids) | (
                report_tool_ids - valid_tool_result_ids
            )
            if invalid_refs:
                data_quality.append(
                    f"{agent_report.report_id}: unknown provider evidence/tool IDs: "
                    f"{sorted(invalid_refs)}"
                )
            for provider_claim in agent_report.claims:
                claim_refs = (
                    set(provider_claim.evidence_ids) | report_evidence_ids | report_tool_ids
                )
                valid_refs = claim_refs & (valid_evidence_ids | valid_tool_result_ids)
                if valid_refs and not invalid_refs:
                    disputes.append(
                        Dispute(
                            claim_ids=[provider_claim.claim_id],
                            issue=(
                                "Provider claim references valid artifacts but lacks "
                                "typed adjudication"
                            ),
                            positions=[provider_claim.text],
                            resolution_basis=[f"valid references: {sorted(valid_refs)}"],
                            unresolved=True,
                        )
                    )
                else:
                    disputes.append(
                        Dispute(
                            claim_ids=[provider_claim.claim_id],
                            issue=(
                                "Provider claim lacks valid claim-level evidence or "
                                "tool verification"
                            ),
                            positions=[provider_claim.text],
                            resolution=(
                                "Rejected from the adjudicated conclusion and labeled UNKNOWN"
                            ),
                            resolution_basis=["provider output is untrusted input"],
                            unresolved=False,
                        )
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
        for result in tool_results:
            if result.status is ToolStatus.FAILED:
                data_quality.append(f"{result.tool_name} failed: {result.error}")
            if result.status is ToolStatus.UNAVAILABLE:
                data_quality.append(f"{result.tool_name} unavailable: {result.error}")
        rationale_sources: list[tuple[str, str]] = []
        if case.raw_text:
            rationale_sources.append(("input-raw", case.raw_text))
        rationale_sources.extend((claim.claim_id, claim.text) for claim in case.claims)
        for agent_report in reports:
            rationale_sources.extend((claim.claim_id, claim.text) for claim in agent_report.claims)
            rationale_sources.extend(
                (f"{agent_report.report_id}-conclusion", text) for text in agent_report.conclusions
            )
        for source_id, text in rationale_sources:
            for finding in detect_results_orientation(text):
                disputes.append(
                    Dispute(
                        claim_ids=[source_id],
                        issue="結果論を意思決定の正しさの根拠として使用しています。",
                        positions=[text],
                        resolution=finding.correction,
                        resolution_basis=[f"deterministic rule: {finding.rule_id}"],
                        unresolved=False,
                    )
                )
                data_quality.append(
                    f"{source_id}: 結果論の論拠を棄却し、意思決定時点の情報で再評価が必要です。"
                )
        machine.transition(RunState.ADJUDICATION, "evidence strength, not vote count, used")
        claim_assessments = self._adjudicate_claims(case, tool_results, data_quality)
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

    def _adjudicate_claims(
        self,
        case: CaseInput,
        tool_results: list[ToolResult],
        data_quality: list[str],
    ) -> list[Claim]:
        assessments = list(case.claims)
        checks = case.metadata.get("claim_checks", [])
        if not isinstance(checks, list):
            data_quality.append("metadata.claim_checks must be a list")
            return assessments
        by_tool: dict[str, list[ToolResult]] = {}
        for result in tool_results:
            by_tool.setdefault(result.tool_name, []).append(result)
        known_claim_ids = {claim.claim_id for claim in case.claims}
        for raw_check in checks:
            if not isinstance(raw_check, dict):
                data_quality.append("a claim check was not an object")
                continue
            try:
                check = ClaimCheck.model_validate(raw_check)
            except ValueError as exc:
                data_quality.append(f"invalid claim check: {exc}")
                continue
            claim_id = check.claim_id
            if claim_id not in known_claim_ids:
                data_quality.append(f"{claim_id}: claim check references an unknown claim")
                continue
            tool_name = check.tool_name
            candidates = by_tool.get(tool_name, [])
            if not candidates or candidates[-1].status is not ToolStatus.SUCCESS:
                data_quality.append(f"{claim_id}: verification tool {tool_name!r} did not succeed")
                continue
            result = candidates[-1]
            try:
                calculated = float(_lookup_path(result.output, check.output_path))
                claimed = check.claimed_value
                tolerance = check.tolerance
            except (KeyError, TypeError, ValueError) as exc:
                data_quality.append(f"{claim_id}: invalid claim check: {exc}")
                continue
            if not math.isfinite(calculated):
                data_quality.append(f"{claim_id}: calculated claim value is not finite")
                continue
            exact_result = result.exactness is Exactness.EXACT
            if exact_result:
                agrees = abs(calculated - claimed) <= tolerance
                verdict = "一致します" if agrees else "一致せず、訂正が必要です"
                text = f"{claim_id}: USER_CLAIM={claimed} は CALCULATED={calculated} と{verdict}。"
                label = EpistemicLabel.CALCULATED
                confidence = ConfidenceGrade.A
                approximation_limits: list[str] = []
            else:
                interval = result.confidence_interval
                if (
                    interval is not None
                    and all(math.isfinite(bound) for bound in interval)
                    and interval[0] <= interval[1]
                ):
                    in_interval = interval[0] - tolerance <= claimed <= interval[1] + tolerance
                    verdict = (
                        f"95%信頼区間[{interval[0]}, {interval[1]}]内です"
                        if in_interval
                        else f"95%信頼区間[{interval[0]}, {interval[1]}]外です"
                    )
                    interval_limit = "信頼区間はツールが報告した近似誤差範囲です。"
                else:
                    agrees = abs(calculated - claimed) <= tolerance
                    verdict = "点推定と一致します" if agrees else "点推定と一致しません"
                    interval_limit = "この近似結果には利用可能な信頼区間がありません。"
                text = (
                    f"{claim_id}: USER_CLAIM={claimed} は ESTIMATE(point)={calculated}について"
                    f"{verdict}。近似値のためexactな訂正とは扱いません。"
                )
                label = EpistemicLabel.ESTIMATE
                confidence = ConfidenceGrade.C
                approximation_limits = [
                    f"{tool_name} のexactnessは {result.exactness.value} です。",
                    interval_limit,
                ]
            assessments.append(
                Claim(
                    claim_id=f"adjudication-{claim_id}",
                    text=text,
                    label=label,
                    confidence=confidence,
                    limitations=[
                        f"検証範囲は {tool_name}.{check.output_path} の数値比較です。",
                        *([f"単位: {check.unit}"] if check.unit else []),
                        *approximation_limits,
                    ],
                )
            )
        if case.claims and not checks:
            data_quality.append(
                "ユーザー主張は入力として保存しましたが、検証条件がないため真偽未判定です。"
            )
        return assessments

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
        corrections = [claim for claim in claim_assessments if "訂正が必要" in claim.text]
        failed = [result for result in tool_results if result.status is ToolStatus.FAILED]
        successes = [result for result in tool_results if result.status is ToolStatus.SUCCESS]
        if any(event.blocked for event in security_events):
            conclusion = "".join(
                (
                    "このフレームワークは事後検討専用です。",
                    "禁止用途に該当するため分析を実行しませんでした。",
                )
            )
        elif machine.state is RunState.HUMAN_REVIEW_REQUIRED:
            conclusion = "外部操作は未実行です。人間の承認または拒否を待っています。"
        elif machine.state is RunState.FAILED_WITH_LIMITATIONS:
            conclusion = "実行予算または安全上の制限に達したため、制限付きで終了しました。"
        elif corrections:
            conclusion = "ユーザー主張に、再現可能なローカル計算に基づく訂正が必要です。"
        elif case.kind == "hand" and data_quality:
            conclusion = "ハンド入力に矛盾または不足があるため、戦略結論を断定しません。"
        elif failed:
            conclusion = "一部の計算が失敗したため、利用可能な結果と制限だけを返します。"
        elif successes:
            conclusion = "指定されたローカル検証・計算を完了しました。"
        else:
            conclusion = "正確な結論に必要な検証入力が不足しているため、断定を保留します。"
        exact_successes = [r for r in successes if r.exactness is Exactness.EXACT]
        adjudicated_claim_ids = {
            claim.claim_id.removeprefix("adjudication-")
            for claim in claim_assessments
            if claim.claim_id.startswith("adjudication-")
            and claim.label is EpistemicLabel.CALCULATED
            and claim.confidence is ConfidenceGrade.A
        }
        has_unverified_material_claim = any(
            claim.claim_id not in adjudicated_claim_ids for claim in case.claims
        )
        if machine.state is RunState.HUMAN_REVIEW_REQUIRED:
            confidence = ConfidenceGrade.D
        elif (
            successes
            and len(exact_successes) == len(successes)
            and not failed
            and not data_quality
            and not has_unverified_material_claim
            and not any(dispute.unresolved for dispute in disputes)
        ):
            confidence = ConfidenceGrade.A
        elif (
            successes
            and not failed
            and not data_quality
            and not has_unverified_material_claim
            and not any(dispute.unresolved for dispute in disputes)
        ):
            confidence = ConfidenceGrade.B
        else:
            confidence = ConfidenceGrade.C
        analysis_sections = [
            {
                "title": report.agent_role,
                "epistemic_status": EpistemicLabel.UNKNOWN.value,
                "unverified_conclusions": report.conclusions,
                "unverified_claims": [claim.text for claim in report.claims],
                "uncertainties": report.uncertainties,
                "objections": report.objections,
                "unresolved_questions": report.unresolved_questions,
            }
            for report in reports
        ]
        reproduction_steps = [
            "argv-json: "
            + json.dumps(
                [
                    "poker-deliberate",
                    "calculate",
                    result.tool_name,
                    "--analysis-scope",
                    "retrospective",
                    "--input",
                    str(
                        self.store.run_dir(run_id)
                        / "tool_results"
                        / f"{result.result_id}.input.json"
                    ),
                ],
                ensure_ascii=False,
            )
            for result in tool_results
            if result.reproduce_command is not None
        ]
        limitations = list(dict.fromkeys(data_quality))
        if not self.provider.availability().available:
            limitations.append(self.provider.availability().reason)
        if case.kind in {"hand", "strategy"}:
            limitations.append(
                "外部ソルバーの実行・収束確認なしにGTOまたは均衡を主張していません。"
            )
        report = FinalReport(
            run_id=run_id,
            run_status=(
                "approval_required"
                if machine.state is RunState.HUMAN_REVIEW_REQUIRED
                else "failed_with_limitations"
                if machine.state is RunState.FAILED_WITH_LIMITATIONS
                else "completed"
            ),
            conclusion=conclusion,
            reconstructed_input=redact_sensitive(
                case, enabled=not self.config.record_sensitive_data
            ),
            data_quality=list(dict.fromkeys(data_quality)),
            claim_assessments=claim_assessments,
            analysis_sections=analysis_sections,
            agent_execution_records=execution_records,
            security_events=security_events,
            tool_results=tool_results,
            alternatives=[],
            sensitivity=[
                result.output for result in tool_results if result.tool_name == "sensitivity"
            ],
            disputes=disputes,
            evidence=evidence_records,
            reproduction_steps=reproduction_steps,
            approvals=[
                ApprovalRequest.model_validate(item)
                for item in redact_sensitive(
                    approvals.all(), enabled=not self.config.record_sensitive_data
                )
            ],
            confidence=confidence,
            limitations=list(dict.fromkeys(limitations)),
        )
        report = FinalReport.model_validate(
            redact_sensitive(report, enabled=not self.config.record_sensitive_data)
        )
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
