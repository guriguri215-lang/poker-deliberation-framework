from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from poker_deliberation.codex_bridge.canonical import (
    BridgeCanonicalError,
    canonical_json_bytes,
    domain_sha256,
)
from poker_deliberation.codex_bridge.conformance import build_bridge_role_conformance
from poker_deliberation.codex_bridge.contracts import (
    BridgeContractError,
    admit_role_request,
    assert_no_replay,
    build_execution_audit,
    build_role_confirmation,
    build_role_request,
    build_run_plan,
    build_runtime_policy,
    expected_evidence_references,
    role_output_schema,
    role_output_schema_for_request,
    validate_role_response,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    SAFE_INFERENCE_NARRATIVE,
    SAFE_UNKNOWN_NARRATIVE,
    BoundedCodexBridgeRequestV1,
    BridgeClaimV1,
    BridgeConclusionCode,
    BridgeConfirmationAuthorityV1,
    BridgeEffectState,
    BridgeEpistemicLabel,
    BridgeEvidenceReferenceV1,
    BridgeRole,
    BridgeRoleOutputV1,
    BridgeRoleResultV1,
    BridgeSourceContextV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.transport import DeterministicReadOnlyTransport
from tests.codex_bridge_support import REPOSITORY_ROOT, verified_bridge_source


def test_role_output_schema_requires_every_object_field_without_defaults() -> None:
    schema = role_output_schema()

    def assert_strict_objects(value: object) -> None:
        if isinstance(value, dict):
            assert "default" not in value
            properties = value.get("properties")
            if value.get("type") == "object" and isinstance(properties, dict):
                assert value.get("additionalProperties") is False
                assert value.get("required") == sorted(properties)
            for item in value.values():
                assert_strict_objects(item)
        elif isinstance(value, list):
            for item in value:
                assert_strict_objects(item)

    assert_strict_objects(schema)

    narrative = schema["$defs"]["BridgeClaimV1"]["properties"]["narrative"]
    assert narrative["enum"] == [SAFE_INFERENCE_NARRATIVE, SAFE_UNKNOWN_NARRATIVE]
    claim_id = schema["$defs"]["BridgeClaimV1"]["properties"]["claim_id"]
    assert claim_id["pattern"] == r"^claim-(?:0[1-9]|1[0-6])$"


def test_role_output_schema_binds_exact_identity_and_evidence(tmp_path: Path) -> None:
    request = _request(tmp_path)
    schema = role_output_schema_for_request(request)
    assert request.output_schema_sha256 == domain_sha256(
        "poker-bounded-codex-bridge-output-schema-v1",
        schema,
    )
    properties = schema["properties"]
    assert isinstance(properties, dict)
    assignment = request.context.assignment
    policy = request.context.runtime_policy
    exact = {
        "bridge_run_id": assignment.bridge_run_id,
        "auth_mode": request.auth_mode.value,
        "role": assignment.role.value,
        "assignment_id": assignment.assignment_id,
        "attempt_id": assignment.attempt_id,
        "model": policy.model,
        "model_provider": policy.model_provider,
        "runtime_identity": policy.runtime_identity,
    }
    for name, value in exact.items():
        field = properties[name]
        assert isinstance(field, dict)
        assert field["const"] == value

    references = properties["evidence_references"]
    assert isinstance(references, dict)
    assert references["minItems"] == len(request.required_evidence_references)
    assert references["maxItems"] == len(request.required_evidence_references)
    items = references["items"]
    assert isinstance(items, dict)
    branches = items["anyOf"]
    assert isinstance(branches, list)
    observed = []
    for branch in branches:
        assert isinstance(branch, dict)
        branch_properties = branch["properties"]
        assert isinstance(branch_properties, dict)
        observed.append(
            (
                branch_properties["evidence_id"]["const"],
                branch_properties["evidence_kind"]["const"],
                branch_properties["evidence_sha256"]["const"],
            )
        )
    assert observed == [
        (item.evidence_id, item.evidence_kind, item.evidence_sha256)
        for item in request.required_evidence_references
    ]


def _source(tmp_path: Path) -> BridgeSourceContextV1:
    return verified_bridge_source(tmp_path)


def _request(tmp_path: Path) -> BoundedCodexBridgeRequestV1:
    source = _source(tmp_path)
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
    )
    return build_role_request(
        bridge_run_id="bridge-run-contract",
        role=BridgeRole.STRATEGY_ANALYST,
        assignment_id="assignment-codex_subscription-strategy",
        attempt_id="attempt-codex_subscription-strategy",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        source_context=source,
        runtime_policy=build_runtime_policy(auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION),
        conformance=conformance[0],
    )


def _role_chain(
    tmp_path: Path,
) -> tuple[tuple[BoundedCodexBridgeRequestV1, BridgeRoleResultV1], ...]:
    source = _source(tmp_path)
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
    )
    policy = build_runtime_policy(auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION)
    expires_at = datetime(2030, 1, 1, tzinfo=UTC)

    def clock() -> datetime:
        return datetime(2029, 12, 31, 23, 40, tzinfo=UTC)

    transport = DeterministicReadOnlyTransport(
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        clock=clock,
    )
    chain: list[tuple[BoundedCodexBridgeRequestV1, BridgeRoleResultV1]] = []
    for ordinal, role in enumerate(BRIDGE_ROLE_ORDER):
        if role is BridgeRole.ADJUDICATOR:
            parents = tuple(result for _request, result in chain[:3])
        elif role is BridgeRole.REPORT_WRITER:
            parents = (chain[3][1],)
        else:
            parents = ()
        request = build_role_request(
            bridge_run_id="bridge-run-parent-contract",
            role=role,
            assignment_id=f"assignment-codex_subscription-{role.value}",
            attempt_id=f"attempt-codex_subscription-{role.value}",
            expires_at=expires_at,
            source_context=source,
            runtime_policy=policy,
            conformance=conformance[ordinal],
            parent_results=parents,
        )
        response = transport.execute(request).response_bytes
        chain.append((request, validate_role_response(request, response)))
    return tuple(chain)


def test_verified_terminal_projects_minimal_exact_context(tmp_path: Path) -> None:
    source = _source(tmp_path)
    payload = source.model_dump(mode="json")
    encoded = canonical_json_bytes(source)

    assert payload["math"]["required_equity"] == {"numerator": 5, "denominator": 24}
    assert payload["math"]["action_comparison"] == "call"
    assert payload["math"]["solver_status"] == "unavailable"
    assert b"raw_text" not in encoded
    assert b"observations" not in encoded
    assert b"final_report" not in encoded.lower()


def test_strategy_request_seeds_only_safe_neutral_narrative_forms(tmp_path: Path) -> None:
    instructions = _request(tmp_path).developer_instructions
    lowered = instructions.lower()

    assert (
        "The supplied evidence supports only the bounded comparison under its stated assumption."
        in instructions
    )
    assert "Practical applicability remains unknown under the supplied assumption." in instructions
    assert "Copy required_evidence_references exactly." in instructions
    assert "every narrative value is a closed enum" in lowered
    assert "do not write any other narrative text" in lowered
    for forbidden in (
        "gto",
        "equilibrium",
        "solver-derived",
        "always",
        "must",
        "unconditionally",
    ):
        assert forbidden not in lowered


def test_request_confirmation_admission_and_response_are_exactly_bound(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    policy = build_runtime_policy(auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION)
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
    )
    created = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)
    plan = build_run_plan(
        bridge_run_id="bridge-run-contract",
        source_context=source,
        runtime_policy=policy,
        role_conformance=conformance,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        created_at=created,
    )
    request = build_role_request(
        bridge_run_id=plan.bridge_run_id,
        role=BridgeRole.STRATEGY_ANALYST,
        assignment_id="assignment-codex_subscription-strategy",
        attempt_id="attempt-codex_subscription-strategy",
        expires_at=created + timedelta(minutes=15),
        source_context=source,
        runtime_policy=policy,
        conformance=conformance[0],
    )
    confirmation = build_role_confirmation(
        request,
        confirmation_id="confirmation-strategy",
        idempotency_key="idempotency-strategy",
        authority=BridgeConfirmationAuthorityV1(
            authority_id="local-test-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        confirmed_at=created,
    )
    admission_record = admit_role_request(
        request,
        confirmation,
        admitted_at=created + timedelta(seconds=1),
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
    )
    references = expected_evidence_references(request)
    output = BridgeRoleOutputV1(
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        bridge_run_id=plan.bridge_run_id,
        role=BridgeRole.STRATEGY_ANALYST,
        assignment_id="assignment-codex_subscription-strategy",
        attempt_id="attempt-codex_subscription-strategy",
        model=policy.model,
        model_provider=policy.model_provider,
        runtime_identity=policy.runtime_identity,
        conclusions=(
            BridgeClaimV1(
                claim_id="claim-01",
                conclusion_code=BridgeConclusionCode.STRATEGY_OBSERVATION,
                label=BridgeEpistemicLabel.INFERENCE,
                narrative=SAFE_INFERENCE_NARRATIVE,
                evidence_ids=("source-result",),
            ),
        ),
        evidence_references=references,
    )
    result = validate_role_response(request, canonical_json_bytes(output))

    assert request.allowed_conclusion_codes == (
        BridgeConclusionCode.NO_UNCONDITIONAL_RECOMMENDATION,
        BridgeConclusionCode.STRATEGY_OBSERVATION,
    )
    assert request.required_evidence_references == references
    assert request.claim_evidence_rule == "any_required_evidence"
    assert request.calculated_labels_allowed is False
    assert admission_record.effect_state == "not_launched"
    assert result.output == output
    assert result.result_sha256 != request.request_sha256

    for narrative in (
        "５割の頻度でコールする。",
        "エースキングスーテッドを含むレンジです。",
        "ソルバー解析による戦略です。",
        "This is CALCULATED.",
    ):
        mutated = output.model_dump(mode="json")
        mutated["conclusions"][0]["narrative"] = narrative
        with pytest.raises(BridgeCanonicalError):
            validate_role_response(request, canonical_json_bytes(mutated))


def test_contract_rejects_replay_mutation_and_model_numeric_claim(tmp_path: Path) -> None:
    request = _request(tmp_path)
    key = (
        request.auth_mode,
        request.context.assignment.bridge_run_id,
        request.context.assignment.assignment_id,
        request.context.assignment.attempt_id,
    )
    with pytest.raises(BridgeContractError, match="duplicate execution"):
        assert_no_replay(request=request, existing_attempts={key: request.request_sha256})

    mutated = request.model_dump(mode="json")
    mutated["context"]["assignment"]["attempt_id"] = "attempt-mutated"
    with pytest.raises(ValidationError):
        BoundedCodexBridgeRequestV1.model_validate(mutated, strict=True)

    for narrative in (
        "The equity is 99 percent.",
        "This is not a gto claim.",
        "５割の頻度でコールする。",
        "エースキングスーテッドを含むレンジです。",
        "ソルバー解析による戦略です。",
        "This is CALCULATED.",
    ):
        with pytest.raises(ValidationError):
            BridgeClaimV1(
                claim_id="claim-01",
                conclusion_code=BridgeConclusionCode.STRATEGY_OBSERVATION,
                label=BridgeEpistemicLabel.INFERENCE,
                narrative=narrative,
                evidence_ids=("source-result",),
            )

    with pytest.raises(ValidationError, match="does not match its epistemic label"):
        BridgeClaimV1(
            claim_id="claim-01",
            conclusion_code=BridgeConclusionCode.STRATEGY_OBSERVATION,
            label=BridgeEpistemicLabel.UNKNOWN,
            narrative=SAFE_INFERENCE_NARRATIVE,
            evidence_ids=("source-result",),
        )

    mutated_policy = request.context.runtime_policy.model_dump(mode="python")
    mutated_policy["runtime_binary_sha256"] = "0" * 64
    with pytest.raises(ValidationError):
        type(request.context.runtime_policy).model_validate(
            mutated_policy,
            strict=True,
        )


def test_admission_rejects_source_manifest_rebinding(tmp_path: Path) -> None:
    request = _request(tmp_path)
    confirmed = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)
    confirmation = build_role_confirmation(
        request,
        confirmation_id="confirmation-strategy",
        idempotency_key="idempotency-strategy",
        authority=BridgeConfirmationAuthorityV1(
            authority_id="local-test-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        confirmed_at=confirmed,
    )
    with pytest.raises(BridgeContractError, match="binding failed"):
        admit_role_request(
            request,
            confirmation,
            admitted_at=confirmed + timedelta(seconds=1),
            current_source_terminal_manifest_sha256="f" * 64,
        )


def test_role_conformance_is_exactly_bound_to_p2_025a_inventory() -> None:
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
    )

    assert tuple(item.role for item in conformance) == BRIDGE_ROLE_ORDER
    assert all(item.role_read_only for item in conformance)
    assert all(item.declared_tool_allowlist == () for item in conformance)
    assert len({item.runtime_role_definition_sha256 for item in conformance}) == 5
    assert len({item.codex_runtime_inventory_sha256 for item in conformance}) == 1
    assert len({item.python_runtime_inventory_sha256 for item in conformance}) == 1
    assert len({item.semantic_mapping_sha256 for item in conformance}) == 1

    mutated = conformance[0].model_dump(mode="python")
    mutated["semantic_role"] = "math-audit"
    with pytest.raises(ValidationError, match="semantic mapping"):
        type(conformance[0]).model_validate(mutated, strict=True)


def test_dependent_outputs_cannot_bypass_parent_lineage() -> None:
    references = (
        BridgeEvidenceReferenceV1(
            evidence_id="parent-assignment-adjudicator",
            evidence_kind="adjudication",
            evidence_sha256="1" * 64,
        ),
        BridgeEvidenceReferenceV1(
            evidence_id="source-result",
            evidence_kind="source_result",
            evidence_sha256="2" * 64,
        ),
    )
    with pytest.raises(ValidationError, match="adjudicated parent"):
        BridgeRoleOutputV1(
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            bridge_run_id="bridge-run-contract",
            role=BridgeRole.REPORT_WRITER,
            assignment_id="assignment-codex_subscription-report-writer",
            attempt_id="attempt-codex_subscription-report-writer",
            model="gpt-5.6-terra",
            model_provider="openai",
            runtime_identity="openai-codex-cli/0.144.4",
            conclusions=(
                BridgeClaimV1(
                    claim_id="claim-01",
                    conclusion_code=BridgeConclusionCode.REPORT_BOUND,
                    label=BridgeEpistemicLabel.INFERENCE,
                    narrative=SAFE_INFERENCE_NARRATIVE,
                    evidence_ids=("source-result",),
                ),
            ),
            evidence_references=references,
        )


def test_parent_payload_is_complete_hash_bound_and_role_ordered(tmp_path: Path) -> None:
    chain = _role_chain(tmp_path)
    adjudicator_request = chain[3][0]
    parents = adjudicator_request.context.parent_results

    assert tuple(parent.role for parent in parents) == BRIDGE_ROLE_ORDER[:3]
    assert tuple(parent.output for parent in parents) == tuple(
        result.output for _request, result in chain[:3]
    )
    assert all(parent.output.conclusions for parent in parents)
    assert all(parent.output.evidence_references for parent in parents)
    assert all(
        parent.result_sha256
        == domain_sha256(
            "poker-bounded-codex-bridge-role-result-v1",
            {
                "output": parent.output,
                "response_bytes_sha256": parent.response_bytes_sha256,
            },
        )
        for parent in parents
    )

    missing = adjudicator_request.model_dump(mode="python")
    missing["context"]["parent_results"][0].pop("output")
    with pytest.raises(ValidationError):
        BoundedCodexBridgeRequestV1.model_validate(missing, strict=True)

    changed_body = adjudicator_request.model_dump(mode="python")
    changed_body["context"]["parent_results"][0]["output"]["conclusions"][0]["conclusion_code"] = (
        BridgeConclusionCode.NO_UNCONDITIONAL_RECOMMENDATION
    )
    with pytest.raises(ValidationError, match="parent role result hash mismatch"):
        BoundedCodexBridgeRequestV1.model_validate(changed_body, strict=True)

    changed_role = adjudicator_request.model_dump(mode="python")
    changed_parent = changed_role["context"]["parent_results"][0]
    changed_parent["output"]["role"] = BridgeRole.MATH_TOOL_AUDITOR
    changed_parent["output"]["conclusions"][0]["conclusion_code"] = (
        BridgeConclusionCode.MATH_CONSISTENT
    )
    changed_parent["result_sha256"] = domain_sha256(
        "poker-bounded-codex-bridge-role-result-v1",
        {
            "output": changed_parent["output"],
            "response_bytes_sha256": changed_parent["response_bytes_sha256"],
        },
    )
    changed_result_hashes = list(changed_role["context"]["assignment"]["parent_result_sha256s"])
    changed_result_hashes[0] = changed_parent["result_sha256"]
    changed_role["context"]["assignment"]["parent_result_sha256s"] = tuple(changed_result_hashes)
    with pytest.raises(ValidationError, match="context parent role order mismatch"):
        BoundedCodexBridgeRequestV1.model_validate(changed_role, strict=True)

    reordered = adjudicator_request.model_dump(mode="python")
    reordered["context"]["parent_results"] = (
        reordered["context"]["parent_results"][1],
        reordered["context"]["parent_results"][0],
        reordered["context"]["parent_results"][2],
    )
    reordered["context"]["assignment"]["parent_assignment_ids"] = (
        reordered["context"]["assignment"]["parent_assignment_ids"][1],
        reordered["context"]["assignment"]["parent_assignment_ids"][0],
        reordered["context"]["assignment"]["parent_assignment_ids"][2],
    )
    reordered["context"]["assignment"]["parent_result_sha256s"] = (
        reordered["context"]["assignment"]["parent_result_sha256s"][1],
        reordered["context"]["assignment"]["parent_result_sha256s"][0],
        reordered["context"]["assignment"]["parent_result_sha256s"][2],
    )
    with pytest.raises(ValidationError, match="context parent role order mismatch"):
        BoundedCodexBridgeRequestV1.model_validate(reordered, strict=True)


def test_report_writer_is_exact_deterministic_adjudication_projection(tmp_path: Path) -> None:
    report_request, report_result = _role_chain(tmp_path)[4]
    adjudicator = report_request.context.parent_results[0]

    assert tuple(claim.claim_id for claim in report_result.output.conclusions) == tuple(
        claim.claim_id for claim in adjudicator.output.conclusions
    )
    assert tuple(claim.label for claim in report_result.output.conclusions) == tuple(
        claim.label for claim in adjudicator.output.conclusions
    )
    assert tuple(claim.narrative for claim in report_result.output.conclusions) == tuple(
        claim.narrative for claim in adjudicator.output.conclusions
    )
    assert all(
        claim.evidence_ids == (f"parent-{adjudicator.assignment_id}",)
        for claim in report_result.output.conclusions
    )

    added_claim = report_result.output.model_dump(mode="json")
    added_claim["conclusions"].append(
        {
            "claim_id": "claim-02",
            "conclusion_code": BridgeConclusionCode.REPORT_BOUND.value,
            "evidence_ids": [f"parent-{adjudicator.assignment_id}"],
            "label": BridgeEpistemicLabel.INFERENCE.value,
            "narrative": SAFE_INFERENCE_NARRATIVE,
        }
    )
    with pytest.raises(BridgeContractError, match="deterministic adjudication projection"):
        validate_role_response(report_request, canonical_json_bytes(added_claim))

    changed_label = report_result.output.model_dump(mode="json")
    changed_label["conclusions"][0]["label"] = BridgeEpistemicLabel.UNKNOWN.value
    changed_label["conclusions"][0]["narrative"] = SAFE_UNKNOWN_NARRATIVE
    with pytest.raises(BridgeContractError, match="deterministic adjudication projection"):
        validate_role_response(report_request, canonical_json_bytes(changed_label))


def test_claim_ids_are_content_free_ordered_and_gapless() -> None:
    with pytest.raises(ValidationError):
        BridgeClaimV1(
            claim_id="GTO-call-AKs-99-percent",
            conclusion_code=BridgeConclusionCode.STRATEGY_OBSERVATION,
            label=BridgeEpistemicLabel.INFERENCE,
            narrative=SAFE_INFERENCE_NARRATIVE,
            evidence_ids=("source-result",),
        )

    reference = BridgeEvidenceReferenceV1(
        evidence_id="source-result",
        evidence_kind="source_result",
        evidence_sha256="1" * 64,
    )
    with pytest.raises(ValidationError, match="content-free, ordered, and gapless"):
        BridgeRoleOutputV1(
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            bridge_run_id="bridge-run-contract",
            role=BridgeRole.STRATEGY_ANALYST,
            assignment_id="assignment-codex_subscription-strategy-analyst",
            attempt_id="attempt-codex_subscription-strategy-analyst",
            model="gpt-5.6-terra",
            model_provider="openai",
            runtime_identity="openai-codex-cli/0.144.4",
            conclusions=(
                BridgeClaimV1(
                    claim_id="claim-02",
                    conclusion_code=BridgeConclusionCode.STRATEGY_OBSERVATION,
                    label=BridgeEpistemicLabel.INFERENCE,
                    narrative=SAFE_INFERENCE_NARRATIVE,
                    evidence_ids=("source-result",),
                ),
            ),
            evidence_references=(reference,),
        )


def test_each_adjudicator_claim_must_bind_all_three_parent_results() -> None:
    references = tuple(
        BridgeEvidenceReferenceV1(
            evidence_id=f"parent-role-{index}",
            evidence_kind="role_result",
            evidence_sha256=str(index) * 64,
        )
        for index in range(1, 4)
    )
    with pytest.raises(ValidationError, match="each adjudicator claim"):
        BridgeRoleOutputV1(
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            bridge_run_id="bridge-run-contract",
            role=BridgeRole.ADJUDICATOR,
            assignment_id="assignment-codex_subscription-adjudicator",
            attempt_id="attempt-codex_subscription-adjudicator",
            model="gpt-5.6-terra",
            model_provider="openai",
            runtime_identity="openai-codex-cli/0.144.4",
            conclusions=tuple(
                BridgeClaimV1(
                    claim_id=f"claim-{index:02d}",
                    conclusion_code=BridgeConclusionCode.ADJUDICATED_LIMITED,
                    label=BridgeEpistemicLabel.INFERENCE,
                    narrative=SAFE_INFERENCE_NARRATIVE,
                    evidence_ids=(f"parent-role-{index}",),
                )
                for index in range(1, 4)
            ),
            evidence_references=references,
        )


@pytest.mark.parametrize(
    ("effect_state", "cancellation_kind"),
    (
        (BridgeEffectState.CANCELLED, "cooperative"),
        (BridgeEffectState.CANCEL_UNCONFIRMED, "unconfirmed"),
        (BridgeEffectState.FAILED, "not_requested"),
    ),
)
def test_execution_audit_binds_cancellation_kind_to_effect_state(
    tmp_path: Path,
    effect_state: BridgeEffectState,
    cancellation_kind: str,
) -> None:
    request = _request(tmp_path)
    confirmed_at = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)
    confirmation = build_role_confirmation(
        request,
        confirmation_id="confirmation-cancellation-contract",
        idempotency_key="idempotency-cancellation-contract",
        authority=BridgeConfirmationAuthorityV1(
            authority_id="local-test-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        confirmed_at=confirmed_at,
    )
    admission = admit_role_request(
        request,
        confirmation,
        admitted_at=confirmed_at + timedelta(seconds=1),
        current_source_terminal_manifest_sha256=(
            request.context.source_context.source.source_terminal_manifest_sha256
        ),
    )
    common: dict[str, object] = {
        "request": request,
        "confirmation": confirmation,
        "admission": admission,
        "transport_qualification": "deterministic_fixture",
        "effect_state": effect_state,
        "thread_id_sha256": "1" * 64,
        "turn_id_sha256": "2" * 64,
        "launched_at": confirmed_at + timedelta(seconds=2),
        "completed_at": confirmed_at + timedelta(seconds=3),
        "duration_ms": 1000,
        "usage": None,
        "response_bytes": None,
        "stream_bytes": 0,
        "unexpected_item_types": (),
        "result_sha256": None,
        "failure_reason_code": "test_terminal_failure",
        "model_identity_evidence": "unavailable",
        "observed_model": None,
        "observed_model_provider": None,
        "observed_reasoning_effort": None,
        "observed_service_tier": None,
        "observed_identity_sha256": None,
    }
    audit = build_execution_audit(  # type: ignore[arg-type]
        **common,
        cancellation_kind=cancellation_kind,
    )
    assert audit.cancellation_kind == cancellation_kind

    wrong_kind = {
        "cooperative": "unconfirmed",
        "unconfirmed": "not_requested",
        "not_requested": "cooperative",
    }[cancellation_kind]
    with pytest.raises(ValidationError, match="cancellation kind"):
        build_execution_audit(  # type: ignore[arg-type]
            **common,
            cancellation_kind=wrong_kind,
        )
    with pytest.raises(ValidationError, match="lacks sealed execution evidence"):
        build_execution_audit(  # type: ignore[arg-type]
            **{
                **common,
                "transport_qualification": "actual_live",
            },
            cancellation_kind=cancellation_kind,
        )


def test_runtime_output_schema_uses_recursive_canonical_key_order() -> None:
    def verify(value: object) -> None:
        if isinstance(value, dict):
            assert list(value) == sorted(value, key=lambda item: item.encode("utf-8"))
            for item in value.values():
                verify(item)
        elif isinstance(value, list):
            for item in value:
                verify(item)

    verify(role_output_schema())
