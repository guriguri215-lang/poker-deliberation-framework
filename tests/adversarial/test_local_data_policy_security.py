from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    DEFAULT_LOCAL_DATA_POLICY,
    LifecycleDisposition,
    LifecyclePolicyError,
    LifecyclePolicyFailureCode,
    LifecycleSubject,
    ProtectionReason,
    RetentionAnchorKind,
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
        owned_by_application=True,
        integrity_verified=True,
        lineage_verified=True,
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
    ],
)
def test_path_like_or_unknown_logical_names_are_never_ownership_proof(
    logical_name: str,
) -> None:
    with pytest.raises(LifecyclePolicyError) as exc_info:
        classify_artifact(logical_name)

    assert exc_info.value.failure.code is LifecyclePolicyFailureCode.UNKNOWN_ARTIFACT_KIND


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

    assert policy_result.failure is not None
    assert policy_result.failure.code is LifecyclePolicyFailureCode.UNKNOWN_POLICY
    assert policy_result.failure.manual_review_required is True
    assert classification_result.failure is not None
    assert classification_result.failure.code is LifecyclePolicyFailureCode.UNKNOWN_CLASSIFICATION
    assert classification_result.failure.manual_review_required is True


def test_legacy_and_unverified_ownership_cannot_become_delete_candidates() -> None:
    legacy = _subject().model_copy(
        update={
            "state": SubjectState.LEGACY_UNVERIFIED,
            "retention_anchor_kind": RetentionAnchorKind.NOT_APPLICABLE,
            "retention_started_at": None,
        }
    )
    unowned = _subject().model_copy(update={"owned_by_application": False})

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
