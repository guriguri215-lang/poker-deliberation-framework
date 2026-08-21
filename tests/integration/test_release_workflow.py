from __future__ import annotations

from pathlib import Path

import yaml

from scripts.release_readiness import (
    SUPPORTED_OPERATING_SYSTEMS,
    SUPPORTED_PYTHON_VERSIONS,
    WORKFLOW_PATH,
)
from scripts.run_ci_test_shard import discover_test_files, partition_test_files

ROOT = Path(__file__).resolve().parents[2]


def test_quality_workflow_has_sharded_test_matrix_and_separate_package_evidence() -> None:
    workflow_path = ROOT / WORKFLOW_PATH
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    triggers = workflow.get("on", workflow.get(True))
    jobs = workflow["jobs"]
    test_job = jobs["test-matrix"]
    matrix = test_job["strategy"]["matrix"]
    shard_test_step = test_job["steps"][-1]

    assert triggers == {
        "pull_request": None,
        "push": {"branches": ["main"]},
        "workflow_dispatch": None,
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["static-quality"]["runs-on"] == "windows-latest"
    assert test_job["name"] == (
        "test (${{ matrix.os }}, Python ${{ matrix.python-version }}, shard ${{ matrix.shard }}/3)"
    )
    assert test_job["timeout-minutes"] == 330
    assert test_job["strategy"]["fail-fast"] is False
    assert set(matrix) == {"os", "python-version", "shard"}
    assert tuple(matrix["os"]) == SUPPORTED_OPERATING_SYSTEMS
    assert tuple(matrix["python-version"]) == SUPPORTED_PYTHON_VERSIONS
    assert tuple(matrix["shard"]) == (1, 2, 3)
    assert len(matrix["os"]) * len(matrix["python-version"]) * len(matrix["shard"]) == 12
    assert shard_test_step == {
        "name": "Run deterministic test shard in fresh pytest processes",
        "run": (
            "python scripts/run_ci_test_shard.py "
            "--shard-number ${{ matrix.shard }} --shard-count 3 "
            '--temp-root "${{ runner.temp }}/pdt-'
            '${{ matrix.python-version }}-${{ matrix.shard }}"'
        ),
    }
    assert (ROOT / "scripts" / "run_ci_test_shard.py").is_file()

    expected_checkout = {
        "uses": "actions/checkout@v4",
        "with": {
            "fetch-depth": 0,
            "ref": "${{ github.event.pull_request.head.sha || github.sha }}",
        },
    }
    for job_name in ("test-matrix", "static-quality", "package-evidence"):
        assert jobs[job_name]["steps"][0] == expected_checkout

    focused_test_step = jobs["static-quality"]["steps"][-2]
    assert focused_test_step == {
        "name": "Run focused release contract tests",
        "run": (
            "python -m pytest tests/unit/test_release_readiness.py "
            "tests/unit/test_ci_test_shard.py "
            "tests/integration/test_release_workflow.py "
            "tests/integration/test_roadmap_status.py"
        ),
    }
    assert "scripts/release_readiness.py" in workflow_text
    assert "actions/upload-artifact@v4" in workflow_text
    assert workflow_text.count("ref: ${{ github.event.pull_request.head.sha || github.sha }}") == 3
    assert workflow_text.count("fetch-depth: 0") == 3
    assert "--output-dir build/release-evidence" in workflow_text


def test_current_test_inventory_is_covered_once_by_three_shards() -> None:
    test_files = discover_test_files(ROOT)
    partitions = tuple(
        partition_test_files(test_files, shard_number=shard_number, shard_count=3)
        for shard_number in (1, 2, 3)
    )
    flattened = tuple(path for partition in partitions for path in partition)

    assert test_files
    assert all(partitions)
    assert len(flattened) == len(test_files)
    assert set(flattened) == set(test_files)
    assert len(flattened) == len(set(flattened))
