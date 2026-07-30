from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.phases.contracts import canonical_sha256 as phase_canonical_sha256
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionRecord,
    AgentExecutionStatus,
    CaseInput,
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
    canonical_json_bytes,
    classification_evidence_sha256,
    domain_sha256,
    parse_canonical_json,
    parse_canonical_model,
    payload_source_id,
    run_id_sha256,
    run_lock_key_sha256,
    sha256_bytes,
    transaction_sha256,
    validate_assignment_execution_correlation,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    BudgetPolicyBindingV1,
    ContextBindingV1,
    LocalDataBindingV1,
    LockMetadataV1,
    PhaseBindingV1,
    ProvenanceBindingV1,
    RecoveryClaimRequestV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    RootInitializationRequestV1,
    RunStorageError,
    RunStorageFailureCode,
    SourceBindingV1,
    ToolBindingV1,
)
from poker_deliberation.storage.revision_store import (
    RunRevisionStore,
    initialize_revision_root,
    inspect_root_initialization,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2i-", dir=parent) as directory:
        yield Path(directory)


def _root_request(revision: Path, legacy: Path) -> RootInitializationRequestV1:
    return RootInitializationRequestV1(
        revision_root=revision,
        legacy_runs_root=legacy,
        root_id="root-" + "1" * 32,
        initialized_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
    )


def _input_artifact(text: str = "exact payload") -> RevisionArtifactV1:
    data = canonical_json_bytes(CaseInput(case_id="case-storage", kind="claim", raw_text=text))
    evidence = ClassificationEvidence(
        source_classifications=(ContextClassification.PUBLIC,),
        restricted_secret_check_completed=True,
    )
    local = LocalDataBindingV1(
        logical_name="input.json",
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
    )
    source = SourceBindingV1(
        source_id="user-input",
        source_kind="user_input",
        trust_kind="trusted_user_input",
        source_sha256=domain_sha256("poker-user-input-source-v1", data),
    )
    return RevisionArtifactV1(
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
        provenance_bindings=(local, source),
    )


def _canonical_artifact(
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


def _payload_source(artifact: RevisionArtifactV1) -> SourceBindingV1:
    return SourceBindingV1(
        source_id=payload_source_id(artifact.logical_name),
        source_kind="payload_artifact",
        trust_kind="verified_payload",
        source_logical_name=artifact.logical_name,
        source_schema_version=artifact.artifact_schema_version,
        source_sha256=sha256_bytes(artifact.exact_bytes),
    )


def _final_report_request(
    *,
    report_schema: str,
    tool_ordinals: tuple[tuple[str, int], ...] = (),
    report_result_ids: tuple[str, ...] | None = None,
    include_provider_record: bool = False,
    include_final_context: bool | None = None,
    mismatch_result_binding: str | None = None,
) -> RevisionPublishRequestV1:
    run_id = "Run-final-artifact"
    case = CaseInput(case_id="case-final-artifact", kind="claim", raw_text="final artifact")
    case_bytes = canonical_json_bytes(case)
    empty_json = b"[]"
    input_artifact = _canonical_artifact(
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
    normalized = _canonical_artifact(
        "normalized_case.json",
        data=case_bytes,
        schema="poker-normalized-case-artifact-v1",
        origin="normalization_output",
        sources=(_payload_source(input_artifact),),
    )
    assumptions = _canonical_artifact(
        "assumptions.json",
        data=empty_json,
        schema="poker-assumption-list-artifact-v1",
        origin="assumption_ledger",
        sources=(_payload_source(input_artifact),),
    )
    evidence = _canonical_artifact(
        "evidence.jsonl",
        data=b"",
        schema="poker-evidence-record-jsonl-artifact-v1",
        origin="evidence_ledger",
        sources=(_payload_source(input_artifact),),
    ).model_copy(
        update={
            "media_type": "application/x-ndjson",
            "serialization": "poker-run-storage-jsonl-v1",
        }
    )
    approvals = _canonical_artifact(
        "approvals.json",
        data=empty_json,
        schema="poker-approval-request-list-artifact-v1",
        origin="approval_ledger",
        sources=(_payload_source(input_artifact),),
    )
    context = ContextBindingV1(
        context_sha256="1" * 64,
        context_id="context-final-artifact",
        attempt_id="context-attempt-final-artifact",
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
    assignments_value: tuple[AgentAssignment, ...] = ()
    execution_values: tuple[AgentExecutionRecord, ...] = ()
    if include_provider_record:
        assignment = AgentAssignment(
            assignment_id="assignment-final-artifact",
            agent_role="reviewer",
            task="review",
        )
        assignments_value = (assignment,)
        execution_values = (
            AgentExecutionRecord(
                execution_id="execution-final-artifact",
                assignment_id=assignment.assignment_id,
                agent_role=assignment.agent_role,
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
            ),
        )
    assignments_bytes = canonical_json_bytes(assignments_value)
    assignments = _canonical_artifact(
        "assignments.json",
        data=assignments_bytes,
        schema="poker-agent-assignment-list-artifact-v1",
        origin="assignment_ledger",
        sources=(_payload_source(normalized),),
    )
    execution_bytes = canonical_json_bytes(execution_values)
    execution_phase = PhaseBindingV1(
        run_id=run_id,
        phase_id="analysis",
        phase_schema_version="1.0.0",
        attempt_id="phase-execution-final-artifact",
        context_ids=(context.context_id,) if include_provider_record else (),
        input_hash="6" * 64,
        policy_snapshot_hash="7" * 64,
        output_hash="8" * 64,
    )
    execution_records = _canonical_artifact(
        "agent_execution_records.json",
        data=execution_bytes,
        schema="poker-agent-execution-record-list-artifact-v1",
        origin="agent_execution_ledger",
        sources=(_payload_source(assignments), _payload_source(normalized)),
        extra_bindings=(context, execution_phase) if include_provider_record else (),
    )
    security_events = _canonical_artifact(
        "security_events.json",
        data=empty_json,
        schema="poker-security-event-list-artifact-v1",
        origin="security_event_ledger",
        sources=(_payload_source(input_artifact),),
    )

    tool_phase = PhaseBindingV1(
        run_id=run_id,
        phase_id="tool_research",
        phase_schema_version="1.0.0",
        attempt_id="phase-tool-final-artifact",
        input_hash="9" * 64,
        policy_snapshot_hash="a" * 64,
    )
    tool_artifacts: list[RevisionArtifactV1] = []
    tool_results: dict[str, ToolResult] = {}
    for result_id, ordinal in tool_ordinals:
        tool_input_bytes = b"{}"
        tool_request = ToolRequest(
            request_id=f"request-{result_id}",
            tool_name="solver_status",
            input={},
            requested_by="phase",
            requires_approval=False,
            contract_version="2.0.0",
        )
        result = ToolResult(
            result_id=result_id,
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
        input_sha = phase_canonical_sha256(tool_request.input)
        binding = ToolBindingV1(
            run_id=run_id,
            phase_attempt_id=tool_phase.attempt_id,
            ordinal=ordinal,
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
        tool_input = _canonical_artifact(
            f"tool_results/{result_id}.input.json",
            data=tool_input_bytes,
            schema="poker-tool-input-artifact-v1",
            origin="tool_input",
            sources=(_payload_source(input_artifact), _payload_source(normalized)),
            extra_bindings=(tool_phase, binding),
        )
        result_binding = (
            binding.model_copy(update={"requested_by": "mismatched-phase"})
            if mismatch_result_binding == result_id
            else binding
        )
        tool_result = _canonical_artifact(
            f"tool_results/{result_id}.json",
            data=result_bytes,
            schema="poker-tool-result-artifact-v1",
            origin="tool_result",
            sources=(_payload_source(tool_input),),
            extra_bindings=(tool_phase, result_binding),
        )
        tool_artifacts.extend((tool_input, tool_result))
        tool_results[result_id] = result

    disputes = _canonical_artifact(
        "disputes.json",
        data=empty_json,
        schema="poker-dispute-list-artifact-v1",
        origin="dispute_ledger",
        sources=tuple(
            _payload_source(artifact)
            for artifact in tool_artifacts
            if artifact.logical_name.endswith(".json")
            and not artifact.logical_name.endswith(".input.json")
        ),
    )
    if report_result_ids is None:
        if report_schema == "poker-final-report-artifact-v2":
            report_result_ids = tuple(
                result_id for result_id, _ordinal in sorted(tool_ordinals, key=lambda item: item[1])
            )
        else:
            report_result_ids = tuple(sorted(tool_results, key=lambda item: item.encode("utf-8")))
    final_report = FinalReport(
        run_id=run_id,
        conclusion="Versioned final-report artifact",
        agent_execution_records=list(execution_values),
        tool_results=[tool_results[result_id] for result_id in report_result_ids],
        generated_at=NOW,
    )
    final_phase = PhaseBindingV1(
        run_id=run_id,
        phase_id="synthesis",
        phase_schema_version="1.0.0",
        attempt_id="phase-synthesis-final-artifact",
        context_ids=(context.context_id,) if include_provider_record else (),
        input_hash="b" * 64,
        policy_snapshot_hash="c" * 64,
        output_hash="d" * 64,
    )
    budget = BudgetPolicyBindingV1(
        policy_schema_version="2.0.0",
        policy_sha256="e" * 64,
    )
    base_artifacts = (
        input_artifact,
        normalized,
        assumptions,
        evidence,
        approvals,
        assignments,
        execution_records,
        security_events,
        *tool_artifacts,
        disputes,
    )
    if include_final_context is None:
        include_final_context = (
            include_provider_record or report_schema == "poker-final-report-artifact-v1"
        )
    final_bindings: tuple[ProvenanceBindingV1, ...] = (
        *((context,) if include_final_context else ()),
        final_phase,
        budget,
    )
    final_json = _canonical_artifact(
        "final_report.json",
        data=canonical_json_bytes(final_report),
        schema=report_schema,
        origin="final_report_json",
        sources=tuple(_payload_source(artifact) for artifact in base_artifacts),
        extra_bindings=final_bindings,
    )
    return RevisionPublishRequestV1(
        run_id=run_id,
        transaction_id="txn-" + "f" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="p2-010b-phase-revision",
        producer_version="0.2.0",
        artifacts=(*base_artifacts, final_json),
    )


def _request(
    transaction_digit: str,
    *,
    revision: int,
    artifact: RevisionArtifactV1,
    expected_revision: int | None = None,
    expected_manifest: str | None = None,
    expected_pointer: str | None = None,
) -> RevisionPublishRequestV1:
    return RevisionPublishRequestV1(
        run_id="Run-storage",
        transaction_id="txn-" + transaction_digit * 32,
        proposed_revision=revision,
        expected_revision=expected_revision,
        expected_manifest_sha256=expected_manifest,
        expected_pointer_sha256=expected_pointer,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(artifact,),
    )


def test_versioned_final_report_v1_regression_and_v2_execution_order() -> None:
    v1_request = _final_report_request(
        report_schema="poker-final-report-artifact-v1",
        tool_ordinals=(("z-result", 0), ("a-result", 1)),
        report_result_ids=("a-result", "z-result"),
    )
    v1_inventory, _v1_heads, _v1_parsed = build_inventory(
        v1_request,
        max_artifact_bytes=1_000_000,
    )
    assert (
        next(
            entry for entry in v1_inventory if entry.logical_name == "final_report.json"
        ).artifact_schema_version
        == "poker-final-report-artifact-v1"
    )

    v2_request = _final_report_request(
        report_schema="poker-final-report-artifact-v2",
        tool_ordinals=(("z-result", 0), ("a-result", 1)),
    )
    v2_inventory, _v2_heads, v2_parsed = build_inventory(
        v2_request,
        max_artifact_bytes=1_000_000,
    )
    assert [result.result_id for result in v2_parsed["final_report.json"].tool_results] == [
        "z-result",
        "a-result",
    ]
    assert [
        entry.logical_name
        for entry in v2_inventory
        if entry.logical_name.startswith("tool_results/")
    ] == [
        "tool_results/a-result.input.json",
        "tool_results/z-result.input.json",
        "tool_results/a-result.json",
        "tool_results/z-result.json",
    ]


def test_final_report_v2_rejects_binding_ordinal_and_embedded_order_errors() -> None:
    cases = (
        (
            _final_report_request(
                report_schema="poker-final-report-artifact-v2",
                tool_ordinals=(("a-result", 0),),
                mismatch_result_binding="a-result",
            ),
            "byte-identical",
        ),
        (
            _final_report_request(
                report_schema="poker-final-report-artifact-v2",
                tool_ordinals=(("a-result", 0), ("b-result", 0)),
            ),
            "unique and contiguous",
        ),
        (
            _final_report_request(
                report_schema="poker-final-report-artifact-v2",
                tool_ordinals=(("a-result", 0), ("b-result", 2)),
            ),
            "unique and contiguous",
        ),
        (
            _final_report_request(
                report_schema="poker-final-report-artifact-v2",
                tool_ordinals=(("z-result", 0), ("a-result", 1)),
                report_result_ids=("a-result", "z-result"),
            ),
            "embedded tool results mismatch",
        ),
    )
    for request, message in cases:
        with pytest.raises(CanonicalStorageError, match=message):
            build_inventory(request, max_artifact_bytes=1_000_000)


def test_final_report_v2_context_presence_is_exactly_provider_trace_sensitive() -> None:
    zero_provider = _final_report_request(
        report_schema="poker-final-report-artifact-v2",
    )
    build_inventory(zero_provider, max_artifact_bytes=1_000_000)

    spurious = _final_report_request(
        report_schema="poker-final-report-artifact-v2",
        include_final_context=True,
    )
    with pytest.raises(CanonicalStorageError, match="spurious provider context"):
        build_inventory(spurious, max_artifact_bytes=1_000_000)

    v2_semantics_cross_labeled_as_v1 = _final_report_request(
        report_schema="poker-final-report-artifact-v1",
        include_final_context=False,
    )
    with pytest.raises(CanonicalStorageError, match="lacks context provenance"):
        build_inventory(
            v2_semantics_cross_labeled_as_v1,
            max_artifact_bytes=1_000_000,
        )

    missing = _final_report_request(
        report_schema="poker-final-report-artifact-v2",
        include_provider_record=True,
        include_final_context=False,
    )
    with pytest.raises(CanonicalStorageError, match="lacks required provider context"):
        build_inventory(missing, max_artifact_bytes=1_000_000)

    provider = _final_report_request(
        report_schema="poker-final-report-artifact-v2",
        include_provider_record=True,
    )
    build_inventory(provider, max_artifact_bytes=1_000_000)


def test_revision_rejects_execution_with_stale_assignment_ledger() -> None:
    request = _final_report_request(
        report_schema="poker-final-report-artifact-v2",
        include_provider_record=True,
    )
    assignment_artifact = next(
        artifact for artifact in request.artifacts if artifact.logical_name == "assignments.json"
    )
    execution_artifact = next(
        artifact
        for artifact in request.artifacts
        if artifact.logical_name == "agent_execution_records.json"
    )
    assignments = [
        AgentAssignment.model_validate(item)
        for item in parse_canonical_json(assignment_artifact.exact_bytes)
    ]
    assignments[0] = assignments[0].model_copy(update={"assignment_id": "assignment-stale-ledger"})
    execution_records = [
        AgentExecutionRecord.model_validate(item)
        for item in parse_canonical_json(execution_artifact.exact_bytes)
    ]

    with pytest.raises(
        CanonicalStorageError,
        match="does not correlate to its assignment ledger",
    ):
        validate_assignment_execution_correlation(assignments, execution_records)


def test_final_report_v2_publishes_and_reads_from_matching_dedicated_root(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy-v2"
    legacy.mkdir()
    revision = short_tmp / "dedicated-v2"
    initialize_revision_root(
        RootInitializationRequestV1(
            revision_root=revision,
            legacy_runs_root=legacy,
            root_id="root-" + "8" * 32,
            initialized_at=NOW,
            producer_id="p2-010b-phase-revision",
            producer_version="0.2.0",
        )
    )
    request = _final_report_request(
        report_schema="poker-final-report-artifact-v2",
        tool_ordinals=(("z-result", 0), ("a-result", 1)),
    )
    store = RunRevisionStore(
        revision,
        legacy,
        producer_id="p2-010b-phase-revision",
        producer_version="0.2.0",
    )
    outcome = store.publish(request)
    verified = store.read_current(request.run_id)

    assert outcome.outcome_kind == "published"
    assert verified.current_revision == 1


def test_explicit_root_init_publish_two_revisions_read_and_replay(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "dedicated revisions"
    root_request = _root_request(revision, legacy)

    assert inspect_root_initialization(revision, legacy).status == "uninitialized"
    assert not revision.exists()
    assert initialize_revision_root(root_request).outcome_kind == "initialized"
    assert initialize_revision_root(root_request).outcome_kind == "already_initialized"
    assert inspect_root_initialization(revision, legacy).status == "initialized"

    artifact = _input_artifact()
    first_request = _request("2", revision=1, artifact=artifact)
    store = RunRevisionStore(revision, legacy)
    first = store.publish(first_request)
    verified_first = store.read_current("Run-storage")

    assert first.outcome_kind == "published"
    assert verified_first.current_revision == 1
    payload_path = (
        revision
        / "runs"
        / "Run-storage"
        / ".revision-store"
        / "revisions"
        / f"r1-{first_request.transaction_id}"
        / "payload"
        / "input.json"
    )
    assert payload_path.read_bytes() == artifact.exact_bytes

    second_request = _request(
        "3",
        revision=2,
        artifact=artifact,
        expected_revision=1,
        expected_manifest=verified_first.manifest_sha256,
        expected_pointer=verified_first.current_pointer_sha256,
    )
    second = store.publish(second_request)
    monkeypatch.chdir(short_tmp)
    verified_second = RunRevisionStore(revision, legacy).read_current("Run-storage")

    assert second.outcome_kind == "published"
    assert verified_second.current_revision == 2
    assert [item.revision for item in verified_second.reachable_history] == [2, 1]
    assert store.publish(first_request).outcome_kind == "historical_committed"
    assert store.publish(second_request).outcome_kind == "current_committed"
    historical_claim = RecoveryClaimRequestV1(
        run_id_sha256=first.run_id_sha256,
        transaction_id=first_request.transaction_id,
        transaction_sha256=first.transaction_sha256,
        observed_pointer_sha256=verified_second.current_pointer_sha256,
        orphan_form="unreferenced_revision",
        claim_id="claim-" + "8" * 32,
        claimant_token="owner-" + "9" * 32,
        claimed_at=NOW,
    )
    with pytest.raises(RunStorageError) as historical:
        store.claim_orphan(first_request.run_id, historical_claim)
    assert historical.value.failure.code is RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT
    assert not list(revision.rglob("completion.json"))


def test_stale_cas_and_recreated_bound_legacy_root_fail_closed(short_tmp: Path) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    artifact = _input_artifact()
    first_request = _request("2", revision=1, artifact=artifact)
    store.publish(first_request)

    stale = _request(
        "3",
        revision=2,
        artifact=artifact,
        expected_revision=1,
        expected_manifest="a" * 64,
        expected_pointer="b" * 64,
    )
    with pytest.raises(RunStorageError) as conflict:
        store.publish(stale)
    assert conflict.value.failure.code is RunStorageFailureCode.RUN_CONFLICT
    assert store.read_current("Run-storage").current_revision == 1

    legacy.rmdir()
    legacy.mkdir()
    with pytest.raises(RunStorageError) as identity:
        store.read_current("Run-storage")
    assert identity.value.failure.code is RunStorageFailureCode.ROOT_INITIALIZATION_INCOMPLETE


def test_run_quota_accepts_exact_physical_peak_and_rejects_one_over(
    short_tmp: Path,
) -> None:
    def provision(name: str) -> tuple[Path, Path]:
        base = short_tmp / name
        base.mkdir()
        legacy = base / "legacy"
        legacy.mkdir()
        revision = base / "revision"
        initialize_revision_root(_root_request(revision, legacy))
        return revision, legacy

    def fixed_owner(prefix: str) -> str:
        return f"{prefix}-" + "d" * 32

    request = _request("e", revision=1, artifact=_input_artifact())
    reference_revision, reference_legacy = provision("reference")
    reference_store = RunRevisionStore(
        reference_revision,
        reference_legacy,
        id_factory=fixed_owner,
        clock=lambda: NOW,
    )
    reference_store.publish(request)
    exact_peak = reference_store._run_physical_bytes(request.run_id)

    equal_revision, equal_legacy = provision("equal")
    equal_store = RunRevisionStore(
        equal_revision,
        equal_legacy,
        max_run_bytes=exact_peak,
        id_factory=fixed_owner,
        clock=lambda: NOW,
    )
    assert equal_store.publish(request).outcome_kind == "published"

    over_revision, over_legacy = provision("one-over")
    over_store = RunRevisionStore(
        over_revision,
        over_legacy,
        max_run_bytes=exact_peak - 1,
        id_factory=fixed_owner,
        clock=lambda: NOW,
    )
    with pytest.raises(RunStorageError) as error:
        over_store.publish(request)
    assert error.value.failure.code is RunStorageFailureCode.RUN_BUDGET_EXCEEDED
    assert not (over_revision / "runs" / request.run_id).exists()


def test_reachable_revision_cannot_be_claimed(short_tmp: Path) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    request = _request("f", revision=1, artifact=_input_artifact())
    outcome = store.publish(request)
    claim = RecoveryClaimRequestV1(
        run_id_sha256=outcome.run_id_sha256,
        transaction_id=request.transaction_id,
        transaction_sha256=outcome.transaction_sha256,
        observed_pointer_sha256=outcome.pointer_sha256,
        orphan_form="unreferenced_revision",
        claim_id="claim-" + "1" * 32,
        claimant_token="owner-" + "2" * 32,
        claimed_at=NOW,
    )

    with pytest.raises(RunStorageError) as error:
        store.claim_orphan(request.run_id, claim)
    assert error.value.failure.code is RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT
    assert not list(revision.rglob(f"{request.transaction_id}.json"))


def test_same_transaction_with_different_digest_is_idempotency_conflict(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    request = _request("9", revision=1, artifact=_input_artifact("first bytes"))
    store.publish(request)
    before = store.read_current(request.run_id)

    conflicting = _request(
        "9",
        revision=1,
        artifact=_input_artifact("different bytes"),
    )
    with pytest.raises(RunStorageError) as error:
        store.publish(conflicting)
    assert error.value.failure.code is RunStorageFailureCode.IDEMPOTENCY_CONFLICT
    after = store.read_current(request.run_id)
    assert after.current_pointer_sha256 == before.current_pointer_sha256
    assert after.reachable_history == before.reachable_history


def test_existing_exact_claim_remains_idempotent_after_current_advances(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    orphan_request = _request("a", revision=1, artifact=_input_artifact("orphan"))

    def leave_unreferenced(hook: str) -> None:
        if hook == "current.before_replace":
            raise OSError("leave unreferenced revision")

    with pytest.raises(RunStorageError):
        RunRevisionStore(
            revision,
            legacy,
            fault_injector=leave_unreferenced,
        ).publish(orphan_request)

    store = RunRevisionStore(revision, legacy)
    orphan = store.inspect_orphans(orphan_request.run_id).revision_orphans[0]
    assert orphan.transaction_sha256 is not None
    claim_request = RecoveryClaimRequestV1(
        run_id_sha256=run_id_sha256(orphan_request.run_id),
        transaction_id=orphan_request.transaction_id,
        transaction_sha256=orphan.transaction_sha256,
        observed_pointer_sha256=None,
        orphan_form="unreferenced_revision",
        claim_id="claim-" + "a" * 32,
        claimant_token="owner-" + "a" * 32,
        claimed_at=NOW,
    )
    claim = store.claim_orphan(orphan_request.run_id, claim_request)

    current_request = _request(
        "b",
        revision=1,
        artifact=_input_artifact("current"),
    )
    store.publish(current_request)
    assert store.read_current(current_request.run_id).current_revision == 1
    assert (
        RunRevisionStore(revision, legacy).claim_orphan(
            orphan_request.run_id,
            claim_request,
        )
        == claim
    )


def test_corrupt_advisory_metadata_is_replaced_only_during_a_new_locked_publish(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    first_request = _request("a", revision=1, artifact=_input_artifact())
    store.publish(first_request)
    first = store.read_current(first_request.run_id)
    metadata = (
        revision
        / ".revision-control"
        / "locks"
        / f"{run_lock_key_sha256(first_request.run_id)}.metadata.json"
    )
    metadata.write_bytes(b"{")

    second_request = _request(
        "b",
        revision=2,
        artifact=_input_artifact(),
        expected_revision=1,
        expected_manifest=first.manifest_sha256,
        expected_pointer=first.current_pointer_sha256,
    )
    assert store.publish(second_request).outcome_kind == "published"
    parsed = parse_canonical_model(metadata.read_bytes(), LockMetadataV1)
    assert parsed.transaction_id == second_request.transaction_id


def test_lineage_self_backedge_attempt_fails_closed(short_tmp: Path) -> None:
    legacy = short_tmp / "legacy-cycle"
    legacy.mkdir()
    revision = short_tmp / "revision-cycle"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    artifact = _input_artifact()
    first_request = _request("c", revision=1, artifact=artifact)
    store.publish(first_request)
    first = store.read_current(first_request.run_id)
    second_request = _request(
        "d",
        revision=2,
        artifact=artifact,
        expected_revision=1,
        expected_manifest=first.manifest_sha256,
        expected_pointer=first.current_pointer_sha256,
    )
    store.publish(second_request)
    control = revision / "runs" / first_request.run_id / ".revision-store"
    revision_dir = control / "revisions" / f"r2-{second_request.transaction_id}"
    transaction_path = revision_dir / "transaction.json"
    manifest_path = revision_dir / "manifest.json"
    current_path = control / "current.json"
    old_manifest_sha = sha256_bytes(manifest_path.read_bytes())
    transaction = parse_canonical_json(transaction_path.read_bytes())
    manifest = parse_canonical_json(manifest_path.read_bytes())
    pointer = parse_canonical_json(current_path.read_bytes())
    assert isinstance(transaction, dict)
    assert isinstance(manifest, dict)
    assert isinstance(pointer, dict)
    transaction["expected_manifest_sha256"] = old_manifest_sha
    transaction_projection = dict(transaction)
    transaction_projection.pop("transaction_sha256")
    transaction["transaction_sha256"] = transaction_sha256(transaction_projection)
    manifest["previous_manifest_sha256"] = old_manifest_sha
    manifest["transaction_sha256"] = transaction["transaction_sha256"]
    transaction_path.write_bytes(canonical_json_bytes(transaction))
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    pointer["transaction_sha256"] = transaction["transaction_sha256"]
    pointer["manifest_sha256"] = sha256_bytes(manifest_bytes)
    current_path.write_bytes(canonical_json_bytes(pointer))

    with pytest.raises(RunStorageError) as error:
        store.read_current(first_request.run_id)
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
    assert error.value.failure.reconciliation_required is True
