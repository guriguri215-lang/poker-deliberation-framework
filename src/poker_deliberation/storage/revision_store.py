"""Opt-in immutable structural revision storage for P2-012A.

This module is intentionally disconnected from the product ``RunStore`` and
orchestrator.  It publishes only ``structural_nonterminal`` revisions and does
not create a completion marker or map a revision to a product run status.
"""

from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel, ValidationError

from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    artifact_table_entry,
    ascii_casefold,
    build_inventory,
    canonical_json_bytes,
    check_path_lengths,
    ensure_strict_positive_int,
    inventory_sha256,
    legacy_root_identity_sha256,
    parse_canonical_model,
    platform_adapter,
    provenance_heads,
    recovery_claim_sha256,
    run_id_sha256,
    run_lock_key_sha256,
    sha256_bytes,
    transaction_sha256,
    validate_artifact,
    validate_run_id,
)
from poker_deliberation.storage.revision_lock import (
    AuthorityLease,
    FaultInjector,
    LockBusyError,
    LockReleaseError,
    LockUnavailableError,
    acquire_authority,
    verify_directory,
    verify_regular_single_link,
)
from poker_deliberation.storage.revision_models import (
    DurabilityEvidenceV1,
    LocalDataBindingV1,
    LockMetadataV1,
    OrphanEntryV1,
    OrphanInspectionV1,
    OwnershipMarkerV1,
    PayloadInventoryEntryV1,
    ReachableRevisionV1,
    RecoveryClaimRequestV1,
    RecoveryClaimV1,
    RevisionArtifactV1,
    RevisionPublishOutcomeV1,
    RevisionPublishRequestV1,
    RevisionTransactionDescriptorV1,
    RootInitializationInspectionV1,
    RootInitializationOutcomeV1,
    RootInitializationRequestV1,
    RunStorageError,
    RunStorageFailureCode,
    RunStorageFailureV1,
    StorageRevisionManifestV1,
    StorageRevisionPointerV1,
    StructuralArtifactHistoryV1,
    StructuralArtifactRevisionV1,
    VerifiedStorageRevisionV1,
)

DEFAULT_MAX_ARTIFACT_BYTES = 1_000_000
DEFAULT_MAX_RUN_BYTES = 10_000_000
_ROOT_TEMP = re.compile(r"^(?:ownership|\.revision-control|runs)\.(root-[0-9a-f]{32})\.tmp$")
_REVISION_DIR = re.compile(r"^r(?P<revision>[1-9][0-9]*)-(?P<transaction>txn-[0-9a-f]{32})$")
_TRANSACTION_DIR = re.compile(r"^txn-[0-9a-f]{32}$")
_CURRENT_TEMP = re.compile(r"^current\.txn-[0-9a-f]{32}\.tmp$")
_CLAIM_TEMP = re.compile(r"^txn-[0-9a-f]{32}\.claim-[0-9a-f]{32}\.json$")
_CLAIM_FINAL = re.compile(r"^txn-[0-9a-f]{32}\.json$")
_OWNER_TOKEN = re.compile(r"^owner-[0-9a-f]{32}$")
Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]
T = TypeVar("T", bound=BaseModel)


class _StorageBoundaryError(RuntimeError):
    def __init__(
        self,
        kind: Literal["write", "durability", "verification"],
        boundary: str,
        cause: BaseException,
    ) -> None:
        self.kind = kind
        self.boundary = boundary
        super().__init__(boundary)
        self.__cause__ = cause


class _PublishBoundaryError(RuntimeError):
    def __init__(self, *, invoked: bool, boundary: str, cause: BaseException) -> None:
        self.invoked = invoked
        self.boundary = boundary
        super().__init__(boundary)
        self.__cause__ = cause


class _ArtifactBoundaryError(CanonicalStorageError):
    def __init__(self, code: RunStorageFailureCode, cause: BaseException) -> None:
        self.code = code
        super().__init__(code.value)
        self.__cause__ = cause


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_id_factory(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(16)}"


def _directory_is_reparse(path: Path) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _reject_linked_path_chain(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    components = (*reversed(absolute.parents), absolute)
    for component in components:
        try:
            info = component.lstat()
        except FileNotFoundError:
            break
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise CanonicalStorageError("root path chain contains a link or reparse point")
    return absolute


def _strict_root_paths(revision_root: Path, legacy_runs_root: Path) -> tuple[Path, Path]:
    revision_input = _reject_linked_path_chain(revision_root)
    legacy_input = _reject_linked_path_chain(legacy_runs_root)
    revision = revision_input.resolve(strict=False)
    try:
        legacy = legacy_input.resolve(strict=True)
    except OSError as exc:
        raise CanonicalStorageError("legacy runs root must already exist") from exc
    if _directory_is_reparse(legacy):
        raise CanonicalStorageError("legacy runs root must be a regular non-reparse directory")
    if revision == legacy or revision in legacy.parents or legacy in revision.parents:
        raise CanonicalStorageError("revision and legacy roots must not overlap")
    parent = revision.parent
    if not parent.exists() or _directory_is_reparse(parent):
        raise CanonicalStorageError("revision root parent must be an existing regular directory")
    check_path_lengths((revision, legacy))
    return revision, legacy


def _directory_sync(
    path: Path,
    *,
    injector: FaultInjector | None = None,
    hook: str = "directory_sync",
) -> str:
    verify_directory(path)
    if os.name == "nt":
        return "unavailable"
    descriptor: int | None = None
    try:
        if injector is not None:
            injector(f"{hook}.before_open")
        descriptor = os.open(path, os.O_RDONLY)
        if injector is not None:
            injector(f"{hook}.after_open")
    except Exception as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise _StorageBoundaryError("write", f"{hook}.open", exc) from exc
    try:
        if injector is not None:
            injector(f"{hook}.before_fsync")
        os.fsync(descriptor)
        if injector is not None:
            injector(f"{hook}.after_fsync")
    except Exception as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise _StorageBoundaryError("durability", f"{hook}.fsync", exc) from exc
    try:
        if injector is not None:
            injector(f"{hook}.before_close")
        os.close(descriptor)
        descriptor = None
        if injector is not None:
            injector(f"{hook}.after_close")
    except Exception as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise _StorageBoundaryError("write", f"{hook}.close", exc) from exc
    try:
        if injector is not None:
            injector(f"{hook}.before_identity_reread")
        verify_directory(path)
        if injector is not None:
            injector(f"{hook}.after_identity_reread")
    except Exception as exc:
        raise _StorageBoundaryError("verification", f"{hook}.identity", exc) from exc
    return "confirmed"


def _write_exclusive_verified(
    path: Path,
    data: bytes,
    *,
    max_bytes: int,
    injector: FaultInjector | None,
    hook: str,
) -> None:
    if len(data) > max_bytes:
        raise CanonicalStorageError("control artifact exceeds exact byte limit")
    check_path_lengths((path,))
    stream: Any = None
    try:
        if injector is not None:
            injector(f"{hook}.before_open")
        stream = path.open("xb")
        if injector is not None:
            injector(f"{hook}.after_open")
    except Exception as exc:
        if stream is not None:
            with suppress(Exception):
                stream.close()
        raise _StorageBoundaryError("write", f"{hook}.open", exc) from exc
    try:
        if injector is not None:
            injector(f"{hook}.before_write")
        stream.write(data)
        if injector is not None:
            injector(f"{hook}.after_write")
    except Exception as exc:
        with suppress(Exception):
            stream.close()
        raise _StorageBoundaryError("write", f"{hook}.write", exc) from exc
    try:
        if injector is not None:
            injector(f"{hook}.before_flush")
        stream.flush()
        if injector is not None:
            injector(f"{hook}.after_flush")
            injector(f"{hook}.before_fsync")
        os.fsync(stream.fileno())
        if injector is not None:
            injector(f"{hook}.after_fsync")
    except Exception as exc:
        with suppress(Exception):
            stream.close()
        raise _StorageBoundaryError("durability", f"{hook}.fsync", exc) from exc
    try:
        if injector is not None:
            injector(f"{hook}.before_close")
        stream.close()
        if injector is not None:
            injector(f"{hook}.after_close")
    except Exception as exc:
        with suppress(Exception):
            stream.close()
        raise _StorageBoundaryError("write", f"{hook}.close", exc) from exc
    try:
        if injector is not None:
            injector(f"{hook}.before_reread")
        verify_regular_single_link(path)
        reread = path.read_bytes()
        if injector is not None:
            injector(f"{hook}.after_reread")
            injector(f"{hook}.before_verify")
        if reread != data:
            raise CanonicalStorageError("written bytes did not reread exactly")
        if injector is not None:
            injector(f"{hook}.after_verify")
    except Exception as exc:
        raise _StorageBoundaryError("verification", f"{hook}.verification", exc) from exc


def _mkdir_verified(
    path: Path,
    *,
    injector: FaultInjector | None,
    hook: str,
) -> None:
    try:
        if injector is not None:
            injector(f"{hook}.before_mkdir")
        path.mkdir()
        if injector is not None:
            injector(f"{hook}.after_mkdir")
    except Exception as exc:
        raise _StorageBoundaryError("write", f"{hook}.mkdir", exc) from exc
    try:
        verify_directory(path)
    except Exception as exc:
        raise _StorageBoundaryError("verification", f"{hook}.identity", exc) from exc
    _directory_sync(
        path.parent,
        injector=injector,
        hook=f"{hook}.parent_sync",
    )


def _read_control(
    path: Path,
    model: type[T],
    *,
    max_bytes: int,
    injector: FaultInjector | None = None,
    hook: str | None = None,
    allowed_link_counts: frozenset[int] = frozenset({1}),
) -> tuple[T, bytes, str]:
    if injector is not None and hook is not None:
        injector(f"{hook}.before_reread")
    if allowed_link_counts == frozenset({1}):
        verify_regular_single_link(path)
    else:
        info = path.lstat()
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or attributes & reparse_flag
            or info.st_nlink not in allowed_link_counts
        ):
            raise CanonicalStorageError("control file has an unrecognized link state")
    data = path.read_bytes()
    if injector is not None and hook is not None:
        injector(f"{hook}.after_reread")
    if len(data) > max_bytes:
        raise CanonicalStorageError("control artifact exceeds exact byte limit")
    if injector is not None and hook is not None:
        injector(f"{hook}.before_hash")
    digest = sha256_bytes(data)
    if injector is not None and hook is not None:
        injector(f"{hook}.after_hash")
        injector(f"{hook}.before_schema")
    value = parse_canonical_model(data, model)
    if injector is not None and hook is not None:
        injector(f"{hook}.after_schema")
    return value, data, digest


def _read_recovery_claim(
    path: Path,
    *,
    max_bytes: int,
    injector: FaultInjector | None = None,
    hook: str | None = None,
) -> tuple[RecoveryClaimV1, bytes, str]:
    claim, data, digest = _read_control(
        path,
        RecoveryClaimV1,
        max_bytes=max_bytes,
        injector=injector,
        hook=hook,
    )
    projection = claim.model_dump(mode="python")
    recorded_digest = cast(str, projection.pop("claim_sha256"))
    if injector is not None and hook is not None:
        injector(f"{hook}.before_claim_hash")
    if recovery_claim_sha256(projection) != recorded_digest:
        raise CanonicalStorageError("recovery claim digest mismatch")
    if injector is not None and hook is not None:
        injector(f"{hook}.after_claim_hash")
    return claim, data, digest


def _read_control_verified(
    path: Path,
    model: type[T],
    *,
    max_bytes: int,
    injector: FaultInjector | None,
    hook: str,
) -> tuple[T, bytes, str]:
    try:
        return _read_control(
            path,
            model,
            max_bytes=max_bytes,
            injector=injector,
            hook=hook,
        )
    except Exception as exc:
        raise _StorageBoundaryError("verification", hook, exc) from exc


def _root_durability(
    *,
    file_sync: str,
    directory_sync: str,
    reconciliation: str,
) -> DurabilityEvidenceV1:
    return DurabilityEvidenceV1(
        platform_adapter=cast(Any, platform_adapter()),
        file_sync=cast(Any, file_sync),
        directory_sync=cast(Any, directory_sync),
        pointer_replace="not_attempted",
        reconciliation=cast(Any, reconciliation),
    )


def _idle_durability() -> DurabilityEvidenceV1:
    return DurabilityEvidenceV1(
        platform_adapter=cast(Any, platform_adapter()),
        file_sync="not_attempted",
        directory_sync="not_attempted",
        pointer_replace="not_attempted",
        reconciliation="confirmed",
    )


def _published_durability(directory_state: str) -> DurabilityEvidenceV1:
    return DurabilityEvidenceV1(
        platform_adapter=cast(Any, platform_adapter()),
        file_sync="confirmed",
        directory_sync=cast(Any, directory_state),
        pointer_replace="confirmed",
        reconciliation="confirmed",
    )


def _reconciliation_durability(
    *,
    file_sync: str = "confirmed",
    pointer_replace: str = "not_attempted",
) -> DurabilityEvidenceV1:
    return DurabilityEvidenceV1(
        platform_adapter=cast(Any, platform_adapter()),
        file_sync=cast(Any, file_sync),
        directory_sync="not_attempted",
        pointer_replace=cast(Any, pointer_replace),
        reconciliation="required",
    )


def _root_failure(
    request: RootInitializationRequestV1,
    code: RunStorageFailureCode,
    *,
    stage: str,
    effect: str = "none",
    reconciliation: bool = False,
    durability: DurabilityEvidenceV1 | None = None,
) -> RunStorageError:
    return RunStorageError(
        RunStorageFailureV1(
            code=code,
            stage=cast(Any, stage),
            message=code.value,
            automatic_retry_allowed=False,
            retryable=code is RunStorageFailureCode.RUN_LOCKED,
            reconciliation_required=reconciliation,
            filesystem_effect=cast(Any, effect),
            domain_effect="not_started",
            previous_revision_effect="not_applicable",
            root_id=request.root_id,
            run_id_sha256=None,
            transaction_id=None,
            expected_revision=None,
            observed_revision=None,
            durability_evidence=durability,
        )
    )


def _run_failure(
    run_id: str,
    code: RunStorageFailureCode,
    *,
    stage: str,
    transaction_id: str | None = None,
    expected_revision: int | None = None,
    observed_revision: int | None = None,
    effect: str = "none",
    domain_effect: str = "current_unchanged",
    previous_effect: str = "not_applicable",
    reconciliation: bool = False,
    durability: DurabilityEvidenceV1 | None = None,
) -> RunStorageError:
    return RunStorageError(
        RunStorageFailureV1(
            code=code,
            stage=cast(Any, stage),
            message=code.value,
            automatic_retry_allowed=False,
            retryable=code is RunStorageFailureCode.RUN_LOCKED,
            reconciliation_required=reconciliation,
            filesystem_effect=cast(Any, effect),
            domain_effect=cast(Any, domain_effect),
            previous_revision_effect=cast(Any, previous_effect),
            root_id=None,
            run_id_sha256=run_id_sha256(run_id),
            transaction_id=transaction_id,
            expected_revision=expected_revision,
            observed_revision=observed_revision,
            durability_evidence=durability,
        )
    )


def _initialization_paths(request: RootInitializationRequestV1) -> tuple[Path, ...]:
    root = request.revision_root.resolve(strict=False)
    control_temp = root / f".revision-control.{request.root_id}.tmp"
    return (
        root,
        root / ".revision-init.authority.lock",
        root / "ownership.json",
        root / f"ownership.{request.root_id}.tmp",
        control_temp,
        control_temp / "locks",
        root / f"runs.{request.root_id}.tmp",
        root / ".revision-control",
        root / ".revision-control" / "locks",
        root / "runs",
    )


def inspect_root_initialization(
    revision_root: Path,
    legacy_runs_root: Path,
    *,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
) -> RootInitializationInspectionV1:
    """Inspect initialization without creating a path or lock."""

    ensure_strict_positive_int(max_artifact_bytes, "max_artifact_bytes")
    root, legacy = _strict_root_paths(revision_root, legacy_runs_root)
    if not root.exists():
        return RootInitializationInspectionV1(status="uninitialized")
    if _directory_is_reparse(root):
        return RootInitializationInspectionV1(status="corrupt")
    names = tuple(sorted((item.name for item in root.iterdir()), key=lambda item: item.encode()))
    recognized: list[str] = []
    root_ids: set[str] = set()
    allowed_fixed = {
        ".revision-init.authority.lock",
        "ownership.json",
        ".revision-control",
        "runs",
    }
    for name in names:
        if name in allowed_fixed:
            recognized.append(name)
            continue
        match = _ROOT_TEMP.fullmatch(name)
        if match is None:
            return RootInitializationInspectionV1(
                status="corrupt",
                recognized_relative_paths=tuple(sorted(recognized, key=lambda item: item.encode())),
            )
        root_ids.add(match.group(1))
        recognized.append(name)
    marker_path = root / "ownership.json"
    if marker_path.exists():
        try:
            marker, _data, marker_sha = _read_control(
                marker_path,
                OwnershipMarkerV1,
                max_bytes=max_artifact_bytes,
            )
            if marker.legacy_runs_root_identity_sha256 != legacy_root_identity_sha256(legacy):
                raise CanonicalStorageError("legacy root identity mismatch")
            if root_ids:
                raise CanonicalStorageError("initialized root retains initialization temp state")
            if set(names) != allowed_fixed:
                raise CanonicalStorageError("initialized root entry set is not exact")
            authority = root / ".revision-init.authority.lock"
            authority_stat = verify_regular_single_link(authority)
            if authority_stat.st_size not in {0, 1}:
                raise CanonicalStorageError("root authority length is not repairable")
            try:
                authority_bytes = authority.read_bytes()
            except PermissionError:
                if os.name != "nt":
                    raise
            else:
                if authority_bytes not in {b"", b"\0"}:
                    raise CanonicalStorageError("root authority is not the exact stable byte")
            if not (root / ".revision-control" / "locks").is_dir() or not (root / "runs").is_dir():
                raise CanonicalStorageError("initialized root lacks control directories")
            verify_directory(root / ".revision-control")
            verify_directory(root / ".revision-control" / "locks")
            verify_directory(root / "runs")
            return RootInitializationInspectionV1(
                status="initialized",
                root_id=marker.root_id,
                ownership_marker_sha256=marker_sha,
                recognized_relative_paths=tuple(recognized),
            )
        except (CanonicalStorageError, OSError, ValidationError):
            return RootInitializationInspectionV1(
                status="corrupt",
                recognized_relative_paths=tuple(recognized),
            )
    if not names:
        return RootInitializationInspectionV1(status="uninitialized")
    if names == (".revision-init.authority.lock",):
        return RootInitializationInspectionV1(
            status="uninitialized",
            recognized_relative_paths=names,
        )
    if len(root_ids) == 1:
        return RootInitializationInspectionV1(
            status="incomplete",
            root_id=next(iter(root_ids)),
            recognized_relative_paths=tuple(recognized),
        )
    return RootInitializationInspectionV1(
        status="corrupt",
        recognized_relative_paths=tuple(recognized),
    )


def initialize_revision_root(
    request: RootInitializationRequestV1,
    *,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    fault_injector: FaultInjector | None = None,
) -> RootInitializationOutcomeV1:
    """Explicitly initialize one empty dedicated revision root."""

    ensure_strict_positive_int(max_artifact_bytes, "max_artifact_bytes")
    try:
        root, legacy = _strict_root_paths(request.revision_root, request.legacy_runs_root)
        if request.revision_root.resolve(strict=False) != root:
            raise CanonicalStorageError("revision root identity mismatch")
        check_path_lengths(_initialization_paths(request))
        if root.exists():
            inspection = inspect_root_initialization(
                root,
                legacy,
                max_artifact_bytes=max_artifact_bytes,
            )
            if inspection.status == "corrupt":
                raise _root_failure(
                    request,
                    RunStorageFailureCode.RUN_NAMESPACE_CONFLICT,
                    stage="root_preflight",
                )
        legacy_identity = legacy_root_identity_sha256(legacy)
        marker_plan = OwnershipMarkerV1(
            root_id=request.root_id,
            legacy_runs_root_identity_sha256=legacy_identity,
            initialized_at=request.initialized_at,
            producer_id=request.producer_id,
            producer_version=request.producer_version,
        )
        marker_plan_bytes = canonical_json_bytes(marker_plan)
        marker_plan_sha = sha256_bytes(marker_plan_bytes)
        if len(marker_plan_bytes) > max_artifact_bytes:
            raise _root_failure(
                request,
                RunStorageFailureCode.ARTIFACT_BUDGET_EXCEEDED,
                stage="root_preflight",
            )
    except RunStorageError:
        raise
    except (CanonicalStorageError, OSError, ValidationError) as exc:
        message = str(exc)
        raise _root_failure(
            request,
            (
                RunStorageFailureCode.LINK_OR_REPARSE_DETECTED
                if "link" in message or "reparse" in message
                else RunStorageFailureCode.PATH_CONFINEMENT_FAILED
            ),
            stage="root_preflight",
        ) from exc

    intent_key = (
        f"root-intent:{root.parent.stat().st_dev}:{root.parent.stat().st_ino}:"
        f"{ascii_casefold(root.name)}"
    )
    authority = root / ".revision-init.authority.lock"
    root_created = False

    def prepare_root() -> None:
        nonlocal root_created
        try:
            if not root.exists():
                if fault_injector is not None:
                    fault_injector("root.before_mkdir")
                root.mkdir()
                root_created = True
                if fault_injector is not None:
                    fault_injector("root.after_mkdir_before_authority")
                _directory_sync(
                    root.parent,
                    injector=fault_injector,
                    hook="root.parent_sync",
                )
        except Exception as exc:
            boundary_kind = exc.kind if isinstance(exc, _StorageBoundaryError) else "write"
            raise LockUnavailableError(
                control_changed=root_created,
                boundary_kind=boundary_kind,
            ) from exc
        try:
            verify_directory(root)
        except Exception as exc:
            raise LockUnavailableError(
                control_changed=root_created,
                boundary_kind="verification",
            ) from exc

    def root_identity_keys() -> tuple[str, ...]:
        identity = root.stat()
        return (f"root-identity:{identity.st_dev}:{identity.st_ino}",)

    try:
        lease = acquire_authority(
            authority,
            registry_keys=(intent_key,),
            bootstrap=True,
            injector=fault_injector,
            prepare=prepare_root,
            additional_registry_keys=root_identity_keys,
        )
    except LockBusyError as exc:
        raise _root_failure(
            request,
            RunStorageFailureCode.RUN_LOCKED,
            stage="root_initialization",
            effect="control_only" if exc.control_changed or root_created else "none",
        ) from exc
    except LockUnavailableError as exc:
        code = {
            "unavailable": RunStorageFailureCode.LOCK_UNAVAILABLE,
            "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
            "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
            "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
        }[exc.boundary_kind]
        raise _root_failure(
            request,
            code,
            stage="root_initialization",
            effect="control_only" if exc.control_changed or root_created else "none",
            reconciliation=exc.control_changed or root_created,
        ) from exc

    outcome: RootInitializationOutcomeV1 | None = None
    try:
        if legacy_root_identity_sha256(legacy) != legacy_identity:
            raise _root_failure(
                request,
                RunStorageFailureCode.RUN_NAMESPACE_CONFLICT,
                stage="root_initialization",
                effect="control_only" if lease.control_changed else "none",
            )
        inspection = inspect_root_initialization(
            root,
            legacy,
            max_artifact_bytes=max_artifact_bytes,
        )
        if inspection.status == "initialized":
            marker, _data, marker_sha = _read_control_verified(
                root / "ownership.json",
                OwnershipMarkerV1,
                max_bytes=max_artifact_bytes,
                injector=fault_injector,
                hook="root.existing_ownership",
            )
            if (
                marker.root_id != request.root_id
                or marker.initialized_at != request.initialized_at
                or marker.producer_id != request.producer_id
                or marker.producer_version != request.producer_version
                or marker.legacy_runs_root_identity_sha256 != legacy_identity
            ):
                raise _root_failure(
                    request,
                    RunStorageFailureCode.RUN_NAMESPACE_CONFLICT,
                    stage="root_initialization",
                    effect="control_only" if lease.control_changed else "none",
                )
            outcome = RootInitializationOutcomeV1(
                outcome_kind="already_initialized",
                root_id=request.root_id,
                ownership_marker_sha256=marker_sha,
                filesystem_effect="none",
                durability_evidence=_idle_durability(),
            )
            return outcome
        if inspection.status not in {"uninitialized"}:
            outcome = RootInitializationOutcomeV1(
                outcome_kind="reconciliation_required",
                root_id=request.root_id,
                ownership_marker_sha256=None,
                filesystem_effect="control_only",
                durability_evidence=_root_durability(
                    file_sync="not_attempted",
                    directory_sync="not_attempted",
                    reconciliation="required",
                ),
            )
            return outcome

        marker = marker_plan
        marker_bytes = marker_plan_bytes
        marker_sha = marker_plan_sha
        ownership_temp = root / f"ownership.{request.root_id}.tmp"
        control_temp = root / f".revision-control.{request.root_id}.tmp"
        runs_temp = root / f"runs.{request.root_id}.tmp"
        _write_exclusive_verified(
            ownership_temp,
            marker_bytes,
            max_bytes=max_artifact_bytes,
            injector=fault_injector,
            hook="root.ownership",
        )
        _mkdir_verified(
            control_temp,
            injector=fault_injector,
            hook="root.control_temp",
        )
        _mkdir_verified(
            control_temp / "locks",
            injector=fault_injector,
            hook="root.locks_temp",
        )
        _mkdir_verified(
            runs_temp,
            injector=fault_injector,
            hook="root.runs_temp",
        )
        for source, target, hook in (
            (control_temp, root / ".revision-control", "root.control_rename"),
            (runs_temp, root / "runs", "root.runs_rename"),
        ):
            invoked = False
            try:
                if fault_injector is not None:
                    fault_injector(
                        "root.before_control_rename"
                        if hook == "root.control_rename"
                        else "root.before_runs_rename"
                    )
                    fault_injector(f"{hook}.before")
                invoked = True
                source.rename(target)
                if fault_injector is not None:
                    fault_injector(f"{hook}.after")
            except Exception as exc:
                raise _PublishBoundaryError(
                    invoked=invoked,
                    boundary=hook,
                    cause=exc,
                ) from exc
            _directory_sync(
                root,
                injector=fault_injector,
                hook=f"{hook}.parent_sync",
            )
        marker_replace_invoked = False
        try:
            if fault_injector is not None:
                fault_injector("root.before_marker_replace")
                fault_injector("root.marker_replace.before")
            marker_replace_invoked = True
            os.replace(ownership_temp, root / "ownership.json")
            if fault_injector is not None:
                fault_injector("root.marker_replace.after")
        except Exception as exc:
            raise _PublishBoundaryError(
                invoked=marker_replace_invoked,
                boundary="root.marker_replace",
                cause=exc,
            ) from exc
        marker_check, _data, marker_check_sha = _read_control_verified(
            root / "ownership.json",
            OwnershipMarkerV1,
            max_bytes=max_artifact_bytes,
            injector=fault_injector,
            hook="root.final_ownership",
        )
        if marker_check != marker or marker_check_sha != marker_sha:
            raise CanonicalStorageError("ownership marker reconciliation failed")
        directory_state = _directory_sync(
            root,
            injector=fault_injector,
            hook="root.directory_sync",
        )
        outcome = RootInitializationOutcomeV1(
            outcome_kind="initialized",
            root_id=request.root_id,
            ownership_marker_sha256=marker_sha,
            filesystem_effect="control_only",
            durability_evidence=_root_durability(
                file_sync="confirmed",
                directory_sync=directory_state,
                reconciliation="confirmed",
            ),
        )
        return outcome
    except RunStorageError:
        raise
    except _StorageBoundaryError as exc:
        code = {
            "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
            "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
            "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
        }[exc.kind]
        raise _root_failure(
            request,
            code,
            stage="root_initialization",
            effect="control_only",
            reconciliation=True,
            durability=_root_durability(
                file_sync="failed" if exc.kind == "durability" else "not_attempted",
                directory_sync="not_attempted",
                reconciliation="required",
            ),
        ) from exc
    except _PublishBoundaryError as exc:
        raise _root_failure(
            request,
            (
                RunStorageFailureCode.EFFECT_UNKNOWN
                if exc.invoked
                else RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED
            ),
            stage="root_initialization",
            effect="control_only",
            reconciliation=True,
            durability=_root_durability(
                file_sync="confirmed",
                directory_sync="not_attempted",
                reconciliation="required",
            ),
        ) from exc
    except Exception as exc:
        raise _root_failure(
            request,
            RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
            stage="root_initialization",
            effect="control_only",
            reconciliation=True,
            durability=_root_durability(
                file_sync="failed",
                directory_sync="not_attempted",
                reconciliation="required",
            ),
        ) from exc
    finally:
        try:
            lease.release()
        except LockReleaseError as exc:
            raise _root_failure(
                request,
                RunStorageFailureCode.EFFECT_UNKNOWN,
                stage="root_initialization",
                effect=(outcome.filesystem_effect if outcome is not None else "control_only"),
                reconciliation=True,
            ) from exc


def reconcile_revision_root(
    request: RootInitializationRequestV1,
    *,
    expected_ownership_marker_sha256: str,
    max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
    fault_injector: FaultInjector | None = None,
) -> RootInitializationOutcomeV1:
    """Finish only an exact, single-root-ID recognized partial initialization."""
    ensure_strict_positive_int(max_artifact_bytes, "max_artifact_bytes")
    try:
        root, legacy = _strict_root_paths(request.revision_root, request.legacy_runs_root)
        check_path_lengths(_initialization_paths(request))
        inspection = inspect_root_initialization(
            root,
            legacy,
            max_artifact_bytes=max_artifact_bytes,
        )
        if inspection.status == "initialized":
            if inspection.ownership_marker_sha256 != expected_ownership_marker_sha256:
                raise _root_failure(
                    request,
                    RunStorageFailureCode.RUN_NAMESPACE_CONFLICT,
                    stage="root_preflight",
                )
        elif inspection.status != "incomplete" or inspection.root_id != request.root_id:
            raise _root_failure(
                request,
                RunStorageFailureCode.ROOT_INITIALIZATION_INCOMPLETE,
                stage="root_preflight",
            )
    except RunStorageError:
        raise
    except (CanonicalStorageError, OSError, ValidationError) as exc:
        message = str(exc)
        raise _root_failure(
            request,
            (
                RunStorageFailureCode.LINK_OR_REPARSE_DETECTED
                if "link" in message or "reparse" in message
                else RunStorageFailureCode.PATH_CONFINEMENT_FAILED
            ),
            stage="root_preflight",
        ) from exc

    intent_key = (
        f"root-intent:{root.parent.stat().st_dev}:{root.parent.stat().st_ino}:"
        f"{ascii_casefold(root.name)}"
    )

    def root_identity_keys() -> tuple[str, ...]:
        identity = root.stat()
        return (f"root-identity:{identity.st_dev}:{identity.st_ino}",)

    try:
        lease = acquire_authority(
            root / ".revision-init.authority.lock",
            registry_keys=(intent_key,),
            bootstrap=True,
            injector=fault_injector,
            additional_registry_keys=root_identity_keys,
        )
    except LockBusyError as exc:
        raise _root_failure(
            request,
            RunStorageFailureCode.RUN_LOCKED,
            stage="root_initialization",
            effect="control_only" if exc.control_changed else "none",
        ) from exc
    except LockUnavailableError as exc:
        code = {
            "unavailable": RunStorageFailureCode.LOCK_UNAVAILABLE,
            "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
            "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
            "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
        }[exc.boundary_kind]
        raise _root_failure(
            request,
            code,
            stage="root_initialization",
            effect="control_only" if exc.control_changed else "none",
            reconciliation=exc.control_changed,
        ) from exc

    outcome: RootInitializationOutcomeV1 | None = None
    try:
        expected_legacy_identity = legacy_root_identity_sha256(legacy)
        locked_inspection = inspect_root_initialization(
            root,
            legacy,
            max_artifact_bytes=max_artifact_bytes,
        )
        if locked_inspection.status == "initialized":
            if locked_inspection.ownership_marker_sha256 != expected_ownership_marker_sha256:
                raise _root_failure(
                    request,
                    RunStorageFailureCode.RUN_NAMESPACE_CONFLICT,
                    stage="root_initialization",
                )
            outcome = RootInitializationOutcomeV1(
                outcome_kind="already_initialized",
                root_id=request.root_id,
                ownership_marker_sha256=expected_ownership_marker_sha256,
                filesystem_effect="none",
                durability_evidence=_idle_durability(),
            )
            return outcome
        if locked_inspection.status != "incomplete" or locked_inspection.root_id != request.root_id:
            raise _root_failure(
                request,
                RunStorageFailureCode.ROOT_INITIALIZATION_INCOMPLETE,
                stage="root_initialization",
                effect="control_only",
                reconciliation=True,
            )
        marker_temp = root / f"ownership.{request.root_id}.tmp"
        marker, marker_bytes, marker_sha = _read_control_verified(
            marker_temp,
            OwnershipMarkerV1,
            max_bytes=max_artifact_bytes,
            injector=fault_injector,
            hook="root.reconcile.ownership_temp",
        )
        expected_marker = OwnershipMarkerV1(
            root_id=request.root_id,
            legacy_runs_root_identity_sha256=expected_legacy_identity,
            initialized_at=request.initialized_at,
            producer_id=request.producer_id,
            producer_version=request.producer_version,
        )
        if (
            marker != expected_marker
            or marker_sha != expected_ownership_marker_sha256
            or marker_bytes != canonical_json_bytes(expected_marker)
        ):
            raise _root_failure(
                request,
                RunStorageFailureCode.RUN_NAMESPACE_CONFLICT,
                stage="root_initialization",
                effect="control_only",
                reconciliation=True,
            )
        control_temp = root / f".revision-control.{request.root_id}.tmp"
        runs_temp = root / f"runs.{request.root_id}.tmp"
        control_final = root / ".revision-control"
        runs_final = root / "runs"
        if control_final.exists() and control_temp.exists():
            raise _root_failure(
                request,
                RunStorageFailureCode.RUN_NAMESPACE_CONFLICT,
                stage="root_initialization",
                effect="control_only",
                reconciliation=True,
            )
        if runs_final.exists() and runs_temp.exists():
            raise _root_failure(
                request,
                RunStorageFailureCode.RUN_NAMESPACE_CONFLICT,
                stage="root_initialization",
                effect="control_only",
                reconciliation=True,
            )
        if control_final.exists():
            verify_directory(control_final)
            if {item.name for item in control_final.iterdir()} != {"locks"}:
                raise CanonicalStorageError("partial control root is not exact")
            verify_directory(control_final / "locks")
            if any((control_final / "locks").iterdir()):
                raise CanonicalStorageError("partial lock root is not empty")
        else:
            verify_directory(control_temp)
            verify_directory(control_temp / "locks")
            if {item.name for item in control_temp.iterdir()} != {"locks"}:
                raise CanonicalStorageError("partial control temp is not exact")
            if any((control_temp / "locks").iterdir()):
                raise CanonicalStorageError("partial lock temp is not empty")
            invoked = False
            try:
                if fault_injector is not None:
                    fault_injector("root.reconcile.control_rename.before")
                invoked = True
                control_temp.rename(control_final)
                if fault_injector is not None:
                    fault_injector("root.reconcile.control_rename.after")
            except Exception as exc:
                raise _PublishBoundaryError(
                    invoked=invoked,
                    boundary="root.reconcile.control_rename",
                    cause=exc,
                ) from exc
            _directory_sync(
                root,
                injector=fault_injector,
                hook="root.reconcile.control_parent_sync",
            )
        if runs_final.exists():
            verify_directory(runs_final)
            if any(runs_final.iterdir()):
                raise CanonicalStorageError("partial runs root is not empty")
        else:
            verify_directory(runs_temp)
            if any(runs_temp.iterdir()):
                raise CanonicalStorageError("partial runs temp is not empty")
            invoked = False
            try:
                if fault_injector is not None:
                    fault_injector("root.reconcile.runs_rename.before")
                invoked = True
                runs_temp.rename(runs_final)
                if fault_injector is not None:
                    fault_injector("root.reconcile.runs_rename.after")
            except Exception as exc:
                raise _PublishBoundaryError(
                    invoked=invoked,
                    boundary="root.reconcile.runs_rename",
                    cause=exc,
                ) from exc
            _directory_sync(
                root,
                injector=fault_injector,
                hook="root.reconcile.runs_parent_sync",
            )
        marker_invoked = False
        try:
            if fault_injector is not None:
                fault_injector("root.reconcile.before_marker_replace")
            marker_invoked = True
            os.replace(marker_temp, root / "ownership.json")
            if fault_injector is not None:
                fault_injector("root.reconcile.after_marker_replace")
        except Exception as exc:
            raise _PublishBoundaryError(
                invoked=marker_invoked,
                boundary="root.reconcile.marker_replace",
                cause=exc,
            ) from exc
        final_marker, _final_bytes, final_sha = _read_control_verified(
            root / "ownership.json",
            OwnershipMarkerV1,
            max_bytes=max_artifact_bytes,
            injector=fault_injector,
            hook="root.reconcile.final_ownership",
        )
        if final_marker != expected_marker or final_sha != expected_ownership_marker_sha256:
            raise CanonicalStorageError("reconciled ownership marker mismatch")
        directory_state = _directory_sync(
            root,
            injector=fault_injector,
            hook="root.directory_sync",
        )
        outcome = RootInitializationOutcomeV1(
            outcome_kind="initialized",
            root_id=request.root_id,
            ownership_marker_sha256=final_sha,
            filesystem_effect="control_only",
            durability_evidence=_root_durability(
                file_sync="confirmed",
                directory_sync=directory_state,
                reconciliation="confirmed",
            ),
        )
        return outcome
    except RunStorageError:
        raise
    except _StorageBoundaryError as exc:
        code = {
            "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
            "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
            "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
        }[exc.kind]
        raise _root_failure(
            request,
            code,
            stage="root_initialization",
            effect="control_only",
            reconciliation=True,
            durability=_root_durability(
                file_sync="failed" if exc.kind == "durability" else "not_attempted",
                directory_sync="not_attempted",
                reconciliation="required",
            ),
        ) from exc
    except _PublishBoundaryError as exc:
        raise _root_failure(
            request,
            (
                RunStorageFailureCode.EFFECT_UNKNOWN
                if exc.invoked
                else RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED
            ),
            stage="root_initialization",
            effect="control_only",
            reconciliation=True,
        ) from exc
    except Exception as exc:
        raise _root_failure(
            request,
            RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED,
            stage="root_initialization",
            effect="control_only",
            reconciliation=True,
            durability=_root_durability(
                file_sync="failed",
                directory_sync="not_attempted",
                reconciliation="required",
            ),
        ) from exc
    finally:
        try:
            lease.release()
        except LockReleaseError as exc:
            raise _root_failure(
                request,
                RunStorageFailureCode.EFFECT_UNKNOWN,
                stage="root_initialization",
                effect=(outcome.filesystem_effect if outcome is not None else "control_only"),
                reconciliation=True,
            ) from exc


@dataclass(frozen=True)
class _PreparedRevision:
    request: RevisionPublishRequestV1
    inventories: tuple[PayloadInventoryEntryV1, ...]
    heads: tuple[Any, ...]
    transaction: RevisionTransactionDescriptorV1
    transaction_bytes: bytes
    inventory_sha256: str
    manifest: StorageRevisionManifestV1
    manifest_bytes: bytes
    manifest_sha256: str
    pointer: StorageRevisionPointerV1
    pointer_bytes: bytes
    pointer_sha256: str
    artifacts: Mapping[str, RevisionArtifactV1]


@dataclass(frozen=True)
class _VerifiedChain:
    pointer: StorageRevisionPointerV1
    pointer_sha256: str
    entries: tuple[
        tuple[
            ReachableRevisionV1,
            StorageRevisionManifestV1,
            str,
            RevisionTransactionDescriptorV1,
        ],
        ...,
    ]


class RunRevisionStore:
    """Side-effect-free handle for an explicitly initialized revision root."""

    def __init__(
        self,
        revision_root: Path,
        legacy_runs_root: Path,
        *,
        max_artifact_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        max_run_bytes: int = DEFAULT_MAX_RUN_BYTES,
        clock: Clock = _default_clock,
        id_factory: IdFactory = _default_id_factory,
        fault_injector: FaultInjector | None = None,
        producer_id: str = "poker-deliberation",
        producer_version: str = "0.1.0",
    ) -> None:
        self.max_artifact_bytes = ensure_strict_positive_int(
            max_artifact_bytes,
            "max_artifact_bytes",
        )
        self.max_run_bytes = ensure_strict_positive_int(max_run_bytes, "max_run_bytes")
        self.revision_root, self.legacy_runs_root = _strict_root_paths(
            revision_root,
            legacy_runs_root,
        )
        self._revision_root_identity: tuple[int, int] | None = None
        if self.revision_root.exists():
            verify_directory(self.revision_root)
            root_stat = self.revision_root.stat()
            self._revision_root_identity = (root_stat.st_dev, root_stat.st_ino)
        self.clock = clock
        self.id_factory = id_factory
        self.fault_injector = fault_injector
        self.producer_id = producer_id
        self.producer_version = producer_version

    @property
    def locks_root(self) -> Path:
        return self.revision_root / ".revision-control" / "locks"

    @property
    def runs_root(self) -> Path:
        return self.revision_root / "runs"

    def _ownership(self, run_id: str) -> tuple[OwnershipMarkerV1, str]:
        try:
            root_stat = self.revision_root.stat()
            observed_root_identity = (root_stat.st_dev, root_stat.st_ino)
            if (
                self._revision_root_identity is not None
                and observed_root_identity != self._revision_root_identity
            ):
                raise CanonicalStorageError("revision root identity changed")
            inspection = inspect_root_initialization(
                self.revision_root,
                self.legacy_runs_root,
                max_artifact_bytes=self.max_artifact_bytes,
            )
            if inspection.status != "initialized":
                raise CanonicalStorageError("revision root is not initialized")
            marker, _data, marker_sha = _read_control(
                self.revision_root / "ownership.json",
                OwnershipMarkerV1,
                max_bytes=self.max_artifact_bytes,
                injector=self.fault_injector,
                hook="ownership",
            )
            if marker.legacy_runs_root_identity_sha256 != legacy_root_identity_sha256(
                self.legacy_runs_root
            ):
                raise CanonicalStorageError("bound legacy root identity changed")
            if (
                marker.producer_id != self.producer_id
                or marker.producer_version != self.producer_version
            ):
                raise CanonicalStorageError("ownership producer does not match configured store")
            if self._revision_root_identity is None:
                self._revision_root_identity = observed_root_identity
            return marker, marker_sha
        except (CanonicalStorageError, OSError, ValidationError) as exc:
            raise _run_failure(
                run_id,
                RunStorageFailureCode.ROOT_INITIALIZATION_INCOMPLETE,
                stage="preflight",
            ) from exc

    def _run_paths(
        self,
        run_id: str,
        owner_token: str,
        transaction_id: str,
        revision: int,
    ) -> tuple[Path, ...]:
        key = run_lock_key_sha256(run_id)
        run = self.runs_root / run_id
        control = run / ".revision-store"
        staging = control / "transactions" / transaction_id
        revision_path = control / "revisions" / f"r{revision}-{transaction_id}"
        return (
            self.locks_root / f"{key}.authority.lock",
            self.locks_root / f"{key}.metadata.json",
            self.locks_root / f"{key}.{owner_token}.metadata.tmp",
            run,
            control,
            control / "transactions",
            control / "revisions",
            control / "recovery-claims",
            control / "recovery-claims" / ".tmp",
            staging,
            staging / "transaction.json",
            staging / "manifest.json",
            revision_path,
            control / "current.json",
            control / f"current.{transaction_id}.tmp",
        )

    def _preflight(self, request: RevisionPublishRequestV1) -> _PreparedRevision:
        try:
            validate_run_id(request.run_id)
            if (
                request.producer_id != self.producer_id
                or request.producer_version != self.producer_version
            ):
                raise CanonicalStorageError(
                    "request producer does not match configured store producer"
                )
            owner_probe = "owner-" + "0" * 32
            paths = list(
                self._run_paths(
                    request.run_id,
                    owner_probe,
                    request.transaction_id,
                    request.proposed_revision,
                )
            )
            for artifact in request.artifacts:
                paths.append(
                    self.runs_root
                    / request.run_id
                    / ".revision-store"
                    / "transactions"
                    / request.transaction_id
                    / "payload"
                    / artifact.logical_name
                )
            check_path_lengths(paths)
            inventories, heads, _parsed = build_inventory(
                request,
                max_artifact_bytes=self.max_artifact_bytes,
            )
            inventory_digest = inventory_sha256(inventories)
            base: dict[str, Any] = {
                "schema_version": "1.0.0",
                "storage_protocol": "poker-run-revision-v1",
                "canonicalization": "poker-run-storage-json-v1",
                "hash_algorithm": "sha256",
                "run_id": request.run_id,
                "transaction_id": request.transaction_id,
                "proposed_revision": request.proposed_revision,
                "expected_revision": request.expected_revision,
                "expected_manifest_sha256": request.expected_manifest_sha256,
                "expected_pointer_sha256": request.expected_pointer_sha256,
                "created_at": request.created_at,
                "producer_id": request.producer_id,
                "producer_version": request.producer_version,
                "artifact_plan": inventories,
                "provenance_heads": heads,
            }
            digest = transaction_sha256(base)
            transaction = RevisionTransactionDescriptorV1(
                **base,
                transaction_sha256=digest,
            )
            transaction_bytes = canonical_json_bytes(transaction)
            manifest = StorageRevisionManifestV1(
                run_id=request.run_id,
                revision=request.proposed_revision,
                transaction_id=request.transaction_id,
                transaction_sha256=transaction.transaction_sha256,
                previous_revision=request.expected_revision,
                previous_manifest_sha256=request.expected_manifest_sha256,
                expected_pointer_sha256=request.expected_pointer_sha256,
                created_at=request.created_at,
                producer_id=request.producer_id,
                producer_version=request.producer_version,
                inventory_sha256=inventory_digest,
                provenance_heads=heads,
                artifacts=inventories,
            )
            manifest_bytes = canonical_json_bytes(manifest)
            manifest_digest = sha256_bytes(manifest_bytes)
            pointer = StorageRevisionPointerV1(
                run_id=request.run_id,
                revision=request.proposed_revision,
                transaction_id=request.transaction_id,
                revision_relative_path=(
                    f"revisions/r{request.proposed_revision}-{request.transaction_id}"
                ),
                transaction_sha256=transaction.transaction_sha256,
                manifest_sha256=manifest_digest,
                inventory_sha256=inventory_digest,
                published_at=request.created_at,
            )
            pointer_bytes = canonical_json_bytes(pointer)
            control_sizes = (len(transaction_bytes), len(manifest_bytes), len(pointer_bytes))
            if any(size > self.max_artifact_bytes for size in control_sizes):
                raise CanonicalStorageError("control artifact exceeds exact byte limit")
            return _PreparedRevision(
                request=request,
                inventories=inventories,
                heads=heads,
                transaction=transaction,
                transaction_bytes=transaction_bytes,
                inventory_sha256=inventory_digest,
                manifest=manifest,
                manifest_bytes=manifest_bytes,
                manifest_sha256=manifest_digest,
                pointer=pointer,
                pointer_bytes=pointer_bytes,
                pointer_sha256=sha256_bytes(pointer_bytes),
                artifacts={artifact.logical_name: artifact for artifact in request.artifacts},
            )
        except (CanonicalStorageError, ValidationError) as exc:
            message = str(exc)
            if "path exceeds" in message or "segment exceeds" in message:
                code = RunStorageFailureCode.PATH_CONFINEMENT_FAILED
            elif "exceeds" in message:
                code = RunStorageFailureCode.ARTIFACT_BUDGET_EXCEEDED
            elif "classification cannot be persisted" in message:
                code = RunStorageFailureCode.ENCRYPTION_REQUIRED
            elif "classification" in message or "policy" in message:
                code = RunStorageFailureCode.PERSISTENCE_FORBIDDEN
            elif "schema" in message or "canonical" in message:
                code = RunStorageFailureCode.ARTIFACT_SCHEMA_ERROR
            else:
                code = RunStorageFailureCode.INVALID_STORAGE_INPUT
            raise _run_failure(
                request.run_id,
                code,
                stage="preflight",
                transaction_id=request.transaction_id,
                expected_revision=request.expected_revision,
                previous_effect=(
                    "not_applicable" if request.proposed_revision == 1 else "unchanged"
                ),
            ) from exc

    def _scan_case_aliases(self, run_id: str) -> None:
        alias = ascii_casefold(run_id)
        for root in (self.runs_root, self.legacy_runs_root):
            verify_directory(root)
            for sibling in root.iterdir():
                if ascii_casefold(sibling.name) != alias:
                    continue
                if root == self.legacy_runs_root or sibling.name != run_id:
                    raise _run_failure(
                        run_id,
                        (
                            RunStorageFailureCode.LEGACY_RUN_UNVERIFIED
                            if root == self.legacy_runs_root
                            else RunStorageFailureCode.RUN_NAMESPACE_CONFLICT
                        ),
                        stage="locked_admission",
                    )

    def _validate_existing_namespace(
        self,
        run_id: str,
        *,
        verify_immutable_revisions: bool = True,
    ) -> None:
        key = run_lock_key_sha256(run_id)
        allowed_lock_names = {
            f"{key}.authority.lock",
            f"{key}.metadata.json",
        }
        metadata_temp = re.compile(rf"^{re.escape(key)}\.owner-[0-9a-f]{{32}}\.metadata\.tmp$")
        for path in self.locks_root.glob(f"{key}*"):
            if path.name not in allowed_lock_names and metadata_temp.fullmatch(path.name) is None:
                raise CanonicalStorageError("run lock namespace has unknown entries")
            info = verify_regular_single_link(path)
            if path.name.endswith((".metadata.json", ".metadata.tmp")) and (
                info.st_size > self.max_artifact_bytes
            ):
                raise CanonicalStorageError("metadata exceeds exact byte limit")

        run = self.runs_root / run_id
        if not run.exists():
            return
        verify_directory(run)
        run_entries = {item.name: item for item in run.iterdir()}
        if set(run_entries) - {".revision-store"}:
            raise CanonicalStorageError("run namespace has unknown top-level entries")
        control = run_entries.get(".revision-store")
        if control is None:
            return
        verify_directory(control)
        control_entries = {item.name: item for item in control.iterdir()}
        for name, path in control_entries.items():
            if name in {"transactions", "revisions", "recovery-claims"}:
                verify_directory(path)
            elif name == "current.json" or _CURRENT_TEMP.fullmatch(name):
                verify_regular_single_link(path)
            else:
                raise CanonicalStorageError("run control namespace has unknown entries")

        transactions = control_entries.get("transactions")
        if transactions is not None:
            for path in transactions.iterdir():
                if _TRANSACTION_DIR.fullmatch(path.name) is None:
                    raise CanonicalStorageError("transaction namespace has unknown entries")
                verify_directory(path)
                entries = {item.name: item for item in path.iterdir()}
                if set(entries) - {"transaction.json", "manifest.json", "payload"}:
                    raise CanonicalStorageError("staging transaction has unknown entries")
                for control_name in ("transaction.json", "manifest.json"):
                    if control_name in entries:
                        verify_regular_single_link(entries[control_name])
                payload = entries.get("payload")
                if payload is not None:
                    verify_directory(payload)
                    for item in payload.rglob("*"):
                        relative = item.relative_to(payload).as_posix()
                        if item.is_dir():
                            verify_directory(item)
                            if relative not in {"agent_reports", "tool_results"}:
                                raise CanonicalStorageError(
                                    "staging payload has an unknown directory"
                                )
                        else:
                            verify_regular_single_link(item)
                            artifact_table_entry(relative)

        revisions = control_entries.get("revisions")
        if revisions is not None:
            for path in revisions.iterdir():
                if _REVISION_DIR.fullmatch(path.name) is None:
                    raise CanonicalStorageError("revision namespace has unknown entries")
                if verify_immutable_revisions:
                    self._verify_revision_directory(path, run_id=run_id)
                else:
                    verify_directory(path)

        claims = control_entries.get("recovery-claims")
        if claims is not None:
            for path in claims.iterdir():
                if path.name == ".tmp":
                    verify_directory(path)
                    for temp in path.iterdir():
                        if _CLAIM_TEMP.fullmatch(temp.name) is None:
                            raise CanonicalStorageError(
                                "recovery claim temp namespace has unknown entries"
                            )
                        verify_regular_single_link(temp)
                elif _CLAIM_FINAL.fullmatch(path.name):
                    verify_regular_single_link(path)
                else:
                    raise CanonicalStorageError("recovery claim namespace has unknown entries")

    def _authority(
        self,
        run_id: str,
        marker_sha: str,
        *,
        bootstrap: bool,
    ) -> AuthorityLease:
        key = run_lock_key_sha256(run_id)
        authority = self.locks_root / f"{key}.authority.lock"
        registry_key = (
            f"run:{self.revision_root.stat().st_dev}:{self.revision_root.stat().st_ino}:"
            f"{marker_sha}:{ascii_casefold(run_id)}"
        )
        try:
            return acquire_authority(
                authority,
                registry_keys=(registry_key,),
                bootstrap=bootstrap,
                injector=self.fault_injector,
            )
        except LockBusyError as exc:
            raise _run_failure(
                run_id,
                RunStorageFailureCode.RUN_LOCKED,
                stage="lock_bootstrap",
                effect="control_only" if exc.control_changed else "none",
            ) from exc
        except LockUnavailableError as exc:
            code = {
                "unavailable": RunStorageFailureCode.LOCK_UNAVAILABLE,
                "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
                "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
                "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
            }[exc.boundary_kind]
            raise _run_failure(
                run_id,
                code,
                stage="lock_bootstrap",
                effect="control_only" if exc.control_changed else "none",
                reconciliation=exc.control_changed,
            ) from exc

    def _metadata_value(
        self,
        request: RevisionPublishRequestV1,
        marker_sha: str,
        lease: AuthorityLease,
        owner_token: str,
        acquired_at: datetime,
    ) -> tuple[LockMetadataV1, bytes]:
        metadata = LockMetadataV1(
            run_id_sha256=run_id_sha256(request.run_id),
            ownership_marker_sha256=marker_sha,
            authority_identity_sha256=lease.authority_identity_sha256,
            owner_token=owner_token,
            process_id=os.getpid(),
            adapter=cast(Any, lease.adapter),
            transaction_id=request.transaction_id,
            expected_revision=request.expected_revision,
            acquired_at=acquired_at,
        )
        data = canonical_json_bytes(metadata)
        return metadata, data

    def _metadata(
        self,
        request: RevisionPublishRequestV1,
        metadata: LockMetadataV1,
        data: bytes,
        owner_token: str,
    ) -> None:
        key = run_lock_key_sha256(request.run_id)
        temp = self.locks_root / f"{key}.{owner_token}.metadata.tmp"
        final = self.locks_root / f"{key}.metadata.json"
        _write_exclusive_verified(
            temp,
            data,
            max_bytes=self.max_artifact_bytes,
            injector=self.fault_injector,
            hook="metadata",
        )
        replace_invoked = False
        try:
            if self.fault_injector is not None:
                self.fault_injector("metadata.before_replace")
            replace_invoked = True
            os.replace(temp, final)
            if self.fault_injector is not None:
                self.fault_injector("metadata.after_replace")
        except Exception as exc:
            raise _PublishBoundaryError(
                invoked=replace_invoked,
                boundary="metadata.replace",
                cause=exc,
            ) from exc
        try:
            if self.fault_injector is not None:
                self.fault_injector("metadata.before_final_reread")
            parsed, reread, _digest = _read_control(
                final,
                LockMetadataV1,
                max_bytes=self.max_artifact_bytes,
                injector=self.fault_injector,
                hook="metadata.final",
            )
            if self.fault_injector is not None:
                self.fault_injector("metadata.after_final_reread")
            if parsed != metadata or reread != data:
                raise CanonicalStorageError("metadata replace verification failed")
        except Exception as exc:
            raise _StorageBoundaryError(
                "verification",
                "metadata.final_verification",
                exc,
            ) from exc

    def _bootstrap_namespace(self, run_id: str) -> Path:
        run = self.runs_root / run_id
        expected = (
            run,
            run / ".revision-store",
            run / ".revision-store" / "transactions",
            run / ".revision-store" / "revisions",
            run / ".revision-store" / "recovery-claims",
            run / ".revision-store" / "recovery-claims" / ".tmp",
        )
        for index, path in enumerate(expected):
            if path.exists():
                verify_directory(path)
                if path == run:
                    unexpected = {item.name for item in path.iterdir()} - {".revision-store"}
                elif path.name == ".revision-store":
                    unexpected = {item.name for item in path.iterdir()} - {
                        "transactions",
                        "revisions",
                        "recovery-claims",
                        "current.json",
                    }
                    unexpected = {
                        item for item in unexpected if _CURRENT_TEMP.fullmatch(item) is None
                    }
                else:
                    unexpected = set()
                if unexpected:
                    raise CanonicalStorageError("existing namespace has unknown entries")
                continue
            if self.fault_injector is not None:
                try:
                    self.fault_injector(f"namespace.before_mkdir.{index}")
                except Exception as exc:
                    raise _StorageBoundaryError(
                        "write",
                        f"namespace.before_mkdir.{index}",
                        exc,
                    ) from exc
            try:
                path.mkdir()
            except Exception as exc:
                raise _StorageBoundaryError(
                    "write",
                    f"namespace.mkdir.{index}",
                    exc,
                ) from exc
            try:
                verify_directory(path)
            except Exception as exc:
                raise _StorageBoundaryError(
                    "verification",
                    f"namespace.identity.{index}",
                    exc,
                ) from exc
            if self.fault_injector is not None:
                try:
                    self.fault_injector(f"namespace.after_mkdir.{index}")
                except Exception as exc:
                    raise _StorageBoundaryError(
                        "write",
                        f"namespace.after_mkdir.{index}",
                        exc,
                    ) from exc
            _directory_sync(
                path.parent,
                injector=self.fault_injector,
                hook=f"namespace.parent_sync.{index}",
            )
        return run / ".revision-store"

    def _run_physical_bytes(self, run_id: str) -> int:
        key = run_lock_key_sha256(run_id)
        paths: list[Path] = []
        counted_identities: set[tuple[int, int]] = set()
        run = self.runs_root / run_id
        if run.exists():
            for item in run.rglob("*"):
                info = item.lstat()
                attributes = getattr(info, "st_file_attributes", 0)
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
                    raise CanonicalStorageError("linked or reparsed namespace entry")
                if stat.S_ISDIR(info.st_mode):
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise CanonicalStorageError("nonregular or hardlinked namespace entry")
                identity = (info.st_dev, info.st_ino)
                if identity in counted_identities:
                    continue
                counted_identities.add(identity)
                paths.append(item)
        for item in self.locks_root.glob(f"{key}*"):
            verify_regular_single_link(item)
            paths.append(item)
        return sum(item.stat().st_size for item in paths)

    def _verify_inventory_entry(
        self,
        revision_dir: Path,
        entry: PayloadInventoryEntryV1,
        *,
        run_id: str,
        injector: FaultInjector | None = None,
    ) -> RevisionArtifactV1:
        expected_table = artifact_table_entry(
            entry.logical_name,
            (entry.artifact_schema_version if entry.logical_name == "final_report.json" else None),
        )
        if entry.revision_relative_path != f"payload/{entry.logical_name}":
            raise CanonicalStorageError("stored payload path is not canonical")
        if (
            entry.media_type,
            entry.serialization,
            entry.artifact_schema_version,
            expected_table[3],
        ) != expected_table:
            raise CanonicalStorageError("stored artifact admission table mismatch")
        payload = revision_dir / entry.revision_relative_path
        try:
            if injector is not None:
                injector(f"immutable_payload.{entry.logical_name}.before_reread")
            if not payload.exists():
                raise FileNotFoundError(payload)
            verify_regular_single_link(payload)
            data = payload.read_bytes()
            if injector is not None:
                injector(f"immutable_payload.{entry.logical_name}.after_reread")
        except FileNotFoundError as exc:
            raise _ArtifactBoundaryError(
                RunStorageFailureCode.ARTIFACT_MISSING,
                exc,
            ) from exc
        except Exception as exc:
            raise _ArtifactBoundaryError(
                RunStorageFailureCode.ARTIFACT_SCHEMA_ERROR,
                exc,
            ) from exc
        if injector is not None:
            try:
                injector(f"immutable_payload.{entry.logical_name}.before_hash")
            except Exception as exc:
                raise _ArtifactBoundaryError(
                    RunStorageFailureCode.ARTIFACT_HASH_MISMATCH,
                    exc,
                ) from exc
        if len(data) != entry.size_bytes or sha256_bytes(data) != entry.sha256:
            raise _ArtifactBoundaryError(
                RunStorageFailureCode.ARTIFACT_HASH_MISMATCH,
                CanonicalStorageError("stored payload size/hash mismatch"),
            )
        if injector is not None:
            try:
                injector(f"immutable_payload.{entry.logical_name}.after_hash")
            except Exception as exc:
                raise _ArtifactBoundaryError(
                    RunStorageFailureCode.ARTIFACT_HASH_MISMATCH,
                    exc,
                ) from exc
        local_bindings = [
            binding
            for binding in entry.provenance_bindings
            if isinstance(binding, LocalDataBindingV1)
        ]
        if len(local_bindings) != 1:
            raise CanonicalStorageError("stored payload lacks local-data provenance")
        local = local_bindings[0]
        try:
            if injector is not None:
                injector(f"immutable_payload.{entry.logical_name}.before_schema")
            artifact = RevisionArtifactV1(
                logical_name=entry.logical_name,
                media_type=entry.media_type,
                artifact_schema_version=entry.artifact_schema_version,
                serialization=entry.serialization,
                exact_bytes=data,
                required=entry.required,
                classification=entry.classification,
                classification_source=entry.classification_source,
                classification_evidence=entry.classification_evidence,
                policy_sha256=local.policy_sha256,
                origin_kind=cast(Any, expected_table[3]),
                provenance_bindings=entry.provenance_bindings,
            )
            validate_artifact(artifact, run_id, self.max_artifact_bytes)
            if injector is not None:
                injector(f"immutable_payload.{entry.logical_name}.after_schema")
        except Exception as exc:
            raise _ArtifactBoundaryError(
                RunStorageFailureCode.ARTIFACT_SCHEMA_ERROR,
                exc,
            ) from exc
        return artifact

    def _verify_revision_directory(
        self,
        revision_dir: Path,
        *,
        run_id: str,
        expected_pointer: StorageRevisionPointerV1 | None = None,
        injector: FaultInjector | None = None,
    ) -> tuple[
        ReachableRevisionV1,
        StorageRevisionManifestV1,
        str,
        RevisionTransactionDescriptorV1,
    ]:
        verify_directory(revision_dir)
        match = _REVISION_DIR.fullmatch(revision_dir.name)
        staging_match = _TRANSACTION_DIR.fullmatch(revision_dir.name)
        if match is None and staging_match is None:
            raise CanonicalStorageError("invalid immutable revision directory name")
        try:
            if injector is not None:
                injector("immutable_control.before_transaction_reread")
            transaction, transaction_bytes, _transaction_file_sha = _read_control(
                revision_dir / "transaction.json",
                RevisionTransactionDescriptorV1,
                max_bytes=self.max_artifact_bytes,
                injector=injector,
                hook="immutable_control.transaction",
            )
            if injector is not None:
                injector("immutable_control.after_transaction_reread")
        except Exception as exc:
            if injector is None:
                raise
            raise _StorageBoundaryError(
                "verification",
                "immutable_control.transaction",
                exc,
            ) from exc
        try:
            if injector is not None:
                injector("immutable_control.transaction.before_identity")
            transaction_projection = transaction.model_dump(mode="python")
            recorded_transaction_sha = cast(
                str,
                transaction_projection.pop("transaction_sha256"),
            )
            if (
                transaction_sha256(transaction_projection) != recorded_transaction_sha
                or transaction.run_id != run_id
                or (
                    match is not None
                    and (
                        transaction.transaction_id != match.group("transaction")
                        or transaction.proposed_revision != int(match.group("revision"))
                    )
                )
                or (
                    staging_match is not None
                    and transaction.transaction_id != staging_match.group(0)
                )
            ):
                raise CanonicalStorageError("immutable transaction identity mismatch")
            if injector is not None:
                injector("immutable_control.transaction.after_identity")
        except Exception as exc:
            if injector is None:
                raise
            raise _StorageBoundaryError(
                "verification",
                "immutable_control.transaction.identity",
                exc,
            ) from exc
        try:
            if injector is not None:
                injector("immutable_control.before_manifest_reread")
            manifest, _manifest_bytes, manifest_sha = _read_control(
                revision_dir / "manifest.json",
                StorageRevisionManifestV1,
                max_bytes=self.max_artifact_bytes,
                injector=injector,
                hook="immutable_control.manifest",
            )
            if injector is not None:
                injector("immutable_control.after_manifest_reread")
        except Exception as exc:
            if injector is None:
                raise
            raise _StorageBoundaryError(
                "verification",
                "immutable_control.manifest",
                exc,
            ) from exc
        try:
            if injector is not None:
                injector("immutable_control.manifest.before_correlation")
            if (
                manifest.run_id != run_id
                or manifest.revision != transaction.proposed_revision
                or manifest.transaction_id != transaction.transaction_id
                or manifest.transaction_sha256 != transaction.transaction_sha256
                or manifest.previous_revision != transaction.expected_revision
                or manifest.previous_manifest_sha256 != transaction.expected_manifest_sha256
                or manifest.expected_pointer_sha256 != transaction.expected_pointer_sha256
                or manifest.created_at != transaction.created_at
                or manifest.producer_id != transaction.producer_id
                or manifest.producer_version != transaction.producer_version
                or manifest.producer_id != self.producer_id
                or manifest.producer_version != self.producer_version
                or manifest.artifacts != transaction.artifact_plan
                or manifest.provenance_heads != transaction.provenance_heads
            ):
                raise CanonicalStorageError("immutable manifest/transaction mismatch")
            if injector is not None:
                injector("immutable_control.manifest.after_correlation")
                injector("immutable_control.manifest.before_inventory_digest")
            if inventory_sha256(manifest.artifacts) != manifest.inventory_sha256:
                raise CanonicalStorageError("immutable manifest inventory digest mismatch")
            if injector is not None:
                injector("immutable_control.manifest.after_inventory_digest")
                injector("immutable_control.manifest.before_provenance_heads")
            if provenance_heads(manifest.artifacts) != manifest.provenance_heads:
                raise CanonicalStorageError("immutable manifest provenance heads mismatch")
            if injector is not None:
                injector("immutable_control.manifest.after_provenance_heads")
        except Exception as exc:
            if injector is None:
                raise
            raise _StorageBoundaryError(
                "verification",
                "immutable_control.manifest.correlation",
                exc,
            ) from exc
        try:
            if injector is not None:
                injector("immutable_control.manifest.before_inventory_paths")
            payload_root = revision_dir / "payload"
            verify_directory(payload_root)
            expected_payload_files = {
                PurePosixPath(entry.revision_relative_path).relative_to("payload").as_posix()
                for entry in manifest.artifacts
            }
            actual_payload_files: set[str] = set()
            expected_payload_directories: set[str] = set()
            for expected_file in expected_payload_files:
                parent = PurePosixPath(expected_file).parent
                while parent != PurePosixPath("."):
                    expected_payload_directories.add(parent.as_posix())
                    parent = parent.parent
            actual_payload_directories: set[str] = set()
            for item in payload_root.rglob("*"):
                relative = item.relative_to(payload_root).as_posix()
                info = item.lstat()
                attributes = getattr(info, "st_file_attributes", 0)
                reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
                    raise CanonicalStorageError(
                        "payload namespace contains a link or reparse point"
                    )
                if stat.S_ISDIR(info.st_mode):
                    verify_directory(item)
                    actual_payload_directories.add(relative)
                else:
                    verify_regular_single_link(item)
                    actual_payload_files.add(relative)
            if (
                actual_payload_files != expected_payload_files
                or actual_payload_directories != expected_payload_directories
            ):
                raise CanonicalStorageError(
                    "payload namespace does not match exact inventory paths"
                )
            if injector is not None:
                injector("immutable_control.manifest.after_inventory_paths")
        except Exception as exc:
            if injector is None:
                raise
            raise _StorageBoundaryError(
                "verification",
                "immutable_control.manifest.inventory_paths",
                exc,
            ) from exc
        artifacts = tuple(
            self._verify_inventory_entry(
                revision_dir,
                entry,
                run_id=run_id,
                injector=injector,
            )
            for entry in manifest.artifacts
        )
        verification_request = RevisionPublishRequestV1(
            run_id=run_id,
            transaction_id=transaction.transaction_id,
            proposed_revision=transaction.proposed_revision,
            expected_revision=transaction.expected_revision,
            expected_manifest_sha256=transaction.expected_manifest_sha256,
            expected_pointer_sha256=transaction.expected_pointer_sha256,
            created_at=transaction.created_at,
            producer_id=transaction.producer_id,
            producer_version=transaction.producer_version,
            artifacts=artifacts,
        )
        try:
            if injector is not None:
                injector("immutable_control.manifest.before_inventory_replay")
            verified_inventory, verified_heads, _parsed = build_inventory(
                verification_request,
                max_artifact_bytes=self.max_artifact_bytes,
            )
            if (
                verified_inventory != manifest.artifacts
                or verified_heads != manifest.provenance_heads
            ):
                raise CanonicalStorageError("stored inventory provenance replay mismatch")
            if injector is not None:
                injector("immutable_control.manifest.after_inventory_replay")
        except Exception as exc:
            if injector is None:
                raise
            raise _StorageBoundaryError(
                "verification",
                "immutable_control.manifest.inventory_replay",
                exc,
            ) from exc
        expected_files = {"transaction.json", "manifest.json", "payload"}
        if {item.name for item in revision_dir.iterdir()} != expected_files:
            raise CanonicalStorageError("immutable revision has unknown entries")
        if sha256_bytes(transaction_bytes) == manifest_sha:
            raise CanonicalStorageError("control hashes unexpectedly alias")
        if expected_pointer is not None and (
            expected_pointer.revision != manifest.revision
            or expected_pointer.transaction_id != manifest.transaction_id
            or expected_pointer.transaction_sha256 != manifest.transaction_sha256
            or expected_pointer.manifest_sha256 != manifest_sha
            or expected_pointer.inventory_sha256 != manifest.inventory_sha256
            or expected_pointer.published_at != manifest.created_at
        ):
            raise CanonicalStorageError("current pointer does not match its revision")
        reachable = ReachableRevisionV1(
            revision=manifest.revision,
            transaction_id=manifest.transaction_id,
            revision_relative_path=(f"revisions/r{manifest.revision}-{manifest.transaction_id}"),
            transaction_sha256=manifest.transaction_sha256,
            manifest_sha256=manifest_sha,
        )
        return reachable, manifest, manifest_sha, transaction

    def _read_chain(
        self,
        run_id: str,
        *,
        verify_unreachable: bool = True,
        injector: FaultInjector | None = None,
        hook: str | None = None,
    ) -> _VerifiedChain:
        validate_run_id(run_id)
        self._ownership(run_id)
        self._validate_existing_namespace(
            run_id,
            verify_immutable_revisions=verify_unreachable,
        )
        current = self.runs_root / run_id / ".revision-store" / "current.json"
        pointer, pointer_bytes, pointer_sha = _read_control(
            current,
            StorageRevisionPointerV1,
            max_bytes=self.max_artifact_bytes,
            injector=injector,
            hook=None if hook is None else f"{hook}.current",
        )
        if pointer.run_id != run_id:
            raise CanonicalStorageError("current pointer cross-run mismatch")
        entries: list[
            tuple[
                ReachableRevisionV1,
                StorageRevisionManifestV1,
                str,
                RevisionTransactionDescriptorV1,
            ]
        ] = []
        expected_revision = pointer.revision
        expected_manifest_sha = pointer.manifest_sha256
        seen: set[tuple[int, str]] = set()
        while True:
            if expected_revision == pointer.revision:
                revision_dir = (
                    self.runs_root / run_id / ".revision-store" / pointer.revision_relative_path
                )
            else:
                previous_manifest = entries[-1][1]
                candidates = list(
                    (self.runs_root / run_id / ".revision-store" / "revisions").glob(
                        f"r{expected_revision}-txn-*"
                    )
                )
                matching_candidates: list[Path] = []
                for candidate in candidates:
                    try:
                        candidate_manifest, _data, candidate_sha = _read_control(
                            candidate / "manifest.json",
                            StorageRevisionManifestV1,
                            max_bytes=self.max_artifact_bytes,
                            injector=injector,
                            hook=(
                                None
                                if hook is None
                                else f"{hook}.lineage_candidate.{expected_revision}"
                            ),
                        )
                    except (CanonicalStorageError, OSError, ValidationError):
                        continue
                    if (
                        candidate_sha == expected_manifest_sha
                        and candidate_manifest.run_id == run_id
                        and candidate_manifest.revision == expected_revision
                    ):
                        matching_candidates.append(candidate)
                if len(matching_candidates) != 1:
                    raise CanonicalStorageError("lineage previous revision is ambiguous or missing")
                revision_dir = matching_candidates[0]
                if previous_manifest.previous_manifest_sha256 != expected_manifest_sha:
                    raise CanonicalStorageError("lineage previous manifest hash mismatch")
            entry = self._verify_revision_directory(
                revision_dir,
                run_id=run_id,
                expected_pointer=pointer if expected_revision == pointer.revision else None,
                injector=injector,
            )
            reachable, manifest, manifest_sha, _transaction = entry
            identity = (reachable.revision, reachable.transaction_id)
            if identity in seen or manifest_sha != expected_manifest_sha:
                raise CanonicalStorageError("lineage cycle or manifest hash mismatch")
            seen.add(identity)
            entries.append(entry)
            if manifest.revision == 1:
                break
            if (
                manifest.previous_revision != manifest.revision - 1
                or manifest.previous_manifest_sha256 is None
            ):
                raise CanonicalStorageError("lineage decrement mismatch")
            expected_revision = manifest.previous_revision
            expected_manifest_sha = manifest.previous_manifest_sha256
        if injector is not None and hook is not None:
            injector(f"{hook}.before_current_consistency_reread")
        verify_regular_single_link(current)
        pointer_reread = current.read_bytes()
        if injector is not None and hook is not None:
            injector(f"{hook}.after_current_consistency_reread")
        if pointer_reread != pointer_bytes:
            raise CanonicalStorageError("current changed during structural read")
        return _VerifiedChain(
            pointer=pointer,
            pointer_sha256=pointer_sha,
            entries=tuple(entries),
        )

    def read_current(self, run_id: str) -> VerifiedStorageRevisionV1:
        """Verify one complete structural current-to-genesis chain without a lock."""

        chain: _VerifiedChain | None = None
        for attempt in range(3):
            try:
                chain = self._read_chain(run_id)
                break
            except FileNotFoundError as exc:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_INCOMPLETE,
                    stage="initial_read",
                ) from exc
            except RunStorageError:
                raise
            except CanonicalStorageError as exc:
                if str(exc) == "current changed during structural read" and attempt < 2:
                    continue
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_CORRUPT,
                    stage="initial_read",
                    reconciliation=True,
                ) from exc
            except (OSError, ValidationError) as exc:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_CORRUPT,
                    stage="initial_read",
                    reconciliation=True,
                ) from exc
        if chain is None:
            raise AssertionError("read retry loop ended without a verified chain")
        current_entry = chain.entries[0]
        return VerifiedStorageRevisionV1(
            run_id=run_id,
            current_revision=chain.pointer.revision,
            current_pointer_sha256=chain.pointer_sha256,
            manifest_sha256=current_entry[2],
            inventory_sha256=current_entry[1].inventory_sha256,
            reachable_history=tuple(entry[0] for entry in chain.entries),
        )

    def _read_structural_artifact_history(
        self,
        run_id: str,
        logical_name: str,
        *,
        artifact_schema_version: str | None = None,
    ) -> StructuralArtifactHistoryV1:
        """Return verified bytes without assigning product lifecycle status.

        This method is intentionally private and is not exported from
        :mod:`poker_deliberation.storage`.  It verifies the complete structural
        chain, each immutable payload, and a stable current pointer.
        """

        for attempt in range(3):
            try:
                chain = self._read_chain(run_id)
                revisions: list[StructuralArtifactRevisionV1] = []
                for reachable, manifest, manifest_sha, _transaction in chain.entries:
                    matches = tuple(
                        entry
                        for entry in manifest.artifacts
                        if entry.logical_name == logical_name
                    )
                    if len(matches) != 1:
                        raise CanonicalStorageError(
                            "structural artifact is missing or duplicated"
                        )
                    entry = matches[0]
                    if (
                        artifact_schema_version is not None
                        and entry.artifact_schema_version != artifact_schema_version
                    ):
                        raise CanonicalStorageError(
                            "structural artifact schema version mismatch"
                        )
                    revision_dir = (
                        self.runs_root
                        / run_id
                        / ".revision-store"
                        / reachable.revision_relative_path
                    )
                    artifact = self._verify_inventory_entry(
                        revision_dir,
                        entry,
                        run_id=run_id,
                    )
                    revisions.append(
                        StructuralArtifactRevisionV1(
                            revision=reachable.revision,
                            transaction_id=reachable.transaction_id,
                            manifest_sha256=manifest_sha,
                            logical_name=entry.logical_name,
                            artifact_schema_version=entry.artifact_schema_version,
                            size_bytes=entry.size_bytes,
                            sha256=entry.sha256,
                            exact_bytes=artifact.exact_bytes,
                        )
                    )
                current_path = (
                    self.runs_root / run_id / ".revision-store" / "current.json"
                )
                verify_regular_single_link(current_path)
                if sha256_bytes(current_path.read_bytes()) != chain.pointer_sha256:
                    if attempt < 2:
                        continue
                    raise CanonicalStorageError(
                        "current changed during structural artifact read"
                    )
                return StructuralArtifactHistoryV1(
                    run_id=run_id,
                    logical_name=logical_name,
                    current_revision=chain.pointer.revision,
                    current_pointer_sha256=chain.pointer_sha256,
                    revisions=tuple(revisions),
                )
            except FileNotFoundError as exc:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_INCOMPLETE,
                    stage="initial_read",
                ) from exc
            except RunStorageError:
                raise
            except CanonicalStorageError as exc:
                if (
                    str(exc) == "current changed during structural read"
                    and attempt < 2
                ):
                    continue
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_CORRUPT,
                    stage="initial_read",
                    reconciliation=True,
                ) from exc
            except (OSError, ValidationError) as exc:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_CORRUPT,
                    stage="initial_read",
                    reconciliation=True,
                ) from exc
        raise AssertionError("structural artifact read retry loop ended unexpectedly")

    def _existing_chain_or_none(
        self,
        run_id: str,
        *,
        verify_unreachable: bool = True,
        injector: FaultInjector | None = None,
        hook: str | None = None,
    ) -> _VerifiedChain | None:
        current = self.runs_root / run_id / ".revision-store" / "current.json"
        if injector is not None and hook is not None:
            injector(f"{hook}.before_exists")
        if not current.exists():
            if injector is not None and hook is not None:
                injector(f"{hook}.after_exists")
            return None
        if injector is not None and hook is not None:
            injector(f"{hook}.after_exists")
        return self._read_chain(
            run_id,
            verify_unreachable=verify_unreachable,
            injector=injector,
            hook=hook,
        )

    def _idempotency_outcome(
        self,
        prepared: _PreparedRevision,
        chain: _VerifiedChain | None,
    ) -> RevisionPublishOutcomeV1 | None:
        if chain is None:
            return None
        request = prepared.request
        for index, (reachable, _manifest, _manifest_sha, transaction) in enumerate(chain.entries):
            if transaction.transaction_id != request.transaction_id:
                continue
            if transaction.transaction_sha256 != prepared.transaction.transaction_sha256:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.IDEMPOTENCY_CONFLICT,
                    stage="initial_read",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=chain.pointer.revision,
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                )
            return RevisionPublishOutcomeV1(
                outcome_kind="current_committed" if index == 0 else "historical_committed",
                run_id_sha256=run_id_sha256(request.run_id),
                transaction_id=request.transaction_id,
                transaction_sha256=transaction.transaction_sha256,
                revision=reachable.revision,
                observed_current_revision=chain.pointer.revision,
                manifest_sha256=reachable.manifest_sha256,
                pointer_sha256=chain.pointer_sha256,
                filesystem_effect="none",
                domain_effect="current_unchanged",
                previous_revision_effect=(
                    "not_applicable" if reachable.revision == 1 else "unchanged"
                ),
                durability_evidence=_idle_durability(),
            )
        return None

    def _cas_matches(
        self,
        request: RevisionPublishRequestV1,
        chain: _VerifiedChain | None,
    ) -> bool:
        if chain is None:
            return (
                request.expected_revision is None
                and request.expected_manifest_sha256 is None
                and request.expected_pointer_sha256 is None
                and request.proposed_revision == 1
            )
        return (
            request.expected_revision == chain.pointer.revision
            and request.expected_manifest_sha256 == chain.entries[0][2]
            and request.expected_pointer_sha256 == chain.pointer_sha256
            and request.proposed_revision == chain.pointer.revision + 1
        )

    def _replay_orphan_effect(
        self,
        prepared: _PreparedRevision,
        chain: _VerifiedChain | None,
    ) -> Literal["staging_orphan", "unreferenced_revision"] | None:
        request = prepared.request
        control = self.runs_root / request.run_id / ".revision-store"
        staging = control / "transactions" / request.transaction_id
        revisions = control / "revisions"
        candidates = [staging] if staging.exists() else []
        if revisions.exists():
            candidates.extend(
                path
                for path in revisions.iterdir()
                if path.name.endswith(f"-{request.transaction_id}")
            )
        for candidate in candidates:
            candidate_effect: Literal[
                "staging_orphan",
                "unreferenced_revision",
            ] = (
                "staging_orphan"
                if candidate.parent.name == "transactions"
                else "unreferenced_revision"
            )
            descriptor_path = candidate / "transaction.json"
            if not descriptor_path.exists():
                return candidate_effect

            def replay_injector(
                hook: str,
                effect: Literal[
                    "staging_orphan",
                    "unreferenced_revision",
                ] = candidate_effect,
            ) -> None:
                if self.fault_injector is None:
                    return
                try:
                    self.fault_injector(hook)
                except Exception as exc:
                    raise _run_failure(
                        request.run_id,
                        RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
                        stage="initial_read",
                        transaction_id=request.transaction_id,
                        expected_revision=request.expected_revision,
                        observed_revision=(None if chain is None else chain.pointer.revision),
                        effect=effect,
                        previous_effect=(
                            "not_applicable" if request.proposed_revision == 1 else "unchanged"
                        ),
                        reconciliation=True,
                    ) from exc

            try:
                descriptor, _data, _digest = _read_control(
                    descriptor_path,
                    RevisionTransactionDescriptorV1,
                    max_bytes=self.max_artifact_bytes,
                    injector=replay_injector,
                    hook="replay_orphan.transaction",
                )
            except (CanonicalStorageError, OSError, ValidationError):
                return candidate_effect
            descriptor_projection = descriptor.model_dump(mode="python")
            recorded_descriptor_sha = cast(
                str,
                descriptor_projection.pop("transaction_sha256"),
            )
            if (
                transaction_sha256(descriptor_projection) != recorded_descriptor_sha
                or descriptor.run_id != request.run_id
                or descriptor.transaction_id != request.transaction_id
            ):
                return candidate_effect
            if descriptor.transaction_sha256 != prepared.transaction.transaction_sha256:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.IDEMPOTENCY_CONFLICT,
                    stage="initial_read",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect=candidate_effect,
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=True,
                )
            return candidate_effect
        return None

    def publish(self, request: RevisionPublishRequestV1) -> RevisionPublishOutcomeV1:
        """Publish one immutable structural revision under a serialized CAS."""

        prepared = self._preflight(request)
        _marker, marker_sha = self._ownership(request.run_id)
        owner_token = self.id_factory("owner")
        acquired_at = self.clock()
        if not _OWNER_TOKEN.fullmatch(owner_token):
            raise _run_failure(
                request.run_id,
                RunStorageFailureCode.INTERNAL_INVARIANT_ERROR,
                stage="preflight",
                transaction_id=request.transaction_id,
            )
        check_path_lengths(
            self._run_paths(
                request.run_id,
                owner_token,
                request.transaction_id,
                request.proposed_revision,
            )
        )
        lease = self._authority(request.run_id, marker_sha, bootstrap=True)
        outcome: RevisionPublishOutcomeV1 | None = None
        pending_failure: RunStorageError | None = None
        release_error: LockReleaseError | None = None
        try:
            self._scan_case_aliases(request.run_id)
            try:
                self._validate_existing_namespace(request.run_id)
            except (CanonicalStorageError, OSError) as exc:
                message = str(exc)
                code = (
                    RunStorageFailureCode.LINK_OR_REPARSE_DETECTED
                    if "linked" in message or "reparse" in message or "hardlink" in message
                    else RunStorageFailureCode.RUN_NAMESPACE_CONFLICT
                )
                raise _run_failure(
                    request.run_id,
                    code,
                    stage="locked_admission",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect="control_only" if lease.control_changed else "none",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                ) from exc
            try:
                chain = self._existing_chain_or_none(
                    request.run_id,
                    injector=self.fault_injector,
                    hook="initial_read",
                )
            except (CanonicalStorageError, OSError, ValidationError) as exc:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.RUN_CORRUPT,
                    stage="initial_read",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    reconciliation=True,
                ) from exc

            control = self.runs_root / request.run_id / ".revision-store"
            staging_path = control / "transactions" / request.transaction_id
            reachable_revision_names = (
                set()
                if chain is None
                else {Path(entry[0].revision_relative_path).name for entry in chain.entries}
            )
            revisions_path = control / "revisions"
            has_unreferenced_revision = revisions_path.exists() and any(
                path.name.endswith(f"-{request.transaction_id}")
                and path.name not in reachable_revision_names
                for path in revisions_path.iterdir()
            )
            if staging_path.exists():
                admission_effect = "staging_orphan"
            elif has_unreferenced_revision:
                admission_effect = "unreferenced_revision"
            else:
                admission_effect = "control_only" if lease.control_changed else "none"
            admission_requires_reconciliation = admission_effect in {
                "staging_orphan",
                "unreferenced_revision",
            }

            def locked_admission_fault(hook: str) -> None:
                if self.fault_injector is None:
                    return
                try:
                    self.fault_injector(hook)
                except Exception as exc:
                    raise _run_failure(
                        request.run_id,
                        RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
                        stage="locked_admission",
                        transaction_id=request.transaction_id,
                        expected_revision=request.expected_revision,
                        observed_revision=(None if chain is None else chain.pointer.revision),
                        effect=admission_effect,
                        previous_effect=(
                            "not_applicable" if request.proposed_revision == 1 else "unchanged"
                        ),
                        reconciliation=admission_requires_reconciliation,
                    ) from exc

            locked_admission_fault("locked_admission.before_byte_count")
            try:
                current_bytes = self._run_physical_bytes(request.run_id)
            except (CanonicalStorageError, OSError) as exc:
                message = str(exc)
                code = (
                    RunStorageFailureCode.LINK_OR_REPARSE_DETECTED
                    if "linked" in message or "reparse" in message or "hardlink" in message
                    else RunStorageFailureCode.RUN_NAMESPACE_CONFLICT
                )
                raise _run_failure(
                    request.run_id,
                    code,
                    stage="locked_admission",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=None if chain is None else chain.pointer.revision,
                    effect=admission_effect,
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=admission_requires_reconciliation,
                ) from exc
            locked_admission_fault("locked_admission.after_byte_count")
            if current_bytes > self.max_run_bytes:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.RUN_BUDGET_EXCEEDED,
                    stage="locked_admission",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=None if chain is None else chain.pointer.revision,
                    effect=admission_effect,
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=admission_requires_reconciliation,
                )
            idempotent = self._idempotency_outcome(prepared, chain)
            if idempotent is not None:
                outcome = idempotent
                return outcome
            replay_orphan_effect = self._replay_orphan_effect(prepared, chain)
            if replay_orphan_effect is not None:
                outcome = RevisionPublishOutcomeV1(
                    outcome_kind="reconciliation_required",
                    run_id_sha256=run_id_sha256(request.run_id),
                    transaction_id=request.transaction_id,
                    transaction_sha256=prepared.transaction.transaction_sha256,
                    revision=request.proposed_revision,
                    observed_current_revision=(None if chain is None else chain.pointer.revision),
                    manifest_sha256=None,
                    pointer_sha256=None if chain is None else chain.pointer_sha256,
                    filesystem_effect=replay_orphan_effect,
                    domain_effect="current_unchanged",
                    previous_revision_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    durability_evidence=_reconciliation_durability(),
                )
                return outcome
            if not self._cas_matches(request, chain):
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.RUN_CONFLICT,
                    stage="initial_read",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=None if chain is None else chain.pointer.revision,
                    effect="control_only" if lease.control_changed else "none",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                )
            metadata, metadata_bytes = self._metadata_value(
                request,
                marker_sha,
                lease,
                owner_token,
                acquired_at,
            )
            if len(metadata_bytes) > self.max_artifact_bytes:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.ARTIFACT_BUDGET_EXCEEDED,
                    stage="locked_admission",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=None if chain is None else chain.pointer.revision,
                    effect="control_only" if lease.control_changed else "none",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                )
            revision_bytes = (
                len(prepared.transaction_bytes)
                + len(prepared.manifest_bytes)
                + sum(entry.size_bytes for entry in prepared.inventories)
            )
            metadata_final = self.locks_root / (
                f"{run_lock_key_sha256(request.run_id)}.metadata.json"
            )
            previous_metadata_bytes = (
                metadata_final.stat().st_size if metadata_final.exists() else 0
            )
            projected_after_metadata = current_bytes - previous_metadata_bytes + len(metadata_bytes)
            peak_bytes = max(
                current_bytes + len(metadata_bytes),
                projected_after_metadata + revision_bytes,
                projected_after_metadata + revision_bytes + len(prepared.pointer_bytes),
            )
            locked_admission_fault("locked_admission.before_peak_check")
            if peak_bytes > self.max_run_bytes:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.RUN_BUDGET_EXCEEDED,
                    stage="locked_admission",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=None if chain is None else chain.pointer.revision,
                    effect="control_only" if lease.control_changed else "none",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                )
            locked_admission_fault("locked_admission.after_peak_check")
            try:
                self._metadata(request, metadata, metadata_bytes, owner_token)
            except RunStorageError:
                raise
            except _StorageBoundaryError as exc:
                code = {
                    "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
                    "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
                    "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
                }[exc.kind]
                raise _run_failure(
                    request.run_id,
                    code,
                    stage="lock_metadata",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect="control_only",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=False,
                    durability=(
                        _reconciliation_durability(file_sync="failed")
                        if exc.kind == "durability"
                        else None
                    ),
                ) from exc
            except _PublishBoundaryError as exc:
                raise _run_failure(
                    request.run_id,
                    (
                        RunStorageFailureCode.EFFECT_UNKNOWN
                        if exc.invoked
                        else RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED
                    ),
                    stage="lock_metadata",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect="control_only",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=exc.invoked,
                ) from exc
            except Exception as exc:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED,
                    stage="lock_metadata",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect="control_only",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=True,
                ) from exc
            try:
                control = self._bootstrap_namespace(request.run_id)
            except _StorageBoundaryError as exc:
                code = {
                    "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
                    "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
                    "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
                }[exc.kind]
                raise _run_failure(
                    request.run_id,
                    code,
                    stage="namespace_bootstrap",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect="control_only",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=exc.kind == "verification",
                    durability=(
                        _reconciliation_durability(file_sync="failed")
                        if exc.kind == "durability"
                        else None
                    ),
                ) from exc
            except Exception as exc:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
                    stage="namespace_bootstrap",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect="control_only",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=True,
                ) from exc
            staging = control / "transactions" / request.transaction_id
            revision_dir = (
                control / "revisions" / f"r{request.proposed_revision}-{request.transaction_id}"
            )
            pointer_temp = control / f"current.{request.transaction_id}.tmp"
            write_stage = "staging"
            staging_created = False
            rename_invoked = False
            revision_published = False
            try:
                if self.fault_injector is not None:
                    self.fault_injector("staging.before_mkdir")
                staging.mkdir()
                staging_created = True
                if self.fault_injector is not None:
                    self.fault_injector("staging.after_mkdir")
                _directory_sync(
                    staging.parent,
                    injector=self.fault_injector,
                    hook="staging.parent_sync",
                )
                payload_root = staging / "payload"
                _mkdir_verified(
                    payload_root,
                    injector=self.fault_injector,
                    hook="staging.payload_root",
                )
                _directory_sync(
                    staging,
                    injector=self.fault_injector,
                    hook="staging.directory_sync",
                )
                write_stage = "transaction"
                _write_exclusive_verified(
                    staging / "transaction.json",
                    prepared.transaction_bytes,
                    max_bytes=self.max_artifact_bytes,
                    injector=self.fault_injector,
                    hook="transaction",
                )
                write_stage = "payload"
                for entry in prepared.inventories:
                    artifact = prepared.artifacts[entry.logical_name]
                    payload_path = staging / entry.revision_relative_path
                    if payload_path.parent != payload_root:
                        if payload_path.parent.exists():
                            verify_directory(payload_path.parent)
                        else:
                            _mkdir_verified(
                                payload_path.parent,
                                injector=self.fault_injector,
                                hook=f"payload_parent.{payload_path.parent.name}",
                            )
                    _directory_sync(
                        payload_path.parent,
                        injector=self.fault_injector,
                        hook=f"payload_directory.{entry.logical_name}",
                    )
                    _write_exclusive_verified(
                        payload_path,
                        artifact.exact_bytes,
                        max_bytes=self.max_artifact_bytes,
                        injector=self.fault_injector,
                        hook=f"payload.{entry.logical_name}",
                    )
                write_stage = "manifest"
                _write_exclusive_verified(
                    staging / "manifest.json",
                    prepared.manifest_bytes,
                    max_bytes=self.max_artifact_bytes,
                    injector=self.fault_injector,
                    hook="manifest",
                )
                self._verify_revision_directory(
                    staging,
                    run_id=request.run_id,
                    injector=self.fault_injector,
                )
                for directory in sorted(
                    (path for path in staging.rglob("*") if path.is_dir()),
                    key=lambda path: len(path.parts),
                    reverse=True,
                ):
                    _directory_sync(
                        directory,
                        injector=self.fault_injector,
                        hook=f"revision_pre_publish.{directory.relative_to(staging).as_posix()}",
                    )
                _directory_sync(
                    staging,
                    injector=self.fault_injector,
                    hook="revision_pre_publish.root",
                )
                write_stage = "revision_publish"
                if self.fault_injector is not None:
                    self.fault_injector("revision.before_rename")
                rename_invoked = True
                staging.rename(revision_dir)
                revision_published = True
                if self.fault_injector is not None:
                    self.fault_injector("revision.after_rename")
                _directory_sync(
                    staging.parent,
                    injector=self.fault_injector,
                    hook="revision.transactions_parent_sync",
                )
                _directory_sync(
                    revision_dir.parent,
                    injector=self.fault_injector,
                    hook="revision.revisions_parent_sync",
                )
                verified_revision = self._verify_revision_directory(
                    revision_dir,
                    run_id=request.run_id,
                    injector=self.fault_injector,
                )
                if verified_revision[2] != prepared.manifest_sha256:
                    raise CanonicalStorageError("published revision manifest hash mismatch")
            except Exception as exc:
                if isinstance(exc, _ArtifactBoundaryError):
                    code = exc.code
                    effect = "unreferenced_revision" if revision_published else "staging_orphan"
                    reconciliation = True
                elif isinstance(exc, _StorageBoundaryError) and exc.kind == "durability":
                    code = RunStorageFailureCode.DURABILITY_UNCONFIRMED
                    effect = "unreferenced_revision" if revision_published else "staging_orphan"
                    reconciliation = True
                elif not staging_created:
                    code = RunStorageFailureCode.TRANSACTION_WRITE_FAILED
                    effect = "control_only"
                    reconciliation = False
                elif write_stage != "revision_publish":
                    code = (
                        RunStorageFailureCode.DURABILITY_UNCONFIRMED
                        if isinstance(exc, _StorageBoundaryError) and exc.kind == "durability"
                        else (
                            RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
                            if isinstance(exc, _StorageBoundaryError) and exc.kind == "verification"
                            else RunStorageFailureCode.TRANSACTION_WRITE_FAILED
                        )
                    )
                    effect = "staging_orphan"
                    reconciliation = True
                elif revision_published:
                    code = RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
                    effect = "unreferenced_revision"
                    reconciliation = True
                elif rename_invoked:
                    code = RunStorageFailureCode.EFFECT_UNKNOWN
                    effect = "unreferenced_revision" if revision_dir.exists() else "staging_orphan"
                    reconciliation = True
                else:
                    code = RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED
                    effect = "staging_orphan"
                    reconciliation = True
                raise _run_failure(
                    request.run_id,
                    code,
                    stage="payload" if isinstance(exc, _ArtifactBoundaryError) else write_stage,
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect=effect,
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=reconciliation,
                    durability=(
                        _reconciliation_durability(file_sync="failed")
                        if isinstance(exc, _StorageBoundaryError) and exc.kind == "durability"
                        else None
                    ),
                ) from exc
            try:
                _write_exclusive_verified(
                    pointer_temp,
                    prepared.pointer_bytes,
                    max_bytes=self.max_artifact_bytes,
                    injector=self.fault_injector,
                    hook="pointer",
                )
            except _StorageBoundaryError as exc:
                code = {
                    "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
                    "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
                    "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
                }[exc.kind]
                raise _run_failure(
                    request.run_id,
                    code,
                    stage="pointer",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    effect="unreferenced_revision",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=True,
                    durability=(
                        _reconciliation_durability(file_sync="failed")
                        if exc.kind == "durability"
                        else None
                    ),
                ) from exc
            try:
                replace_invoked = False
                final_cas_verified = False
                reconciliation_started = False
                strict_current_pointer_verified = False
                final_chain = self._existing_chain_or_none(
                    request.run_id,
                    injector=self.fault_injector,
                    hook="final_cas",
                )
                if not self._cas_matches(request, final_chain):
                    raise _run_failure(
                        request.run_id,
                        RunStorageFailureCode.RUN_CONFLICT,
                        stage="final_cas",
                        transaction_id=request.transaction_id,
                        expected_revision=request.expected_revision,
                        observed_revision=(
                            None if final_chain is None else final_chain.pointer.revision
                        ),
                        effect="unreferenced_revision",
                        previous_effect=(
                            "not_applicable" if request.proposed_revision == 1 else "unchanged"
                        ),
                        reconciliation=True,
                    )
                final_cas_verified = True
                self._ownership(request.run_id)
                if self.fault_injector is not None:
                    self.fault_injector("current.before_replace")
                replace_invoked = True
                os.replace(pointer_temp, control / "current.json")
                if self.fault_injector is not None:
                    self.fault_injector("current.after_replace")
                reconciliation_started = True

                confirmed_pointer, confirmed_pointer_bytes, confirmed_pointer_sha = _read_control(
                    control / "current.json",
                    StorageRevisionPointerV1,
                    max_bytes=self.max_artifact_bytes,
                    injector=self.fault_injector,
                    hook="reconciliation.current",
                )
                if (
                    confirmed_pointer != prepared.pointer
                    or confirmed_pointer_bytes != prepared.pointer_bytes
                    or confirmed_pointer_sha != prepared.pointer_sha256
                ):
                    raise CanonicalStorageError("reconciliation current pointer mismatch")
                strict_current_pointer_verified = True

                published_chain = self._read_chain(
                    request.run_id,
                    injector=self.fault_injector,
                    hook="reconciliation.lineage",
                )
                if (
                    published_chain.pointer != prepared.pointer
                    or published_chain.entries[0][2] != prepared.manifest_sha256
                ):
                    raise CanonicalStorageError("current reconciliation mismatch")
            except RunStorageError:
                raise
            except Exception as exc:
                if not replace_invoked:
                    raise _run_failure(
                        request.run_id,
                        (
                            RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED
                            if final_cas_verified
                            else RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
                        ),
                        stage="current_replace" if final_cas_verified else "final_cas",
                        transaction_id=request.transaction_id,
                        expected_revision=request.expected_revision,
                        effect="unreferenced_revision",
                        domain_effect="current_unchanged",
                        previous_effect=(
                            "not_applicable" if request.proposed_revision == 1 else "unchanged"
                        ),
                        reconciliation=True,
                        durability=_reconciliation_durability(),
                    ) from exc
                if reconciliation_started and not strict_current_pointer_verified:
                    raise _run_failure(
                        request.run_id,
                        RunStorageFailureCode.EFFECT_UNKNOWN,
                        stage="reconciliation",
                        transaction_id=request.transaction_id,
                        expected_revision=request.expected_revision,
                        observed_revision=None,
                        effect="current_replace_attempted",
                        domain_effect="current_may_have_advanced",
                        previous_effect=(
                            "not_applicable" if request.proposed_revision == 1 else "unconfirmed"
                        ),
                        reconciliation=True,
                        durability=_reconciliation_durability(
                            pointer_replace="attempted_unconfirmed"
                        ),
                    ) from exc
                if reconciliation_started:
                    raise _run_failure(
                        request.run_id,
                        RunStorageFailureCode.RUN_CORRUPT,
                        stage="reconciliation",
                        transaction_id=request.transaction_id,
                        expected_revision=request.expected_revision,
                        observed_revision=request.proposed_revision,
                        effect="current_advanced",
                        domain_effect="current_advanced",
                        previous_effect=(
                            "not_applicable" if request.proposed_revision == 1 else "unconfirmed"
                        ),
                        reconciliation=True,
                        durability=_reconciliation_durability(pointer_replace="confirmed"),
                    ) from exc
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.EFFECT_UNKNOWN,
                    stage="current_replace",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=None,
                    effect="current_replace_attempted",
                    domain_effect="current_may_have_advanced",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unconfirmed"
                    ),
                    reconciliation=True,
                    durability=_reconciliation_durability(pointer_replace="attempted_unconfirmed"),
                ) from exc
            try:
                directory_state = _directory_sync(
                    control,
                    injector=self.fault_injector,
                    hook="current.directory_sync",
                )
            except Exception as exc:
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.DURABILITY_UNCONFIRMED,
                    stage="directory_sync",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=request.proposed_revision,
                    effect="current_advanced",
                    domain_effect="current_advanced",
                    previous_effect=(
                        "not_applicable" if request.proposed_revision == 1 else "unchanged"
                    ),
                    reconciliation=True,
                    durability=DurabilityEvidenceV1(
                        platform_adapter=cast(Any, platform_adapter()),
                        file_sync="confirmed",
                        directory_sync="failed",
                        pointer_replace="confirmed",
                        reconciliation="required",
                    ),
                ) from exc
            outcome = RevisionPublishOutcomeV1(
                outcome_kind="published",
                run_id_sha256=run_id_sha256(request.run_id),
                transaction_id=request.transaction_id,
                transaction_sha256=prepared.transaction.transaction_sha256,
                revision=request.proposed_revision,
                observed_current_revision=request.proposed_revision,
                manifest_sha256=prepared.manifest_sha256,
                pointer_sha256=prepared.pointer_sha256,
                filesystem_effect="current_advanced",
                domain_effect="current_advanced",
                previous_revision_effect=(
                    "not_applicable" if request.proposed_revision == 1 else "unchanged"
                ),
                durability_evidence=_published_durability(directory_state),
            )
            return outcome
        except RunStorageError as exc:
            pending_failure = exc
            raise
        finally:
            try:
                lease.release()
            except LockReleaseError as exc:
                release_error = exc
            if release_error is not None:
                if pending_failure is not None:
                    prior = pending_failure.failure
                    effect = prior.filesystem_effect
                    domain_effect = prior.domain_effect
                    observed_revision = prior.observed_revision
                    previous_effect = prior.previous_revision_effect
                    durability = prior.durability_evidence or _reconciliation_durability()
                elif outcome is not None:
                    effect = outcome.filesystem_effect
                    domain_effect = outcome.domain_effect
                    observed_revision = outcome.observed_current_revision
                    previous_effect = outcome.previous_revision_effect
                    durability = outcome.durability_evidence
                else:
                    effect = "control_only" if lease.control_changed else "none"
                    domain_effect = "current_unchanged"
                    observed_revision = None
                    previous_effect = (
                        "not_applicable" if request.proposed_revision == 1 else "unconfirmed"
                    )
                    durability = _reconciliation_durability()
                raise _run_failure(
                    request.run_id,
                    RunStorageFailureCode.EFFECT_UNKNOWN,
                    stage="lock_release",
                    transaction_id=request.transaction_id,
                    expected_revision=request.expected_revision,
                    observed_revision=observed_revision,
                    effect=effect,
                    domain_effect=domain_effect,
                    previous_effect=previous_effect,
                    reconciliation=True,
                    durability=durability,
                ) from release_error

    def _inspect_orphans_locked(self, run_id: str) -> OrphanInspectionV1:
        def inspect_fault(hook: str) -> None:
            if self.fault_injector is None:
                return
            try:
                self.fault_injector(hook)
            except Exception as exc:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_CORRUPT,
                    stage="orphan_inspect",
                    reconciliation=True,
                ) from exc

        inspect_fault("orphan_inspect.before_current_and_lineage")
        chain = self._existing_chain_or_none(run_id, verify_unreachable=False)
        inspect_fault("orphan_inspect.after_current_and_lineage")
        reachable = () if chain is None else tuple(entry[0] for entry in chain.entries)
        reachable_names = {Path(entry.revision_relative_path).name for entry in reachable}
        control = self.runs_root / run_id / ".revision-store"
        staging_entries: list[OrphanEntryV1] = []
        revision_entries: list[OrphanEntryV1] = []
        transactions = control / "transactions"
        if transactions.exists():
            for path in sorted(transactions.iterdir(), key=lambda item: item.name.encode()):
                inspect_fault(f"orphan_inspect.before_staging.{path.name}")
                match = _TRANSACTION_DIR.fullmatch(path.name)
                if match is None:
                    raise CanonicalStorageError("unknown staging entry")
                verification = "path_only"
                digest: str | None = None
                revision: int | None = None
                try:

                    def staging_injector(hook: str) -> None:
                        inspect_fault(hook)

                    transaction, _data, _file_sha = _read_control(
                        path / "transaction.json",
                        RevisionTransactionDescriptorV1,
                        max_bytes=self.max_artifact_bytes,
                        injector=staging_injector,
                        hook=f"orphan_inspect.transaction.{path.name}",
                    )
                    transaction_projection = transaction.model_dump(mode="python")
                    recorded_transaction_sha = cast(
                        str,
                        transaction_projection.pop("transaction_sha256"),
                    )
                    if (
                        transaction_sha256(transaction_projection) != recorded_transaction_sha
                        or transaction.run_id != run_id
                        or transaction.transaction_id != path.name
                    ):
                        raise CanonicalStorageError("staging orphan transaction identity mismatch")
                    digest = transaction.transaction_sha256
                    revision = transaction.proposed_revision
                    verification = "descriptor_verified"
                except (CanonicalStorageError, OSError, ValidationError):
                    pass
                staging_entries.append(
                    OrphanEntryV1(
                        orphan_form="staging",
                        verification_state=cast(Any, verification),
                        transaction_id=path.name,
                        transaction_sha256=digest,
                        revision=revision,
                        relative_path=f"transactions/{path.name}",
                    )
                )
                inspect_fault(f"orphan_inspect.after_staging.{path.name}")
        revisions = control / "revisions"
        if revisions.exists():
            for path in sorted(revisions.iterdir(), key=lambda item: item.name.encode()):
                if path.name in reachable_names:
                    continue
                inspect_fault(f"orphan_inspect.before_revision.{path.name}")
                match = _REVISION_DIR.fullmatch(path.name)
                if match is None:
                    raise CanonicalStorageError("unknown revision entry")
                verification = "path_only"
                digest = None
                target_faulted = False
                target_prefix = f"orphan_inspect.revision.{path.name}"

                def target_injector(hook: str, prefix: str = target_prefix) -> None:
                    nonlocal target_faulted
                    try:
                        inspect_fault(f"{prefix}.{hook}")
                    except RunStorageError:
                        target_faulted = True
                        raise

                try:
                    verified = self._verify_revision_directory(
                        path,
                        run_id=run_id,
                        injector=target_injector,
                    )
                    digest = verified[3].transaction_sha256
                    verification = "manifest_verified"
                except (
                    CanonicalStorageError,
                    OSError,
                    ValidationError,
                    _StorageBoundaryError,
                ):
                    if target_faulted:
                        raise
                    pass
                revision_entries.append(
                    OrphanEntryV1(
                        orphan_form="unreferenced_revision",
                        verification_state=cast(Any, verification),
                        transaction_id=match.group("transaction"),
                        transaction_sha256=digest,
                        revision=int(match.group("revision")),
                        relative_path=f"revisions/{path.name}",
                    )
                )
                inspect_fault(f"orphan_inspect.after_revision.{path.name}")
        return OrphanInspectionV1(
            run_id_sha256=run_id_sha256(run_id),
            current_pointer_sha256=None if chain is None else chain.pointer_sha256,
            reachable_history=reachable,
            staging_orphans=tuple(staging_entries),
            revision_orphans=tuple(revision_entries),
        )

    def inspect_orphans(self, run_id: str) -> OrphanInspectionV1:
        """Inspect, but never repair, staging and unreachable revisions."""

        _marker, marker_sha = self._ownership(run_id)
        lease = self._authority(run_id, marker_sha, bootstrap=False)
        try:
            self._validate_existing_namespace(
                run_id,
                verify_immutable_revisions=False,
            )
            return self._inspect_orphans_locked(run_id)
        except RunStorageError:
            raise
        except Exception as exc:
            raise _run_failure(
                run_id,
                RunStorageFailureCode.RUN_CORRUPT,
                stage="orphan_inspect",
                reconciliation=True,
            ) from exc
        finally:
            try:
                lease.release()
            except LockReleaseError as exc:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.EFFECT_UNKNOWN,
                    stage="lock_release",
                    reconciliation=True,
                ) from exc

    def claim_orphan(
        self,
        run_id: str,
        request: RecoveryClaimRequestV1,
    ) -> RecoveryClaimV1:
        """Publish metadata-only evidence for one verified unreachable transaction."""

        try:
            validate_run_id(run_id)
            if request.run_id_sha256 != run_id_sha256(run_id):
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.CROSS_RUN_MISMATCH,
                    stage="preflight",
                    transaction_id=request.transaction_id,
                )
            projection = request.model_dump(mode="python")
            claim = RecoveryClaimV1(
                **projection,
                claim_sha256=recovery_claim_sha256(projection),
            )
            data = canonical_json_bytes(claim)
            key = run_lock_key_sha256(run_id)
            control = self.runs_root / run_id / ".revision-store" / "recovery-claims"
            final = control / f"{request.transaction_id}.json"
            temp = control / ".tmp" / f"{request.transaction_id}.{request.claim_id}.json"
            check_path_lengths(
                (
                    self.locks_root / f"{key}.authority.lock",
                    control,
                    control / ".tmp",
                    final,
                    temp,
                )
            )
            if len(data) > self.max_artifact_bytes:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.ARTIFACT_BUDGET_EXCEEDED,
                    stage="preflight",
                    transaction_id=request.transaction_id,
                )
        except RunStorageError:
            raise
        except (CanonicalStorageError, OSError, ValidationError) as exc:
            raise _run_failure(
                run_id,
                RunStorageFailureCode.PATH_CONFINEMENT_FAILED,
                stage="preflight",
                transaction_id=request.transaction_id,
            ) from exc
        _marker, marker_sha = self._ownership(run_id)
        lease = self._authority(run_id, marker_sha, bootstrap=False)
        try:

            def claim_admission_fault(hook: str) -> None:
                if self.fault_injector is None:
                    return
                try:
                    self.fault_injector(hook)
                except Exception as exc:
                    raise _run_failure(
                        run_id,
                        RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
                        stage="recovery_claim",
                        transaction_id=request.transaction_id,
                        effect="control_only",
                        reconciliation=temp.exists() or final.exists(),
                    ) from exc

            self._validate_existing_namespace(
                run_id,
                verify_immutable_revisions=False,
            )
            claim_admission_fault("recovery_claim.admission.before_byte_count")
            try:
                physical_bytes = self._run_physical_bytes(run_id)
            except (CanonicalStorageError, OSError) as exc:
                message = str(exc)
                code = (
                    RunStorageFailureCode.LINK_OR_REPARSE_DETECTED
                    if "linked" in message or "reparse" in message or "hardlink" in message
                    else RunStorageFailureCode.RUN_NAMESPACE_CONFLICT
                )
                raise _run_failure(
                    run_id,
                    code,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                    effect="control_only",
                    reconciliation=False,
                ) from exc
            claim_admission_fault("recovery_claim.admission.after_byte_count")
            if physical_bytes > self.max_run_bytes:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_BUDGET_EXCEEDED,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                )
            if final.exists():
                try:
                    existing, existing_bytes, _digest = _read_recovery_claim(
                        final,
                        max_bytes=self.max_artifact_bytes,
                        injector=self.fault_injector,
                        hook="recovery_claim.existing_final",
                    )
                except (CanonicalStorageError, OSError, ValidationError) as exc:
                    raise _run_failure(
                        run_id,
                        RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE,
                        stage="recovery_claim",
                        transaction_id=request.transaction_id,
                        effect="control_only",
                        reconciliation=True,
                    ) from exc
                if existing != claim or existing_bytes != data:
                    raise _run_failure(
                        run_id,
                        RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT,
                        stage="recovery_claim",
                        transaction_id=request.transaction_id,
                    )
                return existing
            inspection = self._inspect_orphans_locked(run_id)
            if inspection.current_pointer_sha256 != request.observed_pointer_sha256:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                    effect="control_only" if temp.exists() else "none",
                    reconciliation=temp.exists(),
                )
            candidates = {
                (entry.orphan_form, entry.transaction_id): entry
                for entry in (*inspection.staging_orphans, *inspection.revision_orphans)
            }
            target = candidates.get((request.orphan_form, request.transaction_id))
            if target is None:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                    effect="control_only" if temp.exists() else "none",
                    reconciliation=temp.exists(),
                )
            if (
                target.verification_state == "path_only"
                or target.transaction_sha256 is None
                or target.transaction_sha256 != request.transaction_sha256
            ):
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_INCOMPLETE,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                    effect="control_only" if temp.exists() else "none",
                    reconciliation=temp.exists(),
                )
            temp_already_verified = False
            if temp.exists():
                try:
                    existing_temp, existing_temp_bytes, _digest = _read_recovery_claim(
                        temp,
                        max_bytes=self.max_artifact_bytes,
                        injector=self.fault_injector,
                        hook="recovery_claim.existing_temp",
                    )
                except (CanonicalStorageError, OSError, ValidationError) as exc:
                    raise _run_failure(
                        run_id,
                        RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE,
                        stage="recovery_claim",
                        transaction_id=request.transaction_id,
                        effect="control_only",
                        reconciliation=True,
                    ) from exc
                if existing_temp != claim or existing_temp_bytes != data:
                    raise _run_failure(
                        run_id,
                        RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT,
                        stage="recovery_claim",
                        transaction_id=request.transaction_id,
                        effect="control_only",
                        reconciliation=True,
                    )
                temp_already_verified = True
            peak_additional_bytes = 0 if temp_already_verified else len(data)
            claim_admission_fault("recovery_claim.admission.before_peak_check")
            if physical_bytes + peak_additional_bytes > self.max_run_bytes:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RUN_BUDGET_EXCEEDED,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                )
            claim_admission_fault("recovery_claim.admission.after_peak_check")
            if not temp_already_verified:
                _write_exclusive_verified(
                    temp,
                    data,
                    max_bytes=self.max_artifact_bytes,
                    injector=self.fault_injector,
                    hook="recovery_claim",
                )
                _directory_sync(
                    temp.parent,
                    injector=self.fault_injector,
                    hook="recovery_claim.temp_parent_sync",
                )
            # Recheck reachability while the same authority is held.
            chain = self._existing_chain_or_none(run_id)
            if chain is not None and any(
                entry[0].transaction_id == request.transaction_id for entry in chain.entries
            ):
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                )
            if self.fault_injector is not None:
                self.fault_injector("recovery_claim.before_final_recheck")
            if final.exists():
                try:
                    existing, existing_bytes, _digest = _read_recovery_claim(
                        final,
                        max_bytes=self.max_artifact_bytes,
                        injector=self.fault_injector,
                        hook="recovery_claim.final_recheck",
                    )
                except (CanonicalStorageError, OSError, ValidationError) as exc:
                    raise _run_failure(
                        run_id,
                        RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE,
                        stage="recovery_claim",
                        transaction_id=request.transaction_id,
                        effect="control_only",
                        reconciliation=True,
                    ) from exc
                if existing != claim or existing_bytes != data:
                    raise _run_failure(
                        run_id,
                        RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT,
                        stage="recovery_claim",
                        transaction_id=request.transaction_id,
                        effect="control_only",
                    ) from None
                return existing
            if self.fault_injector is not None:
                self.fault_injector("recovery_claim.after_final_recheck")
            finalize_invoked = False
            try:
                if self.fault_injector is not None:
                    self.fault_injector("recovery_claim.before_finalize")
                finalize_invoked = True
                os.replace(temp, final)
                if self.fault_injector is not None:
                    self.fault_injector("recovery_claim.after_finalize")
                _directory_sync(
                    control,
                    injector=self.fault_injector,
                    hook="recovery_claim.final_parent_sync",
                )
                _directory_sync(
                    temp.parent,
                    injector=self.fault_injector,
                    hook="recovery_claim.temp_parent_after_finalize_sync",
                )
            except Exception as exc:
                if isinstance(exc, _StorageBoundaryError):
                    boundary_code = {
                        "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
                        "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
                        "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
                    }[exc.kind]
                else:
                    boundary_code = (
                        RunStorageFailureCode.EFFECT_UNKNOWN
                        if finalize_invoked
                        else RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED
                    )
                raise _run_failure(
                    run_id,
                    boundary_code,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                    effect="control_only",
                    reconciliation=True,
                ) from exc
            try:
                if self.fault_injector is not None:
                    self.fault_injector("recovery_claim.before_final_reread")
                verified, verified_bytes, _digest = _read_recovery_claim(
                    final,
                    max_bytes=self.max_artifact_bytes,
                    injector=self.fault_injector,
                    hook="recovery_claim.final",
                )
                if verified != claim or verified_bytes != data:
                    raise CanonicalStorageError("recovery claim final verification failed")
                if self.fault_injector is not None:
                    self.fault_injector("recovery_claim.after_final_reread")
                    self.fault_injector("recovery_claim.before_post_reconcile")
                post_chain = self._existing_chain_or_none(run_id)
                if post_chain is not None and any(
                    entry[0].transaction_id == request.transaction_id
                    for entry in post_chain.entries
                ):
                    raise CanonicalStorageError("claimed transaction became reachable")
                if self.fault_injector is not None:
                    self.fault_injector("recovery_claim.after_post_reconcile")
            except (CanonicalStorageError, OSError, ValidationError) as exc:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE,
                    stage="recovery_claim",
                    transaction_id=request.transaction_id,
                    effect="control_only",
                    reconciliation=True,
                ) from exc
            return verified
        except RunStorageError:
            raise
        except _StorageBoundaryError as exc:
            code = {
                "write": RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
                "durability": RunStorageFailureCode.DURABILITY_UNCONFIRMED,
                "verification": RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
            }[exc.kind]
            raise _run_failure(
                run_id,
                code,
                stage="recovery_claim",
                transaction_id=request.transaction_id,
                effect="control_only",
                reconciliation=temp.exists(),
                durability=(
                    _reconciliation_durability(file_sync="failed")
                    if exc.kind == "durability"
                    else None
                ),
            ) from exc
        except Exception as exc:
            raise _run_failure(
                run_id,
                RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE,
                stage="recovery_claim",
                transaction_id=request.transaction_id,
                effect="control_only",
                reconciliation=True,
            ) from exc
        finally:
            try:
                lease.release()
            except LockReleaseError as exc:
                raise _run_failure(
                    run_id,
                    RunStorageFailureCode.EFFECT_UNKNOWN,
                    stage="lock_release",
                    transaction_id=request.transaction_id,
                    effect="control_only",
                    reconciliation=True,
                ) from exc


__all__ = [
    "DEFAULT_MAX_ARTIFACT_BYTES",
    "DEFAULT_MAX_RUN_BYTES",
    "RunRevisionStore",
    "initialize_revision_root",
    "inspect_root_initialization",
    "reconcile_revision_root",
]
