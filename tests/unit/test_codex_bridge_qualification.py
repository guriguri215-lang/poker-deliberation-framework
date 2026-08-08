from __future__ import annotations

import base64
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

import poker_deliberation.codex_bridge.subscription_transport as subscription_module
from poker_deliberation.codex_bridge.canonical import canonical_json_bytes, parse_canonical_model
from poker_deliberation.codex_bridge.controller import (
    BoundedCodexBridgeController,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BRIDGE_RUNTIME_BINARY_SHA256,
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeExecutionAuditV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.qualification import (
    PUBLIC_SYNTHETIC_FIXTURE_ID,
    SanitizedLiveQualificationManifestV1,
    build_sanitized_live_qualification_manifest,
    load_public_synthetic_fixture,
)
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore, VerifiedBridgeRead
from poker_deliberation.codex_bridge.subscription_transport import (
    CodexSubscriptionCliTransport,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportResult,
    DeterministicReadOnlyTransport,
)
from tests.codex_bridge_support import REPOSITORY_ROOT, verified_bridge_source

_MODE = RuntimeAuthModeV1.CODEX_SUBSCRIPTION
_FIXTURE = REPOSITORY_ROOT / "tests/fixtures/codex_bridge/v1/public-synthetic-qualification.json"


class _Clock:
    def __init__(self) -> None:
        self.current = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


class _ActualLiveTaggedFixtureTransport:
    """Test-only stand-in; never evidence of an actual qualification."""

    auth_mode = _MODE
    transport_qualification: Literal["actual_live"] = "actual_live"

    def __init__(self, clock: _Clock) -> None:
        self.delegate = DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)

    def execute(self, request: BoundedCodexBridgeRequestV1) -> BridgeTransportResult:
        return replace(
            self.delegate.execute(request),
            transport_qualification="actual_live",
        )


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
    transport = (
        _ActualLiveTaggedFixtureTransport(clock)
        if actual_live_tag
        else DeterministicReadOnlyTransport(auth_mode=_MODE, clock=clock)
    )
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


def test_sanitized_manifest_is_canonical_hash_bound_and_has_no_api_live_claim(
    tmp_path: Path,
) -> None:
    terminal = _terminal(tmp_path, actual_live_tag=True)
    manifest = build_sanitized_live_qualification_manifest(
        terminal,
        repository_root=REPOSITORY_ROOT,
        qualification_id="test-only-actual-tagged-fixture",
        deterministic_evaluation_sha256="3" * 64,
    )

    assert manifest.qualification_status == "passed"
    assert manifest.api_live_executed is False
    assert manifest.api_production_qualified is False
    assert manifest.configured_model_provider == "openai"
    assert manifest.auth_boundary == "chatgpt"
    assert manifest.auth_enforcement == "same_process_forced_login_method_chatgpt"
    assert manifest.model_reroute_observed is False
    assert manifest.effective_model_identity_status == "UNKNOWN_codex_exec_json_not_exposed"
    assert manifest.actual_backend_model_input_status == "UNKNOWN_codex_exec_json_not_exposed"
    assert all(item.transport_qualification == "actual_live" for item in manifest.roles)
    assert all(
        item.model_identity_evidence == "requested_pinned_no_fallback_no_reroute"
        for item in manifest.roles
    )
    assert len({item.thread_id_sha256 for item in manifest.roles}) == 5
    assert manifest.runtime_source_inventory
    assert manifest.runtime_source_inventory_sha256
    data = canonical_json_bytes(manifest)
    assert parse_canonical_model(data, SanitizedLiveQualificationManifestV1) == manifest
    mutated = manifest.model_dump(mode="python")
    mutated["roles"][0]["outbound_bytes"] += 1
    with pytest.raises(ValidationError):
        SanitizedLiveQualificationManifestV1.model_validate(mutated, strict=True)
