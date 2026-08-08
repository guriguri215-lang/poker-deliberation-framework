from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import inspect
import sys
import tomllib
import types
from datetime import UTC, datetime
from pathlib import Path
from tempfile import mkdtemp

import pytest

import poker_deliberation.codex_bridge.sdk_transport as sdk_transport_module
import poker_deliberation.codex_bridge.sdk_worker as sdk_worker_module
from poker_deliberation.codex_bridge.models import (
    BRIDGE_RUNTIME_BINARY_SHA256,
    BoundedCodexBridgeRequestV1,
    BridgeEffectState,
    BridgeTransportUsageV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.sdk_transport import OpenAIAPITransport
from poker_deliberation.codex_bridge.sdk_worker import _file_sha256, _write_runtime_config
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    DeterministicReadOnlyTransport,
)
from tests.codex_bridge_support import prepared_bridge_request


@pytest.fixture(scope="module")
def sdk_request() -> BoundedCodexBridgeRequestV1:
    return prepared_bridge_request(
        Path(mkdtemp(prefix="p25b-sdk-")) / "p3",
        auth_mode=RuntimeAuthModeV1.OPENAI_API,
    )


def _enable_unqualified_contract_probe(
    transport: OpenAIAPITransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reach only synthetic worker fixtures; the product execute path remains fail-closed."""

    monkeypatch.setattr(transport, "_require_api_live_qualification", lambda: None)


def test_sdk_transport_fails_before_process_without_dedicated_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    request = prepared_bridge_request(tmp_path / "p3", auth_mode=RuntimeAuthModeV1.OPENAI_API)
    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        repository_root=Path(__file__).resolve().parents[2],
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "missing_auth"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED
    assert not (tmp_path / "runtime").exists()


def test_sdk_transport_rejects_cross_mode_before_credentials_or_worker_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    request = prepared_bridge_request(
        tmp_path / "p3",
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
    )
    credential_checks = 0
    worker_factory_calls = 0
    popen_calls = 0

    def credential_check() -> None:
        nonlocal credential_checks
        credential_checks += 1

    def worker_factory(_home: Path, _cwd: Path) -> list[str]:
        nonlocal worker_factory_calls
        worker_factory_calls += 1
        return [sys.executable, "worker.py"]

    def popen(*_args: object, **_kwargs: object) -> None:
        nonlocal popen_calls
        popen_calls += 1
        raise AssertionError("Popen must not be reached for a cross-mode request")

    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        worker_command_factory=worker_factory,
    )
    monkeypatch.setattr(transport, "_require_credential_reference", credential_check)
    monkeypatch.setattr(sdk_transport_module.subprocess, "Popen", popen)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "transport_auth_mode_mismatch"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED
    assert caught.value.launched_at is None
    assert caught.value.duration_ms == 0
    assert caught.value.stream_bytes == 0
    assert credential_checks == 0
    assert worker_factory_calls == 0
    assert popen_calls == 0
    assert not (tmp_path / "runtime").exists()


def test_sdk_transport_refuses_live_api_without_versioned_cost_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    request = prepared_bridge_request(tmp_path / "p3", auth_mode=RuntimeAuthModeV1.OPENAI_API)
    worker_factory_calls = 0
    popen_calls = 0

    def worker_factory(_home: Path, _cwd: Path) -> list[str]:
        nonlocal worker_factory_calls
        worker_factory_calls += 1
        return [sys.executable, "worker.py"]

    def popen(*_args: object, **_kwargs: object) -> None:
        nonlocal popen_calls
        popen_calls += 1
        raise AssertionError("unqualified API adapter must not create a process")

    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        worker_command_factory=worker_factory,
    )
    monkeypatch.setattr(sdk_transport_module.subprocess, "Popen", popen)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "api_live_execution_unqualified_cost_authority"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED
    assert caught.value.launched_at is None
    assert worker_factory_calls == 0
    assert popen_calls == 0
    assert not (tmp_path / "runtime").exists()
    assert "synthetic-secret-canary" not in str(caught.value)


def test_worker_never_copies_configured_cap_into_observed_cost() -> None:
    unavailable = BridgeTransportUsageV1(
        input_tokens=1,
        cached_input_tokens=0,
        output_tokens=1,
        reasoning_output_tokens=0,
        estimated_cost_micro_usd=None,
        cost_authority="unavailable",
    )
    assert unavailable.estimated_cost_micro_usd is None
    worker_source = inspect.getsource(sdk_worker_module)
    assert "budget.max_cost_micro_usd" not in worker_source
    assert '"cost_authority": "unavailable"' in worker_source


def test_sdk_transport_sanitizes_environment_and_tracks_post_launch_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    monkeypatch.setenv("UNRELATED_SECRET_CANARY", "must-not-cross-worker-boundary")
    request = prepared_bridge_request(tmp_path / "p3", auth_mode=RuntimeAuthModeV1.OPENAI_API)
    script = tmp_path / "fake_worker.py"
    script.write_text(
        """import datetime
import json
import os
import sys


def emit(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(\",\", \":\")
    )
    print(encoded, flush=True)


assert os.environ.get(\"OPENAI_API_KEY\") == \"synthetic-secret-canary\"
assert \"P2_025B_OPENAI_API_KEY\" not in os.environ
assert \"UNRELATED_SECRET_CANARY\" not in os.environ
sys.stdin.buffer.read()
now = datetime.datetime.now(datetime.UTC).isoformat().replace(\"+00:00\", \"Z\")
emit({
    \"schema_version\": \"1.0.0\",
    \"event\": \"ready\",
    \"runtime_identity\": \"openai-codex-python-sdk/0.144.4+codex-cli/0.144.4\",
    \"server_name\": \"synthetic-worker\",
    \"server_version\": \"0.144.4\",
})
emit({
    \"schema_version\": \"1.0.0\",
    \"event\": \"turn_launch_intent\",
    \"thread_id_sha256\": \"a\" * 64,
    \"launched_at\": now,
})
emit({
    \"schema_version\": \"1.0.0\",
    \"event\": \"turn_launched\",
    \"thread_id_sha256\": \"a\" * 64,
    \"turn_id_sha256\": \"b\" * 64,
    \"launched_at\": now,
})
emit({
    \"schema_version\": \"1.0.0\",
    \"event\": \"failure\",
    \"reason_code\": \"synthetic_post_launch_failure\",
    \"turn_launched\": True,
    \"completed_at\": now,
    \"duration_ms\": 1,
    \"stream_bytes\": 1,
    \"item_types\": [],
    \"runtime_identity\": \"openai-codex-python-sdk/0.144.4+codex-cli/0.144.4\",
    \"observed_model\": \"gpt-5.6-terra\",
    \"observed_model_provider\": \"openai_responses_api_no_retry\",
    \"observed_reasoning_effort\": \"medium\",
    \"observed_service_tier\": \"default\",
    \"response_bytes\": 17,
    \"usage\": {
        \"input_tokens\": 123,
        \"cached_input_tokens\": 0,
        \"output_tokens\": 45,
        \"reasoning_output_tokens\": 6,
        \"estimated_cost_micro_usd\": 7,
        \"cost_authority\": \"estimate\",
        \"invoice_authority\": False,
    },
})
raise SystemExit(4)
""",
        encoding="utf-8",
        newline="\n",
    )
    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        worker_command_factory=lambda _home, _cwd: [sys.executable, str(script)],
    )
    _enable_unqualified_contract_probe(transport, monkeypatch)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "synthetic_post_launch_failure"
    assert caught.value.effect_state is BridgeEffectState.EFFECT_UNKNOWN
    assert caught.value.evidence is not None
    assert caught.value.evidence.response_bytes == 17
    assert caught.value.evidence.usage is not None
    assert caught.value.evidence.usage.input_tokens == 123
    assert caught.value.evidence.usage.output_tokens == 45
    assert caught.value.evidence.usage.reasoning_output_tokens == 6
    assert caught.value.evidence.observed_model == "gpt-5.6-terra"
    assert "synthetic-secret-canary" not in str(caught.value)
    assert "must-not-cross-worker-boundary" not in str(caught.value)


def test_launch_intent_then_turn_await_failure_is_effect_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_request: BoundedCodexBridgeRequestV1,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    script = tmp_path / "turn_await_failure.py"
    script.write_text(
        """import datetime
import json
import sys


def emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)


sys.stdin.buffer.read()
now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
emit({
    "schema_version": "1.0.0",
    "event": "ready",
    "runtime_identity": "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    "server_name": "Codex Desktop",
    "server_version": "0.144.4",
})
emit({
    "schema_version": "1.0.0",
    "event": "turn_launch_intent",
    "thread_id_sha256": "a" * 64,
    "launched_at": now,
})
emit({
    "schema_version": "1.0.0",
    "event": "failure",
    "reason_code": "worker_exception_after_launch",
    "turn_launched": True,
    "completed_at": now,
    "duration_ms": 1,
    "stream_bytes": 0,
    "item_types": [],
    "runtime_identity": None,
    "observed_model": None,
    "observed_model_provider": None,
    "observed_reasoning_effort": None,
    "observed_service_tier": None,
    "response_bytes": None,
    "usage": None,
})
raise SystemExit(4)
""",
        encoding="utf-8",
        newline="\n",
    )
    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        worker_command_factory=lambda _home, _cwd: [sys.executable, str(script)],
    )
    _enable_unqualified_contract_probe(transport, monkeypatch)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(sdk_request)

    assert caught.value.reason_code == "worker_exception_after_launch"
    assert caught.value.effect_state is BridgeEffectState.EFFECT_UNKNOWN
    assert caught.value.launched_at is not None
    assert caught.value.thread_id_sha256 is None
    assert caught.value.turn_id_sha256 is None


def test_sdk_worker_flushes_launch_intent_before_awaiting_turn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_request: BoundedCodexBridgeRequestV1,
) -> None:
    emitted: list[dict[str, object]] = []
    turn_called = False

    class _FakeTurnThread:
        id = "synthetic-thread-id"

        async def turn(self, *_args: object, **_kwargs: object) -> object:
            nonlocal turn_called
            turn_called = True
            assert emitted[-1]["event"] == "turn_launch_intent"
            raise RuntimeError("synthetic await failure")

    class _FakeAsyncCodex:
        metadata = types.SimpleNamespace(
            serverInfo=types.SimpleNamespace(name="Codex Desktop", version="0.144.4")
        )

        def __init__(self, _config: object) -> None:
            pass

        async def __aenter__(self) -> _FakeAsyncCodex:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def thread_start(self, **_kwargs: object) -> _FakeTurnThread:
            return _FakeTurnThread()

    class _FakeConfig:
        def __init__(self, **_kwargs: object) -> None:
            pass

    openai_codex = types.ModuleType("openai_codex")
    openai_codex.ApprovalMode = types.SimpleNamespace(deny_all="deny_all")
    openai_codex.AsyncCodex = _FakeAsyncCodex
    openai_codex.CodexConfig = _FakeConfig
    openai_codex.Sandbox = types.SimpleNamespace(read_only="read_only")
    generated = types.ModuleType("openai_codex.generated.v2_all")
    for name in (
        "AgentMessageThreadItem",
        "ItemCompletedNotification",
        "ItemStartedNotification",
        "ModelReroutedNotification",
        "ReasoningThreadItem",
        "ThreadSettingsUpdatedNotification",
        "ThreadTokenUsageUpdatedNotification",
        "TurnCompletedNotification",
    ):
        setattr(generated, name, type(name, (), {}))
    generated.MessagePhase = types.SimpleNamespace(final_answer="final_answer")
    generated.Personality = types.SimpleNamespace(none="none")
    generated.ReasoningEffort = types.SimpleNamespace(medium="medium")
    generated.ReasoningSummary = _FakeConfig
    generated.TurnStatus = types.SimpleNamespace(interrupted="interrupted", completed="completed")
    codex_cli_bin = types.ModuleType("codex_cli_bin")
    codex_cli_bin.bundled_codex_path = lambda: tmp_path / "codex.exe"
    monkeypatch.setitem(sys.modules, "openai_codex", openai_codex)
    monkeypatch.setitem(sys.modules, "openai_codex.generated.v2_all", generated)
    monkeypatch.setitem(sys.modules, "codex_cli_bin", codex_cli_bin)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    monkeypatch.setattr(sdk_worker_module, "_emit", emitted.append)
    monkeypatch.setattr(sdk_worker_module, "_write_runtime_config", lambda _path: None)
    monkeypatch.setattr(
        sdk_worker_module,
        "_verify_codex_binary",
        lambda path: path,
    )

    result = asyncio.run(sdk_worker_module._run(sdk_request, tmp_path / "home", tmp_path / "cwd"))

    assert result == 4
    assert turn_called is True
    assert [item["event"] for item in emitted] == ["ready", "turn_launch_intent", "failure"]
    assert emitted[-1]["reason_code"] == "worker_exception_after_launch"
    assert emitted[-1]["turn_launched"] is True
    assert "synthetic-secret-canary" not in str(emitted)


def test_worker_config_is_strict_read_only_and_disables_provider_retries(
    tmp_path: Path,
) -> None:
    codex_home = tmp_path / "home"
    codex_home.mkdir()
    _write_runtime_config(codex_home)

    parsed = tomllib.loads((codex_home / "config.toml").read_text(encoding="utf-8"))
    provider_id = "openai_responses_api_no_retry"
    provider = parsed["model_providers"][provider_id]
    assert parsed["model_provider"] == provider_id
    assert parsed["approval_policy"] == "never"
    assert parsed["sandbox_mode"] == "read-only"
    assert parsed["history"]["persistence"] == "none"
    assert parsed["features"]["multi_agent"] is False
    assert parsed["features"]["shell_tool"] is False
    assert parsed["web_search"] == "disabled"
    assert provider == {
        "name": "P2-025B OpenAI no retry",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "wire_api": "responses",
        "request_max_retries": 0,
        "stream_max_retries": 0,
        "stream_idle_timeout_ms": 120000,
    }


def test_exact_sdk_cli_license_and_bundled_binary_identity() -> None:
    from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]

    assert importlib.metadata.version("openai-codex") == "0.144.4"
    assert importlib.metadata.version("openai-codex-cli-bin") == "0.144.4"
    for distribution in ("openai-codex", "openai-codex-cli-bin"):
        metadata = importlib.metadata.metadata(distribution)
        assert (metadata.get("License-Expression") or metadata.get("License")) == "Apache-2.0"
    assert _file_sha256(bundled_codex_path()) == BRIDGE_RUNTIME_BINARY_SHA256


def test_default_worker_command_rebinds_the_candidate_checkout(tmp_path: Path) -> None:
    repository = Path(__file__).resolve().parents[2]
    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        repository_root=repository,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
    )

    command = transport._default_command(tmp_path / "home", tmp_path / "cwd")

    assert command[-6:] == [
        "--repository-root",
        str(repository),
        "--repository-commit",
        "1" * 40,
        "--repository-tree",
        "2" * 40,
    ]


@pytest.mark.parametrize(
    ("server_name", "accepted"),
    (("Codex Desktop", True), ("synthetic-worker", False)),
)
def test_sdk_transport_validates_success_server_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_request: BoundedCodexBridgeRequestV1,
    server_name: str,
    accepted: bool,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    now = datetime(2030, 1, 1, tzinfo=UTC)
    fixture = DeterministicReadOnlyTransport(
        auth_mode=RuntimeAuthModeV1.OPENAI_API,
        clock=lambda: now,
    ).execute(sdk_request)
    response_base64 = base64.b64encode(fixture.response_bytes).decode("ascii")
    script = tmp_path / "result_worker.py"
    script.write_text(
        f"""import datetime
import json
import sys


def emit(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), flush=True)


sys.stdin.buffer.read()
now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
emit({{
    "schema_version": "1.0.0",
    "event": "ready",
    "runtime_identity": "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    "server_name": {server_name!r},
    "server_version": "0.144.4",
}})
emit({{
    "schema_version": "1.0.0",
    "event": "turn_launch_intent",
    "thread_id_sha256": "a" * 64,
    "launched_at": now,
}})
emit({{
    "schema_version": "1.0.0",
    "event": "turn_launched",
    "thread_id_sha256": "a" * 64,
    "turn_id_sha256": "b" * 64,
    "launched_at": now,
}})
emit({{
    "schema_version": "1.0.0",
    "event": "result",
    "runtime_identity": "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    "observed_model": "gpt-5.6-terra",
    "observed_model_provider": "openai_responses_api_no_retry",
    "observed_reasoning_effort": "medium",
    "observed_service_tier": "default",
    "response_base64": {response_base64!r},
    "completed_at": now,
    "duration_ms": 1,
    "stream_bytes": {len(fixture.response_bytes)},
    "item_types": ["agent_message"],
    "usage": {{
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "estimated_cost_micro_usd": 0,
        "cost_authority": "estimate",
        "invoice_authority": False,
    }},
}})
""",
        encoding="utf-8",
        newline="\n",
    )
    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        worker_command_factory=lambda _home, _cwd: [sys.executable, str(script)],
    )
    _enable_unqualified_contract_probe(transport, monkeypatch)

    if accepted:
        assert transport.execute(sdk_request).response_bytes == fixture.response_bytes
    else:
        with pytest.raises(BridgeTransportFailure, match="worker_protocol_or_exit_mismatch"):
            transport.execute(sdk_request)


def _write_waiting_worker(path: Path, *, launched: bool, overflow: bool = False) -> None:
    launch_event = ""
    if launched:
        launch_event = """
emit({
    "schema_version": "1.0.0",
    "event": "turn_launched",
    "thread_id_sha256": "a" * 64,
    "turn_id_sha256": "b" * 64,
    "launched_at": now,
})
"""
    overflow_output = ""
    if overflow:
        overflow_output = "sys.stdout.write('x' * 8192)\nsys.stdout.flush()\n"
    path.write_text(
        f"""import datetime
import json
import sys
import time


def emit(value):
    print(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        flush=True,
    )


sys.stdin.buffer.read()
now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
emit({{
    "schema_version": "1.0.0",
    "event": "ready",
    "runtime_identity": "openai-codex-python-sdk/0.144.4+codex-cli/0.144.4",
    "server_name": "synthetic-worker",
    "server_version": "0.144.4",
}})
emit({{
    "schema_version": "1.0.0",
    "event": "turn_launch_intent",
    "thread_id_sha256": "a" * 64,
    "launched_at": now,
}})
{launch_event}
{overflow_output}
time.sleep(10)
""",
        encoding="utf-8",
        newline="\n",
    )


@pytest.mark.parametrize(
    ("launched", "expected"),
    (
        (False, BridgeEffectState.EFFECT_UNKNOWN),
        (True, BridgeEffectState.CANCEL_UNCONFIRMED),
    ),
)
def test_sdk_transport_timeout_reports_only_observable_effect_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_request: BoundedCodexBridgeRequestV1,
    launched: bool,
    expected: BridgeEffectState,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    monkeypatch.setattr(sdk_transport_module, "MAX_ROLE_RUNTIME_MS", 1000)
    script = tmp_path / "waiter.py"
    _write_waiting_worker(script, launched=launched)
    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        worker_command_factory=lambda _home, _cwd: [sys.executable, str(script)],
    )
    _enable_unqualified_contract_probe(transport, monkeypatch)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(sdk_request)

    assert caught.value.reason_code == "worker_timeout"
    assert caught.value.effect_state is expected
    assert (caught.value.thread_id_sha256 is not None) is launched


def test_sdk_transport_output_cap_preserves_prior_launch_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_request: BoundedCodexBridgeRequestV1,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    monkeypatch.setattr(sdk_transport_module, "_WORKER_OUTPUT_CAP", 1024)
    script = tmp_path / "overflow.py"
    _write_waiting_worker(script, launched=True, overflow=True)
    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        worker_command_factory=lambda _home, _cwd: [sys.executable, str(script)],
    )
    _enable_unqualified_contract_probe(transport, monkeypatch)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(sdk_request)

    assert caught.value.reason_code == "worker_output_cap_exceeded"
    assert caught.value.effect_state is BridgeEffectState.EFFECT_UNKNOWN


def test_sdk_transport_runtime_unavailable_is_not_launched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sdk_request: BoundedCodexBridgeRequestV1,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-secret-canary")
    transport = OpenAIAPITransport(
        tmp_path / "runtime",
        worker_command_factory=lambda _home, _cwd: [str(tmp_path / "missing-runtime.exe")],
    )
    _enable_unqualified_contract_probe(transport, monkeypatch)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(sdk_request)

    assert caught.value.reason_code == "worker_process_launch_failed"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED
