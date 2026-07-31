"""Exact interpreter/helper identity qualification for P2-028A."""

from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path

from poker_deliberation.isolated_jobs.canonical import isolated_job_sha256
from poker_deliberation.isolated_jobs.models import (
    ExecutionIdentityV1,
    FileIdentityV1,
    IsolatedJobError,
    JobFailureCode,
)
from poker_deliberation.isolated_jobs.paths import file_identity

_READ_CHUNK = 1024 * 1024
_MAX_IDENTITY_FILE_BYTES = 256 * 1024 * 1024


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_IDENTITY_FILE_BYTES:
                    raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH)
                digest.update(chunk)
    except OSError as exc:
        raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH) from exc
    return digest.hexdigest()


def _identity(path: Path, *, single_link: bool) -> FileIdentityV1:
    return file_identity(
        path,
        sha256=sha256_file(path),
        require_single_link=single_link,
    )


def _python_dll(interpreter: Path) -> Path:
    version = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
    candidates = (
        interpreter.parent / version,
        Path(sys.base_prefix) / version,
        Path(sys.base_prefix) / "DLLs" / version,
    )
    matches = tuple(path for path in candidates if path.is_file())
    unique = {str(path.resolve(strict=True)): path for path in matches}
    if len(unique) != 1:
        raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH)
    return next(iter(unique.values()))


def _encoding_files() -> tuple[Path, Path, Path]:
    root = Path(sys.base_prefix) / "Lib" / "encodings"
    return (
        root / "__init__.py",
        root / "aliases.py",
        root / "utf_8.py",
    )


def _identity_payload(identity: ExecutionIdentityV1) -> dict[str, object]:
    payload = identity.model_dump(mode="json")
    payload.pop("identity_sha256")
    return payload


def build_execution_identity() -> ExecutionIdentityV1:
    if sys.platform != "win32" or platform.machine().upper() != "AMD64":
        raise IsolatedJobError(JobFailureCode.UNSUPPORTED_PLATFORM)
    base = Path(getattr(sys, "_base_executable", sys.executable))
    helper = Path(__file__).with_name("synthetic_child.py")
    encoding_paths = _encoding_files()
    encoding_identities = (
        _identity(encoding_paths[0], single_link=False),
        _identity(encoding_paths[1], single_link=False),
        _identity(encoding_paths[2], single_link=False),
    )
    partial = ExecutionIdentityV1.model_construct(
        interpreter=_identity(base, single_link=False),
        python_dll=_identity(_python_dll(base), single_link=False),
        encoding_files=encoding_identities,
        synthetic_helper=_identity(helper, single_link=True),
        python_version=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        architecture="AMD64",
        identity_sha256="0" * 64,
    )
    return ExecutionIdentityV1(
        **(
            partial.model_dump(mode="python")
            | {"identity_sha256": isolated_job_sha256(_identity_payload(partial))}
        )
    )


def verify_execution_identity(expected: ExecutionIdentityV1) -> None:
    observed = build_execution_identity()
    if observed != expected:
        raise IsolatedJobError(JobFailureCode.IDENTITY_MISMATCH)
