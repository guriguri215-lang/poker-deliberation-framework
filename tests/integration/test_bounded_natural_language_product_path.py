from __future__ import annotations

from dataclasses import replace

import pytest

from poker_deliberation.bounded_natural_language import BoundedNaturalLanguageError
from poker_deliberation.bounded_natural_language_models import (
    BoundedNaturalLanguageDiagnosticCode,
    BoundedNaturalLanguageProvenanceV1,
)
from poker_deliberation.bounded_natural_language_provenance import (
    build_bounded_natural_language_provenance,
    verify_bounded_natural_language_provenance,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.schemas import AgentAssignment, AgentReport
from poker_deliberation.storage.revision_canonical import (
    parse_canonical_model,
    parse_canonical_model_list,
)
from tests.bounded_natural_language_support import (
    SOURCE_BYTES,
    app_config,
    bounded_admission,
)


def _ordered_support(read, report):
    assignments = [
        *parse_canonical_model_list(read.payload_bytes("assignments.json"), AgentAssignment)
    ]
    reports = [
        parse_canonical_model(payload.exact_bytes, AgentReport)
        for payload in read.payloads
        if payload.inventory.logical_name.startswith("agent_reports/")
    ]
    by_role = {item.agent_role: item for item in reports}
    ordered = [by_role[item.agent_role] for item in report.agent_execution_records]
    return assignments, ordered


def test_bounded_review_publishes_and_replays_complete_terminal_chain(tmp_path) -> None:
    admission = bounded_admission(run_id="run-bounded-product-1")
    orchestrator = Orchestrator(
        config=app_config(tmp_path),
        provider=LocalProvider(),
    )

    report = orchestrator.run_bounded_natural_language_review(admission)
    assert report.run_status == "completed"
    assert report.confidence.value == "C"
    assert [item.tool_name for item in report.tool_results] == [
        "hand_validator",
        "hand_pot_ledger",
        "pot_odds",
    ]
    assert report.reconstructed_input["raw_text"] is None
    assert report.claim_assessments[0].label.value == "USER_CLAIM"
    assert all(section["epistemic_status"] == "UNKNOWN" for section in report.analysis_sections)

    read = orchestrator.product_store.read_current(report.run_id)
    bounded_names = {
        payload.inventory.logical_name
        for payload in read.payloads
        if payload.inventory.logical_name.startswith("bounded_nl")
    }
    assert bounded_names == {
        "bounded_nl_source.txt",
        "bounded_nl_candidate.json",
        "bounded_nl_confirmation.json",
        "bounded_nl_provenance.json",
    }
    provenance = parse_canonical_model(
        read.payload_bytes("bounded_nl_provenance.json"),
        BoundedNaturalLanguageProvenanceV1,
    )
    assert [item.epistemic_label for item in provenance.tool_support] == [
        "CALCULATED",
        "CALCULATED",
        "CALCULATED",
    ]
    assert [item.tool_name for item in provenance.tool_support] == [
        "hand_validator",
        "hand_pot_ledger",
        "pot_odds",
    ]
    assignments, agent_reports = _ordered_support(read, report)
    verify_bounded_natural_language_provenance(
        source_bytes=SOURCE_BYTES,
        candidate=admission.candidate,
        confirmation=admission.confirmation,
        case=admission.case,
        report=report,
        provenance=provenance,
        assignments=assignments,
        agent_reports=agent_reports,
        storage_root=orchestrator.product_store.revision_root,
        storage_revision=read.revision,
        storage_transaction_id=read.transaction_id,
    )


def test_exact_same_confirmation_is_idempotent_but_changed_source_is_not(tmp_path) -> None:
    admission = bounded_admission(run_id="run-bounded-product-replay")
    orchestrator = Orchestrator(config=app_config(tmp_path), provider=LocalProvider())
    first = orchestrator.run_bounded_natural_language_review(admission)
    replay = orchestrator.run_bounded_natural_language_review(admission)
    assert replay == first

    conflicting = replace(admission, source_bytes=admission.source_bytes + b"\n")
    with pytest.raises(BoundedNaturalLanguageError) as error:
        orchestrator.run_bounded_natural_language_review(conflicting)
    assert error.value.code in {
        BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING,
        BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_REPLAY,
    }
    assert orchestrator.product_store.read_current(first.run_id).revision == 1


def test_report_tool_tamper_is_rejected_by_provenance_builder(tmp_path) -> None:
    admission = bounded_admission(run_id="run-bounded-product-tamper")
    orchestrator = Orchestrator(config=app_config(tmp_path), provider=LocalProvider())
    report = orchestrator.run_bounded_natural_language_review(admission)
    read = orchestrator.product_store.read_current(report.run_id)
    assignments, agent_reports = _ordered_support(read, report)
    forged_tool = report.tool_results[-1].model_copy(
        update={"input": {"pot_before_bet": 99.0, "opponent_bet": 8.0, "call_cost": 8.0}}
    )
    forged_report = report.model_copy(
        update={"tool_results": [*report.tool_results[:-1], forged_tool]}
    )

    with pytest.raises((BoundedNaturalLanguageError, ValueError)):
        build_bounded_natural_language_provenance(
            admission,
            forged_report,
            assignments=assignments,
            agent_reports=agent_reports,
            storage_root=orchestrator.product_store.revision_root,
            storage_revision=read.revision,
            storage_transaction_id=read.transaction_id,
        )


def test_prepare_and_confirmation_objects_do_not_create_a_run_namespace(tmp_path) -> None:
    fresh_root = tmp_path / "no-create-contract"
    orchestrator = Orchestrator(config=app_config(fresh_root), provider=LocalProvider())
    admission = bounded_admission(run_id="run-bounded-not-created")

    assert orchestrator._namespace_kind(admission.confirmation.run_id) is None
