from __future__ import annotations

from pathlib import Path

import pytest
from conftest import BASE_TEMP_REQUEST_ATTR, BASE_TEMP_SOURCE_ATTR

ROOT = Path(__file__).resolve().parents[2]


def test_pytest_temp_respects_caller_or_uses_workspace_session(
    pytestconfig: pytest.Config,
) -> None:
    basetemp = Path(str(pytestconfig.option.basetemp)).resolve()
    source = getattr(pytestconfig.option, BASE_TEMP_SOURCE_ATTR)
    requested = getattr(pytestconfig.option, BASE_TEMP_REQUEST_ATTR)

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
