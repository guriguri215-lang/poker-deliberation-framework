from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from poker_deliberation.schemas import FinalReport
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from poker_deliberation.storage.terminal_canonical import (
    UnsupportedTerminalVersion,
    budget_binding_sha256,
    canonical_terminal_bytes,
    completion_marker_sha256,
    current_pointer_sha256,
    empty_lineage_head_sha256,
    inventory_entry,
    manifest_sha256,
    parse_completion_marker,
    parse_current_pointer,
    parse_run_manifest,
    required_inventory_sha256,
    terminal_inventory_sha256,
)
from poker_deliberation.storage.terminal_models import (
    BudgetSettlementBindingV2,
    CompletionMarkerV2,
    ProductRunFailureCode,
    ProductRunFailureV2,
    RunCurrentPointerV2,
    RunManifestV2,
    RunReadStatus,
)

NOW = datetime(2026, 7, 25, 12, tzinfo=UTC)
SHA = "1" * 64


def _binding() -> BudgetSettlementBindingV2:
    return BudgetSettlementBindingV2(
        budget_run_id_sha256="2" * 64,
        budget_policy_sha256="3" * 64,
        reservation_operation_id="reserve-run-1",
        reservation_request_sha256="4" * 64,
        permit_id="permit-run-1",
        settlement_operation_id="settle-run-1",
        settlement_id="settlement-run-1",
    )


def _inventory() -> tuple[object, ...]:
    report = FinalReport(run_id="run-1", conclusion="verified fixture")
    entry = inventory_entry(
        logical_name="final_report.json",
        data=canonical_json_bytes(report),
        media_type="application/json",
        artifact_schema_version="poker-final-report-artifact-v2",
        serialization="poker-run-storage-json-v1",
    )
    return (entry,)


def _manifest(
    *,
    publication_kind: str = "product_terminal",
    status: str = "succeeded",
) -> RunManifestV2:
    inventory = _inventory()
    binding = _binding()
    return RunManifestV2(
        publication_kind=publication_kind,
        run_id="run-1",
        revision=1,
        transaction_id="txn-" + "a" * 32,
        created_at=NOW,
        updated_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        framework_version="0.1.0",
        source_commit_id="5" * 64,
        tool_contract_versions=(),
        status=status,
        canonical_input_sha256="6" * 64,
        config_sha256="7" * 64,
        budget_policy_sha256=binding.budget_policy_sha256,
        budget_binding=binding,
        redaction_policy_sha256="8" * 64,
        local_data_policy_sha256="9" * 64,
        state_checkpoint_sha256="a" * 64,
        event_head_sha256=empty_lineage_head_sha256("event"),
        approval_lineage_head_sha256=empty_lineage_head_sha256("approval"),
        context_lineage_head_sha256=empty_lineage_head_sha256("context"),
        execution_lineage_head_sha256=empty_lineage_head_sha256("execution"),
        inventory_sha256=terminal_inventory_sha256(inventory),
        lifecycle_audit_sha256=("b" * 64 if publication_kind == "product_terminal" else None),
        artifacts=inventory,
    )


def test_exact_serialized_field_sets_are_frozen() -> None:
    assert set(RunManifestV2.model_fields) == {
        "run_schema_version",
        "storage_protocol",
        "canonicalization",
        "hash_algorithm",
        "publication_kind",
        "run_id",
        "revision",
        "transaction_id",
        "previous_revision",
        "previous_manifest_sha256",
        "expected_pointer_sha256",
        "created_at",
        "updated_at",
        "producer_id",
        "producer_version",
        "framework_version",
        "source_commit_id",
        "tool_contract_versions",
        "status",
        "canonical_input_sha256",
        "config_sha256",
        "budget_policy_sha256",
        "budget_binding",
        "redaction_policy_sha256",
        "local_data_policy_sha256",
        "state_checkpoint_sha256",
        "event_head_sha256",
        "approval_lineage_head_sha256",
        "context_lineage_head_sha256",
        "execution_lineage_head_sha256",
        "legacy_source",
        "inventory_sha256",
        "lifecycle_audit_sha256",
        "artifacts",
    }
    assert set(CompletionMarkerV2.model_fields) == {
        "schema_version",
        "storage_protocol",
        "canonicalization",
        "hash_algorithm",
        "run_id",
        "terminal_revision",
        "terminal_transaction_id",
        "terminal_status",
        "terminal_manifest_sha256",
        "required_inventory_sha256",
        "budget_binding_sha256",
        "lifecycle_audit_sha256",
        "published_at",
    }
    assert set(RunCurrentPointerV2.model_fields) == {
        "schema_version",
        "storage_protocol",
        "canonicalization",
        "hash_algorithm",
        "publication_kind",
        "run_id",
        "revision",
        "transaction_id",
        "revision_relative_path",
        "status",
        "manifest_sha256",
        "inventory_sha256",
        "completion_marker_sha256",
        "published_at",
    }


def test_terminal_controls_round_trip_canonical_bytes_and_derived_hashes() -> None:
    manifest = _manifest()
    manifest_bytes = canonical_terminal_bytes(manifest)
    marker = CompletionMarkerV2(
        run_id=manifest.run_id,
        terminal_revision=manifest.revision,
        terminal_transaction_id=manifest.transaction_id,
        terminal_status="succeeded",
        terminal_manifest_sha256=manifest_sha256(manifest_bytes),
        required_inventory_sha256=required_inventory_sha256(manifest.artifacts),
        budget_binding_sha256=budget_binding_sha256(manifest.budget_binding),
        lifecycle_audit_sha256=manifest.lifecycle_audit_sha256,
        published_at=NOW,
    )
    marker_bytes = canonical_terminal_bytes(marker)
    pointer = RunCurrentPointerV2(
        publication_kind="product_terminal",
        run_id=manifest.run_id,
        revision=manifest.revision,
        transaction_id=manifest.transaction_id,
        revision_relative_path=f"revisions/r1-{manifest.transaction_id}",
        status="succeeded",
        manifest_sha256=manifest_sha256(manifest_bytes),
        inventory_sha256=manifest.inventory_sha256,
        completion_marker_sha256=completion_marker_sha256(marker_bytes),
        published_at=NOW,
    )
    pointer_bytes = canonical_terminal_bytes(pointer)

    assert parse_run_manifest(manifest_bytes) == manifest
    assert parse_completion_marker(marker_bytes) == marker
    assert parse_current_pointer(pointer_bytes) == pointer
    assert current_pointer_sha256(pointer_bytes) == current_pointer_sha256(pointer)
    assert b'"marker_sha256"' not in marker_bytes
    assert b'"pointer_sha256"' not in pointer_bytes


@pytest.mark.parametrize(
    ("publication_kind", "status"),
    [
        ("product_checkpoint", "succeeded"),
        ("product_terminal", "in_progress"),
        ("legacy_copy", "succeeded"),
    ],
)
def test_manifest_rejects_forbidden_publication_status_pairs(
    publication_kind: str,
    status: str,
) -> None:
    with pytest.raises(ValidationError, match="publication/status"):
        _manifest(publication_kind=publication_kind, status=status)


def test_future_version_is_distinct_from_corrupt_supported_version() -> None:
    future = canonical_terminal_bytes(
        {
            "schema_version": "9.0.0",
            "storage_protocol": "poker-run-terminal-v2",
        }
    )
    with pytest.raises(UnsupportedTerminalVersion):
        parse_current_pointer(future)

    corrupt = canonical_terminal_bytes(
        {
            "schema_version": "2.0.0",
            "storage_protocol": "poker-run-terminal-v2",
        }
    )
    with pytest.raises(Exception) as captured:
        parse_current_pointer(corrupt)
    assert not isinstance(captured.value, UnsupportedTerminalVersion)


def test_failure_contract_is_redacted_and_only_lock_contention_is_retryable() -> None:
    failure = ProductRunFailureV2(
        code=ProductRunFailureCode.RUN_LOCKED,
        stage="lock_acquire",
        message_code="run_locked",
        retryable=True,
        reconciliation_required=False,
        filesystem_effect="none",
        domain_effect="current_unchanged",
        previous_revision_effect="not_applicable",
        run_id_sha256=SHA,
    )
    assert failure.retryable is True
    assert failure.automatic_retry_allowed is False
    assert "path" not in failure.model_dump(mode="json")

    with pytest.raises(ValidationError, match="only run_locked"):
        ProductRunFailureV2(
            **{
                **failure.model_dump(mode="python"),
                "code": ProductRunFailureCode.RUN_CORRUPT,
                "message_code": "run_corrupt",
                "read_status": RunReadStatus.CORRUPT,
            }
        )
