from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from poker_deliberation.local_data_cleanup import evaluate_cleanup_candidate
from poker_deliberation.local_data_cleanup_canonical import (
    CleanupCanonicalError,
    canonical_cleanup_bytes,
    cleanup_plan_sha256,
    parse_canonical_cleanup_json,
    parse_cleanup_model,
    run_id_sha256,
)
from poker_deliberation.local_data_cleanup_models import (
    ApprovalRetentionEvidenceV1,
    CleanupCandidateEvidenceV1,
    CleanupFailureCode,
    CleanupLimitsV1,
    CleanupPlanV1,
    LegalHoldSnapshotV1,
    LifecycleEligibilityV1,
    ProductRunSourceV1,
    TreeInventoryEntryV1,
    TreeInventoryV1,
)
from poker_deliberation.local_data_policy import DEFAULT_LOCAL_DATA_POLICY

SHA = "a" * 64
NOW = datetime(2026, 7, 25, 9, 0, tzinfo=UTC)


def _inventory(run_hash: str) -> TreeInventoryV1:
    return TreeInventoryV1(
        run_id_sha256=run_hash,
        entries=(
            TreeInventoryEntryV1(
                relative_path="control",
                entry_kind="directory",
                size_bytes=0,
                identity_sha256="b" * 64,
            ),
            TreeInventoryEntryV1(
                relative_path="control/current.json",
                entry_kind="file",
                size_bytes=12,
                content_sha256="c" * 64,
                identity_sha256="d" * 64,
            ),
        ),
        entry_count=2,
        total_bytes=12,
    )


def _evidence(**updates: object) -> CleanupCandidateEvidenceV1:
    run_hash = run_id_sha256("run-1")
    source = ProductRunSourceV1(
        run_id="run-1",
        run_id_sha256=run_hash,
        product_root_identity_sha256=SHA,
        product_ownership_marker_sha256="b" * 64,
        current_revision=3,
        current_transaction_id="txn-" + "1" * 32,
        current_pointer_sha256="c" * 64,
        manifest_sha256="d" * 64,
        inventory_sha256="e" * 64,
        completion_marker_sha256="f" * 64,
        terminal_status="succeeded",
        terminal_published_at=NOW - timedelta(days=400),
    )
    values: dict[str, object] = {
        "cleanup_root_id": "cleanup-root-" + "2" * 32,
        "cleanup_root_marker_sha256": "3" * 64,
        "executor_sha256": "4" * 64,
        "source": source,
        "tree_inventory": _inventory(run_hash),
        "lifecycle": LifecycleEligibilityV1(
            local_data_policy_id=DEFAULT_LOCAL_DATA_POLICY.policy_id,
            local_data_policy_sha256=DEFAULT_LOCAL_DATA_POLICY.canonical_sha256,
            lifecycle_audit_sha256="5" * 64,
            audited_subject_count=2,
            delete_candidate_count=2,
            latest_retention_expires_at=NOW - timedelta(days=1),
            evaluated_at=NOW,
        ),
        "approval_retention": ApprovalRetentionEvidenceV1(
            approval_ledger_sha256="6" * 64,
            v1_pending_count=0,
            v2_pending_count=0,
            failure_audit_head_sha256="7" * 64,
            failure_audit_retention_expires_at=NOW - timedelta(seconds=1),
            evaluated_at=NOW,
        ),
        "legal_hold": LegalHoldSnapshotV1(
            provider_id="legal-hold-test",
            provider_version="1.0.0",
            run_id_sha256=run_hash,
            legal_hold=False,
            snapshot_reference_sha256="8" * 64,
            resolved_at=NOW,
        ),
        "expected_cleanup_revision": 0,
        "expected_cleanup_pointer_sha256": None,
        "product_active": False,
        "ownership_verified": True,
        "path_confinement_verified": True,
        "integrity_verified": True,
        "lineage_verified": True,
        "cleanup_capacity_reserved": True,
        "generated_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "execution_id": "cleanup-execution-1",
        "idempotency_key": "cleanup-key-1",
    }
    values.update(updates)
    return CleanupCandidateEvidenceV1(**values)


def test_cleanup_models_are_strict_frozen_and_extra_forbid() -> None:
    with pytest.raises(ValidationError):
        CleanupLimitsV1(maximum_tree_entries="10000")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        CleanupLimitsV1(unapproved=True)  # type: ignore[call-arg]
    limits = CleanupLimitsV1()
    with pytest.raises(ValidationError):
        limits.maximum_tree_entries = 1  # type: ignore[misc]


def test_canonical_bytes_are_nfc_compact_and_domain_separated() -> None:
    value = {"schema_version": "1.0.0", "name": "é", "number": 1}
    encoded = canonical_cleanup_bytes(value)
    assert encoded == (
        b'{"name":"\\xc3\\xa9","number":1,"schema_version":"1.0.0"}'.decode(
            "unicode_escape"
        ).encode("latin1")
    )
    assert not encoded.endswith(b"\n")
    assert run_id_sha256("run-1") != hashlib.sha256(encoded).hexdigest()
    with pytest.raises(CleanupCanonicalError, match="NFC"):
        canonical_cleanup_bytes({"name": "e\u0301"})


@pytest.mark.parametrize(
    "payload",
    [
        b'{"schema_version":"1.0.0","schema_version":"1.0.0"}',
        b"\xef\xbb\xbf{}",
        b'{"schema_version":"1.0.0"}\n',
        b'{ "schema_version":"1.0.0"}',
        b'{"value":NaN}',
    ],
)
def test_parser_rejects_noncanonical_or_ambiguous_bytes(payload: bytes) -> None:
    with pytest.raises(CleanupCanonicalError):
        parse_canonical_cleanup_json(payload)


def test_eligible_product_candidate_builds_one_exact_plan() -> None:
    result = evaluate_cleanup_candidate(_evidence())
    assert result.outcome_kind == "eligible"
    assert result.plan is not None
    assert result.plan_sha256 == cleanup_plan_sha256(result.plan)
    assert len(result.plan.actions) == 1
    assert result.plan.actions[0].action_kind == "quarantine_product_run"
    assert result.filesystem_mutation is False
    assert result.domain_mutation is False
    reparsed = parse_cleanup_model(canonical_cleanup_bytes(result.plan), CleanupPlanV1)
    assert reparsed == result.plan


@pytest.mark.parametrize(
    ("updates", "code"),
    [
        ({"ownership_verified": False}, CleanupFailureCode.OWNERSHIP_UNVERIFIED),
        ({"path_confinement_verified": False}, CleanupFailureCode.PATH_CONFINEMENT_FAILED),
        ({"integrity_verified": False}, CleanupFailureCode.CANDIDATE_INELIGIBLE),
        ({"product_active": True}, CleanupFailureCode.ACTIVE_OR_PENDING),
        ({"cleanup_capacity_reserved": False}, CleanupFailureCode.CAPACITY_EXCEEDED),
    ],
)
def test_protection_precedence_returns_mutation_zero(
    updates: dict[str, object],
    code: CleanupFailureCode,
) -> None:
    result = evaluate_cleanup_candidate(_evidence(**updates))
    assert result.outcome_kind == "ineligible"
    assert result.failure is not None
    assert result.failure.code is code
    assert result.filesystem_mutation is False
    assert result.domain_mutation is False


def test_pending_approval_precedes_legal_hold_and_policy_failure() -> None:
    approval = _evidence().approval_retention.model_copy(update={"v2_pending_count": 1})
    hold = _evidence().legal_hold.model_copy(update={"legal_hold": True})
    lifecycle = _evidence().lifecycle.model_copy(update={"delete_candidate_count": 0})
    result = evaluate_cleanup_candidate(
        _evidence(
            approval_retention=approval,
            legal_hold=hold,
            lifecycle=lifecycle,
        )
    )
    assert result.failure is not None
    assert result.failure.code is CleanupFailureCode.ACTIVE_OR_PENDING


def test_any_plan_field_change_changes_digest() -> None:
    result = evaluate_cleanup_candidate(_evidence())
    assert result.plan is not None
    changed = result.plan.model_copy(update={"execution_id": "cleanup-execution-2"})
    assert cleanup_plan_sha256(changed) != result.plan_sha256
