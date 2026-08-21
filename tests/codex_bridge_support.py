from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from poker_deliberation.codex_bridge.conformance import build_bridge_role_conformance
from poker_deliberation.codex_bridge.contracts import (
    build_role_request,
    build_runtime_policy,
)
from poker_deliberation.codex_bridge.models import (
    BoundedCodexBridgeRequestV1,
    BridgeRole,
    BridgeSourceContextV1,
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.source import project_verified_p3_terminal
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from tests.bounded_river_call_ev_support import admission, app_config

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def verified_bridge_source(
    root: Path,
    *,
    run_id: str = "run-codex-bridge-source",
) -> BridgeSourceContextV1:
    orchestrator = Orchestrator(config=app_config(root), provider=LocalProvider())
    report = orchestrator.run_bounded_river_call_ev_review(admission(run_id=run_id))
    read = orchestrator.product_store.read_current(report.run_id)
    return project_verified_p3_terminal(
        read,
        source_revision_root=orchestrator.product_store.revision_root,
    )


def prepared_bridge_request(
    root: Path,
    *,
    auth_mode: RuntimeAuthModeV1 = RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
) -> BoundedCodexBridgeRequestV1:
    source = verified_bridge_source(root)
    conformance = build_bridge_role_conformance(
        REPOSITORY_ROOT,
        repository_commit_id="1" * 40,
        include_repository_skill_bindings=(auth_mode is RuntimeAuthModeV1.CODEX_SUBSCRIPTION),
    )
    return build_role_request(
        bridge_run_id="bridge-run-sdk-transport",
        role=BridgeRole.STRATEGY_ANALYST,
        assignment_id=f"assignment-{auth_mode.value}-strategy",
        attempt_id=f"attempt-{auth_mode.value}-strategy",
        expires_at=datetime(2030, 1, 1, tzinfo=UTC),
        source_context=source,
        runtime_policy=build_runtime_policy(
            auth_mode=auth_mode,
            api_max_cost_micro_usd=(204_000 if auth_mode is RuntimeAuthModeV1.OPENAI_API else None),
        ),
        conformance=conformance[0],
    )


__all__ = ["REPOSITORY_ROOT", "prepared_bridge_request", "verified_bridge_source"]
