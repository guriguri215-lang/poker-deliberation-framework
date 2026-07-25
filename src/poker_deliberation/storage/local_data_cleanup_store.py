"""Durable local-only P2-027B cleanup root and quarantine storage."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from poker_deliberation.local_data_cleanup_canonical import (
    ROOT_IDENTITY_DOMAIN,
    TREE_IDENTITY_DOMAIN,
    canonical_cleanup_bytes,
    canonical_cleanup_sha256,
    cleanup_approval_binding_sha256,
    cleanup_manifest_sha256,
    cleanup_plan_sha256,
    cleanup_pointer_sha256,
    cleanup_receipt_sha256,
    cleanup_root_marker_sha256,
    cleanup_tombstone_sha256,
    cleanup_transaction_sha256,
    parse_cleanup_model,
    tree_inventory_sha256,
)
from poker_deliberation.local_data_cleanup_models import (
    DEFAULT_CLEANUP_LIMITS,
    CleanupActionKind,
    CleanupApprovalBindingV1,
    CleanupCurrentPointerV1,
    CleanupDurabilityEvidenceV1,
    CleanupExecutionResultV1,
    CleanupFailureCode,
    CleanupLimitsV1,
    CleanupManifestV1,
    CleanupPlanV1,
    CleanupReceiptV1,
    CleanupRootInitializationOutcomeV1,
    CleanupRootInspectionV1,
    CleanupRootMarkerV1,
    CleanupState,
    CleanupTombstoneV1,
    CleanupTransactionV1,
    ProductRunSourceV1,
    TreeInventoryEntryV1,
    TreeInventoryV1,
    cleanup_failure,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    platform_adapter,
)
from poker_deliberation.storage.revision_lock import (
    LockReleaseError,
    verify_directory,
    verify_regular_single_link,
)
from poker_deliberation.storage.revision_store import ExistingRunAuthorityV1
from poker_deliberation.storage.terminal_models import ProductRunError, VerifiedRunReadV2
from poker_deliberation.storage.terminal_store import TerminalRunStore

FaultInjector = Callable[[str], None]
_ROOT_ENTRIES = frozenset({"ownership.json", ".cleanup-control", "runs", "quarantine", "deleting"})
_RUN_CONTROL_ENTRIES = frozenset({"transactions", "revisions", "current.json"})


class CleanupStorageError(ValueError):
    """A redacted typed cleanup storage failure."""

    def __init__(self, failure: object) -> None:
        super().__init__(getattr(getattr(failure, "code", None), "value", "cleanup_failure"))
        self.failure = failure


def _fail(
    code: CleanupFailureCode,
    *,
    run_id_sha256: str | None = None,
    plan_sha256: str | None = None,
    transaction_id: str | None = None,
    filesystem_effect: Literal[
        "none",
        "journal_only",
        "source_moved",
        "delete_staging_moved",
        "partial_delete",
        "control_published",
    ] = "none",
    domain_effect: Literal[
        "none",
        "current_unchanged",
        "current_may_have_advanced",
        "current_advanced",
    ] = "none",
) -> CleanupStorageError:
    return CleanupStorageError(
        cleanup_failure(
            code,
            run_id_sha256=run_id_sha256,
            plan_sha256=plan_sha256,
            transaction_id=transaction_id,
            filesystem_effect=filesystem_effect,
            domain_effect=domain_effect,
        )
    )


def _fault(injector: FaultInjector | None, hook: str) -> None:
    if injector is not None:
        injector(hook)


def _resolved_absolute(path: Path, field_name: str) -> Path:
    if not path.is_absolute():
        raise CanonicalStorageError(f"{field_name} must be absolute")
    resolved = path.resolve(strict=False)
    parts = tuple(part.casefold() for part in resolved.parts)
    if (
        resolved == Path.home().resolve()
        or resolved == Path.cwd().resolve()
        or ".git" in parts
        or "user_materials" in parts
        or any(
            parts[index : index + 2] == ("tmp", "goals") for index in range(max(0, len(parts) - 1))
        )
    ):
        raise CanonicalStorageError(f"{field_name} is an excluded root")
    return resolved


def _overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _root_identity(path: Path) -> str:
    verify_directory(path)
    resolved = path.resolve(strict=True)
    info = resolved.stat()
    return canonical_cleanup_sha256(
        ROOT_IDENTITY_DOMAIN,
        {
            "adapter": platform_adapter(),
            "resolved_path": unicodedata.normalize("NFC", str(resolved)),
            "st_dev": info.st_dev,
            "st_ino": info.st_ino,
        },
    )


def _directory_sync(path: Path) -> Literal["confirmed", "unavailable"]:
    if os.name == "nt":
        return "unavailable"
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return "confirmed"


def _write_exclusive(path: Path, data: bytes, *, max_bytes: int) -> None:
    if not data or len(data) > max_bytes:
        raise CanonicalStorageError("cleanup control artifact exceeds its byte limit")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    verify_regular_single_link(path)
    if path.read_bytes() != data:
        raise CanonicalStorageError("cleanup control artifact reread mismatch")


def _read_control(path: Path, model: type[Any], *, max_bytes: int) -> tuple[Any, bytes]:
    info = verify_regular_single_link(path)
    if info.st_size > max_bytes:
        raise CanonicalStorageError("cleanup control artifact exceeds its byte limit")
    data = path.read_bytes()
    return parse_cleanup_model(data, model, max_bytes=max_bytes), data


def _idle_durability(
    *,
    reconciliation: Literal["confirmed", "required"] = "confirmed",
) -> CleanupDurabilityEvidenceV1:
    return CleanupDurabilityEvidenceV1(
        platform_adapter=cast(Any, platform_adapter()),
        journal_file_sync="not_attempted",
        effect_rename="not_attempted",
        control_file_sync="not_attempted",
        directory_sync="not_attempted",
        pointer_replace="not_attempted",
        reconciliation=reconciliation,
    )


def inspect_cleanup_root(
    cleanup_root: Path,
    *,
    expected_product_root_identity_sha256: str | None = None,
    expected_product_ownership_marker_sha256: str | None = None,
    max_artifact_bytes: int = 1_000_000,
) -> CleanupRootInspectionV1:
    """Inspect a cleanup root without creating or repairing anything."""

    root = _resolved_absolute(cleanup_root, "cleanup_root")
    if not root.exists():
        return CleanupRootInspectionV1(status="uninitialized")
    try:
        verify_directory(root)
        entries = {entry.name: entry for entry in root.iterdir()}
        recognized = tuple(sorted(set(entries) & _ROOT_ENTRIES, key=lambda item: item.encode()))
        if not entries:
            return CleanupRootInspectionV1(status="uninitialized")
        if set(entries) != _ROOT_ENTRIES:
            return CleanupRootInspectionV1(
                status="corrupt" if set(entries) - _ROOT_ENTRIES else "incomplete",
                recognized_relative_paths=cast(Any, recognized),
            )
        for name in _ROOT_ENTRIES - {"ownership.json"}:
            verify_directory(entries[name])
        marker, marker_bytes = _read_control(
            entries["ownership.json"],
            CleanupRootMarkerV1,
            max_bytes=max_artifact_bytes,
        )
        marker = cast(CleanupRootMarkerV1, marker)
        if marker.cleanup_root_identity_sha256 != _root_identity(
            root
        ) or cleanup_root_marker_sha256(marker) != canonical_cleanup_sha256(
            "poker-local-data-cleanup-root-marker-v1",
            parse_cleanup_model(
                marker_bytes,
                CleanupRootMarkerV1,
                max_bytes=max_artifact_bytes,
            ),
        ):
            raise CanonicalStorageError("cleanup root marker identity mismatch")
        if (
            expected_product_root_identity_sha256 is not None
            and marker.product_root_identity_sha256 != expected_product_root_identity_sha256
        ):
            raise CanonicalStorageError("cleanup root product identity mismatch")
        if (
            expected_product_ownership_marker_sha256 is not None
            and marker.product_ownership_marker_sha256 != expected_product_ownership_marker_sha256
        ):
            raise CanonicalStorageError("cleanup root ownership binding mismatch")
        return CleanupRootInspectionV1(
            status="initialized",
            root_id=marker.root_id,
            marker_sha256=cleanup_root_marker_sha256(marker),
            recognized_relative_paths=cast(
                Any,
                tuple(sorted(_ROOT_ENTRIES, key=lambda item: item.encode())),
            ),
        )
    except (CanonicalStorageError, OSError, ValidationError):
        return CleanupRootInspectionV1(status="corrupt")


def initialize_cleanup_root(
    cleanup_root: Path,
    terminal_store: TerminalRunStore,
    *,
    existing_run_id: str,
    root_id: str,
    initialized_at: datetime,
    limits: CleanupLimitsV1 = DEFAULT_CLEANUP_LIMITS,
    fault_injector: FaultInjector | None = None,
) -> CleanupRootInitializationOutcomeV1:
    """Explicitly initialize a dedicated same-volume cleanup root."""

    root = _resolved_absolute(cleanup_root, "cleanup_root")
    product_root = _resolved_absolute(terminal_store.revision_root, "product_root")
    legacy_root = _resolved_absolute(terminal_store.legacy_runs_root, "legacy_root")
    if _overlap(root, product_root) or _overlap(root, legacy_root):
        raise _fail(CleanupFailureCode.PATH_CONFINEMENT_FAILED)
    created = False
    authority: ExistingRunAuthorityV1 | None = None
    try:
        authority = terminal_store.foundation.acquire_existing_run_authority(existing_run_id)
        authority.revalidate()
        product_identity = authority.revision_root_identity_sha256
        inspection = inspect_cleanup_root(
            root,
            expected_product_root_identity_sha256=product_identity,
            expected_product_ownership_marker_sha256=authority.ownership_marker_sha256,
            max_artifact_bytes=limits.maximum_control_artifact_bytes,
        )
        if inspection.status == "initialized":
            if inspection.root_id != root_id:
                raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED)
            return CleanupRootInitializationOutcomeV1(
                outcome_kind="already_initialized",
                root_id=root_id,
                marker_sha256=inspection.marker_sha256,
                filesystem_effect="none",
                durability=_idle_durability(),
            )
        if inspection.status not in {"uninitialized"}:
            raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED)
        if not root.parent.exists():
            raise _fail(CleanupFailureCode.PATH_CONFINEMENT_FAILED)
        verify_directory(root.parent)
        if root.parent.stat().st_dev != authority.run_path.stat().st_dev:
            raise _fail(CleanupFailureCode.CROSS_VOLUME)
        if root.exists() and any(root.iterdir()):
            raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED)
        _fault(fault_injector, "initialize.before_root")
        if not root.exists():
            root.mkdir()
            created = True
        verify_directory(root)
        if root.stat().st_dev != authority.run_path.stat().st_dev:
            raise _fail(CleanupFailureCode.CROSS_VOLUME)
        for name in (".cleanup-control", "runs", "quarantine", "deleting"):
            _fault(fault_injector, f"initialize.before_mkdir.{name}")
            (root / name).mkdir()
            verify_directory(root / name)
        marker = CleanupRootMarkerV1(
            root_id=root_id,
            cleanup_root_identity_sha256=_root_identity(root),
            product_root_identity_sha256=product_identity,
            product_ownership_marker_sha256=authority.ownership_marker_sha256,
            limits=limits,
            initialized_at=initialized_at,
        )
        _fault(fault_injector, "initialize.before_marker")
        _write_exclusive(
            root / "ownership.json",
            canonical_cleanup_bytes(marker),
            max_bytes=limits.maximum_control_artifact_bytes,
        )
        marker_sha = cleanup_root_marker_sha256(marker)
        if (
            inspect_cleanup_root(
                root,
                expected_product_root_identity_sha256=product_identity,
                expected_product_ownership_marker_sha256=authority.ownership_marker_sha256,
                max_artifact_bytes=limits.maximum_control_artifact_bytes,
            ).marker_sha256
            != marker_sha
        ):
            raise CanonicalStorageError("initialized cleanup root did not reread")
        directory_state = _directory_sync(root)
        return CleanupRootInitializationOutcomeV1(
            outcome_kind="initialized",
            root_id=root_id,
            marker_sha256=marker_sha,
            filesystem_effect="control_only",
            durability=CleanupDurabilityEvidenceV1(
                platform_adapter=cast(Any, platform_adapter()),
                journal_file_sync="not_attempted",
                effect_rename="not_attempted",
                control_file_sync="confirmed",
                directory_sync=directory_state,
                pointer_replace="not_attempted",
                reconciliation="confirmed",
            ),
        )
    except CleanupStorageError:
        raise
    except Exception as exc:
        if created or root.exists():
            return CleanupRootInitializationOutcomeV1(
                outcome_kind="reconciliation_required",
                root_id=root_id,
                marker_sha256=None,
                filesystem_effect="control_only",
                durability=_idle_durability(reconciliation="required"),
            )
        raise _fail(CleanupFailureCode.INTERNAL_INVARIANT_ERROR) from exc
    finally:
        if authority is not None:
            try:
                authority.release()
            except LockReleaseError as exc:
                raise _fail(CleanupFailureCode.EFFECT_UNKNOWN) from exc


def scan_cleanup_tree(
    root: Path,
    *,
    run_id_sha256: str,
    limits: CleanupLimitsV1 = DEFAULT_CLEANUP_LIMITS,
) -> TreeInventoryV1:
    """Read one explicit tree without following links or using mtime."""

    verify_directory(root)
    entries: list[TreeInventoryEntryV1] = []
    total_bytes = 0

    def walk(directory: Path) -> None:
        nonlocal total_bytes
        children = list(directory.iterdir())
        aliases: set[str] = set()
        for child in children:
            normalized = unicodedata.normalize("NFC", child.name)
            alias = normalized.casefold()
            if normalized != child.name or alias in aliases:
                raise _fail(
                    CleanupFailureCode.ALIAS_CONFLICT,
                    run_id_sha256=run_id_sha256,
                )
            aliases.add(alias)
        for child in sorted(children, key=lambda item: item.name.encode("utf-8")):
            relative = child.relative_to(root).as_posix()
            info = child.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
                raise _fail(
                    CleanupFailureCode.LINK_OR_REPARSE_DETECTED,
                    run_id_sha256=run_id_sha256,
                )
            if stat.S_ISDIR(info.st_mode):
                identity = canonical_cleanup_sha256(
                    TREE_IDENTITY_DOMAIN,
                    {
                        "entry_kind": "directory",
                        "relative_path": relative,
                        "st_dev": info.st_dev,
                        "st_ino": info.st_ino,
                    },
                )
                entries.append(
                    TreeInventoryEntryV1(
                        relative_path=relative,
                        entry_kind="directory",
                        size_bytes=0,
                        identity_sha256=identity,
                    )
                )
                walk(child)
            elif stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise _fail(
                        CleanupFailureCode.HARDLINK_DETECTED,
                        run_id_sha256=run_id_sha256,
                    )
                if info.st_size > limits.maximum_target_bytes - total_bytes:
                    raise _fail(
                        CleanupFailureCode.CAPACITY_EXCEEDED,
                        run_id_sha256=run_id_sha256,
                    )
                data = child.read_bytes()
                after = child.stat()
                if len(data) != info.st_size or (after.st_dev, after.st_ino, after.st_size) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                ):
                    raise _fail(
                        CleanupFailureCode.STALE_SOURCE,
                        run_id_sha256=run_id_sha256,
                    )
                total_bytes += len(data)
                entries.append(
                    TreeInventoryEntryV1(
                        relative_path=relative,
                        entry_kind="file",
                        size_bytes=len(data),
                        content_sha256=hashlib.sha256(data).hexdigest(),
                        identity_sha256=canonical_cleanup_sha256(
                            TREE_IDENTITY_DOMAIN,
                            {
                                "entry_kind": "file",
                                "relative_path": relative,
                                "st_dev": info.st_dev,
                                "st_ino": info.st_ino,
                                "size_bytes": info.st_size,
                            },
                        ),
                    )
                )
            else:
                raise _fail(
                    CleanupFailureCode.PATH_CONFINEMENT_FAILED,
                    run_id_sha256=run_id_sha256,
                )
            if len(entries) > limits.maximum_tree_entries:
                raise _fail(
                    CleanupFailureCode.CAPACITY_EXCEEDED,
                    run_id_sha256=run_id_sha256,
                )

    walk(root)
    if not entries:
        raise _fail(CleanupFailureCode.CANDIDATE_INELIGIBLE, run_id_sha256=run_id_sha256)
    ordered = tuple(sorted(entries, key=lambda item: item.relative_path.encode("utf-8")))
    return TreeInventoryV1(
        run_id_sha256=run_id_sha256,
        entries=ordered,
        entry_count=len(ordered),
        total_bytes=total_bytes,
    )


class LocalDataCleanupStore:
    """Cleanup control store bound to one product terminal store."""

    def __init__(
        self,
        cleanup_root: Path,
        terminal_store: TerminalRunStore,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        self.cleanup_root = _resolved_absolute(cleanup_root, "cleanup_root")
        self.terminal_store = terminal_store
        self.fault_injector = fault_injector

    def marker(self) -> tuple[CleanupRootMarkerV1, str]:
        marker, _data = _read_control(
            self.cleanup_root / "ownership.json",
            CleanupRootMarkerV1,
            max_bytes=DEFAULT_CLEANUP_LIMITS.maximum_control_artifact_bytes,
        )
        marker = cast(CleanupRootMarkerV1, marker)
        if marker.cleanup_root_identity_sha256 != _root_identity(self.cleanup_root):
            raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED)
        return marker, cleanup_root_marker_sha256(marker)

    def _run_control(self, run_hash: str) -> Path:
        return self.cleanup_root / "runs" / run_hash

    def read_current(
        self,
        run_hash: str,
    ) -> (
        tuple[
            CleanupCurrentPointerV1,
            CleanupManifestV1,
            CleanupReceiptV1,
            CleanupTombstoneV1,
        ]
        | None
    ):
        marker, _marker_sha = self.marker()
        control = self._run_control(run_hash)
        if not control.exists():
            return None
        verify_directory(control)
        entries = {item.name: item for item in control.iterdir()}
        if not set(entries) <= _RUN_CONTROL_ENTRIES or "current.json" not in entries:
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        pointer, pointer_bytes = _read_control(
            entries["current.json"],
            CleanupCurrentPointerV1,
            max_bytes=marker.limits.maximum_control_artifact_bytes,
        )
        pointer = cast(CleanupCurrentPointerV1, pointer)
        if pointer.run_id_sha256 != run_hash:
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        revision = self.cleanup_root / pointer.revision_relative_path
        verify_directory(revision)
        revision_entries = {item.name: item for item in revision.iterdir()}
        if set(revision_entries) != {
            "transaction.json",
            "manifest.json",
            "receipt.json",
            "tombstone.json",
        }:
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        manifest, _manifest_bytes = _read_control(
            revision_entries["manifest.json"],
            CleanupManifestV1,
            max_bytes=marker.limits.maximum_control_artifact_bytes,
        )
        receipt, _receipt_bytes = _read_control(
            revision_entries["receipt.json"],
            CleanupReceiptV1,
            max_bytes=marker.limits.maximum_control_artifact_bytes,
        )
        tombstone, _tombstone_bytes = _read_control(
            revision_entries["tombstone.json"],
            CleanupTombstoneV1,
            max_bytes=marker.limits.maximum_control_artifact_bytes,
        )
        manifest = cast(CleanupManifestV1, manifest)
        receipt = cast(CleanupReceiptV1, receipt)
        tombstone = cast(CleanupTombstoneV1, tombstone)
        if (
            cleanup_pointer_sha256(pointer) != cleanup_pointer_sha256(pointer_bytes)
            or pointer.manifest_sha256 != cleanup_manifest_sha256(manifest)
            or pointer.receipt_sha256 != cleanup_receipt_sha256(receipt)
            or pointer.tombstone_sha256 != cleanup_tombstone_sha256(tombstone)
            or manifest.receipt_sha256 != pointer.receipt_sha256
            or manifest.tombstone_sha256 != pointer.tombstone_sha256
            or manifest.state != pointer.state
            or receipt.result_state != pointer.state
            or tombstone.state != pointer.state
        ):
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        if (
            _read_control(
                control / "current.json",
                CleanupCurrentPointerV1,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )[1]
            != pointer_bytes
        ):
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        return pointer, manifest, receipt, tombstone

    @staticmethod
    def _same_product_current(plan: CleanupPlanV1, current: VerifiedRunReadV2) -> bool:
        source = plan.source
        return isinstance(source, ProductRunSourceV1) and (
            source.run_id == current.run_id
            and source.current_revision == current.revision
            and source.current_transaction_id == current.transaction_id
            and source.current_pointer_sha256 == current.current_pointer_sha256
            and source.manifest_sha256 == current.manifest_sha256
            and source.inventory_sha256 == current.inventory_sha256
            and source.completion_marker_sha256 == current.completion_marker_sha256
            and current.pointer.publication_kind == "product_terminal"
        )

    def publish_quarantine(
        self,
        plan: CleanupPlanV1,
        approval: CleanupApprovalBindingV1,
        *,
        transaction_id: str,
        effect_at: datetime,
    ) -> CleanupExecutionResultV1:
        """Publish one authorized quarantine transition under product authority."""

        if not isinstance(plan.source, ProductRunSourceV1):
            raise _fail(CleanupFailureCode.INVALID_PLAN)
        run_id = plan.source.run_id
        run_hash = plan.source.run_id_sha256
        plan_sha = cleanup_plan_sha256(plan)
        approval_sha = cleanup_approval_binding_sha256(approval)
        marker, marker_sha = self.marker()
        if (
            marker.root_id != plan.cleanup_root_id
            or marker_sha != plan.cleanup_root_marker_sha256
            or plan.limits != marker.limits
            or plan.expected_cleanup_revision != 0
            or plan.expected_cleanup_pointer_sha256 is not None
        ):
            raise _fail(
                CleanupFailureCode.STALE_CLEANUP_REVISION,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
        authority: ExistingRunAuthorityV1 | None = None
        journal_published = False
        source_moved = False
        try:
            authority = self.terminal_store.foundation.acquire_existing_run_authority(run_id)
            current = self.terminal_store.read_current(run_id)
            if not self._same_product_current(plan, current):
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            if self.read_current(run_hash) is not None:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            source = authority.run_path
            inventory = scan_cleanup_tree(source, run_id_sha256=run_hash, limits=marker.limits)
            source_tree_sha = tree_inventory_sha256(inventory)
            if source_tree_sha != plan.tree_inventory_sha256:
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            destination = self.cleanup_root / plan.actions[0].destination_relative_path
            if destination.exists() or source.stat().st_dev != destination.parent.stat().st_dev:
                raise _fail(
                    (
                        CleanupFailureCode.IDEMPOTENCY_CONFLICT
                        if destination.exists()
                        else CleanupFailureCode.CROSS_VOLUME
                    ),
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            authority.revalidate()
            locked_current = self.terminal_store.read_current(run_id)
            if not self._same_product_current(plan, locked_current):
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            locked_inventory = scan_cleanup_tree(
                source,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(locked_inventory) != source_tree_sha:
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            control = self._run_control(run_hash)
            transaction_root = control / "transactions" / transaction_id
            revision = control / "revisions" / f"r1-{transaction_id}"
            if control.exists():
                raise _fail(
                    CleanupFailureCode.IDEMPOTENCY_CONFLICT,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            control.mkdir()
            (control / "transactions").mkdir()
            (control / "revisions").mkdir()
            transaction_root.mkdir()
            transaction_base: dict[str, Any] = {
                "schema_version": "1.0.0",
                "run_id_sha256": run_hash,
                "transaction_id": transaction_id,
                "proposed_revision": 1,
                "expected_revision": 0,
                "expected_pointer_sha256": None,
                "action_kind": CleanupActionKind.QUARANTINE_PRODUCT_RUN,
                "plan_sha256": plan_sha,
                "approval_binding_sha256": approval_sha,
                "source_tree_sha256": source_tree_sha,
                "created_at": effect_at,
            }
            transaction = CleanupTransactionV1(
                **transaction_base,
                transaction_sha256=cleanup_transaction_sha256(transaction_base),
            )
            _fault(self.fault_injector, "quarantine.before_journal")
            _write_exclusive(
                transaction_root / "transaction.json",
                canonical_cleanup_bytes(transaction),
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )
            journal_published = True
            _directory_sync(transaction_root)
            _fault(self.fault_injector, "quarantine.before_effect")
            os.replace(source, destination)
            source_moved = True
            _fault(self.fault_injector, "quarantine.after_effect")
            if source.exists():
                raise CanonicalStorageError("quarantine source remained after rename")
            destination_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(destination_inventory) != source_tree_sha:
                raise CanonicalStorageError("quarantine destination tree changed")
            revision.mkdir()
            receipt = CleanupReceiptV1(
                action_kind=CleanupActionKind.QUARANTINE_PRODUCT_RUN,
                run_id_sha256=run_hash,
                transaction_id=transaction_id,
                plan_sha256=plan_sha,
                approval_binding_sha256=approval_sha,
                source_tree_sha256=source_tree_sha,
                result_state=CleanupState.QUARANTINED,
                effect_started_at=effect_at,
                committed_at=effect_at,
                durability=CleanupDurabilityEvidenceV1(
                    platform_adapter=cast(Any, platform_adapter()),
                    journal_file_sync="confirmed",
                    effect_rename="confirmed",
                    control_file_sync="confirmed",
                    directory_sync=_directory_sync(destination.parent),
                    pointer_replace="not_attempted",
                    reconciliation="confirmed",
                ),
            )
            receipt_sha = cleanup_receipt_sha256(receipt)
            tombstone = CleanupTombstoneV1(
                run_id_sha256=run_hash,
                source_pointer_sha256=plan.source.current_pointer_sha256,
                source_manifest_sha256=plan.source.manifest_sha256,
                quarantine_tree_sha256=source_tree_sha,
                state=CleanupState.QUARANTINED,
                receipt_sha256=receipt_sha,
                quarantine_entered_at=effect_at,
                receipt_retain_until=effect_at + timedelta(days=365),
            )
            tombstone_sha = cleanup_tombstone_sha256(tombstone)
            manifest = CleanupManifestV1(
                run_id_sha256=run_hash,
                revision=1,
                transaction_id=transaction_id,
                state=CleanupState.QUARANTINED,
                plan_sha256=plan_sha,
                approval_binding_sha256=approval_sha,
                source_tree_sha256=source_tree_sha,
                receipt_sha256=receipt_sha,
                tombstone_sha256=tombstone_sha,
                created_at=effect_at,
            )
            manifest_sha = cleanup_manifest_sha256(manifest)
            pointer = CleanupCurrentPointerV1(
                run_id_sha256=run_hash,
                revision=1,
                transaction_id=transaction_id,
                revision_relative_path=f"runs/{run_hash}/revisions/r1-{transaction_id}",
                state=CleanupState.QUARANTINED,
                manifest_sha256=manifest_sha,
                receipt_sha256=receipt_sha,
                tombstone_sha256=tombstone_sha,
                published_at=effect_at,
            )
            for name, value in (
                ("transaction.json", transaction),
                ("manifest.json", manifest),
                ("receipt.json", receipt),
                ("tombstone.json", tombstone),
            ):
                _write_exclusive(
                    revision / name,
                    canonical_cleanup_bytes(value),
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
            _directory_sync(revision)
            pointer_bytes = canonical_cleanup_bytes(pointer)
            temporary = control / f"current.{transaction_id}.tmp"
            _write_exclusive(
                temporary,
                pointer_bytes,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )
            _fault(self.fault_injector, "quarantine.before_pointer_replace")
            os.replace(temporary, control / "current.json")
            _fault(self.fault_injector, "quarantine.after_pointer_replace")
            if (control / "current.json").read_bytes() != pointer_bytes:
                raise CanonicalStorageError("cleanup current pointer reread mismatch")
            _directory_sync(control)
            verified = self.read_current(run_hash)
            if verified is None or verified[0] != pointer:
                raise CanonicalStorageError("cleanup committed state did not verify")
            return CleanupExecutionResultV1(
                outcome_kind="committed",
                run_id_sha256=run_hash,
                execution_id=plan.execution_id,
                idempotency_key=plan.idempotency_key,
                transaction_id=transaction_id,
                plan_sha256=plan_sha,
                cleanup_revision=1,
                cleanup_pointer_sha256=cleanup_pointer_sha256(pointer),
                receipt=receipt,
                receipt_sha256=receipt_sha,
                tombstone=tombstone,
                tombstone_sha256=tombstone_sha,
            )
        except CleanupStorageError:
            raise
        except ProductRunError as exc:
            code = (
                CleanupFailureCode.RUN_LOCKED
                if exc.failure.code.value == "run_locked"
                else CleanupFailureCode.STALE_SOURCE
            )
            raise _fail(
                code,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            ) from exc
        except Exception as exc:
            if source_moved:
                code = CleanupFailureCode.RECONCILIATION_REQUIRED
                effect = "source_moved"
            elif journal_published:
                code = CleanupFailureCode.RECONCILIATION_REQUIRED
                effect = "journal_only"
            else:
                code = CleanupFailureCode.INTERNAL_INVARIANT_ERROR
                effect = "none"
            raise _fail(
                code,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
                filesystem_effect=cast(Any, effect),
                domain_effect="current_unchanged",
            ) from exc
        finally:
            if authority is not None:
                try:
                    authority.release()
                except LockReleaseError as exc:
                    raise _fail(
                        CleanupFailureCode.EFFECT_UNKNOWN,
                        run_id_sha256=run_hash,
                        plan_sha256=plan_sha,
                        transaction_id=transaction_id,
                        filesystem_effect=("source_moved" if source_moved else "journal_only"),
                        domain_effect="current_may_have_advanced",
                    ) from exc


__all__ = [
    "CleanupStorageError",
    "LocalDataCleanupStore",
    "initialize_cleanup_root",
    "inspect_cleanup_root",
    "scan_cleanup_tree",
]
