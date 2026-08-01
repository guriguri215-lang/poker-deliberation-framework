from __future__ import annotations

import base64
import hashlib
import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Generator
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYTEST_TEMP_ROOT = ROOT / ".pytest-tmp"
WINDOWS_SHORT_TEMP_ROOT = Path(tempfile.gettempdir()) / "poker-deliberation-tests"
BASE_TEMP_SOURCE_ATTR = "_poker_basetemp_source"
BASE_TEMP_REQUEST_ATTR = "_poker_basetemp_request"
PYTEST_SESSION_TOKEN_ATTR = "_poker_session_token"


def _remove_readonly(
    function: Callable[[str], object],
    path: str,
    error: BaseException,
) -> None:
    if not isinstance(error, PermissionError):
        raise error
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _windows_tmp_target_name(*, session_token: str, nodeid: str) -> str:
    """Return a short path component isolated by process and test node."""

    return hashlib.sha256(f"{session_token}\0{nodeid}".encode()).hexdigest()[:12]


def pytest_configure(config: pytest.Config) -> None:
    """Keep default pytest temp data writable, ignored, and isolated per process."""

    session_token = base64.urlsafe_b64encode(uuid4().bytes[:3]).decode("ascii")
    setattr(config, PYTEST_SESSION_TOKEN_ATTR, session_token)
    explicit_basetemp = getattr(config.option, "basetemp", None)
    if explicit_basetemp:
        setattr(config.option, BASE_TEMP_SOURCE_ATTR, "caller")
        setattr(config.option, BASE_TEMP_REQUEST_ATTR, str(explicit_basetemp))
        return
    session_name = f"s-{session_token}"
    PYTEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    config.option.basetemp = str(PYTEST_TEMP_ROOT / session_name)
    setattr(config.option, BASE_TEMP_SOURCE_ATTR, "automatic")
    setattr(config.option, BASE_TEMP_REQUEST_ATTR, None)


if os.name == "nt":

    @pytest.fixture
    def tmp_path(
        request: pytest.FixtureRequest,
    ) -> Generator[Path, None, None]:
        """Keep Windows test paths below the conservative storage path limit."""

        session_token = getattr(request.config, PYTEST_SESSION_TOKEN_ATTR)
        root = WINDOWS_SHORT_TEMP_ROOT.resolve()
        root.mkdir(parents=True, exist_ok=True)
        target = (
            root
            / _windows_tmp_target_name(
                session_token=session_token,
                nodeid=request.node.nodeid,
            )
        ).resolve()
        if target.parent != root:
            raise RuntimeError("hashed pytest directory escaped its configured short root")
        if target.exists():
            shutil.rmtree(target, onexc=_remove_readonly)
        target.mkdir(parents=True, exist_ok=False)
        try:
            yield target
        finally:
            if target.exists() and target.parent == root:
                shutil.rmtree(target, onexc=_remove_readonly)
