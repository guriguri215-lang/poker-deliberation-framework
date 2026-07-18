"""Application-owned limits and environment configuration."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class BudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_deliberation_rounds: int = Field(default=2, ge=0, le=10)
    max_tool_retries: int = Field(default=2, ge=0, le=10)
    max_concurrent_agents: int = Field(default=5, ge=1, le=32)
    max_agent_depth: int = Field(default=1, ge=0, le=4)
    max_runtime_seconds: float = Field(default=300.0, gt=0)
    max_external_cost_usd: float = Field(default=0.0, ge=0)
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
