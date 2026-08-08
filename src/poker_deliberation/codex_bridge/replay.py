"""Complete immutable replay for a bounded Codex bridge revision."""

from __future__ import annotations

from dataclasses import dataclass

from poker_deliberation.codex_bridge.canonical import canonical_json_bytes, domain_sha256
from poker_deliberation.codex_bridge.contracts import (
    admit_role_request,
    build_role_request,
    build_run_plan,
    validate_role_response,
)
from poker_deliberation.codex_bridge.controller import (
    canonical_assignment_id,
    canonical_attempt_id,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    OBSERVED_TRANSPORT_IDENTITY_HASH_DOMAIN,
    BoundedCodexBridgeRequestV1,
    BridgeEffectState,
    BridgeExecutionAuditV1,
    BridgePreExecutionAdmissionV1,
    BridgeRole,
    BridgeRoleConfirmationV1,
    BridgeRoleResultV1,
    BridgeRunPlanV1,
    BridgeSourceContextV1,
    BridgeTerminalStatus,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.storage import VerifiedBridgeRead


class BridgeReplayError(ValueError):
    """Raised when a structurally valid revision violates bridge semantics."""


@dataclass(frozen=True, slots=True)
class BridgeReplayResult:
    bridge_run_id: str
    auth_mode: RuntimeAuthModeV1
    revision: int
    status: BridgeTerminalStatus
    completed_roles: tuple[BridgeRole, ...]
    pending_roles: tuple[BridgeRole, ...]
    reconciliation_required: bool
    total_input_tokens: int
    total_output_tokens: int
    total_estimated_cost_micro_usd: int | None


def _artifact_map(read: VerifiedBridgeRead):  # type: ignore[no-untyped-def]
    return {item.logical_name: item.model for item in read.decoded_artifacts()}


def _require(model_map: dict[str, object], name: str, expected: type[object]) -> object:
    try:
        value = model_map[name]
    except KeyError as exc:
        raise BridgeReplayError(f"required bridge artifact is absent: {name}") from exc
    if not isinstance(value, expected):
        raise BridgeReplayError(f"bridge artifact schema mismatch: {name}")
    return value


def replay_bridge(read: VerifiedBridgeRead) -> BridgeReplayResult:
    """Replay anchors, confirmations, admissions, results, audits, lineage, and budgets."""

    artifacts = _artifact_map(read)
    plan = _require(artifacts, "run_plan.json", BridgeRunPlanV1)
    source = _require(artifacts, "source_context.json", BridgeSourceContextV1)
    assert isinstance(plan, BridgeRunPlanV1)
    assert isinstance(source, BridgeSourceContextV1)
    if (
        read.pointer.bridge_run_id != plan.bridge_run_id
        or read.pointer.auth_mode is not plan.auth_mode
        or read.manifest.auth_mode is not plan.auth_mode
        or read.manifest.runtime_policy_sha256 != plan.runtime_policy_sha256
        or read.manifest.run_plan_sha256 != plan.plan_sha256
        or read.manifest.source_terminal_manifest_sha256
        != source.source.source_terminal_manifest_sha256
        or plan.source != source.source
    ):
        raise BridgeReplayError("bridge storage anchors do not correlate")

    completed: list[BridgeRole] = []
    input_tokens = 0
    output_tokens = 0
    estimated_cost = 0
    estimated_cost_known = plan.auth_mode is RuntimeAuthModeV1.OPENAI_API
    accepted_input_tokens = 0
    accepted_output_tokens = 0
    accepted_estimated_cost = 0
    open_admission = False
    assignments: set[str] = set()
    attempts: set[str] = set()
    thread_ids: set[str] = set()
    turn_ids: set[str] = set()
    known_names = {"run_plan.json", "source_context.json"}
    results: dict[BridgeRole, BridgeRoleResultV1] = {}
    terminal_failure: BridgeExecutionAuditV1 | None = None
    runtime_policy = None

    for ordinal, role in enumerate(BRIDGE_ROLE_ORDER):
        request_name = role_artifact_name(role, "request")
        confirmation_name = role_artifact_name(role, "confirmation")
        admission_name = role_artifact_name(role, "admission")
        result_name = role_artifact_name(role, "result")
        audit_name = role_artifact_name(role, "audit")
        role_names = {
            request_name,
            confirmation_name,
            admission_name,
            result_name,
            audit_name,
        }
        known_names.update(role_names)
        present = role_names & set(artifacts)
        if not present:
            continue
        request = _require(artifacts, request_name, BoundedCodexBridgeRequestV1)
        assert isinstance(request, BoundedCodexBridgeRequestV1)
        assignment = request.context.assignment
        if (
            assignment.bridge_run_id != plan.bridge_run_id
            or request.auth_mode is not plan.auth_mode
            or assignment.auth_mode is not plan.auth_mode
            or assignment.role is not role
            or assignment.ordinal != ordinal
            or assignment.assignment_id
            != canonical_assignment_id(plan.bridge_run_id, plan.auth_mode, role)
            or assignment.attempt_id
            != canonical_attempt_id(plan.bridge_run_id, plan.auth_mode, role)
            or request.context.source_context != source
            or request.context.runtime_policy.policy_sha256 != plan.runtime_policy_sha256
            or assignment.assignment_id in assignments
            or assignment.attempt_id in attempts
        ):
            raise BridgeReplayError("bridge request identity or context replay failed")
        assignments.add(assignment.assignment_id)
        attempts.add(assignment.attempt_id)
        if runtime_policy is None:
            runtime_policy = request.context.runtime_policy
        elif runtime_policy != request.context.runtime_policy:
            raise BridgeReplayError("bridge runtime policy mutated across roles")
        if role in BRIDGE_ROLE_ORDER[:3]:
            expected_parents: tuple[BridgeRoleResultV1, ...] = ()
        elif role is BridgeRole.ADJUDICATOR:
            expected_parents = tuple(results[item] for item in BRIDGE_ROLE_ORDER[:3])
        else:
            expected_parents = (results[BridgeRole.ADJUDICATOR],)
        if tuple(item.result_sha256 for item in expected_parents) != (
            assignment.parent_result_sha256s
        ):
            raise BridgeReplayError("bridge parent result lineage failed replay")
        rebuilt_request = build_role_request(
            bridge_run_id=plan.bridge_run_id,
            role=role,
            assignment_id=assignment.assignment_id,
            attempt_id=assignment.attempt_id,
            expires_at=assignment.expires_at,
            source_context=source,
            runtime_policy=request.context.runtime_policy,
            conformance=plan.role_conformance[ordinal],
            parent_results=expected_parents,
        )
        if rebuilt_request != request:
            raise BridgeReplayError("bridge role request failed deterministic replay")
        if ordinal == 0:
            rebuilt_plan = build_run_plan(
                bridge_run_id=plan.bridge_run_id,
                source_context=source,
                runtime_policy=request.context.runtime_policy,
                role_conformance=plan.role_conformance,
                repository_commit_id=plan.repository_commit_id,
                repository_tree_id=plan.repository_tree_id,
                created_at=plan.created_at,
            )
            if rebuilt_plan != plan:
                raise BridgeReplayError("bridge run plan failed deterministic replay")

        confirmation = artifacts.get(confirmation_name)
        admission = artifacts.get(admission_name)
        result = artifacts.get(result_name)
        audit = artifacts.get(audit_name)
        if confirmation is None:
            if any(item is not None for item in (admission, result, audit)):
                raise BridgeReplayError("bridge execution exists without confirmation")
            continue
        if not isinstance(confirmation, BridgeRoleConfirmationV1):
            raise BridgeReplayError("bridge confirmation schema mismatch")
        if (
            confirmation.bridge_run_id != plan.bridge_run_id
            or confirmation.auth_mode is not plan.auth_mode
            or confirmation.role is not role
            or confirmation.assignment_id != assignment.assignment_id
            or confirmation.attempt_id != assignment.attempt_id
            or confirmation.request_sha256 != request.request_sha256
            or confirmation.request_bytes_sha256 != request.request_bytes_sha256
            or confirmation.envelope_sha256 != request.context.envelope_sha256
            or confirmation.runtime_policy_sha256 != request.context.runtime_policy.policy_sha256
            or confirmation.runtime_identity != request.context.runtime_policy.runtime_identity
            or confirmation.model_provider != request.context.runtime_policy.model_provider
            or confirmation.model != request.context.runtime_policy.model
            or confirmation.credential_reference
            != request.context.runtime_policy.credential_reference
        ):
            raise BridgeReplayError("bridge confirmation failed replay")
        if admission is None:
            if result is not None or audit is not None:
                raise BridgeReplayError("bridge result exists without admission")
            continue
        if not isinstance(admission, BridgePreExecutionAdmissionV1):
            raise BridgeReplayError("bridge admission schema mismatch")
        rebuilt_admission = admit_role_request(
            request,
            confirmation,
            admitted_at=admission.admitted_at,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        )
        if rebuilt_admission != admission:
            raise BridgeReplayError("bridge pre-execution admission failed replay")
        if audit is None:
            if result is not None:
                raise BridgeReplayError("bridge result exists without execution audit")
            open_admission = True
            continue
        if not isinstance(audit, BridgeExecutionAuditV1):
            raise BridgeReplayError("bridge execution audit schema mismatch")
        if (
            audit.bridge_run_id != plan.bridge_run_id
            or audit.auth_mode is not plan.auth_mode
            or audit.role is not role
            or audit.assignment_id != assignment.assignment_id
            or audit.attempt_id != assignment.attempt_id
            or audit.request_sha256 != request.request_sha256
            or audit.confirmation_sha256 != confirmation.confirmation_sha256
            or audit.admission_sha256 != admission.admission_sha256
            or audit.runtime_policy_sha256 != request.context.runtime_policy.policy_sha256
            or audit.interface != request.context.runtime_policy.interface
            or audit.credential_reference != request.context.runtime_policy.credential_reference
            or audit.remote_retention_policy
            != request.context.runtime_policy.remote_retention_policy
            or audit.runtime_identity != request.context.runtime_policy.runtime_identity
            or audit.requested_model != request.context.runtime_policy.model
            or audit.requested_model_provider != request.context.runtime_policy.model_provider
            or audit.reasoning_effort != request.context.runtime_policy.reasoning_effort
            or audit.service_tier != request.context.runtime_policy.service_tier
        ):
            raise BridgeReplayError("bridge execution audit binding failed replay")
        expected_cancellation_kind = {
            BridgeEffectState.CANCELLED: "cooperative",
            BridgeEffectState.CANCEL_UNCONFIRMED: "unconfirmed",
        }.get(audit.effect_state, "not_requested")
        if audit.cancellation_kind != expected_cancellation_kind:
            raise BridgeReplayError("bridge cancellation evidence failed replay")
        if audit.usage is not None:
            input_tokens += audit.usage.input_tokens
            output_tokens += audit.usage.output_tokens
            if plan.auth_mode is RuntimeAuthModeV1.OPENAI_API:
                if audit.usage.estimated_cost_micro_usd is None:
                    estimated_cost_known = False
                else:
                    estimated_cost += audit.usage.estimated_cost_micro_usd
        if audit.thread_id_sha256 is not None:
            if (
                audit.thread_id_sha256 in thread_ids
                or audit.turn_id_sha256 is None
                or audit.turn_id_sha256 in turn_ids
            ):
                raise BridgeReplayError("bridge thread or turn identity was reused")
            thread_ids.add(audit.thread_id_sha256)
            turn_ids.add(audit.turn_id_sha256)
        if audit.effect_state is BridgeEffectState.SUCCEEDED:
            if not isinstance(result, BridgeRoleResultV1):
                raise BridgeReplayError("successful bridge audit lacks a role result")
            rebuilt_result = validate_role_response(
                request,
                canonical_json_bytes(result.output),
            )
            if rebuilt_result != result or audit.result_sha256 != result.result_sha256:
                raise BridgeReplayError("bridge role result failed replay")
            if plan.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION:
                identity_failed = (
                    audit.model_identity_evidence != "requested_pinned_no_fallback_no_reroute"
                    or audit.observed_model is not None
                    or audit.observed_model_provider is not None
                    or audit.observed_reasoning_effort is not None
                    or audit.observed_service_tier is not None
                    or audit.observed_identity_sha256 is not None
                )
            else:
                identity_failed = (
                    audit.model_identity_evidence != "direct_observation"
                    or audit.observed_model != request.context.runtime_policy.model
                    or audit.observed_model_provider
                    != request.context.runtime_policy.model_provider
                    or audit.observed_reasoning_effort
                    != request.context.runtime_policy.reasoning_effort
                    or audit.observed_service_tier != request.context.runtime_policy.service_tier
                    or audit.observed_identity_sha256
                    != domain_sha256(
                        OBSERVED_TRANSPORT_IDENTITY_HASH_DOMAIN,
                        {
                            "runtime_identity": request.context.runtime_policy.runtime_identity,
                            "model": request.context.runtime_policy.model,
                            "model_provider": request.context.runtime_policy.model_provider,
                            "reasoning_effort": request.context.runtime_policy.reasoning_effort,
                            "service_tier": request.context.runtime_policy.service_tier,
                        },
                    )
                )
            if (
                identity_failed
                or audit.response_bytes != len(canonical_json_bytes(result.output))
                or audit.duration_ms is None
                or audit.duration_ms > request.context.runtime_policy.budget.max_runtime_ms
                or audit.stream_bytes is None
                or audit.stream_bytes > request.context.runtime_policy.budget.max_stream_bytes
                or audit.usage is None
                or audit.usage.input_tokens > request.context.runtime_policy.budget.max_input_tokens
                or audit.usage.output_tokens
                > request.context.runtime_policy.budget.max_output_tokens
                or (
                    plan.auth_mode is RuntimeAuthModeV1.OPENAI_API
                    and (
                        audit.usage.estimated_cost_micro_usd is None
                        or request.context.runtime_policy.budget.max_cost_micro_usd is None
                        or audit.usage.estimated_cost_micro_usd
                        > request.context.runtime_policy.budget.max_cost_micro_usd
                    )
                )
                or (
                    plan.auth_mode is not RuntimeAuthModeV1.OPENAI_API
                    and audit.usage.estimated_cost_micro_usd is not None
                )
            ):
                raise BridgeReplayError("bridge successful transport budget failed replay")
            results[role] = result
            completed.append(role)
            assert audit.usage is not None
            accepted_input_tokens += audit.usage.input_tokens
            accepted_output_tokens += audit.usage.output_tokens
            if plan.auth_mode is RuntimeAuthModeV1.OPENAI_API:
                assert audit.usage.estimated_cost_micro_usd is not None
                accepted_estimated_cost += audit.usage.estimated_cost_micro_usd
        else:
            if result is not None or terminal_failure is not None:
                raise BridgeReplayError("bridge failure artifact matrix is invalid")
            if audit.failure_reason_code in {
                "structured_result_invalid",
                "transport_policy_mismatch",
            } and (
                audit.usage is None
                or audit.response_bytes is None
                or (
                    audit.model_identity_evidence == "direct_observation"
                    and audit.observed_identity_sha256 is None
                )
            ):
                raise BridgeReplayError("bridge returned transport evidence was discarded")
            terminal_failure = audit

    unknown = set(artifacts) - known_names
    if unknown:
        raise BridgeReplayError("unknown bridge artifact logical name")
    if runtime_policy is not None and (
        accepted_input_tokens > plan.total_max_input_tokens
        or accepted_output_tokens > plan.total_max_output_tokens
        or (
            plan.auth_mode is RuntimeAuthModeV1.OPENAI_API
            and (
                plan.total_max_cost_micro_usd is None
                or accepted_estimated_cost > plan.total_max_cost_micro_usd
            )
        )
    ):
        raise BridgeReplayError("bridge aggregate budget failed replay")
    if terminal_failure is not None:
        expected_status = {
            BridgeEffectState.NOT_LAUNCHED: "failed",
            BridgeEffectState.LAUNCHED: "effect_unknown",
            BridgeEffectState.FAILED: "failed",
            BridgeEffectState.TIMED_OUT: "timed_out",
            BridgeEffectState.CANCELLED: "cancelled",
            BridgeEffectState.CANCEL_UNCONFIRMED: "cancel_unconfirmed",
            BridgeEffectState.EFFECT_UNKNOWN: "effect_unknown",
        }[terminal_failure.effect_state]
        if read.pointer.status != expected_status:
            raise BridgeReplayError("bridge failure status does not match its execution audit")
    elif read.pointer.status == "succeeded":
        if tuple(completed) != BRIDGE_ROLE_ORDER or read.completion_marker is None:
            raise BridgeReplayError("successful bridge run is incomplete")
    elif read.pointer.status not in {"approval_required", "in_progress"}:
        raise BridgeReplayError("terminal bridge status lacks failure evidence")
    pending = tuple(role for role in BRIDGE_ROLE_ORDER if role not in completed)
    return BridgeReplayResult(
        bridge_run_id=plan.bridge_run_id,
        auth_mode=plan.auth_mode,
        revision=read.pointer.revision,
        status=read.pointer.status,
        completed_roles=tuple(completed),
        pending_roles=pending,
        reconciliation_required=(
            open_admission
            or (
                terminal_failure is not None
                and terminal_failure.effect_state
                in {
                    BridgeEffectState.LAUNCHED,
                    BridgeEffectState.CANCEL_UNCONFIRMED,
                    BridgeEffectState.EFFECT_UNKNOWN,
                }
            )
        ),
        total_input_tokens=input_tokens,
        total_output_tokens=output_tokens,
        total_estimated_cost_micro_usd=(
            estimated_cost
            if plan.auth_mode is RuntimeAuthModeV1.OPENAI_API and estimated_cost_known
            else None
        ),
    )


__all__ = ["BridgeReplayError", "BridgeReplayResult", "replay_bridge"]
