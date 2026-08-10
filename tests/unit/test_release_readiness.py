from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.release_readiness import (
    CandidateBindingV1,
    ReleaseReadinessError,
    build_license_inventory,
    inspect_sdist,
    inspect_wheel,
    parse_requirements_lock,
)

ROOT = Path(__file__).resolve().parents[2]


def test_requirements_lock_is_exact_unique_and_matches_installed_versions() -> None:
    pins = parse_requirements_lock(ROOT / "requirements.lock")
    inventory = build_license_inventory(ROOT)

    assert len(pins) == len(inventory.records)
    assert inventory.all_locked_versions_match is True
    assert inventory.requirements_lock_sha256 != inventory.pyproject_sha256
    assert all(record.installed_version == record.locked_version for record in inventory.records)


@pytest.mark.parametrize(
    "content",
    (
        "pydantic>=2\n",
        "pydantic==2.13.4\nPydantic==2.13.4\n",
        "name==1; python_version > '3.11'\n",
    ),
)
def test_requirements_lock_rejects_noncanonical_or_duplicate_pins(
    tmp_path: Path, content: str
) -> None:
    path = tmp_path / "requirements.lock"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ReleaseReadinessError):
        parse_requirements_lock(path)


def test_candidate_binding_schema_is_strict() -> None:
    with pytest.raises(ValidationError):
        CandidateBindingV1.model_validate(
            {"commit": "a" * 40, "tree": "b" * 40, "local_path": "C:/private"},
            strict=True,
        )


def test_wheel_archive_requires_metadata_entry_point_license_and_package_data(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "poker_deliberation_framework-0.1.0-py3-none-any.whl"
    dist_info = "poker_deliberation_framework-0.1.0.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("poker_deliberation/roadmap_status.json", "{}")
        archive.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: poker-deliberation-framework\n"
            "Version: 0.1.0\n"
            "Requires-Python: >=3.11\n"
            "License-Expression: MIT\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\npoker-deliberate = poker_deliberation.cli:main\n",
        )
        archive.writestr(f"{dist_info}/licenses/LICENSE", "MIT License\n")

    evidence = inspect_wheel(wheel)

    assert evidence.package_data_present is True
    assert evidence.cli_entry_point_present is True
    assert evidence.project_metadata_consistent is True
    assert evidence.root_license_present is True
    assert evidence.forbidden_paths_absent is True


def test_sdist_archive_requires_declared_release_files_and_excludes_local_data(
    tmp_path: Path,
) -> None:
    sdist = tmp_path / "poker_deliberation_framework-0.1.0.tar.gz"
    root = "poker_deliberation_framework-0.1.0"
    required = (
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "pyproject.toml",
        "requirements.lock",
        "src/poker_deliberation/roadmap_status.json",
    )
    with tarfile.open(sdist, "w:gz") as archive:
        for relative in required:
            payload = b"fixture\n"
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    evidence = inspect_sdist(sdist)

    assert evidence.package_data_present is True
    assert evidence.project_metadata_consistent is True
    assert evidence.root_license_present is True
    assert evidence.forbidden_paths_absent is True


def test_sdist_archive_rejects_local_intermediate_data(tmp_path: Path) -> None:
    sdist = tmp_path / "candidate.tar.gz"
    root = "candidate"
    required = (
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "pyproject.toml",
        "requirements.lock",
        "src/poker_deliberation/roadmap_status.json",
        "tmp/private-evidence.json",
    )
    with tarfile.open(sdist, "w:gz") as archive:
        for relative in required:
            payload = b"fixture\n"
            info = tarfile.TarInfo(f"{root}/{relative}")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))

    evidence = inspect_sdist(sdist)

    assert evidence.forbidden_paths_absent is False
