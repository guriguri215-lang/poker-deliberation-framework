from __future__ import annotations

from datetime import UTC, datetime
from itertools import permutations
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from hypothesis import given
from hypothesis import strategies as st

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.phases.contracts import canonical_sha256 as phase_canonical_sha256
from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionRecord,
    AgentExecutionStatus,
    AgentReport,
    ApprovalRequest,
    ApprovalStatus,
    CaseInput,
    EvidenceRecord,
    Exactness,
    FinalReport,
    NumericalExactness,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    build_inventory,
    canonical_domain_sha256,
    canonical_json_bytes,
    classification_evidence_sha256,
    domain_sha256,
    inventory_sha256,
    payload_order_key,
    payload_source_id,
    run_lock_key_sha256,
    sha256_bytes,
    upstream_source_sha256,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    ApprovalDecisionBindingV1,
    ArtifactIntentSnapshotV1,
    BudgetPolicyBindingV1,
    ContextBindingV1,
    LocalDataBindingV1,
    PhaseBindingV1,
    ProvenanceBindingV1,
    ReportBindingV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    SourceBindingV1,
    ToolBindingV1,
)
from poker_deliberation.storage.revision_store import RunRevisionStore

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


def _payload_source(
    logical_name: str,
    schema_version: str,
    data: bytes,
) -> SourceBindingV1:
    return SourceBindingV1(
        source_id=payload_source_id(logical_name),
        source_kind="payload_artifact",
        trust_kind="verified_payload",
        source_logical_name=logical_name,
        source_schema_version=schema_version,
        source_sha256=sha256_bytes(data),
    )


def test_artifact_order_permutations_have_identical_inventory_heads_and_manifest() -> None:
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

    temp_parent = Path(__file__).resolve().parents[2] / "tmp"
    temp_parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2p-", dir=temp_parent) as directory:
        root = Path(directory)
        legacy = root / "legacy"
        legacy.mkdir()
        store = RunRevisionStore(root / "revision", legacy)
        observed: set[tuple[str, str, bytes]] = set()
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
            inventory, heads, _parsed = build_inventory(
                request,
                max_artifact_bytes=1_000_000,
            )
            observed.add(
                (
                    inventory_sha256(inventory),
                    canonical_json_bytes(heads).decode(),
                    store._preflight(request).manifest_bytes,
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


def test_tool_results_precede_reports_and_unlisted_external_sources_fail_closed() -> None:
    assert payload_order_key("tool_results/result-1.input.json") < payload_order_key(
        "tool_results/result-1.json"
    )
    assert payload_order_key("tool_results/result-1.json") < payload_order_key(
        "agent_reports/report-1.json"
    )

    case = CaseInput(case_id="case-source-graph", kind="claim", raw_text="source graph")
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
    normalized = _artifact(
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
            SourceBindingV1(
                source_id="unlisted-external",
                source_kind="external_evidence",
                trust_kind="declared_external_evidence",
                consumer_record_id="record-without-ledger",
                source_sha256="f" * 64,
            ),
        ),
    )
    request = RevisionPublishRequestV1(
        run_id="Run-source-graph",
        transaction_id="txn-" + "a" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(normalized, input_artifact),
    )
    with pytest.raises(CanonicalStorageError, match="external-evidence"):
        build_inventory(request, max_artifact_bytes=1_000_000)


def test_payload_source_future_edge_is_rejected_before_exact_graph_acceptance() -> None:
    case = CaseInput(case_id="case-future-edge", kind="claim", raw_text="future")
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
    security_bytes = b"[]"
    security = _artifact(
        "security_events.json",
        data=security_bytes,
        schema="poker-security-event-list-artifact-v1",
        origin="security_event_ledger",
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
    assumptions = _artifact(
        "assumptions.json",
        data=b"[]",
        schema="poker-assumption-list-artifact-v1",
        origin="assumption_ledger",
        sources=(
            SourceBindingV1(
                source_id=payload_source_id("security_events.json"),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name="security_events.json",
                source_schema_version="poker-security-event-list-artifact-v1",
                source_sha256=sha256_bytes(security_bytes),
            ),
        ),
    )
    request = RevisionPublishRequestV1(
        run_id="Run-future-edge",
        transaction_id="txn-" + "b" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(security, assumptions, input_artifact),
    )
    with pytest.raises(CanonicalStorageError, match="precede"):
        build_inventory(request, max_artifact_bytes=1_000_000)


def test_tool_binding_replay_rejects_nested_result_input_tamper() -> None:
    run_id = "Run-tool-replay"
    case = CaseInput(case_id="case-tool-replay", kind="claim", raw_text="tool")
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
    tool_input_bytes = b"{}"
    tool_request = ToolRequest(
        request_id="request-tool-replay",
        tool_name="solver_status",
        input={},
        requested_by="phase",
        requires_approval=False,
        contract_version="2.0.0",
    )
    result = ToolResult(
        result_id="result-tool-replay",
        tool_name="solver_status",
        input={},
        output={"available": False},
        status=ToolStatus.SUCCESS,
        exactness=Exactness.EXACT,
        numeric_exactness=NumericalExactness.EXACT,
        contract_version="2.0.0",
        duration_seconds=0.0,
        created_at=NOW,
    )
    result_bytes = canonical_json_bytes(result)
    phase = PhaseBindingV1(
        run_id=run_id,
        phase_id="tool_research",
        phase_schema_version="1.0.0",
        attempt_id="phase-tool-replay",
        input_hash="1" * 64,
        policy_snapshot_hash="2" * 64,
    )
    input_sha = phase_canonical_sha256({})
    tool = ToolBindingV1(
        run_id=run_id,
        phase_attempt_id=phase.attempt_id,
        ordinal=0,
        request_id=tool_request.request_id,
        request_tool_name=tool_request.tool_name,
        requested_by=tool_request.requested_by,
        requires_approval=tool_request.requires_approval,
        requested_contract_version=tool_request.contract_version,
        tool_request_sha256=phase_canonical_sha256(tool_request),
        request_input_artifact_sha256=sha256_bytes(tool_input_bytes),
        result_id=result.result_id,
        result_tool_name=result.tool_name,
        result_artifact_sha256=sha256_bytes(result_bytes),
        request_input_sha256=input_sha,
        validated_result_input_sha256=input_sha,
        materialized_result_input_sha256=input_sha,
        supported_contract_version="2.0.0",
        result_contract_version=result.contract_version,
    )
    tool_input = _artifact(
        "tool_results/result-tool-replay.input.json",
        data=tool_input_bytes,
        schema="poker-tool-input-artifact-v1",
        origin="tool_input",
        sources=(
            SourceBindingV1(
                source_id=payload_source_id("input.json"),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name="input.json",
                source_schema_version="poker-case-input-artifact-v1",
                source_sha256=sha256_bytes(case_bytes),
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
        extra_bindings=(phase, tool),
    )
    tool_result = _artifact(
        "tool_results/result-tool-replay.json",
        data=result_bytes,
        schema="poker-tool-result-artifact-v1",
        origin="tool_result",
        sources=(
            SourceBindingV1(
                source_id=payload_source_id(tool_input.logical_name),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name=tool_input.logical_name,
                source_schema_version=tool_input.artifact_schema_version,
                source_sha256=sha256_bytes(tool_input_bytes),
            ),
        ),
        extra_bindings=(phase, tool),
    )
    assignments_bytes = canonical_json_bytes(
        (
            AgentAssignment(
                assignment_id="assignment-tool-replay",
                agent_role="reviewer",
                task="use tool result",
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
        context_sha256="3" * 64,
        context_id="context-tool-replay",
        attempt_id="attempt-tool-replay",
        schema_version="1.0.0",
        classification=ContextClassification.INTERNAL,
        payload_sha256="4" * 64,
        source_sha256="5" * 64,
        policy_sha256="6" * 64,
        envelope_sha256="7" * 64,
        expires_at=NOW,
        producer_runtime="producer",
        consumer_runtime="consumer",
    )
    report = AgentReport(
        report_id="report-tool-replay",
        agent_role="reviewer",
        task="use tool result",
        tool_result_ids=(result.result_id,),
    )
    report_artifact = _artifact(
        "agent_reports/report-tool-replay.json",
        data=canonical_json_bytes(report),
        schema="poker-agent-report-artifact-v1",
        origin="agent_report",
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
            SourceBindingV1(
                source_id=payload_source_id(tool_result.logical_name),
                source_kind="payload_artifact",
                trust_kind="verified_payload",
                source_logical_name=tool_result.logical_name,
                source_schema_version=tool_result.artifact_schema_version,
                source_sha256=sha256_bytes(result_bytes),
            ),
        ),
        extra_bindings=(context, phase),
    )
    request = RevisionPublishRequestV1(
        run_id=run_id,
        transaction_id="txn-" + "c" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(
            report_artifact,
            tool_result,
            assignments,
            normalized,
            tool_input,
            input_artifact,
        ),
    )
    build_inventory(request, max_artifact_bytes=1_000_000)

    tampered = result.model_copy(update={"input": {"nested": {"value": 1}}})
    tampered_artifact = tool_result.model_copy(
        update={"exact_bytes": canonical_json_bytes(tampered)}
    )
    with pytest.raises(CanonicalStorageError, match="binding correlation mismatch"):
        build_inventory(
            request.model_copy(
                update={"artifacts": (input_artifact, normalized, tool_input, tampered_artifact)}
            ),
            max_artifact_bytes=1_000_000,
        )


def test_decided_approval_requires_exact_external_decision_provenance() -> None:
    case = CaseInput(case_id="case-approval", kind="claim", raw_text="approval")
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
    approval = ApprovalRequest(
        approval_id="approval-storage",
        requested_action="external action",
        reason="needed",
        expected_benefit="evidence",
        risks=["external effect"],
        cost_or_resource_estimate="bounded",
        alternatives=["decline"],
        effect_of_declining="no action",
        status=ApprovalStatus.APPROVED,
        decision_reason="explicit decision",
        created_at=NOW,
        decided_at=NOW,
    )
    approvals = _artifact(
        "approvals.json",
        data=canonical_json_bytes((approval,)),
        schema="poker-approval-request-list-artifact-v1",
        origin="approval_ledger",
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
    request = RevisionPublishRequestV1(
        run_id="Run-approval",
        transaction_id="txn-" + "d" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(input_artifact, approvals),
    )
    with pytest.raises(CanonicalStorageError, match="decision provenance"):
        build_inventory(request, max_artifact_bytes=1_000_000)

    external_source_id = f"approval-decision-{approval.approval_id}"
    decision = ApprovalDecisionBindingV1(
        approval_id=approval.approval_id,
        decision="approved",
        decided_at=NOW,
        decision_reason_sha256=domain_sha256(
            "poker-approval-decision-reason-v1",
            approval.decision_reason.encode("utf-8"),
        ),
        external_source_id=external_source_id,
    )
    decision_source = SourceBindingV1(
        source_id=external_source_id,
        source_kind="external_evidence",
        trust_kind="declared_external_evidence",
        source_sha256=canonical_domain_sha256(
            "poker-approval-decision-evidence-v1",
            {
                "approval_id": approval.approval_id,
                "status": approval.status,
                "decision_reason": approval.decision_reason,
                "decided_at": approval.decided_at,
            },
        ),
    )
    valid_approvals = approvals.model_copy(
        update={
            "provenance_bindings": (
                *approvals.provenance_bindings,
                decision,
                decision_source,
            )
        }
    )
    build_inventory(
        request.model_copy(update={"artifacts": (input_artifact, valid_approvals)}),
        max_artifact_bytes=1_000_000,
    )

    wrong_consumer = decision_source.model_copy(update={"consumer_record_id": approval.approval_id})
    invalid_approvals = valid_approvals.model_copy(
        update={
            "provenance_bindings": (
                *approvals.provenance_bindings,
                decision,
                wrong_consumer,
            )
        }
    )
    with pytest.raises(CanonicalStorageError, match="source evidence mismatch"):
        build_inventory(
            request.model_copy(update={"artifacts": (input_artifact, invalid_approvals)}),
            max_artifact_bytes=1_000_000,
        )


def test_evidence_source_correlation_and_binding_kind_matrix_are_exact() -> None:
    case = CaseInput(case_id="case-evidence", kind="claim", raw_text="evidence")
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
                source_sha256=domain_sha256(
                    "poker-user-input-source-v1",
                    case_bytes,
                ),
            ),
        ),
    )
    record = EvidenceRecord(
        evidence_id="evidence-storage",
        source_title="Storage evidence",
        organization_or_author="Repository",
        source_type="repository",
        identifier="revision-storage-test",
        accessed_date="2026-07-24",
        summary="Typed provenance fixture",
        source_tier=1,
    )
    evidence_bytes = canonical_json_bytes(record) + b"\n"
    payload_source = SourceBindingV1(
        source_id=payload_source_id("input.json"),
        source_kind="payload_artifact",
        trust_kind="verified_payload",
        source_logical_name="input.json",
        source_schema_version="poker-case-input-artifact-v1",
        source_sha256=sha256_bytes(case_bytes),
    )
    evidence_source = SourceBindingV1(
        source_id=record.evidence_id,
        source_kind="external_evidence",
        trust_kind="declared_external_evidence",
        consumer_record_id=record.evidence_id,
        source_sha256=canonical_domain_sha256(
            "poker-evidence-record-source-v1",
            record,
        ),
    )
    evidence = _artifact(
        "evidence.jsonl",
        data=evidence_bytes,
        schema="poker-evidence-record-jsonl-artifact-v1",
        origin="evidence_ledger",
        sources=(payload_source, evidence_source),
    ).model_copy(
        update={
            "media_type": "application/x-ndjson",
            "serialization": "poker-run-storage-jsonl-v1",
        }
    )
    request = RevisionPublishRequestV1(
        run_id="Run-evidence",
        transaction_id="txn-" + "e" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(input_artifact, evidence),
    )
    build_inventory(request, max_artifact_bytes=1_000_000)

    wrong_hash = evidence_source.model_copy(update={"source_sha256": "f" * 64})
    invalid_evidence = evidence.model_copy(
        update={
            "provenance_bindings": (
                evidence.provenance_bindings[0],
                payload_source,
                wrong_hash,
            )
        }
    )
    with pytest.raises(CanonicalStorageError, match="source correlation mismatch"):
        build_inventory(
            request.model_copy(update={"artifacts": (input_artifact, invalid_evidence)}),
            max_artifact_bytes=1_000_000,
        )

    wrong_consumer = evidence_source.model_copy(update={"consumer_record_id": "different-evidence"})
    wrong_consumer_evidence = evidence.model_copy(
        update={
            "provenance_bindings": (
                evidence.provenance_bindings[0],
                payload_source,
                wrong_consumer,
            )
        }
    )
    with pytest.raises(CanonicalStorageError, match="source correlation mismatch"):
        build_inventory(
            request.model_copy(update={"artifacts": (input_artifact, wrong_consumer_evidence)}),
            max_artifact_bytes=1_000_000,
        )

    extra_budget = BudgetPolicyBindingV1(
        policy_schema_version="2.0.0",
        policy_sha256="a" * 64,
    )
    invalid_input = input_artifact.model_copy(
        update={
            "provenance_bindings": (
                *input_artifact.provenance_bindings,
                extra_budget,
            )
        }
    )
    with pytest.raises(CanonicalStorageError, match="outside its contract"):
        build_inventory(
            request.model_copy(update={"artifacts": (invalid_input, evidence)}),
            max_artifact_bytes=1_000_000,
        )

    conflicting_source = input_artifact.provenance_bindings[-1].model_copy(
        update={"source_sha256": "b" * 64}
    )
    duplicate_input = input_artifact.model_copy(
        update={
            "provenance_bindings": (
                *input_artifact.provenance_bindings,
                conflicting_source,
            )
        }
    )
    with pytest.raises(CanonicalStorageError, match="conflicting provenance"):
        build_inventory(
            request.model_copy(update={"artifacts": (duplicate_input, evidence)}),
            max_artifact_bytes=1_000_000,
        )


def test_final_report_json_and_markdown_bindings_replay_exactly() -> None:
    run_id = "Run-final-report"
    case = CaseInput(case_id="case-final-report", kind="claim", raw_text="final")
    case_bytes = canonical_json_bytes(case)
    empty_json = b"[]"
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
                source_sha256=domain_sha256(
                    "poker-user-input-source-v1",
                    case_bytes,
                ),
            ),
        ),
    )
    normalized = _artifact(
        "normalized_case.json",
        data=case_bytes,
        schema="poker-normalized-case-artifact-v1",
        origin="normalization_output",
        sources=(
            _payload_source(
                "input.json",
                "poker-case-input-artifact-v1",
                case_bytes,
            ),
        ),
    )
    assumptions = _artifact(
        "assumptions.json",
        data=empty_json,
        schema="poker-assumption-list-artifact-v1",
        origin="assumption_ledger",
        sources=(
            _payload_source(
                "input.json",
                "poker-case-input-artifact-v1",
                case_bytes,
            ),
        ),
    )
    evidence = _artifact(
        "evidence.jsonl",
        data=b"",
        schema="poker-evidence-record-jsonl-artifact-v1",
        origin="evidence_ledger",
        sources=(
            _payload_source(
                "input.json",
                "poker-case-input-artifact-v1",
                case_bytes,
            ),
        ),
    ).model_copy(
        update={
            "media_type": "application/x-ndjson",
            "serialization": "poker-run-storage-jsonl-v1",
        }
    )
    approvals = _artifact(
        "approvals.json",
        data=empty_json,
        schema="poker-approval-request-list-artifact-v1",
        origin="approval_ledger",
        sources=(
            _payload_source(
                "input.json",
                "poker-case-input-artifact-v1",
                case_bytes,
            ),
        ),
    )
    assignments = _artifact(
        "assignments.json",
        data=empty_json,
        schema="poker-agent-assignment-list-artifact-v1",
        origin="assignment_ledger",
        sources=(
            _payload_source(
                "normalized_case.json",
                "poker-normalized-case-artifact-v1",
                case_bytes,
            ),
        ),
    )
    execution_records = _artifact(
        "agent_execution_records.json",
        data=empty_json,
        schema="poker-agent-execution-record-list-artifact-v1",
        origin="agent_execution_ledger",
        sources=(
            _payload_source(
                "assignments.json",
                "poker-agent-assignment-list-artifact-v1",
                empty_json,
            ),
            _payload_source(
                "normalized_case.json",
                "poker-normalized-case-artifact-v1",
                case_bytes,
            ),
        ),
    )
    security_events = _artifact(
        "security_events.json",
        data=empty_json,
        schema="poker-security-event-list-artifact-v1",
        origin="security_event_ledger",
        sources=(
            _payload_source(
                "input.json",
                "poker-case-input-artifact-v1",
                case_bytes,
            ),
        ),
    )
    disputes = _artifact(
        "disputes.json",
        data=empty_json,
        schema="poker-dispute-list-artifact-v1",
        origin="dispute_ledger",
        sources=(),
    )
    context = ContextBindingV1(
        context_sha256="1" * 64,
        context_id="context-final",
        attempt_id="attempt-final",
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
    phase = PhaseBindingV1(
        run_id=run_id,
        phase_id="synthesis",
        phase_schema_version="1.0.0",
        attempt_id="phase-final",
        context_ids=(context.context_id,),
        input_hash="6" * 64,
        policy_snapshot_hash="7" * 64,
        output_hash="8" * 64,
    )
    budget = BudgetPolicyBindingV1(
        policy_schema_version="2.0.0",
        policy_sha256="9" * 64,
    )
    final_report = FinalReport(
        run_id=run_id,
        conclusion="Exact final report correlation",
        generated_at=NOW,
    )
    final_json_bytes = canonical_json_bytes(final_report)
    base_artifacts = (
        input_artifact,
        normalized,
        assumptions,
        evidence,
        approvals,
        assignments,
        execution_records,
        security_events,
        disputes,
    )
    schemas_and_bytes = {
        artifact.logical_name: (
            artifact.artifact_schema_version,
            artifact.exact_bytes,
        )
        for artifact in base_artifacts
    }
    final_sources = tuple(
        _payload_source(logical_name, *schemas_and_bytes[logical_name])
        for logical_name in (
            "input.json",
            "normalized_case.json",
            "assumptions.json",
            "evidence.jsonl",
            "approvals.json",
            "assignments.json",
            "agent_execution_records.json",
            "security_events.json",
            "disputes.json",
        )
    )
    final_json = _artifact(
        "final_report.json",
        data=final_json_bytes,
        schema="poker-final-report-artifact-v1",
        origin="final_report_json",
        sources=final_sources,
        extra_bindings=(context, phase, budget),
    )
    markdown_bytes = render_markdown(final_report).encode("utf-8")
    final_json_source = _payload_source(
        "final_report.json",
        "poker-final-report-artifact-v1",
        final_json_bytes,
    )
    report_binding = ReportBindingV1(
        report_id=run_id,
        report_schema_version="poker-final-report-artifact-v1",
        report_json_sha256=sha256_bytes(final_json_bytes),
        rendered_markdown_sha256=sha256_bytes(markdown_bytes),
        upstream_source_sha256=upstream_source_sha256(final_sources),
    )
    markdown = _artifact(
        "final_report.md",
        data=markdown_bytes,
        schema="poker-final-report-markdown-artifact-v1",
        origin="final_report_markdown",
        sources=(final_json_source,),
        extra_bindings=(report_binding,),
    ).model_copy(
        update={
            "media_type": "text/markdown",
            "serialization": "poker-run-storage-utf8-text-v1",
        }
    )
    request = RevisionPublishRequestV1(
        run_id=run_id,
        transaction_id="txn-" + "f" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(*base_artifacts, final_json, markdown),
    )
    inventory, _heads, _parsed = build_inventory(
        request,
        max_artifact_bytes=1_000_000,
    )
    assert inventory[-2].logical_name == "final_report.json"
    assert inventory[-1].logical_name == "final_report.md"

    invalid_binding = report_binding.model_copy(update={"rendered_markdown_sha256": "0" * 64})
    invalid_markdown = markdown.model_copy(
        update={
            "provenance_bindings": (
                markdown.provenance_bindings[0],
                final_json_source,
                invalid_binding,
            )
        }
    )
    with pytest.raises(CanonicalStorageError, match="binding correlation mismatch"):
        build_inventory(
            request.model_copy(
                update={
                    "artifacts": (
                        *base_artifacts,
                        final_json,
                        invalid_markdown,
                    )
                }
            ),
            max_artifact_bytes=1_000_000,
        )

    missing_budget = final_json.model_copy(
        update={
            "provenance_bindings": tuple(
                binding
                for binding in final_json.provenance_bindings
                if not isinstance(binding, BudgetPolicyBindingV1)
            )
        }
    )
    with pytest.raises(CanonicalStorageError, match="one budget policy binding"):
        build_inventory(
            request.model_copy(
                update={
                    "artifacts": (
                        *base_artifacts,
                        missing_budget,
                        markdown,
                    )
                }
            ),
            max_artifact_bytes=1_000_000,
        )
