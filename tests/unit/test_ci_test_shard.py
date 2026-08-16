from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts import run_ci_test_shard as runner


def _make_test_file(repository_root: Path, relative_path: str) -> None:
    path = repository_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def test_example():\n    assert True\n", encoding="utf-8")


def _junit_path(command: list[str]) -> Path:
    return Path(command[command.index("--junitxml") + 1])


def _write_junit(command: list[str], *, tests: int = 1) -> None:
    path = _junit_path(command)
    path.write_text(f'<testsuites tests="{tests}" failures="0" errors="0"/>', encoding="utf-8")


def test_discovery_and_round_robin_partition_are_complete_and_disjoint(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo"
    for relative_path in (
        "tests/z/test_z.py",
        "tests/test_b.py",
        "tests/a/test_a.py",
        "tests/a/legacy_test.py",
        "tests/a/helper.py",
    ):
        _make_test_file(repository_root, relative_path)

    files = runner.discover_test_files(repository_root)

    assert files == (
        "tests/a/legacy_test.py",
        "tests/a/test_a.py",
        "tests/test_b.py",
        "tests/z/test_z.py",
    )
    partitions = runner.build_partitions(files, shard_count=3)
    assert partitions == (
        ("tests/a/legacy_test.py", "tests/z/test_z.py"),
        ("tests/a/test_a.py",),
        ("tests/test_b.py",),
    )
    assert runner.partition_test_files(files, shard_number=2, shard_count=2) == (
        "tests/a/test_a.py",
        "tests/z/test_z.py",
    )
    runner.audit_partitions(files, partitions)

    with pytest.raises(runner.RunnerAccountingError, match="duplicates"):
        runner.audit_partitions(files, (files[:2], files[1:]))
    with pytest.raises(runner.RunnerAccountingError, match="missing"):
        runner.audit_partitions(files, (files[:2],))
    with pytest.raises(runner.RunnerAccountingError, match="unexpected"):
        runner.audit_partitions(files, (files, ("tests/test_unexpected.py",)))
    with pytest.raises(runner.RunnerContractError, match="cannot exceed"):
        runner.build_partitions(files, shard_count=len(files) + 1)


@pytest.mark.parametrize(
    ("shard_number", "shard_count"),
    ((0, 1), (2, 1), (1, 0), (1, -1)),
)
def test_invalid_shard_arguments_return_contract_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    shard_number: int,
    shard_count: int,
) -> None:
    monkeypatch.setattr(runner, "REPOSITORY_ROOT", tmp_path)

    result = runner.main(
        [
            "--shard-number",
            str(shard_number),
            "--shard-count",
            str(shard_count),
            "--temp-root",
            str(tmp_path / "run"),
        ]
    )

    assert result == 2
    assert "runner contract error" in capsys.readouterr().err


def test_main_returns_one_for_accounting_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_accounting(*args: Any, **kwargs: Any) -> int:
        del args, kwargs
        raise runner.RunnerAccountingError("missing=['tests/test_missing.py']")

    monkeypatch.setattr(runner, "run_test_shard", fail_accounting)

    result = runner.main(
        [
            "--shard-number",
            "1",
            "--shard-count",
            "1",
            "--temp-root",
            str(tmp_path),
        ]
    )

    assert result == 1
    assert "runner accounting error" in capsys.readouterr().err


def test_each_selected_file_gets_one_fresh_process_and_unique_runtime_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repo"
    selected_files = (
        "tests/a/test_a.py",
        "tests/b/test_b.py",
        "tests/test_c.py",
    )
    for relative_path in selected_files:
        _make_test_file(repository_root, relative_path)
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        _write_junit(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_test_shard(
        repository_root,
        shard_number=1,
        shard_count=1,
        temp_root=tmp_path / "ci",
    )

    assert result == 0
    assert len(calls) == len(selected_files)
    observed_files: list[str] = []
    runtime_paths: list[str] = []
    for command, kwargs in calls:
        assert command[:4] == [sys.executable, "-m", "pytest", "--color=no"]
        assert kwargs["cwd"] == repository_root.resolve()
        assert kwargs["check"] is False
        assert kwargs["shell"] is False
        assert "stdout" not in kwargs
        assert "stderr" not in kwargs
        observed_files.append(command[-1])

        environment = kwargs["env"]
        assert "PYTEST_ADDOPTS" not in environment
        assert environment["PYTHONUTF8"] == "1"
        assert environment["NO_COLOR"] == "1"
        for name in ("TEMP", "TMP", "TMPDIR"):
            assert environment.get(name) == runner.os.environ.get(name)
        basetemp = command[command.index("--basetemp") + 1]
        cache_option = command[command.index("-o") + 1]
        assert cache_option.startswith("cache_dir=")
        runtime_paths.extend(
            (
                basetemp,
                cache_option.removeprefix("cache_dir="),
                str(_junit_path(command)),
                environment["HYPOTHESIS_STORAGE_DIRECTORY"],
            )
        )
    assert tuple(observed_files) == selected_files
    assert len(runtime_paths) == len(set(runtime_paths))


def test_pytest_failure_does_not_prevent_later_files_from_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repo"
    selected_files = tuple(f"tests/test_{name}.py" for name in ("a", "b", "c"))
    for relative_path in selected_files:
        _make_test_file(repository_root, relative_path)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        _write_junit(command)
        return subprocess.CompletedProcess(command, 1 if len(calls) == 1 else 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_test_shard(
        repository_root,
        shard_number=1,
        shard_count=1,
        temp_root=tmp_path / "ci",
    )

    assert result == 1
    assert tuple(command[-1] for command in calls) == selected_files


@pytest.mark.parametrize("junit_payload", (None, b"<not-xml", b'<testsuites tests="0"/>'))
def test_missing_invalid_or_zero_test_junit_is_a_file_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    junit_payload: bytes | None,
) -> None:
    repository_root = tmp_path / "repo"
    _make_test_file(repository_root, "tests/test_only.py")

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        if junit_payload is not None:
            _junit_path(command).write_bytes(junit_payload)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    result = runner.run_test_shard(
        repository_root,
        shard_number=1,
        shard_count=1,
        temp_root=tmp_path / "ci",
    )

    assert result == 1
