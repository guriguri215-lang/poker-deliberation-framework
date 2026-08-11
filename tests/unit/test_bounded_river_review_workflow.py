from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from poker_deliberation import bounded_river_review_workflow as workflow
from poker_deliberation.bounded_river_review_workflow import (
    BoundedRiverReviewWorkflowError,
    bounded_river_confirmation_hashes,
    bounded_river_review_confirmation_preview,
    bounded_river_review_linkage_sha256,
    bounded_river_review_plan_sha256,
    bounded_river_review_report_view,
    bounded_river_review_workflow_status,
    confirm_bounded_river_review_workflow,
    prepare_bounded_river_review_workflow,
    replay_bounded_river_review_workflow,
    run_bounded_river_review_workflow,
)
from poker_deliberation.codex_bridge import product as bridge_product
from poker_deliberation.codex_bridge.controller import BoundedCodexBridgeController
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BridgeConfirmationAuthorityV1,
    BridgeRole,
    BridgeRoleResultV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore
from poker_deliberation.codex_bridge.transport import DeterministicReadOnlyTransport
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes,
    parse_canonical_model,
    sha256_bytes,
)
from tests.bounded_river_call_ev_support import app_config, range_definition, river_source

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _allow_test_root(monkeypatch: pytest.MonkeyPatch) -> None:
    def allow(path: Path, _repository_root: Path) -> Path:
        return path.resolve()

    monkeypatch.setattr(workflow, "confined_runtime_scratch_path", allow)
    monkeypatch.setattr(workflow, "_confined_read_only_workflow_root", allow)


def _prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_mode: RuntimeAuthModeV1 = RuntimeAuthModeV1.LOCAL_ONLY,
    api_max_cost_micro_usd: int | None = None,
):
    _allow_test_root(monkeypatch)
    repository = tmp_path / "repository"
    repository.mkdir()
    source = river_source()
    plan, preparation = prepare_bounded_river_review_workflow(
        source,
        range_definition(source),
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id="workflow-unit",
        intake_id="intake-workflow-unit",
        source_run_id="run-workflow-unit",
        bridge_run_id="bridge-workflow-unit",
        source_id="fixture-workflow-unit",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=auth_mode,
        api_max_cost_micro_usd=api_max_cost_micro_usd,
        clock=lambda: datetime(2026, 8, 9, 13, 50, tzinfo=UTC),
    )
    return repository, source, plan, preparation


def _complete_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_mode: RuntimeAuthModeV1 = RuntimeAuthModeV1.LOCAL_ONLY,
    api_max_cost_micro_usd: int | None = None,
):
    repository, source, plan, preparation = _prepare(
        tmp_path,
        monkeypatch,
        auth_mode=auth_mode,
        api_max_cost_micro_usd=api_max_cost_micro_usd,
    )
    shutil.copytree(REPOSITORY_ROOT / ".codex" / "agents", repository / ".codex" / "agents")
    assert preparation.candidate is not None
    confirmation = confirm_bounded_river_review_workflow(
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id=plan.workflow_id,
        authority_id="local-unit-user",
        confirmation_id="confirmation-workflow-unit",
        idempotency_key="idempotency-workflow-unit",
        expected_plan_sha256=plan.plan_sha256,
        expected_hashes=bounded_river_confirmation_hashes(preparation.candidate),
    )
    monkeypatch.setattr(bridge_product, "verify_bridge_checkout", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        bridge_product,
        "verify_bridge_module_origins",
        lambda *args, **kwargs: None,
    )
    config = app_config(tmp_path / "storage")
    status = run_bounded_river_review_workflow(
        source,
        config=config,
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id=plan.workflow_id,
    )
    return repository, config, plan, confirmation, status


def _complete_local_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    repository, config, plan, confirmation, status = _complete_workflow(
        tmp_path,
        monkeypatch,
    )
    assert status.state == "completed_local_only"
    return repository, config, plan, confirmation


class _StepClock:
    def __init__(self) -> None:
        self.current = datetime.now(UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def _confirm_bridge_role(
    controller: BoundedCodexBridgeController,
    bridge_run_id: str,
    role: BridgeRole,
) -> None:
    request = controller.read_role_request(bridge_run_id, role)
    controller.confirm_role(
        bridge_run_id,
        role,
        authority=BridgeConfirmationAuthorityV1(
            authority_id="local-test-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        confirmation_id=f"confirmation-{bridge_run_id}-{role.value}",
        idempotency_key=f"idempotency-{bridge_run_id}-{role.value}",
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


def _tree_snapshot(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(item.relative_to(root).as_posix() for item in root.rglob("*") if item.is_dir())
    )
    files = {
        item.relative_to(root).as_posix(): item.read_bytes()
        for item in root.rglob("*")
        if item.is_file()
    }
    return directories, files


def _rewrite_rehashed_plan_and_linkage(
    repository: Path,
    plan_update: dict[str, object],
) -> None:
    workflow_directory = next((repository / "tmp" / "workflows").rglob("plan.json")).parent
    plan_path = workflow_directory / "plan.json"
    stored_plan = parse_canonical_model(
        plan_path.read_bytes(),
        workflow.BoundedRiverReviewWorkflowPlanV1,
    )
    provisional_plan = stored_plan.model_copy(update={**plan_update, "plan_sha256": "0" * 64})
    tampered_plan = provisional_plan.model_copy(
        update={"plan_sha256": bounded_river_review_plan_sha256(provisional_plan)}
    )
    plan_path.write_bytes(canonical_json_bytes(tampered_plan))

    linkage_path = workflow_directory / "linkage.json"
    linkage = parse_canonical_model(
        linkage_path.read_bytes(),
        workflow.BoundedRiverReviewWorkflowLinkageV1,
    )
    linkage_update: dict[str, object] = {
        "plan_sha256": tampered_plan.plan_sha256,
        "linkage_sha256": "0" * 64,
    }
    if "workflow_id" in plan_update:
        linkage_update["workflow_id"] = plan_update["workflow_id"]
    provisional_linkage = linkage.model_copy(update=linkage_update)
    tampered_linkage = provisional_linkage.model_copy(
        update={"linkage_sha256": bounded_river_review_linkage_sha256(provisional_linkage)}
    )
    linkage_path.write_bytes(canonical_json_bytes(tampered_linkage))


def test_workflow_root_authority_routes_mutations_and_pure_reads_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RootProbe(Exception):
        pass

    observed: list[tuple[bool, bool]] = []

    def probe_root(
        _repository_root: Path,
        _workflow_root: Path,
        *,
        create: bool = True,
        pure_read: bool = False,
    ) -> Path:
        observed.append((create, pure_read))
        raise RootProbe

    monkeypatch.setattr(workflow, "_workflow_root", probe_root)
    repository = tmp_path / "repository"
    repository.mkdir()
    workflow_root = repository / "tmp" / "workflows"
    source = river_source()

    with pytest.raises(RootProbe):
        prepare_bounded_river_review_workflow(
            source,
            range_definition(source),
            repository_root=repository,
            workflow_root=workflow_root,
            workflow_id="workflow-probe",
            intake_id="intake-probe",
            source_run_id="run-probe",
            bridge_run_id="bridge-probe",
            source_id="fixture-probe",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
            repository_commit_id="1" * 40,
            repository_tree_id="2" * 40,
        )
    assert observed.pop() == (True, False)

    with pytest.raises(RootProbe):
        confirm_bounded_river_review_workflow(
            repository_root=repository,
            workflow_root=workflow_root,
            workflow_id="workflow-probe",
            authority_id="authority-probe",
            confirmation_id="confirmation-probe",
            idempotency_key="idempotency-probe",
            expected_plan_sha256="0" * 64,
            expected_hashes=(),
        )
    assert observed.pop() == (False, False)

    config = app_config(tmp_path / "storage")
    with pytest.raises(RootProbe):
        run_bounded_river_review_workflow(
            source,
            config=config,
            repository_root=repository,
            workflow_root=workflow_root,
            workflow_id="workflow-probe",
        )
    assert observed.pop() == (False, False)
    with pytest.raises(RootProbe):
        workflow.resume_bounded_river_review_workflow(
            None,
            config=config,
            repository_root=repository,
            workflow_root=workflow_root,
            workflow_id="workflow-probe",
        )
    assert observed.pop() == (False, False)

    for pure_read in (
        bounded_river_review_workflow_status,
        bounded_river_review_report_view,
        replay_bounded_river_review_workflow,
    ):
        with pytest.raises(RootProbe):
            pure_read(
                config=config,
                repository_root=repository,
                workflow_root=workflow_root,
                workflow_id="workflow-probe",
            )
        assert observed.pop() == (False, True)
    assert observed == []


@pytest.mark.parametrize(
    ("auth_mode", "api_max_cost_micro_usd"),
    [
        (RuntimeAuthModeV1.LOCAL_ONLY, None),
        (RuntimeAuthModeV1.CODEX_SUBSCRIPTION, None),
        (RuntimeAuthModeV1.OPENAI_API, 1),
    ],
)
def test_prepare_binds_each_runtime_mode_without_execution_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: RuntimeAuthModeV1,
    api_max_cost_micro_usd: int | None,
) -> None:
    repository, _source, plan, _preparation = _prepare(
        tmp_path,
        monkeypatch,
        auth_mode=auth_mode,
        api_max_cost_micro_usd=api_max_cost_micro_usd,
    )

    assert plan.auth_mode is auth_mode
    assert plan.api_max_cost_micro_usd == api_max_cost_micro_usd
    assert not tuple((repository / "tmp" / "workflows").rglob("bridge"))
    assert not tuple((repository / "tmp" / "workflows").rglob("runtime"))


def test_prepare_persists_only_canonical_projection_and_exact_confirmation_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, source, plan, preparation = _prepare(tmp_path, monkeypatch)

    preview = bounded_river_review_confirmation_preview(plan, preparation)
    assert preview["state"] == "awaiting_confirmation"
    assert tuple(preview["expected_hashes"].values()) == bounded_river_confirmation_hashes(
        preparation.candidate  # type: ignore[arg-type]
    )
    workflow_files = tuple((repository / "tmp" / "workflows").rglob("*.json"))
    assert {item.name for item in workflow_files} == {"plan.json", "preparation.json"}
    assert all(source not in item.read_bytes() for item in workflow_files)

    status = bounded_river_review_workflow_status(
        config=app_config(tmp_path / "storage"),
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id="workflow-unit",
    )
    assert status.state == "awaiting_confirmation"
    assert status.next_action == "confirm"


def test_confirmation_preserves_all_p3_030c_hash_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _source, plan, preparation = _prepare(tmp_path, monkeypatch)
    assert preparation.candidate is not None

    confirmation = confirm_bounded_river_review_workflow(
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id="workflow-unit",
        authority_id="local-unit-user",
        confirmation_id="confirmation-workflow-unit",
        idempotency_key="idempotency-workflow-unit",
        expected_plan_sha256=plan.plan_sha256,
        expected_hashes=bounded_river_confirmation_hashes(preparation.candidate),
        confirmed_at=datetime(2026, 8, 9, 13, 51, tzinfo=UTC),
        expires_at=datetime(2026, 8, 10, 13, 51, tzinfo=UTC),
    )
    assert confirmation.run_id == plan.source_run_id
    assert confirmation.confirmation_sha256

    status = bounded_river_review_workflow_status(
        config=app_config(tmp_path / "storage"),
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id="workflow-unit",
    )
    assert status.state == "ready_to_run"
    assert status.next_action == "run"
    assert status.confirmation_sha256 == confirmation.confirmation_sha256


def test_confirmation_rejects_plan_or_candidate_mismatch_without_echoing_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _source, plan, preparation = _prepare(tmp_path, monkeypatch)
    assert preparation.candidate is not None
    hashes = list(bounded_river_confirmation_hashes(preparation.candidate))
    sensitive = "private-user-marker"
    hashes[-1] = "0" * 64

    with pytest.raises(BoundedRiverReviewWorkflowError) as exc_info:
        confirm_bounded_river_review_workflow(
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id="workflow-unit",
            authority_id=sensitive,
            confirmation_id="confirmation-workflow-unit",
            idempotency_key="idempotency-workflow-unit",
            expected_plan_sha256=plan.plan_sha256,
            expected_hashes=tuple(hashes),
        )

    assert str(exc_info.value) == "BRW_E_CONFIRMATION_BINDING"
    assert sensitive not in str(exc_info.value)
    assert not tuple((repository / "tmp" / "workflows").rglob("confirmation.json"))


def test_run_rejects_changed_source_before_product_or_bridge_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, source, plan, preparation = _prepare(tmp_path, monkeypatch)
    assert preparation.candidate is not None
    confirm_bounded_river_review_workflow(
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id="workflow-unit",
        authority_id="local-unit-user",
        confirmation_id="confirmation-workflow-unit",
        idempotency_key="idempotency-workflow-unit",
        expected_plan_sha256=plan.plan_sha256,
        expected_hashes=bounded_river_confirmation_hashes(preparation.candidate),
    )
    product_called = False

    def unexpected_product_call(*args, **kwargs):
        nonlocal product_called
        product_called = True
        raise AssertionError("product execution must not start")

    monkeypatch.setattr(workflow, "review_bounded_river_call_ev_intake", unexpected_product_call)
    monkeypatch.setattr(workflow, "prepare_product_bridge", unexpected_product_call)
    with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_SOURCE_BINDING"):
        run_bounded_river_review_workflow(
            source + b"\n",
            config=app_config(tmp_path / "storage"),
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id="workflow-unit",
        )
    assert product_called is False


def test_status_rejects_tampered_confirmation_without_echoing_private_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _source, plan, preparation = _prepare(tmp_path, monkeypatch)
    assert preparation.candidate is not None
    confirmation = confirm_bounded_river_review_workflow(
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id="workflow-unit",
        authority_id="private-user-marker",
        confirmation_id="confirmation-workflow-unit",
        idempotency_key="idempotency-workflow-unit",
        expected_plan_sha256=plan.plan_sha256,
        expected_hashes=bounded_river_confirmation_hashes(preparation.candidate),
    )
    confirmation_path = next((repository / "tmp" / "workflows").rglob("confirmation.json"))
    confirmation_path.write_bytes(
        canonical_json_bytes(confirmation.model_copy(update={"confirmation_sha256": "0" * 64}))
    )

    with pytest.raises(BoundedRiverReviewWorkflowError) as exc_info:
        bounded_river_review_workflow_status(
            config=app_config(tmp_path / "storage"),
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id="workflow-unit",
        )

    assert str(exc_info.value) == "BRW_E_CONFIRMATION_BINDING"
    assert "private-user-marker" not in str(exc_info.value)


def _assert_pure_read_storage_failure(repository: Path, tmp_path: Path) -> None:
    for reader in (bounded_river_review_workflow_status, bounded_river_review_report_view):
        with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_STORAGE"):
            reader(
                config=app_config(tmp_path / "storage"),
                repository_root=repository,
                workflow_root=repository / "tmp" / "workflows",
                workflow_id="workflow-unit",
            )


def test_pure_read_rejects_hardlinked_workflow_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _source, _plan, _preparation = _prepare(tmp_path, monkeypatch)
    plan_path = next((repository / "tmp" / "workflows").rglob("plan.json"))
    alias = tmp_path / "plan-hardlink.json"
    try:
        alias.hardlink_to(plan_path)
    except OSError:
        pytest.skip("hardlink creation is not available")

    _assert_pure_read_storage_failure(repository, tmp_path)


def test_pure_read_rejects_symlinked_workflow_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _source, _plan, _preparation = _prepare(tmp_path, monkeypatch)
    plan_path = next((repository / "tmp" / "workflows").rglob("plan.json"))
    protected = repository / "user_materials"
    protected.mkdir()
    protected_plan = protected / "plan.json"
    plan_path.replace(protected_plan)
    try:
        plan_path.symlink_to(protected_plan)
    except OSError:
        protected_plan.replace(plan_path)
        pytest.skip("file symlink creation is not available")
    try:
        _assert_pure_read_storage_failure(repository, tmp_path)
    finally:
        plan_path.unlink()
        protected_plan.replace(plan_path)


def test_pure_read_rejects_symlinked_workflow_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _source, _plan, _preparation = _prepare(tmp_path, monkeypatch)
    workflow_root = repository / "tmp" / "workflows"
    workflow_directory = next(item for item in workflow_root.iterdir() if item.is_dir())
    outside = tmp_path / "outside-workflow"
    workflow_directory.replace(outside)
    try:
        workflow_directory.symlink_to(outside, target_is_directory=True)
    except OSError:
        outside.replace(workflow_directory)
        pytest.skip("directory symlink creation is not available")
    try:
        _assert_pure_read_storage_failure(repository, tmp_path)
    finally:
        workflow_directory.unlink()
        outside.replace(workflow_directory)


def test_report_view_replays_local_only_terminal_without_runtime_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, config, plan, confirmation = _complete_local_only(tmp_path, monkeypatch)
    observed_root = tmp_path
    before = _tree_snapshot(observed_root)

    def unexpected_runtime(*args, **kwargs):
        raise AssertionError("report view must not invoke a product or nonlocal runtime")

    def unexpected_write(*args, **kwargs):
        raise AssertionError("report view must not invoke a filesystem write primitive")

    monkeypatch.setattr(workflow, "Orchestrator", unexpected_runtime)
    monkeypatch.setattr(workflow, "LocalProvider", unexpected_runtime)
    monkeypatch.setattr(workflow, "prepare_product_bridge", unexpected_runtime)
    monkeypatch.setattr(workflow, "review_bounded_river_call_ev_intake", unexpected_runtime)
    monkeypatch.setattr(bridge_product.tempfile, "TemporaryDirectory", unexpected_write)
    monkeypatch.setattr(Path, "mkdir", unexpected_write)
    monkeypatch.setattr(Path, "write_bytes", unexpected_write)

    view = bounded_river_review_report_view(
        config=config,
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id=plan.workflow_id,
    )

    assert view.state == "completed_local_only"
    assert view.bridge_mode is RuntimeAuthModeV1.LOCAL_ONLY
    assert view.completed_roles == ()
    assert view.source_run_id == plan.source_run_id
    assert view.bridge_run_id == plan.bridge_run_id
    assert view.plan_sha256 == plan.plan_sha256
    assert view.confirmation_sha256 == confirmation.confirmation_sha256
    assert view.final_report.run_id == plan.source_run_id
    assert view.final_report_artifact_sha256 == sha256_bytes(
        canonical_json_bytes(view.final_report)
    )
    assert view.report_writer_additive_evidence == ()
    assert _tree_snapshot(observed_root) == before

    absent_legacy_root = tmp_path / "absent-legacy-root"
    alternate_config = config.model_copy(update={"runs_dir": absent_legacy_root})
    with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_SOURCE_RUN"):
        bounded_river_review_report_view(
            config=alternate_config,
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id=plan.workflow_id,
        )
    assert not absent_legacy_root.exists()


def test_report_view_missing_workflow_does_not_create_any_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _allow_test_root(monkeypatch)
    repository = tmp_path / "repository"
    repository.mkdir()
    workflow_root = repository / "tmp" / "missing-workflows"
    config = app_config(tmp_path / "absent-storage")
    storage_roots = config.resolved_storage_roots()

    with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_STORAGE"):
        bounded_river_review_report_view(
            config=config,
            repository_root=repository,
            workflow_root=workflow_root,
            workflow_id="missing-workflow",
        )

    assert not workflow_root.exists()
    assert all(not root.exists() for root in storage_roots)


def test_report_view_fails_closed_when_linkage_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, config, plan, _confirmation = _complete_local_only(tmp_path, monkeypatch)
    linkage_path = next((repository / "tmp" / "workflows").rglob("linkage.json"))
    linkage_path.unlink()

    with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_LINKAGE"):
        bounded_river_review_report_view(
            config=config,
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id=plan.workflow_id,
        )


def test_replay_rejects_rehashed_linkage_identity_cross_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, config, plan, _confirmation = _complete_local_only(tmp_path, monkeypatch)
    linkage_path = next((repository / "tmp" / "workflows").rglob("linkage.json"))
    linkage = parse_canonical_model(
        linkage_path.read_bytes(),
        workflow.BoundedRiverReviewWorkflowLinkageV1,
    )
    for linkage_update in (
        {"source_run_id": "other-source-run"},
        {"bridge_run_id": "other-bridge-run"},
        {"auth_mode": RuntimeAuthModeV1.CODEX_SUBSCRIPTION},
    ):
        provisional = linkage.model_copy(update={**linkage_update, "linkage_sha256": "0" * 64})
        tampered = provisional.model_copy(
            update={"linkage_sha256": bounded_river_review_linkage_sha256(provisional)}
        )
        linkage_path.write_bytes(canonical_json_bytes(tampered))

        with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_LINKAGE"):
            bounded_river_review_workflow_status(
                config=config,
                repository_root=repository,
                workflow_root=repository / "tmp" / "workflows",
                workflow_id=plan.workflow_id,
            )
        with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_LINKAGE"):
            bounded_river_review_report_view(
                config=config,
                repository_root=repository,
                workflow_root=repository / "tmp" / "workflows",
                workflow_id=plan.workflow_id,
            )


def test_report_view_accepts_linked_ancestor_after_real_five_role_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, config, plan, _confirmation, initial_status = _complete_workflow(
        tmp_path,
        monkeypatch,
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
    )
    assert initial_status.state == "awaiting_role_review"
    initial_view = bounded_river_review_report_view(
        config=config,
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id=plan.workflow_id,
    )
    workflow_directory = next((repository / "tmp" / "workflows").rglob("linkage.json")).parent
    linkage = parse_canonical_model(
        (workflow_directory / "linkage.json").read_bytes(),
        workflow.BoundedRiverReviewWorkflowLinkageV1,
    )
    store = BoundedCodexBridgeStore(workflow_directory / "bridge")
    clock = _StepClock()
    controller = BoundedCodexBridgeController(store, clock=clock)
    transport = DeterministicReadOnlyTransport(
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        clock=clock,
    )

    for role in BRIDGE_ROLE_ORDER:
        _confirm_bridge_role(controller, plan.bridge_run_id, role)
        controller.execute_confirmed_role(
            plan.bridge_run_id,
            role,
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            current_source_terminal_manifest_sha256=(initial_view.source_terminal_manifest_sha256),
            transport=transport,
        )

    current = store.read_current(plan.bridge_run_id)
    assert current.manifest.manifest_sha256 != linkage.bridge_manifest_sha256
    assert current.manifest.inventory_sha256 != linkage.bridge_inventory_sha256
    for status in (
        bounded_river_review_workflow_status(
            config=config,
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id=plan.workflow_id,
        ),
        replay_bounded_river_review_workflow(
            config=config,
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id=plan.workflow_id,
        ),
    ):
        assert status.state == "completed"
        assert status.completed_roles == BRIDGE_ROLE_ORDER

    view = bounded_river_review_report_view(
        config=config,
        repository_root=repository,
        workflow_root=repository / "tmp" / "workflows",
        workflow_id=plan.workflow_id,
    )
    assert view.state == "completed"
    assert view.completed_roles == BRIDGE_ROLE_ORDER
    assert view.bridge_manifest_sha256 == current.manifest.manifest_sha256
    assert view.bridge_inventory_sha256 == current.manifest.inventory_sha256
    assert view.linkage_sha256 == linkage.linkage_sha256
    assert view.final_report == initial_view.final_report
    assert view.final_report_artifact_sha256 == initial_view.final_report_artifact_sha256
    assert view.report_writer_additive_evidence

    results = [
        item.model
        for item in current.decoded_artifacts()
        if isinstance(item.model, BridgeRoleResultV1)
    ]
    writer = next(item for item in results if item.output.role is BridgeRole.REPORT_WRITER)
    projected_bytes = canonical_json_bytes(view)
    assert all(
        claim.narrative.encode("utf-8") not in projected_bytes
        for claim in (*writer.output.conclusions, *writer.output.uncertainties)
    )


@pytest.mark.parametrize(
    ("plan_update", "error_code"),
    [
        ({"workflow_id": "other-workflow"}, "BRW_E_PLAN_BINDING"),
        ({"source_sha256": "f" * 64}, "BRW_E_SOURCE_BINDING"),
        ({"repository_commit_id": "3" * 40}, "BRW_E_BRIDGE_BINDING"),
        ({"repository_tree_id": "4" * 40}, "BRW_E_BRIDGE_BINDING"),
    ],
)
def test_report_view_rejects_rehashed_plan_semantic_binding_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plan_update: dict[str, object],
    error_code: str,
) -> None:
    repository, config, plan, _confirmation = _complete_local_only(tmp_path, monkeypatch)
    _rewrite_rehashed_plan_and_linkage(repository, plan_update)

    with pytest.raises(BoundedRiverReviewWorkflowError, match=error_code):
        bounded_river_review_report_view(
            config=config,
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id=plan.workflow_id,
        )


def test_report_view_binds_openai_per_role_budget_to_bridge_total(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, config, plan, _confirmation, status = _complete_workflow(
        tmp_path,
        monkeypatch,
        auth_mode=RuntimeAuthModeV1.OPENAI_API,
        api_max_cost_micro_usd=11,
    )
    assert status.state == "awaiting_role_review"
    assert (
        bounded_river_review_report_view(
            config=config,
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id=plan.workflow_id,
        ).bridge_mode
        is RuntimeAuthModeV1.OPENAI_API
    )

    _rewrite_rehashed_plan_and_linkage(repository, {"api_max_cost_micro_usd": 12})
    with pytest.raises(BoundedRiverReviewWorkflowError, match="BRW_E_BRIDGE_BINDING"):
        bounded_river_review_report_view(
            config=config,
            repository_root=repository,
            workflow_root=repository / "tmp" / "workflows",
            workflow_id=plan.workflow_id,
        )
