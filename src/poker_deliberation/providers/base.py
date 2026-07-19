"""Provider boundary for optional model-backed analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event
from time import monotonic
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport


class ProviderStatus(StrEnum):
    """User-visible execution state for a provider capability."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class ProviderAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ProviderStatus
    available: bool
    provider: str
    reason: str
    version: str | None = None

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_status(cls, value: Any) -> Any:
        """Keep explicit injected providers compatible while making state unambiguous."""

        if isinstance(value, dict) and "status" not in value and "available" in value:
            return {
                **value,
                "status": "available" if bool(value["available"]) else "unavailable",
            }
        return value

    @model_validator(mode="after")
    def status_matches_available(self) -> ProviderAvailability:
        if self.available != (self.status is ProviderStatus.AVAILABLE):
            raise ValueError("available must be true exactly when status is 'available'")
        return self


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
