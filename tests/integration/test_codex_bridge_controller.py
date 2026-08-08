from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.codex_bridge.canonical import domain_sha256
from poker_deliberation.codex_bridge.contracts import BridgeContractError
from poker_deliberation.codex_bridge.controller import (
    BoundedCodexBridgeController,
    BridgeControllerError,
    canonical_assignment_id,
    canonical_attempt_id,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeEffectState,
    BridgeExecutionAuditV1,
    BridgeRole,
    BridgeRoleConfirmationV1,
    BridgeRoleResultV1,
    BridgeTransportUsageV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.storage import (
    BoundedCodexBridgeStore,
    BridgeStorageError,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    BridgeTransportFailureEvidence,
    BridgeTransportResult,
    DeterministicReadOnlyTransport,
)
from tests.codex_bridge_support import REPOSITORY_ROOT, verified_bridge_source

_MODE = RuntimeAuthModeV1.CODEX_SUBSCRIPTION


class StepClock:
    def __init__(self) -> None:
        self.current = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def _authority() -> BridgeConfirmationAuthorityV1:
    return BridgeConfirmationAuthorityV1(
        authority_id="local-test-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )


def _confirm(
    controller: BoundedCodexBridgeController,
    role: BridgeRole,
) -> None:
    _confirm_run(controller, "bridge-run-controller", role)


def _confirm_run(
    controller: BoundedCodexBridgeController,
    bridge_run_id: str,
    role: BridgeRole,
    *,
    confirmation_id: str | None = None,
    idempotency_key: str | None = None,
) -> None:
    request = controller.read_role_request(bridge_run_id, role)
    controller.confirm_role(
        bridge_run_id,
        role,
        authority=_authority(),
        confirmation_id=confirmation_id or f"confirmation-{bridge_run_id}-{role.value}",
        idempotency_key=idempotency_key or f"idempotency-{bridge_run_id}-{role.value}",
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


def test_five_role_fixture_path_is_serial_independent_and_replayable(tmp_path: Path) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    controller = BoundedCodexBridgeController(store, clock=clock)
    transport = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
    prepared = controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    prepared_artifacts = {item.logical_name: item.model for item in prepared.decoded_artifacts()}
    first_request = prepared_artifacts[role_artifact_name(BridgeRole.STRATEGY_ANALYST, "request")]
    assert isinstance(first_request, BoundedCodexBridgeRequestV1)
    assert first_request.context.assignment.expires_at - prepared.pointer.published_at == timedelta(
        days=7
    )
    for role in BRIDGE_ROLE_ORDER[:3]:
        _confirm(controller, role)
    for role in BRIDGE_ROLE_ORDER[:3]:
        controller.execute_confirmed_role(
            "bridge-run-controller",
            role,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )
    _confirm(controller, BridgeRole.ADJUDICATOR)
    controller.execute_confirmed_role(
        "bridge-run-controller",
        BridgeRole.ADJUDICATOR,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=transport,
    )
    _confirm(controller, BridgeRole.REPORT_WRITER)
    terminal = controller.execute_confirmed_role(
        "bridge-run-controller",
        BridgeRole.REPORT_WRITER,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=transport,
    )

    assert terminal.pointer.status == "succeeded"
    assert terminal.completion_marker is not None
    assert transport.calls == [
        canonical_assignment_id("bridge-run-controller", _MODE, role) for role in BRIDGE_ROLE_ORDER
    ]
    artifacts = terminal.decoded_artifacts()
    results = [item.model for item in artifacts if isinstance(item.model, BridgeRoleResultV1)]
    assert len(results) == 5
    adjudicator = next(item for item in results if item.output.role is BridgeRole.ADJUDICATOR)
    report = next(item for item in results if item.output.role is BridgeRole.REPORT_WRITER)
    assert len(adjudicator.output.evidence_references) > len(results[0].output.evidence_references)
    assert any(
        reference.evidence_sha256 == adjudicator.result_sha256
        for reference in report.output.evidence_references
    )
    reopened = BoundedCodexBridgeStore(tmp_path / "bridge").read_current("bridge-run-controller")
    assert reopened.pointer == terminal.pointer
    assert reopened.artifact_bytes("source_context.json") == terminal.artifact_bytes(
        "source_context.json"
    )
    replay = replay_bridge(reopened)
    assert replay.completed_roles == BRIDGE_ROLE_ORDER
    assert replay.pending_roles == ()
    assert replay.reconciliation_required is False
    assert replay.total_input_tokens == 0
    assert replay.total_output_tokens == 0


def test_controller_rejects_out_of_order_and_cross_source_execution(tmp_path: Path) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    controller = BoundedCodexBridgeController(
        BoundedCodexBridgeStore(tmp_path / "bridge"),
        clock=clock,
    )
    transport = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
    controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    _confirm(controller, BridgeRole.MATH_TOOL_AUDITOR)
    with pytest.raises(BridgeControllerError, match="order is not serial"):
        controller.execute_confirmed_role(
            "bridge-run-controller",
            BridgeRole.MATH_TOOL_AUDITOR,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )
    _confirm(controller, BridgeRole.STRATEGY_ANALYST)
    with pytest.raises(ValueError, match="binding failed"):
        controller.execute_confirmed_role(
            "bridge-run-controller",
            BridgeRole.STRATEGY_ANALYST,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256="f" * 64,
            transport=transport,
        )
    assert transport.calls == []


def test_durable_open_admission_forbids_blind_retry_after_publication_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    controller = BoundedCodexBridgeController(store, clock=clock)
    transport = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
    controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    _confirm(controller, BridgeRole.STRATEGY_ANALYST)

    original_publish = store.publish
    publication_count = 0

    def fail_after_response(publication):  # type: ignore[no-untyped-def]
        nonlocal publication_count
        publication_count += 1
        if publication_count == 2:
            raise RuntimeError("synthetic post-response publication crash")
        return original_publish(publication)

    monkeypatch.setattr(store, "publish", fail_after_response)
    with pytest.raises(RuntimeError, match="post-response publication crash"):
        controller.execute_confirmed_role(
            "bridge-run-controller",
            BridgeRole.STRATEGY_ANALYST,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )
    monkeypatch.setattr(store, "publish", original_publish)

    recovered = store.read_current("bridge-run-controller")
    replayed = replay_bridge(recovered)
    assert replayed.status == "in_progress"
    assert replayed.reconciliation_required is True
    strategy_assignment = canonical_assignment_id(
        "bridge-run-controller",
        _MODE,
        BridgeRole.STRATEGY_ANALYST,
    )
    assert transport.calls == [strategy_assignment]
    with pytest.raises(BridgeContractError, match="duplicate execution is forbidden"):
        controller.execute_confirmed_role(
            "bridge-run-controller",
            BridgeRole.STRATEGY_ANALYST,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )
    assert transport.calls == [strategy_assignment]


def test_bridge_namespace_rejects_cross_run_thread_and_turn_replay(tmp_path: Path) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    transport = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
    first = BoundedCodexBridgeController(store, clock=clock)
    first.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    _confirm(first, BridgeRole.STRATEGY_ANALYST)
    accepted = first.execute_confirmed_role(
        "bridge-run-controller",
        BridgeRole.STRATEGY_ANALYST,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=transport,
    )
    assert accepted.pointer.status == "in_progress"
    first_audit = next(
        item.model
        for item in accepted.decoded_artifacts()
        if item.logical_name == role_artifact_name(BridgeRole.STRATEGY_ANALYST, "audit")
    )
    assert isinstance(first_audit, BridgeExecutionAuditV1)
    assert first_audit.thread_id_sha256 is not None
    assert first_audit.turn_id_sha256 is not None

    second_run_id = "bridge-run-controller-second"
    second = BoundedCodexBridgeController(store, clock=clock)
    second.prepare_run(
        bridge_run_id=second_run_id,
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    request = second.read_role_request(second_run_id, BridgeRole.STRATEGY_ANALYST)
    second.confirm_role(
        second_run_id,
        BridgeRole.STRATEGY_ANALYST,
        authority=_authority(),
        confirmation_id="confirmation-second-strategy",
        idempotency_key="idempotency-second-strategy",
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

    class _ReusedIdentityTransport:
        auth_mode = _MODE
        transport_qualification = "deterministic_fixture"

        def execute(self, role_request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
            returned = transport.execute(role_request)
            return replace(
                returned,
                thread_id_sha256=first_audit.thread_id_sha256,
                turn_id_sha256=first_audit.turn_id_sha256,
            )

    rejected = second.execute_confirmed_role(
        second_run_id,
        BridgeRole.STRATEGY_ANALYST,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=_ReusedIdentityTransport(),
    )

    replayed = replay_bridge(rejected)
    assert rejected.pointer.status == "effect_unknown"
    assert replayed.reconciliation_required is True
    assert replayed.completed_roles == ()
    assert transport.calls == [
        canonical_assignment_id("bridge-run-controller", _MODE, BridgeRole.STRATEGY_ANALYST),
        canonical_assignment_id(second_run_id, _MODE, BridgeRole.STRATEGY_ANALYST),
    ]


def test_run_scoped_ids_and_store_wide_confirmation_ids_cannot_cross_runs(
    tmp_path: Path,
) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    controllers = [BoundedCodexBridgeController(store, clock=clock) for _ in range(2)]
    run_ids = ("bridge-run-one", "bridge-run-two")
    for controller, run_id in zip(controllers, run_ids, strict=True):
        controller.prepare_run(
            bridge_run_id=run_id,
            source_context=source,
            repository_root=REPOSITORY_ROOT,
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
            auth_mode=_MODE,
        )
    requests = [
        controller.read_role_request(run_id, BridgeRole.STRATEGY_ANALYST)
        for controller, run_id in zip(controllers, run_ids, strict=True)
    ]
    assert requests[0].context.assignment.assignment_id != (
        requests[1].context.assignment.assignment_id
    )
    assert requests[0].context.assignment.attempt_id != requests[1].context.assignment.attempt_id
    assert requests[0].context.assignment.assignment_id == canonical_assignment_id(
        run_ids[0], _MODE, BridgeRole.STRATEGY_ANALYST
    )
    assert requests[0].context.assignment.attempt_id == canonical_attempt_id(
        run_ids[0], _MODE, BridgeRole.STRATEGY_ANALYST
    )

    _confirm_run(
        controllers[0],
        run_ids[0],
        BridgeRole.STRATEGY_ANALYST,
        confirmation_id="confirmation-shared",
        idempotency_key="idempotency-shared",
    )
    with pytest.raises(BridgeControllerError, match="identifier was reused"):
        _confirm_run(
            controllers[1],
            run_ids[1],
            BridgeRole.STRATEGY_ANALYST,
            confirmation_id="confirmation-shared",
            idempotency_key="idempotency-unique",
        )
    with pytest.raises(BridgeControllerError, match="identifier was reused"):
        _confirm_run(
            controllers[1],
            run_ids[1],
            BridgeRole.STRATEGY_ANALYST,
            confirmation_id="confirmation-unique",
            idempotency_key="idempotency-shared",
        )


def test_dependent_request_is_atomic_with_successful_parent_publication(tmp_path: Path) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    controller = BoundedCodexBridgeController(store, clock=clock)
    transport = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
    controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    for role in BRIDGE_ROLE_ORDER[:3]:
        _confirm(controller, role)
        controller.execute_confirmed_role(
            "bridge-run-controller",
            role,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )
    after_skeptic = store.read_current("bridge-run-controller")
    skeptic_names = {item.logical_name for item in after_skeptic.manifest.inventory}
    assert role_artifact_name(BridgeRole.SKEPTIC_FALSIFIER, "result") in skeptic_names
    assert role_artifact_name(BridgeRole.ADJUDICATOR, "request") in skeptic_names

    _confirm(controller, BridgeRole.ADJUDICATOR)
    after_adjudicator = controller.execute_confirmed_role(
        "bridge-run-controller",
        BridgeRole.ADJUDICATOR,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=transport,
    )
    adjudicator_names = {item.logical_name for item in after_adjudicator.manifest.inventory}
    assert role_artifact_name(BridgeRole.ADJUDICATOR, "result") in adjudicator_names
    assert role_artifact_name(BridgeRole.REPORT_WRITER, "request") in adjudicator_names


def test_terminal_reader_requires_execution_identity_claim(tmp_path: Path) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    controller = BoundedCodexBridgeController(store, clock=clock)
    controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    _confirm(controller, BridgeRole.STRATEGY_ANALYST)
    completed = controller.execute_confirmed_role(
        "bridge-run-controller",
        BridgeRole.STRATEGY_ANALYST,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock),
    )
    audit = next(
        item.model
        for item in completed.decoded_artifacts()
        if item.logical_name == role_artifact_name(BridgeRole.STRATEGY_ANALYST, "audit")
    )
    assert isinstance(audit, BridgeExecutionAuditV1)
    assert audit.thread_id_sha256 is not None
    claim = store.root / ".i" / f"thread-{audit.thread_id_sha256}.json"
    claim.unlink()

    with pytest.raises(BridgeStorageError, match="identity claim"):
        store.read_current("bridge-run-controller")


def test_reader_requires_both_confirmation_identifier_claims(tmp_path: Path) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    controller = BoundedCodexBridgeController(store, clock=clock)
    controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    _confirm(controller, BridgeRole.STRATEGY_ANALYST)
    confirmed = store.read_current("bridge-run-controller")
    second_run_id = "bridge-run-controller-second"
    second = BoundedCodexBridgeController(store, clock=clock)
    second.prepare_run(
        bridge_run_id=second_run_id,
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    confirmation = next(
        item.model
        for item in confirmed.decoded_artifacts()
        if item.logical_name == role_artifact_name(BridgeRole.STRATEGY_ANALYST, "confirmation")
    )
    assert isinstance(confirmation, BridgeRoleConfirmationV1)

    for kind, identifier in (
        ("confirmation", confirmation.confirmation_id),
        ("idempotency", confirmation.idempotency_key),
    ):
        identifier_sha = domain_sha256(
            "poker-bounded-codex-bridge-confirmation-identifier-v1",
            identifier,
        )
        claim = store.root / ".c" / f"{kind}-{identifier_sha}.json"
        retained = claim.read_bytes()
        claim.unlink()
        with pytest.raises(BridgeStorageError, match="claim is missing or invalid"):
            store.read_current("bridge-run-controller")
        if kind == "confirmation":
            with pytest.raises(BridgeControllerError, match="identifier was reused"):
                _confirm_run(
                    second,
                    second_run_id,
                    BridgeRole.STRATEGY_ANALYST,
                    confirmation_id=confirmation.confirmation_id,
                    idempotency_key="idempotency-second-after-deletion",
                )
        claim.write_bytes(retained)
    assert replay_bridge(store.read_current("bridge-run-controller")).status == "approval_required"


def test_negative_terminal_audit_keeps_typed_transport_evidence(tmp_path: Path) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    controller = BoundedCodexBridgeController(store, clock=clock)
    controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    _confirm(controller, BridgeRole.STRATEGY_ANALYST)

    class _KnownFailureTransport:
        auth_mode = _MODE
        transport_qualification = "deterministic_fixture"

        def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
            policy = request.context.runtime_policy
            raise BridgeTransportFailure(
                "subscription_protocol_or_output_invalid",
                effect_state=BridgeEffectState.FAILED,
                launched_at=clock(),
                completed_at=clock(),
                duration_ms=1,
                stream_bytes=91,
                item_types=("command_execution",),
                thread_id_sha256="a" * 64,
                turn_id_sha256="b" * 64,
                evidence=BridgeTransportFailureEvidence(
                    usage=BridgeTransportUsageV1(
                        input_tokens=123,
                        cached_input_tokens=0,
                        output_tokens=45,
                        reasoning_output_tokens=6,
                        estimated_cost_micro_usd=None,
                        cost_authority="not_applicable",
                    ),
                    response_bytes=19,
                    runtime_identity=policy.runtime_identity,
                    model_identity_evidence="unavailable",
                    observed_model=None,
                    observed_model_provider=None,
                    observed_reasoning_effort=None,
                    observed_service_tier=None,
                ),
            )

    terminal = controller.execute_confirmed_role(
        "bridge-run-controller",
        BridgeRole.STRATEGY_ANALYST,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=_KnownFailureTransport(),
    )
    audit = next(
        item.model
        for item in terminal.decoded_artifacts()
        if item.logical_name == role_artifact_name(BridgeRole.STRATEGY_ANALYST, "audit")
    )
    assert isinstance(audit, BridgeExecutionAuditV1)
    assert audit.usage is not None
    assert audit.usage.input_tokens == 123
    assert audit.usage.output_tokens == 45
    assert audit.response_bytes == 19
    assert audit.model_identity_evidence == "unavailable"
    assert audit.observed_identity_sha256 is None
    replayed = replay_bridge(terminal)
    assert replayed.total_input_tokens == 123
    assert replayed.total_output_tokens == 45


@pytest.mark.parametrize(
    ("effect_state", "expected_kind", "expected_status"),
    (
        (BridgeEffectState.CANCELLED, "cooperative", "cancelled"),
        (BridgeEffectState.CANCEL_UNCONFIRMED, "unconfirmed", "cancel_unconfirmed"),
    ),
)
def test_cancellation_kind_is_effect_state_bound(
    tmp_path: Path,
    effect_state: BridgeEffectState,
    expected_kind: str,
    expected_status: str,
) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    controller = BoundedCodexBridgeController(
        BoundedCodexBridgeStore(tmp_path / "bridge"),
        clock=clock,
    )
    controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    _confirm(controller, BridgeRole.STRATEGY_ANALYST)

    class _CancelledTransport:
        auth_mode = _MODE
        transport_qualification = "deterministic_fixture"

        def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
            raise BridgeTransportFailure(
                "synthetic-cancellation",
                effect_state=effect_state,
                launched_at=clock(),
                completed_at=clock(),
                duration_ms=1,
                stream_bytes=0,
                thread_id_sha256="c" * 64,
                turn_id_sha256="d" * 64,
            )

    terminal = controller.execute_confirmed_role(
        "bridge-run-controller",
        BridgeRole.STRATEGY_ANALYST,
        auth_mode=_MODE,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=_CancelledTransport(),
    )
    audit = next(
        item.model
        for item in terminal.decoded_artifacts()
        if item.logical_name == role_artifact_name(BridgeRole.STRATEGY_ANALYST, "audit")
    )
    assert isinstance(audit, BridgeExecutionAuditV1)
    assert audit.cancellation_kind == expected_kind
    assert replay_bridge(terminal).status == expected_status


def test_controller_confirmation_retry_adopts_post_rename_orphan(tmp_path: Path) -> None:
    clock = StepClock()
    source = verified_bridge_source(tmp_path / "p3")
    armed = False
    fired = False

    def inject(hook: str) -> None:
        nonlocal fired
        if armed and hook == "codex_bridge.publish.revision.after_rename" and not fired:
            fired = True
            raise OSError("synthetic confirmation publication crash")

    store = BoundedCodexBridgeStore(tmp_path / "bridge", fault_injector=inject)
    controller = BoundedCodexBridgeController(store, clock=clock)
    controller.prepare_run(
        bridge_run_id="bridge-run-controller",
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    armed = True

    with pytest.raises(OSError, match="confirmation publication crash"):
        _confirm(controller, BridgeRole.STRATEGY_ANALYST)

    before_recovery = store.read_current("bridge-run-controller")
    assert before_recovery.pointer.revision == 1
    with pytest.raises(BridgeStorageError, match=r"reconciled.*retried from current"):
        _confirm(controller, BridgeRole.STRATEGY_ANALYST)
    recovered = store.read_current("bridge-run-controller")
    _run, _control, _transactions, revisions, _current = store._paths("bridge-run-controller")
    replayed = replay_bridge(recovered)

    assert recovered.pointer.revision == 2
    assert len(store._revision_candidates(revisions, 2)) == 1
    assert role_artifact_name(BridgeRole.STRATEGY_ANALYST, "confirmation") in {
        item.logical_name for item in recovered.decoded_artifacts()
    }
    assert replayed.status == "approval_required"
    assert replayed.reconciliation_required is False
