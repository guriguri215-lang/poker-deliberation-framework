from __future__ import annotations

import base64
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

import poker_deliberation.codex_bridge.subscription_transport as subscription_module
from poker_deliberation.codex_bridge.controller import (
    BoundedCodexBridgeController,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BRIDGE_RUNTIME_BINARY_SHA256,
    BridgeConfirmationAuthorityV1,
    BridgeExecutionAuditV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.qualification import (
    PUBLIC_SYNTHETIC_FIXTURE_ID,
    build_sanitized_live_qualification_manifest,
    load_public_synthetic_fixture,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore, VerifiedBridgeRead
from poker_deliberation.codex_bridge.subscription_transport import (
    CodexSubscriptionCliTransport,
)
from poker_deliberation.codex_bridge.transport import (
    DeterministicReadOnlyTransport,
)
from tests.codex_bridge_support import REPOSITORY_ROOT, verified_bridge_source

_MODE = RuntimeAuthModeV1.CODEX_SUBSCRIPTION
_FIXTURE = REPOSITORY_ROOT / "tests/fixtures/codex_bridge/v1/public-synthetic-qualification.json"


def _skill_catalog_probe(
    _cwd: Path,
    _environment: dict[str, str],
    skill_snapshot: tuple[subscription_module._SkillState, ...],
) -> bytes:
    lines = ["<skills_instructions>", "## Skills", "### Available skills"]
    lines.extend(
        f"- {item.skill_id}: fixture description (file: {item.configuration_path})"
        for item in skill_snapshot
        if item.enabled and item.skill_id is not None
    )
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


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def _terminal(
    root: Path,
    *,
    actual_live_tag: bool,
) -> VerifiedBridgeRead:
    clock = _Clock()
    source = verified_bridge_source(root / "p3", run_id=f"source-{actual_live_tag}")
    controller = BoundedCodexBridgeController(
        BoundedCodexBridgeStore(root / "bridge"),
        clock=clock,
    )
    run_id = f"qualification-{actual_live_tag}"
    controller.prepare_run(
        bridge_run_id=run_id,
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    transport = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
    if actual_live_tag:
        # Reproduces fa6f73b: the deterministic result reads this mutable instance
        # attribute and therefore also returns the caller-selected actual_live string.
        transport.__dict__["transport_qualification"] = "actual_live"
    for role in BRIDGE_ROLE_ORDER:
        request = controller.read_role_request(run_id, role)
        controller.confirm_role(
            run_id,
            role,
            authority=BridgeConfirmationAuthorityV1(
                authority_id="qualification-test-user",
                authority_kind="local_user",
                authentication="self_asserted",
            ),
            confirmation_id=f"confirmation-{actual_live_tag}-{role.value}",
            idempotency_key=f"idempotency-{actual_live_tag}-{role.value}",
            expected_request_sha256=request.request_sha256,
            expected_request_bytes_sha256=request.request_bytes_sha256,
            expected_envelope_sha256=request.context.envelope_sha256,
            expected_runtime_policy_sha256=request.context.runtime_policy.policy_sha256,
            expected_auth_mode=_MODE,
            expected_runtime_identity=request.context.runtime_policy.runtime_identity,
            expected_model_provider=request.context.runtime_policy.model_provider,
            expected_model=request.context.runtime_policy.model,
            expected_credential_reference=request.context.runtime_policy.credential_reference,
            expected_remote_retention_policy=(
                request.context.runtime_policy.remote_retention_policy
            ),
        )
        terminal = controller.execute_confirmed_role(
            run_id,
            role,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )
    return terminal


def test_public_synthetic_qualification_fixture_is_strict_and_public() -> None:
    fixture = load_public_synthetic_fixture(_FIXTURE)

    assert fixture.fixture_id == PUBLIC_SYNTHETIC_FIXTURE_ID
    assert fixture.role_order == BRIDGE_ROLE_ORDER
    assert fixture.model_processing_authorized is True
    assert fixture.raw_japanese_source_outbound is False
    payload = fixture.model_dump(mode="python")
    payload["unexpected"] = "refused"
    with pytest.raises(ValidationError):
        type(fixture).model_validate(payload, strict=True)


def test_deterministic_transport_cannot_be_promoted_to_live_qualification(
    tmp_path: Path,
) -> None:
    terminal = _terminal(tmp_path, actual_live_tag=False)

    with pytest.raises(ValueError, match="role evidence is incomplete"):
        build_sanitized_live_qualification_manifest(
            terminal,
            repository_root=REPOSITORY_ROOT,
            qualification_id="deterministic-must-not-qualify",
            deterministic_evaluation_sha256="3" * 64,
        )


def test_injected_subscription_process_cannot_be_promoted_to_live_qualification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = verified_bridge_source(tmp_path / "p3", run_id="source-injected-subscription")
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(tmp_path / "bridge"))
    run_id = "qualification-injected-subscription"
    controller.prepare_run(
        bridge_run_id=run_id,
        source_context=source,
        repository_root=REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        auth_mode=_MODE,
    )
    codex_home = tmp_path / "credential-codex-home"
    skill = codex_home / "skills" / "fixture-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# disabled fixture skill\n", encoding="utf-8")
    script = tmp_path / "fake_subscription_process.py"
    script.write_text(
        """import base64
import json
import pathlib
import sys

output = pathlib.Path(sys.argv[1])
output.write_bytes(base64.b64decode(sys.argv[2]))
sys.stdin.buffer.read()
events = [
    {"type": "thread.started", "thread_id": sys.argv[3]},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "item-1", "type": "agent_message"}},
    {"type": "turn.completed", "usage": {
        "input_tokens": 10,
        "cached_input_tokens": 0,
        "output_tokens": 5,
        "reasoning_output_tokens": 1,
    }},
]
for event in events:
    print(json.dumps(event, separators=(",", ":")), flush=True)
""",
        encoding="utf-8",
        newline="\n",
    )
    monkeypatch.setattr(
        subscription_module,
        "_file_sha256",
        lambda _path: BRIDGE_RUNTIME_BINARY_SHA256,
    )

    terminal: VerifiedBridgeRead | None = None
    for ordinal, role in enumerate(BRIDGE_ROLE_ORDER):
        request = controller.read_role_request(run_id, role)
        controller.confirm_role(
            run_id,
            role,
            authority=BridgeConfirmationAuthorityV1(
                authority_id="qualification-test-user",
                authority_kind="local_user",
                authentication="self_asserted",
            ),
            confirmation_id=f"confirmation-injected-{role.value}",
            idempotency_key=f"idempotency-injected-{role.value}",
            expected_request_sha256=request.request_sha256,
            expected_request_bytes_sha256=request.request_bytes_sha256,
            expected_envelope_sha256=request.context.envelope_sha256,
            expected_runtime_policy_sha256=request.context.runtime_policy.policy_sha256,
            expected_auth_mode=_MODE,
            expected_runtime_identity=request.context.runtime_policy.runtime_identity,
            expected_model_provider=request.context.runtime_policy.model_provider,
            expected_model=request.context.runtime_policy.model,
            expected_credential_reference=request.context.runtime_policy.credential_reference,
            expected_remote_retention_policy=(
                request.context.runtime_policy.remote_retention_policy
            ),
        )
        response = (
            DeterministicReadOnlyTransport(
                auth_mode=_MODE,
                clock=lambda: datetime.now(UTC),
            )
            .execute(request)
            .response_bytes
        )
        encoded = base64.b64encode(response).decode("ascii")
        transport = CodexSubscriptionCliTransport(
            tmp_path / "runtime",
            codex_binary=Path(sys.executable),
            auth_status_probe=lambda _cwd, _env: True,
            command_factory=lambda _cwd, _schema, output, *, index=ordinal, payload=encoded: [
                sys.executable,
                str(script),
                str(output),
                payload,
                f"thread-injected-{index}",
            ],
            skill_catalog_probe=_skill_catalog_probe,
            isolation_root=tmp_path / "isolated-execution",
            credential_codex_home=codex_home,
        )
        terminal = controller.execute_confirmed_role(
            run_id,
            role,
            auth_mode=_MODE,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )

    assert terminal is not None
    artifacts = {item.logical_name: item.model for item in terminal.decoded_artifacts()}
    for role in BRIDGE_ROLE_ORDER:
        audit = artifacts[role_artifact_name(role, "audit")]
        assert isinstance(audit, BridgeExecutionAuditV1)
        assert audit.transport_qualification == "deterministic_fixture"
    with pytest.raises(ValueError, match="role evidence is incomplete"):
        build_sanitized_live_qualification_manifest(
            terminal,
            repository_root=REPOSITORY_ROOT,
            qualification_id="injected-subscription-must-not-qualify",
            deterministic_evaluation_sha256="3" * 64,
        )


def test_five_role_mutable_actual_live_label_spoof_is_rejected(
    tmp_path: Path,
) -> None:
    terminal = _terminal(tmp_path, actual_live_tag=True)
    assert terminal.pointer.status == "succeeded"
    assert replay_bridge(terminal).status == "succeeded"
    artifacts = {item.logical_name: item.model for item in terminal.decoded_artifacts()}
    for role in BRIDGE_ROLE_ORDER:
        audit = artifacts[role_artifact_name(role, "audit")]
        assert isinstance(audit, BridgeExecutionAuditV1)
        assert audit.transport_qualification == "deterministic_fixture"
        assert audit.live_execution_evidence is None
    with pytest.raises(ValueError, match="role evidence is incomplete"):
        build_sanitized_live_qualification_manifest(
            terminal,
            repository_root=REPOSITORY_ROOT,
            qualification_id="mutable-label-spoof-must-not-qualify",
            deterministic_evaluation_sha256="3" * 64,
        )
