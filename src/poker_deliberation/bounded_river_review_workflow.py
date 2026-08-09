"""Resumable product composition for one bounded Japanese river review."""

from __future__ import annotations

import hashlib
import os
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import BaseModel

from poker_deliberation.bounded_river_call_ev import (
    admit_bounded_river_call_ev_review,
    bounded_river_authority_sha256,
    bounded_river_confirmation_sha256,
    create_bounded_river_call_ev_authority,
    create_bounded_river_call_ev_confirmation,
    prepare_bounded_river_call_ev_intake,
    review_bounded_river_call_ev_intake,
)
from poker_deliberation.bounded_river_call_ev_models import (
    MAX_BOUNDED_RIVER_CALL_EV_ARTIFACT_BYTES,
    BoundedRiverCallEvCandidateV1,
    BoundedRiverCallEvConfirmationV1,
    BoundedRiverCallEvPreparationResultV1,
)
from poker_deliberation.bounded_river_review_workflow_models import (
    BOUNDED_RIVER_REVIEW_WORKFLOW_MAX_ARTIFACT_BYTES,
    BoundedRiverReviewWorkflowLinkageV1,
    BoundedRiverReviewWorkflowPlanV1,
    BoundedRiverReviewWorkflowStatusV1,
    WorkflowNextAction,
    WorkflowState,
)
from poker_deliberation.codex_bridge.controller import BoundedCodexBridgeController
from poker_deliberation.codex_bridge.models import BridgeSourceContextV1, RuntimeAuthModeV1
from poker_deliberation.codex_bridge.product import (
    confined_runtime_scratch_path,
    prepare_product_bridge,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.source import project_verified_p3_terminal
from poker_deliberation.codex_bridge.source_reader import (
    P3TerminalSourceReadError,
    read_verified_p3_terminal_source,
)
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore, VerifiedBridgeRead
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.storage.revision_canonical import (
    canonical_domain_sha256,
    canonical_json_bytes,
    parse_canonical_model,
    sha256_bytes,
    validate_run_id,
)
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    VerifiedRunReadV2,
)

_PLAN_HASH_DOMAIN = "poker-bounded-river-review-workflow-plan-v1"
_LINKAGE_HASH_DOMAIN = "poker-bounded-river-review-workflow-linkage-v1"
_WORKFLOW_DIRECTORY_DOMAIN = b"poker-bounded-river-review-workflow-directory-v1\0"
Clock = Callable[[], datetime]


class BoundedRiverReviewWorkflowError(ValueError):
    """Stable workflow error that never includes source or credential values."""


def _fail(code: str) -> NoReturn:
    raise BoundedRiverReviewWorkflowError(code)


def _now() -> datetime:
    return datetime.now(UTC)


def _without_hash(model: BaseModel, field: str) -> dict[str, Any]:
    value = model.model_dump(mode="json")
    value.pop(field)
    return value


def bounded_river_review_plan_sha256(plan: BoundedRiverReviewWorkflowPlanV1) -> str:
    return canonical_domain_sha256(_PLAN_HASH_DOMAIN, _without_hash(plan, "plan_sha256"))


def bounded_river_review_linkage_sha256(
    linkage: BoundedRiverReviewWorkflowLinkageV1,
) -> str:
    return canonical_domain_sha256(
        _LINKAGE_HASH_DOMAIN,
        _without_hash(linkage, "linkage_sha256"),
    )


def bounded_river_confirmation_hashes(
    candidate: BoundedRiverCallEvCandidateV1,
) -> tuple[str, ...]:
    projection = candidate.projection
    return (
        projection.source_sha256,
        projection.bounded_candidate_sha256,
        projection.source_bindings_sha256,
        projection.focal_sha256,
        projection.extractor_sha256,
        projection.tool_plan_sha256,
        projection.range_definition_sha256,
        projection.range_target_sha256,
        projection.range_binding_sha256,
        projection.equity_model_sha256,
        projection.call_ev_model_sha256,
        candidate.candidate_sha256,
    )


def _workflow_root(repository_root: Path, workflow_root: Path) -> Path:
    root = confined_runtime_scratch_path(workflow_root, repository_root)
    root.mkdir(parents=True, exist_ok=True)
    if confined_runtime_scratch_path(workflow_root, repository_root) != root:
        _fail("BRW_E_STORAGE")
    return root


def _workflow_directory(root: Path, workflow_id: str) -> Path:
    try:
        validate_run_id(workflow_id)
    except ValueError:
        _fail("BRW_E_WORKFLOW_ID")
    digest = hashlib.sha256(_WORKFLOW_DIRECTORY_DOMAIN + workflow_id.encode("utf-8")).hexdigest()[
        :32
    ]
    return root / digest


def _write_new(path: Path, value: BaseModel) -> bytes:
    data = canonical_json_bytes(value)
    if not data or len(data) > BOUNDED_RIVER_REVIEW_WORKFLOW_MAX_ARTIFACT_BYTES:
        _fail("BRW_E_STORAGE")
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != data:
            _fail("BRW_E_STORAGE")
    except OSError as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc
    return data


def _read_model(path: Path, model: type[BaseModel]) -> BaseModel:
    try:
        size = path.stat().st_size
        if size < 1 or size > BOUNDED_RIVER_REVIEW_WORKFLOW_MAX_ARTIFACT_BYTES:
            _fail("BRW_E_STORAGE")
        data = path.read_bytes()
        if len(data) != size:
            _fail("BRW_E_STORAGE")
        return parse_canonical_model(data, model)
    except (OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc


def _read_plan(directory: Path) -> BoundedRiverReviewWorkflowPlanV1:
    plan = _read_model(directory / "plan.json", BoundedRiverReviewWorkflowPlanV1)
    assert isinstance(plan, BoundedRiverReviewWorkflowPlanV1)
    if plan.plan_sha256 != bounded_river_review_plan_sha256(plan):
        _fail("BRW_E_PLAN_BINDING")
    return plan


def _read_preparation(directory: Path) -> BoundedRiverCallEvPreparationResultV1:
    preparation = _read_model(
        directory / "preparation.json",
        BoundedRiverCallEvPreparationResultV1,
    )
    assert isinstance(preparation, BoundedRiverCallEvPreparationResultV1)
    if preparation.status != "ready" or preparation.candidate is None:
        _fail("BRW_E_PREPARATION")
    return preparation


def _read_confirmation(directory: Path) -> BoundedRiverCallEvConfirmationV1:
    confirmation = _read_model(
        directory / "confirmation.json",
        BoundedRiverCallEvConfirmationV1,
    )
    assert isinstance(confirmation, BoundedRiverCallEvConfirmationV1)
    return confirmation


def _verify_confirmation_binding(
    plan: BoundedRiverReviewWorkflowPlanV1,
    preparation: BoundedRiverCallEvPreparationResultV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
) -> None:
    candidate = preparation.candidate
    if candidate is None:  # pragma: no cover - checked by the preparation reader
        _fail("BRW_E_PREPARATION")
    confirmation_hashes = (
        confirmation.source_sha256,
        confirmation.bounded_candidate_sha256,
        confirmation.source_bindings_sha256,
        confirmation.focal_sha256,
        confirmation.extractor_sha256,
        confirmation.tool_plan_sha256,
        confirmation.range_definition_sha256,
        confirmation.range_target_sha256,
        confirmation.range_binding_sha256,
        confirmation.equity_model_sha256,
        confirmation.call_ev_model_sha256,
        confirmation.candidate_sha256,
    )
    if (
        confirmation.confirmation_sha256 != bounded_river_confirmation_sha256(confirmation)
        or confirmation.authority_snapshot_sha256
        != bounded_river_authority_sha256(confirmation.authority)
        or confirmation_hashes != bounded_river_confirmation_hashes(candidate)
        or confirmation.run_id != plan.source_run_id
        or confirmation.intake_id != plan.intake_id
    ):
        _fail("BRW_E_CONFIRMATION_BINDING")


def _read_linkage(directory: Path) -> BoundedRiverReviewWorkflowLinkageV1:
    linkage = _read_model(directory / "linkage.json", BoundedRiverReviewWorkflowLinkageV1)
    assert isinstance(linkage, BoundedRiverReviewWorkflowLinkageV1)
    if linkage.linkage_sha256 != bounded_river_review_linkage_sha256(linkage):
        _fail("BRW_E_LINKAGE")
    return linkage


def prepare_bounded_river_review_workflow(
    source_bytes: bytes,
    range_definition: VersionedRangeDefinitionV1,
    *,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    intake_id: str,
    source_run_id: str,
    bridge_run_id: str,
    source_id: str,
    source_kind: Literal["user_supplied", "repository_fixture"],
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ],
    usage_classification: Literal["local_analysis_only", "redistribution_allowed"],
    classification: Literal["internal", "public"],
    repository_commit_id: str,
    repository_tree_id: str,
    auth_mode: RuntimeAuthModeV1 = RuntimeAuthModeV1.LOCAL_ONLY,
    api_max_cost_micro_usd: int | None = None,
    clock: Clock = _now,
) -> tuple[BoundedRiverReviewWorkflowPlanV1, BoundedRiverCallEvPreparationResultV1]:
    """Prepare immutable P3-030C material without creating a product run."""

    root = _workflow_root(repository_root, workflow_root)
    directory = _workflow_directory(root, workflow_id)
    if directory.exists():
        _fail("BRW_E_WORKFLOW_EXISTS")
    preparation = prepare_bounded_river_call_ev_intake(
        source_bytes,
        range_definition,
        intake_id=intake_id,
        source_id=source_id,
        source_kind=source_kind,
        license_classification=license_classification,
        usage_classification=usage_classification,
        classification=classification,
    )
    if preparation.status != "ready" or preparation.candidate is None:
        _fail("BRW_E_PREPARATION")
    preparation_bytes = canonical_json_bytes(preparation)
    if len(preparation_bytes) > MAX_BOUNDED_RIVER_CALL_EV_ARTIFACT_BYTES:
        _fail("BRW_E_STORAGE")
    provisional = BoundedRiverReviewWorkflowPlanV1(
        workflow_id=workflow_id,
        intake_id=intake_id,
        source_run_id=source_run_id,
        bridge_run_id=bridge_run_id,
        auth_mode=auth_mode,
        api_max_cost_micro_usd=api_max_cost_micro_usd,
        repository_commit_id=repository_commit_id,
        repository_tree_id=repository_tree_id,
        source_sha256=hashlib.sha256(source_bytes).hexdigest(),
        preparation_sha256=sha256_bytes(preparation_bytes),
        created_at=clock(),
        plan_sha256="0" * 64,
    )
    plan = provisional.model_copy(
        update={"plan_sha256": bounded_river_review_plan_sha256(provisional)}
    )
    temporary = root / f".{directory.name}.{secrets.token_hex(8)}.tmp"
    try:
        temporary.mkdir()
        _write_new(temporary / "plan.json", plan)
        _write_new(temporary / "preparation.json", preparation)
        os.replace(temporary, directory)
    except OSError as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc
    read_plan = _read_plan(directory)
    read_preparation = _read_preparation(directory)
    if read_plan != plan or read_preparation != preparation:
        _fail("BRW_E_STORAGE")
    return plan, preparation


def bounded_river_review_confirmation_preview(
    plan: BoundedRiverReviewWorkflowPlanV1,
    preparation: BoundedRiverCallEvPreparationResultV1,
) -> dict[str, object]:
    if preparation.status != "ready" or preparation.candidate is None:
        _fail("BRW_E_PREPARATION")
    names = (
        "source_sha256",
        "bounded_candidate_sha256",
        "source_bindings_sha256",
        "focal_sha256",
        "extractor_sha256",
        "tool_plan_sha256",
        "range_definition_sha256",
        "range_target_sha256",
        "range_binding_sha256",
        "equity_model_sha256",
        "call_ev_model_sha256",
        "candidate_sha256",
    )
    return {
        "schema_version": "1.0.0",
        "workflow_id": plan.workflow_id,
        "state": "awaiting_confirmation",
        "auth_mode": plan.auth_mode,
        "plan_sha256": plan.plan_sha256,
        "source_run_id": plan.source_run_id,
        "bridge_run_id": plan.bridge_run_id,
        "expected_hashes": dict(
            zip(names, bounded_river_confirmation_hashes(preparation.candidate), strict=True)
        ),
    }


def confirm_bounded_river_review_workflow(
    *,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    authority_id: str,
    confirmation_id: str,
    idempotency_key: str,
    expected_plan_sha256: str,
    expected_hashes: tuple[str, ...],
    confirmed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> BoundedRiverCallEvConfirmationV1:
    root = _workflow_root(repository_root, workflow_root)
    directory = _workflow_directory(root, workflow_id)
    plan = _read_plan(directory)
    preparation = _read_preparation(directory)
    if expected_plan_sha256 != plan.plan_sha256:
        _fail("BRW_E_PLAN_BINDING")
    if preparation.candidate is None:  # pragma: no cover - checked by reader
        _fail("BRW_E_PREPARATION")
    if expected_hashes != bounded_river_confirmation_hashes(preparation.candidate):
        _fail("BRW_E_CONFIRMATION_BINDING")
    confirmation_path = directory / "confirmation.json"
    if confirmation_path.exists():
        _fail("BRW_E_ALREADY_CONFIRMED")
    confirmation = create_bounded_river_call_ev_confirmation(
        preparation.candidate,
        run_id=plan.source_run_id,
        confirmation_id=confirmation_id,
        idempotency_key=idempotency_key,
        authority=create_bounded_river_call_ev_authority(
            authority_id=authority_id,
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        expected_hashes=expected_hashes,
        confirmed_at=confirmed_at,
        expires_at=expires_at,
    )
    _write_new(confirmation_path, confirmation)
    read_confirmation = _read_confirmation(directory)
    if read_confirmation != confirmation:
        _fail("BRW_E_STORAGE")
    _verify_confirmation_binding(plan, preparation, read_confirmation)
    return confirmation


def _verified_source_read(
    config: AppConfig,
    source_run_id: str,
) -> tuple[VerifiedRunReadV2, BridgeSourceContextV1, str]:
    orchestrator = Orchestrator(config=config, provider=LocalProvider())
    read = orchestrator.product_store.read_current(source_run_id)
    try:
        verified = read_verified_p3_terminal_source(
            read,
            source_revision_root=orchestrator.product_store.revision_root,
        )
    except P3TerminalSourceReadError as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_SOURCE_RUN") from exc
    source = project_verified_p3_terminal(
        read,
        source_revision_root=orchestrator.product_store.revision_root,
    )
    return read, source, verified.confirmation.confirmation_sha256


def _bridge_read(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
) -> VerifiedBridgeRead | None:
    bridge_root = directory / "bridge"
    if not bridge_root.exists():
        return None
    return BoundedCodexBridgeStore(bridge_root).read_current(plan.bridge_run_id)


def _verify_bridge_source(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    expected_source: object,
) -> VerifiedBridgeRead:
    bridge = _bridge_read(directory, plan)
    if bridge is None:
        _fail("BRW_E_BRIDGE")
    stored_source = BoundedCodexBridgeController(
        BoundedCodexBridgeStore(directory / "bridge")
    ).read_source_context(plan.bridge_run_id)
    if stored_source != expected_source or bridge.pointer.auth_mode is not plan.auth_mode:
        _fail("BRW_E_BRIDGE_BINDING")
    return bridge


def _link(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
    source_read: Any,
    bridge_read: VerifiedBridgeRead,
    *,
    linked_at: datetime,
) -> BoundedRiverReviewWorkflowLinkageV1:
    path = directory / "linkage.json"
    if path.exists():
        existing = _read_linkage(directory)
        expected = (
            plan.workflow_id,
            plan.plan_sha256,
            confirmation.confirmation_sha256,
            plan.source_run_id,
            source_read.manifest_sha256,
            source_read.manifest.inventory_sha256,
            plan.bridge_run_id,
            plan.auth_mode,
            bridge_read.manifest.manifest_sha256,
            bridge_read.manifest.inventory_sha256,
        )
        actual = (
            existing.workflow_id,
            existing.plan_sha256,
            existing.confirmation_sha256,
            existing.source_run_id,
            existing.source_terminal_manifest_sha256,
            existing.source_terminal_inventory_sha256,
            existing.bridge_run_id,
            existing.auth_mode,
            existing.bridge_manifest_sha256,
            existing.bridge_inventory_sha256,
        )
        if actual != expected:
            _fail("BRW_E_LINKAGE")
        return existing
    provisional = BoundedRiverReviewWorkflowLinkageV1(
        workflow_id=plan.workflow_id,
        plan_sha256=plan.plan_sha256,
        confirmation_sha256=confirmation.confirmation_sha256,
        source_run_id=plan.source_run_id,
        source_terminal_manifest_sha256=source_read.manifest_sha256,
        source_terminal_inventory_sha256=source_read.manifest.inventory_sha256,
        bridge_run_id=plan.bridge_run_id,
        auth_mode=plan.auth_mode,
        bridge_manifest_sha256=bridge_read.manifest.manifest_sha256,
        bridge_inventory_sha256=bridge_read.manifest.inventory_sha256,
        linked_at=linked_at,
        linkage_sha256="0" * 64,
    )
    linkage = provisional.model_copy(
        update={"linkage_sha256": bounded_river_review_linkage_sha256(provisional)}
    )
    _write_new(path, linkage)
    return linkage


def _complete_from_verified_source(
    *,
    config: AppConfig,
    repository_root: Path,
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
    source_read: VerifiedRunReadV2,
    source_context: BridgeSourceContextV1,
    source_confirmation_sha256: str,
    clock: Clock,
) -> BoundedRiverReviewWorkflowStatusV1:
    if (
        source_context.source.source_terminal_run_id != plan.source_run_id
        or source_context.source.source_candidate_sha256 != confirmation.candidate_sha256
        or source_confirmation_sha256 != confirmation.confirmation_sha256
    ):
        _fail("BRW_E_SOURCE_BINDING")
    bridge = _bridge_read(directory, plan)
    if bridge is None:
        bridge = prepare_product_bridge(
            config=config,
            repository_root=repository_root,
            bridge_root=directory / "bridge",
            source_run_id=plan.source_run_id,
            bridge_run_id=plan.bridge_run_id,
            repository_commit_id=plan.repository_commit_id,
            repository_tree_id=plan.repository_tree_id,
            auth_mode=plan.auth_mode,
            api_max_cost_micro_usd=plan.api_max_cost_micro_usd,
        )
    bridge = _verify_bridge_source(directory, plan, source_context)
    _link(
        directory,
        plan,
        confirmation,
        source_read,
        bridge,
        linked_at=clock(),
    )
    return bounded_river_review_workflow_status(
        config=config,
        repository_root=repository_root,
        workflow_root=directory.parent,
        workflow_id=plan.workflow_id,
    )


def run_bounded_river_review_workflow(
    source_bytes: bytes,
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    clock: Clock = _now,
) -> BoundedRiverReviewWorkflowStatusV1:
    root = _workflow_root(repository_root, workflow_root)
    directory = _workflow_directory(root, workflow_id)
    plan = _read_plan(directory)
    preparation = _read_preparation(directory)
    confirmation = _read_confirmation(directory)
    _verify_confirmation_binding(plan, preparation, confirmation)
    if (
        hashlib.sha256(source_bytes).hexdigest() != plan.source_sha256
        or sha256_bytes(canonical_json_bytes(preparation)) != plan.preparation_sha256
        or confirmation.run_id != plan.source_run_id
        or confirmation.intake_id != plan.intake_id
    ):
        _fail("BRW_E_SOURCE_BINDING")
    if preparation.candidate is None:  # pragma: no cover - checked by reader
        _fail("BRW_E_PREPARATION")
    admission = admit_bounded_river_call_ev_review(
        source_bytes,
        preparation.candidate,
        confirmation,
    )
    report = review_bounded_river_call_ev_intake(admission, config=config)
    if report.run_id != plan.source_run_id or report.run_status != "completed":
        _fail("BRW_E_SOURCE_RUN")
    source_read, source_context, source_confirmation_sha256 = _verified_source_read(
        config, plan.source_run_id
    )
    return _complete_from_verified_source(
        config=config,
        repository_root=repository_root,
        directory=directory,
        plan=plan,
        confirmation=confirmation,
        source_read=source_read,
        source_context=source_context,
        source_confirmation_sha256=source_confirmation_sha256,
        clock=clock,
    )


def resume_bounded_river_review_workflow(
    source_bytes: bytes | None,
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    clock: Clock = _now,
) -> BoundedRiverReviewWorkflowStatusV1:
    root = _workflow_root(repository_root, workflow_root)
    directory = _workflow_directory(root, workflow_id)
    if (directory / "linkage.json").exists():
        return replay_bounded_river_review_workflow(
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
        )
    if source_bytes is None:
        plan = _read_plan(directory)
        preparation = _read_preparation(directory)
        confirmation = _read_confirmation(directory)
        _verify_confirmation_binding(plan, preparation, confirmation)
        if preparation.candidate is None:  # pragma: no cover - checked by reader
            _fail("BRW_E_PREPARATION")
        try:
            source_read, source_context, source_confirmation_sha256 = _verified_source_read(
                config, plan.source_run_id
            )
        except ProductRunError as exc:
            if exc.failure.code is ProductRunFailureCode.RUN_NOT_FOUND:
                _fail("BRW_E_SOURCE_REQUIRED")
            raise BoundedRiverReviewWorkflowError("BRW_E_SOURCE_RUN") from exc
        if source_context.source.source_candidate_sha256 != preparation.candidate.candidate_sha256:
            _fail("BRW_E_SOURCE_BINDING")
        return _complete_from_verified_source(
            config=config,
            repository_root=repository_root,
            directory=directory,
            plan=plan,
            confirmation=confirmation,
            source_read=source_read,
            source_context=source_context,
            source_confirmation_sha256=source_confirmation_sha256,
            clock=clock,
        )
    return run_bounded_river_review_workflow(
        source_bytes,
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        clock=clock,
    )


def _status_from_bridge(
    plan: BoundedRiverReviewWorkflowPlanV1,
    bridge: VerifiedBridgeRead,
    *,
    confirmation_sha256: str,
    source_terminal_manifest_sha256: str,
) -> BoundedRiverReviewWorkflowStatusV1:
    replayed = replay_bridge(bridge)
    if replayed.status == "succeeded":
        state: WorkflowState = "completed"
        next_action: WorkflowNextAction = "none"
    elif replayed.status in {
        "failed",
        "timed_out",
        "cancelled",
        "cancel_unconfirmed",
        "effect_unknown",
    }:
        state = "failed"
        next_action = "none"
    elif plan.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY:
        state = "completed_local_only"
        next_action = "none"
    elif replayed.completed_roles:
        state = "role_review_in_progress"
        next_action = "use_existing_bridge_commands"
    else:
        state = "awaiting_role_review"
        next_action = "use_existing_bridge_commands"
    return BoundedRiverReviewWorkflowStatusV1(
        workflow_id=plan.workflow_id,
        state=state,
        auth_mode=plan.auth_mode,
        plan_sha256=plan.plan_sha256,
        confirmation_sha256=confirmation_sha256,
        source_run_id=plan.source_run_id,
        source_terminal_manifest_sha256=source_terminal_manifest_sha256,
        bridge_run_id=plan.bridge_run_id,
        bridge_manifest_sha256=bridge.manifest.manifest_sha256,
        bridge_status=replayed.status,
        completed_roles=replayed.completed_roles,
        pending_roles=replayed.pending_roles,
        reconciliation_required=replayed.reconciliation_required,
        next_action=next_action,
    )


def bounded_river_review_workflow_status(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
) -> BoundedRiverReviewWorkflowStatusV1:
    root = _workflow_root(repository_root, workflow_root)
    directory = _workflow_directory(root, workflow_id)
    plan = _read_plan(directory)
    preparation = _read_preparation(directory)
    if sha256_bytes(canonical_json_bytes(preparation)) != plan.preparation_sha256:
        _fail("BRW_E_PLAN_BINDING")
    confirmation_path = directory / "confirmation.json"
    if not confirmation_path.exists():
        return BoundedRiverReviewWorkflowStatusV1(
            workflow_id=plan.workflow_id,
            state="awaiting_confirmation",
            auth_mode=plan.auth_mode,
            plan_sha256=plan.plan_sha256,
            source_run_id=plan.source_run_id,
            bridge_run_id=plan.bridge_run_id,
            next_action="confirm",
        )
    confirmation = _read_confirmation(directory)
    _verify_confirmation_binding(plan, preparation, confirmation)
    bridge = _bridge_read(directory, plan)
    if bridge is None:
        source_read = None
        source_context = None
        if config.revision_runs_dir.exists():
            try:
                (
                    source_read,
                    source_context,
                    source_confirmation_sha256,
                ) = _verified_source_read(config, plan.source_run_id)
            except ProductRunError as exc:
                if exc.failure.code is not ProductRunFailureCode.RUN_NOT_FOUND:
                    raise BoundedRiverReviewWorkflowError("BRW_E_SOURCE_RUN") from exc
        if source_read is not None and source_context is not None:
            if (
                source_context.source.source_candidate_sha256 != confirmation.candidate_sha256
                or source_confirmation_sha256 != confirmation.confirmation_sha256
            ):
                _fail("BRW_E_SOURCE_BINDING")
            return BoundedRiverReviewWorkflowStatusV1(
                workflow_id=plan.workflow_id,
                state="ready_to_resume",
                auth_mode=plan.auth_mode,
                plan_sha256=plan.plan_sha256,
                confirmation_sha256=confirmation.confirmation_sha256,
                source_run_id=plan.source_run_id,
                source_terminal_manifest_sha256=source_read.manifest_sha256,
                bridge_run_id=plan.bridge_run_id,
                next_action="resume",
            )
        return BoundedRiverReviewWorkflowStatusV1(
            workflow_id=plan.workflow_id,
            state="ready_to_run",
            auth_mode=plan.auth_mode,
            plan_sha256=plan.plan_sha256,
            confirmation_sha256=confirmation.confirmation_sha256,
            source_run_id=plan.source_run_id,
            bridge_run_id=plan.bridge_run_id,
            next_action="run",
        )
    source_read, source_context, source_confirmation_sha256 = _verified_source_read(
        config, plan.source_run_id
    )
    if source_confirmation_sha256 != confirmation.confirmation_sha256:
        _fail("BRW_E_SOURCE_BINDING")
    bridge = _verify_bridge_source(directory, plan, source_context)
    if not (directory / "linkage.json").exists():
        partial = _status_from_bridge(
            plan,
            bridge,
            confirmation_sha256=confirmation.confirmation_sha256,
            source_terminal_manifest_sha256=source_read.manifest_sha256,
        )
        return partial.model_copy(update={"state": "ready_to_resume", "next_action": "resume"})
    linkage = _read_linkage(directory)
    expected = (
        plan.workflow_id,
        plan.plan_sha256,
        confirmation.confirmation_sha256,
        source_read.manifest_sha256,
        source_read.manifest.inventory_sha256,
        bridge.manifest.manifest_sha256,
        bridge.manifest.inventory_sha256,
    )
    actual = (
        linkage.workflow_id,
        linkage.plan_sha256,
        linkage.confirmation_sha256,
        linkage.source_terminal_manifest_sha256,
        linkage.source_terminal_inventory_sha256,
        linkage.bridge_manifest_sha256,
        linkage.bridge_inventory_sha256,
    )
    if actual != expected:
        _fail("BRW_E_LINKAGE")
    return _status_from_bridge(
        plan,
        bridge,
        confirmation_sha256=confirmation.confirmation_sha256,
        source_terminal_manifest_sha256=source_read.manifest_sha256,
    )


def replay_bounded_river_review_workflow(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
) -> BoundedRiverReviewWorkflowStatusV1:
    """Replay canonical P3 and bridge storage without a new provider or model call."""

    return bounded_river_review_workflow_status(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
    )


__all__ = [
    "BoundedRiverReviewWorkflowError",
    "bounded_river_confirmation_hashes",
    "bounded_river_review_confirmation_preview",
    "bounded_river_review_linkage_sha256",
    "bounded_river_review_plan_sha256",
    "bounded_river_review_workflow_status",
    "confirm_bounded_river_review_workflow",
    "prepare_bounded_river_review_workflow",
    "replay_bounded_river_review_workflow",
    "resume_bounded_river_review_workflow",
    "run_bounded_river_review_workflow",
]
