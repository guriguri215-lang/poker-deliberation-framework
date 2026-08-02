from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import poker_deliberation.bounded_natural_language as bounded_nl_module
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


@pytest.mark.parametrize(
    ("index", "update"),
    [
        (0, {"output": {"valid": False}}),
        (0, {"output": {"valid": True}}),
        (1, {"output": {}}),
        (2, {"numeric_exactness": NumericalExactness.APPROXIMATE}),
        (6, {"verification": None}),
    ],
)
def test_completed_direct_tool_envelope_tamper_fails_closed(
    tmp_path: Path,
    index: int,
    update: dict[str, object],
) -> None:
    admitted = admission(run_id=f"run-river-direct-envelope-{index}")
    report = Orchestrator(app_config(tmp_path)).run_bounded_river_call_ev_review(admitted)
    results = list(report.tool_results)
    results[index] = results[index].model_copy(update=update, deep=True)

    with pytest.raises(BoundedRiverCallEvError):
        build_bounded_river_call_ev_result(admitted, results)


@pytest.mark.parametrize(
    "field",
    ["run_id", "confirmation_id", "idempotency_key"],
)
def test_credential_shaped_control_ids_are_refused_without_echo(field: str) -> None:
    prepared = ready_preparation(intake_id=f"intake-secret-control-{field}")
    assert prepared.candidate is not None
    candidate = prepared.candidate
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    values = {
        "run_id": "run-safe-control-id",
        "confirmation_id": "confirmation-safe-control-id",
        "idempotency_key": "idempotency-safe-control-id",
    }
    values[field] = secret
    authority = create_bounded_river_call_ev_authority(
        authority_id="local-test-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )

    with pytest.raises(BoundedRiverCallEvError) as caught:
        create_bounded_river_call_ev_confirmation(
            candidate,
            authority=authority,
            expected_hashes=candidate_hashes(candidate),
            **values,
        )
    assert secret not in str(caught.value)


def test_credential_shaped_authority_id_is_refused_without_echo() -> None:
    secret = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456"
    with pytest.raises(BoundedRiverCallEvError) as caught:
        create_bounded_river_call_ev_authority(
            authority_id=secret,
            authority_kind="local_user",
            authentication="self_asserted",
        )
    assert secret not in str(caught.value)


def test_admission_and_terminal_replay_do_not_reenter_bounded_nl_calculators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admitted = admission(run_id="run-river-calculator-free-replay")

    def forbidden_registry(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("P3-030B calculators must not run during P3-030C replay")

    monkeypatch.setattr(bounded_nl_module, "default_registry", forbidden_registry)
    replayed = admit_bounded_river_call_ev_review(
        admitted.source_bytes,
        admitted.candidate,
        admitted.confirmation,
    )
    orchestrator = Orchestrator(app_config(tmp_path))
    report = orchestrator.run_bounded_river_call_ev_review(replayed)

    assert report.run_status == "completed"
    assert orchestrator.run_bounded_river_call_ev_review(replayed) == report
