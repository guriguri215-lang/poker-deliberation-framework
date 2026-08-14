from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

import poker_deliberation.codex_bridge.subscription_transport as subscription_module
from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    domain_sha256,
    sha256_bytes,
)
from poker_deliberation.codex_bridge.conformance import build_bridge_role_conformance
from poker_deliberation.codex_bridge.contracts import (
    build_role_request,
    legacy_role_developer_instructions,
    role_output_schema_for_request,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BRIDGE_RUNTIME_BINARY_SHA256,
    CONTEXT_HASH_DOMAIN,
    REQUEST_HASH_DOMAIN,
    BoundedCodexBridgeRequestV1,
    BridgeEffectState,
    BridgeRole,
    CodexSubscriptionLiveExecutionEvidenceV1,
    RuntimeAuthModeV1,
    repository_skill_for_role,
)
from poker_deliberation.codex_bridge.runtime_scratch import PreparedRuntimeRoot
from poker_deliberation.codex_bridge.subscription_transport import (
    CodexSubscriptionCliTransport,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    DeterministicReadOnlyTransport,
)
from tests.codex_bridge_support import REPOSITORY_ROOT, prepared_bridge_request


def _catalog_payload(entries: tuple[tuple[str, Path], ...]) -> bytes:
    lines = ["<skills_instructions>", "## Skills", "### Available skills"]
    lines.extend(f"- {skill_id}: fixture description (file: {path})" for skill_id, path in entries)
    lines.append("</skills_instructions>")
    return json.dumps(
        [
            {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "\n".join(lines)}],
            }
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _catalog_probe_payload(
    _cwd: Path,
    _environment: dict[str, str],
    skill_snapshot: tuple[subscription_module._SkillState, ...],
) -> bytes:
    return _catalog_payload(
        tuple(
            (item.skill_id, item.configuration_path)
            for item in skill_snapshot
            if item.enabled and item.skill_id is not None
        )
    )


def _legacy_subscription_request(
    request: BoundedCodexBridgeRequestV1,
) -> BoundedCodexBridgeRequestV1:
    payload = request.model_dump(mode="python")
    conformance = payload["context"]["assignment"]["conformance"]
    for name in tuple(conformance):
        if name.startswith("repository_skill_"):
            conformance.pop(name)
    payload["developer_instructions"] = legacy_role_developer_instructions(
        request.context.assignment.role
    )
    context = payload["context"]
    context_without_hash = dict(context)
    context_without_hash.pop("envelope_sha256")
    context["envelope_sha256"] = domain_sha256(CONTEXT_HASH_DOMAIN, context_without_hash)
    without_request_hashes = dict(payload)
    without_request_hashes.pop("request_sha256")
    without_request_hashes.pop("request_bytes_sha256")
    payload["request_bytes_sha256"] = sha256_bytes(canonical_json_bytes(without_request_hashes))
    request_projection = dict(payload)
    request_projection.pop("request_sha256")
    payload["request_sha256"] = domain_sha256(REQUEST_HASH_DOMAIN, request_projection)
    return BoundedCodexBridgeRequestV1.model_validate(payload, strict=True)


def _transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_present: bool,
    command_factory=None,  # type: ignore[no-untyped-def]
    skill_catalog_probe=_catalog_probe_payload,  # type: ignore[no-untyped-def]
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
        skill_catalog_probe=skill_catalog_probe,
        isolation_root=tmp_path / "isolated-execution",
        credential_codex_home=codex_home,
    )


def test_subscription_command_is_ephemeral_read_only_and_tools_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
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
    source_snapshot = transport._skill_snapshot(request)
    execution_cwd = tmp_path / "command-cwd"
    execution_cwd.mkdir()
    skill_snapshot = transport._stage_skill_snapshot(
        cwd=execution_cwd,
        source_snapshot=source_snapshot,
    )
    command = transport._command(
        cwd=execution_cwd,
        schema=tmp_path / "schema.json",
        output=tmp_path / "output.json",
        skill_snapshot=skill_snapshot,
    )
    catalog_command = transport._catalog_command(
        cwd=execution_cwd,
        skill_snapshot=skill_snapshot,
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
    for configured_command in (command, catalog_command):
        assert configured_command.count("skills.bundled.enabled=false") == 1
        bundled_index = configured_command.index("skills.bundled.enabled=false")
        assert configured_command[bundled_index - 1] == "-c"
    skill_config = next(item for item in command if item.startswith("skills.config="))
    assert "enabled=false" in skill_config
    assert "fixture-skill" in skill_config
    assert "plugin-skill" in skill_config
    assert "review-poker-hand" in skill_config
    assert "run-poker-calculation" not in skill_config
    assert "audit-poker-claim" not in skill_config
    assert "SKILL.md" in skill_config
    assert skill_config.count("enabled=true") == 1
    assert skill_config.count("enabled=false") == 2
    selected = next(item for item in skill_snapshot if item.enabled)
    assert (
        selected.configuration_path
        == (execution_cwd / ".agents" / "skills" / "review-poker-hand" / "SKILL.md").resolve()
    )
    assert selected.content_sha256 == selected.source_content_sha256
    assert "skills.config=[]" not in command
    assert "OPENAI_API_KEY" not in joined
    command_hash = transport._command_contract_sha256(
        command,
        cwd=execution_cwd,
        schema=tmp_path / "schema.json",
        output=tmp_path / "output.json",
        skill_snapshot=skill_snapshot,
    )
    command_without_bundled_skill_gate = list(command)
    bundled_index = command_without_bundled_skill_gate.index("skills.bundled.enabled=false")
    del command_without_bundled_skill_gate[bundled_index - 1 : bundled_index + 1]
    assert command_hash != transport._command_contract_sha256(
        command_without_bundled_skill_gate,
        cwd=execution_cwd,
        schema=tmp_path / "schema.json",
        output=tmp_path / "output.json",
        skill_snapshot=skill_snapshot,
    )
    changed_snapshot = tuple(
        replace(item, source_content_sha256="0" * 64) if item.enabled else item
        for item in skill_snapshot
    )
    assert command_hash != transport._command_contract_sha256(
        command,
        cwd=execution_cwd,
        schema=tmp_path / "schema.json",
        output=tmp_path / "output.json",
        skill_snapshot=changed_snapshot,
    )
    assert transport._runtime_configuration_sha256(
        environment={},
        command_contract_sha256=command_hash,
        skill_catalog_sha256="a" * 64,
    ) != transport._runtime_configuration_sha256(
        environment={},
        command_contract_sha256=command_hash,
        skill_catalog_sha256="b" * 64,
    )


def test_subscription_catalog_attests_only_the_staged_selected_repository_skill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    transport = _transport(tmp_path, monkeypatch, auth_present=True)
    execution_cwd = tmp_path / "catalog-cwd"
    execution_cwd.mkdir()
    source_snapshot = transport._skill_snapshot(request)
    skill_snapshot = transport._stage_skill_snapshot(
        cwd=execution_cwd,
        source_snapshot=source_snapshot,
    )

    catalog_sha256 = transport._probe_skill_catalog(
        cwd=execution_cwd,
        environment=transport._environment(transport._process_context("c" * 32)),
        skill_snapshot=skill_snapshot,
    )

    selected = tuple(item for item in skill_snapshot if item.enabled)
    assert len(catalog_sha256) == 64
    assert tuple(item.skill_id for item in selected) == ("review-poker-hand",)
    assert selected[0].configuration_path.is_relative_to(execution_cwd)
    assert all(
        item.skill_id not in {"audit-poker-claim", "run-poker-calculation"}
        for item in skill_snapshot
    )
    assert not any(path.name == "prompt-input.json" for path in execution_cwd.rglob("*"))


def test_subscription_catalog_accepts_the_codex_0144_4_developer_input_shape(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "SKILL.md"
    staged.write_text("# selected\n", encoding="utf-8")
    payload = json.loads(_catalog_payload((("review-poker-hand", staged),)))
    payload[0]["internal_chat_message_metadata_passthrough"] = None
    payload[0]["content"].insert(
        0,
        {"type": "input_text", "text": "fixture base developer instructions"},
    )

    catalog_sha256 = subscription_module._skill_catalog_sha256(
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        selected_skill_id="review-poker-hand",
        staged_skill_path=staged,
    )

    assert len(catalog_sha256) == 64


def test_subscription_catalog_accepts_absent_block_only_for_a_no_skill_role(
    tmp_path: Path,
) -> None:
    prompt_without_skills = json.dumps(
        [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "fixture prompt"}],
            }
        ],
        separators=(",", ":"),
    ).encode("utf-8")

    catalog_sha256 = subscription_module._skill_catalog_sha256(
        prompt_without_skills,
        selected_skill_id=None,
        staged_skill_path=None,
    )

    assert len(catalog_sha256) == 64
    with pytest.raises(ValueError, match="missing its selected Skill"):
        subscription_module._skill_catalog_sha256(
            prompt_without_skills,
            selected_skill_id="review-poker-hand",
            staged_skill_path=tmp_path / "SKILL.md",
        )


@pytest.mark.parametrize(
    ("message_type", "role"),
    (
        ("message", "user"),
        ("message", "assistant"),
        ("message", None),
        ("function_call", "developer"),
        (None, "developer"),
    ),
)
def test_subscription_catalog_rejects_a_skill_block_without_developer_message_authority(
    tmp_path: Path,
    message_type: str | None,
    role: str | None,
) -> None:
    staged = tmp_path / "SKILL.md"
    staged.write_text("# selected\n", encoding="utf-8")
    payload = json.loads(_catalog_payload((("review-poker-hand", staged),)))
    message = payload[0]
    if message_type is None:
        message.pop("type")
    else:
        message["type"] = message_type
    if role is None:
        message.pop("role")
    else:
        message["role"] = role

    with pytest.raises(ValueError, match="authority is invalid"):
        subscription_module._skill_catalog_sha256(
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            selected_skill_id="review-poker-hand",
            staged_skill_path=staged,
        )


@pytest.mark.parametrize("item_type", ("output_text", None))
def test_subscription_catalog_rejects_a_skill_block_in_a_non_input_text_item(
    tmp_path: Path,
    item_type: str | None,
) -> None:
    staged = tmp_path / "SKILL.md"
    staged.write_text("# selected\n", encoding="utf-8")
    payload = json.loads(_catalog_payload((("review-poker-hand", staged),)))
    item = payload[0]["content"][0]
    if item_type is None:
        item.pop("type")
    else:
        item["type"] = item_type

    with pytest.raises(ValueError, match="authority is invalid"):
        subscription_module._skill_catalog_sha256(
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            selected_skill_id="review-poker-hand",
            staged_skill_path=staged,
        )


@pytest.mark.parametrize(("prefix", "suffix"), (("prefix\n", ""), ("", "\nsuffix")))
def test_subscription_catalog_rejects_a_non_standalone_skill_block(
    tmp_path: Path,
    prefix: str,
    suffix: str,
) -> None:
    staged = tmp_path / "SKILL.md"
    staged.write_text("# selected\n", encoding="utf-8")
    payload = json.loads(_catalog_payload((("review-poker-hand", staged),)))
    item = payload[0]["content"][0]
    item["text"] = prefix + item["text"] + suffix

    with pytest.raises(ValueError, match="not a standalone developer input"):
        subscription_module._skill_catalog_sha256(
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            selected_skill_id="review-poker-hand",
            staged_skill_path=staged,
        )


@pytest.mark.parametrize(
    "text",
    (
        "<skills_instructions>\n## Skills",
        "## Skills\n</skills_instructions>",
        "<skills_instructions><skills_instructions></skills_instructions>",
        "<skills_instructions></skills_instructions></skills_instructions>",
    ),
)
def test_subscription_catalog_rejects_ambiguous_skill_boundaries(text: str) -> None:
    payload = [
        {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": text}],
        }
    ]

    with pytest.raises(ValueError, match="boundary is ambiguous"):
        subscription_module._skill_catalog_sha256(
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            selected_skill_id=None,
            staged_skill_path=None,
        )


def test_subscription_catalog_rejects_a_no_skill_spoof_from_a_user_message() -> None:
    payload = json.loads(_catalog_payload(()))
    payload[0]["role"] = "user"

    with pytest.raises(ValueError, match="authority is invalid"):
        subscription_module._skill_catalog_sha256(
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            selected_skill_id=None,
            staged_skill_path=None,
        )


def test_subscription_catalog_rejects_an_ambient_or_bundled_skill_leak(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "SKILL.md"
    staged.write_text("# selected\n", encoding="utf-8")
    leaked_catalog = _catalog_payload(
        (
            ("review-poker-hand", staged),
            ("imagegen", tmp_path / "ambient" / "SKILL.md"),
        )
    )

    with pytest.raises(ValueError, match="exclusive selection mismatch"):
        subscription_module._skill_catalog_sha256(
            leaked_catalog,
            selected_skill_id="review-poker-hand",
            staged_skill_path=staged,
        )
    with pytest.raises(ValueError, match="exposed an unselected Skill"):
        subscription_module._skill_catalog_sha256(
            _catalog_payload((("openai-docs", tmp_path / "bundled" / "SKILL.md"),)),
            selected_skill_id=None,
            staged_skill_path=None,
        )


def test_subscription_catalog_rejects_nonselected_repository_skill_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")

    def wrong_catalog(
        _cwd: Path,
        _environment: dict[str, str],
        skill_snapshot: tuple[subscription_module._SkillState, ...],
    ) -> bytes:
        selected = next(item for item in skill_snapshot if item.enabled)
        return _catalog_payload(
            (
                ("review-poker-hand", selected.configuration_path),
                ("audit-poker-claim", selected.configuration_path),
            )
        )

    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=lambda *_args: [sys.executable],
        skill_catalog_probe=wrong_catalog,
    )
    monkeypatch.setattr(
        subscription_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("model process must not launch"),
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_skill_catalog_probe_failed"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED


def test_subscription_catalog_probe_is_bounded_and_fails_closed_on_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        skill_catalog_probe=None,
    )
    execution_cwd = tmp_path / "timeout-cwd"
    execution_cwd.mkdir()
    skill_snapshot = transport._stage_skill_snapshot(
        cwd=execution_cwd,
        source_snapshot=transport._skill_snapshot(request),
    )
    script = tmp_path / "slow_catalog_probe.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(subscription_module, "_SKILL_CATALOG_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        CodexSubscriptionCliTransport,
        "_catalog_command",
        lambda *_args, **_kwargs: [sys.executable, str(script)],
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport._probe_skill_catalog(
            cwd=execution_cwd,
            environment={},
            skill_snapshot=skill_snapshot,
        )

    assert caught.value.reason_code == "subscription_skill_catalog_probe_failed"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED


def test_subscription_skill_paths_derive_from_canonical_role_mapping() -> None:
    expected_assignments = (
        "review-poker-hand",
        "run-poker-calculation",
        "audit-poker-claim",
        None,
        None,
    )
    assert tuple(repository_skill_for_role(role) for role in BRIDGE_ROLE_ORDER) == (
        expected_assignments
    )
    assert tuple(repository_skill_for_role(role) for role in BridgeRole) == expected_assignments
    assert subscription_module._REPOSITORY_SKILL_PATHS == (
        ".agents/skills/audit-poker-claim/SKILL.md",
        ".agents/skills/review-poker-hand/SKILL.md",
        ".agents/skills/run-poker-calculation/SKILL.md",
    )


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


def test_legacy_subscription_request_is_readable_but_rejected_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _legacy_subscription_request(prepared_bridge_request(tmp_path / "p3"))
    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=False,
        command_factory=lambda *_args: pytest.fail("model process must not launch"),
    )

    assert request.context.assignment.conformance.repository_skill_id is None
    assert b"repository_skill_" not in canonical_json_bytes(request.context.assignment.conformance)
    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_context_preflight_failed"
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


def test_subscription_context_uses_repository_skill_with_empty_ambient_inventory(
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
        isolation_root=tmp_path / "isolated-execution",
        credential_codex_home=codex_home,
    )
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        include_repository_skill_bindings=True,
    )
    observed: dict[BridgeRole, tuple[str | None, ...]] = {}
    for ordinal, role in enumerate(BRIDGE_ROLE_ORDER[:3]):
        role_request = build_role_request(
            bridge_run_id=request.context.assignment.bridge_run_id,
            role=role,
            assignment_id=f"assignment-codex_subscription-{role.value}",
            attempt_id=f"attempt-codex_subscription-{role.value}",
            expires_at=request.context.assignment.expires_at,
            source_context=request.context.source_context,
            runtime_policy=request.context.runtime_policy,
            conformance=conformance[ordinal],
        )
        snapshot = transport._skill_snapshot(role_request)
        observed[role] = tuple(item.skill_id for item in snapshot if item.enabled)

    assert observed == {
        BridgeRole.STRATEGY_ANALYST: ("review-poker-hand",),
        BridgeRole.MATH_TOOL_AUDITOR: ("run-poker-calculation",),
        BridgeRole.SKEPTIC_FALSIFIER: ("audit-poker-claim",),
    }


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


def test_selected_repository_skill_drift_fails_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    repository = tmp_path / "repository"
    for relative in subscription_module._REPOSITORY_SKILL_PATHS:
        source = REPOSITORY_ROOT.joinpath(*relative.split("/"))
        target = repository.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    selected = repository / ".agents" / "skills" / "review-poker-hand" / "SKILL.md"

    def mutate_selected(_cwd: Path, _schema: Path, _output: Path) -> list[str]:
        selected.write_bytes(selected.read_bytes() + b"\n")
        return [sys.executable]

    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=mutate_selected,
    )
    transport.repository_root = repository.resolve()
    monkeypatch.setattr(
        subscription_module.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("model process must not launch"),
    )

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_context_preflight_failed"
    assert caught.value.effect_state is BridgeEffectState.NOT_LAUNCHED


def test_staged_repository_skill_drift_fails_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")

    def mutate_staged(cwd: Path, _schema: Path, _output: Path) -> list[str]:
        selected = cwd / ".agents" / "skills" / "review-poker-hand" / "SKILL.md"
        before = selected.stat()
        content = selected.read_bytes()
        selected.write_bytes(bytes([content[0] ^ 1]) + content[1:])
        os.utime(selected, ns=(before.st_atime_ns, before.st_mtime_ns))
        return [sys.executable]

    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=mutate_staged,
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


def test_selected_repository_skill_drift_after_launch_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    repository = tmp_path / "repository"
    for relative in subscription_module._REPOSITORY_SKILL_PATHS:
        source = REPOSITORY_ROOT.joinpath(*relative.split("/"))
        target = repository.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    selected = repository / ".agents" / "skills" / "review-poker-hand" / "SKILL.md"
    response = (
        DeterministicReadOnlyTransport(
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
        )
        .execute(request)
        .response_bytes
    )
    script = tmp_path / "drift_after_launch.py"
    script.write_text(
        """import base64
import json
import pathlib
import sys

selected = pathlib.Path(sys.argv[1])
output = pathlib.Path(sys.argv[2])
response = base64.b64decode(sys.argv[3])
sys.stdin.buffer.read()
selected.write_bytes(selected.read_bytes() + b"\\n")
output.write_bytes(response)
events = [
    {"type": "thread.started", "thread_id": "thread-skill-drift"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item-1", "type": "agent_message"}},
    {"type": "turn.completed", "usage": {
        "input_tokens": 1,
        "cached_input_tokens": 0,
        "output_tokens": 1,
        "reasoning_output_tokens": 0,
    }},
]
for event in events:
    print(json.dumps(event, separators=(",", ":")), flush=True)
""",
        encoding="utf-8",
        newline="\n",
    )
    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=lambda _cwd, _schema, output: [
            sys.executable,
            str(script),
            str(selected),
            str(output),
            base64.b64encode(response).decode("ascii"),
        ],
    )
    transport.repository_root = repository.resolve()

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_context_drift"
    assert caught.value.effect_state is BridgeEffectState.FAILED


def test_subscription_timeout_with_execution_skill_drift_keeps_cancel_unconfirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    script = tmp_path / "timeout_with_skill_drift.py"
    script.write_text(
        """import json
import pathlib
import sys
import time

selected = pathlib.Path(sys.argv[1])
sys.stdin.buffer.read()
print(json.dumps({"type": "thread.started", "thread_id": "thread-timeout-drift"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
selected.write_bytes(selected.read_bytes() + b"\\n")
time.sleep(30)
""",
        encoding="utf-8",
        newline="\n",
    )
    transport = _transport(
        tmp_path,
        monkeypatch,
        auth_present=True,
        command_factory=lambda cwd, _schema, _output: [
            sys.executable,
            str(script),
            str(cwd / ".agents" / "skills" / "review-poker-hand" / "SKILL.md"),
        ],
    )
    monkeypatch.setattr(subscription_module, "MAX_ROLE_RUNTIME_MS", 500)

    with pytest.raises(BridgeTransportFailure) as caught:
        transport.execute(request)

    assert caught.value.reason_code == "subscription_context_drift"
    assert caught.value.effect_state is BridgeEffectState.CANCEL_UNCONFIRMED
    assert caught.value.thread_id_sha256 == sha256_bytes(b"thread-timeout-drift")
    assert caught.value.turn_id_sha256 is not None
