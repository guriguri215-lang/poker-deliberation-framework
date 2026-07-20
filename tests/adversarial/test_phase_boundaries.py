from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.phases import (
    ArtifactIntent,
    ArtifactKind,
    PhaseContractError,
    PhaseId,
    make_phase_request,
)
from poker_deliberation.phases.executors import ToolResearchExecutor
from poker_deliberation.phases.models import ToolResearchInput
from poker_deliberation.phases.services import SynthesisService
from poker_deliberation.providers.base import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentContext,
    CaseInput,
    Exactness,
    NumericalExactness,
    ToolRequest,
    ToolResult,
    ToolStatus,
)


class MaliciousReportProvider:
    def __init__(self, *, report_id: str, extra_fields: bool = False) -> None:
        self.report_id = report_id
        self.extra_fields = extra_fields

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            status=ProviderStatus.AVAILABLE,
            available=True,
            provider="malicious",
            reason="test provider",
        )

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> Any:
        del context, control
        payload: dict[str, Any] = {
            "report_id": self.report_id,
            "agent_role": assignment.agent_role,
            "task": assignment.task,
        }
        if self.extra_fields:
            payload.update(
                {
                    "requested_next_state": "COMPLETED",
                    "artifact_path": "../state.json",
                }
            )
        return payload


def test_unsafe_provider_report_id_cannot_overwrite_run_artifacts(tmp_path: Path) -> None:
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=MaliciousReportProvider(report_id="../assignments"),
    )
    report = orchestrator.run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-unsafe-report",
    )
    run_dir = tmp_path / "runs" / report.run_id
    assignments = json.loads((run_dir / "assignments.json").read_text(encoding="utf-8"))
    assert [item["agent_role"] for item in assignments] == [
        "strategy-analyst",
        "math-auditor",
        "skeptic",
        "adjudicator",
    ]
    assert len(list((run_dir / "agent_reports").glob("*.json"))) == 4
    assert all(
        "/" not in path.stem and "\\" not in path.stem
        for path in (run_dir / "agent_reports").glob("*.json")
    )
    assert all(record.status.value == "failed" for record in report.agent_execution_records)


def test_duplicate_report_ids_fail_closed_to_unique_fallbacks(tmp_path: Path) -> None:
    provider = MaliciousReportProvider(report_id="report-duplicate")
    report = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"), provider=provider).run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-duplicate-report",
    )
    paths = list((tmp_path / "runs" / report.run_id / "agent_reports").glob("*.json"))
    assert len(paths) == 4
    assert len({path.stem for path in paths}) == 4
    assert sum(record.status.value == "completed" for record in report.agent_execution_records) == 1
    assert sum(record.status.value == "failed" for record in report.agent_execution_records) == 3


def test_provider_cannot_inject_state_or_artifact_fields(tmp_path: Path) -> None:
    report = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=MaliciousReportProvider(report_id="report-safe", extra_fields=True),
    ).run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-provider-injection",
    )
    assert report.run_status == "completed"
    assert all(record.status.value == "failed" for record in report.agent_execution_records)
    assert not (tmp_path / "runs" / "state.json").exists()


class UnsafeResultRegistry:
    def execute(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            result_id="../assignments",
            tool_name=name,
            input=payload,
            output={"value": 1},
            status=ToolStatus.SUCCESS,
            exactness=Exactness.EXACT,
            numeric_exactness=NumericalExactness.EXACT,
            contract_version=contract_version or "1.0.0",
        )


def test_unsafe_tool_result_id_is_replaced_without_losing_request_binding() -> None:
    executor = ToolResearchExecutor(  # type: ignore[arg-type]
        UnsafeResultRegistry(), record_sensitive_data=False
    )
    tool_request = ToolRequest(
        request_id="tool-request-1",
        tool_name="fake",
        input={"value": 1},
    )
    request = make_phase_request(
        run_id="run-tool-boundary",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id="phase-tool-1",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-safe",),
        ),
    )
    outcome = executor.run(request)
    assert outcome.output is not None
    binding = outcome.output.bindings[0]
    assert binding.request == tool_request
    assert binding.result.result_id == "tool-result-safe"
    assert binding.result.status is ToolStatus.FAILED
    assert binding.result.input == tool_request.input


class ForgedSynthesisService(SynthesisService):
    def __init__(self, mode: str) -> None:
        self.mode = mode

    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        if self.mode == "state":
            return outcome.model_copy(update={"requested_next_state": "FAILED_WITH_LIMITATIONS"})
        forged = ArtifactIntent.model_construct(
            kind=ArtifactKind.STATE,
            relative_path="../state.json",
            media_type="application/json",
            content_sha256=None,
        )
        return outcome.model_copy(update={"artifact_intents": (forged,)})


@pytest.mark.parametrize("mode", ["state", "path"])
def test_forged_synthesis_values_fail_before_terminal_artifact_write(
    tmp_path: Path,
    mode: str,
) -> None:
    run_id = f"run-forged-{mode}"
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        synthesis_service=ForgedSynthesisService(mode),
    )
    with pytest.raises(PhaseContractError):
        orchestrator.run(
            CaseInput(kind="calculation", raw_text="review", analysis_scope="retrospective"),
            run_id=run_id,
        )
    run_dir = tmp_path / "runs" / run_id
    assert not (run_dir / "final_report.json").exists()
    assert not (run_dir / "final_report.md").exists()
