from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from poker_deliberation import bounded_river_review_workflow as workflow
from poker_deliberation.bounded_river_review_workflow import (
    BoundedRiverReviewWorkflowError,
    bounded_river_confirmation_hashes,
    bounded_river_review_confirmation_preview,
    bounded_river_review_workflow_status,
    confirm_bounded_river_review_workflow,
    prepare_bounded_river_review_workflow,
    run_bounded_river_review_workflow,
)
from poker_deliberation.codex_bridge.models import RuntimeAuthModeV1
from poker_deliberation.storage.revision_canonical import canonical_json_bytes
from tests.bounded_river_call_ev_support import app_config, range_definition, river_source


def _allow_test_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        workflow,
        "confined_runtime_scratch_path",
        lambda path, _repository_root: path.resolve(),
    )


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
