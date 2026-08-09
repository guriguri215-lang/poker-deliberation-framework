"""One-shot official Codex SDK worker for one already-admitted role request.

This module is launched only by :mod:`sdk_transport`. It never retries, never
reuses a thread, and emits a small canonical control protocol rather than a
Codex JSONL/model trace.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    parse_canonical_model,
)
from poker_deliberation.codex_bridge.contracts import (
    outbound_request_bytes,
    role_output_schema_for_request,
)
from poker_deliberation.codex_bridge.identity import (
    verify_bridge_checkout,
    verify_bridge_module_origins,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_OPENAI_API_PROVIDER_ID,
    BRIDGE_OPENAI_API_RUNTIME_ID,
    BRIDGE_REASONING_EFFORT,
    BRIDGE_RUNTIME_BINARY_SHA256,
    BRIDGE_SERVICE_TIER,
    MAX_CONTEXT_BYTES,
    MAX_RESPONSE_BYTES,
    MAX_STREAM_BYTES,
    BoundedCodexBridgeRequestV1,
    BridgeRoleOutputV1,
    RuntimeAuthModeV1,
)

_SYSTEM_SKILLS = ("imagegen", "openai-docs", "plugin-creator", "skill-creator", "skill-installer")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _emit(payload: dict[str, object]) -> None:
    sys.stdout.buffer.write(canonical_json_bytes(payload) + b"\n")
    sys.stdout.buffer.flush()


def _hash_identifier(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_codex_binary(path: Path) -> Path:
    resolved = path.resolve(strict=True)
    if _file_sha256(resolved) != BRIDGE_RUNTIME_BINARY_SHA256:
        raise RuntimeError("bundled Codex binary hash mismatch")
    return resolved


def _item_type_name(value: str) -> str:
    if value == "agentMessage":
        return "agent_message"
    if value == "reasoning":
        return "reasoning"
    output: list[str] = []
    for character in value:
        if character.isupper():
            output.extend(("_", character.lower()))
        else:
            output.append(character)
    return "".join(output)


def _write_runtime_config(codex_home: Path) -> None:
    skill_lines = "\n".join(
        (
            "[[skills.config]]\n"
            f"path = {json.dumps(str(codex_home / 'skills' / '.system' / name))}\n"
            "enabled = false"
        )
        for name in _SYSTEM_SKILLS
    )
    config = (
        'approval_policy = "never"\n'
        'sandbox_mode = "read-only"\n'
        f'model_provider = "{BRIDGE_OPENAI_API_PROVIDER_ID}"\n'
        'web_search = "disabled"\n'
        'personality = "none"\n'
        'history.persistence = "none"\n'
        "hide_agent_reasoning = true\n"
        "show_raw_agent_reasoning = false\n"
        "check_for_update_on_startup = false\n"
        "allow_login_shell = false\n"
        'shell_environment_policy.inherit = "none"\n'
        "shell_environment_policy.ignore_default_excludes = false\n"
        "analytics.enabled = false\n"
        "feedback.enabled = false\n"
        "apps._default.enabled = false\n"
        "features.apps = false\n"
        "features.code_mode = false\n"
        "features.goals = false\n"
        "features.hooks = false\n"
        "features.memories = false\n"
        "features.multi_agent = false\n"
        "features.network_proxy = false\n"
        "features.remote_plugin = false\n"
        "features.shell_snapshot = false\n"
        "features.shell_tool = false\n"
        "features.skill_mcp_dependency_install = false\n"
        "features.unified_exec = false\n"
        f"[model_providers.{BRIDGE_OPENAI_API_PROVIDER_ID}]\n"
        'name = "P2-025B OpenAI no retry"\n'
        'base_url = "https://api.openai.com/v1"\n'
        'env_key = "OPENAI_API_KEY"\n'
        'wire_api = "responses"\n'
        "request_max_retries = 0\n"
        "stream_max_retries = 0\n"
        "stream_idle_timeout_ms = 120000\n"
        f"{skill_lines}\n"
    )
    path = codex_home / "config.toml"
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(config)
        stream.flush()


def _event_size(event: object) -> int:
    method = getattr(event, "method", "unknown")
    payload = getattr(event, "payload", None)
    if isinstance(payload, BaseModel):
        value: object = payload.model_dump(mode="json")
    elif hasattr(payload, "params"):
        value = vars(payload).get("params")
    else:
        value = None
    return len(canonical_json_bytes({"method": method, "payload": value}))


async def _run(request: BoundedCodexBridgeRequestV1, codex_home: Path, cwd: Path) -> int:
    from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]
    from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
    from openai_codex.generated.v2_all import (
        AgentMessageThreadItem,
        ItemCompletedNotification,
        ItemStartedNotification,
        MessagePhase,
        ModelReroutedNotification,
        Personality,
        ReasoningEffort,
        ReasoningSummary,
        ReasoningThreadItem,
        ThreadSettingsUpdatedNotification,
        ThreadTokenUsageUpdatedNotification,
        TurnCompletedNotification,
        TurnStatus,
    )

    turn_effect_possible = False
    launched_at: str | None = None
    started = time.monotonic()
    stream_bytes = 0
    item_types: set[str] = set()
    final_response: str | None = None
    usage: Any | None = None
    observed_model: str | None = None
    observed_model_provider: str | None = None
    observed_reasoning_effort: str | None = None
    observed_service_tier: str | None = None

    def failure_evidence() -> dict[str, object]:
        identity_verified = (
            observed_model == request.context.runtime_policy.model
            and observed_model_provider == request.context.runtime_policy.model_provider
            and observed_reasoning_effort == request.context.runtime_policy.reasoning_effort
            and observed_service_tier == request.context.runtime_policy.service_tier
        )
        return {
            "runtime_identity": (
                request.context.runtime_policy.runtime_identity if identity_verified else None
            ),
            "observed_model": observed_model if identity_verified else None,
            "observed_model_provider": observed_model_provider if identity_verified else None,
            "observed_reasoning_effort": (observed_reasoning_effort if identity_verified else None),
            "observed_service_tier": observed_service_tier if identity_verified else None,
            "response_bytes": (
                None if final_response is None else len(final_response.encode("utf-8"))
            ),
            "usage": (
                None
                if usage is None
                else {
                    "input_tokens": usage.input_tokens,
                    "cached_input_tokens": usage.cached_input_tokens,
                    "output_tokens": usage.output_tokens,
                    "reasoning_output_tokens": usage.reasoning_output_tokens,
                    "estimated_cost_micro_usd": None,
                    "cost_authority": "unavailable",
                    "invoice_authority": False,
                }
            ),
        }

    try:
        if request.auth_mode is not RuntimeAuthModeV1.OPENAI_API:
            raise RuntimeError("SDK worker refuses non-API auth mode")
        if "OPENAI_API_KEY" not in os.environ:
            _emit(
                {
                    "schema_version": "1.0.0",
                    "event": "failure",
                    "reason_code": "missing_auth",
                    "turn_launched": False,
                    "completed_at": _timestamp(),
                    "duration_ms": 0,
                    "stream_bytes": 0,
                    "item_types": [],
                    **failure_evidence(),
                }
            )
            return 2
        _write_runtime_config(codex_home)
        codex_binary = _verify_codex_binary(bundled_codex_path())
        config = CodexConfig(
            codex_bin=str(codex_binary),
            launch_args_override=(
                str(codex_binary),
                "--strict-config",
                "app-server",
                "--listen",
                "stdio://",
            ),
            cwd=str(cwd),
            env={"CODEX_HOME": str(codex_home)},
            client_name="poker_bounded_codex_bridge",
            client_title="Poker bounded Codex bridge",
            client_version="1.0.0",
            experimental_api=True,
        )
        async with AsyncCodex(config) as codex:
            metadata = codex.metadata
            server = metadata.serverInfo
            server_name = None if server is None else server.name
            server_version = None if server is None else server.version
            if (
                server_name != "Codex Desktop"
                or not isinstance(server_version, str)
                or re.fullmatch(r"0\.144\.4(?:[ +(-].*)?", server_version) is None
            ):
                raise RuntimeError("Codex app-server identity mismatch")
            _emit(
                {
                    "schema_version": "1.0.0",
                    "event": "ready",
                    "runtime_identity": BRIDGE_OPENAI_API_RUNTIME_ID,
                    "server_name": server_name,
                    "server_version": server_version,
                }
            )
            thread = await codex.thread_start(
                approval_mode=ApprovalMode.deny_all,
                cwd=str(cwd),
                developer_instructions=request.developer_instructions,
                ephemeral=True,
                model=request.context.runtime_policy.model,
                personality=Personality.none,
                sandbox=Sandbox.read_only,
                service_tier="default",
            )
            schema: dict[str, Any] = role_output_schema_for_request(request)
            launched_at = _timestamp()
            _emit(
                {
                    "schema_version": "1.0.0",
                    "event": "turn_launch_intent",
                    "thread_id_sha256": _hash_identifier(thread.id),
                    "launched_at": launched_at,
                }
            )
            # Once the durable intent is emitted, the following await can transmit the
            # request before control returns, so any exception has an unknown remote effect.
            turn_effect_possible = True
            turn = await thread.turn(
                outbound_request_bytes(request).decode("utf-8"),
                approval_mode=ApprovalMode.deny_all,
                cwd=str(cwd),
                effort=ReasoningEffort.medium,
                model=request.context.runtime_policy.model,
                output_schema=schema,
                personality=Personality.none,
                sandbox=Sandbox.read_only,
                service_tier="default",
                summary=ReasoningSummary(root="none"),
            )
            _emit(
                {
                    "schema_version": "1.0.0",
                    "event": "turn_launched",
                    "thread_id_sha256": _hash_identifier(thread.id),
                    "turn_id_sha256": _hash_identifier(turn.id),
                    "launched_at": launched_at,
                }
            )
            final_response_count = 0
            completed = None
            interrupted = False
            settings_verified = False
            async for event in turn.stream():
                stream_bytes += _event_size(event)
                if stream_bytes > MAX_STREAM_BYTES:
                    if not interrupted:
                        await turn.interrupt()
                        interrupted = True
                    continue
                payload = event.payload
                if isinstance(payload, ThreadSettingsUpdatedNotification):
                    settings = payload.thread_settings
                    observed_model = settings.model
                    observed_model_provider = settings.model_provider
                    effort_value = getattr(settings.effort, "value", settings.effort)
                    tier_value = getattr(settings.service_tier, "value", settings.service_tier)
                    observed_reasoning_effort = (
                        effort_value if isinstance(effort_value, str) else None
                    )
                    observed_service_tier = tier_value if isinstance(tier_value, str) else None
                    sandbox_policy = settings.sandbox_policy.root
                    settings_verified = (
                        settings.model == request.context.runtime_policy.model
                        and settings.model_provider
                        == request.context.runtime_policy.model_provider
                        == BRIDGE_OPENAI_API_PROVIDER_ID
                        and observed_reasoning_effort
                        == request.context.runtime_policy.reasoning_effort
                        == BRIDGE_REASONING_EFFORT
                        and observed_service_tier
                        == request.context.runtime_policy.service_tier
                        == BRIDGE_SERVICE_TIER
                        and getattr(settings.approval_policy.root, "value", None) == "never"
                        and getattr(sandbox_policy, "type", None) == "readOnly"
                        and getattr(sandbox_policy, "network_access", None) is False
                    )
                    if not settings_verified and not interrupted:
                        item_types.add("runtime_settings_mismatch")
                        await turn.interrupt()
                        interrupted = True
                if isinstance(payload, ModelReroutedNotification):
                    item_types.add("model_rerouted")
                    if not interrupted:
                        await turn.interrupt()
                        interrupted = True
                if isinstance(payload, ItemStartedNotification | ItemCompletedNotification):
                    item = payload.item.root
                    item_type = _item_type_name(str(item.type))
                    item_types.add(item_type)
                    if not isinstance(item, AgentMessageThreadItem | ReasoningThreadItem):
                        if not interrupted:
                            await turn.interrupt()
                            interrupted = True
                    elif isinstance(payload, ItemCompletedNotification) and isinstance(
                        item, AgentMessageThreadItem
                    ):
                        if item.phase not in {None, MessagePhase.final_answer}:
                            item_types.add("agent_message_nonfinal")
                            if not interrupted:
                                await turn.interrupt()
                                interrupted = True
                        else:
                            final_response_count += 1
                            final_response = item.text
                            if final_response_count > 1:
                                item_types.add("multiple_agent_messages")
                                if not interrupted:
                                    await turn.interrupt()
                                    interrupted = True
                if isinstance(payload, ThreadTokenUsageUpdatedNotification):
                    usage = payload.token_usage.last
                if isinstance(payload, TurnCompletedNotification):
                    completed = payload.turn
            completed_at = _timestamp()
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            unexpected = tuple(
                sorted(
                    item_types - {"agent_message", "reasoning"},
                    key=lambda item: item.encode("utf-8"),
                )
            )
            if completed is None:
                raise RuntimeError("turn completion was not observed")
            if interrupted or completed.status is TurnStatus.interrupted:
                _emit(
                    {
                        "schema_version": "1.0.0",
                        "event": "failure",
                        "reason_code": (
                            "unexpected_runtime_item" if unexpected else "stream_cap_exceeded"
                        ),
                        "turn_launched": True,
                        "completed_at": completed_at,
                        "duration_ms": duration_ms,
                        "stream_bytes": stream_bytes,
                        "item_types": list(unexpected),
                        **failure_evidence(),
                    }
                )
                return 3
            if (
                completed.status is not TurnStatus.completed
                or final_response is None
                or not settings_verified
                or observed_model is None
                or observed_model_provider is None
                or observed_reasoning_effort is None
                or observed_service_tier is None
            ):
                raise RuntimeError("turn did not produce a final response")
            response_bytes = final_response.encode("utf-8")
            if len(response_bytes) > MAX_RESPONSE_BYTES:
                raise RuntimeError("response exceeded the local byte cap")
            validated = parse_canonical_model(response_bytes, BridgeRoleOutputV1)
            response_bytes = canonical_json_bytes(validated)
            if usage is None:
                raise RuntimeError("turn usage was not observed")
            _emit(
                {
                    "schema_version": "1.0.0",
                    "event": "result",
                    "runtime_identity": request.context.runtime_policy.runtime_identity,
                    "observed_model": observed_model,
                    "observed_model_provider": observed_model_provider,
                    "observed_reasoning_effort": observed_reasoning_effort,
                    "observed_service_tier": observed_service_tier,
                    "response_base64": base64.b64encode(response_bytes).decode("ascii"),
                    "completed_at": completed_at,
                    "duration_ms": duration_ms,
                    "stream_bytes": stream_bytes,
                    "item_types": sorted(item_types, key=lambda item: item.encode("utf-8")),
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "cached_input_tokens": usage.cached_input_tokens,
                        "output_tokens": usage.output_tokens,
                        "reasoning_output_tokens": usage.reasoning_output_tokens,
                        "estimated_cost_micro_usd": None,
                        "cost_authority": "unavailable",
                        "invoice_authority": False,
                    },
                }
            )
            return 0
    except Exception:
        _emit(
            {
                "schema_version": "1.0.0",
                "event": "failure",
                "reason_code": (
                    "worker_exception_after_launch"
                    if turn_effect_possible
                    else "worker_exception_before_launch"
                ),
                "turn_launched": turn_effect_possible,
                "completed_at": _timestamp(),
                "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
                "stream_bytes": 0,
                "item_types": [],
                **failure_evidence(),
            }
        )
        return 4


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--codex-home", required=True)
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--repository-root", required=True)
    parser.add_argument("--repository-commit", required=True)
    parser.add_argument("--repository-tree", required=True)
    args = parser.parse_args(argv)
    codex_home = Path(args.codex_home).resolve()
    cwd = Path(args.cwd).resolve()
    repository_root = Path(args.repository_root).resolve()
    if (
        not codex_home.is_dir()
        or any(codex_home.iterdir())
        or not cwd.is_dir()
        or any(cwd.iterdir())
    ):
        return 5
    request_bytes = sys.stdin.buffer.read(MAX_CONTEXT_BYTES + 1)
    if len(request_bytes) > MAX_CONTEXT_BYTES:
        return 6
    try:
        request = parse_canonical_model(request_bytes, BoundedCodexBridgeRequestV1)
    except Exception:
        return 7
    try:
        verify_bridge_checkout(
            repository_root,
            repository_commit_id=args.repository_commit,
            repository_tree_id=args.repository_tree,
        )
        verify_bridge_module_origins(repository_root)
    except Exception:
        _emit(
            {
                "schema_version": "1.0.0",
                "event": "failure",
                "reason_code": "worker_repository_identity_rejected",
                "turn_launched": False,
                "completed_at": _timestamp(),
                "duration_ms": 0,
                "stream_bytes": 0,
                "item_types": [],
                "runtime_identity": None,
                "observed_model": None,
                "observed_model_provider": None,
                "observed_reasoning_effort": None,
                "observed_service_tier": None,
                "response_bytes": None,
                "usage": None,
            }
        )
        return 8
    return asyncio.run(_run(request, codex_home, cwd))


if __name__ == "__main__":
    raise SystemExit(main())
