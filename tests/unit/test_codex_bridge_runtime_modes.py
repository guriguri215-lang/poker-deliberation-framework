from __future__ import annotations

import os
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from poker_deliberation.bounded_river_call_ev_evaluation import (
    build_repository_owned_bounded_river_evaluation_admission,
)
from poker_deliberation.codex_bridge.canonical import canonical_json_bytes
from poker_deliberation.codex_bridge.contracts import (
    BridgeContractError,
    build_runtime_policy,
    role_output_schema,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_LOCAL_PROVIDER_ID,
    BRIDGE_OPENAI_API_PROVIDER_ID,
    BRIDGE_SUBSCRIPTION_PROVIDER_ID,
    BridgeRuntimePolicyV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    DeterministicReadOnlyTransport,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from tests.bounded_river_call_ev_support import app_config
from tests.codex_bridge_support import prepared_bridge_request


@pytest.mark.parametrize(
    ("mode", "provider", "credential", "network", "model"),
    (
        (RuntimeAuthModeV1.LOCAL_ONLY, BRIDGE_LOCAL_PROVIDER_ID, "none", False, None),
        (
            RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            BRIDGE_SUBSCRIPTION_PROVIDER_ID,
            "codex_home:saved_chatgpt_login",
            True,
            "gpt-5.6-terra",
        ),
        (
            RuntimeAuthModeV1.OPENAI_API,
            BRIDGE_OPENAI_API_PROVIDER_ID,
            "env:OPENAI_API_KEY",
            True,
            "gpt-5.6-terra",
        ),
    ),
)
def test_runtime_auth_modes_have_disjoint_exact_policies(
    mode: RuntimeAuthModeV1,
    provider: str,
    credential: str,
    network: bool,
    model: str | None,
) -> None:
    policy = build_runtime_policy(
        auth_mode=mode,
        api_max_cost_micro_usd=(204_000 if mode is RuntimeAuthModeV1.OPENAI_API else None),
    )

    assert policy.auth_mode is mode
    assert policy.model_provider == provider
    assert policy.credential_reference == credential
    assert policy.network_allowed is network
    assert policy.model == model
    assert policy.provider_selection_source == "explicit_auth_mode"
    assert policy.api_key_presence_selects_mode is False
    assert policy.provider_fallback_allowed is False
    assert policy.model_fallback_allowed is False
    assert policy.tool_allowlist == ()
    assert policy.shell_enabled is False
    assert policy.web_enabled is False
    assert policy.mcp_enabled is False
    assert policy.apps_enabled is False
    assert policy.nested_agents_enabled is False
    assert policy.file_write_enabled is False


def test_unknown_mode_and_implicit_api_budget_are_rejected() -> None:
    with pytest.raises(ValueError):
        RuntimeAuthModeV1("automatic")
    with pytest.raises(BridgeContractError, match="explicit cost budget"):
        build_runtime_policy(auth_mode=RuntimeAuthModeV1.OPENAI_API)
    with pytest.raises(BridgeContractError, match="forbidden outside"):
        build_runtime_policy(
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            api_max_cost_micro_usd=1,
        )


def test_api_key_presence_does_not_select_or_mutate_other_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-api-key-canary")
    local = build_runtime_policy(auth_mode=RuntimeAuthModeV1.LOCAL_ONLY)
    subscription = build_runtime_policy(auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION)
    encoded = canonical_json_bytes((local, subscription))

    assert local.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY
    assert subscription.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
    assert b"synthetic-api-key-canary" not in encoded
    assert os.environ["OPENAI_API_KEY"] == "synthetic-api-key-canary"


def test_local_only_p3_path_completes_with_network_blocked_and_api_key_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbid_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("local_only attempted network access")

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-api-key-canary")
    monkeypatch.setattr(socket.socket, "connect", forbid_network)
    policy = build_runtime_policy(auth_mode=RuntimeAuthModeV1.LOCAL_ONLY)
    orchestrator = Orchestrator(config=app_config(tmp_path), provider=LocalProvider())
    report = orchestrator.run_bounded_river_call_ev_review(
        build_repository_owned_bounded_river_evaluation_admission(
            "QcJc",
            "local-only-network-blocked",
        )
    )

    assert policy.network_allowed is False
    assert report.run_status == "completed"
    assert orchestrator.product_store.read_current(report.run_id).pointer.status == "succeeded"


def test_policy_and_output_schema_never_accept_credential_values() -> None:
    policy = build_runtime_policy(
        auth_mode=RuntimeAuthModeV1.OPENAI_API,
        api_max_cost_micro_usd=204_000,
    )
    changed = policy.model_dump(mode="python")
    changed["credential_reference"] = "synthetic-secret-canary"
    with pytest.raises(ValidationError):
        BridgeRuntimePolicyV1.model_validate(changed, strict=True)
    schema = canonical_json_bytes(role_output_schema())
    assert b"credential" not in schema.lower()


def test_transport_cannot_cross_auth_modes(tmp_path: Path) -> None:
    request = prepared_bridge_request(tmp_path / "p3")
    transport = DeterministicReadOnlyTransport(
        auth_mode=RuntimeAuthModeV1.OPENAI_API,
        clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
    )

    with pytest.raises(BridgeTransportFailure, match="transport_auth_mode_mismatch"):
        transport.execute(request)


def test_cli_import_and_help_do_not_import_optional_codex_or_api_packages() -> None:
    script = """
import builtins
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'codex_cli_bin' or name.startswith('openai_codex'):
        raise AssertionError('optional runtime import attempted: ' + name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
from poker_deliberation.cli import build_parser
parser = build_parser()
parser.parse_args(['doctor'])
"""

    completed = subprocess.run(
        (sys.executable, "-c", script),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")
