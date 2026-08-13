from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from poker_deliberation.budgets import (
    BudgetFailureCode,
    BudgetPolicyV2,
    ExecutionClass,
    FailureCategory,
    FakeMonotonicClock,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.phases import AnalysisExecutor, ToolResearchExecutor
from poker_deliberation.phases.services import (
    ContextBuildService,
    NormalizationService,
    SynthesisService,
)
from poker_deliberation.providers import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport, CaseInput
from poker_deliberation.storage.terminal_models import RunReadStatus
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.registry import ToolDefinition, ToolRegistry


class CostedProvider:
    def __init__(
        self,
        execution_class: ExecutionClass,
        estimate_micro_usd: int | None,
        *,
        clock: FakeMonotonicClock | None = None,
        advance_ns: int = 0,
        conclusion_size: int = 0,
    ) -> None:
        self.execution_class = execution_class
        self.estimate_micro_usd = estimate_micro_usd
        self.clock = clock
        self.advance_ns = advance_ns
        self.conclusion_size = conclusion_size
        self.calls = 0

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            status=ProviderStatus.AVAILABLE,
            available=True,
            provider="costed-test",
            reason="test provider",
            execution_class=self.execution_class,
            estimated_cost_micro_usd=self.estimate_micro_usd,
        )

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context
        self.calls += 1
        if self.clock is not None:
            self.clock.advance_ns(self.advance_ns)
        return AgentReport(
            report_id=f"report-{assignment.agent_role}",
            agent_role=assignment.agent_role,
            task=assignment.task,
            conclusions=["x" * self.conclusion_size] if self.conclusion_size else [],
        )


class CapturingAnalysisExecutor(AnalysisExecutor):
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.outcomes = []

    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        self.outcomes.append(outcome)
        return outcome


class FailingProvider(CostedProvider):
    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, assignment, control
        self.calls += 1
        raise ValueError("deterministic invalid provider payload")


class CancellingProvider(CostedProvider):
    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, assignment
        self.calls += 1
        control.request_cancel()
        control.raise_if_cancelled()
        raise AssertionError("cancelled provider must not continue")


class CapturingToolResearchExecutor(ToolResearchExecutor):
    def __init__(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.outcomes = []

    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        self.outcomes.append(outcome)
        return outcome


class AdvancingToolResearchExecutor(CapturingToolResearchExecutor):
    def __init__(self, *args, clock: FakeMonotonicClock, **kwargs):  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self.clock = clock

    def run(self, request):  # type: ignore[no-untyped-def]
        self.clock.advance_ns(2_000_000_000)
        return super().run(request)


def _strategy_case() -> CaseInput:
    return CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective")


@pytest.mark.parametrize(
    ("policy", "estimate"),
    [
        (BudgetPolicyV2(max_external_cost_micro_usd=0), 1),
        (BudgetPolicyV2(max_external_cost_micro_usd=10), None),
        (BudgetPolicyV2(max_external_cost_micro_usd=10), 11),
    ],
)
def test_external_cost_is_refused_before_provider_call(
    tmp_path: Path,
    policy: BudgetPolicyV2,
    estimate: int | None,
) -> None:
    provider = CostedProvider(ExecutionClass.EXTERNAL, estimate)
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        budget_policy=policy,
    ).run(_strategy_case(), run_id="run-external-refused")

    assert provider.calls == 0
    assert report.run_status == "failed_with_limitations"
    assert any("budget refused" in item for item in report.data_quality)


def test_known_external_cost_under_cap_is_accounted_serially(tmp_path: Path) -> None:
    provider = CostedProvider(ExecutionClass.EXTERNAL, 3)
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        budget_policy=BudgetPolicyV2(max_external_cost_micro_usd=12),
    ).run(_strategy_case(), run_id="run-external-known")

    assert provider.calls == 4
    assert report.run_status == "completed"


def test_local_free_provider_and_calculator_work_with_cost_cap_zero(tmp_path: Path) -> None:
    provider = CostedProvider(ExecutionClass.LOCAL_FREE, None)
    strategy = Orchestrator(
        AppConfig(runs_dir=tmp_path / "provider"),
        provider=provider,
        budget_policy=BudgetPolicyV2(max_external_cost_micro_usd=0),
    ).run(_strategy_case(), run_id="run-local-provider")
    calculation = Orchestrator(
        AppConfig(runs_dir=tmp_path / "calculator"),
        budget_policy=BudgetPolicyV2(max_external_cost_micro_usd=0),
    ).run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["pot_odds"],
            metadata={
                "tool_inputs": {
                    "pot_odds": {
                        "pot_before_bet": 100,
                        "opponent_bet": 50,
                        "call_cost": 50,
                    }
                }
            },
        ),
        run_id="run-local-calculator",
    )

    assert strategy.run_status == "completed"
    assert provider.calls == 4
    assert calculation.run_status == "completed"
    assert calculation.tool_results[0].status.value == "success"


def test_fake_clock_runtime_overrun_becomes_structured_limitation(tmp_path: Path) -> None:
    clock = FakeMonotonicClock()
    provider = CostedProvider(
        ExecutionClass.EXTERNAL,
        5,
        clock=clock,
        advance_ns=2_000_000_000,
    )
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(
            max_runtime_seconds=1.0,
            max_external_cost_micro_usd=10,
        ),
    )
    report = orchestrator.run(_strategy_case(), run_id="run-runtime-overrun")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert any("deadline" in item for item in report.data_quality)
    usage = orchestrator._run_machines[report.run_id].ledger.settled_snapshot()
    assert usage.active_runtime_ns == 2_000_000_000
    assert usage.provider_attempts == 1
    assert usage.external_cost_micro_usd == 5


def test_late_provider_output_is_rejected_by_injected_deadline(tmp_path: Path) -> None:
    clock = FakeMonotonicClock()
    provider = CostedProvider(
        ExecutionClass.LOCAL_FREE,
        None,
        clock=clock,
        advance_ns=31_000_000_000,
    )
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=300.0),
    ).run(_strategy_case(), run_id="run-late-provider-output")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert report.agent_execution_records[0].status.value == "failed"


def test_provider_failure_is_not_assumed_transient_or_given_tool_retries(
    tmp_path: Path,
) -> None:
    provider = FailingProvider(ExecutionClass.LOCAL_FREE, None)
    executor = CapturingAnalysisExecutor(
        provider,
        context_clock=lambda: datetime.now(UTC),
        record_clock=lambda: datetime.now(UTC),
    )
    Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        analysis_executor=executor,
        budget_policy=BudgetPolicyV2(max_tool_retries=3),
    ).run(_strategy_case(), run_id="run-provider-permanent-failure")

    classification = executor.outcomes[0].output.retry_classification
    assert classification is not None
    assert classification.category is FailureCategory.PROVIDER_PERMANENT
    assert not classification.retryable
    assert classification.max_retries == 0


def test_explicit_provider_cancellation_is_not_misclassified_as_deadline(
    tmp_path: Path,
) -> None:
    provider = CancellingProvider(ExecutionClass.LOCAL_FREE, None)
    executor = CapturingAnalysisExecutor(
        provider,
        context_clock=lambda: datetime.now(UTC),
        record_clock=lambda: datetime.now(UTC),
    )
    Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        analysis_executor=executor,
    ).run(_strategy_case(), run_id="run-provider-cancelled")

    classification = executor.outcomes[0].output.retry_classification
    assert classification is not None
    assert classification.category is FailureCategory.CANCEL
    assert not classification.retryable


def test_provider_output_split_cap_rejects_oversized_report(tmp_path: Path) -> None:
    provider = CostedProvider(
        ExecutionClass.LOCAL_FREE,
        None,
        conclusion_size=4000,
    )
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        budget_policy=BudgetPolicyV2(max_provider_output_bytes=1024),
    ).run(_strategy_case(), run_id="run-provider-output-cap")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert any("strict budget" in item for item in report.data_quality)


class SecretProvider(CostedProvider):
    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, control
        self.calls += 1
        return AgentReport(
            report_id=f"report-{assignment.agent_role}",
            agent_role=assignment.agent_role,
            task=assignment.task,
            conclusions=["sk-" + "a" * 4000],
        )


def test_raw_provider_output_is_capped_before_redaction(tmp_path: Path) -> None:
    provider = SecretProvider(ExecutionClass.LOCAL_FREE, None)
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        budget_policy=BudgetPolicyV2(
            max_provider_output_bytes=1024,
            max_artifact_bytes=100_000,
        ),
    ).run(_strategy_case(), run_id="run-raw-provider-output-cap")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert any("output exceeded" in item for item in report.data_quality)


def test_zero_deliberation_rounds_skip_provider_analysis(tmp_path: Path) -> None:
    provider = CostedProvider(ExecutionClass.LOCAL_FREE, None)

    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        budget_policy=BudgetPolicyV2(max_deliberation_rounds=0),
    ).run(_strategy_case(), run_id="run-zero-rounds")

    assert provider.calls == 0
    assert report.run_status == "failed_with_limitations"
    assert "provider analysis skipped because round budget is zero" in report.data_quality


def test_injected_analysis_executor_cannot_use_a_different_provider(tmp_path: Path) -> None:
    orchestrator_provider = CostedProvider(ExecutionClass.LOCAL_FREE, None)
    executor_provider = CostedProvider(ExecutionClass.EXTERNAL, 1)
    executor = AnalysisExecutor(
        executor_provider,
        context_clock=lambda: datetime.now(UTC),
        record_clock=lambda: datetime.now(UTC),
    )

    with pytest.raises(ValueError, match="executor provider must match"):
        Orchestrator(
            AppConfig(runs_dir=tmp_path / "runs"),
            provider=orchestrator_provider,
            analysis_executor=executor,
            budget_policy=BudgetPolicyV2(max_external_cost_micro_usd=0),
        )

    assert orchestrator_provider.calls == 0
    assert executor_provider.calls == 0


def test_injected_effect_boundaries_cannot_use_different_clocks(tmp_path: Path) -> None:
    run_clock = FakeMonotonicClock()
    executor_clock = FakeMonotonicClock()
    provider = CostedProvider(ExecutionClass.LOCAL_FREE, None)
    executor = AnalysisExecutor(
        provider,
        context_clock=lambda: datetime.now(UTC),
        record_clock=lambda: datetime.now(UTC),
        monotonic_clock=executor_clock,
    )
    with pytest.raises(ValueError, match="effect clocks must match"):
        Orchestrator(
            AppConfig(runs_dir=tmp_path / "provider"),
            provider=provider,
            analysis_executor=executor,
            monotonic_clock=run_clock,
        )

    registry = ToolRegistry(monotonic_clock=executor_clock)
    with pytest.raises(ValueError, match="effect clocks must match"):
        Orchestrator(
            AppConfig(runs_dir=tmp_path / "tool"),
            registry=registry,
            monotonic_clock=run_clock,
        )


def test_injected_tool_executor_cannot_change_redaction_policy(tmp_path: Path) -> None:
    registry = default_registry()
    executor = ToolResearchExecutor(registry, record_sensitive_data=True)

    with pytest.raises(ValueError, match="redaction policy must match"):
        Orchestrator(
            AppConfig(runs_dir=tmp_path / "runs", record_sensitive_data=False),
            registry=registry,
            tool_research_executor=executor,
        )


class AdvancingContextBuildService(ContextBuildService):
    def __init__(self, clock: FakeMonotonicClock) -> None:
        super().__init__()
        self.clock = clock

    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        self.clock.advance_ns(2_000_000_000)
        return outcome


def test_context_build_runtime_overrun_refuses_provider_start(tmp_path: Path) -> None:
    clock = FakeMonotonicClock()
    provider = CostedProvider(ExecutionClass.EXTERNAL, 1)

    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(
            max_runtime_seconds=1.0,
            max_external_cost_micro_usd=10,
        ),
        context_build_service=AdvancingContextBuildService(clock),
    ).run(_strategy_case(), run_id="run-context-budget-overrun")

    assert provider.calls == 0
    assert report.run_status == "failed_with_limitations"
    assert "maximum runtime reached during context build" in report.data_quality


def test_context_handoff_runtime_overrun_refuses_provider_start(tmp_path: Path) -> None:
    clock = FakeMonotonicClock()
    provider = CostedProvider(ExecutionClass.LOCAL_FREE, None)
    context_clock_calls = 0

    def context_clock() -> datetime:
        nonlocal context_clock_calls
        context_clock_calls += 1
        if context_clock_calls == 2:
            clock.advance_ns(2_000_000_000)
        return datetime.now(UTC)

    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_clock=context_clock,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=1.0),
    ).run(_strategy_case(), run_id="run-context-handoff-overrun")

    assert context_clock_calls == 2
    assert provider.calls == 0
    assert report.run_status == "failed_with_limitations"
    assert any("budget refused" in item for item in report.data_quality)


class MutableClock:
    def __init__(self, value: object) -> None:
        self.value = value

    def now_ns(self) -> int:
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value  # type: ignore[return-value]


class SequenceAfterActivationClock:
    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.active = False
        self.index = 0

    def activate(self) -> None:
        self.active = True

    def now_ns(self) -> int:
        if not self.active:
            return 0
        value = self.values[min(self.index, len(self.values) - 1)]
        self.index += 1
        return value


class ActivatingContextBuildService(ContextBuildService):
    def __init__(self, clock: SequenceAfterActivationClock) -> None:
        super().__init__()
        self.clock = clock

    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        self.clock.activate()
        return outcome


def test_context_runtime_window_at_exact_cap_publishes_structured_failure(
    tmp_path: Path,
) -> None:
    clock = SequenceAfterActivationClock([1_000_000_000, 1_000_000_001])
    provider = CostedProvider(ExecutionClass.LOCAL_FREE, None)
    run_id = "run-context-window-exact-cap"
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_build_service=ActivatingContextBuildService(clock),
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=1.0),
    )

    report = orchestrator.run(_strategy_case(), run_id=run_id)
    state = orchestrator.store.read_json(run_id, "state.json")
    verified = orchestrator.product_store.read_current(run_id)

    assert provider.calls == 0
    assert report.run_status == "failed_with_limitations"
    assert "maximum runtime reached during context build" in report.data_quality
    assert state["state"] == "FAILED_WITH_LIMITATIONS"
    assert verified.read_status is RunReadStatus.FAILED
    assert orchestrator._run_machines[run_id].last_budget_failure is not None
    assert (
        orchestrator._run_machines[run_id].last_budget_failure.code
        is BudgetFailureCode.RUNTIME_EXCEEDED
    )


class ClockMutatingProvider(CostedProvider):
    def __init__(self, clock: MutableClock, post_value: object) -> None:
        super().__init__(ExecutionClass.LOCAL_FREE, None)
        self.boundary_clock = clock
        self.post_value = post_value

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, control
        self.calls += 1
        self.boundary_clock.value = self.post_value
        return AgentReport(
            report_id=f"report-{assignment.agent_role}",
            agent_role=assignment.agent_role,
            task=assignment.task,
        )


@pytest.mark.parametrize(
    ("boundary_value", "expected_code"),
    [
        (900_000_000, "clock_rollback"),
        ("bad-clock", "usage_malformed"),
        (RuntimeError("clock unavailable"), "usage_malformed"),
    ],
)
def test_context_handoff_clock_failure_is_structured_before_provider_start(
    tmp_path: Path,
    boundary_value: object,
    expected_code: str,
) -> None:
    clock = MutableClock(1_000_000_000)
    provider = CostedProvider(ExecutionClass.LOCAL_FREE, None)
    context_clock_calls = 0

    def context_clock() -> datetime:
        nonlocal context_clock_calls
        context_clock_calls += 1
        if context_clock_calls == 2:
            clock.value = boundary_value
        return datetime.now(UTC)

    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        context_clock=context_clock,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=10.0),
    ).run(_strategy_case(), run_id=f"run-handoff-{expected_code}")

    assert provider.calls == 0
    assert report.run_status == "failed_with_limitations"
    assert any(expected_code in item for item in report.data_quality)


@pytest.mark.parametrize(
    ("post_value", "expected_code"),
    [
        (400_000_000, "clock_rollback"),
        ("bad-clock", "usage_malformed"),
        (RuntimeError("clock unavailable"), "usage_malformed"),
    ],
)
def test_post_provider_clock_failure_stops_later_effects(
    tmp_path: Path,
    post_value: object,
    expected_code: str,
) -> None:
    clock = MutableClock(500_000_000)
    provider = ClockMutatingProvider(clock, post_value)
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=10.0),
    ).run(_strategy_case(), run_id=f"run-post-provider-{expected_code}")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert any(expected_code in item for item in report.data_quality)


def test_provider_effect_high_water_stops_later_effects_after_rollback(
    tmp_path: Path,
) -> None:
    clock = SequenceAfterActivationClock([500_000_000, 400_000_000])

    class HighWaterProvider(CostedProvider):
        def analyze(
            self,
            context: AgentContext,
            assignment: AgentAssignment,
            control: ProviderControl,
        ) -> AgentReport:
            del context, control
            self.calls += 1
            clock.activate()
            return AgentReport(
                report_id=f"report-{assignment.agent_role}",
                agent_role=assignment.agent_role,
                task=assignment.task,
            )

    provider = HighWaterProvider(ExecutionClass.LOCAL_FREE, None)
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=10.0),
    )
    report = orchestrator.run(_strategy_case(), run_id="run-provider-high-water-rollback")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert any("clock_rollback" in item for item in report.data_quality)
    events = orchestrator._run_machines[report.run_id].snapshot()["events"]
    assert any("clock_rollback" in str(event) for event in events)


class AdvancingNormalizationService(NormalizationService):
    def __init__(self, clock: FakeMonotonicClock) -> None:
        self.clock = clock

    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        self.clock.advance_ns(2_000_000_000)
        return outcome


def test_early_phase_runtime_exhaustion_returns_structured_tool_limitation(
    tmp_path: Path,
) -> None:
    clock = FakeMonotonicClock()
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        monotonic_clock=clock,
        normalization_service=AdvancingNormalizationService(clock),
        budget_policy=BudgetPolicyV2(max_runtime_seconds=1.0),
    ).run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["pot_odds"],
            metadata={
                "tool_inputs": {
                    "pot_odds": {
                        "pot_before_bet": 100,
                        "opponent_bet": 50,
                        "call_cost": 50,
                    }
                }
            },
        ),
        run_id="run-early-runtime-tool-refusal",
    )

    assert report.run_status == "failed_with_limitations"
    assert report.tool_results == []
    assert "strict runtime refused before requested tool execution" in report.data_quality


def test_tool_effect_boundary_rechecks_absolute_run_deadline(tmp_path: Path) -> None:
    clock = FakeMonotonicClock()
    effect_calls = 0

    def counted_tool(_: dict[str, object]) -> dict[str, object]:
        nonlocal effect_calls
        effect_calls += 1
        return {"value": 1}

    registry = ToolRegistry(monotonic_clock=clock)
    registry.register(
        ToolDefinition(
            name="counted_tool",
            purpose="absolute runtime boundary fixture",
            exact_or_approximate="exact",
            supported_games=("fixture",),
            function=counted_tool,
        )
    )
    executor = AdvancingToolResearchExecutor(
        registry,
        record_sensitive_data=False,
        clock=clock,
    )
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        registry=registry,
        tool_research_executor=executor,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=1.0),
    ).run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["counted_tool"],
        ),
        run_id="run-tool-effect-deadline",
    )

    assert effect_calls == 0
    assert report.run_status == "failed_with_limitations"
    assert any("runtime_exceeded" in item for item in report.data_quality)


def test_tool_effect_high_water_is_carried_between_serial_tools(tmp_path: Path) -> None:
    clock = SequenceAfterActivationClock([500_000_000, 500_000_000, 400_000_000])
    effect_calls: list[str] = []

    def first_tool(_: dict[str, object]) -> dict[str, object]:
        effect_calls.append("first")
        clock.activate()
        return {"value": 1}

    def second_tool(_: dict[str, object]) -> dict[str, object]:
        effect_calls.append("second")
        return {"value": 2}

    registry = ToolRegistry(monotonic_clock=clock)
    for name, function in (("first", first_tool), ("second", second_tool)):
        registry.register(
            ToolDefinition(
                name=name,
                purpose="serial high-water fixture",
                exact_or_approximate="exact",
                supported_games=("fixture",),
                function=function,
            )
        )
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        registry=registry,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=10.0),
    ).run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["first", "second"],
        ),
        run_id="run-tool-high-water-rollback",
    )

    assert effect_calls == ["first"]
    assert report.run_status == "failed_with_limitations"
    assert any("clock_rollback" in item for item in report.data_quality)


class AdvancingSynthesisService(SynthesisService):
    def __init__(self, clock: FakeMonotonicClock, advance_ns: int = 500_000_000) -> None:
        self.clock = clock
        self.advance_ns = advance_ns

    def run(self, request):  # type: ignore[no-untyped-def]
        self.clock.advance_ns(self.advance_ns)
        return super().run(request)


def test_internal_approval_report_work_is_charged_before_human_wait_pause(
    tmp_path: Path,
) -> None:
    clock = FakeMonotonicClock()
    run_id = "run-approval-wait-accounting"
    case = CaseInput(
        kind="strategy",
        raw_text="external solver",
        analysis_scope="retrospective",
        metadata={
            "approval_requests": [
                {
                    "approval_id": "approval-budget-wait",
                    "requested_action": "external solver",
                    "reason": "test approval boundary",
                    "expected_benefit": "external result",
                    "risks": ["external execution"],
                    "cost_or_resource_estimate": "unknown",
                    "alternatives": ["local analysis"],
                    "effect_of_declining": "no external result",
                }
            ]
        },
    )

    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        monotonic_clock=clock,
        synthesis_service=AdvancingSynthesisService(clock),
    )
    report = orchestrator.run(case, run_id=run_id)
    state = orchestrator.store.read_json(run_id, "state.json")
    verified = orchestrator.product_store.read_current(run_id)

    assert report.run_status == "approval_required"
    assert state["elapsed_seconds"] == 0.5
    assert verified.read_status is RunReadStatus.APPROVAL_REQUIRED


@pytest.mark.parametrize("approval_required", [False, True])
def test_final_synthesis_runtime_overrun_is_structured_and_not_completed(
    tmp_path: Path,
    approval_required: bool,
) -> None:
    clock = FakeMonotonicClock()
    metadata = (
        {
            "approval_requests": [
                {
                    "approval_id": "approval-synthesis-overrun",
                    "requested_action": "external solver",
                    "reason": "test final synthesis deadline",
                    "expected_benefit": "external result",
                    "risks": ["external execution"],
                    "cost_or_resource_estimate": "unknown",
                    "alternatives": ["local analysis"],
                    "effect_of_declining": "no external result",
                }
            ]
        }
        if approval_required
        else {}
    )
    run_id = f"run-synthesis-overrun-{approval_required}"
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=1.0),
        synthesis_service=AdvancingSynthesisService(clock, 2_000_000_000),
    )
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="review final synthesis runtime",
            analysis_scope="retrospective",
            metadata=metadata,
        ),
        run_id=run_id,
    )
    state = orchestrator.store.read_json(run_id, "state.json")
    verified = orchestrator.product_store.read_current(run_id)

    assert report.run_status == "failed_with_limitations"
    assert state["state"] == "FAILED_WITH_LIMITATIONS"
    assert verified.read_status is RunReadStatus.FAILED
    assert "maximum runtime exceeded during final synthesis" in report.data_quality


def test_final_artifact_runtime_overrun_rewrites_terminal_state_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FakeMonotonicClock()
    run_id = "run-final-artifact-overrun"
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=1.0),
    )
    original_write_text = orchestrator.store.write_text

    def advance_on_final_markdown(run_id_value: str, relative: str, value: str):  # type: ignore[no-untyped-def]
        path = original_write_text(run_id_value, relative, value)
        if relative == "final_report.md":
            clock.advance_ns(2_000_000_000)
        return path

    monkeypatch.setattr(orchestrator.store, "write_text", advance_on_final_markdown)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="review final artifact runtime",
            analysis_scope="retrospective",
        ),
        run_id=run_id,
    )
    state = orchestrator.store.read_json(run_id, "state.json")
    stored_report = orchestrator.store.read_json(run_id, "final_report.json")
    verified = orchestrator.product_store.read_current(run_id)

    assert report.run_status == "failed_with_limitations"
    assert state["state"] == "FAILED_WITH_LIMITATIONS"
    assert stored_report["run_status"] == "failed_with_limitations"
    assert verified.read_status is RunReadStatus.FAILED
    assert "maximum runtime exceeded during final artifact writes" in report.data_quality


def test_run_store_writes_settle_peak_artifact_and_current_run_bytes(tmp_path: Path) -> None:
    run_id = "run-storage-accounting"
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"))

    orchestrator.run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["pot_odds"],
            metadata={
                "tool_inputs": {
                    "pot_odds": {
                        "pot_before_bet": 100,
                        "opponent_bet": 50,
                        "call_cost": 50,
                    }
                }
            },
        ),
        run_id=run_id,
    )
    payloads = orchestrator.store.verified_payloads(run_id)
    sizes = [len(payload.exact_bytes) for payload in payloads]
    usage = orchestrator._run_machines[run_id].usage_snapshot()
    verified = orchestrator.product_store.read_current(run_id)

    assert usage.artifact_bytes == max(sizes)
    assert usage.run_bytes == sum(sizes)
    assert verified.read_status is RunReadStatus.SUCCEEDED


def test_tool_failure_has_non_retryable_production_classification(tmp_path: Path) -> None:
    registry = default_registry()
    executor = CapturingToolResearchExecutor(registry, record_sensitive_data=False)
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        registry=registry,
        tool_research_executor=executor,
        budget_policy=BudgetPolicyV2(max_tool_retries=3),
    )

    orchestrator.run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["pot_odds"],
            metadata={"tool_inputs": {"pot_odds": {}}},
        ),
        run_id="run-tool-retry-classification",
    )

    classification = executor.outcomes[0].output.retry_classifications[0]
    assert classification is not None
    assert classification.category is FailureCategory.TOOL_DETERMINISTIC
    assert not classification.retryable
    assert classification.max_retries == 3


def test_oversized_raw_tool_output_becomes_typed_budget_failure(tmp_path: Path) -> None:
    registry = ToolRegistry(max_output_bytes=1024)
    registry.register(
        ToolDefinition(
            name="big",
            purpose="oversized output fixture",
            exact_or_approximate="exact",
            supported_games=("fixture",),
            function=lambda _: {"x": "a" * 2000},
        )
    )
    executor = CapturingToolResearchExecutor(registry, record_sensitive_data=False)
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        registry=registry,
        tool_research_executor=executor,
        budget_policy=BudgetPolicyV2(max_tool_output_bytes=1024),
    ).run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["big"],
        ),
        run_id="run-oversized-raw-tool-output",
    )

    outcome = executor.outcomes[0].output
    assert report.run_status == "failed_with_limitations"
    assert outcome.budget_failure is not None
    assert outcome.budget_failure.code.value == "tool_output_exceeded"
    assert outcome.budget_failure.observed is not None
    assert outcome.retry_classifications[0].category is FailureCategory.BUDGET


def test_policy_caps_raw_tool_output_before_redaction_with_looser_registry(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(max_output_bytes=10_000)
    registry.register(
        ToolDefinition(
            name="secret_output",
            purpose="raw output cap fixture",
            exact_or_approximate="exact",
            supported_games=("fixture",),
            function=lambda _: {"secret": "sk-" + "a" * 4000},
        )
    )
    executor = CapturingToolResearchExecutor(registry, record_sensitive_data=False)
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        registry=registry,
        tool_research_executor=executor,
        budget_policy=BudgetPolicyV2(
            max_tool_output_bytes=1024,
            max_artifact_bytes=100_000,
        ),
    ).run(
        CaseInput(
            kind="calculation",
            analysis_scope="retrospective",
            requested_tools=["secret_output"],
        ),
        run_id="run-policy-raw-tool-output-cap",
    )

    outcome = executor.outcomes[0].output
    assert report.run_status == "failed_with_limitations"
    assert outcome.budget_failure is not None
    assert outcome.budget_failure.code.value == "tool_output_exceeded"
