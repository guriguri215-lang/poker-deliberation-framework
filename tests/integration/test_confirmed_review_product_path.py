from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

import poker_deliberation.orchestrator as orchestrator_module
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
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.schemas import ConfidenceGrade, EpistemicLabel
from poker_deliberation.storage.revision_canonical import parse_canonical_model
from poker_deliberation.storage.terminal_models import RunReadStatus
from poker_deliberation.tools import default_registry
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
