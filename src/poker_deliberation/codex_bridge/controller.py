"""Serial orchestration for the bounded five-role Codex bridge."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TypeVar, cast

from pydantic import BaseModel

from poker_deliberation.codex_bridge.canonical import domain_sha256
from poker_deliberation.codex_bridge.conformance import build_bridge_role_conformance
from poker_deliberation.codex_bridge.contracts import (
    admit_role_request,
    assert_no_replay,
    build_execution_audit,
    build_role_confirmation,
    build_role_request,
    build_run_plan,
    build_runtime_policy,
    validate_role_response,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    MAX_ASSIGNMENT_LIFETIME_SECONDS,
    MAX_CONFIRMATION_LIFETIME_SECONDS,
    OBSERVED_TRANSPORT_IDENTITY_HASH_DOMAIN,
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeEffectState,
    BridgeExecutionAuditV1,
    BridgePreExecutionAdmissionV1,
    BridgeRole,
    BridgeRoleConfirmationV1,
    BridgeRoleResultV1,
    BridgeRunPlanV1,
    BridgeSourceContextV1,
    BridgeTerminalStatus,
    CodexSubscriptionLiveExecutionEvidenceV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.storage import (
    BoundedCodexBridgeStore,
    BridgeExecutionIdentityCollisionError,
    BridgeStorageError,
    BridgeStoredArtifact,
    VerifiedBridgeRead,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransport,
    BridgeTransportFailure,
    BridgeTransportFailureEvidence,
    BridgeTransportResult,
)

Clock = Callable[[], datetime]
ModelT = TypeVar("ModelT", bound=BaseModel)


class BridgeControllerError(ValueError):
    """Raised when the bridge state machine refuses a transition."""


def _role_base(role: BridgeRole) -> str:
    return f"roles/{BRIDGE_ROLE_ORDER.index(role)}"


def _role_name(role: BridgeRole, artifact: str) -> str:
    return f"{_role_base(role)}/{artifact}.json"


def role_artifact_name(role: BridgeRole, artifact: str) -> str:
    """Return the stable logical path for one role-scoped artifact."""

    return _role_name(role, artifact)


def canonical_assignment_id(
    bridge_run_id: str,
    auth_mode: RuntimeAuthModeV1,
    role: BridgeRole,
) -> str:
    """Return the non-reusable assignment ID bound to one exact bridge run."""

    run_binding = domain_sha256(
        "poker-bounded-codex-bridge-assignment-id-v1",
        {
            "bridge_run_id": bridge_run_id,
            "auth_mode": auth_mode,
            "role": role,
        },
    )[:16]
    return f"assignment-{auth_mode.value}-{role.value}-{run_binding}"


def canonical_attempt_id(
    bridge_run_id: str,
    auth_mode: RuntimeAuthModeV1,
    role: BridgeRole,
) -> str:
    """Return the non-reusable attempt ID bound to one exact bridge run."""

    run_binding = domain_sha256(
        "poker-bounded-codex-bridge-attempt-id-v1",
        {
            "bridge_run_id": bridge_run_id,
            "auth_mode": auth_mode,
            "role": role,
        },
    )[:16]
    return f"attempt-{auth_mode.value}-{role.value}-{run_binding}"


def _observed_transport_identity_sha256(result: BridgeTransportResult) -> str | None:
    """Hash trusted observed identity fields without publishing raw model text."""

    if result.model_identity_evidence != "direct_observation":
        return None
    return domain_sha256(
        OBSERVED_TRANSPORT_IDENTITY_HASH_DOMAIN,
        {
            "runtime_identity": result.runtime_identity,
            "model": result.observed_model,
            "model_provider": result.observed_model_provider,
            "reasoning_effort": result.observed_reasoning_effort,
            "service_tier": result.observed_service_tier,
        },
    )


def _observed_failure_identity_sha256(
    evidence: BridgeTransportFailureEvidence,
) -> str | None:
    if evidence.model_identity_evidence != "direct_observation":
        return None
    values = (
        evidence.runtime_identity,
        evidence.observed_model,
        evidence.observed_model_provider,
        evidence.observed_reasoning_effort,
        evidence.observed_service_tier,
    )
    if any(value is None for value in values):
        return None
    return domain_sha256(
        OBSERVED_TRANSPORT_IDENTITY_HASH_DOMAIN,
        {
            "runtime_identity": evidence.runtime_identity,
            "model": evidence.observed_model,
            "model_provider": evidence.observed_model_provider,
            "reasoning_effort": evidence.observed_reasoning_effort,
            "service_tier": evidence.observed_service_tier,
        },
    )


def _transport_identity_matches_policy(
    request: BoundedCodexBridgeRequestV1,
    result: BridgeTransportResult,
) -> bool:
    policy = request.context.runtime_policy
    if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION:
        return (
            result.runtime_identity == policy.runtime_identity
            and result.model_identity_evidence == "requested_pinned_no_fallback_no_reroute"
            and result.observed_model is None
            and result.observed_model_provider is None
            and result.observed_reasoning_effort is None
            and result.observed_service_tier is None
        )
    return (
        result.runtime_identity == policy.runtime_identity
        and result.model_identity_evidence == "direct_observation"
        and result.observed_model == policy.model
        and result.observed_model_provider == policy.model_provider
        and result.observed_reasoning_effort == policy.reasoning_effort
        and result.observed_service_tier == policy.service_tier
    )


def _cancellation_kind(
    effect_state: BridgeEffectState,
) -> Literal["not_requested", "cooperative", "unconfirmed"]:
    if effect_state is BridgeEffectState.CANCELLED:
        return "cooperative"
    if effect_state is BridgeEffectState.CANCEL_UNCONFIRMED:
        return "unconfirmed"
    return "not_requested"


def _validated_live_execution_evidence(
    transport: BridgeTransport,
    request: BoundedCodexBridgeRequestV1,
    result: BridgeTransportResult,
) -> CodexSubscriptionLiveExecutionEvidenceV1 | None:
    """Derive live status from the exact sealed implementation, never a caller label."""

    try:
        from poker_deliberation.codex_bridge.subscription_transport import (
            validated_sealed_live_execution,
        )

        return validated_sealed_live_execution(transport, request, result)
    except Exception as exc:
        raise BridgeTransportFailure(
            "transport_live_attestation_invalid",
            effect_state=BridgeEffectState.FAILED,
            launched_at=result.launched_at,
            completed_at=result.completed_at,
            duration_ms=result.duration_ms,
            stream_bytes=result.stream_bytes,
            item_types=result.item_types,
            thread_id_sha256=result.thread_id_sha256,
            turn_id_sha256=result.turn_id_sha256,
            transport_result=result,
        ) from exc


def _append_artifacts(
    existing: tuple[BridgeStoredArtifact, ...],
    additions: tuple[BridgeStoredArtifact, ...],
) -> tuple[BridgeStoredArtifact, ...]:
    merged = {item.logical_name: item for item in existing}
    for item in additions:
        if item.logical_name in merged:
            raise BridgeControllerError("bridge artifact is immutable and already exists")
        merged[item.logical_name] = item
    return tuple(merged[name] for name in sorted(merged, key=lambda item: item.encode("utf-8")))


def _model_at(
    read: VerifiedBridgeRead,
    logical_name: str,
    expected_type: type[ModelT],
) -> ModelT:
    for item in read.decoded_artifacts():
        if item.logical_name == logical_name:
            if not isinstance(item.model, expected_type):
                raise BridgeControllerError("bridge artifact schema mismatch")
            return item.model
    raise BridgeControllerError("required bridge artifact is absent")


class BoundedCodexBridgeController:
    """Stop-after-each-role controller; it never retries or runs roles in parallel."""

    def __init__(self, store: BoundedCodexBridgeStore, *, clock: Clock | None = None) -> None:
        self.store = store
        self.clock = clock or (lambda: datetime.now(UTC))

    def read_role_request(
        self,
        bridge_run_id: str,
        role: BridgeRole,
    ) -> BoundedCodexBridgeRequestV1:
        """Read one verified pending or completed role request without executing it."""

        return _model_at(
            self.store.read_current(bridge_run_id),
            _role_name(role, "request"),
            BoundedCodexBridgeRequestV1,
        )

    def read_source_context(self, bridge_run_id: str) -> BridgeSourceContextV1:
        """Read the immutable projected P3-030C source context."""

        return _model_at(
            self.store.read_current(bridge_run_id),
            "source_context.json",
            BridgeSourceContextV1,
        )

    def read_run_plan(self, bridge_run_id: str) -> BridgeRunPlanV1:
        """Read the immutable repository/runtime plan for one bridge run."""

        return _model_at(
            self.store.read_current(bridge_run_id),
            "run_plan.json",
            BridgeRunPlanV1,
        )

    @staticmethod
    def _assignment_id(
        bridge_run_id: str,
        auth_mode: RuntimeAuthModeV1,
        role: BridgeRole,
    ) -> str:
        return canonical_assignment_id(bridge_run_id, auth_mode, role)

    @staticmethod
    def _attempt_id(
        bridge_run_id: str,
        auth_mode: RuntimeAuthModeV1,
        role: BridgeRole,
    ) -> str:
        return canonical_attempt_id(bridge_run_id, auth_mode, role)

    def prepare_run(
        self,
        *,
        bridge_run_id: str,
        source_context: BridgeSourceContextV1,
        repository_root: Path,
        repository_commit_id: str,
        repository_tree_id: str,
        auth_mode: RuntimeAuthModeV1,
        api_max_cost_micro_usd: int | None = None,
    ) -> VerifiedBridgeRead:
        created_at = self.clock()
        policy = build_runtime_policy(
            auth_mode=auth_mode,
            api_max_cost_micro_usd=api_max_cost_micro_usd,
        )
        role_conformance = build_bridge_role_conformance(
            repository_root,
            repository_commit_id=repository_commit_id,
        )
        plan = build_run_plan(
            bridge_run_id=bridge_run_id,
            source_context=source_context,
            runtime_policy=policy,
            role_conformance=role_conformance,
            repository_commit_id=repository_commit_id,
            repository_tree_id=repository_tree_id,
            created_at=created_at,
        )
        expires_at = created_at + timedelta(seconds=MAX_ASSIGNMENT_LIFETIME_SECONDS)
        independent = tuple(
            build_role_request(
                bridge_run_id=bridge_run_id,
                role=role,
                assignment_id=self._assignment_id(bridge_run_id, auth_mode, role),
                attempt_id=self._attempt_id(bridge_run_id, auth_mode, role),
                expires_at=expires_at,
                source_context=source_context,
                runtime_policy=policy,
                conformance=role_conformance[BRIDGE_ROLE_ORDER.index(role)],
            )
            for role in BRIDGE_ROLE_ORDER[:3]
        )
        artifacts = (
            BridgeStoredArtifact("run_plan.json", "run_plan", plan),
            BridgeStoredArtifact("source_context.json", "source_context", source_context),
            *(
                BridgeStoredArtifact(
                    _role_name(item.context.assignment.role, "request"),
                    "request",
                    item,
                )
                for item in independent
            ),
        )
        publication = self.store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=None,
            published_at=created_at,
            artifacts=artifacts,
        )
        self.store.publish(publication)
        return self.store.read_current(bridge_run_id)

    def confirm_role(
        self,
        bridge_run_id: str,
        role: BridgeRole,
        *,
        authority: BridgeConfirmationAuthorityV1,
        confirmation_id: str,
        idempotency_key: str,
        expected_request_sha256: str,
        expected_request_bytes_sha256: str,
        expected_envelope_sha256: str,
        expected_runtime_policy_sha256: str,
        expected_auth_mode: RuntimeAuthModeV1,
        expected_runtime_identity: str,
        expected_model_provider: str,
        expected_model: str | None,
        expected_credential_reference: str,
        expected_remote_retention_policy: str,
    ) -> VerifiedBridgeRead:
        current = self.store.read_current(bridge_run_id)
        if current.pointer.status not in {"approval_required", "in_progress"}:
            raise BridgeControllerError("terminal bridge run cannot accept confirmation")
        request = _model_at(
            current,
            _role_name(role, "request"),
            BoundedCodexBridgeRequestV1,
        )
        if (
            expected_auth_mode is not request.auth_mode
            or expected_request_sha256 != request.request_sha256
            or expected_request_bytes_sha256 != request.request_bytes_sha256
            or expected_envelope_sha256 != request.context.envelope_sha256
            or expected_runtime_policy_sha256 != request.context.runtime_policy.policy_sha256
            or expected_runtime_identity != request.context.runtime_policy.runtime_identity
            or expected_model_provider != request.context.runtime_policy.model_provider
            or expected_model != request.context.runtime_policy.model
            or expected_credential_reference != request.context.runtime_policy.credential_reference
            or expected_remote_retention_policy
            != request.context.runtime_policy.remote_retention_policy
        ):
            raise BridgeControllerError("exact bridge request confirmation mismatch")
        for item in current.decoded_artifacts():
            if isinstance(item.model, BridgeRoleConfirmationV1) and (
                item.model.confirmation_id == confirmation_id
                or item.model.idempotency_key == idempotency_key
            ):
                raise BridgeControllerError("bridge confirmation identifier was reused")
        now = self.clock()
        confirmation = build_role_confirmation(
            request,
            confirmation_id=confirmation_id,
            idempotency_key=idempotency_key,
            authority=authority,
            confirmed_at=now,
            expires_at=min(
                request.context.assignment.expires_at,
                now + timedelta(seconds=MAX_CONFIRMATION_LIFETIME_SECONDS),
            ),
        )
        try:
            self.store.claim_confirmation_identifiers(
                bridge_run_id=bridge_run_id,
                auth_mode=request.auth_mode,
                role=role,
                request_sha256=request.request_sha256,
                confirmation_id=confirmation_id,
                idempotency_key=idempotency_key,
            )
        except BridgeStorageError as exc:
            raise BridgeControllerError("bridge confirmation identifier was reused") from exc
        artifacts = _append_artifacts(
            current.decoded_artifacts(),
            (
                BridgeStoredArtifact(
                    _role_name(role, "confirmation"),
                    "confirmation",
                    confirmation,
                ),
            ),
        )
        plan = _model_at(current, "run_plan.json", BridgeRunPlanV1)
        publication = self.store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=current,
            published_at=now,
            artifacts=artifacts,
        )
        self.store.publish(publication)
        return self.store.read_current(bridge_run_id)

    @staticmethod
    def _existing_attempts(
        read: VerifiedBridgeRead,
    ) -> dict[tuple[RuntimeAuthModeV1, str, str, str], str]:
        attempts: dict[tuple[RuntimeAuthModeV1, str, str, str], str] = {}
        for item in read.decoded_artifacts():
            if isinstance(item.model, BridgePreExecutionAdmissionV1):
                admission = item.model
                attempts[
                    (
                        admission.auth_mode,
                        admission.bridge_run_id,
                        admission.assignment_id,
                        admission.attempt_id,
                    )
                ] = admission.request_sha256
            elif isinstance(item.model, BridgeExecutionAuditV1):
                audit = item.model
                attempts[
                    (
                        audit.auth_mode,
                        audit.bridge_run_id,
                        audit.assignment_id,
                        audit.attempt_id,
                    )
                ] = audit.request_sha256
        return attempts

    @staticmethod
    def _assert_turn_order(read: VerifiedBridgeRead, role: BridgeRole) -> None:
        ordinal = BRIDGE_ROLE_ORDER.index(role)
        names = {item.logical_name for item in read.manifest.inventory}
        for prior in BRIDGE_ROLE_ORDER[:ordinal]:
            if _role_name(prior, "result") not in names:
                raise BridgeControllerError("bridge role execution order is not serial")
        if _role_name(role, "result") in names or _role_name(role, "audit") in names:
            raise BridgeControllerError("bridge role attempt is already terminal")

    @staticmethod
    def _validate_transport(
        request: BoundedCodexBridgeRequestV1,
        result: BridgeTransportResult,
    ) -> BridgeRoleResultV1:
        policy = request.context.runtime_policy
        unexpected = tuple(
            sorted(
                set(result.item_types) - {"agent_message", "reasoning"},
                key=lambda item: item.encode("utf-8"),
            )
        )
        usage = result.usage
        if (
            result.auth_mode is not request.auth_mode
            or result.runtime_identity != policy.runtime_identity
            or not _transport_identity_matches_policy(request, result)
            or unexpected
            or "agent_message" not in result.item_types
            or len(result.response_bytes) > policy.budget.max_response_bytes
            or result.stream_bytes > policy.budget.max_stream_bytes
            or result.duration_ms > policy.budget.max_runtime_ms
            or usage.input_tokens > policy.budget.max_input_tokens
            or usage.output_tokens > policy.budget.max_output_tokens
            or (
                request.auth_mode is RuntimeAuthModeV1.OPENAI_API
                and (
                    usage.estimated_cost_micro_usd is None
                    or policy.budget.max_cost_micro_usd is None
                    or usage.estimated_cost_micro_usd > policy.budget.max_cost_micro_usd
                )
            )
            or (
                request.auth_mode is not RuntimeAuthModeV1.OPENAI_API
                and usage.estimated_cost_micro_usd is not None
            )
            or (
                usage.cost_authority
                != (
                    "estimate"
                    if request.auth_mode is RuntimeAuthModeV1.OPENAI_API
                    else "not_applicable"
                )
            )
        ):
            raise BridgeTransportFailure(
                "transport_policy_mismatch",
                effect_state=BridgeEffectState.FAILED,
                launched_at=result.launched_at,
                completed_at=result.completed_at,
                duration_ms=result.duration_ms,
                stream_bytes=result.stream_bytes,
                item_types=unexpected,
                thread_id_sha256=result.thread_id_sha256,
                turn_id_sha256=result.turn_id_sha256,
                transport_result=result,
            )
        try:
            return validate_role_response(request, result.response_bytes)
        except Exception as exc:
            raise BridgeTransportFailure(
                "structured_result_invalid",
                effect_state=BridgeEffectState.FAILED,
                launched_at=result.launched_at,
                completed_at=result.completed_at,
                duration_ms=result.duration_ms,
                stream_bytes=result.stream_bytes,
                item_types=unexpected,
                thread_id_sha256=result.thread_id_sha256,
                turn_id_sha256=result.turn_id_sha256,
                transport_result=result,
            ) from exc

    def _dependent_request(
        self,
        current: VerifiedBridgeRead,
        completed_role: BridgeRole,
        *,
        source: BridgeSourceContextV1,
        plan: BridgeRunPlanV1,
        completed_result: BridgeRoleResultV1,
    ) -> BoundedCodexBridgeRequestV1 | None:
        if completed_role is BridgeRole.SKEPTIC_FALSIFIER:
            role = BridgeRole.ADJUDICATOR
            parents = tuple(
                (
                    completed_result
                    if item is completed_role
                    else _model_at(current, _role_name(item, "result"), BridgeRoleResultV1)
                )
                for item in BRIDGE_ROLE_ORDER[:3]
            )
        elif completed_role is BridgeRole.ADJUDICATOR:
            role = BridgeRole.REPORT_WRITER
            parents = (completed_result,)
        else:
            return None
        policy = _model_at(
            current,
            _role_name(BridgeRole.STRATEGY_ANALYST, "request"),
            BoundedCodexBridgeRequestV1,
        ).context.runtime_policy
        return build_role_request(
            bridge_run_id=plan.bridge_run_id,
            role=role,
            assignment_id=self._assignment_id(plan.bridge_run_id, policy.auth_mode, role),
            attempt_id=self._attempt_id(plan.bridge_run_id, policy.auth_mode, role),
            expires_at=self.clock() + timedelta(seconds=MAX_ASSIGNMENT_LIFETIME_SECONDS),
            source_context=source,
            runtime_policy=policy,
            conformance=plan.role_conformance[BRIDGE_ROLE_ORDER.index(role)],
            parent_results=parents,
        )

    def _publish_failure(
        self,
        current: VerifiedBridgeRead,
        *,
        plan: BridgeRunPlanV1,
        role: BridgeRole,
        audit: BridgeExecutionAuditV1,
    ) -> VerifiedBridgeRead:
        status = cast(
            BridgeTerminalStatus,
            {
                BridgeEffectState.TIMED_OUT: "timed_out",
                BridgeEffectState.CANCELLED: "cancelled",
                BridgeEffectState.CANCEL_UNCONFIRMED: "cancel_unconfirmed",
                BridgeEffectState.EFFECT_UNKNOWN: "effect_unknown",
            }.get(audit.effect_state, "failed"),
        )
        artifacts = _append_artifacts(
            current.decoded_artifacts(),
            (BridgeStoredArtifact(_role_name(role, "audit"), "execution_audit", audit),),
        )
        publication = self.store.prepare_request(
            run_plan=plan,
            status=status,
            expected=current,
            published_at=self.clock(),
            artifacts=artifacts,
        )
        self.store.publish(publication)
        return self.store.read_current(plan.bridge_run_id)

    def _claim_execution_identity(
        self,
        *,
        request: BoundedCodexBridgeRequestV1,
        confirmation: BridgeRoleConfirmationV1,
        admission: BridgePreExecutionAdmissionV1,
        audit: BridgeExecutionAuditV1,
    ) -> BridgeExecutionAuditV1:
        try:
            self.store.claim_execution_identity(audit)
            return audit
        except BridgeExecutionIdentityCollisionError:
            failure_reason_code = "execution_identity_registry_rejected"
            thread_id_sha256 = audit.thread_id_sha256
            turn_id_sha256 = audit.turn_id_sha256
        except BridgeStorageError:
            failure_reason_code = "execution_identity_registry_corrupt"
            # Preserve observed hashes and all other trusted transport evidence. The
            # store records that they were not proven globally reserved and blocks
            # subsequent execution until reconciliation.
            thread_id_sha256 = audit.thread_id_sha256
            turn_id_sha256 = audit.turn_id_sha256
        return build_execution_audit(
            request,
            confirmation,
            admission,
            transport_qualification=audit.transport_qualification,
            effect_state=BridgeEffectState.EFFECT_UNKNOWN,
            thread_id_sha256=thread_id_sha256,
            turn_id_sha256=turn_id_sha256,
            launched_at=audit.launched_at,
            completed_at=audit.completed_at,
            duration_ms=audit.duration_ms,
            usage=audit.usage,
            response_bytes=audit.response_bytes,
            stream_bytes=audit.stream_bytes,
            unexpected_item_types=audit.unexpected_item_types,
            cancellation_kind=_cancellation_kind(BridgeEffectState.EFFECT_UNKNOWN),
            result_sha256=None,
            failure_reason_code=failure_reason_code,
            model_identity_evidence=audit.model_identity_evidence,
            observed_model=audit.observed_model,
            observed_model_provider=audit.observed_model_provider,
            observed_reasoning_effort=audit.observed_reasoning_effort,
            observed_service_tier=audit.observed_service_tier,
            observed_identity_sha256=audit.observed_identity_sha256,
            live_execution_evidence=audit.live_execution_evidence,
        )

    def execute_confirmed_role(
        self,
        bridge_run_id: str,
        role: BridgeRole,
        *,
        auth_mode: RuntimeAuthModeV1,
        current_source_terminal_manifest_sha256: str,
        transport: BridgeTransport,
    ) -> VerifiedBridgeRead:
        current = self.store.read_current(bridge_run_id)
        if current.pointer.status not in {"approval_required", "in_progress"}:
            raise BridgeControllerError("terminal bridge run cannot execute")
        plan = _model_at(current, "run_plan.json", BridgeRunPlanV1)
        source = _model_at(current, "source_context.json", BridgeSourceContextV1)
        request = _model_at(current, _role_name(role, "request"), BoundedCodexBridgeRequestV1)
        if (
            auth_mode is not request.auth_mode
            or transport.auth_mode is not request.auth_mode
            or plan.auth_mode is not request.auth_mode
            or current.pointer.auth_mode is not request.auth_mode
        ):
            raise BridgeControllerError("execution auth mode binding mismatch")
        self._assert_turn_order(current, role)
        confirmation = _model_at(
            current,
            _role_name(role, "confirmation"),
            BridgeRoleConfirmationV1,
        )
        assert_no_replay(
            request=request,
            existing_attempts=self._existing_attempts(current),
        )
        try:
            self.store.verify_execution_identity_history()
        except BridgeStorageError as exc:
            raise BridgeControllerError(
                "execution identity registry failed pre-launch validation"
            ) from exc
        admitted_at = self.clock()
        admission = admit_role_request(
            request,
            confirmation,
            admitted_at=admitted_at,
            current_source_terminal_manifest_sha256=(current_source_terminal_manifest_sha256),
        )
        admitted_artifacts = _append_artifacts(
            current.decoded_artifacts(),
            (
                BridgeStoredArtifact(
                    _role_name(role, "admission"),
                    "admission",
                    admission,
                ),
            ),
        )
        pre_execution = self.store.prepare_request(
            run_plan=plan,
            status="in_progress",
            expected=current,
            published_at=admitted_at,
            artifacts=admitted_artifacts,
        )
        self.store.publish(pre_execution)
        admitted_current = self.store.read_current(bridge_run_id)
        persisted_admission = _model_at(
            admitted_current,
            _role_name(role, "admission"),
            BridgePreExecutionAdmissionV1,
        )
        if persisted_admission != admission:
            raise BridgeStorageError("durable pre-execution admission did not replay")
        derived_transport_qualification: Literal["deterministic_fixture", "actual_live"] = (
            "deterministic_fixture"
        )
        live_execution_evidence: CodexSubscriptionLiveExecutionEvidenceV1 | None = None
        try:
            transport_result = transport.execute(request)
            live_execution_evidence = _validated_live_execution_evidence(
                transport,
                request,
                transport_result,
            )
            if live_execution_evidence is not None:
                derived_transport_qualification = "actual_live"
            role_result = self._validate_transport(request, transport_result)
        except BridgeTransportFailure as exc:
            trusted_result = exc.transport_result
            evidence = exc.evidence
            policy = request.context.runtime_policy
            usage = (
                trusted_result.usage
                if trusted_result is not None
                else (None if evidence is None else evidence.usage)
            )
            response_bytes = (
                len(trusted_result.response_bytes)
                if trusted_result is not None
                else (None if evidence is None else evidence.response_bytes)
            )
            observed_model = (
                trusted_result.observed_model
                if trusted_result is not None
                else (None if evidence is None else evidence.observed_model)
            )
            observed_model_provider = (
                trusted_result.observed_model_provider
                if trusted_result is not None
                else (None if evidence is None else evidence.observed_model_provider)
            )
            observed_reasoning_effort = (
                trusted_result.observed_reasoning_effort
                if trusted_result is not None
                else (None if evidence is None else evidence.observed_reasoning_effort)
            )
            observed_service_tier = (
                trusted_result.observed_service_tier
                if trusted_result is not None
                else (None if evidence is None else evidence.observed_service_tier)
            )
            model_identity_evidence = (
                trusted_result.model_identity_evidence
                if trusted_result is not None
                and _transport_identity_matches_policy(request, trusted_result)
                else ("unavailable" if evidence is None else evidence.model_identity_evidence)
            )
            audit = build_execution_audit(
                request,
                confirmation,
                admission,
                transport_qualification=derived_transport_qualification,
                effect_state=exc.effect_state,
                thread_id_sha256=exc.thread_id_sha256,
                turn_id_sha256=exc.turn_id_sha256,
                launched_at=exc.launched_at,
                completed_at=exc.completed_at,
                duration_ms=exc.duration_ms,
                usage=usage,
                response_bytes=response_bytes,
                stream_bytes=exc.stream_bytes,
                unexpected_item_types=exc.item_types,
                cancellation_kind=_cancellation_kind(exc.effect_state),
                result_sha256=None,
                failure_reason_code=exc.reason_code,
                model_identity_evidence=model_identity_evidence,
                observed_model=(policy.model if observed_model == policy.model else None),
                observed_model_provider=(
                    policy.model_provider
                    if observed_model_provider == policy.model_provider
                    else None
                ),
                observed_reasoning_effort=(
                    policy.reasoning_effort
                    if observed_reasoning_effort == policy.reasoning_effort
                    else None
                ),
                observed_service_tier=(
                    policy.service_tier if observed_service_tier == policy.service_tier else None
                ),
                observed_identity_sha256=(
                    _observed_transport_identity_sha256(trusted_result)
                    if trusted_result is not None
                    and _transport_identity_matches_policy(request, trusted_result)
                    else (None if evidence is None else _observed_failure_identity_sha256(evidence))
                ),
                live_execution_evidence=live_execution_evidence,
            )
            audit = self._claim_execution_identity(
                request=request,
                confirmation=confirmation,
                admission=admission,
                audit=audit,
            )
            return self._publish_failure(
                admitted_current,
                plan=plan,
                role=role,
                audit=audit,
            )
        except Exception:
            audit = build_execution_audit(
                request,
                confirmation,
                admission,
                transport_qualification="deterministic_fixture",
                effect_state=BridgeEffectState.EFFECT_UNKNOWN,
                thread_id_sha256=None,
                turn_id_sha256=None,
                launched_at=None,
                completed_at=self.clock(),
                duration_ms=0,
                usage=None,
                response_bytes=None,
                stream_bytes=0,
                unexpected_item_types=(),
                cancellation_kind=_cancellation_kind(BridgeEffectState.EFFECT_UNKNOWN),
                result_sha256=None,
                failure_reason_code="transport_unclassified_exception",
                model_identity_evidence="unavailable",
                observed_model=None,
                observed_model_provider=None,
                observed_reasoning_effort=None,
                observed_service_tier=None,
                observed_identity_sha256=None,
                live_execution_evidence=None,
            )
            audit = self._claim_execution_identity(
                request=request,
                confirmation=confirmation,
                admission=admission,
                audit=audit,
            )
            try:
                return self._publish_failure(
                    admitted_current,
                    plan=plan,
                    role=role,
                    audit=audit,
                )
            except Exception as publication_exc:
                raise BridgeControllerError(
                    "transport effect and failure publication both require reconciliation"
                ) from publication_exc
        audit = build_execution_audit(
            request,
            confirmation,
            admission,
            transport_qualification=derived_transport_qualification,
            effect_state=BridgeEffectState.SUCCEEDED,
            thread_id_sha256=transport_result.thread_id_sha256,
            turn_id_sha256=transport_result.turn_id_sha256,
            launched_at=transport_result.launched_at,
            completed_at=transport_result.completed_at,
            duration_ms=transport_result.duration_ms,
            usage=transport_result.usage,
            response_bytes=len(transport_result.response_bytes),
            stream_bytes=transport_result.stream_bytes,
            unexpected_item_types=(),
            cancellation_kind="not_requested",
            result_sha256=role_result.result_sha256,
            failure_reason_code=None,
            model_identity_evidence=transport_result.model_identity_evidence,
            observed_model=transport_result.observed_model,
            observed_model_provider=transport_result.observed_model_provider,
            observed_reasoning_effort=transport_result.observed_reasoning_effort,
            observed_service_tier=transport_result.observed_service_tier,
            observed_identity_sha256=_observed_transport_identity_sha256(transport_result),
            live_execution_evidence=live_execution_evidence,
        )
        audit = self._claim_execution_identity(
            request=request,
            confirmation=confirmation,
            admission=admission,
            audit=audit,
        )
        if audit.effect_state is not BridgeEffectState.SUCCEEDED:
            return self._publish_failure(
                admitted_current,
                plan=plan,
                role=role,
                audit=audit,
            )
        additions: tuple[BridgeStoredArtifact, ...] = (
            BridgeStoredArtifact(_role_name(role, "result"), "role_result", role_result),
            BridgeStoredArtifact(_role_name(role, "audit"), "execution_audit", audit),
        )
        dependent = self._dependent_request(
            admitted_current,
            role,
            source=source,
            plan=plan,
            completed_result=role_result,
        )
        if dependent is not None:
            additions = (
                *additions,
                BridgeStoredArtifact(
                    _role_name(dependent.context.assignment.role, "request"),
                    "request",
                    dependent,
                ),
            )
        final_artifacts = _append_artifacts(admitted_current.decoded_artifacts(), additions)
        status: BridgeTerminalStatus = (
            "approval_required"
            if dependent is not None
            else ("succeeded" if role is BridgeRole.REPORT_WRITER else "in_progress")
        )
        publication = self.store.prepare_request(
            run_plan=plan,
            status=status,
            expected=admitted_current,
            published_at=self.clock(),
            artifacts=final_artifacts,
        )
        self.store.publish(publication)
        return self.store.read_current(bridge_run_id)


__all__ = [
    "BoundedCodexBridgeController",
    "BridgeControllerError",
    "canonical_assignment_id",
    "canonical_attempt_id",
    "role_artifact_name",
]
