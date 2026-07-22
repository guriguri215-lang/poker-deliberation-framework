"""Attempt-local and run-local serial usage accounting."""

from __future__ import annotations

from poker_deliberation.budgets.clock import MonotonicClock, SystemMonotonicClock
from poker_deliberation.budgets.contracts import (
    BudgetFailure,
    BudgetFailureCode,
    BudgetLimitError,
    BudgetPolicyV2,
    BudgetSnapshot,
    ExecutionClass,
    UsageDelta,
)


class SerialUsageLedger:
    """In-memory serial accounting; no reservation, manifest, CAS, or retry loop."""

    def __init__(
        self,
        policy: BudgetPolicyV2,
        *,
        clock: MonotonicClock | None = None,
        initial: BudgetSnapshot | None = None,
        active: bool = True,
    ) -> None:
        self.policy = BudgetPolicyV2.model_validate(policy.model_dump(mode="python"))
        self.clock = clock or SystemMonotonicClock()
        if initial is not None and initial.policy_sha256 != self.policy.canonical_sha256:
            raise ValueError("initial usage policy hash mismatch")
        self._snapshot = initial or BudgetSnapshot(policy_sha256=self.policy.canonical_sha256)
        self._active = active
        self._last_ns = self._read_clock()
        self._validate_snapshot(self._snapshot)

    def _read_clock(self) -> int:
        now = self.clock.now_ns()
        if isinstance(now, bool) or not isinstance(now, int):
            raise BudgetLimitError(
                BudgetFailure(
                    code=BudgetFailureCode.USAGE_MALFORMED,
                    resource="clock",
                    message="monotonic clock must return integer nanoseconds",
                )
            )
        if now < 0:
            raise BudgetLimitError(
                BudgetFailure(
                    code=BudgetFailureCode.USAGE_MALFORMED,
                    resource="clock",
                    message="monotonic clock returned a negative value",
                )
            )
        return now

    def _observe_runtime(self) -> None:
        now = self._read_clock()
        if now < self._last_ns:
            raise BudgetLimitError(
                BudgetFailure(
                    code=BudgetFailureCode.CLOCK_ROLLBACK,
                    resource="active_runtime_ns",
                    message="monotonic clock moved backwards",
                    observed=self._last_ns - now,
                )
            )
        elapsed = now - self._last_ns if self._active else 0
        self._last_ns = now
        if elapsed:
            self._snapshot = self._snapshot.apply(UsageDelta(active_runtime_ns=elapsed))
            self._validate_snapshot(self._snapshot)

    def _failure(
        self,
        code: BudgetFailureCode,
        resource: str,
        limit: int,
        observed: int,
    ) -> BudgetLimitError:
        return BudgetLimitError(
            BudgetFailure(
                code=code,
                resource=resource,
                message=f"{resource} exceeded its strict budget",
                limit=limit,
                observed=observed,
            )
        )

    def _validate_snapshot(self, snapshot: BudgetSnapshot) -> None:
        checks = (
            (
                snapshot.active_runtime_ns,
                self.policy.runtime_limit_ns,
                BudgetFailureCode.RUNTIME_EXCEEDED,
                "active_runtime_ns",
            ),
            (
                snapshot.external_cost_micro_usd,
                self.policy.max_external_cost_micro_usd,
                BudgetFailureCode.EXTERNAL_COST_EXCEEDED,
                "external_cost_micro_usd",
            ),
            (
                snapshot.provider_output_bytes,
                self.policy.max_provider_output_bytes,
                BudgetFailureCode.PROVIDER_OUTPUT_EXCEEDED,
                "provider_output_bytes",
            ),
            (
                snapshot.tool_input_bytes,
                self.policy.max_tool_input_bytes,
                BudgetFailureCode.TOOL_INPUT_EXCEEDED,
                "tool_input_bytes",
            ),
            (
                snapshot.tool_output_bytes,
                self.policy.max_tool_output_bytes,
                BudgetFailureCode.TOOL_OUTPUT_EXCEEDED,
                "tool_output_bytes",
            ),
            (
                snapshot.artifact_bytes,
                self.policy.max_artifact_bytes,
                BudgetFailureCode.ARTIFACT_EXCEEDED,
                "artifact_bytes",
            ),
            (
                snapshot.run_bytes,
                self.policy.max_run_bytes,
                BudgetFailureCode.RUN_EXCEEDED,
                "run_bytes",
            ),
            (
                snapshot.peak_concurrency,
                1,
                BudgetFailureCode.UNSUPPORTED_CONCURRENCY,
                "peak_concurrency",
            ),
        )
        for observed, limit, code, resource in checks:
            if observed > limit:
                raise self._failure(code, resource, limit, observed)

    def snapshot(self) -> BudgetSnapshot:
        self._observe_runtime()
        return self._snapshot

    def apply(self, delta: UsageDelta) -> BudgetSnapshot:
        self._observe_runtime()
        candidate = self._snapshot.apply(delta)
        self._validate_snapshot(candidate)
        self._snapshot = candidate
        return self._snapshot

    def preflight(self, delta: UsageDelta) -> BudgetSnapshot:
        self._observe_runtime()
        candidate = self._snapshot.apply(delta)
        self._validate_snapshot(candidate)
        return candidate

    def pause(self) -> BudgetSnapshot:
        self._observe_runtime()
        self._active = False
        return self._snapshot

    def resume(self) -> None:
        now = self._read_clock()
        if now < self._last_ns:
            raise BudgetLimitError(
                BudgetFailure(
                    code=BudgetFailureCode.CLOCK_ROLLBACK,
                    resource="active_runtime_ns",
                    message="monotonic clock moved backwards while paused",
                    observed=self._last_ns - now,
                )
            )
        self._last_ns = now
        self._active = True

    def begin_provider_attempt(
        self,
        execution_class: ExecutionClass,
        estimated_cost_micro_usd: int | None,
    ) -> BudgetSnapshot:
        if execution_class is ExecutionClass.UNKNOWN:
            raise BudgetLimitError(
                BudgetFailure(
                    code=BudgetFailureCode.EXTERNAL_EXECUTION_UNKNOWN,
                    resource="provider_execution_class",
                    message="provider execution class is unknown",
                )
            )
        cost = 0
        if execution_class is ExecutionClass.EXTERNAL:
            if self.policy.max_external_cost_micro_usd == 0:
                raise BudgetLimitError(
                    BudgetFailure(
                        code=BudgetFailureCode.EXTERNAL_COST_DISABLED,
                        resource="external_cost_micro_usd",
                        message="external calls are disabled by a zero cost cap",
                        limit=0,
                    )
                )
            if estimated_cost_micro_usd is None:
                raise BudgetLimitError(
                    BudgetFailure(
                        code=BudgetFailureCode.EXTERNAL_COST_UNKNOWN,
                        resource="external_cost_micro_usd",
                        message="external call cost is unknown",
                        limit=self.policy.max_external_cost_micro_usd,
                    )
                )
            if (
                isinstance(estimated_cost_micro_usd, bool)
                or not isinstance(estimated_cost_micro_usd, int)
                or estimated_cost_micro_usd < 0
            ):
                raise BudgetLimitError(
                    BudgetFailure(
                        code=BudgetFailureCode.USAGE_MALFORMED,
                        resource="external_cost_micro_usd",
                        message="external cost estimate must be a non-negative integer",
                    )
                )
            cost = estimated_cost_micro_usd
        return self.apply(
            UsageDelta(
                provider_attempts=1,
                external_cost_micro_usd=cost,
                peak_concurrency=1,
            )
        )
