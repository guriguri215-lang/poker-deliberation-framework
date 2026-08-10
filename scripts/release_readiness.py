"""Repository script for candidate-bound, offline-first pre-release build evidence."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import site
import subprocess
import sys
import tarfile
import tempfile
import venv
import zipfile
from collections.abc import Mapping
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from poker_deliberation.public_preflight import run_preflight  # type: ignore[import-untyped]

RELEASE_EVIDENCE_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
LICENSE_INVENTORY_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
SUPPORTED_OPERATING_SYSTEMS = ("ubuntu-latest", "windows-latest")
SUPPORTED_PYTHON_VERSIONS = ("3.12", "3.13")
WORKFLOW_PATH = ".github/workflows/quality.yml"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_GIT_OID_PATTERN = r"^[0-9a-f]{40,64}$"
_LOCK_PATTERN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)$")


class ReleaseReadinessError(RuntimeError):
    """Raised when candidate-bound release evidence cannot be established."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class CandidateBindingV1(_StrictModel):
    commit: str = Field(pattern=_GIT_OID_PATTERN)
    tree: str = Field(pattern=_GIT_OID_PATTERN)


class EnvironmentEvidenceV1(_StrictModel):
    operating_system: str
    python_implementation: str
    python_version: str


class MatrixEvidenceV1(_StrictModel):
    operating_systems: tuple[str, ...]
    python_versions: tuple[str, ...]
    workflow_path: str
    workflow_sha256: str = Field(pattern=_SHA256_PATTERN)
    status: Literal["defined_by_workflow"]


class CommandEvidenceV1(_StrictModel):
    name: str
    command: str
    status: Literal["passed"]


class ArtifactEvidenceV1(_StrictModel):
    filename: str
    kind: Literal["wheel", "sdist"]
    sha256: str = Field(pattern=_SHA256_PATTERN)
    size_bytes: int = Field(gt=0)


class ArchiveEvidenceV1(_StrictModel):
    filename: str
    kind: Literal["wheel", "sdist"]
    package_data_present: bool
    cli_entry_point_present: bool
    project_metadata_consistent: bool
    root_license_present: bool
    forbidden_paths_absent: bool


class LockedLicenseRecordV1(_StrictModel):
    name: str
    normalized_name: str
    locked_version: str
    installed_version: str | None
    license_expression: str | None
    license_source: Literal["license-expression", "license-field", "classifier", "unavailable"]
    status: Literal["known", "unknown", "not_installed", "version_mismatch"]


class LicenseInventoryV1(_StrictModel):
    schema_version: Literal["1.0.0"]
    requirements_lock_sha256: str = Field(pattern=_SHA256_PATTERN)
    pyproject_sha256: str = Field(pattern=_SHA256_PATTERN)
    records: tuple[LockedLicenseRecordV1, ...]
    unknown_packages: tuple[str, ...]
    all_locked_versions_match: bool


class ReproducibilityEvidenceV1(_StrictModel):
    source_date_epoch: int = Field(ge=0)
    second_build_equal: bool
    artifacts: tuple[ArtifactEvidenceV1, ...]


class OfflineInstallEvidenceV1(_StrictModel):
    isolation_mode: Literal["venv-with-qualified-base-site-packages"]
    project_install_no_index: bool
    project_install_no_deps: bool
    cli_help_passed: bool
    doctor_passed: bool
    local_only_smoke_passed: bool


class PreflightSummaryV1(_StrictModel):
    pass_count: int = Field(ge=0)
    review_count: int = Field(ge=0)
    fail_count: int = Field(ge=0)
    unknown_count: int = Field(ge=0)


class PublicPreflightEvidenceV1(_StrictModel):
    report_filename: str
    report_sha256: str = Field(pattern=_SHA256_PATTERN)
    publication_decision: Literal["human_review_required"]
    summary: PreflightSummaryV1


class ReleaseEvidenceManifestV1(_StrictModel):
    schema_version: Literal["1.0.0"]
    candidate: CandidateBindingV1
    environment: EnvironmentEvidenceV1
    matrix: MatrixEvidenceV1
    reproducibility: ReproducibilityEvidenceV1
    archives: tuple[ArchiveEvidenceV1, ...]
    offline_install: OfflineInstallEvidenceV1
    license_inventory_filename: str
    license_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    license_inventory: LicenseInventoryV1
    public_preflight: PublicPreflightEvidenceV1
    commands: tuple[CommandEvidenceV1, ...]
    result: Literal["passed"]


def canonical_json_bytes(value: BaseModel | dict[str, object]) -> bytes:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def parse_requirements_lock(path: Path) -> tuple[tuple[str, str], ...]:
    if not path.is_file():
        raise ReleaseReadinessError("requirements.lock is missing")
    records: list[tuple[str, str]] = []
    normalized_seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _LOCK_PATTERN.fullmatch(stripped)
        if match is None:
            raise ReleaseReadinessError(
                f"requirements.lock line {line_number} is not an exact name==version pin"
            )
        name = match.group("name")
        version = match.group("version")
        normalized = _normalized_distribution_name(name)
        if normalized in normalized_seen:
            raise ReleaseReadinessError(f"duplicate locked distribution: {normalized}")
        normalized_seen.add(normalized)
        records.append((name, version))
    if not records:
        raise ReleaseReadinessError("requirements.lock contains no exact pins")
    return tuple(records)


def _metadata_license(metadata: importlib.metadata.PackageMetadata) -> tuple[str | None, str]:
    metadata_mapping = cast(Mapping[str, str], metadata)
    expression = metadata_mapping.get("License-Expression")
    if expression and expression.strip().upper() != "UNKNOWN":
        return " ".join(expression.split()), "license-expression"
    license_field = metadata_mapping.get("License")
    if license_field and license_field.strip().upper() != "UNKNOWN":
        normalized = " ".join(license_field.split())
        return normalized[:500], "license-field"
    classifiers = metadata.get_all("Classifier", [])
    license_classifiers = sorted(
        item.removeprefix("License :: ") for item in classifiers if item.startswith("License :: ")
    )
    if license_classifiers:
        return "; ".join(license_classifiers)[:500], "classifier"
    return None, "unavailable"


def build_license_inventory(repo: Path) -> LicenseInventoryV1:
    lock_path = repo / "requirements.lock"
    pyproject_path = repo / "pyproject.toml"
    if not pyproject_path.is_file():
        raise ReleaseReadinessError("pyproject.toml is missing")
    records: list[LockedLicenseRecordV1] = []
    unknown: list[str] = []
    versions_match = True
    for name, locked_version in parse_requirements_lock(lock_path):
        normalized = _normalized_distribution_name(name)
        try:
            distribution = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            versions_match = False
            unknown.append(normalized)
            records.append(
                LockedLicenseRecordV1(
                    name=name,
                    normalized_name=normalized,
                    locked_version=locked_version,
                    installed_version=None,
                    license_expression=None,
                    license_source="unavailable",
                    status="not_installed",
                )
            )
            continue
        license_expression, license_source = _metadata_license(distribution.metadata)
        if distribution.version != locked_version:
            status = "version_mismatch"
            versions_match = False
            unknown.append(normalized)
        elif license_expression is None:
            status = "unknown"
            unknown.append(normalized)
        else:
            status = "known"
        records.append(
            LockedLicenseRecordV1(
                name=name,
                normalized_name=normalized,
                locked_version=locked_version,
                installed_version=distribution.version,
                license_expression=license_expression,
                license_source=license_source,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
            )
        )
    return LicenseInventoryV1(
        schema_version=LICENSE_INVENTORY_SCHEMA_VERSION,
        requirements_lock_sha256=sha256_file(lock_path),
        pyproject_sha256=sha256_file(pyproject_path),
        records=tuple(sorted(records, key=lambda item: item.normalized_name)),
        unknown_packages=tuple(sorted(set(unknown))),
        all_locked_versions_match=versions_match,
    )


def _run(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        stdout = completed.stdout.decode("utf-8", errors="replace")[-4000:]
        stderr = completed.stderr.decode("utf-8", errors="replace")[-4000:]
        raise ReleaseReadinessError(
            f"command failed ({completed.returncode}): {' '.join(args)}\n{stdout}\n{stderr}"
        )
    return completed


def _git_text(repo: Path, *args: str) -> str:
    return _run(["git", *args], cwd=repo).stdout.decode("ascii").strip()


def candidate_binding(repo: Path) -> tuple[CandidateBindingV1, int]:
    dirty = _git_text(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty:
        raise ReleaseReadinessError("tracked worktree must be clean before evidence generation")
    commit = _git_text(repo, "rev-parse", "HEAD")
    tree = _git_text(repo, "show", "-s", "--format=%T", "HEAD")
    source_date_epoch = int(_git_text(repo, "show", "-s", "--format=%ct", "HEAD"))
    return CandidateBindingV1(commit=commit, tree=tree), source_date_epoch


def _safe_extract_git_archive(archive: bytes, destination: Path) -> None:
    archive_path = destination / "source.tar"
    archive_path.write_bytes(archive)
    source = destination / "source"
    source.mkdir()
    with tarfile.open(archive_path, mode="r:") as stream:
        for member in sorted(stream.getmembers(), key=lambda item: item.name):
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ReleaseReadinessError("git archive contains an unsafe member")
            target = source.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise ReleaseReadinessError("git archive contains a non-regular member")
            extracted = stream.extractfile(member)
            if extracted is None:
                raise ReleaseReadinessError("git archive member could not be read")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.read())
            target.chmod(member.mode & 0o777)


def normalize_sdist(path: Path, source_date_epoch: int) -> None:
    canonical_path = path.with_suffix(path.suffix + ".canonical")
    with tarfile.open(path, mode="r:gz") as source:
        members = sorted(source.getmembers(), key=lambda item: item.name)
        with (
            canonical_path.open("wb") as raw_output,
            gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                mtime=source_date_epoch,
            ) as compressed,
            tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as output,
        ):
            for member in members:
                if not member.isfile() and not member.isdir():
                    raise ReleaseReadinessError("sdist contains a non-regular archive member")
                normalized = tarfile.TarInfo(member.name)
                normalized.type = member.type
                normalized.mode = 0o755 if member.isdir() else 0o644
                normalized.mtime = source_date_epoch
                normalized.uid = 0
                normalized.gid = 0
                normalized.uname = ""
                normalized.gname = ""
                if member.isfile():
                    extracted = source.extractfile(member)
                    if extracted is None:
                        raise ReleaseReadinessError("sdist member could not be read")
                    payload = extracted.read()
                    normalized.size = len(payload)
                    output.addfile(normalized, io.BytesIO(payload))
                else:
                    output.addfile(normalized)
    os.replace(canonical_path, path)


def _build_once(
    repo: Path,
    commit: str,
    source_date_epoch: int,
    destination: Path,
) -> tuple[ArtifactEvidenceV1, ...]:
    with tempfile.TemporaryDirectory(prefix="poker-release-source-") as raw_temp:
        temp = Path(raw_temp)
        archive = _run(["git", "archive", "--format=tar", commit], cwd=repo).stdout
        _safe_extract_git_archive(archive, temp)
        source = temp / "source"
        destination.mkdir(parents=True, exist_ok=False)
        build_script = (
            "from pathlib import Path; import sys; "
            "from setuptools.build_meta import build_sdist, build_wheel; "
            "out=str(Path(sys.argv[1]).resolve()); "
            "print(build_sdist(out)); print(build_wheel(out))"
        )
        env = os.environ.copy()
        env["SOURCE_DATE_EPOCH"] = str(source_date_epoch)
        env["PYTHONHASHSEED"] = "0"
        _run([sys.executable, "-c", build_script, str(destination)], cwd=source, env=env)
    for path in destination.iterdir():
        if path.name.endswith(".tar.gz"):
            normalize_sdist(path, source_date_epoch)
    artifacts: list[ArtifactEvidenceV1] = []
    for path in sorted(destination.iterdir(), key=lambda item: item.name):
        if path.suffix == ".whl":
            kind = "wheel"
        elif path.name.endswith(".tar.gz"):
            kind = "sdist"
        else:
            raise ReleaseReadinessError(f"unexpected build artifact: {path.name}")
        artifacts.append(
            ArtifactEvidenceV1(
                filename=path.name,
                kind=kind,  # type: ignore[arg-type]
                sha256=sha256_file(path),
                size_bytes=path.stat().st_size,
            )
        )
    if [item.kind for item in artifacts].count("wheel") != 1 or [
        item.kind for item in artifacts
    ].count("sdist") != 1:
        raise ReleaseReadinessError("build must produce exactly one wheel and one sdist")
    return tuple(artifacts)


def _archive_forbidden(paths: list[str]) -> bool:
    forbidden = {".git", ".venv", "build", "dist", "runs", "tmp", "user_materials"}
    for raw_path in paths:
        parts = PurePosixPath(raw_path).parts
        relevant = parts[1:] if len(parts) > 1 else parts
        if any(part in forbidden for part in relevant):
            return False
    return True


def inspect_wheel(path: Path) -> ArchiveEvidenceV1:
    with zipfile.ZipFile(path) as archive:
        names = sorted(archive.namelist())
        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        license_names = [
            name
            for name in names
            if ".dist-info/" in name and PurePosixPath(name).name.upper() == "LICENSE"
        ]
        if len(metadata_names) != 1:
            raise ReleaseReadinessError("wheel must contain exactly one METADATA file")
        metadata = BytesParser().parsebytes(archive.read(metadata_names[0]))
        entry_text = archive.read(entry_names[0]).decode("utf-8") if len(entry_names) == 1 else ""
    metadata_consistent = (
        metadata.get("Name") == "poker-deliberation-framework"
        and metadata.get("Version") == "0.1.0"
        and metadata.get("Requires-Python") == ">=3.11"
        and (metadata.get("License-Expression") == "MIT" or metadata.get("License") == "MIT")
    )
    return ArchiveEvidenceV1(
        filename=path.name,
        kind="wheel",
        package_data_present="poker_deliberation/roadmap_status.json" in names,
        cli_entry_point_present=(
            len(entry_names) == 1 and "poker-deliberate = poker_deliberation.cli:main" in entry_text
        ),
        project_metadata_consistent=metadata_consistent,
        root_license_present=bool(license_names),
        forbidden_paths_absent=_archive_forbidden(names),
    )


def inspect_sdist(path: Path) -> ArchiveEvidenceV1:
    with tarfile.open(path, mode="r:gz") as archive:
        names = sorted(member.name for member in archive.getmembers() if member.isfile())
    roots = {PurePosixPath(name).parts[0] for name in names if PurePosixPath(name).parts}
    if len(roots) != 1:
        raise ReleaseReadinessError("sdist must contain exactly one root directory")
    root = next(iter(roots))
    required = {
        f"{root}/LICENSE",
        f"{root}/MANIFEST.in",
        f"{root}/README.md",
        f"{root}/pyproject.toml",
        f"{root}/requirements.lock",
        f"{root}/src/poker_deliberation/roadmap_status.json",
    }
    return ArchiveEvidenceV1(
        filename=path.name,
        kind="sdist",
        package_data_present=f"{root}/src/poker_deliberation/roadmap_status.json" in names,
        cli_entry_point_present=f"{root}/pyproject.toml" in names,
        project_metadata_consistent=required.issubset(names),
        root_license_present=f"{root}/LICENSE" in names,
        forbidden_paths_absent=_archive_forbidden(names),
    )


def inspect_archives(directory: Path) -> tuple[ArchiveEvidenceV1, ...]:
    evidence: list[ArchiveEvidenceV1] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if path.suffix == ".whl":
            evidence.append(inspect_wheel(path))
        elif path.name.endswith(".tar.gz"):
            evidence.append(inspect_sdist(path))
    if len(evidence) != 2:
        raise ReleaseReadinessError("expected exactly two inspected archives")
    for item in evidence:
        if not all(
            (
                item.package_data_present,
                item.cli_entry_point_present,
                item.project_metadata_consistent,
                item.root_license_present,
                item.forbidden_paths_absent,
            )
        ):
            raise ReleaseReadinessError(f"archive validation failed: {item.filename}")
    return tuple(evidence)


def _venv_paths(root: Path) -> tuple[Path, Path]:
    if os.name == "nt":
        return root / "Scripts" / "python.exe", root / "Scripts" / "poker-deliberate.exe"
    return root / "bin" / "python", root / "bin" / "poker-deliberate"


def offline_install_smoke(wheel: Path) -> OfflineInstallEvidenceV1:
    with tempfile.TemporaryDirectory(prefix="poker-release-install-") as raw_temp:
        temp = Path(raw_temp)
        environment = temp / "venv"
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
        python, entry_point = _venv_paths(environment)
        env = os.environ.copy()
        env.pop("OPENAI_API_KEY", None)
        _run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--no-deps",
                str(wheel.resolve()),
            ],
            cwd=temp,
            env=env,
        )
        qualified_base_sites = [
            Path(candidate)
            for candidate in site.getsitepackages()
            if (Path(candidate) / "pydantic").is_dir()
        ]
        if len(qualified_base_sites) != 1:
            raise ReleaseReadinessError(
                "expected one current locked site-packages directory containing pydantic"
            )
        target_site_result = _run(
            [str(python), "-c", "import site; print(site.getsitepackages()[0])"],
            cwd=temp,
            env=env,
        )
        target_site = Path(target_site_result.stdout.decode("utf-8").strip())
        (target_site / "qualified-base-site-packages.pth").write_text(
            str(qualified_base_sites[0].resolve()) + "\n",
            encoding="utf-8",
        )
        help_result = _run([str(entry_point), "--help"], cwd=temp, env=env)
        doctor_result = _run([str(entry_point), "doctor", "--format", "json"], cwd=temp, env=env)
        doctor = json.loads(doctor_result.stdout.decode("utf-8"))
        local_only_script = (
            "import socket; "
            "socket.socket.connect=lambda *_a,**_k: (_ for _ in ()).throw("
            "AssertionError('network attempted')); "
            "from poker_deliberation.codex_bridge.contracts import build_runtime_policy; "
            "from poker_deliberation.codex_bridge.models import RuntimeAuthModeV1; "
            "p=build_runtime_policy(auth_mode=RuntimeAuthModeV1.LOCAL_ONLY); "
            "assert p.network_allowed is False and p.credential_value_access == 'none'"
        )
        _run([str(python), "-c", local_only_script], cwd=temp, env=env)
    return OfflineInstallEvidenceV1(
        isolation_mode="venv-with-qualified-base-site-packages",
        project_install_no_index=True,
        project_install_no_deps=True,
        cli_help_passed=b"doctor" in help_result.stdout,
        doctor_passed=doctor.get("status") == "ok",
        local_only_smoke_passed=True,
    )


def _write_canonical_new(path: Path, value: BaseModel | dict[str, object]) -> str:
    if path.exists():
        raise ReleaseReadinessError(f"refusing to overwrite evidence: {path.name}")
    payload = canonical_json_bytes(value) + b"\n"
    path.write_bytes(payload)
    return hashlib.sha256(payload).hexdigest()


def build_release_evidence(repo: Path, output_dir: Path) -> ReleaseEvidenceManifestV1:
    repo = repo.resolve()
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise ReleaseReadinessError("output directory must be absent or empty")
    output_dir.mkdir(parents=True, exist_ok=True)
    binding, source_date_epoch = candidate_binding(repo)
    workflow = repo / WORKFLOW_PATH
    if not workflow.is_file():
        raise ReleaseReadinessError(f"missing workflow: {WORKFLOW_PATH}")

    first_dir = output_dir / "artifacts"
    second_dir = output_dir / "reproducibility-check"
    first = _build_once(repo, binding.commit, source_date_epoch, first_dir)
    second = _build_once(repo, binding.commit, source_date_epoch, second_dir)
    first_identity = [(item.filename, item.sha256, item.size_bytes) for item in first]
    second_identity = [(item.filename, item.sha256, item.size_bytes) for item in second]
    if first_identity != second_identity:
        raise ReleaseReadinessError("two clean builds did not produce identical artifacts")

    archives = inspect_archives(first_dir)
    wheel = next(first_dir / item.filename for item in first if item.kind == "wheel")
    install = offline_install_smoke(wheel)
    if not all((install.cli_help_passed, install.doctor_passed, install.local_only_smoke_passed)):
        raise ReleaseReadinessError("offline install smoke did not pass")

    licenses = build_license_inventory(repo)
    if not licenses.all_locked_versions_match:
        raise ReleaseReadinessError("installed environment does not match requirements.lock")
    license_filename = "license-inventory.json"
    license_sha256 = _write_canonical_new(output_dir / license_filename, licenses)

    preflight = run_preflight(repo)
    preflight_filename = "public-preflight.json"
    preflight_sha256 = _write_canonical_new(output_dir / preflight_filename, preflight)
    raw_summary = preflight.get("summary")
    if not isinstance(raw_summary, dict):
        raise ReleaseReadinessError("public preflight summary is missing")
    summary = PreflightSummaryV1(
        pass_count=int(raw_summary.get("pass", 0)),
        review_count=int(raw_summary.get("review", 0)),
        fail_count=int(raw_summary.get("fail", 0)),
        unknown_count=int(raw_summary.get("unknown", 0)),
    )
    if summary.fail_count:
        raise ReleaseReadinessError("public preflight reported a failing check")

    manifest = ReleaseEvidenceManifestV1(
        schema_version=RELEASE_EVIDENCE_SCHEMA_VERSION,
        candidate=binding,
        environment=EnvironmentEvidenceV1(
            operating_system=platform.system(),
            python_implementation=platform.python_implementation(),
            python_version=platform.python_version(),
        ),
        matrix=MatrixEvidenceV1(
            operating_systems=SUPPORTED_OPERATING_SYSTEMS,
            python_versions=SUPPORTED_PYTHON_VERSIONS,
            workflow_path=WORKFLOW_PATH,
            workflow_sha256=sha256_file(workflow),
            status="defined_by_workflow",
        ),
        reproducibility=ReproducibilityEvidenceV1(
            source_date_epoch=source_date_epoch,
            second_build_equal=True,
            artifacts=first,
        ),
        archives=archives,
        offline_install=install,
        license_inventory_filename=license_filename,
        license_inventory_sha256=license_sha256,
        license_inventory=licenses,
        public_preflight=PublicPreflightEvidenceV1(
            report_filename=preflight_filename,
            report_sha256=preflight_sha256,
            publication_decision="human_review_required",
            summary=summary,
        ),
        commands=(
            CommandEvidenceV1(
                name="clean-build-twice",
                command="setuptools.build_meta build_sdist + build_wheel from git archive",
                status="passed",
            ),
            CommandEvidenceV1(
                name="offline-project-install",
                command="python -m pip install --no-index --no-deps <wheel>",
                status="passed",
            ),
            CommandEvidenceV1(
                name="cli-help",
                command="poker-deliberate --help",
                status="passed",
            ),
            CommandEvidenceV1(
                name="doctor",
                command="poker-deliberate doctor --format json",
                status="passed",
            ),
            CommandEvidenceV1(
                name="local-only-smoke",
                command="installed-package local_only policy smoke with network blocked",
                status="passed",
            ),
            CommandEvidenceV1(
                name="public-preflight",
                command="python scripts/public_preflight.py --repo . --format json",
                status="passed",
            ),
        ),
        result="passed",
    )
    _write_canonical_new(output_dir / "release-evidence.json", manifest)
    return manifest


def validate_release_evidence(path: Path) -> ReleaseEvidenceManifestV1:
    payload = path.read_bytes()
    parsed = ReleaseEvidenceManifestV1.model_validate_json(payload, strict=True)
    if payload.rstrip(b"\r\n") != canonical_json_bytes(parsed):
        raise ReleaseReadinessError("release evidence is not strict canonical JSON")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--output-dir", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = build_release_evidence(args.repo, args.output_dir)
    print(canonical_json_bytes(manifest).decode("utf-8"))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
