from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation.codex_bridge.conformance import build_bridge_role_conformance
from poker_deliberation.codex_bridge.contracts import build_run_plan, build_runtime_policy
from poker_deliberation.codex_bridge.models import RuntimeAuthModeV1
from poker_deliberation.codex_bridge.storage import (
    BoundedCodexBridgeStore,
    BridgeStorageError,
    BridgeStoredArtifact,
)
from tests.codex_bridge_support import REPOSITORY_ROOT, verified_bridge_source


def _anchors(tmp_path: Path):  # type: ignore[no-untyped-def]
    source = verified_bridge_source(tmp_path / "p3")
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
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
    transaction_ids = iter(("txn-" + "a" * 32, "txn-" + "b" * 32))
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
