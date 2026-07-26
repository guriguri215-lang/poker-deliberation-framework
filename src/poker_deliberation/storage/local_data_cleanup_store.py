"""Durable local-only P2-027B cleanup root and quarantine storage."""

from __future__ import annotations

import hashlib
import os
import stat
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
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
    run_id_sha256,
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
_PRODUCT_ROOT_ENTRIES = frozenset(
    {"ownership.json", ".revision-init.authority.lock", ".revision-control", "runs"}
)
_NAMESPACE_ENTRY_LIMIT = 10_000
_RUN_CONTROL_ENTRIES = frozenset({"transactions", "revisions", "current.json"})
_StreamSignature = tuple[tuple[str, int], ...]
_DirectoryChain = tuple[tuple[Path, int, int, _StreamSignature | None], ...]
_DirectoryChains = tuple[_DirectoryChain, ...]
_ControlEntrySnapshot = tuple[
    str,
    Literal["directory", "file"],
    int,
    int,
    int,
    str | None,
    _StreamSignature,
]


@dataclass(frozen=True)
class _ControlTreeSnapshot:
    control: Path
    maximum_bytes: int
    directory_chains: _DirectoryChains
    entries: tuple[_ControlEntrySnapshot, ...]


@dataclass(frozen=True)
class _OptionalControlSnapshot:
    control: Path
    parent_chain: _DirectoryChain
    tree: _ControlTreeSnapshot | None


@dataclass(frozen=True)
class _ExactFileSnapshot:
    path: Path
    identity: tuple[int, int]
    size: int
    content_sha256: str
    streams: _StreamSignature


@dataclass(frozen=True)
class _ProductAuthoritySnapshot:
    directory_chains: _DirectoryChains
    root_entries: _DirectoryEntriesSnapshot
    ownership: _ExactFileSnapshot
    root_authority: _ExactFileSnapshot


@dataclass(frozen=True)
class _DirectoryEntriesSnapshot:
    path: Path
    chain: _DirectoryChain
    maximum_entries: int
    maximum_bytes: int
    entries: tuple[_ControlEntrySnapshot, ...]


@dataclass(frozen=True)
class _InitializationStateSnapshot:
    root: Path
    parent_chain: _DirectoryChain
    root_present: bool
    directories: tuple[_DirectoryEntriesSnapshot, ...]
    run_control: _OptionalControlSnapshot | None


@dataclass(frozen=True)
class _NamespaceTreeSnapshot:
    path: Path
    parent_chain: _DirectoryChain
    target_chain: _DirectoryChain | None
    maximum_entries: int
    state: Literal["absent", "present"]
    tree_sha256: str | None


@dataclass(frozen=True)
class _RollbackJournal:
    transaction_root: Path
    transaction_identity: tuple[int, int]
    transaction_size: int
    transaction_bytes: bytes
    transaction_streams: _StreamSignature
    transaction_root_chain: _DirectoryChain
    empty_directory_chains: tuple[_DirectoryChain, ...]


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
    expected: _RollbackJournal | None,
) -> bool:
    """Remove only the exact journal/scaffold created by this attempt."""

    if expected is None:
        return False
    try:
        _verify_directory_chain(expected.transaction_root_chain)
        for chain in expected.empty_directory_chains:
            _verify_directory_chain(chain)
        transaction = expected.transaction_root / "transaction.json"
        info = verify_regular_single_link(transaction)
        if (
            (info.st_dev, info.st_ino) != expected.transaction_identity
            or info.st_size != expected.transaction_size
            or _windows_stream_signature(transaction) != expected.transaction_streams
            or transaction.read_bytes() != expected.transaction_bytes
        ):
            return False
        _verify_directory_chain(expected.transaction_root_chain)
        for chain in expected.empty_directory_chains:
            _verify_directory_chain(chain)
        transaction.unlink()
        _verify_directory_chain(expected.transaction_root_chain)
        expected.transaction_root.rmdir()
        for chain in expected.empty_directory_chains:
            _verify_directory_chain(chain)
            chain[-1][0].rmdir()
        return True
    except (CanonicalStorageError, OSError):
        return False


def _capture_pre_effect_journal(
    transaction_root: Path,
    transaction_bytes: bytes,
    *,
    owned_anchor: Path,
    empty_directories: tuple[Path, ...] = (),
) -> _RollbackJournal:
    transaction = transaction_root / "transaction.json"
    info = verify_regular_single_link(transaction)
    streams = _windows_stream_signature(transaction)
    if any(name != "::$DATA" for name, _size in streams):
        raise CanonicalStorageError("cleanup journal has an alternate data stream")
    return _RollbackJournal(
        transaction_root=transaction_root,
        transaction_identity=(info.st_dev, info.st_ino),
        transaction_size=len(transaction_bytes),
        transaction_bytes=transaction_bytes,
        transaction_streams=streams,
        transaction_root_chain=_capture_directory_chain(
            transaction_root,
            owned_anchor=owned_anchor,
        ),
        empty_directory_chains=tuple(
            _capture_directory_chain(directory, owned_anchor=owned_anchor)
            for directory in empty_directories
        ),
    )


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
    fault_preparer: Callable[[], None] | None = None,
) -> None:
    if not data or len(data) > max_bytes:
        raise CanonicalStorageError("cleanup control artifact exceeds its byte limit")
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        if fault_hook is not None:
            if fault_preparer is not None:
                fault_preparer()
            _fault(fault_injector, f"{fault_hook}.after_write")
        os.fsync(stream.fileno())
    if _read_control_bytes(path, max_bytes=max_bytes) != data:
        raise CanonicalStorageError("cleanup control artifact reread mismatch")


def _read_control_bytes(path: Path, *, max_bytes: int) -> bytes:
    try:
        before = verify_regular_single_link(path)
        if before.st_size > max_bytes:
            raise CanonicalStorageError("cleanup control artifact exceeds its byte limit")
        if _has_nondefault_windows_stream(path):
            raise CanonicalStorageError("cleanup control artifact has an alternate data stream")
        data = path.read_bytes()
        after = verify_regular_single_link(path)
        if (
            len(data) != before.st_size
            or (after.st_dev, after.st_ino, after.st_size)
            != (before.st_dev, before.st_ino, before.st_size)
            or _has_nondefault_windows_stream(path)
        ):
            raise CanonicalStorageError("cleanup control artifact changed during read")
    except CanonicalStorageError:
        raise
    except OSError as exc:
        raise CanonicalStorageError("cleanup control artifact read failed") from exc
    return data


def _read_control(path: Path, model: type[Any], *, max_bytes: int) -> tuple[Any, bytes]:
    data = _read_control_bytes(path, max_bytes=max_bytes)
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
    if not _strict_lexists(root):
        return CleanupRootInspectionV1(status="uninitialized")
    try:
        verify_directory(root)
        if _has_nondefault_windows_stream(root):
            raise CanonicalStorageError("cleanup root has an alternate data stream")
        entries: dict[str, Path] = {}
        for entry in root.iterdir():
            if len(entries) >= len(_ROOT_ENTRIES):
                recognized = tuple(
                    sorted(set(entries) & _ROOT_ENTRIES, key=lambda item: item.encode())
                )
                return CleanupRootInspectionV1(
                    status="corrupt",
                    recognized_relative_paths=cast(Any, recognized),
                )
            entries[entry.name] = entry
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
    mutation_started = False
    reconciliation_reported = False
    authority: ExistingRunAuthorityV1 | None = None
    product_authority_snapshot: _ProductAuthoritySnapshot | None = None

    def release_and_verify_success(
        result: CleanupRootInitializationOutcomeV1,
        *,
        product_identity_sha256: str,
        product_ownership_marker_sha256: str,
        marker_sha256: str,
    ) -> CleanupRootInitializationOutcomeV1:
        nonlocal authority
        if authority is None:
            raise CanonicalStorageError("cleanup initialization authority disappeared")
        if product_authority_snapshot is None:
            raise CanonicalStorageError("product authority snapshot is unavailable")
        initialization_state = _capture_initialization_state(
            root,
            run_hash=run_id_sha256(existing_run_id),
            maximum_entries=_NAMESPACE_ENTRY_LIMIT,
            maximum_bytes=limits.maximum_control_bytes_per_run,
        )
        held_authority = authority
        authority = None
        try:
            held_authority.release()
        except LockReleaseError as exc:
            raise _fail(CleanupFailureCode.EFFECT_UNKNOWN) from exc
        _verify_product_authority_snapshot(
            product_authority_snapshot,
            terminal_store=terminal_store,
        )
        _verify_initialization_state(initialization_state)
        released_inspection = inspect_cleanup_root(
            root,
            expected_product_root_identity_sha256=product_identity_sha256,
            expected_product_ownership_marker_sha256=product_ownership_marker_sha256,
            max_artifact_bytes=limits.maximum_control_artifact_bytes,
        )
        if (
            released_inspection.status != "initialized"
            or released_inspection.root_id != root_id
            or released_inspection.marker_sha256 != marker_sha256
        ):
            raise CanonicalStorageError("cleanup root changed after authority release")
        _verify_product_authority_snapshot(
            product_authority_snapshot,
            terminal_store=terminal_store,
        )
        _verify_initialization_state(initialization_state)
        return result

    try:
        authority = terminal_store.foundation.acquire_existing_run_authority(existing_run_id)
        product_authority_snapshot = _capture_product_authority_snapshot(terminal_store)
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
            if inspection.marker_sha256 is None:
                raise CanonicalStorageError("initialized cleanup root lacks marker identity")
            return release_and_verify_success(
                CleanupRootInitializationOutcomeV1(
                    outcome_kind="already_initialized",
                    root_id=root_id,
                    marker_sha256=inspection.marker_sha256,
                    filesystem_effect="none",
                    durability=_idle_durability(),
                ),
                product_identity_sha256=product_identity,
                product_ownership_marker_sha256=authority.ownership_marker_sha256,
                marker_sha256=inspection.marker_sha256,
            )
        if inspection.status not in {"uninitialized"}:
            raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED)
        if not _strict_lexists(root.parent):
            raise _fail(CleanupFailureCode.PATH_CONFINEMENT_FAILED)
        verify_directory(root.parent)
        if root.parent.stat().st_dev != authority.run_path.stat().st_dev:
            raise _fail(CleanupFailureCode.CROSS_VOLUME)
        if _strict_lexists(root) and any(root.iterdir()):
            raise _fail(CleanupFailureCode.OWNERSHIP_UNVERIFIED)
        _fault(fault_injector, "initialize.before_root")
        if not _strict_lexists(root):
            mutation_started = True
            root.mkdir()
            created = True
        verify_directory(root)
        if root.stat().st_dev != authority.run_path.stat().st_dev:
            raise _fail(CleanupFailureCode.CROSS_VOLUME)
        for name in (".cleanup-control", "runs", "quarantine", "deleting"):
            _fault(fault_injector, f"initialize.before_mkdir.{name}")
            mutation_started = True
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
        return release_and_verify_success(
            CleanupRootInitializationOutcomeV1(
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
            ),
            product_identity_sha256=product_identity,
            product_ownership_marker_sha256=authority.ownership_marker_sha256,
            marker_sha256=marker_sha,
        )
    except CleanupStorageError:
        raise
    except Exception as exc:
        if mutation_started or created:
            reconciliation_reported = True
            return CleanupRootInitializationOutcomeV1(
                outcome_kind="reconciliation_required",
                root_id=root_id,
                marker_sha256=None,
                filesystem_effect="control_only",
                durability=_idle_durability(reconciliation="required"),
            )
        try:
            root_present = _strict_lexists(root)
        except OSError:
            root_present = False
        if root_present:
            reconciliation_reported = True
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
            release_state_uncertain = False
            initialization_state: _InitializationStateSnapshot | None = None
            try:
                initialization_state = _capture_initialization_state(
                    root,
                    run_hash=run_id_sha256(existing_run_id),
                    maximum_entries=_NAMESPACE_ENTRY_LIMIT,
                    maximum_bytes=limits.maximum_control_bytes_per_run,
                )
            except Exception:
                release_state_uncertain = True
            held_authority = authority
            authority = None
            try:
                held_authority.release()
            except LockReleaseError as exc:
                raise _fail(CleanupFailureCode.EFFECT_UNKNOWN) from exc
            try:
                if product_authority_snapshot is None:
                    raise CanonicalStorageError(
                        "cleanup initialization release state is unavailable"
                    )
                _verify_product_authority_snapshot(
                    product_authority_snapshot,
                    terminal_store=terminal_store,
                )
                if release_state_uncertain or initialization_state is None:
                    if not reconciliation_reported:
                        raise CanonicalStorageError(
                            "cleanup initialization release state is unavailable"
                        )
                else:
                    _verify_initialization_state(initialization_state)
            except Exception as exc:
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
    discovered_entries = 0

    def walk(directory: Path) -> None:
        nonlocal discovered_entries, total_bytes
        children: list[Path] = []
        aliases: set[str] = set()
        for child in directory.iterdir():
            if discovered_entries >= limits.maximum_tree_entries:
                raise _fail(
                    CleanupFailureCode.CAPACITY_EXCEEDED,
                    run_id_sha256=run_id_sha256,
                )
            discovered_entries += 1
            normalized = unicodedata.normalize("NFC", child.name)
            alias = normalized.casefold()
            if normalized != child.name or alias in aliases:
                raise _fail(
                    CleanupFailureCode.ALIAS_CONFLICT,
                    run_id_sha256=run_id_sha256,
                )
            aliases.add(alias)
            children.append(child)
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
    owned_anchor: Path,
) -> _DirectoryChain:
    absolute = Path(os.path.abspath(path))
    owned_at = Path(os.path.abspath(owned_anchor))
    if absolute != owned_at and owned_at not in absolute.parents:
        raise CanonicalStorageError("delete staging parent escaped cleanup root")
    current = Path(absolute.anchor)
    observed: list[tuple[Path, int, int, tuple[tuple[str, int], ...] | None]] = []
    for index, part in enumerate(absolute.parts):
        if index:
            current /= part
        info = current.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        owned_component = current == owned_at or owned_at in current.parents
        try:
            stream_signature = _windows_stream_signature(current)
        except OSError:
            if owned_component:
                raise
            stream_signature = None
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
            or (
                owned_component
                and (
                    stream_signature is None
                    or any(name != "::$DATA" for name, _size in stream_signature)
                )
            )
        ):
            raise CanonicalStorageError("delete staging ancestor identity changed")
        observed.append((current, info.st_dev, info.st_ino, stream_signature))
    return tuple(observed)


def _verify_directory_chain(
    expected: _DirectoryChain,
) -> None:
    for path, expected_dev, expected_ino, expected_streams in expected:
        info = path.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        try:
            observed_streams = _windows_stream_signature(path)
        except OSError:
            observed_streams = None
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
            or (info.st_dev, info.st_ino) != (expected_dev, expected_ino)
            or observed_streams != expected_streams
        ):
            raise CanonicalStorageError("delete staging ancestor identity changed")


def _capture_control_tree_snapshot(
    control: Path,
    *,
    owned_anchor: Path,
    maximum_bytes: int,
) -> _ControlTreeSnapshot:
    chains: list[_DirectoryChain] = []
    entries: list[_ControlEntrySnapshot] = []
    stack = [control]
    total_bytes = 0
    while stack:
        directory = stack.pop()
        chains.append(
            _capture_directory_chain(
                directory,
                owned_anchor=owned_anchor,
            )
        )
        if len(chains) > 256:
            raise CanonicalStorageError("cleanup control directory capacity exceeded")
        for child in directory.iterdir():
            if len(entries) >= 256:
                raise CanonicalStorageError("cleanup control entry capacity exceeded")
            info = child.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
                raise CanonicalStorageError("cleanup control directory contains a link")
            streams = _windows_stream_signature(child)
            if any(name != "::$DATA" for name, _size in streams):
                raise CanonicalStorageError("cleanup control entry has an alternate data stream")
            relative = child.relative_to(control).as_posix()
            if stat.S_ISDIR(info.st_mode):
                entries.append(
                    (
                        relative,
                        "directory",
                        info.st_dev,
                        info.st_ino,
                        0,
                        None,
                        streams,
                    )
                )
                stack.append(child)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if info.st_size > maximum_bytes - total_bytes:
                    raise CanonicalStorageError("cleanup control tree exceeds its byte limit")
                data = child.read_bytes()
                after = verify_regular_single_link(child)
                after_streams = _windows_stream_signature(child)
                if (
                    len(data) != info.st_size
                    or (after.st_dev, after.st_ino, after.st_size)
                    != (info.st_dev, info.st_ino, info.st_size)
                    or after_streams != streams
                ):
                    raise CanonicalStorageError("cleanup control entry changed during snapshot")
                total_bytes += len(data)
                entries.append(
                    (
                        relative,
                        "file",
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        hashlib.sha256(data).hexdigest(),
                        streams,
                    )
                )
            else:
                raise CanonicalStorageError("cleanup control entry is not a regular artifact")
    snapshot = _ControlTreeSnapshot(
        control=control,
        maximum_bytes=maximum_bytes,
        directory_chains=tuple(
            sorted(
                chains,
                key=lambda chain: str(chain[-1][0]).encode("utf-8"),
            )
        ),
        entries=tuple(sorted(entries, key=lambda item: item[0].encode("utf-8"))),
    )
    _verify_directory_chains(snapshot.directory_chains)
    return snapshot


def _verify_directory_chains(expected: _DirectoryChains) -> None:
    if not expected:
        raise CanonicalStorageError("cleanup control directory state is unavailable")
    for chain in expected:
        _verify_directory_chain(chain)


def _capture_exact_file_snapshot(path: Path, *, maximum_bytes: int) -> _ExactFileSnapshot:
    data = _read_control_bytes(path, max_bytes=maximum_bytes)
    info = verify_regular_single_link(path)
    streams = _windows_stream_signature(path)
    return _ExactFileSnapshot(
        path=path,
        identity=(info.st_dev, info.st_ino),
        size=info.st_size,
        content_sha256=hashlib.sha256(data).hexdigest(),
        streams=streams,
    )


def _verify_exact_file_snapshot(
    expected: _ExactFileSnapshot,
    *,
    maximum_bytes: int,
) -> None:
    observed = _capture_exact_file_snapshot(
        expected.path,
        maximum_bytes=maximum_bytes,
    )
    if observed != expected:
        raise CanonicalStorageError("authority file changed")


def _capture_product_authority_snapshot(
    terminal_store: TerminalRunStore,
) -> _ProductAuthoritySnapshot:
    root = Path(os.path.abspath(terminal_store.revision_root))
    legacy_root = Path(os.path.abspath(terminal_store.legacy_runs_root))
    root_entries = _capture_directory_entries_snapshot(
        root,
        owned_anchor=root,
        maximum_entries=len(_PRODUCT_ROOT_ENTRIES),
        maximum_bytes=terminal_store.max_artifact_bytes + 1,
    )
    if {entry[0] for entry in root_entries.entries} != _PRODUCT_ROOT_ENTRIES:
        raise CanonicalStorageError("product root entries changed")
    directory_chains = (
        _capture_directory_chain(legacy_root, owned_anchor=legacy_root),
        *tuple(
            _capture_directory_chain(path, owned_anchor=root)
            for path in (
                root,
                root / ".revision-control",
                root / ".revision-control" / "locks",
                root / "runs",
            )
        ),
    )
    return _ProductAuthoritySnapshot(
        directory_chains=directory_chains,
        root_entries=root_entries,
        ownership=_capture_exact_file_snapshot(
            root / "ownership.json",
            maximum_bytes=terminal_store.max_artifact_bytes,
        ),
        root_authority=_capture_exact_file_snapshot(
            root / ".revision-init.authority.lock",
            maximum_bytes=1,
        ),
    )


def _verify_product_authority_snapshot(
    expected: _ProductAuthoritySnapshot,
    *,
    terminal_store: TerminalRunStore,
) -> None:
    _verify_directory_chains(expected.directory_chains)
    _verify_directory_entries_snapshot(
        expected.root_entries,
        owned_anchor=Path(os.path.abspath(terminal_store.revision_root)),
    )
    if {entry[0] for entry in expected.root_entries.entries} != _PRODUCT_ROOT_ENTRIES:
        raise CanonicalStorageError("product root entries changed")
    _verify_exact_file_snapshot(
        expected.ownership,
        maximum_bytes=terminal_store.max_artifact_bytes,
    )
    _verify_exact_file_snapshot(
        expected.root_authority,
        maximum_bytes=1,
    )
    _verify_directory_chains(expected.directory_chains)


def _capture_directory_entries_snapshot(
    path: Path,
    *,
    owned_anchor: Path,
    maximum_entries: int,
    maximum_bytes: int,
) -> _DirectoryEntriesSnapshot:
    chain = _capture_directory_chain(path, owned_anchor=owned_anchor)
    entries: list[_ControlEntrySnapshot] = []
    total_bytes = 0
    aliases: set[str] = set()
    for child in path.iterdir():
        if len(entries) >= maximum_entries:
            raise CanonicalStorageError("directory entry capacity exceeded")
        normalized = unicodedata.normalize("NFC", child.name)
        alias = normalized.casefold()
        if normalized != child.name or alias in aliases:
            raise CanonicalStorageError("directory entry alias conflict")
        aliases.add(alias)
        info = child.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise CanonicalStorageError("directory entry contains a link")
        streams = _windows_stream_signature(child)
        if any(name != "::$DATA" for name, _size in streams):
            raise CanonicalStorageError("directory entry has an alternate data stream")
        if stat.S_ISDIR(info.st_mode):
            kind: Literal["directory", "file"] = "directory"
            size = 0
            content_sha256 = None
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if info.st_size > maximum_bytes - total_bytes:
                raise CanonicalStorageError("directory snapshot exceeds its byte limit")
            data = child.read_bytes()
            after = verify_regular_single_link(child)
            if (
                len(data) != info.st_size
                or (after.st_dev, after.st_ino, after.st_size)
                != (info.st_dev, info.st_ino, info.st_size)
                or _windows_stream_signature(child) != streams
            ):
                raise CanonicalStorageError("directory entry changed during snapshot")
            kind = "file"
            size = info.st_size
            total_bytes += size
            content_sha256 = hashlib.sha256(data).hexdigest()
        else:
            raise CanonicalStorageError("directory entry is not regular")
        entries.append(
            (
                child.name,
                kind,
                info.st_dev,
                info.st_ino,
                size,
                content_sha256,
                streams,
            )
        )
    snapshot = _DirectoryEntriesSnapshot(
        path=path,
        chain=chain,
        maximum_entries=maximum_entries,
        maximum_bytes=maximum_bytes,
        entries=tuple(sorted(entries, key=lambda item: item[0].encode("utf-8"))),
    )
    _verify_directory_chain(chain)
    return snapshot


def _capture_cleanup_authority_snapshot(
    root: Path,
    *,
    maximum_bytes: int,
) -> _DirectoryEntriesSnapshot:
    snapshot = _capture_directory_entries_snapshot(
        root,
        owned_anchor=root,
        maximum_entries=len(_ROOT_ENTRIES),
        maximum_bytes=maximum_bytes,
    )
    if {entry[0] for entry in snapshot.entries} != _ROOT_ENTRIES:
        raise CanonicalStorageError("cleanup root entries changed")
    return snapshot


def _verify_cleanup_authority_snapshot(
    expected: _DirectoryEntriesSnapshot,
) -> None:
    _verify_directory_entries_snapshot(
        expected,
        owned_anchor=expected.path,
    )
    if {entry[0] for entry in expected.entries} != _ROOT_ENTRIES:
        raise CanonicalStorageError("cleanup root entries changed")


def _verify_directory_entries_snapshot(
    expected: _DirectoryEntriesSnapshot,
    *,
    owned_anchor: Path,
) -> None:
    _verify_directory_chain(expected.chain)
    observed = _capture_directory_entries_snapshot(
        expected.path,
        owned_anchor=owned_anchor,
        maximum_entries=expected.maximum_entries,
        maximum_bytes=expected.maximum_bytes,
    )
    _verify_directory_chain(expected.chain)
    if observed != expected:
        raise CanonicalStorageError("directory entries changed")


def _capture_initialization_state(
    root: Path,
    *,
    run_hash: str,
    maximum_entries: int,
    maximum_bytes: int,
) -> _InitializationStateSnapshot:
    parent_chain = _capture_directory_chain(
        root.parent,
        owned_anchor=root.parent,
    )
    if not _strict_lexists(root):
        return _InitializationStateSnapshot(
            root=root,
            parent_chain=parent_chain,
            root_present=False,
            directories=(),
            run_control=None,
        )
    directories = tuple(
        _capture_directory_entries_snapshot(
            path,
            owned_anchor=root,
            maximum_entries=maximum_entries,
            maximum_bytes=maximum_bytes,
        )
        for path in (
            root,
            *(
                root / name
                for name in (".cleanup-control", "runs", "quarantine", "deleting")
                if _strict_lexists(root / name)
            ),
        )
    )
    return _InitializationStateSnapshot(
        root=root,
        parent_chain=parent_chain,
        root_present=True,
        directories=directories,
        run_control=(
            _capture_optional_control_snapshot(
                root / "runs" / run_hash,
                owned_anchor=root,
                maximum_bytes=maximum_bytes,
            )
            if _strict_lexists(root / "runs")
            else None
        ),
    )


def _verify_initialization_state(
    expected: _InitializationStateSnapshot,
) -> None:
    _verify_directory_chain(expected.parent_chain)
    if _strict_lexists(expected.root) != expected.root_present:
        raise CanonicalStorageError("cleanup initialization root presence changed")
    for directory in expected.directories:
        _verify_directory_entries_snapshot(
            directory,
            owned_anchor=expected.root,
        )
    if expected.run_control is not None:
        _verify_optional_control_snapshot(
            expected.run_control,
            owned_anchor=expected.root,
        )
    _verify_directory_chain(expected.parent_chain)


def _verify_control_tree_snapshot(
    expected: _ControlTreeSnapshot,
    *,
    owned_anchor: Path,
) -> None:
    _verify_directory_chains(expected.directory_chains)
    observed = _capture_control_tree_snapshot(
        expected.control,
        owned_anchor=owned_anchor,
        maximum_bytes=expected.maximum_bytes,
    )
    _verify_directory_chains(expected.directory_chains)
    if (
        observed.directory_chains != expected.directory_chains
        or observed.entries != expected.entries
    ):
        raise CanonicalStorageError("cleanup control tree changed")


def _capture_optional_control_snapshot(
    control: Path,
    *,
    owned_anchor: Path,
    maximum_bytes: int,
) -> _OptionalControlSnapshot:
    parent_chain = _capture_directory_chain(
        control.parent,
        owned_anchor=owned_anchor,
    )
    tree = (
        _capture_control_tree_snapshot(
            control,
            owned_anchor=owned_anchor,
            maximum_bytes=maximum_bytes,
        )
        if _strict_lexists(control)
        else None
    )
    _verify_directory_chain(parent_chain)
    return _OptionalControlSnapshot(
        control=control,
        parent_chain=parent_chain,
        tree=tree,
    )


def _verify_optional_control_snapshot(
    expected: _OptionalControlSnapshot,
    *,
    owned_anchor: Path,
) -> None:
    _verify_directory_chain(expected.parent_chain)
    exists = _strict_lexists(expected.control)
    if exists != (expected.tree is not None):
        raise CanonicalStorageError("cleanup control presence changed")
    if expected.tree is not None:
        _verify_control_tree_snapshot(
            expected.tree,
            owned_anchor=owned_anchor,
        )
    _verify_directory_chain(expected.parent_chain)


def _unlink_inventory_tree(
    root: Path,
    inventory: TreeInventoryV1,
    *,
    expected_parent_chain: _DirectoryChain,
    additional_parent_chains: tuple[_DirectoryChain, ...],
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
        for chain in additional_parent_chains:
            _verify_directory_chain(chain)
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
    for chain in additional_parent_chains:
        _verify_directory_chain(chain)
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
    _verify_directory_chain(expected_parent_chain)
    for chain in additional_parent_chains:
        _verify_directory_chain(chain)
    return deleted + 1


def _observe_staging_failure(
    staging: Path,
    *,
    run_id_sha256: str,
    expected_tree_sha256: str,
    limits: CleanupLimitsV1,
    expected_parent_chain: _DirectoryChain | None = None,
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


def _tree_observation_token(
    path: Path,
    *,
    run_id_sha256: str,
    limits: CleanupLimitsV1,
) -> tuple[Literal["absent", "present"], str | None]:
    if not _strict_lexists(path):
        return "absent", None
    inventory = scan_cleanup_tree(
        path,
        run_id_sha256=run_id_sha256,
        limits=limits,
    )
    return "present", tree_inventory_sha256(inventory)


def _capture_namespace_tree_snapshot(
    path: Path,
    *,
    owned_anchor: Path,
    run_id_sha256: str,
    limits: CleanupLimitsV1,
) -> _NamespaceTreeSnapshot:
    parent_chain = _capture_directory_chain(
        path.parent,
        owned_anchor=owned_anchor,
    )
    expected_alias = unicodedata.normalize("NFC", path.name).casefold()
    aliases: set[str] = set()
    exact = False
    for entry_count, child in enumerate(path.parent.iterdir(), start=1):
        if entry_count > _NAMESPACE_ENTRY_LIMIT:
            raise CanonicalStorageError("namespace entry capacity exceeded")
        normalized = unicodedata.normalize("NFC", child.name)
        alias = normalized.casefold()
        if normalized != child.name or alias in aliases:
            raise CanonicalStorageError("namespace entry alias conflict")
        aliases.add(alias)
        if alias == expected_alias:
            if child.name != path.name:
                raise CanonicalStorageError("namespace target has a case alias")
            exact = True
    target_chain = _capture_directory_chain(path, owned_anchor=owned_anchor) if exact else None
    state, tree_sha256 = _tree_observation_token(
        path,
        run_id_sha256=run_id_sha256,
        limits=limits,
    )
    if exact != (state == "present"):
        raise CanonicalStorageError("namespace target presence changed during snapshot")
    if target_chain is not None:
        _verify_directory_chain(target_chain)
    _verify_directory_chain(parent_chain)
    return _NamespaceTreeSnapshot(
        path=path,
        parent_chain=parent_chain,
        target_chain=target_chain,
        maximum_entries=_NAMESPACE_ENTRY_LIMIT,
        state=state,
        tree_sha256=tree_sha256,
    )


def _verify_namespace_tree_snapshot(
    expected: _NamespaceTreeSnapshot,
    *,
    owned_anchor: Path,
    run_id_sha256: str,
    limits: CleanupLimitsV1,
) -> None:
    _verify_directory_chain(expected.parent_chain)
    if expected.target_chain is not None:
        _verify_directory_chain(expected.target_chain)
    observed = _capture_namespace_tree_snapshot(
        expected.path,
        owned_anchor=owned_anchor,
        run_id_sha256=run_id_sha256,
        limits=limits,
    )
    if expected.target_chain is not None:
        _verify_directory_chain(expected.target_chain)
    _verify_directory_chain(expected.parent_chain)
    if observed != expected:
        raise CanonicalStorageError("namespace target changed")


def _control_tree_bytes(
    control: Path,
    *,
    maximum_bytes: int,
    owned_anchor: Path,
) -> int:
    if not _strict_lexists(control):
        return 0
    _capture_directory_chain(control, owned_anchor=owned_anchor)
    total = 0
    entries = 0
    stack = [control]
    while stack:
        directory = stack.pop()
        _capture_directory_chain(directory, owned_anchor=owned_anchor)
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
                if _has_nondefault_windows_stream(child):
                    raise _fail(CleanupFailureCode.PATH_CONFINEMENT_FAILED)
                stack.append(child)
            elif (
                stat.S_ISREG(info.st_mode)
                and info.st_nlink == 1
                and not _has_nondefault_windows_stream(child)
            ):
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
    maximum_entries: int = _NAMESPACE_ENTRY_LIMIT,
) -> Path:
    verify_directory(parent)
    expected_alias = unicodedata.normalize("NFC", expected_name).casefold()
    exact = False
    aliases: set[str] = set()
    for entry_count, child in enumerate(parent.iterdir(), start=1):
        if entry_count > maximum_entries:
            raise _fail(CleanupFailureCode.CAPACITY_EXCEEDED)
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


def _revision_number_from_directory_name(name: str) -> int | None:
    for revision in (1, 2, 3):
        prefix = f"r{revision}-cleanup-txn-"
        if name.startswith(prefix):
            digest = name[len(prefix) :]
            if len(digest) == 32 and all(character in "0123456789abcdef" for character in digest):
                return revision
    return None


def _validated_revision_directories(
    revisions: Path,
    *,
    cleanup_root: Path,
    maximum_artifact_bytes: int,
) -> dict[str, Path]:
    _capture_directory_chain(revisions, owned_anchor=cleanup_root)
    result: dict[str, Path] = {}
    aliases: set[str] = set()
    expected_artifacts = {
        "transaction.json",
        "manifest.json",
        "receipt.json",
        "tombstone.json",
    }
    for child in revisions.iterdir():
        normalized = unicodedata.normalize("NFC", child.name)
        alias = normalized.casefold()
        if (
            normalized != child.name
            or alias in aliases
            or _revision_number_from_directory_name(child.name) is None
        ):
            raise CanonicalStorageError("cleanup revision directory name is invalid")
        aliases.add(alias)
        _capture_directory_chain(child, owned_anchor=cleanup_root)
        artifacts = {entry.name: entry for entry in child.iterdir()}
        if set(artifacts) != expected_artifacts:
            raise CanonicalStorageError("cleanup revision directory entries are invalid")
        for artifact in artifacts.values():
            _read_control_bytes(
                artifact,
                max_bytes=maximum_artifact_bytes,
            )
        result[child.name] = child
    return result


def _windows_stream_signature(path: Path) -> tuple[tuple[str, int], ...]:
    if os.name != "nt":
        return ()
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
        error = ctypes.get_last_error()
        if error == 38:
            return ()
        raise OSError(error, "could not enumerate Windows streams", str(path))
    streams: list[tuple[str, int]] = []
    try:
        while True:
            streams.append((data.stream_name, data.stream_size))
            if not find_next(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                if error != 38:
                    raise OSError(error, "could not enumerate Windows streams", str(path))
                return tuple(sorted(streams))
    finally:
        find_close(handle)


def _has_nondefault_windows_stream(path: Path) -> bool:
    return any(name != "::$DATA" for name, _size in _windows_stream_signature(path))


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

    def _local_marker(self) -> tuple[CleanupRootMarkerV1, str]:
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
        return marker, marker_sha

    def marker(self) -> tuple[CleanupRootMarkerV1, str]:
        marker, marker_sha = self._local_marker()
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
            _capture_directory_chain(control, owned_anchor=self.cleanup_root)
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
                temporary_bytes = _read_control_bytes(
                    temporary_path,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
                if temporary_bytes != pointer_temporary[1]:
                    raise _fail(
                        CleanupFailureCode.STALE_CLEANUP_REVISION,
                        run_id_sha256=run_hash,
                        transaction_id=transaction.transaction_id,
                    )
            transactions = entries["transactions"]
            revisions = entries["revisions"]
            _capture_directory_chain(transactions, owned_anchor=self.cleanup_root)
            _capture_directory_chain(revisions, owned_anchor=self.cleanup_root)
            journal_directories = {item.name: item for item in transactions.iterdir()}
            if set(journal_directories) != {transaction.transaction_id}:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    transaction_id=transaction.transaction_id,
                )
            journal_root = journal_directories[transaction.transaction_id]
            _capture_directory_chain(journal_root, owned_anchor=self.cleanup_root)
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
            _capture_directory_chain(revision_root, owned_anchor=self.cleanup_root)
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
        pending_revision_name: str | None = None,
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
        if expected_marker is None:
            marker, marker_sha = self.marker()
        else:
            marker, marker_sha = self._local_marker()
            if marker != expected_marker[0] or marker_sha != expected_marker[1]:
                raise _fail(
                    CleanupFailureCode.OWNERSHIP_UNVERIFIED,
                    run_id_sha256=run_hash,
                )
        control = self._run_control(run_hash)
        if not _strict_lexists(control):
            return None
        _capture_directory_chain(control, owned_anchor=self.cleanup_root)
        _capture_control_tree_snapshot(
            control,
            owned_anchor=self.cleanup_root,
            maximum_bytes=marker.limits.maximum_control_bytes_per_run,
        )
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
        revision_directories = _validated_revision_directories(
            entries["revisions"],
            cleanup_root=self.cleanup_root,
            maximum_artifact_bytes=marker.limits.maximum_control_artifact_bytes,
        )
        revision = self.cleanup_root / pointer.revision_relative_path
        if (
            revision.parent != entries["revisions"]
            or revision_directories.get(revision.name) != revision
        ):
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        _capture_directory_chain(revision, owned_anchor=self.cleanup_root)
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
        reachable_revision_names = {revision.name}
        for prior_revision in range(pointer.revision - 1, 0, -1):
            prefix = f"r{prior_revision}-"
            matches = tuple(
                child for name, child in revision_directories.items() if name.startswith(prefix)
            )
            if len(matches) != 1:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            prior_directory = matches[0]
            _capture_directory_chain(prior_directory, owned_anchor=self.cleanup_root)
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
            reachable_revision_names.add(prior_directory.name)
            successor_manifest = prior_manifest
        allowed_revision_names = set(reachable_revision_names)
        if pending_transaction is not None:
            allowed_revision_names.add(
                f"r{pending_transaction.proposed_revision}-{pending_transaction.transaction_id}"
            )
        if pending_revision_name is not None:
            if _revision_number_from_directory_name(pending_revision_name) != pointer.revision + 1:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            allowed_revision_names.add(pending_revision_name)
        if pending_pointer is not None:
            pending_value = parse_cleanup_model(
                pending_pointer[1],
                CleanupCurrentPointerV1,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )
            pending_revision_path = self.cleanup_root / pending_value.revision_relative_path
            if (
                pending_value.run_id_sha256 != run_hash
                or pending_value.revision != pointer.revision + 1
                or pending_revision_path.parent != entries["revisions"]
            ):
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                )
            allowed_revision_names.add(pending_revision_path.name)
        observed_revision_names = set(revision_directories)
        if (
            not reachable_revision_names <= observed_revision_names
            or not observed_revision_names <= allowed_revision_names
        ):
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
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
        _capture_directory_chain(transactions_root, owned_anchor=self.cleanup_root)
        journal_directories = {item.name: item for item in transactions_root.iterdir()}
        if set(journal_directories) != set(expected_journals):
            raise _fail(CleanupFailureCode.STALE_CLEANUP_REVISION, run_id_sha256=run_hash)
        for journal_id, expected_transaction in expected_journals.items():
            journal_root = journal_directories[journal_id]
            _capture_directory_chain(journal_root, owned_anchor=self.cleanup_root)
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
            if (
                _read_control_bytes(
                    temporary,
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
                != pending_pointer[1]
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
        control = self._run_control(run_hash)
        lineage_snapshot = _capture_control_tree_snapshot(
            control,
            owned_anchor=self.cleanup_root,
            maximum_bytes=marker.limits.maximum_control_bytes_per_run,
        )
        product_snapshot = _capture_product_authority_snapshot(self.terminal_store)

        def verify_persisted_lineage() -> None:
            _verify_product_authority_snapshot(
                product_snapshot,
                terminal_store=self.terminal_store,
            )
            _verify_control_tree_snapshot(
                lineage_snapshot,
                owned_anchor=self.cleanup_root,
            )
            observed = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            _verify_control_tree_snapshot(
                lineage_snapshot,
                owned_anchor=self.cleanup_root,
            )
            if observed != current:
                raise _fail(
                    CleanupFailureCode.STALE_CLEANUP_REVISION,
                    run_id_sha256=run_hash,
                    plan_sha256=cleanup_plan_sha256(plan),
                )

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
        verify_persisted_lineage()
        revisions_root = control / "revisions"
        _capture_directory_chain(revisions_root, owned_anchor=self.cleanup_root)
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
            _capture_directory_chain(revision_directory, owned_anchor=self.cleanup_root)
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
            verify_persisted_lineage()
            return None
        result = max(exact, key=lambda item: item[0].revision)
        verify_persisted_lineage()
        return result

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
        cleanup_authority_snapshot = _capture_cleanup_authority_snapshot(
            self.cleanup_root,
            maximum_bytes=marker.limits.maximum_control_artifact_bytes,
        )
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
        planned_destination = self.cleanup_root / plan.actions[0].destination_relative_path
        if (
            plan.actions[0].action_kind is not CleanupActionKind.QUARANTINE_PRODUCT_RUN
            or planned_destination.parent != self.cleanup_root / "quarantine"
            or planned_destination.name != run_id
        ):
            raise _fail(CleanupFailureCode.INVALID_PLAN, run_id_sha256=run_hash)
        authority: ExistingRunAuthorityV1 | None = None
        journal_published = False
        control_created = False
        source_moved = False
        pointer_replace_attempted = False
        rollback_journal: _RollbackJournal | None = None
        source: Path | None = None
        destination: Path | None = planned_destination
        source_parent_chain: _DirectoryChain | None = None
        destination_parent_chain: _DirectoryChain | None = None
        control_directory_chains: _DirectoryChains = ()
        control_tree_snapshot: _ControlTreeSnapshot | None = None
        product_authority_snapshot: _ProductAuthoritySnapshot | None = None

        def capture_control_directory_chains(*directories: Path) -> None:
            nonlocal control_directory_chains
            control_directory_chains = tuple(
                _capture_directory_chain(
                    directory,
                    owned_anchor=self.cleanup_root,
                )
                for directory in directories
            )

        def verify_control_directory_chains() -> None:
            if (
                not control_directory_chains
                or control_tree_snapshot is None
                or product_authority_snapshot is None
            ):
                raise CanonicalStorageError("cleanup control directory state is unavailable")
            _verify_product_authority_snapshot(
                product_authority_snapshot,
                terminal_store=self.terminal_store,
            )
            _verify_cleanup_authority_snapshot(cleanup_authority_snapshot)
            for chain in control_directory_chains:
                _verify_directory_chain(chain)
            _verify_control_tree_snapshot(
                control_tree_snapshot,
                owned_anchor=self.cleanup_root,
            )

        def capture_control_tree_snapshot() -> None:
            nonlocal control_tree_snapshot
            control_tree_snapshot = _capture_control_tree_snapshot(
                self.cleanup_root / "runs" / run_hash,
                owned_anchor=self.cleanup_root,
                maximum_bytes=marker.limits.maximum_control_bytes_per_run,
            )

        def verify_effect_parent_chains() -> None:
            if source_parent_chain is None or destination_parent_chain is None:
                raise CanonicalStorageError("quarantine effect parent chain is unavailable")
            _verify_directory_chain(source_parent_chain)
            _verify_directory_chain(destination_parent_chain)
            verify_control_directory_chains()

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
            source = authority.run_path
            product_authority_snapshot = _capture_product_authority_snapshot(self.terminal_store)
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
            inventory = scan_cleanup_tree(source, run_id_sha256=run_hash, limits=marker.limits)
            source_tree_sha = tree_inventory_sha256(inventory)
            if source_tree_sha != plan.tree_inventory_sha256:
                raise _fail(
                    CleanupFailureCode.STALE_SOURCE,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            observed_destination = _namespace_child(
                self.cleanup_root / "quarantine",
                run_id,
                require_exact=False,
            )
            if (
                source
                != self.terminal_store.foundation.revision_root
                / plan.actions[0].source_relative_path
                or observed_destination != destination
            ):
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
            if _strict_lexists(control):
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
            transaction_bytes = canonical_cleanup_bytes(transaction)
            projected = _control_tree_bytes(
                control,
                maximum_bytes=marker.limits.maximum_control_bytes_per_run,
                owned_anchor=self.cleanup_root,
            ) + (5 * marker.limits.maximum_control_artifact_bytes + 2 * len(transaction_bytes))
            if projected > marker.limits.maximum_control_bytes_per_run:
                raise _fail(
                    CleanupFailureCode.AUDIT_CAPACITY_EXCEEDED,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            journal_parent_chain = _capture_directory_chain(
                control.parent,
                owned_anchor=self.cleanup_root,
            )
            _fault(self.fault_injector, "quarantine.before_journal")
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            _verify_directory_chain(journal_parent_chain)
            if _strict_lexists(control):
                raise _fail(
                    CleanupFailureCode.IDEMPOTENCY_CONFLICT,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            control.mkdir()
            control_created = True
            (control / "transactions").mkdir()
            (control / "revisions").mkdir()
            transaction_root.mkdir()

            def capture_rollback_journal() -> None:
                nonlocal rollback_journal
                rollback_journal = _capture_pre_effect_journal(
                    transaction_root,
                    transaction_bytes,
                    owned_anchor=self.cleanup_root,
                    empty_directories=(
                        control / "revisions",
                        control / "transactions",
                        control,
                    ),
                )

            _write_exclusive(
                transaction_root / "transaction.json",
                transaction_bytes,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
                fault_injector=self.fault_injector,
                fault_hook="quarantine.journal",
                fault_preparer=capture_rollback_journal,
            )
            journal_published = True
            _directory_sync(transaction_root)
            capture_control_directory_chains(
                transaction_root,
                control / "revisions",
            )
            capture_control_tree_snapshot()
            _fault(self.fault_injector, "quarantine.before_effect")
            verify_control_directory_chains()
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            effect_at = clock()
            verify_control_directory_chains()
            final_approval = authorize(effect_at)
            verify_control_directory_chains()
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
            source_parent_chain = _capture_directory_chain(
                source.parent,
                owned_anchor=source.parent,
            )
            destination_parent_chain = _capture_directory_chain(
                destination.parent,
                owned_anchor=self.cleanup_root,
            )
            os.replace(source, destination)
            source_moved = True
            _fault(self.fault_injector, "quarantine.after_effect")
            verify_effect_parent_chains()
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source remained after rename")
            destination_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(destination_inventory) != source_tree_sha:
                raise CanonicalStorageError("quarantine destination tree changed")
            verify_effect_parent_chains()
            rename_directory_sync = _directory_sync_many(source.parent, destination.parent)
            committed_at = clock()
            verify_effect_parent_chains()
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
            verify_control_directory_chains()
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
            capture_control_directory_chains(
                transaction_root,
                revision,
            )
            capture_control_tree_snapshot()
            _fault(self.fault_injector, "quarantine.before_pointer_replace")
            verify_effect_parent_chains()
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
            capture_control_tree_snapshot()
            _fault(self.fault_injector, "quarantine.after_pointer_replace")
            verify_effect_parent_chains()
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source reappeared after pointer replace")
            published_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(published_inventory) != source_tree_sha:
                raise CanonicalStorageError("quarantine destination changed after pointer replace")
            if (
                _read_control_bytes(
                    control / "current.json",
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
                != pointer_bytes
            ):
                raise CanonicalStorageError("cleanup current pointer reread mismatch")
            _directory_sync(control)
            verified = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if verified is None or verified[0] != pointer:
                raise CanonicalStorageError("cleanup committed state did not verify")
            verify_effect_parent_chains()
            result = CleanupExecutionResultV1(
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
            if authority is None:
                raise CanonicalStorageError("quarantine authority disappeared before release")
            if source is None or destination is None:
                raise CanonicalStorageError("quarantine namespace state is unavailable")
            released_source_snapshot = _capture_namespace_tree_snapshot(
                source,
                owned_anchor=self.terminal_store.foundation.revision_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            released_destination_snapshot = _capture_namespace_tree_snapshot(
                destination,
                owned_anchor=self.cleanup_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            held_authority = authority
            authority = None
            try:
                held_authority.release()
            except LockReleaseError as exc:
                raise _fail(
                    CleanupFailureCode.EFFECT_UNKNOWN,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect="source_moved",
                    domain_effect="current_may_have_advanced",
                ) from exc
            released_current = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if released_current is None or released_current[0] != pointer:
                raise CanonicalStorageError("cleanup state changed after authority release")
            verify_effect_parent_chains()
            _verify_namespace_tree_snapshot(
                released_source_snapshot,
                owned_anchor=self.terminal_store.foundation.revision_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            _verify_namespace_tree_snapshot(
                released_destination_snapshot,
                owned_anchor=self.cleanup_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            verify_effect_parent_chains()
            return result
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
                rolled_back = _rollback_pre_effect_journal(rollback_journal)
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
                rolled_back = _rollback_pre_effect_journal(rollback_journal)
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
                rolled_back = _rollback_pre_effect_journal(rollback_journal)
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
                rolled_back = _rollback_pre_effect_journal(rollback_journal)
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
                rolled_back = _rollback_pre_effect_journal(rollback_journal)
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
                release_state_uncertain = False
                control_snapshot: _OptionalControlSnapshot | None = None
                source_snapshot: _NamespaceTreeSnapshot | None = None
                destination_snapshot: _NamespaceTreeSnapshot | None = None
                try:
                    control_snapshot = _capture_optional_control_snapshot(
                        self.cleanup_root / "runs" / run_hash,
                        owned_anchor=self.cleanup_root,
                        maximum_bytes=marker.limits.maximum_control_bytes_per_run,
                    )
                    if source is None or destination is None:
                        raise CanonicalStorageError("quarantine namespace state is unavailable")
                    source_snapshot = _capture_namespace_tree_snapshot(
                        source,
                        owned_anchor=self.terminal_store.foundation.revision_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                    destination_snapshot = _capture_namespace_tree_snapshot(
                        destination,
                        owned_anchor=self.cleanup_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                except Exception:
                    release_state_uncertain = True
                held_authority = authority
                authority = None
                try:
                    held_authority.release()
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
                try:
                    if (
                        release_state_uncertain
                        or control_snapshot is None
                        or product_authority_snapshot is None
                        or source_snapshot is None
                        or destination_snapshot is None
                    ):
                        raise CanonicalStorageError(
                            "quarantine release state could not be captured"
                        )
                    _verify_optional_control_snapshot(
                        control_snapshot,
                        owned_anchor=self.cleanup_root,
                    )
                    _verify_product_authority_snapshot(
                        product_authority_snapshot,
                        terminal_store=self.terminal_store,
                    )
                    _verify_cleanup_authority_snapshot(cleanup_authority_snapshot)
                    _verify_namespace_tree_snapshot(
                        source_snapshot,
                        owned_anchor=self.terminal_store.foundation.revision_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                    _verify_namespace_tree_snapshot(
                        destination_snapshot,
                        owned_anchor=self.cleanup_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                except Exception as exc:
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
        cleanup_authority_snapshot = _capture_cleanup_authority_snapshot(
            self.cleanup_root,
            maximum_bytes=marker.limits.maximum_control_artifact_bytes,
        )
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
        rollback_journal: _RollbackJournal | None = None
        source_parent_chain: _DirectoryChain | None = None
        staging_parent_chain: _DirectoryChain | None = None
        control_directory_chains: _DirectoryChains = ()
        control_tree_snapshot: _ControlTreeSnapshot | None = None
        product_authority_snapshot: _ProductAuthoritySnapshot | None = None
        former_product_snapshot: _NamespaceTreeSnapshot | None = None

        def capture_control_directory_chains(*directories: Path) -> None:
            nonlocal control_directory_chains
            control_directory_chains = tuple(
                _capture_directory_chain(
                    directory,
                    owned_anchor=self.cleanup_root,
                )
                for directory in directories
            )

        def verify_control_directory_chains() -> None:
            if (
                not control_directory_chains
                or control_tree_snapshot is None
                or product_authority_snapshot is None
            ):
                raise CanonicalStorageError("cleanup control directory state is unavailable")
            _verify_product_authority_snapshot(
                product_authority_snapshot,
                terminal_store=self.terminal_store,
            )
            _verify_cleanup_authority_snapshot(cleanup_authority_snapshot)
            if former_product_snapshot is None:
                raise CanonicalStorageError("detached product namespace state is unavailable")
            _verify_namespace_tree_snapshot(
                former_product_snapshot,
                owned_anchor=self.terminal_store.foundation.revision_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            for chain in control_directory_chains:
                _verify_directory_chain(chain)
            _verify_control_tree_snapshot(
                control_tree_snapshot,
                owned_anchor=self.cleanup_root,
            )

        def capture_control_tree_snapshot() -> None:
            nonlocal control_tree_snapshot
            control_tree_snapshot = _capture_control_tree_snapshot(
                self.cleanup_root / "runs" / run_hash,
                owned_anchor=self.cleanup_root,
                maximum_bytes=marker.limits.maximum_control_bytes_per_run,
            )

        def verify_effect_parent_chains() -> None:
            if source_parent_chain is None or staging_parent_chain is None:
                raise CanonicalStorageError("delete effect parent chain is unavailable")
            _verify_directory_chain(source_parent_chain)
            _verify_directory_chain(staging_parent_chain)
            verify_control_directory_chains()

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
            product_authority_snapshot = _capture_product_authority_snapshot(self.terminal_store)
            former_product_snapshot = _capture_namespace_tree_snapshot(
                authority.former_run_path,
                owned_anchor=self.terminal_store.foundation.revision_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
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
            prior_transaction_root = control / "transactions" / prior_manifest.transaction_id
            prior_revision = self.cleanup_root / prior_pointer.revision_relative_path
            revision_two = control / "revisions" / f"r2-{transaction_id}"
            revision_three = control / "revisions" / f"r3-{transaction_id}"
            if (
                _strict_lexists(transaction_root)
                or _strict_lexists(revision_two)
                or _strict_lexists(revision_three)
            ):
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
            transaction_two_bytes = canonical_cleanup_bytes(transaction_two)
            projected = _control_tree_bytes(
                control,
                maximum_bytes=marker.limits.maximum_control_bytes_per_run,
                owned_anchor=self.cleanup_root,
            ) + (8 * marker.limits.maximum_control_artifact_bytes + 3 * len(transaction_two_bytes))
            if projected > marker.limits.maximum_control_bytes_per_run:
                raise _fail(
                    CleanupFailureCode.AUDIT_CAPACITY_EXCEEDED,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            pre_journal_control_snapshot = _capture_control_tree_snapshot(
                control,
                owned_anchor=self.cleanup_root,
                maximum_bytes=marker.limits.maximum_control_bytes_per_run,
            )
            _fault(self.fault_injector, "delete.before_journal")
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            _verify_control_tree_snapshot(
                pre_journal_control_snapshot,
                owned_anchor=self.cleanup_root,
            )
            if _strict_lexists(transaction_root):
                raise _fail(
                    CleanupFailureCode.IDEMPOTENCY_CONFLICT,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                )
            transaction_root.mkdir()
            transaction_root_created = True

            def capture_rollback_journal() -> None:
                nonlocal rollback_journal
                rollback_journal = _capture_pre_effect_journal(
                    transaction_root,
                    transaction_two_bytes,
                    owned_anchor=self.cleanup_root,
                )

            _write_exclusive(
                transaction_root / "transaction.json",
                transaction_two_bytes,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
                fault_injector=self.fault_injector,
                fault_hook="delete.journal",
                fault_preparer=capture_rollback_journal,
            )
            journal_published = True
            _directory_sync(transaction_root)
            capture_control_directory_chains(
                prior_transaction_root,
                transaction_root,
                prior_revision,
            )
            capture_control_tree_snapshot()
            _fault(self.fault_injector, "delete.before_staging_rename")
            verify_control_directory_chains()
            _require_not_cancelled(
                cancelled,
                run_id_sha256=run_hash,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            effect_at = clock()
            verify_control_directory_chains()
            final_approval = authorize(effect_at, transaction_two)
            verify_control_directory_chains()
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
            source_parent_chain = _capture_directory_chain(
                source.parent,
                owned_anchor=self.cleanup_root,
            )
            staging_parent_chain = _capture_directory_chain(
                destination.parent,
                owned_anchor=self.cleanup_root,
            )
            os.replace(source, destination)
            staging_moved = True
            _fault(self.fault_injector, "delete.after_staging_rename")
            verify_effect_parent_chains()
            if _strict_lexists(source):
                raise CanonicalStorageError("quarantine source remained after staging rename")
            staged_inventory = scan_cleanup_tree(
                destination,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            if tree_inventory_sha256(staged_inventory) != source_tree_sha:
                raise CanonicalStorageError("delete staging tree changed")
            verify_effect_parent_chains()
            rename_directory_sync = _directory_sync_many(source.parent, destination.parent)
            prepared_at = clock()
            verify_effect_parent_chains()
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
            capture_control_directory_chains(
                prior_transaction_root,
                transaction_root,
                prior_revision,
                revision_two,
            )
            capture_control_tree_snapshot()
            _fault(self.fault_injector, "delete.before_prepare_pointer_replace")
            verify_effect_parent_chains()
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
            capture_control_tree_snapshot()
            _fault(self.fault_injector, "delete.after_prepare_pointer_replace")
            verify_effect_parent_chains()
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
            if (
                _read_control_bytes(
                    control / "current.json",
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
                != pointer_two_bytes
            ):
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
            verify_effect_parent_chains()

            _fault(self.fault_injector, "delete.before_unlink_start")
            verify_effect_parent_chains()
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
                additional_parent_chains=(
                    source_parent_chain,
                    *control_directory_chains,
                ),
                expected_root_identity=staging_root_identity,
                fault_injector=self.fault_injector,
                progress=delete_progress,
                cancelled=cancelled,
                plan_sha256=plan_sha,
                transaction_id=transaction_id,
            )
            if _strict_lexists(destination):
                raise CanonicalStorageError("delete staging remained after unlink")
            verify_effect_parent_chains()
            delete_directory_sync = _directory_sync(destination.parent)
            final_at = clock()
            verify_effect_parent_chains()
            if _strict_lexists(source) or _strict_lexists(destination):
                raise CanonicalStorageError("delete payload namespace reappeared after final clock")
            current_after_final_clock = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if current_after_final_clock is None or current_after_final_clock[0] != pointer_two:
                raise CanonicalStorageError("cleanup delete CAS changed after final clock")
            verify_control_directory_chains()
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
                pending_revision_name=revision_three.name,
            )
            if verified_before_final is None or verified_before_final[0] != pointer_two:
                raise CanonicalStorageError("cleanup delete CAS changed before final")
            capture_control_directory_chains(
                prior_transaction_root,
                transaction_root,
                prior_revision,
                revision_two,
                revision_three,
            )
            capture_control_tree_snapshot()
            pointer_three_bytes = canonical_cleanup_bytes(pointer_three)
            temporary_three = control / f"current.{transaction_id}.deleted.tmp"
            _fault(self.fault_injector, "delete.before_final_pointer_temp_write")
            verify_effect_parent_chains()
            _write_exclusive(
                temporary_three,
                pointer_three_bytes,
                max_bytes=marker.limits.maximum_control_artifact_bytes,
            )
            capture_control_tree_snapshot()
            _fault(self.fault_injector, "delete.before_final_pointer_replace")
            verify_effect_parent_chains()
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
            capture_control_tree_snapshot()
            _fault(self.fault_injector, "delete.after_final_pointer_replace")
            verify_effect_parent_chains()
            if _strict_lexists(source) or _strict_lexists(destination):
                raise CanonicalStorageError(
                    "delete payload namespace reappeared after final pointer replace"
                )
            if (
                _read_control_bytes(
                    control / "current.json",
                    max_bytes=marker.limits.maximum_control_artifact_bytes,
                )
                != pointer_three_bytes
            ):
                raise CanonicalStorageError("deleted current reread mismatch")
            _directory_sync(control)
            verified_three = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if verified_three is None or verified_three[0] != pointer_three:
                raise CanonicalStorageError("deleted state did not verify")
            verify_effect_parent_chains()
            result = CleanupExecutionResultV1(
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
            if authority is None:
                raise CanonicalStorageError("delete authority disappeared before release")
            if destination is None:
                raise CanonicalStorageError("delete namespace state is unavailable")
            released_source_snapshot = _capture_namespace_tree_snapshot(
                source,
                owned_anchor=self.cleanup_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            released_destination_snapshot = _capture_namespace_tree_snapshot(
                destination,
                owned_anchor=self.cleanup_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            held_authority = authority
            authority = None
            try:
                held_authority.release()
            except LockReleaseError as exc:
                raise _fail(
                    CleanupFailureCode.EFFECT_UNKNOWN,
                    run_id_sha256=run_hash,
                    plan_sha256=plan_sha,
                    transaction_id=transaction_id,
                    filesystem_effect="partial_delete",
                    domain_effect="current_may_have_advanced",
                ) from exc
            released_current = self._read_current(
                run_hash,
                expected_marker=(marker, marker_sha),
            )
            if released_current is None or released_current[0] != pointer_three:
                raise CanonicalStorageError("deleted state changed after authority release")
            verify_effect_parent_chains()
            _verify_namespace_tree_snapshot(
                released_source_snapshot,
                owned_anchor=self.cleanup_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            _verify_namespace_tree_snapshot(
                released_destination_snapshot,
                owned_anchor=self.cleanup_root,
                run_id_sha256=run_hash,
                limits=marker.limits,
            )
            verify_effect_parent_chains()
            return result
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
                if _rollback_pre_effect_journal(rollback_journal):
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
                if _rollback_pre_effect_journal(rollback_journal):
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
                if _rollback_pre_effect_journal(rollback_journal):
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
                release_destination = destination
                release_state_uncertain = False
                control_snapshot: _OptionalControlSnapshot | None = None
                source_snapshot: _NamespaceTreeSnapshot | None = None
                destination_snapshot: _NamespaceTreeSnapshot | None = None
                try:
                    if release_destination is None:
                        raise CanonicalStorageError("delete destination is unavailable")
                    control_snapshot = _capture_optional_control_snapshot(
                        self.cleanup_root / "runs" / run_hash,
                        owned_anchor=self.cleanup_root,
                        maximum_bytes=marker.limits.maximum_control_bytes_per_run,
                    )
                    source_snapshot = _capture_namespace_tree_snapshot(
                        source,
                        owned_anchor=self.cleanup_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                    destination_snapshot = _capture_namespace_tree_snapshot(
                        release_destination,
                        owned_anchor=self.cleanup_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                except Exception:
                    release_state_uncertain = True
                held_authority = authority
                authority = None
                try:
                    held_authority.release()
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
                try:
                    if (
                        release_state_uncertain
                        or control_snapshot is None
                        or source_snapshot is None
                        or destination_snapshot is None
                        or release_destination is None
                        or product_authority_snapshot is None
                        or former_product_snapshot is None
                    ):
                        raise CanonicalStorageError("delete release state could not be captured")
                    _verify_optional_control_snapshot(
                        control_snapshot,
                        owned_anchor=self.cleanup_root,
                    )
                    _verify_product_authority_snapshot(
                        product_authority_snapshot,
                        terminal_store=self.terminal_store,
                    )
                    _verify_cleanup_authority_snapshot(cleanup_authority_snapshot)
                    _verify_namespace_tree_snapshot(
                        former_product_snapshot,
                        owned_anchor=self.terminal_store.foundation.revision_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                    _verify_namespace_tree_snapshot(
                        source_snapshot,
                        owned_anchor=self.cleanup_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                    _verify_namespace_tree_snapshot(
                        destination_snapshot,
                        owned_anchor=self.cleanup_root,
                        run_id_sha256=run_hash,
                        limits=marker.limits,
                    )
                except Exception as exc:
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
