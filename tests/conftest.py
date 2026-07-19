from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTEST_TEMP_ROOT = ROOT / ".pytest-tmp"
BASE_TEMP_SOURCE_ATTR = "_poker_basetemp_source"
BASE_TEMP_REQUEST_ATTR = "_poker_basetemp_request"


def pytest_configure(config: pytest.Config) -> None:
    """Keep default pytest temp data writable, ignored, and isolated per process."""

    explicit_basetemp = getattr(config.option, "basetemp", None)
    if explicit_basetemp:
        setattr(config.option, BASE_TEMP_SOURCE_ATTR, "caller")
        setattr(config.option, BASE_TEMP_REQUEST_ATTR, str(explicit_basetemp))
        return
    session_name = f"s-{os.getpid():x}-{uuid4().hex[:6]}"
    PYTEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(PYTEST_TEMP_ROOT / session_name)
    setattr(config.option, BASE_TEMP_SOURCE_ATTR, "automatic")
    setattr(config.option, BASE_TEMP_REQUEST_ATTR, None)
