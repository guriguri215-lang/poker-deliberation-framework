"""Deterministic phase services with no persistence or external-effect ownership."""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from datetime import datetime
from typing import Any, Generic, TypeVar

from poker_deliberation.context_lifecycle import (
    build_context_envelope,
    context_payload,
)
from poker_deliberation.isolation import build_blind_decision_context
from poker_deliberation.normalization import (
    NormalizationResultV1,
    normalization_diagnostic_text,
    verify_normalization_binding,
)
from poker_deliberation.phases.contracts import (
    ArtifactIntent,
    ArtifactKind,
    PhaseId,
    PhaseOutcome,
    PhaseRequest,
    revalidate_request,
    successful_outcome,
)
from poker_deliberation.phases.models import (
    AdjudicationInput,
    AdjudicationOutput,
    ApprovalProposalV2,
    ContextBuildInput,
    ContextBuildOutput,
    ContextDispatch,
    CritiqueInput,
    CritiqueOutput,
    IntakeValidationInput,
    IntakeValidationOutput,
    NormalizationInput,
    NormalizationOutput,
    RoutingInput,
    RoutingOutput,
    SynthesisInput,
    SynthesisOutput,
)
from poker_deliberation.results_orientation import detect_results_orientation
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    ApprovalProposal,
    ApprovalRequest,
    CaseInput,
    Claim,
    ClaimCheck,
    ConfidenceGrade,
    Dispute,
    EpistemicLabel,
    FinalReport,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.security import isolate_prompt_injection, redact_sensitive

InputT = TypeVar("InputT")
OutputT = TypeVar("OutputT")

_UNVERIFIED_CLAIM_WARNING = (
    "ユーザー主張は入力として保存しましたが、検証条件がないため真偽未判定です。"
)


def _constant_clock(value: datetime) -> Callable[[], datetime]:
    def read() -> datetime:
        return value

    return read


def _approval_json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"unsupported approval proposal value: {type(value).__name__}")


def is_verified_claim_correction(claim: Claim) -> bool:
    """Return whether adjudication produced an exact, high-confidence correction."""

    return (
        claim.claim_id.startswith("adjudication-")
        and claim.label is EpistemicLabel.CALCULATED
        and claim.confidence is ConfidenceGrade.A
        and "訂正が必要" in claim.text
    )


class PurePhaseService(Generic[InputT, OutputT]):
    phase_id: PhaseId
    input_type: type[InputT]

    def isolate(self, request: PhaseRequest[Any]) -> PhaseRequest[InputT]:
        return revalidate_request(
            request,
            phase_id=self.phase_id,
            input_type=self.input_type,
        )


class IntakeValidationService(PurePhaseService[IntakeValidationInput, IntakeValidationOutput]):
    phase_id = PhaseId.INTAKE_VALIDATION
    input_type = IntakeValidationInput

    def run(
        self, request: PhaseRequest[IntakeValidationInput]
    ) -> PhaseOutcome[IntakeValidationOutput]:
        isolated = self.isolate(request)
        value = isolated.input
        case = CaseInput.model_validate(value.case.model_dump(mode="python"))
        safe_case = CaseInput.model_validate(
            redact_sensitive(case, enabled=not value.record_sensitive_data)
        )
        warnings: list[str] = []
        known_claim_ids = {claim.claim_id for claim in case.claims}
        accepted_evidence = []
        for record in case.evidence:
            unknown_claims = set(record.supported_claim_ids) - known_claim_ids
            if unknown_claims:
                warnings.append(
                    f"{record.evidence_id}: unknown supported claim IDs: {sorted(unknown_claims)}"
                )
            else:
                accepted_evidence.append(record.model_copy(deep=True))

        approval_proposals: list[ApprovalProposal | ApprovalProposalV2] = []
        raw_approvals = safe_case.metadata.get("approval_requests", [])
        if isinstance(raw_approvals, list):
            fallback_ids = iter(value.fallback_approval_ids)
            for raw_approval in raw_approvals:
                fallback_id = next(fallback_ids, None)
                if not isinstance(raw_approval, dict):
                    continue
                supplied_version = raw_approval.get("schema_version")
                if supplied_version is not None:
                    if supplied_version != "2.0.0":
                        warnings.append("unsupported approval proposal schema_version")
                        continue
                    try:
                        proposal_v2 = ApprovalProposalV2.model_validate_json(
                            json.dumps(
                                raw_approval,
                                ensure_ascii=False,
                                allow_nan=False,
                                default=_approval_json_default,
                            )
                        )
                    except ValueError as exc:
                        warnings.append(f"invalid V2 approval proposal: {exc}")
                        continue
                    if (
                        proposal_v2.action_plan.action_category
                        not in value.sensitive_action_categories
                    ):
                        raise ValueError("approval proposal category is not a sensitive action")
                    approval_proposals.append(proposal_v2)
                    continue
                proposal_fields = ApprovalProposal.model_fields
                injected_fields = set(raw_approval) - set(proposal_fields)
                proposal_payload = {
                    key: item for key, item in raw_approval.items() if key in proposal_fields
                }
                if "approval_id" not in proposal_payload and fallback_id is not None:
                    proposal_payload["approval_id"] = fallback_id
                try:
                    proposal = ApprovalProposal.model_validate(proposal_payload)
                except ValueError as exc:
                    warnings.append(f"invalid approval proposal: {exc}")
                    if fallback_id is None:
                        warnings.append(
                            "malformed approval proposal lacked an injected fallback ID"
                        )
                        continue
                    proposal = ApprovalProposal(
                        approval_id=fallback_id,
                        requested_action="review malformed external-action request",
                        reason="approval metadata was malformed and must fail closed",
                        expected_benefit="preserve approval integrity",
                        risks=["untrusted approval metadata"],
                        cost_or_resource_estimate="unknown",
                        alternatives=["reject the malformed request"],
                        effect_of_declining="no external action is performed",
                    )
                if injected_fields:
                    warnings.append(
                        "input-supplied approval decision fields were ignored: "
                        f"{sorted(injected_fields)}"
                    )
                if proposal.action_category not in value.sensitive_action_categories:
                    raise ValueError("approval proposal category is not a sensitive action")
                approval_proposals.append(proposal)
        elif raw_approvals:
            warnings.append("metadata.approval_requests must be a list")
        normalization_warnings = case.metadata.get("normalization_warnings", [])
        if isinstance(normalization_warnings, list):
            warnings.extend(str(item) for item in normalization_warnings)
        output = IntakeValidationOutput(
            case=case,
            safe_case=safe_case,
            accepted_evidence=tuple(accepted_evidence),
            approval_proposals=tuple(approval_proposals),
            security_events=tuple(event.model_copy(deep=True) for event in value.security_events),
            data_quality=tuple(warnings),
        )
        return successful_outcome(isolated, output, warnings=output.data_quality)


class NormalizationService(PurePhaseService[NormalizationInput, NormalizationOutput]):
    phase_id = PhaseId.NORMALIZATION
    input_type = NormalizationInput

    def run(self, request: PhaseRequest[NormalizationInput]) -> PhaseOutcome[NormalizationOutput]:
        isolated = self.isolate(request)
        value = isolated.input
        normalized_case = CaseInput.model_validate(value.safe_case.model_dump(mode="python"))
        normalization = (
            None
            if value.normalization is None
            else NormalizationResultV1.model_validate(
                value.normalization.model_dump(mode="python"),
                strict=True,
            )
        )
        warnings = list(value.warnings)
        if normalization is not None:
            verify_normalization_binding(normalized_case, normalized_case, normalization)
            warnings.extend(
                normalization_diagnostic_text(item) for item in normalization.diagnostics
            )
        output = NormalizationOutput(
            normalized_case=normalized_case,
            normalization=normalization,
            assumptions=tuple(dict(item) for item in value.assumptions),
            warnings=tuple(warnings),
        )
        return successful_outcome(isolated, output, warnings=output.warnings)


class RoutingService(PurePhaseService[RoutingInput, RoutingOutput]):
    phase_id = PhaseId.ROUTING
    input_type = RoutingInput

    def run(self, request: PhaseRequest[RoutingInput]) -> PhaseOutcome[RoutingOutput]:
        isolated = self.isolate(request)
        value = isolated.input
        expected_roles = (
            ("math-auditor", "report-writer")
            if value.case_kind == "calculation"
            else ("intake", "strategy-analyst", "math-auditor", "skeptic", "adjudicator")
            if value.case_kind == "hand"
            else ("math-auditor", "evidence-researcher", "skeptic", "adjudicator")
            if value.case_kind == "claim"
            else ("strategy-analyst", "math-auditor", "skeptic", "adjudicator")
        )
        roles = tuple(assignment.agent_role for assignment in value.role_snapshot)
        if roles != expected_roles:
            raise ValueError("role snapshot does not match the canonical serial route")
        assignment_ids = [assignment.assignment_id for assignment in value.role_snapshot]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError("assignment IDs must be unique")
        output = RoutingOutput(
            assignments=tuple(
                AgentAssignment.model_validate(assignment.model_dump(mode="python"))
                for assignment in value.role_snapshot
            )
        )
        return successful_outcome(isolated, output)


def build_agent_context(
    case: CaseInput,
    role: str,
    registered_tools: frozenset[str],
    blind_context_builder: Callable[[CaseInput], Any] = build_blind_decision_context,
) -> AgentContext:
    """Build the canonical provider context for one routed role."""

    if role not in {
        "intake",
        "strategy-analyst",
        "math-auditor",
        "evidence-researcher",
        "skeptic",
        "adjudicator",
        "report-writer",
    }:
        raise ValueError("unknown agent role")
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
            blind_decision_context=blind_context_builder(case),
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


class ContextBuildService(PurePhaseService[ContextBuildInput, ContextBuildOutput]):
    phase_id = PhaseId.CONTEXT_BUILD
    input_type = ContextBuildInput

    def __init__(
        self,
        *,
        blind_context_builder: Callable[[CaseInput], Any] = build_blind_decision_context,
    ) -> None:
        self.blind_context_builder = blind_context_builder

    def run(self, request: PhaseRequest[ContextBuildInput]) -> PhaseOutcome[ContextBuildOutput]:
        isolated = self.isolate(request)
        value = isolated.input
        if isolated.context_ids != (value.context_id,):
            raise ValueError("context build request does not match its context ID")
        context = build_agent_context(
            value.case,
            value.assignment.agent_role,
            frozenset(value.registered_tools),
            self.blind_context_builder,
        )
        assignment = AgentAssignment.model_validate(
            value.assignment.model_copy(
                update={"context_keys": sorted(context_payload(context))},
                deep=True,
            ).model_dump(mode="python")
        )
        envelope = build_context_envelope(
            context,
            assignment,
            run_id=isolated.run_id,
            expires_at=value.expires_at,
            clock=_constant_clock(value.created_at),
            context_id=value.context_id,
            attempt_id=value.context_attempt_id,
        )
        dispatch = ContextDispatch(
            assignment=assignment,
            context=context,
            envelope=envelope,
        )
        output = ContextBuildOutput(dispatches=(dispatch,))
        return successful_outcome(isolated, output)


def _dispute_id(kind: str, ordinal: int, value: Any) -> str:
    from poker_deliberation.phases.contracts import canonical_sha256

    return f"dispute-{canonical_sha256({'kind': kind, 'ordinal': ordinal, 'value': value})[:12]}"


class CritiqueService(PurePhaseService[CritiqueInput, CritiqueOutput]):
    phase_id = PhaseId.CRITIQUE
    input_type = CritiqueInput

    def run(self, request: PhaseRequest[CritiqueInput]) -> PhaseOutcome[CritiqueOutput]:
        isolated = self.isolate(request)
        value = isolated.input
        disputes = [item.model_copy(deep=True) for item in value.existing_disputes]
        warnings: list[str] = []
        valid_evidence_ids = set(value.evidence_ids)
        valid_tool_result_ids = {
            result.result_id for result in value.tool_results if result.status is ToolStatus.SUCCESS
        }
        ordinal = len(disputes)
        if value.include_objections:
            for report in value.reports:
                for objection in report.objections:
                    if value.case.claims:
                        disputes.append(
                            Dispute(
                                dispute_id=_dispute_id("objection", ordinal, objection),
                                claim_ids=[value.case.claims[0].claim_id],
                                issue=objection,
                                positions=[objection],
                                unresolved=True,
                            )
                        )
                        ordinal += 1
        if value.include_provider_claims:
            for report in value.reports:
                report_evidence_ids = set(report.evidence_ids)
                report_tool_ids = set(report.tool_result_ids)
                invalid_refs = (report_evidence_ids - valid_evidence_ids) | (
                    report_tool_ids - valid_tool_result_ids
                )
                if invalid_refs:
                    warnings.append(
                        f"{report.report_id}: unknown provider evidence/tool IDs: "
                        f"{sorted(invalid_refs)}"
                    )
                for provider_claim in report.claims:
                    claim_refs = (
                        set(provider_claim.evidence_ids) | report_evidence_ids | report_tool_ids
                    )
                    valid_refs = claim_refs & (valid_evidence_ids | valid_tool_result_ids)
                    if valid_refs and not invalid_refs:
                        dispute = Dispute(
                            dispute_id=_dispute_id("provider-claim", ordinal, provider_claim.text),
                            claim_ids=[provider_claim.claim_id],
                            issue=(
                                "Provider claim references valid artifacts but lacks typed "
                                "adjudication"
                            ),
                            positions=[provider_claim.text],
                            resolution_basis=[f"valid references: {sorted(valid_refs)}"],
                            unresolved=True,
                        )
                    else:
                        dispute = Dispute(
                            dispute_id=_dispute_id("provider-claim", ordinal, provider_claim.text),
                            claim_ids=[provider_claim.claim_id],
                            issue=(
                                "Provider claim lacks valid claim-level evidence or tool "
                                "verification"
                            ),
                            positions=[provider_claim.text],
                            resolution=(
                                "Rejected from the adjudicated conclusion and labeled UNKNOWN"
                            ),
                            resolution_basis=["provider output is untrusted input"],
                            unresolved=False,
                        )
                    disputes.append(dispute)
                    ordinal += 1
        if value.include_auxiliary_findings:
            for result in value.tool_results:
                if result.status is ToolStatus.FAILED:
                    warnings.append(f"{result.tool_name} failed: {result.error}")
                if result.status is ToolStatus.UNAVAILABLE:
                    warnings.append(f"{result.tool_name} unavailable: {result.error}")
            rationale_sources: list[tuple[str, str]] = []
            if value.case.raw_text:
                rationale_sources.append(("input-raw", value.case.raw_text))
            rationale_sources.extend((claim.claim_id, claim.text) for claim in value.case.claims)
            for report in value.reports:
                rationale_sources.extend((claim.claim_id, claim.text) for claim in report.claims)
                rationale_sources.extend(
                    (f"{report.report_id}-conclusion", text) for text in report.conclusions
                )
            for source_id, text in rationale_sources:
                for finding in detect_results_orientation(text):
                    disputes.append(
                        Dispute(
                            dispute_id=_dispute_id(
                                "results-orientation", ordinal, (source_id, text, finding.rule_id)
                            ),
                            claim_ids=[source_id],
                            issue="結果論を意思決定の正しさの根拠として使用しています。",
                            positions=[text],
                            resolution=finding.correction,
                            resolution_basis=[f"deterministic rule: {finding.rule_id}"],
                            unresolved=False,
                        )
                    )
                    ordinal += 1
                    warnings.append(
                        f"{source_id}: 結果論の論拠を棄却し、意思決定時点の情報で再評価が必要です。"
                    )
        output = CritiqueOutput(disputes=tuple(disputes), data_quality=tuple(warnings))
        return successful_outcome(isolated, output, warnings=output.data_quality)


def _lookup_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def _tool_comparison_tolerance(
    result: ToolResult,
    claimed: float,
    calculated: float,
    requested: float | None,
) -> float:
    if requested is not None:
        return requested
    numeric = result.numeric_exactness
    if numeric in {NumericalExactness.EXACT, NumericalExactness.EXACT_UNDER_MODEL}:
        return 0.0
    if numeric is NumericalExactness.APPROXIMATE:
        return 0.0
    if numeric is not NumericalExactness.FLOATING_VERIFIED or result.verification is None:
        raise ValueError("no comparison tolerance is available for this result")
    policy = result.verification.tolerance
    scale = max(abs(claimed), abs(calculated), 1.0)
    if policy.kind == "absolute" and policy.absolute is not None:
        return policy.absolute
    if policy.kind == "relative" and policy.relative is not None:
        return policy.relative * scale
    if policy.kind == "absolute-or-relative":
        if policy.absolute is None or policy.relative is None:
            raise ValueError("incomplete absolute-or-relative tolerance policy")
        return max(policy.absolute, policy.relative * scale)
    if policy.kind == "ulp" and policy.ulps is not None:
        return math.ulp(scale) * policy.ulps
    if policy.kind == "caller-supplied":
        resolved = result.input.get("tolerance", result.output.get("verification_tolerance"))
        if isinstance(resolved, (int, float)) and math.isfinite(float(resolved)) and resolved >= 0:
            return float(resolved)
    raise ValueError("the tool-specific tolerance cannot be resolved")


class AdjudicationService(PurePhaseService[AdjudicationInput, AdjudicationOutput]):
    phase_id = PhaseId.ADJUDICATION
    input_type = AdjudicationInput

    def run(self, request: PhaseRequest[AdjudicationInput]) -> PhaseOutcome[AdjudicationOutput]:
        isolated = self.isolate(request)
        value = isolated.input
        assessments = [claim.model_copy(deep=True) for claim in value.case.claims]
        warnings: list[str] = []
        checks = value.case.metadata.get("claim_checks", [])
        if not isinstance(checks, list):
            output = AdjudicationOutput(
                claim_assessments=tuple(assessments),
                data_quality=("metadata.claim_checks must be a list",),
            )
            return successful_outcome(isolated, output, warnings=output.data_quality)
        by_tool: dict[str, list[ToolResult]] = {}
        for result in value.tool_results:
            by_tool.setdefault(result.tool_name, []).append(result)
        known_claim_ids = {claim.claim_id for claim in value.case.claims}
        for raw_check in checks:
            if not isinstance(raw_check, dict):
                warnings.append("a claim check was not an object")
                continue
            try:
                check = ClaimCheck.model_validate(raw_check)
            except ValueError as exc:
                warnings.append(f"invalid claim check: {exc}")
                continue
            if check.claim_id not in known_claim_ids:
                warnings.append(f"{check.claim_id}: claim check references an unknown claim")
                continue
            candidates = by_tool.get(check.tool_name, [])
            if not candidates or candidates[-1].status is not ToolStatus.SUCCESS:
                warnings.append(
                    f"{check.claim_id}: verification tool {check.tool_name!r} did not succeed"
                )
                continue
            result = candidates[-1]
            try:
                calculated = float(_lookup_path(result.output, check.output_path))
                tolerance = _tool_comparison_tolerance(
                    result,
                    check.claimed_value,
                    calculated,
                    check.tolerance,
                )
            except (KeyError, TypeError, ValueError) as exc:
                warnings.append(f"{check.claim_id}: invalid claim check: {exc}")
                continue
            if not math.isfinite(calculated):
                warnings.append(f"{check.claim_id}: calculated claim value is not finite")
                continue
            verified_result = result.numeric_exactness in {
                NumericalExactness.EXACT,
                NumericalExactness.EXACT_UNDER_MODEL,
                NumericalExactness.FLOATING_VERIFIED,
            }
            if verified_result:
                agrees = abs(calculated - check.claimed_value) <= tolerance
                verdict = "一致します" if agrees else "一致せず、訂正が必要です"
                text = (
                    f"{check.claim_id}: USER_CLAIM={check.claimed_value} は "
                    f"CALCULATED={calculated} と{verdict}。"
                )
                label = EpistemicLabel.CALCULATED
                confidence = ConfidenceGrade.A
                approximation_limits = [
                    f"numeric_exactness: {result.numeric_exactness.value}",
                    f"comparison tolerance: {tolerance}",
                    *(
                        [f"model qualifier: {result.model_qualifier}"]
                        if result.model_qualifier
                        else []
                    ),
                ]
            else:
                interval = result.confidence_interval
                if (
                    interval is not None
                    and all(math.isfinite(bound) for bound in interval)
                    and interval[0] <= interval[1]
                ):
                    in_interval = (
                        interval[0] - tolerance <= check.claimed_value <= interval[1] + tolerance
                    )
                    verdict = (
                        f"95%信頼区間[{interval[0]}, {interval[1]}]内です"
                        if in_interval
                        else f"95%信頼区間[{interval[0]}, {interval[1]}]外です"
                    )
                    interval_limit = "信頼区間はツールが報告した近似誤差範囲です。"
                else:
                    agrees = abs(calculated - check.claimed_value) <= tolerance
                    verdict = "点推定と一致します" if agrees else "点推定と一致しません"
                    interval_limit = "この近似結果には利用可能な信頼区間がありません。"
                text = (
                    f"{check.claim_id}: USER_CLAIM={check.claimed_value} は "
                    f"ESTIMATE(point)={calculated}について{verdict}。"
                    "近似値のためexactな訂正とは扱いません。"
                )
                label = EpistemicLabel.ESTIMATE
                confidence = ConfidenceGrade.C
                approximation_limits = [
                    f"{check.tool_name} のnumeric_exactnessは "
                    f"{result.numeric_exactness.value} です。",
                    interval_limit,
                ]
            assessments.append(
                Claim(
                    claim_id=f"adjudication-{check.claim_id}",
                    text=text,
                    label=label,
                    confidence=confidence,
                    limitations=[
                        f"検証範囲は {check.tool_name}.{check.output_path} の数値比較です。",
                        *([f"単位: {check.unit}"] if check.unit else []),
                        *approximation_limits,
                    ],
                )
            )
        if value.case.claims and not checks:
            warnings.append(_UNVERIFIED_CLAIM_WARNING)
        output = AdjudicationOutput(
            claim_assessments=tuple(assessments), data_quality=tuple(warnings)
        )
        return successful_outcome(isolated, output, warnings=output.data_quality)


class SynthesisService(PurePhaseService[SynthesisInput, SynthesisOutput]):
    phase_id = PhaseId.SYNTHESIS
    input_type = SynthesisInput

    def run(self, request: PhaseRequest[SynthesisInput]) -> PhaseOutcome[SynthesisOutput]:
        isolated = self.isolate(request)
        value = isolated.input
        if value.run_id != isolated.run_id:
            raise ValueError("synthesis input run ID does not match its request")
        corrections = [
            claim for claim in value.claim_assessments if is_verified_claim_correction(claim)
        ]
        failed = [result for result in value.tool_results if result.status is ToolStatus.FAILED]
        successes = [result for result in value.tool_results if result.status is ToolStatus.SUCCESS]
        hand_input_quality_issues = [
            item for item in value.data_quality if item != _UNVERIFIED_CLAIM_WARNING
        ]
        if any(event.blocked for event in value.security_events):
            conclusion = (
                "このフレームワークは事後検討専用です。"
                "禁止用途に該当するため分析を実行しませんでした。"
            )
        elif value.machine_state == "HUMAN_REVIEW_REQUIRED":
            conclusion = "外部操作は未実行です。人間の承認または拒否を待っています。"
        elif value.machine_state == "FAILED_WITH_LIMITATIONS":
            conclusion = "実行予算または安全上の制限に達したため、制限付きで終了しました。"
        elif corrections:
            conclusion = "ユーザー主張に、再現可能なローカル計算に基づく訂正が必要です。"
        elif value.case.kind == "hand" and hand_input_quality_issues:
            conclusion = "ハンド入力に矛盾または不足があるため、戦略結論を断定しません。"
        elif failed:
            conclusion = "一部の計算が失敗したため、利用可能な結果と制限だけを返します。"
        elif successes:
            conclusion = "指定されたローカル検証・計算を完了しました。"
        else:
            conclusion = "正確な結論に必要な検証入力が不足しているため、断定を保留します。"
        verified_successes = [
            result
            for result in successes
            if result.numeric_exactness
            in {
                NumericalExactness.EXACT,
                NumericalExactness.EXACT_UNDER_MODEL,
                NumericalExactness.FLOATING_VERIFIED,
            }
        ]
        adjudicated_claim_ids = {
            claim.claim_id.removeprefix("adjudication-")
            for claim in value.claim_assessments
            if claim.claim_id.startswith("adjudication-")
            and claim.label is EpistemicLabel.CALCULATED
            and claim.confidence is ConfidenceGrade.A
        }
        has_unverified_material_claim = any(
            claim.claim_id not in adjudicated_claim_ids for claim in value.case.claims
        )
        if value.machine_state == "HUMAN_REVIEW_REQUIRED":
            confidence = ConfidenceGrade.D
        elif (
            successes
            and len(verified_successes) == len(successes)
            and not failed
            and not value.data_quality
            and not has_unverified_material_claim
            and not any(dispute.unresolved for dispute in value.disputes)
        ):
            confidence = ConfidenceGrade.A
        elif (
            successes
            and not failed
            and not value.data_quality
            and not has_unverified_material_claim
            and not any(dispute.unresolved for dispute in value.disputes)
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
            for report in value.reports
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
                    artifact_path,
                ],
                ensure_ascii=False,
            )
            for result, artifact_path in zip(
                value.tool_results,
                value.tool_input_artifact_paths,
                strict=True,
            )
            if result.reproduce_command is not None
        ]
        limitations = list(dict.fromkeys(value.data_quality))
        if not value.provider_snapshot.available:
            limitations.append(value.provider_snapshot.reason)
        if value.case.kind in {"hand", "strategy"}:
            limitations.append(
                "外部ソルバーの実行・収束確認なしにGTOまたは均衡を主張していません。"
            )
        report = FinalReport(
            run_id=value.run_id,
            run_status=(
                "approval_required"
                if value.machine_state == "HUMAN_REVIEW_REQUIRED"
                else "failed_with_limitations"
                if value.machine_state == "FAILED_WITH_LIMITATIONS"
                else "completed"
            ),
            conclusion=conclusion,
            reconstructed_input=redact_sensitive(
                value.case, enabled=not value.record_sensitive_data
            ),
            data_quality=list(dict.fromkeys(value.data_quality)),
            claim_assessments=list(value.claim_assessments),
            analysis_sections=analysis_sections,
            agent_execution_records=list(value.execution_records),
            security_events=list(value.security_events),
            tool_results=list(value.tool_results),
            alternatives=[],
            sensitivity=[
                result.output for result in value.tool_results if result.tool_name == "sensitivity"
            ],
            disputes=list(value.disputes),
            evidence=list(value.evidence_records),
            reproduction_steps=reproduction_steps,
            approvals=[
                ApprovalRequest.model_validate(item)
                for item in redact_sensitive(
                    list(value.approvals), enabled=not value.record_sensitive_data
                )
            ],
            confidence=confidence,
            limitations=list(dict.fromkeys(limitations)),
            generated_at=value.generated_at,
        )
        report = FinalReport.model_validate(
            redact_sensitive(report, enabled=not value.record_sensitive_data)
        )
        output = SynthesisOutput(report=report)
        intents = (
            ArtifactIntent(
                kind=ArtifactKind.AGENT_EXECUTION_RECORDS,
                relative_path="agent_execution_records.json",
                media_type="application/json",
            ),
            ArtifactIntent(
                kind=ArtifactKind.SECURITY_EVENTS,
                relative_path="security_events.json",
                media_type="application/json",
            ),
            ArtifactIntent(
                kind=ArtifactKind.STATE,
                relative_path="state.json",
                media_type="application/json",
            ),
            ArtifactIntent(
                kind=ArtifactKind.APPROVALS,
                relative_path="approvals.json",
                media_type="application/json",
            ),
            ArtifactIntent(
                kind=ArtifactKind.DISPUTES,
                relative_path="disputes.json",
                media_type="application/json",
            ),
            ArtifactIntent(
                kind=ArtifactKind.FINAL_REPORT_JSON,
                relative_path="final_report.json",
                media_type="application/json",
            ),
            ArtifactIntent(
                kind=ArtifactKind.FINAL_REPORT_MARKDOWN,
                relative_path="final_report.md",
                media_type="text/markdown",
            ),
        )
        return successful_outcome(
            isolated,
            output,
            requested_next_state="completed" if value.completed else None,
            artifact_intents=intents,
        )
