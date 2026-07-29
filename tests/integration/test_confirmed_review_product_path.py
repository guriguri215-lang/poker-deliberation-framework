from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from poker_deliberation.config import BudgetConfig
from poker_deliberation.confirmed_review import (
    ConfirmedReviewError,
    admit_confirmed_review,
    create_review_confirmation,
)
from poker_deliberation.confirmed_review_models import (
    ConfirmedReviewDiagnosticCode,
    ConfirmedReviewProvenanceV1,
    ReviewConfirmationAuthorityV1,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
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
