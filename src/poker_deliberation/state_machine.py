"""Explicit, bounded workflow state machine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from poker_deliberation.budgets import (
    BudgetFailureCode,
    BudgetLimitError,
    BudgetPolicyV2,
    BudgetSnapshot,
    MonotonicClock,
    SerialUsageLedger,
    SystemMonotonicClock,
    UsageDelta,
)
from poker_deliberation.config import BudgetConfig, migrate_budget_config


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
    budgets: BudgetConfig | BudgetPolicyV2
    state: RunState = RunState.INTAKE
    events: list[StateEvent] = field(default_factory=list)
    deliberation_rounds: int = 0
    tool_retries: dict[str, int] = field(default_factory=dict)
    clock: MonotonicClock = field(default_factory=SystemMonotonicClock)
    ledger: SerialUsageLedger = field(init=False)
    _tool_retry_limit: int = field(init=False)

    def __post_init__(self) -> None:
        legacy_retry_limit = (
            self.budgets.max_tool_retries if isinstance(self.budgets, BudgetConfig) else None
        )
        policy = (
            self.budgets
            if isinstance(self.budgets, BudgetPolicyV2)
            else migrate_budget_config(self.budgets).policy
        )
        self.budgets = policy
        self._tool_retry_limit = (
            legacy_retry_limit if legacy_retry_limit is not None else policy.max_tool_retries
        )
        self.ledger = SerialUsageLedger(policy, clock=self.clock)

    @classmethod
    def from_snapshot(
        cls,
        budgets: BudgetConfig | BudgetPolicyV2,
        snapshot: dict[str, object],
        *,
        clock: MonotonicClock | None = None,
    ) -> WorkflowStateMachine:
        machine = cls(
            budgets=budgets,
            state=RunState(str(snapshot["state"])),
            clock=clock or SystemMonotonicClock(),
        )
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
        elapsed_ns = int(float(str(snapshot.get("elapsed_seconds", 0.0))) * 1_000_000_000)
        policy = machine.budgets
        if not isinstance(policy, BudgetPolicyV2):  # pragma: no cover - narrowed in __post_init__
            raise TypeError("workflow budget policy was not resolved")
        machine.ledger = SerialUsageLedger(
            policy,
            clock=machine.clock,
            initial=BudgetSnapshot(
                policy_sha256=policy.canonical_sha256,
                active_runtime_ns=elapsed_ns,
            ),
        )
        return machine

    def transition(self, target: RunState, reason: str) -> None:
        if target not in ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"illegal state transition: {self.state} -> {target}")
        self.events.append(StateEvent(source=self.state, target=target, reason=reason))
        self.state = target

    def enforce_runtime(self) -> bool:
        """Fail closed after a completed step exceeds the run budget."""

        try:
            self.ledger.snapshot()
            return True
        except BudgetLimitError as exc:
            failure_reason = f"strict budget observation failed: {exc.failure.code}"
        if self.state is not RunState.FAILED_WITH_LIMITATIONS:
            self.events.append(
                StateEvent(
                    source=self.state,
                    target=RunState.FAILED_WITH_LIMITATIONS,
                    reason=failure_reason,
                )
            )
            self.state = RunState.FAILED_WITH_LIMITATIONS
        return False

    @property
    def elapsed_seconds(self) -> float:
        try:
            snapshot = self.ledger.snapshot()
            return snapshot.active_runtime_ns / 1_000_000_000
        except BudgetLimitError as exc:
            if (
                exc.failure.code is BudgetFailureCode.RUNTIME_EXCEEDED
                and exc.failure.observed is not None
            ):
                return exc.failure.observed / 1_000_000_000
            return self.ledger.settled_snapshot().active_runtime_ns / 1_000_000_000

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def start_deliberation_round(self) -> bool:
        if not isinstance(self.budgets, BudgetPolicyV2):
            raise TypeError("workflow budget policy was not resolved")
        if self.deliberation_rounds >= self.budgets.max_deliberation_rounds:
            return False
        self.deliberation_rounds += 1
        return True

    def allow_tool_retry(self, tool_name: str) -> bool:
        if not isinstance(self.budgets, BudgetPolicyV2):
            raise TypeError("workflow budget policy was not resolved")
        retries = self.tool_retries.get(tool_name, 0)
        if retries >= self._tool_retry_limit:
            return False
        self.tool_retries[tool_name] = retries + 1
        return True

    def apply_usage(self, delta: UsageDelta) -> BudgetSnapshot:
        return self.ledger.apply(delta)

    def usage_snapshot(self) -> BudgetSnapshot:
        return self.ledger.snapshot()

    def runtime_window(self) -> tuple[BudgetSnapshot, int, int]:
        return self.ledger.runtime_window()

    def pause_active_runtime(self) -> None:
        self.ledger.pause()

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
