from __future__ import annotations

from pathlib import Path

import yaml

from scripts.release_readiness import (
    SUPPORTED_OPERATING_SYSTEMS,
    SUPPORTED_PYTHON_VERSIONS,
    WORKFLOW_PATH,
)

ROOT = Path(__file__).resolve().parents[2]


def test_quality_workflow_has_one_full_test_matrix_and_separate_package_evidence() -> None:
    workflow_path = ROOT / WORKFLOW_PATH
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    triggers = workflow.get("on", workflow.get(True))
    jobs = workflow["jobs"]
    matrix = jobs["test-matrix"]["strategy"]["matrix"]

    assert triggers == {
        "pull_request": None,
        "push": {"branches": ["main"]},
        "workflow_dispatch": None,
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert tuple(matrix["os"]) == SUPPORTED_OPERATING_SYSTEMS
    assert tuple(matrix["python-version"]) == SUPPORTED_PYTHON_VERSIONS
    assert "python -m pytest" in workflow_text
    assert workflow_text.count("Run full test suite once per matrix row") == 1
    assert "scripts/release_readiness.py" in workflow_text
    assert "actions/upload-artifact@v4" in workflow_text
    assert workflow_text.count("ref: ${{ github.event.pull_request.head.sha || github.sha }}") == 3
    assert workflow_text.count("fetch-depth: 0") == 3
    assert "--output-dir build/release-evidence" in workflow_text
