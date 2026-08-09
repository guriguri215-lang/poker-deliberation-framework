"""Single-use filesystem capability for bridge runtime scratch storage."""

from __future__ import annotations

import os
import stat
import threading
from dataclasses import dataclass
from pathlib import Path


class RuntimeScratchIdentityError(RuntimeError):
    """Raised when a prepared runtime directory loses its filesystem identity."""


def _is_link_or_reparse(status: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(file_attributes & reparse_flag)


@dataclass(frozen=True, slots=True)
class _DirectoryIdentity:
    path: Path
    device: int
    inode: int
    mode: int


def _directory_identity(path: Path) -> _DirectoryIdentity:
    try:
        status = path.lstat()
    except OSError as exc:
        raise RuntimeScratchIdentityError("runtime scratch directory is unavailable") from exc
    if not stat.S_ISDIR(status.st_mode) or _is_link_or_reparse(status):
        raise RuntimeScratchIdentityError(
            "runtime scratch directory is linked, reparsed, or non-directory"
        )
    return _DirectoryIdentity(
        path=path,
        device=status.st_dev,
        inode=status.st_ino,
        mode=stat.S_IFMT(status.st_mode),
    )


class PreparedRuntimeRoot:
    """An exclusively-created runtime root whose path identity is repeatedly checked.

    The capability is process-local and single-use.  It narrows path replacement races by
    validating every repository-relative ancestor before each security-sensitive use.
    """

    __slots__ = ("_identities", "_path", "_repository", "_state", "_state_lock")

    def __init__(
        self,
        *,
        path: Path,
        repository: Path,
        identities: tuple[_DirectoryIdentity, ...],
    ) -> None:
        self._path = path
        self._repository = repository
        self._identities = identities
        self._state = "prepared"
        self._state_lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def repository(self) -> Path:
        return self._repository

    @classmethod
    def create(cls, path: Path, repository: Path) -> PreparedRuntimeRoot:
        """Exclusively create ``path`` and bind all ancestor directory identities."""

        repository = repository.resolve(strict=True)
        lexical = Path(os.path.abspath(path))
        try:
            relative = lexical.relative_to(repository)
        except ValueError as exc:
            raise RuntimeScratchIdentityError(
                "runtime scratch root escaped its repository"
            ) from exc
        if not relative.parts:
            raise RuntimeScratchIdentityError("runtime scratch root cannot be the repository")

        current = repository
        identities: list[_DirectoryIdentity] = [_directory_identity(repository)]
        for index, part in enumerate(relative.parts):
            current /= part
            is_root = index == len(relative.parts) - 1
            try:
                current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                except OSError as exc:
                    raise RuntimeScratchIdentityError(
                        "runtime scratch directory could not be created exclusively"
                    ) from exc
            except OSError as exc:
                raise RuntimeScratchIdentityError(
                    "runtime scratch directory inspection failed"
                ) from exc
            else:
                if is_root:
                    raise RuntimeScratchIdentityError(
                        "runtime scratch root already exists and is not single-use"
                    )
            identity = _directory_identity(current)
            try:
                if current.resolve(strict=True) != current:
                    raise RuntimeScratchIdentityError(
                        "runtime scratch directory resolution changed during creation"
                    )
            except OSError as exc:
                raise RuntimeScratchIdentityError(
                    "runtime scratch directory resolution failed"
                ) from exc
            identities.append(identity)

        prepared = cls(path=lexical, repository=repository, identities=tuple(identities))
        prepared.verify()
        return prepared

    def verify(self) -> None:
        """Fail closed if any bound directory was replaced, linked, or reparsed."""

        if (
            not self._identities
            or self.repository != self._identities[0].path
            or self.path != self._identities[-1].path
        ):
            raise RuntimeScratchIdentityError("runtime scratch capability path changed")
        try:
            resolved = self.path.resolve(strict=True)
        except OSError as exc:
            raise RuntimeScratchIdentityError("runtime scratch root is unavailable") from exc
        if resolved != self.path or not resolved.is_relative_to(self.repository):
            raise RuntimeScratchIdentityError("runtime scratch root escaped its repository")
        for expected in self._identities:
            current = _directory_identity(expected.path)
            if current != expected:
                raise RuntimeScratchIdentityError("runtime scratch directory identity changed")

    def begin(self) -> None:
        """Consume the capability for exactly one transport execution."""

        with self._state_lock:
            if self._state != "prepared":
                raise RuntimeScratchIdentityError("runtime scratch capability was already consumed")
            self.verify()
            self._state = "active"

    def verify_active(self) -> None:
        with self._state_lock:
            if self._state != "active":
                raise RuntimeScratchIdentityError("runtime scratch capability is not active")
            self.verify()

    def finish(self) -> None:
        """Verify the final identity and permanently close the capability."""

        with self._state_lock:
            if self._state != "active":
                raise RuntimeScratchIdentityError("runtime scratch capability is not active")
            self.verify()
            self._state = "finished"


__all__ = ["PreparedRuntimeRoot", "RuntimeScratchIdentityError"]
