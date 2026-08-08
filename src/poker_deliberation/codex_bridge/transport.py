"""Transport boundary for deterministic fixtures and the actual Codex worker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, Protocol

from poker_deliberation.codex_bridge.canonical import canonical_json_bytes, domain_sha256
from poker_deliberation.codex_bridge.contracts import expected_evidence_references
from poker_deliberation.codex_bridge.models import (
    SAFE_INFERENCE_NARRATIVE,
    SAFE_UNKNOWN_NARRATIVE,
    BoundedCodexBridgeRequestV1,
    BridgeClaimV1,
    BridgeConclusionCode,
    BridgeEffectState,
    BridgeEpistemicLabel,
    BridgeRole,
    BridgeRoleOutputV1,
    BridgeTransportUsageV1,
    Narrative,
    RuntimeAuthModeV1,
)


@dataclass(frozen=True, slots=True)
class BridgeTransportResult:
    auth_mode: RuntimeAuthModeV1
    transport_qualification: Literal["deterministic_fixture", "actual_live"]
    response_bytes: bytes
    usage: BridgeTransportUsageV1
    model_identity_evidence: Literal[
        "direct_observation",
        "requested_pinned_no_fallback_no_reroute",
    ]
    observed_model: str | None
    observed_model_provider: str | None
    observed_reasoning_effort: str | None
    observed_service_tier: str | None
    runtime_identity: str
    thread_id_sha256: str
    turn_id_sha256: str
    launched_at: datetime
    completed_at: datetime
    duration_ms: int
    stream_bytes: int
    item_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BridgeTransportFailureEvidence:
    """Strictly parsed transport evidence retained without raw response content."""

    usage: BridgeTransportUsageV1 | None = None
    response_bytes: int | None = None
    runtime_identity: str | None = None
    model_identity_evidence: Literal[
        "direct_observation",
        "requested_pinned_no_fallback_no_reroute",
        "unavailable",
    ] = "unavailable"
    observed_model: str | None = None
    observed_model_provider: str | None = None
    observed_reasoning_effort: str | None = None
    observed_service_tier: str | None = None


class BridgeTransportFailure(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        *,
        effect_state: BridgeEffectState,
        launched_at: datetime | None,
        completed_at: datetime | None,
        duration_ms: int | None,
        stream_bytes: int,
        item_types: tuple[str, ...] = (),
        thread_id_sha256: str | None = None,
        turn_id_sha256: str | None = None,
        transport_result: BridgeTransportResult | None = None,
        evidence: BridgeTransportFailureEvidence | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.effect_state = effect_state
        self.launched_at = launched_at
        self.completed_at = completed_at
        self.duration_ms = duration_ms
        self.stream_bytes = stream_bytes
        self.item_types = item_types
        self.thread_id_sha256 = thread_id_sha256
        self.turn_id_sha256 = turn_id_sha256
        # A controller-originated validation failure retains this trusted structured
        # envelope. Raw response content is never copied into the durable audit.
        self.transport_result = transport_result
        self.evidence = evidence
        super().__init__(reason_code)


class BridgeTransport(Protocol):
    auth_mode: RuntimeAuthModeV1

    @property
    def transport_qualification(self) -> Literal["deterministic_fixture", "actual_live"]: ...

    def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult: ...


_FIXTURE_CONCLUSIONS: Final[
    dict[BridgeRole, tuple[BridgeConclusionCode, BridgeEpistemicLabel, Narrative]]
] = {
    BridgeRole.STRATEGY_ANALYST: (
        BridgeConclusionCode.STRATEGY_OBSERVATION,
        BridgeEpistemicLabel.INFERENCE,
        SAFE_INFERENCE_NARRATIVE,
    ),
    BridgeRole.MATH_TOOL_AUDITOR: (
        BridgeConclusionCode.MATH_CONSISTENT,
        BridgeEpistemicLabel.INFERENCE,
        SAFE_INFERENCE_NARRATIVE,
    ),
    BridgeRole.SKEPTIC_FALSIFIER: (
        BridgeConclusionCode.MISSING_PREMISE,
        BridgeEpistemicLabel.UNKNOWN,
        SAFE_UNKNOWN_NARRATIVE,
    ),
    BridgeRole.ADJUDICATOR: (
        BridgeConclusionCode.ADJUDICATED_LIMITED,
        BridgeEpistemicLabel.INFERENCE,
        SAFE_INFERENCE_NARRATIVE,
    ),
    BridgeRole.REPORT_WRITER: (
        BridgeConclusionCode.REPORT_LIMITED,
        BridgeEpistemicLabel.INFERENCE,
        SAFE_INFERENCE_NARRATIVE,
    ),
}


class DeterministicReadOnlyTransport:
    """No-network contract fixture; never evidence of an actual Codex bridge."""

    transport_qualification: Literal["deterministic_fixture"] = "deterministic_fixture"

    def __init__(self, *, auth_mode: RuntimeAuthModeV1, clock: ProtocolClock) -> None:
        self.auth_mode = auth_mode
        self.clock = clock
        self.calls: list[str] = []

    def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
        if request.auth_mode is not self.auth_mode:
            raise BridgeTransportFailure(
                "transport_auth_mode_mismatch",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=self.clock(),
                duration_ms=0,
                stream_bytes=0,
            )
        assignment = request.context.assignment
        launched_at = self.clock()
        code, label, narrative = _FIXTURE_CONCLUSIONS[assignment.role]
        references = expected_evidence_references(request)
        evidence_ids = (
            tuple(
                sorted(
                    (f"parent-{item.assignment_id}" for item in request.context.parent_results),
                    key=lambda item: item.encode("utf-8"),
                )
            )
            if request.context.parent_results
            else ("source-result",)
        )
        output = BridgeRoleOutputV1(
            auth_mode=request.auth_mode,
            bridge_run_id=assignment.bridge_run_id,
            role=assignment.role,
            assignment_id=assignment.assignment_id,
            attempt_id=assignment.attempt_id,
            model=request.context.runtime_policy.model,
            model_provider=request.context.runtime_policy.model_provider,
            runtime_identity=request.context.runtime_policy.runtime_identity,
            conclusions=(
                BridgeClaimV1(
                    claim_id="claim-01",
                    conclusion_code=code,
                    label=label,
                    narrative=narrative,
                    evidence_ids=evidence_ids,
                ),
            ),
            evidence_references=references,
        )
        response = canonical_json_bytes(output)
        completed_at = self.clock()
        self.calls.append(assignment.assignment_id)
        return BridgeTransportResult(
            auth_mode=request.auth_mode,
            transport_qualification=self.transport_qualification,
            response_bytes=response,
            usage=BridgeTransportUsageV1(
                input_tokens=0,
                cached_input_tokens=0,
                output_tokens=0,
                reasoning_output_tokens=0,
                estimated_cost_micro_usd=(
                    0 if request.auth_mode is RuntimeAuthModeV1.OPENAI_API else None
                ),
                cost_authority=(
                    "estimate"
                    if request.auth_mode is RuntimeAuthModeV1.OPENAI_API
                    else "not_applicable"
                ),
            ),
            model_identity_evidence=(
                "requested_pinned_no_fallback_no_reroute"
                if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
                else "direct_observation"
            ),
            observed_model=(
                None
                if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
                else request.context.runtime_policy.model
            ),
            observed_model_provider=(
                None
                if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
                else request.context.runtime_policy.model_provider
            ),
            observed_reasoning_effort=(
                None
                if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
                else request.context.runtime_policy.reasoning_effort
            ),
            observed_service_tier=(
                None
                if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
                else request.context.runtime_policy.service_tier
            ),
            runtime_identity=request.context.runtime_policy.runtime_identity,
            thread_id_sha256=domain_sha256(
                "poker-bounded-codex-fixture-thread-v1",
                assignment.assignment_id,
            ),
            turn_id_sha256=domain_sha256(
                "poker-bounded-codex-fixture-turn-v1",
                assignment.attempt_id,
            ),
            launched_at=launched_at,
            completed_at=completed_at,
            duration_ms=max(0, int((completed_at - launched_at).total_seconds() * 1000)),
            stream_bytes=len(response),
            item_types=("agent_message",),
        )


class ProtocolClock(Protocol):
    def __call__(self) -> datetime: ...


__all__ = [
    "BridgeTransport",
    "BridgeTransportFailure",
    "BridgeTransportFailureEvidence",
    "BridgeTransportResult",
    "DeterministicReadOnlyTransport",
]
