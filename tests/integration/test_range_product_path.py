from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from poker_deliberation.budgets import BudgetPolicyV2, FakeMonotonicClock
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_grammar import verify_versioned_range_tool_chain
from poker_deliberation.reporting import render_summary
from poker_deliberation.schemas import (
    CaseInput,
    NumericalExactness,
    ToolStatus,
)
from poker_deliberation.storage.terminal_models import RunReadStatus
from poker_deliberation.tools import default_registry
from tests.range_support import versioned_range_hand


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _case(notation: str = "AKs@0.25,QQ@0.5") -> CaseInput:
    hand, _ = versioned_range_hand(notation)
    return CaseInput(
        kind="hand",
        hand=hand,
        analysis_scope="retrospective",
        requested_tools=["combos"],
    )


def test_product_path_auto_validates_then_runs_canonical_combos(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    case = _case()

    report = Orchestrator(config).run(case, run_id="p3-016a-range-product")

    validation = next(
        result for result in report.tool_results if result.tool_name == "range_validate"
    )
    combos = next(result for result in report.tool_results if result.tool_name == "combos")
    assert report.run_status == "completed"
    assert validation.status is ToolStatus.SUCCESS
    assert validation.numeric_exactness is NumericalExactness.EXACT
    assert validation.output["status"] == "success"
    assert combos.status is ToolStatus.SUCCESS
    assert combos.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
    assert combos.input == {
        "range": validation.output["canonical_notation"],
        "dead_cards": [],
    }
    assert combos.output["combo_count"] == validation.output["combo_count"]
    assert combos.verification is not None and combos.verification.passed
    verify_versioned_range_tool_chain(case, report.tool_results)
    summary = render_summary(report)
    assert "**CALCULATED** `range_validate` (`exact`)" in summary
    assert '"combo_count":8' in summary

    reader = Orchestrator(config)
    verified = reader.product_store.read_current(report.run_id)
    assert verified.read_status is RunReadStatus.SUCCEEDED
    assert reader.load_report(report.run_id) == report


def test_invalid_versioned_range_fails_before_combos(tmp_path: Path) -> None:
    report = Orchestrator(_config(tmp_path)).run(
        _case("QQ+"),
        run_id="p3-016a-range-invalid",
    )

    validation = next(
        result for result in report.tool_results if result.tool_name == "range_validate"
    )
    assert validation.status is ToolStatus.SUCCESS
    assert validation.output["status"] == "failed"
    assert validation.output["diagnostics"][0]["code"] == "RNG_E_SYNTAX"
    assert not any(result.tool_name == "combos" for result in report.tool_results)
    assert any("RNG_E_SYNTAX" in item for item in report.data_quality)


def test_conflicting_manual_combos_input_fails_closed(tmp_path: Path) -> None:
    case = _case()
    case.metadata = {
        "tool_inputs": {
            "combos": {
                "range": "AA",
                "dead_cards": [],
            }
        }
    }

    report = Orchestrator(_config(tmp_path)).run(
        case,
        run_id="p3-016a-range-conflict",
    )

    assert any(result.tool_name == "range_validate" for result in report.tool_results)
    assert not any(result.tool_name == "combos" for result in report.tool_results)
    assert any("conflicting combos input" in item for item in report.data_quality)


def test_semantic_replay_rejects_tampered_validation_output(tmp_path: Path) -> None:
    case = _case()
    report = Orchestrator(_config(tmp_path)).run(
        case,
        run_id="p3-016a-range-replay-tamper",
    )
    results = list(report.tool_results)
    index = next(
        index for index, result in enumerate(results) if result.tool_name == "range_validate"
    )
    tampered_output = dict(results[index].output)
    tampered_output["total_weight_millionths"] += 1
    results[index] = results[index].model_copy(update={"output": tampered_output})

    with pytest.raises(ValueError, match=r"incomplete|deterministic replay"):
        verify_versioned_range_tool_chain(case, results)


def test_semantic_replay_rejects_tampered_combos_verification_metadata(
    tmp_path: Path,
) -> None:
    case = _case()
    report = Orchestrator(_config(tmp_path)).run(
        case,
        run_id="p3-016a-range-verification-tamper",
    )
    results = list(report.tool_results)
    index = next(index for index, result in enumerate(results) if result.tool_name == "combos")
    verification = results[index].verification
    assert verification is not None
    results[index] = results[index].model_copy(
        update={
            "verification": verification.model_copy(
                update={
                    "method": "forged-method",
                    "observations": ["forged-observation"],
                }
            )
        }
    )

    with pytest.raises(ValueError, match="verification metadata"):
        verify_versioned_range_tool_chain(case, results)


def test_semantic_replay_requires_validation_before_combos(tmp_path: Path) -> None:
    case = _case()
    report = Orchestrator(_config(tmp_path)).run(
        case,
        run_id="p3-016a-range-order-tamper",
    )
    results = list(report.tool_results)
    validation_index = next(
        index for index, result in enumerate(results) if result.tool_name == "range_validate"
    )
    combos_index = next(
        index for index, result in enumerate(results) if result.tool_name == "combos"
    )
    results[validation_index], results[combos_index] = (
        results[combos_index],
        results[validation_index],
    )

    with pytest.raises(ValueError, match="must precede"):
        verify_versioned_range_tool_chain(case, results)


def test_actual_range_validation_failure_prevents_combos_execution(
    tmp_path: Path,
) -> None:
    clock = FakeMonotonicClock()
    registry = default_registry(
        max_duration_seconds=0.1,
        monotonic_clock=clock,
    )
    calls: list[str] = []
    original_range = registry._tools["range_validate"]
    original_combos = registry._tools["combos"]

    def slow_range(payload: dict[str, object]) -> dict[str, object]:
        calls.append("range_validate")
        output = original_range.function(payload)
        clock.advance_ns(200_000_000)
        return output

    def counted_combos(payload: dict[str, object]) -> dict[str, object]:
        calls.append("combos")
        return original_combos.function(payload)

    registry._tools["range_validate"] = replace(
        original_range,
        function=slow_range,
    )
    registry._tools["combos"] = replace(
        original_combos,
        function=counted_combos,
    )
    orchestrator = Orchestrator(
        _config(tmp_path),
        registry=registry,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=10.0),
    )

    report = orchestrator.run(
        _case(),
        run_id="p3-016a-range-runtime-failure",
    )

    assert report.run_status == "failed_with_limitations"
    assert calls == ["range_validate"]
    assert [
        (result.tool_name, result.status.value)
        for result in report.tool_results
        if result.tool_name in {"range_validate", "combos"}
    ] == [("range_validate", "failed")]
    assert (
        orchestrator.product_store.read_current(report.run_id).read_status is RunReadStatus.FAILED
    )


def test_actual_combos_failure_marks_product_run_failed(
    tmp_path: Path,
) -> None:
    clock = FakeMonotonicClock()
    registry = default_registry(
        max_duration_seconds=0.1,
        monotonic_clock=clock,
    )
    calls: list[str] = []
    original_range = registry._tools["range_validate"]
    original_combos = registry._tools["combos"]

    def counted_range(payload: dict[str, object]) -> dict[str, object]:
        calls.append("range_validate")
        return original_range.function(payload)

    def slow_combos(payload: dict[str, object]) -> dict[str, object]:
        calls.append("combos")
        output = original_combos.function(payload)
        clock.advance_ns(200_000_000)
        return output

    registry._tools["range_validate"] = replace(
        original_range,
        function=counted_range,
    )
    registry._tools["combos"] = replace(
        original_combos,
        function=slow_combos,
    )
    orchestrator = Orchestrator(
        _config(tmp_path),
        registry=registry,
        monotonic_clock=clock,
        budget_policy=BudgetPolicyV2(max_runtime_seconds=10.0),
    )

    report = orchestrator.run(
        _case(),
        run_id="p3-016a-combos-runtime-failure",
    )

    assert report.run_status == "failed_with_limitations"
    assert calls == ["range_validate", "combos"]
    assert [
        (result.tool_name, result.status.value)
        for result in report.tool_results
        if result.tool_name in {"range_validate", "combos"}
    ] == [
        ("range_validate", "success"),
        ("combos", "failed"),
    ]
    assert (
        orchestrator.product_store.read_current(report.run_id).read_status is RunReadStatus.FAILED
    )


def test_failed_terminal_before_tool_phase_preserves_empty_versioned_chain(
    tmp_path: Path,
) -> None:
    hand, _ = versioned_range_hand()
    case = CaseInput(
        kind="hand",
        hand=hand,
        requested_tools=["combos"],
    )

    report = Orchestrator(_config(tmp_path)).run(
        case,
        run_id="p3-016a-range-early-failure",
    )

    assert report.run_status == "failed_with_limitations"
    assert not any(
        result.tool_name in {"range_validate", "combos"} for result in report.tool_results
    )
    verify_versioned_range_tool_chain(
        case,
        report.tool_results,
        run_status=report.run_status,
    )
    with pytest.raises(ValueError, match="requires one bound validation result"):
        verify_versioned_range_tool_chain(case, report.tool_results)

    reader = Orchestrator(_config(tmp_path))
    verified = reader.product_store.read_current(report.run_id)
    assert verified.read_status is RunReadStatus.FAILED
    loaded = reader.load_report(report.run_id)
    assert loaded.run_status == "failed_with_limitations"
    assert "verified product run status: failed" in loaded.limitations
