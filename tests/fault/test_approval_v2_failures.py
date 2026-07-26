from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from poker_deliberation.approval_models import ApprovalFailureCode
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.revision_canonical import CanonicalStorageError
from poker_deliberation.storage.terminal_store import (
    ApprovalFailureAuditError,
    ApprovalFailureAuditRequest,
)

NOW = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        runs_dir=tmp_path / "legacy",
        revision_runs_dir=tmp_path / "product",
        durable_budget_runs_dir=tmp_path / "budget",
    )


def test_audit_pointer_fault_is_unconfirmed_and_domain_current_is_unchanged(
    tmp_path: Path,
) -> None:
    orchestrator = Orchestrator(_config(tmp_path), terminal_clock=lambda: NOW)
    report = orchestrator.run(
        CaseInput(
            kind="calculation",
            raw_text="fault fixture",
            analysis_scope="retrospective",
        ),
        run_id="run-approval-audit-fault",
    )
    before = orchestrator.product_store.read_current(report.run_id)

    def inject(hook: str) -> None:
        if hook == "approval_audit.current.before_replace":
            raise OSError("simulated pointer fault")

    orchestrator.product_store.fault_injector = inject
    request = ApprovalFailureAuditRequest(
        run_id=report.run_id,
        actor_sha256=HASH_A,
        decision_id_sha256=HASH_B,
        idempotency_key_sha256=HASH_A,
        batch_sha256=HASH_B,
        failure_code=ApprovalFailureCode.STALE_DECISION,
        observed_run_revision=before.revision,
        observed_ledger_revision=0,
        occurred_at=NOW,
    )

    with pytest.raises(ApprovalFailureAuditError) as captured:
        orchestrator.product_store.append_approval_failure_audit(request)

    after = orchestrator.product_store.read_current(report.run_id)
    assert captured.value.failure.code is ApprovalFailureCode.AUDIT_UNCONFIRMED
    assert captured.value.failure.audit_confirmed is False
    assert after.current_pointer_sha256 == before.current_pointer_sha256
    with pytest.raises(CanonicalStorageError):
        orchestrator.product_store.read_approval_failure_audit(report.run_id)
