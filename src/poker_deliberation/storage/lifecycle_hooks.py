"""Pure P2-027A lifecycle-policy hooks for verified product revisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from poker_deliberation.local_data_policy import (
    DEFAULT_LOCAL_DATA_POLICY,
    ArtifactClassification,
    EvidenceVerificationState,
    LifecycleAuditMetadata,
    LifecycleEvaluationResult,
    LifecyclePolicyError,
    LifecycleSubject,
    OwnershipProvenance,
    QuarantineReason,
    RetentionAnchorKind,
    RunVerificationBasis,
    SubjectEncryptionState,
    SubjectState,
    classify_artifact,
    evaluate_local_data,
)
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from poker_deliberation.storage.revision_models import PayloadInventoryEntryV1
from poker_deliberation.storage.terminal_canonical import lifecycle_audit_sha256


@dataclass(frozen=True, slots=True)
class TerminalLifecycleAuditBundle:
    audits: tuple[LifecycleAuditMetadata, ...]
    canonical_bytes: bytes
    sha256: str


def _classification(entry: PayloadInventoryEntryV1) -> ArtifactClassification:
    try:
        replay = classify_artifact(
            entry.logical_name,
            source_classifications=(entry.classification_evidence.source_classifications),
            explicit_classification=(entry.classification_evidence.explicit_classification),
            explicit_source_trusted=(entry.classification_evidence.explicit_source_trusted),
            restricted_secret_check_completed=(
                entry.classification_evidence.restricted_secret_check_completed
            ),
            contains_restricted_secret=(entry.classification_evidence.contains_restricted_secret),
        )
    except LifecyclePolicyError as exc:
        raise ValueError("lifecycle artifact classification replay failed") from exc
    if (
        replay.classification is not entry.classification
        or replay.classification_source is not entry.classification_source
        or replay.classification_evidence != entry.classification_evidence
    ):
        raise ValueError("lifecycle artifact classification identity mismatch")
    return replay


def build_terminal_lifecycle_audit(
    *,
    run_id: str,
    revision: int,
    published_at: datetime,
    inventory: tuple[PayloadInventoryEntryV1, ...],
) -> TerminalLifecycleAuditBundle:
    """Evaluate bounded metadata only; this function performs no filesystem action."""

    if not inventory:
        raise ValueError("terminal lifecycle evaluation requires payload inventory")
    audits: list[LifecycleAuditMetadata] = []
    for entry in inventory:
        if entry.logical_name == "lifecycle_audit.json":
            raise ValueError("lifecycle audit cannot recursively audit itself")
        classification = _classification(entry)
        subject = LifecycleSubject(
            subject_kind=classification.subject_kind,
            subject_id=f"artifact-{entry.sha256[:24]}",
            logical_name=entry.logical_name,
            classification=entry.classification,
            classification_source=entry.classification_source,
            classification_evidence=entry.classification_evidence,
            encryption_state=SubjectEncryptionState.UNKNOWN_OR_UNENCRYPTED,
            state=SubjectState.VERIFIED_TERMINAL,
            retention_anchor_kind=RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
            retention_started_at=published_at,
            run_id=run_id,
            revision=revision,
            subject_sha256=entry.sha256,
            source_sha256=entry.source_sha256,
            run_verification_basis=RunVerificationBasis.FUTURE_VERIFIED_REVISION_V1,
            ownership_provenance=OwnershipProvenance.FUTURE_VERIFIED_MANIFEST_V1,
            integrity_state=EvidenceVerificationState.VERIFIED,
            lineage_state=EvidenceVerificationState.VERIFIED,
            legal_hold=False,
        )
        result = evaluate_local_data(
            subject,
            clock=lambda: published_at,
            expected_policy_sha256=DEFAULT_LOCAL_DATA_POLICY.canonical_sha256,
        )
        if result.status != "evaluated" or result.audit is None:
            raise ValueError("terminal lifecycle policy evaluation failed")
        audits.append(result.audit)
    ordered = tuple(sorted(audits, key=lambda item: item.logical_name.encode("utf-8")))
    canonical = canonical_json_bytes(ordered)
    return TerminalLifecycleAuditBundle(
        audits=ordered,
        canonical_bytes=canonical,
        sha256=lifecycle_audit_sha256(canonical),
    )


def evaluate_reader_candidate(
    *,
    run_id: str,
    logical_name: str,
    subject_sha256: str,
    source_sha256: str,
    state: SubjectState,
    evaluated_at: datetime,
) -> LifecycleEvaluationResult:
    """Return a pure manual-review/quarantine candidate for an untrusted read."""

    if state not in {
        SubjectState.INCOMPLETE,
        SubjectState.CORRUPT,
        SubjectState.ORPHAN_TRANSACTION,
        SubjectState.UNSUPPORTED_FUTURE_VERSION,
    }:
        raise ValueError("reader candidate requires an untrusted subject state")
    classification = classify_artifact(
        logical_name,
        restricted_secret_check_completed=True,
    )
    reason = {
        SubjectState.INCOMPLETE: QuarantineReason.INCOMPLETE,
        SubjectState.CORRUPT: QuarantineReason.CORRUPT,
        SubjectState.ORPHAN_TRANSACTION: QuarantineReason.ORPHAN_TRANSACTION,
        SubjectState.UNSUPPORTED_FUTURE_VERSION: QuarantineReason.LINEAGE_MISMATCH,
    }[state]
    future_version = state is SubjectState.UNSUPPORTED_FUTURE_VERSION
    subject = LifecycleSubject(
        subject_kind=classification.subject_kind,
        subject_id=f"candidate-{subject_sha256[:24]}",
        logical_name=logical_name,
        classification=classification.classification,
        classification_source=classification.classification_source,
        classification_evidence=classification.classification_evidence,
        encryption_state=SubjectEncryptionState.UNKNOWN_OR_UNENCRYPTED,
        state=state,
        run_id=run_id,
        subject_sha256=subject_sha256,
        source_sha256=source_sha256,
        ownership_provenance=(
            OwnershipProvenance.UNVERIFIED
            if future_version
            else OwnershipProvenance.RUN_CONTRACT_V1
        ),
        integrity_state=(
            EvidenceVerificationState.UNVERIFIED
            if future_version
            else EvidenceVerificationState.MISMATCH
        ),
        lineage_state=(
            EvidenceVerificationState.UNVERIFIED
            if future_version
            else EvidenceVerificationState.VERIFIED
        ),
        legal_hold=False,
    )
    return evaluate_local_data(
        subject,
        clock=lambda: evaluated_at,
        expected_policy_sha256=DEFAULT_LOCAL_DATA_POLICY.canonical_sha256,
        quarantine_reasons=(reason,),
    )


__all__ = [
    "TerminalLifecycleAuditBundle",
    "build_terminal_lifecycle_audit",
    "evaluate_reader_candidate",
]
