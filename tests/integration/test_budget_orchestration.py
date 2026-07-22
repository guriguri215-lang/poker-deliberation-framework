from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.budgets import BudgetPolicyV2, ExecutionClass, FakeMonotonicClock
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport, CaseInput


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
    assert any("strict budget" in item for item in report.data_quality)


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
        ExecutionClass.LOCAL_FREE,
        None,
        clock=clock,
        advance_ns=2_000_000_000,
    )
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=1.0),
    ).run(_strategy_case(), run_id="run-runtime-overrun")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert any("usage settlement" in item for item in report.data_quality)


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
