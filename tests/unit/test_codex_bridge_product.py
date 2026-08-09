from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from poker_deliberation.codex_bridge import product
from poker_deliberation.codex_bridge.models import BridgeRole, RuntimeAuthModeV1
from poker_deliberation.codex_bridge.product import (
    BridgeProductError,
    confined_runtime_scratch_path,
)
from tests.bounded_river_call_ev_support import app_config


def _git(repository: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    (repository / ".gitignore").write_text(
        "tmp/\nruns/*\n!runs/.gitkeep\n",
        encoding="utf-8",
    )
    (repository / "runs").mkdir()
    (repository / "runs" / ".gitkeep").write_bytes(b"")
    _git(repository, "add", ".gitignore", "runs/.gitkeep")
    return repository


@pytest.mark.parametrize("relative", ("tmp/bridge-runtime", "runs/bridge-runtime"))
def test_runtime_scratch_accepts_only_repository_ignored_namespaces(
    tmp_path: Path,
    relative: str,
) -> None:
    repository = _repository(tmp_path)
    requested = repository / relative

    assert confined_runtime_scratch_path(requested, repository) == requested.resolve()
    assert not requested.exists()


def test_runtime_scratch_rejects_tracked_nonignored_and_untrusted_ignore_sources(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    existing_arbitrary = repository / "docs" / "runtime"
    existing_arbitrary.mkdir(parents=True)
    tracked_scratch = repository / "tmp" / "tracked"
    tracked_scratch.mkdir(parents=True)
    (tracked_scratch / "marker.txt").write_text("tracked", encoding="utf-8")
    _git(repository, "add", "-f", "tmp/tracked/marker.txt")
    info_exclude = repository / ".git" / "info" / "exclude"
    info_exclude.write_text("private/\n", encoding="utf-8")
    nested = repository / "nested"
    nested.mkdir()
    (nested / ".gitignore").write_text("runtime/\n", encoding="utf-8")

    with pytest.raises(BridgeProductError, match="not ignored"):
        confined_runtime_scratch_path(existing_arbitrary, repository)
    with pytest.raises(BridgeProductError, match="tracked path"):
        confined_runtime_scratch_path(tracked_scratch, repository)
    with pytest.raises(BridgeProductError, match=r"tracked repository \.gitignore"):
        confined_runtime_scratch_path(repository / "private" / "runtime", repository)
    with pytest.raises(BridgeProductError, match=r"tracked repository \.gitignore"):
        confined_runtime_scratch_path(nested / "runtime", repository)


@pytest.mark.parametrize(
    "runtime_path",
    (
        Path(".git/runtime"),
        Path("user_materials/runtime"),
        Path("tmp/../tmp/runtime"),
    ),
)
def test_runtime_scratch_rejects_protected_or_traversing_components(
    tmp_path: Path,
    runtime_path: Path,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(BridgeProductError, match="protected component"):
        confined_runtime_scratch_path(repository / runtime_path, repository)
    with pytest.raises(BridgeProductError, match="outside"):
        confined_runtime_scratch_path(tmp_path / "outside-runtime", repository)


def test_runtime_scratch_rejects_symlink_before_resolving_its_target(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    target = repository / "tmp" / "target"
    target.mkdir(parents=True)
    link = repository / "tmp" / "runtime-link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is not available")

    with pytest.raises(BridgeProductError, match="link or reparse"):
        confined_runtime_scratch_path(link, repository)


def test_windows_reparse_attribute_is_rejected_without_link_privilege() -> None:
    if os.name != "nt":
        pytest.skip("Windows reparse attributes are not available")

    class ReparseStatus:
        st_mode = stat.S_IFDIR
        st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

    assert product._is_link_or_reparse(ReparseStatus()) is True  # type: ignore[arg-type]


def test_git_ignore_probe_does_not_inherit_credential_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _repository(tmp_path)
    real_run = subprocess.run
    canary = "synthetic-secret-canary"
    monkeypatch.setenv("OPENAI_API_KEY", canary)

    def inspect_environment(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        environment = kwargs.get("env")
        assert isinstance(environment, dict)
        assert "OPENAI_API_KEY" not in environment
        return real_run(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(product.subprocess, "run", inspect_environment)

    assert confined_runtime_scratch_path(repository / "tmp" / "runtime", repository).is_absolute()


@pytest.mark.parametrize("auth_mode", tuple(RuntimeAuthModeV1))
def test_every_product_auth_mode_rejects_public_runtime_root_before_storage_or_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auth_mode: RuntimeAuthModeV1,
) -> None:
    repository = _repository(tmp_path)
    canary = "synthetic-secret-canary"

    def forbid_storage(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unsafe runtime root reached product storage")

    monkeypatch.setattr(product, "BoundedCodexBridgeStore", forbid_storage)
    with pytest.raises(BridgeProductError, match="not ignored") as error:
        product.execute_product_role(
            config=app_config(repository / "config"),
            repository_root=repository,
            bridge_root=repository / "tmp" / "bridge",
            runtime_root=repository / "docs" / f"raw-events-{canary}",
            bridge_run_id="bridge-runtime-boundary",
            role=BridgeRole.STRATEGY_ANALYST,
            auth_mode=auth_mode,
        )

    assert canary not in str(error.value)
    assert not (repository / "docs" / f"raw-events-{canary}").exists()
