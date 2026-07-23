from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    DEFAULT_LOCAL_DATA_POLICY,
    ArtifactClassification,
    ClassificationEvidence,
    ClassificationSource,
    EncryptionCapabilityState,
    EvidenceVerificationState,
    LifecycleDisposition,
    LifecycleFailure,
    LifecyclePolicyError,
    LifecyclePolicyFailureCode,
    LifecycleSubject,
    OwnershipProvenance,
    ProtectionReason,
    RetentionAnchorKind,
    RunVerificationBasis,
    SubjectEncryptionState,
    SubjectKind,
    SubjectState,
    classify_artifact,
    evaluate_local_data,
)

pytestmark = pytest.mark.adversarial

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)


def _subject() -> LifecycleSubject:
    return LifecycleSubject(
        subject_kind=SubjectKind.RUN_AUDIT,
        subject_id="audit-security",
        logical_name="state.json",
        state=SubjectState.VERIFIED_TERMINAL,
        retention_anchor_kind=RetentionAnchorKind.VERIFIED_TERMINAL_PUBLISHED,
        retention_started_at=NOW - timedelta(days=90),
        run_id="run-security",
        revision=1,
        subject_sha256="a" * 64,
        source_sha256="b" * 64,
        run_verification_basis=RunVerificationBasis.FUTURE_VERIFIED_REVISION_V1,
        encryption_state=SubjectEncryptionState.UNKNOWN_OR_UNENCRYPTED,
        ownership_provenance=OwnershipProvenance.FUTURE_VERIFIED_MANIFEST_V1,
        integrity_state=EvidenceVerificationState.VERIFIED,
        lineage_state=EvidenceVerificationState.VERIFIED,
        legal_hold=False,
    )


def test_unknown_extra_and_naive_time_inputs_fail_closed() -> None:
    extra = _subject().model_dump(mode="python")
    extra["path"] = "../../user_materials/private.txt"
    result = evaluate_local_data(extra, clock=lambda: NOW)

    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.INVALID_POLICY
    assert result.failure.filesystem_mutation is False
    assert result.failure.domain_mutation is False

    unsupported = _subject().model_dump(mode="python")
    unsupported["schema_version"] = "2.0.0"
    result = evaluate_local_data(unsupported, clock=lambda: NOW)
    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.UNSUPPORTED_SCHEMA
    assert result.failure.manual_review_required is True

    naive = _subject().model_dump(mode="python")
    naive["retention_started_at"] = datetime(2026, 1, 1)
    result = evaluate_local_data(naive, clock=lambda: NOW)
    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.INVALID_UTC


@pytest.mark.parametrize(
    "logical_name",
    [
        "../input.json",
        "user_materials/input.json",
        "tmp/goal/input.json",
        "agent_reports/../../secret.json",
        "tool_results/result.json/extra",
        "state.json\x00",
        "agent_reports/CON.json",
        "agent_reports/NUL.json",
        "tool_results/LPT1.json",
    ],
)
def test_path_like_or_unknown_logical_names_are_never_ownership_proof(
    logical_name: str,
) -> None:
    with pytest.raises(LifecyclePolicyError) as exc_info:
        classify_artifact(logical_name)

    assert exc_info.value.failure.code is LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND
    assert exc_info.value.failure.manual_review_required is True


def test_exported_artifact_schema_cannot_bypass_the_logical_mapping() -> None:
    with pytest.raises(ValidationError, match="approved kind"):
        ArtifactClassification(
            logical_name="../user_materials/private.txt",
            subject_kind=SubjectKind.RUN_REPORT,
            classification=ContextClassification.INTERNAL,
            classification_source=ClassificationSource.DEFAULT_INTERNAL,
            classification_evidence=ClassificationEvidence(),
        )


def test_untrusted_public_and_source_downgrade_are_denied() -> None:
    with pytest.raises(LifecyclePolicyError) as untrusted:
        classify_artifact(
            "final_report.md",
            explicit_classification=ContextClassification.PUBLIC,
        )
    with pytest.raises(LifecyclePolicyError) as downgrade:
        classify_artifact(
            "final_report.md",
            source_classifications=(ContextClassification.SENSITIVE,),
            explicit_classification=ContextClassification.INTERNAL,
            explicit_source_trusted=True,
        )

    assert (
        untrusted.value.failure.code is LifecyclePolicyFailureCode.CLASSIFICATION_DOWNGRADE_DENIED
    )
    assert (
        downgrade.value.failure.code is LifecyclePolicyFailureCode.CLASSIFICATION_DOWNGRADE_DENIED
    )


@pytest.mark.parametrize(
    "classification",
    [
        ContextClassification.INTERNAL,
        ContextClassification.SENSITIVE,
    ],
)
def test_every_untrusted_explicit_classification_is_denied(
    classification: ContextClassification,
) -> None:
    with pytest.raises(LifecyclePolicyError) as exc_info:
        classify_artifact(
            "final_report.md",
            explicit_classification=classification,
            explicit_source_trusted=False,
        )

    assert exc_info.value.failure.code is LifecyclePolicyFailureCode.CLASSIFICATION_DOWNGRADE_DENIED


def test_public_requires_a_completed_clean_restricted_secret_check() -> None:
    with pytest.raises(LifecyclePolicyError):
        classify_artifact(
            "final_report.md",
            explicit_classification=ContextClassification.PUBLIC,
            explicit_source_trusted=True,
        )
    detected = classify_artifact(
        "final_report.md",
        explicit_classification=ContextClassification.PUBLIC,
        explicit_source_trusted=True,
        restricted_secret_check_completed=True,
        contains_restricted_secret=True,
    )
    assert detected.classification is ContextClassification.RESTRICTED


def test_policy_hash_substitution_and_invalid_audit_fields_return_no_action() -> None:
    mismatch = evaluate_local_data(
        _subject(),
        clock=lambda: NOW,
        expected_policy_sha256="f" * 64,
    )
    invalid_action = evaluate_local_data(
        _subject(),
        clock=lambda: NOW,
        action_digest="../../not-a-digest",
    )

    assert mismatch.failure is not None
    assert mismatch.failure.code is LifecyclePolicyFailureCode.POLICY_HASH_MISMATCH
    assert invalid_action.failure is not None
    assert invalid_action.failure.code is LifecyclePolicyFailureCode.INVALID_POLICY
    assert mismatch.audit is None
    assert invalid_action.audit is None


def test_unknown_policy_and_classification_require_manual_review() -> None:
    unknown_policy = DEFAULT_LOCAL_DATA_POLICY.model_dump(mode="python")
    unknown_policy["policy_id"] = "unknown-policy"
    future_policy = DEFAULT_LOCAL_DATA_POLICY.model_dump(mode="python")
    future_policy["schema_version"] = "2.0.0"
    unknown_classification = _subject().model_dump(mode="python")
    unknown_classification["classification"] = "unknown"

    policy_result = evaluate_local_data(
        _subject(),
        clock=lambda: NOW,
        policy=unknown_policy,
    )
    classification_result = evaluate_local_data(
        unknown_classification,
        clock=lambda: NOW,
    )
    future_policy_result = evaluate_local_data(
        _subject(),
        clock=lambda: NOW,
        policy=future_policy,
    )

    assert policy_result.failure is not None
    assert policy_result.failure.code is LifecyclePolicyFailureCode.UNKNOWN_POLICY
    assert policy_result.failure.manual_review_required is True
    assert classification_result.failure is not None
    assert classification_result.failure.code is LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION
    assert classification_result.failure.manual_review_required is True
    assert future_policy_result.failure is not None
    assert future_policy_result.failure.code is LifecyclePolicyFailureCode.UNSUPPORTED_SCHEMA
    assert future_policy_result.failure.manual_review_required is True


def test_subject_classification_is_bound_to_the_typed_source_vector() -> None:
    downgraded = _subject().model_dump(mode="python")
    downgraded["classification_evidence"] = ClassificationEvidence(
        source_classifications=(ContextClassification.RESTRICTED,)
    )

    result = evaluate_local_data(downgraded, clock=lambda: NOW)

    assert result.audit is None
    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.INVALID_POLICY


@pytest.mark.parametrize(
    "logical_name",
    [
        "user_materials/private.txt",
        "C:private",
        "file:stream",
        "NUL",
        "CON.txt",
        "LPT1.log",
        "trailing.",
    ],
)
def test_path_like_non_run_subjects_cannot_become_delete_candidates(
    logical_name: str,
) -> None:
    with pytest.raises(ValidationError, match="opaque portable identifier"):
        LifecycleSubject(
            subject_kind=SubjectKind.APPLICATION_TEMP,
            subject_id="excluded-temp",
            logical_name=logical_name,
            encryption_state=SubjectEncryptionState.UNKNOWN_OR_UNENCRYPTED,
            state=SubjectState.VERIFIED_TERMINAL,
            retention_anchor_kind=RetentionAnchorKind.APPLICATION_CREATED,
            retention_started_at=NOW - timedelta(days=1),
            subject_sha256="c" * 64,
            source_sha256="d" * 64,
            ownership_provenance=OwnershipProvenance.EXCLUDED_USER_MATERIAL,
            integrity_state=EvidenceVerificationState.VERIFIED,
            lineage_state=EvidenceVerificationState.VERIFIED,
            legal_hold=False,
        )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("subject_id", "NUL"),
        ("subject_id", "CON.txt"),
        ("run_id", "LPT1.log"),
        ("run_id", "file."),
    ],
)
def test_windows_device_names_are_rejected_at_identifier_boundaries(
    field_name: str,
    value: str,
) -> None:
    subject_values = _subject().model_dump(mode="python")
    subject_values[field_name] = value

    with pytest.raises(ValidationError, match="opaque portable identifier"):
        LifecycleSubject.model_validate(subject_values)


def test_excluded_non_run_subject_cannot_become_delete_candidate() -> None:
    excluded = LifecycleSubject(
        subject_kind=SubjectKind.APPLICATION_TEMP,
        subject_id="excluded-temp",
        logical_name="excluded-temp",
        encryption_state=SubjectEncryptionState.UNKNOWN_OR_UNENCRYPTED,
        state=SubjectState.VERIFIED_TERMINAL,
        retention_anchor_kind=RetentionAnchorKind.APPLICATION_CREATED,
        retention_started_at=NOW - timedelta(days=1),
        subject_sha256="c" * 64,
        source_sha256="d" * 64,
        ownership_provenance=OwnershipProvenance.EXCLUDED_USER_MATERIAL,
        integrity_state=EvidenceVerificationState.VERIFIED,
        lineage_state=EvidenceVerificationState.VERIFIED,
        legal_hold=False,
    )
    result = evaluate_local_data(excluded, clock=lambda: NOW)
    assert result.audit is not None
    assert result.audit.proposed_disposition is LifecycleDisposition.PROTECTED
    assert ProtectionReason.OWNERSHIP_UNVERIFIED in result.audit.protection_reasons


def test_run_quarantine_cannot_reuse_terminal_publication_expiry() -> None:
    values = _subject().model_dump(mode="python")
    values["state"] = SubjectState.QUARANTINED
    values["run_verification_basis"] = RunVerificationBasis.NOT_APPLICABLE
    values["ownership_provenance"] = OwnershipProvenance.RUN_CONTRACT_V1

    with pytest.raises(ValidationError, match="quarantined state"):
        LifecycleSubject.model_validate(values)


def test_supported_integrity_mismatch_is_quarantine_not_protection() -> None:
    values = _subject().model_dump(mode="python")
    values.update(
        {
            "state": SubjectState.CORRUPT,
            "retention_anchor_kind": RetentionAnchorKind.NOT_APPLICABLE,
            "retention_started_at": None,
            "run_verification_basis": RunVerificationBasis.NOT_APPLICABLE,
            "ownership_provenance": OwnershipProvenance.RUN_CONTRACT_V1,
            "integrity_state": EvidenceVerificationState.MISMATCH,
        }
    )
    result = evaluate_local_data(values, clock=lambda: NOW)

    assert result.audit is not None
    assert result.audit.proposed_disposition is LifecycleDisposition.QUARANTINE_CANDIDATE


def test_public_boundaries_reject_truthy_strings_and_non_string_hashes() -> None:
    with pytest.raises(LifecyclePolicyError):
        classify_artifact(
            "final_report.md",
            explicit_classification=ContextClassification.PUBLIC,
            explicit_source_trusted="false",  # type: ignore[arg-type]
            restricted_secret_check_completed=True,
        )
    with pytest.raises(LifecyclePolicyError) as invalid_name:
        classify_artifact(None)  # type: ignore[arg-type]
    assert invalid_name.value.failure.code is LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND

    result = evaluate_local_data(
        _subject(),
        clock=lambda: NOW,
        expected_policy_sha256=1,  # type: ignore[arg-type]
    )
    assert result.failure is not None
    assert result.failure.code is LifecyclePolicyFailureCode.POLICY_HASH_MISMATCH


def test_audit_schema_rejects_secret_metadata_and_inconsistent_disposition() -> None:
    subject = _subject()
    result = evaluate_local_data(subject, clock=lambda: NOW)
    assert result.audit is not None
    audit_values = result.audit.model_dump(mode="python")

    with pytest.raises(ValidationError, match=r"pattern|secret shape"):
        type(result.audit).model_validate({**audit_values, "logical_name": "sk-abcdefghijk"})
    secret_metadata = [
        ("run_id", "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"),
        ("subject_id", "AKIAABCDEFGHIJKLMNOP"),
        (
            "run_id",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature",
        ),
        ("subject_id", "AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ123456789"),
    ]
    for field_name, secret_value in secret_metadata:
        with pytest.raises(ValidationError, match="pattern"):
            type(result.audit).model_validate(
                {
                    **audit_values,
                    field_name: secret_value,
                }
            )
    with pytest.raises(ValidationError, match="policy hash"):
        type(result.audit).model_validate({**audit_values, "policy_sha256": "f" * 64})
    with pytest.raises(ValidationError, match=r"protection reasons|delete candidate"):
        type(result.audit).model_validate(
            {
                **audit_values,
                "proposed_disposition": LifecycleDisposition.DELETE_CANDIDATE,
                "protection_reasons": (ProtectionReason.ACTIVE_RUN,),
            }
        )


def test_audit_and_failure_never_copy_raw_identifiers_or_approval_references() -> None:
    subject = _subject()
    approval_reference = "approval-security-1"
    result = evaluate_local_data(
        subject,
        clock=lambda: NOW,
        approval_reference=approval_reference,
    )
    assert result.audit is not None
    serialized = result.audit.model_dump_json()

    assert subject.subject_id not in serialized
    assert subject.run_id is not None
    assert subject.run_id not in serialized
    assert approval_reference not in serialized
    assert len(result.audit.subject_id) == 64
    assert result.audit.run_id is not None
    assert len(result.audit.run_id) == 64
    assert result.audit.approval_reference is not None
    assert len(result.audit.approval_reference) == 64

    failure = evaluate_local_data(
        subject,
        clock=lambda: NOW,
        expected_policy_sha256="f" * 64,
    )
    assert failure.failure is not None
    assert subject.subject_id not in failure.failure.model_dump_json()
    assert failure.failure.subject_id == result.audit.subject_id


def test_identifier_digests_are_deterministic_and_purpose_separated() -> None:
    shared_identifier = "shared-identifier"
    subject = _subject().model_copy(
        update={
            "subject_id": shared_identifier,
            "run_id": shared_identifier,
        }
    )

    first = evaluate_local_data(
        subject,
        clock=lambda: NOW,
        approval_reference=shared_identifier,
    )
    second = evaluate_local_data(
        subject,
        clock=lambda: NOW,
        approval_reference=shared_identifier,
    )

    assert first.audit is not None
    assert second.audit is not None
    digests = {
        first.audit.subject_id,
        first.audit.run_id,
        first.audit.approval_reference,
    }
    assert None not in digests
    assert len(digests) == 3
    assert first.audit.subject_id == second.audit.subject_id
    assert first.audit.run_id == second.audit.run_id
    assert first.audit.approval_reference == second.audit.approval_reference


def test_variable_run_artifact_identifier_is_digested_in_audit() -> None:
    raw_identifier = "eyJhbGciOiJIUzI1NiJ9.e30.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    raw_logical_name = f"agent_reports/{raw_identifier}.json"
    subject_values = _subject().model_dump(mode="python")
    subject_values.update(
        {
            "subject_kind": SubjectKind.RUN_PAYLOAD,
            "logical_name": raw_logical_name,
        }
    )

    result = evaluate_local_data(
        LifecycleSubject.model_validate(subject_values),
        clock=lambda: NOW,
    )

    assert result.audit is not None
    assert result.audit.logical_name.startswith("agent_reports/")
    assert result.audit.logical_name.endswith(".json")
    assert raw_identifier not in result.audit.logical_name
    assert raw_identifier not in result.audit.model_dump_json()


def test_failure_message_is_fixed_by_failure_code() -> None:
    with pytest.raises(ValidationError, match="fixed message"):
        LifecycleFailure(
            code=LifecyclePolicyFailureCode.INVALID_POLICY,
            message="Bearer example-secret-value",
        )


def test_restricted_persistence_denial_precedes_active_protection() -> None:
    subject_values = _subject().model_dump(mode="python")
    subject_values.update(
        {
            "state": SubjectState.ACTIVE,
            "retention_anchor_kind": RetentionAnchorKind.NOT_APPLICABLE,
            "retention_started_at": None,
            "run_verification_basis": RunVerificationBasis.NOT_APPLICABLE,
            "ownership_provenance": OwnershipProvenance.RUN_CONTRACT_V1,
            "classification": ContextClassification.RESTRICTED,
            "classification_source": ClassificationSource.CREDENTIAL_DETECTION,
            "classification_evidence": ClassificationEvidence(
                contains_restricted_secret=True,
            ),
        }
    )

    result = evaluate_local_data(
        LifecycleSubject.model_validate(subject_values),
        clock=lambda: NOW,
    )

    assert result.audit is not None
    assert result.audit.proposed_disposition is LifecycleDisposition.DENY_PERSISTENCE
    assert result.audit.failure_code is LifecyclePolicyFailureCode.PERSISTENCE_FORBIDDEN
    assert ProtectionReason.ACTIVE_RUN in result.audit.protection_reasons


def test_incomplete_delete_evidence_retains_or_protects_without_internal_failure() -> None:
    application_subject = LifecycleSubject(
        subject_kind=SubjectKind.APPLICATION_TEMP,
        subject_id="temp-without-hashes",
        logical_name="temp-without-hashes",
        encryption_state=SubjectEncryptionState.UNKNOWN_OR_UNENCRYPTED,
        state=SubjectState.VERIFIED_TERMINAL,
        retention_anchor_kind=RetentionAnchorKind.APPLICATION_CREATED,
        retention_started_at=NOW - timedelta(days=1),
        ownership_provenance=OwnershipProvenance.TYPED_APPLICATION_METADATA_V1,
        integrity_state=EvidenceVerificationState.VERIFIED,
        lineage_state=EvidenceVerificationState.VERIFIED,
        legal_hold=False,
    )
    mixed_run = _subject().model_copy(
        update={"ownership_provenance": OwnershipProvenance.RUN_CONTRACT_V1}
    )

    application_result = evaluate_local_data(application_subject, clock=lambda: NOW)
    run_result = evaluate_local_data(mixed_run, clock=lambda: NOW)

    assert application_result.failure is None
    assert application_result.audit is not None
    assert application_result.audit.proposed_disposition is LifecycleDisposition.RETAIN
    assert run_result.failure is None
    assert run_result.audit is not None
    assert run_result.audit.proposed_disposition is LifecycleDisposition.PROTECTED
    assert ProtectionReason.OWNERSHIP_UNVERIFIED in run_result.audit.protection_reasons


def test_sensitive_audit_cannot_claim_encryption_denial_when_verified() -> None:
    subject_values = _subject().model_dump(mode="python")
    subject_values.update(
        {
            "classification": ContextClassification.SENSITIVE,
            "classification_source": ClassificationSource.EXPLICIT_TRUSTED,
            "classification_evidence": ClassificationEvidence(
                explicit_classification=ContextClassification.SENSITIVE,
                explicit_source_trusted=True,
            ),
            "encryption_state": SubjectEncryptionState.ENCRYPTED_VERIFIED,
        }
    )
    result = evaluate_local_data(
        LifecycleSubject.model_validate(subject_values),
        clock=lambda: NOW,
        encryption_capability=EncryptionCapabilityState.AVAILABLE,
    )
    assert result.audit is not None
    audit_values = result.audit.model_dump(mode="python")

    with pytest.raises(ValidationError, match="disposition"):
        type(result.audit).model_validate(
            {
                **audit_values,
                "proposed_disposition": LifecycleDisposition.DENY_PERSISTENCE,
                "failure_code": LifecyclePolicyFailureCode.ENCRYPTION_REQUIRED,
            }
        )


def test_restricted_audit_cannot_claim_delete_candidate() -> None:
    subject_values = _subject().model_dump(mode="python")
    subject_values.update(
        {
            "classification": ContextClassification.RESTRICTED,
            "classification_source": ClassificationSource.CREDENTIAL_DETECTION,
            "classification_evidence": ClassificationEvidence(
                contains_restricted_secret=True,
            ),
        }
    )
    result = evaluate_local_data(
        LifecycleSubject.model_validate(subject_values),
        clock=lambda: NOW,
    )
    assert result.audit is not None
    assert result.audit.proposed_disposition is LifecycleDisposition.DENY_PERSISTENCE
    audit_values = result.audit.model_dump(mode="python")

    with pytest.raises(ValidationError, match="disposition"):
        type(result.audit).model_validate(
            {
                **audit_values,
                "proposed_disposition": LifecycleDisposition.DELETE_CANDIDATE,
                "failure_code": None,
            }
        )


def test_legacy_and_unverified_ownership_cannot_become_delete_candidates() -> None:
    legacy = _subject().model_copy(
        update={
            "state": SubjectState.LEGACY_UNVERIFIED,
            "retention_anchor_kind": RetentionAnchorKind.NOT_APPLICABLE,
            "retention_started_at": None,
            "run_verification_basis": RunVerificationBasis.LEGACY_V1_UNVERIFIED,
            "ownership_provenance": OwnershipProvenance.RUN_CONTRACT_V1,
        }
    )
    unowned = _subject().model_copy(update={"ownership_provenance": OwnershipProvenance.UNVERIFIED})

    legacy_result = evaluate_local_data(legacy, clock=lambda: NOW)
    unowned_result = evaluate_local_data(unowned, clock=lambda: NOW)

    assert legacy_result.audit is not None
    assert legacy_result.audit.proposed_disposition is LifecycleDisposition.PROTECTED
    assert legacy_result.audit.manual_review_required is True
    assert unowned_result.audit is not None
    assert unowned_result.audit.proposed_disposition is LifecycleDisposition.PROTECTED
    assert ProtectionReason.OWNERSHIP_UNVERIFIED in (unowned_result.audit.protection_reasons)


def test_schema_is_frozen_and_policy_module_has_no_effectful_imports() -> None:
    with pytest.raises(ValidationError):
        DEFAULT_LOCAL_DATA_POLICY.cache_max_days = 8

    module_path = ROOT / "src" / "poker_deliberation" / "local_data_policy.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported_modules.add(node.module)

    forbidden = {
        "os",
        "pathlib",
        "shutil",
        "subprocess",
        "tempfile",
        "poker_deliberation.storage",
        "poker_deliberation.orchestrator",
        "poker_deliberation.cli",
        "poker_deliberation.approvals",
    }
    assert not {
        module
        for module in imported_modules
        if any(module == item or module.startswith(f"{item}.") for item in forbidden)
    }
