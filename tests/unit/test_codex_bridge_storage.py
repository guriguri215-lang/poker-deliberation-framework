from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.codex_bridge.canonical import BridgeCanonicalError
from poker_deliberation.codex_bridge.conformance import build_bridge_role_conformance
from poker_deliberation.codex_bridge.contracts import build_run_plan, build_runtime_policy
from poker_deliberation.codex_bridge.controller import (
    BoundedCodexBridgeController,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeEffectState,
    BridgeExecutionAuditV1,
    BridgeRole,
    BridgeSourceContextV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.storage import (
    BoundedCodexBridgeStore,
    BridgeStorageError,
    BridgeStoredArtifact,
)
from poker_deliberation.codex_bridge.transport import BridgeTransportFailure, BridgeTransportResult
from tests.codex_bridge_support import REPOSITORY_ROOT, verified_bridge_source


def _anchors(tmp_path: Path):  # type: ignore[no-untyped-def]
    source = verified_bridge_source(tmp_path / "p3")
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        include_repository_skill_bindings=True,
    )
    created = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)
    plan = build_run_plan(
        bridge_run_id="bridge-run-storage",
        source_context=source,
        runtime_policy=build_runtime_policy(auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION),
        role_conformance=conformance,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        created_at=created,
    )
    artifacts = (
        BridgeStoredArtifact("run_plan.json", "run_plan", plan),
        BridgeStoredArtifact("source_context.json", "source_context", source),
    )
    return source, plan, created, artifacts


def test_marker_last_store_publishes_and_replays_revision_lineage(tmp_path: Path) -> None:
    _source, plan, created, artifacts = _anchors(tmp_path)
    transaction_ids = iter(("txn-" + "a" * 32, "txn-" + "b" * 32, "txn-" + "c" * 32))
    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: next(transaction_ids),
    )

    first_request = store.prepare_request(
        run_plan=plan,
        status="approval_required",
        expected=None,
        published_at=created,
        artifacts=artifacts,
    )
    first = store.publish(first_request)
    verified_first = store.read_current(plan.bridge_run_id)
    second_request = store.prepare_request(
        run_plan=plan,
        status="failed",
        expected=verified_first,
        published_at=created + timedelta(seconds=1),
        artifacts=artifacts,
    )
    second = store.publish(second_request)
    verified_second = store.read_current(plan.bridge_run_id)

    assert first.revision == 1
    assert first.completion_marker_sha256 is None
    assert second.revision == 2
    assert second.completion_marker_sha256 is not None
    assert verified_second.pointer.status == "failed"
    assert verified_second.completion_marker is not None
    assert verified_second.manifest.previous_manifest_sha256 == first.manifest_sha256
    assert verified_second.manifest.expected_pointer_sha256 == first.pointer_sha256
    downgrade = store.prepare_request(
        run_plan=plan,
        status="in_progress",
        expected=verified_second,
        published_at=created + timedelta(seconds=2),
        artifacts=artifacts,
    )
    with pytest.raises(BridgeStorageError, match="terminal bridge revision"):
        store.publish(downgrade)
    assert store.read_current(plan.bridge_run_id).pointer == verified_second.pointer


def test_store_rejects_stale_cas_and_payload_mutation(tmp_path: Path) -> None:
    _source, plan, created, artifacts = _anchors(tmp_path)
    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: "txn-" + "c" * 32,
    )
    request = store.prepare_request(
        run_plan=plan,
        status="approval_required",
        expected=None,
        published_at=created,
        artifacts=artifacts,
    )
    store.publish(request)
    with pytest.raises(BridgeStorageError, match="lost CAS"):
        store.publish(request)

    verified = store.read_current(plan.bridge_run_id)
    run, _control, _transactions, revisions, _current = store._paths(plan.bridge_run_id)
    assert run.exists()
    payload = (
        revisions
        / f"r{verified.pointer.revision}-{verified.pointer.transaction_id}"
        / "payload"
        / "source_context.json"
    )
    data = payload.read_bytes()
    payload.write_bytes(data + b" ")
    with pytest.raises(BridgeStorageError, match="hash or size mismatch"):
        store.read_current(plan.bridge_run_id)


def test_successor_cannot_remove_or_mutate_prior_inventory(tmp_path: Path) -> None:
    source, plan, created, artifacts = _anchors(tmp_path)
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    retained = BridgeStoredArtifact("retained_plan.json", "run_plan", plan)
    store.publish(
        store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=None,
            published_at=created,
            artifacts=(*artifacts, retained),
        )
    )
    first = store.read_current(plan.bridge_run_id)

    rollback = store.prepare_request(
        run_plan=plan,
        status="approval_required",
        expected=first,
        published_at=created + timedelta(seconds=1),
        artifacts=artifacts,
    )
    with pytest.raises(BridgeStorageError, match="rolled back"):
        store.publish(rollback)

    different_plan = build_run_plan(
        bridge_run_id="bridge-run-mutated-retained-artifact",
        source_context=source,
        runtime_policy=build_runtime_policy(auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION),
        role_conformance=build_bridge_role_conformance(
            REPOSITORY_ROOT,
            repository_commit_id="1" * 40,
            include_repository_skill_bindings=True,
        ),
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        created_at=created + timedelta(seconds=2),
    )
    mutation = store.prepare_request(
        run_plan=plan,
        status="approval_required",
        expected=first,
        published_at=created + timedelta(seconds=2),
        artifacts=(
            *artifacts,
            BridgeStoredArtifact(
                "retained_plan.json",
                "run_plan",
                different_plan,
            ),
        ),
    )
    with pytest.raises(BridgeStorageError, match="mutated"):
        store.publish(mutation)
    assert store.read_current(plan.bridge_run_id).pointer == first.pointer


def test_history_reader_rejects_canonical_inventory_rollback(tmp_path: Path) -> None:
    source, plan, created, artifacts = _anchors(tmp_path)
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    store.publish(
        store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=None,
            published_at=created,
            artifacts=(
                *artifacts,
                BridgeStoredArtifact("retained_source.json", "source_context", source),
            ),
        )
    )
    first = store.read_current(plan.bridge_run_id)
    rollback = store._prepare(
        store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=first,
            published_at=created + timedelta(seconds=1),
            artifacts=artifacts,
        )
    )
    _run, _control, _transactions, revisions, current = store._paths(plan.bridge_run_id)
    revision = revisions / f"r{rollback.pointer.revision}-{rollback.pointer.transaction_id}"
    payload = revision / "payload"
    payload.mkdir(parents=True)
    for entry in rollback.manifest.inventory:
        destination = payload / entry.logical_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(rollback.artifact_bytes[entry.logical_name])
    (revision / "manifest.json").write_bytes(rollback.manifest_bytes)
    current.write_bytes(rollback.pointer_bytes)

    with pytest.raises(BridgeStorageError, match="rolled back"):
        store.read_current(plan.bridge_run_id)


class _StepClock:
    def __init__(self) -> None:
        self.current = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def _prepare_confirmed_partial_thread_run(
    store: BoundedCodexBridgeStore,
    *,
    clock: _StepClock,
    source: BridgeSourceContextV1,
    bridge_run_id: str,
) -> BoundedCodexBridgeController:
    controller = BoundedCodexBridgeController(store, clock=clock)
    controller.prepare_run(
        bridge_run_id=bridge_run_id,
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
    )
    role = BridgeRole.STRATEGY_ANALYST
    request = controller.read_role_request(bridge_run_id, role)
    controller.confirm_role(
        bridge_run_id,
        role,
        authority=BridgeConfirmationAuthorityV1(
            authority_id=f"authority-{bridge_run_id}",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        confirmation_id=f"confirmation-{bridge_run_id}",
        idempotency_key=f"idempotency-{bridge_run_id}",
        expected_request_sha256=request.request_sha256,
        expected_request_bytes_sha256=request.request_bytes_sha256,
        expected_envelope_sha256=request.context.envelope_sha256,
        expected_runtime_policy_sha256=request.context.runtime_policy.policy_sha256,
        expected_auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        expected_runtime_identity=request.context.runtime_policy.runtime_identity,
        expected_model_provider=request.context.runtime_policy.model_provider,
        expected_model=request.context.runtime_policy.model,
        expected_credential_reference=request.context.runtime_policy.credential_reference,
        expected_remote_retention_policy=(request.context.runtime_policy.remote_retention_policy),
    )
    return controller


class _PartialThreadFailureTransport:
    auth_mode = RuntimeAuthModeV1.CODEX_SUBSCRIPTION
    transport_qualification = "deterministic_fixture"

    def __init__(self, *, clock: _StepClock, thread_sha256: str) -> None:
        self.clock = clock
        self.thread_sha256 = thread_sha256

    def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
        raise BridgeTransportFailure(
            "subscription_protocol_or_output_invalid",
            effect_state=BridgeEffectState.EFFECT_UNKNOWN,
            launched_at=None,
            completed_at=self.clock(),
            duration_ms=1,
            stream_bytes=64,
            thread_id_sha256=self.thread_sha256,
            turn_id_sha256=None,
        )


def test_partial_thread_claim_is_global_replayable_and_corruption_blocks_history(
    tmp_path: Path,
) -> None:
    source = verified_bridge_source(tmp_path / "p3")
    store = BoundedCodexBridgeStore(tmp_path / "bridge")
    clock = _StepClock()
    role = BridgeRole.STRATEGY_ANALYST
    thread_sha256 = "a" * 64
    first_run_id = "bridge-run-partial-thread-first"
    first = _prepare_confirmed_partial_thread_run(
        store,
        clock=clock,
        source=source,
        bridge_run_id=first_run_id,
    )
    first_terminal = first.execute_confirmed_role(
        first_run_id,
        role,
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=_PartialThreadFailureTransport(
            clock=clock,
            thread_sha256=thread_sha256,
        ),
    )
    first_audit = next(
        item.model
        for item in first_terminal.decoded_artifacts()
        if item.logical_name == role_artifact_name(role, "audit")
    )
    assert isinstance(first_audit, BridgeExecutionAuditV1)
    assert first_audit.thread_id_sha256 == thread_sha256
    assert first_audit.turn_id_sha256 is None
    assert replay_bridge(first_terminal).reconciliation_required is True
    claim_path = store.root / ".i" / f"thread-{thread_sha256}.json"
    assert claim_path.is_file()
    assert not tuple((store.root / ".i").glob("turn-*.json"))

    second_run_id = "bridge-run-partial-thread-second"
    second = _prepare_confirmed_partial_thread_run(
        store,
        clock=clock,
        source=source,
        bridge_run_id=second_run_id,
    )
    second_terminal = second.execute_confirmed_role(
        second_run_id,
        role,
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
        transport=_PartialThreadFailureTransport(
            clock=clock,
            thread_sha256=thread_sha256,
        ),
    )
    second_audit = next(
        item.model
        for item in second_terminal.decoded_artifacts()
        if item.logical_name == role_artifact_name(role, "audit")
    )
    assert isinstance(second_audit, BridgeExecutionAuditV1)
    assert second_audit.effect_state is BridgeEffectState.EFFECT_UNKNOWN
    assert second_audit.failure_reason_code == "execution_identity_registry_rejected"
    assert second_audit.thread_id_sha256 == thread_sha256
    assert second_audit.turn_id_sha256 is None
    assert replay_bridge(second_terminal).reconciliation_required is True

    claim_path.write_bytes(b"{}")
    with pytest.raises(BridgeCanonicalError, match="strict schema"):
        store.read_current(first_run_id)
    with pytest.raises(BridgeStorageError, match="identity registry history"):
        store.verify_execution_identity_history()
