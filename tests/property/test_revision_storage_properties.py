from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionRecord,
    AgentExecutionStatus,
    CaseInput,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    build_inventory,
    canonical_json_bytes,
    classification_evidence_sha256,
    domain_sha256,
    inventory_sha256,
    payload_source_id,
    run_lock_key_sha256,
    sha256_bytes,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    ArtifactIntentSnapshotV1,
    ContextBindingV1,
    LocalDataBindingV1,
    PhaseBindingV1,
    ProvenanceBindingV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    SourceBindingV1,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)


def _artifact(
    logical_name: str,
    *,
    data: bytes,
    schema: str,
    origin: str,
    sources: tuple[SourceBindingV1, ...],
    extra_bindings: tuple[ProvenanceBindingV1, ...] = (),
) -> RevisionArtifactV1:
    evidence = ClassificationEvidence(
        source_classifications=(ContextClassification.PUBLIC,),
        restricted_secret_check_completed=True,
    )
    local = LocalDataBindingV1(
        logical_name=logical_name,
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
    )
    return RevisionArtifactV1(
        logical_name=logical_name,
        media_type="application/json",
        artifact_schema_version=schema,
        serialization="poker-run-storage-json-v1",
        exact_bytes=data,
        required=True,
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
        origin_kind=origin,
        provenance_bindings=(local, *sources, *extra_bindings),
    )


def test_artifact_order_permutations_have_identical_inventory_and_heads() -> None:
    case = CaseInput(case_id="case-order", kind="claim", raw_text="order invariant")
    data = canonical_json_bytes(case)
    input_artifact = _artifact(
        "input.json",
        data=data,
        schema="poker-case-input-artifact-v1",
        origin="case_input",
        sources=(
            SourceBindingV1(
                source_id="user-input",
                source_kind="user_input",
                trust_kind="trusted_user_input",
                source_sha256=domain_sha256("poker-user-input-source-v1", data),
            ),
        ),
    )
    normalized_artifact = _artifact(
        "normalized_case.json",
        data=data,
        schema="poker-normalized-case-artifact-v1",
        origin="normalization_output",
        sources=(
            SourceBindingV1(
                source_id=payload_source_id("input.json"),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name="input.json",
                source_schema_version="poker-case-input-artifact-v1",
                source_sha256=sha256_bytes(data),
            ),
        ),
    )

    observed: set[tuple[str, str]] = set()
    for order in permutations((input_artifact, normalized_artifact)):
        request = RevisionPublishRequestV1(
            run_id="Run-order",
            transaction_id="txn-" + "1" * 32,
            proposed_revision=1,
            created_at=NOW,
            producer_id="poker-deliberation",
            producer_version="0.1.0",
            artifacts=order,
        )
        inventory, heads, _parsed = build_inventory(request, max_artifact_bytes=1_000_000)
        observed.add(
            (
                inventory_sha256(inventory),
                canonical_json_bytes(heads).decode(),
            )
        )
        assert [entry.logical_name for entry in inventory] == [
            "input.json",
            "normalized_case.json",
        ]
    assert len(observed) == 1


@given(
    prefix=st.sampled_from(["Run", "run", "RUN"]),
    suffix=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789_-", min_size=1, max_size=20),
)
def test_lock_key_ascii_casefolds_all_run_id_case_aliases(prefix: str, suffix: str) -> None:
    values = {
        run_lock_key_sha256(f"{candidate}-{suffix}")
        for candidate in (prefix.lower(), prefix.upper(), prefix.title())
    }
    assert len(values) == 1


def test_execution_context_and_phase_intent_correlation_replays_exactly() -> None:
    case = CaseInput(case_id="case-provenance", kind="claim", raw_text="provenance")
    case_bytes = canonical_json_bytes(case)
    input_artifact = _artifact(
        "input.json",
        data=case_bytes,
        schema="poker-case-input-artifact-v1",
        origin="case_input",
        sources=(
            SourceBindingV1(
                source_id="user-input",
                source_kind="user_input",
                trust_kind="trusted_user_input",
                source_sha256=domain_sha256("poker-user-input-source-v1", case_bytes),
            ),
        ),
    )
    normalized = _artifact(
        "normalized_case.json",
        data=case_bytes,
        schema="poker-normalized-case-artifact-v1",
        origin="normalization_output",
        sources=(
            SourceBindingV1(
                source_id=payload_source_id("input.json"),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name="input.json",
                source_schema_version="poker-case-input-artifact-v1",
                source_sha256=sha256_bytes(case_bytes),
            ),
        ),
    )
    assignments_bytes = canonical_json_bytes(
        (
            AgentAssignment(
                assignment_id="assignment-storage",
                agent_role="reviewer",
                task="review",
            ),
        )
    )
    assignments = _artifact(
        "assignments.json",
        data=assignments_bytes,
        schema="poker-agent-assignment-list-artifact-v1",
        origin="assignment_ledger",
        sources=(
            SourceBindingV1(
                source_id=payload_source_id("normalized_case.json"),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name="normalized_case.json",
                source_schema_version="poker-normalized-case-artifact-v1",
                source_sha256=sha256_bytes(case_bytes),
            ),
        ),
    )
    context = ContextBindingV1(
        context_sha256="1" * 64,
        context_id="context-storage",
        attempt_id="attempt-storage",
        schema_version="1.0.0",
        classification=ContextClassification.INTERNAL,
        payload_sha256="2" * 64,
        source_sha256="3" * 64,
        policy_sha256="4" * 64,
        envelope_sha256="5" * 64,
        expires_at=NOW,
        producer_runtime="producer",
        consumer_runtime="consumer",
    )
    record = AgentExecutionRecord(
        execution_id="execution-storage",
        assignment_id="assignment-storage",
        agent_role="reviewer",
        provider="local",
        context_sha256=context.context_sha256,
        context_id=context.context_id,
        context_attempt_id=context.attempt_id,
        context_schema_version=context.schema_version,
        context_classification=context.classification.value,
        context_payload_sha256=context.payload_sha256,
        context_source_sha256=context.source_sha256,
        context_policy_sha256=context.policy_sha256,
        context_envelope_sha256=context.envelope_sha256,
        context_expires_at=context.expires_at,
        context_producer_runtime=context.producer_runtime,
        context_consumer_runtime=context.consumer_runtime,
        status=AgentExecutionStatus.COMPLETED,
        started_at=NOW,
        completed_at=NOW,
    )
    records_bytes = canonical_json_bytes((record,))
    phase = PhaseBindingV1(
        run_id="Run-provenance",
        phase_id="analysis",
        phase_schema_version="1.0.0",
        attempt_id="phase-attempt",
        context_ids=(context.context_id,),
        input_hash="6" * 64,
        policy_snapshot_hash="7" * 64,
        output_hash="8" * 64,
        artifact_intents=(
            ArtifactIntentSnapshotV1(
                kind="agent_execution_records",
                relative_path="agent_execution_records.json",
                media_type="application/json",
                content_sha256=sha256_bytes(records_bytes),
            ),
        ),
    )
    records = _artifact(
        "agent_execution_records.json",
        data=records_bytes,
        schema="poker-agent-execution-record-list-artifact-v1",
        origin="agent_execution_ledger",
        sources=(
            SourceBindingV1(
                source_id=payload_source_id("assignments.json"),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name="assignments.json",
                source_schema_version="poker-agent-assignment-list-artifact-v1",
                source_sha256=sha256_bytes(assignments_bytes),
            ),
            SourceBindingV1(
                source_id=payload_source_id("normalized_case.json"),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name="normalized_case.json",
                source_schema_version="poker-normalized-case-artifact-v1",
                source_sha256=sha256_bytes(case_bytes),
            ),
        ),
        extra_bindings=(context, phase),
    )

    request = RevisionPublishRequestV1(
        run_id="Run-provenance",
        transaction_id="txn-" + "9" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(input_artifact, normalized, assignments, records),
    )
    inventory, _heads, _parsed = build_inventory(
        request,
        max_artifact_bytes=1_000_000,
    )
    assert inventory[-1].logical_name == "agent_execution_records.json"

    wrong_context = context.model_copy(update={"payload_sha256": "f" * 64})
    invalid_records = records.model_copy(
        update={"provenance_bindings": (*records.provenance_bindings[:-2], wrong_context, phase)}
    )
    with pytest.raises(CanonicalStorageError, match="context correlation mismatch"):
        build_inventory(
            request.model_copy(
                update={"artifacts": (input_artifact, normalized, assignments, invalid_records)}
            ),
            max_artifact_bytes=1_000_000,
        )
