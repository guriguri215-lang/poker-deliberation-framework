from __future__ import annotations

import os
import socket
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from poker_deliberation.bounded_river_call_ev_evaluation import (
    build_repository_owned_bounded_river_evaluation_admission,
)
from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    domain_sha256,
    sha256_bytes,
)
from poker_deliberation.codex_bridge.conformance import build_bridge_role_conformance
from poker_deliberation.codex_bridge.contracts import (
    BridgeContractError,
    admit_role_request,
    build_execution_audit,
    build_role_confirmation,
    build_role_request,
    build_run_plan,
    build_runtime_policy,
    legacy_role_developer_instructions,
    role_output_schema,
)
from poker_deliberation.codex_bridge.controller import (
    canonical_assignment_id,
    canonical_attempt_id,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_LOCAL_PROVIDER_ID,
    BRIDGE_OPENAI_API_PROVIDER_ID,
    BRIDGE_ROLE_ORDER,
    BRIDGE_SUBSCRIPTION_PROVIDER_ID,
    REQUEST_HASH_DOMAIN,
    RUN_PLAN_HASH_DOMAIN,
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeEffectState,
    BridgeRole,
    BridgeRoleConformanceBindingV1,
    BridgeRunPlanV1,
    BridgeRuntimePolicyV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.replay import BridgeReplayError, replay_bridge
from poker_deliberation.codex_bridge.storage import (
    BoundedCodexBridgeStore,
    BridgeStoredArtifact,
)
from poker_deliberation.codex_bridge.transport import (
    BridgeTransportFailure,
    DeterministicReadOnlyTransport,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from tests.bounded_river_call_ev_support import app_config
from tests.codex_bridge_support import (
    REPOSITORY_ROOT,
    prepared_bridge_request,
    verified_bridge_source,
)


def _legacy_request(
    request: BoundedCodexBridgeRequestV1,
    *,
    conformance: BridgeRoleConformanceBindingV1 | None = None,
) -> BoundedCodexBridgeRequestV1:
    payload = request.model_dump(mode="python")
    if conformance is not None:
        payload["context"]["assignment"]["conformance"] = conformance.model_dump(mode="python")
        context = payload["context"]
        context_without_hash = dict(context)
        context_without_hash.pop("envelope_sha256")
        context["envelope_sha256"] = domain_sha256(
            "poker-bounded-codex-bridge-context-v1",
            context_without_hash,
        )
    payload["developer_instructions"] = legacy_role_developer_instructions(
        request.context.assignment.role
    )
    without_hashes = dict(payload)
    without_hashes.pop("request_sha256")
    without_hashes.pop("request_bytes_sha256")
    payload["request_bytes_sha256"] = sha256_bytes(canonical_json_bytes(without_hashes))
    request_projection = dict(payload)
    request_projection.pop("request_sha256")
    payload["request_sha256"] = domain_sha256(REQUEST_HASH_DOMAIN, request_projection)
    return BoundedCodexBridgeRequestV1.model_validate(payload, strict=True)


def _legacy_plan(
    plan: BridgeRunPlanV1,
    conformance: tuple[BridgeRoleConformanceBindingV1, ...],
) -> BridgeRunPlanV1:
    payload = plan.model_dump(mode="python")
    payload["role_conformance"] = tuple(item.model_dump(mode="python") for item in conformance)
    payload.pop("plan_sha256")
    return BridgeRunPlanV1.model_validate(
        {**payload, "plan_sha256": domain_sha256(RUN_PLAN_HASH_DOMAIN, payload)},
        strict=True,
    )


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
    orchestrator = Orchestrator(
        config=app_config(tmp_path),
        provider=LocalProvider(),
        budget_policy=BudgetPolicyV2(max_runtime_seconds=900.0),
    )
    report = orchestrator.run_bounded_river_call_ev_review(
        build_repository_owned_bounded_river_evaluation_admission(
            "QcJc",
            "local-only-network-blocked",
        )
    )

    assert policy.network_allowed is False
    assert report.run_status == "completed", report.limitations
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


@pytest.mark.parametrize(
    "mode",
    (RuntimeAuthModeV1.LOCAL_ONLY, RuntimeAuthModeV1.OPENAI_API),
)
def test_non_subscription_requests_and_fixture_outputs_make_no_skill_claim(
    tmp_path: Path,
    mode: RuntimeAuthModeV1,
) -> None:
    request = prepared_bridge_request(tmp_path / mode.value, auth_mode=mode)
    conformance = request.context.assignment.conformance
    response = (
        DeterministicReadOnlyTransport(
            auth_mode=mode,
            clock=lambda: datetime(2030, 1, 1, tzinfo=UTC),
        )
        .execute(request)
        .response_bytes
    )

    assert conformance.has_complete_repository_skill_binding is False
    assert conformance.repository_skill_id is None
    assert " Apply $" not in request.developer_instructions
    assert b"repository_skill_" not in canonical_json_bytes(request)
    assert b"applied_skill" not in response


@pytest.mark.parametrize(
    ("mode", "legacy_replay_allowed"),
    (
        (RuntimeAuthModeV1.LOCAL_ONLY, True),
        (RuntimeAuthModeV1.OPENAI_API, True),
        (RuntimeAuthModeV1.CODEX_SUBSCRIPTION, False),
    ),
)
def test_legacy_terminal_replay_is_auth_mode_and_skill_shape_bound(
    tmp_path: Path,
    mode: RuntimeAuthModeV1,
    legacy_replay_allowed: bool,
) -> None:
    created = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)
    bridge_run_id = f"bridge-run-legacy-{mode.value}"
    source = verified_bridge_source(tmp_path / f"p3-{mode.value}")
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        include_repository_skill_bindings=(mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION),
    )
    policy = build_runtime_policy(
        auth_mode=mode,
        api_max_cost_micro_usd=(204_000 if mode is RuntimeAuthModeV1.OPENAI_API else None),
    )
    plan = build_run_plan(
        bridge_run_id=bridge_run_id,
        source_context=source,
        runtime_policy=policy,
        role_conformance=conformance,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        created_at=created,
    )
    role = BridgeRole.STRATEGY_ANALYST
    current_request = build_role_request(
        bridge_run_id=bridge_run_id,
        role=role,
        assignment_id=canonical_assignment_id(bridge_run_id, mode, role),
        attempt_id=canonical_attempt_id(bridge_run_id, mode, role),
        expires_at=created + timedelta(days=7),
        source_context=source,
        runtime_policy=policy,
        conformance=conformance[0],
    )
    request = _legacy_request(current_request)
    authority = BridgeConfirmationAuthorityV1(
        authority_id="local-test-user",
        authority_kind="local_user",
        authentication="self_asserted",
    )
    confirmation = build_role_confirmation(
        request,
        confirmation_id=f"confirmation-legacy-{mode.value}",
        idempotency_key=f"idempotency-legacy-{mode.value}",
        authority=authority,
        confirmed_at=created + timedelta(seconds=1),
    )
    admission = admit_role_request(
        request,
        confirmation,
        admitted_at=created + timedelta(seconds=2),
        current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
    )
    audit = build_execution_audit(
        request,
        confirmation,
        admission,
        transport_qualification="deterministic_fixture",
        effect_state=BridgeEffectState.NOT_LAUNCHED,
        thread_id_sha256=None,
        turn_id_sha256=None,
        launched_at=None,
        completed_at=created + timedelta(seconds=3),
        duration_ms=0,
        usage=None,
        response_bytes=None,
        stream_bytes=0,
        unexpected_item_types=(),
        cancellation_kind="not_requested",
        result_sha256=None,
        failure_reason_code="legacy_terminal_fixture",
        model_identity_evidence="unavailable",
        observed_model=None,
        observed_model_provider=None,
        observed_reasoning_effort=None,
        observed_service_tier=None,
        observed_identity_sha256=None,
    )
    store = BoundedCodexBridgeStore(tmp_path / f"bridge-{mode.value}")
    store.claim_confirmation_identifiers(
        bridge_run_id=bridge_run_id,
        auth_mode=mode,
        role=role,
        request_sha256=request.request_sha256,
        confirmation_id=confirmation.confirmation_id,
        idempotency_key=confirmation.idempotency_key,
    )
    artifacts = (
        BridgeStoredArtifact("run_plan.json", "run_plan", plan),
        BridgeStoredArtifact("source_context.json", "source_context", source),
        BridgeStoredArtifact(role_artifact_name(role, "request"), "request", request),
        BridgeStoredArtifact(
            role_artifact_name(role, "confirmation"),
            "confirmation",
            confirmation,
        ),
        BridgeStoredArtifact(role_artifact_name(role, "admission"), "admission", admission),
        BridgeStoredArtifact(role_artifact_name(role, "audit"), "execution_audit", audit),
    )
    store.publish(
        store.prepare_request(
            run_plan=plan,
            status="failed",
            expected=None,
            published_at=created + timedelta(seconds=4),
            artifacts=artifacts,
        )
    )

    read = store.read_current(bridge_run_id)
    assert read.pointer.status == "failed"
    assert canonical_json_bytes(request) == read.artifact_bytes(role_artifact_name(role, "request"))
    if legacy_replay_allowed:
        replayed = replay_bridge(read)
        assert replayed.status == "failed"
        assert replayed.completed_roles == ()
    else:
        with pytest.raises(BridgeReplayError, match="cannot carry repository Skill"):
            replay_bridge(read)


def test_legacy_subscription_no_skill_run_remains_readable_and_replayable(
    tmp_path: Path,
) -> None:
    created = datetime(2029, 12, 31, 23, 40, tzinfo=UTC)
    mode = RuntimeAuthModeV1.CODEX_SUBSCRIPTION
    bridge_run_id = "bridge-run-legacy-subscription-no-skill"
    source = verified_bridge_source(tmp_path / "p3-legacy-subscription")
    current_conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        include_repository_skill_bindings=True,
    )
    legacy_conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        include_repository_skill_bindings=False,
    )
    policy = build_runtime_policy(auth_mode=mode)
    current_plan = build_run_plan(
        bridge_run_id=bridge_run_id,
        source_context=source,
        runtime_policy=policy,
        role_conformance=current_conformance,
        repository_commit_id="1" * 40,
        repository_tree_id="2" * 40,
        created_at=created,
    )
    plan = _legacy_plan(current_plan, legacy_conformance)
    requests = tuple(
        _legacy_request(
            build_role_request(
                bridge_run_id=bridge_run_id,
                role=role,
                assignment_id=canonical_assignment_id(bridge_run_id, mode, role),
                attempt_id=canonical_attempt_id(bridge_run_id, mode, role),
                expires_at=created + timedelta(days=7),
                source_context=source,
                runtime_policy=policy,
                conformance=current_conformance[ordinal],
            ),
            conformance=legacy_conformance[ordinal],
        )
        for ordinal, role in enumerate(BRIDGE_ROLE_ORDER[:3])
    )
    store = BoundedCodexBridgeStore(tmp_path / "bridge-legacy-subscription")
    artifacts = (
        BridgeStoredArtifact("run_plan.json", "run_plan", plan),
        BridgeStoredArtifact("source_context.json", "source_context", source),
        *(
            BridgeStoredArtifact(
                role_artifact_name(request.context.assignment.role, "request"),
                "request",
                request,
            )
            for request in requests
        ),
    )
    store.publish(
        store.prepare_request(
            run_plan=plan,
            status="approval_required",
            expected=None,
            published_at=created + timedelta(seconds=1),
            artifacts=artifacts,
        )
    )

    read = store.read_current(bridge_run_id)
    replayed = replay_bridge(read)
    assert read.pointer.status == "approval_required"
    assert replayed.status == "approval_required"
    assert replayed.completed_roles == ()
    assert all(not item.has_complete_repository_skill_binding for item in plan.role_conformance)


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
