from __future__ import annotations

import base64
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import poker_deliberation.codex_bridge.subscription_transport as subscription_module
from poker_deliberation.codex_bridge.canonical import canonical_json_bytes, sha256_bytes
from poker_deliberation.codex_bridge.contracts import role_output_schema_for_request
from poker_deliberation.codex_bridge.models import (
    BRIDGE_RUNTIME_BINARY_SHA256,
    BridgeEffectState,
    CodexSubscriptionLiveExecutionEvidenceV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.runtime_scratch import PreparedRuntimeRoot
from poker_deliberation.codex_bridge.subscription_transport import (
    CodexSubscriptionCliTransport,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    DeterministicReadOnlyTransport,
)
from tests.codex_bridge_support import prepared_bridge_request


def _transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_present: bool,
    command_factory=None,  # type: ignore[no-untyped-def]
) -> CodexSubscriptionCliTransport:
    codex_home = tmp_path / "credential-codex-home"
    skill = codex_home / "skills" / "fixture-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# disabled fixture skill\n", encoding="utf-8")
    monkeypatch.setattr(
        subscription_module,
        "_file_sha256",
        lambda _path: BRIDGE_RUNTIME_BINARY_SHA256,
    )
    return CodexSubscriptionCliTransport(
        tmp_path / "runtime",
        codex_binary=Path(sys.executable),
        auth_status_probe=lambda _cwd, _env: auth_present,
        command_factory=command_factory,
        isolation_root=tmp_path / "isolated-execution",
        credential_codex_home=codex_home,
    )


def test_subscription_command_is_ephemeral_read_only_and_tools_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _transport(tmp_path, monkeypatch, auth_present=True)
    plugin_skill = (
        transport.credential_codex_home
        / "plugins"
        / "cache"
        / "fixture-plugin"
        / "skills"
        / "plugin-skill"
    )
    plugin_skill.mkdir(parents=True)
    (plugin_skill / "SKILL.md").write_text("# disabled plugin skill\n", encoding="utf-8")
    command = transport._command(
        cwd=tmp_path,
        schema=tmp_path / "schema.json",
        output=tmp_path / "output.json",
        skill_snapshot=transport._skill_snapshot(),
    )
    joined = " ".join(command)

    assert command[:4] == [str(Path(sys.executable).resolve()), "--strict-config", "-a", "never"]
    assert "exec" in command
    assert "--ephemeral" in command
    assert "--ignore-user-config" in command
    assert "--ignore-rules" in command
    assert "read-only" in command
    assert 'forced_login_method="chatgpt"' in command
    assert 'model_provider="openai"' in command
    assert "project_doc_max_bytes=0" in command
    assert "project_doc_fallback_filenames=[]" in command
    assert "features.shell_tool=false" in command
    assert "features.multi_agent=false" in command
    assert "features.apps=false" in command
    assert "features.browser_use=false" in command
    assert "mcp_servers={}" in command
    skill_config = next(item for item in command if item.startswith("skills.config="))
    assert "enabled=false" in skill_config
    assert "fixture-skill" in skill_config
    assert "plugin-skill" in skill_config
    assert "SKILL.md" in skill_config
    assert skill_config.count("enabled=false") == 2
    assert "skills.config=[]" not in command
    assert "OPENAI_API_KEY" not in joined


def test_subscription_constructor_labels_never_claim_actual_live(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subscription_module,
        "_file_sha256",
        lambda _path: BRIDGE_RUNTIME_BINARY_SHA256,
    )
    product = CodexSubscriptionCliTransport(
        tmp_path / "product-runtime",
        codex_binary=Path(sys.executable),
        isolation_root=tmp_path / "product-isolation",
        credential_codex_home=tmp_path / "credential-codex-home",
    )
    injected_auth = CodexSubscriptionCliTransport(
        tmp_path / "auth-fixture-runtime",
        codex_binary=Path(sys.executable),
        auth_status_probe=lambda _cwd, _env: True,
        isolation_root=tmp_path / "auth-fixture-isolation",
        credential_codex_home=tmp_path / "credential-codex-home",
    )
    injected_process = CodexSubscriptionCliTransport(
        tmp_path / "process-fixture-runtime",
        codex_binary=Path(sys.executable),
        command_factory=lambda _cwd, _schema, _output: [sys.executable],
        isolation_root=tmp_path / "process-fixture-isolation",
        credential_codex_home=tmp_path / "credential-codex-home",
    )

    # A constructor/mutable attribute is not execution evidence. Even an otherwise
    # default-looking object remains deterministic until a sealed process completes.
    assert product.transport_qualification == "deterministic_fixture"
    assert injected_auth.transport_qualification == "deterministic_fixture"
    assert injected_process.transport_qualification == "deterministic_fixture"


def test_wrapped_transport_cannot_relay_typed_live_attestation(
    tmp_path: Path,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    result = DeterministicReadOnlyTransport(
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    ).execute(request)
    evidence = CodexSubscriptionCliTransport._live_execution_evidence(
        request,
        runtime_source_inventory_sha256="a" * 64,
        runtime_configuration_sha256="b" * 64,
        output_schema_sha256=sha256_bytes(
            canonical_json_bytes(role_output_schema_for_request(request))
        ),
        command_contract_sha256="c" * 64,
        launch_intent_sha256="d" * 64,
        response=result.response_bytes,
        raw_events=b"{}\n",
        usage=result.usage,
        thread_id_sha256=result.thread_id_sha256,
        turn_id_sha256=result.turn_id_sha256,
    )
    relayed = replace(
        result,
        transport_qualification="actual_live",
        live_execution_evidence=evidence,
        _live_execution_capability=subscription_module._SEALED_LIVE_EXECUTION_CAPABILITY,
    )

    with pytest.raises(ValueError, match="unsealed subscription"):
        subscription_module.validated_sealed_live_execution(object(), request, relayed)

    mutated = evidence.model_dump(mode="python")
    mutated["response_bytes_sha256"] = "e" * 64
    with pytest.raises(ValueError, match="runtime hash mismatch"):
        CodexSubscriptionLiveExecutionEvidenceV1.model_validate(mutated, strict=True)


def test_subscription_missing_login_fails_before_process_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=False,
        command_factory=lambda *_args: pytest.fail("model process must not launch"),
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "missing_subscription_auth"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED


@pytest.mark.parametrize(
    ("stdout", "stderr"),
    (
        (b"Logged in using ChatGPT\n", b""),
        (b"", b"Logged in using ChatGPT\n"),
    ),
)
def test_subscription_login_status_accepts_exact_message_on_one_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
) -> None:
    monkeypatch.setattr(
        subscription_module,
        "_file_sha256",
        lambda _path: BRIDGE_RUNTIME_BINARY_SHA256,
    )
    monkeypatch.setattr(
        subscription_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=stdout,
            stderr=stderr,
        ),
    )
    transport = CodexSubscriptionCliTransport(
        tmp_path / "runtime",
        codex_binary=Path(sys.executable),
        isolation_root=tmp_path / "isolated-execution",
        credential_codex_home=tmp_path / "credential-codex-home",
    )

    transport._probe_auth(cwd=tmp_path, environment={})


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    (
        (0, b"Logged in using an API key\n", b""),
        (0, b"Logged in using ChatGPT\n", b"warning\n"),
        (0, b"Logged in using ChatGPT\n", b"Logged in using ChatGPT\n"),
        (1, b"", b"Logged in using ChatGPT\n"),
    ),
)
def test_subscription_login_status_rejects_nonexact_or_ambiguous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int,
    stdout: bytes,
    stderr: bytes,
) -> None:
    monkeypatch.setattr(
        subscription_module,
        "_file_sha256",
        lambda _path: BRIDGE_RUNTIME_BINARY_SHA256,
    )
    monkeypatch.setattr(
        subscription_module.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args[0],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        ),
    )
    transport = CodexSubscriptionCliTransport(
        tmp_path / "runtime",
        codex_binary=Path(sys.executable),
        isolation_root=tmp_path / "isolated-execution",
        credential_codex_home=tmp_path / "credential-codex-home",
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport._probe_auth(cwd=tmp_path, environment={})

    assert caught.value.reason_code == "missing_subscription_auth"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED


@pytest.mark.parametrize("item_type", ("agent_message", "command_execution", "error"))
def test_subscription_jsonl_transport_accepts_only_read_only_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    item_type: str,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    fixed = datetime(2030, 1, 1, tzinfo=UTC)
    response = (
        DeterministicReadOnlyTransport(
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            clock=lambda: fixed,
        )
        .execute(request)
        .response_bytes
    )
    script = tmp_path / "fake_codex.py"
    script.write_text(
        """import base64
import json
import os
import pathlib
import sys

output = pathlib.Path(sys.argv[1])
response = base64.b64decode(sys.argv[2])
item_type = sys.argv[3]
payload = json.loads(sys.stdin.buffer.read())
assert payload["auth_mode"] == "codex_subscription"
assert "OPENAI_API_KEY" not in os.environ
assert "CODEX_API_KEY" not in os.environ
assert "UNRELATED_SECRET_CANARY" not in os.environ
assert "PATH" not in os.environ
assert pathlib.Path.cwd() != output.parent
assert not pathlib.Path.cwd().is_relative_to(output.parent.parent)
assert pathlib.Path(os.environ["HOME"]) != output.parent.parent
assert os.environ["HOME"] == os.environ["USERPROFILE"]
assert os.environ["TEMP"] == os.environ["TMP"]
output.write_bytes(response)
events = [
    {"type": "thread.started", "thread_id": "thread-public-fixture"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item-1", "type": item_type}},
    {"type": "turn.completed", "usage": {
        "input_tokens": 123,
        "cached_input_tokens": 0,
        "output_tokens": 45,
        "reasoning_output_tokens": 6,
    }},
]
for event in events:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
""",
        encoding="utf-8",
        newline="\n",
    )
    encoded = base64.b64encode(response).decode("ascii")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-api-key-canary")
    monkeypatch.setenv("CODEX_API_KEY", "synthetic-codex-key-canary")
    monkeypatch.setenv("UNRELATED_SECRET_CANARY", "synthetic-unrelated-canary")
    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=lambda _attempt, _schema, output: [
            sys.executable,
            str(script),
            str(output),
            encoded,
            item_type,
        ],
    )

    if item_type == "agent_message":
        result = transport.execute(request)
        assert result.response_bytes == response
        assert result.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
        assert result.transport_qualification == "deterministic_fixture"
        assert result.item_types == ("agent_message",)
        assert result.usage.estimated_cost_micro_usd is None
        assert result.model_identity_evidence == "requested_pinned_no_fallback_no_reroute"
        assert result.observed_model is None
        assert result.observed_model_provider is None
        assert result.observed_reasoning_effort is None
        assert result.observed_service_tier is None
        raw = next((tmp_path / "runtime").rglob("raw-events.jsonl")).read_bytes()
        schema_bytes = next((tmp_path / "runtime").rglob("output-schema.json")).read_bytes()
        assert raw.endswith(b"\n")
        assert schema_bytes == canonical_json_bytes(role_output_schema_for_request(request))
        assert canonical_json_bytes(request).find(b"synthetic-api-key-canary") == -1
    else:
        with pytest.raises(BridgeTransportFailure) as caught:
            transport.execute(request)
        assert caught.value.reason_code == "subscription_protocol_or_output_invalid"
        assert caught.value.effect_state is BridgeEffectState.FAILED
        assert caught.value.evidence is not None
        assert caught.value.evidence.response_bytes == len(response)
        assert caught.value.evidence.usage is not None
        assert caught.value.evidence.usage.input_tokens == 123
        assert caught.value.evidence.usage.output_tokens == 45
        assert caught.value.evidence.usage.reasoning_output_tokens == 6
        assert caught.value.evidence.observed_model is None
        assert caught.value.evidence.observed_model_provider is None
        assert caught.value.evidence.model_identity_evidence == (
            "unavailable" if item_type == "error" else "requested_pinned_no_fallback_no_reroute"
        )


def test_subscription_empty_output_and_nonzero_exit_keep_known_usage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    script = tmp_path / "empty_failed_codex.py"
    script.write_text(
        """import json
import pathlib
import sys

pathlib.Path(sys.argv[1]).write_bytes(b"")
sys.stdin.buffer.read()
events = [
    {"type": "thread.started", "thread_id": "thread-empty-output"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item-1", "type": "agent_message"}},
    {"type": "turn.completed", "usage": {
        "input_tokens": 123,
        "cached_input_tokens": 0,
        "output_tokens": 45,
        "reasoning_output_tokens": 6,
    }},
]
for event in events:
    print(json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
raise SystemExit(9)
""",
        encoding="utf-8",
        newline="\n",
    )
    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=lambda _attempt, _schema, output: [
            sys.executable,
            str(script),
            str(output),
        ],
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_protocol_or_output_invalid"
    assert caught.value.effect_state is BridgeEffectState.FAILED
    assert caught.value.evidence is not None
    assert caught.value.evidence.response_bytes == 0
    assert caught.value.evidence.usage is not None
    assert caught.value.evidence.usage.input_tokens == 123
    assert caught.value.evidence.usage.output_tokens == 45
    assert caught.value.evidence.usage.reasoning_output_tokens == 6


def test_subscription_thread_started_before_turn_failure_keeps_partial_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    script = tmp_path / "thread_only_codex.py"
    script.write_text(
        """import json
import sys

sys.stdin.buffer.read()
print(json.dumps(
    {"type": "thread.started", "thread_id": "thread-before-turn"},
    ensure_ascii=False,
    separators=(",", ":"),
), flush=True)
raise SystemExit(9)
""",
        encoding="utf-8",
        newline="\n",
    )
    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=lambda _attempt, _schema, _output: [
            sys.executable,
            str(script),
        ],
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_protocol_or_output_invalid"
    assert caught.value.effect_state is BridgeEffectState.EFFECT_UNKNOWN
    assert caught.value.launched_at is None
    assert caught.value.thread_id_sha256 == sha256_bytes(b"thread-before-turn")
    assert caught.value.turn_id_sha256 is None
    assert "thread-before-turn" not in str(caught.value)


def test_subscription_context_uses_name_allowlist_and_separate_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "api-key-canary")
    monkeypatch.setenv("CODEX_API_KEY", "codex-key-canary")
    monkeypatch.setenv("UNRELATED_SECRET_CANARY", "unrelated-canary")
    transport = _transport(tmp_path, monkeypatch, auth_present=True)
    context = transport._process_context("a" * 32)
    environment = transport._environment(context)

    controlled = {
        "APPDATA",
        "CODEX_HOME",
        "HOME",
        "LOCALAPPDATA",
        "NO_COLOR",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "TEMP",
        "TMP",
        "USERPROFILE",
    }
    os_names = {"COMSPEC", "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "WINDIR"}
    assert set(environment) <= controlled | os_names
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_API_KEY" not in environment
    assert "UNRELATED_SECRET_CANARY" not in environment
    assert "PATH" not in environment
    assert context.cwd.is_relative_to(transport.isolation_root)
    assert not context.cwd.is_relative_to(transport.runtime_root)
    assert environment["HOME"] == environment["USERPROFILE"]
    assert environment["TEMP"] == environment["TMP"]
    assert environment["CODEX_HOME"] == str(transport.credential_codex_home)


def test_subscription_context_attempt_is_single_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = _transport(tmp_path, monkeypatch, auth_present=True)
    key = "b" * 32
    transport._process_context(key)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport._process_context(key)

    assert caught.value.reason_code == "subscription_context_attempt_reuse"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED


def test_subscription_runtime_capability_rejects_path_change_before_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime_root = repository / "tmp" / "runs" / "runtime-synthetic-private-canary"
    capability = PreparedRuntimeRoot.create(runtime_root, repository)
    monkeypatch.setattr(
        subscription_module,
        "_file_sha256",
        lambda _path: BRIDGE_RUNTIME_BINARY_SHA256,
    )
    transport = CodexSubscriptionCliTransport(
        runtime_root,
        codex_binary=Path(sys.executable),
        auth_status_probe=lambda _cwd, _env: True,
        command_factory=lambda *_args: pytest.fail("model process must not launch"),
        isolation_root=tmp_path / "isolated-execution",
        credential_codex_home=tmp_path / "credential-codex-home",
        runtime_capability=capability,
    )
    changed_root = repository / "tmp" / "runs" / "changed-synthetic-secret-canary"
    transport.runtime_root = changed_root

    with pytest.raises(BridgeTransportFailure) as caught:
        transport._attempt("a" * 32)

    assert caught.value.reason_code == "runtime_scratch_identity_changed"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED
    assert "synthetic-private-canary" not in str(caught.value)
    assert "synthetic-secret-canary" not in str(caught.value)
    assert not changed_root.exists()


def test_subscription_context_rejects_empty_skill_inventory_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    codex_home = tmp_path / "empty-codex-home"
    codex_home.mkdir()
    monkeypatch.setattr(
        subscription_module,
        "_file_sha256",
        lambda _path: BRIDGE_RUNTIME_BINARY_SHA256,
    )
    transport = CodexSubscriptionCliTransport(
        tmp_path / "runtime",
        codex_binary=Path(sys.executable),
        auth_status_probe=lambda _cwd, _env: True,
        command_factory=lambda *_args: pytest.fail("model process must not launch"),
        isolation_root=tmp_path / "isolated-execution",
        credential_codex_home=codex_home,
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_context_preflight_failed"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED


def test_subscription_skill_content_drift_fails_before_launch_even_with_restored_mtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    skill_file = tmp_path / "credential-codex-home" / "skills" / "fixture-skill" / "SKILL.md"

    def mutate_skill(_cwd: Path, _schema: Path, _output: Path) -> list[str]:
        before = skill_file.stat()
        content = skill_file.read_bytes()
        skill_file.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        os.utime(skill_file, ns=(before.st_atime_ns, before.st_mtime_ns))
        return [sys.executable]

    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=mutate_skill,
    )
    monkeypatch.setattr(
        subscription_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("model process must not launch"),
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_context_drift"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED
