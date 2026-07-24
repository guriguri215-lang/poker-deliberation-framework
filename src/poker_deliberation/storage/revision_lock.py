"""Stable nonblocking process/kernel authority locks for P2-012A."""

from __future__ import annotations

import errno
import os
import stat
import threading
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import BinaryIO, Final, Literal

from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    authority_identity_sha256,
    check_path_lengths,
    platform_adapter,
)

AUTHORITY_BYTE: Final = b"\0"
FaultInjector = Callable[[str], None]

_REGISTRY_MUTEX = threading.Lock()
_HELD_KEYS: set[str] = set()


class LockBusyError(RuntimeError):
    def __init__(self, *, control_changed: bool):
        self.control_changed = control_changed
        super().__init__("run_locked")


class LockUnavailableError(RuntimeError):
    def __init__(
        self,
        *,
        control_changed: bool,
        boundary_kind: Literal[
            "unavailable", "write", "durability", "verification"
        ] = "unavailable",
    ):
        self.control_changed = control_changed
        self.boundary_kind = boundary_kind
        super().__init__("lock_unavailable")


class LockReleaseError(RuntimeError):
    """Kernel release was not confirmed; registry keys remain held."""


def _fault(injector: FaultInjector | None, hook: str) -> None:
    if injector is not None:
        injector(hook)


def _reserve(keys: tuple[str, ...]) -> None:
    with _REGISTRY_MUTEX:
        if any(key in _HELD_KEYS for key in keys):
            raise LockBusyError(control_changed=False)
        _HELD_KEYS.update(keys)


def _release_registry(keys: tuple[str, ...]) -> None:
    with _REGISTRY_MUTEX:
        for key in keys:
            _HELD_KEYS.discard(key)


def _extend_reservation(keys: tuple[str, ...], additions: tuple[str, ...]) -> tuple[str, ...]:
    combined = tuple(dict.fromkeys((*keys, *additions)))
    new_keys = tuple(key for key in combined if key not in keys)
    with _REGISTRY_MUTEX:
        if any(key in _HELD_KEYS for key in new_keys):
            raise LockBusyError(control_changed=False)
        _HELD_KEYS.update(new_keys)
    return combined


def registry_contains(key: str) -> bool:
    with _REGISTRY_MUTEX:
        return key in _HELD_KEYS


def _reparse_or_link(path: Path, *, regular_file: bool) -> bool:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if attributes & reparse_flag:
        return True
    if regular_file and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        return True
    return not regular_file and not stat.S_ISDIR(info.st_mode)


def verify_directory(path: Path) -> None:
    if not path.exists() or _reparse_or_link(path, regular_file=False):
        raise CanonicalStorageError("directory identity is linked, reparsed, or invalid")


def verify_regular_single_link(path: Path) -> os.stat_result:
    if not path.exists() or _reparse_or_link(path, regular_file=True):
        raise CanonicalStorageError("authority is linked, reparsed, hardlinked, or nonregular")
    return path.stat()


def _open_authority(
    path: Path,
    *,
    bootstrap: bool,
    injector: FaultInjector | None,
) -> tuple[BinaryIO, bool]:
    check_path_lengths((path,))
    control_changed = False
    try:
        _fault(injector, "authority.before_open")
    except Exception as exc:
        raise LockUnavailableError(
            control_changed=False,
            boundary_kind="write",
        ) from exc
    try:
        stream = path.open("r+b", buffering=0)
    except FileNotFoundError:
        if not bootstrap:
            raise LockUnavailableError(control_changed=False) from None
        try:
            _fault(injector, "authority.before_create")
        except Exception as exc:
            raise LockUnavailableError(
                control_changed=False,
                boundary_kind="write",
            ) from exc
        try:
            stream = path.open("x+b", buffering=0)
        except FileExistsError:
            stream = path.open("r+b", buffering=0)
        else:
            control_changed = True
            try:
                _fault(injector, "authority.before_write")
                stream.write(AUTHORITY_BYTE)
                _fault(injector, "authority.after_write")
            except Exception as exc:
                stream.close()
                raise LockUnavailableError(
                    control_changed=True,
                    boundary_kind="write",
                ) from exc
            try:
                _fault(injector, "authority.before_flush")
                stream.flush()
                _fault(injector, "authority.after_flush")
                _fault(injector, "authority.before_fsync")
                os.fsync(stream.fileno())
                _fault(injector, "authority.after_fsync")
            except Exception as exc:
                stream.close()
                raise LockUnavailableError(
                    control_changed=True,
                    boundary_kind="durability",
                ) from exc
        try:
            _fault(injector, "authority.after_create")
        except Exception as exc:
            stream.close()
            raise LockUnavailableError(
                control_changed=control_changed,
                boundary_kind="verification",
            ) from exc
    except OSError as exc:
        raise LockUnavailableError(
            control_changed=False,
            boundary_kind="write",
        ) from exc
    try:
        _fault(injector, "authority.before_identity_reread")
        before = verify_regular_single_link(path)
        stream.seek(0)
        _fault(injector, "authority.before_reread")
        content = stream.read(2)
        _fault(injector, "authority.after_reread")
        if content == b"":
            if not bootstrap:
                raise LockUnavailableError(control_changed=control_changed)
            _fault(injector, "authority.before_zero_length_repair")
            stream.seek(0)
            try:
                _fault(injector, "authority.before_write")
                stream.write(AUTHORITY_BYTE)
                _fault(injector, "authority.after_write")
            except Exception as exc:
                raise LockUnavailableError(
                    control_changed=control_changed,
                    boundary_kind="write",
                ) from exc
            try:
                _fault(injector, "authority.before_flush")
                stream.flush()
                _fault(injector, "authority.after_flush")
                _fault(injector, "authority.before_fsync")
                os.fsync(stream.fileno())
                _fault(injector, "authority.after_fsync")
            except Exception as exc:
                raise LockUnavailableError(
                    control_changed=control_changed,
                    boundary_kind="durability",
                ) from exc
            control_changed = True
            _fault(injector, "authority.after_zero_length_repair")
            stream.seek(0)
            content = stream.read(2)
        if content != AUTHORITY_BYTE:
            raise LockUnavailableError(control_changed=control_changed)
        after = verify_regular_single_link(path)
        _fault(injector, "authority.after_identity_reread")
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise LockUnavailableError(control_changed=control_changed)
        return stream, control_changed
    except OSError as exc:
        stream.close()
        if _is_lock_contention(exc):
            raise LockBusyError(control_changed=control_changed) from exc
        raise LockUnavailableError(
            control_changed=control_changed,
            boundary_kind="verification",
        ) from exc
    except LockUnavailableError:
        stream.close()
        raise
    except Exception:
        stream.close()
        raise LockUnavailableError(
            control_changed=control_changed,
            boundary_kind="verification",
        ) from None


def _kernel_acquire(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl = import_module("fcntl")
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _kernel_release(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl = import_module("fcntl")
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _is_lock_contention(exc: OSError) -> bool:
    if exc.errno in {errno.EACCES, errno.EAGAIN}:
        return True
    return getattr(exc, "winerror", None) in {33, 36, 158}


@dataclass
class AuthorityLease:
    path: Path
    stream: BinaryIO
    registry_keys: tuple[str, ...]
    control_changed: bool
    authority_identity_sha256: str
    adapter: str
    injector: FaultInjector | None = None
    released: bool = False

    def release(self) -> None:
        if self.released:
            return
        try:
            _fault(self.injector, "authority.before_kernel_release")
            _kernel_release(self.stream)
            _fault(self.injector, "authority.after_kernel_release")
            _fault(self.injector, "authority.before_close")
            self.stream.close()
            _fault(self.injector, "authority.after_close")
        except Exception as exc:
            with suppress(OSError):
                self.stream.close()
            raise LockReleaseError("lock release could not be confirmed") from exc
        _release_registry(self.registry_keys)
        self.released = True

    def __enter__(self) -> AuthorityLease:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


def acquire_authority(
    path: Path,
    *,
    registry_keys: Iterable[str],
    bootstrap: bool,
    injector: FaultInjector | None = None,
    prepare: Callable[[], None] | None = None,
    additional_registry_keys: Callable[[], Iterable[str]] | None = None,
) -> AuthorityLease:
    keys = tuple(dict.fromkeys(registry_keys))
    if not keys:
        raise ValueError("at least one registry key is required")
    stream: BinaryIO | None = None
    control_changed = False
    registry_reserved = False
    try:
        _fault(injector, "registry.before_reserve")
        _reserve(keys)
        registry_reserved = True
        _fault(injector, "registry.after_reserve")
        if prepare is not None:
            prepare()
        if additional_registry_keys is not None:
            _fault(injector, "registry.before_extend")
            keys = _extend_reservation(keys, tuple(additional_registry_keys()))
            _fault(injector, "registry.after_extend")
        stream, control_changed = _open_authority(
            path,
            bootstrap=bootstrap,
            injector=injector,
        )
        _fault(injector, "authority.before_kernel_acquire")
        try:
            _kernel_acquire(stream)
        except OSError as exc:
            stream.close()
            _release_registry(keys)
            if _is_lock_contention(exc):
                raise LockBusyError(control_changed=control_changed) from exc
            raise LockUnavailableError(control_changed=control_changed) from exc
        _fault(injector, "authority.after_kernel_acquire")
        file_stat = verify_regular_single_link(path)
        return AuthorityLease(
            path=path,
            stream=stream,
            registry_keys=keys,
            control_changed=control_changed,
            authority_identity_sha256=authority_identity_sha256(file_stat),
            adapter=platform_adapter(),
            injector=injector,
        )
    except LockBusyError:
        if stream is not None and not stream.closed:
            stream.close()
        if registry_reserved:
            _release_registry(keys)
        raise
    except Exception as exc:
        if stream is not None and not stream.closed:
            stream.close()
        if registry_reserved:
            _release_registry(keys)
        if isinstance(exc, LockUnavailableError):
            raise
        raise LockUnavailableError(control_changed=control_changed) from exc


__all__ = [
    "AUTHORITY_BYTE",
    "AuthorityLease",
    "FaultInjector",
    "LockBusyError",
    "LockReleaseError",
    "LockUnavailableError",
    "acquire_authority",
    "registry_contains",
    "verify_directory",
    "verify_regular_single_link",
]
