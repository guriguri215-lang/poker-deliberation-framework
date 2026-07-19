"""Scripted provider for deterministic integration and adversarial tests."""

from __future__ import annotations

from typing import Any

from poker_deliberation.providers.base import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport


class DeterministicMockProvider:
    def __init__(self, scripts: dict[str, AgentReport | dict[str, Any]]) -> None:
        self._scripts = {
            role: AgentReport.model_validate(report).model_copy(deep=True)
            for role, report in scripts.items()
        }
        self.contexts: list[tuple[str, AgentContext]] = []

    def availability(self) -> ProviderAvailability:
        return ProviderAvailability(
            status=ProviderStatus.AVAILABLE,
            available=True,
            provider="deterministic-mock",
            reason="scripted test reports are available",
            version="1.0.0",
        )

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        control.raise_if_cancelled()
        self.contexts.append((assignment.agent_role, context.model_copy(deep=True)))
        if assignment.agent_role not in self._scripts:
            raise RuntimeError(f"no scripted report for role {assignment.agent_role!r}")
        report = self._scripts[assignment.agent_role].model_copy(deep=True)
        if report.agent_role != assignment.agent_role:
            raise ValueError("scripted report role does not match assignment")
        return report
