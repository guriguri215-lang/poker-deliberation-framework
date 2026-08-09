"""Name-only environment filter before the optional official API runtime starts.

This helper deliberately never indexes or retrieves the API-key value. It removes
all environment names outside a fixed allowlist, then lets the operating system
inherit the remaining environment into the official runtime worker.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

_ALLOWED_ENVIRONMENT_NAMES = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "LOCALAPPDATA",
        "OPENAI_API_KEY",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "WINDIR",
    }
)


def _emit_launch_failure() -> None:
    payload = {
        "completed_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "duration_ms": 0,
        "event": "failure",
        "item_types": [],
        "reason_code": "worker_process_launch_failed",
        "schema_version": "1.0.0",
        "stream_bytes": 0,
        "turn_launched": False,
    }
    data = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    sys.stdout.buffer.write(data + b"\n")
    sys.stdout.buffer.flush()


def main() -> int:
    try:
        separator = sys.argv.index("--")
    except ValueError:
        return 64
    command = sys.argv[separator + 1 :]
    if not command or "OPENAI_API_KEY" not in os.environ:
        return 64
    for name in tuple(os.environ):
        if name not in _ALLOWED_ENVIRONMENT_NAMES:
            del os.environ[name]
    os.environ.update(
        {
            "NO_COLOR": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    try:
        if Path(command[0]).resolve(strict=True) != Path(sys.executable).resolve(strict=True):
            raise OSError("credential launcher accepts only the current Python runtime")
        arguments = command[1:]
        if len(arguments) >= 3 and arguments[:2] == ["-I", "-m"]:
            module = arguments[2]
            sys.argv = [module, *arguments[3:]]
            runpy.run_module(module, run_name="__main__", alter_sys=False)
        elif arguments and not arguments[0].startswith("-"):
            script = str(Path(arguments[0]).resolve(strict=True))
            sys.argv = [script, *arguments[1:]]
            runpy.run_path(script, run_name="__main__")
        else:
            raise OSError("credential launcher command shape is not allowed")
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 1
    except OSError:
        _emit_launch_failure()
        return 70
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
