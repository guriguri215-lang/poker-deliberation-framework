"""Capped one-shot subprocess transport for the official Codex Python SDK worker."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, cast

from poker_deliberation.codex_bridge.canonical import canonical_json_bytes, domain_sha256
from poker_deliberation.codex_bridge.models import (
    BRIDGE_MODEL_ID,
    BRIDGE_OPENAI_API_CREDENTIAL_REFERENCE,
    BRIDGE_OPENAI_API_PROVIDER_ID,
    BRIDGE_OPENAI_API_RUNTIME_ID,
    BRIDGE_REASONING_EFFORT,
    BRIDGE_SERVICE_TIER,
    MAX_CONTEXT_BYTES,
    MAX_ROLE_RUNTIME_MS,
    MAX_STREAM_BYTES,
    BoundedCodexBridgeRequestV1,
    BridgeEffectState,
    BridgeTransportUsageV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.runtime_scratch import (
    PreparedRuntimeRoot,
    RuntimeScratchIdentityError,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    BridgeTransportFailureEvidence,
    BridgeTransportResult,
)
from poker_deliberation.storage.revision_lock import verify_directory

_WORKER_OUTPUT_CAP = MAX_STREAM_BYTES
_WORKER_STDERR_CAP = 65_536
_EVENT_KEYS = {
    "ready": {
        "schema_version",
        "event",
        "runtime_identity",
        "server_name",
        "server_version",
    },
    "turn_launch_intent": {
        "schema_version",
        "event",
        "thread_id_sha256",
        "launched_at",
    },
    "turn_launched": {
        "schema_version",
        "event",
        "thread_id_sha256",
        "turn_id_sha256",
        "launched_at",
    },
    "result": {
        "schema_version",
        "event",
        "runtime_identity",
        "observed_model",
        "observed_model_provider",
        "observed_reasoning_effort",
        "observed_service_tier",
        "response_base64",
        "completed_at",
        "duration_ms",
        "stream_bytes",
        "item_types",
        "usage",
    },
    "failure": {
        "schema_version",
        "event",
        "reason_code",
        "turn_launched",
        "completed_at",
        "duration_ms",
        "stream_bytes",
        "item_types",
        "runtime_identity",
        "observed_model",
        "observed_model_provider",
        "observed_reasoning_effort",
        "observed_service_tier",
        "response_bytes",
        "usage",
    },
}


class _CappedReader(threading.Thread):
    def __init__(self, stream: BinaryIO, maximum: int) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.maximum = maximum
        self.data = bytearray()
        self.total = 0
        self.overflow = threading.Event()

    def run(self) -> None:
        while True:
            chunk = self.stream.read(4096)
            if not chunk:
                return
            self.total += len(chunk)
            if len(self.data) < self.maximum:
                remaining = self.maximum - len(self.data)
                self.data.extend(chunk[:remaining])
            if self.total > self.maximum:
                self.overflow.set()


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("worker timestamp is absent")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValueError("worker timestamp is not UTC")
    return parsed


def _failure_evidence(value: dict[str, object]) -> BridgeTransportFailureEvidence:
    usage_value = value["usage"]
    usage = (
        None
        if usage_value is None
        else BridgeTransportUsageV1.model_validate(usage_value, strict=True)
    )
    response_bytes = value["response_bytes"]
    if response_bytes is not None and (
        not isinstance(response_bytes, int)
        or isinstance(response_bytes, bool)
        or response_bytes < 0
    ):
        raise ValueError("worker failure response byte count is invalid")
    identity = (
        value["runtime_identity"],
        value["observed_model"],
        value["observed_model_provider"],
        value["observed_reasoning_effort"],
        value["observed_service_tier"],
    )
    expected = (
        BRIDGE_OPENAI_API_RUNTIME_ID,
        BRIDGE_MODEL_ID,
        BRIDGE_OPENAI_API_PROVIDER_ID,
        BRIDGE_REASONING_EFFORT,
        BRIDGE_SERVICE_TIER,
    )
    if any(item is not None and not isinstance(item, str) for item in identity):
        raise ValueError("worker failure observed identity is invalid")
    if identity != (None, None, None, None, None) and identity != expected:
        raise ValueError("worker failure observed identity is not allowlisted")
    return BridgeTransportFailureEvidence(
        usage=usage,
        response_bytes=response_bytes,
        runtime_identity=identity[0],
        model_identity_evidence=("direct_observation" if identity == expected else "unavailable"),
        observed_model=identity[1],
        observed_model_provider=identity[2],
        observed_reasoning_effort=identity[3],
        observed_service_tier=identity[4],
    )


def _parse_worker_lines(data: bytes) -> tuple[list[dict[str, object]], int]:
    lines = data.splitlines()
    if not lines or data.endswith(b"\n") is False:
        raise ValueError("worker protocol is incomplete")
    events: list[dict[str, object]] = []
    for line in lines:
        value = json.loads(line)
        if not isinstance(value, dict) or canonical_json_bytes(value) != line:
            raise ValueError("worker protocol is noncanonical")
        event = value.get("event")
        if (
            not isinstance(event, str)
            or event not in _EVENT_KEYS
            or set(value) != _EVENT_KEYS[event]
            or value.get("schema_version") != "1.0.0"
        ):
            raise ValueError("worker protocol schema is not exact")
        events.append(value)
    return events, len(data)


def _parse_worker_prefix(data: bytes) -> list[dict[str, object]]:
    """Recover only complete, canonical protocol events before a malformed suffix."""

    events: list[dict[str, object]] = []
    for line in data.splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break
        raw = line[:-1]
        try:
            value = json.loads(raw)
        except Exception:
            break
        if not isinstance(value, dict) or canonical_json_bytes(value) != raw:
            break
        event = value.get("event")
        if (
            not isinstance(event, str)
            or event not in _EVENT_KEYS
            or set(value) != _EVENT_KEYS[event]
            or value.get("schema_version") != "1.0.0"
        ):
            break
        events.append(value)
    return events


class OpenAIAPITransport:
    """Explicit optional API contract adapter; live execution is fail-closed."""

    auth_mode = RuntimeAuthModeV1.OPENAI_API
    transport_qualification: Literal["deterministic_fixture"] = "deterministic_fixture"
    api_live_qualified: Literal[False] = False
    price_authority_version: None = None
    provider_hard_cost_stop: Literal[False] = False

    def __init__(
        self,
        runtime_root: Path,
        *,
        repository_root: Path | None = None,
        repository_commit_id: str | None = None,
        repository_tree_id: str | None = None,
        credential_env_name: str = "OPENAI_API_KEY",
        worker_command_factory: Callable[[Path, Path], list[str]] | None = None,
        runtime_capability: PreparedRuntimeRoot | None = None,
        runtime_capability_factory: Callable[[], PreparedRuntimeRoot] | None = None,
    ) -> None:
        if f"env:{credential_env_name}" != BRIDGE_OPENAI_API_CREDENTIAL_REFERENCE:
            raise ValueError("credential reference is outside the bridge contract")
        self.runtime_root = runtime_root.resolve(strict=False)
        self.runtime_capability = runtime_capability
        self.runtime_capability_factory = runtime_capability_factory
        if runtime_capability is not None and runtime_capability_factory is not None:
            raise ValueError("runtime capability and factory are mutually exclusive")
        if runtime_capability is not None and runtime_capability.path != self.runtime_root:
            raise ValueError("runtime capability path mismatch")
        self.repository_root = repository_root
        self.repository_commit_id = repository_commit_id
        self.repository_tree_id = repository_tree_id
        self.credential_env_name = credential_env_name
        if worker_command_factory is None and (
            repository_root is None or repository_commit_id is None or repository_tree_id is None
        ):
            raise ValueError("default Codex worker requires an exact repository identity")
        self.worker_command_factory = worker_command_factory or self._default_command

    def _default_command(self, codex_home: Path, cwd: Path) -> list[str]:
        assert self.repository_root is not None
        assert self.repository_commit_id is not None
        assert self.repository_tree_id is not None
        return [
            sys.executable,
            "-I",
            "-m",
            "poker_deliberation.codex_bridge.sdk_worker",
            "--codex-home",
            str(codex_home),
            "--cwd",
            str(cwd),
            "--repository-root",
            str(self.repository_root),
            "--repository-commit",
            self.repository_commit_id,
            "--repository-tree",
            self.repository_tree_id,
        ]

    def _attempt_paths(self, request: BoundedCodexBridgeRequestV1) -> tuple[Path, Path]:
        assignment = request.context.assignment
        key = domain_sha256(
            "poker-bounded-codex-worker-attempt-v1",
            {
                "bridge_run_id": assignment.bridge_run_id,
                "assignment_id": assignment.assignment_id,
                "attempt_id": assignment.attempt_id,
                "request_sha256": request.request_sha256,
            },
        )[:32]
        attempt = self.runtime_root / key
        return attempt / "home", attempt / "cwd"

    def _begin_runtime_capability(self) -> None:
        if self.runtime_capability is None and self.runtime_capability_factory is not None:
            factory = self.runtime_capability_factory
            self.runtime_capability_factory = None
            try:
                capability = factory()
                if capability.path != self.runtime_root:
                    raise RuntimeScratchIdentityError("runtime scratch capability path changed")
            except Exception as exc:
                raise BridgeTransportFailure(
                    "runtime_scratch_identity_changed",
                    effect_state=BridgeEffectState.NOT_LAUNCHED,
                    launched_at=None,
                    completed_at=datetime.now(UTC),
                    duration_ms=0,
                    stream_bytes=0,
                ) from exc
            self.runtime_capability = capability
        if self.runtime_capability is None:
            return
        try:
            if self.runtime_capability.path != self.runtime_root:
                raise RuntimeScratchIdentityError("runtime scratch capability path changed")
            self.runtime_capability.begin()
        except RuntimeScratchIdentityError as exc:
            raise BridgeTransportFailure(
                "runtime_scratch_identity_changed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            ) from exc

    def _verify_runtime_capability(self, *, process_started: bool = False) -> None:
        if self.runtime_capability is None:
            return
        try:
            if self.runtime_capability.path != self.runtime_root:
                raise RuntimeScratchIdentityError("runtime scratch capability path changed")
            self.runtime_capability.verify_active()
        except RuntimeScratchIdentityError as exc:
            raise BridgeTransportFailure(
                "runtime_scratch_identity_changed",
                effect_state=(
                    BridgeEffectState.EFFECT_UNKNOWN
                    if process_started
                    else BridgeEffectState.NOT_LAUNCHED
                ),
                launched_at=datetime.now(UTC) if process_started else None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            ) from exc

    def _finish_runtime_capability(self) -> None:
        if self.runtime_capability is None:
            return
        try:
            if self.runtime_capability.path != self.runtime_root:
                raise RuntimeScratchIdentityError("runtime scratch capability path changed")
            self.runtime_capability.finish()
        except RuntimeScratchIdentityError as exc:
            raise BridgeTransportFailure(
                "runtime_scratch_identity_changed",
                effect_state=BridgeEffectState.EFFECT_UNKNOWN,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            ) from exc

    def _create_attempt(self, request: BoundedCodexBridgeRequestV1) -> tuple[Path, Path]:
        self._begin_runtime_capability()
        if self.runtime_capability is None:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
        verify_directory(self.runtime_root)
        self._verify_runtime_capability()
        home, cwd = self._attempt_paths(request)
        attempt = home.parent
        if attempt.exists():
            raise BridgeTransportFailure(
                "runtime_attempt_reuse",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )
        attempt.mkdir()
        home.mkdir()
        cwd.mkdir()
        verify_directory(attempt)
        verify_directory(home)
        verify_directory(cwd)
        self._verify_runtime_capability()
        return home, cwd

    def _require_credential_reference(self) -> None:
        if self.credential_env_name not in os.environ:
            raise BridgeTransportFailure(
                "missing_auth",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )

    @staticmethod
    def _require_api_live_qualification() -> None:
        """Fail closed until a versioned price authority and provider hard stop exist."""

        raise BridgeTransportFailure(
            "api_live_execution_unqualified_cost_authority",
            effect_state=BridgeEffectState.NOT_LAUNCHED,
            launched_at=None,
            completed_at=datetime.now(UTC),
            duration_ms=0,
            stream_bytes=0,
        )

    @staticmethod
    def _credential_launcher_command(command: list[str]) -> list[str]:
        if not command:
            raise ValueError("API worker command is empty")
        executable = Path(command[0]).resolve(strict=True)
        if executable != Path(sys.executable).resolve(strict=True):
            raise ValueError("API worker must use the current Python runtime")
        arguments = command[1:]
        if not (
            (len(arguments) >= 3 and arguments[:2] == ["-I", "-m"])
            or (arguments and not arguments[0].startswith("-"))
        ):
            raise ValueError("API worker command shape is not allowed")
        return [
            sys.executable,
            "-I",
            "-m",
            "poker_deliberation.codex_bridge.credential_launcher",
            "--",
            *command,
        ]

    @staticmethod
    def _terminate_worker(process: subprocess.Popen[bytes]) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass

    def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
        if request.auth_mode is not self.auth_mode:
            raise BridgeTransportFailure(
                "transport_auth_mode_mismatch",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )
        self._require_credential_reference()
        self._require_api_live_qualification()
        started = time.monotonic()
        proposed_home, proposed_cwd = self._attempt_paths(request)
        try:
            command = self._credential_launcher_command(
                self.worker_command_factory(proposed_home, proposed_cwd)
            )
        except Exception as exc:
            raise BridgeTransportFailure(
                "worker_process_launch_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=0,
            ) from exc
        home, cwd = self._create_attempt(request)
        if (home, cwd) != (proposed_home, proposed_cwd):  # pragma: no cover - pure helper
            raise AssertionError("API attempt path changed during launch")
        process: subprocess.Popen[bytes] | None = None
        try:
            self._verify_runtime_capability()
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=None,
            )
        except Exception as exc:
            raise BridgeTransportFailure(
                "worker_process_launch_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=0,
            ) from exc
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = _CappedReader(cast(BinaryIO, process.stdout), _WORKER_OUTPUT_CAP)
        stderr = _CappedReader(cast(BinaryIO, process.stderr), _WORKER_STDERR_CAP)
        stdout.start()
        stderr.start()
        request_bytes = canonical_json_bytes(request)
        if len(request_bytes) > MAX_CONTEXT_BYTES:
            self._terminate_worker(process)
            raise BridgeTransportFailure(
                "worker_input_cap_exceeded",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )
        try:
            process.stdin.write(request_bytes)
            process.stdin.close()
        except Exception as exc:
            self._terminate_worker(process)
            raise BridgeTransportFailure(
                "worker_input_write_failed",
                effect_state=BridgeEffectState.EFFECT_UNKNOWN,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=stdout.total,
            ) from exc
        deadline = started + (MAX_ROLE_RUNTIME_MS / 1000)
        timed_out = False
        while process.poll() is None:
            if stdout.overflow.is_set() or stderr.overflow.is_set():
                self._terminate_worker(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._terminate_worker(process)
                break
            time.sleep(0.02)
        stdout.join(timeout=2)
        stderr.join(timeout=2)
        self._verify_runtime_capability(process_started=True)
        try:
            events, protocol_bytes = _parse_worker_lines(bytes(stdout.data))
            protocol_valid = True
        except Exception:
            events = _parse_worker_prefix(bytes(stdout.data))
            protocol_bytes = stdout.total
            protocol_valid = False
        intent_events = [item for item in events if item.get("event") == "turn_launch_intent"]
        launched_events = [item for item in events if item.get("event") == "turn_launched"]
        thread_id_sha256: str | None = None
        turn_id_sha256: str | None = None
        if len(launched_events) == 1:
            thread_value = launched_events[0].get("thread_id_sha256")
            turn_value = launched_events[0].get("turn_id_sha256")
            if (
                isinstance(thread_value, str)
                and isinstance(turn_value, str)
                and re.fullmatch(r"[0-9a-f]{64}", thread_value) is not None
                and re.fullmatch(r"[0-9a-f]{64}", turn_value) is not None
            ):
                thread_id_sha256 = thread_value
                turn_id_sha256 = turn_value
        try:
            intent_at = (
                _parse_time(intent_events[0].get("launched_at"))
                if len(intent_events) == 1
                else None
            )
            launched_at = (
                _parse_time(launched_events[0].get("launched_at"))
                if len(launched_events) == 1
                else intent_at
            )
        except Exception as exc:
            raise BridgeTransportFailure(
                "worker_launch_event_invalid",
                effect_state=BridgeEffectState.EFFECT_UNKNOWN,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=protocol_bytes,
            ) from exc
        intent_thread_sha256 = (
            intent_events[0].get("thread_id_sha256") if len(intent_events) == 1 else None
        )
        launch_protocol_correlates = (
            len(intent_events) == 1
            and isinstance(intent_thread_sha256, str)
            and re.fullmatch(r"[0-9a-f]{64}", intent_thread_sha256) is not None
            and (
                len(launched_events) == 0
                or (
                    intent_thread_sha256 == thread_id_sha256
                    and intent_at is not None
                    and launched_at == intent_at
                )
            )
        )
        if timed_out:
            raise BridgeTransportFailure(
                "worker_timeout",
                effect_state=(
                    BridgeEffectState.CANCEL_UNCONFIRMED
                    if len(launched_events) == 1
                    else BridgeEffectState.EFFECT_UNKNOWN
                ),
                launched_at=launched_at,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=protocol_bytes,
                thread_id_sha256=thread_id_sha256,
                turn_id_sha256=turn_id_sha256,
            )
        if stdout.overflow.is_set() or stderr.overflow.is_set():
            raise BridgeTransportFailure(
                "worker_output_cap_exceeded",
                effect_state=(
                    BridgeEffectState.CANCEL_UNCONFIRMED
                    if len(launched_events) == 1
                    else BridgeEffectState.EFFECT_UNKNOWN
                ),
                launched_at=launched_at,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=protocol_bytes,
                thread_id_sha256=thread_id_sha256,
                turn_id_sha256=turn_id_sha256,
            )
        ready = [item for item in events if item.get("event") == "ready"]
        result_events = [item for item in events if item.get("event") == "result"]
        failure_events = [item for item in events if item.get("event") == "failure"]
        if failure_events and len(failure_events) == 1 and not result_events:
            failure = failure_events[0]
            after_launch = (
                failure.get("turn_launched") is True
                or len(intent_events) == 1
                or len(launched_events) == 1
            )
            reason = failure.get("reason_code")
            item_types = failure.get("item_types")
            if (
                not isinstance(reason, str)
                or not isinstance(item_types, list)
                or not all(isinstance(item, str) for item in item_types)
            ):
                reason = "worker_failure_protocol_invalid"
                item_types = []
            duration_value = failure.get("duration_ms")
            duration_ms = duration_value if isinstance(duration_value, int) else None
            try:
                evidence = _failure_evidence(failure)
            except Exception:
                reason = "worker_failure_protocol_invalid"
                evidence = None
            raise BridgeTransportFailure(
                reason,
                effect_state=(
                    BridgeEffectState.EFFECT_UNKNOWN
                    if after_launch
                    else BridgeEffectState.NOT_LAUNCHED
                ),
                launched_at=launched_at or (datetime.now(UTC) if after_launch else None),
                completed_at=_parse_time(failure.get("completed_at")),
                duration_ms=duration_ms,
                stream_bytes=protocol_bytes,
                item_types=tuple(sorted(item_types)),
                thread_id_sha256=thread_id_sha256,
                turn_id_sha256=turn_id_sha256,
                evidence=evidence,
            )
        if (
            not protocol_valid
            or process.returncode != 0
            or len(ready) != 1
            or not launch_protocol_correlates
            or len(launched_events) != 1
            or len(result_events) != 1
            or failure_events
            or ready[0].get("runtime_identity") != BRIDGE_OPENAI_API_RUNTIME_ID
            or ready[0].get("server_name") != "Codex Desktop"
            or not isinstance(ready[0].get("server_version"), str)
            or re.fullmatch(
                r"0\.144\.4(?:[ +(-].*)?",
                cast(str, ready[0].get("server_version")),
            )
            is None
        ):
            raise BridgeTransportFailure(
                "worker_protocol_or_exit_mismatch",
                effect_state=BridgeEffectState.EFFECT_UNKNOWN,
                launched_at=launched_at,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=protocol_bytes,
                thread_id_sha256=thread_id_sha256,
                turn_id_sha256=turn_id_sha256,
            )
        result = result_events[0]
        try:
            response_encoded = result["response_base64"]
            if not isinstance(response_encoded, str):
                raise ValueError("response is not base64 text")
            response = base64.b64decode(response_encoded, validate=True)
            usage_value = result["usage"]
            if not isinstance(usage_value, dict):
                raise ValueError("worker usage is not an object")
            usage = BridgeTransportUsageV1.model_validate(usage_value, strict=True)
            item_types_value = result["item_types"]
            if not isinstance(item_types_value, list) or not all(
                isinstance(item, str) for item in item_types_value
            ):
                raise ValueError("worker item types are invalid")
            completed_at = _parse_time(result["completed_at"])
            duration = result["duration_ms"]
            stream_bytes = result["stream_bytes"]
            runtime_identity = result["runtime_identity"]
            observed_model = result["observed_model"]
            observed_model_provider = result["observed_model_provider"]
            observed_reasoning_effort = result["observed_reasoning_effort"]
            observed_service_tier = result["observed_service_tier"]
            if (
                not isinstance(duration, int)
                or not isinstance(stream_bytes, int)
                or not isinstance(runtime_identity, str)
                or not isinstance(observed_model, str)
                or not isinstance(observed_model_provider, str)
                or not isinstance(observed_reasoning_effort, str)
                or not isinstance(observed_service_tier, str)
                or runtime_identity != BRIDGE_OPENAI_API_RUNTIME_ID
            ):
                raise ValueError("worker result scalar type mismatch")
        except Exception as exc:
            raise BridgeTransportFailure(
                "worker_result_protocol_invalid",
                effect_state=BridgeEffectState.EFFECT_UNKNOWN,
                launched_at=launched_at,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=protocol_bytes,
                thread_id_sha256=thread_id_sha256,
                turn_id_sha256=turn_id_sha256,
            ) from exc
        if launched_at is None or thread_id_sha256 is None or turn_id_sha256 is None:
            raise BridgeTransportFailure(
                "worker_launch_event_missing",
                effect_state=BridgeEffectState.EFFECT_UNKNOWN,
                launched_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=protocol_bytes,
            )
        transport_result = BridgeTransportResult(
            auth_mode=self.auth_mode,
            transport_qualification=self.transport_qualification,
            response_bytes=response,
            usage=usage,
            model_identity_evidence="direct_observation",
            observed_model=observed_model,
            observed_model_provider=observed_model_provider,
            observed_reasoning_effort=observed_reasoning_effort,
            observed_service_tier=observed_service_tier,
            runtime_identity=runtime_identity,
            thread_id_sha256=thread_id_sha256,
            turn_id_sha256=turn_id_sha256,
            launched_at=launched_at,
            completed_at=completed_at,
            duration_ms=duration,
            stream_bytes=stream_bytes,
            item_types=tuple(item_types_value),
        )
        self._finish_runtime_capability()
        return transport_result


__all__ = ["OpenAIAPITransport"]
