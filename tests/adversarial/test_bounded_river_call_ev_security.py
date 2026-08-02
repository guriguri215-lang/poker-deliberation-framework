from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.bounded_river_call_ev import (
    BoundedRiverCallEvError,
    admit_bounded_river_call_ev_review,
    build_bounded_river_call_ev_result,
    create_bounded_river_call_ev_authority,
    create_bounded_river_call_ev_confirmation,
    prepare_bounded_river_call_ev_intake,
    verify_bounded_river_call_ev_candidate,
    verify_bounded_river_call_ev_tool_chain,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import Exactness, NumericalExactness, ToolStatus
from tests.bounded_river_call_ev_support import (
    admission,
    app_config,
    candidate_hashes,
    range_definition,
    ready_preparation,
    river_source,
)


@pytest.mark.parametrize(
    "field",
    [
        "source_sha256",
        "bounded_candidate_sha256",
        "source_bindings_sha256",
        "focal_sha256",
        "extractor_sha256",
        "tool_plan_sha256",
        "range_definition_sha256",
        "range_target_sha256",
        "range_binding_sha256",
        "equity_model_sha256",
        "call_ev_model_sha256",
    ],
)
def test_each_independent_candidate_hash_domain_fails_closed(field: str) -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None
    forged_projection = prepared.candidate.projection.model_copy(update={field: "0" * 64})
    forged = prepared.candidate.model_copy(update={"projection": forged_projection})

    with pytest.raises(BoundedRiverCallEvError):
        verify_bounded_river_call_ev_candidate(forged)


def test_complete_candidate_hash_domain_fails_closed() -> None:
    prepared = ready_preparation()
    assert prepared.candidate is not None

    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_CANDIDATE"):
        verify_bounded_river_call_ev_candidate(
            prepared.candidate.model_copy(update={"candidate_sha256": "0" * 64})
        )


def test_expired_confirmation_and_source_mutation_fail_closed() -> None:
    source = river_source()
    prepared = ready_preparation(source_bytes=source, intake_id="intake-expiry")
    assert prepared.candidate is not None
    candidate = prepared.candidate
    confirmed_at = datetime.now(UTC) - timedelta(hours=2)
    confirmation = create_bounded_river_call_ev_confirmation(
        candidate,
        run_id="run-river-expired",
        confirmation_id="confirmation-river-expired",
        idempotency_key="idempotency-river-expired",
        authority=create_bounded_river_call_ev_authority(
            authority_id="local-test-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        expected_hashes=candidate_hashes(candidate),
        confirmed_at=confirmed_at,
        expires_at=confirmed_at + timedelta(hours=1),
    )

    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_CONFIRMATION_EXPIRED"):
        admit_bounded_river_call_ev_review(source, candidate, confirmation)
    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_SOURCE"):
        admit_bounded_river_call_ev_review(source + b"\n", candidate, confirmation)


@pytest.mark.parametrize("mutation", ["target", "action_prefix", "street"])
def test_range_target_and_condition_mutations_are_refused(mutation: str) -> None:
    source = river_source()
    definition = range_definition(source)
    payload = definition.model_dump(mode="python")
    if mutation == "target":
        payload["target_player_id"] = "Hero"
    elif mutation == "action_prefix":
        payload["game_conditions"]["action_prefix_sha256"] = "0" * 64
    else:
        payload["game_conditions"]["street"] = "turn"
    forged = definition.model_validate(payload)

    prepared = prepare_bounded_river_call_ev_intake(
        source,
        forged,
        intake_id=f"intake-range-{mutation}",
        source_id="fixture-range-mutation",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )

    assert prepared.status == "blocked"
    assert prepared.diagnostics[0].code.value == "BRC_E_TARGET"


def test_cross_run_range_mutation_cannot_reuse_terminal_namespace(tmp_path: Path) -> None:
    first = admission(run_id="run-river-cross-replay", notation="QcJc")
    second = admission(run_id="run-river-cross-replay", notation="9c9d")
    orchestrator = Orchestrator(app_config(tmp_path))
    assert orchestrator.run_bounded_river_call_ev_review(first).run_status == "completed"

    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_CONFIRMATION_REPLAY"):
        orchestrator.run_bounded_river_call_ev_review(second)


def test_manual_call_ev_input_conflict_is_refused_before_execution(tmp_path: Path) -> None:
    admitted = admission(run_id="run-river-manual-conflict")
    payload = admitted.case.model_dump(mode="python")
    payload["metadata"]["tool_inputs"]["raked_call_ev"]["equity"] = 0.5
    forged = replace(admitted, case=admitted.case.model_validate(payload))

    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_CONFIRMATION_BINDING"):
        Orchestrator(app_config(tmp_path)).run_bounded_river_call_ev_review(forged)


def test_order_numeric_and_partial_prefix_tamper_fail_closed(tmp_path: Path) -> None:
    admitted = admission(run_id="run-river-chain-security")
    report = Orchestrator(app_config(tmp_path)).run_bounded_river_call_ev_review(admitted)
    results = list(report.tool_results)

    reordered = [results[1], results[0], *results[2:]]
    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_TOOL_PLAN"):
        build_bounded_river_call_ev_result(admitted, reordered)

    pot = results[2]
    forged_output = dict(pot.output)
    forged_output["required_equity"] = 0.5
    forged_pot = pot.model_copy(update={"output": forged_output})
    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_NUMERIC"):
        build_bounded_river_call_ev_result(
            admitted,
            [*results[:2], forged_pot, *results[3:]],
        )

    forged_prefix = results[0].model_copy(update={"output": {"valid": True}})
    failed_ledger = results[1].model_copy(
        update={
            "status": ToolStatus.FAILED,
            "exactness": Exactness.UNAVAILABLE,
            "numeric_exactness": NumericalExactness.UNAVAILABLE,
            "output": {},
            "verification": None,
            "error": "fixture failure",
        }
    )
    with pytest.raises(BoundedRiverCallEvError, match="BRC_E_REPLAY"):
        verify_bounded_river_call_ev_tool_chain(
            admitted,
            [forged_prefix, failed_ledger],
            run_status="failed_with_limitations",
        )
