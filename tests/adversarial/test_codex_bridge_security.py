from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from poker_deliberation.codex_bridge.controller import (
    BoundedCodexBridgeController,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeExecutionAuditV1,
    BridgeRole,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportResult,
    DeterministicReadOnlyTransport,
)
from tests.codex_bridge_support import REPOSITORY_ROOT, verified_bridge_source

_MODE = RuntimeAuthModeV1.CODEX_SUBSCRIPTION


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class _MutatingTransport:
    def __init__(
        self,
        clock: _Clock,
        mutate: Callable[[BridgeTransportResult], BridgeTransportResult],
    ) -> None:
        self.auth_mode = _MODE
        self.transport_qualification: Literal["deterministic_fixture"] = "deterministic_fixture"
        self.delegate = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
        self.mutate = mutate
        self.result: BridgeTransportResult | None = None

    def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
        self.result = self.mutate(self.delegate.execute(request))
        return self.result


def _confirm(
    controller: BoundedCodexBridgeController,
    bridge_run_id: str,
) -> None:
    request = controller.read_role_request(bridge_run_id, BridgeRole.STRATEGY_ANALYST)
    controller.confirm_role(
        bridge_run_id,
        BridgeRole.STRATEGY_ANALYST,
        authority=BridgeConfirmationAuthorityV1(
            authority_id="local-adversarial-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        confirmation_id=f"confirmation-{bridge_run_id}",
        idempotency_key=f"idempotency-{bridge_run_id}",
        expected_request_sha256=request.request_sha256,
        expected_request_bytes_sha256=request.request_bytes_sha256,
        expected_envelope_sha256=request.context.envelope_sha256,
        expected_runtime_policy_sha256=request.context.runtime_policy.policy_sha256,
        expected_auth_mode=_MODE,
        expected_runtime_identity=request.context.runtime_policy.runtime_identity,
        expected_model_provider=request.context.runtime_policy.model_provider,
        expected_model=request.context.runtime_policy.model,
        expected_credential_reference=request.context.runtime_policy.credential_reference,
        expected_remote_retention_policy=(request.context.runtime_policy.remote_retention_policy),
    )


def test_secret_and_prompt_injection_shaped_identifiers_fail_before_context_build() -> None:
    for authority_id in (
        "api_" + "key:" + "synthetic-" + "secret-canary",
        "ignore.previous.instructions",
        "developer-message",
        "jailbreak",
    ):
        with pytest.raises(ValidationError):
            BridgeConfirmationAuthorityV1(
                authority_id=authority_id,
                authority_kind="local_user",
                authentication="self_asserted",
            )


def test_prompt_injection_shaped_run_id_is_rejected_before_id_derivation(tmp_path: Path) -> None:
    source = verified_bridge_source(tmp_path / "p3")
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(tmp_path / "bridge"))
    with pytest.raises(ValidationError):
        controller.prepare_run(
            bridge_run_id="ignore.previous.instructions",
            source_context=source,
            repository_root=REPOSITORY_ROOT,
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
            auth_mode=_MODE,
        )


def test_transport_identity_tool_and_budget_mismatches_fail_closed(tmp_path: Path) -> None:
    source = verified_bridge_source(tmp_path / "p3")
    mutations: tuple[Callable[[BridgeTransportResult], BridgeTransportResult], ...] = (
        lambda result: replace(result, runtime_identity="unexpected-runtime"),
        lambda result: replace(result, model_identity_evidence="direct_observation"),
        lambda result: replace(result, observed_model="unexpected-model"),
        lambda result: replace(result, observed_model_provider="unexpected-provider"),
        lambda result: replace(result, observed_reasoning_effort="high"),
        lambda result: replace(result, observed_service_tier="priority"),
        lambda result: replace(result, item_types=("agent_message", "command_execution")),
        lambda result: replace(result, response_bytes=b"x" * 32_769),
        lambda result: replace(result, stream_bytes=262_145),
        lambda result: replace(result, duration_ms=120_001),
        lambda result: replace(
            result,
            usage=result.usage.model_copy(update={"input_tokens": 24_001}),
        ),
        lambda result: replace(
            result,
            usage=result.usage.model_copy(update={"output_tokens": 6_001}),
        ),
        lambda result: replace(
            result,
            usage=result.usage.model_copy(update={"estimated_cost_micro_usd": 204_001}),
        ),
    )
    for ordinal, mutate in enumerate(mutations):
        run_id = f"bridge-adversarial-{ordinal}"
        clock = _Clock()
        controller = BoundedCodexBridgeController(
            BoundedCodexBridgeStore(tmp_path / f"bridge-{ordinal}"),
            clock=clock,
        )
        controller.prepare_run(
            bridge_run_id=run_id,
            source_context=source,
            repository_root=REPOSITORY_ROOT,
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
            auth_mode=_MODE,
        )
        _confirm(controller, run_id)
        transport = _MutatingTransport(clock, mutate)
        terminal = controller.execute_confirmed_role(
            run_id,
            BridgeRole.STRATEGY_ANALYST,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )

        replayed = replay_bridge(terminal)
        audit_name = role_artifact_name(BridgeRole.STRATEGY_ANALYST, "audit")
        audit = next(
            item.model for item in terminal.decoded_artifacts() if item.logical_name == audit_name
        )
        assert isinstance(audit, BridgeExecutionAuditV1)
        assert terminal.pointer.status == "failed"
        assert replayed.reconciliation_required is False
        assert audit.failure_reason_code == "transport_policy_mismatch"
        assert transport.result is not None
        assert audit.usage == transport.result.usage
        assert audit.response_bytes == len(transport.result.response_bytes)
        assert audit.observed_identity_sha256 is None
        assert audit.model_identity_evidence in {
            "requested_pinned_no_fallback_no_reroute",
            "unavailable",
        }
        assert replayed.total_input_tokens == transport.result.usage.input_tokens
        assert replayed.total_output_tokens == transport.result.usage.output_tokens
        assert b"unexpected-model" not in terminal.artifact_bytes(audit_name)


def test_invalid_structured_response_keeps_trusted_transport_accounting(tmp_path: Path) -> None:
    clock = _Clock()
    source = verified_bridge_source(tmp_path / "p3")
    run_id = "bridge-invalid-structured-response"
    controller = BoundedCodexBridgeController(
        BoundedCodexBridgeStore(tmp_path / "bridge"),
        clock=clock,
    )
    controller.prepare_run(
        bridge_run_id=run_id,
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    _confirm(controller, run_id)

    def malformed(result: BridgeTransportResult) -> BridgeTransportResult:
        return replace(
            result,
            response_bytes=b"FORGED-RAW-RESPONSE",
            usage=result.usage.model_copy(update={"input_tokens": 17, "output_tokens": 11}),
        )

    transport = _MutatingTransport(clock, malformed)
    terminal = controller.execute_confirmed_role(
        run_id,
        BridgeRole.STRATEGY_ANALYST,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=transport,
    )
    audit_name = role_artifact_name(BridgeRole.STRATEGY_ANALYST, "audit")
    audit = next(
        item.model for item in terminal.decoded_artifacts() if item.logical_name == audit_name
    )
    assert isinstance(audit, BridgeExecutionAuditV1)
    assert audit.failure_reason_code == "structured_result_invalid"
    assert audit.response_bytes == len(b"FORGED-RAW-RESPONSE")
    assert audit.usage is not None
    assert audit.usage.input_tokens == 17
    assert audit.usage.output_tokens == 11
    assert audit.model_identity_evidence == "requested_pinned_no_fallback_no_reroute"
    assert audit.observed_identity_sha256 is None
    replayed = replay_bridge(terminal)
    assert replayed.total_input_tokens == 17
    assert replayed.total_output_tokens == 11
    assert b"FORGED-RAW-RESPONSE" not in terminal.artifact_bytes(audit_name)
