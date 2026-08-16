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
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PYTEST_EXIT_NO_TESTS_COLLECTED = 5


class RunnerContractError(ValueError):
    """Raised when the shard runner's invocation or environment is invalid."""


class RunnerAccountingError(ValueError):
    """Raised when discovered, partitioned, and executed files do not reconcile."""


class JUnitEvidenceError(ValueError):
    """Raised when a per-file pytest run did not produce valid test evidence."""


@dataclass(frozen=True)
class JUnitEvidence:
    """Validated JUnit counts and result-level evidence for one pytest process."""

    tests: int
    skipped: int | None
    failures: int | None
    errors: int | None
    testcase_count: int
    all_testcases_skipped: bool
    contains_failure_or_error: bool
    suite_results_reconciled: bool

    @property
    def all_skipped(self) -> bool:
        return (
            self.testcase_count == self.tests
            and self.testcase_count > 0
            and self.all_testcases_skipped
            and not self.contains_failure_or_error
            and self.suite_results_reconciled
            and self.skipped == self.tests
            and self.failures == 0
            and self.errors == 0
        )


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


def _parse_count(value: str | None, *, name: str, path: Path) -> int:
    if value is None:
        raise JUnitEvidenceError(f"JUnit {name} attribute is missing: {path}")
    try:
        count = int(value)
    except ValueError as exc:
        raise JUnitEvidenceError(f"JUnit {name} attribute is not an integer: {path}") from exc
    if count < 0:
        raise JUnitEvidenceError(f"JUnit {name} attribute is negative: {path}")
    return count


def _aggregate_count(
    root: ET.Element,
    suites: Sequence[ET.Element],
    *,
    name: str,
    path: Path,
    required: bool,
) -> int | None:
    root_value = root.get(name)
    root_count = _parse_count(root_value, name=name, path=path) if root_value is not None else None
    suite_counts: list[int] = []
    suites_complete = bool(suites)
    for suite in suites:
        value = suite.get(name)
        if value is None:
            suites_complete = False
        else:
            suite_counts.append(_parse_count(value, name=name, path=path))
    suite_count = sum(suite_counts) if suites_complete else None
    if root_count is not None and suite_count is not None and root_count != suite_count:
        raise JUnitEvidenceError(f"JUnit {name} aggregate does not match its suites: {path}")
    count = root_count if root_count is not None else suite_count
    if required and count is None:
        raise JUnitEvidenceError(f"JUnit {name} attribute is missing: {path}")
    return count


def _suite_results_reconcile(suite: ET.Element, *, path: Path) -> bool:
    required_counts: dict[str, int] = {}
    for name in ("tests", "skipped", "failures", "errors"):
        value = suite.get(name)
        if value is None:
            return False
        required_counts[name] = _parse_count(value, name=name, path=path)

    testcases = tuple(child for child in suite if _xml_local_name(child.tag) == "testcase")
    skipped_results = 0
    failure_results = 0
    error_results = 0
    for testcase in testcases:
        result_elements = tuple(
            child
            for child in testcase
            if _xml_local_name(child.tag) in {"skipped", "failure", "error"}
        )
        result_names = tuple(_xml_local_name(child.tag) for child in result_elements)
        skipped_results += result_names.count("skipped")
        failure_results += result_names.count("failure")
        error_results += result_names.count("error")
        if result_names != ("skipped",):
            return False
        if result_elements[0].get("message") != "collection skipped":
            return False
        if "type" in result_elements[0].attrib:
            return False
    return (
        len(testcases) == required_counts["tests"]
        and skipped_results == required_counts["skipped"] == required_counts["tests"]
        and failure_results == required_counts["failures"] == 0
        and error_results == required_counts["errors"] == 0
    )


def junit_evidence(path: Path) -> JUnitEvidence:
    """Parse and validate pytest JUnit counts and testcase result evidence."""

    if not path.is_file():
        raise JUnitEvidenceError(f"JUnit file is missing: {path}")
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError) as exc:
        raise JUnitEvidenceError(f"JUnit file is unreadable or invalid: {path}") from exc

    root_name = _xml_local_name(root.tag)
    if root_name == "testsuite":
        suites: tuple[ET.Element, ...] = ()
        result_suites = (root,)
    elif root_name == "testsuites":
        suites = tuple(child for child in root if _xml_local_name(child.tag) == "testsuite")
        result_suites = suites
    else:
        raise JUnitEvidenceError(f"JUnit document has an unexpected root element: {path}")

    tests = _aggregate_count(root, suites, name="tests", path=path, required=True)
    assert tests is not None
    if tests == 0:
        raise JUnitEvidenceError(f"JUnit document reports zero tests: {path}")
    skipped = _aggregate_count(root, suites, name="skipped", path=path, required=False)
    failures = _aggregate_count(root, suites, name="failures", path=path, required=False)
    errors = _aggregate_count(root, suites, name="errors", path=path, required=False)
    outcome_counts = tuple(count for count in (skipped, failures, errors) if count is not None)
    if any(count > tests for count in outcome_counts):
        raise JUnitEvidenceError(f"JUnit outcome count exceeds its test count: {path}")
    if len(outcome_counts) == 3 and sum(outcome_counts) > tests:
        raise JUnitEvidenceError(f"JUnit outcome counts exceed its test count: {path}")

    testcases = tuple(
        element for element in root.iter() if _xml_local_name(element.tag) == "testcase"
    )
    all_testcases_skipped = bool(testcases) and all(
        any(_xml_local_name(child.tag) == "skipped" for child in testcase) for testcase in testcases
    )
    contains_failure_or_error = any(
        _xml_local_name(element.tag) in {"failure", "error"} for element in root.iter()
    )
    suite_results_reconciled = bool(result_suites) and all(
        _suite_results_reconcile(suite, path=path) for suite in result_suites
    )
    return JUnitEvidence(
        tests=tests,
        skipped=skipped,
        failures=failures,
        errors=errors,
        testcase_count=len(testcases),
        all_testcases_skipped=all_testcases_skipped,
        contains_failure_or_error=contains_failure_or_error,
        suite_results_reconciled=suite_results_reconciled,
    )


def junit_test_count(path: Path) -> int:
    """Parse a pytest JUnit document and require evidence of at least one test."""

    return junit_evidence(path).tests


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

        evidence: JUnitEvidence | None = None
        evidence_error: JUnitEvidenceError | None = None
        try:
            evidence = junit_evidence(paths["junit"])
            total_tests += evidence.tests
        except JUnitEvidenceError as exc:
            evidence_error = exc
            print(
                f"[ci-shard {shard_number}/{shard_count}] {exc}",
                file=sys.stderr,
                flush=True,
            )
        accepted_return_code = return_code == 0 or (
            return_code == PYTEST_EXIT_NO_TESTS_COLLECTED
            and evidence is not None
            and evidence.all_skipped
        )
        if not accepted_return_code or evidence_error is not None:
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
