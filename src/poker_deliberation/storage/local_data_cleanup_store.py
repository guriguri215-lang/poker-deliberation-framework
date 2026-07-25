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

from pydantic import TypeAdapter, ValidationError

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
    CleanupReconciliationReportV1,
    CleanupRootInitializationOutcomeV1,
    CleanupRootInspectionV1,
    CleanupRootMarkerV1,
    CleanupState,
    CleanupTombstoneV1,
    CleanupTransactionV1,
    ProductRunSourceV1,
    RootId,
    TreeInventoryEntryV1,
    TreeInventoryV1,
    cleanup_failure,
)
from poker_deliberation.local_data_policy import DEFAULT_LOCAL_DATA_POLICY
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    platform_adapter,
)
from poker_deliberation.storage.revision_lock import (
    LockReleaseError,
    verify_directory,
    verify_regular_single_link,
)
from poker_deliberation.storage.revision_models import RunStorageError
from poker_deliberation.storage.revision_store import (
    DetachedRunAuthorityV1,
    ExistingRunAuthorityV1,
    RunAuthorityBindingV1,
)
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


def _strict_lexists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _resolved_absolute(path: Path, field_name: str) -> Path:
    if not path.is_absolute():
        raise CanonicalStorageError(f"{field_name} must be absolute")
    absolute = Path(os.path.abspath(path))
    parts_to_check = absolute.parts
    current = Path(parts_to_check[0])
    for part in parts_to_check[1:]:
        current /= part
        if not _strict_lexists(current):
            continue
        info = current.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise CanonicalStorageError(f"{field_name} contains a link or reparse point")
    for ancestor in (absolute, *absolute.parents):
        if _strict_lexists(ancestor / ".git"):
            raise CanonicalStorageError(f"{field_name} is an excluded root inside a repository")
    resolved = absolute.resolve(strict=False)
    parts = tuple(part.casefold() for part in resolved.parts)
    if (
        resolved == Path.home().resolve()
        or resolved == Path.cwd().resolve()
        or Path.cwd().resolve() in resolved.parents
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


def _directory_sync_many(*paths: Path) -> Literal["confirmed", "unavailable"]:
    states = {_directory_sync(path) for path in paths}
    return "unavailable" if "unavailable" in states else "confirmed"


def _rollback_pre_effect_journal(
    transaction_root: Path,
    *,
    empty_directories: tuple[Path, ...] = (),
) -> bool:
    """Remove only the exact journal/scaffold created by this attempt."""

    try:
        transaction = transaction_root / "transaction.json"
        if _strict_lexists(transaction):
            verify_regular_single_link(transaction)
            transaction.unlink()
        if _strict_lexists(transaction_root):
            verify_directory(transaction_root)
            transaction_root.rmdir()
        for directory in empty_directories:
            if _strict_lexists(directory):
                verify_directory(directory)
                directory.rmdir()
        return True
    except (CanonicalStorageError, OSError):
        return False


def _require_not_cancelled(
    cancelled: Callable[[], bool] | None,
    *,
    run_id_sha256: str,
    plan_sha256: str,
    transaction_id: str,
) -> None:
    if cancelled is not None and cancelled():
        raise _fail(
            CleanupFailureCode.CANCELLED,
            run_id_sha256=run_id_sha256,
            plan_sha256=plan_sha256,
            transaction_id=transaction_id,
        )


def _write_exclusive(
    path: Path,
    data: bytes,
    *,
    max_bytes: int,
    fault_injector: FaultInjector | None = None,
    fault_hook: str | None = None,
) -> None:
    if not data or len(data) > max_bytes:
        raise CanonicalStorageError("cleanup control artifact exceeds its byte limit")
    with path.open("xb") as stream:
        stream.write(data)
        if fault_hook is not None:
            _fault(fault_injector, f"{fault_hook}.after_write")
        stream.flush()
        os.fsync(stream.fileno())
    verify_regular_single_link(path)
    if path.read_bytes() != data:
        raise CanonicalStorageError("cleanup control artifact reread mismatch")


def _read_control(path: Path, model: type[Any], *, max_bytes: int) -> tuple[Any, bytes]:
    try:
        info = verify_regular_single_link(path)
        if info.st_size > max_bytes:
            raise CanonicalStorageError("cleanup control artifact exceeds its byte limit")
        data = path.read_bytes()
    except CanonicalStorageError:
        raise
    except OSError as exc:
        raise CanonicalStorageError("cleanup control artifact read failed") from exc
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
        if _has_nondefault_windows_stream(root):
            raise CanonicalStorageError("cleanup root has an alternate data stream")
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
            if _has_nondefault_windows_stream(entries[name]):
                raise CanonicalStorageError(
                    "cleanup control directory has an alternate data stream"
                )
        if _has_nondefault_windows_stream(entries["ownership.json"]):
            raise CanonicalStorageError("cleanup ownership marker has an alternate data stream")
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

    try:
        TypeAdapter(RootId).validate_python(root_id, strict=True)
    except ValidationError:
        raise _fail(CleanupFailureCode.INVALID_PLAN) from None
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
    if _has_nondefault_windows_stream(root):
        raise _fail(
            CleanupFailureCode.PATH_CONFINEMENT_FAILED,
            run_id_sha256=run_id_sha256,
        )
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
            if _has_nondefault_windows_stream(child):
                raise _fail(
                    CleanupFailureCode.PATH_CONFINEMENT_FAILED,
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


def _capture_directory_chain(
    path: Path,
    *,
    anchor: Path,
) -> tuple[tuple[Path, int, int], ...]:
    absolute = Path(os.path.abspath(path))
    anchored_at = Path(os.path.abspath(anchor))
    if absolute != anchored_at and anchored_at not in absolute.parents:
        raise CanonicalStorageError("delete staging parent escaped cleanup root")
    current = anchored_at
    observed: list[tuple[Path, int, int]] = []
    for part in (None, *absolute.relative_to(anchored_at).parts):
        if part is not None:
            current /= part
        info = current.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
            or _has_nondefault_windows_stream(current)
        ):
            raise CanonicalStorageError("delete staging ancestor identity changed")
        observed.append((current, info.st_dev, info.st_ino))
    return tuple(observed)


def _verify_directory_chain(
    expected: tuple[tuple[Path, int, int], ...],
) -> None:
    for path, expected_dev, expected_ino in expected:
        info = path.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
            or (info.st_dev, info.st_ino) != (expected_dev, expected_ino)
            or _has_nondefault_windows_stream(path)
        ):
            raise CanonicalStorageError("delete staging ancestor identity changed")


def _unlink_inventory_tree(
    root: Path,
    inventory: TreeInventoryV1,
    *,
    expected_parent_chain: tuple[tuple[Path, int, int], ...],
    expected_root_identity: tuple[int, int],
    fault_injector: FaultInjector | None,
    progress: list[int],
    cancelled: Callable[[], bool] | None,
    plan_sha256: str,
    transaction_id: str,
) -> int:
    """Unlink one previously verified tree without traversal or link following."""

    directory_entries = {
        entry.relative_path: entry for entry in inventory.entries if entry.entry_kind == "directory"
    }

    def verify_directory_identity(
        path: Path,
        *,
        relative_path: str | None,
        expected_identity: tuple[int, int] | None = None,
    ) -> None:
        info = path.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
            or _has_nondefault_windows_stream(path)
        ):
            raise CanonicalStorageError("delete staging directory identity changed")
        if expected_identity is not None:
            if (info.st_dev, info.st_ino) != expected_identity:
                raise CanonicalStorageError("delete staging root identity changed")
            return
        if relative_path is None:
            raise CanonicalStorageError("delete staging directory identity changed")
        expected = directory_entries.get(relative_path)
        identity = canonical_cleanup_sha256(
            TREE_IDENTITY_DOMAIN,
            {
                "entry_kind": "directory",
                "relative_path": relative_path,
                "st_dev": info.st_dev,
                "st_ino": info.st_ino,
            },
        )
        if expected is None or identity != expected.identity_sha256:
            raise CanonicalStorageError("delete staging directory identity changed")

    def verify_root_and_ancestors(relative_path: str) -> None:
        _verify_directory_chain(expected_parent_chain)
        verify_directory_identity(
            root,
            relative_path=None,
            expected_identity=expected_root_identity,
        )
        parts = relative_path.split("/")
        for index in range(1, len(parts)):
            ancestor_relative = "/".join(parts[:index])
            verify_directory_identity(
                root.joinpath(*parts[:index]),
                relative_path=ancestor_relative,
            )

    deleted = 0
    ordered = sorted(
        inventory.entries,
        key=lambda item: (
            item.relative_path.count("/"),
            item.relative_path.encode("utf-8"),
        ),
        reverse=True,
    )
    for index, entry in enumerate(ordered):
        _fault(fault_injector, f"delete.before_unlink.{index}")
        _require_not_cancelled(
            cancelled,
            run_id_sha256=inventory.run_id_sha256,
            plan_sha256=plan_sha256,
            transaction_id=transaction_id,
        )
        verify_root_and_ancestors(entry.relative_path)
        target = root.joinpath(*entry.relative_path.split("/"))
        info = target.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise CanonicalStorageError("delete staging contains a link or reparse point")
        if entry.entry_kind == "file":
            identity = canonical_cleanup_sha256(
                TREE_IDENTITY_DOMAIN,
                {
                    "entry_kind": "file",
                    "relative_path": entry.relative_path,
                    "st_dev": info.st_dev,
                    "st_ino": info.st_ino,
                    "size_bytes": info.st_size,
                },
            )
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size != entry.size_bytes
                or identity != entry.identity_sha256
                or _has_nondefault_windows_stream(target)
            ):
                raise CanonicalStorageError("delete staging file identity changed")
            data = target.read_bytes()
            after = target.stat()
            if (
                len(data) != entry.size_bytes
                or hashlib.sha256(data).hexdigest() != entry.content_sha256
                or (after.st_dev, after.st_ino, after.st_size)
                != (info.st_dev, info.st_ino, info.st_size)
            ):
                raise CanonicalStorageError("delete staging file content changed")
            target.unlink()
        else:
            identity = canonical_cleanup_sha256(
                TREE_IDENTITY_DOMAIN,
                {
                    "entry_kind": "directory",
                    "relative_path": entry.relative_path,
                    "st_dev": info.st_dev,
                    "st_ino": info.st_ino,
                },
            )
            if (
                not stat.S_ISDIR(info.st_mode)
                or identity != entry.identity_sha256
                or _has_nondefault_windows_stream(target)
            ):
                raise CanonicalStorageError("delete staging directory identity changed")
            target.rmdir()
        deleted += 1
        progress[0] = deleted
        _fault(fault_injector, f"delete.after_unlink.{index}")
    _fault(fault_injector, "delete.before_staging_rmdir")
    _require_not_cancelled(
        cancelled,
        run_id_sha256=inventory.run_id_sha256,
        plan_sha256=plan_sha256,
        transaction_id=transaction_id,
    )
    _verify_directory_chain(expected_parent_chain)
    verify_directory_identity(
        root,
        relative_path=None,
        expected_identity=expected_root_identity,
    )
    root.rmdir()
    progress[0] = deleted + 1
    _fault(fault_injector, "delete.after_staging_rmdir")
    _require_not_cancelled(
        cancelled,
        run_id_sha256=inventory.run_id_sha256,
        plan_sha256=plan_sha256,
        transaction_id=transaction_id,
    )
    return deleted + 1


def _observe_staging_failure(
    staging: Path,
    *,
    run_id_sha256: str,
    expected_tree_sha256: str,
    limits: CleanupLimitsV1,
    expected_parent_chain: tuple[tuple[Path, int, int], ...] | None = None,
) -> tuple[Literal["delete_staging_moved", "partial_delete"], bool]:
    """Boundedly classify staging after a failed delete; bool means unreadable."""

    try:
        if expected_parent_chain is not None:
            _verify_directory_chain(expected_parent_chain)
        if not _strict_lexists(staging):
            return "partial_delete", False
        observed = scan_cleanup_tree(
            staging,
            run_id_sha256=run_id_sha256,
            limits=limits,
        )
    except Exception:
        return "partial_delete", True
    if tree_inventory_sha256(observed) == expected_tree_sha256:
        return "delete_staging_moved", False
    return "partial_delete", False


def _control_tree_bytes(control: Path, *, maximum_bytes: int) -> int:
    if not control.exists():
        return 0
    verify_directory(control)
    total = 0
    entries = 0
    stack = [control]
    while stack:
        directory = stack.pop()
        for child in directory.iterdir():
            entries += 1
            if entries > 256:
                raise _fail(CleanupFailureCode.AUDIT_CAPACITY_EXCEEDED)
            info = child.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
                raise _fail(CleanupFailureCode.LINK_OR_REPARSE_DETECTED)
            if stat.S_ISDIR(info.st_mode):
                stack.append(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                total += info.st_size
                if total > maximum_bytes:
                    raise _fail(CleanupFailureCode.AUDIT_CAPACITY_EXCEEDED)
            else:
                raise _fail(CleanupFailureCode.PATH_CONFINEMENT_FAILED)
    return total


def _namespace_child(
    parent: Path,
    expected_name: str,
    *,
    require_exact: bool,
) -> Path:
    verify_directory(parent)
    expected_alias = unicodedata.normalize("NFC", expected_name).casefold()
    exact = False
    aliases: set[str] = set()
    for child in parent.iterdir():
        normalized = unicodedata.normalize("NFC", child.name)
        alias = normalized.casefold()
        if normalized != child.name or alias in aliases:
            raise _fail(CleanupFailureCode.ALIAS_CONFLICT)
        aliases.add(alias)
        if alias == expected_alias:
            if child.name != expected_name:
                raise _fail(CleanupFailureCode.ALIAS_CONFLICT)
            exact = True
    if require_exact and not exact:
        raise _fail(CleanupFailureCode.STALE_SOURCE)
    return parent / expected_name


def _has_nondefault_windows_stream(path: Path) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    class StreamData(ctypes.Structure):
        _fields_ = [
            ("stream_size", ctypes.c_longlong),
            ("stream_name", ctypes.c_wchar * 296),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(StreamData),
        wintypes.DWORD,
    ]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(StreamData)]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL
    data = StreamData()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        return ctypes.get_last_error() != 38
    try:
        while True:
            if data.stream_name != "::$DATA":
                return True
            if not find_next(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                return error != 38  # ERROR_HANDLE_EOF is the only clean terminator.
    finally:
        find_close(handle)


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
        marker_sha = cleanup_root_marker_sha256(marker)
        inspection = inspect_cleanup_root(
            self.cleanup_root,
            expected_product_root_identity_sha256=marker.product_root_identity_sha256,
            expected_product_ownership_marker_sha256=marker.product_ownership_marker_sha256,
            max_artifact_bytes=marker.limits.maximum_control_artifact_bytes,
        )
        if (
            marker.cleanup_root_identity_sha256 != _root_identity(self.cleanup_root)
            or inspection.status != "initialized"
            or inspection.marker_sha256 != marker_sha
        ):
            raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED)
        try:
            binding = self.terminal_store.foundation.inspect_root_authority_binding()
        except RunStorageError as exc:
            raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED) from exc
        self._require_product_binding(marker, binding)
        return marker, marker_sha

    @staticmethod
    def _require_product_binding(
        marker: CleanupRootMarkerV1,
        binding: RunAuthorityBindingV1 | ExistingRunAuthorityV1 | DetachedRunAuthorityV1,
    ) -> None:
        if (
            marker.product_root_identity_sha256 != binding.revision_root_identity_sha256
            or marker.product_ownership_marker_sha256 != binding.ownership_marker_sha256
        ):
            raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED)

    def _run_control(self, run_hash: str) -> Path:
        return _namespace_child(
            self.cleanup_root / "runs",
            run_hash,
            require_exact=False,
        )

    def _verify_pending_quarantine_control(
        self,
        *,
        marker: CleanupRootMarkerV1,
        marker_sha256: str,
        transaction: CleanupTransactionV1,
        revision_artifacts: tuple[
            CleanupManifestV1,
            CleanupReceiptV1,
            CleanupTombstoneV1,
        ]
        | None = None,
        pointer_temporary: tuple[str, bytes] | None = None,
    ) -> None:
        """Re-read the exact pre-current quarantine control state without callbacks."""

        run_hash = transaction.run_id_sha256
        try:
            live_marker, live_marker_sha = self.marker()
            if live_marker != marker or live_marker_sha != marker_sha256:
                raise _fail(
                    CleanupFailureCode.OWNERSHIP_UNVERIFIED,
                    run_id_sha256=run_hash,
                    transaction_id=transaction.transaction_id,
                )
            control = self._run_control(run_hash)
            verify_directory(control)
            entries = {item.name: item for item in control.iterdir()}
            expected_control_entries = {"transactions", "revisions"}
            if pointer_temporary is not None:
                expected_control_entries.add(pointer_temporary[0])
            if set(entries) != expected_control_entries:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    transaction_id=transaction.transaction_id,
                )
            if pointer_temporary is not None:
                temporary_path = entries[pointer_temporary[0]]
                temporary_info = verify_regular_single_link(temporary_path)
                if temporary_info.st_size > marker.limits.maximum_control_artifact_bytes:
                    raise _fail(
                        CleanupFailureCode.STALE_CLEANUP_REVISION,
                        run_id_sha256=run_hash,
                        transaction_id=transaction.transaction_id,
                    )
                temporary_bytes = temporary_path.read_bytes()
                if temporary_bytes != pointer_temporary[1]:
                    raise _fail(
                        CleanupFailureCode.STALE_CLEANUP_REVISION,
                        run_id_sha256=run_hash,
                        transaction_id=transaction.transaction_id,
                    )
            transactions = entries["transactions"]
            revisions = entries["revisions"]
            verify_directory(transactions)
            verify_directory(revisions)
            journal_directories = {item.name: item for item in transactions.iterdir()}
            if set(journal_directories) != {transaction.transaction_id}:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    transaction_id=transaction.transaction_id,
                )
            journal_root = journal_directories[transaction.transaction_id]
            verify_directory(journal_root)
            journal_entries = {item.name: item for item in journal_root.iterdir()}
            if set(journal_entries) != {"transaction.json"}:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    transaction_id=transaction.transaction_id,
                )
            observed_transaction, observed_bytes = _read_control(
                journal_entries["transaction.json"],
                CleanupTransactionV1,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )
            if observed_transaction != transaction or observed_bytes != canonical_cleanup_bytes(
                transaction
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    transaction_id=transaction.transaction_id,
                )
            revision_entries = {item.name: item for item in revisions.iterdir()}
            if revision_artifacts is None:
                if revision_entries:
                    raise _fail(
                        CleanupFailureCode.STALE_CLEANUP_REVISION,
                        run_id_sha256=run_hash,
                        transaction_id=transaction.transaction_id,
                    )
                return
            revision_name = f"r1-{transaction.transaction_id}"
            if set(revision_entries) != {revision_name}:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    transaction_id=transaction.transaction_id,
                )
            revision_root = revision_entries[revision_name]
            verify_directory(revision_root)
            artifacts = {item.name: item for item in revision_root.iterdir()}
            expected = {
                "transaction.json": transaction,
                "manifest.json": revision_artifacts[0],
                "receipt.json": revision_artifacts[1],
                "tombstone.json": revision_artifacts[2],
            }
            if set(artifacts) != set(expected):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    transaction_id=transaction.transaction_id,
                )
            for name, value in expected.items():
                observed, exact_bytes = _read_control(
                    artifacts[name],
                    type(value),
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
                if observed != value or exact_bytes != canonical_cleanup_bytes(value):
                    raise _fail(
                        CleanupFailureCode.STALE_CLEANUP_REVISION,
                        run_id_sha256=run_hash,
                        transaction_id=transaction.transaction_id,
                    )
        except CleanupStorageError:
            raise
        except Exception:
            raise _fail(
                CleanupFailureCode.STALE_CLEANUP_REVISION,
                run_id_sha256=run_hash,
                transaction_id=transaction.transaction_id,
            ) from None

    def quarantine_path(self, run_id: str) -> Path:
        return _namespace_child(
            self.cleanup_root / "quarantine",
            run_id,
            require_exact=True,
        )

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
        return self._read_current(run_hash)

    def _read_current(
        self,
        run_hash: str,
        *,
        pending_transaction: CleanupTransactionV1 | None = None,
        pending_pointer: tuple[str, bytes] | None = None,
        expected_marker: tuple[CleanupRootMarkerV1, str] | None = None,
    ) -> (
        tuple[
            CleanupCurrentPointerV1,
            CleanupManifestV1,
            CleanupReceiptV1,
            CleanupTombstoneV1,
        ]
        | None
    ):
        marker, marker_sha = self.marker()
        if expected_marker is not None and (
            marker != expected_marker[0] or marker_sha != expected_marker[1]
        ):
            raise _fail(
                CleanupFailureCode.OWNERSHIP_UNVERIFIED,
                run_id_sha256=run_hash,
            )
        control = self._run_control(run_hash)
        if not control.exists():
            return None
        verify_directory(control)
        entries = {item.name: item for item in control.iterdir()}
        expected_control_entries = set(_RUN_CONTROL_ENTRIES)
        if pending_pointer is not None:
            expected_control_entries.add(pending_pointer[0])
        if set(entries) != expected_control_entries:
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
        transaction, _transaction_bytes = _read_control(
            revision_entries["transaction.json"],
            CleanupTransactionV1,
            max_bytes=marker.limits.maximum_control_artifact_bytes,
        )
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
        transaction = cast(CleanupTransactionV1, transaction)
        receipt = cast(CleanupReceiptV1, receipt)
        tombstone = cast(CleanupTombstoneV1, tombstone)
        if (
            cleanup_pointer_sha256(pointer) != cleanup_pointer_sha256(pointer_bytes)
            or transaction.transaction_sha256 != cleanup_transaction_sha256(transaction)
            or transaction.run_id_sha256 != run_hash
            or transaction.transaction_id != pointer.transaction_id
            or transaction.proposed_revision != pointer.revision
            or transaction.plan_sha256 != manifest.plan_sha256
            or transaction.approval_binding_sha256 != manifest.approval_binding_sha256
            or pointer.manifest_sha256 != cleanup_manifest_sha256(manifest)
            or pointer.receipt_sha256 != cleanup_receipt_sha256(receipt)
            or pointer.tombstone_sha256 != cleanup_tombstone_sha256(tombstone)
            or manifest.receipt_sha256 != pointer.receipt_sha256
            or manifest.tombstone_sha256 != pointer.tombstone_sha256
            or manifest.state != pointer.state
            or receipt.result_state != pointer.state
            or tombstone.state != pointer.state
            or manifest.action_kind != receipt.action_kind
            or manifest.execution_id != receipt.execution_id
            or manifest.idempotency_key != receipt.idempotency_key
            or manifest.plan_sha256 != receipt.plan_sha256
            or manifest.approval_binding_sha256 != receipt.approval_binding_sha256
            or cleanup_plan_sha256(manifest.plan) != manifest.plan_sha256
            or cleanup_approval_binding_sha256(manifest.approval_binding)
            != manifest.approval_binding_sha256
            or manifest.approval_binding.actor_sha256 != receipt.actor_sha256
            or manifest.approval_binding.authority_snapshot_sha256
            != receipt.authority_snapshot_sha256
            or tombstone.receipt_sha256 != pointer.receipt_sha256
            or tombstone.receipt_retain_until != receipt.committed_at + timedelta(days=365)
            or manifest.transaction_id != pointer.transaction_id
            or manifest.revision != pointer.revision
            or manifest.run_id_sha256 != run_hash
            or receipt.run_id_sha256 != run_hash
            or tombstone.run_id_sha256 != run_hash
        ):
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        if pointer.revision not in {1, 2, 3}:
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        successor_manifest = manifest
        expected_state_sequence = {
            3: CleanupState.DELETED,
            2: CleanupState.DELETE_PREPARED,
            1: CleanupState.QUARANTINED,
        }
        reachable_transactions = {pointer.revision: transaction}
        for prior_revision in range(pointer.revision - 1, 0, -1):
            revisions_root = control / "revisions"
            verify_directory(revisions_root)
            prefix = f"r{prior_revision}-"
            matches = tuple(
                child for child in revisions_root.iterdir() if child.name.startswith(prefix)
            )
            if len(matches) != 1:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            prior_directory = matches[0]
            verify_directory(prior_directory)
            prior_entries = {item.name: item for item in prior_directory.iterdir()}
            if set(prior_entries) != {
                "transaction.json",
                "manifest.json",
                "receipt.json",
                "tombstone.json",
            }:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            prior_transaction = cast(
                CleanupTransactionV1,
                _read_control(
                    prior_entries["transaction.json"],
                    CleanupTransactionV1,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )[0],
            )
            prior_manifest = cast(
                CleanupManifestV1,
                _read_control(
                    prior_entries["manifest.json"],
                    CleanupManifestV1,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )[0],
            )
            prior_receipt = cast(
                CleanupReceiptV1,
                _read_control(
                    prior_entries["receipt.json"],
                    CleanupReceiptV1,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )[0],
            )
            prior_tombstone = cast(
                CleanupTombstoneV1,
                _read_control(
                    prior_entries["tombstone.json"],
                    CleanupTombstoneV1,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )[0],
            )
            prior_manifest_sha = cleanup_manifest_sha256(prior_manifest)
            prior_receipt_sha = cleanup_receipt_sha256(prior_receipt)
            prior_tombstone_sha = cleanup_tombstone_sha256(prior_tombstone)
            if prior_directory.name != (f"r{prior_revision}-{prior_manifest.transaction_id}"):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            prior_pointer = CleanupCurrentPointerV1(
                run_id_sha256=run_hash,
                revision=prior_revision,
                transaction_id=prior_manifest.transaction_id,
                revision_relative_path=prior_directory.relative_to(self.cleanup_root).as_posix(),
                state=prior_manifest.state,
                manifest_sha256=prior_manifest_sha,
                receipt_sha256=prior_receipt_sha,
                tombstone_sha256=prior_tombstone_sha,
                published_at=prior_manifest.created_at,
            )
            if (
                prior_manifest.revision != prior_revision
                or prior_manifest.state is not expected_state_sequence[prior_revision]
                or prior_transaction.transaction_sha256
                != cleanup_transaction_sha256(prior_transaction)
                or prior_transaction.proposed_revision != prior_revision
                or prior_transaction.transaction_id != prior_manifest.transaction_id
                or prior_manifest.receipt_sha256 != prior_receipt_sha
                or prior_manifest.tombstone_sha256 != prior_tombstone_sha
                or prior_receipt.result_state is not prior_manifest.state
                or prior_tombstone.state is not prior_manifest.state
                or prior_tombstone.receipt_sha256 != prior_receipt_sha
                or prior_tombstone.receipt_retain_until
                != prior_receipt.committed_at + timedelta(days=365)
                or successor_manifest.previous_revision != prior_revision
                or successor_manifest.previous_manifest_sha256 != prior_manifest_sha
                or successor_manifest.expected_pointer_sha256
                != cleanup_pointer_sha256(prior_pointer)
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            reachable_transactions[prior_revision] = prior_transaction
            successor_manifest = prior_manifest
        expected_journals = {
            candidate.transaction_id: candidate
            for revision_number, candidate in reachable_transactions.items()
            if revision_number in {1, 2}
        }
        if pending_transaction is not None:
            if (
                pending_transaction.transaction_sha256
                != cleanup_transaction_sha256(pending_transaction)
                or pending_transaction.run_id_sha256 != run_hash
                or pending_transaction.proposed_revision != pointer.revision + 1
                or pending_transaction.expected_revision != pointer.revision
                or pending_transaction.expected_pointer_sha256 != cleanup_pointer_sha256(pointer)
                or pending_transaction.transaction_id in expected_journals
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            expected_journals[pending_transaction.transaction_id] = pending_transaction
        transactions_root = entries["transactions"]
        verify_directory(transactions_root)
        journal_directories = {item.name: item for item in transactions_root.iterdir()}
        if set(journal_directories) != set(expected_journals):
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        for journal_id, expected_transaction in expected_journals.items():
            journal_root = journal_directories[journal_id]
            verify_directory(journal_root)
            journal_entries = {item.name: item for item in journal_root.iterdir()}
            if set(journal_entries) != {"transaction.json"}:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            journal, journal_bytes = _read_control(
                journal_entries["transaction.json"],
                CleanupTransactionV1,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )
            if journal != expected_transaction or journal_bytes != canonical_cleanup_bytes(
                expected_transaction
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
        if (
            _read_control(
                control / "current.json",
                CleanupCurrentPointerV1,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )[1]
            != pointer_bytes
        ):
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        if pending_pointer is not None:
            temporary = entries[pending_pointer[0]]
            temporary_info = verify_regular_single_link(temporary)
            if (
                temporary_info.st_size > marker.limits.maximum_control_artifact_bytes
                or temporary.read_bytes() != pending_pointer[1]
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
        return pointer, manifest, receipt, tombstone

    def read_operation(
        self,
        plan: CleanupPlanV1,
        *,
        approval_run_id_sha256: str,
        approval_request_id: str,
    ) -> (
        tuple[
            CleanupCurrentPointerV1,
            CleanupManifestV1,
            CleanupReceiptV1,
            CleanupTombstoneV1,
        ]
        | None
    ):
        """Find one exact persisted operation after validating the full current lineage."""

        run_hash = plan.source.run_id_sha256
        if not _strict_lexists(self._run_control(run_hash)):
            return None
        marker, marker_sha = self.marker()
        if (
            marker.root_id != plan.cleanup_root_id
            or marker_sha != plan.cleanup_root_marker_sha256
            or marker.limits != plan.limits
            or (
                isinstance(plan.source, ProductRunSourceV1)
                and (
                    marker.product_root_identity_sha256 != plan.source.product_root_identity_sha256
                    or marker.product_ownership_marker_sha256
                    != plan.source.product_ownership_marker_sha256
                )
            )
            or (
                not isinstance(plan.source, ProductRunSourceV1)
                and marker.cleanup_root_identity_sha256 != plan.source.cleanup_root_identity_sha256
            )
        ):
            raise _fail(
                CleanupFailureCode.STALE_CLEANUP_REVISION,
                run_id_sha256=run_hash,
                plan_sha256=cleanup_plan_sha256(plan),
            )
        current = self._read_current(
            run_hash,
            expected_marker=(marker, marker_sha),
        )
        if current is None:
            return None
        try:
            binding = self.terminal_store.foundation.inspect_run_authority_binding(
                plan.source.run_id,
                detached=True,
            )
        except (ProductRunError, RunStorageError) as exc:
            raise _fail(
                CleanupFailureCode.OWNERSHIP_UNVERIFIED,
                run_id_sha256=run_hash,
                plan_sha256=cleanup_plan_sha256(plan),
            ) from exc
        self._require_product_binding(marker, binding)
        control = self._run_control(run_hash)
        revisions_root = control / "revisions"
        verify_directory(revisions_root)
        current_pointer = current[0]
        reachable_directories = [self.cleanup_root / current_pointer.revision_relative_path]
        for prior_revision in range(current_pointer.revision - 1, 0, -1):
            matches = tuple(
                child
                for child in revisions_root.iterdir()
                if child.name.startswith(f"r{prior_revision}-")
            )
            if len(matches) != 1:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            reachable_directories.append(matches[0])
        plan_sha = cleanup_plan_sha256(plan)
        exact: list[
            tuple[
                CleanupCurrentPointerV1,
                CleanupManifestV1,
                CleanupReceiptV1,
                CleanupTombstoneV1,
            ]
        ] = []
        identity_seen = False
        for revision_directory in reachable_directories:
            verify_directory(revision_directory)
            entries = {item.name: item for item in revision_directory.iterdir()}
            if set(entries) != {
                "transaction.json",
                "manifest.json",
                "receipt.json",
                "tombstone.json",
            }:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            transaction = cast(
                CleanupTransactionV1,
                _read_control(
                    entries["transaction.json"],
                    CleanupTransactionV1,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )[0],
            )
            manifest = cast(
                CleanupManifestV1,
                _read_control(
                    entries["manifest.json"],
                    CleanupManifestV1,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )[0],
            )
            receipt = cast(
                CleanupReceiptV1,
                _read_control(
                    entries["receipt.json"],
                    CleanupReceiptV1,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )[0],
            )
            tombstone = cast(
                CleanupTombstoneV1,
                _read_control(
                    entries["tombstone.json"],
                    CleanupTombstoneV1,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )[0],
            )
            same_identity = (
                manifest.execution_id == plan.execution_id
                or manifest.idempotency_key == plan.idempotency_key
            )
            if not same_identity:
                continue
            identity_seen = True
            approval = manifest.approval_binding
            if (
                revision_directory.name != f"r{manifest.revision}-{manifest.transaction_id}"
                or transaction.transaction_sha256 != cleanup_transaction_sha256(transaction)
                or transaction.run_id_sha256 != run_hash
                or transaction.transaction_id != manifest.transaction_id
                or transaction.proposed_revision != manifest.revision
                or transaction.expected_revision != manifest.revision - 1
                or transaction.expected_pointer_sha256 != manifest.expected_pointer_sha256
                or transaction.action_kind is not manifest.action_kind
                or transaction.execution_id != manifest.execution_id
                or transaction.idempotency_key != manifest.idempotency_key
                or transaction.plan != manifest.plan
                or transaction.plan_sha256 != manifest.plan_sha256
                or transaction.approval_binding != manifest.approval_binding
                or transaction.approval_binding_sha256 != manifest.approval_binding_sha256
                or transaction.source_tree_sha256 != manifest.source_tree_sha256
                or manifest.execution_id != plan.execution_id
                or manifest.idempotency_key != plan.idempotency_key
                or manifest.plan_sha256 != plan_sha
                or approval.approval_run_id_sha256 != approval_run_id_sha256
                or approval.request_id != approval_request_id
                or cleanup_plan_sha256(manifest.plan) != plan_sha
                or receipt.plan_sha256 != plan_sha
                or receipt.approval_binding_sha256 != manifest.approval_binding_sha256
                or cleanup_receipt_sha256(receipt) != manifest.receipt_sha256
                or cleanup_tombstone_sha256(tombstone) != manifest.tombstone_sha256
            ):
                raise _fail(
                    CleanupFailureCode.IDEMPOTENCY_CONFLICT,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                )
            pointer = CleanupCurrentPointerV1(
                run_id_sha256=run_hash,
                revision=manifest.revision,
                transaction_id=manifest.transaction_id,
                revision_relative_path=revision_directory.relative_to(self.cleanup_root).as_posix(),
                state=manifest.state,
                manifest_sha256=cleanup_manifest_sha256(manifest),
                receipt_sha256=cleanup_receipt_sha256(receipt),
                tombstone_sha256=cleanup_tombstone_sha256(tombstone),
                published_at=manifest.created_at,
            )
            exact.append((pointer, manifest, receipt, tombstone))
        if identity_seen and not exact:
            raise _fail(
                CleanupFailureCode.IDEMPOTENCY_CONFLICT,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
            )
        if not exact:
            return None
        return max(exact, key=lambda item: item[0].revision)

    def inspect_reconciliation(
        self,
        plan: CleanupPlanV1,
        *,
        transaction_id: str,
    ) -> CleanupReconciliationReportV1:
        """Classify observed state without repairing, deleting, or retrying."""

        run_hash = plan.source.run_id_sha256
        plan_sha = cleanup_plan_sha256(plan)
        marker, marker_sha = self.marker()
        if (
            marker.root_id != plan.cleanup_root_id
            or marker_sha != plan.cleanup_root_marker_sha256
            or (
                isinstance(plan.source, ProductRunSourceV1)
                and (
                    marker.product_root_identity_sha256 != plan.source.product_root_identity_sha256
                    or marker.product_ownership_marker_sha256
                    != plan.source.product_ownership_marker_sha256
                )
            )
            or (
                not isinstance(plan.source, ProductRunSourceV1)
                and marker.cleanup_root_identity_sha256 != plan.source.cleanup_root_identity_sha256
            )
        ):
            raise _fail(
                CleanupFailureCode.OWNERSHIP_UNVERIFIED,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )

        def observed_tree(
            path: Path,
        ) -> Literal["absent", "exact", "mismatch", "unreadable"]:
            try:
                if not _strict_lexists(path):
                    return "absent"
                inventory = scan_cleanup_tree(
                    path,
                    run_id_sha256=run_hash,
                    limits=marker.limits,
                )
                return (
                    "exact"
                    if tree_inventory_sha256(inventory) == plan.tree_inventory_sha256
                    else "mismatch"
                )
            except Exception:
                return "unreadable"

        action = plan.actions[0]
        if isinstance(plan.source, ProductRunSourceV1):
            source_path = self.terminal_store.foundation.runs_root / plan.source.run_id
            destination_path = self.cleanup_root / action.destination_relative_path
            source = observed_tree(source_path)
            destination = observed_tree(destination_path)
            staging: Literal["absent", "exact", "partial", "unreadable"] = "absent"
            expected_state = CleanupState.QUARANTINED
        else:
            source_path = self.cleanup_root / action.source_relative_path
            staging_path = self.cleanup_root / action.destination_relative_path
            source = observed_tree(source_path)
            destination = "absent"
            raw_staging = observed_tree(staging_path)
            staging = cast(
                Any,
                "partial" if raw_staging == "mismatch" else raw_staging,
            )
            expected_state = CleanupState.DELETED
        current_observed: Literal["absent", "prior", "committed", "mismatch", "unreadable"]
        receipt_observed: Literal["absent", "exact", "mismatch", "unreadable"] = "absent"
        tombstone_observed: Literal["absent", "exact", "mismatch", "unreadable"] = "absent"
        try:
            control_path = self._run_control(run_hash)
            if not _strict_lexists(control_path):
                current_observed = "absent"
            else:
                current = self.read_current(run_hash)
                if current is None:
                    current_observed = "absent"
                    raise CanonicalStorageError("cleanup current disappeared during inspection")
                pointer, manifest, receipt, tombstone = current
                if (
                    plan.expected_cleanup_pointer_sha256 is not None
                    and cleanup_pointer_sha256(pointer) == plan.expected_cleanup_pointer_sha256
                ):
                    current_observed = "prior"
                elif (
                    pointer.state is expected_state
                    and manifest.plan_sha256 == plan_sha
                    and manifest.execution_id == plan.execution_id
                    and manifest.idempotency_key == plan.idempotency_key
                ):
                    current_observed = "committed"
                    receipt_observed = (
                        "exact"
                        if cleanup_receipt_sha256(receipt) == pointer.receipt_sha256
                        else "mismatch"
                    )
                    tombstone_observed = (
                        "exact"
                        if cleanup_tombstone_sha256(tombstone) == pointer.tombstone_sha256
                        else "mismatch"
                    )
                elif (
                    pointer.state is CleanupState.DELETE_PREPARED
                    and manifest.plan_sha256 == plan_sha
                    and manifest.execution_id == plan.execution_id
                    and manifest.idempotency_key == plan.idempotency_key
                ):
                    current_observed = "prior"
                else:
                    current_observed = "mismatch"
                    receipt_observed = "mismatch"
                    tombstone_observed = "mismatch"
        except Exception:
            current_observed = "unreadable"
            receipt_observed = "unreadable"
            tombstone_observed = "unreadable"
        if (
            current_observed == "committed"
            and receipt_observed == "exact"
            and tombstone_observed == "exact"
            and (
                (
                    isinstance(plan.source, ProductRunSourceV1)
                    and source == "absent"
                    and destination == "exact"
                )
                or (
                    not isinstance(plan.source, ProductRunSourceV1)
                    and source == "absent"
                    and staging == "absent"
                )
            )
        ):
            classification = "committed"
        elif (
            current_observed in {"absent", "prior"}
            and source == "exact"
            and destination == "absent"
            and staging == "absent"
        ):
            classification = "no_effect"
        elif "unreadable" in {
            source,
            destination,
            staging,
            current_observed,
            receipt_observed,
            tombstone_observed,
        } or "mismatch" in {
            source,
            destination,
            current_observed,
            receipt_observed,
            tombstone_observed,
        }:
            classification = "effect_unknown"
        else:
            classification = "reconciliation_required"
        return CleanupReconciliationReportV1(
            run_id_sha256=run_hash,
            transaction_id=transaction_id,
            plan_sha256=plan_sha,
            observed_source=source,
            observed_destination=destination,
            observed_staging=staging,
            observed_current=current_observed,
            observed_receipt=receipt_observed,
            observed_tombstone=tombstone_observed,
            classification=cast(Any, classification),
        )

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

    def _publish_quarantine(
        self,
        plan: CleanupPlanV1,
        *,
        transaction_id: str,
        clock: Callable[[], datetime],
        authorize: Callable[[datetime], CleanupApprovalBindingV1],
        cancelled: Callable[[], bool] | None = None,
    ) -> CleanupExecutionResultV1:
        """Internal quarantine transition with in-lock authorization.

        Callers outside the cleanup executor must not invoke this private seam.
        The callback is deliberately evaluated only after source/tree/CAS
        revalidation while the existing product-run authority is held.
        """

        if not isinstance(plan.source, ProductRunSourceV1):
            raise _fail(CleanupFailureCode.INVALID_PLAN)
        run_id = plan.source.run_id
        run_hash = plan.source.run_id_sha256
        plan_sha = cleanup_plan_sha256(plan)
        marker, marker_sha = self.marker()
        if (
            marker.product_root_identity_sha256 != plan.source.product_root_identity_sha256
            or marker.product_ownership_marker_sha256 != plan.source.product_ownership_marker_sha256
        ):
            raise _fail(
                CleanupFailureCode.OWNERSHIP_UNVERIFIED,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
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
        control_created = False
        source_moved = False
        pointer_replace_attempted = False

        def current_failure_uncertain() -> bool:
            try:
                report = self.inspect_reconciliation(
                    plan,
                    transaction_id=transaction_id,
                )
            except Exception:
                return True
            return report.observed_current != "absent"

        try:
            authority = self.terminal_store.foundation.acquire_existing_run_authority(run_id)
            self._require_product_binding(marker, authority)
            current = self.terminal_store.read_current(run_id)
            if not self._same_product_current(plan, current):
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            if (
                self._read_current(
                    run_hash,
                    expected_marker=(marker, marker_sha),
                )
                is not None
            ):
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
            destination = _namespace_child(
                self.cleanup_root / "quarantine",
                run_id,
                require_exact=False,
            )
            if (
                source
                != self.terminal_store.foundation.revision_root
                / plan.actions[0].source_relative_path
                or destination != self.cleanup_root / plan.actions[0].destination_relative_path
            ):
                raise _fail(CleanupFailureCode.INVALID_PLAN, run_id_sha256=run_hash)
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
            approval = authorize(clock())
            approval_sha = cleanup_approval_binding_sha256(approval)
            journal_at = clock()
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
            transaction_base: dict[str, Any] = {
                "schema_version": "1.0.0",
                "run_id_sha256": run_hash,
                "transaction_id": transaction_id,
                "proposed_revision": 1,
                "expected_revision": 0,
                "expected_pointer_sha256": None,
                "action_kind": CleanupActionKind.QUARANTINE_PRODUCT_RUN,
                "execution_id": plan.execution_id,
                "idempotency_key": plan.idempotency_key,
                "plan": plan,
                "plan_sha256": plan_sha,
                "approval_binding": approval,
                "approval_binding_sha256": approval_sha,
                "source_tree_sha256": source_tree_sha,
                "created_at": journal_at,
            }
            transaction = CleanupTransactionV1(
                **transaction_base,
                transaction_sha256=cleanup_transaction_sha256(transaction_base),
            )
            projected = _control_tree_bytes(
                control,
                maximum_bytes=marker.limits.maximum_control_bytes_per_run,
            ) + (
                5 * marker.limits.maximum_control_artifact_bytes
                + 2 * len(canonical_cleanup_bytes(transaction))
            )
            if projected > marker.limits.maximum_control_bytes_per_run:
                raise _fail(
                    CleanupFailureCode.AUDIT_CAPACITY_EXCEEDED,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            _fault(self.fault_injector, "quarantine.before_journal")
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            control.mkdir()
            control_created = True
            (control / "transactions").mkdir()
            (control / "revisions").mkdir()
            transaction_root.mkdir()
            _write_exclusive(
                transaction_root / "transaction.json",
                canonical_cleanup_bytes(transaction),
                max_bytes=marker.limits.maximum_control_artifact_bytes,
                fault_injector=self.fault_injector,
                fault_hook="quarantine.journal",
            )
            journal_published = True
            _directory_sync(transaction_root)
            _fault(self.fault_injector, "quarantine.before_effect")
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            effect_at = clock()
            final_approval = authorize(effect_at)
            if final_approval != approval:
                raise _fail(
                    CleanupFailureCode.APPROVAL_MISMATCH,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            self._verify_pending_quarantine_control(
                marker=marker,
                marker_sha256=marker_sha,
                transaction=transaction,
            )
            authority.revalidate()
            final_current = self.terminal_store.read_current(run_id)
            if not self._same_product_current(plan, final_current):
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            final_inventory = scan_cleanup_tree(
                source,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(final_inventory) != source_tree_sha:
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            final_destination = _namespace_child(
                self.cleanup_root / "quarantine",
                run_id,
                require_exact=False,
            )
            if final_destination != destination:
                raise _fail(CleanupFailureCode.INVALID_PLAN, run_id_sha256=run_hash)
            if (
                _strict_lexists(destination)
                or source.stat().st_dev != destination.parent.stat().st_dev
            ):
                raise _fail(
                    (
                        CleanupFailureCode.IDEMPOTENCY_CONFLICT
                        if _strict_lexists(destination)
                        else CleanupFailureCode.CROSS_VOLUME
                    ),
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            os.replace(source, destination)
            source_moved = True
            _fault(self.fault_injector, "quarantine.after_effect")
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source remained after rename")
            destination_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(destination_inventory) != source_tree_sha:
                raise CanonicalStorageError("quarantine destination tree changed")
            rename_directory_sync = _directory_sync_many(source.parent, destination.parent)
            committed_at = clock()
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source reappeared after commit clock")
            committed_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(committed_inventory) != source_tree_sha:
                raise CanonicalStorageError("quarantine destination changed after commit clock")
            self._verify_pending_quarantine_control(
                marker=marker,
                marker_sha256=marker_sha,
                transaction=transaction,
            )
            revision.mkdir()
            receipt = CleanupReceiptV1(
                action_kind=CleanupActionKind.QUARANTINE_PRODUCT_RUN,
                run_id_sha256=run_hash,
                transaction_id=transaction_id,
                execution_id=plan.execution_id,
                idempotency_key=plan.idempotency_key,
                plan_sha256=plan_sha,
                approval_binding_sha256=approval_sha,
                actor_sha256=approval.actor_sha256,
                authority_snapshot_sha256=approval.authority_snapshot_sha256,
                source_tree_sha256=source_tree_sha,
                result_state=CleanupState.QUARANTINED,
                effect_started_at=effect_at,
                committed_at=committed_at,
                durability=CleanupDurabilityEvidenceV1(
                    platform_adapter=cast(Any, platform_adapter()),
                    journal_file_sync="confirmed",
                    effect_rename="confirmed",
                    control_file_sync="confirmed",
                    directory_sync=rename_directory_sync,
                    pointer_replace="confirmed",
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
                receipt_retain_until=committed_at + timedelta(days=365),
            )
            tombstone_sha = cleanup_tombstone_sha256(tombstone)
            manifest = CleanupManifestV1(
                run_id_sha256=run_hash,
                revision=1,
                transaction_id=transaction_id,
                state=CleanupState.QUARANTINED,
                action_kind=CleanupActionKind.QUARANTINE_PRODUCT_RUN,
                execution_id=plan.execution_id,
                idempotency_key=plan.idempotency_key,
                plan=plan,
                plan_sha256=plan_sha,
                approval_binding=approval,
                approval_binding_sha256=approval_sha,
                source_tree_sha256=source_tree_sha,
                receipt_sha256=receipt_sha,
                tombstone_sha256=tombstone_sha,
                created_at=committed_at,
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
                published_at=committed_at,
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
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source reappeared before pointer replace")
            pre_pointer_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(pre_pointer_inventory) != source_tree_sha:
                raise CanonicalStorageError("quarantine destination changed before pointer replace")
            self._verify_pending_quarantine_control(
                marker=marker,
                marker_sha256=marker_sha,
                transaction=transaction,
                revision_artifacts=(manifest, receipt, tombstone),
                pointer_temporary=(temporary.name, pointer_bytes),
            )
            pointer_replace_attempted = True
            os.replace(temporary, control / "current.json")
            _fault(self.fault_injector, "quarantine.after_pointer_replace")
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source reappeared after pointer replace")
            published_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(published_inventory) != source_tree_sha:
                raise CanonicalStorageError("quarantine destination changed after pointer replace")
            if (control / "current.json").read_bytes() != pointer_bytes:
                raise CanonicalStorageError("cleanup current pointer reread mismatch")
            _directory_sync(control)
            verified = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
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
        except (ProductRunError, RunStorageError) as exc:
            code = (
                CleanupFailureCode.RUN_LOCKED
                if exc.failure.code.value == "run_locked"
                else CleanupFailureCode.STALE_SOURCE
            )
            if source_moved:
                current_uncertain = current_failure_uncertain()
                raise _fail(
                    (
                        CleanupFailureCode.EFFECT_UNKNOWN
                        if current_uncertain
                        else CleanupFailureCode.RECONCILIATION_REQUIRED
                    ),
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect="source_moved",
                    domain_effect=(
                        "current_may_have_advanced" if current_uncertain else "current_unchanged"
                    ),
                ) from exc
            if control_created:
                rolled_back = _rollback_pre_effect_journal(
                    transaction_root,
                    empty_directories=(
                        control / "revisions",
                        control / "transactions",
                        control,
                    ),
                )
                if rolled_back:
                    journal_published = False
                    control_created = False
                else:
                    raise _fail(
                        CleanupFailureCode.RECONCILIATION_REQUIRED,
                        run_id_sha256=run_hash,
                        plan_sha256=plan_sha,
                        transaction_id=transaction_id,
                        filesystem_effect=(
                            "journal_only" if journal_published else "control_published"
                        ),
                        domain_effect="current_unchanged",
                    ) from exc
            raise _fail(
                code,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            ) from exc
        except CleanupStorageError as exc:
            if pointer_replace_attempted:
                raise _fail(
                    CleanupFailureCode.EFFECT_UNKNOWN,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect="source_moved",
                    domain_effect="current_may_have_advanced",
                ) from exc
            if source_moved:
                current_uncertain = current_failure_uncertain()
                raise _fail(
                    (
                        CleanupFailureCode.EFFECT_UNKNOWN
                        if current_uncertain
                        else CleanupFailureCode.RECONCILIATION_REQUIRED
                    ),
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect="source_moved",
                    domain_effect=(
                        "current_may_have_advanced" if current_uncertain else "current_unchanged"
                    ),
                ) from exc
            if journal_published:
                rolled_back = _rollback_pre_effect_journal(
                    transaction_root,
                    empty_directories=(
                        (control / "revisions", control / "transactions", control)
                        if control_created
                        else ()
                    ),
                )
                if rolled_back:
                    journal_published = False
                    control_created = False
                    raise
                raise _fail(
                    CleanupFailureCode.RECONCILIATION_REQUIRED,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect="journal_only",
                    domain_effect="current_unchanged",
                ) from exc
            if control_created:
                rolled_back = _rollback_pre_effect_journal(
                    transaction_root,
                    empty_directories=(
                        control / "revisions",
                        control / "transactions",
                        control,
                    ),
                )
                if rolled_back:
                    control_created = False
                    raise
                raise _fail(
                    CleanupFailureCode.RECONCILIATION_REQUIRED,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect="control_published",
                    domain_effect="current_unchanged",
                ) from exc
            raise
        except Exception as exc:
            if pointer_replace_attempted:
                code = CleanupFailureCode.EFFECT_UNKNOWN
                effect = "source_moved"
                domain_effect = "current_may_have_advanced"
            elif source_moved:
                current_uncertain = current_failure_uncertain()
                code = (
                    CleanupFailureCode.EFFECT_UNKNOWN
                    if current_uncertain
                    else CleanupFailureCode.RECONCILIATION_REQUIRED
                )
                effect = "source_moved"
                domain_effect = (
                    "current_may_have_advanced" if current_uncertain else "current_unchanged"
                )
            elif journal_published:
                rolled_back = _rollback_pre_effect_journal(
                    transaction_root,
                    empty_directories=(
                        (control / "revisions", control / "transactions", control)
                        if control_created
                        else ()
                    ),
                )
                if rolled_back:
                    journal_published = False
                    control_created = False
                    code = CleanupFailureCode.INTERNAL_INVARIANT_ERROR
                    effect = "none"
                    domain_effect = "none"
                else:
                    code = CleanupFailureCode.RECONCILIATION_REQUIRED
                    effect = "journal_only"
                    domain_effect = "current_unchanged"
            elif control_created:
                rolled_back = _rollback_pre_effect_journal(
                    transaction_root,
                    empty_directories=(
                        control / "revisions",
                        control / "transactions",
                        control,
                    ),
                )
                if rolled_back:
                    control_created = False
                    code = CleanupFailureCode.INTERNAL_INVARIANT_ERROR
                    effect = "none"
                    domain_effect = "none"
                else:
                    code = CleanupFailureCode.RECONCILIATION_REQUIRED
                    effect = "control_published"
                    domain_effect = "current_unchanged"
            else:
                code = CleanupFailureCode.INTERNAL_INVARIANT_ERROR
                effect = "none"
                domain_effect = "none"
            raise _fail(
                code,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
                filesystem_effect=cast(Any, effect),
                domain_effect=cast(Any, domain_effect),
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
                        filesystem_effect=(
                            "source_moved"
                            if source_moved
                            else "journal_only"
                            if journal_published
                            else "control_published"
                            if control_created
                            else "none"
                        ),
                        domain_effect="current_may_have_advanced",
                    ) from exc

    def _publish_delete(
        self,
        plan: CleanupPlanV1,
        *,
        transaction_id: str,
        clock: Callable[[], datetime],
        authorize: Callable[
            [datetime, CleanupTransactionV1 | None],
            CleanupApprovalBindingV1,
        ],
        cancelled: Callable[[], bool] | None = None,
    ) -> CleanupExecutionResultV1:
        """Internal staged delete with a durable delete-prepared checkpoint."""

        from poker_deliberation.local_data_cleanup_models import QuarantineSourceV1

        if not isinstance(plan.source, QuarantineSourceV1):
            raise _fail(CleanupFailureCode.INVALID_PLAN)
        run_id = plan.source.run_id
        run_hash = plan.source.run_id_sha256
        plan_sha = cleanup_plan_sha256(plan)
        marker, marker_sha = self.marker()
        if (
            marker.root_id != plan.cleanup_root_id
            or marker_sha != plan.cleanup_root_marker_sha256
            or marker.cleanup_root_identity_sha256 != plan.source.cleanup_root_identity_sha256
            or plan.limits != marker.limits
        ):
            raise _fail(
                CleanupFailureCode.STALE_CLEANUP_REVISION,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
        authority: DetachedRunAuthorityV1 | None = None
        journal_published = False
        transaction_root_created = False
        staging_moved = False
        deleted_entries = 0
        delete_progress = [0]
        prepared_published = False
        prepare_pointer_attempted = False
        final_pointer_attempted = False
        destination: Path | None = None
        staging_parent_chain: tuple[tuple[Path, int, int], ...] | None = None

        def observed_staging_failure() -> tuple[
            Literal["delete_staging_moved", "partial_delete"],
            bool,
        ]:
            if destination is None:
                return "partial_delete", True
            return _observe_staging_failure(
                destination,
                run_id_sha256=run_hash,
                expected_tree_sha256=plan.tree_inventory_sha256,
                limits=marker.limits,
                expected_parent_chain=staging_parent_chain,
            )

        def current_failure_uncertain() -> bool:
            try:
                report = self.inspect_reconciliation(
                    plan,
                    transaction_id=transaction_id,
                )
            except Exception:
                return True
            return report.observed_current != "prior"

        try:
            storage_checked_at = clock()
            current = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if current is None:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            prior_pointer, prior_manifest, _prior_receipt, prior_tombstone = current
            prior_pointer_sha = cleanup_pointer_sha256(prior_pointer)
            prior_manifest_sha = cleanup_manifest_sha256(prior_manifest)
            if (
                prior_pointer.state is not CleanupState.QUARANTINED
                or prior_pointer.revision != plan.expected_cleanup_revision
                or prior_pointer_sha != plan.expected_cleanup_pointer_sha256
                or prior_pointer.revision != plan.source.cleanup_revision
                or prior_pointer_sha != plan.source.cleanup_pointer_sha256
                or cleanup_tombstone_sha256(prior_tombstone) != plan.source.tombstone_sha256
                or prior_tombstone.quarantine_tree_sha256 != plan.source.quarantine_tree_sha256
                or plan.source.quarantine_entered_at != prior_tombstone.quarantine_entered_at
                or plan.source.delete_eligible_at
                != prior_tombstone.quarantine_entered_at
                + timedelta(days=DEFAULT_LOCAL_DATA_POLICY.quarantine_review_days)
                or storage_checked_at < plan.source.delete_eligible_at
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            source = _namespace_child(
                self.cleanup_root / "quarantine",
                run_id,
                require_exact=True,
            )
            planned_destination = self.cleanup_root / plan.actions[0].destination_relative_path
            if planned_destination.parent != self.cleanup_root / "deleting":
                raise _fail(CleanupFailureCode.INVALID_PLAN, run_id_sha256=run_hash)
            destination = _namespace_child(
                self.cleanup_root / "deleting",
                planned_destination.name,
                require_exact=False,
            )
            if (
                plan.actions[0].action_kind is not CleanupActionKind.DELETE_QUARANTINE_PAYLOAD
                or source != self.cleanup_root / plan.actions[0].source_relative_path
                or destination != planned_destination
            ):
                raise _fail(CleanupFailureCode.INVALID_PLAN, run_id_sha256=run_hash)
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
            inventory = scan_cleanup_tree(
                source,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            source_tree_sha = tree_inventory_sha256(inventory)
            if (
                source_tree_sha != plan.tree_inventory_sha256
                or source_tree_sha != plan.source.quarantine_tree_sha256
            ):
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            authority = self.terminal_store.foundation.acquire_detached_run_authority(run_id)
            self._require_product_binding(marker, authority)
            authority.revalidate()
            locked_current = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if (
                locked_current is None
                or cleanup_pointer_sha256(locked_current[0]) != prior_pointer_sha
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
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
            approval = authorize(clock(), None)
            approval_sha = cleanup_approval_binding_sha256(approval)
            journal_at = clock()
            control = self._run_control(run_hash)
            transaction_root = control / "transactions" / transaction_id
            revision_two = control / "revisions" / f"r2-{transaction_id}"
            revision_three = control / "revisions" / f"r3-{transaction_id}"
            if transaction_root.exists() or revision_two.exists() or revision_three.exists():
                raise _fail(
                    CleanupFailureCode.IDEMPOTENCY_CONFLICT,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            transaction_base: dict[str, Any] = {
                "schema_version": "1.0.0",
                "run_id_sha256": run_hash,
                "transaction_id": transaction_id,
                "proposed_revision": 2,
                "expected_revision": 1,
                "expected_pointer_sha256": prior_pointer_sha,
                "action_kind": CleanupActionKind.DELETE_QUARANTINE_PAYLOAD,
                "execution_id": plan.execution_id,
                "idempotency_key": plan.idempotency_key,
                "plan": plan,
                "plan_sha256": plan_sha,
                "approval_binding": approval,
                "approval_binding_sha256": approval_sha,
                "source_tree_sha256": source_tree_sha,
                "created_at": journal_at,
            }
            transaction_two = CleanupTransactionV1(
                **transaction_base,
                transaction_sha256=cleanup_transaction_sha256(transaction_base),
            )
            projected = _control_tree_bytes(
                control,
                maximum_bytes=marker.limits.maximum_control_bytes_per_run,
            ) + (
                8 * marker.limits.maximum_control_artifact_bytes
                + 3 * len(canonical_cleanup_bytes(transaction_two))
            )
            if projected > marker.limits.maximum_control_bytes_per_run:
                raise _fail(
                    CleanupFailureCode.AUDIT_CAPACITY_EXCEEDED,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            _fault(self.fault_injector, "delete.before_journal")
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            transaction_root.mkdir()
            transaction_root_created = True
            _write_exclusive(
                transaction_root / "transaction.json",
                canonical_cleanup_bytes(transaction_two),
                max_bytes=marker.limits.maximum_control_artifact_bytes,
                fault_injector=self.fault_injector,
                fault_hook="delete.journal",
            )
            journal_published = True
            _directory_sync(transaction_root)
            _fault(self.fault_injector, "delete.before_staging_rename")
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            effect_at = clock()
            final_approval = authorize(effect_at, transaction_two)
            if final_approval != approval:
                raise _fail(
                    CleanupFailureCode.APPROVAL_MISMATCH,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            authority.revalidate()
            final_current = self._read_current(
                run_hash,
                pending_transaction=transaction_two,
                expected_marker=(marker, marker_sha),
            )
            if (
                final_current is None
                or cleanup_pointer_sha256(final_current[0]) != prior_pointer_sha
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            final_inventory = scan_cleanup_tree(
                source,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(final_inventory) != source_tree_sha:
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            final_destination = _namespace_child(
                self.cleanup_root / "deleting",
                planned_destination.name,
                require_exact=False,
            )
            if final_destination != destination:
                raise _fail(CleanupFailureCode.INVALID_PLAN, run_id_sha256=run_hash)
            if (
                _strict_lexists(destination)
                or source.stat().st_dev != destination.parent.stat().st_dev
            ):
                raise _fail(
                    (
                        CleanupFailureCode.IDEMPOTENCY_CONFLICT
                        if _strict_lexists(destination)
                        else CleanupFailureCode.CROSS_VOLUME
                    ),
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            os.replace(source, destination)
            staging_moved = True
            _fault(self.fault_injector, "delete.after_staging_rename")
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source remained after staging rename")
            staged_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(staged_inventory) != source_tree_sha:
                raise CanonicalStorageError("delete staging tree changed")
            rename_directory_sync = _directory_sync_many(source.parent, destination.parent)
            prepared_at = clock()
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source reappeared after prepare clock")
            prepared_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(prepared_inventory) != source_tree_sha:
                raise CanonicalStorageError("delete staging changed after prepare clock")
            prepared_current = self._read_current(
                run_hash,
                pending_transaction=transaction_two,
                expected_marker=(marker, marker_sha),
            )
            if (
                prepared_current is None
                or cleanup_pointer_sha256(prepared_current[0]) != prior_pointer_sha
            ):
                raise CanonicalStorageError("cleanup delete CAS changed after prepare clock")

            revision_two.mkdir()
            receipt_two = CleanupReceiptV1(
                action_kind=CleanupActionKind.DELETE_QUARANTINE_PAYLOAD,
                run_id_sha256=run_hash,
                transaction_id=transaction_id,
                execution_id=plan.execution_id,
                idempotency_key=plan.idempotency_key,
                plan_sha256=plan_sha,
                approval_binding_sha256=approval_sha,
                actor_sha256=approval.actor_sha256,
                authority_snapshot_sha256=approval.authority_snapshot_sha256,
                source_tree_sha256=source_tree_sha,
                result_state=CleanupState.DELETE_PREPARED,
                effect_started_at=effect_at,
                committed_at=prepared_at,
                durability=CleanupDurabilityEvidenceV1(
                    platform_adapter=cast(Any, platform_adapter()),
                    journal_file_sync="confirmed",
                    effect_rename="confirmed",
                    control_file_sync="confirmed",
                    directory_sync=rename_directory_sync,
                    pointer_replace="confirmed",
                    reconciliation="confirmed",
                ),
            )
            receipt_two_sha = cleanup_receipt_sha256(receipt_two)
            tombstone_two = CleanupTombstoneV1(
                run_id_sha256=run_hash,
                source_pointer_sha256=prior_tombstone.source_pointer_sha256,
                source_manifest_sha256=prior_tombstone.source_manifest_sha256,
                quarantine_tree_sha256=source_tree_sha,
                state=CleanupState.DELETE_PREPARED,
                receipt_sha256=receipt_two_sha,
                quarantine_entered_at=prior_tombstone.quarantine_entered_at,
                receipt_retain_until=prepared_at + timedelta(days=365),
            )
            tombstone_two_sha = cleanup_tombstone_sha256(tombstone_two)
            manifest_two = CleanupManifestV1(
                run_id_sha256=run_hash,
                revision=2,
                transaction_id=transaction_id,
                previous_revision=1,
                previous_manifest_sha256=prior_manifest_sha,
                expected_pointer_sha256=prior_pointer_sha,
                state=CleanupState.DELETE_PREPARED,
                action_kind=CleanupActionKind.DELETE_QUARANTINE_PAYLOAD,
                execution_id=plan.execution_id,
                idempotency_key=plan.idempotency_key,
                plan=plan,
                plan_sha256=plan_sha,
                approval_binding=approval,
                approval_binding_sha256=approval_sha,
                source_tree_sha256=source_tree_sha,
                receipt_sha256=receipt_two_sha,
                tombstone_sha256=tombstone_two_sha,
                created_at=prepared_at,
            )
            manifest_two_sha = cleanup_manifest_sha256(manifest_two)
            pointer_two = CleanupCurrentPointerV1(
                run_id_sha256=run_hash,
                revision=2,
                transaction_id=transaction_id,
                revision_relative_path=f"runs/{run_hash}/revisions/r2-{transaction_id}",
                state=CleanupState.DELETE_PREPARED,
                manifest_sha256=manifest_two_sha,
                receipt_sha256=receipt_two_sha,
                tombstone_sha256=tombstone_two_sha,
                published_at=prepared_at,
            )
            for name, value in (
                ("transaction.json", transaction_two),
                ("manifest.json", manifest_two),
                ("receipt.json", receipt_two),
                ("tombstone.json", tombstone_two),
            ):
                _write_exclusive(
                    revision_two / name,
                    canonical_cleanup_bytes(value),
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
            _directory_sync(revision_two)
            pending_current = self._read_current(
                run_hash,
                pending_transaction=transaction_two,
                expected_marker=(marker, marker_sha),
            )
            if (
                pending_current is None
                or cleanup_pointer_sha256(pending_current[0]) != prior_pointer_sha
            ):
                raise CanonicalStorageError("cleanup delete CAS changed before prepare")
            pointer_two_bytes = canonical_cleanup_bytes(pointer_two)
            temporary_two = control / f"current.{transaction_id}.prepare.tmp"
            _write_exclusive(
                temporary_two,
                pointer_two_bytes,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )
            _fault(self.fault_injector, "delete.before_prepare_pointer_replace")
            if _strict_lexists(source):
                raise CanonicalStorageError(
                    "quarantine source reappeared before prepare pointer replace"
                )
            pre_prepare_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(pre_prepare_inventory) != source_tree_sha:
                raise CanonicalStorageError("delete staging changed before prepare pointer replace")
            pending_before_prepare = self._read_current(
                run_hash,
                pending_transaction=transaction_two,
                pending_pointer=(temporary_two.name, pointer_two_bytes),
                expected_marker=(marker, marker_sha),
            )
            if (
                pending_before_prepare is None
                or cleanup_pointer_sha256(pending_before_prepare[0]) != prior_pointer_sha
            ):
                raise CanonicalStorageError(
                    "cleanup delete CAS changed before prepare pointer replace"
                )
            prepare_pointer_attempted = True
            os.replace(temporary_two, control / "current.json")
            _fault(self.fault_injector, "delete.after_prepare_pointer_replace")
            if _strict_lexists(source):
                raise CanonicalStorageError(
                    "quarantine source reappeared after prepare pointer replace"
                )
            published_prepared_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(published_prepared_inventory) != source_tree_sha:
                raise CanonicalStorageError("delete staging changed after prepare pointer replace")
            if (control / "current.json").read_bytes() != pointer_two_bytes:
                raise CanonicalStorageError("delete-prepared current reread mismatch")
            _directory_sync(control)
            prepared_published = True
            verified_two = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if verified_two is None or verified_two[0] != pointer_two:
                raise CanonicalStorageError("delete-prepared state did not verify")
            staging_root_info = destination.lstat()
            staging_root_attributes = getattr(staging_root_info, "st_file_attributes", 0)
            if (
                not stat.S_ISDIR(staging_root_info.st_mode)
                or stat.S_ISLNK(staging_root_info.st_mode)
                or staging_root_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or _has_nondefault_windows_stream(destination)
            ):
                raise CanonicalStorageError("delete staging root identity changed")
            staging_root_identity = (
                staging_root_info.st_dev,
                staging_root_info.st_ino,
            )
            staging_parent_chain = _capture_directory_chain(
                destination.parent,
                anchor=self.cleanup_root,
            )

            _fault(self.fault_injector, "delete.before_unlink_start")
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            deleted_entries = _unlink_inventory_tree(
                destination,
                staged_inventory,
                expected_parent_chain=staging_parent_chain,
                expected_root_identity=staging_root_identity,
                fault_injector=self.fault_injector,
                progress=delete_progress,
                cancelled=cancelled,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            if _strict_lexists(destination):
                raise CanonicalStorageError("delete staging remained after unlink")
            delete_directory_sync = _directory_sync(destination.parent)
            final_at = clock()
            if _strict_lexists(source) or _strict_lexists(destination):
                raise CanonicalStorageError("delete payload namespace reappeared after final clock")
            current_after_final_clock = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if current_after_final_clock is None or current_after_final_clock[0] != pointer_two:
                raise CanonicalStorageError("cleanup delete CAS changed after final clock")
            transaction_three_base = transaction_base | {
                "proposed_revision": 3,
                "expected_revision": 2,
                "expected_pointer_sha256": cleanup_pointer_sha256(pointer_two),
                "created_at": final_at,
            }
            transaction_three = CleanupTransactionV1(
                **transaction_three_base,
                transaction_sha256=cleanup_transaction_sha256(transaction_three_base),
            )
            revision_three.mkdir()
            receipt_three = CleanupReceiptV1(
                action_kind=CleanupActionKind.DELETE_QUARANTINE_PAYLOAD,
                run_id_sha256=run_hash,
                transaction_id=transaction_id,
                execution_id=plan.execution_id,
                idempotency_key=plan.idempotency_key,
                plan_sha256=plan_sha,
                approval_binding_sha256=approval_sha,
                actor_sha256=approval.actor_sha256,
                authority_snapshot_sha256=approval.authority_snapshot_sha256,
                source_tree_sha256=source_tree_sha,
                result_state=CleanupState.DELETED,
                effect_started_at=effect_at,
                committed_at=final_at,
                durability=CleanupDurabilityEvidenceV1(
                    platform_adapter=cast(Any, platform_adapter()),
                    journal_file_sync="confirmed",
                    effect_rename="confirmed",
                    control_file_sync="confirmed",
                    directory_sync=delete_directory_sync,
                    pointer_replace="confirmed",
                    reconciliation="confirmed",
                ),
            )
            receipt_three_sha = cleanup_receipt_sha256(receipt_three)
            tombstone_three = CleanupTombstoneV1(
                run_id_sha256=run_hash,
                source_pointer_sha256=prior_tombstone.source_pointer_sha256,
                source_manifest_sha256=prior_tombstone.source_manifest_sha256,
                quarantine_tree_sha256=source_tree_sha,
                state=CleanupState.DELETED,
                receipt_sha256=receipt_three_sha,
                quarantine_entered_at=prior_tombstone.quarantine_entered_at,
                receipt_retain_until=final_at + timedelta(days=365),
            )
            tombstone_three_sha = cleanup_tombstone_sha256(tombstone_three)
            manifest_three = CleanupManifestV1(
                run_id_sha256=run_hash,
                revision=3,
                transaction_id=transaction_id,
                previous_revision=2,
                previous_manifest_sha256=manifest_two_sha,
                expected_pointer_sha256=cleanup_pointer_sha256(pointer_two),
                state=CleanupState.DELETED,
                action_kind=CleanupActionKind.DELETE_QUARANTINE_PAYLOAD,
                execution_id=plan.execution_id,
                idempotency_key=plan.idempotency_key,
                plan=plan,
                plan_sha256=plan_sha,
                approval_binding=approval,
                approval_binding_sha256=approval_sha,
                source_tree_sha256=source_tree_sha,
                receipt_sha256=receipt_three_sha,
                tombstone_sha256=tombstone_three_sha,
                created_at=final_at,
            )
            manifest_three_sha = cleanup_manifest_sha256(manifest_three)
            pointer_three = CleanupCurrentPointerV1(
                run_id_sha256=run_hash,
                revision=3,
                transaction_id=transaction_id,
                revision_relative_path=f"runs/{run_hash}/revisions/r3-{transaction_id}",
                state=CleanupState.DELETED,
                manifest_sha256=manifest_three_sha,
                receipt_sha256=receipt_three_sha,
                tombstone_sha256=tombstone_three_sha,
                published_at=final_at,
            )
            for name, value in (
                ("transaction.json", transaction_three),
                ("manifest.json", manifest_three),
                ("receipt.json", receipt_three),
                ("tombstone.json", tombstone_three),
            ):
                _write_exclusive(
                    revision_three / name,
                    canonical_cleanup_bytes(value),
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
            _directory_sync(revision_three)
            verified_before_final = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if verified_before_final is None or verified_before_final[0] != pointer_two:
                raise CanonicalStorageError("cleanup delete CAS changed before final")
            pointer_three_bytes = canonical_cleanup_bytes(pointer_three)
            temporary_three = control / f"current.{transaction_id}.deleted.tmp"
            _fault(self.fault_injector, "delete.before_final_pointer_temp_write")
            _write_exclusive(
                temporary_three,
                pointer_three_bytes,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )
            _fault(self.fault_injector, "delete.before_final_pointer_replace")
            if _strict_lexists(source) or _strict_lexists(destination):
                raise CanonicalStorageError(
                    "delete payload namespace reappeared before final pointer replace"
                )
            current_before_final_replace = self._read_current(
                run_hash,
                pending_pointer=(temporary_three.name, pointer_three_bytes),
                expected_marker=(marker, marker_sha),
            )
            if (
                current_before_final_replace is None
                or current_before_final_replace[0] != pointer_two
            ):
                raise CanonicalStorageError(
                    "cleanup delete CAS changed before final pointer replace"
                )
            final_pointer_attempted = True
            os.replace(temporary_three, control / "current.json")
            _fault(self.fault_injector, "delete.after_final_pointer_replace")
            if _strict_lexists(source) or _strict_lexists(destination):
                raise CanonicalStorageError(
                    "delete payload namespace reappeared after final pointer replace"
                )
            if (control / "current.json").read_bytes() != pointer_three_bytes:
                raise CanonicalStorageError("deleted current reread mismatch")
            _directory_sync(control)
            verified_three = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if verified_three is None or verified_three[0] != pointer_three:
                raise CanonicalStorageError("deleted state did not verify")
            return CleanupExecutionResultV1(
                outcome_kind="committed",
                run_id_sha256=run_hash,
                execution_id=plan.execution_id,
                idempotency_key=plan.idempotency_key,
                transaction_id=transaction_id,
                plan_sha256=plan_sha,
                cleanup_revision=3,
                cleanup_pointer_sha256=cleanup_pointer_sha256(pointer_three),
                receipt=receipt_three,
                receipt_sha256=receipt_three_sha,
                tombstone=tombstone_three,
                tombstone_sha256=tombstone_three_sha,
            )
        except (ProductRunError, RunStorageError) as exc:
            code = (
                CleanupFailureCode.RUN_LOCKED
                if exc.failure.code.value == "run_locked"
                else CleanupFailureCode.STALE_SOURCE
            )
            deleted_entries = max(deleted_entries, delete_progress[0])
            if staging_moved:
                filesystem_effect, staging_unreadable = observed_staging_failure()
                current_uncertain = current_failure_uncertain()
                effect_uncertain = staging_unreadable or current_uncertain
                raise _fail(
                    (
                        CleanupFailureCode.EFFECT_UNKNOWN
                        if effect_uncertain
                        else CleanupFailureCode.RECONCILIATION_REQUIRED
                    ),
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect=filesystem_effect,
                    domain_effect=(
                        "current_may_have_advanced"
                        if effect_uncertain
                        else "current_advanced"
                        if prepared_published
                        else "current_unchanged"
                    ),
                ) from exc
            if journal_published or transaction_root_created:
                if _rollback_pre_effect_journal(transaction_root):
                    journal_published = False
                    transaction_root_created = False
                else:
                    raise _fail(
                        CleanupFailureCode.RECONCILIATION_REQUIRED,
                        run_id_sha256=run_hash,
                        plan_sha256=plan_sha,
                        transaction_id=transaction_id,
                        filesystem_effect="journal_only",
                        domain_effect="current_unchanged",
                    ) from exc
            raise _fail(
                code,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            ) from exc
        except CleanupStorageError as exc:
            deleted_entries = max(deleted_entries, delete_progress[0])
            if final_pointer_attempted or (prepare_pointer_attempted and not prepared_published):
                filesystem_effect = (
                    observed_staging_failure()[0] if staging_moved else "delete_staging_moved"
                )
                raise _fail(
                    CleanupFailureCode.EFFECT_UNKNOWN,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect=filesystem_effect,
                    domain_effect="current_may_have_advanced",
                ) from exc
            if staging_moved:
                filesystem_effect, staging_unreadable = observed_staging_failure()
                current_uncertain = current_failure_uncertain()
                effect_uncertain = staging_unreadable or current_uncertain
                raise _fail(
                    (
                        CleanupFailureCode.EFFECT_UNKNOWN
                        if effect_uncertain
                        else CleanupFailureCode.RECONCILIATION_REQUIRED
                    ),
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect=filesystem_effect,
                    domain_effect=(
                        "current_may_have_advanced"
                        if effect_uncertain
                        else "current_advanced"
                        if prepared_published
                        else "current_unchanged"
                    ),
                ) from exc
            if journal_published or transaction_root_created:
                if _rollback_pre_effect_journal(transaction_root):
                    journal_published = False
                    transaction_root_created = False
                    raise
                raise _fail(
                    CleanupFailureCode.RECONCILIATION_REQUIRED,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect="journal_only",
                    domain_effect="current_unchanged",
                ) from exc
            raise
        except Exception as exc:
            deleted_entries = max(deleted_entries, delete_progress[0])
            staging_effect, staging_unreadable = (
                observed_staging_failure() if staging_moved else ("partial_delete", False)
            )
            current_uncertain = current_failure_uncertain() if staging_moved else False
            effect_uncertain = staging_unreadable or current_uncertain
            if final_pointer_attempted or (prepare_pointer_attempted and not prepared_published):
                code = CleanupFailureCode.EFFECT_UNKNOWN
                domain_effect = "current_may_have_advanced"
            elif staging_moved:
                code = (
                    CleanupFailureCode.EFFECT_UNKNOWN
                    if effect_uncertain
                    else CleanupFailureCode.RECONCILIATION_REQUIRED
                )
                domain_effect = (
                    "current_may_have_advanced"
                    if effect_uncertain
                    else "current_advanced"
                    if prepared_published
                    else "current_unchanged"
                )
            elif journal_published or transaction_root_created:
                if _rollback_pre_effect_journal(transaction_root):
                    journal_published = False
                    transaction_root_created = False
                    code = CleanupFailureCode.INTERNAL_INVARIANT_ERROR
                    domain_effect = "none"
                else:
                    code = CleanupFailureCode.RECONCILIATION_REQUIRED
                    domain_effect = "current_unchanged"
            else:
                code = CleanupFailureCode.INTERNAL_INVARIANT_ERROR
                domain_effect = "none"
            generic_filesystem_effect = (
                staging_effect
                if staging_moved
                else "journal_only"
                if journal_published or transaction_root_created
                else "none"
            )
            raise _fail(
                code,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
                filesystem_effect=cast(Any, generic_filesystem_effect),
                domain_effect=cast(Any, domain_effect),
            ) from exc
        finally:
            deleted_entries = max(deleted_entries, delete_progress[0])
            if authority is not None:
                try:
                    authority.release()
                except LockReleaseError as exc:
                    release_staging_effect = (
                        observed_staging_failure()[0] if staging_moved else "partial_delete"
                    )
                    raise _fail(
                        CleanupFailureCode.EFFECT_UNKNOWN,
                        run_id_sha256=run_hash,
                        plan_sha256=plan_sha,
                        transaction_id=transaction_id,
                        filesystem_effect=(
                            release_staging_effect
                            if staging_moved
                            else "journal_only"
                            if journal_published or transaction_root_created
                            else "none"
                        ),
                        domain_effect="current_may_have_advanced",
                    ) from exc


__all__ = [
    "CleanupStorageError",
    "LocalDataCleanupStore",
    "initialize_cleanup_root",
    "inspect_cleanup_root",
    "scan_cleanup_tree",
]
