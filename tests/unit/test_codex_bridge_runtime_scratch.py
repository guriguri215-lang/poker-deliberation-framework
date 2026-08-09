from __future__ import annotations

from pathlib import Path

import pytest

from poker_deliberation.codex_bridge.runtime_scratch import (
    PreparedRuntimeRoot,
    RuntimeScratchIdentityError,
)


def test_prepared_runtime_root_is_single_use_and_keeps_its_path(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime_root = repository / "tmp" / "runs" / "runtime"

    prepared = PreparedRuntimeRoot.create(runtime_root, repository)

    assert prepared.path == runtime_root.resolve()
    assert prepared.repository == repository.resolve()
    prepared.begin()
    prepared.verify_active()
    prepared.finish()
    with pytest.raises(RuntimeScratchIdentityError, match="already consumed"):
        prepared.begin()


def test_prepared_runtime_root_rejects_same_path_directory_replacement(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime_root = repository / "tmp" / "runs" / "runtime"
    prepared = PreparedRuntimeRoot.create(runtime_root, repository)
    prepared.begin()
    retired_root = repository / "retired-runtime"
    runtime_root.rename(retired_root)
    runtime_root.mkdir()

    with pytest.raises(RuntimeScratchIdentityError, match="identity changed"):
        prepared.verify_active()
    with pytest.raises(RuntimeScratchIdentityError, match="identity changed"):
        prepared.finish()


def test_prepared_runtime_root_path_is_read_only_and_errors_hide_path_values(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository-synthetic-private-canary"
    repository.mkdir()
    runtime_root = repository / "tmp" / "runs" / "runtime-synthetic-secret-canary"
    prepared = PreparedRuntimeRoot.create(runtime_root, repository)

    with pytest.raises(AttributeError):
        prepared.path = repository / "other"  # type: ignore[misc]

    prepared.begin()
    runtime_root.rename(repository / "retired")
    runtime_root.mkdir()
    with pytest.raises(RuntimeScratchIdentityError) as caught:
        prepared.finish()

    assert "synthetic-private-canary" not in str(caught.value)
    assert "synthetic-secret-canary" not in str(caught.value)
