from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_equity import (
    VersionedRangeRiverEquityError,
    admit_versioned_range_river_equity,
    build_versioned_range_river_equity_result,
    expected_versioned_range_equity_input,
    verify_versioned_range_river_equity_tool_chain,
)
from poker_deliberation.range_grammar import validate_versioned_range
from poker_deliberation.schemas import CaseInput, ToolStatus
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
)
from poker_deliberation.tools import default_registry
from tests.range_support import versioned_river_equity_case


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _all_starting_classes() -> str:
    ranks = "AKQJT98765432"
    tokens = [rank + rank for rank in ranks]
    for first_index, first in enumerate(ranks):
        for second in ranks[first_index + 1 :]:
            tokens.extend((first + second + "s", first + second + "o"))
    return ",".join(tokens)


def test_dedicated_product_path_runs_bound_tools_and_replays_storage(tmp_path: Path) -> None:
    config = _config(tmp_path)
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    orchestrator = Orchestrator(config)

    report = orchestrator.run_versioned_range_river_equity(
        admission,
        run_id="p3-016b-range-equity-product",
    )

    assert report.run_status == "completed"
    assert [result.tool_name for result in report.tool_results] == [
        "range_validate",
        "combos",
        "holdem_equity",
    ]
    assert all(result.status is ToolStatus.SUCCESS for result in report.tool_results)
    bridge = build_versioned_range_river_equity_result(admission.case, report.tool_results)
    assert (bridge.equity_numerator, bridge.equity_denominator) == (3, 4)
    assert (
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            report.tool_results,
        )
        == bridge
    )

    reader = Orchestrator(config)
    verified = reader.product_store.read_current(report.run_id)
    assert verified.read_status is RunReadStatus.SUCCEEDED
    assert verified.payload_bytes("range_equity_binding.json") == canonical_json_bytes(
        admission.binding
    )
    assert reader.load_report(report.run_id) == report


def test_generic_run_rejects_a_caller_supplied_bridge_marker(tmp_path: Path) -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_PROVENANCE"):
        Orchestrator(_config(tmp_path)).run(
            admission.case,
            run_id="p3-016b-marker-bypass",
        )


def test_generic_unmarked_run_preserves_the_legacy_tool_path(tmp_path: Path) -> None:
    orchestrator = Orchestrator(_config(tmp_path))

    report = orchestrator.run(
        versioned_river_equity_case(),
        run_id="p3-016b-legacy-unmarked-shape",
    )

    assert report.run_status == "completed"
    assert [(item.tool_name, item.status) for item in report.tool_results] == [
        ("range_validate", ToolStatus.SUCCESS),
        ("combos", ToolStatus.SUCCESS),
        ("holdem_equity", ToolStatus.FAILED),
    ]
    assert (
        orchestrator.product_store.read_current(report.run_id).read_status
        is RunReadStatus.SUCCEEDED
    )


def test_generic_unmarked_manual_exact_equity_input_remains_supported(tmp_path: Path) -> None:
    candidate = versioned_river_equity_case()
    assert candidate.hand is not None
    validation = validate_versioned_range(candidate.hand, candidate.hand.known_ranges[0])
    payload = candidate.model_dump(mode="python")
    payload["metadata"] = {
        "tool_inputs": {
            "holdem_equity": expected_versioned_range_equity_input(candidate, validation),
        }
    }
    ordinary = CaseInput.model_validate(payload)
    orchestrator = Orchestrator(_config(tmp_path))

    report = orchestrator.run(ordinary, run_id="p3-016b-legacy-manual-exact")

    assert report.run_status == "completed"
    assert [(item.tool_name, item.status) for item in report.tool_results] == [
        ("range_validate", ToolStatus.SUCCESS),
        ("combos", ToolStatus.SUCCESS),
        ("holdem_equity", ToolStatus.SUCCESS),
    ]
    assert (
        orchestrator.product_store.read_current(report.run_id).read_status
        is RunReadStatus.SUCCEEDED
    )


def test_failed_combos_stops_before_equity(tmp_path: Path) -> None:
    registry = default_registry()
    calls: list[str] = []
    original_combos = registry._tools["combos"]
    original_equity = registry._tools["holdem_equity"]

    def failing_combos(_payload: dict[str, object]) -> dict[str, object]:
        calls.append("combos")
        raise ValueError("fixture combos failure")

    def counted_equity(payload: dict[str, object]) -> dict[str, object]:
        calls.append("holdem_equity")
        return original_equity.function(payload)

    registry._tools["combos"] = replace(
        original_combos,
        function=failing_combos,
        phase_isolated=False,
    )
    registry._tools["holdem_equity"] = replace(
        original_equity,
        function=counted_equity,
        phase_isolated=False,
    )
    orchestrator = Orchestrator(_config(tmp_path), registry=registry)
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())

    report = orchestrator.run_versioned_range_river_equity(
        admission,
        run_id="p3-016b-combos-failure",
    )

    assert report.run_status == "failed_with_limitations"
    assert calls == ["combos"]
    assert [result.tool_name for result in report.tool_results] == ["range_validate", "combos"]
    assert report.tool_results[-1].status is ToolStatus.FAILED
    assert report.tool_results[-1].error == "ValueError: fixture combos failure"
    assert (
        "product persistence refused: tool result lacks independent replay authority"
        in report.limitations
    )
    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


def test_full_169_class_river_range_is_bounded_to_990_evaluations(tmp_path: Path) -> None:
    admission = admit_versioned_range_river_equity(
        versioned_river_equity_case(_all_starting_classes())
    )

    report = Orchestrator(_config(tmp_path)).run_versioned_range_river_equity(
        admission,
        run_id="p3-016b-max-river-range",
    )

    bridge = build_versioned_range_river_equity_result(admission.case, report.tool_results)
    equity = report.tool_results[-1]
    assert report.run_status == "completed"
    assert admission.binding.combo_count == 990
    assert bridge.combo_count == 990
    assert equity.output["evaluations"] == 990
    assert equity.input["max_exact_evaluations"] == 990
