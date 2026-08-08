"""Pinned one-turn transport using saved ChatGPT/Codex subscription authentication."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, cast

from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    domain_sha256,
    sha256_bytes,
)
from poker_deliberation.codex_bridge.contracts import (
    outbound_request_bytes,
    role_output_schema_for_request,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_MODEL_ID,
    BRIDGE_REASONING_EFFORT,
    BRIDGE_RUNTIME_BINARY_SHA256,
    BRIDGE_SERVICE_TIER,
    BRIDGE_SUBSCRIPTION_PROVIDER_ID,
    BRIDGE_SUBSCRIPTION_RUNTIME_ID,
    MAX_CONTEXT_BYTES,
    MAX_ROLE_RUNTIME_MS,
    MAX_STREAM_BYTES,
    BoundedCodexBridgeRequestV1,
    BridgeEffectState,
    BridgeTransportUsageV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    BridgeTransportFailureEvidence,
    BridgeTransportResult,
)
from poker_deliberation.storage.revision_lock import (
    verify_directory,
    verify_regular_single_link,
)

_STDERR_CAP = 65_536
_OS_ENVIRONMENT_NAMES = (
    "COMSPEC",
    "PATHEXT",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "WINDIR",
)
_MAX_DISCOVERED_SKILLS = 256
_MAX_SKILL_BYTES = 262_144
_MAX_SKILL_TOTAL_BYTES = 2_097_152
_DISABLED_FEATURES = (
    "apps",
    "auth_elicitation",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "code_mode",
    "code_mode_host",
    "computer_use",
    "goals",
    "guardian_approval",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "network_proxy",
    "plugins",
    "remote_plugin",
    "shell_snapshot",
    "shell_tool",
    "skill_mcp_dependency_install",
    "tool_call_mcp_elicitation",
    "tool_suggest",
    "unified_exec",
    "workspace_dependencies",
)
_ALLOWED_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "item.started",
    "item.completed",
    "turn.completed",
}
_ALLOWED_ITEM_TYPES = {"agent_message", "reasoning"}


@dataclass(frozen=True, slots=True)
class _SkillState:
    configuration_path: Path
    size: int
    modified_ns: int
    file_identity: int
    content_sha256: str


@dataclass(frozen=True, slots=True)
class _SubscriptionProcessContext:
    cwd: Path
    home: Path
    appdata: Path
    local_appdata: Path
    temporary: Path
    codex_home: Path


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
                self.data.extend(chunk[: self.maximum - len(self.data)])
            if self.total > self.maximum:
                self.overflow.set()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _parse_events(data: bytes) -> list[dict[str, object]]:
    if not data or not data.endswith(b"\n"):
        raise ValueError("Codex JSONL is incomplete")
    events: list[dict[str, object]] = []
    for line in data.splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("Codex JSONL event is not an object")
        events.append(value)
    return events


def _thread_from_prefix(data: bytes) -> tuple[str | None, bool, tuple[str, ...]]:
    thread: str | None = None
    launched = False
    item_types: set[str] = set()
    for line in data.splitlines():
        try:
            value = json.loads(line)
        except Exception:
            break
        if not isinstance(value, dict):
            break
        event_type = value.get("type")
        if event_type == "thread.started" and isinstance(value.get("thread_id"), str):
            thread = cast(str, value["thread_id"])
        elif event_type == "turn.started":
            launched = True
        elif event_type in {"item.started", "item.completed"}:
            item = value.get("item")
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                item_types.add(cast(str, item["type"]))
    return thread, launched, tuple(sorted(item_types, key=lambda item: item.encode("utf-8")))


def _required_usage_int(value: dict[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise ValueError(f"Codex usage field is invalid: {key}")
    return item


class CodexSubscriptionCliTransport:
    """No-fallback saved-login transport for one fresh ephemeral Codex exec turn."""

    auth_mode = RuntimeAuthModeV1.CODEX_SUBSCRIPTION
    transport_qualification: Literal["actual_live"] = "actual_live"

    def __init__(
        self,
        runtime_root: Path,
        *,
        codex_binary: Path,
        auth_status_probe: Callable[[Path, dict[str, str]], bool] | None = None,
        command_factory: Callable[[Path, Path, Path], list[str]] | None = None,
        isolation_root: Path | None = None,
        credential_codex_home: Path | None = None,
    ) -> None:
        self.runtime_root = runtime_root.resolve(strict=False)
        self.codex_binary = codex_binary.resolve(strict=True)
        if _file_sha256(self.codex_binary) != BRIDGE_RUNTIME_BINARY_SHA256:
            raise ValueError("bundled Codex binary hash mismatch")
        self.auth_status_probe = auth_status_probe
        self.command_factory = command_factory

        configured_codex_home = credential_codex_home
        if configured_codex_home is None:
            raw_codex_home = os.environ.get("CODEX_HOME")
            raw_user_home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
            if raw_codex_home:
                configured_codex_home = Path(raw_codex_home)
            elif raw_user_home:
                configured_codex_home = Path(raw_user_home) / ".codex"
            else:
                raise ValueError("saved-login CODEX_HOME cannot be resolved")
        self.credential_codex_home = configured_codex_home.resolve(strict=False)

        root = isolation_root
        if root is None:
            root = Path(tempfile.gettempdir()) / "poker-deliberation-codex-subscription-v1"
        self.isolation_root = root.resolve(strict=False)
        if self._paths_overlap(self.runtime_root, self.isolation_root):
            raise ValueError("subscription execution context must be separate from raw traces")
        repository_root = self._repository_root(self.runtime_root)
        if repository_root is not None and self.isolation_root.is_relative_to(repository_root):
            raise ValueError("subscription execution context must be outside the repository")

    @staticmethod
    def _paths_overlap(first: Path, second: Path) -> bool:
        return first == second or first.is_relative_to(second) or second.is_relative_to(first)

    @staticmethod
    def _repository_root(path: Path) -> Path | None:
        for candidate in (path, *path.parents):
            if (candidate / ".git").exists():
                return candidate
        return None

    def _environment(self, context: _SubscriptionProcessContext) -> dict[str, str]:
        environment = {
            name: os.environ[name] for name in _OS_ENVIRONMENT_NAMES if name in os.environ
        }
        environment.update(
            {
                "APPDATA": str(context.appdata),
                "CODEX_HOME": str(context.codex_home),
                "HOME": str(context.home),
                "LOCALAPPDATA": str(context.local_appdata),
                "NO_COLOR": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "TEMP": str(context.temporary),
                "TMP": str(context.temporary),
                "USERPROFILE": str(context.home),
            }
        )
        # OPENAI_API_KEY and CODEX_API_KEY are intentionally absent. Their presence in the
        # parent process can never select or authenticate subscription mode.
        return environment

    def _process_context(self, key: str) -> _SubscriptionProcessContext:
        try:
            self.isolation_root.mkdir(parents=True, exist_ok=True)
            verify_directory(self.isolation_root)
            root = self.isolation_root / key
            if root.exists():
                raise BridgeTransportFailure(
                    "subscription_context_attempt_reuse",
                    effect_state=BridgeEffectState.NOT_LAUNCHED,
                    launched_at=None,
                    completed_at=datetime.now(UTC),
                    duration_ms=0,
                    stream_bytes=0,
                )
            root.mkdir()
            verify_directory(root)
            context = _SubscriptionProcessContext(
                cwd=root / "cwd",
                home=root / "home",
                appdata=root / "home" / "AppData" / "Roaming",
                local_appdata=root / "home" / "AppData" / "Local",
                temporary=root / "tmp",
                codex_home=self.credential_codex_home,
            )
            for path in (
                context.cwd,
                context.home,
                context.appdata.parent,
                context.appdata,
                context.local_appdata,
                context.temporary,
            ):
                path.mkdir(parents=True, exist_ok=True)
                verify_directory(path)
        except BridgeTransportFailure:
            raise
        except Exception as exc:
            raise BridgeTransportFailure(
                "subscription_context_preflight_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            ) from exc
        return context

    def _skill_snapshot(self) -> tuple[_SkillState, ...]:
        try:
            codex_home = self.credential_codex_home.resolve(strict=True)
            if not codex_home.is_dir():
                raise ValueError("CODEX_HOME is not a directory")
            # Never traverse CODEX_HOME as a whole: it also owns authentication and
            # sandbox-secret stores. Only the documented skill root and locally cached
            # plugin contributions can affect skill discovery with user config ignored.
            discovery_roots = tuple(
                candidate
                for candidate in (codex_home / "skills", codex_home / "plugins")
                if candidate.exists()
            )
            states: list[_SkillState] = []
            total_bytes = 0
            for discovery_root in discovery_roots:
                resolved_root = discovery_root.resolve(strict=True)
                if not resolved_root.is_dir() or not resolved_root.is_relative_to(codex_home):
                    raise ValueError("skill discovery root escapes CODEX_HOME")
                for skill_file in resolved_root.rglob("SKILL.md"):
                    resolved_file = skill_file.resolve(strict=True)
                    if not resolved_file.is_file() or not resolved_file.is_relative_to(
                        resolved_root
                    ):
                        raise ValueError("skill path escapes its discovery root")
                    status = verify_regular_single_link(resolved_file)
                    if status.st_size > _MAX_SKILL_BYTES:
                        raise ValueError("skill file size bound exceeded")
                    total_bytes += status.st_size
                    if total_bytes > _MAX_SKILL_TOTAL_BYTES:
                        raise ValueError("skill total size bound exceeded")
                    digest = hashlib.sha256()
                    # The digest binds drift without retaining or logging skill content.
                    with resolved_file.open("rb") as stream:
                        while chunk := stream.read(65_536):
                            digest.update(chunk)
                        after = os.fstat(stream.fileno())
                    if (
                        after.st_size != status.st_size
                        or after.st_mtime_ns != status.st_mtime_ns
                        or after.st_ino != status.st_ino
                    ):
                        raise ValueError("skill changed while it was inspected")
                    states.append(
                        _SkillState(
                            configuration_path=resolved_file,
                            size=status.st_size,
                            modified_ns=status.st_mtime_ns,
                            file_identity=status.st_ino,
                            content_sha256=digest.hexdigest(),
                        )
                    )
                    if len(states) > _MAX_DISCOVERED_SKILLS:
                        raise ValueError("skill discovery bound exceeded")
        except Exception as exc:
            raise BridgeTransportFailure(
                "subscription_context_preflight_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            ) from exc
        if not states:
            raise BridgeTransportFailure(
                "subscription_context_preflight_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )
        states.sort(key=lambda item: str(item.configuration_path).encode("utf-8"))
        if len({item.configuration_path for item in states}) != len(states):
            raise BridgeTransportFailure(
                "subscription_context_preflight_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )
        return tuple(states)

    @staticmethod
    def _skills_config(snapshot: tuple[_SkillState, ...]) -> str:
        entries = ",".join(
            "{path="
            + json.dumps(str(item.configuration_path), ensure_ascii=True)
            + ",enabled=false}"
            for item in snapshot
        )
        return f"skills.config=[{entries}]"

    def _attempt_key(self, request: BoundedCodexBridgeRequestV1) -> str:
        assignment = request.context.assignment
        return domain_sha256(
            "poker-bounded-codex-subscription-attempt-v1",
            {
                "auth_mode": request.auth_mode,
                "bridge_run_id": assignment.bridge_run_id,
                "assignment_id": assignment.assignment_id,
                "attempt_id": assignment.attempt_id,
                "request_sha256": request.request_sha256,
            },
        )[:32]

    def _attempt(self, key: str) -> Path:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        verify_directory(self.runtime_root)
        attempt = self.runtime_root / key
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
        verify_directory(attempt)
        return attempt

    def _probe_auth(self, *, cwd: Path, environment: dict[str, str]) -> None:
        if self.auth_status_probe is not None:
            if self.auth_status_probe(cwd, environment):
                return
            raise BridgeTransportFailure(
                "missing_subscription_auth",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )
        try:
            completed = subprocess.run(
                (str(self.codex_binary), "login", "status"),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                cwd=cwd,
                env=environment,
                check=False,
                timeout=15,
            )
        except Exception as exc:
            raise BridgeTransportFailure(
                "subscription_auth_probe_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            ) from exc
        stdout = completed.stdout.decode("utf-8", errors="replace").strip()
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        status_messages = tuple(item for item in (stdout, stderr) if item)
        if completed.returncode != 0 or status_messages != ("Logged in using ChatGPT",):
            raise BridgeTransportFailure(
                "missing_subscription_auth",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass

    def _command(
        self,
        *,
        cwd: Path,
        schema: Path,
        output: Path,
        skill_snapshot: tuple[_SkillState, ...],
    ) -> list[str]:
        command = [
            str(self.codex_binary),
            "--strict-config",
            "-a",
            "never",
            "-s",
            "read-only",
            "-m",
            BRIDGE_MODEL_ID,
            "-C",
            str(cwd),
            "-c",
            'forced_login_method="chatgpt"',
            "-c",
            'model_provider="openai"',
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            "project_doc_fallback_filenames=[]",
            "-c",
            'model_reasoning_effort="medium"',
            "-c",
            'service_tier="default"',
            "-c",
            'web_search="disabled"',
            "-c",
            'personality="none"',
            "-c",
            'history.persistence="none"',
            "-c",
            "hide_agent_reasoning=true",
            "-c",
            "show_raw_agent_reasoning=false",
            "-c",
            "check_for_update_on_startup=false",
            "-c",
            "allow_login_shell=false",
            "-c",
            'shell_environment_policy.inherit="none"',
            "-c",
            "analytics.enabled=false",
            "-c",
            "feedback.enabled=false",
            "-c",
            "apps._default.enabled=false",
            "-c",
            "mcp_servers={}",
            "-c",
            self._skills_config(skill_snapshot),
        ]
        for feature in _DISABLED_FEATURES:
            command.extend(("-c", f"features.{feature}=false"))
        command.extend(
            (
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--json",
                "--color",
                "never",
                "--output-schema",
                str(schema),
                "--output-last-message",
                str(output),
                "-",
            )
        )
        return command

    def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
        now = datetime.now(UTC)
        if request.auth_mode is not self.auth_mode:
            raise BridgeTransportFailure(
                "transport_auth_mode_mismatch",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=now,
                duration_ms=0,
                stream_bytes=0,
            )
        policy = request.context.runtime_policy
        if (
            policy.runtime_identity != BRIDGE_SUBSCRIPTION_RUNTIME_ID
            or policy.model_provider != BRIDGE_SUBSCRIPTION_PROVIDER_ID
            or policy.model != BRIDGE_MODEL_ID
            or policy.reasoning_effort != BRIDGE_REASONING_EFFORT
            or policy.service_tier != BRIDGE_SERVICE_TIER
        ):
            raise BridgeTransportFailure(
                "subscription_policy_mismatch",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=now,
                duration_ms=0,
                stream_bytes=0,
            )
        prompt = outbound_request_bytes(request)
        if len(prompt) > MAX_CONTEXT_BYTES:
            raise BridgeTransportFailure(
                "context_cap_exceeded",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=now,
                duration_ms=0,
                stream_bytes=0,
            )
        attempt_key = self._attempt_key(request)
        attempt = self._attempt(attempt_key)
        process_context = self._process_context(attempt_key)
        schema_path = attempt / "output-schema.json"
        output_path = attempt / "last-message.json"
        events_path = attempt / "raw-events.jsonl"
        stderr_path = attempt / "stderr.bin"
        _write_exclusive(
            schema_path,
            canonical_json_bytes(role_output_schema_for_request(request)),
        )
        environment = self._environment(process_context)
        self._probe_auth(cwd=process_context.cwd, environment=environment)
        skill_snapshot = self._skill_snapshot()
        started = time.monotonic()
        try:
            command = (
                self.command_factory(process_context.cwd, schema_path, output_path)
                if self.command_factory is not None
                else self._command(
                    cwd=process_context.cwd,
                    schema=schema_path,
                    output=output_path,
                    skill_snapshot=skill_snapshot,
                )
            )
            if self._skill_snapshot() != skill_snapshot:
                raise BridgeTransportFailure(
                    "subscription_context_drift",
                    effect_state=BridgeEffectState.NOT_LAUNCHED,
                    launched_at=None,
                    completed_at=datetime.now(UTC),
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    stream_bytes=0,
                )
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=process_context.cwd,
                env=environment,
            )
        except BridgeTransportFailure:
            raise
        except Exception as exc:
            raise BridgeTransportFailure(
                "subscription_process_launch_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=0,
            ) from exc
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stdout = _CappedReader(cast(BinaryIO, process.stdout), MAX_STREAM_BYTES)
        stderr = _CappedReader(cast(BinaryIO, process.stderr), _STDERR_CAP)
        stdout.start()
        stderr.start()
        try:
            process.stdin.write(prompt)
            process.stdin.close()
        except Exception as exc:
            self._terminate(process)
            raise BridgeTransportFailure(
                "subscription_input_write_failed",
                effect_state=BridgeEffectState.EFFECT_UNKNOWN,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                stream_bytes=stdout.total,
            ) from exc
        timed_out = False
        deadline = started + (MAX_ROLE_RUNTIME_MS / 1000)
        while process.poll() is None:
            if stdout.overflow.is_set() or stderr.overflow.is_set():
                self._terminate(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                self._terminate(process)
                break
            time.sleep(0.02)
        stdout.join(timeout=2)
        stderr.join(timeout=2)
        raw_events = bytes(stdout.data)
        _write_exclusive(events_path, raw_events)
        _write_exclusive(stderr_path, bytes(stderr.data))
        thread_id, launched, prefix_items = _thread_from_prefix(raw_events)
        thread_hash = sha256_bytes(thread_id.encode("utf-8")) if thread_id is not None else None
        turn_hash = (
            domain_sha256(
                "poker-bounded-codex-subscription-turn-v1",
                {
                    "thread_id_sha256": thread_hash,
                    "request_sha256": request.request_sha256,
                    "attempt_id": request.context.assignment.attempt_id,
                },
            )
            if thread_hash is not None and launched
            else None
        )
        launched_at = now if launched else None
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        if timed_out or stdout.overflow.is_set() or stderr.overflow.is_set():
            raise BridgeTransportFailure(
                "subscription_timeout" if timed_out else "subscription_output_cap_exceeded",
                effect_state=(
                    BridgeEffectState.CANCEL_UNCONFIRMED
                    if launched
                    else BridgeEffectState.EFFECT_UNKNOWN
                ),
                launched_at=launched_at,
                completed_at=datetime.now(UTC),
                duration_ms=duration_ms,
                stream_bytes=stdout.total,
                item_types=prefix_items,
                thread_id_sha256=thread_hash,
                turn_id_sha256=turn_hash,
            )
        failure_evidence: BridgeTransportFailureEvidence | None = None
        try:
            events = _parse_events(raw_events)
            event_types = tuple(cast(str, item.get("type")) for item in events)
            if any(item not in _ALLOWED_EVENT_TYPES for item in event_types):
                raise ValueError("unapproved Codex event type")
            threads = [item for item in events if item.get("type") == "thread.started"]
            turns = [item for item in events if item.get("type") == "turn.started"]
            completions = [item for item in events if item.get("type") == "turn.completed"]
            if len(threads) != 1 or len(turns) != 1 or len(completions) != 1:
                raise ValueError("Codex lifecycle event count mismatch")
            raw_thread = threads[0].get("thread_id")
            if not isinstance(raw_thread, str) or not raw_thread:
                raise ValueError("Codex thread identity missing")
            item_types: set[str] = set()
            final_messages = 0
            unapproved_items = False
            for event in events:
                if event.get("type") not in {"item.started", "item.completed"}:
                    continue
                item = event.get("item")
                if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                    raise ValueError("Codex item event malformed")
                item_type = cast(str, item["type"])
                item_types.add(item_type)
                if item_type not in _ALLOWED_ITEM_TYPES:
                    unapproved_items = True
                if event.get("type") == "item.completed" and item_type == "agent_message":
                    final_messages += 1
            usage_raw = completions[0].get("usage")
            if not isinstance(usage_raw, dict):
                raise ValueError("Codex usage event missing")
            usage = BridgeTransportUsageV1(
                input_tokens=_required_usage_int(usage_raw, "input_tokens"),
                cached_input_tokens=_required_usage_int(usage_raw, "cached_input_tokens"),
                output_tokens=_required_usage_int(usage_raw, "output_tokens"),
                reasoning_output_tokens=_required_usage_int(usage_raw, "reasoning_output_tokens"),
                estimated_cost_micro_usd=None,
                cost_authority="not_applicable",
            )
            response = output_path.read_bytes() if output_path.exists() else None
            failure_evidence = BridgeTransportFailureEvidence(
                usage=usage,
                response_bytes=None if response is None else len(response),
                runtime_identity=BRIDGE_SUBSCRIPTION_RUNTIME_ID,
                model_identity_evidence=(
                    "requested_pinned_no_fallback_no_reroute"
                    if "error" not in item_types
                    else "unavailable"
                ),
                observed_model=None,
                observed_model_provider=None,
                observed_reasoning_effort=None,
                observed_service_tier=None,
            )
            if unapproved_items:
                raise ValueError("Codex emitted an unapproved item")
            if final_messages != 1:
                raise ValueError("Codex final agent message count mismatch")
            if not response:
                raise ValueError("Codex final response is empty")
        except Exception as exc:
            raise BridgeTransportFailure(
                "subscription_protocol_or_output_invalid",
                effect_state=(
                    BridgeEffectState.FAILED if launched else BridgeEffectState.EFFECT_UNKNOWN
                ),
                launched_at=launched_at,
                completed_at=datetime.now(UTC),
                duration_ms=duration_ms,
                stream_bytes=stdout.total,
                item_types=prefix_items,
                thread_id_sha256=thread_hash,
                turn_id_sha256=turn_hash,
                evidence=failure_evidence,
            ) from exc
        if process.returncode != 0 or thread_hash is None or turn_hash is None:
            raise BridgeTransportFailure(
                "subscription_exit_or_identity_mismatch",
                effect_state=(
                    BridgeEffectState.FAILED if launched else BridgeEffectState.EFFECT_UNKNOWN
                ),
                launched_at=launched_at,
                completed_at=datetime.now(UTC),
                duration_ms=duration_ms,
                stream_bytes=stdout.total,
                item_types=prefix_items,
                thread_id_sha256=thread_hash,
                turn_id_sha256=turn_hash,
                evidence=failure_evidence,
            )
        assert response is not None
        return BridgeTransportResult(
            auth_mode=self.auth_mode,
            transport_qualification=self.transport_qualification,
            response_bytes=response,
            usage=usage,
            model_identity_evidence="requested_pinned_no_fallback_no_reroute",
            observed_model=None,
            observed_model_provider=None,
            observed_reasoning_effort=None,
            observed_service_tier=None,
            runtime_identity=BRIDGE_SUBSCRIPTION_RUNTIME_ID,
            thread_id_sha256=thread_hash,
            turn_id_sha256=turn_hash,
            launched_at=launched_at or now,
            completed_at=datetime.now(UTC),
            duration_ms=duration_ms,
            stream_bytes=stdout.total,
            item_types=tuple(sorted(item_types, key=lambda item: item.encode("utf-8"))),
        )


__all__ = ["CodexSubscriptionCliTransport"]
