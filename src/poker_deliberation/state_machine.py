"""Explicit, bounded workflow state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from time import monotonic

from poker_deliberation.config import BudgetConfig


class RunState(StrEnum):
    INTAKE = "INTAKE"
    NORMALIZE = "NORMALIZE"
    DATA_VALIDATION = "DATA_VALIDATION"
    TASK_ROUTING = "TASK_ROUTING"
    INDEPENDENT_ANALYSIS = "INDEPENDENT_ANALYSIS"
    TOOL_AND_RESEARCH = "TOOL_AND_RESEARCH"
    CRITIQUE = "CRITIQUE"
    ADJUDICATION = "ADJUDICATION"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"
    FINAL_SYNTHESIS = "FINAL_SYNTHESIS"
    COMPLETED = "COMPLETED"
    FAILED_WITH_LIMITATIONS = "FAILED_WITH_LIMITATIONS"


TERMINAL_STATES = {RunState.COMPLETED, RunState.FAILED_WITH_LIMITATIONS}

ALLOWED_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.INTAKE: {RunState.NORMALIZE, RunState.FAILED_WITH_LIMITATIONS},
    RunState.NORMALIZE: {RunState.DATA_VALIDATION, RunState.FAILED_WITH_LIMITATIONS},
    RunState.DATA_VALIDATION: {
        RunState.TASK_ROUTING,
        RunState.HUMAN_REVIEW_REQUIRED,
        RunState.FAILED_WITH_LIMITATIONS,
    },
    RunState.TASK_ROUTING: {
        RunState.INDEPENDENT_ANALYSIS,
        RunState.TOOL_AND_RESEARCH,
        RunState.FAILED_WITH_LIMITATIONS,
    },
    RunState.INDEPENDENT_ANALYSIS: {
        RunState.TOOL_AND_RESEARCH,
        RunState.CRITIQUE,
        RunState.FAILED_WITH_LIMITATIONS,
    },
    RunState.TOOL_AND_RESEARCH: {
        RunState.CRITIQUE,
        RunState.HUMAN_REVIEW_REQUIRED,
        RunState.FAILED_WITH_LIMITATIONS,
    },
    RunState.CRITIQUE: {
        RunState.TOOL_AND_RESEARCH,
        RunState.ADJUDICATION,
        RunState.FAILED_WITH_LIMITATIONS,
    },
    RunState.ADJUDICATION: {
        RunState.HUMAN_REVIEW_REQUIRED,
        RunState.FINAL_SYNTHESIS,
        RunState.FAILED_WITH_LIMITATIONS,
    },
    RunState.HUMAN_REVIEW_REQUIRED: {
        RunState.TOOL_AND_RESEARCH,
        RunState.FINAL_SYNTHESIS,
        RunState.FAILED_WITH_LIMITATIONS,
    },
    RunState.FINAL_SYNTHESIS: {RunState.COMPLETED, RunState.FAILED_WITH_LIMITATIONS},
    RunState.COMPLETED: set(),
    RunState.FAILED_WITH_LIMITATIONS: set(),
}


@dataclass(slots=True)
class StateEvent:
    source: RunState
    target: RunState
    reason: str


@dataclass(slots=True)
class WorkflowStateMachine:
    budgets: BudgetConfig
    state: RunState = RunState.INTAKE
    events: list[StateEvent] = field(default_factory=list)
    deliberation_rounds: int = 0
    tool_retries: dict[str, int] = field(default_factory=dict)
    started_at: float = field(default_factory=monotonic)

    @classmethod
    def from_snapshot(
        cls, budgets: BudgetConfig, snapshot: dict[str, object]
    ) -> WorkflowStateMachine:
        machine = cls(budgets=budgets, state=RunState(str(snapshot["state"])))
        raw_events = snapshot.get("events", [])
        if isinstance(raw_events, list):
            machine.events = [
                StateEvent(
                    source=RunState(str(item["source"])),
                    target=RunState(str(item["target"])),
                    reason=str(item["reason"]),
                )
                for item in raw_events
                if isinstance(item, dict)
            ]
        machine.deliberation_rounds = int(str(snapshot.get("deliberation_rounds", 0)))
        raw_retries = snapshot.get("tool_retries", {})
        if isinstance(raw_retries, dict):
            machine.tool_retries = {str(key): int(value) for key, value in raw_retries.items()}
        machine.started_at = monotonic() - float(str(snapshot.get("elapsed_seconds", 0.0)))
        return machine

    def transition(self, target: RunState, reason: str) -> None:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"illegal state transition: {self.state} -> {target}")
        self.events.append(StateEvent(source=self.state, target=target, reason=reason))
        self.state = target

    def enforce_runtime(self) -> bool:
        """Fail closed after a completed step exceeds the run budget."""

        if self.elapsed_seconds <= self.budgets.max_runtime_seconds:
            return True
        if not self.terminal:
            self.events.append(
                StateEvent(
                    source=self.state,
                    target=RunState.FAILED_WITH_LIMITATIONS,
                    reason="maximum runtime exceeded",
                )
            )
            self.state = RunState.FAILED_WITH_LIMITATIONS
        return False

    @property
    def elapsed_seconds(self) -> float:
        return monotonic() - self.started_at

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def start_deliberation_round(self) -> bool:
        if self.deliberation_rounds >= self.budgets.max_deliberation_rounds:
            return False
        self.deliberation_rounds += 1
        return True

    def allow_tool_retry(self, tool_name: str) -> bool:
        retries = self.tool_retries.get(tool_name, 0)
        if retries >= self.budgets.max_tool_retries:
            return False
        self.tool_retries[tool_name] = retries + 1
        return True

    def snapshot(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "events": [
                {"source": event.source.value, "target": event.target.value, "reason": event.reason}
                for event in self.events
            ],
            "deliberation_rounds": self.deliberation_rounds,
            "tool_retries": dict(self.tool_retries),
            "elapsed_seconds": self.elapsed_seconds,
        }
