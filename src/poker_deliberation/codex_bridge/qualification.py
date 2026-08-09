"""Strict public fixture and sanitized actual-live qualification manifest contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    domain_sha256,
    sha256_bytes,
    without_field,
)
from poker_deliberation.codex_bridge.contracts import outbound_request_bytes
from poker_deliberation.codex_bridge.controller import role_artifact_name
from poker_deliberation.codex_bridge.identity import (
    BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
    bridge_runtime_source_inventory,
    bridge_runtime_source_inventory_sha256,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_MODEL_ID,
    BRIDGE_REASONING_EFFORT,
    BRIDGE_ROLE_ORDER,
    BRIDGE_RUNTIME_BINARY_SHA256,
    BRIDGE_SERVICE_TIER,
    BRIDGE_SUBSCRIPTION_RUNTIME_ID,
    BoundedCodexBridgeRequestV1,
    BridgeEffectState,
    BridgeExecutionAuditV1,
    BridgePreExecutionAdmissionV1,
    BridgeRole,
    BridgeRoleConfirmationV1,
    BridgeRoleResultV1,
    BridgeRunPlanV1,
    BridgeSourceContextV1,
    BridgeTransportUsageV1,
    CodexSubscriptionLiveExecutionEvidenceV1,
    GitObjectId,
    PortableId,
    RuntimeAuthModeV1,
    Sha256,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.storage import VerifiedBridgeRead

QUALIFICATION_SCHEMA_VERSION: Final[Literal["2.0.0"]] = "2.0.0"
QUALIFICATION_MANIFEST_HASH_DOMAIN: Final = "poker-bounded-codex-subscription-live-qualification-v2"
PUBLIC_SYNTHETIC_FIXTURE_ID: Final = "p2-025b-public-river-call-positive-v1"
QUALIFICATION_LIMITATIONS: Final = (
    "actual_backend_model_input_UNKNOWN",
    "api_live_not_executed",
    "backend_immutable_model_snapshot_UNKNOWN",
    "human_usefulness_UNKNOWN",
    "provider_internal_retry_UNKNOWN",
    "remote_cancel_finality_UNKNOWN",
    "strategy_quality_UNKNOWN",
    "workspace_retention_policy_UNKNOWN",
)


class _QualificationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class PublicSyntheticQualificationFixtureV1(_QualificationModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    fixture_id: Literal["p2-025b-public-river-call-positive-v1"]
    source_builder: Literal["build_repository_owned_bounded_river_evaluation_admission"]
    source_terminal_run_id: PortableId
    range_notation: Literal["QcJc"]
    source_kind: Literal["repository_fixture"]
    license_classification: Literal["repository_owned_mit"]
    usage_classification: Literal["redistribution_allowed"]
    content_classification: Literal["public"]
    model_processing_authorized: Literal[True]
    raw_japanese_source_outbound: Literal[False]
    auth_mode: Literal[RuntimeAuthModeV1.CODEX_SUBSCRIPTION]
    runtime_identity: Literal["openai-codex-cli/0.144.4"]
    runtime_binary_sha256: Sha256
    package: Literal["openai-codex==0.144.4"]
    package_license: Literal["Apache-2.0"]
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal["medium"]
    service_tier: Literal["default"]
    role_order: tuple[BridgeRole, ...] = Field(min_length=5, max_length=5)
    expected_turns: Literal[5]
    api_live_in_scope: Literal[False]

    @model_validator(mode="after")
    def exact_qualification_fixture(self) -> PublicSyntheticQualificationFixtureV1:
        if (
            self.role_order != BRIDGE_ROLE_ORDER
            or self.runtime_identity != BRIDGE_SUBSCRIPTION_RUNTIME_ID
            or self.runtime_binary_sha256 != BRIDGE_RUNTIME_BINARY_SHA256
            or self.model != BRIDGE_MODEL_ID
            or self.reasoning_effort != BRIDGE_REASONING_EFFORT
            or self.service_tier != BRIDGE_SERVICE_TIER
        ):
            raise ValueError("public qualification fixture identity mismatch")
        return self


def load_public_synthetic_fixture(path: Path) -> PublicSyntheticQualificationFixtureV1:
    try:
        return PublicSyntheticQualificationFixtureV1.model_validate_json(
            path.read_bytes(),
            strict=True,
        )
    except Exception as exc:
        raise ValueError("public qualification fixture failed strict schema validation") from exc


class SanitizedQualificationRoleV2(_QualificationModel):
    role: BridgeRole
    assignment_id: PortableId
    attempt_id: PortableId
    parent_assignment_ids: tuple[PortableId, ...] = Field(max_length=3)
    request_sha256: Sha256
    request_bytes_sha256: Sha256
    outbound_bytes: int = Field(gt=0, le=65_536)
    outbound_canonical_utf8: str = Field(min_length=1, max_length=65_536)
    envelope_sha256: Sha256
    confirmation_sha256: Sha256
    admission_sha256: Sha256
    result_sha256: Sha256
    execution_audit_sha256: Sha256
    live_execution_evidence: CodexSubscriptionLiveExecutionEvidenceV1
    effect_state: Literal[BridgeEffectState.SUCCEEDED]
    transport_qualification: Literal["actual_live"]
    model_identity_evidence: Literal["requested_pinned_no_fallback_no_reroute"]
    thread_id_sha256: Sha256
    turn_id_sha256: Sha256
    usage: BridgeTransportUsageV1
    duration_ms: int = Field(ge=0, le=120_000)
    response_bytes: int = Field(gt=0, le=32_768)
    stream_bytes: int = Field(gt=0, le=262_144)
    unexpected_item_types: tuple[()] = ()

    @model_validator(mode="after")
    def exact_outbound_bytes(self) -> SanitizedQualificationRoleV2:
        try:
            outbound = self.outbound_canonical_utf8.encode("utf-8")
        except UnicodeEncodeError as exc:  # pragma: no cover - Python str invariant
            raise ValueError("qualification outbound text is not UTF-8") from exc
        if (
            len(outbound) != self.outbound_bytes
            or sha256_bytes(outbound) != self.request_bytes_sha256
            or self.live_execution_evidence.request_sha256 != self.request_sha256
            or self.live_execution_evidence.request_bytes_sha256 != self.request_bytes_sha256
            or self.live_execution_evidence.thread_id_sha256 != self.thread_id_sha256
            or self.live_execution_evidence.turn_id_sha256 != self.turn_id_sha256
        ):
            raise ValueError("qualification outbound byte binding mismatch")
        return self


class SanitizedRuntimeSourceFileV1(_QualificationModel):
    path: str = Field(pattern=r"^[A-Za-z0-9_.][A-Za-z0-9_./-]{0,254}$")
    size: int = Field(ge=1, le=2_000_000)
    sha256: Sha256


class SanitizedLiveQualificationManifestV2(_QualificationModel):
    schema_version: Literal["2.0.0"] = QUALIFICATION_SCHEMA_VERSION
    qualification_id: PortableId
    qualification_status: Literal["passed"]
    qualified_scope: Literal["codex_subscription_bounded_river_review_only"]
    fixture_id: Literal["p2-025b-public-river-call-positive-v1"]
    auth_mode: Literal[RuntimeAuthModeV1.CODEX_SUBSCRIPTION]
    api_live_executed: Literal[False]
    api_production_qualified: Literal[False]
    repository_commit_id: GitObjectId
    repository_tree_id: GitObjectId
    runtime_source_inventory_hash_domain: Literal["poker-bounded-codex-runtime-source-inventory-v1"]
    runtime_source_inventory: tuple[SanitizedRuntimeSourceFileV1, ...] = Field(
        min_length=1,
        max_length=512,
    )
    runtime_source_inventory_sha256: Sha256
    bridge_run_id: PortableId
    source_terminal_run_id: PortableId
    source_terminal_manifest_sha256: Sha256
    source_result_sha256: Sha256
    source_context_sha256: Sha256
    runtime_interface: Literal["codex_exec_json"]
    runtime_identity: Literal["openai-codex-cli/0.144.4"]
    runtime_binary_sha256: Sha256
    package: Literal["openai-codex==0.144.4"]
    package_license: Literal["Apache-2.0"]
    model: Literal["gpt-5.6-terra"]
    reasoning_effort: Literal["medium"]
    service_tier: Literal["default"]
    model_identity_basis: Literal["pinned_launch_configuration_and_transport_contract"]
    configured_model_provider: Literal["openai"]
    auth_boundary: Literal["chatgpt"]
    auth_enforcement: Literal["same_process_forced_login_method_chatgpt"]
    provider_model_fallback_allowed: Literal[False]
    model_reroute_observed: Literal[False]
    effective_model_identity_status: Literal["UNKNOWN_codex_exec_json_not_exposed"]
    application_outbound_scope: Literal["application_owned_canonical_stdin"]
    actual_backend_model_input_status: Literal["UNKNOWN_codex_exec_json_not_exposed"]
    backend_immutable_model_snapshot: Literal["UNKNOWN"]
    credential_reference: Literal["codex_home:saved_chatgpt_login"]
    credential_values_published: Literal[False]
    trace_policy: Literal["validated_typed_public_raw_local_only"]
    raw_trace_published: Literal[False]
    raw_japanese_source_outbound: Literal[False]
    remote_retention_policy: Literal["chatgpt_workspace_policy_unknown"]
    provider_internal_retry_status: Literal["UNKNOWN"]
    product_retry_count: Literal[0]
    roles: tuple[SanitizedQualificationRoleV2, ...] = Field(min_length=5, max_length=5)
    total_input_tokens: int = Field(ge=0, le=120_000)
    total_output_tokens: int = Field(ge=0, le=30_000)
    terminal_revision: int = Field(ge=1)
    terminal_manifest_sha256: Sha256
    terminal_inventory_sha256: Sha256
    terminal_completion_marker_sha256: Sha256
    deterministic_evaluation_sha256: Sha256
    limitations: tuple[str, ...] = Field(min_length=8, max_length=8)
    manifest_sha256: Sha256

    @field_validator("limitations")
    @classmethod
    def exact_limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != QUALIFICATION_LIMITATIONS:
            raise ValueError("qualification limitations mismatch")
        return value

    @model_validator(mode="after")
    def exact_live_qualification(self) -> SanitizedLiveQualificationManifestV2:
        source_inventory_payload = [
            item.model_dump(mode="json") for item in self.runtime_source_inventory
        ]
        if (
            tuple(item.role for item in self.roles) != BRIDGE_ROLE_ORDER
            or len({item.assignment_id for item in self.roles}) != 5
            or len({item.attempt_id for item in self.roles}) != 5
            or len({item.thread_id_sha256 for item in self.roles}) != 5
            or len({item.turn_id_sha256 for item in self.roles}) != 5
            or len({item.live_execution_evidence.attestation_sha256 for item in self.roles}) != 5
            or self.runtime_identity != BRIDGE_SUBSCRIPTION_RUNTIME_ID
            or self.runtime_binary_sha256 != BRIDGE_RUNTIME_BINARY_SHA256
            or self.model != BRIDGE_MODEL_ID
            or self.reasoning_effort != BRIDGE_REASONING_EFFORT
            or self.service_tier != BRIDGE_SERVICE_TIER
            or any(
                item.model_identity_evidence != "requested_pinned_no_fallback_no_reroute"
                for item in self.roles
            )
            or sum(item.usage.input_tokens for item in self.roles) != self.total_input_tokens
            or sum(item.usage.output_tokens for item in self.roles) != self.total_output_tokens
            or any(item.usage.estimated_cost_micro_usd is not None for item in self.roles)
            or any(
                item.live_execution_evidence.runtime_source_inventory_sha256
                != self.runtime_source_inventory_sha256
                for item in self.roles
            )
            or tuple(item.path for item in self.runtime_source_inventory)
            != tuple(sorted(item.path for item in self.runtime_source_inventory))
            or len({item.path for item in self.runtime_source_inventory})
            != len(self.runtime_source_inventory)
            or self.runtime_source_inventory_sha256
            != domain_sha256(
                BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
                source_inventory_payload,
            )
        ):
            raise ValueError("qualification role or runtime inventory mismatch")
        expected_parents = (
            (),
            (),
            (),
            tuple(item.assignment_id for item in self.roles[:3]),
            (self.roles[3].assignment_id,),
        )
        if tuple(item.parent_assignment_ids for item in self.roles) != expected_parents:
            raise ValueError("qualification parent lineage mismatch")
        if self.manifest_sha256 != domain_sha256(
            QUALIFICATION_MANIFEST_HASH_DOMAIN,
            without_field(self, "manifest_sha256"),
        ):
            raise ValueError("qualification manifest hash mismatch")
        return self


def build_sanitized_live_qualification_manifest(
    read: VerifiedBridgeRead,
    *,
    repository_root: Path,
    qualification_id: str,
    deterministic_evaluation_sha256: str,
) -> SanitizedLiveQualificationManifestV2:
    """Build a public manifest only from a complete verified subscription terminal replay."""

    replayed = replay_bridge(read)
    if (
        replayed.status != "succeeded"
        or replayed.auth_mode is not RuntimeAuthModeV1.CODEX_SUBSCRIPTION
        or replayed.completed_roles != BRIDGE_ROLE_ORDER
        or read.completion_marker is None
    ):
        raise ValueError("subscription bridge run is not qualified for public projection")
    completion_marker_sha256 = read.pointer.completion_marker_sha256
    if (
        completion_marker_sha256 is None
        or read.completion_marker_bytes is None
        or sha256_bytes(read.completion_marker_bytes) != completion_marker_sha256
    ):
        raise ValueError("qualification completion marker binding is missing")
    artifacts = {item.logical_name: item.model for item in read.decoded_artifacts()}
    plan = artifacts.get("run_plan.json")
    source = artifacts.get("source_context.json")
    if not isinstance(plan, BridgeRunPlanV1) or not isinstance(source, BridgeSourceContextV1):
        raise ValueError("qualification run anchors are missing")
    policy = None
    roles: list[SanitizedQualificationRoleV2] = []
    for role in BRIDGE_ROLE_ORDER:
        request = artifacts.get(role_artifact_name(role, "request"))
        confirmation = artifacts.get(role_artifact_name(role, "confirmation"))
        admission = artifacts.get(role_artifact_name(role, "admission"))
        result = artifacts.get(role_artifact_name(role, "result"))
        audit = artifacts.get(role_artifact_name(role, "audit"))
        if (
            not isinstance(request, BoundedCodexBridgeRequestV1)
            or not isinstance(confirmation, BridgeRoleConfirmationV1)
            or not isinstance(admission, BridgePreExecutionAdmissionV1)
            or not isinstance(result, BridgeRoleResultV1)
            or not isinstance(audit, BridgeExecutionAuditV1)
            or audit.effect_state is not BridgeEffectState.SUCCEEDED
            or audit.transport_qualification != "actual_live"
            or audit.live_execution_evidence is None
            or audit.model_identity_evidence != "requested_pinned_no_fallback_no_reroute"
            or audit.observed_model is not None
            or audit.observed_model_provider is not None
            or audit.observed_reasoning_effort is not None
            or audit.observed_service_tier is not None
            or audit.observed_identity_sha256 is not None
            or audit.usage is None
            or audit.thread_id_sha256 is None
            or audit.turn_id_sha256 is None
            or audit.duration_ms is None
            or audit.response_bytes is None
            or audit.stream_bytes is None
        ):
            raise ValueError("qualification role evidence is incomplete")
        if policy is None:
            policy = request.context.runtime_policy
        elif policy != request.context.runtime_policy:
            raise ValueError("qualification runtime policy changed across roles")
        outbound = outbound_request_bytes(request)
        roles.append(
            SanitizedQualificationRoleV2(
                role=role,
                assignment_id=request.context.assignment.assignment_id,
                attempt_id=request.context.assignment.attempt_id,
                parent_assignment_ids=request.context.assignment.parent_assignment_ids,
                request_sha256=request.request_sha256,
                request_bytes_sha256=request.request_bytes_sha256,
                outbound_bytes=len(outbound),
                outbound_canonical_utf8=outbound.decode("utf-8"),
                envelope_sha256=request.context.envelope_sha256,
                confirmation_sha256=confirmation.confirmation_sha256,
                admission_sha256=admission.admission_sha256,
                result_sha256=result.result_sha256,
                execution_audit_sha256=audit.audit_sha256,
                live_execution_evidence=audit.live_execution_evidence,
                effect_state=BridgeEffectState.SUCCEEDED,
                transport_qualification="actual_live",
                model_identity_evidence="requested_pinned_no_fallback_no_reroute",
                thread_id_sha256=audit.thread_id_sha256,
                turn_id_sha256=audit.turn_id_sha256,
                usage=audit.usage,
                duration_ms=audit.duration_ms,
                response_bytes=audit.response_bytes,
                stream_bytes=audit.stream_bytes,
                unexpected_item_types=(),
            )
        )
    if policy is None:
        raise ValueError("qualification runtime policy is absent")
    payload: dict[str, object] = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "qualification_id": qualification_id,
        "qualification_status": "passed",
        "qualified_scope": "codex_subscription_bounded_river_review_only",
        "fixture_id": PUBLIC_SYNTHETIC_FIXTURE_ID,
        "auth_mode": RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        "api_live_executed": False,
        "api_production_qualified": False,
        "repository_commit_id": plan.repository_commit_id,
        "repository_tree_id": plan.repository_tree_id,
        "runtime_source_inventory_hash_domain": (BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN),
        "runtime_source_inventory": tuple(
            SanitizedRuntimeSourceFileV1(
                path=item.path,
                size=item.size,
                sha256=item.sha256,
            )
            for item in bridge_runtime_source_inventory(repository_root)
        ),
        "runtime_source_inventory_sha256": (
            bridge_runtime_source_inventory_sha256(repository_root)
        ),
        "bridge_run_id": plan.bridge_run_id,
        "source_terminal_run_id": source.source.source_terminal_run_id,
        "source_terminal_manifest_sha256": source.source.source_terminal_manifest_sha256,
        "source_result_sha256": source.source.source_result_sha256,
        "source_context_sha256": source.context_payload_sha256,
        "runtime_interface": "codex_exec_json",
        "runtime_identity": BRIDGE_SUBSCRIPTION_RUNTIME_ID,
        "runtime_binary_sha256": BRIDGE_RUNTIME_BINARY_SHA256,
        "package": "openai-codex==0.144.4",
        "package_license": "Apache-2.0",
        "model": BRIDGE_MODEL_ID,
        "reasoning_effort": BRIDGE_REASONING_EFFORT,
        "service_tier": BRIDGE_SERVICE_TIER,
        "model_identity_basis": "pinned_launch_configuration_and_transport_contract",
        "configured_model_provider": "openai",
        "auth_boundary": "chatgpt",
        "auth_enforcement": "same_process_forced_login_method_chatgpt",
        "provider_model_fallback_allowed": False,
        "model_reroute_observed": False,
        "effective_model_identity_status": "UNKNOWN_codex_exec_json_not_exposed",
        "application_outbound_scope": "application_owned_canonical_stdin",
        "actual_backend_model_input_status": "UNKNOWN_codex_exec_json_not_exposed",
        "backend_immutable_model_snapshot": "UNKNOWN",
        "credential_reference": "codex_home:saved_chatgpt_login",
        "credential_values_published": False,
        "trace_policy": policy.trace_policy,
        "raw_trace_published": False,
        "raw_japanese_source_outbound": False,
        "remote_retention_policy": policy.remote_retention_policy,
        "provider_internal_retry_status": policy.provider_internal_retry_status,
        "product_retry_count": 0,
        "roles": tuple(roles),
        "total_input_tokens": replayed.total_input_tokens,
        "total_output_tokens": replayed.total_output_tokens,
        "terminal_revision": read.pointer.revision,
        "terminal_manifest_sha256": read.manifest.manifest_sha256,
        "terminal_inventory_sha256": read.manifest.inventory_sha256,
        "terminal_completion_marker_sha256": completion_marker_sha256,
        "deterministic_evaluation_sha256": deterministic_evaluation_sha256,
        "limitations": QUALIFICATION_LIMITATIONS,
    }
    return SanitizedLiveQualificationManifestV2.model_validate(
        {
            **payload,
            "manifest_sha256": domain_sha256(QUALIFICATION_MANIFEST_HASH_DOMAIN, payload),
        },
        strict=True,
    )


def write_sanitized_live_qualification_manifest(
    path: Path,
    manifest: SanitizedLiveQualificationManifestV2,
) -> None:
    """Exclusively publish one already-validated public canonical manifest."""

    data = canonical_json_bytes(manifest)
    with path.open("xb") as stream:
        stream.write(data)


__all__ = [
    "PUBLIC_SYNTHETIC_FIXTURE_ID",
    "QUALIFICATION_LIMITATIONS",
    "QUALIFICATION_MANIFEST_HASH_DOMAIN",
    "QUALIFICATION_SCHEMA_VERSION",
    "PublicSyntheticQualificationFixtureV1",
    "SanitizedLiveQualificationManifestV2",
    "SanitizedQualificationRoleV2",
    "SanitizedRuntimeSourceFileV1",
    "build_sanitized_live_qualification_manifest",
    "load_public_synthetic_fixture",
    "write_sanitized_live_qualification_manifest",
]
