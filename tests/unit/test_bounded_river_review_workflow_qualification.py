from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from poker_deliberation import bounded_river_review_workflow_qualification as qualification
from poker_deliberation.bounded_river_review_workflow_qualification import (
    QUALIFICATION_CONFIRMATION_FIELDS_HASH_DOMAIN,
    QUALIFICATION_LIMITATIONS,
    QUALIFICATION_MANIFEST_HASH_DOMAIN,
    BoundedRiverReviewWorkflowQualificationError,
    SanitizedBoundedRiverReviewWorkflowConfirmationHashesV1,
    SanitizedBoundedRiverReviewWorkflowEvaluationEvidenceV1,
    SanitizedBoundedRiverReviewWorkflowLineageV1,
    SanitizedBoundedRiverReviewWorkflowQualificationManifestV1,
    SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1,
    SanitizedBoundedRiverReviewWorkflowSourceIdentityV1,
    SanitizedBoundedRiverReviewWorkflowTerminalEvidenceV1,
    load_sanitized_bounded_river_review_workflow_qualification_manifest,
    write_sanitized_bounded_river_review_workflow_qualification_manifest,
)
from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    domain_sha256,
)
from poker_deliberation.codex_bridge.identity import (
    BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
    BridgeRuntimeSourceFile,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BridgeEffectState,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.qualification import SanitizedRuntimeSourceFileV1
from tests.bounded_river_call_ev_support import river_source


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


def _strict_manifest() -> SanitizedBoundedRiverReviewWorkflowQualificationManifestV1:
    source_manifest = _sha("source-manifest")
    source_inventory = _sha("source-inventory")
    bridge_manifest = _sha("bridge-manifest")
    bridge_inventory = _sha("bridge-inventory")
    final_report = _sha("final-report")
    source_fixture_sha256 = _sha("source")
    range_fixture_sha256 = _sha("range-fixture-file")
    range_definition_sha256 = _sha("range-definition-fixture")
    source_projection_sha256 = _sha("source-projection")
    range_definition_projection_sha256 = _sha("range-definition-projection")
    fixture_sha256 = _sha("fixture")
    runtime_inventory = (
        SanitizedRuntimeSourceFileV1(
            path="tests/fixtures/bounded_river_review_workflow/v1/range.json",
            size=1,
            sha256=range_fixture_sha256,
        ),
        SanitizedRuntimeSourceFileV1(
            path="tests/fixtures/bounded_river_review_workflow/v1/source-ja.txt",
            size=1,
            sha256=source_fixture_sha256,
        ),
        SanitizedRuntimeSourceFileV1(
            path="tests/fixtures/bounded_river_review_workflow/v2/scenarios.json",
            size=1,
            sha256=fixture_sha256,
        ),
    )
    runtime_inventory_sha256 = domain_sha256(
        BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
        [item.model_dump(mode="json") for item in runtime_inventory],
    )
    source_identity = SanitizedBoundedRiverReviewWorkflowSourceIdentityV1(
        source_run_id="source-run",
        source_sha256=source_fixture_sha256,
        source_terminal_revision=1,
        source_terminal_revision_root_sha256=_sha("source-revision-root"),
        source_terminal_pointer_sha256=_sha("source-pointer"),
        source_terminal_manifest_sha256=source_manifest,
        source_terminal_inventory_sha256=source_inventory,
        source_terminal_completion_marker_sha256=_sha("source-marker"),
        source_candidate_sha256=_sha("candidate"),
        source_binding_sha256=_sha("source-binding"),
        source_result_sha256=_sha("source-result"),
        source_provenance_sha256=_sha("source-provenance"),
        source_context_sha256=_sha("source-context"),
    )
    confirmation_hashes = SanitizedBoundedRiverReviewWorkflowConfirmationHashesV1(
        source_sha256=source_projection_sha256,
        bounded_candidate_sha256=_sha("bounded-candidate"),
        source_bindings_sha256=_sha("source-bindings"),
        focal_sha256=_sha("focal"),
        extractor_sha256=_sha("extractor"),
        tool_plan_sha256=_sha("tool-plan"),
        range_definition_sha256=range_definition_projection_sha256,
        range_target_sha256=_sha("range-target"),
        range_binding_sha256=_sha("range-binding"),
        equity_model_sha256=_sha("equity-model"),
        call_ev_model_sha256=_sha("call-ev-model"),
        candidate_sha256=source_identity.source_candidate_sha256,
    )
    lineage = SanitizedBoundedRiverReviewWorkflowLineageV1(
        plan_sha256=_sha("plan"),
        workflow_confirmation_sha256=_sha("workflow-confirmation"),
        linkage_sha256=_sha("linkage"),
        linked_source_terminal_manifest_sha256=source_manifest,
        linked_source_terminal_inventory_sha256=source_inventory,
        linked_bridge_manifest_sha256=_sha("linked-bridge-manifest"),
        linked_bridge_inventory_sha256=_sha("linked-bridge-inventory"),
        current_bridge_revision=17,
        current_bridge_manifest_sha256=bridge_manifest,
        current_bridge_inventory_sha256=bridge_inventory,
        current_bridge_pointer_sha256=_sha("bridge-pointer"),
        current_bridge_previous_manifest_sha256=_sha("bridge-parent-manifest"),
        current_bridge_expected_pointer_sha256=_sha("bridge-parent-pointer"),
        current_bridge_completion_marker_sha256=_sha("bridge-marker"),
    )
    roles = tuple(
        SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1(
            role=role,
            role_ordinal=ordinal,
            workflow_role_confirmation_binding_sha256=_sha(f"binding-{role.value}"),
            workflow_role_confirmation_receipt_sha256=_sha(f"receipt-{role.value}"),
            confirmation_field_count=17,
            confirmation_fields_sha256=domain_sha256(
                QUALIFICATION_CONFIRMATION_FIELDS_HASH_DOMAIN,
                {f"expected_field_{index}": _sha(f"{role.value}-{index}") for index in range(17)},
            ),
            request_sha256=_sha(f"request-{role.value}"),
            request_bytes_sha256=_sha(f"request-bytes-{role.value}"),
            envelope_sha256=_sha(f"envelope-{role.value}"),
            runtime_policy_sha256=_sha(f"runtime-policy-{role.value}"),
            confirmation_sha256=_sha(f"confirmation-{role.value}"),
            admission_sha256=_sha(f"admission-{role.value}"),
            result_sha256=_sha(f"result-{role.value}"),
            execution_audit_sha256=_sha(f"audit-{role.value}"),
            preview_bridge_revision=ordinal * 3 + 2,
            preview_bridge_manifest_sha256=_sha(f"preview-manifest-{role.value}"),
            preview_bridge_inventory_sha256=_sha(f"preview-inventory-{role.value}"),
            preview_bridge_pointer_sha256=_sha(f"preview-pointer-{role.value}"),
            confirmed_bridge_revision=ordinal * 3 + 3,
            confirmed_bridge_manifest_sha256=_sha(f"confirmed-manifest-{role.value}"),
            confirmed_bridge_inventory_sha256=_sha(f"confirmed-inventory-{role.value}"),
            confirmed_bridge_pointer_sha256=_sha(f"confirmed-pointer-{role.value}"),
            effect_state=BridgeEffectState.SUCCEEDED,
            transport_qualification="deterministic_fixture",
            live_execution_evidence_sha256=None,
        )
        for ordinal, role in enumerate(BRIDGE_ROLE_ORDER)
    )
    terminal = SanitizedBoundedRiverReviewWorkflowTerminalEvidenceV1(
        workflow_state="completed",
        bridge_status="succeeded",
        completed_roles=BRIDGE_ROLE_ORDER,
        pending_role_count=0,
        reconciliation_required=False,
        total_input_tokens=5,
        total_output_tokens=5,
        total_estimated_cost_micro_usd=None,
        bridge_replay_sha256=_sha("bridge-replay"),
        workflow_status_sha256=_sha("workflow-status"),
        workflow_replay_sha256=_sha("workflow-status"),
        report_view_sha256=_sha("report-view"),
        final_report_artifact_sha256=final_report,
    )
    confirmation_hash_values = list(confirmation_hashes.model_dump(mode="json").values())
    role_receipts_payload = {
        "field_hashes": [item.confirmation_fields_sha256 for item in roles],
        "receipt_hashes": [item.workflow_role_confirmation_receipt_sha256 for item in roles],
    }
    p2_artifact_hashes = [
        artifact_sha256
        for item in roles
        for artifact_sha256 in (
            item.request_sha256,
            item.confirmation_sha256,
            item.admission_sha256,
            item.result_sha256,
            item.execution_audit_sha256,
        )
    ]
    terminal_payload = {
        "workflow_status_sha256": terminal.workflow_status_sha256,
        "workflow_replay_sha256": terminal.workflow_replay_sha256,
        "bridge_manifest_sha256": bridge_manifest,
        "bridge_inventory_sha256": bridge_inventory,
        "report_sha256": terminal.report_view_sha256,
    }
    confirmation_hashes_sha256 = domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-confirmation-v2",
        confirmation_hash_values,
    )
    role_confirmation_receipts_sha256 = domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-receipts-v2",
        role_receipts_payload,
    )
    p2_artifact_lineage_sha256 = domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-p2-lineage-v2",
        p2_artifact_hashes,
    )
    terminal_replay_report_sha256 = domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-terminal-v2",
        terminal_payload,
    )
    evaluation_evidence = (
        (fixture_sha256, lineage.plan_sha256, source_manifest),
        (confirmation_hashes_sha256, lineage.workflow_confirmation_sha256),
        (
            role_confirmation_receipts_sha256,
            *(item.confirmation_fields_sha256 for item in roles),
            domain_sha256(
                "poker-bounded-river-review-workflow-evaluation-claim-token-v1",
                "exact-seventeen-field-contract-and-production-mismatch-refused",
            ),
        ),
        (p2_artifact_lineage_sha256,),
        (terminal_replay_report_sha256, final_report),
        (runtime_inventory_sha256, runtime_inventory_sha256),
    )
    evaluation = SanitizedBoundedRiverReviewWorkflowEvaluationEvidenceV1(
        schema_version="2.0.0",
        evaluation_id="p3-030g-bounded-river-review-workflow-evaluation-v2",
        fixture_id="p3-030g-bounded-river-review-workflow-v2",
        fixture_sha256=fixture_sha256,
        source_fixture_sha256=source_fixture_sha256,
        range_fixture_sha256=range_fixture_sha256,
        range_definition_sha256=range_definition_sha256,
        source_projection_sha256=source_projection_sha256,
        range_definition_projection_sha256=range_definition_projection_sha256,
        result_sha256=_sha("evaluation-result"),
        source_commit_id="1" * 40,
        source_tree_id="2" * 40,
        workflow_id="workflow-v2",
        plan_sha256=lineage.plan_sha256,
        workflow_confirmation_sha256=lineage.workflow_confirmation_sha256,
        linkage_sha256=lineage.linkage_sha256,
        source_terminal_manifest_sha256=source_manifest,
        source_terminal_inventory_sha256=source_inventory,
        bridge_terminal_manifest_sha256=bridge_manifest,
        bridge_terminal_inventory_sha256=bridge_inventory,
        final_report_artifact_sha256=final_report,
        confirmation_hashes_sha256=confirmation_hashes_sha256,
        role_confirmation_receipts_sha256=role_confirmation_receipts_sha256,
        role_confirmation_fields_sha256=tuple(item.confirmation_fields_sha256 for item in roles),
        all_confirmation_field_mutations_sha256=domain_sha256(
            "poker-bounded-river-review-workflow-evaluation-claim-token-v1",
            "exact-seventeen-field-contract-and-production-mismatch-refused",
        ),
        p2_artifact_lineage_sha256=p2_artifact_lineage_sha256,
        terminal_replay_report_sha256=terminal_replay_report_sha256,
        runtime_source_inventory_sha256=runtime_inventory_sha256,
        case_ids=(
            "source-workflow-identity",
            "workflow-confirmation-binding",
            "five-role-supervision",
            "p2-artifact-lineage",
            "terminal-replay-report",
            "repository-runtime-identity",
        ),
        metric_ids=(
            "source_workflow_identity",
            "workflow_confirmation_binding",
            "five_role_supervision",
            "p2_artifact_lineage",
            "terminal_replay_report",
            "repository_runtime_identity",
        ),
        case_evidence_sha256=evaluation_evidence,
        metric_evidence_sha256=evaluation_evidence,
        case_count=6,
        passed_case_count=6,
        metric_count=6,
        passed_metric_count=6,
        score_milli=1000,
        status="pass",
        passed=True,
        transport_qualification="deterministic_fixture",
        live_qualification_status="UNKNOWN",
        actual_backend_model_input="UNKNOWN",
        api_live_executed=False,
    )
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "poker-bounded-river-review-workflow-qualification",
        "qualification_id": "qualification-v1",
        "qualification_status": "passed",
        "qualified_scope": "p3_030f_completed_deterministic_workflow",
        "auth_mode": RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        "transport_qualification": "deterministic_fixture",
        "live_qualification_status": "UNKNOWN",
        "actual_backend_model_input": "UNKNOWN",
        "api_live_executed": False,
        "api_production_qualified": False,
        "repository_commit_id": "1" * 40,
        "repository_tree_id": "2" * 40,
        "runtime_source_inventory_hash_domain": BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
        "runtime_source_inventory": runtime_inventory,
        "runtime_source_inventory_sha256": runtime_inventory_sha256,
        "codex_runtime_inventory_sha256": _sha("codex-runtime"),
        "python_runtime_inventory_sha256": _sha("python-runtime"),
        "semantic_mapping_sha256": _sha("semantic-mapping"),
        "workflow_id": "workflow-v2",
        "bridge_run_id": "bridge-v2",
        "source_identity": source_identity,
        "confirmation_hashes": confirmation_hashes,
        "lineage": lineage,
        "roles": roles,
        "terminal": terminal,
        "deterministic_evaluation": evaluation,
        "limitations": QUALIFICATION_LIMITATIONS,
    }
    return SanitizedBoundedRiverReviewWorkflowQualificationManifestV1.model_validate(
        {
            **payload,
            "manifest_sha256": domain_sha256(QUALIFICATION_MANIFEST_HASH_DOMAIN, payload),
        },
        strict=True,
    )


def test_manifest_is_canonical_sanitized_self_hashed_and_fail_closed(tmp_path: Path) -> None:
    manifest = _strict_manifest()
    destination = tmp_path / "qualification.json"
    write_sanitized_bounded_river_review_workflow_qualification_manifest(
        destination,
        manifest,
    )
    canonical = destination.read_bytes()

    assert canonical == canonical_json_bytes(manifest)
    assert (
        load_sanitized_bounded_river_review_workflow_qualification_manifest(destination) == manifest
    )
    assert manifest.transport_qualification == "deterministic_fixture"
    assert manifest.live_qualification_status == "UNKNOWN"
    assert manifest.api_live_executed is False
    assert manifest.api_production_qualified is False
    assert len(manifest.confirmation_hashes.model_fields) == 12
    assert tuple(item.role for item in manifest.roles) == BRIDGE_ROLE_ORDER
    assert all(item.confirmation_field_count == 17 for item in manifest.roles)
    assert len({item.workflow_role_confirmation_binding_sha256 for item in manifest.roles}) == 5
    assert len({item.workflow_role_confirmation_receipt_sha256 for item in manifest.roles}) == 5
    assert len({item.confirmation_fields_sha256 for item in manifest.roles}) == 5
    assert all(item.live_execution_evidence_sha256 is None for item in manifest.roles)
    assert manifest.deterministic_evaluation.case_count == 6
    assert manifest.deterministic_evaluation.metric_count == 6

    excluded = (
        river_source().decode("utf-8"),
        "system_prompt",
        "outbound_canonical_utf8",
        "credential_reference",
        "codex_home:saved_chatgpt_login",
        "narrative",
        "model_trace",
        "user_materials",
    )
    text = canonical.decode("utf-8")
    assert all(value not in text for value in excluded)

    tampered_payload = manifest.model_dump(mode="json")
    tampered_payload["qualification_id"] = "qualification-tampered"
    tampered_path = tmp_path / "qualification-tampered.json"
    tampered_path.write_bytes(canonical_json_bytes(tampered_payload))
    with pytest.raises(
        BoundedRiverReviewWorkflowQualificationError,
        match=r"^BRWQ_E_MANIFEST_STORAGE$",
    ):
        load_sanitized_bounded_river_review_workflow_qualification_manifest(tampered_path)

    bypassed_validation = manifest.model_copy(
        update={"manifest_sha256": "0" * 64},
    )
    with pytest.raises(
        BoundedRiverReviewWorkflowQualificationError,
        match=r"^BRWQ_E_MANIFEST_STORAGE$",
    ):
        write_sanitized_bounded_river_review_workflow_qualification_manifest(
            tmp_path / "qualification-invalid-object.json",
            bypassed_validation,
        )


def test_checkout_and_runtime_inventory_gates_are_explicit_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        qualification,
        "verify_bridge_checkout",
        lambda *args, **kwargs: calls.append("checkout"),
    )
    monkeypatch.setattr(
        qualification,
        "verify_bridge_module_origins",
        lambda *args, **kwargs: calls.append("bridge-origins"),
    )
    monkeypatch.setattr(
        qualification,
        "_verify_workflow_module_origins",
        lambda *args, **kwargs: calls.append("workflow-origins"),
    )
    qualification._verify_qualification_checkout(
        tmp_path,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
    )
    assert calls == ["checkout", "bridge-origins", "workflow-origins"]

    inventory = (
        BridgeRuntimeSourceFile(
            path="src/poker_deliberation/example.py",
            size=1,
            sha256=_sha("runtime-file"),
        ),
    )
    monkeypatch.setattr(
        qualification,
        "bridge_runtime_source_inventory",
        lambda *args, **kwargs: inventory,
    )
    monkeypatch.setattr(
        qualification,
        "bridge_runtime_source_inventory_sha256",
        lambda *args, **kwargs: "0" * 64,
    )
    with pytest.raises(
        BoundedRiverReviewWorkflowQualificationError,
        match=r"^BRWQ_E_RUNTIME_INVENTORY$",
    ):
        qualification._runtime_inventory_snapshot(tmp_path)


def test_builder_gates_checkout_and_inventory_before_evaluator_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ClaimedEvaluation:
        source_commit_id = "1" * 40
        source_tree_id = "2" * 40

    calls: list[str] = []

    def checkout(*args: object, **kwargs: object) -> None:
        assert kwargs == {
            "repository_commit_id": "1" * 40,
            "repository_tree_id": "2" * 40,
        }
        calls.append("checkout")

    def inventory(*args: object, **kwargs: object) -> tuple[tuple[object, ...], str]:
        calls.append("inventory")
        return (), _sha("inventory")

    def evaluation(*args: object, **kwargs: object) -> None:
        calls.append("evaluation")
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_STOP")

    monkeypatch.setattr(qualification, "_verify_qualification_checkout", checkout)
    monkeypatch.setattr(qualification, "_runtime_inventory_snapshot", inventory)
    monkeypatch.setattr(qualification, "_evaluation_projection", evaluation)

    with pytest.raises(
        BoundedRiverReviewWorkflowQualificationError,
        match=r"^BRWQ_E_STOP$",
    ):
        qualification.build_sanitized_bounded_river_review_workflow_qualification_manifest(
            config=object(),  # type: ignore[arg-type]
            repository_root=tmp_path,
            workflow_root=tmp_path / "workflow",
            workflow_id="workflow-v2",
            qualification_id="qualification-v1",
            deterministic_evaluation=ClaimedEvaluation(),  # type: ignore[arg-type]
        )
    assert calls == ["checkout", "inventory", "evaluation"]
