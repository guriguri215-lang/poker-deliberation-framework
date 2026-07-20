"""Explicit serial effect boundaries for provider analysis and local tools."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import datetime
from threading import Thread
from typing import Any

from poker_deliberation.context_lifecycle import (
    ContextEnvelope,
    ContextHandoffRefused,
    ContextLifecycleError,
    legacy_context_sha256,
    validate_context_envelope,
)
from poker_deliberation.phases.contracts import (
    PhaseContractError,
    PhaseId,
    PhaseOutcome,
    PhaseRequest,
    canonical_sha256,
    revalidate_request,
    successful_outcome,
)
from poker_deliberation.phases.models import (
    AnalysisInput,
    AnalysisOutput,
    ToolExecutionBinding,
    ToolResearchInput,
    ToolResearchOutput,
)
from poker_deliberation.providers import AgentProvider, ProviderControl
from poker_deliberation.schemas import (
    AgentContext,
    AgentExecutionRecord,
    AgentExecutionStatus,
    AgentReport,
    ConfidenceGrade,
    EpistemicLabel,
    Exactness,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.security import redact_sensitive
from poker_deliberation.tools import ToolRegistry

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class _ProviderAnalysisFailed(RuntimeError):
    def __init__(self, error_type: str) -> None:
        super().__init__("provider analyze failed")
        self.error_type = error_type


def _analyze_with_timeout(
    provider: AgentProvider,
    context: AgentContext,
    assignment: Any,
    timeout_seconds: float,
) -> AgentReport:
    results: list[AgentReport] = []
    errors: list[Exception] = []
    control = ProviderControl(timeout_seconds=timeout_seconds)

    def invoke() -> None:
        try:
            results.append(provider.analyze(context, assignment, control))
        except Exception as exc:  # the boundary classifies provider failures below
            errors.append(exc)

    worker = Thread(target=invoke, daemon=True, name=f"provider-{assignment.agent_role}")
    worker.start()
    worker.join(timeout_seconds)
    if worker.is_alive():
        control.cancel()
        worker.join(min(0.5, max(0.05, timeout_seconds)))
        suffix = " and ignored cancellation" if worker.is_alive() else " and was cancelled"
        raise TimeoutError(f"provider exceeded deadline {timeout_seconds} seconds{suffix}")
    if errors:
        raise errors[0]
    if not results:
        raise RuntimeError("provider returned no report")
    return results[0]


def _context_record_fields(
    envelope: ContextEnvelope,
    context: AgentContext,
) -> dict[str, Any]:
    return {
        "context_sha256": legacy_context_sha256(context),
        "context_id": envelope.lineage.context_id,
        "context_attempt_id": envelope.lineage.attempt_id,
        "parent_context_id": envelope.lineage.parent_context_id,
        "context_schema_version": envelope.schema_version,
        "context_classification": envelope.policy.classification.value,
        "context_payload_sha256": envelope.payload_sha256,
        "context_source_sha256": envelope.lineage.source_sha256,
        "context_policy_sha256": envelope.policy_sha256,
        "context_envelope_sha256": envelope.integrity_sha256,
        "context_expires_at": envelope.policy.expires_at,
        "context_producer_runtime": envelope.lineage.producer_runtime.value,
        "context_consumer_runtime": envelope.lineage.consumer_runtime.value,
    }


def _safe_unique_id(value: str, existing: set[str], label: str) -> None:
    if not _PORTABLE_ID.fullmatch(value) or value in existing:
        raise PhaseContractError(f"unsafe or duplicate {label}")


def validate_tool_research_output(
    request: PhaseRequest[ToolResearchInput],
    output: ToolResearchOutput,
) -> None:
    """Bind every result to the exact outer request before materialization."""

    value = request.input
    if len(output.bindings) != len(value.requests):
        raise PhaseContractError("tool binding count does not match phase request")
    seen_result_ids = set(value.existing_result_ids)
    for offset, (tool_request, binding) in enumerate(
        zip(value.requests, output.bindings, strict=True)
    ):
        expected_ordinal = value.start_ordinal + offset
        if (
            binding.run_id != request.run_id
            or binding.phase_attempt_id != request.attempt_id
            or binding.ordinal != expected_ordinal
            or binding.request != tool_request
            or binding.requested_contract_version != tool_request.contract_version
            or binding.request_input_sha256 != canonical_sha256(tool_request.input)
        ):
            raise PhaseContractError("tool execution binding correlation mismatch")
        _safe_unique_id(binding.result.result_id, seen_result_ids, "bound tool result ID")
        seen_result_ids.add(binding.result.result_id)


class AnalysisExecutor:
    """Run exactly one provider assignment without owning state or persistence."""

    def __init__(
        self,
        provider: AgentProvider,
        *,
        context_clock: Callable[[], datetime],
        record_clock: Callable[[], datetime],
    ) -> None:
        self.provider = provider
        self.context_clock = context_clock
        self.record_clock = record_clock

    def run(self, request: PhaseRequest[AnalysisInput]) -> PhaseOutcome[AnalysisOutput]:
        isolated = revalidate_request(
            request,
            phase_id=PhaseId.ANALYSIS,
            input_type=AnalysisInput,
        )
        value = isolated.input
        assignment = value.dispatch.assignment
        context = value.dispatch.context
        envelope = value.dispatch.envelope
        if isolated.context_ids != (envelope.lineage.context_id,):
            raise PhaseContractError("analysis request does not match its context ID")
        existing_report_ids = set(value.existing_report_ids)
        _safe_unique_id(value.fallback_report_id, existing_report_ids, "fallback report ID")
        _safe_unique_id(value.execution_id, set(), "execution ID")
        provider_info = self.provider.availability()
        execution_status = AgentExecutionStatus.COMPLETED
        execution_error: str | None = None
        warnings: list[str] = []
        timed_out = False
        try:
            provider_context = validate_context_envelope(
                envelope,
                assignment,
                run_id=isolated.run_id,
                expected_context_id=envelope.lineage.context_id,
                attempt_id=envelope.lineage.attempt_id,
                now=self.context_clock(),
            )
            if not provider_info.available:
                raise ContextHandoffRefused("provider is not available for context handoff")
            try:
                raw_report = _analyze_with_timeout(
                    self.provider,
                    provider_context,
                    assignment.model_copy(deep=True),
                    value.provider_timeout_seconds,
                )
            except TimeoutError:
                raise
            except Exception as exc:
                raise _ProviderAnalysisFailed(type(exc).__name__) from exc
            report = AgentReport.model_validate(
                raw_report.model_dump(mode="python")
                if isinstance(raw_report, AgentReport)
                else raw_report
            )
            normalized_claims = []
            for claim in report.claims:
                limitations = list(claim.limitations)
                if claim.label is not EpistemicLabel.USER_CLAIM or claim.confidence in {
                    ConfidenceGrade.A,
                    ConfidenceGrade.B,
                }:
                    limitations.append(
                        "Provider epistemic labels are untrusted; normalized to USER_CLAIM/C."
                    )
                normalized_claims.append(
                    claim.model_copy(
                        update={
                            "label": EpistemicLabel.USER_CLAIM,
                            "confidence": (
                                ConfidenceGrade.C
                                if claim.confidence in {ConfidenceGrade.A, ConfidenceGrade.B}
                                else claim.confidence
                            ),
                            "limitations": limitations,
                        },
                        deep=True,
                    )
                )
            report = AgentReport.model_validate(
                report.model_copy(update={"claims": normalized_claims}, deep=True).model_dump(
                    mode="python"
                )
            )
            if report.agent_role != assignment.agent_role or report.task != assignment.task:
                raise ContextLifecycleError("provider report correlation mismatch")
            _safe_unique_id(report.report_id, existing_report_ids, "provider report ID")
        except TimeoutError as exc:
            timed_out = True
            execution_status = AgentExecutionStatus.FAILED
            execution_error = str(exc)
            warnings.append(str(exc))
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Provider deadline was exceeded; no output was accepted."],
                confidence=ConfidenceGrade.D,
            )
        except ContextHandoffRefused as exc:
            execution_status = AgentExecutionStatus.REFUSED
            execution_error = str(exc)
            warnings.append(f"provider {assignment.agent_role} handoff refused: {exc}")
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Context handoff was refused by policy."],
                confidence=ConfidenceGrade.D,
            )
        except (ContextLifecycleError, PhaseContractError, ValueError) as exc:
            execution_status = AgentExecutionStatus.FAILED
            execution_error = str(exc)
            warnings.append(f"provider {assignment.agent_role} context rejected: {exc}")
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Context validation failed; no provider output was accepted."],
                confidence=ConfidenceGrade.D,
            )
        except _ProviderAnalysisFailed as exc:
            execution_status = AgentExecutionStatus.FALLBACK
            execution_error = f"{exc.error_type}: provider analyze failed"
            warnings.append(f"provider {assignment.agent_role} failed: {exc.error_type}")
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Provider failed; no specialist conclusion was accepted."],
                confidence=ConfidenceGrade.D,
            )
        except Exception as exc:
            execution_status = AgentExecutionStatus.FALLBACK
            execution_error = f"{type(exc).__name__}: provider analyze failed"
            warnings.append(f"provider {assignment.agent_role} failed: {type(exc).__name__}")
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Provider failed; no specialist conclusion was accepted."],
                confidence=ConfidenceGrade.D,
            )
        report = AgentReport.model_validate(
            redact_sensitive(report, enabled=not value.record_sensitive_data)
        )
        try:
            _safe_unique_id(report.report_id, existing_report_ids, "redacted report ID")
        except PhaseContractError as exc:
            warnings.append(f"provider {assignment.agent_role} report ID rejected: {exc}")
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Provider report identity became unsafe after redaction."],
                confidence=ConfidenceGrade.D,
            )
            execution_status = AgentExecutionStatus.FAILED
            execution_error = str(exc)
        report_size = len(
            json.dumps(report.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        )
        if report_size > value.max_output_bytes:
            warnings.append(f"provider {assignment.agent_role} output exceeded the hard byte limit")
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Oversized provider output was rejected."],
                confidence=ConfidenceGrade.D,
            )
            execution_status = AgentExecutionStatus.FAILED
            execution_error = "provider output exceeded the hard byte limit"
        execution_record = AgentExecutionRecord(
            execution_id=value.execution_id,
            assignment_id=assignment.assignment_id,
            agent_role=assignment.agent_role,
            provider=provider_info.provider,
            provider_version=provider_info.version,
            model=getattr(self.provider, "model", None),
            reasoning_effort=getattr(self.provider, "reasoning_effort", None),
            allowed_tools=[
                name for name in context.requested_tools if name in value.registered_tools
            ],
            **_context_record_fields(envelope, context),
            status=execution_status,
            started_at=value.started_at,
            completed_at=self.record_clock(),
            error=execution_error,
        )
        output = AnalysisOutput(
            assignment=assignment,
            context=context,
            envelope=envelope,
            report=report,
            execution_record=execution_record,
            data_quality=tuple(warnings),
            timed_out=timed_out,
        )
        return successful_outcome(
            isolated,
            output,
            warnings=output.data_quality,
            completed_with_failures=execution_status is not AgentExecutionStatus.COMPLETED,
        )


class ToolResearchExecutor:
    """Execute ordered local ToolRequests and retain full ToolResult metadata."""

    def __init__(self, registry: ToolRegistry, *, record_sensitive_data: bool) -> None:
        self.registry = registry
        self.record_sensitive_data = record_sensitive_data

    def run(self, request: PhaseRequest[ToolResearchInput]) -> PhaseOutcome[ToolResearchOutput]:
        isolated = revalidate_request(
            request,
            phase_id=PhaseId.TOOL_RESEARCH,
            input_type=ToolResearchInput,
        )
        value = isolated.input
        if isolated.context_ids:
            raise PhaseContractError("tool research does not accept provider context IDs")
        seen = set(value.existing_result_ids)
        bindings: list[ToolExecutionBinding] = []
        warnings: list[str] = []
        any_failure = False
        for offset, (tool_request, fallback_result_id) in enumerate(
            zip(value.requests, value.fallback_result_ids, strict=True)
        ):
            _safe_unique_id(fallback_result_id, seen, "fallback result ID")
            request_is_safe = bool(_PORTABLE_ID.fullmatch(tool_request.request_id))
            supported_contract_version = tool_request.contract_version or "1.0.0"
            if request_is_safe:
                raw_result = ToolResult.model_validate(
                    self.registry.execute(
                        tool_request.tool_name,
                        dict(tool_request.input),
                        contract_version=tool_request.contract_version,
                    )
                )
                supported_contract_version = raw_result.contract_version
                if (
                    raw_result.tool_name != tool_request.tool_name
                    or raw_result.input != tool_request.input
                    or (
                        raw_result.status is ToolStatus.SUCCESS
                        and tool_request.contract_version is not None
                        and raw_result.contract_version != tool_request.contract_version
                    )
                ):
                    warnings.append(f"{tool_request.tool_name}: tool result correlation mismatch")
                    result = ToolResult(
                        result_id=fallback_result_id,
                        tool_name=tool_request.tool_name,
                        input=dict(tool_request.input),
                        status=ToolStatus.FAILED,
                        exactness=Exactness.UNAVAILABLE,
                        numeric_exactness=NumericalExactness.UNAVAILABLE,
                        contract_version=supported_contract_version,
                        error="tool result correlation mismatch",
                    )
                else:
                    result = ToolResult.model_validate(
                        redact_sensitive(raw_result, enabled=not self.record_sensitive_data)
                    )
            else:
                result = ToolResult(
                    result_id=fallback_result_id,
                    tool_name=tool_request.tool_name,
                    input=dict(tool_request.input),
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    numeric_exactness=NumericalExactness.UNAVAILABLE,
                    contract_version=supported_contract_version,
                    error="unsafe tool request correlation ID",
                )
                warnings.append(f"{tool_request.tool_name}: unsafe tool request correlation ID")
            result_is_safe = bool(_PORTABLE_ID.fullmatch(result.result_id)) and (
                result.result_id not in seen
            )
            if not result_is_safe:
                warnings.append(f"{tool_request.tool_name}: unsafe or duplicate tool result ID")
                result = ToolResult(
                    result_id=fallback_result_id,
                    tool_name=tool_request.tool_name,
                    input=dict(tool_request.input),
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    numeric_exactness=NumericalExactness.UNAVAILABLE,
                    contract_version=supported_contract_version,
                    error="unsafe or duplicate tool result ID",
                )
            result = ToolResult.model_validate(
                redact_sensitive(result, enabled=not self.record_sensitive_data)
            )
            if not _PORTABLE_ID.fullmatch(result.result_id) or result.result_id in seen:
                warnings.append(
                    f"{tool_request.tool_name}: unsafe or duplicate tool result ID after redaction"
                )
                result = ToolResult.model_validate(
                    redact_sensitive(
                        ToolResult(
                            result_id=fallback_result_id,
                            tool_name=tool_request.tool_name,
                            input=dict(tool_request.input),
                            status=ToolStatus.FAILED,
                            exactness=Exactness.UNAVAILABLE,
                            numeric_exactness=NumericalExactness.UNAVAILABLE,
                            contract_version=supported_contract_version,
                            error="unsafe or duplicate tool result ID after redaction",
                        ),
                        enabled=not self.record_sensitive_data,
                    )
                )
            seen.add(result.result_id)
            any_failure = any_failure or result.status is not ToolStatus.SUCCESS
            bindings.append(
                ToolExecutionBinding(
                    run_id=isolated.run_id,
                    phase_attempt_id=isolated.attempt_id,
                    ordinal=value.start_ordinal + offset,
                    request=tool_request,
                    request_input_sha256=canonical_sha256(tool_request.input),
                    validated_result_input_sha256=canonical_sha256(tool_request.input),
                    materialized_result_input_sha256=canonical_sha256(result.input),
                    requested_contract_version=tool_request.contract_version,
                    supported_contract_version=supported_contract_version,
                    result_contract_version=result.contract_version,
                    result=result,
                )
            )
        output = ToolResearchOutput(
            bindings=tuple(bindings),
            data_quality=tuple(warnings),
        )
        validate_tool_research_output(isolated, output)
        return successful_outcome(
            isolated,
            output,
            warnings=output.data_quality,
            completed_with_failures=any_failure,
        )
