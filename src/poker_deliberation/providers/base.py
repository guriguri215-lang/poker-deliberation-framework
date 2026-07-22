"""Provider boundary for optional model-backed analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation.budgets import (
    CancellationStatus,
    DeadlineStatus,
    ExecutionClass,
    MonotonicClock,
    SystemMonotonicClock,
)
from poker_deliberation.schemas import AgentAssignment, AgentContext, AgentReport


class ProviderStatus(StrEnum):
    """User-visible execution state for a provider capability."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


class ProviderAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: ProviderStatus
    available: bool
    provider: str
    reason: str
    version: str | None = None
    execution_class: ExecutionClass = ExecutionClass.UNKNOWN
    estimated_cost_micro_usd: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def infer_legacy_status(cls, value: Any) -> Any:
        """Keep explicit injected providers compatible while making state unambiguous."""

        if isinstance(value, dict) and "status" not in value and "available" in value:
            return {
                **value,
                "status": (
                    ProviderStatus.AVAILABLE
                    if bool(value["available"])
                    else ProviderStatus.UNAVAILABLE
                ),
            }
        return value

    @model_validator(mode="after")
    def status_matches_available(self) -> ProviderAvailability:
        if self.available != (self.status is ProviderStatus.AVAILABLE):
            raise ValueError("available must be true exactly when status is 'available'")
        return self


class ProviderControlError(TimeoutError):
    def __init__(
        self,
        message: str,
        *,
        deadline_status: DeadlineStatus,
        cancellation_status: CancellationStatus,
    ) -> None:
        super().__init__(message)
        self.deadline_status = deadline_status
        self.cancellation_status = cancellation_status


@dataclass(slots=True)
class ProviderControl:
    """Cooperative deadline/cancellation contract for every provider implementation."""

    timeout_seconds: float
    clock: MonotonicClock = field(default_factory=SystemMonotonicClock)
    _cancelled: Event = field(default_factory=Event)
    _started_ns: int = field(init=False)
    _cancellation_status: CancellationStatus = field(
        default=CancellationStatus.NOT_REQUESTED,
        init=False,
    )

    def __post_init__(self) -> None:
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise TypeError("timeout_seconds must be numeric")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = float(self.timeout_seconds)
        self._started_ns = self.clock.now_ns()

    @property
    def deadline_status(self) -> DeadlineStatus:
        return (
            DeadlineStatus.TIMED_OUT
            if self.clock.now_ns() - self._started_ns >= int(self.timeout_seconds * 1_000_000_000)
            else DeadlineStatus.ACTIVE
        )

    @property
    def cancellation_status(self) -> CancellationStatus:
        return self._cancellation_status

    @property
    def remaining_seconds(self) -> float:
        elapsed = (self.clock.now_ns() - self._started_ns) / 1_000_000_000
        return max(0.0, self.timeout_seconds - elapsed)

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set() or self.deadline_status is DeadlineStatus.TIMED_OUT

    def cancel(self) -> None:
        self.request_cancel()

    def request_cancel(self) -> None:
        self._cancelled.set()
        if self._cancellation_status is CancellationStatus.NOT_REQUESTED:
            self._cancellation_status = CancellationStatus.CANCEL_REQUESTED

    def acknowledge_cancel(self) -> None:
        self.request_cancel()
        self._cancellation_status = CancellationStatus.CANCELLED

    def mark_cancel_unconfirmed(self) -> None:
        self.request_cancel()
        self._cancellation_status = CancellationStatus.CANCEL_UNCONFIRMED

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            self.acknowledge_cancel()
            raise ProviderControlError(
                "provider deadline/cancellation acknowledged cooperatively",
                deadline_status=self.deadline_status,
                cancellation_status=self.cancellation_status,
            )


class AgentProvider(Protocol):
    def availability(self) -> ProviderAvailability: ...

    def analyze(
        self,
        context: AgentContext,
        assignment: AgentAssignment,
        control: ProviderControl,
    ) -> AgentReport: ...
