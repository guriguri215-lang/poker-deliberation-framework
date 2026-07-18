"""Optional OpenAI Agents SDK adapter with explicit availability checks."""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os

from poker_deliberation.providers.base import ProviderAvailability, ProviderControl
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport


class OpenAIAgentsProvider:
    def availability(self) -> ProviderAvailability:
        if importlib.util.find_spec("agents") is None:
            return ProviderAvailability(
                available=False,
                provider="openai-agents",
                reason="openai-agents package is not installed",
            )
        if not os.getenv("OPENAI_API_KEY"):
            return ProviderAvailability(
                available=False,
                provider="openai-agents",
                reason="OPENAI_API_KEY is not configured",
                version=self._version(),
            )
        return ProviderAvailability(
            available=True,
            provider="openai-agents",
            reason="SDK and API key are available",
            version=self._version(),
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
        availability = self.availability()
        if not availability.available:
            raise RuntimeError(availability.reason)
        raise NotImplementedError(
            "MVP defines the provider boundary but does not send user data externally; "
            "implement after explicit approval and integration tests"
        )
