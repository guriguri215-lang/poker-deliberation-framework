from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    artifact_table_entry,
    build_inventory,
    canonical_json_bytes,
    canonicalize_bindings,
    classification_evidence_sha256,
    domain_sha256,
    parse_canonical_json,
    validate_canonical_text,
    validate_logical_name,
    validate_run_id,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    DurabilityEvidenceV1,
    LocalDataBindingV1,
    LockMetadataV1,
    PayloadInventoryEntryV1,
    RevisionArtifactV1,
    RevisionPublishOutcomeV1,
    RevisionPublishRequestV1,
    RevisionTransactionDescriptorV1,
    SourceBindingV1,
    StorageRevisionManifestV1,
    StorageRevisionPointerV1,
    ToolBindingV1,
)
from poker_deliberation.storage.terminal_canonical import _validate_json_value

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def test_final_report_artifact_table_dispatch_preserves_v1_and_admits_only_v2() -> None:
    v1 = (
        "application/json",
        "poker-run-storage-json-v1",
        "poker-final-report-artifact-v1",
        "final_report_json",
    )
    v2 = (
        "application/json",
        "poker-run-storage-json-v1",
        "poker-final-report-artifact-v2",
        "final_report_json",
    )

    assert artifact_table_entry("final_report.json") == v1
    assert artifact_table_entry("final_report.json", v1[2]) == v1
    assert artifact_table_entry("final_report.json", v2[2]) == v2
    with pytest.raises(CanonicalStorageError, match="unknown final-report"):
        artifact_table_entry("final_report.json", "poker-final-report-artifact-v3")
    with pytest.raises(CanonicalStorageError, match="schema version mismatch"):
        artifact_table_entry("input.json", v2[2])


def _evidence() -> ClassificationEvidence:
    return ClassificationEvidence(
        source_classifications=(ContextClassification.PUBLIC,),
        restricted_secret_check_completed=True,
    )


def _local_binding() -> LocalDataBindingV1:
    evidence = _evidence()
    return LocalDataBindingV1(
        logical_name="input.json",
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
    )


def test_control_models_are_strict_frozen_extra_forbid_and_nonterminal() -> None:
    request = RevisionPublishRequestV1(
        run_id="Run-1",
        transaction_id="txn-" + "1" * 32,
        proposed_revision=1,
        expected_revision=None,
        expected_manifest_sha256=None,
        expected_pointer_sha256=None,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(),
    )

    with pytest.raises(ValidationError):
        request.run_id = "changed"
    with pytest.raises(ValidationError):
        RevisionPublishRequestV1.model_validate(
            {**request.model_dump(mode="python"), "terminal": True},
            strict=True,
        )
    with pytest.raises(ValidationError):
        RevisionPublishRequestV1.model_validate(
            {**request.model_dump(mode="python"), "schema_version": "2.0.0"},
            strict=True,
        )
    with pytest.raises(ValidationError):
        RevisionPublishRequestV1(
            **{
                **request.model_dump(mode="python"),
                "created_at": NOW.astimezone(timezone(timedelta(hours=9))),
            }
        )


def test_revision_sequence_and_closed_outcome_matrix_fail_closed() -> None:
    base = {
        "run_id": "Run-1",
        "transaction_id": "txn-" + "1" * 32,
        "proposed_revision": 2,
        "expected_revision": 1,
        "expected_manifest_sha256": "a" * 64,
        "expected_pointer_sha256": "b" * 64,
        "created_at": NOW,
        "producer_id": "poker-deliberation",
        "producer_version": "0.1.0",
        "artifacts": (),
    }
    RevisionPublishRequestV1(**base)
    with pytest.raises(ValidationError):
        RevisionPublishRequestV1(**{**base, "proposed_revision": 3})
    with pytest.raises(ValidationError):
        RevisionPublishRequestV1(**{**base, "expected_manifest_sha256": None})

    durability = DurabilityEvidenceV1(
        platform_adapter="windows_msvcrt",
        file_sync="confirmed",
        directory_sync="unavailable",
        pointer_replace="confirmed",
        reconciliation="confirmed",
    )
    RevisionPublishOutcomeV1(
        outcome_kind="published",
        run_id_sha256="c" * 64,
        transaction_id="txn-" + "1" * 32,
        transaction_sha256="d" * 64,
        revision=1,
        observed_current_revision=1,
        manifest_sha256="e" * 64,
        pointer_sha256="f" * 64,
        filesystem_effect="current_advanced",
        domain_effect="current_advanced",
        previous_revision_effect="not_applicable",
        durability_evidence=durability,
    )
    with pytest.raises(ValidationError):
        RevisionPublishOutcomeV1(
            outcome_kind="published",
            run_id_sha256="c" * 64,
            transaction_id="txn-" + "1" * 32,
            transaction_sha256="d" * 64,
            revision=1,
            observed_current_revision=1,
            manifest_sha256="e" * 64,
            pointer_sha256="f" * 64,
            filesystem_effect="none",
            domain_effect="current_advanced",
            previous_revision_effect="not_applicable",
            durability_evidence=durability,
        )


def test_canonical_json_rejects_duplicate_keys_nonnfc_and_noncanonical_time() -> None:
    assert canonical_json_bytes({"é": 1, "a": 2}) == b'{"a":2,"\xc3\xa9":1}'
    with pytest.raises(CanonicalStorageError, match="duplicate"):
        parse_canonical_json(b'{"a":1,"a":2}')
    with pytest.raises(CanonicalStorageError, match="NFC"):
        canonical_json_bytes({"e\u0301": 1})
    with pytest.raises(CanonicalStorageError, match="canonical"):
        parse_canonical_json(b'{"b":2, "a":1}')

    artifact = RevisionArtifactV1(
        logical_name="input.json",
        media_type="application/json",
        artifact_schema_version="poker-case-input-artifact-v1",
        serialization="poker-run-storage-json-v1",
        exact_bytes=b"{}",
        required=True,
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=_evidence(),
        policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
        origin_kind="case_input",
        provenance_bindings=(_local_binding(),),
    )
    assert artifact.exact_bytes == b"{}"


def test_canonical_json_maps_excessive_nesting_to_stable_error() -> None:
    nested: object = None
    for _ in range(5_000):
        nested = [nested]

    with pytest.raises(CanonicalStorageError):
        canonical_json_bytes({"output": nested})

    encoded = b'{"output":' + (b"[" * 5_000) + b"null" + (b"]" * 5_000) + b"}"
    with pytest.raises(CanonicalStorageError):
        parse_canonical_json(encoded)
    with pytest.raises(CanonicalStorageError):
        _validate_json_value("tool_results/tool-result-depth.json", encoded)


@pytest.mark.parametrize(
    "value",
    [
        "../run",
        "CON",
        "run.",
        "run ",
        "e\u0301",
        "C:run",
        "a/b",
        "sk-abcdefghijk",
    ],
)
def test_run_id_rejects_nonportable_or_alias_prone_values(value: str) -> None:
    with pytest.raises(CanonicalStorageError):
        validate_run_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "../input.json",
        "/input.json",
        "C:/input.json",
        "tool_results\\x.json",
        "tool_results/x:stream.json",
        "tool_results/CON.json",
        "agent_reports/sk-abcdefghijk.json",
    ],
)
def test_logical_paths_reject_traversal_ads_devices_and_slash_mismatch(value: str) -> None:
    with pytest.raises(CanonicalStorageError):
        validate_logical_name(value)


def test_utf8_text_preserves_optional_final_lf_but_rejects_bom_and_crlf() -> None:
    assert validate_canonical_text("日本語".encode()) == "日本語"
    assert validate_canonical_text("日本語\n".encode()) == "日本語\n"
    with pytest.raises(CanonicalStorageError):
        validate_canonical_text(b"\xef\xbb\xbftext")
    with pytest.raises(CanonicalStorageError):
        validate_canonical_text(b"text\r\n")


def test_exact_control_schema_field_sets_remain_frozen() -> None:
    assert set(StorageRevisionManifestV1.model_fields) == {
        "schema_version",
        "storage_protocol",
        "canonicalization",
        "hash_algorithm",
        "publication_kind",
        "run_id",
        "revision",
        "transaction_id",
        "transaction_sha256",
        "previous_revision",
        "previous_manifest_sha256",
        "expected_pointer_sha256",
        "created_at",
        "producer_id",
        "producer_version",
        "inventory_sha256",
        "provenance_heads",
        "artifacts",
    }
    assert set(StorageRevisionPointerV1.model_fields) == {
        "schema_version",
        "storage_protocol",
        "canonicalization",
        "hash_algorithm",
        "publication_kind",
        "run_id",
        "revision",
        "transaction_id",
        "revision_relative_path",
        "transaction_sha256",
        "manifest_sha256",
        "inventory_sha256",
        "published_at",
    }
    assert set(RevisionTransactionDescriptorV1.model_fields) == {
        "schema_version",
        "storage_protocol",
        "canonicalization",
        "hash_algorithm",
        "run_id",
        "transaction_id",
        "proposed_revision",
        "expected_revision",
        "expected_manifest_sha256",
        "expected_pointer_sha256",
        "created_at",
        "producer_id",
        "producer_version",
        "artifact_plan",
        "provenance_heads",
        "transaction_sha256",
    }
    assert set(LockMetadataV1.model_fields) == {
        "schema_version",
        "storage_protocol",
        "run_id_sha256",
        "ownership_marker_sha256",
        "authority_identity_sha256",
        "owner_token",
        "process_id",
        "adapter",
        "transaction_id",
        "expected_revision",
        "acquired_at",
    }
    assert "manifest_sha256" not in StorageRevisionManifestV1.model_fields
    assert "pointer_sha256" not in StorageRevisionPointerV1.model_fields


def test_artifact_byte_limit_accepts_equality_and_rejects_one_over() -> None:
    data = canonical_json_bytes(
        CaseInput(
            case_id="case-budget",
            kind="claim",
            raw_text="exact byte equality",
        )
    )
    evidence = _evidence()
    artifact = RevisionArtifactV1(
        logical_name="input.json",
        media_type="application/json",
        artifact_schema_version="poker-case-input-artifact-v1",
        serialization="poker-run-storage-json-v1",
        exact_bytes=data,
        required=True,
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
        origin_kind="case_input",
        provenance_bindings=(
            _local_binding(),
            SourceBindingV1(
                source_id="user-input",
                source_kind="user_input",
                trust_kind="trusted_user_input",
                source_sha256=domain_sha256("poker-user-input-source-v1", data),
            ),
        ),
    )
    request = RevisionPublishRequestV1(
        run_id="Run-budget",
        transaction_id="txn-" + "a" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(artifact,),
    )
    with pytest.raises(CanonicalStorageError, match="exceeds"):
        build_inventory(request, max_artifact_bytes=len(data) - 1)
    inventory, _heads, _parsed = build_inventory(
        request,
        max_artifact_bytes=len(data),
    )
    assert isinstance(inventory[0], PayloadInventoryEntryV1)
    assert inventory[0].size_bytes == len(data)
    isolated_producer_request = request.model_copy(
        update={
            "producer_id": "p2-028a-isolated-job-control",
            "producer_version": "1.0.0",
        }
    )
    with pytest.raises(CanonicalStorageError, match="complete artifact set"):
        build_inventory(
            isolated_producer_request,
            max_artifact_bytes=len(data),
        )


def _tool_binding(ordinal: int, result_id: str) -> ToolBindingV1:
    return ToolBindingV1(
        run_id="Run-tool-order",
        phase_attempt_id="phase-attempt",
        ordinal=ordinal,
        request_id=f"request-{result_id}",
        request_tool_name="pot-odds",
        requested_by="phase",
        requires_approval=False,
        requested_contract_version="1.0.0",
        tool_request_sha256="1" * 64,
        request_input_artifact_sha256="2" * 64,
        result_id=result_id,
        result_tool_name="pot-odds",
        result_artifact_sha256="3" * 64,
        request_input_sha256="4" * 64,
        validated_result_input_sha256="4" * 64,
        materialized_result_input_sha256="4" * 64,
        supported_contract_version="1.0.0",
        result_contract_version="1.0.0",
    )


def test_provenance_order_keeps_tool_ordinal_as_a_typed_integer() -> None:
    ordered = canonicalize_bindings(
        (
            _tool_binding(10, "result-ten"),
            _tool_binding(2, "result-two"),
        )
    )
    assert [binding.ordinal for binding in ordered if isinstance(binding, ToolBindingV1)] == [2, 10]


def test_source_control_metadata_rejects_nonpayload_paths_and_secret_shapes() -> None:
    with pytest.raises(ValidationError):
        SourceBindingV1(
            source_id="external",
            source_kind="external_evidence",
            trust_kind="declared_external_evidence",
            source_logical_name="C:/private/secret",
            source_sha256="a" * 64,
        )
    with pytest.raises(ValidationError):
        SourceBindingV1(
            source_id="sk-abcdefghijk",
            source_kind="external_evidence",
            trust_kind="declared_external_evidence",
            source_sha256="a" * 64,
        )
