from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from poker_deliberation.config import AppConfig
from poker_deliberation.context_lifecycle import build_context_envelope, context_payload
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.phases import (
    AnalysisExecutor,
    ArtifactIntent,
    ArtifactKind,
    PhaseContractError,
    PhaseFailureCode,
    PhaseId,
    PhaseStatus,
    make_phase_request,
    validate_tool_research_output,
)
from poker_deliberation.phases.contracts import successful_outcome
from poker_deliberation.phases.executors import ToolResearchExecutor
from poker_deliberation.phases.models import (
    ContextDispatch,
    ToolExecutionBinding,
    ToolResearchInput,
)
from poker_deliberation.phases.services import SynthesisService
from poker_deliberation.providers.base import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.providers.local import LocalProvider
from poker_deliberation.range_equity import expected_versioned_range_equity_input
from poker_deliberation.range_grammar import validate_versioned_range
from poker_deliberation.range_models import VersionedRangeDefinitionV1
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
from poker_deliberation.tools import contracts as tool_contracts
from poker_deliberation.tools.registry import default_registry
from tests.range_support import versioned_river_equity_case


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


def test_versioned_failure_prebind_does_not_execute_calculators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = versioned_river_equity_case()
    assert case.hand is not None
    definition = case.hand.known_ranges[0]
    assert isinstance(definition, VersionedRangeDefinitionV1)
    validation = validate_versioned_range(case.hand, definition)
    assert validation.status == "success"
    payloads = {
        "range_validate": {
            "schema_version": "1.0.0",
            "hand": case.hand.model_dump(mode="json"),
            "range_definition": definition.model_dump(mode="json"),
        },
        "combos": {"range": validation.canonical_notation, "dead_cards": []},
        "holdem_equity": expected_versioned_range_equity_input(case, validation),
    }

    def forbidden_calculation(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("failure prebind executed a calculator")

    monkeypatch.setattr(
        "poker_deliberation.range_grammar.validate_versioned_range",
        forbidden_calculation,
    )
    monkeypatch.setattr(
        "poker_deliberation.tools.combinations.parse_weighted_range",
        forbidden_calculation,
    )
    monkeypatch.setattr(
        "poker_deliberation.tools.equity.holdem_equity",
        forbidden_calculation,
    )

    for tool_name, payload in payloads.items():
        assert tool_contracts.versioned_range_bridge_failure_input_matches(
            tool_name,
            payload,
            "2.0.0",
        )


class ForgedAnalysisExecutor(AnalysisExecutor):
    def run(self, request):  # type: ignore[no-untyped-def]
        outcome = super().run(request)
        assert outcome.output is not None
        forged_report = outcome.output.report.model_copy(
            update={"report_id": "../state"},
            deep=True,
        )
        forged_output = outcome.output.model_copy(
            update={"report": forged_report},
            deep=True,
        )
        return successful_outcome(request, forged_output)


def test_unsafe_provider_report_id_cannot_overwrite_run_artifacts(tmp_path: Path) -> None:
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=MaliciousReportProvider(report_id="../assignments"),
    )
    report = orchestrator.run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-unsafe-report",
    )
    verified = orchestrator.product_store.read_current(report.run_id)
    assignments = json.loads(verified.payload_bytes("assignments.json"))
    report_names = [
        payload.inventory.logical_name
        for payload in verified.payloads
        if payload.inventory.logical_name.startswith("agent_reports/")
    ]
    assert [item["agent_role"] for item in assignments] == [
        "strategy-analyst",
        "math-auditor",
        "skeptic",
        "adjudicator",
    ]
    assert len(report_names) == 4
    assert all("/" not in Path(name).stem and "\\" not in Path(name).stem for name in report_names)
    assert all(record.status.value == "failed" for record in report.agent_execution_records)


def test_duplicate_report_ids_fail_closed_to_unique_fallbacks(tmp_path: Path) -> None:
    provider = MaliciousReportProvider(report_id="report-duplicate")
    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "runs"), provider=provider)
    report = orchestrator.run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-duplicate-report",
    )
    verified = orchestrator.product_store.read_current(report.run_id)
    report_names = [
        payload.inventory.logical_name
        for payload in verified.payloads
        if payload.inventory.logical_name.startswith("agent_reports/")
    ]
    assert len(report_names) == 4
    assert len({Path(name).stem for name in report_names}) == 4
    assert sum(record.status.value == "completed" for record in report.agent_execution_records) == 1
    assert sum(record.status.value == "failed" for record in report.agent_execution_records) == 3


def test_report_id_made_unsafe_by_redaction_uses_safe_fallback(tmp_path: Path) -> None:
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=MaliciousReportProvider(report_id="sk-abcdefghijk"),
    )
    report = orchestrator.run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-redacted-report-id",
    )
    verified = orchestrator.product_store.read_current(report.run_id)
    report_names = [
        payload.inventory.logical_name
        for payload in verified.payloads
        if payload.inventory.logical_name.startswith("agent_reports/")
    ]
    assert len(report_names) == 4
    assert "agent_reports/[REDACTED].json" not in report_names
    assert all(record.status.value == "failed" for record in report.agent_execution_records)


def test_provider_cannot_inject_state_or_artifact_fields(tmp_path: Path) -> None:
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        provider=MaliciousReportProvider(report_id="report-safe", extra_fields=True),
    )
    report = orchestrator.run(
        CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
        run_id="run-provider-injection",
    )
    assert report.run_status == "completed"
    assert all(record.status.value == "failed" for record in report.agent_execution_records)
    assert not (orchestrator.product_store.runs_root / "state.json").exists()
    assert all(
        ".." not in Path(payload.inventory.logical_name).parts
        for payload in orchestrator.product_store.read_current(report.run_id).payloads
    )


def test_forged_analysis_output_fails_before_report_materialization(tmp_path: Path) -> None:
    fixed = datetime(2026, 7, 20, 23, 59, tzinfo=UTC)
    executor = ForgedAnalysisExecutor(
        LocalProvider(),
        context_clock=lambda: fixed,
        record_clock=lambda: fixed,
    )
    run_id = "run-forged-analysis"
    orchestrator = Orchestrator(
        AppConfig(runs_dir=tmp_path / "runs"),
        context_clock=lambda: fixed,
        analysis_executor=executor,
    )
    with pytest.raises(PhaseContractError, match="analysis output report ID"):
        orchestrator.run(
            CaseInput(kind="strategy", raw_text="review", analysis_scope="retrospective"),
            run_id=run_id,
        )
    assert not list((tmp_path / "runs" / run_id / "agent_reports").glob("*.json"))


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

    def execute_for_phase(
        self,
        name: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        return self.execute(name, payload, contract_version=kwargs.get("contract_version"))

    def reverify_materialized_result(self, _result: ToolResult) -> None:
        return None


class MismatchedContractRegistry(UnsafeResultRegistry):
    def execute(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
    ) -> ToolResult:
        del contract_version
        return ToolResult(
            result_id="tool-result-contract-mismatch",
            tool_name=name,
            input=payload,
            output={"value": 1},
            status=ToolStatus.SUCCESS,
            exactness=Exactness.EXACT,
            numeric_exactness=NumericalExactness.EXACT,
            contract_version="999.0.0",
        )


class RedactedResultIdRegistry(UnsafeResultRegistry):
    def execute(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
    ) -> ToolResult:
        return ToolResult(
            result_id="sk-abcdefghijk",
            tool_name=name,
            input=payload,
            output={"value": 1},
            status=ToolStatus.SUCCESS,
            exactness=Exactness.EXACT,
            numeric_exactness=NumericalExactness.EXACT,
            contract_version=contract_version or "1.0.0",
        )


class SelfAttestedFloatingRegistry:
    def execute(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
    ) -> ToolResult:
        result = default_registry().execute(
            name,
            payload,
            contract_version=contract_version,
        )
        assert result.verification is not None
        return result.model_copy(
            update={
                "verification": result.verification.model_copy(
                    update={"observations": ["self-attested verification"]}
                )
            }
        )

    def execute_for_phase(
        self,
        name: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> ToolResult:
        return self.execute(name, payload, contract_version=kwargs.get("contract_version"))

    def reverify_materialized_result(self, result: ToolResult) -> None:
        default_registry().reverify_materialized_result(result)


class ExecuteOnlyRegistry:
    def __init__(self) -> None:
        self.executed = False

    def execute(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
    ) -> ToolResult:
        self.executed = True
        raise AssertionError((name, payload, contract_version))


def test_tool_executor_fails_closed_when_registry_lacks_phase_isolation() -> None:
    registry = ExecuteOnlyRegistry()
    tool_request = ToolRequest(
        request_id="tool-request-no-isolation",
        tool_name="pot_odds",
        input={
            "pot_before_bet": 100.0,
            "opponent_bet": 50.0,
            "call_cost": 50.0,
        },
        contract_version="2.0.0",
    )
    request = make_phase_request(
        run_id="run-tool-no-isolation",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id="phase-tool-no-isolation",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-no-isolation",),
        ),
    )

    outcome = ToolResearchExecutor(  # type: ignore[arg-type]
        registry,
        record_sensitive_data=False,
    ).run(request)

    assert outcome.status is PhaseStatus.FAILED
    assert outcome.failure is not None
    assert outcome.failure.code is PhaseFailureCode.VALIDATION
    assert "lacks hard-isolated phase execution" in outcome.failure.message
    assert not registry.executed


def test_tool_executor_rejects_self_attested_floating_verification() -> None:
    tool_request = ToolRequest(
        request_id="tool-request-self-attested",
        tool_name="pot_odds",
        input={
            "pot_before_bet": 100.0,
            "opponent_bet": 50.0,
            "call_cost": 50.0,
            "expected_rake": 0.0,
        },
        contract_version="2.0.0",
    )
    request = make_phase_request(
        run_id="run-tool-self-attested",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id="phase-tool-self-attested",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-self-attested",),
        ),
    )

    outcome = ToolResearchExecutor(  # type: ignore[arg-type]
        SelfAttestedFloatingRegistry(), record_sensitive_data=False
    ).run(request)

    assert outcome.status is PhaseStatus.FAILED
    assert outcome.output is None
    assert outcome.failure is not None
    assert outcome.failure.code is PhaseFailureCode.VALIDATION
    assert "executable verification mismatch" in outcome.failure.message


def test_tool_executor_preserves_fresh_hand_validator_timeout_as_typed_output() -> None:
    case = versioned_river_equity_case()
    assert case.hand is not None
    registry = default_registry(max_duration_seconds=0.001)
    definition = registry._tools["hand_validator"]
    assert definition.contract is not None
    tool_request = ToolRequest(
        request_id="tool-request-hand-timeout",
        tool_name="hand_validator",
        input=case.hand.model_dump(mode="json"),
        contract_version=definition.contract.contract_version,
    )
    request = make_phase_request(
        run_id="run-tool-hand-timeout",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id="phase-tool-hand-timeout",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-hand-timeout",),
        ),
    )

    outcome = ToolResearchExecutor(registry, record_sensitive_data=False).run(request)

    assert outcome.status is PhaseStatus.COMPLETED_WITH_FAILURES
    assert outcome.failure is None
    assert outcome.output is not None
    result = outcome.output.bindings[0].result
    assert result.status is ToolStatus.FAILED
    assert result.output == {}
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE


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


def test_tool_result_id_made_unsafe_by_redaction_uses_safe_fallback() -> None:
    executor = ToolResearchExecutor(  # type: ignore[arg-type]
        RedactedResultIdRegistry(), record_sensitive_data=False
    )
    tool_request = ToolRequest(
        request_id="tool-request-redaction",
        tool_name="fake",
        input={"value": 1},
    )
    request = make_phase_request(
        run_id="run-tool-redaction",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id="phase-tool-redaction",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-safe",),
        ),
    )
    outcome = executor.run(request)
    assert outcome.output is not None
    result = outcome.output.bindings[0].result
    assert result.result_id == "tool-result-safe"
    assert result.status is ToolStatus.FAILED
    assert "unsafe or duplicate" in (result.error or "")


def test_successful_tool_result_with_wrong_contract_version_fails_closed() -> None:
    executor = ToolResearchExecutor(  # type: ignore[arg-type]
        MismatchedContractRegistry(), record_sensitive_data=False
    )
    tool_request = ToolRequest(
        request_id="tool-request-contract",
        tool_name="fake",
        input={"value": 1},
        contract_version="2.0.0",
    )
    request = make_phase_request(
        run_id="run-tool-contract",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id="phase-tool-contract",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-safe",),
        ),
    )
    outcome = executor.run(request)
    assert outcome.status is PhaseStatus.FAILED
    assert outcome.output is None
    assert outcome.failure is not None
    assert outcome.failure.code is PhaseFailureCode.CORRELATION
    assert "correlation mismatch" in outcome.failure.message


@pytest.mark.parametrize(
    ("tool_name", "requested_by"),
    [
        ("range_validate", "versioned-range-product"),
        ("combos", "versioned-range-product"),
        ("holdem_equity", "versioned-range-bridge"),
    ],
)
def test_failed_bridge_result_with_wrong_contract_version_fails_phase_correlation(
    tool_name: str,
    requested_by: str,
) -> None:
    case = versioned_river_equity_case()
    assert case.hand is not None
    definition = case.hand.known_ranges[0]
    assert isinstance(definition, VersionedRangeDefinitionV1)
    validation_payload = {
        "schema_version": "1.0.0",
        "hand": case.hand.model_dump(mode="json"),
        "range_definition": definition.model_dump(mode="json"),
    }
    validation = validate_versioned_range(case.hand, definition)
    assert validation.status == "success"
    payloads = {
        "range_validate": validation_payload,
        "combos": {"range": validation.canonical_notation, "dead_cards": []},
        "holdem_equity": expected_versioned_range_equity_input(case, validation),
    }
    payload = payloads[tool_name]
    registry = default_registry()
    direct = registry.execute(
        tool_name,
        payload,
        contract_version="999.0.0",
        _bind_versioned_range_failure=True,
    )
    assert direct.status is ToolStatus.FAILED
    assert "contract version mismatch" in (direct.error or "")
    assert "versioned-range bridge contract" not in (direct.error or "")
    tool_request = ToolRequest(
        request_id=f"tool-request-version-{tool_name}",
        tool_name=tool_name,
        input=payload,
        requested_by=requested_by,
        contract_version="999.0.0",
    )
    request = make_phase_request(
        run_id=f"run-tool-version-{tool_name}",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id=f"phase-tool-version-{tool_name}",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=(f"tool-result-version-{tool_name}",),
        ),
    )

    outcome = ToolResearchExecutor(registry, record_sensitive_data=False).run(request)

    assert outcome.status is PhaseStatus.FAILED
    assert outcome.output is None
    assert outcome.failure is not None
    assert outcome.failure.code is PhaseFailureCode.CORRELATION


@pytest.mark.parametrize(
    "requested_by",
    ["versioned-range-product", "versioned-range-bridge"],
)
def test_forged_versioned_range_marker_cannot_bind_non_bridge_failure(
    requested_by: str,
) -> None:
    tool_request = ToolRequest(
        request_id=f"tool-request-marker-{requested_by}",
        tool_name="hand_validator",
        input={},
        requested_by=requested_by,
    )
    request = make_phase_request(
        run_id="run-tool-marker",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id=f"phase-tool-marker-{requested_by}",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-marker",),
        ),
    )

    outcome = ToolResearchExecutor(default_registry(), record_sensitive_data=False).run(request)

    assert outcome.output is not None
    result = outcome.output.bindings[0].result
    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert "ValidationError" in result.error
    assert "versioned-range bridge contract" not in result.error


@pytest.mark.parametrize(
    ("payload_update", "expected_diagnostic"),
    [
        ({"game_type": "PLO"}, "NLHE only"),
        ({"opponent_ranges": ["QQ"]}, "exactly one villain"),
    ],
)
def test_forged_bridge_marker_preserves_ordinary_equity_diagnostic(
    payload_update: dict[str, object],
    expected_diagnostic: str,
) -> None:
    payload: dict[str, object] = {
        "hero_range": "AsAh",
        "villain_range": "KsKh",
        "board": ["2c", "3d", "4h", "5s", "6c"],
        "dead_cards": [],
        "game_type": "NLHE",
        "mode": "exact",
        "max_exact_evaluations": 990,
    }
    payload.update(payload_update)
    tool_request = ToolRequest(
        request_id="tool-request-forged-bridge",
        tool_name="holdem_equity",
        input=payload,
        requested_by="versioned-range-bridge",
        contract_version="2.0.0",
    )
    request = make_phase_request(
        run_id="run-tool-forged-bridge",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id="phase-tool-forged-bridge",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-forged-bridge",),
        ),
    )

    registry = default_registry()
    direct = registry.execute(
        "holdem_equity",
        payload,
        contract_version="2.0.0",
        _bind_versioned_range_failure=True,
    )
    assert expected_diagnostic in (direct.error or "")
    assert "versioned-range bridge contract" not in (direct.error or "")

    outcome = ToolResearchExecutor(registry, record_sensitive_data=False).run(request)

    assert outcome.output is not None
    result = outcome.output.bindings[0].result
    assert result.status is ToolStatus.FAILED
    assert result.error is not None
    assert expected_diagnostic in result.error
    assert "versioned-range bridge contract" not in result.error
    binding_payload = outcome.output.bindings[0].model_dump(mode="python")
    binding_payload["request"]["contract_version"] = "999.0.0"
    binding_payload["requested_contract_version"] = "999.0.0"
    with pytest.raises(ValidationError, match="tool result contract version mismatch"):
        ToolExecutionBinding.model_validate(binding_payload)


@pytest.mark.parametrize(
    ("tool_name", "payload", "requested_by"),
    [
        (
            "combos",
            {"range": "not-a-range", "dead_cards": []},
            "versioned-range-product",
        ),
        (
            "holdem_equity",
            {
                "hero_range": "not-a-range",
                "villain_range": "KsKh",
                "board": ["2c", "3d", "4h", "5s", "6c"],
                "dead_cards": [],
                "game_type": "NLHE",
                "mode": "exact",
                "max_exact_evaluations": 990,
            },
            "versioned-range-bridge",
        ),
    ],
)
def test_forged_bridge_marker_preserves_invalid_range_diagnostic(
    tool_name: str,
    payload: dict[str, object],
    requested_by: str,
) -> None:
    tool_request = ToolRequest(
        request_id=f"tool-request-invalid-{tool_name}",
        tool_name=tool_name,
        input=payload,
        requested_by=requested_by,
        contract_version="2.0.0",
    )
    request = make_phase_request(
        run_id=f"run-tool-invalid-{tool_name}",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id=f"phase-tool-invalid-{tool_name}",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=(f"tool-result-invalid-{tool_name}",),
        ),
    )

    registry = default_registry()
    direct = registry.execute(
        tool_name,
        payload,
        contract_version="2.0.0",
        _bind_versioned_range_failure=True,
    )
    assert "unsupported range token" in (direct.error or "")
    assert "versioned-range bridge contract" not in (direct.error or "")

    outcome = ToolResearchExecutor(registry, record_sensitive_data=False).run(request)

    assert outcome.output is not None
    result = outcome.output.bindings[0].result
    assert result.status is ToolStatus.FAILED
    assert "unsupported range" in (result.error or "")
    assert "versioned-range bridge contract" not in (result.error or "")


def test_tool_binding_from_another_phase_attempt_is_rejected() -> None:
    executor = ToolResearchExecutor(  # type: ignore[arg-type]
        UnsafeResultRegistry(), record_sensitive_data=False
    )
    tool_request = ToolRequest(
        request_id="tool-request-outer",
        tool_name="fake",
        input={"value": 1},
        contract_version="2.0.0",
    )
    request = make_phase_request(
        run_id="run-tool-outer",
        phase_id=PhaseId.TOOL_RESEARCH,
        attempt_id="phase-tool-outer",
        policy_snapshot_hash="a" * 64,
        input_value=ToolResearchInput(
            requests=(tool_request,),
            fallback_result_ids=("tool-result-safe",),
        ),
    )
    outcome = executor.run(request)
    assert outcome.output is not None
    forged = outcome.output.model_copy(
        update={
            "bindings": (outcome.output.bindings[0].model_copy(update={"run_id": "run-other"}),)
        },
        deep=True,
    )
    with pytest.raises(PhaseContractError, match="binding correlation mismatch"):
        validate_tool_research_output(request, forged)


def test_context_dispatch_rejects_context_from_another_envelope_payload() -> None:
    context = AgentContext(kind="strategy", objective="ENVELOPE-A", strategy_text="review")
    assignment = AgentAssignment(
        assignment_id="assignment-context",
        agent_role="strategy-analyst",
        task="review",
        context_keys=sorted(context_payload(context)),
    )
    now = datetime(2026, 7, 20, tzinfo=UTC)
    envelope = build_context_envelope(
        context,
        assignment,
        run_id="run-context",
        expires_at=now + timedelta(minutes=1),
        clock=lambda: now,
        context_id="context-a",
        attempt_id="attempt-a",
    )
    tampered_context = context.model_copy(update={"objective": "DISPATCH-B"}, deep=True)
    with pytest.raises(ValidationError, match="canonical envelope payload"):
        ContextDispatch(
            assignment=assignment,
            context=tampered_context,
            envelope=envelope,
        )


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
    current = orchestrator.product_store.runs_root / run_id / ".terminal-store" / "current.json"
    assert not current.exists()
