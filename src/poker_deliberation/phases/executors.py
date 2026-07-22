"""Explicit serial effect boundaries for provider analysis and local tools."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime
from threading import Thread
from typing import Any

from poker_deliberation.budgets import (
    BudgetFailure,
    BudgetFailureCode,
    BudgetLimitError,
    CancellationStatus,
    DeadlineStatus,
    FailureCategory,
    FakeMonotonicClock,
    IdempotencyStatus,
    MonotonicClock,
    RetryClassification,
    SerialUsageLedger,
    SystemMonotonicClock,
    UsageDelta,
    canonical_json_utf8_size,
    classify_retry,
)
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
from poker_deliberation.providers import AgentProvider, ProviderControl, ProviderControlError
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
from poker_deliberation.tools import ToolByteLimitError, ToolRegistry

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
    clock: MonotonicClock,
    *,
    budget_observed_at_ns: int,
    run_deadline_ns: int,
    runtime_limit_ns: int,
    active_runtime_ns: int,
) -> AgentReport:
    results: list[AgentReport] = []
    errors: list[Exception] = []
    try:
        effect_start_ns = clock.now_ns()
    except Exception as exc:
        raise BudgetLimitError(
            BudgetFailure(
                code=BudgetFailureCode.USAGE_MALFORMED,
                resource="clock",
                message=f"monotonic clock read failed: {type(exc).__name__}",
            )
        ) from exc
    if (
        isinstance(effect_start_ns, bool)
        or not isinstance(effect_start_ns, int)
        or effect_start_ns < 0
    ):
        raise BudgetLimitError(
            BudgetFailure(
                code=BudgetFailureCode.USAGE_MALFORMED,
                resource="clock",
                message="monotonic clock must return non-negative integer nanoseconds",
            )
        )
    if effect_start_ns < budget_observed_at_ns:
        raise BudgetLimitError(
            BudgetFailure(
                code=BudgetFailureCode.CLOCK_ROLLBACK,
                resource="active_runtime_ns",
                message="monotonic clock moved backwards before provider execution",
                observed=budget_observed_at_ns - effect_start_ns,
            )
        )
    if effect_start_ns >= run_deadline_ns:
        raise BudgetLimitError(
            BudgetFailure(
                code=BudgetFailureCode.RUNTIME_EXCEEDED,
                resource="active_runtime_ns",
                message="active runtime expired before provider execution",
                limit=runtime_limit_ns,
                observed=active_runtime_ns + effect_start_ns - budget_observed_at_ns,
            )
        )
    effect_timeout_seconds = min(
        timeout_seconds,
        (run_deadline_ns - effect_start_ns) / 1_000_000_000,
    )
    control = ProviderControl(
        timeout_seconds=effect_timeout_seconds,
        clock=clock,
        observed_start_ns=effect_start_ns,
    )

    def typed_clock_failure(exc: ValueError) -> BudgetLimitError:
        message = str(exc)
        rollback = "backwards" in message
        return BudgetLimitError(
            BudgetFailure(
                code=(
                    BudgetFailureCode.CLOCK_ROLLBACK
                    if rollback
                    else BudgetFailureCode.USAGE_MALFORMED
                ),
                resource="active_runtime_ns" if rollback else "clock",
                message=message,
            )
        )

    def invoke() -> None:
        try:
            try:
                control.raise_if_cancelled()
            except ValueError as exc:
                errors.append(typed_clock_failure(exc))
                return
            results.append(provider.analyze(context, assignment, control))
        except Exception as exc:  # the boundary classifies provider failures below
            errors.append(exc)

    worker = Thread(target=invoke, daemon=True, name=f"provider-{assignment.agent_role}")
    worker.start()
    worker.join(effect_timeout_seconds)
    if worker.is_alive():
        control.request_cancel()
        worker.join(min(0.5, max(0.05, effect_timeout_seconds)))
        if worker.is_alive() or control.cancellation_status is not CancellationStatus.CANCELLED:
            control.mark_cancel_unconfirmed()
        raise ProviderControlError(
            f"provider exceeded deadline {effect_timeout_seconds} seconds",
            deadline_status=DeadlineStatus.TIMED_OUT,
            cancellation_status=control.cancellation_status,
        )
    if errors:
        error = errors[0]
        raise error
    if not results:
        raise RuntimeError("provider returned no report")
    try:
        final_deadline_status = control.deadline_status
    except ValueError as exc:
        raise typed_clock_failure(exc) from exc
    if final_deadline_status is DeadlineStatus.TIMED_OUT:
        control.request_cancel()
        raise ProviderControlError(
            f"provider exceeded deadline {effect_timeout_seconds} seconds",
            deadline_status=DeadlineStatus.TIMED_OUT,
            cancellation_status=control.cancellation_status,
        )
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


def validate_analysis_output(
    request: PhaseRequest[AnalysisInput],
    output: AnalysisOutput,
) -> None:
    """Bind a provider result to its exact dispatch before materialization."""

    value = request.input
    dispatch = value.dispatch
    assignment = dispatch.assignment
    context = dispatch.context
    envelope = dispatch.envelope
    if (
        output.assignment != assignment
        or output.context != context
        or output.envelope != envelope
        or output.report.agent_role != assignment.agent_role
        or output.report.task != assignment.task
    ):
        raise PhaseContractError("analysis output dispatch correlation mismatch")
    _safe_unique_id(
        output.report.report_id,
        set(value.existing_report_ids),
        "analysis output report ID",
    )
    record = output.execution_record
    if (
        record.execution_id != value.execution_id
        or record.assignment_id != assignment.assignment_id
        or record.agent_role != assignment.agent_role
        or record.allowed_tools
        != [name for name in context.requested_tools if name in value.registered_tools]
    ):
        raise PhaseContractError("analysis execution record correlation mismatch")
    for field, expected in _context_record_fields(envelope, context).items():
        if getattr(record, field) != expected:
            raise PhaseContractError("analysis execution context correlation mismatch")
    if output.timed_out and record.status is not AgentExecutionStatus.FAILED:
        raise PhaseContractError("timed-out analysis must have a failed execution record")


class AnalysisExecutor:
    """Run exactly one provider assignment without owning state or persistence."""

    def __init__(
        self,
        provider: AgentProvider,
        *,
        context_clock: Callable[[], datetime],
        record_clock: Callable[[], datetime],
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        self.provider = provider
        self.context_clock = context_clock
        self.record_clock = record_clock
        self.monotonic_clock = monotonic_clock or SystemMonotonicClock()

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
        provider_info = value.provider_availability
        execution_status = AgentExecutionStatus.COMPLETED
        execution_error: str | None = None
        warnings: list[str] = []
        timed_out = False
        deadline_status = DeadlineStatus.ACTIVE
        cancellation_status = CancellationStatus.NOT_REQUESTED
        budget_failure: BudgetFailure | None = None
        retry_classification = None
        usage_delta = UsageDelta()
        accepted_provider_output = False
        provider_output_size = 0
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
            attempt_ledger = SerialUsageLedger(
                value.budget_policy,
                clock=FakeMonotonicClock(value.budget_observed_at_ns),
                initial=value.budget_snapshot,
                active=False,
            )
            attempt_ledger.begin_provider_attempt(
                provider_info.execution_class,
                provider_info.estimated_cost_micro_usd,
            )
            usage_delta = UsageDelta(
                provider_attempts=1,
                external_cost_micro_usd=(
                    provider_info.estimated_cost_micro_usd or 0
                    if provider_info.execution_class.value == "external"
                    else 0
                ),
                peak_concurrency=1,
            )
            try:
                raw_report = _analyze_with_timeout(
                    self.provider,
                    provider_context,
                    assignment.model_copy(deep=True),
                    value.provider_timeout_seconds,
                    self.monotonic_clock,
                    budget_observed_at_ns=value.budget_observed_at_ns,
                    run_deadline_ns=value.run_deadline_ns,
                    runtime_limit_ns=value.budget_policy.runtime_limit_ns,
                    active_runtime_ns=value.budget_snapshot.active_runtime_ns,
                )
            except ProviderControlError:
                raise
            except BudgetLimitError:
                raise
            except Exception as exc:
                raise _ProviderAnalysisFailed(type(exc).__name__) from exc
            report = AgentReport.model_validate(
                raw_report.model_dump(mode="python")
                if isinstance(raw_report, AgentReport)
                else raw_report
            )
            provider_output_size = canonical_json_utf8_size(report)
            SerialUsageLedger(
                value.budget_policy,
                clock=FakeMonotonicClock(value.budget_observed_at_ns),
                initial=value.budget_snapshot.apply(usage_delta),
                active=False,
            ).preflight(UsageDelta(provider_output_bytes=provider_output_size))
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
            accepted_provider_output = True
        except BudgetLimitError as exc:
            budget_failure = exc.failure
            execution_status = AgentExecutionStatus.REFUSED
            execution_error = exc.failure.message
            if exc.failure.code is BudgetFailureCode.PROVIDER_OUTPUT_EXCEEDED:
                warnings.append(
                    f"provider {assignment.agent_role} output exceeded the hard byte limit"
                )
            else:
                warnings.append(
                    f"provider {assignment.agent_role} budget refused: {exc.failure.code}"
                )
            retry_classification = classify_retry(
                FailureCategory.BUDGET,
                max_retries=0,
            )
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Provider execution was refused by the strict budget policy."],
                confidence=ConfidenceGrade.D,
            )
        except ProviderControlError as exc:
            deadline_status = exc.deadline_status
            cancellation_status = exc.cancellation_status
            timed_out = deadline_status is DeadlineStatus.TIMED_OUT
            execution_status = AgentExecutionStatus.FAILED
            execution_error = str(exc)
            warnings.append(str(exc))
            retry_classification = classify_retry(
                (
                    FailureCategory.DEADLINE
                    if deadline_status is DeadlineStatus.TIMED_OUT
                    else FailureCategory.CANCEL
                ),
                max_retries=0,
            )
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
            retry_classification = classify_retry(
                FailureCategory.UNAVAILABLE,
                max_retries=0,
            )
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
            retry_classification = classify_retry(
                FailureCategory.VALIDATION,
                max_retries=0,
            )
            report = AgentReport(
                report_id=value.fallback_report_id,
                agent_role=assignment.agent_role,
                task=assignment.task,
                uncertainties=["Context validation failed; no provider output was accepted."],
                confidence=ConfidenceGrade.D,
            )
        except _ProviderAnalysisFailed as exc:
            legacy_timeout = exc.error_type == "TimeoutError" and value.legacy_provider_contract
            if legacy_timeout:
                timed_out = True
                deadline_status = DeadlineStatus.TIMED_OUT
                cancellation_status = CancellationStatus.CANCEL_UNCONFIRMED
            execution_status = (
                AgentExecutionStatus.FAILED if legacy_timeout else AgentExecutionStatus.FALLBACK
            )
            execution_error = f"{exc.error_type}: provider analyze failed"
            warnings.append(f"provider {assignment.agent_role} failed: {exc.error_type}")
            retry_classification = classify_retry(
                FailureCategory.DEADLINE if legacy_timeout else FailureCategory.PROVIDER_PERMANENT,
                idempotency=(
                    IdempotencyStatus.NOT_APPLICABLE
                    if legacy_timeout
                    else IdempotencyStatus.UNKNOWN
                ),
                max_retries=0,
            )
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
            retry_classification = classify_retry(
                FailureCategory.INTERNAL,
                max_retries=0,
            )
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
            accepted_provider_output = False
        if accepted_provider_output:
            report_size = max(provider_output_size, canonical_json_utf8_size(report))
            try:
                SerialUsageLedger(
                    value.budget_policy,
                    clock=FakeMonotonicClock(value.budget_observed_at_ns),
                    initial=value.budget_snapshot.apply(usage_delta),
                    active=False,
                ).preflight(UsageDelta(provider_output_bytes=report_size))
            except BudgetLimitError as exc:
                budget_failure = exc.failure
                warnings.append(
                    f"provider {assignment.agent_role} output exceeded the hard byte limit"
                )
                report = AgentReport(
                    report_id=value.fallback_report_id,
                    agent_role=assignment.agent_role,
                    task=assignment.task,
                    uncertainties=["Oversized provider output was rejected."],
                    confidence=ConfidenceGrade.D,
                )
                execution_status = AgentExecutionStatus.FAILED
                execution_error = "provider output exceeded the hard byte limit"
                retry_classification = classify_retry(
                    FailureCategory.BUDGET,
                    max_retries=0,
                )
            else:
                usage_delta = usage_delta.combine(UsageDelta(provider_output_bytes=report_size))
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
            usage_delta=usage_delta,
            budget_failure=budget_failure,
            retry_classification=retry_classification,
            deadline_status=deadline_status,
            cancellation_status=cancellation_status,
        )
        validate_analysis_output(isolated, output)
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
        retry_classifications: list[RetryClassification | None] = []
        warnings: list[str] = []
        any_failure = False
        budget_failure: BudgetFailure | None = None
        usage_delta = UsageDelta()
        budget_ledger = (
            SerialUsageLedger(
                value.budget_policy,
                initial=value.budget_snapshot,
                active=False,
            )
            if value.budget_policy is not None and value.budget_snapshot is not None
            else None
        )
        for offset, (tool_request, fallback_result_id) in enumerate(
            zip(value.requests, value.fallback_result_ids, strict=True)
        ):
            raw_result_bytes = 0
            _safe_unique_id(fallback_result_id, seen, "fallback result ID")
            request_is_safe = bool(_PORTABLE_ID.fullmatch(tool_request.request_id))
            supported_contract_version = tool_request.contract_version or "1.0.0"
            request_bytes = canonical_json_utf8_size(tool_request.input)
            current_delta = UsageDelta(tool_attempts=1, tool_input_bytes=request_bytes)
            if budget_ledger is not None and budget_failure is None:
                try:
                    budget_ledger.apply(current_delta)
                    usage_delta = usage_delta.combine(current_delta)
                except BudgetLimitError as exc:
                    budget_failure = exc.failure
                    warnings.append(f"{tool_request.tool_name}: strict budget refused execution")
            if request_is_safe and budget_failure is None:
                try:
                    phase_execute = getattr(self.registry, "execute_for_phase", None)
                    raw_value = (
                        phase_execute(
                            tool_request.tool_name,
                            dict(tool_request.input),
                            contract_version=tool_request.contract_version,
                            budget_observed_at_ns=value.budget_observed_at_ns,
                            run_deadline_ns=value.run_deadline_ns,
                            runtime_limit_ns=(
                                value.budget_policy.runtime_limit_ns
                                if value.budget_policy is not None
                                else None
                            ),
                            active_runtime_ns=(
                                value.budget_snapshot.active_runtime_ns
                                if value.budget_snapshot is not None
                                else None
                            ),
                        )
                        if callable(phase_execute)
                        else self.registry.execute(
                            tool_request.tool_name,
                            dict(tool_request.input),
                            contract_version=tool_request.contract_version,
                        )
                    )
                    raw_result = ToolResult.model_validate(raw_value)
                except ToolByteLimitError as exc:
                    budget_failure = BudgetFailure(
                        code=(
                            BudgetFailureCode.TOOL_INPUT_EXCEEDED
                            if exc.resource == "tool_input_bytes"
                            else BudgetFailureCode.TOOL_OUTPUT_EXCEEDED
                        ),
                        resource=exc.resource,
                        message=str(exc),
                        limit=exc.limit,
                        observed=exc.observed,
                    )
                    warnings.append(f"{tool_request.tool_name}: {exc}")
                    result = ToolResult(
                        result_id=fallback_result_id,
                        tool_name=tool_request.tool_name,
                        input=dict(tool_request.input),
                        status=ToolStatus.FAILED,
                        exactness=Exactness.UNAVAILABLE,
                        numeric_exactness=NumericalExactness.UNAVAILABLE,
                        contract_version=supported_contract_version,
                        error=f"strict budget failure: {budget_failure.code.value}",
                    )
                except BudgetLimitError as exc:
                    budget_failure = exc.failure
                    warnings.append(
                        f"{tool_request.tool_name}: strict runtime budget refused execution"
                    )
                    result = ToolResult(
                        result_id=fallback_result_id,
                        tool_name=tool_request.tool_name,
                        input=dict(tool_request.input),
                        status=ToolStatus.FAILED,
                        exactness=Exactness.UNAVAILABLE,
                        numeric_exactness=NumericalExactness.UNAVAILABLE,
                        contract_version=supported_contract_version,
                        error=f"strict budget failure: {budget_failure.code.value}",
                    )
                else:
                    raw_result_bytes = canonical_json_utf8_size(raw_result.output)
                    if budget_ledger is not None and budget_failure is None:
                        try:
                            raw_output_delta = UsageDelta(tool_output_bytes=raw_result_bytes)
                            budget_ledger.apply(raw_output_delta)
                            usage_delta = usage_delta.combine(raw_output_delta)
                        except BudgetLimitError as exc:
                            budget_failure = exc.failure
                            warnings.append(
                                f"{tool_request.tool_name}: raw tool output exceeded strict budget"
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
                        warnings.append(
                            f"{tool_request.tool_name}: tool result correlation mismatch"
                        )
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
            if budget_failure is not None and request_is_safe:
                result = ToolResult(
                    result_id=fallback_result_id,
                    tool_name=tool_request.tool_name,
                    input=dict(tool_request.input),
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    numeric_exactness=NumericalExactness.UNAVAILABLE,
                    contract_version=supported_contract_version,
                    error=f"strict budget failure: {budget_failure.code.value}",
                )
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
            result_bytes = max(raw_result_bytes, canonical_json_utf8_size(result.output))
            if budget_ledger is not None and budget_failure is None:
                try:
                    budget_ledger.apply(UsageDelta(tool_output_bytes=result_bytes))
                    usage_delta = usage_delta.combine(UsageDelta(tool_output_bytes=result_bytes))
                except BudgetLimitError as exc:
                    budget_failure = exc.failure
                    warnings.append(f"{tool_request.tool_name}: tool output exceeded strict budget")
                    result = ToolResult(
                        result_id=fallback_result_id,
                        tool_name=tool_request.tool_name,
                        input=dict(tool_request.input),
                        status=ToolStatus.FAILED,
                        exactness=Exactness.UNAVAILABLE,
                        numeric_exactness=NumericalExactness.UNAVAILABLE,
                        contract_version=supported_contract_version,
                        error=f"strict budget failure: {budget_failure.code.value}",
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
            if result.status is ToolStatus.SUCCESS:
                retry_classifications.append(None)
            else:
                if budget_failure is not None:
                    category = FailureCategory.BUDGET
                elif not request_is_safe or "correlation" in (result.error or ""):
                    category = FailureCategory.VALIDATION
                elif result.status is ToolStatus.UNAVAILABLE:
                    category = FailureCategory.UNAVAILABLE
                elif "runtime limit" in (result.error or ""):
                    category = FailureCategory.DEADLINE
                else:
                    category = FailureCategory.TOOL_DETERMINISTIC
                retry_classifications.append(
                    classify_retry(
                        category,
                        idempotency=IdempotencyStatus.IDEMPOTENT,
                        max_retries=(
                            value.budget_policy.max_tool_retries
                            if value.budget_policy is not None
                            else 0
                        ),
                    )
                )
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
            usage_delta=usage_delta,
            budget_failure=budget_failure,
            retry_classifications=tuple(retry_classifications),
        )
        validate_tool_research_output(isolated, output)
        return successful_outcome(
            isolated,
            output,
            warnings=output.data_quality,
            completed_with_failures=any_failure,
        )
