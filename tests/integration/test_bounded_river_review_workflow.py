from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from tests.bounded_river_call_ev_support import app_config, range_definition, river_source

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


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


def test_supervised_workflow_completes_exactly_one_confirmed_role_per_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = uuid4().hex
    workflow_root = REPOSITORY_ROOT / "tmp" / f"wf-supervised-{token[:8]}"
    storage_root = workflow_root / "s"
    storage_root.mkdir(parents=True)
    config = app_config(storage_root)
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
        status = run_bounded_river_review_workflow(
            source,
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
        )
        initial_view = bounded_river_review_report_view(
            config=config,
            repository_root=REPOSITORY_ROOT,
            workflow_root=workflow_root,
            workflow_id=plan.workflow_id,
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
    finally:
        if workflow_root.exists():
            shutil.rmtree(workflow_root)
