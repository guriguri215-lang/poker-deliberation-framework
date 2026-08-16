"""Run one deterministic CI test-file shard with per-file process isolation."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class RunnerContractError(ValueError):
    """Raised when the shard runner's invocation or environment is invalid."""


class RunnerAccountingError(ValueError):
    """Raised when discovered, partitioned, and executed files do not reconcile."""


class JUnitEvidenceError(ValueError):
    """Raised when a per-file pytest run did not produce valid test evidence."""


def discover_test_files(repository_root: Path) -> tuple[str, ...]:
    """Return sorted repository-relative POSIX paths for the test suite."""

    root = repository_root.resolve()
    tests_root = root / "tests"
    if not tests_root.is_dir():
        raise RunnerContractError(f"tests directory does not exist: {tests_root}")
    files = tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in tests_root.rglob("*.py")
            if path.is_file() and (path.name.startswith("test_") or path.name.endswith("_test.py"))
        )
    )
    if not files:
        raise RunnerContractError("no pytest-standard test files were discovered")
    return files


def _validate_shard_args(*, shard_number: int, shard_count: int) -> None:
    if shard_count < 1:
        raise RunnerContractError("shard-count must be at least 1")
    if not 1 <= shard_number <= shard_count:
        raise RunnerContractError("shard-number must be between 1 and shard-count")


def partition_test_files(
    files: Sequence[str],
    *,
    shard_number: int,
    shard_count: int,
) -> tuple[str, ...]:
    """Select a one-based deterministic round-robin test-file shard."""

    _validate_shard_args(shard_number=shard_number, shard_count=shard_count)
    return tuple(files[shard_number - 1 :: shard_count])


def audit_partitions(files: Sequence[str], partitions: Sequence[Sequence[str]]) -> None:
    """Fail if shard partitions duplicate, omit, or introduce any test file."""

    expected = Counter(files)
    observed = Counter(file for partition in partitions for file in partition)
    duplicate_inputs = sorted(file for file, count in expected.items() if count > 1)
    duplicates = sorted(file for file, count in observed.items() if count > 1)
    missing = sorted((expected - observed).elements())
    unexpected = sorted((observed - expected).elements())
    if duplicate_inputs or duplicates or missing or unexpected:
        details = (
            f"duplicate_inputs={duplicate_inputs}, duplicates={duplicates}, "
            f"missing={missing}, unexpected={unexpected}"
        )
        raise RunnerAccountingError(f"invalid shard partition accounting: {details}")


def build_partitions(files: Sequence[str], *, shard_count: int) -> tuple[tuple[str, ...], ...]:
    """Build and audit the complete deterministic partition set."""

    _validate_shard_args(shard_number=1, shard_count=shard_count)
    partitions = tuple(
        partition_test_files(files, shard_number=number, shard_count=shard_count)
        for number in range(1, shard_count + 1)
    )
    if any(not partition for partition in partitions):
        raise RunnerContractError("shard-count cannot exceed the discovered test-file count")
    audit_partitions(files, partitions)
    return partitions


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _parse_test_count(value: str | None, *, path: Path) -> int:
    if value is None:
        raise JUnitEvidenceError(f"JUnit tests attribute is missing: {path}")
    try:
        count = int(value)
    except ValueError as exc:
        raise JUnitEvidenceError(f"JUnit tests attribute is not an integer: {path}") from exc
    if count < 0:
        raise JUnitEvidenceError(f"JUnit tests attribute is negative: {path}")
    return count


def junit_test_count(path: Path) -> int:
    """Parse a pytest JUnit document and require evidence of at least one test."""

    if not path.is_file():
        raise JUnitEvidenceError(f"JUnit file is missing: {path}")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise JUnitEvidenceError(f"JUnit file is unreadable or invalid: {path}") from exc

    root_name = _xml_local_name(root.tag)
    if root_name == "testsuite":
        count = _parse_test_count(root.get("tests"), path=path)
    elif root_name == "testsuites":
        if root.get("tests") is not None:
            count = _parse_test_count(root.get("tests"), path=path)
        else:
            suites = [child for child in root if _xml_local_name(child.tag) == "testsuite"]
            if not suites:
                raise JUnitEvidenceError(f"JUnit document has no test suites: {path}")
            count = sum(_parse_test_count(suite.get("tests"), path=path) for suite in suites)
    else:
        raise JUnitEvidenceError(f"JUnit document has an unexpected root element: {path}")

    if count == 0:
        raise JUnitEvidenceError(f"JUnit document reports zero tests: {path}")
    return count


def _file_runtime_paths(invocation_root: Path, file_index: int) -> dict[str, Path]:
    file_root = invocation_root / f"f{file_index}"
    file_root.mkdir()
    paths = {
        "basetemp": file_root / "b",
        "cache": file_root / "c",
        "junit": file_root / "j.xml",
        "hypothesis": file_root / "h",
    }
    paths["hypothesis"].mkdir()
    return paths


def _audit_attempts(selected: Sequence[str], attempted: Sequence[str]) -> None:
    expected = Counter(selected)
    observed = Counter(attempted)
    duplicates = sorted(file for file, count in observed.items() if count > 1)
    missing = sorted((expected - observed).elements())
    unexpected = sorted((observed - expected).elements())
    if duplicates or missing or unexpected:
        details = f"duplicates={duplicates}, missing={missing}, unexpected={unexpected}"
        raise RunnerAccountingError(f"invalid selected-file execution accounting: {details}")


def run_test_shard(
    repository_root: Path,
    *,
    shard_number: int,
    shard_count: int,
    temp_root: Path,
) -> int:
    """Run every selected file once in its own pytest process."""

    _validate_shard_args(shard_number=shard_number, shard_count=shard_count)
    root = repository_root.resolve()
    files = discover_test_files(root)
    partitions = build_partitions(files, shard_count=shard_count)
    selected = partitions[shard_number - 1]

    resolved_temp_root = temp_root if temp_root.is_absolute() else root / temp_root
    resolved_temp_root = resolved_temp_root.resolve()
    resolved_temp_root.mkdir(parents=True, exist_ok=True)
    invocation_root = Path(tempfile.mkdtemp(prefix=f"s{shard_number}-", dir=resolved_temp_root))

    attempted: list[str] = []
    failed_files: list[str] = []
    total_tests = 0
    for file_index, test_file in enumerate(selected):
        paths = _file_runtime_paths(invocation_root, file_index)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "--color=no",
            "--basetemp",
            str(paths["basetemp"]),
            "-o",
            f"cache_dir={paths['cache']}",
            "--junitxml",
            str(paths["junit"]),
            test_file,
        ]
        environment = os.environ.copy()
        environment.pop("PYTEST_ADDOPTS", None)
        environment["PYTHONUTF8"] = "1"
        environment["NO_COLOR"] = "1"
        environment["HYPOTHESIS_STORAGE_DIRECTORY"] = str(paths["hypothesis"])

        attempted.append(test_file)
        print(f"[ci-shard {shard_number}/{shard_count}] running {test_file}", flush=True)
        return_code: int | None
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                check=False,
                shell=False,
            )
            return_code = completed.returncode
        except OSError as exc:
            print(
                f"[ci-shard {shard_number}/{shard_count}] pytest launch failed for "
                f"{test_file}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            return_code = None

        evidence_error: JUnitEvidenceError | None = None
        try:
            total_tests += junit_test_count(paths["junit"])
        except JUnitEvidenceError as exc:
            evidence_error = exc
            print(
                f"[ci-shard {shard_number}/{shard_count}] {exc}",
                file=sys.stderr,
                flush=True,
            )
        if return_code != 0 or evidence_error is not None:
            failed_files.append(test_file)

    _audit_attempts(selected, attempted)
    print(
        f"[ci-shard {shard_number}/{shard_count}] files={len(selected)} "
        f"tests={total_tests} failures={len(failed_files)}",
        flush=True,
    )
    for failed_file in failed_files:
        print(
            f"[ci-shard {shard_number}/{shard_count}] failed={failed_file}",
            file=sys.stderr,
            flush=True,
        )
    return 1 if failed_files else 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one deterministic pytest file shard with fresh-process isolation."
    )
    parser.add_argument("--shard-number", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    parser.add_argument("--temp-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return run_test_shard(
            REPOSITORY_ROOT,
            shard_number=args.shard_number,
            shard_count=args.shard_count,
            temp_root=args.temp_root,
        )
    except RunnerAccountingError as exc:
        print(f"ci shard runner accounting error: {exc}", file=sys.stderr)
        return 1
    except (OSError, RunnerContractError) as exc:
        print(f"ci shard runner contract error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
