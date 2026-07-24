"""P2-010B phase revision coordinator integration tests."""

from __future__ import annotations

import pickle
from collections.abc import Generator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.agents import select_roles
from poker_deliberation.budgets import BudgetSnapshot, FakeMonotonicClock
from poker_deliberation.config import AppConfig
from poker_deliberation.context_lifecycle import (
    ContextClassification,
    legacy_context_sha256,
)
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.phases.contracts import (
    ArtifactIntent,
    ArtifactKind,
    PhaseId,
    make_phase_request,
    successful_outcome,
)
from poker_deliberation.phases.contracts import (
    canonical_sha256 as phase_canonical_sha256,
)
from poker_deliberation.phases.executors import AnalysisExecutor
from poker_deliberation.phases.models import (
    AnalysisInput,
    AnalysisOutput,
    ContextBuildInput,
    ContextBuildOutput,
    ProviderSnapshot,
    SynthesisInput,
    ToolExecutionBinding,
    ToolResearchInput,
    ToolResearchOutput,
)
from poker_deliberation.phases.revision_coordinator import (
    COORDINATOR_PRODUCER_ID,
    COORDINATOR_PRODUCER_VERSION,
    PhaseRevisionBundleV1,
    PhaseRevisionCoordinator,
    PhaseRevisionFailureCode,
    PhaseRevisionFailureV1,
    PhaseRevisionTraceV1,
    PhaseTracePair,
    PhaseTransitionApplyResultV1,
    PhaseTransitionAuthorizationV1,
)
from poker_deliberation.phases.services import ContextBuildService, SynthesisService
from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import CaseInput, FinalReport, ToolRequest, ToolResult
from poker_deliberation.state_machine import RunState, WorkflowStateMachine
from poker_deliberation.storage.revision_canonical import (
    TEXT_SERIALIZATION,
    canonical_json_bytes,
    classification_evidence_sha256,
    parse_canonical_json,
    payload_source_id,
    sha256_bytes,
    upstream_source_sha256,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    ArtifactIntentSnapshotV1,
    BudgetPolicyBindingV1,
    ContextBindingV1,
    LocalDataBindingV1,
    PhaseBindingV1,
    ReportBindingV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    RootInitializationRequestV1,
    SourceBindingV1,
    ToolBindingV1,
)
from poker_deliberation.storage.revision_store import (
    RunRevisionStore,
    initialize_revision_root,
)
from poker_deliberation.tools import default_registry
from tests.integration import test_revision_storage as revision_storage_fixture

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2c-", dir=parent) as directory:
        yield Path(directory)


def _artifact_intents() -> tuple[ArtifactIntent, ...]:
    return (
        ArtifactIntent(
            kind=ArtifactKind.AGENT_EXECUTION_RECORDS,
            relative_path="agent_execution_records.json",
            media_type="application/json",
        ),
        ArtifactIntent(
            kind=ArtifactKind.SECURITY_EVENTS,
            relative_path="security_events.json",
            media_type="application/json",
        ),
        ArtifactIntent(
            kind=ArtifactKind.STATE,
            relative_path="state.json",
            media_type="application/json",
        ),
        ArtifactIntent(
            kind=ArtifactKind.APPROVALS,
            relative_path="approvals.json",
            media_type="application/json",
        ),
        ArtifactIntent(
            kind=ArtifactKind.DISPUTES,
            relative_path="disputes.json",
            media_type="application/json",
        ),
        ArtifactIntent(
            kind=ArtifactKind.FINAL_REPORT_JSON,
            relative_path="final_report.json",
            media_type="application/json",
        ),
        ArtifactIntent(
            kind=ArtifactKind.FINAL_REPORT_MARKDOWN,
            relative_path="final_report.md",
            media_type="text/markdown",
        ),
    )


def _markdown_artifact(
    final_json: RevisionArtifactV1,
    report: FinalReport,
) -> RevisionArtifactV1:
    data = render_markdown(report).encode("utf-8")
    evidence = ClassificationEvidence(
        source_classifications=(ContextClassification.PUBLIC,),
        restricted_secret_check_completed=True,
    )
    local = LocalDataBindingV1(
        logical_name="final_report.md",
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
    )
    source = SourceBindingV1(
        source_id=payload_source_id("final_report.json"),
        source_kind="payload_artifact",
        trust_kind="verified_payload",
        source_logical_name="final_report.json",
        source_schema_version=final_json.artifact_schema_version,
        source_sha256=sha256_bytes(final_json.exact_bytes),
    )
    report_binding = ReportBindingV1(
        report_id=report.run_id,
        report_schema_version=final_json.artifact_schema_version,
        report_json_sha256=sha256_bytes(final_json.exact_bytes),
        rendered_markdown_sha256=sha256_bytes(data),
        upstream_source_sha256=upstream_source_sha256(final_json.provenance_bindings),
    )
    return RevisionArtifactV1(
        logical_name="final_report.md",
        media_type="text/markdown",
        artifact_schema_version="poker-final-report-markdown-artifact-v1",
        serialization=TEXT_SERIALIZATION,
        exact_bytes=data,
        required=True,
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
        origin_kind="final_report_markdown",
        provenance_bindings=(local, source, report_binding),
    )


def build_valid_scenario(
    tmp_path: Path,
    *,
    tool_ordinals: tuple[tuple[str, int], ...] = (),
    with_provider_trace: bool = False,
) -> tuple[
    Orchestrator,
    WorkflowStateMachine,
    PhaseRevisionCoordinator,
    PhaseRevisionBundleV1,
]:
    """Build one empty-provider/tool but fully correlated structural revision."""

    orchestrator = Orchestrator(AppConfig(runs_dir=tmp_path / "ordinary-runs"))
    base = revision_storage_fixture._final_report_request(
        report_schema="poker-final-report-artifact-v2",
        tool_ordinals=tool_ordinals,
    )
    by_name = {artifact.logical_name: artifact for artifact in base.artifacts}
    case = CaseInput.model_validate(
        parse_canonical_json(by_name["normalized_case.json"].exact_bytes)
    )
    base_report = FinalReport.model_validate(
        parse_canonical_json(by_name["final_report.json"].exact_bytes)
    )
    context_trace: tuple[
        PhaseTracePair[ContextBuildInput, ContextBuildOutput],
        ...,
    ] = ()
    analysis_trace: tuple[PhaseTracePair[AnalysisInput, AnalysisOutput], ...] = ()
    context_bindings: tuple[ContextBindingV1, ...] = ()
    analysis_outputs: tuple[AnalysisOutput, ...] = ()
    if with_provider_trace:
        registered_tools = tuple(default_registry().names())
        assignment = select_roles(case)[0]
        context_request = make_phase_request(
            run_id=base.run_id,
            phase_id=PhaseId.CONTEXT_BUILD,
            attempt_id="phase-context-p2-010b",
            policy_snapshot_hash=orchestrator.phase_policy_snapshot_hash,
            context_ids=("context-p2-010b",),
            input_value=ContextBuildInput(
                case=case,
                assignment=assignment,
                registered_tools=registered_tools,
                created_at=NOW,
                expires_at=NOW + timedelta(seconds=30),
                context_id="context-p2-010b",
                context_attempt_id="context-attempt-p2-010b",
            ),
        )
        context_outcome = ContextBuildService().run(context_request)
        assert context_outcome.output is not None
        dispatch = context_outcome.output.dispatches[0]
        provider_availability = orchestrator.provider.availability()
        analysis_request = make_phase_request(
            run_id=base.run_id,
            phase_id=PhaseId.ANALYSIS,
            attempt_id="phase-analysis-p2-010b",
            policy_snapshot_hash=orchestrator.phase_policy_snapshot_hash,
            context_ids=("context-p2-010b",),
            input_value=AnalysisInput(
                dispatch=dispatch,
                provider_timeout_seconds=30.0,
                registered_tools=registered_tools,
                max_output_bytes=orchestrator.budget_policy.max_provider_output_bytes,
                record_sensitive_data=False,
                started_at=NOW,
                execution_id="execution-p2-010b",
                fallback_report_id="report-p2-010b",
                provider_availability=provider_availability,
                budget_policy=orchestrator.budget_policy,
                budget_snapshot=BudgetSnapshot(
                    policy_sha256=orchestrator.budget_policy.canonical_sha256
                ),
                budget_observed_at_ns=0,
                run_deadline_ns=orchestrator.budget_policy.runtime_limit_ns,
            ),
        )
        analysis_outcome = AnalysisExecutor(
            orchestrator.provider,
            context_clock=lambda: NOW,
            record_clock=lambda: NOW,
            monotonic_clock=FakeMonotonicClock(0),
        ).run(analysis_request)
        assert analysis_outcome.output is not None
        analysis_output = analysis_outcome.output
        envelope = dispatch.envelope
        context_binding = ContextBindingV1(
            context_sha256=legacy_context_sha256(dispatch.context),
            context_id=envelope.lineage.context_id,
            attempt_id=envelope.lineage.attempt_id,
            parent_context_id=envelope.lineage.parent_context_id,
            schema_version=envelope.schema_version,
            classification=envelope.policy.classification,
            payload_sha256=envelope.payload_sha256,
            source_sha256=envelope.lineage.source_sha256,
            policy_sha256=envelope.policy_sha256,
            envelope_sha256=envelope.integrity_sha256,
            expires_at=envelope.policy.expires_at,
            producer_runtime=envelope.lineage.producer_runtime.value,
            consumer_runtime=envelope.lineage.consumer_runtime.value,
        )
        analysis_binding = PhaseBindingV1(
            run_id=analysis_request.run_id,
            phase_id=analysis_request.phase_id.value,
            phase_schema_version=analysis_request.phase_schema_version,
            attempt_id=analysis_request.attempt_id,
            context_ids=analysis_request.context_ids,
            input_hash=analysis_request.input_hash,
            policy_snapshot_hash=analysis_request.policy_snapshot_hash,
            output_hash=analysis_outcome.output_hash,
        )
        by_name["assignments.json"] = revision_storage_fixture._canonical_artifact(
            "assignments.json",
            data=canonical_json_bytes((dispatch.assignment,)),
            schema="poker-agent-assignment-list-artifact-v1",
            origin="assignment_ledger",
            sources=(revision_storage_fixture._payload_source(by_name["normalized_case.json"]),),
        )
        by_name["agent_execution_records.json"] = revision_storage_fixture._canonical_artifact(
            "agent_execution_records.json",
            data=canonical_json_bytes((analysis_output.execution_record,)),
            schema="poker-agent-execution-record-list-artifact-v1",
            origin="agent_execution_ledger",
            sources=(
                revision_storage_fixture._payload_source(by_name["assignments.json"]),
                revision_storage_fixture._payload_source(by_name["normalized_case.json"]),
            ),
            extra_bindings=(context_binding, analysis_binding),
        )
        report_name = f"agent_reports/{analysis_output.report.report_id}.json"
        by_name[report_name] = revision_storage_fixture._canonical_artifact(
            report_name,
            data=canonical_json_bytes(analysis_output.report),
            schema="poker-agent-report-artifact-v1",
            origin="agent_report",
            sources=(
                revision_storage_fixture._payload_source(by_name["assignments.json"]),
                revision_storage_fixture._payload_source(by_name["normalized_case.json"]),
            ),
            extra_bindings=(context_binding, analysis_binding),
        )
        context_trace = (PhaseTracePair(request=context_request, outcome=context_outcome),)
        analysis_trace = (PhaseTracePair(request=analysis_request, outcome=analysis_outcome),)
        context_bindings = (context_binding,)
        analysis_outputs = (analysis_output,)
    ordered_tool_results: tuple[ToolResult, ...] = ()
    tool_trace: tuple[
        PhaseTracePair[ToolResearchInput, ToolResearchOutput],
        ...,
    ] = ()
    if tool_ordinals:
        ordered_result_ids = tuple(
            result_id
            for result_id, _ordinal in sorted(
                tool_ordinals,
                key=lambda item: item[1],
            )
        )
        tool_requests = tuple(
            ToolRequest(
                request_id=f"request-{result_id}",
                tool_name="solver_status",
                input={},
                requested_by="phase",
                requires_approval=False,
                contract_version="2.0.0",
            )
            for result_id in ordered_result_ids
        )
        tool_phase_request = make_phase_request(
            run_id=base.run_id,
            phase_id=PhaseId.TOOL_RESEARCH,
            attempt_id="phase-tool-final-artifact",
            policy_snapshot_hash=orchestrator.phase_policy_snapshot_hash,
            input_value=ToolResearchInput(
                requests=tool_requests,
                fallback_result_ids=tuple(
                    f"fallback-{index}" for index in range(len(tool_requests))
                ),
            ),
        )
        ordered_tool_results = tuple(
            default_registry()
            .execute(
                "solver_status",
                {},
                contract_version="2.0.0",
            )
            .model_copy(
                update={
                    "result_id": result_id,
                    "duration_seconds": 0.0,
                    "created_at": NOW,
                }
            )
            for result_id in ordered_result_ids
        )
        tool_execution_bindings = tuple(
            ToolExecutionBinding(
                run_id=base.run_id,
                phase_attempt_id=tool_phase_request.attempt_id,
                ordinal=ordinal,
                request=tool_request,
                request_input_sha256=phase_canonical_sha256(tool_request.input),
                validated_result_input_sha256=phase_canonical_sha256(tool_request.input),
                materialized_result_input_sha256=phase_canonical_sha256(tool_request.input),
                requested_contract_version=tool_request.contract_version,
                supported_contract_version="2.0.0",
                result_contract_version="2.0.0",
                result=result,
            )
            for ordinal, (tool_request, result) in enumerate(
                zip(tool_requests, ordered_tool_results, strict=True)
            )
        )
        tool_phase_outcome = successful_outcome(
            tool_phase_request,
            ToolResearchOutput(
                bindings=tool_execution_bindings,
                retry_classifications=(None,) * len(tool_execution_bindings),
            ),
        )
        tool_phase_binding = PhaseBindingV1(
            run_id=tool_phase_request.run_id,
            phase_id=tool_phase_request.phase_id.value,
            phase_schema_version=tool_phase_request.phase_schema_version,
            attempt_id=tool_phase_request.attempt_id,
            context_ids=tool_phase_request.context_ids,
            input_hash=tool_phase_request.input_hash,
            policy_snapshot_hash=tool_phase_request.policy_snapshot_hash,
            output_hash=tool_phase_outcome.output_hash,
        )
        for binding in tool_execution_bindings:
            result_id = binding.result.result_id
            input_name = f"tool_results/{result_id}.input.json"
            result_name = f"tool_results/{result_id}.json"
            input_artifact = by_name[input_name]
            result_artifact = by_name[result_name].model_copy(
                update={"exact_bytes": canonical_json_bytes(binding.result)}
            )
            tool_binding = ToolBindingV1(
                run_id=base.run_id,
                phase_attempt_id=tool_phase_request.attempt_id,
                ordinal=binding.ordinal,
                request_id=binding.request.request_id,
                request_tool_name=binding.request.tool_name,
                requested_by=binding.request.requested_by,
                requires_approval=binding.request.requires_approval,
                requested_contract_version=binding.requested_contract_version,
                tool_request_sha256=phase_canonical_sha256(binding.request),
                request_input_artifact_sha256=sha256_bytes(input_artifact.exact_bytes),
                result_id=result_id,
                result_tool_name=binding.result.tool_name,
                result_artifact_sha256=sha256_bytes(result_artifact.exact_bytes),
                request_input_sha256=binding.request_input_sha256,
                validated_result_input_sha256=binding.validated_result_input_sha256,
                materialized_result_input_sha256=binding.materialized_result_input_sha256,
                supported_contract_version=binding.supported_contract_version,
                result_contract_version=binding.result_contract_version,
            )
            by_name[input_name] = input_artifact.model_copy(
                update={
                    "provenance_bindings": (
                        *(
                            item
                            for item in input_artifact.provenance_bindings
                            if not isinstance(item, (PhaseBindingV1, ToolBindingV1))
                        ),
                        tool_phase_binding,
                        tool_binding,
                    )
                }
            )
            by_name[result_name] = result_artifact.model_copy(
                update={
                    "provenance_bindings": (
                        *(
                            item
                            for item in result_artifact.provenance_bindings
                            if not isinstance(item, (PhaseBindingV1, ToolBindingV1))
                        ),
                        tool_phase_binding,
                        tool_binding,
                    )
                }
            )
        by_name["disputes.json"] = revision_storage_fixture._canonical_artifact(
            "disputes.json",
            data=by_name["disputes.json"].exact_bytes,
            schema=by_name["disputes.json"].artifact_schema_version,
            origin="dispute_ledger",
            sources=tuple(
                revision_storage_fixture._payload_source(by_name[f"tool_results/{result_id}.json"])
                for result_id in ordered_result_ids
            ),
        )
        tool_trace = (
            PhaseTracePair(
                request=tool_phase_request,
                outcome=tool_phase_outcome,
            ),
        )
    by_name["disputes.json"] = revision_storage_fixture._canonical_artifact(
        "disputes.json",
        data=by_name["disputes.json"].exact_bytes,
        schema=by_name["disputes.json"].artifact_schema_version,
        origin="dispute_ledger",
        sources=tuple(
            revision_storage_fixture._payload_source(artifact)
            for logical_name, artifact in by_name.items()
            if (
                logical_name.startswith("agent_reports/")
                or (
                    logical_name.startswith("tool_results/")
                    and logical_name.endswith(".json")
                    and not logical_name.endswith(".input.json")
                )
            )
        ),
    )
    synthesis_request = make_phase_request(
        run_id=base.run_id,
        phase_id=PhaseId.SYNTHESIS,
        attempt_id="phase-synthesis-p2-010b",
        policy_snapshot_hash=orchestrator.phase_policy_snapshot_hash,
        context_ids=tuple(binding.context_id for binding in context_bindings),
        input_value=SynthesisInput(
            run_id=base.run_id,
            machine_state="FINAL_SYNTHESIS",
            completed=True,
            case=case,
            data_quality=tuple(base_report.data_quality),
            claim_assessments=tuple(base_report.claim_assessments),
            reports=tuple(output.report for output in analysis_outputs),
            execution_records=tuple(output.execution_record for output in analysis_outputs),
            tool_results=ordered_tool_results,
            disputes=tuple(base_report.disputes),
            evidence_records=tuple(base_report.evidence),
            approvals=tuple(base_report.approvals),
            security_events=tuple(base_report.security_events),
            provider_snapshot=ProviderSnapshot(
                available=with_provider_trace,
                reason="local provider trace" if with_provider_trace else "not configured",
            ),
            tool_input_artifact_paths=tuple(
                f"tool_results/{result.result_id}.input.json" for result in ordered_tool_results
            ),
            record_sensitive_data=False,
            generated_at=NOW,
        ),
    )
    synthesis_outcome = SynthesisService().run(synthesis_request)
    synthesis_output = synthesis_outcome.output
    assert synthesis_output is not None
    report = synthesis_output.report
    report_bytes = canonical_json_bytes(report)
    markdown_bytes = render_markdown(report).encode("utf-8")
    intent_snapshots = tuple(
        ArtifactIntentSnapshotV1(
            kind=intent.kind.value,
            relative_path=intent.relative_path,
            media_type=intent.media_type,
            content_sha256=(
                None
                if intent.kind is ArtifactKind.STATE
                else sha256_bytes(report_bytes)
                if intent.kind is ArtifactKind.FINAL_REPORT_JSON
                else sha256_bytes(markdown_bytes)
                if intent.kind is ArtifactKind.FINAL_REPORT_MARKDOWN
                else sha256_bytes(by_name[intent.relative_path].exact_bytes)
            ),
        )
        for intent in synthesis_outcome.artifact_intents
    )
    synthesis_binding = PhaseBindingV1(
        run_id=synthesis_request.run_id,
        phase_id=synthesis_request.phase_id.value,
        phase_schema_version=synthesis_request.phase_schema_version,
        attempt_id=synthesis_request.attempt_id,
        context_ids=synthesis_request.context_ids,
        input_hash=synthesis_request.input_hash,
        policy_snapshot_hash=synthesis_request.policy_snapshot_hash,
        output_hash=synthesis_outcome.output_hash,
        artifact_intents=intent_snapshots,
    )
    pre_final_artifacts = tuple(
        artifact
        for logical_name, artifact in by_name.items()
        if logical_name != "final_report.json"
    )
    final_json = revision_storage_fixture._canonical_artifact(
        "final_report.json",
        data=report_bytes,
        schema="poker-final-report-artifact-v2",
        origin="final_report_json",
        sources=tuple(
            revision_storage_fixture._payload_source(artifact) for artifact in pre_final_artifacts
        ),
        extra_bindings=(
            *context_bindings,
            synthesis_binding,
            BudgetPolicyBindingV1(
                policy_schema_version=orchestrator.budget_policy.schema_version,
                policy_sha256=orchestrator.budget_policy.canonical_sha256,
            ),
        ),
    )
    markdown = _markdown_artifact(final_json, report)
    request = RevisionPublishRequestV1.model_validate(
        base.model_copy(
            update={
                "artifacts": (
                    *pre_final_artifacts,
                    final_json,
                    markdown,
                )
            }
        ).model_dump(mode="python")
    )

    legacy_root = tmp_path / "legacy-runs"
    legacy_root.mkdir()
    revision_root = tmp_path / "revision-runs"
    initialize_revision_root(
        RootInitializationRequestV1(
            revision_root=revision_root,
            legacy_runs_root=legacy_root,
            root_id="root-" + "9" * 32,
            initialized_at=NOW,
            producer_id=COORDINATOR_PRODUCER_ID,
            producer_version=COORDINATOR_PRODUCER_VERSION,
        )
    )
    store = RunRevisionStore(
        revision_root,
        legacy_root,
        producer_id=COORDINATOR_PRODUCER_ID,
        producer_version=COORDINATOR_PRODUCER_VERSION,
    )
    coordinator = PhaseRevisionCoordinator(
        store,
        budget_policy=orchestrator.budget_policy,
        expected_policy_snapshot_hash=orchestrator.phase_policy_snapshot_hash,
    )
    machine = WorkflowStateMachine(
        orchestrator.budget_policy,
        state=RunState.FINAL_SYNTHESIS,
    )
    prepared = orchestrator._prepare_revision_bundle(
        machine,
        run_id=request.run_id,
        trace=PhaseRevisionTraceV1(
            synthesis=PhaseTracePair(
                request=synthesis_request,
                outcome=synthesis_outcome,
            ),
            context_builds=context_trace,
            analyses=analysis_trace,
            tool_research=tool_trace,
        ),
        request=request,
    )
    assert isinstance(prepared, PhaseRevisionBundleV1)
    return orchestrator, machine, coordinator, prepared


def test_published_revision_authorizes_exact_orchestrator_transition(
    short_tmp: Path,
) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)

    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)

    applied = orchestrator._apply_revision_transition(
        machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=authorization,
    )

    assert applied == PhaseTransitionApplyResultV1(outcome_kind="applied")
    assert machine.state is RunState.COMPLETED
    assert machine.events[-1].reason == "durable synthesis revision committed"


def test_prepare_rejects_non_synthesis_machine_without_mutation(
    short_tmp: Path,
) -> None:
    orchestrator, _machine, _coordinator, bundle = build_valid_scenario(short_tmp)
    machine = WorkflowStateMachine(orchestrator.budget_policy)
    before = machine.snapshot()

    result = orchestrator._prepare_revision_bundle(
        machine,
        run_id=bundle.request.run_id,
        trace=bundle.trace,
        request=bundle.request,
    )

    assert result == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.INVALID_PLAN)
    assert machine.snapshot() == before


def test_exact_same_process_replay_is_idempotent(short_tmp: Path) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    first_authorization = coordinator.publish(bundle)
    assert isinstance(first_authorization, PhaseTransitionAuthorizationV1)
    assert isinstance(
        orchestrator._apply_revision_transition(
            machine,
            coordinator=coordinator,
            bundle=bundle,
            authorization=first_authorization,
        ),
        PhaseTransitionApplyResultV1,
    )

    replay_authorization = coordinator.publish(bundle)
    assert isinstance(replay_authorization, PhaseTransitionAuthorizationV1)
    replay = orchestrator._apply_revision_transition(
        machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=replay_authorization,
    )

    assert replay == PhaseTransitionApplyResultV1(outcome_kind="already_applied")
    assert len(machine.events) == 1


def test_authorization_is_nonserializable_and_exact_data_is_immutable(
    short_tmp: Path,
) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(short_tmp)
    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)

    with pytest.raises(TypeError):
        pickle.dumps(authorization)

    object.__setattr__(authorization, "manifest_sha256", "0" * 64)
    denied = orchestrator._apply_revision_transition(
        machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=authorization,
    )
    assert denied == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.AUTHORIZATION_MISMATCH)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert machine.events == []


def test_tool_trace_preserves_execution_ordinal_over_lexical_result_id(
    short_tmp: Path,
) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(
        short_tmp,
        tool_ordinals=(("z-result", 0), ("a-result", 1)),
    )

    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)
    result = orchestrator._apply_revision_transition(
        machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=authorization,
    )

    assert result == PhaseTransitionApplyResultV1(outcome_kind="applied")
    report = bundle.trace.synthesis.outcome.output
    assert report is not None
    assert [item.result_id for item in report.report.tool_results] == [
        "z-result",
        "a-result",
    ]


def test_exact_provider_context_and_analysis_trace_authorizes_transition(
    short_tmp: Path,
) -> None:
    orchestrator, machine, coordinator, bundle = build_valid_scenario(
        short_tmp,
        with_provider_trace=True,
    )

    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)
    applied = orchestrator._apply_revision_transition(
        machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=authorization,
    )

    assert applied == PhaseTransitionApplyResultV1(outcome_kind="applied")
    assert len(bundle.trace.context_builds) == 1
    assert len(bundle.trace.analyses) == 1


def test_forged_context_case_preimage_is_denied_without_storage_mutation(
    short_tmp: Path,
) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(
        short_tmp,
        with_provider_trace=True,
    )
    pair = bundle.trace.context_builds[0]
    forged_input = pair.request.input.model_copy(
        update={
            "case": pair.request.input.case.model_copy(update={"raw_text": "FORGED-DIFFERENT-CASE"})
        }
    )
    forged_request = make_phase_request(
        run_id=pair.request.run_id,
        phase_id=PhaseId.CONTEXT_BUILD,
        attempt_id=pair.request.attempt_id,
        policy_snapshot_hash=pair.request.policy_snapshot_hash,
        context_ids=pair.request.context_ids,
        input_value=forged_input,
    )
    forged_outcome = pair.outcome.model_copy(update={"input_hash": forged_request.input_hash})
    forged_trace = replace(
        bundle.trace,
        context_builds=(
            PhaseTracePair(
                request=forged_request,
                outcome=forged_outcome,
            ),
        ),
    )
    forged_bundle = replace(bundle, trace=forged_trace)

    denied = coordinator.publish(forged_bundle)

    assert denied == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.INVALID_TRACE)
    assert machine.state is RunState.FINAL_SYNTHESIS
    assert not (
        coordinator.store.runs_root / bundle.request.run_id / ".revision-store" / "current.json"
    ).exists()


def test_duplicate_context_dispatch_trace_is_denied(
    short_tmp: Path,
) -> None:
    _orchestrator, machine, coordinator, bundle = build_valid_scenario(
        short_tmp,
        with_provider_trace=True,
    )
    forged_bundle = replace(
        bundle,
        trace=replace(
            bundle.trace,
            context_builds=(
                bundle.trace.context_builds[0],
                bundle.trace.context_builds[0],
            ),
        ),
    )

    denied = coordinator.publish(forged_bundle)

    assert denied == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.INVALID_TRACE)
    assert machine.state is RunState.FINAL_SYNTHESIS


def test_transition_authorization_cannot_apply_to_an_equivalent_other_machine(
    short_tmp: Path,
) -> None:
    orchestrator, source_machine, coordinator, bundle = build_valid_scenario(short_tmp)
    authorization = coordinator.publish(bundle)
    assert isinstance(authorization, PhaseTransitionAuthorizationV1)
    other_machine = WorkflowStateMachine(
        orchestrator.budget_policy,
        state=RunState.FINAL_SYNTHESIS,
    )

    denied = orchestrator._apply_revision_transition(
        other_machine,
        coordinator=coordinator,
        bundle=bundle,
        authorization=authorization,
    )

    assert denied == PhaseRevisionFailureV1(code=PhaseRevisionFailureCode.AUTHORIZATION_MISMATCH)
    assert source_machine.state is RunState.FINAL_SYNTHESIS
    assert other_machine.state is RunState.FINAL_SYNTHESIS
    assert other_machine.events == []
