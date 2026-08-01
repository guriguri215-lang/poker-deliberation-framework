"""Fail-closed directory metadata durability for pre-execution commitments."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from poker_deliberation.storage.revision_canonical import CanonicalStorageError
from poker_deliberation.storage.revision_lock import verify_directory

DirectorySyncFaultInjector = Callable[[str], None]


def _fault(injector: DirectorySyncFaultInjector | None, hook: str) -> None:
    if injector is not None:
        injector(hook)


def _sync_windows_directory(path: Path) -> None:
    """Open a directory as a Win32 file object and flush its metadata."""

    import ctypes
    from ctypes import wintypes

    kernel32: Any = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = (wintypes.HANDLE,)
    flush_file_buffers.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_write = 0x40000000
    share_read_write_delete = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    invalid_handle = wintypes.HANDLE(-1).value
    handle = create_file(
        str(path),
        generic_write,
        share_read_write_delete,
        None,
        open_existing,
        file_flag_backup_semantics,
        None,
    )
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    pending: OSError | None = None
    try:
        if not flush_file_buffers(handle):
            pending = ctypes.WinError(ctypes.get_last_error())
    finally:
        if not close_handle(handle) and pending is None:
            pending = ctypes.WinError(ctypes.get_last_error())
    if pending is not None:
        raise pending


def sync_directory(
    path: Path,
    *,
    injector: DirectorySyncFaultInjector | None = None,
    hook: str = "directory_sync",
) -> None:
    """Durably flush one directory or raise before the caller may execute tools."""

    try:
        verify_directory(path)
        _fault(injector, f"{hook}.before_open")
        if os.name == "nt":
            _sync_windows_directory(path)
        else:
            descriptor: int | None = None
            try:
                descriptor = os.open(path, os.O_RDONLY)
                _fault(injector, f"{hook}.after_open")
                _fault(injector, f"{hook}.before_fsync")
                os.fsync(descriptor)
                _fault(injector, f"{hook}.after_fsync")
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        _fault(injector, f"{hook}.after_sync")
        verify_directory(path)
    except CanonicalStorageError:
        raise
    except Exception as exc:
        raise CanonicalStorageError(f"{hook} durability sync failed") from exc


__all__ = ["DirectorySyncFaultInjector", "sync_directory"]
