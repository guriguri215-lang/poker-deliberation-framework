"""Product-facing bounded bridge operations used by the CLI."""

from __future__ import annotations

import base64
from pathlib import Path

from poker_deliberation.codex_bridge.canonical import sha256_bytes
from poker_deliberation.codex_bridge.contracts import outbound_request_bytes
from poker_deliberation.codex_bridge.controller import BoundedCodexBridgeController
from poker_deliberation.codex_bridge.identity import (
    verify_bridge_checkout,
    verify_bridge_module_origins,
)
from poker_deliberation.codex_bridge.models import (
    BoundedCodexBridgeRequestV1,
    BridgeConfirmationAuthorityV1,
    BridgeRole,
    BridgeSourceContextV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.replay import BridgeReplayResult, replay_bridge
from poker_deliberation.codex_bridge.sdk_transport import OpenAIAPITransport
from poker_deliberation.codex_bridge.source import project_verified_p3_terminal
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore, VerifiedBridgeRead
from poker_deliberation.codex_bridge.subscription_transport import CodexSubscriptionCliTransport
from poker_deliberation.codex_bridge.transport import BridgeTransport
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider


class BridgeProductError(ValueError):
    """Raised when a product operation would escape the bounded bridge contract."""


def confined_product_path(path: Path, repository_root: Path) -> Path:
    """Resolve a bridge-owned path without permitting repository metadata/user data writes."""

    repository = repository_root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    if (
        resolved == repository
        or repository not in resolved.parents
        or any(
            part.casefold() in {".git", "user_materials"}
            for part in resolved.relative_to(repository).parts
        )
    ):
        raise BridgeProductError("bridge path is outside its repository-owned namespace")
    return resolved


def _require_disjoint(path: Path, protected: tuple[Path, ...], label: str) -> None:
    if any(path == other or path in other.parents or other in path.parents for other in protected):
        raise BridgeProductError(f"{label} overlaps protected product storage")


def _verified_source(
    config: AppConfig,
    source_run_id: str,
) -> tuple[BridgeSourceContextV1, str]:
    orchestrator = Orchestrator(config=config, provider=LocalProvider())
    read = orchestrator.product_store.read_current(source_run_id)
    source = project_verified_p3_terminal(
        read,
        source_revision_root=orchestrator.product_store.revision_root,
    )
    return source, read.manifest_sha256


def prepare_product_bridge(
    *,
    config: AppConfig,
    repository_root: Path,
    bridge_root: Path,
    source_run_id: str,
    bridge_run_id: str,
    repository_commit_id: str,
    repository_tree_id: str,
    auth_mode: RuntimeAuthModeV1,
    api_max_cost_micro_usd: int | None = None,
) -> VerifiedBridgeRead:
    repository = repository_root.resolve(strict=True)
    verify_bridge_checkout(
        repository,
        repository_commit_id=repository_commit_id,
        repository_tree_id=repository_tree_id,
    )
    verify_bridge_module_origins(repository)
    root = confined_product_path(bridge_root, repository_root)
    _require_disjoint(root, config.resolved_storage_roots(), "bridge storage")
    source, _manifest = _verified_source(config, source_run_id)
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(root))
    return controller.prepare_run(
        bridge_run_id=bridge_run_id,
        source_context=source,
        repository_root=repository,
        repository_commit_id=repository_commit_id,
        repository_tree_id=repository_tree_id,
        auth_mode=auth_mode,
        api_max_cost_micro_usd=api_max_cost_micro_usd,
    )


def role_request_preview(request: BoundedCodexBridgeRequestV1) -> dict[str, object]:
    outbound = outbound_request_bytes(request)
    if sha256_bytes(outbound) != request.request_bytes_sha256:
        raise BridgeProductError("outbound preview hash mismatch")
    assignment = request.context.assignment
    policy = request.context.runtime_policy
    return {
        "schema_version": "1.0.0",
        "bridge_run_id": assignment.bridge_run_id,
        "auth_mode": request.auth_mode,
        "role": assignment.role,
        "assignment_id": assignment.assignment_id,
        "attempt_id": assignment.attempt_id,
        "parent_assignment_ids": assignment.parent_assignment_ids,
        "expires_at": assignment.expires_at.isoformat().replace("+00:00", "Z"),
        "request_sha256": request.request_sha256,
        "request_bytes_sha256": request.request_bytes_sha256,
        "outbound_scope": "application_owned_canonical_stdin",
        "outbound_bytes": len(outbound),
        "outbound_utf8": outbound.decode("utf-8"),
        "outbound_base64": base64.b64encode(outbound).decode("ascii"),
        "envelope_sha256": request.context.envelope_sha256,
        "runtime_policy_sha256": policy.policy_sha256,
        "runtime_identity": policy.runtime_identity,
        "runtime_binary_sha256": policy.runtime_binary_sha256,
        "model": policy.model,
        "model_provider": policy.model_provider,
        "auth_boundary": (
            "chatgpt"
            if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
            else ("openai_api_key" if request.auth_mode is RuntimeAuthModeV1.OPENAI_API else "none")
        ),
        "effective_model_identity_status": (
            "UNKNOWN_codex_exec_json_not_exposed"
            if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
            else "not_applicable_to_confirmation"
        ),
        "actual_backend_model_input_status": (
            "UNKNOWN_codex_exec_json_not_exposed"
            if request.auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
            else "not_applicable_to_confirmation"
        ),
        "reasoning_effort": policy.reasoning_effort,
        "service_tier": policy.service_tier,
        "classification": policy.classification,
        "usage_classification": policy.usage_classification,
        "model_processing_authorized": policy.model_processing_authorized,
        "credential_reference": policy.credential_reference,
        "credential_value_access": policy.credential_value_access,
        "trace_policy": policy.trace_policy,
        "remote_retention_policy": policy.remote_retention_policy,
        "tool_allowlist": policy.tool_allowlist,
        "provider_fallback_allowed": policy.provider_fallback_allowed,
        "model_fallback_allowed": policy.model_fallback_allowed,
        "network_allowed": policy.network_allowed,
        "budget": policy.budget.model_dump(mode="json"),
    }


def read_product_request(
    *,
    repository_root: Path,
    bridge_root: Path,
    bridge_run_id: str,
    role: BridgeRole,
    auth_mode: RuntimeAuthModeV1,
) -> BoundedCodexBridgeRequestV1:
    root = confined_product_path(bridge_root, repository_root)
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(root))
    request = controller.read_role_request(bridge_run_id, role)
    if request.auth_mode is not auth_mode:
        raise BridgeProductError("request auth mode mismatch")
    return request


def confirm_product_role(
    *,
    repository_root: Path,
    bridge_root: Path,
    bridge_run_id: str,
    role: BridgeRole,
    authority_id: str,
    confirmation_id: str,
    idempotency_key: str,
    expected_request_sha256: str,
    expected_request_bytes_sha256: str,
    expected_envelope_sha256: str,
    expected_runtime_policy_sha256: str,
    expected_auth_mode: RuntimeAuthModeV1,
    expected_runtime_identity: str,
    expected_model_provider: str,
    expected_model: str | None,
    expected_credential_reference: str,
    expected_remote_retention_policy: str,
) -> VerifiedBridgeRead:
    root = confined_product_path(bridge_root, repository_root)
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(root))
    return controller.confirm_role(
        bridge_run_id,
        role,
        authority=BridgeConfirmationAuthorityV1(
            authority_id=authority_id,
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        confirmation_id=confirmation_id,
        idempotency_key=idempotency_key,
        expected_request_sha256=expected_request_sha256,
        expected_request_bytes_sha256=expected_request_bytes_sha256,
        expected_envelope_sha256=expected_envelope_sha256,
        expected_runtime_policy_sha256=expected_runtime_policy_sha256,
        expected_auth_mode=expected_auth_mode,
        expected_runtime_identity=expected_runtime_identity,
        expected_model_provider=expected_model_provider,
        expected_model=expected_model,
        expected_credential_reference=expected_credential_reference,
        expected_remote_retention_policy=expected_remote_retention_policy,
    )


def execute_product_role(
    *,
    config: AppConfig,
    repository_root: Path,
    bridge_root: Path,
    runtime_root: Path,
    bridge_run_id: str,
    role: BridgeRole,
    auth_mode: RuntimeAuthModeV1,
    codex_binary: Path | None = None,
) -> VerifiedBridgeRead:
    bridge = confined_product_path(bridge_root, repository_root)
    runtime = confined_product_path(runtime_root, repository_root)
    if bridge == runtime or bridge in runtime.parents or runtime in bridge.parents:
        raise BridgeProductError("bridge storage and runtime scratch roots must not overlap")
    protected = config.resolved_storage_roots()
    _require_disjoint(bridge, protected, "bridge storage")
    _require_disjoint(runtime, protected, "runtime scratch")
    controller = BoundedCodexBridgeController(BoundedCodexBridgeStore(bridge))
    plan = controller.read_run_plan(bridge_run_id)
    if plan.auth_mode is not auth_mode:
        raise BridgeProductError("execution auth mode mismatch")
    if auth_mode is RuntimeAuthModeV1.LOCAL_ONLY:
        raise BridgeProductError("local_only never launches a model or network transport")
    verify_bridge_checkout(
        repository_root,
        repository_commit_id=plan.repository_commit_id,
        repository_tree_id=plan.repository_tree_id,
    )
    verify_bridge_module_origins(repository_root)
    stored_source = controller.read_source_context(bridge_run_id)
    current_source, current_manifest_sha256 = _verified_source(
        config,
        stored_source.source.source_terminal_run_id,
    )
    if current_source != stored_source:
        raise BridgeProductError("current P3-030C source no longer matches the bridge context")
    transport: BridgeTransport
    if auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION:
        if codex_binary is None:
            try:
                from codex_cli_bin import bundled_codex_path  # type: ignore[import-untyped]
            except ImportError as exc:
                raise BridgeProductError("subscription runtime extra is not installed") from exc
            codex_binary = bundled_codex_path()
        transport = CodexSubscriptionCliTransport(runtime, codex_binary=codex_binary)
    elif auth_mode is RuntimeAuthModeV1.OPENAI_API:
        transport = OpenAIAPITransport(
            runtime,
            repository_root=repository_root,
            repository_commit_id=plan.repository_commit_id,
            repository_tree_id=plan.repository_tree_id,
        )
    else:  # pragma: no cover - enum construction rejects unknown modes
        raise BridgeProductError("unknown execution auth mode")
    return controller.execute_confirmed_role(
        bridge_run_id,
        role,
        auth_mode=auth_mode,
        current_source_terminal_manifest_sha256=current_manifest_sha256,
        transport=transport,
    )


def replay_product_bridge(
    *,
    repository_root: Path,
    bridge_root: Path,
    bridge_run_id: str,
    auth_mode: RuntimeAuthModeV1,
) -> BridgeReplayResult:
    root = confined_product_path(bridge_root, repository_root)
    replayed = replay_bridge(BoundedCodexBridgeStore(root).read_current(bridge_run_id))
    if replayed.auth_mode is not auth_mode:
        raise BridgeProductError("terminal replay auth mode mismatch")
    return replayed


def bridge_read_summary(read: VerifiedBridgeRead) -> dict[str, object]:
    replayed = replay_bridge(read)
    return {
        "bridge_run_id": replayed.bridge_run_id,
        "auth_mode": replayed.auth_mode,
        "revision": replayed.revision,
        "status": replayed.status,
        "completed_roles": replayed.completed_roles,
        "pending_roles": replayed.pending_roles,
        "reconciliation_required": replayed.reconciliation_required,
        "total_input_tokens": replayed.total_input_tokens,
        "total_output_tokens": replayed.total_output_tokens,
        "total_estimated_cost_micro_usd": replayed.total_estimated_cost_micro_usd,
        "manifest_sha256": read.manifest.manifest_sha256,
        "inventory_sha256": read.manifest.inventory_sha256,
        "completion_marker_sha256": read.pointer.completion_marker_sha256,
    }


__all__ = [
    "BridgeProductError",
    "bridge_read_summary",
    "confined_product_path",
    "confirm_product_role",
    "execute_product_role",
    "prepare_product_bridge",
    "read_product_request",
    "replay_product_bridge",
    "role_request_preview",
]
