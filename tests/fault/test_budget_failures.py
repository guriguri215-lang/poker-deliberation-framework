from __future__ import annotations

from pathlib import Path
from time import sleep

import pytest

from poker_deliberation.budgets import (
    BudgetFailureCode,
    BudgetLimitError,
    BudgetPolicyV2,
    ExecutionClass,
    FakeMonotonicClock,
    SerialUsageLedger,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport, CaseInput


class _BaseProvider:
    def __init__(self) -> None:
        self.calls = 0

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            status=ProviderStatus.AVAILABLE,
            available=True,
            provider="fault-test",
            reason="test provider",
            execution_class=ExecutionClass.LOCAL_FREE,
        )


class RawTimeoutProvider(_BaseProvider):
    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, assignment, control
        self.calls += 1
        raise TimeoutError("provider-internal timeout")


class CooperativeDeadlineProvider(_BaseProvider):
    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, assignment
        self.calls += 1
        while not control.cancelled:
            sleep(0.001)
        control.raise_if_cancelled()
        raise AssertionError("cooperative cancellation must raise")


class UncooperativeDeadlineProvider(_BaseProvider):
    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        del context, control
        self.calls += 1
        sleep(0.3)
        return AgentReport(
            report_id=f"late-{assignment.agent_role}",
            agent_role=assignment.agent_role,
            task=assignment.task,
        )


def _strategy_case() -> CaseInput:
    return CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective")


def test_raw_timeout_error_is_not_misreported_as_deadline_or_retried(tmp_path: Path) -> None:
    provider = RawTimeoutProvider()

    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
    ).run(_strategy_case(), run_id="run-raw-timeout")

    assert provider.calls == 4
    assert all(record.status.value == "fallback" for record in report.agent_execution_records)
    assert not any("deadline" in item for item in report.data_quality)


def test_cooperative_deadline_records_cancelled_without_hard_stop_claim(
    tmp_path: Path,
) -> None:
    provider = CooperativeDeadlineProvider()
    clock = FakeMonotonicClock()

    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=0.05),
    ).run(_strategy_case(), run_id="run-cooperative-deadline")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert not any("cancellation was not confirmed" in item for item in report.data_quality)


def test_uncooperative_deadline_surfaces_cancel_unconfirmed(tmp_path: Path) -> None:
    provider = UncooperativeDeadlineProvider()
    clock = FakeMonotonicClock()

    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=provider,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=0.05),
    ).run(_strategy_case(), run_id="run-uncooperative-deadline")

    assert provider.calls == 1
    assert report.run_status == "failed_with_limitations"
    assert "provider cancellation was not confirmed" in report.data_quality


class MalformedClock:
    def now_ns(self) -> int:
        return "not-an-integer"  # type: ignore[return-value]


def test_malformed_clock_fails_as_typed_usage_error() -> None:
    with pytest.raises(BudgetLimitError) as error:
        SerialUsageLedger(BudgetPolicyV2(), clock=MalformedClock())

    assert error.value.failure.code is BudgetFailureCode.USAGE_MALFORMED


@pytest.mark.parametrize(
    ("policy", "case", "expected_code"),
    [
        (
            BudgetPolicyV2(max_artifact_bytes=1024, max_run_bytes=10_240),
            CaseInput(kind="calculation", raw_text="x"),
            "artifact_exceeded",
        ),
        (
            BudgetPolicyV2(max_artifact_bytes=8000, max_run_bytes=10_240),
            CaseInput(kind="calculation", raw_text="x" * 6000),
            "run_exceeded",
        ),
    ],
)
def test_storage_byte_exhaustion_returns_structured_limitation(
    tmp_path: Path,
    policy: BudgetPolicyV2,
    case: CaseInput,
    expected_code: str,
) -> None:
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / expected_code),
        budget_policy=policy,
    ).run(case, run_id=f"run-{expected_code}")

    assert report.run_status == "failed_with_limitations"
    assert any(expected_code in item for item in report.data_quality)
