from __future__ import annotations

from datetime import timedelta

from poker_deliberation.local_data_cleanup import LocalDataCleanupExecutor
from poker_deliberation.local_data_cleanup_canonical import run_id_sha256
from poker_deliberation.local_data_cleanup_models import CleanupFailureCode
from tests.integration.test_local_data_cleanup_executor import (
    DELETE_AT,
    EVALUATED,
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
    assert report.classification == "reconciliation_required"
    assert report.observed_source == "absent"
    assert report.observed_destination == "exact"
    assert retry.outcome_kind == "failed"


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
