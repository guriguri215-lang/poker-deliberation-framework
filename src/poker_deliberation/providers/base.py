"""Provider boundary for optional model-backed analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from time import monotonic
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport


class ProviderAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    provider: str
    reason: str
    version: str | None = None


@dataclass(slots=True)
class ProviderControl:
    """Cooperative deadline/cancellation contract for every provider implementation."""

    timeout_seconds: float
    started_at: float = field(default_factory=monotonic)
    _cancelled: Event = field(default_factory=Event)

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.timeout_seconds - (monotonic() - self.started_at))

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set() or self.remaining_seconds <= 0

    def cancel(self) -> None:
        self._cancelled.set()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise TimeoutError("provider deadline/cancellation reached")


class AgentProvider(Protocol):
    def availability(self) -> ProviderAvailability: ...

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport: ...
