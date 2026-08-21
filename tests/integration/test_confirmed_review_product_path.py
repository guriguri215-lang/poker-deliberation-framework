from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import poker_deliberation.confirmed_review as confirmed_review_module
import poker_deliberation.orchestrator as orchestrator_module
import poker_deliberation.tools.registry as tool_registry_module
from poker_deliberation.agents import select_roles
from poker_deliberation.config import BudgetConfig
from poker_deliberation.confirmed_review import (
    ConfirmedReviewError,
    admit_confirmed_review,
    build_confirmed_review_provenance,
    create_review_confirmation,
)
from poker_deliberation.confirmed_review_models import (
    ConfirmedReviewDiagnosticCode,
    ConfirmedReviewProvenanceV1,
    ReviewConfirmationAuthorityV1,
)
from poker_deliberation.context_lifecycle import (
    build_context_envelope,
    context_payload,
    legacy_context_sha256,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.phases.services import build_agent_context
from poker_deliberation.providers import LocalProvider
from poker_deliberation.range_grammar import action_prefix_sha256
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionStatus,
    AgentReport,
    CanonicalHand,
    ConfidenceGrade,
    EpistemicLabel,
    FinalReport,
)
from poker_deliberation.state_machine import RunState
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
    parse_canonical_model,
)
from poker_deliberation.storage.terminal_canonical import product_payload_commitments
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
)
from poker_deliberation.tools import ToolRegistry, default_registry
from tests.confirmed_review_support import app_config, confirmed_admission
from tests.confirmed_review_support import candidate_payload as base_candidate_payload
from tests.range_support import versioned_range_hand


def test_confirmed_review_publishes_complete_bound_artifact_chain(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-product-1",
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    )
    report = orchestrator.run_confirmed_review(admission)
    assert report.run_status == "completed"
    assert report.conclusion == "指定されたローカル検証・計算を完了しました。"
    assert [item.tool_name for item in report.tool_results] == ["hand_validator"]
    assert all(item.provider == "local" for item in report.agent_execution_records)

    read = orchestrator.product_store.read_current(report.run_id)
    assert read.read_status is RunReadStatus.SUCCEEDED
    names = {item.inventory.logical_name for item in read.payloads}
    assert {
        "confirmed_review_source.txt",
        "confirmed_review_candidate.json",
        "confirmed_review_confirmation.json",
        "confirmed_review_provenance.json",
        "input.json",
        "final_report.json",
        "lifecycle_audit.json",
    } <= names
    provenance = parse_canonical_model(
        read.payload_bytes("confirmed_review_provenance.json"),
        ConfirmedReviewProvenanceV1,
    )
    assert provenance.source_sha256 == admission.candidate.projection.source.content_sha256
    assert provenance.candidate_sha256 == admission.candidate.candidate_sha256
    assert provenance.confirmation_sha256 == admission.confirmation.confirmation_sha256
    assert provenance.provider_narrative_epistemic_label == "UNKNOWN"
    assert {item.epistemic_label for item in provenance.tool_support} == {"CALCULATED"}

    payloads = {payload.inventory.logical_name: payload.exact_bytes for payload in read.payloads}
    forged_state = json.loads(payloads["state.json"])
    assert forged_state["events"][-1]["target"] == "COMPLETED"
    forged_state["events"][-1]["target"] = "FAILED_WITH_LIMITATIONS"
    payloads["state.json"] = canonical_json_bytes(forged_state)
    with pytest.raises(CanonicalStorageError, match="terminal event"):
        product_payload_commitments(
            payloads,
            run_id=report.run_id,
            status="succeeded",
            revision=read.revision,
            revision_root=orchestrator.product_store.revision_root,
            transaction_id=read.transaction_id,
        )


def test_report_confirmed_marker_must_match_admitted_case(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-report-marker-1",
        now=datetime.now(UTC),
    )
    report = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    ).run_confirmed_review(admission)
    reconstructed_input = report.model_dump(mode="json")["reconstructed_input"]
    reconstructed_input["metadata"]["confirmed_review"]["intake_id"] = "intake-forged"
    forged_report = report.model_copy(
        update={"reconstructed_input": reconstructed_input},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as marker:
        build_confirmed_review_provenance(admission, forged_report)
    assert marker.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH


def test_report_input_and_claim_assessments_must_match_admitted_case(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-report-input-1",
        now=datetime.now(UTC),
    )
    report = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    ).run_confirmed_review(admission)

    reconstructed_input = report.model_dump(mode="json")["reconstructed_input"]
    reconstructed_input["case_id"] = "forged-different-case"
    forged_input_report = report.model_copy(
        update={"reconstructed_input": reconstructed_input},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as reconstructed:
        build_confirmed_review_provenance(admission, forged_input_report)
    assert reconstructed.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    forged_claim = report.claim_assessments[0].model_copy(
        update={
            "claim_id": "forged-fact-claim",
            "label": EpistemicLabel.FACT,
            "confidence": ConfidenceGrade.A,
        },
        deep=True,
    )
    forged_claim_report = report.model_copy(
        update={"claim_assessments": [*report.claim_assessments, forged_claim]},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as claims:
        build_confirmed_review_provenance(admission, forged_claim_report)
    assert claims.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    validator = report.tool_results[0]
    forged_tool_input = validator.model_copy(
        update={
            "input": {
                **validator.input,
                "hero_cards": ["Qs", "Jd"],
            }
        },
        deep=True,
    )
    forged_tool_input_report = report.model_copy(
        update={"tool_results": [forged_tool_input, *report.tool_results[1:]]},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as tool_input:
        build_confirmed_review_provenance(admission, forged_tool_input_report)
    assert tool_input.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    forged_tool_output = validator.model_copy(
        update={"output": {**validator.output, "warnings": ["forged-output"]}},
        deep=True,
    )
    forged_tool_output_report = report.model_copy(
        update={"tool_results": [forged_tool_output, *report.tool_results[1:]]},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as tool_output:
        build_confirmed_review_provenance(admission, forged_tool_output_report)
    assert tool_output.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for metadata_field in ("version", "contract_version"):
        forged_tool_metadata = validator.model_copy(
            update={metadata_field: "9.9.9"},
            deep=True,
        )
        forged_tool_metadata_report = report.model_copy(
            update={
                "tool_results": [
                    forged_tool_metadata,
                    *report.tool_results[1:],
                ]
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as tool_metadata:
            build_confirmed_review_provenance(admission, forged_tool_metadata_report)
        assert tool_metadata.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for observation_field, forged_value in (
        ("duration_seconds", 1_000_000.0),
        ("created_at", report.generated_at + timedelta(days=1)),
    ):
        forged_observation = validator.model_copy(
            update={observation_field: forged_value},
            deep=True,
        )
        forged_observation_report = report.model_copy(
            update={"tool_results": [forged_observation, *report.tool_results[1:]]},
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as observation:
            build_confirmed_review_provenance(admission, forged_observation_report)
        assert observation.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    forged_report_time = report.model_copy(
        update={"generated_at": admission.confirmation.expires_at + timedelta(seconds=1)},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as report_time:
        build_confirmed_review_provenance(admission, forged_report_time)
    assert report_time.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    first_agent = report.agent_execution_records[0]
    for update in (
        {
            "started_at": report.generated_at + timedelta(seconds=1),
            "completed_at": report.generated_at + timedelta(seconds=2),
        },
        {
            "started_at": first_agent.completed_at + timedelta(seconds=1),
            "completed_at": first_agent.completed_at,
        },
    ):
        forged_agent = first_agent.model_copy(update=update, deep=True)
        forged_agent_report = report.model_copy(
            update={
                "agent_execution_records": [
                    forged_agent,
                    *report.agent_execution_records[1:],
                ]
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as agent_time:
            build_confirmed_review_provenance(admission, forged_agent_report)
        assert agent_time.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for update in (
        {"allowed_tools": ["solver_adapter"]},
        {"context_expires_at": first_agent.completed_at - timedelta(seconds=1)},
        {"context_expires_at": first_agent.started_at + timedelta(days=1)},
        {"context_producer_runtime": "external"},
    ):
        forged_agent = first_agent.model_copy(update=update, deep=True)
        forged_agent_report = report.model_copy(
            update={
                "agent_execution_records": [
                    forged_agent,
                    *report.agent_execution_records[1:],
                ]
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError):
            build_confirmed_review_provenance(admission, forged_agent_report)

    for field in (
        "context_sha256",
        "context_payload_sha256",
        "context_source_sha256",
        "context_policy_sha256",
        "context_envelope_sha256",
    ):
        forged_agent = first_agent.model_copy(update={field: "f" * 64}, deep=True)
        forged_agent_report = report.model_copy(
            update={
                "agent_execution_records": [
                    forged_agent,
                    *report.agent_execution_records[1:],
                ]
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as context_hash:
            build_confirmed_review_provenance(admission, forged_agent_report)
        assert context_hash.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    duplicate_execution_id = first_agent.execution_id
    duplicate_assignment_id = first_agent.assignment_id
    duplicate_context_id = first_agent.context_id
    duplicate_context_attempt_id = first_agent.context_attempt_id
    duplicate_agent_report = report.model_copy(
        update={
            "agent_execution_records": [
                record.model_copy(
                    update={
                        "execution_id": duplicate_execution_id,
                        "assignment_id": duplicate_assignment_id,
                        "context_id": duplicate_context_id,
                        "context_attempt_id": duplicate_context_attempt_id,
                    },
                    deep=True,
                )
                for record in report.agent_execution_records
            ]
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as duplicate_agent:
        build_confirmed_review_provenance(admission, duplicate_agent_report)
    assert duplicate_agent.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    reversed_agent_report = report.model_copy(
        update={
            "agent_execution_records": list(reversed(report.agent_execution_records)),
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as reversed_agent:
        build_confirmed_review_provenance(admission, reversed_agent_report)
    assert reversed_agent.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    second_agent = report.agent_execution_records[1]
    overlapping_started_at = first_agent.completed_at - timedelta(microseconds=1)
    assert admission.admitted_at <= overlapping_started_at < first_agent.completed_at
    overlapping_expiry = overlapping_started_at + timedelta(seconds=30)
    registered_tools = frozenset(default_registry().names())
    overlapping_context = build_agent_context(
        admission.case,
        second_agent.agent_role,
        registered_tools,
    )
    assignment_template = next(
        assignment
        for assignment in select_roles(admission.case)
        if assignment.agent_role == second_agent.agent_role
    )
    overlapping_assignment = assignment_template.model_copy(
        update={
            "assignment_id": second_agent.assignment_id,
            "context_keys": sorted(context_payload(overlapping_context)),
        },
        deep=True,
    )
    overlapping_envelope = build_context_envelope(
        overlapping_context,
        overlapping_assignment,
        run_id=report.run_id,
        expires_at=overlapping_expiry,
        clock=lambda: overlapping_started_at,
        context_id=second_agent.context_id,
        attempt_id=second_agent.context_attempt_id,
    )
    overlapping_agent = second_agent.model_copy(
        update={
            "started_at": overlapping_started_at,
            "context_sha256": legacy_context_sha256(overlapping_context),
            "context_payload_sha256": overlapping_envelope.payload_sha256,
            "context_source_sha256": overlapping_envelope.lineage.source_sha256,
            "context_policy_sha256": overlapping_envelope.policy_sha256,
            "context_envelope_sha256": overlapping_envelope.integrity_sha256,
            "context_expires_at": overlapping_expiry,
        },
        deep=True,
    )
    overlapping_report = report.model_copy(
        update={
            "agent_execution_records": [
                first_agent,
                overlapping_agent,
                *report.agent_execution_records[2:],
            ],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as overlapping_execution:
        build_confirmed_review_provenance(admission, overlapping_report)
    assert overlapping_execution.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    completed_with_error = first_agent.model_copy(
        update={"error": "forged completed-record error"},
        deep=True,
    )
    completed_with_error_report = report.model_copy(
        update={
            "agent_execution_records": [
                completed_with_error,
                *report.agent_execution_records[1:],
            ],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as completed_error:
        build_confirmed_review_provenance(admission, completed_with_error_report)
    assert completed_error.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH


@pytest.mark.parametrize(
    "status",
    [
        AgentExecutionStatus.FAILED,
        AgentExecutionStatus.REFUSED,
        AgentExecutionStatus.FALLBACK,
    ],
)
def test_noncompleted_execution_requires_safe_nonempty_error(
    tmp_path,
    status: AgentExecutionStatus,
) -> None:
    admission = confirmed_admission(
        run_id=f"run-confirmed-execution-error-{status.value}",
        now=datetime.now(UTC),
    )
    report = Orchestrator(
        app_config(tmp_path / status.value),
        provider=LocalProvider(),
    ).run_confirmed_review(admission)
    first = report.agent_execution_records[0]

    missing_error = first.model_copy(
        update={"status": status, "error": None},
        deep=True,
    )
    missing_error_report = report.model_copy(
        update={
            "run_status": "failed_with_limitations",
            "agent_execution_records": [
                missing_error,
                *report.agent_execution_records[1:],
            ],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as missing:
        build_confirmed_review_provenance(admission, missing_error_report)
    assert missing.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for secret_text in (
        "api_key=sk_test_never_store",
        "api\u0600_key=ABCDEFGHIJKLMNOP123456",
        "api\ufff0_key=ABCDEFGHIJKLMNOP123456",
        "api_key=ABCDEFGHIJKLMNOP123456 api\u180b_key=QRSTUVWXYZABCDEFGHIJ",
    ):
        secret_error = first.model_copy(
            update={"status": status, "error": secret_text},
            deep=True,
        )
        secret_error_report = report.model_copy(
            update={
                "run_status": "failed_with_limitations",
                "conclusion": ("実行予算または安全上の制限に達したため、制限付きで終了しました。"),
                "agent_execution_records": [
                    secret_error,
                    *report.agent_execution_records[1:],
                ],
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as secret:
            build_confirmed_review_provenance(admission, secret_error_report)
        assert secret.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH


def test_provider_failure_status_and_runtime_stage_are_authoritative(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-provider-failure-authority-1",
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(app_config(tmp_path), provider=LocalProvider())
    report = orchestrator.run_confirmed_review(admission)
    read = orchestrator.product_store.read_current(report.run_id)
    storage_authority = {
        "storage_root": orchestrator.product_store.revision_root,
        "storage_revision": read.revision,
        "storage_transaction_id": read.transaction_id,
    }
    assignments = [
        AgentAssignment.model_validate(item)
        for item in json.loads(read.payload_bytes("assignments.json"))
    ]
    reports_by_role = {
        parsed.agent_role: parsed
        for payload in read.payloads
        if payload.inventory.logical_name.startswith("agent_reports/")
        for parsed in [AgentReport.model_validate_json(payload.exact_bytes)]
    }
    agent_reports = [
        reports_by_role[record.agent_role] for record in report.agent_execution_records
    ]
    provenance_authority = {
        **storage_authority,
        "assignments": assignments,
        "agent_reports": agent_reports,
    }
    first = report.agent_execution_records[0]

    def forged_failure_report(
        *,
        status: AgentExecutionStatus,
        error: str,
        data_quality: list[str],
    ) -> FinalReport:
        record = first.model_copy(update={"status": status, "error": error}, deep=True)
        exact_data_quality = [*report.data_quality, *data_quality]
        return report.model_copy(
            update={
                "run_status": "failed_with_limitations",
                "conclusion": ("実行予算または安全上の制限に達したため、制限付きで終了しました。"),
                "agent_execution_records": [
                    record,
                    *report.agent_execution_records[1:],
                ],
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )

    wrong_status_cases = (
        (
            AgentExecutionStatus.REFUSED,
            "context envelope has expired",
            ["provider intake context rejected: context envelope has expired"],
        ),
        (
            AgentExecutionStatus.FALLBACK,
            "context envelope has expired",
            ["provider intake context rejected: context envelope has expired"],
        ),
        (
            AgentExecutionStatus.FAILED,
            "context handoff policy refused",
            ["provider intake handoff refused: context handoff policy refused"],
        ),
        (
            AgentExecutionStatus.REFUSED,
            "provider report ID is duplicated",
            ["provider intake report ID rejected: provider report ID is duplicated"],
        ),
        (
            AgentExecutionStatus.FAILED,
            "RuntimeError: provider analyze failed",
            ["provider intake failed: RuntimeError"],
        ),
        (
            AgentExecutionStatus.REFUSED,
            "RuntimeError: provider analyze failed",
            ["provider intake failed: RuntimeError"],
        ),
    )
    for status, error, data_quality in wrong_status_cases:
        with pytest.raises(ConfirmedReviewError) as wrong_status:
            build_confirmed_review_provenance(
                admission,
                forged_failure_report(
                    status=status,
                    error=error,
                    data_quality=data_quality,
                ),
                **provenance_authority,
            )
        assert wrong_status.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    alias_report = forged_failure_report(
        status=AgentExecutionStatus.FAILED,
        error="context envelope has expired",
        data_quality=[
            "provider intake context rejected: context envelope has expired",
            "provider intake report ID rejected: context envelope has expired",
        ],
    )
    with pytest.raises(ConfirmedReviewError) as alias:
        build_confirmed_review_provenance(
            admission,
            alias_report,
            **provenance_authority,
        )
    assert alias.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    cancellation_report = forged_failure_report(
        status=AgentExecutionStatus.FAILED,
        error="provider exceeded deadline 0.125 seconds",
        data_quality=[
            "provider exceeded deadline 0.125 seconds",
            "provider cancellation was not confirmed",
        ],
    )
    with pytest.raises(ConfirmedReviewError) as continued_after_cancellation:
        build_confirmed_review_provenance(
            admission,
            cancellation_report,
            **provenance_authority,
        )
    assert continued_after_cancellation.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    runtime_message = "maximum runtime reached before provider analysis"
    runtime_report = report.model_copy(
        update={
            "run_status": "failed_with_limitations",
            "data_quality": [*report.data_quality, runtime_message],
            "limitations": [
                *report.data_quality,
                runtime_message,
                report.limitations[-1],
            ],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as runtime_stage:
        build_confirmed_review_provenance(
            admission,
            runtime_report,
            **provenance_authority,
        )
    assert runtime_stage.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    failed_conclusion = forged_failure_report(
        status=AgentExecutionStatus.FAILED,
        error="context envelope has expired",
        data_quality=["provider intake context rejected: context envelope has expired"],
    ).conclusion

    def staged_failure_report(
        message: str,
        *,
        records=report.agent_execution_records,
        tool_results=report.tool_results,
        analysis_sections=report.analysis_sections,
        reproduction_steps=report.reproduction_steps,
    ) -> FinalReport:
        exact_data_quality = [*report.data_quality, message]
        return report.model_copy(
            update={
                "run_status": "failed_with_limitations",
                "conclusion": failed_conclusion,
                "agent_execution_records": list(records),
                "tool_results": list(tool_results),
                "analysis_sections": list(analysis_sections),
                "reproduction_steps": list(reproduction_steps),
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )

    for legitimate_stage, records, sections, exact_agent_reports in (
        (
            "provider analysis skipped because round budget is zero",
            [],
            [],
            [],
        ),
        (
            "maximum runtime reached before provider analysis",
            [],
            [],
            [],
        ),
        (
            "maximum runtime exceeded after provider analysis",
            report.agent_execution_records,
            report.analysis_sections,
            agent_reports,
        ),
    ):
        provenance = build_confirmed_review_provenance(
            admission,
            staged_failure_report(
                legitimate_stage,
                records=records,
                analysis_sections=sections,
            ),
            assignments=assignments,
            agent_reports=exact_agent_reports,
            **storage_authority,
        )
        assert provenance.terminal_status == "failed_with_limitations"

    impossible_after_tool = staged_failure_report(
        "maximum runtime exceeded after tool execution",
        tool_results=[],
        reproduction_steps=[],
    )
    with pytest.raises(ConfirmedReviewError) as missing_tool_artifacts:
        build_confirmed_review_provenance(
            admission,
            impossible_after_tool,
            **provenance_authority,
        )
    assert missing_tool_artifacts.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    impossible_external_budget = staged_failure_report(
        "strict budget failure: external_cost_exceeded"
    )
    with pytest.raises(ConfirmedReviewError) as external_budget:
        build_confirmed_review_provenance(
            admission,
            impossible_external_budget,
            **provenance_authority,
        )
    assert external_budget.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    impossible_range_stage = staged_failure_report(
        "strict runtime refused before versioned range validation"
    )
    with pytest.raises(ConfirmedReviewError) as absent_range_stage:
        build_confirmed_review_provenance(
            admission,
            impossible_range_stage,
            **provenance_authority,
        )
    assert absent_range_stage.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    mutually_exclusive_stages = staged_failure_report(
        "provider analysis skipped because round budget is zero",
        records=[],
        analysis_sections=[],
    )
    exact_data_quality = [
        *mutually_exclusive_stages.data_quality,
        "maximum runtime reached before provider analysis",
    ]
    mutually_exclusive_stages = mutually_exclusive_stages.model_copy(
        update={
            "data_quality": exact_data_quality,
            "limitations": [*exact_data_quality, report.limitations[-1]],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as double_stage:
        build_confirmed_review_provenance(
            admission,
            mutually_exclusive_stages,
            assignments=assignments,
            agent_reports=[],
            **storage_authority,
        )
    assert double_stage.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    external_record = first.model_copy(
        update={
            "status": AgentExecutionStatus.REFUSED,
            "error": "external_cost_micro_usd exceeded its strict budget",
        },
        deep=True,
    )
    external_record_report = staged_failure_report(
        f"provider {first.agent_role} budget refused: external_cost_exceeded",
        records=[external_record],
        analysis_sections=report.analysis_sections[:1],
    )
    with pytest.raises(ConfirmedReviewError) as local_external_record:
        build_confirmed_review_provenance(
            admission,
            external_record_report,
            assignments=assignments,
            agent_reports=agent_reports[:1],
            **storage_authority,
        )
    assert local_external_record.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    provider_output_record = first.model_copy(
        update={
            "status": AgentExecutionStatus.REFUSED,
            "error": "provider_output_bytes exceeded its strict budget",
        },
        deep=True,
    )
    provider_output_alias = f"provider {first.agent_role} budget refused: provider_output_exceeded"
    provider_output_alias_report = staged_failure_report(
        provider_output_alias,
        records=[provider_output_record],
        analysis_sections=report.analysis_sections[:1],
    )
    exact_data_quality = [
        *provider_output_alias_report.data_quality,
        "strict budget failure: provider_output_exceeded",
    ]
    provider_output_alias_report = provider_output_alias_report.model_copy(
        update={
            "data_quality": exact_data_quality,
            "limitations": [*exact_data_quality, report.limitations[-1]],
        },
        deep=True,
    )
    provider_output_agent_report = agent_reports[0].model_copy(
        update={"confidence": ConfidenceGrade.D},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as provider_output_dq_alias:
        build_confirmed_review_provenance(
            admission,
            provider_output_alias_report,
            assignments=assignments,
            agent_reports=[provider_output_agent_report],
            **storage_authority,
        )
    assert provider_output_dq_alias.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    canonical_provider_output = f"provider {first.agent_role} output exceeded the hard byte limit"
    provider_output_without_terminal = staged_failure_report(
        canonical_provider_output,
        records=[provider_output_record],
        analysis_sections=report.analysis_sections[:1],
    )
    with pytest.raises(ConfirmedReviewError) as missing_provider_output_terminal:
        build_confirmed_review_provenance(
            admission,
            provider_output_without_terminal,
            assignments=assignments,
            agent_reports=[provider_output_agent_report],
            **storage_authority,
        )
    assert (
        missing_provider_output_terminal.value.code
        is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH
    )

    provider_output_terminal_sources = (
        "strict budget failure: provider_output_exceeded",
        "strict usage settlement failed: runtime_exceeded",
        "strict usage settlement failed: clock_rollback",
    )
    for terminal_source in provider_output_terminal_sources:
        exact_report = staged_failure_report(
            canonical_provider_output,
            records=[provider_output_record],
            analysis_sections=report.analysis_sections[:1],
        )
        exact_data_quality = [*exact_report.data_quality, terminal_source]
        exact_report = exact_report.model_copy(
            update={
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )
        provenance = build_confirmed_review_provenance(
            admission,
            exact_report,
            assignments=assignments,
            agent_reports=[provider_output_agent_report],
            **storage_authority,
        )
        assert provenance.terminal_status == "failed_with_limitations"

    impossible_usage_terminal_report = staged_failure_report(
        canonical_provider_output,
        records=[provider_output_record],
        analysis_sections=report.analysis_sections[:1],
    )
    exact_data_quality = [
        *impossible_usage_terminal_report.data_quality,
        "strict usage settlement failed: usage_malformed",
    ]
    impossible_usage_terminal_report = impossible_usage_terminal_report.model_copy(
        update={
            "data_quality": exact_data_quality,
            "limitations": [*exact_data_quality, report.limitations[-1]],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as impossible_usage_terminal:
        build_confirmed_review_provenance(
            admission,
            impossible_usage_terminal_report,
            assignments=assignments,
            agent_reports=[provider_output_agent_report],
            **storage_authority,
        )
    assert impossible_usage_terminal.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for conflicting_terminal_sources in (
        provider_output_terminal_sources[:2],
        (
            provider_output_terminal_sources[0],
            provider_output_terminal_sources[2],
        ),
        (
            provider_output_terminal_sources[1],
            provider_output_terminal_sources[2],
        ),
    ):
        conflicting_report = staged_failure_report(
            canonical_provider_output,
            records=[provider_output_record],
            analysis_sections=report.analysis_sections[:1],
        )
        exact_data_quality = [
            *conflicting_report.data_quality,
            *conflicting_terminal_sources,
        ]
        conflicting_report = conflicting_report.model_copy(
            update={
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as conflicting_terminal:
            build_confirmed_review_provenance(
                admission,
                conflicting_report,
                assignments=assignments,
                agent_reports=[provider_output_agent_report],
                **storage_authority,
            )
        assert conflicting_terminal.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for bare_additional_budget in (
        "strict budget failure: runtime_exceeded",
        "strict budget failure: clock_rollback",
        "strict budget failure: usage_malformed",
    ):
        bare_budget_report = staged_failure_report(
            canonical_provider_output,
            records=[provider_output_record],
            analysis_sections=report.analysis_sections[:1],
        )
        exact_data_quality = [
            *bare_budget_report.data_quality,
            provider_output_terminal_sources[0],
            bare_additional_budget,
        ]
        bare_budget_report = bare_budget_report.model_copy(
            update={
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as bare_budget:
            build_confirmed_review_provenance(
                admission,
                bare_budget_report,
                assignments=assignments,
                agent_reports=[provider_output_agent_report],
                **storage_authority,
            )
        assert bare_budget.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for impossible_followup_budgets in (
        ("strict budget failure: usage_malformed",),
        (
            "strict budget failure: runtime_exceeded",
            "strict budget failure: clock_rollback",
        ),
    ):
        impossible_followup_report = staged_failure_report(
            canonical_provider_output,
            records=[provider_output_record],
            analysis_sections=report.analysis_sections[:1],
        )
        exact_data_quality = [
            *impossible_followup_report.data_quality,
            provider_output_terminal_sources[0],
            "maximum runtime exceeded during final synthesis",
            *impossible_followup_budgets,
        ]
        impossible_followup_report = impossible_followup_report.model_copy(
            update={
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as impossible_followup:
            build_confirmed_review_provenance(
                admission,
                impossible_followup_report,
                assignments=assignments,
                agent_reports=[provider_output_agent_report],
                **storage_authority,
            )
        assert impossible_followup.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for missing_followup_budget_stage in (
        "maximum runtime exceeded during final synthesis",
        "maximum runtime exceeded during final artifact writes",
    ):
        missing_followup_budget_report = staged_failure_report(
            canonical_provider_output,
            records=[provider_output_record],
            analysis_sections=report.analysis_sections[:1],
        )
        exact_data_quality = [
            *missing_followup_budget_report.data_quality,
            provider_output_terminal_sources[0],
            missing_followup_budget_stage,
        ]
        missing_followup_budget_report = missing_followup_budget_report.model_copy(
            update={
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as missing_followup_budget:
            build_confirmed_review_provenance(
                admission,
                missing_followup_budget_report,
                assignments=assignments,
                agent_reports=[provider_output_agent_report],
                **storage_authority,
            )
        assert missing_followup_budget.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for followup_runtime_stage, followup_budget in (
        (
            "maximum runtime exceeded during final synthesis",
            "strict budget failure: runtime_exceeded",
        ),
        (
            "maximum runtime exceeded during final artifact writes",
            "strict budget failure: clock_rollback",
        ),
    ):
        followup_runtime_report = staged_failure_report(
            canonical_provider_output,
            records=[provider_output_record],
            analysis_sections=report.analysis_sections[:1],
        )
        exact_data_quality = [
            *followup_runtime_report.data_quality,
            provider_output_terminal_sources[0],
            followup_runtime_stage,
            followup_budget,
        ]
        followup_runtime_report = followup_runtime_report.model_copy(
            update={
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )
        followup_provenance = build_confirmed_review_provenance(
            admission,
            followup_runtime_report,
            assignments=assignments,
            agent_reports=[provider_output_agent_report],
            **storage_authority,
        )
        assert followup_provenance.terminal_status == "failed_with_limitations"

    for strict_usage_terminal, followup_runtime_stage, followup_budget in (
        (
            "strict usage settlement failed: runtime_exceeded",
            "maximum runtime exceeded during final synthesis",
            "strict budget failure: runtime_exceeded",
        ),
        (
            "strict usage settlement failed: clock_rollback",
            "maximum runtime exceeded during final artifact writes",
            "strict budget failure: clock_rollback",
        ),
    ):
        matching_followup_report = staged_failure_report(
            canonical_provider_output,
            records=[provider_output_record],
            analysis_sections=report.analysis_sections[:1],
        )
        exact_data_quality = [
            *matching_followup_report.data_quality,
            strict_usage_terminal,
            followup_runtime_stage,
            followup_budget,
        ]
        matching_followup_report = matching_followup_report.model_copy(
            update={
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )
        matching_followup_provenance = build_confirmed_review_provenance(
            admission,
            matching_followup_report,
            assignments=assignments,
            agent_reports=[provider_output_agent_report],
            **storage_authority,
        )
        assert matching_followup_provenance.terminal_status == "failed_with_limitations"

    for strict_usage_terminal, mismatched_followup_budget in (
        (
            "strict usage settlement failed: runtime_exceeded",
            "strict budget failure: clock_rollback",
        ),
        (
            "strict usage settlement failed: clock_rollback",
            "strict budget failure: runtime_exceeded",
        ),
    ):
        mismatched_followup_report = staged_failure_report(
            canonical_provider_output,
            records=[provider_output_record],
            analysis_sections=report.analysis_sections[:1],
        )
        exact_data_quality = [
            *mismatched_followup_report.data_quality,
            strict_usage_terminal,
            "maximum runtime exceeded during final synthesis",
            mismatched_followup_budget,
        ]
        mismatched_followup_report = mismatched_followup_report.model_copy(
            update={
                "data_quality": exact_data_quality,
                "limitations": [*exact_data_quality, report.limitations[-1]],
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as mismatched_followup:
            build_confirmed_review_provenance(
                admission,
                mismatched_followup_report,
                assignments=assignments,
                agent_reports=[provider_output_agent_report],
                **storage_authority,
            )
        assert mismatched_followup.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    ordered_runtime_report = staged_failure_report("maximum runtime exceeded after tool execution")
    ordered_runtime_data_quality = [
        *ordered_runtime_report.data_quality,
        "strict budget failure: runtime_exceeded",
        "maximum runtime exceeded during final synthesis",
        "maximum runtime exceeded during final artifact writes",
        "maximum runtime exceeded during terminal publication",
    ]
    ordered_runtime_report = ordered_runtime_report.model_copy(
        update={
            "data_quality": ordered_runtime_data_quality,
            "limitations": [*ordered_runtime_data_quality, report.limitations[-1]],
        },
        deep=True,
    )
    ordered_runtime_provenance = build_confirmed_review_provenance(
        admission,
        ordered_runtime_report,
        **provenance_authority,
    )
    assert ordered_runtime_provenance.terminal_status == "failed_with_limitations"

    reversed_followup_data_quality = [
        *ordered_runtime_report.data_quality[:-3],
        "maximum runtime exceeded during final artifact writes",
        "maximum runtime exceeded during final synthesis",
        "maximum runtime exceeded during terminal publication",
    ]
    reversed_followup_report = ordered_runtime_report.model_copy(
        update={
            "data_quality": reversed_followup_data_quality,
            "limitations": [*reversed_followup_data_quality, report.limitations[-1]],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as reversed_followup:
        build_confirmed_review_provenance(
            admission,
            reversed_followup_report,
            **provenance_authority,
        )
    assert reversed_followup.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    continued_after_failure = forged_failure_report(
        status=AgentExecutionStatus.FAILED,
        error="context envelope has expired",
        data_quality=[f"provider {first.agent_role} context expired"],
    )
    with pytest.raises(ConfirmedReviewError) as post_failure_records:
        build_confirmed_review_provenance(
            admission,
            continued_after_failure,
            **provenance_authority,
        )
    assert post_failure_records.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    for impossible_budget_message in (
        "strict usage settlement failed: tool_output_exceeded",
        "strict budget failure: artifact_exceeded",
        "strict usage settlement failed: provider_output_exceeded",
        "strict budget failure: provider_output_exceeded",
    ):
        with pytest.raises(ConfirmedReviewError) as impossible_budget_stage:
            build_confirmed_review_provenance(
                admission,
                staged_failure_report(impossible_budget_message),
                **provenance_authority,
            )
        assert impossible_budget_stage.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH


def test_agent_reports_and_synthesis_projection_are_authoritative(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-agent-report-authority-1",
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    )
    report = orchestrator.run_confirmed_review(admission)
    read = orchestrator.product_store.read_current(report.run_id)
    assignments = [
        AgentAssignment.model_validate(item)
        for item in json.loads(read.payload_bytes("assignments.json"))
    ]
    reports_by_role: dict[str, AgentReport] = {}
    for payload in read.payloads:
        if payload.inventory.logical_name.startswith("agent_reports/"):
            agent_report = AgentReport.model_validate_json(payload.exact_bytes)
            reports_by_role[agent_report.agent_role] = agent_report
    durable_report = FinalReport.model_validate_json(read.payload_bytes("final_report.json"))
    agent_reports = [
        reports_by_role[record.agent_role] for record in durable_report.agent_execution_records
    ]
    storage_authority = {
        "storage_root": orchestrator.product_store.revision_root,
        "storage_revision": read.revision,
        "storage_transaction_id": read.transaction_id,
    }
    durable_provenance = ConfirmedReviewProvenanceV1.model_validate_json(
        read.payload_bytes("confirmed_review_provenance.json")
    )
    replay_admission = replace(admission, admitted_at=durable_provenance.admitted_at)
    rebuilt_provenance = build_confirmed_review_provenance(
        replay_admission,
        durable_report,
        assignments=assignments,
        agent_reports=agent_reports,
        **storage_authority,
    )
    assert rebuilt_provenance.model_dump(
        exclude={"provenance_sha256"}
    ) == durable_provenance.model_dump(exclude={"provenance_sha256"})

    same_run_authorities = {
        result.result_id: canonical_json_bytes(result) for result in durable_report.tool_results
    }
    same_run_provenance = (
        confirmed_review_module._build_confirmed_review_provenance_from_same_run_authority(
            replay_admission,
            durable_report,
            same_run_tool_authorities=same_run_authorities,
            assignments=assignments,
            agent_reports=agent_reports,
            **storage_authority,
        )
    )
    assert same_run_provenance == durable_provenance
    with pytest.raises(ConfirmedReviewError) as missing_same_run_authority:
        confirmed_review_module._build_confirmed_review_provenance_from_same_run_authority(
            replay_admission,
            durable_report,
            same_run_tool_authorities={},
            assignments=assignments,
            agent_reports=agent_reports,
            **storage_authority,
        )
    assert missing_same_run_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH
    forged_same_run_authorities = dict(same_run_authorities)
    forged_same_run_authorities[durable_report.tool_results[0].result_id] = canonical_json_bytes(
        {"forged": True}
    )
    with pytest.raises(ConfirmedReviewError) as forged_same_run_authority:
        confirmed_review_module._build_confirmed_review_provenance_from_same_run_authority(
            replay_admission,
            durable_report,
            same_run_tool_authorities=forged_same_run_authorities,
            assignments=assignments,
            agent_reports=agent_reports,
            **storage_authority,
        )
    assert forged_same_run_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH
    extra_same_run_authorities = {
        **same_run_authorities,
        "tool-result-extra-authority": canonical_json_bytes({"forged": True}),
    }
    with (
        pytest.raises(ConfirmedReviewError) as extra_same_run_authority,
        confirmed_review_module._confirmed_review_same_run_publication_authority(
            replay_admission,
            durable_report,
            extra_same_run_authorities,
        ),
    ):
        raise AssertionError("extra authority must be rejected before publication")
    assert extra_same_run_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    with confirmed_review_module._confirmed_review_same_run_publication_authority(
        replay_admission,
        durable_report,
        same_run_authorities,
    ):
        active = confirmed_review_module._active_confirmed_review_same_run_tool_authorities(
            run_id=durable_report.run_id,
            case=replay_admission.case,
            report=durable_report,
            source_bytes=replay_admission.source_bytes,
            candidate=replay_admission.candidate,
            confirmation=replay_admission.confirmation,
        )
        assert active == same_run_authorities
        with pytest.raises(ConfirmedReviewError) as cross_run_authority:
            confirmed_review_module._active_confirmed_review_same_run_tool_authorities(
                run_id="run-confirmed-cross-run-authority",
                case=replay_admission.case,
                report=durable_report,
            )
        assert cross_run_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH
        tampered_result = durable_report.tool_results[0].model_copy(
            update={
                "output": {
                    **durable_report.tool_results[0].output,
                    "valid": False,
                }
            },
            deep=True,
        )
        tampered_report = durable_report.model_copy(
            update={
                "tool_results": [
                    tampered_result,
                    *durable_report.tool_results[1:],
                ]
            },
            deep=True,
        )
        with pytest.raises(ConfirmedReviewError) as tampered_authority:
            confirmed_review_module._active_confirmed_review_same_run_tool_authorities(
                run_id=durable_report.run_id,
                case=replay_admission.case,
                report=tampered_report,
            )
        assert tampered_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH
        copied_context = copy_context()
        with ThreadPoolExecutor(max_workers=1) as executor:
            crossed_thread = executor.submit(
                copied_context.run,
                confirmed_review_module._active_confirmed_review_same_run_tool_authorities,
                run_id=durable_report.run_id,
                case=replay_admission.case,
                report=durable_report,
            )
            with pytest.raises(ConfirmedReviewError) as crossed_thread_authority:
                crossed_thread.result()
        assert crossed_thread_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH
    assert (
        confirmed_review_module._active_confirmed_review_same_run_tool_authorities(
            run_id=durable_report.run_id,
            case=replay_admission.case,
            report=durable_report,
        )
        is None
    )
    with pytest.raises(ConfirmedReviewError) as expired_copied_authority:
        copied_context.run(
            confirmed_review_module._active_confirmed_review_same_run_tool_authorities,
            run_id=durable_report.run_id,
            case=replay_admission.case,
            report=durable_report,
        )
    assert expired_copied_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    forged_agent_reports = [
        agent_report.model_copy(
            update={"conclusions": ["FACT: Hero has exactly 100% equity."]},
            deep=True,
        )
        for agent_report in agent_reports
    ]
    forged_sections = [
        {
            "title": agent_report.agent_role,
            "epistemic_status": "UNKNOWN",
            "unverified_conclusions": agent_report.conclusions,
            "unverified_claims": [claim.text for claim in agent_report.claims],
            "uncertainties": agent_report.uncertainties,
            "objections": agent_report.objections,
            "unresolved_questions": agent_report.unresolved_questions,
        }
        for agent_report in forged_agent_reports
    ]
    forged_report = report.model_copy(
        update={
            "conclusion": "FACT: Hero has exactly 100% equity and this is proven GTO.",
            "analysis_sections": forged_sections,
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as forged:
        build_confirmed_review_provenance(
            admission,
            forged_report,
            assignments=assignments,
            agent_reports=forged_agent_reports,
            **storage_authority,
        )
    assert forged.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    with pytest.raises(ConfirmedReviewError) as missing_authority:
        build_confirmed_review_provenance(admission, forged_report)
    assert missing_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    runtime_message = "maximum runtime exceeded after provider analysis"
    forged_runtime_report = report.model_copy(
        update={
            "data_quality": [*report.data_quality, runtime_message],
            "limitations": [
                *report.data_quality,
                runtime_message,
                report.limitations[-1],
            ],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as forged_runtime:
        build_confirmed_review_provenance(
            admission,
            forged_runtime_report,
            assignments=assignments,
            agent_reports=agent_reports,
            **storage_authority,
        )
    assert forged_runtime.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    provider_runtime_message = "provider intake context expired"
    completed_records_with_provider_failure = report.model_copy(
        update={
            "run_status": "failed_with_limitations",
            "conclusion": ("実行予算または安全上の制限に達したため、制限付きで終了しました。"),
            "data_quality": [*report.data_quality, provider_runtime_message],
            "limitations": [
                *report.data_quality,
                provider_runtime_message,
                report.limitations[-1],
            ],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as provider_runtime:
        build_confirmed_review_provenance(
            admission,
            completed_records_with_provider_failure,
            assignments=assignments,
            agent_reports=agent_reports,
            **storage_authority,
        )
    assert provider_runtime.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    mismatched_record = report.agent_execution_records[0].model_copy(
        update={
            "status": AgentExecutionStatus.FAILED,
            "error": "provider returned malformed output",
        },
        deep=True,
    )
    mismatched_runtime_report = report.model_copy(
        update={
            "run_status": "failed_with_limitations",
            "conclusion": "実行中の制限に達したため、制限付きで終了しました。",
            "agent_execution_records": [
                mismatched_record,
                *report.agent_execution_records[1:],
            ],
            "data_quality": [*report.data_quality, provider_runtime_message],
            "limitations": [
                *report.data_quality,
                provider_runtime_message,
                report.limitations[-1],
            ],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as mismatched_runtime:
        build_confirmed_review_provenance(
            admission,
            mismatched_runtime_report,
            assignments=assignments,
            agent_reports=agent_reports,
            **storage_authority,
        )
    assert mismatched_runtime.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    silent_record = report.agent_execution_records[0].model_copy(
        update={
            "status": AgentExecutionStatus.FAILED,
            "error": "forged silent provider failure",
        },
        deep=True,
    )
    silent_failure_report = report.model_copy(
        update={
            "run_status": "failed_with_limitations",
            "conclusion": ("実行予算または安全上の制限に達したため、制限付きで終了しました。"),
            "agent_execution_records": [
                silent_record,
                *report.agent_execution_records[1:],
            ],
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as silent_failure:
        build_confirmed_review_provenance(
            admission,
            silent_failure_report,
            assignments=assignments,
            agent_reports=agent_reports,
            **storage_authority,
        )
    assert silent_failure.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    forged_steps = [
        step.replace(report.run_id, "run-confirmed-forged-other")
        for step in report.reproduction_steps
    ]
    assert forged_steps != report.reproduction_steps
    forged_path_report = report.model_copy(
        update={"reproduction_steps": forged_steps},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as forged_path:
        build_confirmed_review_provenance(
            admission,
            forged_path_report,
            assignments=assignments,
            agent_reports=agent_reports,
            **storage_authority,
        )
    assert forged_path.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH

    def mutate_reproduction_paths(mutator):
        mutated: list[str] = []
        for step in report.reproduction_steps:
            prefix = "argv-json: "
            argv = json.loads(step.removeprefix(prefix))
            argv[-1] = mutator(argv[-1])
            mutated.append(prefix + json.dumps(argv, ensure_ascii=False))
        return mutated

    forged_transaction_report = report.model_copy(
        update={
            "reproduction_steps": mutate_reproduction_paths(
                lambda path: path.replace(read.transaction_id, f"txn-{'a' * 32}")
            )
        },
        deep=True,
    )
    forged_root_report = report.model_copy(
        update={
            "reproduction_steps": mutate_reproduction_paths(
                lambda path: (
                    "C:/forged-root/runs/" + path.replace("\\", "/").split("/runs/", maxsplit=1)[1]
                )
            )
        },
        deep=True,
    )
    for forged_authority_report in (forged_transaction_report, forged_root_report):
        with pytest.raises(ConfirmedReviewError) as forged_authority:
            build_confirmed_review_provenance(
                admission,
                forged_authority_report,
                assignments=assignments,
                agent_reports=agent_reports,
                **storage_authority,
            )
        assert forged_authority.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH


def test_fresh_read_after_confirmed_publication_hard_replays_tools(
    tmp_path,
    monkeypatch,
) -> None:
    hand, _definition = versioned_range_hand()
    payload = base_candidate_payload(intake_id="intake-confirmed-fresh-read-replay-1")
    payload["hand"] = hand.model_dump(mode="json")
    admission = confirmed_admission(
        run_id="run-confirmed-fresh-read-replay-1",
        payload=payload,
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    )
    original_default_registry = confirmed_review_module.default_registry
    original_range_plan_validator = confirmed_review_module.validate_versioned_range
    original_tool_registry_factory = tool_registry_module.default_registry
    active_range_plan_count = 0

    def forbidden_publication_replay(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("active same-run publication must not hard-replay tools")

    def admission_only_range_validation(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal active_range_plan_count
        active_range_plan_count += 1
        if active_range_plan_count > 1:
            raise AssertionError("active publication must not repeat range admission validation")
        return original_range_plan_validator(*args, **kwargs)

    monkeypatch.setattr(
        confirmed_review_module,
        "default_registry",
        forbidden_publication_replay,
    )
    monkeypatch.setattr(
        confirmed_review_module,
        "validate_versioned_range",
        admission_only_range_validation,
    )
    monkeypatch.setattr(
        tool_registry_module,
        "default_registry",
        forbidden_publication_replay,
    )
    report = orchestrator.run_confirmed_review(admission)
    assert active_range_plan_count == 1
    monkeypatch.setattr(
        confirmed_review_module,
        "default_registry",
        original_default_registry,
    )
    monkeypatch.setattr(
        confirmed_review_module,
        "validate_versioned_range",
        original_range_plan_validator,
    )
    monkeypatch.setattr(
        tool_registry_module,
        "default_registry",
        original_tool_registry_factory,
    )
    replay_count = 0
    range_plan_replay_count = 0
    range_replay_count = 0

    def counted_default_registry(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal replay_count
        replay_count += 1
        return original_default_registry(*args, **kwargs)

    def counted_range_registry(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal range_replay_count
        range_replay_count += 1
        return original_tool_registry_factory(*args, **kwargs)

    def counted_range_plan(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal range_plan_replay_count
        range_plan_replay_count += 1
        return original_range_plan_validator(*args, **kwargs)

    monkeypatch.setattr(
        confirmed_review_module,
        "default_registry",
        counted_default_registry,
    )
    monkeypatch.setattr(
        confirmed_review_module,
        "validate_versioned_range",
        counted_range_plan,
    )
    monkeypatch.setattr(
        tool_registry_module,
        "default_registry",
        counted_range_registry,
    )

    read = orchestrator.product_store.read_current(report.run_id)

    assert read.read_status is RunReadStatus.SUCCEEDED
    assert replay_count >= 1
    assert range_plan_replay_count >= 1
    assert range_replay_count >= 1


def test_user_claim_wording_cannot_create_a_calculated_correction(tmp_path) -> None:
    payload = base_candidate_payload(intake_id="intake-confirmed-user-wording-1")
    payload["claims"][0]["text"] = (
        "USER_CLAIM contains 訂正が必要 as an unverified phrase; "
        "no correction calculation is supplied."
    )
    admission = confirmed_admission(
        run_id="run-confirmed-user-wording-1",
        payload=payload,
        now=datetime.now(UTC),
    )
    report = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    ).run_confirmed_review(admission)

    assert [result.tool_name for result in report.tool_results] == ["hand_validator"]
    assert "再現可能なローカル計算に基づく訂正" not in report.conclusion


def test_all_final_report_authority_fields_are_fail_closed(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-full-report-authority-1",
        now=datetime.now(UTC),
    )
    report = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    ).run_confirmed_review(admission)
    mutations = (
        {"confidence": ConfidenceGrade.A},
        {"limitations": ["GTO is proven exactly."]},
        {"alternatives": ["Guaranteed winning shove."]},
        {"sensitivity": [{"equity": 1.0}]},
        {"reproduction_steps": ["solver proof complete"]},
        {"evidence": [{"forged": "evidence"}]},
        {
            "data_quality": [*report.data_quality, "GTO is proven exactly."],
            "limitations": [
                *report.data_quality,
                "GTO is proven exactly.",
                report.limitations[-1],
            ],
        },
    )
    for update in mutations:
        forged = report.model_copy(update=update, deep=True)
        with pytest.raises(ConfirmedReviewError) as captured:
            build_confirmed_review_provenance(admission, forged)
        assert captured.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH


def test_tiny_runtime_confirmed_review_returns_ephemeral_fail_closed_terminal(
    tmp_path,
) -> None:
    config = app_config(tmp_path)
    config.budgets = BudgetConfig(max_runtime_seconds=0.000_000_001)
    orchestrator = Orchestrator(config, provider=LocalProvider())
    admission = confirmed_admission(
        run_id="run-confirmed-tiny-runtime",
        now=datetime.now(UTC),
    )
    report = orchestrator.run_confirmed_review(admission)

    assert report.run_status == "failed_with_limitations"
    assert any(
        item.startswith(("strict usage settlement failed: ", "strict budget failure: "))
        for item in report.data_quality
    )
    assert any(
        item
        in {
            "maximum runtime exceeded during terminal publication",
            "product persistence refused: tool result lacks independent replay authority",
        }
        for item in report.data_quality
    )
    assert report.limitations[: len(report.data_quality)] == report.data_quality
    assert report.confidence is ConfidenceGrade.D
    with pytest.raises(ProductRunError) as current:
        orchestrator.product_store.read_current(report.run_id)
    assert current.value.failure.code is ProductRunFailureCode.RUN_NOT_FOUND


def test_invalid_hand_stops_before_agent_analysis(tmp_path) -> None:
    payload = base_candidate_payload(intake_id="intake-confirmed-invalid-hand-1")
    payload["hand"]["hero_cards"] = ["As", "As"]
    admission = confirmed_admission(
        run_id="run-confirmed-invalid-hand-1",
        payload=payload,
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    )

    with pytest.raises(ConfirmedReviewError) as invalid:
        orchestrator.run_confirmed_review(admission)

    assert invalid.value.code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA
    events = orchestrator._run_machines[admission.confirmation.run_id].snapshot()["events"]
    assert all(
        event["target"] not in {RunState.TASK_ROUTING.value, RunState.INDEPENDENT_ANALYSIS.value}
        for event in events
    )
    assert admission.confirmation.run_id not in orchestrator._phase_tool_publication_authorities


def test_exact_idempotent_replay_is_read_only_and_conflict_fails(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-replay-1",
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    )
    first = orchestrator.run_confirmed_review(admission)
    first_read = orchestrator.product_store.read_current(first.run_id)
    replay = orchestrator.run_confirmed_review(admission)
    replay_read = orchestrator.product_store.read_current(first.run_id)
    assert replay == first
    assert replay_read.revision == first_read.revision
    assert replay_read.current_pointer_sha256 == first_read.current_pointer_sha256

    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-test-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    different_confirmation = create_review_confirmation(
        admission.candidate,
        run_id=admission.confirmation.run_id,
        confirmation_id="confirmation-conflict-valid",
        idempotency_key="different-idempotency-key",
        authority=authority,
        expected_source_sha256=admission.candidate.projection.source.content_sha256,
        expected_candidate_sha256=admission.candidate.candidate_sha256,
        confirmed_at=admission.admitted_at,
    )
    conflicting = admit_confirmed_review(
        admission.source_bytes,
        admission.candidate,
        different_confirmation,
    )
    with pytest.raises(ConfirmedReviewError) as captured:
        orchestrator.run_confirmed_review(conflicting)
    assert captured.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_REPLAY


def test_forged_confirmed_metadata_and_injected_runtime_are_rejected(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-runtime-1",
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(app_config(tmp_path))
    with pytest.raises(ConfirmedReviewError) as missing:
        orchestrator.run(admission.case, run_id="run-forged-confirmed-1")
    assert missing.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_MISSING
    assert orchestrator._namespace_kind("run-forged-confirmed-1") is None

    class LocalProviderSubclass(LocalProvider):
        pass

    injected = Orchestrator(
        app_config(tmp_path / "injected"),
        provider=LocalProviderSubclass(),
    )
    with pytest.raises(ConfirmedReviewError) as runtime:
        injected.run_confirmed_review(admission)
    assert runtime.value.code is ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER
    assert injected._namespace_kind(admission.confirmation.run_id) is None

    over_permissive_config = app_config(tmp_path / "over-permissive")
    over_permissive_config.budgets = BudgetConfig(
        max_output_bytes=1_000_001,
        max_run_bytes=10_000_001,
    )
    over_permissive = Orchestrator(
        over_permissive_config,
        provider=LocalProvider(),
    )
    with pytest.raises(ConfirmedReviewError) as runtime_budget:
        over_permissive.run_confirmed_review(admission)
    assert runtime_budget.value.code is ConfirmedReviewDiagnosticCode.RUNTIME_BUDGET
    assert over_permissive._namespace_kind(admission.confirmation.run_id) is None

    injected_clock = Orchestrator(
        app_config(tmp_path / "clock-injected"),
        provider=LocalProvider(),
        context_clock=lambda: datetime.now(UTC),
    )
    with pytest.raises(ConfirmedReviewError) as clock_runtime:
        injected_clock.run_confirmed_review(admission)
    assert clock_runtime.value.code is ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER
    assert injected_clock._namespace_kind(admission.confirmation.run_id) is None


def test_runtime_dependency_mutation_and_historical_admission_are_rejected(tmp_path) -> None:
    admission = confirmed_admission(
        run_id="run-confirmed-runtime-mutation-1",
        now=datetime.now(UTC),
    )

    class LocalProviderSubclass(LocalProvider):
        pass

    provider_mutated = Orchestrator(
        app_config(tmp_path / "provider-mutated"),
        provider=LocalProvider(),
    )
    provider_mutated.analysis_executor.provider = LocalProviderSubclass()
    with pytest.raises(ConfirmedReviewError) as provider_error:
        provider_mutated.run_confirmed_review(admission)
    assert provider_error.value.code is ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER

    registry_mutated = Orchestrator(
        app_config(tmp_path / "registry-mutated"),
        provider=LocalProvider(),
    )
    registry_mutated.tool_research_executor.registry = default_registry()
    with pytest.raises(ConfirmedReviewError) as registry_error:
        registry_mutated.run_confirmed_review(admission)
    assert registry_error.value.code is ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER

    authority = ReviewConfirmationAuthorityV1(
        authority_id="local-test-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    historical_time = datetime.now(UTC) - timedelta(days=2)
    expired_confirmation = create_review_confirmation(
        admission.candidate,
        run_id=admission.confirmation.run_id,
        confirmation_id="confirmation-historical-expired",
        idempotency_key="idempotency-historical-expired",
        authority=authority,
        expected_source_sha256=admission.candidate.projection.source.content_sha256,
        expected_candidate_sha256=admission.candidate.candidate_sha256,
        confirmed_at=historical_time,
        expires_at=historical_time + timedelta(hours=1),
    )
    forged_historical_admission = replace(
        admission,
        confirmation=expired_confirmation,
        admitted_at=historical_time + timedelta(minutes=1),
    )
    current_clock = Orchestrator(
        app_config(tmp_path / "historical"),
        provider=LocalProvider(),
    )
    with pytest.raises(ConfirmedReviewError) as expired:
        current_clock.run_confirmed_review(forged_historical_admission)
    assert expired.value.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_EXPIRED
    assert current_clock._namespace_kind(admission.confirmation.run_id) is None


def test_runtime_callable_and_tool_function_mutation_are_rejected(
    tmp_path,
    monkeypatch,
) -> None:
    def assert_runtime_rejected(label, mutate) -> None:
        admission = confirmed_admission(
            run_id=f"run-confirmed-callable-{label}",
            now=datetime.now(UTC),
        )
        orchestrator = Orchestrator(
            app_config(tmp_path / label),
            provider=LocalProvider(),
        )
        cleanup = mutate(orchestrator)
        try:
            with pytest.raises(ConfirmedReviewError) as runtime:
                orchestrator.run_confirmed_review(admission)
            assert runtime.value.code is ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER
            assert orchestrator._namespace_kind(admission.confirmation.run_id) is None
        finally:
            if cleanup is not None:
                cleanup()

    def shadow_provider(orchestrator) -> None:
        original = orchestrator.provider.analyze
        orchestrator.provider.analyze = lambda context, assignment, control: original(
            context, assignment, control
        )

    def shadow_analysis_executor(orchestrator) -> None:
        original = orchestrator.analysis_executor.run
        orchestrator.analysis_executor.run = lambda request: original(request)

    def shadow_registry_execute(orchestrator) -> None:
        original = orchestrator.registry.execute
        orchestrator.registry.execute = lambda *args, **kwargs: original(*args, **kwargs)

    def shadow_registry_isolated_dispatch(orchestrator) -> None:
        original = orchestrator.registry._execute_isolated
        orchestrator.registry._execute_isolated = lambda *args, **kwargs: original(
            *args,
            **kwargs,
        )

    def replace_registry_impl(orchestrator):
        del orchestrator
        original = ToolRegistry._execute_impl

        def replacement(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return original(self, *args, **kwargs)

        ToolRegistry._execute_impl = replacement
        return lambda: setattr(ToolRegistry, "_execute_impl", original)

    def replace_tool_function(orchestrator) -> None:
        definition = orchestrator.registry._tools["hand_validator"]
        orchestrator.registry._tools["hand_validator"] = replace(
            definition,
            function=lambda payload: definition.function(payload),
        )

    def replace_registry_clock(orchestrator) -> None:
        class InjectedClock:
            def now_ns(self) -> int:
                raise AssertionError("injected registry clock must not execute")

        orchestrator.registry.monotonic_clock = InjectedClock()

    def replace_registry_mapping(orchestrator) -> None:
        original_mapping = orchestrator.registry._tools
        original = original_mapping["hand_validator"]
        replacement = replace(
            original,
            function=lambda payload: original.function(payload),
        )

        class SplitLookupDict(dict):
            def items(self):
                return original_mapping.items()

            def get(self, key, default=None):
                if key == "hand_validator":
                    return replacement
                return original_mapping.get(key, default)

        orchestrator.registry._tools = SplitLookupDict(original_mapping)

    def shadow_synthesis_service(orchestrator) -> None:
        original = orchestrator.synthesis_service.run
        orchestrator.synthesis_service.run = lambda request: original(request)

    def shadow_product_publish(orchestrator) -> None:
        original = orchestrator.product_store.publish
        orchestrator.product_store.publish = lambda request: original(request)

    def shadow_budget_reserve(orchestrator) -> None:
        original = orchestrator.durable_budget.reserve
        orchestrator.durable_budget.reserve = lambda request, **kwargs: original(
            request,
            **kwargs,
        )

    def shadow_buffer_write(orchestrator) -> None:
        original = orchestrator.store.write_json
        orchestrator.store.write_json = lambda run_id, relative, value: original(
            run_id,
            relative,
            value,
        )

    def replace_terminal_clock(orchestrator) -> None:
        orchestrator.terminal_clock = lambda: datetime.now(UTC)

    def replace_terminal_id_factory(orchestrator) -> None:
        orchestrator.terminal_id_factory = lambda prefix: f"{prefix}-forged"

    def shadow_product_prepare(orchestrator) -> None:
        orchestrator.product_store._prepare = lambda *args, **kwargs: None

    def shadow_buffer_internal_write(orchestrator) -> None:
        orchestrator.store._write = lambda *args, **kwargs: None

    def shadow_revision_publish(orchestrator) -> None:
        orchestrator.durable_budget_store.revisions.publish = lambda *args, **kwargs: None

    def replace_persistence_roots(orchestrator) -> None:
        replacement = tmp_path / "replacement-budget-root"
        orchestrator.durable_budget_runs_root = replacement
        orchestrator.durable_budget_store.revisions.revision_root = replacement

    def replace_buffer_root(orchestrator) -> None:
        orchestrator.store.root = tmp_path / "replacement-buffer-root"

    def replace_terminal_clock_code(orchestrator):
        original = orchestrator.terminal_clock
        original_code = original.__code__

        def replacement():
            return datetime.now(UTC)

        original.__code__ = replacement.__code__
        return lambda: setattr(original, "__code__", original_code)

    def replace_provider_code(orchestrator):
        del orchestrator
        original = LocalProvider.analyze
        original_code = original.__code__

        def replacement(self, *args, **kwargs):
            del self, args, kwargs
            return None

        original.__code__ = replacement.__code__
        return lambda: setattr(original, "__code__", original_code)

    def replace_module_commitments_code(orchestrator):
        del orchestrator
        original = orchestrator_module.product_payload_commitments
        original_code = original.__code__

        def replacement(*args, **kwargs):
            del args, kwargs
            return None

        original.__code__ = replacement.__code__
        return lambda: setattr(original, "__code__", original_code)

    def replace_module_commitments(orchestrator) -> None:
        del orchestrator
        original = orchestrator_module.product_payload_commitments
        monkeypatch.setattr(
            orchestrator_module,
            "product_payload_commitments",
            lambda *args, **kwargs: original(*args, **kwargs),
        )

    assert_runtime_rejected("provider", shadow_provider)
    assert_runtime_rejected("analysis-executor", shadow_analysis_executor)
    assert_runtime_rejected("registry-execute", shadow_registry_execute)
    assert_runtime_rejected("registry-isolated-dispatch", shadow_registry_isolated_dispatch)
    assert_runtime_rejected("registry-impl-class", replace_registry_impl)
    assert_runtime_rejected("tool-function", replace_tool_function)
    assert_runtime_rejected("registry-clock", replace_registry_clock)
    assert_runtime_rejected("registry-mapping", replace_registry_mapping)
    assert_runtime_rejected("synthesis-service", shadow_synthesis_service)
    assert_runtime_rejected("product-publish", shadow_product_publish)
    assert_runtime_rejected("budget-reserve", shadow_budget_reserve)
    assert_runtime_rejected("buffer-write", shadow_buffer_write)
    assert_runtime_rejected("terminal-clock", replace_terminal_clock)
    assert_runtime_rejected("terminal-id-factory", replace_terminal_id_factory)
    assert_runtime_rejected("product-prepare", shadow_product_prepare)
    assert_runtime_rejected("buffer-internal-write", shadow_buffer_internal_write)
    assert_runtime_rejected("revision-publish", shadow_revision_publish)
    assert_runtime_rejected("persistence-roots", replace_persistence_roots)
    assert_runtime_rejected("buffer-root", replace_buffer_root)
    assert_runtime_rejected("terminal-clock-code", replace_terminal_clock_code)
    assert_runtime_rejected("provider-code", replace_provider_code)
    assert_runtime_rejected("module-commitments-code", replace_module_commitments_code)
    assert_runtime_rejected("module-commitments", replace_module_commitments)


def test_one_versioned_range_runs_only_validation_then_combos(tmp_path) -> None:
    hand, _definition = versioned_range_hand()
    payload = base_candidate_payload(intake_id="intake-confirmed-range-1")
    payload["hand"] = hand.model_dump(mode="json")
    admission = confirmed_admission(
        run_id="run-confirmed-range-1",
        payload=payload,
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    )
    report = orchestrator.run_confirmed_review(admission)
    assert [item.tool_name for item in report.tool_results] == [
        "hand_validator",
        "range_validate",
        "combos",
    ]
    duplicated_id = report.tool_results[0].result_id
    forged_results = [
        result.model_copy(update={"result_id": duplicated_id}, deep=True)
        for result in report.tool_results
    ]
    forged_report = report.model_copy(
        update={"tool_results": forged_results},
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as duplicate:
        build_confirmed_review_provenance(admission, forged_report)
    assert duplicate.value.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH
    reordered_report = report.model_copy(
        update={
            "tool_results": [
                report.tool_results[0],
                report.tool_results[2],
                report.tool_results[1],
            ]
        },
        deep=True,
    )
    with pytest.raises(ConfirmedReviewError) as reordered:
        build_confirmed_review_provenance(admission, reordered_report)
    assert reordered.value.code is ConfirmedReviewDiagnosticCode.CANDIDATE_TOOL


def test_supported_ledger_profile_is_the_only_optional_ledger_path(tmp_path) -> None:
    payload = base_candidate_payload(intake_id="intake-confirmed-ledger-1")
    payload["hand"]["rake"] = 0
    payload["ledger_profile"] = {
        "schema_version": "1.0.0",
        "profile_id": "generic_nlhe_cash_no_rake_v1",
        "profile_version": "1.0.0",
        "supported_site": "none",
        "chip_unit": "1",
    }
    admission = confirmed_admission(
        run_id="run-confirmed-ledger-1",
        payload=payload,
        now=datetime.now(UTC),
    )
    orchestrator = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    )
    report = orchestrator.run_confirmed_review(admission)
    assert [item.tool_name for item in report.tool_results] == [
        "hand_validator",
        "hand_pot_ledger",
    ]


def test_versioned_range_and_ledger_follow_the_runtime_tool_order(tmp_path) -> None:
    payload = base_candidate_payload(intake_id="intake-confirmed-range-ledger-1")
    payload["hand"]["rake"] = 0
    base_hand = CanonicalHand.model_validate(payload["hand"])
    _hand, definition = versioned_range_hand()
    game_conditions = definition.game_conditions.model_copy(
        update={
            "action_prefix_sha256": action_prefix_sha256(base_hand, 2),
        },
        deep=True,
    )
    definition = definition.model_copy(
        update={"game_conditions": game_conditions},
        deep=True,
    )
    payload["hand"]["known_ranges"] = [definition.model_dump(mode="json")]
    payload["ledger_profile"] = {
        "schema_version": "1.0.0",
        "profile_id": "generic_nlhe_cash_no_rake_v1",
        "profile_version": "1.0.0",
        "supported_site": "none",
        "chip_unit": "1",
    }
    admission = confirmed_admission(
        run_id="run-confirmed-range-ledger-1",
        payload=payload,
        now=datetime.now(UTC),
    )
    report = Orchestrator(
        app_config(tmp_path),
        provider=LocalProvider(),
    ).run_confirmed_review(admission)
    assert report.run_status == "completed"
    assert [item.tool_name for item in report.tool_results] == [
        "hand_validator",
        "range_validate",
        "hand_pot_ledger",
        "combos",
    ]
