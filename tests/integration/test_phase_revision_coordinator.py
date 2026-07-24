"""P2-010B phase revision coordinator integration tests."""

from __future__ import annotations

import pickle
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.config import AppConfig
from poker_deliberation.context_lifecycle import ContextClassification
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
from poker_deliberation.phases.models import (
    ProviderSnapshot,
    SynthesisInput,
    SynthesisOutput,
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
from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import CaseInput, FinalReport, ToolRequest, ToolResult
from poker_deliberation.state_machine import RunState, WorkflowStateMachine
from poker_deliberation.storage.revision_canonical import (
    TEXT_SERIALIZATION,
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
    LocalDataBindingV1,
    PhaseBindingV1,
    ReportBindingV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    RootInitializationRequestV1,
    SourceBindingV1,
)
from poker_deliberation.storage.revision_store import (
    RunRevisionStore,
    initialize_revision_root,
)
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
    report = FinalReport.model_validate(
        parse_canonical_json(by_name["final_report.json"].exact_bytes)
    )
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
                result=ToolResult.model_validate(
                    parse_canonical_json(by_name[f"tool_results/{result_id}.json"].exact_bytes)
                ),
            )
            for ordinal, (result_id, tool_request) in enumerate(
                zip(ordered_result_ids, tool_requests, strict=True)
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
        for result_id in ordered_result_ids:
            for suffix in (".input.json", ".json"):
                logical_name = f"tool_results/{result_id}{suffix}"
                artifact = by_name[logical_name]
                by_name[logical_name] = artifact.model_copy(
                    update={
                        "provenance_bindings": (
                            *(
                                binding
                                for binding in artifact.provenance_bindings
                                if not isinstance(binding, PhaseBindingV1)
                            ),
                            tool_phase_binding,
                        )
                    }
                )
        tool_trace = (
            PhaseTracePair(
                request=tool_phase_request,
                outcome=tool_phase_outcome,
            ),
        )
    synthesis_request = make_phase_request(
        run_id=base.run_id,
        phase_id=PhaseId.SYNTHESIS,
        attempt_id="phase-synthesis-p2-010b",
        policy_snapshot_hash=orchestrator.phase_policy_snapshot_hash,
        input_value=SynthesisInput(
            run_id=base.run_id,
            machine_state="FINAL_SYNTHESIS",
            completed=True,
            case=case,
            data_quality=tuple(report.data_quality),
            claim_assessments=tuple(report.claim_assessments),
            reports=(),
            execution_records=tuple(report.agent_execution_records),
            tool_results=tuple(report.tool_results),
            disputes=tuple(report.disputes),
            evidence_records=tuple(report.evidence),
            approvals=tuple(report.approvals),
            security_events=tuple(report.security_events),
            provider_snapshot=ProviderSnapshot(available=False, reason="not configured"),
            tool_input_artifact_paths=tuple(
                f"tool_results/{result.result_id}.input.json" for result in report.tool_results
            ),
            record_sensitive_data=False,
            generated_at=report.generated_at,
        ),
    )
    synthesis_outcome = successful_outcome(
        synthesis_request,
        SynthesisOutput(report=report),
        requested_next_state="completed",
        artifact_intents=_artifact_intents(),
    )
    markdown = _markdown_artifact(by_name["final_report.json"], report)
    materialized = {
        **by_name,
        "final_report.md": markdown,
    }
    intent_snapshots = tuple(
        ArtifactIntentSnapshotV1(
            kind=intent.kind.value,
            relative_path=intent.relative_path,
            media_type=intent.media_type,
            content_sha256=(
                None
                if intent.kind is ArtifactKind.STATE
                else sha256_bytes(materialized[intent.relative_path].exact_bytes)
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
    final_json = by_name["final_report.json"].model_copy(
        update={
            "provenance_bindings": (
                *(
                    binding
                    for binding in by_name["final_report.json"].provenance_bindings
                    if not isinstance(
                        binding,
                        (PhaseBindingV1, BudgetPolicyBindingV1),
                    )
                ),
                synthesis_binding,
                BudgetPolicyBindingV1(
                    policy_schema_version=orchestrator.budget_policy.schema_version,
                    policy_sha256=orchestrator.budget_policy.canonical_sha256,
                ),
            )
        }
    )
    markdown = _markdown_artifact(final_json, report)
    request = RevisionPublishRequestV1.model_validate(
        base.model_copy(
            update={
                "artifacts": (
                    *(
                        by_name[artifact.logical_name]
                        for artifact in base.artifacts
                        if artifact.logical_name != "final_report.json"
                    ),
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
    prepared = orchestrator.prepare_revision_bundle(
        machine,
        run_id=request.run_id,
        trace=PhaseRevisionTraceV1(
            synthesis=PhaseTracePair(
                request=synthesis_request,
                outcome=synthesis_outcome,
            ),
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

    applied = orchestrator.apply_revision_transition(
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

    result = orchestrator.prepare_revision_bundle(
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
        orchestrator.apply_revision_transition(
            machine,
            coordinator=coordinator,
            bundle=bundle,
            authorization=first_authorization,
        ),
        PhaseTransitionApplyResultV1,
    )

    replay_authorization = coordinator.publish(bundle)
    assert isinstance(replay_authorization, PhaseTransitionAuthorizationV1)
    replay = orchestrator.apply_revision_transition(
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
    denied = orchestrator.apply_revision_transition(
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
    result = orchestrator.apply_revision_transition(
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
