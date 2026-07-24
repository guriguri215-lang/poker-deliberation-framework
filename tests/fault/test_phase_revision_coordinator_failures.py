"""P2-010B phase revision coordinator fault tests."""

from __future__ import annotations

import os
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionFailureCode,
    PhaseRevisionFailureV1,
    PhaseRevisionTraceV1,
    PhaseTracePair,
    PhaseTransitionApplyResultV1,
    PhaseTransitionAuthorizationV1,
)
from poker_deliberation.state_machine import RunState
from poker_deliberation.storage.revision_canonical import run_id_sha256
from poker_deliberation.storage.revision_models import (
    DurabilityEvidenceV1,
    ReachableRevisionV1,
    RevisionPublishOutcomeV1,
    VerifiedStorageRevisionV1,
)
from tests.integration.test_phase_revision_coordinator import (
    build_valid_scenario,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2cf-", dir=parent) as directory:
        yield Path(directory)


def test_forged_phase_policy_is_mutation_zero_invalid_trace(
    short_tmp: Path,
) -> None:
    _orchestrator, _machine, coordinator, bundle = build_valid_scenario(short_tmp)
    forged_request = bundle.trace.synthesis.request.model_copy(
        update={"policy_snapshot_hash": "0" * 64}
    )
    forged = replace(
        bundle,
        trace=PhaseRevisionTraceV1(
            synthesis=PhaseTracePair(
                request=forged_request,
                outcome=bundle.trace.synthesis.outcome,
            )
        ),
    )

    result = coordinator.publish(forged)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.INVALID_TRACE)
    assert not (
        coordinator.store.runs_root / bundle.request.run_id / ".revision-store" / "current.json"
    ).exists()


def test_nonnull_artifact_intent_hash_must_match_admitted_bytes(
    short_tmp: Path,
) -> None:
    _orchestrator, _machine, coordinator, bundle = build_valid_scenario(short_tmp)
    intents = bundle.trace.synthesis.outcome.artifact_intents
    forged_intent = intents[0].model_copy(update={"content_sha256": "0" * 64})
    forged_outcome = bundle.trace.synthesis.outcome.model_copy(
        update={"artifact_intents": (forged_intent, *intents[1:])}
    )
    forged = replace(
        bundle,
        trace=PhaseRevisionTraceV1(
            synthesis=PhaseTracePair(
                request=bundle.trace.synthesis.request,
                outcome=forged_outcome,
            )
        ),
    )

    result = coordinator.publish(forged)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.INVALID_TRACE)


def test_invalid_utf8_is_unsupported_before_storage_mutation(
    short_tmp: Path,
) -> None:
    _orchestrator, _machine, coordinator, bundle = build_valid_scenario(short_tmp)
    target = bundle.request.artifacts[0]
    forged = replace(
        bundle,
        request=bundle.request.model_copy(
            update={
                "artifacts": (
                    target.model_copy(update={"exact_bytes": b"\xff"}),
                    *bundle.request.artifacts[1:],
                )
            }
        ),
    )

    result = coordinator.publish(forged)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.UNSUPPORTED_PAYLOAD)
    assert not (
        coordinator.store.runs_root / bundle.request.run_id / ".revision-store" / "current.json"
    ).exists()


def test_storage_fault_never_authorizes_transition(short_tmp: Path) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)

    def inject(hook: str) -> None:
        if hook == "transaction.before_open":
            raise OSError("synthetic transaction boundary")

    coordinator.store.fault_injector = inject
    result = coordinator.publish(bundle)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []


def test_none_storage_outcome_is_typed_uncertain_without_raw_exception(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    monkeypatch.setattr(coordinator.store, "publish", lambda _request: None)

    result = coordinator.publish(bundle)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []


def test_valid_looking_outcome_without_verified_current_is_uncertain(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    _inventories, _heads, expected_transaction_sha256 = coordinator._validate_trace(bundle)
    forged = RevisionPublishOutcomeV1(
        outcome_kind="published",
        run_id_sha256=run_id_sha256(bundle.request.run_id),
        transaction_id=bundle.request.transaction_id,
        transaction_sha256=expected_transaction_sha256,
        revision=bundle.request.proposed_revision,
        observed_current_revision=bundle.request.proposed_revision,
        manifest_sha256="1" * 64,
        pointer_sha256="2" * 64,
        filesystem_effect="current_advanced",
        domain_effect="current_advanced",
        previous_revision_effect="not_applicable",
        durability_evidence=DurabilityEvidenceV1(
            platform_adapter="windows_msvcrt" if os.name == "nt" else "posix_fcntl",
            file_sync="confirmed",
            directory_sync="confirmed",
            pointer_replace="confirmed",
            reconciliation="confirmed",
        ),
    )
    monkeypatch.setattr(coordinator.store, "publish", lambda _request: forged)

    result = coordinator.publish(bundle)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []


def test_duck_typed_current_never_authorizes_transition(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    _inventories, _heads, expected_transaction_sha256 = coordinator._validate_trace(bundle)
    forged = RevisionPublishOutcomeV1(
        outcome_kind="published",
        run_id_sha256=run_id_sha256(bundle.request.run_id),
        transaction_id=bundle.request.transaction_id,
        transaction_sha256=expected_transaction_sha256,
        revision=bundle.request.proposed_revision,
        observed_current_revision=bundle.request.proposed_revision,
        manifest_sha256="1" * 64,
        pointer_sha256="2" * 64,
        filesystem_effect="current_advanced",
        domain_effect="current_advanced",
        previous_revision_effect="not_applicable",
        durability_evidence=DurabilityEvidenceV1(
            platform_adapter="windows_msvcrt" if os.name == "nt" else "posix_fcntl",
            file_sync="confirmed",
            directory_sync="confirmed",
            pointer_replace="confirmed",
            reconciliation="confirmed",
        ),
    )
    duck_current = SimpleNamespace(
        run_id=bundle.request.run_id,
        current_revision=forged.revision,
        manifest_sha256=forged.manifest_sha256,
        current_pointer_sha256=forged.pointer_sha256,
        reachable_history=(
            SimpleNamespace(
                revision=forged.revision,
                transaction_id=bundle.request.transaction_id,
                transaction_sha256=expected_transaction_sha256,
                manifest_sha256=forged.manifest_sha256,
            ),
        ),
    )
    monkeypatch.setattr(coordinator.store, "publish", lambda _request: forged)
    monkeypatch.setattr(coordinator.store, "read_current", lambda _run_id: duck_current)

    result = coordinator.publish(bundle)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []


def test_inconsistent_verified_history_head_never_authorizes_transition(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    _inventories, _heads, expected_transaction_sha256 = coordinator._validate_trace(bundle)
    forged = RevisionPublishOutcomeV1(
        outcome_kind="published",
        run_id_sha256=run_id_sha256(bundle.request.run_id),
        transaction_id=bundle.request.transaction_id,
        transaction_sha256=expected_transaction_sha256,
        revision=bundle.request.proposed_revision,
        observed_current_revision=bundle.request.proposed_revision,
        manifest_sha256="1" * 64,
        pointer_sha256="2" * 64,
        filesystem_effect="current_advanced",
        domain_effect="current_advanced",
        previous_revision_effect="not_applicable",
        durability_evidence=DurabilityEvidenceV1(
            platform_adapter="windows_msvcrt" if os.name == "nt" else "posix_fcntl",
            file_sync="confirmed",
            directory_sync="confirmed",
            pointer_replace="confirmed",
            reconciliation="confirmed",
        ),
    )
    inconsistent_current = VerifiedStorageRevisionV1(
        run_id=bundle.request.run_id,
        current_revision=forged.revision,
        current_pointer_sha256=forged.pointer_sha256,
        manifest_sha256=forged.manifest_sha256,
        inventory_sha256="3" * 64,
        reachable_history=(
            ReachableRevisionV1(
                revision=9,
                transaction_id=bundle.request.transaction_id,
                revision_relative_path=(
                    f"revisions/00000000000000000009/{bundle.request.transaction_id}"
                ),
                transaction_sha256=expected_transaction_sha256,
                manifest_sha256="4" * 64,
            ),
        ),
    )
    monkeypatch.setattr(coordinator.store, "publish", lambda _request: forged)
    monkeypatch.setattr(
        coordinator.store,
        "read_current",
        lambda _run_id: inconsistent_current,
    )

    result = coordinator.publish(bundle)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []


def test_ambiguous_current_replace_never_authorizes_transition(
    short_tmp: Path,
) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    fired = False

    def inject(hook: str) -> None:
        nonlocal fired
        if hook == "current.after_replace" and not fired:
            fired = True
            raise OSError("synthetic response loss")

    coordinator.store.fault_injector = inject
    result = coordinator.publish(bundle)

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []


def test_pre_apply_fault_keeps_committed_revision_and_live_state(
    short_tmp: Path,
) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)

    def inject(hook: str) -> None:
        if hook == "before_transition":
            raise RuntimeError("synthetic pre-apply fault")

    result = orchestrator._apply_revision_transition(
        machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=authorization,
        fault_injector=inject,
    )

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.APPLY_FAILED)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []
    assert coordinator.store.read_current(bundle.request.run_id).current_revision == 1


def test_post_apply_response_loss_reconciles_to_already_applied(
    short_tmp: Path,
) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)

    def inject(hook: str) -> None:
        if hook == "after_transition":
            raise RuntimeError("synthetic post-apply response loss")

    result = orchestrator._apply_revision_transition(
        machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=authorization,
        fault_injector=inject,
    )

    assert result == PhaseTransitionApplyResultV1(outcome_kind="already_applied")
    assert machine.state is RunState.COMPLETED
    assert len(machine.events) == 1


def test_state_divergence_after_publish_denies_stale_authorization(
    short_tmp: Path,
) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)
    machine.transition(RunState.FAILED_WITH_LIMITATIONS, "independent failure")

    result = orchestrator._apply_revision_transition(
        machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=authorization,
    )

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.AUTHORIZATION_MISMATCH)
    assert machine.state is RunState.FAILED_WITH_LIMITATIONS
    assert all(event.target is not RunState.COMPLETED for event in machine.events)
