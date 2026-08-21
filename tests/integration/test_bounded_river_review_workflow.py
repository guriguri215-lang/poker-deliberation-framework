from __future__ import annotations

import os
import shutil
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pytest

from poker_deliberation import bounded_river_review_workflow as workflow_product
from poker_deliberation.bounded_river_review_workflow import (
    BoundedRiverReviewWorkflowError,
    bounded_river_confirmation_hashes,
    bounded_river_review_report_view,
    bounded_river_review_role_request_preview,
    bounded_river_review_workflow_status,
    confirm_bounded_river_review_role_request,
    confirm_bounded_river_review_workflow,
    execute_bounded_river_review_role,
    prepare_bounded_river_review_workflow,
    replay_bounded_river_review_workflow,
    resume_bounded_river_review_workflow,
    run_bounded_river_review_workflow,
)
from poker_deliberation.codex_bridge import product as bridge_product
from poker_deliberation.codex_bridge.controller import BoundedCodexBridgeController
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BridgeRole,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore
from poker_deliberation.codex_bridge.transport import DeterministicReadOnlyTransport
from poker_deliberation.config import AppConfig
from poker_deliberation.storage.revision_canonical import run_lock_key_sha256
from tests.bounded_river_call_ev_support import app_config, range_definition, river_source

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_storage_root() -> Generator[Path, None, None]:
    with TemporaryDirectory(prefix="p3w-") as directory:
        yield Path(directory)


def _windows_utf16_units_with_nul(path: Path) -> int:
    return len(str(path.resolve(strict=False)).encode("utf-16-le")) // 2 + 1


def _absolute_windows_root(length: int, fill: str) -> Path:
    root = Path("C:\\" + fill * (length - len("C:\\")))
    assert len(str(root)) == length
    return root


def _bridge_confirmation_path(
    workflow_root: Path,
    *,
    workflow_id: str,
    bridge_run_id: str,
) -> Path:
    directory = workflow_product._workflow_directory(workflow_root, workflow_id)
    store = BoundedCodexBridgeStore(directory / "bridge")
    _run, _control, _transactions, revisions, _current = store._paths(bridge_run_id)
    return revisions / f"r16-txn-{'0' * 32}" / "payload" / "roles" / "0" / "confirmation.json"


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class _StepClock:
    def __init__(self) -> None:
        self.current = datetime.now(UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def test_local_only_workflow_runs_resumes_and_replays_without_model_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = uuid4().hex
    workflow_root = REPOSITORY_ROOT / "tmp" / f"wf-{token[:8]}"
    storage_root = workflow_root / "s"
    storage_root.mkdir(parents=True)
    config = app_config(storage_root)
    source = river_source()
    try:
        commit_id = "1" * 40
        tree_id = "2" * 40
        monkeypatch.setattr(bridge_product, "verify_bridge_checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            bridge_product,
            "verify_bridge_module_origins",
            lambda *args, **kwargs: None,
        )
        plan, preparation = prepare_bounded_river_review_workflow(
            source,
            range_definition(source),
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id="workflow-integration",
            intake_id="intake-workflow-integration",
            source_run_id="run-workflow-integration",
            bridge_run_id="bridge-workflow-integration",
            source_id="fixture-workflow-integration",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
            repository_commit_id=commit_id,
            repository_tree_id=tree_id,
            auth_mode=RuntimeAuthModeV1.LOCAL_ONLY,
            clock=lambda: datetime(2026, 8, 9, 14, 0, tzinfo=UTC),
        )
        assert preparation.candidate is not None
        confirmed_at = datetime.now(UTC)
        confirm_bounded_river_review_workflow(
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
            authority_id="local-integration-user",
            confirmation_id="confirmation-workflow-integration",
            idempotency_key="idempotency-workflow-integration",
            expected_plan_sha256=plan.plan_sha256,
            expected_hashes=bounded_river_confirmation_hashes(preparation.candidate),
            confirmed_at=confirmed_at,
            expires_at=confirmed_at + timedelta(hours=1),
        )

        original_prepare_bridge = workflow_product.prepare_product_bridge

        def interrupt_after_source_run(**kwargs):
            raise RuntimeError("simulated interruption after source terminal run")

        monkeypatch.setattr(
            workflow_product,
            "prepare_product_bridge",
            interrupt_after_source_run,
        )
        with pytest.raises(RuntimeError, match="simulated interruption"):
            run_bounded_river_review_workflow(
                source,
                config=config,
                repository_root=REPOSITORY_ROOT,
                workflow_root=workflow_root,
                workflow_id=plan.workflow_id,
                clock=lambda: datetime(2026, 8, 9, 14, 2, tzinfo=UTC),
            )
        partial = bounded_river_review_workflow_status(
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
        )
        assert partial.state == "ready_to_resume"
        assert partial.next_action == "resume"
        assert partial.source_terminal_manifest_sha256
        assert partial.bridge_manifest_sha256 is None
        monkeypatch.setattr(
            workflow_product,
            "prepare_product_bridge",
            original_prepare_bridge,
        )
        completed = resume_bounded_river_review_workflow(
            None,
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
            clock=lambda: datetime(2026, 8, 9, 14, 3, tzinfo=UTC),
        )
        assert completed.state == "completed_local_only"
        assert completed.bridge_status == "approval_required"
        assert completed.completed_roles == ()
        assert completed.pending_roles
        assert completed.next_action == "none"
        assert not tuple(workflow_root.rglob("runtime"))

        resumed = resume_bounded_river_review_workflow(
            None,
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
        )
        replayed = replay_bounded_river_review_workflow(
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
        )
        assert resumed == completed
        assert replayed == completed

        rebound_plan, rebound_preparation = prepare_bounded_river_review_workflow(
            source,
            range_definition(source),
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id="workflow-rebound",
            intake_id="intake-workflow-integration",
            source_run_id=plan.source_run_id,
            bridge_run_id="bridge-workflow-rebound",
            source_id="fixture-workflow-integration",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
            repository_commit_id=commit_id,
            repository_tree_id=tree_id,
            auth_mode=RuntimeAuthModeV1.LOCAL_ONLY,
        )
        assert rebound_preparation.candidate is not None
        rebound_at = datetime.now(UTC)
        confirm_bounded_river_review_workflow(
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=rebound_plan.workflow_id,
            authority_id="local-integration-user",
            confirmation_id="confirmation-workflow-rebound",
            idempotency_key="idempotency-workflow-rebound",
            expected_plan_sha256=rebound_plan.plan_sha256,
            expected_hashes=bounded_river_confirmation_hashes(rebound_preparation.candidate),
            confirmed_at=rebound_at,
            expires_at=rebound_at + timedelta(hours=1),
        )
        with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_SOURCE_BINDING"):
            resume_bounded_river_review_workflow(
                None,
                config=config,
                repository_root=REPOSITORY_ROOT,
                workflow_root=workflow_root,
                workflow_id=rebound_plan.workflow_id,
            )
        rebound_directory = next(
            path.parent
            for path in workflow_root.rglob("plan.json")
            if b'"workflow_id":"workflow-rebound"' in path.read_bytes()
        )
        assert not (rebound_directory / "bridge").exists()
    finally:
        if workflow_root.exists():
            shutil.rmtree(workflow_root)


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy path budget")
@pytest.mark.parametrize(
    ("bounded_root", "allowed_length", "deepest_name"),
    [
        ("revision", 117, "terminal_tool_input"),
        ("budget", 118, "budget_lock_metadata_temp"),
    ],
)
def test_source_storage_preflight_bounds_actual_deepest_path_at_exact_limit(
    bounded_root: str,
    allowed_length: int,
    deepest_name: str,
) -> None:
    run_id = "r"
    allowed_root = _absolute_windows_root(allowed_length, bounded_root[0])
    rejected_root = _absolute_windows_root(allowed_length + 1, bounded_root[0])

    def config(root: Path) -> AppConfig:
        revision_root = root if bounded_root == "revision" else Path("C:\\product-short")
        budget_root = root if bounded_root == "budget" else Path("C:\\budget-short")
        return AppConfig(
            runs_dir=Path("C:\\legacy-short"),
            revision_runs_dir=revision_root,
            durable_budget_runs_dir=budget_root,
        )

    allowed_config = config(allowed_root)
    rejected_config = config(rejected_root)
    allowed_deepest = workflow_product._source_storage_path_budget(allowed_config, run_id)[
        deepest_name
    ]
    rejected_deepest = workflow_product._source_storage_path_budget(rejected_config, run_id)[
        deepest_name
    ]
    assert _windows_utf16_units_with_nul(allowed_deepest) == 260
    assert _windows_utf16_units_with_nul(rejected_deepest) == 261

    workflow_product._preflight_source_storage_paths(allowed_config, run_id)
    with pytest.raises(BoundedRiverReviewWorkflowError, match=r"^BRW_E_STORAGE$"):
        workflow_product._preflight_source_storage_paths(rejected_config, run_id)
    assert not allowed_root.exists()
    assert not rejected_root.exists()


def test_source_storage_preflight_includes_durable_budget_failure_record_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "river-budget-failure-preflight"
    config = app_config(tmp_path / "storage-must-not-exist")
    observed: list[Path] = []

    def refuse_after_observing(paths) -> None:  # type: ignore[no-untyped-def]
        observed.extend(paths)
        raise OSError("bounded path refusal")

    monkeypatch.setattr(workflow_product, "check_path_lengths", refuse_after_observing)

    with pytest.raises(BoundedRiverReviewWorkflowError, match=r"^BRW_E_STORAGE$"):
        workflow_product._preflight_source_storage_paths(config, run_id)

    expected_record = (
        config.resolved_storage_roots()[1]
        / ".revision-control"
        / "bounded-river-call-ev-budget-failures"
        / f"{run_lock_key_sha256(run_id)}.6.json"
    )
    assert expected_record in observed
    assert all(not root.exists() for root in config.resolved_storage_roots())


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy path budget")
def test_workflow_bridge_path_overflow_is_refused_before_root_creation() -> None:
    workflow_id = "w"
    bridge_run_id = "b"
    root_prefix = REPOSITORY_ROOT / "tmp"
    allowed_root = root_prefix / ("a" * (106 - len(str(root_prefix)) - 1))
    rejected_root = root_prefix / ("b" * (107 - len(str(root_prefix)) - 1))
    assert len(str(allowed_root)) == 106
    assert len(str(rejected_root)) == 107
    assert (
        _windows_utf16_units_with_nul(
            _bridge_confirmation_path(
                allowed_root,
                workflow_id=workflow_id,
                bridge_run_id=bridge_run_id,
            )
        )
        == 260
    )
    assert (
        _windows_utf16_units_with_nul(
            _bridge_confirmation_path(
                rejected_root,
                workflow_id=workflow_id,
                bridge_run_id=bridge_run_id,
            )
        )
        == 261
    )

    workflow_product._preflight_workflow_storage_paths(
        REPOSITORY_ROOT,
        allowed_root,
        workflow_id=workflow_id,
        bridge_run_id=bridge_run_id,
    )
    with pytest.raises(BoundedRiverReviewWorkflowError, match=r"^BRW_E_STORAGE$"):
        prepare_bounded_river_review_workflow(
            river_source(),
            range_definition(river_source()),
            repository_root=REPOSITORY_ROOT,
            workflow_root=rejected_root,
            workflow_id=workflow_id,
            intake_id="i",
            source_run_id="s",
            bridge_run_id=bridge_run_id,
            source_id="f",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
        )
    assert not allowed_root.exists()
    assert not rejected_root.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy path budget")
@pytest.mark.parametrize(
    ("entry_name", "fill"),
    [
        ("confirm_workflow", "c"),
        ("run", "d"),
        ("resume", "e"),
    ],
)
def test_existing_deep_workflow_mutators_refuse_before_partial_mutation(
    entry_name: str,
    fill: str,
) -> None:
    token = uuid4().hex[:8]
    workflow_id = "w"
    bridge_run_id = "b"
    root_prefix = REPOSITORY_ROOT / "tmp"
    shallow_root = root_prefix / f"l-{token}"
    segment_length = 107 - len(str(root_prefix)) - 1
    deep_segment = ((fill + token) * (segment_length + 1))[:segment_length]
    deep_root = root_prefix / deep_segment
    storage_root = root_prefix / f"legacy-storage-{entry_name}-{token}"
    runtime_root = root_prefix / f"legacy-runtime-{entry_name}-{token}"
    assert len(str(deep_root)) == 107
    assert (
        _windows_utf16_units_with_nul(
            _bridge_confirmation_path(
                deep_root,
                workflow_id=workflow_id,
                bridge_run_id=bridge_run_id,
            )
        )
        == 261
    )

    source = river_source()
    try:
        prepare_bounded_river_review_workflow(
            source,
            range_definition(source),
            repository_root=REPOSITORY_ROOT,
            workflow_root=shallow_root,
            workflow_id=workflow_id,
            intake_id="i",
            source_run_id="s",
            bridge_run_id=bridge_run_id,
            source_id="f",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
        )
        deep_root.mkdir(parents=True)
        source_directory = workflow_product._workflow_directory(shallow_root, workflow_id)
        target_directory = workflow_product._workflow_directory(deep_root, workflow_id)
        shutil.move(source_directory, target_directory)
        before = _file_snapshot(deep_root)
        config = app_config(storage_root)

        def invoke() -> object:
            if entry_name == "confirm_workflow":
                return confirm_bounded_river_review_workflow(
                    repository_root=REPOSITORY_ROOT,
                    workflow_root=deep_root,
                    workflow_id=workflow_id,
                    authority_id="a",
                    confirmation_id="c",
                    idempotency_key="k",
                    expected_plan_sha256="0" * 64,
                    expected_hashes=(),
                )
            if entry_name == "run":
                return run_bounded_river_review_workflow(
                    source,
                    config=config,
                    repository_root=REPOSITORY_ROOT,
                    workflow_root=deep_root,
                    workflow_id=workflow_id,
                )
            if entry_name == "resume":
                return resume_bounded_river_review_workflow(
                    None,
                    config=config,
                    repository_root=REPOSITORY_ROOT,
                    workflow_root=deep_root,
                    workflow_id=workflow_id,
                )
            return execute_bounded_river_review_role(
                config=config,
                repository_root=REPOSITORY_ROOT,
                workflow_root=deep_root,
                workflow_id=workflow_id,
                runtime_root=runtime_root,
            )

        with pytest.raises(BoundedRiverReviewWorkflowError, match=r"^BRW_E_STORAGE$"):
            invoke()

        assert _file_snapshot(deep_root) == before
        assert all(not root.exists() for root in config.resolved_storage_roots())
        assert not runtime_root.exists()
    finally:
        for path in (shallow_root, deep_root, storage_root, runtime_root):
            if path.exists():
                shutil.rmtree(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy path budget")
def test_linked_deep_workflow_resume_preserves_read_only_replay_compatibility(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = uuid4().hex[:8]
    workflow_id = "w"
    bridge_run_id = "b"
    root_prefix = REPOSITORY_ROOT / "tmp"
    shallow_root = root_prefix / f"l-{token}"
    segment_length = 107 - len(str(root_prefix)) - 1
    deep_segment = (("h" + token) * (segment_length + 1))[:segment_length]
    deep_root = root_prefix / deep_segment
    source = river_source()
    sentinel = object()
    replay_calls: list[dict[str, object]] = []

    try:
        prepare_bounded_river_review_workflow(
            source,
            range_definition(source),
            repository_root=REPOSITORY_ROOT,
            workflow_root=shallow_root,
            workflow_id=workflow_id,
            intake_id="i",
            source_run_id="s",
            bridge_run_id=bridge_run_id,
            source_id="f",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
        )
        deep_root.mkdir(parents=True)
        source_directory = workflow_product._workflow_directory(shallow_root, workflow_id)
        target_directory = workflow_product._workflow_directory(deep_root, workflow_id)
        shutil.move(source_directory, target_directory)
        (target_directory / "linkage.json").write_bytes(b"legacy-linkage-sentinel")

        def replay_only(**kwargs):  # type: ignore[no-untyped-def]
            replay_calls.append(kwargs)
            return sentinel

        monkeypatch.setattr(
            workflow_product,
            "replay_bounded_river_review_workflow",
            replay_only,
        )
        config = app_config(root_prefix / f"replay-storage-{token}")
        assert (
            resume_bounded_river_review_workflow(
                None,
                config=config,
                repository_root=REPOSITORY_ROOT,
                workflow_root=deep_root,
                workflow_id=workflow_id,
            )
            is sentinel
        )
        assert replay_calls == [
            {
                "config": config,
                "repository_root": REPOSITORY_ROOT,
                "workflow_root": deep_root,
                "workflow_id": workflow_id,
            }
        ]
        assert (target_directory / "linkage.json").read_bytes() == b"legacy-linkage-sentinel"
        assert all(not root.exists() for root in config.resolved_storage_roots())
    finally:
        for path in (shallow_root, deep_root, root_prefix / f"replay-storage-{token}"):
            if path.exists():
                shutil.rmtree(path)


@pytest.mark.skipif(os.name != "nt", reason="Windows legacy path budget")
def test_workflow_rejects_terminal_path_overflow_before_storage_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = uuid4().hex[:8]
    workflow_root = REPOSITORY_ROOT / "tmp" / f"wf-path-preflight-{token}"
    long_storage_root = (
        REPOSITORY_ROOT / "tmp" / f"storage-overflow-{token}-{'a' * 64}" / ("b" * 64)
    )
    config = app_config(long_storage_root)
    source = river_source()
    review_calls: list[object] = []

    def record_unexpected_review(*args: object, **kwargs: object) -> object:
        review_calls.append((args, kwargs))
        raise AssertionError("storage preflight must run before review execution")

    monkeypatch.setattr(
        workflow_product,
        "review_bounded_river_call_ev_intake",
        record_unexpected_review,
    )
    try:
        plan, preparation = prepare_bounded_river_review_workflow(
            source,
            range_definition(source),
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id="workflow-path-preflight",
            intake_id="intake-path-preflight",
            source_run_id="run-workflow-path-preflight",
            bridge_run_id="bridge-workflow-path-preflight",
            source_id="fixture-path-preflight",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
        )
        assert preparation.candidate is not None
        confirmed_at = datetime.now(UTC)
        confirm_bounded_river_review_workflow(
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
            authority_id="local-path-preflight-user",
            confirmation_id="confirmation-path-preflight",
            idempotency_key="idempotency-path-preflight",
            expected_plan_sha256=plan.plan_sha256,
            expected_hashes=bounded_river_confirmation_hashes(preparation.candidate),
            confirmed_at=confirmed_at,
            expires_at=confirmed_at + timedelta(hours=1),
        )
        before = {
            path.relative_to(workflow_root).as_posix(): path.read_bytes()
            for path in workflow_root.rglob("*")
            if path.is_file()
        }

        with pytest.raises(BoundedRiverReviewWorkflowError, match=r"^BRW_E_STORAGE$"):
            run_bounded_river_review_workflow(
                source,
                config=config,
                repository_root=REPOSITORY_ROOT,
                workflow_root=workflow_root,
                workflow_id=plan.workflow_id,
            )

        assert review_calls == []
        assert not long_storage_root.exists()
        assert all(not root.exists() for root in config.resolved_storage_roots())
        assert not (
            config.resolved_storage_roots()[1]
            / ".revision-control"
            / "bounded-river-call-ev-budget-failures"
        ).exists()
        assert before == {
            path.relative_to(workflow_root).as_posix(): path.read_bytes()
            for path in workflow_root.rglob("*")
            if path.is_file()
        }
    finally:
        if workflow_root.exists():
            shutil.rmtree(workflow_root)
        if long_storage_root.exists():
            shutil.rmtree(long_storage_root)


def test_supervised_workflow_completes_exactly_one_confirmed_role_per_cycle(
    monkeypatch: pytest.MonkeyPatch,
    short_storage_root: Path,
) -> None:
    token = uuid4().hex
    workflow_root = REPOSITORY_ROOT / "tmp" / f"wf-supervised-{token[:8]}"
    config = app_config(short_storage_root)
    source = river_source()
    try:
        monkeypatch.setattr(bridge_product, "verify_bridge_checkout", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            bridge_product,
            "verify_bridge_module_origins",
            lambda *args, **kwargs: None,
        )
        plan, preparation = prepare_bounded_river_review_workflow(
            source,
            range_definition(source),
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id="workflow-supervised-integration",
            intake_id="intake-supervised-integration",
            source_run_id="run-supervised-integration",
            bridge_run_id="bridge-supervised-integration",
            source_id="fixture-supervised-integration",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        )
        assert preparation.candidate is not None
        confirmed_at = datetime.now(UTC)
        confirm_bounded_river_review_workflow(
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
            authority_id="local-integration-user",
            confirmation_id="confirmation-supervised-integration",
            idempotency_key="idempotency-supervised-integration",
            expected_plan_sha256=plan.plan_sha256,
            expected_hashes=bounded_river_confirmation_hashes(preparation.candidate),
            confirmed_at=confirmed_at,
            expires_at=confirmed_at + timedelta(hours=1),
        )
        original_verified_source_read = workflow_product._verified_source_read
        source_authority = None
        source_storage_snapshot = None
        real_source_read_calls = 0
        source_storage_roots = config.resolved_storage_roots()
        source_revision_root = source_storage_roots[1]

        def stable_verified_source_read(
            requested_config,
            source_run_id,
            *,
            expected_source_sha256,
        ):
            nonlocal source_authority, source_storage_snapshot, real_source_read_calls
            assert requested_config is config
            assert requested_config.resolved_storage_roots() == source_storage_roots
            assert source_run_id == plan.source_run_id
            assert expected_source_sha256 == plan.source_sha256
            if source_authority is None:
                real_source_read_calls += 1
                source_authority = original_verified_source_read(
                    requested_config,
                    source_run_id,
                    expected_source_sha256=expected_source_sha256,
                )
                source_storage_snapshot = _file_snapshot(short_storage_root)
                return source_authority
            assert source_storage_snapshot is not None
            assert _file_snapshot(short_storage_root) == source_storage_snapshot
            source_context, verified_source = workflow_product._replay_verified_source_for_view(
                source_authority[0],
                source_revision_root=source_revision_root,
            )
            assert source_context == source_authority[1]
            assert verified_source.confirmation.confirmation_sha256 == source_authority[2]
            return source_authority

        monkeypatch.setattr(
            workflow_product,
            "_verified_source_read",
            stable_verified_source_read,
        )
        status = run_bounded_river_review_workflow(
            source,
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
        )
        assert source_authority is not None
        assert source_storage_snapshot is not None

        original_source_terminal_for_view = workflow_product._read_source_terminal_for_view
        source_view_authority = None
        real_source_view_calls = 0

        def capture_source_terminal_for_view(requested_config, source_run_id):
            nonlocal source_view_authority, real_source_view_calls
            assert requested_config is config
            assert requested_config.resolved_storage_roots() == source_storage_roots
            assert source_run_id == plan.source_run_id
            assert _file_snapshot(short_storage_root) == source_storage_snapshot
            real_source_view_calls += 1
            source_view_authority = original_source_terminal_for_view(
                requested_config,
                source_run_id,
            )
            assert _file_snapshot(short_storage_root) == source_storage_snapshot
            return source_view_authority

        monkeypatch.setattr(
            workflow_product,
            "_read_source_terminal_for_view",
            capture_source_terminal_for_view,
        )
        initial_view = bounded_river_review_report_view(
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
        )
        assert source_view_authority is not None
        assert source_view_authority[0] == source_authority[0]
        assert source_view_authority[1] == source_revision_root

        def stable_source_terminal_for_view(requested_config, source_run_id):
            assert requested_config is config
            assert requested_config.resolved_storage_roots() == source_storage_roots
            assert source_run_id == plan.source_run_id
            assert _file_snapshot(short_storage_root) == source_storage_snapshot
            return source_view_authority

        monkeypatch.setattr(
            workflow_product,
            "_read_source_terminal_for_view",
            stable_source_terminal_for_view,
        )
        workflow_directory = next(workflow_root.rglob("linkage.json")).parent
        store = BoundedCodexBridgeStore(workflow_directory / "bridge")
        clock = _StepClock()
        transport = DeterministicReadOnlyTransport(
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            clock=clock,
        )
        execution_calls: list[BridgeRole] = []

        def execute_deterministically(**kwargs):
            clock.current = max(clock.current, datetime.now(UTC))
            role = kwargs["role"]
            execution_calls.append(role)
            controller = BoundedCodexBridgeController(
                BoundedCodexBridgeStore(kwargs["bridge_root"]),
                clock=clock,
            )
            source_context = controller.read_source_context(kwargs["bridge_run_id"])
            return controller.execute_confirmed_role(
                kwargs["bridge_run_id"],
                role,
                auth_mode=kwargs["auth_mode"],
                current_source_terminal_manifest_sha256=(
                    source_context.source.source_terminal_manifest_sha256
                ),
                transport=transport,
            )

        monkeypatch.setattr(
            workflow_product,
            "execute_product_role",
            execute_deterministically,
        )

        for ordinal, expected_role in enumerate(BRIDGE_ROLE_ORDER):
            authoritative = replay_bridge(store.read_current(plan.bridge_run_id))
            assert authoritative.completed_roles == BRIDGE_ROLE_ORDER[:ordinal]
            assert authoritative.pending_roles[0] is expected_role
            assert status.next_role is authoritative.pending_roles[0]
            assert status.role_state == "awaiting_confirmation"
            assert status.next_action == "show_role_request"

            preview = bounded_river_review_role_request_preview(
                config=config,
                repository_root=REPOSITORY_ROOT,
                workflow_root=workflow_root,
                workflow_id=plan.workflow_id,
            )
            assert preview["next_role"] is authoritative.pending_roles[0]
            fields = dict(preview["confirmation_fields"])
            assert fields["expected_role"] is expected_role
            calls_before_confirmation = tuple(execution_calls)
            confirmed = confirm_bounded_river_review_role_request(
                config=config,
                repository_root=REPOSITORY_ROOT,
                workflow_root=workflow_root,
                workflow_id=plan.workflow_id,
                authority_id="local-integration-user",
                confirmation_id=f"confirmation-{expected_role.value}",
                idempotency_key=f"idempotency-{expected_role.value}",
                **fields,
            )
            assert tuple(execution_calls) == calls_before_confirmation
            assert confirmed.next_role is expected_role
            assert confirmed.role_state == "executable"
            assert confirmed.next_action == "execute_role"

            status = execute_bounded_river_review_role(
                config=config,
                repository_root=REPOSITORY_ROOT,
                workflow_root=workflow_root,
                workflow_id=plan.workflow_id,
                runtime_root=workflow_root / "runtime",
            )
            assert tuple(execution_calls) == BRIDGE_ROLE_ORDER[: ordinal + 1]
            assert status.completed_roles == BRIDGE_ROLE_ORDER[: ordinal + 1]
            assert status.pending_roles == BRIDGE_ROLE_ORDER[ordinal + 1 :]
            assert (
                replay_bounded_river_review_workflow(
                    config=config,
                    repository_root=REPOSITORY_ROOT,
                    workflow_root=workflow_root,
                    workflow_id=plan.workflow_id,
                )
                == status
            )

            if ordinal == 1:
                resumed = resume_bounded_river_review_workflow(
                    None,
                    config=config,
                    repository_root=REPOSITORY_ROOT,
                    workflow_root=workflow_root,
                    workflow_id=plan.workflow_id,
                )
                assert resumed == status
                assert tuple(execution_calls) == BRIDGE_ROLE_ORDER[:2]

        assert status.state == "completed"
        assert status.bridge_status == "succeeded"
        assert status.next_role is None
        assert status.role_state == "terminal"
        assert status.next_action == "none"
        assert tuple(execution_calls) == BRIDGE_ROLE_ORDER
        terminal_view = bounded_river_review_report_view(
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
        )
        assert terminal_view.state == "completed"
        assert terminal_view.completed_roles == BRIDGE_ROLE_ORDER
        assert terminal_view.final_report == initial_view.final_report
        assert (
            terminal_view.final_report_artifact_sha256 == initial_view.final_report_artifact_sha256
        )
        assert terminal_view.report_writer_additive_evidence
        assert real_source_read_calls == 1
        assert real_source_view_calls == 1
        assert _file_snapshot(short_storage_root) == source_storage_snapshot
    finally:
        if workflow_root.exists():
            shutil.rmtree(workflow_root)
