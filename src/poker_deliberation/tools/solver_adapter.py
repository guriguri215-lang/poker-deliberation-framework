"""External solver capability contract with honest unavailable results."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field


class SolverCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str | None = None
    available: bool
    supported_games: list[str] = Field(default_factory=list)
    supported_operations: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class SolverResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    operation: str
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    capability: SolverCapability


class SolverAdapter(Protocol):
    def capability(self) -> SolverCapability: ...

    def health_check(self) -> SolverResponse: ...

    def solve(self, request: dict[str, Any]) -> SolverResponse: ...

    def best_response(self, request: dict[str, Any]) -> SolverResponse: ...

    def exploitability(self, request: dict[str, Any]) -> SolverResponse: ...

    def resource_estimate(self, request: dict[str, Any]) -> SolverResponse: ...

    def cancel(self, job_id: str) -> SolverResponse: ...


class UnavailableSolverAdapter:
    def __init__(self, name: str = "external-poker-solver") -> None:
        self._capability = SolverCapability(
            name=name,
            available=False,
            supported_games=[],
            supported_operations=[],
            limitations=["No approved external solver is configured."],
        )

    def capability(self) -> SolverCapability:
        return self._capability

    def _unavailable(self, operation: str) -> SolverResponse:
        return SolverResponse(
            status="unavailable",
            operation=operation,
            error="solver unavailable; no equilibrium or strategy result was generated",
            capability=self._capability,
        )

    def health_check(self) -> SolverResponse:
        return self._unavailable("health_check")

    def solve(self, request: dict[str, Any]) -> SolverResponse:
        return self._unavailable("solve")

    def best_response(self, request: dict[str, Any]) -> SolverResponse:
        return self._unavailable("best_response")

    def exploitability(self, request: dict[str, Any]) -> SolverResponse:
        return self._unavailable("exploitability")

    def resource_estimate(self, request: dict[str, Any]) -> SolverResponse:
        return self._unavailable("resource_estimate")

    def cancel(self, job_id: str) -> SolverResponse:
        return self._unavailable("cancel")
