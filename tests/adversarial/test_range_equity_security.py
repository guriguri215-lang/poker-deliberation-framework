from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import poker_deliberation.orchestrator as orchestrator_module
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_equity import (
    VersionedRangeRiverEquityError,
    admit_versioned_range_river_equity,
    verify_versioned_range_river_equity_tool_chain,
    versioned_range_river_equity_binding,
)
from poker_deliberation.range_equity_models import (
    BINDING_HASH_DOMAIN,
    canonical_domain_sha256,
)
from poker_deliberation.range_grammar import action_prefix_sha256
from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import CaseInput, Exactness, NumericalExactness, ToolStatus
from poker_deliberation.storage.range_equity_admission_store import (
    commit_range_equity_admission_record,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    _validate_source_graph,
    canonical_json_bytes,
    run_lock_key_sha256,
)
from poker_deliberation.storage.terminal_canonical import product_payload_commitments
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
)
from poker_deliberation.tools import default_registry
from tests.range_support import versioned_river_equity_case


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def _completed(tmp_path: Path):
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    orchestrator = Orchestrator(_config(tmp_path))
    report = orchestrator.run_versioned_range_river_equity(
        admission,
        run_id="p3-016b-security",
    )
    return admission, orchestrator, report


def test_replay_rejects_a_different_equity_range(tmp_path: Path) -> None:
    admission, _orchestrator, report = _completed(tmp_path)
    results = list(report.tool_results)
    equity = results[-1]
    forged_input = dict(equity.input)
    forged_input["villain_range"] = "QcQd"
    results[-1] = equity.model_copy(update={"input": forged_input})

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        verify_versioned_range_river_equity_tool_chain(admission.case, results)


def test_replay_rejects_forged_equity_value(tmp_path: Path) -> None:
    admission, _orchestrator, report = _completed(tmp_path)
    results = list(report.tool_results)
    equity = results[-1]
    forged_output = dict(equity.output)
    forged_output["hero_equity"] = 0.5
    results[-1] = equity.model_copy(update={"output": forged_output})

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_ORACLE"):
        verify_versioned_range_river_equity_tool_chain(admission.case, results)


def test_failed_equity_replay_rejects_partial_output(tmp_path: Path) -> None:
    admission, _orchestrator, report = _completed(tmp_path)
    results = list(report.tool_results)
    equity = results[-1]
    results[-1] = equity.model_copy(
        update={
            "status": ToolStatus.FAILED,
            "exactness": Exactness.UNAVAILABLE,
            "numeric_exactness": NumericalExactness.UNAVAILABLE,
            "output": {"hero_equity": 0.5},
            "verification": None,
            "error": "fixture failure",
        }
    )

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            results,
            run_status="failed_with_limitations",
        )


def test_terminal_commitment_replay_rejects_forged_equity_artifact(tmp_path: Path) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    equity_name = next(
        name
        for name, data in payloads.items()
        if name.startswith("tool_results/")
        and name.endswith(".json")
        and not name.endswith(".input.json")
        and json.loads(data).get("tool_name") == "holdem_equity"
    )
    forged = json.loads(payloads[equity_name])
    forged["output"]["hero_equity"] = 0.5
    payloads[equity_name] = canonical_json_bytes(forged)

    with pytest.raises(CanonicalStorageError):
        product_payload_commitments(
            payloads,
            run_id=report.run_id,
            status="succeeded",
        )


def test_admission_rejects_a_third_eligible_player() -> None:
    candidate = versioned_river_equity_case()
    payload = candidate.model_dump(mode="python")
    payload["hand"]["actions"][0]["action"] = "check"
    modified = CaseInput.model_validate(payload)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_TARGET"):
        admit_versioned_range_river_equity(modified)


def test_admission_rejects_multiple_versioned_ranges() -> None:
    candidate = versioned_river_equity_case()
    payload = candidate.model_dump(mode="python")
    duplicate = dict(payload["hand"]["known_ranges"][0])
    duplicate["range_id"] = "second-range"
    payload["hand"]["known_ranges"].append(duplicate)
    modified = CaseInput.model_validate(payload)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_RANGE"):
        admit_versioned_range_river_equity(modified)


def test_admission_rejects_a_nonriver_range_condition() -> None:
    candidate = versioned_river_equity_case()
    payload = candidate.model_dump(mode="python")
    payload["hand"]["known_ranges"][0]["game_conditions"]["street"] = "turn"
    modified = CaseInput.model_validate(payload)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_DECISION"):
        admit_versioned_range_river_equity(modified)


def test_admission_rejects_target_all_in() -> None:
    candidate = versioned_river_equity_case()
    payload = candidate.model_dump(mode="python")
    payload["hand"]["actions"][-1]["action"] = "all_in"
    hand = candidate.hand.model_validate(payload["hand"])
    payload["hand"]["known_ranges"][0]["game_conditions"]["action_prefix_sha256"] = (
        action_prefix_sha256(hand, 2)
    )

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_DECISION"):
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))


def test_binding_hash_tamper_retains_provenance_reason_code() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    payload = admission.case.model_dump(mode="python")
    payload["metadata"]["versioned_range_river_equity"]["binding_sha256"] = "0" * 64
    forged = CaseInput.model_validate(payload)

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        versioned_range_river_equity_binding(forged)

    assert error.value.code.value == "REQ_E_PROVENANCE"


def test_schema_input_cannot_spoof_the_provenance_reason_code() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    payload = admission.case.model_dump(mode="python")
    binding = payload["metadata"]["versioned_range_river_equity"]
    binding["range_id"] = "!REQ_E_PROVENANCE"
    binding.pop("binding_sha256")
    binding["binding_sha256"] = canonical_domain_sha256(BINDING_HASH_DOMAIN, binding)
    forged = CaseInput.model_validate(payload)

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        versioned_range_river_equity_binding(forged)

    assert error.value.code.value == "REQ_E_SCHEMA"


def test_present_null_binding_marker_is_schema_error() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    payload = admission.case.model_dump(mode="python")
    payload["metadata"]["versioned_range_river_equity"] = None

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        versioned_range_river_equity_binding(CaseInput.model_validate(payload))

    assert error.value.code.value == "REQ_E_SCHEMA"


def test_unmarked_bridge_shape_remains_indistinguishable_from_legacy_manual_input(
    tmp_path: Path,
) -> None:
    admission, _orchestrator, report = _completed(tmp_path)

    assert (
        verify_versioned_range_river_equity_tool_chain(
            admission.candidate,
            report.tool_results,
        )
        is None
    )


def test_replay_maps_malformed_equity_output_to_a_stable_reason_code(tmp_path: Path) -> None:
    admission, _orchestrator, report = _completed(tmp_path)
    forged = report.tool_results[-1].model_copy(update={"output": {"forged": True}})

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [*report.tool_results[:-1], forged],
        )

    assert error.value.code.value == "REQ_E_CHAIN"


def test_replay_maps_malformed_combos_output_to_a_stable_reason_code(tmp_path: Path) -> None:
    admission, _orchestrator, report = _completed(tmp_path)
    forged = report.tool_results[1].model_copy(update={"output": {"forged": True}})

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [report.tool_results[0], forged, report.tool_results[2]],
        )

    assert error.value.code.value == "REQ_E_CHAIN"


def test_replay_maps_non_json_combos_output_to_a_stable_reason_code(tmp_path: Path) -> None:
    admission, _orchestrator, report = _completed(tmp_path)
    forged_output = dict(report.tool_results[1].output)
    forged_output["unexpected"] = {"not-json"}
    forged = report.tool_results[1].model_copy(update={"output": forged_output})

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [report.tool_results[0], forged, report.tool_results[2]],
        )

    assert error.value.code.value == "REQ_E_CHAIN"


def test_same_run_bridge_reservation_serializes_ordinary_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    config = _config(tmp_path)
    bridge = Orchestrator(config)
    ordinary = Orchestrator(config)
    entered = threading.Event()
    release = threading.Event()
    original_commit = orchestrator_module.commit_range_equity_admission_record

    def blocking_commit(*args: object, **kwargs: object):
        entered.set()
        assert release.wait(timeout=10)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(
        orchestrator_module,
        "commit_range_equity_admission_record",
        blocking_commit,
    )
    reports = []
    failures: list[BaseException] = []

    def run_bridge() -> None:
        try:
            reports.append(
                bridge.run_versioned_range_river_equity(
                    admission,
                    run_id="p3-016b-reservation-race",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run_bridge)
    worker.start()
    assert entered.wait(timeout=10)
    try:
        with pytest.raises(ProductRunError) as caught:
            ordinary.run(
                admission.candidate,
                run_id="p3-016b-reservation-race",
            )
        assert caught.value.failure.code is ProductRunFailureCode.RUN_LOCKED
    finally:
        release.set()
        worker.join(timeout=30)

    assert not worker.is_alive()
    assert failures == []
    assert len(reports) == 1
    assert bridge.product_store.read_current(reports[0].run_id).read_status.value == "succeeded"


def test_orphan_admission_record_rejects_ordinary_reuse_before_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    orchestrator = Orchestrator(_config(tmp_path))
    run_id = "p3-016b-orphan-record"
    orchestrator._initialize_product_storage(run_id)
    commit_range_equity_admission_record(
        orchestrator.product_store.revision_root,
        run_id,
        admission.binding,
        maximum_bytes=orchestrator.budget_policy.max_artifact_bytes,
    )
    calls: list[str] = []
    original_execute = orchestrator.registry.execute

    def counting_execute(tool_name: str, *args: object, **kwargs: object):
        calls.append(tool_name)
        return original_execute(tool_name, *args, **kwargs)

    monkeypatch.setattr(orchestrator.registry, "execute", counting_execute)

    with pytest.raises(ProductRunError) as caught:
        orchestrator.run(admission.candidate, run_id=run_id)

    assert caught.value.failure.code is ProductRunFailureCode.RUN_CONFLICT
    assert calls == []
    assert not orchestrator.store.exists(run_id)
    assert not (orchestrator.product_store.runs_root / run_id).exists()


def test_orphan_admission_record_case_alias_rejects_reuse_before_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    orchestrator = Orchestrator(_config(tmp_path))
    original_run_id = "P3-016B-Orphan-Alias"
    alias_run_id = original_run_id.lower()
    orchestrator._initialize_product_storage(original_run_id)
    commit_range_equity_admission_record(
        orchestrator.product_store.revision_root,
        original_run_id,
        admission.binding,
        maximum_bytes=orchestrator.budget_policy.max_artifact_bytes,
    )
    calls: list[str] = []
    original_execute = orchestrator.registry.execute

    def counting_execute(tool_name: str, *args: object, **kwargs: object):
        calls.append(tool_name)
        return original_execute(tool_name, *args, **kwargs)

    monkeypatch.setattr(orchestrator.registry, "execute", counting_execute)
    with pytest.raises(ProductRunError) as caught:
        orchestrator.run(admission.candidate, run_id=alias_run_id)

    assert caught.value.failure.code is ProductRunFailureCode.RUN_CORRUPT
    assert calls == []
    assert not orchestrator.store.exists(alias_run_id)


def test_partial_replay_rejects_forged_successful_prerequisite(tmp_path: Path) -> None:
    admission, _orchestrator, report = _completed(tmp_path)
    forged = report.tool_results[0].model_copy(update={"output": {"attacker": "forged"}})

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [forged],
            run_status="failed_with_limitations",
        )


def test_partial_replay_rejects_execution_after_failed_validation(tmp_path: Path) -> None:
    admission, _orchestrator, report = _completed(tmp_path)
    failed = report.tool_results[0].model_copy(
        update={
            "status": ToolStatus.FAILED,
            "exactness": Exactness.UNAVAILABLE,
            "numeric_exactness": NumericalExactness.UNAVAILABLE,
            "output": {},
            "verification": None,
            "error": "fixture failure",
        }
    )

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [failed, report.tool_results[1]],
            run_status="failed_with_limitations",
        )


def test_bridge_replay_rejects_unreachable_approval_lifecycle() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_REPLAY"):
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [],
            run_status="approval_required",
        )


def test_terminal_rejects_report_marker_removal_with_rebuilt_markdown(tmp_path: Path) -> None:
    _admission, _orchestrator, report = _completed(tmp_path)
    read = _orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    forged = report.model_copy(deep=True)
    del forged.reconstructed_input["metadata"]["versioned_range_river_equity"]
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")


def test_terminal_rejects_binding_artifact_removal(tmp_path: Path) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name
        not in {
            "lifecycle_audit.json",
            "range_equity_binding.json",
        }
    }

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")


def test_terminal_reader_requires_preexecution_admission_record(tmp_path: Path) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    record_path = (
        orchestrator.product_store.revision_root
        / ".revision-control"
        / "range-equity-admissions"
        / f"{run_lock_key_sha256(report.run_id)}.json"
    )
    assert record_path.is_file()
    record_path.unlink()

    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_CORRUPT


def test_terminal_rejects_report_case_tamper_with_unchanged_marker(tmp_path: Path) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    forged = report.model_copy(deep=True)
    forged.reconstructed_input["hand"]["known_ranges"][0]["notation"] = "AcAd"
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")


def test_terminal_rejects_normalized_case_tamper_with_unchanged_marker(
    tmp_path: Path,
) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    normalized = json.loads(payloads["normalized_case.json"])
    normalized["hand"]["known_ranges"][0]["notation"] = "AcAd"
    payloads["normalized_case.json"] = canonical_json_bytes(normalized)

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")


@pytest.mark.parametrize("replacement", ("delete", "null"))
def test_terminal_rejects_both_marker_downgrade(
    tmp_path: Path,
    replacement: str,
) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    input_payload = json.loads(payloads["input.json"])
    forged = report.model_copy(deep=True)
    if replacement == "delete":
        del input_payload["metadata"]["versioned_range_river_equity"]
        del forged.reconstructed_input["metadata"]["versioned_range_river_equity"]
    else:
        input_payload["metadata"]["versioned_range_river_equity"] = None
        forged.reconstructed_input["metadata"]["versioned_range_river_equity"] = None
    payloads["input.json"] = canonical_json_bytes(input_payload)
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")

    with pytest.raises(CanonicalStorageError):
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")


def test_terminal_rejects_reshaped_unmarked_derived_equity_chain(tmp_path: Path) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    input_payload = json.loads(payloads["input.json"])
    normalized_payload = json.loads(payloads["normalized_case.json"])
    forged = report.model_copy(deep=True)
    for case_payload in (
        input_payload,
        normalized_payload,
        forged.reconstructed_input,
    ):
        del case_payload["metadata"]["versioned_range_river_equity"]
        case_payload["requested_tools"] = ["combos"]
    payloads["input.json"] = canonical_json_bytes(input_payload)
    payloads["normalized_case.json"] = canonical_json_bytes(normalized_payload)
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")


def test_terminal_rejects_binding_and_all_marker_downgrade_from_admission_record(
    tmp_path: Path,
) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name
        not in {
            "lifecycle_audit.json",
            "range_equity_binding.json",
        }
    }
    input_payload = json.loads(payloads["input.json"])
    normalized_payload = json.loads(payloads["normalized_case.json"])
    forged = report.model_copy(deep=True)
    for case_payload in (
        input_payload,
        normalized_payload,
        forged.reconstructed_input,
    ):
        del case_payload["metadata"]["versioned_range_river_equity"]
        case_payload["requested_tools"] = ["combos"]
    payloads["input.json"] = canonical_json_bytes(input_payload)
    payloads["normalized_case.json"] = canonical_json_bytes(normalized_payload)
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        product_payload_commitments(
            payloads,
            run_id=report.run_id,
            status="succeeded",
            revision_root=orchestrator.product_store.revision_root,
        )


def test_terminal_rejects_marker_downgrade_of_a_failed_tool_prefix(tmp_path: Path) -> None:
    registry = default_registry()
    original_combos = registry._tools["combos"]

    def failing_combos(_payload: dict[str, object]) -> dict[str, object]:
        raise ValueError("fixture combos failure")

    registry._tools["combos"] = replace(original_combos, function=failing_combos)
    orchestrator = Orchestrator(_config(tmp_path), registry=registry)
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    report = orchestrator.run_versioned_range_river_equity(
        admission,
        run_id="p3-016b-failed-prefix-downgrade",
    )
    assert report.run_status == "failed_with_limitations"
    assert (
        "product persistence refused: tool result lacks independent replay authority"
        in report.limitations
    )
    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


def test_revision_source_graph_rejects_report_marker_removal(tmp_path: Path) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {payload.inventory.logical_name: payload for payload in read.payloads}
    input_case = CaseInput.model_validate_json(payloads["input.json"].exact_bytes)
    forged = report.model_copy(deep=True)
    del forged.reconstructed_input["metadata"]["versioned_range_river_equity"]

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        _validate_source_graph(
            [payload.inventory for payload in read.payloads],
            {"input.json": input_case, "final_report.json": forged},
            run_id=report.run_id,
        )


def test_revision_source_graph_rejects_report_case_tamper(tmp_path: Path) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {payload.inventory.logical_name: payload for payload in read.payloads}
    input_case = CaseInput.model_validate_json(payloads["input.json"].exact_bytes)
    normalized_case = CaseInput.model_validate_json(payloads["normalized_case.json"].exact_bytes)
    forged = report.model_copy(deep=True)
    forged.reconstructed_input["hand"]["known_ranges"][0]["notation"] = "AcAd"

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        _validate_source_graph(
            [payload.inventory for payload in read.payloads],
            {
                "input.json": input_case,
                "normalized_case.json": normalized_case,
                "final_report.json": forged,
            },
            run_id=report.run_id,
        )


def test_revision_source_graph_rejects_both_marker_removal(tmp_path: Path) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {payload.inventory.logical_name: payload for payload in read.payloads}
    input_payload = json.loads(payloads["input.json"].exact_bytes)
    del input_payload["metadata"]["versioned_range_river_equity"]
    input_case = CaseInput.model_validate(input_payload)
    normalized_case = CaseInput.model_validate_json(payloads["normalized_case.json"].exact_bytes)
    forged = report.model_copy(deep=True)
    del forged.reconstructed_input["metadata"]["versioned_range_river_equity"]

    with pytest.raises(CanonicalStorageError, match="range-equity persisted cases"):
        _validate_source_graph(
            [payload.inventory for payload in read.payloads],
            {
                "input.json": input_case,
                "normalized_case.json": normalized_case,
                "final_report.json": forged,
            },
            run_id=report.run_id,
        )


def test_revision_source_graph_rejects_reshaped_unmarked_derived_equity_chain(
    tmp_path: Path,
) -> None:
    _admission, orchestrator, report = _completed(tmp_path)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {payload.inventory.logical_name: payload for payload in read.payloads}
    input_payload = json.loads(payloads["input.json"].exact_bytes)
    normalized_payload = json.loads(payloads["normalized_case.json"].exact_bytes)
    forged = report.model_copy(deep=True)
    for case_payload in (
        input_payload,
        normalized_payload,
        forged.reconstructed_input,
    ):
        del case_payload["metadata"]["versioned_range_river_equity"]
        case_payload["requested_tools"] = ["combos"]

    with pytest.raises(CanonicalStorageError):
        _validate_source_graph(
            [payload.inventory for payload in read.payloads],
            {
                "input.json": CaseInput.model_validate(input_payload),
                "normalized_case.json": CaseInput.model_validate(normalized_payload),
                "final_report.json": forged,
            },
            run_id=report.run_id,
        )
