"""Fail-closed Windows path and file-identity validation for P2-028A."""

from __future__ import annotations

import os
import re
from pathlib import Path

from poker_deliberation.isolated_jobs.models import (
    DirectoryIdentityV1,
    FileIdentityV1,
    IsolatedJobError,
    JobFailureCode,
)

_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _reject(code: JobFailureCode) -> None:
    raise IsolatedJobError(code)


def _is_reparse(stat_result: os.stat_result) -> bool:
    attributes = int(getattr(stat_result, "st_file_attributes", 0))
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _validate_windows_segments(path: Path) -> None:
    text = str(path)
    if (
        not path.is_absolute()
        or len(text) > 220
        or "\x00" in text
        or text.endswith((" ", "."))
        or not re.match(r"^[A-Za-z]:\\", text)
    ):
        _reject(JobFailureCode.PATH_CONFINEMENT_FAILED)
    for index, part in enumerate(path.parts):
        if index == 0:
            continue
        stem = part.split(".", maxsplit=1)[0].upper()
        if (
            part in {"", ".", ".."}
            or part.endswith((" ", "."))
            or ":" in part
            or stem in _WINDOWS_RESERVED
        ):
            _reject(JobFailureCode.PATH_CONFINEMENT_FAILED)


def _validate_components(path: Path) -> None:
    _validate_windows_segments(path)
    candidate = Path(path.anchor)
    for part in path.parts[1:]:
        candidate /= part
        try:
            info = candidate.lstat()
        except OSError:
            _reject(JobFailureCode.PATH_CONFINEMENT_FAILED)
        if candidate.is_symlink() or _is_reparse(info):
            _reject(JobFailureCode.LINK_OR_REPARSE_DETECTED)


def canonical_existing_path(path: Path, *, directory: bool) -> Path:
    raw = Path(path)
    _validate_windows_segments(raw)
    try:
        absolute = raw.absolute()
        resolved = raw.resolve(strict=True)
    except OSError:
        _reject(JobFailureCode.PATH_CONFINEMENT_FAILED)
    if os.path.normcase(str(absolute)) != os.path.normcase(str(resolved)):
        _reject(JobFailureCode.LINK_OR_REPARSE_DETECTED)
    _validate_components(resolved)
    if directory != resolved.is_dir():
        _reject(JobFailureCode.PATH_CONFINEMENT_FAILED)
    if not directory and not resolved.is_file():
        _reject(JobFailureCode.PATH_CONFINEMENT_FAILED)
    return resolved


def directory_identity(path: Path) -> DirectoryIdentityV1:
    candidate = canonical_existing_path(path, directory=True)
    info = candidate.stat()
    return DirectoryIdentityV1(
        absolute_path=str(candidate),
        device_id=int(info.st_dev),
        file_id=int(info.st_ino),
        modified_time_ns=int(info.st_mtime_ns),
    )


def file_identity(
    path: Path,
    *,
    sha256: str,
    require_single_link: bool,
) -> FileIdentityV1:
    candidate = canonical_existing_path(path, directory=False)
    info = candidate.stat()
    if require_single_link and info.st_nlink != 1:
        _reject(JobFailureCode.HARDLINK_DETECTED)
    return FileIdentityV1(
        absolute_path=str(candidate),
        size_bytes=int(info.st_size),
        sha256=sha256,
        device_id=int(info.st_dev),
        file_id=int(info.st_ino),
        link_count=int(info.st_nlink),
        modified_time_ns=int(info.st_mtime_ns),
    )


def verify_open_file_identity(file_descriptor: int, expected: FileIdentityV1) -> None:
    try:
        info = os.fstat(file_descriptor)
    except OSError:
        _reject(JobFailureCode.PATH_CONFINEMENT_FAILED)
    if (
        int(info.st_dev) != expected.device_id
        or int(info.st_ino) != expected.file_id
        or int(info.st_size) != expected.size_bytes
        or int(info.st_nlink) != expected.link_count
        or int(info.st_mtime_ns) != expected.modified_time_ns
    ):
        _reject(JobFailureCode.IDENTITY_MISMATCH)


def verify_directory_identity(expected: DirectoryIdentityV1) -> None:
    observed = directory_identity(Path(expected.absolute_path))
    if observed != expected:
        _reject(JobFailureCode.IDENTITY_MISMATCH)
