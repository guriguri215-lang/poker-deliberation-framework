from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    DEFAULT_LOCAL_DATA_POLICY,
    ClassificationSource,
    EncryptionRequirement,
    LifecycleDisposition,
    LifecyclePolicyError,
    LifecyclePolicyFailureCode,
    LifecycleSubject,
    ProtectionReason,
    QuarantineReason,
    RetentionAnchorKind,
    SubjectKind,
    SubjectState,
    canonical_local_data_json,
    canonical_local_data_sha256,
    classify_artifact,
    evaluate_local_data,
)

NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
ANCHOR = NOW - timedelta(days=90)


def _run_subject(**updates: object) -> LifecycleSubject:
    values: dict[str, object] = {
        "subject_kind": SubjectKind.RUN_PAYLOAD,
        "subject_id": "subject-1",
        "logical_name": "input.json",
        "classification": ContextClassification.INTERNAL,
        "state": SubjectState.VERIFIED_TERMINAL,
        "retention_anchor_kind": RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
        "retention_started_at": ANCHOR,
        "run_id": "run-1",
        "revision": 1,
        "subject_sha256": "a" * 64,
        "source_sha256": "b" * 64,
        "owned_by_application": True,
        "integrity_verified": True,
        "lineage_verified": True,
        "legal_hold": False,
    }
    values.update(updates)
    return LifecycleSubject(**values)


def _audit(subject: LifecycleSubject, **kwargs: object):
    result = evaluate_local_data(subject, clock=lambda: NOW, **kwargs)
    assert result.status == "evaluated"
    assert result.audit is not None
    assert result.failure is None
    return result.audit


def test_policy_is_strict_frozen_exact_and_canonical() -> None:
    policy = DEFAULT_LOCAL_DATA_POLICY

    assert policy.model_dump(mode="json")["schema_version"] == "1.0.0"
    assert [
        (
            profile.classification,
            profile.retention_days,
            profile.encryption,
        )
        for profile in policy.profiles
    ] == [
        (
            ContextClassification.PUBLIC,
            365,
            EncryptionRequirement.DEFERRED_NO_CLAIM,
        ),
        (
            ContextClassification.INTERNAL,
            90,
            EncryptionRequirement.DEFERRED_NO_CLAIM,
        ),
        (
            ContextClassification.SENSITIVE,
            30,
            EncryptionRequirement.REQUIRED_BEFORE_PERSISTENCE,
        ),
        (
            ContextClassification.RESTRICTED,
            0,
            EncryptionRequirement.PERSISTENCE_FORBIDDEN,
        ),
    ]
    assert policy.cache_max_days == 7
    assert policy.temp_max_days == 1
    assert policy.lifecycle_audit_days == 365
    assert policy.disposition_receipt_days == 365
    assert policy.quarantine_review_days == 30
    assert policy.canonical_sha256 == canonical_local_data_sha256(policy)
    assert canonical_local_data_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'

    with pytest.raises(ValidationError):
        policy.cache_max_days = 8
    with pytest.raises(ValidationError):
        LifecycleSubject.model_validate(
            {
                **_run_subject().model_dump(mode="python"),
                "unexpected": True,
            }
        )


@pytest.mark.parametrize(
    ("logical_name", "kind"),
    [
        ("input.json", SubjectKind.RUN_PAYLOAD),
        ("agent_reports/analyst.json", SubjectKind.RUN_PAYLOAD),
        ("tool_results/equity.input.json", SubjectKind.RUN_PAYLOAD),
        ("normalized_case.json", SubjectKind.RUN_PAYLOAD),
        ("assumptions.json", SubjectKind.RUN_PAYLOAD),
        ("evidence.jsonl", SubjectKind.RUN_PAYLOAD),
        ("approvals.json", SubjectKind.RUN_PAYLOAD),
        (".poker-deliberation-run", SubjectKind.RUN_AUDIT),
        ("state.json", SubjectKind.RUN_AUDIT),
        ("assignments.json", SubjectKind.RUN_AUDIT),
        ("agent_execution_records.json", SubjectKind.RUN_AUDIT),
        ("security_events.json", SubjectKind.RUN_AUDIT),
        ("disputes.json", SubjectKind.RUN_AUDIT),
        ("tool_results/equity.json", SubjectKind.RUN_AUDIT),
        ("final_report.json", SubjectKind.RUN_REPORT),
        ("final_report.md", SubjectKind.RUN_REPORT),
    ],
)
def test_current_artifact_mapping_starts_internal(logical_name: str, kind: SubjectKind) -> None:
    classified = classify_artifact(
        logical_name,
        source_classifications=(ContextClassification.PUBLIC,),
    )

    assert classified.subject_kind is kind
    assert classified.classification is ContextClassification.INTERNAL
    assert classified.classification_source is ClassificationSource.SOURCE_INHERITANCE


def test_classification_is_source_monotone_and_credential_safe() -> None:
    sensitive = classify_artifact(
        "final_report.json",
        source_classifications=(
            ContextClassification.PUBLIC,
            ContextClassification.SENSITIVE,
        ),
    )
    public = classify_artifact(
        "final_report.json",
        explicit_classification=ContextClassification.PUBLIC,
        explicit_source_trusted=True,
    )
    restricted = classify_artifact(
        "final_report.json",
        explicit_classification=ContextClassification.PUBLIC,
        explicit_source_trusted=True,
        contains_restricted_secret=True,
    )

    assert sensitive.classification is ContextClassification.SENSITIVE
    assert public.classification is ContextClassification.PUBLIC
    assert restricted.classification is ContextClassification.RESTRICTED
    assert restricted.classification_source is ClassificationSource.CREDENTIAL_DETECTION

    with pytest.raises(LifecyclePolicyError) as exc_info:
        classify_artifact(
            "input.json",
            source_classifications=(ContextClassification.RESTRICTED,),
            explicit_classification=ContextClassification.INTERNAL,
            explicit_source_trusted=True,
        )
    assert exc_info.value.failure.code is LifecyclePolicyFailureCode.CLASSIFICATION_DOWNGRADE_DENIED


def test_exact_expiry_boundary_proposes_but_never_executes_deletion() -> None:
    before = evaluate_local_data(
        _run_subject(),
        clock=lambda: NOW - timedelta(microseconds=1),
    )
    boundary = evaluate_local_data(_run_subject(), clock=lambda: NOW)

    assert before.audit is not None
    assert before.audit.proposed_disposition is LifecycleDisposition.RETAIN
    assert boundary.audit is not None
    assert boundary.audit.retention_expires_at == NOW
    assert boundary.audit.proposed_disposition is LifecycleDisposition.DELETE_CANDIDATE
    assert "filesystem_mutation" not in boundary.audit.model_fields


def test_protection_and_quarantine_precede_destructive_eligibility() -> None:
    protected = _audit(_run_subject(state=SubjectState.ACTIVE))
    quarantined = _audit(
        _run_subject(
            state=SubjectState.CORRUPT,
            retention_anchor_kind=RetentionAnchorKind.NOT_APPLICABLE,
            retention_started_at=None,
        )
    )
    legacy = _audit(
        _run_subject(
            state=SubjectState.LEGACY_UNVERIFIED,
            retention_anchor_kind=RetentionAnchorKind.NOT_APPLICABLE,
            retention_started_at=None,
        )
    )

    assert protected.proposed_disposition is LifecycleDisposition.PROTECTED
    assert protected.protection_reasons == (ProtectionReason.ACTIVE_RUN,)
    assert quarantined.proposed_disposition is LifecycleDisposition.QUARANTINE_CANDIDATE
    assert quarantined.quarantine_reasons == (QuarantineReason.CORRUPT,)
    assert legacy.proposed_disposition is LifecycleDisposition.PROTECTED
    assert legacy.manual_review_required is True
    assert ProtectionReason.LEGACY_UNVERIFIED in legacy.protection_reasons


def test_encryption_and_attempt_context_policy_are_fail_closed_values() -> None:
    sensitive = _audit(
        _run_subject(
            classification=ContextClassification.SENSITIVE,
            classification_source=ClassificationSource.SOURCE_INHERITANCE,
        )
    )
    encrypted = _audit(
        _run_subject(
            classification=ContextClassification.SENSITIVE,
            classification_source=ClassificationSource.SOURCE_INHERITANCE,
        ),
        encryption_available=True,
    )
    restricted = _audit(
        _run_subject(
            classification=ContextClassification.RESTRICTED,
            classification_source=ClassificationSource.CREDENTIAL_DETECTION,
        )
    )
    attempt = _audit(
        LifecycleSubject(
            subject_kind=SubjectKind.ATTEMPT_CONTEXT,
            subject_id="context-1",
            logical_name="attempt-context",
            state=SubjectState.ACTIVE,
            owned_by_application=True,
            integrity_verified=True,
            lineage_verified=True,
            legal_hold=False,
        )
    )

    assert sensitive.proposed_disposition is LifecycleDisposition.DENY_PERSISTENCE
    assert sensitive.failure_code is LifecyclePolicyFailureCode.ENCRYPTION_REQUIRED
    assert encrypted.proposed_disposition is LifecycleDisposition.DELETE_CANDIDATE
    assert restricted.failure_code is LifecyclePolicyFailureCode.PERSISTENCE_FORBIDDEN
    assert attempt.proposed_disposition is LifecycleDisposition.DENY_PERSISTENCE


def test_subject_overrides_use_typed_anchors_and_fixed_utc_days() -> None:
    cache = _audit(
        LifecycleSubject(
            subject_kind=SubjectKind.APPLICATION_CACHE,
            subject_id="cache-1",
            logical_name="cache-entry",
            state=SubjectState.VERIFIED_TERMINAL,
            retention_anchor_kind=RetentionAnchorKind.APPLICATION_CREATED,
            retention_started_at=NOW - timedelta(days=7),
            owned_by_application=True,
            integrity_verified=True,
            lineage_verified=True,
            legal_hold=False,
        )
    )
    temporary = _audit(
        LifecycleSubject(
            subject_kind=SubjectKind.APPLICATION_TEMP,
            subject_id="temp-1",
            logical_name="temp-entry",
            state=SubjectState.VERIFIED_TERMINAL,
            retention_anchor_kind=RetentionAnchorKind.APPLICATION_CREATED,
            retention_started_at=NOW - timedelta(days=1),
            owned_by_application=True,
            integrity_verified=True,
            lineage_verified=True,
            legal_hold=False,
        )
    )

    assert cache.retention_expires_at == NOW
    assert temporary.retention_expires_at == NOW
    assert cache.proposed_disposition is LifecycleDisposition.DELETE_CANDIDATE
    assert temporary.proposed_disposition is LifecycleDisposition.DELETE_CANDIDATE


def test_classification_source_and_lifecycle_metadata_invariants_are_strict() -> None:
    with pytest.raises(ValidationError, match="default classification source"):
        _run_subject(classification=ContextClassification.PUBLIC)
    with pytest.raises(ValidationError, match="credential detection"):
        _run_subject(
            classification_source=ClassificationSource.CREDENTIAL_DETECTION,
        )
    with pytest.raises(ValidationError, match="logical name"):
        _run_subject(logical_name="unknown.json")
    with pytest.raises(ValidationError, match="must be internal"):
        LifecycleSubject(
            subject_kind=SubjectKind.LIFECYCLE_AUDIT,
            subject_id="lifecycle-audit-1",
            logical_name="lifecycle-audit",
            classification=ContextClassification.SENSITIVE,
            classification_source=ClassificationSource.EXPLICIT_TRUSTED,
            state=SubjectState.VERIFIED_TERMINAL,
            retention_anchor_kind=RetentionAnchorKind.DECISION_COMMITTED,
            retention_started_at=NOW,
            owned_by_application=True,
            integrity_verified=True,
            lineage_verified=True,
            legal_hold=False,
        )


def test_audit_metadata_is_bounded_and_contains_hashes_not_raw_content() -> None:
    audit = _audit(
        _run_subject(),
        approval_reference="approval/local-data-1",
        action_digest="c" * 64,
    )
    dumped = audit.model_dump(mode="json")

    assert dumped["policy_sha256"] == DEFAULT_LOCAL_DATA_POLICY.canonical_sha256
    assert dumped["subject_sha256"] == "a" * 64
    assert dumped["source_sha256"] == "b" * 64
    assert dumped["approval_reference"] == "approval/local-data-1"
    assert "raw_content" not in dumped
    assert "secret" not in dumped
