from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from poker_deliberation.bounded_river_call_ev import BoundedRiverCallEvError, _admit_at
from poker_deliberation.bounded_river_call_ev_models import (
    BoundedRiverCallEvCandidateV1,
    BoundedRiverCallEvConfirmationV1,
    BoundedRiverCallEvProvenanceV1,
    BoundedRiverCallEvResultV1,
)
from poker_deliberation.bounded_river_call_ev_provenance import (
    build_bounded_river_call_ev_provenance,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import AgentAssignment, AgentReport
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
    parse_canonical_model,
    run_lock_key_sha256,
)
from poker_deliberation.storage.terminal_canonical import product_payload_commitments
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
)
from tests.bounded_river_call_ev_support import admission, app_config


def _completed(tmp_path: Path):
    admitted = admission(run_id="run-river-terminal-security")
    orchestrator = Orchestrator(app_config(tmp_path))
    report = orchestrator.run_bounded_river_call_ev_review(admitted)
    read = orchestrator.product_store.read_current(report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    return orchestrator, report, read, payloads


def _replay(orchestrator, report, read, payloads) -> None:
    product_payload_commitments(
        payloads,
        run_id=report.run_id,
        status="succeeded",
        revision=read.revision,
        revision_root=orchestrator.product_store.revision_root,
        transaction_id=read.transaction_id,
    )


@pytest.mark.parametrize(
    "logical_name",
    [
        "bounded_river_call_ev_source.txt",
        "bounded_river_call_ev_range.json",
        "bounded_river_call_ev_candidate.json",
        "bounded_river_call_ev_confirmation.json",
        "bounded_river_call_ev_binding.json",
        "bounded_river_call_ev_result.json",
        "bounded_river_call_ev_provenance.json",
    ],
)
def test_completed_terminal_rejects_each_missing_typed_artifact(
    tmp_path: Path,
    logical_name: str,
) -> None:
    orchestrator, report, read, payloads = _completed(tmp_path)
    del payloads[logical_name]

    with pytest.raises(CanonicalStorageError):
        _replay(orchestrator, report, read, payloads)


def test_terminal_rejects_type_and_hash_mutation(tmp_path: Path) -> None:
    orchestrator, report, read, payloads = _completed(tmp_path)
    payloads["bounded_river_call_ev_candidate.json"] = canonical_json_bytes([])
    with pytest.raises(CanonicalStorageError):
        _replay(orchestrator, report, read, payloads)

    _orchestrator, _report, _read, payloads = _completed(tmp_path / "hash")
    result = json.loads(payloads["bounded_river_call_ev_result.json"])
    result["result_sha256"] = "0" * 64
    payloads["bounded_river_call_ev_result.json"] = canonical_json_bytes(result)
    with pytest.raises(CanonicalStorageError):
        _replay(_orchestrator, _report, _read, payloads)


def test_terminal_rejects_raw_text_in_agent_case(tmp_path: Path) -> None:
    orchestrator, report, read, payloads = _completed(tmp_path)
    input_case = json.loads(payloads["input.json"])
    input_case["raw_text"] = "must never enter the agent context"
    payloads["input.json"] = canonical_json_bytes(input_case)

    with pytest.raises(CanonicalStorageError):
        _replay(orchestrator, report, read, payloads)


@pytest.mark.parametrize(
    "update",
    [
        {"agent_role": "forged-role"},
        {"assignment_id": "assignment-ffffffffffff"},
        {"allowed_tools": []},
        {"context_attempt_id": "attempt-ffffffffffffffffffffffff"},
        {"parent_context_id": "context-forged-parent"},
        {"context_expires_at": None},
        {"context_source_sha256": "0" * 64},
        {"context_policy_sha256": "0" * 64},
        {"context_consumer_runtime": "forged-runtime"},
        {"provider": "external"},
    ],
)
def test_provenance_rejects_role_context_and_lineage_mismatch(
    tmp_path: Path,
    update: dict[str, object],
) -> None:
    orchestrator, report, read, payloads = _completed(tmp_path)
    candidate = parse_canonical_model(
        payloads["bounded_river_call_ev_candidate.json"],
        BoundedRiverCallEvCandidateV1,
    )
    confirmation = parse_canonical_model(
        payloads["bounded_river_call_ev_confirmation.json"],
        BoundedRiverCallEvConfirmationV1,
    )
    provenance = parse_canonical_model(
        payloads["bounded_river_call_ev_provenance.json"],
        BoundedRiverCallEvProvenanceV1,
    )
    admitted = _admit_at(
        payloads["bounded_river_call_ev_source.txt"],
        candidate,
        confirmation,
        admitted_at=provenance.admitted_at,
    )
    result = parse_canonical_model(
        payloads["bounded_river_call_ev_result.json"],
        BoundedRiverCallEvResultV1,
    )
    assignments = TypeAdapter(list[AgentAssignment]).validate_json(payloads["assignments.json"])
    reports = [
        parse_canonical_model(data, AgentReport)
        for name, data in payloads.items()
        if name.startswith("agent_reports/") and name.endswith(".json")
    ]
    reports_by_role = {item.agent_role: item for item in reports}
    ordered_reports = [reports_by_role[item.agent_role] for item in report.agent_execution_records]
    selected_index = (
        next(
            index
            for index, item in enumerate(report.agent_execution_records)
            if item.agent_role == "math-auditor"
        )
        if "allowed_tools" in update
        else 0
    )
    first = report.agent_execution_records[selected_index]
    if "context_expires_at" in update:
        update = {"context_expires_at": first.started_at - timedelta(microseconds=1)}
    forged = report.model_copy(
        update={
            "agent_execution_records": [
                *report.agent_execution_records[:selected_index],
                first.model_copy(update=update),
                *report.agent_execution_records[selected_index + 1 :],
            ]
        },
        deep=True,
    )

    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_CONTEXT"):
        build_bounded_river_call_ev_provenance(
            admitted,
            result,
            forged,
            assignments=assignments,
            agent_reports=ordered_reports,
            storage_root=orchestrator.product_store.revision_root,
            storage_revision=read.revision,
            storage_transaction_id=read.transaction_id,
        )


def test_terminal_reader_requires_preexecution_admission_record(tmp_path: Path) -> None:
    orchestrator, report, _read, _payloads = _completed(tmp_path)
    record_path = (
        orchestrator.product_store.revision_root
        / ".revision-control"
        / "bounded-river-call-ev-admissions"
        / f"{run_lock_key_sha256(report.run_id)}.json"
    )
    assert record_path.is_file()
    record_path.unlink()

    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.read_current(report.run_id)
    assert caught.value.failure.code is ProductRunFailureCode.RUN_CORRUPT


def test_case_folded_run_alias_is_not_a_replay_alias(tmp_path: Path) -> None:
    admitted = admission(run_id="Run-River-Case-Alias")
    orchestrator = Orchestrator(app_config(tmp_path))
    assert orchestrator.run_bounded_river_call_ev_review(admitted).run_status == "completed"

    alias = admission(run_id="run-river-case-alias")
    with pytest.raises((ProductRunError, BoundedRiverCallEvError)):
        orchestrator.run_bounded_river_call_ev_review(alias)
