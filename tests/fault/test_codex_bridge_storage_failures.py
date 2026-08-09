from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from poker_deliberation.codex_bridge.canonical import canonical_json_bytes, domain_sha256
from poker_deliberation.codex_bridge.conformance import build_bridge_role_conformance
from poker_deliberation.codex_bridge.contracts import build_run_plan, build_runtime_policy
from poker_deliberation.codex_bridge.models import (
    TERMINAL_MANIFEST_HASH_DOMAIN,
    BridgeCompletionMarkerV1,
    BridgeTerminalManifestV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.storage import (
    BoundedCodexBridgeStore,
    BridgeStorageError,
    BridgeStoredArtifact,
)
from tests.codex_bridge_support import REPOSITORY_ROOT, verified_bridge_source


def _anchors(tmp_path: Path):  # type: ignore[no-untyped-def]
    source = verified_bridge_source(tmp_path / "p3")
    created = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)
    plan = build_run_plan(
        bridge_run_id="bridge-run-storage-fault",
        source_context=source,
        runtime_policy=build_runtime_policy(auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION),
        role_conformance=build_bridge_role_conformance(
            REPOSITORY_ROOT,
            repository_commit_id="1" * 40,
        ),
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        created_at=created,
    )
    artifacts = (
        BridgeStoredArtifact("run_plan.json", "run_plan", plan),
        BridgeStoredArtifact("source_context.json", "source_context", source),
    )
    return plan, created, artifacts


def _one_shot_fault(target: str):  # type: ignore[no-untyped-def]
    fired = False

    def inject(hook: str) -> None:
        nonlocal fired
        if hook == target and not fired:
            fired = True
            raise OSError(f"synthetic fault at {target}")

    return inject


@pytest.mark.parametrize(
    ("fault_hook", "pointer_was_replaced"),
    (
        ("codex_bridge.publish.revision.after_rename", False),
        ("codex_bridge.publish.current.before_replace", False),
        ("codex_bridge.publish.current.after_replace", True),
    ),
)
def test_publication_fault_boundaries_reconcile_without_duplicate_ordinal(
    tmp_path: Path,
    fault_hook: str,
    pointer_was_replaced: bool,
) -> None:
    plan, created, artifacts = _anchors(tmp_path)
    transaction_id = "txn-" + "a" * 32
    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: transaction_id,
        fault_injector=_one_shot_fault(fault_hook),
    )
    request = store.prepare_request(
        run_plan=plan,
        status="approval_required",
        expected=None,
        published_at=created,
        artifacts=artifacts,
    )

    with pytest.raises(OSError, match="synthetic fault"):
        store.publish(request)

    _run, _control, _transactions, revisions, current = store._paths(plan.bridge_run_id)
    assert len(store._revision_candidates(revisions, 1)) == 1
    assert current.exists() is pointer_was_replaced
    recovery = BoundedCodexBridgeStore(tmp_path / "bridge")
    if pointer_was_replaced:
        verified = recovery.read_current(plan.bridge_run_id)
        with pytest.raises(BridgeStorageError, match="lost CAS"):
            recovery.publish(request)
    else:
        recovered = recovery.publish(request)
        verified = recovery.read_current(plan.bridge_run_id)
        assert recovered.transaction_id == transaction_id

    assert verified.pointer.transaction_id == transaction_id
    assert len(recovery._revision_candidates(revisions, 1)) == 1


def test_recovery_adopts_existing_successor_instead_of_new_transaction(
    tmp_path: Path,
) -> None:
    plan, created, artifacts = _anchors(tmp_path)
    transaction_ids = iter(("txn-" + "a" * 32, "txn-" + "b" * 32))
    armed = False
    fired = False

    def inject(hook: str) -> None:
        nonlocal fired
        if armed and hook == "codex_bridge.publish.revision.after_rename" and not fired:
            fired = True
            raise OSError("synthetic successor orphan")

    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: next(transaction_ids),
        fault_injector=inject,
    )
    first_request = store.prepare_request(
        run_plan=plan,
        status="approval_required",
        expected=None,
        published_at=created,
        artifacts=artifacts,
    )
    store.publish(first_request)
    first = store.read_current(plan.bridge_run_id)
    armed = True
    orphan_request = store.prepare_request(
        run_plan=plan,
        status="failed",
        expected=first,
        published_at=created + timedelta(seconds=1),
        artifacts=artifacts,
    )
    with pytest.raises(OSError, match="successor orphan"):
        store.publish(orphan_request)

    recovery = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: "txn-" + "c" * 32,
    )
    still_first = recovery.read_current(plan.bridge_run_id)
    replacement_request = recovery.prepare_request(
        run_plan=plan,
        status="failed",
        expected=still_first,
        published_at=created + timedelta(seconds=2),
        artifacts=artifacts,
    )
    adopted = recovery.publish(replacement_request)
    verified = recovery.read_current(plan.bridge_run_id)
    _run, _control, _transactions, revisions, _current = recovery._paths(plan.bridge_run_id)

    assert adopted.transaction_id == "txn-" + "b" * 32
    assert verified.pointer.transaction_id == adopted.transaction_id
    assert verified.pointer.status == "failed"
    assert len(recovery._revision_candidates(revisions, 2)) == 1
    assert not (revisions / ("r2-txn-" + "c" * 32)).exists()


def test_logically_different_retry_reconciles_orphan_then_requires_reread(
    tmp_path: Path,
) -> None:
    plan, created, artifacts = _anchors(tmp_path)
    orphan_transaction = "txn-" + "a" * 32
    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: orphan_transaction,
        fault_injector=_one_shot_fault("codex_bridge.publish.revision.after_rename"),
    )
    orphan_request = store.prepare_request(
        run_plan=plan,
        status="approval_required",
        expected=None,
        published_at=created,
        artifacts=artifacts,
    )
    with pytest.raises(OSError, match="synthetic fault"):
        store.publish(orphan_request)

    recovery = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: "txn-" + "b" * 32,
    )
    different_request = recovery.prepare_request(
        run_plan=plan,
        status="failed",
        expected=None,
        published_at=created + timedelta(seconds=1),
        artifacts=artifacts,
    )
    with pytest.raises(BridgeStorageError, match=r"reconciled.*retried from current"):
        recovery.publish(different_request)

    verified = recovery.read_current(plan.bridge_run_id)
    _run, _control, _transactions, revisions, _current = recovery._paths(plan.bridge_run_id)
    assert verified.pointer.transaction_id == orphan_transaction
    assert verified.pointer.status == "approval_required"
    assert len(recovery._revision_candidates(revisions, 1)) == 1
    assert not (revisions / ("r1-txn-" + "b" * 32)).exists()


def test_corrupt_orphan_blocks_new_ordinal_but_current_remains_replayable(
    tmp_path: Path,
) -> None:
    plan, created, artifacts = _anchors(tmp_path)
    transaction_ids = iter(("txn-" + "a" * 32, "txn-" + "b" * 32))
    armed = False

    def inject(hook: str) -> None:
        if armed and hook == "codex_bridge.publish.revision.after_rename":
            raise OSError("synthetic corrupt orphan")

    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: next(transaction_ids),
        fault_injector=inject,
    )
    store.publish(
        store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=None,
            published_at=created,
            artifacts=artifacts,
        )
    )
    first = store.read_current(plan.bridge_run_id)
    armed = True
    with pytest.raises(OSError, match="corrupt orphan"):
        store.publish(
            store.prepare_request(
                run_plan=plan,
                status="failed",
                expected=first,
                published_at=created + timedelta(seconds=1),
                artifacts=artifacts,
            )
        )
    _run, _control, _transactions, revisions, _current = store._paths(plan.bridge_run_id)
    orphan = store._revision_candidates(revisions, 2)[0]
    payload = orphan / "payload" / "source_context.json"
    payload.write_bytes(payload.read_bytes() + b" ")

    recovery = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: "txn-" + "c" * 32,
    )
    replayed_first = recovery.read_current(plan.bridge_run_id)
    assert replayed_first.pointer == first.pointer
    retry = recovery.prepare_request(
        run_plan=plan,
        status="failed",
        expected=replayed_first,
        published_at=created + timedelta(seconds=2),
        artifacts=artifacts,
    )
    with pytest.raises(BridgeStorageError, match="hash or size mismatch"):
        recovery.publish(retry)

    assert recovery.read_current(plan.bridge_run_id).pointer == first.pointer
    assert len(recovery._revision_candidates(revisions, 2)) == 1
    assert not (revisions / ("r2-txn-" + "c" * 32)).exists()


def test_canonical_rollback_orphan_is_not_adopted_or_replaced(tmp_path: Path) -> None:
    plan, created, artifacts = _anchors(tmp_path)
    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: "txn-" + "a" * 32,
    )
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
    rollback = store._prepare(
        store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=first,
            published_at=created + timedelta(seconds=1),
            artifacts=artifacts,
        )
    )
    _run, _control, _transactions, revisions, _current = store._paths(plan.bridge_run_id)
    orphan = revisions / f"r{rollback.pointer.revision}-{rollback.pointer.transaction_id}"
    payload = orphan / "payload"
    payload.mkdir(parents=True)
    for entry in rollback.manifest.inventory:
        destination = payload / entry.logical_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(rollback.artifact_bytes[entry.logical_name])
    (orphan / "manifest.json").write_bytes(rollback.manifest_bytes)

    recovery = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: "txn-" + "b" * 32,
    )
    retry = recovery.prepare_request(
        run_plan=plan,
        status="approval_required",
        expected=recovery.read_current(plan.bridge_run_id),
        published_at=created + timedelta(seconds=2),
        artifacts=(*artifacts, retained),
    )
    with pytest.raises(BridgeStorageError, match="rolled back"):
        recovery.publish(retry)

    assert recovery.read_current(plan.bridge_run_id).pointer == first.pointer
    assert len(recovery._revision_candidates(revisions, 2)) == 1
    assert not (revisions / ("r2-txn-" + "b" * 32)).exists()


def test_valid_shaped_orphan_with_wrong_parent_lineage_is_not_adopted(tmp_path: Path) -> None:
    plan, created, artifacts = _anchors(tmp_path)
    transaction_ids = iter(("txn-" + "a" * 32, "txn-" + "b" * 32))
    armed = False

    def inject(hook: str) -> None:
        if armed and hook == "codex_bridge.publish.revision.after_rename":
            raise OSError("synthetic lineage orphan")

    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: next(transaction_ids),
        fault_injector=inject,
    )
    store.publish(
        store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=None,
            published_at=created,
            artifacts=artifacts,
        )
    )
    first = store.read_current(plan.bridge_run_id)
    armed = True
    with pytest.raises(OSError, match="lineage orphan"):
        store.publish(
            store.prepare_request(
                run_plan=plan,
                status="failed",
                expected=first,
                published_at=created + timedelta(seconds=1),
                artifacts=artifacts,
            )
        )
    _run, _control, _transactions, revisions, _current = store._paths(plan.bridge_run_id)
    orphan = store._revision_candidates(revisions, 2)[0]
    manifest_path = orphan / "manifest.json"
    manifest = BridgeTerminalManifestV1.model_validate_json(manifest_path.read_bytes())
    manifest_payload = manifest.model_dump(mode="python")
    manifest_payload.pop("manifest_sha256")
    manifest_payload["expected_pointer_sha256"] = "f" * 64
    forged_manifest = BridgeTerminalManifestV1.model_validate(
        {
            **manifest_payload,
            "manifest_sha256": domain_sha256(
                TERMINAL_MANIFEST_HASH_DOMAIN,
                manifest_payload,
            ),
        },
        strict=True,
    )
    manifest_path.write_bytes(canonical_json_bytes(forged_manifest))
    marker_path = orphan / "completion.json"
    marker = BridgeCompletionMarkerV1.model_validate_json(marker_path.read_bytes())
    forged_marker = marker.model_copy(
        update={"terminal_manifest_sha256": forged_manifest.manifest_sha256}
    )
    marker_path.write_bytes(canonical_json_bytes(forged_marker))

    recovery = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: "txn-" + "c" * 32,
    )
    retry = recovery.prepare_request(
        run_plan=plan,
        status="failed",
        expected=recovery.read_current(plan.bridge_run_id),
        published_at=created + timedelta(seconds=2),
        artifacts=artifacts,
    )
    with pytest.raises(BridgeStorageError, match="parent lineage mismatch"):
        recovery.publish(retry)

    assert recovery.read_current(plan.bridge_run_id).pointer == first.pointer
    assert len(recovery._revision_candidates(revisions, 2)) == 1
    assert not (revisions / ("r2-txn-" + "c" * 32)).exists()


def test_concurrent_orphan_recovery_publishes_only_existing_revision(tmp_path: Path) -> None:
    plan, created, artifacts = _anchors(tmp_path)
    transaction_ids = iter(("txn-" + "a" * 32, "txn-" + "b" * 32))
    armed = False

    def inject(hook: str) -> None:
        if armed and hook == "codex_bridge.publish.revision.after_rename":
            raise OSError("synthetic concurrent orphan")

    store = BoundedCodexBridgeStore(
        tmp_path / "bridge",
        transaction_id_factory=lambda: next(transaction_ids),
        fault_injector=inject,
    )
    store.publish(
        store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=None,
            published_at=created,
            artifacts=artifacts,
        )
    )
    first = store.read_current(plan.bridge_run_id)
    armed = True
    with pytest.raises(OSError, match="concurrent orphan"):
        store.publish(
            store.prepare_request(
                run_plan=plan,
                status="failed",
                expected=first,
                published_at=created + timedelta(seconds=1),
                artifacts=artifacts,
            )
        )

    stores = (
        BoundedCodexBridgeStore(
            tmp_path / "bridge",
            transaction_id_factory=lambda: "txn-" + "c" * 32,
        ),
        BoundedCodexBridgeStore(
            tmp_path / "bridge",
            transaction_id_factory=lambda: "txn-" + "d" * 32,
        ),
    )
    requests = tuple(
        item.prepare_request(
            run_plan=plan,
            status="failed",
            expected=item.read_current(plan.bridge_run_id),
            published_at=created + timedelta(seconds=2),
            artifacts=artifacts,
        )
        for item in stores
    )
    barrier = Barrier(2)

    def recover(index: int):  # type: ignore[no-untyped-def]
        barrier.wait()
        try:
            return stores[index].publish(requests[index])
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(recover, range(2)))

    outcomes = [item for item in results if not isinstance(item, Exception)]
    assert len(outcomes) == 1
    assert outcomes[0].transaction_id == "txn-" + "b" * 32
    reopened = BoundedCodexBridgeStore(tmp_path / "bridge")
    verified = reopened.read_current(plan.bridge_run_id)
    _run, _control, _transactions, revisions, _current = reopened._paths(plan.bridge_run_id)
    assert verified.pointer.transaction_id == "txn-" + "b" * 32
    assert len(reopened._revision_candidates(revisions, 2)) == 1
