from __future__ import annotations

import os
from pathlib import Path

import pytest
from conftest import (
    BASE_TEMP_REQUEST_ATTR,
    BASE_TEMP_SOURCE_ATTR,
    PYTEST_SESSION_TOKEN_ATTR,
    _windows_tmp_target_name,
)

ROOT = Path(__file__).resolve().parents[2]


def test_pytest_temp_respects_caller_or_uses_workspace_session(
    pytestconfig: pytest.Config,
) -> None:
    session_token = getattr(pytestconfig, PYTEST_SESSION_TOKEN_ATTR)
    basetemp = Path(str(pytestconfig.option.basetemp)).resolve()
    source = getattr(pytestconfig.option, BASE_TEMP_SOURCE_ATTR)
    requested = getattr(pytestconfig.option, BASE_TEMP_REQUEST_ATTR)
    assert len(session_token) == 4

    if source == "caller":
        assert requested is not None
        assert basetemp == Path(str(requested)).resolve()
        return

    assert source == "automatic"
    assert requested is None
    expected_root = (ROOT / ".pytest-tmp").resolve()
    assert expected_root in basetemp.parents
    assert basetemp.name.startswith("s-")
    assert basetemp != expected_root


def test_pytest_temp_root_is_ignored() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".pytest-tmp/" in gitignore
    assert not (ROOT / ".pytest-tmp").is_file()


def test_windows_tmp_target_is_isolated_between_pytest_processes() -> None:
    nodeid = "tests/integration/test_range_equity_evaluation.py::test_same_node"

    first = _windows_tmp_target_name(session_token="AAAA", nodeid=nodeid)
    second = _windows_tmp_target_name(session_token="BBBB", nodeid=nodeid)

    assert first != second
    assert first == _windows_tmp_target_name(session_token="AAAA", nodeid=nodeid)
    assert len(first) == 12


@pytest.mark.skipif(os.name != "nt", reason="Windows short tmp_path override only")
def test_windows_tmp_path_contains_the_pytest_process_token(
    tmp_path: Path,
    pytestconfig: pytest.Config,
) -> None:
    session_token = getattr(pytestconfig, PYTEST_SESSION_TOKEN_ATTR)

    assert tmp_path.name == _windows_tmp_target_name(
        session_token=session_token,
        nodeid=(
            "tests/integration/test_quality_config.py"
            "::test_windows_tmp_path_contains_the_pytest_process_token"
        ),
    )
