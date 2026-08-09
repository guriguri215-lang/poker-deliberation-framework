"""Read-only checkout and loaded-module identity gates for the bounded bridge."""

from __future__ import annotations

import importlib
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from poker_deliberation.codex_bridge.canonical import domain_sha256, sha256_bytes

_SOURCE_ID_LENGTHS = {40, 64}
_BRIDGE_MODULES = (
    "poker_deliberation.codex_bridge.canonical",
    "poker_deliberation.codex_bridge.conformance",
    "poker_deliberation.codex_bridge.contracts",
    "poker_deliberation.codex_bridge.controller",
    "poker_deliberation.codex_bridge.credential_launcher",
    "poker_deliberation.codex_bridge.identity",
    "poker_deliberation.codex_bridge.models",
    "poker_deliberation.codex_bridge.product",
    "poker_deliberation.codex_bridge.qualification",
    "poker_deliberation.codex_bridge.replay",
    "poker_deliberation.codex_bridge.sdk_transport",
    "poker_deliberation.codex_bridge.sdk_worker",
    "poker_deliberation.codex_bridge.source",
    "poker_deliberation.codex_bridge.storage",
    "poker_deliberation.codex_bridge.subscription_transport",
    "poker_deliberation.codex_bridge.transport",
)

BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN = "poker-bounded-codex-runtime-source-inventory-v1"
_RUNTIME_SOURCE_EXCLUSIONS = frozenset(
    {
        # Roadmap prose/status projection is not part of execution or evidence validation.
        "src/poker_deliberation/roadmap.py",
    }
)
_RUNTIME_SUPPORT_PATHS = (
    "pyproject.toml",
    "requirements.lock",
    "scripts/run_codex_bridge_live_qualification.py",
    "tests/fixtures/codex_bridge/v1/public-synthetic-qualification.json",
)


@dataclass(frozen=True, slots=True)
class BridgeRuntimeSourceFile:
    path: str
    size: int
    sha256: str


class BridgeIdentityError(ValueError):
    """Raised when executable bridge bytes cannot be bound to one clean checkout."""


def _source_id(value: str) -> bool:
    return len(value) in _SOURCE_ID_LENGTHS and all(
        character in "0123456789abcdef" for character in value
    )


def _git_stdout(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ("git", "-C", str(root), *arguments),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=15,
        )
    except Exception as exc:
        raise BridgeIdentityError("bridge Git identity probe failed") from exc
    if completed.returncode != 0:
        raise BridgeIdentityError("bridge Git identity probe failed")
    try:
        return completed.stdout.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise BridgeIdentityError("bridge Git identity output is not UTF-8") from exc


def verify_bridge_checkout(
    repository_root: Path,
    *,
    repository_commit_id: str,
    repository_tree_id: str,
) -> None:
    """Require one clean, unmodified checkout with no replace refs or index flags."""

    if not _source_id(repository_commit_id) or not _source_id(repository_tree_id):
        raise BridgeIdentityError("bridge repository identity is invalid")
    root = repository_root.resolve(strict=True)
    actual_commit = _git_stdout(root, "rev-parse", "HEAD")
    actual_tree = _git_stdout(root, "rev-parse", "HEAD^{tree}")
    status = _git_stdout(root, "status", "--porcelain=v1", "--untracked-files=all")
    replace_refs = _git_stdout(root, "replace", "-l")
    index_flags = _git_stdout(root, "ls-files", "-v")
    flagged = any(
        line and (line[0].islower() or line[0] == "S") for line in index_flags.splitlines()
    )
    if (
        actual_commit != repository_commit_id
        or actual_tree != repository_tree_id
        or status
        or replace_refs
        or flagged
    ):
        raise BridgeIdentityError("bridge repository checkout binding mismatch")


def verify_bridge_module_origins(repository_root: Path) -> None:
    """Require loaded bridge modules to originate in the claimed repository package."""

    package_root = (repository_root.resolve(strict=True) / "src" / "poker_deliberation").resolve(
        strict=True
    )
    package = importlib.import_module("poker_deliberation")
    package_file = getattr(package, "__file__", None)
    if package_file is None or Path(package_file).resolve() != package_root / "__init__.py":
        raise BridgeIdentityError("bridge package origin mismatch")
    for module_name in _BRIDGE_MODULES:
        module = importlib.import_module(module_name)
        module_file = getattr(module, "__file__", None)
        if module_file is None or not Path(module_file).resolve().is_relative_to(package_root):
            raise BridgeIdentityError("bridge module origin mismatch")


def bridge_runtime_source_inventory(
    repository_root: Path,
) -> tuple[BridgeRuntimeSourceFile, ...]:
    """Hash the public source bytes that can affect the qualification execution path."""

    root = repository_root.resolve(strict=True)
    source_root = root / "src" / "poker_deliberation"
    paths = {
        path.relative_to(root).as_posix() for path in source_root.rglob("*.py") if path.is_file()
    }
    agent_root = root / ".codex" / "agents"
    paths.update(
        path.relative_to(root).as_posix() for path in agent_root.glob("*.toml") if path.is_file()
    )
    paths.difference_update(_RUNTIME_SOURCE_EXCLUSIONS)
    paths.update(_RUNTIME_SUPPORT_PATHS)
    inventory: list[BridgeRuntimeSourceFile] = []
    for relative in sorted(paths):
        candidate = root.joinpath(*relative.split("/"))
        try:
            metadata = candidate.lstat()
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise BridgeIdentityError("bridge runtime source inventory is incomplete") from exc
        if (
            not candidate.is_file()
            or candidate.is_symlink()
            or metadata.st_nlink != 1
            or resolved != candidate.resolve()
            or (resolved != root and root not in resolved.parents)
        ):
            raise BridgeIdentityError("bridge runtime source inventory path is unsafe")
        data = candidate.read_bytes()
        inventory.append(
            BridgeRuntimeSourceFile(
                path=relative,
                size=len(data),
                sha256=sha256_bytes(data),
            )
        )
    return tuple(inventory)


def bridge_runtime_source_inventory_sha256(repository_root: Path) -> str:
    inventory = bridge_runtime_source_inventory(repository_root)
    return domain_sha256(
        BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
        [asdict(item) for item in inventory],
    )


__all__ = [
    "BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN",
    "BridgeIdentityError",
    "BridgeRuntimeSourceFile",
    "bridge_runtime_source_inventory",
    "bridge_runtime_source_inventory_sha256",
    "verify_bridge_checkout",
    "verify_bridge_module_origins",
]
