"""Construction and validation of exact per-role bridge requests."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    domain_sha256,
    parse_canonical_model,
    sha256_bytes,
)
from poker_deliberation.codex_bridge.models import (
    ADMISSION_HASH_DOMAIN,
    AUTH_MODE_CONTRACT_VERSION,
    BRIDGE_LOCAL_CREDENTIAL_REFERENCE,
    BRIDGE_LOCAL_PROVIDER_ID,
    BRIDGE_LOCAL_RUNTIME_ID,
    BRIDGE_MODEL_ID,
    BRIDGE_OPENAI_API_CREDENTIAL_REFERENCE,
    BRIDGE_OPENAI_API_PROVIDER_ID,
    BRIDGE_OPENAI_API_RUNTIME_ID,
    BRIDGE_REASONING_EFFORT,
    BRIDGE_ROLE_ORDER,
    BRIDGE_RUNTIME_BINARY_SHA256,
    BRIDGE_SERVICE_TIER,
    BRIDGE_SUBSCRIPTION_CREDENTIAL_REFERENCE,
    BRIDGE_SUBSCRIPTION_PROVIDER_ID,
    BRIDGE_SUBSCRIPTION_RUNTIME_ID,
    CONFIRMATION_HASH_DOMAIN,
    CONTEXT_HASH_DOMAIN,
    EXECUTION_AUDIT_HASH_DOMAIN,
    EXECUTION_AUDIT_SCHEMA_VERSION,
    MAX_CONFIRMATION_LIFETIME_SECONDS,
    MAX_CONTEXT_BYTES,
    MAX_RESERVED_COST_MICRO_USD,
    REQUEST_HASH_DOMAIN,
    RESULT_HASH_DOMAIN,
    RUN_PLAN_HASH_DOMAIN,
    SAFE_INFERENCE_NARRATIVE,
    SAFE_UNKNOWN_NARRATIVE,
    BoundedCodexBridgeRequestV1,
    BridgeBudgetV1,
    BridgeClaimV1,
    BridgeConclusionCode,
    BridgeConfirmationAuthorityV1,
    BridgeContextEnvelopeV1,
    BridgeEffectState,
    BridgeEpistemicLabel,
    BridgeEvidenceReferenceV1,
    BridgeExecutionAuditV1,
    BridgeParentResultV1,
    BridgePreExecutionAdmissionV1,
    BridgeRole,
    BridgeRoleAssignmentV1,
    BridgeRoleConfirmationV1,
    BridgeRoleConformanceBindingV1,
    BridgeRoleOutputV1,
    BridgeRoleResultV1,
    BridgeRunPlanV1,
    BridgeRuntimePolicyV1,
    BridgeSourceContextV1,
    BridgeTransportUsageV1,
    CodexSubscriptionLiveExecutionEvidenceV1,
    RuntimeAuthModeV1,
    allowed_conclusion_codes,
    claim_evidence_rule,
)


class BridgeContractError(ValueError):
    """Raised when exact request, authority, or result correlation fails."""


_NEUTRAL_NARRATIVE_INSTRUCTIONS: Final = (
    " Copy required_evidence_references exactly."
    " Assign content-free claim_id values in conclusions-then-uncertainties order exactly as "
    "claim-01, claim-02, and so on without duplicates or gaps."
    " Every narrative value is a closed enum. For an INFERENCE claim copy exactly: "
    f"{SAFE_INFERENCE_NARRATIVE} For an UNKNOWN claim copy exactly: "
    f"{SAFE_UNKNOWN_NARRATIVE} Do not write any other narrative text."
)


_DEVELOPER_INSTRUCTIONS: Final[dict[BridgeRole, str]] = {
    BridgeRole.STRATEGY_ANALYST: (
        "Review only the supplied immutable poker evidence as a read-only strategy analyst. "
        "Do not calculate, infer a range, use tools, cite sources, assert named strategy systems "
        "or optimality, or issue a recommendation. Return only canonical minified JSON with "
        "recursively lexicographically sorted object keys; no Markdown or trailing newline."
    )
    + _NEUTRAL_NARRATIVE_INSTRUCTIONS,
    BridgeRole.MATH_TOOL_AUDITOR: (
        "Audit only whether the supplied exact evidence is internally stated consistently. "
        "Do not recalculate, create a number, run a tool, infer a range, cite a source, or "
        "replace CALCULATED evidence. Return only canonical minified JSON with recursively "
        "lexicographically sorted object keys; no Markdown or trailing newline."
    )
    + _NEUTRAL_NARRATIVE_INSTRUCTIONS,
    BridgeRole.SKEPTIC_FALSIFIER: (
        "Identify only missing premises, counterexamples, and bounded uncertainty in the supplied "
        "evidence. Do not calculate, infer a range, use tools, cite sources, assert named strategy "
        "systems or optimality, or issue a recommendation. Return only canonical minified JSON "
        "with recursively lexicographically sorted object keys; no Markdown or trailing newline."
    )
    + _NEUTRAL_NARRATIVE_INSTRUCTIONS,
    BridgeRole.ADJUDICATOR: (
        "Adjudicate the three independently validated parent results against the immutable source "
        "evidence. Do not decide by vote, calculate, add evidence, infer a range, use tools, cite "
        "sources, assert named strategy systems or optimality, or issue a recommendation. Every "
        "claim must use all three supplied parent evidence IDs exactly. Return only canonical "
        "minified JSON with recursively lexicographically sorted object keys; no Markdown or "
        "trailing newline."
    )
    + _NEUTRAL_NARRATIVE_INSTRUCTIONS,
    BridgeRole.REPORT_WRITER: (
        "Project every adjudicator claim in its original conclusions or uncertainties tuple and "
        "order. Preserve claim ID, label, and narrative. Map adjudicated_support to report_bound; "
        "map adjudicated_limited or adjudicated_unknown to report_limited. Use only the single "
        "adjudication parent evidence ID. Add no fact, number, range, claim, citation, "
        "recommendation, or tool result. Return only canonical minified JSON with sorted object "
        "keys; no Markdown or trailing newline."
    )
    + _NEUTRAL_NARRATIVE_INSTRUCTIONS,
}


def build_runtime_policy(
    *,
    auth_mode: RuntimeAuthModeV1,
    api_max_cost_micro_usd: int | None = None,
) -> BridgeRuntimePolicyV1:
    if auth_mode is not RuntimeAuthModeV1.OPENAI_API and api_max_cost_micro_usd is not None:
        raise BridgeContractError("API cost budget is forbidden outside openai_api mode")
    if auth_mode is RuntimeAuthModeV1.LOCAL_ONLY:
        identity = {
            "interface": "local_provider",
            "runtime_identity": BRIDGE_LOCAL_RUNTIME_ID,
            "runtime_binary_sha256": None,
            "model": None,
            "model_provider": BRIDGE_LOCAL_PROVIDER_ID,
            "reasoning_effort": None,
            "service_tier": None,
            "credential_reference": BRIDGE_LOCAL_CREDENTIAL_REFERENCE,
            "credential_value_access": "none",
            "model_processing_authorized": False,
            "remote_retention_policy": "none_local_only",
            "provider_internal_retry_status": "not_applicable",
            "remote_cancel_finality": "not_applicable",
            "network_allowed": False,
        }
        budget = BridgeBudgetV1(
            auth_mode=auth_mode,
            max_turns=0,
            max_runtime_ms=0,
            max_input_tokens=0,
            max_output_tokens=0,
            cost_budget_kind="not_applicable",
            max_cost_micro_usd=None,
        )
    elif auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION:
        identity = {
            "interface": "codex_exec_json",
            "runtime_identity": BRIDGE_SUBSCRIPTION_RUNTIME_ID,
            "runtime_binary_sha256": BRIDGE_RUNTIME_BINARY_SHA256,
            "model": BRIDGE_MODEL_ID,
            "model_provider": BRIDGE_SUBSCRIPTION_PROVIDER_ID,
            "reasoning_effort": BRIDGE_REASONING_EFFORT,
            "service_tier": BRIDGE_SERVICE_TIER,
            "credential_reference": BRIDGE_SUBSCRIPTION_CREDENTIAL_REFERENCE,
            "credential_value_access": "codex_status_probe_only",
            "model_processing_authorized": True,
            "remote_retention_policy": "chatgpt_workspace_policy_unknown",
            "provider_internal_retry_status": "UNKNOWN",
            "remote_cancel_finality": "UNKNOWN",
            "network_allowed": True,
        }
        budget = BridgeBudgetV1(
            auth_mode=auth_mode,
            max_turns=1,
            max_runtime_ms=120_000,
            max_input_tokens=24_000,
            max_output_tokens=6_000,
            cost_budget_kind="subscription_usage",
            max_cost_micro_usd=None,
        )
    else:
        if (
            api_max_cost_micro_usd is None
            or api_max_cost_micro_usd <= 0
            or api_max_cost_micro_usd * 5 > MAX_RESERVED_COST_MICRO_USD
        ):
            raise BridgeContractError("openai_api requires an explicit cost budget")
        identity = {
            "interface": "codex_sdk_responses",
            "runtime_identity": BRIDGE_OPENAI_API_RUNTIME_ID,
            "runtime_binary_sha256": BRIDGE_RUNTIME_BINARY_SHA256,
            "model": BRIDGE_MODEL_ID,
            "model_provider": BRIDGE_OPENAI_API_PROVIDER_ID,
            "reasoning_effort": BRIDGE_REASONING_EFFORT,
            "service_tier": BRIDGE_SERVICE_TIER,
            "credential_reference": BRIDGE_OPENAI_API_CREDENTIAL_REFERENCE,
            "credential_value_access": "official_runtime_only",
            "model_processing_authorized": True,
            "remote_retention_policy": "openai_api_org_policy_no_zdr_claim",
            "provider_internal_retry_status": "disabled",
            "remote_cancel_finality": "UNKNOWN",
            "network_allowed": True,
        }
        budget = BridgeBudgetV1(
            auth_mode=auth_mode,
            max_turns=1,
            max_runtime_ms=120_000,
            max_input_tokens=24_000,
            max_output_tokens=6_000,
            cost_budget_kind="api_explicit_cap",
            max_cost_micro_usd=api_max_cost_micro_usd,
        )
    payload: dict[str, object] = {
        "auth_mode_contract_version": AUTH_MODE_CONTRACT_VERSION,
        "auth_mode": auth_mode,
        "provider_selection_source": "explicit_auth_mode",
        "api_key_presence_selects_mode": False,
        "provider_fallback_allowed": False,
        "model_fallback_allowed": False,
        **identity,
        "classification": "public",
        "usage_classification": "redistribution_allowed",
        "trace_policy": "validated_typed_public_raw_local_only",
        "tool_allowlist": (),
        "shell_enabled": False,
        "web_enabled": False,
        "mcp_enabled": False,
        "apps_enabled": False,
        "nested_agents_enabled": False,
        "file_write_enabled": False,
        "approval_policy": "never",
        "sandbox": "read-only",
        "serial_execution": True,
        "automatic_product_retry": False,
        "cooperative_cancellation_only": True,
        "hard_process_tree_stop": False,
        "budget": budget,
    }
    return BridgeRuntimePolicyV1.model_validate(
        {
            **payload,
            "policy_sha256": domain_sha256(
                "poker-bounded-codex-bridge-runtime-policy-v1",
                payload,
            ),
        },
        strict=True,
    )


def role_output_schema() -> dict[str, object]:
    """Return the output schema with canonical object-key insertion order."""

    schema = BridgeRoleOutputV1.model_json_schema(mode="validation")

    def strict_objects(value: object) -> None:
        if isinstance(value, dict):
            value.pop("default", None)
            properties = value.get("properties")
            if value.get("type") == "object" and isinstance(properties, dict):
                value["required"] = sorted(properties)
                value["additionalProperties"] = False
            for item in value.values():
                strict_objects(item)
        elif isinstance(value, list):
            for item in value:
                strict_objects(item)

    strict_objects(schema)
    value = json.loads(canonical_json_bytes(schema))
    if not isinstance(value, dict):  # pragma: no cover - Pydantic schema invariant
        raise BridgeContractError("role output schema is not an object")
    return value


def _bound_role_output_schema(
    *,
    bridge_run_id: str,
    auth_mode: RuntimeAuthModeV1,
    role: BridgeRole,
    assignment_id: str,
    attempt_id: str,
    model: str | None,
    model_provider: str,
    runtime_identity: str,
    evidence_references: tuple[BridgeEvidenceReferenceV1, ...],
) -> dict[str, object]:
    schema = role_output_schema()
    properties = schema.get("properties")
    if not isinstance(properties, dict):  # pragma: no cover - Pydantic invariant
        raise BridgeContractError("role output schema properties are missing")
    exact_values: dict[str, object] = {
        "bridge_run_id": bridge_run_id,
        "auth_mode": auth_mode.value,
        "role": role.value,
        "assignment_id": assignment_id,
        "attempt_id": attempt_id,
        "model": model,
        "model_provider": model_provider,
        "runtime_identity": runtime_identity,
    }
    for name, exact in exact_values.items():
        properties[name] = {
            "const": exact,
            "type": "null" if exact is None else "string",
        }
    exact_reference_schemas: list[dict[str, object]] = []
    for reference in evidence_references:
        reference_properties = {
            "evidence_id": {"const": reference.evidence_id, "type": "string"},
            "evidence_kind": {"const": reference.evidence_kind, "type": "string"},
            "evidence_sha256": {
                "const": reference.evidence_sha256,
                "type": "string",
            },
        }
        exact_reference_schemas.append(
            {
                "additionalProperties": False,
                "properties": reference_properties,
                "required": sorted(reference_properties),
                "type": "object",
            }
        )
    properties["evidence_references"] = {
        "items": {"anyOf": exact_reference_schemas},
        "maxItems": len(evidence_references),
        "minItems": len(evidence_references),
        "type": "array",
    }
    definitions = schema.get("$defs")
    if not isinstance(definitions, dict):  # pragma: no cover - Pydantic invariant
        raise BridgeContractError("bound role output schema definitions are missing")
    claim_schema = definitions.get("BridgeClaimV1")
    if not isinstance(claim_schema, dict):  # pragma: no cover - Pydantic invariant
        raise BridgeContractError("bound role output claim schema is missing")
    claim_properties = claim_schema.get("properties")
    if not isinstance(claim_properties, dict):  # pragma: no cover - Pydantic invariant
        raise BridgeContractError("bound role output claim properties are missing")
    claim_properties["evidence_ids"] = {
        "items": {
            "enum": [reference.evidence_id for reference in evidence_references],
            "type": "string",
        },
        "type": "array",
    }
    value = json.loads(canonical_json_bytes(schema))
    if not isinstance(value, dict):  # pragma: no cover - canonical JSON invariant
        raise BridgeContractError("bound role output schema is not an object")
    return value


def role_output_schema_for_request(
    request: BoundedCodexBridgeRequestV1,
) -> dict[str, object]:
    """Return and verify the exact schema passed to one role transport."""

    assignment = request.context.assignment
    policy = request.context.runtime_policy
    schema = _bound_role_output_schema(
        bridge_run_id=assignment.bridge_run_id,
        auth_mode=request.auth_mode,
        role=assignment.role,
        assignment_id=assignment.assignment_id,
        attempt_id=assignment.attempt_id,
        model=policy.model,
        model_provider=policy.model_provider,
        runtime_identity=policy.runtime_identity,
        evidence_references=request.required_evidence_references,
    )
    if request.output_schema_sha256 != domain_sha256(
        "poker-bounded-codex-bridge-output-schema-v1",
        schema,
    ):
        raise BridgeContractError("request output schema binding mismatch")
    return schema


def build_run_plan(
    *,
    bridge_run_id: str,
    source_context: BridgeSourceContextV1,
    runtime_policy: BridgeRuntimePolicyV1,
    role_conformance: tuple[BridgeRoleConformanceBindingV1, ...],
    repository_commit_id: str,
    repository_tree_id: str,
    created_at: datetime,
) -> BridgeRunPlanV1:
    if tuple(item.role for item in role_conformance) != BRIDGE_ROLE_ORDER:
        raise BridgeContractError("P2-025A role conformance order is incomplete")
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "poker-bounded-codex-review-bridge",
        "bridge_run_id": bridge_run_id,
        "auth_mode": runtime_policy.auth_mode,
        "source": source_context.source,
        "role_order": BRIDGE_ROLE_ORDER,
        "role_conformance": role_conformance,
        "codex_runtime_inventory_sha256": (role_conformance[0].codex_runtime_inventory_sha256),
        "python_runtime_inventory_sha256": (role_conformance[0].python_runtime_inventory_sha256),
        "semantic_mapping_sha256": role_conformance[0].semantic_mapping_sha256,
        "runtime_policy_sha256": runtime_policy.policy_sha256,
        "runtime_identity": runtime_policy.runtime_identity,
        "model_provider": runtime_policy.model_provider,
        "model": runtime_policy.model,
        "credential_reference": runtime_policy.credential_reference,
        "remote_retention_policy": runtime_policy.remote_retention_policy,
        "repository_commit_id": repository_commit_id,
        "repository_tree_id": repository_tree_id,
        "created_at": created_at,
        "total_max_turns": 0 if runtime_policy.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY else 5,
        "total_max_runtime_ms": (
            0 if runtime_policy.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY else 600_000
        ),
        "total_max_input_tokens": (
            0 if runtime_policy.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY else 120_000
        ),
        "total_max_output_tokens": (
            0 if runtime_policy.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY else 30_000
        ),
        "total_max_cost_micro_usd": (
            runtime_policy.budget.max_cost_micro_usd * 5
            if runtime_policy.budget.max_cost_micro_usd is not None
            else None
        ),
    }
    return BridgeRunPlanV1.model_validate(
        {**payload, "plan_sha256": domain_sha256(RUN_PLAN_HASH_DOMAIN, payload)},
        strict=True,
    )


def _parent_projection(
    parent_results: tuple[BridgeRoleResultV1, ...],
) -> tuple[BridgeParentResultV1, ...]:
    return tuple(
        BridgeParentResultV1(
            output=result.output,
            response_bytes_sha256=result.response_bytes_sha256,
            result_sha256=result.result_sha256,
        )
        for result in parent_results
    )


def _project_adjudication_for_report(
    parent: BridgeParentResultV1,
) -> tuple[tuple[BridgeClaimV1, ...], tuple[BridgeClaimV1, ...]]:
    if parent.role is not BridgeRole.ADJUDICATOR:
        raise BridgeContractError("report projection requires exactly one adjudicator parent")
    code_projection = {
        BridgeConclusionCode.ADJUDICATED_SUPPORT: BridgeConclusionCode.REPORT_BOUND,
        BridgeConclusionCode.ADJUDICATED_LIMITED: BridgeConclusionCode.REPORT_LIMITED,
        BridgeConclusionCode.ADJUDICATED_UNKNOWN: BridgeConclusionCode.REPORT_LIMITED,
    }
    evidence_ids = (f"parent-{parent.assignment_id}",)

    def project(claim: BridgeClaimV1) -> BridgeClaimV1:
        try:
            conclusion_code = code_projection[claim.conclusion_code]
        except KeyError as exc:  # pragma: no cover - parent output role validator is authoritative
            raise BridgeContractError(
                "adjudicator parent contains a non-adjudicated claim"
            ) from exc
        return BridgeClaimV1(
            claim_id=claim.claim_id,
            conclusion_code=conclusion_code,
            label=claim.label,
            narrative=claim.narrative,
            evidence_ids=evidence_ids,
        )

    return (
        tuple(project(claim) for claim in parent.output.conclusions),
        tuple(project(claim) for claim in parent.output.uncertainties),
    )


def build_role_request(
    *,
    bridge_run_id: str,
    role: BridgeRole,
    assignment_id: str,
    attempt_id: str,
    expires_at: datetime,
    source_context: BridgeSourceContextV1,
    runtime_policy: BridgeRuntimePolicyV1,
    conformance: BridgeRoleConformanceBindingV1,
    parent_results: tuple[BridgeRoleResultV1, ...] = (),
) -> BoundedCodexBridgeRequestV1:
    try:
        ordinal = BRIDGE_ROLE_ORDER.index(role)
    except ValueError as exc:
        raise BridgeContractError("unknown bridge role") from exc
    parents = _parent_projection(parent_results)
    assignment = BridgeRoleAssignmentV1(
        auth_mode=runtime_policy.auth_mode,
        bridge_run_id=bridge_run_id,
        role=role,
        assignment_id=assignment_id,
        attempt_id=attempt_id,
        parent_assignment_ids=tuple(item.assignment_id for item in parents),
        parent_result_sha256s=tuple(item.result_sha256 for item in parents),
        ordinal=ordinal,
        expires_at=expires_at,
        conformance=conformance,
    )
    envelope_payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "poker-bounded-codex-review-bridge",
        "contract_version": "1.0.0",
        "canonicalization_id": "poker-bounded-codex-bridge-json-v1",
        "producer_runtime": "python-orchestrator",
        "consumer_runtime": (
            "local" if runtime_policy.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY else "codex-native"
        ),
        "assignment": assignment,
        "runtime_policy": runtime_policy,
        "source_context": source_context,
        "parent_results": parents,
    }
    envelope = BridgeContextEnvelopeV1.model_validate(
        {
            **envelope_payload,
            "envelope_sha256": domain_sha256(CONTEXT_HASH_DOMAIN, envelope_payload),
        },
        strict=True,
    )
    required_evidence = _expected_evidence_references_from_envelope(envelope)
    output_schema = _bound_role_output_schema(
        bridge_run_id=assignment.bridge_run_id,
        auth_mode=runtime_policy.auth_mode,
        role=role,
        assignment_id=assignment.assignment_id,
        attempt_id=assignment.attempt_id,
        model=runtime_policy.model,
        model_provider=runtime_policy.model_provider,
        runtime_identity=runtime_policy.runtime_identity,
        evidence_references=required_evidence,
    )
    allowed_codes = allowed_conclusion_codes(role)
    evidence_rule = claim_evidence_rule(role)
    request_payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "poker-bounded-codex-review-bridge",
        "request_kind": "bounded_read_only_role_review",
        "auth_mode": runtime_policy.auth_mode,
        "developer_instructions": _DEVELOPER_INSTRUCTIONS[role],
        "output_schema_sha256": domain_sha256(
            "poker-bounded-codex-bridge-output-schema-v1",
            output_schema,
        ),
        "allowed_conclusion_codes": allowed_codes,
        "allowed_conclusion_labels": (
            BridgeEpistemicLabel.INFERENCE,
            BridgeEpistemicLabel.UNKNOWN,
        ),
        "required_uncertainty_label": BridgeEpistemicLabel.UNKNOWN,
        "required_evidence_references": required_evidence,
        "claim_evidence_rule": evidence_rule,
        "narrative_numbers_allowed": False,
        "narrative_ranges_allowed": False,
        "narrative_citations_allowed": False,
        "calculated_labels_allowed": False,
        "context": envelope,
    }
    request_bytes_sha256 = sha256_bytes(canonical_json_bytes(request_payload))
    request_with_bytes = {**request_payload, "request_bytes_sha256": request_bytes_sha256}
    request = BoundedCodexBridgeRequestV1.model_validate(
        {
            **request_with_bytes,
            "request_sha256": domain_sha256(REQUEST_HASH_DOMAIN, request_with_bytes),
        },
        strict=True,
    )
    if len(canonical_json_bytes(request)) > MAX_CONTEXT_BYTES:
        raise BridgeContractError("role request exceeds exact context byte cap")
    return request


def outbound_request_bytes(request: BoundedCodexBridgeRequestV1) -> bytes:
    """Return the exact confirmed model-input bytes (excluding self-referential hashes)."""

    payload = request.model_dump(mode="json")
    payload.pop("request_sha256")
    payload.pop("request_bytes_sha256")
    data = canonical_json_bytes(payload)
    if sha256_bytes(data) != request.request_bytes_sha256:
        raise BridgeContractError("outbound request bytes do not match the confirmation hash")
    return data


def build_role_confirmation(
    request: BoundedCodexBridgeRequestV1,
    *,
    confirmation_id: str,
    idempotency_key: str,
    authority: BridgeConfirmationAuthorityV1,
    confirmed_at: datetime,
    expires_at: datetime | None = None,
) -> BridgeRoleConfirmationV1:
    expiry = expires_at or confirmed_at + timedelta(seconds=MAX_CONFIRMATION_LIFETIME_SECONDS)
    assignment = request.context.assignment
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "poker-bounded-codex-review-bridge",
        "confirmation_id": confirmation_id,
        "idempotency_key": idempotency_key,
        "bridge_run_id": assignment.bridge_run_id,
        "auth_mode": request.auth_mode,
        "role": assignment.role,
        "assignment_id": assignment.assignment_id,
        "attempt_id": assignment.attempt_id,
        "request_sha256": request.request_sha256,
        "request_bytes_sha256": request.request_bytes_sha256,
        "envelope_sha256": request.context.envelope_sha256,
        "runtime_policy_sha256": request.context.runtime_policy.policy_sha256,
        "runtime_identity": request.context.runtime_policy.runtime_identity,
        "model_provider": request.context.runtime_policy.model_provider,
        "model": request.context.runtime_policy.model,
        "credential_reference": request.context.runtime_policy.credential_reference,
        "authority": authority,
        "confirmed_at": confirmed_at,
        "expires_at": expiry,
        "confirmed": True,
    }
    return BridgeRoleConfirmationV1.model_validate(
        {
            **payload,
            "confirmation_sha256": domain_sha256(CONFIRMATION_HASH_DOMAIN, payload),
        },
        strict=True,
    )


def admit_role_request(
    request: BoundedCodexBridgeRequestV1,
    confirmation: BridgeRoleConfirmationV1,
    *,
    admitted_at: datetime,
    current_source_terminal_manifest_sha256: str,
) -> BridgePreExecutionAdmissionV1:
    assignment = request.context.assignment
    expected = (
        request.auth_mode,
        assignment.bridge_run_id,
        assignment.role,
        assignment.assignment_id,
        assignment.attempt_id,
        request.request_sha256,
        request.request_bytes_sha256,
        request.context.envelope_sha256,
        request.context.runtime_policy.policy_sha256,
        request.context.runtime_policy.runtime_identity,
        request.context.runtime_policy.model_provider,
        request.context.runtime_policy.model,
        request.context.runtime_policy.credential_reference,
    )
    actual = (
        confirmation.auth_mode,
        confirmation.bridge_run_id,
        confirmation.role,
        confirmation.assignment_id,
        confirmation.attempt_id,
        confirmation.request_sha256,
        confirmation.request_bytes_sha256,
        confirmation.envelope_sha256,
        confirmation.runtime_policy_sha256,
        confirmation.runtime_identity,
        confirmation.model_provider,
        confirmation.model,
        confirmation.credential_reference,
    )
    if (
        expected != actual
        or admitted_at < confirmation.confirmed_at
        or admitted_at >= confirmation.expires_at
        or admitted_at >= assignment.expires_at
        or current_source_terminal_manifest_sha256
        != request.context.source_context.source.source_terminal_manifest_sha256
    ):
        raise BridgeContractError("pre-execution admission binding failed")
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "bridge_run_id": assignment.bridge_run_id,
        "auth_mode": request.auth_mode,
        "role": assignment.role,
        "assignment_id": assignment.assignment_id,
        "attempt_id": assignment.attempt_id,
        "request_sha256": request.request_sha256,
        "confirmation_sha256": confirmation.confirmation_sha256,
        "runtime_policy_sha256": request.context.runtime_policy.policy_sha256,
        "runtime_identity": request.context.runtime_policy.runtime_identity,
        "model_provider": request.context.runtime_policy.model_provider,
        "model": request.context.runtime_policy.model,
        "credential_reference": request.context.runtime_policy.credential_reference,
        "source_terminal_manifest_sha256": current_source_terminal_manifest_sha256,
        "admitted_at": admitted_at,
        "expires_at": confirmation.expires_at,
        "effect_state": "not_launched",
    }
    return BridgePreExecutionAdmissionV1.model_validate(
        {**payload, "admission_sha256": domain_sha256(ADMISSION_HASH_DOMAIN, payload)},
        strict=True,
    )


def _expected_evidence_references_from_envelope(
    envelope: BridgeContextEnvelopeV1,
) -> tuple[BridgeEvidenceReferenceV1, ...]:
    source = envelope.source_context.source
    references = [
        BridgeEvidenceReferenceV1(
            evidence_id="source-terminal",
            evidence_kind="source_terminal",
            evidence_sha256=source.source_terminal_manifest_sha256,
        ),
        BridgeEvidenceReferenceV1(
            evidence_id="source-candidate",
            evidence_kind="source_candidate",
            evidence_sha256=source.source_candidate_sha256,
        ),
        BridgeEvidenceReferenceV1(
            evidence_id="source-binding",
            evidence_kind="source_binding",
            evidence_sha256=source.source_binding_sha256,
        ),
        BridgeEvidenceReferenceV1(
            evidence_id="source-result",
            evidence_kind="source_result",
            evidence_sha256=source.source_result_sha256,
        ),
        BridgeEvidenceReferenceV1(
            evidence_id="source-provenance",
            evidence_kind="source_provenance",
            evidence_sha256=source.source_provenance_sha256,
        ),
    ]
    references.extend(
        BridgeEvidenceReferenceV1(
            evidence_id=item.evidence_id,
            evidence_kind="tool_result",
            evidence_sha256=item.result_sha256,
        )
        for item in envelope.source_context.math.tool_support
    )
    references.extend(
        BridgeEvidenceReferenceV1(
            evidence_id=f"parent-{item.assignment_id}",
            evidence_kind=(
                "adjudication" if item.role is BridgeRole.ADJUDICATOR else "role_result"
            ),
            evidence_sha256=item.result_sha256,
        )
        for item in envelope.parent_results
    )
    return tuple(sorted(references, key=lambda item: item.evidence_id.encode("utf-8")))


def expected_evidence_references(
    request: BoundedCodexBridgeRequestV1,
) -> tuple[BridgeEvidenceReferenceV1, ...]:
    expected = _expected_evidence_references_from_envelope(request.context)
    if request.required_evidence_references != expected:
        raise BridgeContractError("request required evidence projection mismatch")
    return expected


def validate_role_response(
    request: BoundedCodexBridgeRequestV1,
    response_bytes: bytes,
) -> BridgeRoleResultV1:
    output = parse_canonical_model(response_bytes, BridgeRoleOutputV1)
    assignment = request.context.assignment
    if (
        output.auth_mode is not request.auth_mode
        or output.bridge_run_id != assignment.bridge_run_id
        or output.role is not assignment.role
        or output.assignment_id != assignment.assignment_id
        or output.attempt_id != assignment.attempt_id
        or output.runtime_identity != request.context.runtime_policy.runtime_identity
        or output.model_provider != request.context.runtime_policy.model_provider
        or output.model != request.context.runtime_policy.model
        or output.evidence_references != expected_evidence_references(request)
    ):
        raise BridgeContractError("role response identity or evidence binding mismatch")
    if assignment.role is BridgeRole.REPORT_WRITER:
        expected_conclusions, expected_uncertainties = _project_adjudication_for_report(
            request.context.parent_results[0]
        )
        if (
            output.conclusions != expected_conclusions
            or output.uncertainties != expected_uncertainties
        ):
            raise BridgeContractError(
                "report writer output is not the deterministic adjudication projection"
            )
    payload: dict[str, object] = {
        "output": output,
        "response_bytes_sha256": sha256_bytes(response_bytes),
    }
    return BridgeRoleResultV1.model_validate(
        {**payload, "result_sha256": domain_sha256(RESULT_HASH_DOMAIN, payload)},
        strict=True,
    )


def build_execution_audit(
    request: BoundedCodexBridgeRequestV1,
    confirmation: BridgeRoleConfirmationV1,
    admission: BridgePreExecutionAdmissionV1,
    *,
    transport_qualification: Literal["deterministic_fixture", "actual_live"],
    effect_state: BridgeEffectState,
    thread_id_sha256: str | None,
    turn_id_sha256: str | None,
    launched_at: datetime | None,
    completed_at: datetime | None,
    duration_ms: int | None,
    usage: BridgeTransportUsageV1 | None,
    response_bytes: int | None,
    stream_bytes: int | None,
    unexpected_item_types: tuple[str, ...],
    cancellation_kind: Literal["not_requested", "cooperative", "unconfirmed"],
    result_sha256: str | None,
    failure_reason_code: str | None,
    model_identity_evidence: Literal[
        "direct_observation",
        "requested_pinned_no_fallback_no_reroute",
        "unavailable",
    ],
    observed_model: str | None,
    observed_model_provider: str | None,
    observed_reasoning_effort: str | None,
    observed_service_tier: str | None,
    observed_identity_sha256: str | None,
    live_execution_evidence: CodexSubscriptionLiveExecutionEvidenceV1 | None = None,
) -> BridgeExecutionAuditV1:
    assignment = request.context.assignment
    payload: dict[str, object] = {
        "schema_version": EXECUTION_AUDIT_SCHEMA_VERSION,
        "bridge_run_id": assignment.bridge_run_id,
        "auth_mode": request.auth_mode,
        "role": assignment.role,
        "assignment_id": assignment.assignment_id,
        "attempt_id": assignment.attempt_id,
        "request_sha256": request.request_sha256,
        "confirmation_sha256": confirmation.confirmation_sha256,
        "admission_sha256": admission.admission_sha256,
        "runtime_policy_sha256": request.context.runtime_policy.policy_sha256,
        "transport_qualification": transport_qualification,
        "live_execution_evidence": live_execution_evidence,
        "interface": request.context.runtime_policy.interface,
        "credential_reference": request.context.runtime_policy.credential_reference,
        "remote_retention_policy": request.context.runtime_policy.remote_retention_policy,
        "runtime_identity": request.context.runtime_policy.runtime_identity,
        "model_identity_evidence": model_identity_evidence,
        "requested_model": request.context.runtime_policy.model,
        "observed_model": observed_model,
        "requested_model_provider": request.context.runtime_policy.model_provider,
        "observed_model_provider": observed_model_provider,
        "reasoning_effort": request.context.runtime_policy.reasoning_effort,
        "observed_reasoning_effort": observed_reasoning_effort,
        "service_tier": request.context.runtime_policy.service_tier,
        "observed_service_tier": observed_service_tier,
        "observed_identity_sha256": observed_identity_sha256,
        "effect_state": effect_state,
        "thread_id_sha256": thread_id_sha256,
        "turn_id_sha256": turn_id_sha256,
        "launched_at": launched_at,
        "completed_at": completed_at,
        "duration_ms": duration_ms,
        "usage": usage,
        "response_bytes": response_bytes,
        "stream_bytes": stream_bytes,
        "unexpected_item_types": unexpected_item_types,
        "cancellation_kind": cancellation_kind,
        "automatic_retry_count": 0,
        "result_sha256": result_sha256,
        "failure_reason_code": failure_reason_code,
    }
    return BridgeExecutionAuditV1.model_validate(
        {
            **payload,
            "audit_sha256": domain_sha256(EXECUTION_AUDIT_HASH_DOMAIN, payload),
        },
        strict=True,
    )


def assert_no_replay(
    *,
    request: BoundedCodexBridgeRequestV1,
    existing_attempts: Mapping[tuple[RuntimeAuthModeV1, str, str, str], str],
) -> None:
    assignment = request.context.assignment
    key = (
        request.auth_mode,
        assignment.bridge_run_id,
        assignment.assignment_id,
        assignment.attempt_id,
    )
    existing = existing_attempts.get(key)
    if existing is not None:
        if existing == request.request_sha256:
            raise BridgeContractError("duplicate execution is forbidden even for identical bytes")
        raise BridgeContractError("attempt identifier was replayed with mutated bytes")


def utc_now() -> datetime:
    return datetime.now(UTC)


__all__ = [
    "BridgeContractError",
    "admit_role_request",
    "assert_no_replay",
    "build_execution_audit",
    "build_role_confirmation",
    "build_role_request",
    "build_run_plan",
    "build_runtime_policy",
    "expected_evidence_references",
    "outbound_request_bytes",
    "role_output_schema",
    "role_output_schema_for_request",
    "utc_now",
    "validate_role_response",
]
