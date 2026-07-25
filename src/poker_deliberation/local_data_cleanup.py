"""Pure P2-027B eligibility and exact cleanup-plan construction."""

from __future__ import annotations

from datetime import timedelta

from poker_deliberation.local_data_cleanup_canonical import (
    cleanup_plan_sha256,
    tree_inventory_sha256,
)
from poker_deliberation.local_data_cleanup_models import (
    ApprovalRetentionEvidenceV1,
    CleanupActionKind,
    CleanupActionV1,
    CleanupCandidateEvidenceV1,
    CleanupDryRunResultV1,
    CleanupFailureCode,
    CleanupPlanV1,
    ProductRunSourceV1,
    cleanup_failure,
)
from poker_deliberation.local_data_policy import DEFAULT_LOCAL_DATA_POLICY


def _ineligible(
    evidence: CleanupCandidateEvidenceV1,
    code: CleanupFailureCode,
) -> CleanupDryRunResultV1:
    return CleanupDryRunResultV1(
        outcome_kind="ineligible",
        run_id_sha256=evidence.source.run_id_sha256,
        failure=cleanup_failure(code, run_id_sha256=evidence.source.run_id_sha256),
    )


def _approval_retention_expired(evidence: ApprovalRetentionEvidenceV1) -> bool:
    expiry = evidence.failure_audit_retention_expires_at
    return expiry is None or evidence.evaluated_at >= expiry


def evaluate_cleanup_candidate(
    evidence: CleanupCandidateEvidenceV1,
) -> CleanupDryRunResultV1:
    """Return one exact plan or a typed mutation-zero failure.

    This function is deliberately pure.  A storage adapter must construct the
    evidence from one bounded verified run read before calling it.
    """

    source = evidence.source
    if not evidence.ownership_verified:
        return _ineligible(evidence, CleanupFailureCode.OWNERSHIP_UNVERIFIED)
    if not evidence.path_confinement_verified:
        return _ineligible(evidence, CleanupFailureCode.PATH_CONFINEMENT_FAILED)
    if not evidence.integrity_verified or not evidence.lineage_verified:
        return _ineligible(evidence, CleanupFailureCode.CANDIDATE_INELIGIBLE)
    if evidence.product_active:
        return _ineligible(evidence, CleanupFailureCode.ACTIVE_OR_PENDING)
    if evidence.approval_retention.v1_pending_count or evidence.approval_retention.v2_pending_count:
        return _ineligible(evidence, CleanupFailureCode.ACTIVE_OR_PENDING)
    if evidence.legal_hold.legal_hold:
        return _ineligible(evidence, CleanupFailureCode.LEGAL_HOLD)
    if (
        evidence.lifecycle.local_data_policy_id != DEFAULT_LOCAL_DATA_POLICY.policy_id
        or evidence.lifecycle.local_data_policy_sha256 != DEFAULT_LOCAL_DATA_POLICY.canonical_sha256
    ):
        return _ineligible(evidence, CleanupFailureCode.POLICY_MISMATCH)
    if (
        not evidence.lifecycle.all_delete_candidates
        or evidence.generated_at < evidence.lifecycle.latest_retention_expires_at
    ):
        return _ineligible(evidence, CleanupFailureCode.CANDIDATE_INELIGIBLE)
    if not _approval_retention_expired(evidence.approval_retention):
        return _ineligible(evidence, CleanupFailureCode.CANDIDATE_INELIGIBLE)
    if not evidence.cleanup_capacity_reserved:
        return _ineligible(evidence, CleanupFailureCode.CAPACITY_EXCEEDED)
    if evidence.generated_at >= evidence.expires_at:
        return _ineligible(evidence, CleanupFailureCode.PLAN_EXPIRED)
    if evidence.expires_at - evidence.generated_at > timedelta(
        seconds=evidence.limits.maximum_plan_lifetime_seconds
    ):
        return _ineligible(evidence, CleanupFailureCode.INVALID_PLAN)

    if isinstance(source, ProductRunSourceV1):
        action = CleanupActionV1(
            action_kind=CleanupActionKind.QUARANTINE_PRODUCT_RUN,
            source_relative_path=f"runs/{source.run_id}",
            destination_relative_path=f"quarantine/{source.run_id}",
        )
    elif evidence.generated_at < source.delete_eligible_at:
        return _ineligible(evidence, CleanupFailureCode.CANDIDATE_INELIGIBLE)
    else:
        action = CleanupActionV1(
            action_kind=CleanupActionKind.DELETE_QUARANTINE_PAYLOAD,
            source_relative_path=f"quarantine/{source.run_id}",
            destination_relative_path=f"deleting/{source.run_id}",
        )

    plan = CleanupPlanV1(
        executor_sha256=evidence.executor_sha256,
        cleanup_root_id=evidence.cleanup_root_id,
        cleanup_root_marker_sha256=evidence.cleanup_root_marker_sha256,
        source=source,
        tree_inventory_sha256=tree_inventory_sha256(evidence.tree_inventory),
        lifecycle=evidence.lifecycle,
        approval_retention=evidence.approval_retention,
        legal_hold=evidence.legal_hold,
        expected_cleanup_revision=evidence.expected_cleanup_revision,
        expected_cleanup_pointer_sha256=evidence.expected_cleanup_pointer_sha256,
        actions=(action,),
        limits=evidence.limits,
        generated_at=evidence.generated_at,
        expires_at=evidence.expires_at,
        execution_id=evidence.execution_id,
        idempotency_key=evidence.idempotency_key,
    )
    return CleanupDryRunResultV1(
        outcome_kind="eligible",
        run_id_sha256=source.run_id_sha256,
        plan=plan,
        plan_sha256=cleanup_plan_sha256(plan),
    )


__all__ = ["evaluate_cleanup_candidate"]
