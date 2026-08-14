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
    role_developer_instructions,
    role_output_schema_for_request,
)
from poker_deliberation.codex_bridge.identity import (
    bridge_runtime_source_inventory_sha256,
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
    SUBSCRIPTION_EXECUTION_RUNTIME_HASH_DOMAIN,
    SUBSCRIPTION_SEALED_LIVE_ATTESTATION_HASH_DOMAIN,
    SUBSCRIPTION_USAGE_HASH_DOMAIN,
    BoundedCodexBridgeRequestV1,
    BridgeEffectState,
    BridgeRole,
    BridgeTransportUsageV1,
    CodexSubscriptionLiveExecutionEvidenceV1,
    RuntimeAuthModeV1,
    repository_skill_for_role,
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
_MAX_SKILL_CATALOG_BYTES = 524_288
_MAX_SKILL_CATALOG_MESSAGES = 64
_MAX_SKILL_CATALOG_CONTENT_ITEMS = 256
_MAX_SKILL_CATALOG_LINES = 4_096
_MAX_SKILL_CATALOG_LINE_BYTES = 32_768
_SKILL_CATALOG_TIMEOUT_SECONDS = 15.0
_SKILL_CATALOG_PROBE_PROMPT = "Return no answer. Inspect the locally rendered prompt only."
_REPOSITORY_SKILL_PATHS = tuple(
    sorted(
        {
            f".agents/skills/{skill_id}/SKILL.md"
            for role in BridgeRole
            if (skill_id := repository_skill_for_role(role)) is not None
        },
        key=lambda item: item.encode("utf-8"),
    )
)
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
_PINNED_SUBPROCESS_RUN = subprocess.run
_PINNED_SUBPROCESS_POPEN = subprocess.Popen
_SUBSCRIPTION_RUNTIME_CONFIGURATION_HASH_DOMAIN = (
    "poker-bounded-codex-subscription-runtime-configuration-v1"
)
_SUBSCRIPTION_COMMAND_CONTRACT_HASH_DOMAIN = "poker-bounded-codex-subscription-command-contract-v1"
_SUBSCRIPTION_LAUNCH_INTENT_HASH_DOMAIN = "poker-bounded-codex-subscription-launch-intent-v1"
_SUBSCRIPTION_SKILL_CATALOG_HASH_DOMAIN = "poker-bounded-codex-skill-catalog-v1"
_SEALED_LIVE_EXECUTION_CAPABILITY = object()


@dataclass(frozen=True, slots=True)
class _SkillState:
    configuration_path: Path
    skill_id: str | None
    source_path: str | None
    enabled: bool
    size: int
    modified_ns: int
    file_identity: int
    content_sha256: str
    source_content_sha256: str | None


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


def _read_bounded_regular_file(path: Path, maximum: int) -> tuple[bytes, os.stat_result]:
    status = verify_regular_single_link(path)
    if status.st_size > maximum:
        raise ValueError("file size bound exceeded")
    data = bytearray()
    with path.open("rb") as stream:
        while chunk := stream.read(65_536):
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError("file size changed beyond its bound")
        after = os.fstat(stream.fileno())
    if (
        after.st_size != status.st_size
        or after.st_mtime_ns != status.st_mtime_ns
        or after.st_ino != status.st_ino
    ):
        raise ValueError("file changed while it was inspected")
    return bytes(data), status


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
        if (
            event_type == "thread.started"
            and isinstance(value.get("thread_id"), str)
            and value["thread_id"]
        ):
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


def _skill_catalog_sha256(
    data: bytes,
    *,
    selected_skill_id: str | None,
    staged_skill_path: Path | None,
) -> str:
    if not data or len(data) > _MAX_SKILL_CATALOG_BYTES:
        raise ValueError("Codex Skill catalog size is invalid")
    value = json.loads(data)
    if not isinstance(value, list) or len(value) > _MAX_SKILL_CATALOG_MESSAGES:
        raise ValueError("Codex prompt input is not a bounded message list")
    blocks: list[str] = []
    opening_boundary = "<skills_instructions>"
    closing_boundary = "</skills_instructions>"
    for message in value:
        if not isinstance(message, dict):
            raise ValueError("Codex prompt input message is malformed")
        content = message.get("content")
        if content is None:
            continue
        if not isinstance(content, list) or len(content) > _MAX_SKILL_CATALOG_CONTENT_ITEMS:
            raise ValueError("Codex prompt input content is malformed")
        for item in content:
            if not isinstance(item, dict):
                raise ValueError("Codex prompt input content item is malformed")
            text = item.get("text")
            if not isinstance(text, str):
                continue
            if opening_boundary not in text and closing_boundary not in text:
                continue
            if (
                message.get("type") != "message"
                or message.get("role") != "developer"
                or item.get("type") != "input_text"
            ):
                raise ValueError("Codex Skill catalog authority is invalid")
            if text.count(opening_boundary) != 1 or text.count(closing_boundary) != 1:
                raise ValueError("Codex Skill catalog boundary is ambiguous")
            start = text.index(opening_boundary)
            end = text.index(closing_boundary) + len(closing_boundary)
            if start != 0 or end != len(text):
                raise ValueError("Codex Skill catalog is not a standalone developer input")
            blocks.append(text)
    if not blocks:
        if selected_skill_id is not None or staged_skill_path is not None:
            raise ValueError("Codex Skill catalog is missing its selected Skill")
        return domain_sha256(
            _SUBSCRIPTION_SKILL_CATALOG_HASH_DOMAIN,
            {
                "repository_entries": [],
                "skills_instructions_sha256": None,
            },
        )
    if len(blocks) != 1:
        raise ValueError("Codex Skill catalog is missing or duplicated")
    block = blocks[0]
    lines = block.splitlines()
    if len(lines) > _MAX_SKILL_CATALOG_LINES or any(
        len(line.encode("utf-8")) > _MAX_SKILL_CATALOG_LINE_BYTES for line in lines
    ):
        raise ValueError("Codex Skill catalog line bound exceeded")
    try:
        available_index = lines.index("### Available skills")
    except ValueError as exc:
        raise ValueError("Codex Skill catalog heading is missing") from exc
    entries: list[tuple[str, str]] = []
    for line in lines[available_index + 1 :]:
        if line == "</skills_instructions>":
            break
        if not line or not line.startswith("- "):
            continue
        identifier, separator, remainder = line[2:].partition(": ")
        description, locator_separator, locator = remainder.rpartition(" (file: ")
        if (
            not separator
            or not description
            or not locator_separator
            or not locator.endswith(")")
            or not identifier
            or len(identifier) > 128
            or any(
                not (character.isascii() and (character.isalnum() or character in "-_.:"))
                for character in identifier
            )
        ):
            raise ValueError("Codex Skill catalog entry is malformed")
        entries.append((identifier, locator[:-1]))
        if len(entries) > _MAX_DISCOVERED_SKILLS:
            raise ValueError("Codex Skill catalog entry bound exceeded")
    repository_ids = {Path(item).parent.name for item in _REPOSITORY_SKILL_PATHS}
    repository_entries = [item for item in entries if item[0] in repository_ids]
    if selected_skill_id is None:
        if entries or staged_skill_path is not None:
            raise ValueError("Codex Skill catalog exposed an unselected Skill")
    else:
        if selected_skill_id not in repository_ids or staged_skill_path is None:
            raise ValueError("Codex Skill catalog expectation is invalid")
        if len(entries) != 1 or entries[0][0] != selected_skill_id:
            raise ValueError("Codex Skill catalog exclusive selection mismatch")
        resolved_locator = Path(entries[0][1]).resolve(strict=True)
        resolved_stage = staged_skill_path.resolve(strict=True)
        if resolved_locator != resolved_stage:
            raise ValueError("Codex Skill catalog selected the wrong execution copy")
        verify_regular_single_link(resolved_locator)
    return domain_sha256(
        _SUBSCRIPTION_SKILL_CATALOG_HASH_DOMAIN,
        {
            "repository_entries": repository_entries,
            "skills_instructions_sha256": sha256_bytes(block.encode("utf-8")),
        },
    )


class CodexSubscriptionCliTransport:
    """No-fallback saved-login transport for one fresh ephemeral Codex exec turn."""

    auth_mode = RuntimeAuthModeV1.CODEX_SUBSCRIPTION
    # Diagnostic compatibility only. The controller never trusts this mutable label;
    # actual-live is derived from the sealed per-execution evidence below.
    transport_qualification: Literal["deterministic_fixture"] = "deterministic_fixture"

    def __init__(
        self,
        runtime_root: Path,
        *,
        codex_binary: Path,
        auth_status_probe: Callable[[Path, dict[str, str]], bool] | None = None,
        command_factory: Callable[[Path, Path, Path], list[str]] | None = None,
        skill_catalog_probe: (
            Callable[[Path, dict[str, str], tuple[_SkillState, ...]], bytes] | None
        ) = None,
        isolation_root: Path | None = None,
        credential_codex_home: Path | None = None,
        runtime_capability: PreparedRuntimeRoot | None = None,
    ) -> None:
        default_isolation_root = isolation_root is None
        default_credential_codex_home = credential_codex_home is None
        self.runtime_root = runtime_root.resolve(strict=False)
        self.runtime_capability = runtime_capability
        if runtime_capability is not None and runtime_capability.path != self.runtime_root:
            raise ValueError("runtime capability path mismatch")
        self.codex_binary = codex_binary.resolve(strict=True)
        if _file_sha256(self.codex_binary) != BRIDGE_RUNTIME_BINARY_SHA256:
            raise ValueError("bundled Codex binary hash mismatch")
        self.auth_status_probe = auth_status_probe
        self.command_factory = command_factory
        self.skill_catalog_probe = skill_catalog_probe
        # This private bit is only one input to the controller-side exact-type and
        # implementation-identity gate. It is deliberately never a qualification label.
        self._sealed_default_process = (
            type(self) is CodexSubscriptionCliTransport
            and auth_status_probe is None
            and command_factory is None
            and skill_catalog_probe is None
            and default_isolation_root
            and default_credential_codex_home
            and runtime_capability is not None
        )
        self._sealed_constructor_capability = (
            _SEALED_LIVE_EXECUTION_CAPABILITY if self._sealed_default_process else None
        )

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
        source_repository_root = repository_root or self._repository_root(Path(__file__).resolve())
        if source_repository_root is None:
            raise ValueError("subscription repository root cannot be resolved")
        self.repository_root = source_repository_root.resolve(strict=True)

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

    def _skill_snapshot(
        self,
        request: BoundedCodexBridgeRequestV1,
    ) -> tuple[_SkillState, ...]:
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
                    contents, status = _read_bounded_regular_file(
                        resolved_file,
                        _MAX_SKILL_BYTES,
                    )
                    total_bytes += status.st_size
                    if total_bytes > _MAX_SKILL_TOTAL_BYTES:
                        raise ValueError("skill total size bound exceeded")
                    states.append(
                        _SkillState(
                            configuration_path=resolved_file,
                            skill_id=None,
                            source_path=None,
                            enabled=False,
                            size=status.st_size,
                            modified_ns=status.st_mtime_ns,
                            file_identity=status.st_ino,
                            content_sha256=sha256_bytes(contents),
                            source_content_sha256=None,
                        )
                    )
                    if len(states) > _MAX_DISCOVERED_SKILLS:
                        raise ValueError("skill discovery bound exceeded")
            selected = request.context.assignment.conformance
            if request.developer_instructions != role_developer_instructions(
                selected.role,
                selected,
                RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            ):
                raise ValueError("subscription request uses a legacy role contract")
            if selected.repository_skill_id != repository_skill_for_role(selected.role):
                raise ValueError("subscription request lacks its repository Skill binding")
            skill_id = selected.repository_skill_id
            if skill_id is not None:
                relative = f".agents/skills/{skill_id}/SKILL.md"
                if relative not in _REPOSITORY_SKILL_PATHS:
                    raise ValueError("repository Skill path is outside the allowlist")
                repository_root = self.repository_root.resolve(strict=True)
                resolved_file = repository_root.joinpath(*relative.split("/")).resolve(strict=True)
                if not resolved_file.is_file() or not resolved_file.is_relative_to(repository_root):
                    raise ValueError("repository Skill path escapes its authority")
                contents, status = _read_bounded_regular_file(
                    resolved_file,
                    _MAX_SKILL_BYTES,
                )
                total_bytes += status.st_size
                if total_bytes > _MAX_SKILL_TOTAL_BYTES:
                    raise ValueError("skill total size bound exceeded")
                content_sha256 = sha256_bytes(contents)
                if (
                    selected.repository_skill_source_path != relative
                    or selected.repository_skill_content_sha256 != content_sha256
                    or selected.repository_skill_version_kind != "repository_commit"
                    or selected.repository_skill_version is None
                    or selected.repository_skill_instructions
                    != " ".join(contents.decode("utf-8", errors="strict").split())
                ):
                    raise ValueError("repository Skill binding mismatch")
                states.append(
                    _SkillState(
                        configuration_path=resolved_file,
                        skill_id=skill_id,
                        source_path=relative,
                        enabled=True,
                        size=status.st_size,
                        modified_ns=status.st_mtime_ns,
                        file_identity=status.st_ino,
                        content_sha256=content_sha256,
                        source_content_sha256=content_sha256,
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

    def _stage_skill_snapshot(
        self,
        *,
        cwd: Path,
        source_snapshot: tuple[_SkillState, ...],
    ) -> tuple[_SkillState, ...]:
        try:
            resolved_cwd = cwd.resolve(strict=True)
            verify_directory(resolved_cwd)
            selected = tuple(item for item in source_snapshot if item.enabled)
            if len(selected) > 1:
                raise ValueError("multiple repository Skills were selected")
            execution_states = [item for item in source_snapshot if not item.enabled]
            for source in selected:
                if source.skill_id is None or source.source_content_sha256 is None:
                    raise ValueError("selected repository Skill lacks source identity")
                source_bytes, source_status = _read_bounded_regular_file(
                    source.configuration_path,
                    _MAX_SKILL_BYTES,
                )
                if (
                    source_status.st_size != source.size
                    or source_status.st_mtime_ns != source.modified_ns
                    or source_status.st_ino != source.file_identity
                    or sha256_bytes(source_bytes) != source.source_content_sha256
                ):
                    raise ValueError("repository Skill source changed before staging")
                staged_directory = (resolved_cwd / ".agents" / "skills" / source.skill_id).resolve(
                    strict=False
                )
                if not staged_directory.is_relative_to(resolved_cwd):
                    raise ValueError("repository Skill execution path escapes its cwd")
                staged_directory.mkdir(parents=True, exist_ok=False)
                for directory in (
                    resolved_cwd / ".agents",
                    resolved_cwd / ".agents" / "skills",
                    staged_directory,
                ):
                    verify_directory(directory)
                staged_path = staged_directory / "SKILL.md"
                _write_exclusive(staged_path, source_bytes)
                staged_bytes, staged_status = _read_bounded_regular_file(
                    staged_path.resolve(strict=True),
                    _MAX_SKILL_BYTES,
                )
                staged_sha256 = sha256_bytes(staged_bytes)
                if staged_bytes != source_bytes or staged_sha256 != source.source_content_sha256:
                    raise ValueError("repository Skill execution copy mismatch")
                execution_states.append(
                    _SkillState(
                        configuration_path=staged_path.resolve(strict=True),
                        skill_id=source.skill_id,
                        source_path=source.source_path,
                        enabled=True,
                        size=staged_status.st_size,
                        modified_ns=staged_status.st_mtime_ns,
                        file_identity=staged_status.st_ino,
                        content_sha256=staged_sha256,
                        source_content_sha256=source.source_content_sha256,
                    )
                )
            execution_states.sort(key=lambda item: str(item.configuration_path).encode("utf-8"))
            if len({item.configuration_path for item in execution_states}) != len(execution_states):
                raise ValueError("duplicate Skill configuration path")
            return tuple(execution_states)
        except Exception as exc:
            raise BridgeTransportFailure(
                "subscription_context_preflight_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            ) from exc

    @staticmethod
    def _observed_skill_snapshot(
        snapshot: tuple[_SkillState, ...],
    ) -> tuple[_SkillState, ...]:
        observed: list[_SkillState] = []
        total_bytes = 0
        for expected in snapshot:
            contents, status = _read_bounded_regular_file(
                expected.configuration_path,
                _MAX_SKILL_BYTES,
            )
            total_bytes += status.st_size
            if total_bytes > _MAX_SKILL_TOTAL_BYTES:
                raise ValueError("Skill snapshot total size bound exceeded")
            observed.append(
                _SkillState(
                    configuration_path=expected.configuration_path,
                    skill_id=expected.skill_id,
                    source_path=expected.source_path,
                    enabled=expected.enabled,
                    size=status.st_size,
                    modified_ns=status.st_mtime_ns,
                    file_identity=status.st_ino,
                    content_sha256=sha256_bytes(contents),
                    source_content_sha256=expected.source_content_sha256,
                )
            )
        return tuple(observed)

    @staticmethod
    def _skills_config(snapshot: tuple[_SkillState, ...]) -> str:
        entries = ",".join(
            "{path="
            + json.dumps(str(item.configuration_path), ensure_ascii=True)
            + f",enabled={'true' if item.enabled else 'false'}}}"
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

    def _begin_runtime_capability(self) -> None:
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

    def _attempt(self, key: str) -> Path:
        self._begin_runtime_capability()
        if self.runtime_capability is None:
            self.runtime_root.mkdir(parents=True, exist_ok=True)
        verify_directory(self.runtime_root)
        self._verify_runtime_capability()
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
        self._verify_runtime_capability()
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
            process_runner = (
                _PINNED_SUBPROCESS_RUN if self._sealed_default_process else subprocess.run
            )
            completed = process_runner(
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

    def _configuration_args(
        self,
        skill_snapshot: tuple[_SkillState, ...],
    ) -> list[str]:
        arguments = [
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
            "skills.bundled.enabled=false",
            "-c",
            self._skills_config(skill_snapshot),
        ]
        for feature in _DISABLED_FEATURES:
            arguments.extend(("-c", f"features.{feature}=false"))
        return arguments

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
        ]
        command.extend(self._configuration_args(skill_snapshot))
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

    def _catalog_command(
        self,
        *,
        cwd: Path,
        skill_snapshot: tuple[_SkillState, ...],
    ) -> list[str]:
        # Codex 0.144.4 intentionally rejects --strict-config for debug commands.
        # Every effective execution override is nevertheless reused here, and the
        # production exec remains strict and ignores user configuration.
        command = [
            str(self.codex_binary),
            "-a",
            "never",
            "-s",
            "read-only",
            "-m",
            BRIDGE_MODEL_ID,
            "-C",
            str(cwd),
        ]
        command.extend(self._configuration_args(skill_snapshot))
        command.extend(("debug", "prompt-input", _SKILL_CATALOG_PROBE_PROMPT))
        return command

    def _probe_skill_catalog(
        self,
        *,
        cwd: Path,
        environment: dict[str, str],
        skill_snapshot: tuple[_SkillState, ...],
    ) -> str:
        selected = tuple(item for item in skill_snapshot if item.enabled)
        if len(selected) > 1:
            raise BridgeTransportFailure(
                "subscription_skill_catalog_probe_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            )
        selected_skill_id = selected[0].skill_id if selected else None
        staged_skill_path = selected[0].configuration_path if selected else None
        try:
            if self.skill_catalog_probe is not None:
                raw_catalog = self.skill_catalog_probe(cwd, environment, skill_snapshot)
                if not isinstance(raw_catalog, bytes):
                    raise ValueError("injected Skill catalog probe returned non-bytes")
            else:
                process_factory = (
                    _PINNED_SUBPROCESS_POPEN if self._sealed_default_process else subprocess.Popen
                )
                process = process_factory(
                    self._catalog_command(cwd=cwd, skill_snapshot=skill_snapshot),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=environment,
                )
                assert process.stdout is not None
                assert process.stderr is not None
                stdout = _CappedReader(
                    cast(BinaryIO, process.stdout),
                    _MAX_SKILL_CATALOG_BYTES,
                )
                stderr = _CappedReader(cast(BinaryIO, process.stderr), _STDERR_CAP)
                stdout.start()
                stderr.start()
                deadline = time.monotonic() + _SKILL_CATALOG_TIMEOUT_SECONDS
                timed_out = False
                while process.poll() is None:
                    if stdout.overflow.is_set() or stderr.overflow.is_set():
                        self._terminate(process)
                        break
                    if time.monotonic() >= deadline:
                        timed_out = True
                        self._terminate(process)
                        break
                    time.sleep(0.01)
                stdout.join(timeout=2)
                stderr.join(timeout=2)
                if (
                    timed_out
                    or stdout.overflow.is_set()
                    or stderr.overflow.is_set()
                    or stdout.is_alive()
                    or stderr.is_alive()
                    or process.returncode != 0
                    or stderr.data
                ):
                    raise ValueError("bounded Codex Skill catalog probe failed")
                raw_catalog = bytes(stdout.data)
            return _skill_catalog_sha256(
                raw_catalog,
                selected_skill_id=selected_skill_id,
                staged_skill_path=staged_skill_path,
            )
        except Exception as exc:
            raise BridgeTransportFailure(
                "subscription_skill_catalog_probe_failed",
                effect_state=BridgeEffectState.NOT_LAUNCHED,
                launched_at=None,
                completed_at=datetime.now(UTC),
                duration_ms=0,
                stream_bytes=0,
            ) from exc

    def _command_contract_sha256(
        self,
        command: list[str],
        *,
        cwd: Path,
        schema: Path,
        output: Path,
        skill_snapshot: tuple[_SkillState, ...],
    ) -> str:
        """Hash the executed command without publishing host-specific paths."""

        skill_binding = domain_sha256(
            "poker-bounded-codex-subscription-skill-snapshot-v2",
            [
                {
                    "content_sha256": item.content_sha256,
                    "enabled": item.enabled,
                    "size": item.size,
                    "skill_id": item.skill_id,
                    "source_path": item.source_path,
                    "source_content_sha256": item.source_content_sha256,
                }
                for item in skill_snapshot
            ],
        )
        replacements = {
            str(self.codex_binary): "$CODEX_BINARY",
            str(cwd): "$EXECUTION_CWD",
            str(schema): "$OUTPUT_SCHEMA",
            str(output): "$OUTPUT_MESSAGE",
        }
        normalized: list[str] = []
        for item in command:
            if item.startswith("skills.config="):
                normalized.append(f"skills.config_sha256={skill_binding}")
            else:
                normalized.append(replacements.get(item, item))
        return domain_sha256(
            _SUBSCRIPTION_COMMAND_CONTRACT_HASH_DOMAIN,
            normalized,
        )

    @staticmethod
    def _runtime_configuration_sha256(
        *,
        environment: dict[str, str],
        command_contract_sha256: str,
        skill_catalog_sha256: str,
    ) -> str:
        # The environment is an explicit allowlist and contains no credential values.
        # Only its digest is retained in the public evidence.
        environment_sha256 = domain_sha256(
            "poker-bounded-codex-subscription-environment-v1",
            environment,
        )
        return domain_sha256(
            _SUBSCRIPTION_RUNTIME_CONFIGURATION_HASH_DOMAIN,
            {
                "allowed_event_types": sorted(_ALLOWED_EVENT_TYPES),
                "allowed_item_types": sorted(_ALLOWED_ITEM_TYPES),
                "command_contract_sha256": command_contract_sha256,
                "disabled_features": _DISABLED_FEATURES,
                "environment_sha256": environment_sha256,
                "skill_catalog_sha256": skill_catalog_sha256,
                "stderr_cap": _STDERR_CAP,
                "stream_cap": MAX_STREAM_BYTES,
            },
        )

    @staticmethod
    def _launch_intent_sha256(
        request: BoundedCodexBridgeRequestV1,
        *,
        output_schema_sha256: str,
        command_contract_sha256: str,
        runtime_configuration_sha256: str,
    ) -> str:
        assignment = request.context.assignment
        return domain_sha256(
            _SUBSCRIPTION_LAUNCH_INTENT_HASH_DOMAIN,
            {
                "auth_mode": request.auth_mode,
                "bridge_run_id": assignment.bridge_run_id,
                "role": assignment.role,
                "assignment_id": assignment.assignment_id,
                "attempt_id": assignment.attempt_id,
                "request_sha256": request.request_sha256,
                "request_bytes_sha256": request.request_bytes_sha256,
                "runtime_policy_sha256": request.context.runtime_policy.policy_sha256,
                "output_schema_sha256": output_schema_sha256,
                "command_contract_sha256": command_contract_sha256,
                "runtime_configuration_sha256": runtime_configuration_sha256,
            },
        )

    @staticmethod
    def _live_execution_evidence(
        request: BoundedCodexBridgeRequestV1,
        *,
        runtime_source_inventory_sha256: str,
        runtime_configuration_sha256: str,
        output_schema_sha256: str,
        command_contract_sha256: str,
        launch_intent_sha256: str,
        response: bytes,
        raw_events: bytes,
        usage: BridgeTransportUsageV1,
        thread_id_sha256: str,
        turn_id_sha256: str,
    ) -> CodexSubscriptionLiveExecutionEvidenceV1:
        usage_sha256 = domain_sha256(
            SUBSCRIPTION_USAGE_HASH_DOMAIN,
            usage,
        )
        runtime_payload = {
            "runtime_identity": BRIDGE_SUBSCRIPTION_RUNTIME_ID,
            "runtime_binary_sha256": BRIDGE_RUNTIME_BINARY_SHA256,
            "runtime_source_inventory_sha256": runtime_source_inventory_sha256,
            "runtime_configuration_sha256": runtime_configuration_sha256,
            "request_sha256": request.request_sha256,
            "request_bytes_sha256": request.request_bytes_sha256,
            "output_schema_sha256": output_schema_sha256,
            "command_contract_sha256": command_contract_sha256,
            "launch_intent_sha256": launch_intent_sha256,
            "response_bytes_sha256": sha256_bytes(response),
            "event_stream_sha256": sha256_bytes(raw_events),
            "usage_sha256": usage_sha256,
            "process_returncode": 0,
            "thread_id_sha256": thread_id_sha256,
            "turn_id_sha256": turn_id_sha256,
        }
        payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "evidence_kind": "codex_subscription_sealed_default_execution",
            "transport_type": (
                "poker_deliberation.codex_bridge.subscription_transport."
                "CodexSubscriptionCliTransport"
            ),
            "sealed_default_process": True,
            "default_auth_status_probe": True,
            "default_command_factory": True,
            "default_isolation_root": True,
            "default_credential_codex_home": True,
            "interface": "codex_exec_json",
            "auth_mode": RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            "auth_boundary": "codex_home_saved_chatgpt_login",
            "auth_enforcement": "codex_cli_login_status_exact_chatgpt",
            "credential_values_included": False,
            "provider_model_fallback_allowed": False,
            "model_fallback_allowed": False,
            "process_fallback_allowed": False,
            **runtime_payload,
            "execution_runtime_sha256": domain_sha256(
                SUBSCRIPTION_EXECUTION_RUNTIME_HASH_DOMAIN,
                runtime_payload,
            ),
        }
        return CodexSubscriptionLiveExecutionEvidenceV1.model_validate(
            {
                **payload,
                "attestation_sha256": domain_sha256(
                    SUBSCRIPTION_SEALED_LIVE_ATTESTATION_HASH_DOMAIN,
                    payload,
                ),
            },
            strict=True,
        )

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
        # A legacy v1 request remains readable, but it cannot be launched as a
        # new subscription execution without the complete repository Skill binding.
        source_skill_snapshot = self._skill_snapshot(request)
        sealed_default = _is_exact_sealed_default_transport(self)
        repository_root: Path | None = None
        runtime_source_inventory_before: str | None = None
        if sealed_default:
            repository_root = self._repository_root(self.runtime_root)
            if repository_root is None:
                raise BridgeTransportFailure(
                    "subscription_source_inventory_unavailable",
                    effect_state=BridgeEffectState.NOT_LAUNCHED,
                    launched_at=None,
                    completed_at=now,
                    duration_ms=0,
                    stream_bytes=0,
                )
            try:
                if _file_sha256(self.codex_binary) != BRIDGE_RUNTIME_BINARY_SHA256:
                    raise ValueError("bundled Codex binary hash mismatch")
                runtime_source_inventory_before = bridge_runtime_source_inventory_sha256(
                    repository_root
                )
            except Exception as exc:
                raise BridgeTransportFailure(
                    "subscription_source_inventory_unavailable",
                    effect_state=BridgeEffectState.NOT_LAUNCHED,
                    launched_at=None,
                    completed_at=now,
                    duration_ms=0,
                    stream_bytes=0,
                ) from exc
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
        skill_snapshot = self._stage_skill_snapshot(
            cwd=process_context.cwd,
            source_snapshot=source_skill_snapshot,
        )
        schema_path = attempt / "output-schema.json"
        output_path = attempt / "last-message.json"
        events_path = attempt / "raw-events.jsonl"
        stderr_path = attempt / "stderr.bin"
        output_schema = canonical_json_bytes(role_output_schema_for_request(request))
        output_schema_sha256 = sha256_bytes(output_schema)
        self._verify_runtime_capability()
        _write_exclusive(schema_path, output_schema)
        environment = self._environment(process_context)
        self._probe_auth(cwd=process_context.cwd, environment=environment)
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
            command_contract_sha256 = self._command_contract_sha256(
                command,
                cwd=process_context.cwd,
                schema=schema_path,
                output=output_path,
                skill_snapshot=skill_snapshot,
            )
            skill_catalog_sha256 = self._probe_skill_catalog(
                cwd=process_context.cwd,
                environment=environment,
                skill_snapshot=skill_snapshot,
            )
            runtime_configuration_sha256 = self._runtime_configuration_sha256(
                environment=environment,
                command_contract_sha256=command_contract_sha256,
                skill_catalog_sha256=skill_catalog_sha256,
            )
            launch_intent_sha256 = self._launch_intent_sha256(
                request,
                output_schema_sha256=output_schema_sha256,
                command_contract_sha256=command_contract_sha256,
                runtime_configuration_sha256=runtime_configuration_sha256,
            )
            if (
                self._skill_snapshot(request) != source_skill_snapshot
                or self._observed_skill_snapshot(skill_snapshot) != skill_snapshot
            ):
                raise BridgeTransportFailure(
                    "subscription_context_drift",
                    effect_state=BridgeEffectState.NOT_LAUNCHED,
                    launched_at=None,
                    completed_at=datetime.now(UTC),
                    duration_ms=max(0, int((time.monotonic() - started) * 1000)),
                    stream_bytes=0,
                )
            self._verify_runtime_capability()
            process_factory = _PINNED_SUBPROCESS_POPEN if sealed_default else subprocess.Popen
            process = process_factory(
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
        raw_stderr = bytes(stderr.data)
        self._verify_runtime_capability(process_started=True)
        _write_exclusive(events_path, raw_events)
        self._verify_runtime_capability(process_started=True)
        _write_exclusive(stderr_path, raw_stderr)
        self._verify_runtime_capability(process_started=True)
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
        terminal_cutoff = timed_out or stdout.overflow.is_set() or stderr.overflow.is_set()
        try:
            post_source_skill_snapshot = self._skill_snapshot(request)
            post_execution_skill_snapshot = self._observed_skill_snapshot(skill_snapshot)
        except Exception:
            post_source_skill_snapshot = None
            post_execution_skill_snapshot = None
        if (
            post_source_skill_snapshot != source_skill_snapshot
            or post_execution_skill_snapshot != skill_snapshot
        ):
            raise BridgeTransportFailure(
                "subscription_context_drift",
                effect_state=(
                    BridgeEffectState.CANCEL_UNCONFIRMED
                    if launched and terminal_cutoff
                    else BridgeEffectState.FAILED
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
        if terminal_cutoff:
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
            self._verify_runtime_capability(process_started=True)
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
        live_execution_evidence: CodexSubscriptionLiveExecutionEvidenceV1 | None = None
        live_execution_capability: object | None = None
        if sealed_default:
            assert repository_root is not None
            assert runtime_source_inventory_before is not None
            try:
                if _file_sha256(self.codex_binary) != BRIDGE_RUNTIME_BINARY_SHA256:
                    raise ValueError("bundled Codex binary hash mismatch")
                runtime_source_inventory_after = bridge_runtime_source_inventory_sha256(
                    repository_root
                )
            except Exception as exc:
                raise BridgeTransportFailure(
                    "subscription_context_drift",
                    effect_state=BridgeEffectState.FAILED,
                    launched_at=launched_at,
                    completed_at=datetime.now(UTC),
                    duration_ms=duration_ms,
                    stream_bytes=stdout.total,
                    item_types=prefix_items,
                    thread_id_sha256=thread_hash,
                    turn_id_sha256=turn_hash,
                    evidence=failure_evidence,
                ) from exc
            if runtime_source_inventory_after != runtime_source_inventory_before:
                raise BridgeTransportFailure(
                    "subscription_context_drift",
                    effect_state=BridgeEffectState.FAILED,
                    launched_at=launched_at,
                    completed_at=datetime.now(UTC),
                    duration_ms=duration_ms,
                    stream_bytes=stdout.total,
                    item_types=prefix_items,
                    thread_id_sha256=thread_hash,
                    turn_id_sha256=turn_hash,
                    evidence=failure_evidence,
                )
            live_execution_evidence = self._live_execution_evidence(
                request,
                runtime_source_inventory_sha256=runtime_source_inventory_before,
                runtime_configuration_sha256=runtime_configuration_sha256,
                output_schema_sha256=output_schema_sha256,
                command_contract_sha256=command_contract_sha256,
                launch_intent_sha256=launch_intent_sha256,
                response=response,
                raw_events=raw_events,
                usage=usage,
                thread_id_sha256=thread_hash,
                turn_id_sha256=turn_hash,
            )
            live_execution_capability = _SEALED_LIVE_EXECUTION_CAPABILITY
        result = BridgeTransportResult(
            auth_mode=self.auth_mode,
            transport_qualification=(
                "actual_live" if live_execution_evidence is not None else "deterministic_fixture"
            ),
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
            live_execution_evidence=live_execution_evidence,
            _live_execution_capability=live_execution_capability,
        )
        self._finish_runtime_capability()
        return result


_SEALED_IMPLEMENTATION = {
    "__init__": CodexSubscriptionCliTransport.__init__,
    "_paths_overlap": CodexSubscriptionCliTransport._paths_overlap,
    "_repository_root": CodexSubscriptionCliTransport._repository_root,
    "_environment": CodexSubscriptionCliTransport._environment,
    "_process_context": CodexSubscriptionCliTransport._process_context,
    "_probe_auth": CodexSubscriptionCliTransport._probe_auth,
    "_skill_snapshot": CodexSubscriptionCliTransport._skill_snapshot,
    "_stage_skill_snapshot": CodexSubscriptionCliTransport._stage_skill_snapshot,
    "_observed_skill_snapshot": CodexSubscriptionCliTransport._observed_skill_snapshot,
    "_skills_config": CodexSubscriptionCliTransport._skills_config,
    "_attempt_key": CodexSubscriptionCliTransport._attempt_key,
    "_begin_runtime_capability": CodexSubscriptionCliTransport._begin_runtime_capability,
    "_verify_runtime_capability": CodexSubscriptionCliTransport._verify_runtime_capability,
    "_finish_runtime_capability": CodexSubscriptionCliTransport._finish_runtime_capability,
    "_attempt": CodexSubscriptionCliTransport._attempt,
    "_terminate": CodexSubscriptionCliTransport._terminate,
    "_configuration_args": CodexSubscriptionCliTransport._configuration_args,
    "_command": CodexSubscriptionCliTransport._command,
    "_catalog_command": CodexSubscriptionCliTransport._catalog_command,
    "_probe_skill_catalog": CodexSubscriptionCliTransport._probe_skill_catalog,
    "_command_contract_sha256": CodexSubscriptionCliTransport._command_contract_sha256,
    "_runtime_configuration_sha256": (CodexSubscriptionCliTransport._runtime_configuration_sha256),
    "_launch_intent_sha256": CodexSubscriptionCliTransport._launch_intent_sha256,
    "_live_execution_evidence": CodexSubscriptionCliTransport._live_execution_evidence,
    "execute": CodexSubscriptionCliTransport.execute,
}
_SEALED_MODULE_IMPLEMENTATION = {
    "_file_sha256": _file_sha256,
    "_write_exclusive": _write_exclusive,
    "_read_bounded_regular_file": _read_bounded_regular_file,
    "_parse_events": _parse_events,
    "_thread_from_prefix": _thread_from_prefix,
    "_required_usage_int": _required_usage_int,
    "_skill_catalog_sha256": _skill_catalog_sha256,
    "_CappedReader": _CappedReader,
}
_SEALED_CAPPED_READER_RUN = _CappedReader.run


def _is_exact_sealed_default_transport(value: object) -> bool:
    if type(value) is not CodexSubscriptionCliTransport:
        return False
    transport = value
    if (
        transport.auth_status_probe is not None
        or transport.command_factory is not None
        or transport.skill_catalog_probe is not None
        or transport.runtime_capability is None
        or transport._sealed_default_process is not True
        or transport._sealed_constructor_capability is not _SEALED_LIVE_EXECUTION_CAPABILITY
    ):
        return False
    instance_state = vars(transport)
    if any(name in instance_state for name in _SEALED_IMPLEMENTATION):
        return False
    return (
        CodexSubscriptionCliTransport.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
        and subprocess.run is _PINNED_SUBPROCESS_RUN
        and subprocess.Popen is _PINNED_SUBPROCESS_POPEN
        and _CappedReader.run is _SEALED_CAPPED_READER_RUN
        and all(
            getattr(CodexSubscriptionCliTransport, name) is implementation
            for name, implementation in _SEALED_IMPLEMENTATION.items()
        )
        and all(
            globals()[name] is implementation
            for name, implementation in _SEALED_MODULE_IMPLEMENTATION.items()
        )
    )


def validated_sealed_live_execution(
    transport: object,
    request: BoundedCodexBridgeRequestV1,
    result: BridgeTransportResult,
) -> CodexSubscriptionLiveExecutionEvidenceV1 | None:
    """Return actual-live evidence only for the exact repository-controlled transport."""

    evidence = result.live_execution_evidence
    capability = result._live_execution_capability
    if evidence is None and capability is None:
        return None
    if (
        evidence is None
        or capability is not _SEALED_LIVE_EXECUTION_CAPABILITY
        or not _is_exact_sealed_default_transport(transport)
    ):
        raise ValueError("unsealed subscription live execution evidence")
    exact_transport = cast(CodexSubscriptionCliTransport, transport)
    repository_root = exact_transport._repository_root(exact_transport.runtime_root)
    if repository_root is None:
        raise ValueError("subscription source inventory is unavailable")
    if (
        evidence.runtime_source_inventory_sha256
        != bridge_runtime_source_inventory_sha256(repository_root)
        or evidence.request_sha256 != request.request_sha256
        or evidence.request_bytes_sha256 != request.request_bytes_sha256
        or evidence.response_bytes_sha256 != sha256_bytes(result.response_bytes)
        or evidence.usage_sha256 != domain_sha256(SUBSCRIPTION_USAGE_HASH_DOMAIN, result.usage)
        or evidence.thread_id_sha256 != result.thread_id_sha256
        or evidence.turn_id_sha256 != result.turn_id_sha256
        or evidence.runtime_identity != result.runtime_identity
    ):
        raise ValueError("subscription live execution evidence binding mismatch")
    return evidence


__all__ = [
    "CodexSubscriptionCliTransport",
    "validated_sealed_live_execution",
]
