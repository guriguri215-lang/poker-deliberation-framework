"""Resumable product composition for one bounded Japanese river review."""

from __future__ import annotations

import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Mapping, Set
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
    BoundedRiverReviewReportViewV1,
    BoundedRiverReviewReportWriterEvidenceV1,
    BoundedRiverReviewRoleConfirmationBindingV1,
    BoundedRiverReviewWorkflowLinkageV1,
    BoundedRiverReviewWorkflowPlanV1,
    BoundedRiverReviewWorkflowStatusV1,
    WorkflowNextAction,
    WorkflowRoleState,
    WorkflowState,
)
from poker_deliberation.budgets.durable_store import DurableBudgetStore
from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes as bridge_canonical_json_bytes,
)
from poker_deliberation.codex_bridge.canonical import (
    parse_canonical_model as parse_bridge_canonical_model,
)
from poker_deliberation.codex_bridge.canonical import (
    sha256_bytes as bridge_sha256_bytes,
)
from poker_deliberation.codex_bridge.controller import (
    BoundedCodexBridgeController,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BoundedCodexBridgeRequestV1,
    BridgeRole,
    BridgeRoleConfirmationV1,
    BridgeRoleResultV1,
    BridgeRunPlanV1,
    BridgeSourceContextV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.product import (
    BridgeProductError,
    confined_product_path,
    confined_runtime_scratch_path,
    confirm_product_role,
    execute_product_role,
    prepare_product_bridge,
    read_product_request,
    role_request_preview,
)
from poker_deliberation.codex_bridge.replay import (
    BridgeReplayError,
    BridgeReplayResult,
    replay_bridge,
)
from poker_deliberation.codex_bridge.source import project_verified_p3_terminal
from poker_deliberation.codex_bridge.source_reader import (
    P3TerminalSourceReadError,
    VerifiedP3TerminalSourceV1,
    read_verified_p3_terminal_source,
)
from poker_deliberation.codex_bridge.storage import (
    BoundedCodexBridgeStore,
    BridgeStorageError,
    VerifiedBridgeRead,
)
from poker_deliberation.config import AppConfig, migrate_budget_config
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.storage.directory_durability import sync_directory
from poker_deliberation.storage.revision_canonical import (
    canonical_domain_sha256,
    canonical_json_bytes,
    parse_canonical_model,
    sha256_bytes,
    validate_run_id,
)
from poker_deliberation.storage.revision_lock import (
    verify_directory,
    verify_regular_single_link,
)
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    VerifiedRunReadV2,
)
from poker_deliberation.storage.terminal_store import (
    DurableBudgetCoordinator,
    TerminalRunStore,
)

_PLAN_HASH_DOMAIN = "poker-bounded-river-review-workflow-plan-v1"
_LINKAGE_HASH_DOMAIN = "poker-bounded-river-review-workflow-linkage-v1"
_ROLE_CONFIRMATION_BINDING_HASH_DOMAIN = "poker-bounded-river-review-role-confirmation-binding-v1"
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


def bounded_river_review_role_confirmation_binding_sha256(
    binding: BoundedRiverReviewRoleConfirmationBindingV1,
) -> str:
    return canonical_domain_sha256(
        _ROLE_CONFIRMATION_BINDING_HASH_DOMAIN,
        _without_hash(binding, "binding_sha256"),
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


def _require_plain_workflow_path(path: Path, repository: Path) -> None:
    current = repository
    for part in path.relative_to(repository).parts:
        current /= part
        try:
            status = current.lstat()
        except FileNotFoundError:
            break
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        is_reparse = bool(getattr(status, "st_file_attributes", 0) & reparse_flag)
        if stat.S_ISLNK(status.st_mode) or is_reparse:
            _fail("BRW_E_STORAGE")
        if not stat.S_ISDIR(status.st_mode):
            _fail("BRW_E_STORAGE")


def _confined_read_only_workflow_root(path: Path, repository_root: Path) -> Path:
    """Confine an existing workflow read without Git probes or filesystem writes."""

    repository = repository_root.resolve(strict=True)
    if not repository.is_dir():
        _fail("BRW_E_STORAGE")
    raw_parts = tuple(part.casefold() for part in path.parts)
    if ".." in raw_parts or any(part in {".git", "user_materials"} for part in raw_parts):
        _fail("BRW_E_STORAGE")
    lexical = Path(os.path.abspath(path))
    try:
        lexical.relative_to(repository)
    except ValueError as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc
    _require_plain_workflow_path(lexical, repository)
    resolved = confined_product_path(path, repository)
    _require_plain_workflow_path(lexical, repository)
    if confined_product_path(path, repository) != resolved:
        _fail("BRW_E_STORAGE")
    return resolved


def _workflow_root(
    repository_root: Path,
    workflow_root: Path,
    *,
    create: bool = True,
    pure_read: bool = False,
) -> Path:
    try:
        if create and pure_read:
            _fail("BRW_E_STORAGE")
        confine = _confined_read_only_workflow_root if pure_read else confined_runtime_scratch_path
        root = confine(workflow_root, repository_root)
        if create:
            root.mkdir(parents=True, exist_ok=True)
        if confine(workflow_root, repository_root) != root:
            _fail("BRW_E_STORAGE")
        return root
    except (OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc


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


def _entry_exists(path: Path) -> bool:
    """Distinguish an absent entry from a broken or otherwise unsafe link."""

    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc
    return True


def _stat_identity(status: os.stat_result) -> tuple[int, ...]:
    return (
        status.st_mode,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
        status.st_dev,
        status.st_ino,
        status.st_nlink,
        getattr(status, "st_file_attributes", 0),
    )


def _read_model(path: Path, model: type[BaseModel]) -> BaseModel:
    try:
        verify_directory(path.parent)
        before = verify_regular_single_link(path)
        size = before.st_size
        if size < 1 or size > BOUNDED_RIVER_REVIEW_WORKFLOW_MAX_ARTIFACT_BYTES:
            _fail("BRW_E_STORAGE")
        data = path.read_bytes()
        after = verify_regular_single_link(path)
        verify_directory(path.parent)
        if len(data) != size or _stat_identity(after) != _stat_identity(before):
            _fail("BRW_E_STORAGE")
        return parse_canonical_model(data, model)
    except (OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc


def _read_plan(
    directory: Path,
    *,
    workflow_id: str | None = None,
) -> BoundedRiverReviewWorkflowPlanV1:
    plan = _read_model(directory / "plan.json", BoundedRiverReviewWorkflowPlanV1)
    assert isinstance(plan, BoundedRiverReviewWorkflowPlanV1)
    if plan.plan_sha256 != bounded_river_review_plan_sha256(plan) or (
        workflow_id is not None and plan.workflow_id != workflow_id
    ):
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


def _verify_linkage_plan_binding(
    plan: BoundedRiverReviewWorkflowPlanV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
    linkage: BoundedRiverReviewWorkflowLinkageV1,
) -> None:
    if (
        linkage.workflow_id,
        linkage.plan_sha256,
        linkage.confirmation_sha256,
        linkage.source_run_id,
        linkage.bridge_run_id,
        linkage.auth_mode,
    ) != (
        plan.workflow_id,
        plan.plan_sha256,
        confirmation.confirmation_sha256,
        plan.source_run_id,
        plan.bridge_run_id,
        plan.auth_mode,
    ):
        _fail("BRW_E_LINKAGE")


def _verify_linkage_storage_binding(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
    linkage: BoundedRiverReviewWorkflowLinkageV1,
    source_read: VerifiedRunReadV2,
    bridge_read: VerifiedBridgeRead,
) -> None:
    _verify_linkage_plan_binding(plan, confirmation, linkage)
    if (
        linkage.source_terminal_manifest_sha256,
        linkage.source_terminal_inventory_sha256,
    ) != (
        source_read.manifest_sha256,
        source_read.manifest.inventory_sha256,
    ):
        _fail("BRW_E_LINKAGE")
    _verify_linked_bridge_ancestor(directory, plan, linkage, bridge_read)


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
    if _entry_exists(directory):
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
    read_plan = _read_plan(directory, workflow_id=workflow_id)
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
    root = _workflow_root(repository_root, workflow_root, create=False)
    directory = _workflow_directory(root, workflow_id)
    plan = _read_plan(directory, workflow_id=workflow_id)
    preparation = _read_preparation(directory)
    if expected_plan_sha256 != plan.plan_sha256:
        _fail("BRW_E_PLAN_BINDING")
    if preparation.candidate is None:  # pragma: no cover - checked by reader
        _fail("BRW_E_PREPARATION")
    if expected_hashes != bounded_river_confirmation_hashes(preparation.candidate):
        _fail("BRW_E_CONFIRMATION_BINDING")
    confirmation_path = directory / "confirmation.json"
    if _entry_exists(confirmation_path):
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
    *,
    expected_source_sha256: str,
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
    if hashlib.sha256(verified.source_bytes).hexdigest() != expected_source_sha256:
        _fail("BRW_E_SOURCE_BINDING")
    source = project_verified_p3_terminal(
        read,
        source_revision_root=orchestrator.product_store.revision_root,
    )
    return read, source, verified.confirmation.confirmation_sha256


def _read_only_product_store(config: AppConfig) -> TerminalRunStore:
    """Build verified terminal readers without initializing any storage root."""

    legacy_root, revision_root, budget_root = config.resolved_storage_roots()
    config._validate_nonoverlapping_roots((legacy_root, revision_root, budget_root))
    policy = migrate_budget_config(config.budgets).policy
    budget = DurableBudgetCoordinator(
        DurableBudgetStore(
            budget_root,
            legacy_root,
            max_artifact_bytes=policy.max_artifact_bytes,
            max_run_bytes=policy.max_run_bytes,
        ),
        policy,
    )
    return TerminalRunStore(
        revision_root,
        legacy_root,
        budget=budget,
        max_artifact_bytes=policy.max_artifact_bytes,
        max_run_bytes=policy.max_run_bytes,
    )


def _read_source_terminal_for_view(
    config: AppConfig,
    source_run_id: str,
) -> tuple[VerifiedRunReadV2, Path]:
    """Read one terminal snapshot without parsing its FinalReport or creating roots."""

    try:
        product_store = _read_only_product_store(config)
        read = product_store.read_current(source_run_id)
    except (OSError, ProductRunError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_SOURCE_RUN") from exc
    return read, product_store.revision_root


def _replay_verified_source_for_view(
    read: VerifiedRunReadV2,
    *,
    source_revision_root: Path,
) -> tuple[BridgeSourceContextV1, VerifiedP3TerminalSourceV1]:
    """Semantically replay P3 only after workflow/bridge/linkage verification."""

    try:
        verified = read_verified_p3_terminal_source(
            read,
            source_revision_root=source_revision_root,
        )
        source = project_verified_p3_terminal(
            read,
            source_revision_root=source_revision_root,
        )
    except (OSError, P3TerminalSourceReadError, ProductRunError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_SOURCE_RUN") from exc
    return source, verified


def _bridge_read(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
) -> VerifiedBridgeRead | None:
    bridge_root = directory / "bridge"
    if not _entry_exists(bridge_root):
        return None
    return BoundedCodexBridgeStore(bridge_root).read_current(plan.bridge_run_id)


def _verify_linked_bridge_ancestor(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    linkage: BoundedRiverReviewWorkflowLinkageV1,
    current: VerifiedBridgeRead,
) -> None:
    """Prove the immutable linked bridge revision is in the verified current lineage."""

    store = BoundedCodexBridgeStore(directory / "bridge")
    _run, _control, _transactions, revisions, _pointer = store._paths(plan.bridge_run_id)
    matches = 0
    try:
        for ordinal in range(1, current.pointer.revision + 1):
            candidates = store._revision_candidates(revisions, ordinal)
            if len(candidates) != 1:
                _fail("BRW_E_LINKAGE")
            _prior_pointer, manifest, _marker, _artifacts = store._pointer_for_revision(
                plan.bridge_run_id,
                candidates[0],
            )
            if (
                manifest.manifest_sha256 == linkage.bridge_manifest_sha256
                and manifest.inventory_sha256 == linkage.bridge_inventory_sha256
            ):
                matches += 1
    except (BridgeStorageError, OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_LINKAGE") from exc
    if matches != 1:
        _fail("BRW_E_LINKAGE")


def _role_confirmation_binding_path(directory: Path, role: BridgeRole) -> Path:
    ordinal = BRIDGE_ROLE_ORDER.index(role)
    return directory / f"role-confirmation-binding-{ordinal}-{role.value}.json"


def _bridge_role_request(
    bridge: VerifiedBridgeRead,
    role: BridgeRole,
) -> BoundedCodexBridgeRequestV1:
    try:
        request = parse_bridge_canonical_model(
            bridge.artifact_bytes(role_artifact_name(role, "request")),
            BoundedCodexBridgeRequestV1,
        )
    except (BridgeStorageError, KeyError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_ROLE_BINDING") from exc
    if request.context.assignment.role is not role:
        _fail("BRW_E_ROLE_BINDING")
    return request


def _bridge_role_confirmation(
    bridge: VerifiedBridgeRead,
    role: BridgeRole,
) -> BridgeRoleConfirmationV1 | None:
    name = role_artifact_name(role, "confirmation")
    if name not in bridge.artifacts:
        return None
    try:
        parsed = parse_bridge_canonical_model(
            bridge.artifact_bytes(name),
            BridgeRoleConfirmationV1,
        )
    except (BridgeStorageError, KeyError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_ROLE_BINDING") from exc
    if parsed.role is not role:
        _fail("BRW_E_ROLE_BINDING")
    return parsed


def _verify_bridge_confirmation_request_binding(
    plan: BoundedRiverReviewWorkflowPlanV1,
    request: BoundedCodexBridgeRequestV1,
    confirmation: BridgeRoleConfirmationV1,
) -> None:
    policy = request.context.runtime_policy
    assignment = request.context.assignment
    if (
        request.auth_mode is not plan.auth_mode
        or assignment.bridge_run_id != plan.bridge_run_id
        or confirmation.bridge_run_id != plan.bridge_run_id
        or confirmation.auth_mode is not plan.auth_mode
        or confirmation.role is not assignment.role
        or confirmation.assignment_id != assignment.assignment_id
        or confirmation.attempt_id != assignment.attempt_id
        or confirmation.request_sha256 != request.request_sha256
        or confirmation.request_bytes_sha256 != request.request_bytes_sha256
        or confirmation.envelope_sha256 != request.context.envelope_sha256
        or confirmation.runtime_policy_sha256 != policy.policy_sha256
        or confirmation.runtime_identity != policy.runtime_identity
        or confirmation.model_provider != policy.model_provider
        or confirmation.model != policy.model
        or confirmation.credential_reference != policy.credential_reference
        or confirmation.authority.authority_kind != "local_user"
        or confirmation.authority.authentication != "self_asserted"
    ):
        _fail("BRW_E_ROLE_BINDING")


def _bridge_lineage_snapshot(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    current: VerifiedBridgeRead,
    *,
    revision: int,
    manifest_sha256: str,
    inventory_sha256: str,
    pointer_sha256: str,
) -> tuple[object, object]:
    if revision < 1 or revision > current.pointer.revision:
        _fail("BRW_E_ROLE_BINDING")
    store = BoundedCodexBridgeStore(directory / "bridge")
    _run, _control, _transactions, revisions, _pointer = store._paths(plan.bridge_run_id)
    try:
        candidates = store._revision_candidates(revisions, revision)
        if len(candidates) != 1:
            _fail("BRW_E_ROLE_BINDING")
        pointer, manifest, _marker, _artifacts = store._pointer_for_revision(
            plan.bridge_run_id,
            candidates[0],
        )
        if (
            manifest.manifest_sha256 != manifest_sha256
            or manifest.inventory_sha256 != inventory_sha256
            or bridge_sha256_bytes(bridge_canonical_json_bytes(pointer)) != pointer_sha256
        ):
            _fail("BRW_E_ROLE_BINDING")
        return pointer, manifest
    except (BridgeStorageError, OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_ROLE_BINDING") from exc


def _verify_role_confirmation_binding(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    workflow_confirmation: BoundedRiverCallEvConfirmationV1,
    linkage: BoundedRiverReviewWorkflowLinkageV1,
    bridge: VerifiedBridgeRead,
    binding: BoundedRiverReviewRoleConfirmationBindingV1,
) -> None:
    request = _bridge_role_request(bridge, binding.role)
    bridge_confirmation = _bridge_role_confirmation(bridge, binding.role)
    if bridge_confirmation is None:
        _fail("BRW_E_ROLE_BINDING")
    _verify_bridge_confirmation_request_binding(plan, request, bridge_confirmation)
    policy = request.context.runtime_policy
    expected = (
        plan.workflow_id,
        plan.plan_sha256,
        workflow_confirmation.confirmation_sha256,
        linkage.linkage_sha256,
        plan.bridge_run_id,
        plan.auth_mode,
        BRIDGE_ROLE_ORDER.index(binding.role),
        request.request_sha256,
        request.request_bytes_sha256,
        request.context.envelope_sha256,
        policy.policy_sha256,
        policy.runtime_identity,
        policy.model_provider,
        policy.model,
        policy.credential_reference,
        policy.remote_retention_policy,
        bridge_confirmation.authority.authority_id,
        bridge_confirmation.confirmation_id,
        bridge_confirmation.idempotency_key,
        bridge_confirmation.confirmation_sha256,
        bridge_confirmation.confirmed_at,
        bridge_confirmation.expires_at,
    )
    actual = (
        binding.workflow_id,
        binding.plan_sha256,
        binding.workflow_confirmation_sha256,
        binding.linkage_sha256,
        binding.bridge_run_id,
        binding.auth_mode,
        binding.role_ordinal,
        binding.request_sha256,
        binding.request_bytes_sha256,
        binding.envelope_sha256,
        binding.runtime_policy_sha256,
        binding.runtime_identity,
        binding.model_provider,
        binding.model,
        binding.credential_reference,
        binding.remote_retention_policy,
        binding.authority_id,
        binding.confirmation_id,
        binding.idempotency_key,
        binding.bridge_confirmation_sha256,
        binding.bridge_confirmation_confirmed_at,
        binding.bridge_confirmation_expires_at,
    )
    if (
        binding.binding_sha256 != bounded_river_review_role_confirmation_binding_sha256(binding)
        or actual != expected
    ):
        _fail("BRW_E_ROLE_BINDING")
    preview_pointer, preview_manifest = _bridge_lineage_snapshot(
        directory,
        plan,
        bridge,
        revision=binding.preview_bridge_revision,
        manifest_sha256=binding.preview_bridge_manifest_sha256,
        inventory_sha256=binding.preview_bridge_inventory_sha256,
        pointer_sha256=binding.preview_bridge_pointer_sha256,
    )
    confirmed_pointer, confirmed_manifest = _bridge_lineage_snapshot(
        directory,
        plan,
        bridge,
        revision=binding.confirmed_bridge_revision,
        manifest_sha256=binding.confirmed_bridge_manifest_sha256,
        inventory_sha256=binding.confirmed_bridge_inventory_sha256,
        pointer_sha256=binding.confirmed_bridge_pointer_sha256,
    )
    if binding.confirmed_bridge_revision == binding.preview_bridge_revision:
        if confirmed_pointer != preview_pointer or confirmed_manifest != preview_manifest:
            _fail("BRW_E_ROLE_BINDING")
    elif (
        binding.confirmed_bridge_revision != binding.preview_bridge_revision + 1
        or getattr(confirmed_manifest, "previous_manifest_sha256", None)
        != binding.preview_bridge_manifest_sha256
        or getattr(confirmed_manifest, "expected_pointer_sha256", None)
        != binding.preview_bridge_pointer_sha256
    ):
        _fail("BRW_E_ROLE_BINDING")


def _verified_role_confirmation_bindings(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    workflow_confirmation: BoundedRiverCallEvConfirmationV1,
    linkage: BoundedRiverReviewWorkflowLinkageV1,
    bridge: VerifiedBridgeRead,
    replayed: BridgeReplayResult,
) -> Mapping[BridgeRole, BoundedRiverReviewRoleConfirmationBindingV1]:
    verified: dict[BridgeRole, BoundedRiverReviewRoleConfirmationBindingV1] = {}
    for role in BRIDGE_ROLE_ORDER:
        path = _role_confirmation_binding_path(directory, role)
        if not _entry_exists(path):
            continue
        try:
            parsed = _read_model(path, BoundedRiverReviewRoleConfirmationBindingV1)
            assert isinstance(parsed, BoundedRiverReviewRoleConfirmationBindingV1)
            if parsed.role is not role:
                _fail("BRW_E_ROLE_BINDING")
            _verify_role_confirmation_binding(
                directory,
                plan,
                workflow_confirmation,
                linkage,
                bridge,
                parsed,
            )
        except (OSError, ValueError) as exc:
            if isinstance(exc, BoundedRiverReviewWorkflowError):
                raise
            raise BoundedRiverReviewWorkflowError("BRW_E_ROLE_BINDING") from exc
        verified[role] = parsed
    names = set(bridge.artifacts)
    roles_requiring_binding = set(replayed.completed_roles)
    roles_requiring_binding.update(
        role for role in BRIDGE_ROLE_ORDER if role_artifact_name(role, "admission") in names
    )
    if not roles_requiring_binding.issubset(verified):
        _fail("BRW_E_ROLE_BINDING")
    return verified


def _create_role_confirmation_binding(
    *,
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    workflow_confirmation: BoundedRiverCallEvConfirmationV1,
    linkage: BoundedRiverReviewWorkflowLinkageV1,
    request: BoundedCodexBridgeRequestV1,
    bridge_confirmation: BridgeRoleConfirmationV1,
    preview_bridge: VerifiedBridgeRead,
    confirmed_bridge: VerifiedBridgeRead,
) -> BoundedRiverReviewRoleConfirmationBindingV1:
    role = request.context.assignment.role
    policy = request.context.runtime_policy
    provisional = BoundedRiverReviewRoleConfirmationBindingV1(
        workflow_id=plan.workflow_id,
        plan_sha256=plan.plan_sha256,
        workflow_confirmation_sha256=workflow_confirmation.confirmation_sha256,
        linkage_sha256=linkage.linkage_sha256,
        bridge_run_id=plan.bridge_run_id,
        auth_mode=plan.auth_mode,
        role=role,
        role_ordinal=BRIDGE_ROLE_ORDER.index(role),
        request_sha256=request.request_sha256,
        request_bytes_sha256=request.request_bytes_sha256,
        envelope_sha256=request.context.envelope_sha256,
        runtime_policy_sha256=policy.policy_sha256,
        runtime_identity=policy.runtime_identity,
        model_provider=policy.model_provider,
        model=policy.model,
        credential_reference=policy.credential_reference,
        remote_retention_policy=policy.remote_retention_policy,
        authority_id=bridge_confirmation.authority.authority_id,
        confirmation_id=bridge_confirmation.confirmation_id,
        idempotency_key=bridge_confirmation.idempotency_key,
        bridge_confirmation_sha256=bridge_confirmation.confirmation_sha256,
        bridge_confirmation_confirmed_at=bridge_confirmation.confirmed_at,
        bridge_confirmation_expires_at=bridge_confirmation.expires_at,
        preview_bridge_revision=preview_bridge.pointer.revision,
        preview_bridge_manifest_sha256=preview_bridge.manifest.manifest_sha256,
        preview_bridge_inventory_sha256=preview_bridge.manifest.inventory_sha256,
        preview_bridge_pointer_sha256=preview_bridge.pointer_sha256,
        confirmed_bridge_revision=confirmed_bridge.pointer.revision,
        confirmed_bridge_manifest_sha256=confirmed_bridge.manifest.manifest_sha256,
        confirmed_bridge_inventory_sha256=confirmed_bridge.manifest.inventory_sha256,
        confirmed_bridge_pointer_sha256=confirmed_bridge.pointer_sha256,
        binding_sha256="0" * 64,
    )
    binding = provisional.model_copy(
        update={
            "binding_sha256": bounded_river_review_role_confirmation_binding_sha256(provisional)
        }
    )
    path = _role_confirmation_binding_path(directory, role)
    _write_new(path, binding)
    try:
        sync_directory(directory, hook="bounded_river_review.role_confirmation_binding")
    except (OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc
    stored = _read_model(path, BoundedRiverReviewRoleConfirmationBindingV1)
    assert isinstance(stored, BoundedRiverReviewRoleConfirmationBindingV1)
    if stored != binding:
        _fail("BRW_E_STORAGE")
    _verify_role_confirmation_binding(
        directory,
        plan,
        workflow_confirmation,
        linkage,
        confirmed_bridge,
        stored,
    )
    return stored


def _verified_bridge_run_plan(
    bridge: VerifiedBridgeRead,
    plan: BoundedRiverReviewWorkflowPlanV1,
) -> BridgeRunPlanV1:
    try:
        bridge_plan = parse_bridge_canonical_model(
            bridge.artifact_bytes("run_plan.json"),
            BridgeRunPlanV1,
        )
    except (BridgeStorageError, KeyError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_BRIDGE_BINDING") from exc
    expected_total_max_cost = (
        plan.api_max_cost_micro_usd * len(BRIDGE_ROLE_ORDER)
        if plan.api_max_cost_micro_usd is not None
        else None
    )
    if (
        bridge_plan.bridge_run_id != plan.bridge_run_id
        or bridge_plan.auth_mode is not plan.auth_mode
        or bridge_plan.source.source_terminal_run_id != plan.source_run_id
        or bridge_plan.repository_commit_id != plan.repository_commit_id
        or bridge_plan.repository_tree_id != plan.repository_tree_id
        or bridge_plan.total_max_cost_micro_usd != expected_total_max_cost
    ):
        _fail("BRW_E_BRIDGE_BINDING")
    return bridge_plan


def _verify_bridge_source(
    directory: Path,
    plan: BoundedRiverReviewWorkflowPlanV1,
    expected_source: object,
) -> VerifiedBridgeRead:
    try:
        bridge = _bridge_read(directory, plan)
        if bridge is None:
            _fail("BRW_E_BRIDGE")
        bridge_plan = _verified_bridge_run_plan(bridge, plan)
        stored_source = BoundedCodexBridgeController(
            BoundedCodexBridgeStore(directory / "bridge")
        ).read_source_context(plan.bridge_run_id)
        if (
            stored_source != expected_source
            or bridge_plan.source != stored_source.source
            or bridge.pointer.auth_mode is not plan.auth_mode
        ):
            _fail("BRW_E_BRIDGE_BINDING")
        return bridge
    except (BridgeStorageError, OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_BRIDGE") from exc


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
    if _entry_exists(path):
        existing = _read_linkage(directory)
        _verify_linkage_storage_binding(
            directory,
            plan,
            confirmation,
            existing,
            source_read,
            bridge_read,
        )
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
    root = _workflow_root(repository_root, workflow_root, create=False)
    directory = _workflow_directory(root, workflow_id)
    plan = _read_plan(directory, workflow_id=workflow_id)
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
        config,
        plan.source_run_id,
        expected_source_sha256=plan.source_sha256,
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
    root = _workflow_root(repository_root, workflow_root, create=False)
    directory = _workflow_directory(root, workflow_id)
    if _entry_exists(directory / "linkage.json"):
        return replay_bounded_river_review_workflow(
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
        )
    if source_bytes is None:
        plan = _read_plan(directory, workflow_id=workflow_id)
        preparation = _read_preparation(directory)
        confirmation = _read_confirmation(directory)
        _verify_confirmation_binding(plan, preparation, confirmation)
        if preparation.candidate is None:  # pragma: no cover - checked by reader
            _fail("BRW_E_PREPARATION")
        try:
            source_read, source_context, source_confirmation_sha256 = _verified_source_read(
                config,
                plan.source_run_id,
                expected_source_sha256=plan.source_sha256,
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
    role_bindings: Set[BridgeRole] = frozenset(),
    clock: Clock | None = None,
) -> BoundedRiverReviewWorkflowStatusV1:
    try:
        replayed = replay_bridge(bridge)
    except (BridgeReplayError, BridgeStorageError, OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_BRIDGE") from exc
    return _status_from_replayed_bridge(
        plan,
        bridge,
        replayed,
        confirmation_sha256=confirmation_sha256,
        source_terminal_manifest_sha256=source_terminal_manifest_sha256,
        role_bindings=role_bindings,
        clock=clock,
    )


def _role_progress(
    plan: BoundedRiverReviewWorkflowPlanV1,
    bridge: VerifiedBridgeRead,
    replayed: BridgeReplayResult,
    *,
    role_bindings: Set[BridgeRole] = frozenset(),
    clock: Clock | None = None,
) -> tuple[BridgeRole | None, WorkflowRoleState, WorkflowNextAction]:
    terminal_statuses = {
        "succeeded",
        "failed",
        "timed_out",
        "cancelled",
        "cancel_unconfirmed",
        "effect_unknown",
    }
    if (
        replayed.status in terminal_statuses
        or plan.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY
        or not replayed.pending_roles
    ):
        return None, "terminal", "none"

    role = replayed.pending_roles[0]
    names = set(bridge.artifacts)
    request_name = role_artifact_name(role, "request")
    confirmation_name = role_artifact_name(role, "confirmation")
    admission_name = role_artifact_name(role, "admission")
    audit_name = role_artifact_name(role, "audit")
    if request_name not in names:
        _fail("BRW_E_ROLE_STATE")
    if admission_name in names and audit_name not in names:
        return role, "in_progress", "none"
    request = _bridge_role_request(bridge, role)
    observed_at = (clock or _now)()
    if confirmation_name in names and admission_name not in names:
        confirmation = _bridge_role_confirmation(bridge, role)
        if confirmation is None:  # pragma: no cover - name was present
            _fail("BRW_E_ROLE_BINDING")
        if observed_at >= confirmation.expires_at:
            return role, "expired", "none"
        if role not in role_bindings:
            return role, "awaiting_confirmation", "show_role_request"
        return role, "executable", "execute_role"
    if confirmation_name not in names and admission_name not in names:
        if observed_at >= request.context.assignment.expires_at:
            return role, "expired", "none"
        return role, "awaiting_confirmation", "show_role_request"
    _fail("BRW_E_ROLE_STATE")


def _status_from_replayed_bridge(
    plan: BoundedRiverReviewWorkflowPlanV1,
    bridge: VerifiedBridgeRead,
    replayed: BridgeReplayResult,
    *,
    confirmation_sha256: str,
    source_terminal_manifest_sha256: str,
    role_bindings: Set[BridgeRole] = frozenset(),
    clock: Clock | None = None,
) -> BoundedRiverReviewWorkflowStatusV1:
    next_role, role_state, role_next_action = _role_progress(
        plan,
        bridge,
        replayed,
        role_bindings=role_bindings,
        clock=clock,
    )
    role_request = (
        _bridge_role_request(bridge, next_role)
        if next_role is not None and role_state not in {"in_progress", "terminal"}
        else None
    )
    role_confirmation = (
        _bridge_role_confirmation(bridge, next_role)
        if role_request is not None and next_role is not None
        else None
    )
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
    elif replayed.completed_roles or role_state != "awaiting_confirmation":
        state = "role_review_in_progress"
        next_action = role_next_action
    else:
        state = "awaiting_role_review"
        next_action = role_next_action
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
        next_role=next_role,
        role_state=role_state,
        role_request_expires_at=(
            role_request.context.assignment.expires_at if role_request is not None else None
        ),
        role_confirmation_expires_at=(
            role_confirmation.expires_at if role_confirmation is not None else None
        ),
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
    root = _workflow_root(
        repository_root,
        workflow_root,
        create=False,
        pure_read=True,
    )
    directory = _workflow_directory(root, workflow_id)
    plan = _read_plan(directory, workflow_id=workflow_id)
    preparation = _read_preparation(directory)
    if sha256_bytes(canonical_json_bytes(preparation)) != plan.preparation_sha256:
        _fail("BRW_E_PLAN_BINDING")
    confirmation_path = directory / "confirmation.json"
    if not _entry_exists(confirmation_path):
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
                ) = _verified_source_read(
                    config,
                    plan.source_run_id,
                    expected_source_sha256=plan.source_sha256,
                )
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
        config,
        plan.source_run_id,
        expected_source_sha256=plan.source_sha256,
    )
    if source_confirmation_sha256 != confirmation.confirmation_sha256:
        _fail("BRW_E_SOURCE_BINDING")
    bridge = _verify_bridge_source(directory, plan, source_context)
    if not _entry_exists(directory / "linkage.json"):
        partial = _status_from_bridge(
            plan,
            bridge,
            confirmation_sha256=confirmation.confirmation_sha256,
            source_terminal_manifest_sha256=source_read.manifest_sha256,
        )
        return partial.model_copy(update={"state": "ready_to_resume", "next_action": "resume"})
    linkage = _read_linkage(directory)
    _verify_linkage_storage_binding(
        directory,
        plan,
        confirmation,
        linkage,
        source_read,
        bridge,
    )
    try:
        replayed = replay_bridge(bridge)
    except (BridgeReplayError, BridgeStorageError, OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_BRIDGE") from exc
    bindings = _verified_role_confirmation_bindings(
        directory,
        plan,
        confirmation,
        linkage,
        bridge,
        replayed,
    )
    return _status_from_replayed_bridge(
        plan,
        bridge,
        replayed,
        confirmation_sha256=confirmation.confirmation_sha256,
        source_terminal_manifest_sha256=source_read.manifest_sha256,
        role_bindings=set(bindings),
    )


def _verified_role_workflow(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    pure_read: bool,
    clock: Clock | None = None,
) -> tuple[
    Path,
    BoundedRiverReviewWorkflowPlanV1,
    BoundedRiverCallEvConfirmationV1,
    BoundedRiverReviewWorkflowLinkageV1,
    VerifiedRunReadV2,
    VerifiedBridgeRead,
    BridgeReplayResult,
    BoundedRiverReviewWorkflowStatusV1,
]:
    root = _workflow_root(
        repository_root,
        workflow_root,
        create=False,
        pure_read=pure_read,
    )
    directory = _workflow_directory(root, workflow_id)
    plan = _read_plan(directory, workflow_id=workflow_id)
    preparation = _read_preparation(directory)
    if sha256_bytes(canonical_json_bytes(preparation)) != plan.preparation_sha256:
        _fail("BRW_E_PLAN_BINDING")
    confirmation = _read_confirmation(directory)
    _verify_confirmation_binding(plan, preparation, confirmation)
    if not _entry_exists(directory / "linkage.json"):
        _fail("BRW_E_LINKAGE")
    linkage = _read_linkage(directory)
    _verify_linkage_plan_binding(plan, confirmation, linkage)

    source_read, source_revision_root = _read_source_terminal_for_view(
        config,
        linkage.source_run_id,
    )
    if source_read.run_id != linkage.source_run_id:
        _fail("BRW_E_SOURCE_BINDING")
    source_context, verified_source = _replay_verified_source_for_view(
        source_read,
        source_revision_root=source_revision_root,
    )
    try:
        bridge = _verify_bridge_source(directory, plan, source_context)
        replayed = replay_bridge(bridge)
    except (BridgeReplayError, BridgeStorageError, OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_BRIDGE") from exc
    _verify_linkage_storage_binding(
        directory,
        plan,
        confirmation,
        linkage,
        source_read,
        bridge,
    )
    if (
        source_context.source.source_terminal_run_id != linkage.source_run_id
        or source_context.source.source_candidate_sha256 != confirmation.candidate_sha256
        or verified_source.confirmation.confirmation_sha256 != confirmation.confirmation_sha256
        or hashlib.sha256(verified_source.source_bytes).hexdigest() != plan.source_sha256
    ):
        _fail("BRW_E_SOURCE_BINDING")
    bindings = _verified_role_confirmation_bindings(
        directory,
        plan,
        confirmation,
        linkage,
        bridge,
        replayed,
    )
    status = _status_from_replayed_bridge(
        plan,
        bridge,
        replayed,
        confirmation_sha256=confirmation.confirmation_sha256,
        source_terminal_manifest_sha256=source_read.manifest_sha256,
        role_bindings=set(bindings),
        clock=clock,
    )
    return (
        directory,
        plan,
        confirmation,
        linkage,
        source_read,
        bridge,
        replayed,
        status,
    )


def _verified_role_mutation_context(
    observed: tuple[
        Path,
        BoundedRiverReviewWorkflowPlanV1,
        BoundedRiverCallEvConfirmationV1,
        BoundedRiverReviewWorkflowLinkageV1,
        VerifiedRunReadV2,
        VerifiedBridgeRead,
        BridgeReplayResult,
        BoundedRiverReviewWorkflowStatusV1,
    ],
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    observed_at: datetime,
) -> tuple[
    Path,
    BoundedRiverReviewWorkflowPlanV1,
    BoundedRiverCallEvConfirmationV1,
    BoundedRiverReviewWorkflowLinkageV1,
    VerifiedRunReadV2,
    VerifiedBridgeRead,
    BridgeReplayResult,
    BoundedRiverReviewWorkflowStatusV1,
]:
    """Re-authorize the mutation path and reject any snapshot transition."""

    mutable = _verified_role_workflow(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        pure_read=False,
        clock=lambda: observed_at,
    )
    (
        observed_directory,
        observed_plan,
        observed_confirmation,
        observed_linkage,
        observed_source,
        observed_bridge,
        observed_replayed,
        observed_status,
    ) = observed
    (
        mutable_directory,
        mutable_plan,
        mutable_confirmation,
        mutable_linkage,
        mutable_source,
        mutable_bridge,
        mutable_replayed,
        mutable_status,
    ) = mutable
    if (
        observed_directory != mutable_directory
        or observed_plan != mutable_plan
        or observed_confirmation != mutable_confirmation
        or observed_linkage != mutable_linkage
        or observed_source.manifest_sha256 != mutable_source.manifest_sha256
        or observed_source.manifest.inventory_sha256 != mutable_source.manifest.inventory_sha256
        or observed_bridge.pointer != mutable_bridge.pointer
        or observed_bridge.pointer_sha256 != mutable_bridge.pointer_sha256
        or observed_bridge.manifest.manifest_sha256 != mutable_bridge.manifest.manifest_sha256
        or observed_bridge.manifest.inventory_sha256 != mutable_bridge.manifest.inventory_sha256
        or observed_replayed != mutable_replayed
        or observed_status != mutable_status
    ):
        _fail("BRW_E_ROLE_BINDING")
    return mutable


def bounded_river_review_role_request_preview(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
) -> dict[str, object]:
    """Show the exact next role request from one verified linked workflow."""

    (
        directory,
        plan,
        confirmation,
        linkage,
        source_read,
        bridge,
        _replayed,
        status,
    ) = _verified_role_workflow(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        pure_read=True,
    )
    if plan.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY:
        _fail("BRW_E_LOCAL_ONLY")
    if status.reconciliation_required:
        _fail("BRW_E_ROLE_RECONCILIATION")
    if status.role_state == "expired":
        _fail("BRW_E_ROLE_EXPIRED")
    if status.next_role is None or status.role_state in {"in_progress", "terminal"}:
        _fail("BRW_E_ROLE_STATE")
    role = status.next_role
    try:
        request = read_product_request(
            repository_root=repository_root,
            bridge_root=directory / "bridge",
            bridge_run_id=plan.bridge_run_id,
            role=role,
            auth_mode=plan.auth_mode,
        )
        request_preview = role_request_preview(request)
    except (BridgeProductError, BridgeStorageError, OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_BRIDGE") from exc
    policy = request.context.runtime_policy
    return {
        "schema_version": "1.0.0",
        "contract_id": "poker-bounded-river-review-role-request-preview",
        "workflow_id": plan.workflow_id,
        "workflow_plan_sha256": plan.plan_sha256,
        "workflow_confirmation_sha256": confirmation.confirmation_sha256,
        "workflow_linkage_sha256": linkage.linkage_sha256,
        "source_terminal_manifest_sha256": source_read.manifest_sha256,
        "source_terminal_inventory_sha256": source_read.manifest.inventory_sha256,
        "linked_bridge_manifest_sha256": linkage.bridge_manifest_sha256,
        "linked_bridge_inventory_sha256": linkage.bridge_inventory_sha256,
        "current_bridge_revision": bridge.pointer.revision,
        "current_bridge_status": bridge.pointer.status,
        "current_bridge_manifest_sha256": bridge.manifest.manifest_sha256,
        "current_bridge_inventory_sha256": bridge.manifest.inventory_sha256,
        "current_bridge_pointer_sha256": bridge.pointer_sha256,
        "next_role": role,
        "next_role_state": status.role_state,
        "request": request_preview,
        "confirmation_fields": {
            "expected_plan_sha256": plan.plan_sha256,
            "expected_linkage_sha256": linkage.linkage_sha256,
            "expected_bridge_revision": bridge.pointer.revision,
            "expected_bridge_manifest_sha256": bridge.manifest.manifest_sha256,
            "expected_bridge_inventory_sha256": bridge.manifest.inventory_sha256,
            "expected_bridge_pointer_sha256": bridge.pointer_sha256,
            "expected_role": role,
            "expected_auth_mode": plan.auth_mode,
            "expected_request_sha256": request.request_sha256,
            "expected_request_bytes_sha256": request.request_bytes_sha256,
            "expected_envelope_sha256": request.context.envelope_sha256,
            "expected_runtime_policy_sha256": policy.policy_sha256,
            "expected_runtime_identity": policy.runtime_identity,
            "expected_model_provider": policy.model_provider,
            "expected_model": policy.model,
            "expected_credential_reference": policy.credential_reference,
            "expected_remote_retention_policy": policy.remote_retention_policy,
        },
    }


def confirm_bounded_river_review_role_request(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    authority_id: str,
    confirmation_id: str,
    idempotency_key: str,
    expected_plan_sha256: str,
    expected_linkage_sha256: str,
    expected_bridge_revision: int,
    expected_bridge_manifest_sha256: str,
    expected_bridge_inventory_sha256: str,
    expected_bridge_pointer_sha256: str,
    expected_role: BridgeRole,
    expected_auth_mode: RuntimeAuthModeV1,
    expected_request_sha256: str,
    expected_request_bytes_sha256: str,
    expected_envelope_sha256: str,
    expected_runtime_policy_sha256: str,
    expected_runtime_identity: str,
    expected_model_provider: str,
    expected_model: str | None,
    expected_credential_reference: str,
    expected_remote_retention_policy: str,
) -> BoundedRiverReviewWorkflowStatusV1:
    """Confirm only the next role, cross-bound to one verified workflow snapshot."""

    observed_at = _now()
    observed = _verified_role_workflow(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        pure_read=True,
        clock=lambda: observed_at,
    )
    plan = observed[1]
    status = observed[7]
    if plan.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY:
        _fail("BRW_E_LOCAL_ONLY")
    if status.reconciliation_required:
        _fail("BRW_E_ROLE_RECONCILIATION")
    if status.role_state == "expired":
        _fail("BRW_E_ROLE_EXPIRED")
    if status.next_role is None or status.role_state != "awaiting_confirmation":
        _fail("BRW_E_ROLE_STATE")
    mutable = _verified_role_mutation_context(
        observed,
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        observed_at=observed_at,
    )
    (
        directory,
        plan,
        workflow_confirmation,
        linkage,
        _source,
        bridge,
        _replayed,
        status,
    ) = mutable
    if status.reconciliation_required:
        _fail("BRW_E_ROLE_RECONCILIATION")
    if status.role_state == "expired":
        _fail("BRW_E_ROLE_EXPIRED")
    if status.next_role is None or status.role_state != "awaiting_confirmation":
        _fail("BRW_E_ROLE_STATE")
    if expected_role is not status.next_role:
        _fail("BRW_E_ROLE_ORDER")
    request = _bridge_role_request(bridge, status.next_role)
    policy = request.context.runtime_policy
    if (
        expected_plan_sha256 != plan.plan_sha256
        or expected_linkage_sha256 != linkage.linkage_sha256
        or expected_bridge_revision != bridge.pointer.revision
        or expected_bridge_manifest_sha256 != bridge.manifest.manifest_sha256
        or expected_bridge_inventory_sha256 != bridge.manifest.inventory_sha256
        or expected_bridge_pointer_sha256 != bridge.pointer_sha256
        or expected_auth_mode is not plan.auth_mode
        or expected_request_sha256 != request.request_sha256
        or expected_request_bytes_sha256 != request.request_bytes_sha256
        or expected_envelope_sha256 != request.context.envelope_sha256
        or expected_runtime_policy_sha256 != policy.policy_sha256
        or expected_runtime_identity != policy.runtime_identity
        or expected_model_provider != policy.model_provider
        or expected_model != policy.model
        or expected_credential_reference != policy.credential_reference
        or expected_remote_retention_policy != policy.remote_retention_policy
    ):
        _fail("BRW_E_ROLE_BINDING")
    if _now() >= request.context.assignment.expires_at:
        _fail("BRW_E_ROLE_EXPIRED")
    bridge_confirmation = _bridge_role_confirmation(bridge, status.next_role)
    if bridge_confirmation is None:
        try:
            confirmed = confirm_product_role(
                repository_root=repository_root,
                bridge_root=directory / "bridge",
                bridge_run_id=plan.bridge_run_id,
                role=status.next_role,
                authority_id=authority_id,
                confirmation_id=confirmation_id,
                idempotency_key=idempotency_key,
                expected_request_sha256=expected_request_sha256,
                expected_request_bytes_sha256=expected_request_bytes_sha256,
                expected_envelope_sha256=expected_envelope_sha256,
                expected_runtime_policy_sha256=expected_runtime_policy_sha256,
                expected_auth_mode=expected_auth_mode,
                expected_runtime_identity=expected_runtime_identity,
                expected_model_provider=expected_model_provider,
                expected_model=expected_model,
                expected_credential_reference=expected_credential_reference,
                expected_remote_retention_policy=expected_remote_retention_policy,
                expected_current_revision=expected_bridge_revision,
                expected_current_manifest_sha256=expected_bridge_manifest_sha256,
                expected_current_inventory_sha256=expected_bridge_inventory_sha256,
                expected_current_pointer_sha256=expected_bridge_pointer_sha256,
            )
        except (BridgeProductError, BridgeStorageError, OSError, ValueError) as exc:
            raise BoundedRiverReviewWorkflowError("BRW_E_ROLE_BINDING") from exc
        if (
            confirmed.pointer.revision != expected_bridge_revision + 1
            or confirmed.manifest.previous_manifest_sha256 != expected_bridge_manifest_sha256
            or confirmed.manifest.expected_pointer_sha256 != expected_bridge_pointer_sha256
        ):
            _fail("BRW_E_ROLE_BINDING")
        bridge_confirmation = _bridge_role_confirmation(confirmed, status.next_role)
        if bridge_confirmation is None:  # pragma: no cover - controller contract
            _fail("BRW_E_ROLE_BINDING")
    else:
        confirmed = bridge
    _verify_bridge_confirmation_request_binding(plan, request, bridge_confirmation)
    if (
        bridge_confirmation.authority.authority_id != authority_id
        or bridge_confirmation.confirmation_id != confirmation_id
        or bridge_confirmation.idempotency_key != idempotency_key
    ):
        _fail("BRW_E_ROLE_BINDING")
    if _now() >= bridge_confirmation.expires_at:
        _fail("BRW_E_ROLE_EXPIRED")
    _create_role_confirmation_binding(
        directory=directory,
        plan=plan,
        workflow_confirmation=workflow_confirmation,
        linkage=linkage,
        request=request,
        bridge_confirmation=bridge_confirmation,
        preview_bridge=bridge,
        confirmed_bridge=confirmed,
    )
    return bounded_river_review_workflow_status(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
    )


def execute_bounded_river_review_role(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    runtime_root: Path,
    codex_binary: Path | None = None,
) -> BoundedRiverReviewWorkflowStatusV1:
    """Execute the one confirmed next role, without retry or fallback."""

    observed_at = _now()
    observed = _verified_role_workflow(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        pure_read=True,
        clock=lambda: observed_at,
    )
    plan = observed[1]
    status = observed[7]
    if plan.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY:
        _fail("BRW_E_LOCAL_ONLY")
    if status.reconciliation_required or status.role_state == "in_progress":
        _fail("BRW_E_ROLE_RECONCILIATION")
    if status.role_state == "expired":
        _fail("BRW_E_ROLE_EXPIRED")
    if status.next_role is None or status.role_state != "executable":
        _fail("BRW_E_ROLE_STATE")
    mutable = _verified_role_mutation_context(
        observed,
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        observed_at=observed_at,
    )
    directory, plan, _confirmation, _linkage, _source, _bridge, _replayed, status = mutable
    if status.reconciliation_required or status.role_state == "in_progress":
        _fail("BRW_E_ROLE_RECONCILIATION")
    if status.role_state == "expired":
        _fail("BRW_E_ROLE_EXPIRED")
    if status.next_role is None or status.role_state != "executable":
        _fail("BRW_E_ROLE_STATE")
    bridge_confirmation = _bridge_role_confirmation(_bridge, status.next_role)
    if bridge_confirmation is None:
        _fail("BRW_E_ROLE_BINDING")
    if _now() >= bridge_confirmation.expires_at:
        _fail("BRW_E_ROLE_EXPIRED")
    try:
        execute_product_role(
            config=config,
            repository_root=repository_root,
            bridge_root=directory / "bridge",
            runtime_root=runtime_root,
            bridge_run_id=plan.bridge_run_id,
            role=status.next_role,
            auth_mode=plan.auth_mode,
            codex_binary=codex_binary,
        )
    except (BridgeProductError, BridgeStorageError, OSError, ValueError) as exc:
        try:
            reread = _verified_role_workflow(
                config=config,
                repository_root=repository_root,
                workflow_root=workflow_root,
                workflow_id=workflow_id,
                pure_read=True,
            )
            reread_status = reread[7]
            if reread_status.reconciliation_required or reread_status.role_state == "in_progress":
                raise BoundedRiverReviewWorkflowError("BRW_E_ROLE_RECONCILIATION") from exc
            if reread_status.role_state == "expired":
                raise BoundedRiverReviewWorkflowError("BRW_E_ROLE_EXPIRED") from exc
        except BoundedRiverReviewWorkflowError as reread_error:
            if str(reread_error) in {
                "BRW_E_ROLE_RECONCILIATION",
                "BRW_E_ROLE_EXPIRED",
            }:
                raise
        raise BoundedRiverReviewWorkflowError("BRW_E_ROLE_EXECUTION") from exc
    return bounded_river_review_workflow_status(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
    )


def _project_report_writer_evidence(
    result: BridgeRoleResultV1,
    *,
    bridge_run_id: str,
    auth_mode: RuntimeAuthModeV1,
) -> tuple[BoundedRiverReviewReportWriterEvidenceV1, ...]:
    if (
        result.output.role is not BridgeRole.REPORT_WRITER
        or result.output.bridge_run_id != bridge_run_id
        or result.output.auth_mode is not auth_mode
    ):
        _fail("BRW_E_REPORT_WRITER")
    references = {
        item.evidence_id: item.evidence_sha256 for item in result.output.evidence_references
    }
    pairs = tuple(
        BoundedRiverReviewReportWriterEvidenceV1(
            conclusion_code=claim.conclusion_code,
            referenced_evidence_sha256=references[evidence_id],
        )
        for claim in (*result.output.conclusions, *result.output.uncertainties)
        for evidence_id in claim.evidence_ids
    )
    if not pairs:
        _fail("BRW_E_REPORT_WRITER")
    return pairs


def _report_writer_additive_evidence(
    bridge: VerifiedBridgeRead,
    *,
    completed_roles: tuple[BridgeRole, ...],
    plan: BoundedRiverReviewWorkflowPlanV1,
) -> tuple[BoundedRiverReviewReportWriterEvidenceV1, ...]:
    result_name = role_artifact_name(BridgeRole.REPORT_WRITER, "result")
    result_present = result_name in bridge.artifacts
    writer_completed = BridgeRole.REPORT_WRITER in completed_roles
    if result_present != writer_completed:
        _fail("BRW_E_REPORT_WRITER")
    if not result_present:
        return ()
    try:
        parsed = parse_bridge_canonical_model(
            bridge.artifact_bytes(result_name),
            BridgeRoleResultV1,
        )
        return _project_report_writer_evidence(
            parsed,
            bridge_run_id=plan.bridge_run_id,
            auth_mode=plan.auth_mode,
        )
    except (BridgeStorageError, KeyError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_REPORT_WRITER") from exc


def bounded_river_review_report_view(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
) -> BoundedRiverReviewReportViewV1:
    """Project a linked review without parser, calculator, provider, model, or writes."""

    try:
        root = _workflow_root(
            repository_root,
            workflow_root,
            create=False,
            pure_read=True,
        )
    except (OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_STORAGE") from exc
    directory = _workflow_directory(root, workflow_id)
    plan = _read_plan(directory, workflow_id=workflow_id)
    preparation = _read_preparation(directory)
    if sha256_bytes(canonical_json_bytes(preparation)) != plan.preparation_sha256:
        _fail("BRW_E_PLAN_BINDING")
    confirmation = _read_confirmation(directory)
    _verify_confirmation_binding(plan, preparation, confirmation)
    if not _entry_exists(directory / "linkage.json"):
        _fail("BRW_E_LINKAGE")
    linkage = _read_linkage(directory)
    _verify_linkage_plan_binding(plan, confirmation, linkage)

    source_read, source_revision_root = _read_source_terminal_for_view(
        config,
        linkage.source_run_id,
    )
    if source_read.run_id != linkage.source_run_id:
        _fail("BRW_E_SOURCE_BINDING")
    try:
        bridge = _bridge_read(directory, plan)
        if bridge is None:
            _fail("BRW_E_BRIDGE")
        replayed = replay_bridge(bridge)
        bridge_plan = _verified_bridge_run_plan(bridge, plan)
    except (BridgeReplayError, BridgeStorageError, OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_BRIDGE") from exc
    _verify_linkage_storage_binding(
        directory,
        plan,
        confirmation,
        linkage,
        source_read,
        bridge,
    )
    source_context, verified_source = _replay_verified_source_for_view(
        source_read,
        source_revision_root=source_revision_root,
    )
    try:
        stored_source = parse_bridge_canonical_model(
            bridge.artifact_bytes("source_context.json"),
            BridgeSourceContextV1,
        )
    except (BridgeStorageError, KeyError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowError("BRW_E_BRIDGE_BINDING") from exc
    if (
        stored_source != source_context
        or bridge_plan.source != stored_source.source
        or bridge.pointer.auth_mode is not plan.auth_mode
    ):
        _fail("BRW_E_BRIDGE_BINDING")
    if (
        source_context.source.source_terminal_run_id != linkage.source_run_id
        or source_context.source.source_candidate_sha256 != confirmation.candidate_sha256
        or verified_source.confirmation.confirmation_sha256 != confirmation.confirmation_sha256
        or hashlib.sha256(verified_source.source_bytes).hexdigest() != plan.source_sha256
    ):
        _fail("BRW_E_SOURCE_BINDING")

    try:
        bindings = _verified_role_confirmation_bindings(
            directory,
            plan,
            confirmation,
            linkage,
            bridge,
            replayed,
        )
        report_bytes = source_read.payload_bytes("final_report.json")
        report_payloads = tuple(
            item
            for item in source_read.payloads
            if item.inventory.logical_name == "final_report.json"
        )
        if (
            len(report_payloads) != 1
            or canonical_json_bytes(verified_source.report) != report_bytes
            or report_payloads[0].inventory.sha256 != sha256_bytes(report_bytes)
        ):
            _fail("BRW_E_REPORT_BINDING")
        status = _status_from_replayed_bridge(
            plan,
            bridge,
            replayed,
            confirmation_sha256=confirmation.confirmation_sha256,
            source_terminal_manifest_sha256=source_read.manifest_sha256,
            role_bindings=set(bindings),
        )
        writer_evidence = _report_writer_additive_evidence(
            bridge,
            completed_roles=replayed.completed_roles,
            plan=plan,
        )
        return BoundedRiverReviewReportViewV1(
            workflow_id=plan.workflow_id,
            state=status.state,
            bridge_mode=plan.auth_mode,
            bridge_status=replayed.status,
            completed_roles=replayed.completed_roles,
            source_run_id=linkage.source_run_id,
            bridge_run_id=linkage.bridge_run_id,
            plan_sha256=plan.plan_sha256,
            confirmation_sha256=confirmation.confirmation_sha256,
            linkage_sha256=linkage.linkage_sha256,
            source_terminal_manifest_sha256=source_read.manifest_sha256,
            source_terminal_inventory_sha256=source_read.manifest.inventory_sha256,
            bridge_manifest_sha256=bridge.manifest.manifest_sha256,
            bridge_inventory_sha256=bridge.manifest.inventory_sha256,
            final_report_artifact_sha256=sha256_bytes(report_bytes),
            report_writer_additive_evidence=writer_evidence,
            final_report=verified_source.report,
        )
    except (BridgeReplayError, BridgeStorageError, KeyError, OSError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowError):
            raise
        raise BoundedRiverReviewWorkflowError("BRW_E_REPORT_BINDING") from exc


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
    "bounded_river_review_report_view",
    "bounded_river_review_role_confirmation_binding_sha256",
    "bounded_river_review_role_request_preview",
    "bounded_river_review_workflow_status",
    "confirm_bounded_river_review_role_request",
    "confirm_bounded_river_review_workflow",
    "execute_bounded_river_review_role",
    "prepare_bounded_river_review_workflow",
    "replay_bounded_river_review_workflow",
    "resume_bounded_river_review_workflow",
    "run_bounded_river_review_workflow",
]
