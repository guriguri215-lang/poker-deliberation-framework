"""Optional OpenAI Agents SDK adapter with explicit availability checks."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os

from poker_deliberation.budgets import ExecutionClass
from poker_deliberation.providers.base import (
    ProviderAvailability,
    ProviderControl,
    ProviderStatus,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport


class OpenAIAgentsProvider:
    def availability(self) -> ProviderAvailability:
        package_present = importlib.util.find_spec("agents") is not None
        api_key_present = bool(os.getenv("OPENAI_API_KEY"))
        package_state = "present" if package_present else "absent"
        key_state = "configured" if api_key_present else "not configured"
        return ProviderAvailability(
            status=ProviderStatus.DISABLED,
            available=False,
            provider="openai-agents",
            reason=(
                "outbound analyze is not implemented and is disabled; "
                f"SDK package is {package_state}; API key is {key_state}"
            ),
            version=self._version() if package_present else None,
            execution_class=ExecutionClass.EXTERNAL,
        )

    @staticmethod
    def _version() -> str | None:
        try:
            return importlib.metadata.version("openai-agents")
        except importlib.metadata.PackageNotFoundError:
            return None

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport:
        control.raise_if_cancelled()
        raise NotImplementedError(
            "OpenAIAgentsProvider outbound analyze is not implemented; no user data was sent"
        )
