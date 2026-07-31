"""Repository-owned synthetic child for the P2-028A Windows backend.

This file deliberately imports only the Python standard library so that it can
run under ``-I -S``.  Its command surface is a closed enum plus bounded integer
arguments; it is not a general process launcher.
"""

import _winapi
import msvcrt
import os
import sys
import time

OPERATIONS = (
    "success",
    "known_failure",
    "hang",
    "spawn_tree",
    "stdout_flood",
    "stderr_flood",
    "memory_pressure",
    "cpu_spin",
    "copy_handles",
    "stdin_eof",
    "module_inventory",
)

_FLAGS = frozenset(
    {
        "--protocol",
        "--operation",
        "--duration-ms",
        "--output-bytes",
        "--memory-bytes",
        "--child-count",
        "--exit-code",
        "--input-handle",
    }
)
_OPERATION_VALUE_FLAGS = {
    "success": frozenset(),
    "known_failure": frozenset({"--exit-code"}),
    "hang": frozenset({"--duration-ms"}),
    "spawn_tree": frozenset({"--duration-ms", "--child-count"}),
    "stdout_flood": frozenset({"--output-bytes"}),
    "stderr_flood": frozenset({"--output-bytes"}),
    "memory_pressure": frozenset({"--memory-bytes", "--duration-ms"}),
    "cpu_spin": frozenset({"--duration-ms"}),
    "copy_handles": frozenset({"--input-handle"}),
    "stdin_eof": frozenset(),
    "module_inventory": frozenset(),
}
_INTEGER_BOUNDS = {
    "--duration-ms": (1, 600_000),
    "--output-bytes": (1, 64 * 1024 * 1024),
    "--memory-bytes": (1, 4 * 1024 * 1024 * 1024),
    "--child-count": (1, 8),
    "--exit-code": (1, 125),
    "--input-handle": (0, 2**63 - 1),
}


def _arguments() -> dict[str, str]:
    argv = sys.argv[1:]
    if len(argv) % 2:
        raise ValueError("synthetic arguments must be flag/value pairs")
    parsed: dict[str, str] = {}
    for index in range(0, len(argv), 2):
        flag = argv[index]
        value = argv[index + 1]
        if flag not in _FLAGS or flag in parsed or not value:
            raise ValueError("synthetic argument is unknown, duplicate, or empty")
        parsed[flag] = value
    if parsed.get("--protocol") != "1.0.0":
        raise ValueError("unsupported synthetic protocol")
    operation = parsed.get("--operation")
    if operation not in OPERATIONS:
        raise ValueError("unknown synthetic operation")
    assert operation is not None
    expected_flags = {"--protocol", "--operation"} | set(_OPERATION_VALUE_FLAGS[operation])
    if set(parsed) != expected_flags:
        raise ValueError("synthetic operation arguments do not match its closed matrix")
    for flag, (minimum, maximum) in _INTEGER_BOUNDS.items():
        if flag in parsed:
            integer = int(parsed[flag])
            if integer < minimum or integer > maximum:
                raise ValueError("synthetic integer argument is out of bounds")
    return parsed


def _write_flood(file_descriptor: int, byte_count: int) -> None:
    block = b"x" * 65_536
    remaining = byte_count
    while remaining:
        chunk = block[: min(remaining, len(block))]
        os.write(file_descriptor, chunk)
        remaining -= len(chunk)


def _write_text(file_descriptor: int, value: str) -> None:
    os.write(file_descriptor, value.encode("utf-8"))


def _sleep(duration_ms: int) -> None:
    time.sleep(duration_ms / 1000)


def _spawn_tree(duration_ms: int, child_count: int) -> int:
    command = [
        sys.executable,
        "-I",
        "-S",
        "-B",
        "-u",
        "-X",
        "utf8",
        os.path.abspath(__file__),
        "--protocol",
        "1.0.0",
        "--operation",
        "hang",
        "--duration-ms",
        str(duration_ms),
    ]
    spawned = 0
    for _index in range(child_count):
        try:
            process_handle = os.spawnv(os.P_NOWAIT, sys.executable, command)
        except OSError:
            return 73
        _winapi.CloseHandle(process_handle)
        spawned += 1
    _write_text(
        sys.stdout.fileno(),
        f"spawned={spawned}\n",
    )
    _sleep(duration_ms)
    return 0


def _copy_handle(handle: int) -> None:
    file_descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY)
    try:
        while True:
            chunk = os.read(file_descriptor, 65_536)
            if not chunk:
                break
            os.write(sys.stdout.fileno(), chunk)
    finally:
        os.close(file_descriptor)


def main() -> int:
    try:
        arguments = _arguments()
        operation = arguments["--operation"]
    except (KeyError, ValueError):
        return 72
    if operation == "success":
        _write_text(sys.stdout.fileno(), "ok\n")
        return 0
    if operation == "known_failure":
        _write_text(sys.stderr.fileno(), "known failure\n")
        return int(arguments["--exit-code"])
    if operation == "hang":
        _sleep(int(arguments["--duration-ms"]))
        return 0
    if operation == "spawn_tree":
        return _spawn_tree(
            int(arguments["--duration-ms"]),
            int(arguments["--child-count"]),
        )
    if operation == "stdout_flood":
        _write_flood(sys.stdout.fileno(), int(arguments["--output-bytes"]))
        return 0
    if operation == "stderr_flood":
        _write_flood(sys.stderr.fileno(), int(arguments["--output-bytes"]))
        return 0
    if operation == "memory_pressure":
        requested = int(arguments["--memory-bytes"])
        blocks: list[bytearray] = []
        allocated = 0
        try:
            while allocated < requested:
                size = min(1024 * 1024, requested - allocated)
                blocks.append(bytearray(size))
                allocated += size
            _sleep(int(arguments["--duration-ms"]))
            return 0
        except MemoryError:
            return 70
    if operation == "cpu_spin":
        deadline = time.monotonic_ns() + int(arguments["--duration-ms"]) * 1_000_000
        value = 0
        while time.monotonic_ns() < deadline:
            value = (value * 33 + 17) & 0xFFFFFFFF
        _write_text(sys.stdout.fileno(), f"{value}\n")
        return 0
    if operation == "copy_handles":
        _copy_handle(int(arguments["--input-handle"]))
        return 0
    if operation == "stdin_eof":
        data = os.read(sys.stdin.fileno(), 1)
        _write_text(
            sys.stdout.fileno(),
            "eof\n" if data == b"" else "unexpected-input\n",
        )
        return 0 if data == b"" else 71
    if operation == "module_inventory":
        non_frozen_modules = []
        for name, module in sorted(sys.modules.items()):
            specification = getattr(module, "__spec__", None)
            origin = None if specification is None else specification.origin
            if origin not in {None, "built-in", "frozen"}:
                non_frozen_modules.append(name)
        _write_text(
            sys.stdout.fileno(),
            "\n".join(non_frozen_modules) + "\n",
        )
        return 0
    return 72


if __name__ == "__main__":
    raise SystemExit(main())
