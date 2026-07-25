from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.approval_canonical import approval_actor_sha256
from poker_deliberation.approval_models import ApprovalAuthoritySnapshotV2
from poker_deliberation.config import AppConfig
from poker_deliberation.local_data_cleanup import LocalDataCleanupExecutor
from poker_deliberation.local_data_cleanup_canonical import run_id_sha256
from poker_deliberation.local_data_cleanup_models import (
    CleanupFailureCode,
    CleanupLimitsV1,
    LegalHoldSnapshotV1,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.local_data_cleanup_store import (
    CleanupStorageError,
    initialize_cleanup_root,
    scan_cleanup_tree,
)
from poker_deliberation.storage.revision_canonical import CanonicalStorageError
from poker_deliberation.storage.terminal_models import ProductRunError
from tests.fault.test_local_data_cleanup_failures import _prepare_delete_fixture
from tests.integration.test_local_data_cleanup_executor import (
    CleanupAuthority,
    _approve_cleanup,
)

NOW = datetime(2026, 7, 25, 12, tzinfo=UTC)
SECURITY_AT = NOW + timedelta(days=400)


class NoHold:
    def resolve(
        self,
        run_id_sha256: str,
        *,
        evaluated_at: datetime,
    ) -> LegalHoldSnapshotV1:
        return LegalHoldSnapshotV1(
            provider_id="security-hold",
            provider_version="1.0.0",
            run_id_sha256=run_id_sha256,
            legal_hold=False,
            snapshot_reference_sha256=hashlib.sha256(run_id_sha256.encode()).hexdigest(),
            resolved_at=evaluated_at,
        )


def _orchestrator(tmp_path: Path) -> Orchestrator:
    config = AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )
    orchestrator = Orchestrator(
        config,
        terminal_clock=lambda: NOW,
        context_clock=lambda: NOW,
    )
    orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="disposable security fixture",
            analysis_scope="retrospective",
        ),
        run_id="security-run",
    )
    return orchestrator


def test_cleanup_root_cannot_overlap_product_root(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)

    with pytest.raises(CleanupStorageError) as caught:
        initialize_cleanup_root(
            tmp_path / "product" / "cleanup",
            orchestrator.product_store,
            existing_run_id="security-run",
            root_id="cleanup-root-" + "1" * 32,
            initialized_at=NOW,
        )

    assert caught.value.failure.code is CleanupFailureCode.PATH_CONFINEMENT_FAILED


def test_cleanup_root_cannot_be_inside_repository_workspace(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)

    with pytest.raises(CanonicalStorageError, match="excluded root"):
        LocalDataCleanupExecutor(
            Path.cwd() / "unapproved-cleanup-root",
            orchestrator.product_store,
        )


def test_cleanup_root_cannot_be_inside_another_repository(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    repository = tmp_path / "other-repository"
    (repository / ".git").mkdir(parents=True)

    with pytest.raises(CanonicalStorageError, match="excluded root"):
        LocalDataCleanupExecutor(repository / "cleanup", orchestrator.product_store)


def test_cleanup_root_path_cannot_traverse_a_reparse_or_symlink(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    target = tmp_path / "cleanup-target"
    target.mkdir()
    link = tmp_path / "cleanup-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is not available")

    with pytest.raises(CanonicalStorageError, match="link or reparse"):
        LocalDataCleanupExecutor(link, orchestrator.product_store)


def test_cleanup_root_unknown_entry_blocks_normal_marker_reads(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(cleanup_root, orchestrator.product_store)
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "9" * 32,
        initialized_at=NOW,
    )
    (cleanup_root / "unexpected.txt").write_text("untrusted", encoding="utf-8")

    with pytest.raises(CleanupStorageError) as caught:
        executor.store.marker()

    assert caught.value.failure.code is CleanupFailureCode.OWNERSHIP_UNVERIFIED


def test_cleanup_root_product_binding_cannot_be_reused_with_another_root(
    tmp_path: Path,
) -> None:
    first = _orchestrator(tmp_path / "first")
    second = _orchestrator(tmp_path / "second")
    cleanup_root = tmp_path / "cleanup"
    first_executor = LocalDataCleanupExecutor(
        cleanup_root,
        first.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    first_executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "a" * 32,
        initialized_at=NOW,
    )
    second_executor = LocalDataCleanupExecutor(
        cleanup_root,
        second.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )

    result = second_executor.dry_run_quarantine(
        "security-run",
        execution_id="security-cross-root-execution",
        idempotency_key="security-cross-root-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.OWNERSHIP_UNVERIFIED

    plan = first_executor.dry_run_quarantine(
        "security-run",
        execution_id="security-first-root-execution",
        idempotency_key="security-first-root-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert plan.plan is not None
    forged_source = plan.plan.source.model_copy(update={"product_root_identity_sha256": "f" * 64})
    forged_plan = plan.plan.model_copy(update={"source": forged_source})
    forged_request, forged_provider = _approve_cleanup(
        first,
        forged_plan,
        approval_run_id="security-forged-source-root-approval",
        decision_at=SECURITY_AT,
    )
    forged_result = first_executor.execute_quarantine(
        forged_plan,
        approval_run_id="security-forged-source-root-approval",
        approval_request_id=forged_request,
        authority_provider=forged_provider,
    )
    assert forged_result.failure is not None
    assert forged_result.failure.code is CleanupFailureCode.OWNERSHIP_UNVERIFIED
    assert (first.product_store.foundation.runs_root / "security-run").is_dir()

    request_id, provider = _approve_cleanup(
        first,
        plan.plan,
        approval_run_id="security-first-root-approval",
        decision_at=SECURITY_AT,
    )
    committed = first_executor.execute(
        plan.plan,
        approval_run_id="security-first-root-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    assert committed.outcome_kind == "committed"
    assert second_executor.inspect_cleanup_root().status == "corrupt"

    with pytest.raises(CleanupStorageError) as inspect_error:
        second_executor.inspect_reconciliation(plan.plan)
    assert inspect_error.value.failure.code is CleanupFailureCode.OWNERSHIP_UNVERIFIED

    with pytest.raises(CleanupStorageError) as replay_error:
        second_executor.store.read_operation(
            plan.plan,
            approval_run_id_sha256=run_id_sha256("security-first-root-approval"),
            approval_request_id=request_id,
        )
    assert replay_error.value.failure.code is CleanupFailureCode.OWNERSHIP_UNVERIFIED

    cross_root_replay = second_executor.execute(
        plan.plan,
        approval_run_id="security-first-root-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    assert cross_root_replay.failure is not None
    assert cross_root_replay.failure.code is CleanupFailureCode.OWNERSHIP_UNVERIFIED

    recreated_source = first.product_store.foundation.runs_root / "security-run"
    recreated_source.mkdir()
    (recreated_source / "unexpected.json").write_text("{}", encoding="utf-8")
    recreated_report = first_executor.inspect_reconciliation(plan.plan)

    assert recreated_report.observed_source == "mismatch"
    assert recreated_report.classification != "committed"


def test_missing_standalone_journal_invalidates_reachable_cleanup_history(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "c" * 32,
        initialized_at=NOW,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-missing-journal-execution",
        idempotency_key="security-missing-journal-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-missing-journal-approval",
        decision_at=SECURITY_AT,
    )
    committed = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="security-missing-journal-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    assert committed.outcome_kind == "committed"

    run_hash = run_id_sha256("security-run")
    transactions = cleanup_root / "runs" / run_hash / "transactions"
    journal_root = transactions / committed.transaction_id
    (journal_root / "transaction.json").unlink()
    journal_root.rmdir()
    transactions.rmdir()

    with pytest.raises(CleanupStorageError) as read_error:
        executor.store.read_current(run_hash)
    assert read_error.value.failure.code is CleanupFailureCode.STALE_CLEANUP_REVISION

    executor.clock = lambda: SECURITY_AT + timedelta(days=31)
    delete = executor.dry_run_delete(
        "security-run",
        execution_id="security-missing-journal-delete",
        idempotency_key="security-missing-journal-delete-key",
        expires_at=SECURITY_AT + timedelta(days=31, hours=1),
    )
    assert delete.failure is not None
    assert delete.failure.code is CleanupFailureCode.STALE_CLEANUP_REVISION


def test_dangling_former_product_namespace_is_not_treated_as_detached(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    run_path = orchestrator.product_store.foundation.runs_root / "security-run"
    detached_payload = tmp_path / "detached-payload"
    run_path.rename(detached_payload)
    try:
        run_path.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is not available")

    with pytest.raises(ProductRunError) as caught:
        orchestrator.product_store.foundation.acquire_detached_run_authority("security-run")

    assert caught.value.failure.code.value == "path_confinement_failed"


def test_dangling_cleanup_current_pointer_is_effect_unknown(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "0" * 32,
        initialized_at=NOW,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-dangling-current-execution",
        idempotency_key="security-dangling-current-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-dangling-current-approval",
        decision_at=SECURITY_AT,
    )
    committed = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="security-dangling-current-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    assert committed.outcome_kind == "committed"

    current = cleanup_root / "runs" / run_id_sha256("security-run") / "current.json"
    current.unlink()
    try:
        current.symlink_to(current.parent / "missing-current.json")
    except OSError:
        pytest.skip("file symlink creation is not available")

    report = executor.inspect_reconciliation(dry_run.plan)

    assert report.observed_current == "unreadable"
    assert report.classification == "effect_unknown"


def test_symlink_tree_is_rejected_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "payload.json").write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = target / "escape"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is not available")

    with pytest.raises(CleanupStorageError) as caught:
        scan_cleanup_tree(target, run_id_sha256=run_id_sha256("security-run"))

    assert caught.value.failure.code is CleanupFailureCode.LINK_OR_REPARSE_DETECTED
    assert outside.read_text(encoding="utf-8") == "outside"


def test_hardlink_tree_is_rejected_without_unlinking_either_name(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    first = target / "first.json"
    second = target / "second.json"
    first.write_text("{}", encoding="utf-8")
    try:
        os.link(first, second)
    except OSError:
        pytest.skip("hardlink creation is not available")

    with pytest.raises(CleanupStorageError) as caught:
        scan_cleanup_tree(target, run_id_sha256=run_id_sha256("security-run"))

    assert caught.value.failure.code is CleanupFailureCode.HARDLINK_DETECTED
    assert first.exists() and second.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate streams are Windows-specific")
def test_alternate_data_stream_is_rejected_without_deleting_base_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    base = target / "payload.json"
    base.write_text("{}", encoding="utf-8")
    stream = Path(f"{base}:hidden")
    try:
        stream.write_text("hidden", encoding="utf-8")
    except OSError:
        pytest.skip("alternate data streams are unavailable")

    with pytest.raises(CleanupStorageError) as caught:
        scan_cleanup_tree(target, run_id_sha256=run_id_sha256("security-run"))

    assert caught.value.failure.code is CleanupFailureCode.PATH_CONFINEMENT_FAILED
    assert base.read_text(encoding="utf-8") == "{}"


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate streams are Windows-specific")
def test_directory_alternate_stream_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "payload.json").write_text("{}", encoding="utf-8")
    try:
        Path(f"{target}:hidden").write_text("hidden", encoding="utf-8")
    except OSError:
        pytest.skip("directory alternate streams are unavailable")

    with pytest.raises(CleanupStorageError) as caught:
        scan_cleanup_tree(target, run_id_sha256=run_id_sha256("security-run"))

    assert caught.value.failure.code is CleanupFailureCode.PATH_CONFINEMENT_FAILED


def test_quarantine_namespace_case_alias_is_rejected(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(cleanup_root, orchestrator.product_store)
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "8" * 32,
        initialized_at=NOW,
    )
    alias = cleanup_root / "quarantine" / "Security-Run"
    alias.mkdir()

    with pytest.raises(CleanupStorageError) as caught:
        executor.store.quarantine_path("security-run")

    assert caught.value.failure.code is CleanupFailureCode.ALIAS_CONFLICT
    assert alias.is_dir()


@pytest.mark.parametrize("run_id", ("../escape", "CON", "run:name", "run\\name"))
def test_unsafe_run_id_has_no_cleanup_effect(tmp_path: Path, run_id: str) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "2" * 32,
        initialized_at=NOW,
    )
    before = tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*"))

    result = executor.dry_run_quarantine(
        run_id,
        execution_id="security-execution",
        idempotency_key="security-key",
        expires_at=NOW + timedelta(days=401),
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.INVALID_PLAN
    assert tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*")) == before


def test_forged_plan_change_does_not_match_verified_approval(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: NOW + timedelta(days=400),
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "3" * 32,
        initialized_at=NOW,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-approved-execution",
        idempotency_key="security-approved-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-approval-plan",
        decision_at=SECURITY_AT,
    )
    forged = dry_run.plan.model_copy(
        update={
            "execution_id": "security-forged-execution",
            "idempotency_key": "security-forged-key",
        }
    )
    before = tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*"))

    result = executor.execute_quarantine(
        forged,
        approval_run_id="security-approval-plan",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.APPROVAL_MISMATCH
    assert (tmp_path / "product" / "runs" / "security-run").is_dir()
    assert tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*")) == before


def test_live_revocation_after_approval_prevents_effect(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "4" * 32,
        initialized_at=NOW,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-revoked-execution",
        idempotency_key="security-revoked-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-approval-revoked",
        decision_at=SECURITY_AT,
    )
    assert isinstance(provider, CleanupAuthority)
    provider.actor = provider.actor.model_copy(update={"revocation_status": "revoked"})

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="security-approval-revoked",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.AUTHORITY_REVOKED
    assert (tmp_path / "product" / "runs" / "security-run").is_dir()
    assert not (cleanup_root / "quarantine" / "security-run").exists()


def test_legal_hold_provider_substitution_fails_before_journal(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "b" * 32,
        initialized_at=NOW,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-hold-swap-execution",
        idempotency_key="security-hold-swap-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-approval-hold-swap",
        decision_at=SECURITY_AT,
    )

    class SwitchingHold:
        def __init__(self) -> None:
            self.calls = 0

        def resolve(
            self,
            run_id_sha256: str,
            *,
            evaluated_at: datetime,
        ) -> LegalHoldSnapshotV1:
            self.calls += 1
            exact = NoHold().resolve(run_id_sha256, evaluated_at=evaluated_at)
            return (
                exact
                if self.calls == 1
                else exact.model_copy(update={"provider_id": "substituted-hold-provider"})
            )

    switching = SwitchingHold()
    executor.legal_hold_provider = switching
    before = tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*"))

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="security-approval-hold-swap",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert switching.calls == 2
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.LEGAL_HOLD
    assert tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*")) == before
    assert (orchestrator.product_store.foundation.runs_root / "security-run").is_dir()


def test_pre_effect_cancellation_is_mutation_zero(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "5" * 32,
        initialized_at=NOW,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-cancel-execution",
        idempotency_key="security-cancel-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-approval-cancel",
        decision_at=SECURITY_AT,
    )
    before = tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*"))

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="security-approval-cancel",
        approval_request_id=request_id,
        authority_provider=provider,
        cancelled=lambda: True,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.CANCELLED
    assert tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*")) == before
    assert (tmp_path / "product" / "runs" / "security-run").is_dir()


def test_control_capacity_exhaustion_fails_before_journal_or_move(tmp_path: Path) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    initialize_cleanup_root(
        cleanup_root,
        orchestrator.product_store,
        existing_run_id="security-run",
        root_id="cleanup-root-" + "6" * 32,
        initialized_at=NOW,
        limits=CleanupLimitsV1(maximum_control_bytes_per_run=4_000_000),
    )
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-capacity-execution",
        idempotency_key="security-capacity-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-approval-capacity",
        decision_at=SECURITY_AT,
    )

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="security-approval-capacity",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.AUDIT_CAPACITY_EXCEEDED
    assert not (cleanup_root / "runs" / run_id_sha256("security-run")).exists()
    assert (tmp_path / "product" / "runs" / "security-run").is_dir()
    assert not (cleanup_root / "quarantine" / "security-run").exists()


def test_authority_revoked_on_in_lock_recheck_still_has_mutation_zero(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "7" * 32,
        initialized_at=NOW,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-in-lock-revoke-execution",
        idempotency_key="security-in-lock-revoke-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, approved_provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-approval-in-lock-revoke",
        decision_at=SECURITY_AT,
    )

    class RevokeSecondResolution:
        def __init__(self) -> None:
            self.calls = 0

        def resolve_actor(
            self,
            actor_id: str,
            *,
            decision_at: datetime,
        ) -> ApprovalAuthoritySnapshotV2:
            self.calls += 1
            assert actor_id == approved_provider.actor.actor_id
            actor = (
                approved_provider.actor
                if self.calls == 1
                else approved_provider.actor.model_copy(update={"revocation_status": "revoked"})
            )
            return ApprovalAuthoritySnapshotV2(
                provider_id="test-cleanup-authority",
                provider_version="1.0.0",
                resolved_at=decision_at,
                actor=actor,
                actor_sha256=approval_actor_sha256(actor),
            )

    provider = RevokeSecondResolution()
    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="security-approval-in-lock-revoke",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert provider.calls == 2
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.AUTHORITY_REVOKED
    assert not (cleanup_root / "runs" / run_id_sha256("security-run")).exists()
    assert (tmp_path / "product" / "runs" / "security-run").is_dir()


def test_final_authority_callback_mutation_is_rescanned_before_quarantine_move(
    tmp_path: Path,
) -> None:
    orchestrator = _orchestrator(tmp_path)
    cleanup_root = tmp_path / "cleanup"
    executor = LocalDataCleanupExecutor(
        cleanup_root,
        orchestrator.product_store,
        legal_hold_provider=NoHold(),
        clock=lambda: SECURITY_AT,
    )
    executor.initialize_cleanup_root(
        existing_run_id="security-run",
        root_id="cleanup-root-" + "d" * 32,
        initialized_at=NOW,
    )
    dry_run = executor.dry_run_quarantine(
        "security-run",
        execution_id="security-final-callback-execution",
        idempotency_key="security-final-callback-key",
        expires_at=SECURITY_AT + timedelta(hours=1),
    )
    assert dry_run.plan is not None
    request_id, approved_provider = _approve_cleanup(
        orchestrator,
        dry_run.plan,
        approval_run_id="security-final-callback-approval",
        decision_at=SECURITY_AT,
    )
    run_path = orchestrator.product_store.foundation.runs_root / "security-run"

    class MutateOnThirdResolution:
        def __init__(self) -> None:
            self.calls = 0

        def resolve_actor(
            self,
            actor_id: str,
            *,
            decision_at: datetime,
        ) -> ApprovalAuthoritySnapshotV2:
            self.calls += 1
            if self.calls == 3:
                (run_path / "callback-mutation.json").write_text("{}", encoding="utf-8")
            return approved_provider.resolve_actor(actor_id, decision_at=decision_at)

    provider = MutateOnThirdResolution()
    before_cleanup = tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*"))

    result = executor.execute_quarantine(
        dry_run.plan,
        approval_run_id="security-final-callback-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )

    assert provider.calls == 3
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.STALE_SOURCE
    assert tuple(path.relative_to(cleanup_root) for path in cleanup_root.rglob("*")) == (
        before_cleanup
    )
    assert run_path.is_dir()
    assert (run_path / "callback-mutation.json").is_file()
    assert not (cleanup_root / "quarantine" / "security-run").exists()


def test_final_authority_callback_mutation_is_rescanned_before_delete_move(
    tmp_path: Path,
) -> None:
    run_id = "security-delete-final-callback"
    executor, plan, request_id, approved_provider = _prepare_delete_fixture(
        tmp_path,
        run_id=run_id,
        root_character="e",
    )
    source = executor.store.cleanup_root / "quarantine" / run_id

    class MutateOnThirdResolution:
        def __init__(self) -> None:
            self.calls = 0

        def resolve_actor(
            self,
            actor_id: str,
            *,
            decision_at: datetime,
        ) -> ApprovalAuthoritySnapshotV2:
            self.calls += 1
            if self.calls == 3:
                (source / "callback-mutation.json").write_text("{}", encoding="utf-8")
            return approved_provider.resolve_actor(actor_id, decision_at=decision_at)

    provider = MutateOnThirdResolution()
    result = executor.execute_delete(
        plan,
        approval_run_id=f"{run_id}-delete-approval",
        approval_request_id=request_id,
        authority_provider=provider,
    )
    current = executor.store.read_current(run_id_sha256(run_id))

    assert provider.calls == 3
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.STALE_SOURCE
    assert result.failure.filesystem_effect == "none"
    assert current is not None
    assert current[0].state == "quarantined"
    assert source.is_dir()
    assert (source / "callback-mutation.json").is_file()
    assert not any((executor.store.cleanup_root / "deleting").iterdir())
    assert not (
        executor.store.cleanup_root
        / "runs"
        / run_id_sha256(run_id)
        / "transactions"
        / result.transaction_id
    ).exists()
