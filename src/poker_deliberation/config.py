"""Application-owned limits and environment configuration."""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from poker_deliberation.budgets import (
    BudgetFailure,
    BudgetFailureCode,
    BudgetLimitError,
    BudgetPolicyV2,
    V1BudgetMigrationResult,
    canonical_budget_sha256,
    decimal_usd_to_micro_usd,
)


class BudgetConfig(BaseModel):
    """Legacy v1 public input; orchestration resolves it once into BudgetPolicyV2."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
    )

    max_deliberation_rounds: int = Field(default=2, ge=0, le=10)
    max_tool_retries: int = Field(default=2, ge=0, le=10)
    max_concurrent_agents: int = Field(default=5, ge=1, le=32)
    max_agent_depth: int = Field(default=1, ge=0, le=4)
    max_runtime_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    max_external_cost_usd: float = Field(default=0.0, ge=0, allow_inf_nan=False)
    max_output_bytes: int = Field(default=1_000_000, ge=1_024)
    max_run_bytes: int = Field(default=10_000_000, ge=10_240)


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runs_dir: Path = Path("runs")
    output_language: str = "ja"
    record_sensitive_data: bool = False
    budgets: BudgetConfig = Field(default_factory=BudgetConfig)

    @classmethod
    def from_env(cls) -> AppConfig:
        requested_provider = os.getenv("POKER_DELIBERATION_PROVIDER")
        unsupported_model_settings = [
            name
            for name in ("POKER_DELIBERATION_MODEL", "POKER_DELIBERATION_REASONING_EFFORT")
            if os.getenv(name)
        ]
        if requested_provider and requested_provider != "local":
            raise ValueError(
                "POKER_DELIBERATION_PROVIDER supports only 'local' in the MVP; "
                "inject an AgentProvider explicitly after approval and integration testing"
            )
        if unsupported_model_settings:
            raise ValueError(
                f"unsupported MVP model settings: {unsupported_model_settings}; "
                "the local provider does not call a model"
            )
        configured_runs_dir = Path(os.getenv("POKER_DELIBERATION_RUNS_DIR", "runs"))
        resolved_runs_dir = configured_runs_dir.resolve()
        workspace = Path.cwd().resolve()
        if resolved_runs_dir != workspace and workspace not in resolved_runs_dir.parents:
            raise ValueError(
                "POKER_DELIBERATION_RUNS_DIR must remain inside the current workspace; "
                "construct AppConfig explicitly only after approving an external location"
            )
        return cls(runs_dir=configured_runs_dir)


def migrate_budget_config(config: BudgetConfig) -> V1BudgetMigrationResult:
    """Map legacy declarations to the behavior that was actually active before P2-011A."""

    source = BudgetConfig.model_validate(config.model_dump(mode="python"))
    if source.max_agent_depth != 1:
        raise BudgetLimitError(
            BudgetFailure(
                code=BudgetFailureCode.UNSUPPORTED_LEGACY_FIELD,
                resource="max_agent_depth",
                message="Python orchestration does not implement an active agent-depth budget",
                limit=1,
                observed=source.max_agent_depth,
            )
        )
    if source.max_concurrent_agents not in {1, 5}:
        raise BudgetLimitError(
            BudgetFailure(
                code=BudgetFailureCode.UNSUPPORTED_CONCURRENCY,
                resource="max_concurrent_agents",
                message=(
                    "legacy concurrency values other than the serial or historical "
                    "default are unsupported"
                ),
                limit=1,
                observed=source.max_concurrent_agents,
            )
        )
    external_cost_micro_usd = decimal_usd_to_micro_usd(Decimal(str(source.max_external_cost_usd)))
    policy = BudgetPolicyV2(
        max_deliberation_rounds=1,
        max_tool_retries=0,
        max_concurrent_agents=1,
        max_runtime_seconds=source.max_runtime_seconds,
        max_external_cost_micro_usd=external_cost_micro_usd,
        max_provider_output_bytes=source.max_output_bytes,
        max_tool_input_bytes=source.max_output_bytes,
        max_tool_output_bytes=source.max_output_bytes,
        max_artifact_bytes=source.max_output_bytes,
        max_run_bytes=source.max_run_bytes,
    )
    return V1BudgetMigrationResult(
        source_config_sha256=canonical_budget_sha256(source.model_dump(mode="json")),
        policy=policy,
        ignored_legacy_fields=(
            "max_agent_depth",
            "max_concurrent_agents",
            "max_deliberation_rounds",
            "max_tool_retries",
        ),
        warnings=(
            "Legacy deliberation rounds did not control ordinary orchestration; "
            "v2 resolves one serial analysis batch.",
            "Legacy tool retries did not execute automatically; "
            "v2 resolves zero automatic retries.",
            "Legacy concurrency did not enable parallel execution; "
            "v2 resolves peak concurrency one.",
            "Legacy max_output_bytes was expanded once into provider, tool-input, "
            "tool-output, and artifact UTF-8 byte caps.",
        ),
    )
