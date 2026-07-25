from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from poker_deliberation.local_data_cleanup import LocalDataCleanupExecutor
from poker_deliberation.local_data_cleanup_canonical import (
    canonical_cleanup_bytes,
    parse_cleanup_model,
    run_id_sha256,
)
from poker_deliberation.local_data_cleanup_models import (
    CleanupFailureCode,
    CleanupPlanV1,
    CleanupRootMarkerV1,
)
from tests.integration.test_local_data_cleanup_executor import (
    DELETE_AT,
    EVALUATED,
    CleanupAuthority,
    NoLegalHold,
    _approve_cleanup,
    _case,
    _orchestrator,
    _snapshot,
)


class InjectedFault(RuntimeError):
    pass


def _raise_at(expected: str):
    def injector(hook: str) -> None:
        if hook == expected:
            raise InjectedFault(expected)

    return injector


def _prepare_delete_fixture(
    tmp_path,
    *,
    run_id: str,
    root_character: str,
) -> tuple[LocalDataCleanupExecutor, CleanupPlanV1, str, CleanupAuthority]:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id=run_id)
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id=run_id,
        root_id="cleanup-root-" + root_character * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    quarantine = executor.dry_run_quarantine(
        run_id,
        execution_id=f"{run_id}-quarantine",
        idempotency_key=f"{run_id}-quarantine-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert quarantine.plan is not None
    quarantine_approval = f"{run_id}-quarantine-approval"
    quarantine_request, quarantine_provider = _approve_cleanup(
        orchestrator,
        quarantine.plan,
        approval_run_id=quarantine_approval,
    )
    assert (
        executor.execute_quarantine(
            quarantine.plan,
            approval_run_id=quarantine_approval,
            approval_request_id=quarantine_request,
            authority_provider=quarantine_provider,
        ).outcome_kind
        == "committed"
    )
    executor.clock = lambda: DELETE_AT
    delete = executor.dry_run_delete(
        run_id,
        execution_id=f"{run_id}-delete",
        idempotency_key=f"{run_id}-delete-key",
        expires_at=DELETE_AT + timedelta(hours=1),
    )
    assert delete.plan is not None
    delete_approval = f"{run_id}-delete-approval"
    delete_request, delete_provider = _approve_cleanup(
        orchestrator,
        delete.plan,
        approval_run_id=delete_approval,
        decision_at=DELETE_AT,
    )
    return executor, delete.plan, delete_request, delete_provider


def test_quarantine_before_journal_fault_has_no_control_or_payload_effect(tmp_path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-fault-before-journal")
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id="cleanup-fault-before-journal",
        root_id="cleanup-root-" + "d" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    dry_run = executor.dry_run_quarantine(
        "cleanup-fault-before-journal",
        execution_id="cleanup-before-journal-execution",
        idempotency_key="cleanup-before-journal-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="cleanup-before-journal-approval",
    )
    before = _snapshot(executor.store.cleanup_root)
    executor.store.fault_injector = _raise_at("quarantine.before_journal")

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-before-journal-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.INTERNAL_INVARIANT_ERROR
    assert result.failure.filesystem_effect == "none"
    assert _snapshot(executor.store.cleanup_root) == before
    assert (
        orchestrator.product_store.foundation.runs_root / "cleanup-fault-before-journal"
    ).is_dir()


def test_quarantine_cancellation_after_journal_rolls_back_exact_scaffold(tmp_path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-cancel-after-journal")
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id="cleanup-cancel-after-journal",
        root_id="cleanup-root-" + "e" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    dry_run = executor.dry_run_quarantine(
        "cleanup-cancel-after-journal",
        execution_id="cleanup-cancel-after-journal-execution",
        idempotency_key="cleanup-cancel-after-journal-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="cleanup-cancel-after-journal-approval",
    )
    before = _snapshot(executor.store.cleanup_root)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls >= 4

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-cancel-after-journal-approval",
        approval_request_id=request_id,
        authority_provider=provider,
        cancelled=cancelled,
    )

    assert calls == 4
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.CANCELLED
    assert result.failure.filesystem_effect == "none"
    assert _snapshot(executor.store.cleanup_root) == before
    assert (
        orchestrator.product_store.foundation.runs_root / "cleanup-cancel-after-journal"
    ).is_dir()


def test_quarantine_authority_revoked_after_journal_rolls_back_before_effect(
    tmp_path,
) -> None:
    run_id = "cleanup-revoke-after-journal"
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id=run_id)
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id=run_id,
        root_id="cleanup-root-" + "f" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    dry_run = executor.dry_run_quarantine(
        run_id,
        execution_id=f"{run_id}-execution",
        idempotency_key=f"{run_id}-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id=f"{run_id}-approval",
    )
    before = _snapshot(executor.store.cleanup_root)

    def revoke(hook: str) -> None:
        if hook == "quarantine.before_effect":
            provider.actor = provider.actor.model_copy(update={"revocation_status": "revoked"})

    executor.store.fault_injector = revoke
    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id=f"{run_id}-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.AUTHORITY_REVOKED
    assert result.failure.filesystem_effect == "none"
    assert _snapshot(executor.store.cleanup_root) == before
    assert (orchestrator.product_store.foundation.runs_root / run_id).is_dir()


def test_quarantine_commit_clock_mutation_cannot_publish_success(tmp_path) -> None:
    run_id = "cleanup-quarantine-clock-mutation"
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id=run_id)
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id=run_id,
        root_id="cleanup-root-" + "9" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    dry_run = executor.dry_run_quarantine(
        run_id,
        execution_id=f"{run_id}-execution",
        idempotency_key=f"{run_id}-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id=f"{run_id}-approval",
    )
    destination = executor.store.cleanup_root / dry_run.plan.actions[0].destination_relative_path
    mutated = False

    def mutating_clock():
        nonlocal mutated
        if destination.is_dir() and not mutated:
            (destination / "clock-mutation.json").write_text("{}", encoding="utf-8")
            mutated = True
        return EVALUATED

    executor.clock = mutating_clock
    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id=f"{run_id}-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert mutated
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert result.failure.filesystem_effect == "source_moved"
    assert destination.is_dir()
    assert not (
        executor.store.cleanup_root / "runs" / run_id_sha256(run_id) / "current.json"
    ).exists()


def test_quarantine_post_effect_fault_requires_manual_reconciliation(tmp_path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-fault-quarantine")
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id="cleanup-fault-quarantine",
        root_id="cleanup-root-" + "a" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    dry_run = executor.dry_run_quarantine(
        "cleanup-fault-quarantine",
        execution_id="cleanup-fault-quarantine-execution",
        idempotency_key="cleanup-fault-quarantine-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="cleanup-fault-quarantine-approval",
    )
    executor.store.fault_injector = _raise_at("quarantine.after_effect")

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-fault-quarantine-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    report = executor.inspect_reconciliation(dry_run.plan)
    retry = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-fault-quarantine-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert result.failure.filesystem_effect == "source_moved"
    assert report.classification == "effect_unknown"
    assert report.observed_source == "absent"
    assert report.observed_destination == "exact"
    assert report.observed_current == "unreadable"
    assert retry.failure is not None
    assert retry.failure.code is CleanupFailureCode.EFFECT_UNKNOWN


def test_post_pointer_response_loss_reconciles_as_committed_and_replays(tmp_path) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-fault-response-loss")
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id="cleanup-fault-response-loss",
        root_id="cleanup-root-" + "b" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    dry_run = executor.dry_run_quarantine(
        "cleanup-fault-response-loss",
        execution_id="cleanup-fault-response-execution",
        idempotency_key="cleanup-fault-response-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="cleanup-fault-response-approval",
    )
    executor.store.fault_injector = _raise_at("quarantine.after_pointer_replace")

    uncertain = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-fault-response-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    report = executor.inspect_reconciliation(dry_run.plan)
    executor.store.fault_injector = None
    replay = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-fault-response-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert uncertain.failure is not None
    assert uncertain.failure.code is CleanupFailureCode.EFFECT_UNKNOWN
    assert report.classification == "committed"
    assert replay.outcome_kind == "committed"
    assert replay.receipt is not None


def test_quarantine_post_pointer_marker_replacement_is_effect_unknown(tmp_path) -> None:
    run_id = "cleanup-fault-marker-replacement"
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id=run_id)
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id=run_id,
        root_id="cleanup-root-" + "7" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    dry_run = executor.dry_run_quarantine(
        run_id,
        execution_id="cleanup-marker-replacement-execution",
        idempotency_key="cleanup-marker-replacement-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="cleanup-marker-replacement-approval",
    )
    marker_path = executor.store.cleanup_root / "ownership.json"

    def replace_marker(hook: str) -> None:
        if hook != "quarantine.after_pointer_replace":
            return
        marker = parse_cleanup_model(marker_path.read_bytes(), CleanupRootMarkerV1)
        marker_path.write_bytes(
            canonical_cleanup_bytes(
                marker.model_copy(update={"root_id": "cleanup-root-" + "8" * 32})
            )
        )

    executor.store.fault_injector = replace_marker
    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="cleanup-marker-replacement-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.EFFECT_UNKNOWN
    assert result.failure.filesystem_effect == "source_moved"
    assert result.failure.domain_effect == "current_may_have_advanced"


def test_partial_delete_journal_write_rolls_back_exact_transaction_root(tmp_path) -> None:
    run_id = "cleanup-fault-delete-journal"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="1",
    )
    before = _snapshot(executor.store.cleanup_root)
    executor.store.fault_injector = _raise_at("delete.journal.after_write")

    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    current = executor.store.read_current(run_id_sha256(run_id))

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.INTERNAL_INVARIANT_ERROR
    assert result.failure.filesystem_effect == "none"
    assert _snapshot(executor.store.cleanup_root) == before
    assert current is not None
    assert current[0].state == "quarantined"
    assert not (
        executor.store.cleanup_root
        / "runs"
        / run_id_sha256(run_id)
        / "transactions"
        / result.transaction_id
    ).exists()


def test_delete_authority_revoked_after_journal_rolls_back_before_staging(
    tmp_path,
) -> None:
    run_id = "cleanup-delete-revoke-after-journal"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="0",
    )
    before = _snapshot(executor.store.cleanup_root)

    def revoke(hook: str) -> None:
        if hook == "delete.before_staging_rename":
            provider.actor = provider.actor.model_copy(update={"revocation_status": "revoked"})

    executor.store.fault_injector = revoke
    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    current = executor.store.read_current(run_id_sha256(run_id))

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.AUTHORITY_REVOKED
    assert result.failure.filesystem_effect == "none"
    assert _snapshot(executor.store.cleanup_root) == before
    assert current is not None
    assert current[0].state == "quarantined"
    assert (executor.store.cleanup_root / "quarantine" / run_id).is_dir()


def test_delete_prepare_clock_mutation_cannot_publish_prepared_current(
    tmp_path,
) -> None:
    run_id = "cleanup-delete-prepare-clock"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="5",
    )
    destination = executor.store.cleanup_root / plan.actions[0].destination_relative_path
    mutated = False

    def mutating_clock():
        nonlocal mutated
        if destination.is_dir() and not mutated:
            (destination / "clock-mutation.json").write_text("{}", encoding="utf-8")
            mutated = True
        return DELETE_AT

    executor.clock = mutating_clock
    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    report = executor.inspect_reconciliation(plan)

    assert mutated
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert result.failure.filesystem_effect == "delete_staging_moved"
    assert report.classification == "effect_unknown"
    assert report.observed_staging == "partial"
    assert report.observed_current == "unreadable"


def test_delete_final_clock_mutation_cannot_publish_deleted_current(tmp_path) -> None:
    run_id = "cleanup-delete-final-clock"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="6",
    )
    source = executor.store.cleanup_root / "quarantine" / run_id
    destination = executor.store.cleanup_root / plan.actions[0].destination_relative_path
    mutated = False

    def mutating_clock():
        nonlocal mutated
        if not source.exists() and not destination.exists() and not mutated:
            destination.mkdir()
            (destination / "clock-recreated.json").write_text("{}", encoding="utf-8")
            mutated = True
        return DELETE_AT

    executor.clock = mutating_clock
    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    report = executor.inspect_reconciliation(plan)

    assert mutated
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert result.failure.filesystem_effect == "partial_delete"
    assert result.failure.domain_effect == "current_advanced"
    assert report.classification == "reconciliation_required"
    assert report.observed_current == "prior"
    assert report.observed_staging == "partial"
    assert destination.is_dir()


@pytest.mark.parametrize(
    "hook",
    [
        "delete.after_staging_rename",
        "delete.before_prepare_pointer_replace",
    ],
)
def test_delete_replay_with_pending_control_state_is_effect_unknown(
    tmp_path,
    hook: str,
) -> None:
    suffix = "staged" if hook == "delete.after_staging_rename" else "prepare-temp"
    run_id = f"cleanup-delete-replay-{suffix}"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="4",
    )
    executor.store.fault_injector = _raise_at(hook)

    first = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    report = executor.inspect_reconciliation(plan)
    retry = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert first.failure is not None
    assert first.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert first.failure.filesystem_effect == "delete_staging_moved"
    assert report.observed_staging == "exact"
    assert report.observed_current == "unreadable"
    assert report.classification == "effect_unknown"
    assert retry.failure is not None
    assert retry.failure.code is CleanupFailureCode.EFFECT_UNKNOWN
    assert retry.failure.filesystem_effect == "delete_staging_moved"
    assert retry.failure.domain_effect == "current_may_have_advanced"


def test_orphan_deleted_revision_is_not_replayed_past_delete_prepared(tmp_path) -> None:
    run_id = "cleanup-fault-orphan-deleted"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="2",
    )
    executor.store.fault_injector = _raise_at("delete.before_final_pointer_temp_write")

    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    current = executor.store.read_current(run_id_sha256(run_id))
    retry = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    stable = executor.store.read_current(run_id_sha256(run_id))
    revisions = executor.store.cleanup_root / "runs" / run_id_sha256(run_id) / "revisions"

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert result.failure.filesystem_effect == "partial_delete"
    assert current is not None
    assert current[0].state == "delete_prepared"
    assert retry.failure is not None
    assert retry.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert retry.failure.filesystem_effect == "partial_delete"
    assert retry.cleanup_revision == 2
    assert stable is not None
    assert stable[0].revision == 2
    assert any(path.name.startswith("r3-") for path in revisions.iterdir())


def test_delete_prepared_exact_staging_replay_reports_staging_move(tmp_path) -> None:
    run_id = "cleanup-fault-delete-prepared"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="3",
    )
    executor.store.fault_injector = _raise_at("delete.before_unlink_start")

    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    retry = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    report = executor.inspect_reconciliation(plan)

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert result.failure.filesystem_effect == "delete_staging_moved"
    assert retry.failure is not None
    assert retry.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert retry.failure.filesystem_effect == "delete_staging_moved"
    assert report.observed_staging == "exact"


def test_delete_unlink_callback_payload_mutation_is_not_deleted(tmp_path) -> None:
    run_id = "cleanup-fault-delete-mutation"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="4",
    )
    staging = executor.store.cleanup_root / plan.actions[0].destination_relative_path
    mutated = False

    def mutate_before_unlink(hook: str) -> None:
        nonlocal mutated
        if mutated or not hook.startswith("delete.before_unlink."):
            return
        for path in staging.rglob("*"):
            if path.is_file():
                path.write_bytes(b"callback mutation")
                mutated = True
                return

    executor.store.fault_injector = mutate_before_unlink
    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    report = executor.inspect_reconciliation(plan)

    assert mutated
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert result.failure.filesystem_effect == "partial_delete"
    assert report.observed_staging == "partial"


def test_control_read_oserror_replay_is_effect_unknown(tmp_path, monkeypatch) -> None:
    run_id = "cleanup-fault-control-read"
    executor, plan, request_id, provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="5",
    )
    current_path = executor.store.cleanup_root / "runs" / run_id_sha256(run_id) / "current.json"
    original_read_bytes = Path.read_bytes

    def fail_current_read(path: Path) -> bytes:
        if path == current_path:
            raise PermissionError("injected control read failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_current_read)
    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.EFFECT_UNKNOWN
    assert result.failure.filesystem_effect == "journal_only"
    assert result.failure.domain_effect == "current_may_have_advanced"


def test_partial_delete_cancellation_stops_at_prepared_and_never_auto_resumes(
    tmp_path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    orchestrator.run(_case(), run_id="cleanup-fault-delete")
    executor = LocalDataCleanupExecutor(
        tmp_path / "cleanup",
        orchestrator.product_store,
        legal_hold_provider=NoLegalHold(),
        clock=lambda: EVALUATED,
    )
    executor.initialize_cleanup_root(
        existing_run_id="cleanup-fault-delete",
        root_id="cleanup-root-" + "c" * 32,
        initialized_at=EVALUATED - timedelta(days=400),
    )
    quarantine = executor.dry_run_quarantine(
        "cleanup-fault-delete",
        execution_id="cleanup-fault-delete-quarantine",
        idempotency_key="cleanup-fault-delete-quarantine-key",
        expires_at=EVALUATED + timedelta(hours=1),
    )
    assert quarantine.plan is not None
    quarantine_request, quarantine_provider = _approve_cleanup(
        orchestrator,
        quarantine.plan,
        approval_run_id="cleanup-fault-delete-quarantine-approval",
    )
    assert (
        executor.execute_quarantine(
            quarantine.plan,
            approval_run_id="cleanup-fault-delete-quarantine-approval",
            approval_request_id=quarantine_request,
            authority_provider=quarantine_provider,
        ).outcome_kind
        == "committed"
    )
    executor.clock = lambda: DELETE_AT
    delete = executor.dry_run_delete(
        "cleanup-fault-delete",
        execution_id="cleanup-fault-delete-execution",
        idempotency_key="cleanup-fault-delete-key",
        expires_at=DELETE_AT + timedelta(hours=1),
    )
    assert delete.plan is not None
    delete_request, delete_provider = _approve_cleanup(
        orchestrator,
        delete.plan,
        approval_run_id="cleanup-fault-delete-approval",
        decision_at=DELETE_AT,
    )
    cancellation = {"requested": False}

    def request_cancel_after_first_unlink(hook: str) -> None:
        if hook == "delete.after_unlink.0":
            cancellation["requested"] = True

    executor.store.fault_injector = request_cancel_after_first_unlink

    result = executor.execute_delete(
        delete.plan,
        approval_run_id="cleanup-fault-delete-approval",
        approval_request_id=delete_request,
        authority_provider=delete_provider,
        cancelled=lambda: cancellation["requested"],
    )
    report = executor.inspect_reconciliation(delete.plan)
    retry = executor.execute_delete(
        delete.plan,
        approval_run_id="cleanup-fault-delete-approval",
        approval_request_id=delete_request,
        authority_provider=delete_provider,
    )
    current = executor.store.read_current(run_id_sha256("cleanup-fault-delete"))

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert result.failure.filesystem_effect == "partial_delete"
    assert current is not None
    assert current[0].state == "delete_prepared"
    assert report.classification == "reconciliation_required"
    assert report.observed_staging == "partial"
    assert retry.failure is not None
    assert retry.failure.code is CleanupFailureCode.RECONCILIATION_REQUIRED
    assert retry.failure.filesystem_effect == "partial_delete"
