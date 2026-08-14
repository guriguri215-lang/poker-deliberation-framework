"""Calculator registry that always emits auditable ToolResult objects."""

from __future__ import annotations

import json
import math
import pickle
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from multiprocessing import get_context
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from threading import get_ident
from typing import Any
from uuid import uuid4

from poker_deliberation.budgets import (
    BudgetFailure,
    BudgetFailureCode,
    BudgetLimitError,
    MonotonicClock,
    SystemMonotonicClock,
    canonical_json_utf8_size,
)
from poker_deliberation.schemas import (
    CanonicalHand,
    Exactness,
    NumericalErrorMetadata,
    NumericalExactness,
    ToolResult,
    ToolStatus,
    VerificationMetadata,
)
from poker_deliberation.security import redact_sensitive
from poker_deliberation.tools.best_response import best_response_to_fixed_strategy
from poker_deliberation.tools.combinations import combo_summary, parse_weighted_range
from poker_deliberation.tools.contracts import (
    VERSIONED_RANGE_BRIDGE_TOOL_NAMES,
    RangeValidateInput,
    ToolContract,
    contract_by_name,
    versioned_range_bridge_failure_error,
    versioned_range_bridge_failure_input_matches,
)
from poker_deliberation.tools.equity import holdem_equity
from poker_deliberation.tools.ev_tree import evaluate_ev_tree
from poker_deliberation.tools.hand_pot_ledger import calculate_hand_pot_ledger
from poker_deliberation.tools.hand_validator import validate_hand
from poker_deliberation.tools.icm import calculate_icm
from poker_deliberation.tools.matrix_game import solve_zero_sum_matrix
from poker_deliberation.tools.pot_odds import (
    break_even_fold_frequency,
    pot_odds,
    reconstruct_pot,
)
from poker_deliberation.tools.sensitivity import analyze_scenarios
from poker_deliberation.tools.solver_adapter import UnavailableSolverAdapter
from poker_deliberation.tools.strategy_math import (
    bayes_update,
    bluff_ev,
    effective_stack,
    minimum_defense_frequency,
    polar_river_bluff_fraction,
    rake_amount,
    raked_call_ev,
    stack_to_pot_ratio,
)

ToolFunction = Callable[[dict[str, Any]], dict[str, Any]]
_PHASE_PROCESS_JOIN_SECONDS = 1.0


def _phase_calculation_worker(
    connection: Connection,
    definition: ToolDefinition,
    payload: dict[str, Any],
    contract_version: str | None,
    bind_versioned_range_failure: bool,
    raise_on_byte_limit: bool,
    max_payload_bytes: int,
    max_output_bytes: int,
    max_duration_seconds: float,
    correlation_id: str,
) -> None:
    """Materialize one complete result inside the cancellable worker.

    The parent deliberately receives only a byte envelope.  Input/output schema
    validation, exactness resolution, floating verification, and ToolResult
    construction therefore remain inside the same hard-deadline boundary as the
    calculator itself.
    """

    try:
        registry = ToolRegistry(
            max_payload_bytes=max_payload_bytes,
            max_output_bytes=max_output_bytes,
            max_duration_seconds=max_duration_seconds,
            monotonic_clock=SystemMonotonicClock(),
        )
        registry.register(definition)
        try:
            result = registry._execute_impl(
                definition.name,
                payload,
                contract_version=contract_version,
                _bind_versioned_range_failure=bind_versioned_range_failure,
                _raise_on_byte_limit=raise_on_byte_limit,
            )
        except ToolByteLimitError as exc:
            header = {
                "correlation_id": correlation_id,
                "kind": "byte_limit",
                "resource": exc.resource,
                "limit": exc.limit,
                "observed": exc.observed,
            }
            body = b""
        else:
            header = {"correlation_id": correlation_id, "kind": "result"}
            body = result.model_dump_json().encode("utf-8")
        header_bytes = json.dumps(
            header,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        connection.send_bytes(header_bytes + b"\n" + body)
    except BaseException as exc:
        header = {
            "correlation_id": correlation_id,
            "kind": "worker_failure",
            "error_type": type(exc).__name__,
            "message": str(exc)[:2048],
        }
        with suppress(BaseException):
            connection.send_bytes(
                json.dumps(
                    header,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("ascii")
                + b"\n"
            )
    finally:
        connection.close()


def _stop_phase_process(process: BaseProcess) -> bool:
    """Terminate one worker and confirm that the direct child has stopped."""

    if not process.is_alive():
        process.join(timeout=_PHASE_PROCESS_JOIN_SECONDS)
        return not process.is_alive()
    process.terminate()
    process.join(timeout=_PHASE_PROCESS_JOIN_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=_PHASE_PROCESS_JOIN_SECONDS)
    return not process.is_alive()


def _failure_error(tool_name: str, diagnostic: str, *, bind_versioned_range: bool) -> str:
    if not bind_versioned_range or tool_name not in VERSIONED_RANGE_BRIDGE_TOOL_NAMES:
        return diagnostic
    return versioned_range_bridge_failure_error(tool_name)


class ToolByteLimitError(RuntimeError):
    """Typed internal signal used by the phase boundary without changing public results."""

    def __init__(self, resource: str, *, limit: int, observed: int) -> None:
        super().__init__(f"{resource} exceeds hard limit {limit} bytes")
        self.resource = resource
        self.limit = limit
        self.observed = observed


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    purpose: str
    exact_or_approximate: str
    supported_games: tuple[str, ...]
    function: ToolFunction
    assumptions: tuple[str, ...] = ()
    version: str = "1.0.0"
    contract: ToolContract | None = None
    phase_isolated: bool = False


class ToolRegistry:
    def __init__(
        self,
        *,
        max_payload_bytes: int = 1_000_000,
        max_output_bytes: int = 1_000_000,
        max_duration_seconds: float = 30.0,
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        self._tools: dict[str, ToolDefinition] = {}
        self.max_payload_bytes = max_payload_bytes
        self.max_output_bytes = max_output_bytes
        if isinstance(max_duration_seconds, bool) or not isinstance(
            max_duration_seconds, (int, float)
        ):
            raise TypeError("max_duration_seconds must be numeric")
        if not math.isfinite(float(max_duration_seconds)) or max_duration_seconds <= 0:
            raise ValueError("max_duration_seconds must be finite and positive")
        self.max_duration_seconds = max_duration_seconds
        self.monotonic_clock = monotonic_clock or SystemMonotonicClock()
        # A transient failure cannot be reproduced reliably with a second,
        # longer execution.  Keep only the immediately preceding phase
        # failure so its caller can consume one narrow in-process authority.
        # Concurrent or delayed use fails closed because a later phase call
        # replaces this slot.
        self._fresh_phase_failure: tuple[int, str, bytes, bytes] | None = None
        # A successful phase call has already completed the registered runtime
        # boundary and ToolResult construction.  Retain its exact result once so
        # the serial phase adapter can consume that same execution, including a
        # non-isolated custom tool that has no canonical replay implementation.
        # Serialized/storage callers never opt into this authority and must
        # still receive a full canonical replay (or reject a custom result).
        self._fresh_phase_success: tuple[int, str, bytes, bytes] | None = None

    def _read_clock(self) -> int:
        value = self.monotonic_clock.now_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("monotonic clock must return non-negative integer nanoseconds")
        return value

    def _duration_seconds(self, started_ns: int) -> float:
        completed_ns = self._read_clock()
        if completed_ns < started_ns:
            raise ValueError("monotonic clock moved backwards during tool execution")
        return (completed_ns - started_ns) / 1_000_000_000

    def _failure_duration_seconds(self, started_ns: int) -> float:
        try:
            return self._duration_seconds(started_ns)
        except (ValueError, TypeError):
            return 0.0

    def _read_phase_clock(
        self,
        *,
        not_before_ns: int,
        budget_observed_at_ns: int,
        run_deadline_ns: int,
        runtime_limit_ns: int,
        active_runtime_ns: int,
        observation_sink: list[int] | None,
    ) -> int:
        try:
            value = self._read_clock()
        except Exception as exc:
            raise BudgetLimitError(
                BudgetFailure(
                    code=BudgetFailureCode.USAGE_MALFORMED,
                    resource="clock",
                    message=f"monotonic clock read failed: {type(exc).__name__}",
                )
            ) from exc
        if observation_sink is not None:
            observation_sink.append(value)
        if value < not_before_ns:
            raise BudgetLimitError(
                BudgetFailure(
                    code=BudgetFailureCode.CLOCK_ROLLBACK,
                    resource="active_runtime_ns",
                    message="monotonic clock moved backwards before or during tool execution",
                    observed=not_before_ns - value,
                )
            )
        if value >= run_deadline_ns:
            raise BudgetLimitError(
                BudgetFailure(
                    code=BudgetFailureCode.RUNTIME_EXCEEDED,
                    resource="active_runtime_ns",
                    message="active runtime expired before or during tool execution",
                    limit=runtime_limit_ns,
                    observed=active_runtime_ns + value - budget_observed_at_ns,
                )
            )
        return value

    def register(self, definition: ToolDefinition) -> None:
        if definition.name in self._tools:
            raise ValueError(f"duplicate tool name: {definition.name}")
        if definition.contract is not None and definition.contract.name != definition.name:
            raise ValueError("tool definition name must match its typed contract")
        self._tools[definition.name] = definition

    @staticmethod
    def _materialized_result_projection(result: ToolResult) -> dict[str, Any]:
        projection = result.model_dump(mode="json")
        for field_name in ("created_at", "duration_seconds", "result_id"):
            projection.pop(field_name, None)
        return projection

    @classmethod
    def _materialized_result_projection_bytes(cls, result: ToolResult) -> bytes:
        return json.dumps(
            cls._materialized_result_projection(result),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")

    @staticmethod
    def _immutable_result_snapshot(result: ToolResult) -> tuple[str, bytes, bytes]:
        def exact_bytes(value: ToolResult) -> bytes:
            return json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("ascii")

        redacted = ToolResult.model_validate(redact_sensitive(result, enabled=True))
        return result.result_id, exact_bytes(result), exact_bytes(redacted)

    @staticmethod
    def _validate_canonical_failure_envelope(
        result: ToolResult,
        *,
        contract: ToolContract,
    ) -> None:
        expected_command = (
            f"poker-deliberate calculate {result.tool_name} --analysis-scope retrospective "
            "--input <input.json>"
        )
        failure_only_metadata = (
            result.output,
            result.method,
            result.stochastic,
            result.seed,
            result.samples,
            result.iterations,
            result.confidence_interval,
            result.confidence_level,
            result.error_metadata,
            result.stopping_condition,
            result.verification,
        )
        if (
            result.status is not ToolStatus.FAILED
            or not result.error
            or any(value not in (None, {}) for value in failure_only_metadata)
            or result.exactness is not Exactness.UNAVAILABLE
            or result.numeric_exactness is not NumericalExactness.UNAVAILABLE
            or result.contract_version != contract.contract_version
            or result.version != contract.version
            or tuple(result.assumptions) != contract.assumptions
            or result.model_qualifier is not None
            or result.warnings
            or result.reproduce_command != expected_command
        ):
            raise ValueError("materialized failure envelope is not canonical")

    def _consume_fresh_phase_failure(self, result: ToolResult) -> None:
        fresh = self._fresh_phase_failure
        self._fresh_phase_failure = None
        if fresh is None:
            raise ValueError("failure lacks immediate execution authority")
        owner_thread_id, result_id, original, redacted = fresh
        if owner_thread_id != get_ident():
            raise ValueError("fresh failure belongs to another thread")
        if result_id != result.result_id:
            raise ValueError("failure lacks immediate execution authority")
        _candidate_id, candidate, _candidate_redacted = self._immutable_result_snapshot(result)
        if candidate not in (original, redacted):
            raise ValueError("failure differs from the immediately executed result")

    def _consume_fresh_phase_success(self, result: ToolResult) -> bool:
        fresh = self._fresh_phase_success
        self._fresh_phase_success = None
        if fresh is None:
            return False
        if result.status is not ToolStatus.SUCCESS:
            raise ValueError("fresh phase success authority requires a successful result")
        owner_thread_id, result_id, original, redacted = fresh
        if owner_thread_id != get_ident():
            raise ValueError("fresh phase success belongs to another thread")
        if result_id != result.result_id:
            raise ValueError("fresh phase success result ID changed")
        _candidate_id, candidate, _candidate_redacted = self._immutable_result_snapshot(result)
        if candidate not in (original, redacted):
            raise ValueError("result differs from the immediately executed phase result")
        return True

    def reverify_materialized_result(
        self,
        result: ToolResult,
        *,
        authoritative_budget_failure: BudgetFailure | None = None,
        allow_fresh_execution_failure: bool = False,
        allow_fresh_phase_success: bool = False,
    ) -> None:
        """Verify a result using canonical replay or narrow immediate authority."""

        if allow_fresh_phase_success and self._consume_fresh_phase_success(result):
            return

        canonical_function = _CANONICAL_PHASE_FUNCTIONS.get(result.tool_name)
        contract = contract_by_name().get(result.tool_name)
        if canonical_function is None or contract is None:
            raise ValueError("materialized result has no canonical replay calculator")
        canonical_registry = default_registry(
            max_payload_bytes=self.max_payload_bytes,
            max_output_bytes=self.max_output_bytes,
            max_duration_seconds=max(self.max_duration_seconds, 30.0),
        )
        definition = canonical_registry._tools.get(result.tool_name)
        if (
            definition is None
            or definition.function is not canonical_function
            or definition.contract != contract
            or definition.version != contract.version
            or definition.assumptions != contract.assumptions
            or not definition.phase_isolated
        ):
            raise ValueError("materialized result has no canonical replay contract")
        if result.status is ToolStatus.FAILED and (result.error or "").startswith(
            "strict budget failure: "
        ):
            if authoritative_budget_failure is None:
                raise ValueError("budget failure requires independent storage authority")
            try:
                failure_code = BudgetFailureCode(
                    (result.error or "").removeprefix("strict budget failure: ")
                )
            except ValueError:
                raise ValueError("materialized budget failure has an unknown code") from None
            if authoritative_budget_failure.code is not failure_code:
                raise ValueError("materialized budget failure differs from its authority")
            if (
                result.output
                or result.exactness is not Exactness.UNAVAILABLE
                or result.numeric_exactness is not NumericalExactness.UNAVAILABLE
                or result.contract_version != contract.contract_version
                or result.assumptions
                or result.version != "1.0.0"
                or result.model_qualifier is not None
                or result.method is not None
                or result.stochastic is not None
                or result.seed is not None
                or result.samples is not None
                or result.iterations is not None
                or result.confidence_interval is not None
                or result.confidence_level is not None
                or result.error_metadata is not None
                or result.stopping_condition is not None
                or result.verification is not None
                or result.warnings
                or result.reproduce_command is not None
            ):
                raise ValueError("materialized budget failure envelope is not canonical")
            return
        expected_command = (
            f"poker-deliberate calculate {result.tool_name} --analysis-scope retrospective "
            "--input <input.json>"
        )
        if (
            result.contract_version != contract.contract_version
            or result.version != contract.version
            or tuple(result.assumptions) != contract.assumptions
            or result.model_qualifier
            != (None if result.status is ToolStatus.FAILED else contract.model_qualifier)
            or result.reproduce_command != expected_command
        ):
            raise ValueError("materialized result metadata differs from canonical contract")
        if result.status is ToolStatus.FAILED and allow_fresh_execution_failure:
            self._validate_canonical_failure_envelope(result, contract=contract)
            self._consume_fresh_phase_failure(result)
            return
        replay = canonical_registry.execute_for_phase(
            result.tool_name,
            result.input,
            contract_version=result.contract_version,
        )
        replay_projection = self._materialized_result_projection(replay)
        result_projection = self._materialized_result_projection(result)
        replay_projection_bytes = self._materialized_result_projection_bytes(replay)
        result_projection_bytes = self._materialized_result_projection_bytes(result)
        if replay_projection_bytes != result_projection_bytes:
            redacted_replay = ToolResult.model_validate(redact_sensitive(replay, enabled=True))
            redacted_projection_bytes = self._materialized_result_projection_bytes(redacted_replay)
            if redacted_projection_bytes == result_projection_bytes:
                return
            differing = sorted(
                key
                for key in replay_projection.keys() | result_projection.keys()
                if replay_projection.get(key) != result_projection.get(key)
            )
            if differing == ["verification"]:
                raise ValueError(
                    "materialized tool verification metadata differs from canonical replay"
                )
            if not differing:
                differing = ["canonical JSON representation"]
            raise ValueError(
                "materialized tool result differs from canonical replay: " + ", ".join(differing)
            )

    def names(self) -> list[str]:
        return sorted(self._tools)

    def describe(self) -> list[dict[str, object]]:
        descriptions: list[dict[str, object]] = []
        for tool in sorted(self._tools.values(), key=lambda item: item.name):
            if tool.contract is not None:
                descriptions.append(tool.contract.manifest_entry())
                continue
            descriptions.append(
                {
                    "name": tool.name,
                    "purpose": tool.purpose,
                    "exact_or_approximate": tool.exact_or_approximate,
                    "supported_games": list(tool.supported_games),
                    "assumptions": list(tool.assumptions),
                    "version": tool.version,
                }
            )
        return descriptions

    def runtime_identity_snapshot(
        self,
    ) -> tuple[tuple[str, ToolDefinition, ToolFunction, ToolContract | None], ...]:
        """Return in-process identities that the serial runtime actually invokes."""

        return tuple(
            (name, definition, definition.function, definition.contract)
            for name, definition in sorted(self._tools.items())
        )

    def execute(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
        _bind_versioned_range_failure: bool = False,
        _raise_on_byte_limit: bool = False,
        _budget_observed_at_ns: int | None = None,
        _run_deadline_ns: int | None = None,
        _runtime_limit_ns: int | None = None,
        _active_runtime_ns: int | None = None,
        _runtime_not_before_ns: int | None = None,
        _observation_sink: list[int] | None = None,
    ) -> ToolResult:
        """Execute a calculator, hard-isolating every qualified canonical tool."""

        definition = self._tools.get(name)
        if definition is not None and definition.phase_isolated:
            return self._execute_isolated(
                name,
                payload,
                contract_version=contract_version,
                budget_observed_at_ns=_budget_observed_at_ns,
                run_deadline_ns=_run_deadline_ns,
                runtime_limit_ns=_runtime_limit_ns,
                active_runtime_ns=_active_runtime_ns,
                runtime_not_before_ns=_runtime_not_before_ns,
                observation_sink=_observation_sink,
                _bind_versioned_range_failure=_bind_versioned_range_failure,
                _raise_on_byte_limit=_raise_on_byte_limit,
            )
        return self._execute_impl(
            name,
            payload,
            contract_version=contract_version,
            _bind_versioned_range_failure=_bind_versioned_range_failure,
            _raise_on_byte_limit=_raise_on_byte_limit,
            _budget_observed_at_ns=_budget_observed_at_ns,
            _run_deadline_ns=_run_deadline_ns,
            _runtime_limit_ns=_runtime_limit_ns,
            _active_runtime_ns=_active_runtime_ns,
            _runtime_not_before_ns=_runtime_not_before_ns,
            _observation_sink=_observation_sink,
        )

    def _execute_impl(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
        _bind_versioned_range_failure: bool = False,
        _raise_on_byte_limit: bool = False,
        _budget_observed_at_ns: int | None = None,
        _run_deadline_ns: int | None = None,
        _runtime_limit_ns: int | None = None,
        _active_runtime_ns: int | None = None,
        _runtime_not_before_ns: int | None = None,
        _observation_sink: list[int] | None = None,
    ) -> ToolResult:
        runtime_values = (
            _budget_observed_at_ns,
            _run_deadline_ns,
            _runtime_limit_ns,
            _active_runtime_ns,
            _runtime_not_before_ns,
        )
        if any(item is not None for item in runtime_values) and any(
            item is None for item in runtime_values
        ):
            raise ValueError("tool runtime boundary values must be provided together")

        def read_phase_clock(not_before_ns: int) -> int:
            if (
                _budget_observed_at_ns is None
                or _run_deadline_ns is None
                or _runtime_limit_ns is None
                or _active_runtime_ns is None
            ):
                raise ValueError("phase clock requires a complete runtime boundary")
            return self._read_phase_clock(
                not_before_ns=not_before_ns,
                budget_observed_at_ns=_budget_observed_at_ns,
                run_deadline_ns=_run_deadline_ns,
                runtime_limit_ns=_runtime_limit_ns,
                active_runtime_ns=_active_runtime_ns,
                observation_sink=_observation_sink,
            )

        known_definition = self._tools.get(name)
        known_contract = known_definition.contract if known_definition is not None else None
        payload_size = canonical_json_utf8_size(payload)
        _bind_versioned_range_failure = (
            _bind_versioned_range_failure
            and payload_size <= self.max_payload_bytes
            and known_contract is not None
            and contract_version == known_contract.contract_version
            and versioned_range_bridge_failure_input_matches(
                name,
                payload,
                contract_version,
            )
        )
        if payload_size > self.max_payload_bytes:
            if _raise_on_byte_limit:
                raise ToolByteLimitError(
                    "tool_input_bytes",
                    limit=self.max_payload_bytes,
                    observed=payload_size,
                )
            return ToolResult(
                tool_name=name,
                input={},
                status=ToolStatus.FAILED,
                exactness=Exactness.UNAVAILABLE,
                numeric_exactness=NumericalExactness.UNAVAILABLE,
                contract_version=(known_contract.contract_version if known_contract else "1.0.0"),
                error=_failure_error(
                    name,
                    f"tool input exceeds hard limit {self.max_payload_bytes} bytes",
                    bind_versioned_range=_bind_versioned_range_failure,
                ),
            )
        if known_definition is None:
            return ToolResult(
                tool_name=name,
                input=payload,
                status=ToolStatus.UNAVAILABLE,
                exactness=Exactness.UNAVAILABLE,
                numeric_exactness=NumericalExactness.UNAVAILABLE,
                error=f"unknown tool: {name}",
                reproduce_command=None,
            )
        definition = known_definition
        started = 0
        try:
            started = (
                read_phase_clock(_runtime_not_before_ns or 0)
                if _run_deadline_ns is not None
                else self._read_clock()
            )
            contract = definition.contract
            if (
                contract is not None
                and contract_version is not None
                and contract_version != contract.contract_version
            ):
                raise ValueError(
                    f"contract version mismatch: requested {contract_version}, "
                    f"supported {contract.contract_version}"
                )

            normalized_payload = payload
            if contract is not None:
                validated_input = contract.input_model.model_validate(payload)
                normalized_payload = validated_input.model_dump(mode="python", exclude_unset=True)
            effect_started_ns = (
                read_phase_clock(max(started, _runtime_not_before_ns or 0))
                if _run_deadline_ns is not None
                else started
            )
            output = definition.function(normalized_payload)
            if not isinstance(output, dict):
                raise TypeError("tool function must return a dictionary")
            if contract is not None:
                contract.output_model.model_validate(output)
            if _run_deadline_ns is not None:
                effect_completed_ns = read_phase_clock(effect_started_ns)
                duration = (effect_completed_ns - started) / 1_000_000_000
            else:
                effect_completed_ns = started
                duration = self._duration_seconds(started)
            output_size = canonical_json_utf8_size(output)
            if output_size > self.max_output_bytes:
                if _raise_on_byte_limit:
                    raise ToolByteLimitError(
                        "tool_output_bytes",
                        limit=self.max_output_bytes,
                        observed=output_size,
                    )
                return ToolResult(
                    tool_name=name,
                    input=payload,
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    numeric_exactness=NumericalExactness.UNAVAILABLE,
                    contract_version=(
                        definition.contract.contract_version
                        if definition.contract is not None
                        else "1.0.0"
                    ),
                    assumptions=list(definition.assumptions),
                    version=definition.version,
                    duration_seconds=duration,
                    error=_failure_error(
                        name,
                        f"tool output exceeds hard limit {self.max_output_bytes} bytes",
                        bind_versioned_range=_bind_versioned_range_failure,
                    ),
                    reproduce_command=(
                        f"poker-deliberate calculate {name} --analysis-scope retrospective "
                        "--input <input.json>"
                    ),
                )
            unavailable = bool(output.get("unavailable", False))
            numeric_exactness = (
                NumericalExactness.UNAVAILABLE
                if unavailable
                else (
                    contract.resolve_numeric_exactness(output)
                    if contract is not None
                    else _legacy_numeric_exactness(output, definition.exact_or_approximate)
                )
            )
            exactness = _legacy_exactness_projection(numeric_exactness)
            warnings = _extract_warnings(output)
            if numeric_exactness in {
                NumericalExactness.EXACT_UNDER_MODEL,
                NumericalExactness.FLOATING_VERIFIED,
            }:
                warnings.append(
                    "legacy exactness='exact' is only a compatibility projection; "
                    f"use numeric_exactness='{numeric_exactness.value}'"
                )
            status = ToolStatus.UNAVAILABLE if unavailable else ToolStatus.SUCCESS
            error = str(output.get("error")) if unavailable and output.get("error") else None
            confidence_interval = output.get("confidence_interval_95")
            approximate_metadata = _approximate_metadata(name, output, numeric_exactness)
            verification = _verification_metadata(
                contract,
                numeric_exactness,
                normalized_payload,
                output,
            )
            if _run_deadline_ns is not None:
                verified_ns = read_phase_clock(effect_completed_ns)
                duration = (verified_ns - started) / 1_000_000_000
            else:
                duration = self._duration_seconds(started)
            if duration > self.max_duration_seconds:
                return ToolResult(
                    tool_name=name,
                    input=payload,
                    status=ToolStatus.FAILED,
                    exactness=Exactness.UNAVAILABLE,
                    numeric_exactness=NumericalExactness.UNAVAILABLE,
                    contract_version=(
                        definition.contract.contract_version
                        if definition.contract is not None
                        else "1.0.0"
                    ),
                    assumptions=list(definition.assumptions),
                    version=definition.version,
                    duration_seconds=duration,
                    error=_failure_error(
                        name,
                        (
                            "tool plus verification exceeded post-execution runtime limit "
                            f"{self.max_duration_seconds} seconds"
                        ),
                        bind_versioned_range=_bind_versioned_range_failure,
                    ),
                    reproduce_command=(
                        f"poker-deliberate calculate {name} --analysis-scope retrospective "
                        "--input <input.json>"
                    ),
                )
            return ToolResult(
                tool_name=name,
                input=payload,
                output=output,
                status=status,
                exactness=exactness,
                numeric_exactness=numeric_exactness,
                contract_version=contract.contract_version if contract is not None else "1.0.0",
                assumptions=list(definition.assumptions),
                version=definition.version,
                model_qualifier=contract.model_qualifier if contract is not None else None,
                method=str(output["method"]) if output.get("method") is not None else None,
                stochastic=approximate_metadata.get("stochastic"),
                seed=int(output["seed"]) if output.get("seed") is not None else None,
                samples=int(output["samples"]) if output.get("samples") is not None else None,
                iterations=(
                    int(output["iterations"]) if output.get("iterations") is not None else None
                ),
                confidence_interval=(
                    (float(confidence_interval[0]), float(confidence_interval[1]))
                    if isinstance(confidence_interval, list) and len(confidence_interval) == 2
                    else None
                ),
                confidence_level=approximate_metadata.get("confidence_level"),
                error_metadata=approximate_metadata.get("error_metadata"),
                stopping_condition=approximate_metadata.get("stopping_condition"),
                verification=verification,
                duration_seconds=duration,
                warnings=warnings,
                error=error,
                reproduce_command=(
                    f"poker-deliberate calculate {name} --analysis-scope retrospective "
                    "--input <input.json>"
                ),
            )
        except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
            return ToolResult(
                tool_name=name,
                input=payload,
                status=ToolStatus.FAILED,
                exactness=Exactness.UNAVAILABLE,
                numeric_exactness=NumericalExactness.UNAVAILABLE,
                contract_version=(
                    definition.contract.contract_version
                    if definition.contract is not None
                    else "1.0.0"
                ),
                assumptions=list(definition.assumptions),
                version=definition.version,
                duration_seconds=self._failure_duration_seconds(started),
                # Calculator/schema diagnostics are material facts about the
                # request, not transient boundary failures.  Never replace
                # them with the opaque versioned-range failure binding.
                error=f"{type(exc).__name__}: {exc}",
                reproduce_command=(
                    f"poker-deliberate calculate {name} --analysis-scope retrospective "
                    "--input <input.json>"
                ),
            )

    def execute_for_phase(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None = None,
        budget_observed_at_ns: int | None = None,
        run_deadline_ns: int | None = None,
        runtime_limit_ns: int | None = None,
        active_runtime_ns: int | None = None,
        runtime_not_before_ns: int | None = None,
        observation_sink: list[int] | None = None,
        _bind_versioned_range_failure: bool = False,
    ) -> ToolResult:
        """Execute a phase tool using the same hard boundary as public execution."""

        self._fresh_phase_failure = None
        self._fresh_phase_success = None
        definition = self._tools.get(name)
        if definition is None or not definition.phase_isolated:
            result = self._execute_impl(
                name,
                payload,
                contract_version=contract_version,
                _bind_versioned_range_failure=_bind_versioned_range_failure,
                _raise_on_byte_limit=True,
                _budget_observed_at_ns=budget_observed_at_ns,
                _run_deadline_ns=run_deadline_ns,
                _runtime_limit_ns=runtime_limit_ns,
                _active_runtime_ns=active_runtime_ns,
                _runtime_not_before_ns=runtime_not_before_ns,
                _observation_sink=observation_sink,
            )
        else:
            result = self._execute_isolated(
                name,
                payload,
                contract_version=contract_version,
                budget_observed_at_ns=budget_observed_at_ns,
                run_deadline_ns=run_deadline_ns,
                runtime_limit_ns=runtime_limit_ns,
                active_runtime_ns=active_runtime_ns,
                runtime_not_before_ns=runtime_not_before_ns,
                observation_sink=observation_sink,
                _bind_versioned_range_failure=_bind_versioned_range_failure,
                _raise_on_byte_limit=True,
            )
        if definition is not None and result.status is ToolStatus.SUCCESS:
            self._fresh_phase_success = (
                get_ident(),
                *self._immutable_result_snapshot(result),
            )
        if result.status is ToolStatus.FAILED:
            self._fresh_phase_failure = (
                get_ident(),
                *self._immutable_result_snapshot(result),
            )
        return result

    def _isolated_failure_result(
        self,
        name: str,
        payload: dict[str, Any],
        diagnostic: str,
        *,
        bind_versioned_range_failure: bool,
    ) -> ToolResult:
        definition = self._tools.get(name)
        contract = definition.contract if definition is not None else None
        return ToolResult(
            tool_name=name,
            input=payload,
            status=ToolStatus.FAILED,
            exactness=Exactness.UNAVAILABLE,
            numeric_exactness=NumericalExactness.UNAVAILABLE,
            contract_version=contract.contract_version if contract is not None else "1.0.0",
            assumptions=list(definition.assumptions) if definition is not None else [],
            version=definition.version if definition is not None else "1.0.0",
            error=_failure_error(
                name,
                diagnostic,
                bind_versioned_range=bind_versioned_range_failure,
            ),
            reproduce_command=(
                f"poker-deliberate calculate {name} --analysis-scope retrospective "
                "--input <input.json>"
                if definition is not None
                else None
            ),
        )

    def _execute_isolated(
        self,
        name: str,
        payload: dict[str, Any],
        *,
        contract_version: str | None,
        budget_observed_at_ns: int | None,
        run_deadline_ns: int | None,
        runtime_limit_ns: int | None,
        active_runtime_ns: int | None,
        runtime_not_before_ns: int | None,
        observation_sink: list[int] | None,
        _bind_versioned_range_failure: bool,
        _raise_on_byte_limit: bool,
    ) -> ToolResult:
        """Run validation, calculation, verification, and materialization in one child."""

        definition = self._tools.get(name)
        if definition is None or not definition.phase_isolated:
            raise RuntimeError("isolated execution requires a registered isolated definition")
        canonical_function = _CANONICAL_PHASE_FUNCTIONS.get(name)
        if canonical_function is not None and definition.function is not canonical_function:
            return self._isolated_failure_result(
                name,
                payload,
                "phase-isolated callable differs from canonical calculator",
                bind_versioned_range_failure=_bind_versioned_range_failure,
            )

        payload_size = canonical_json_utf8_size(payload)
        if payload_size > self.max_payload_bytes:
            if _raise_on_byte_limit:
                raise ToolByteLimitError(
                    "tool_input_bytes",
                    limit=self.max_payload_bytes,
                    observed=payload_size,
                )
            return ToolResult(
                tool_name=name,
                input={},
                status=ToolStatus.FAILED,
                exactness=Exactness.UNAVAILABLE,
                numeric_exactness=NumericalExactness.UNAVAILABLE,
                contract_version=(
                    definition.contract.contract_version
                    if definition.contract is not None
                    else "1.0.0"
                ),
                error=_failure_error(
                    name,
                    f"tool input exceeds hard limit {self.max_payload_bytes} bytes",
                    bind_versioned_range=_bind_versioned_range_failure,
                ),
            )

        try:
            pickle.dumps((definition, payload, contract_version))
        except Exception as exc:
            return self._isolated_failure_result(
                name,
                payload,
                f"phase-isolated definition is not spawn-picklable: {type(exc).__name__}",
                bind_versioned_range_failure=_bind_versioned_range_failure,
            )

        runtime_values = (
            budget_observed_at_ns,
            run_deadline_ns,
            runtime_limit_ns,
            active_runtime_ns,
            runtime_not_before_ns,
        )
        if any(item is not None for item in runtime_values) and any(
            item is None for item in runtime_values
        ):
            raise ValueError("tool runtime boundary values must be provided together")

        phase_timeout = False
        if run_deadline_ns is not None:
            if (
                budget_observed_at_ns is None
                or runtime_limit_ns is None
                or active_runtime_ns is None
                or runtime_not_before_ns is None
            ):
                raise ValueError("tool runtime boundary values must be provided together")
            started_ns = self._read_phase_clock(
                not_before_ns=runtime_not_before_ns,
                budget_observed_at_ns=budget_observed_at_ns,
                run_deadline_ns=run_deadline_ns,
                runtime_limit_ns=runtime_limit_ns,
                active_runtime_ns=active_runtime_ns,
                observation_sink=observation_sink,
            )
            effect_started_ns = self._read_phase_clock(
                not_before_ns=max(started_ns, runtime_not_before_ns),
                budget_observed_at_ns=budget_observed_at_ns,
                run_deadline_ns=run_deadline_ns,
                runtime_limit_ns=runtime_limit_ns,
                active_runtime_ns=active_runtime_ns,
                observation_sink=observation_sink,
            )
            remaining_seconds = (run_deadline_ns - effect_started_ns) / 1_000_000_000
            timeout_seconds = min(float(self.max_duration_seconds), remaining_seconds)
            phase_timeout = remaining_seconds <= float(self.max_duration_seconds)
        else:
            started_ns = self._read_clock()
            timeout_seconds = float(self.max_duration_seconds)

        context = get_context("spawn")
        parent_connection, child_connection = context.Pipe(duplex=False)
        correlation_id = uuid4().hex
        process = context.Process(
            target=_phase_calculation_worker,
            args=(
                child_connection,
                definition,
                payload,
                contract_version,
                _bind_versioned_range_failure,
                _raise_on_byte_limit,
                self.max_payload_bytes,
                self.max_output_bytes,
                self.max_duration_seconds,
                correlation_id,
            ),
            daemon=True,
        )
        wall_deadline = time.monotonic() + timeout_seconds
        try:
            try:
                process.start()
            except Exception as exc:
                return self._isolated_failure_result(
                    name,
                    payload,
                    f"phase-isolated worker failed to start: {type(exc).__name__}",
                    bind_versioned_range_failure=_bind_versioned_range_failure,
                )
            finally:
                child_connection.close()

            poll_timeout = max(0.0, wall_deadline - time.monotonic())
            result_ready = poll_timeout > 0.0 and parent_connection.poll(poll_timeout)
            if not result_ready or time.monotonic() >= wall_deadline:
                stopped = _stop_phase_process(process)
                if not stopped:
                    raise RuntimeError("phase-isolated worker could not be confirmed stopped")
                if phase_timeout:
                    if (
                        budget_observed_at_ns is None
                        or run_deadline_ns is None
                        or runtime_limit_ns is None
                        or active_runtime_ns is None
                    ):
                        raise ValueError("phase timeout lacks a complete runtime boundary")
                    raise BudgetLimitError(
                        BudgetFailure(
                            code=BudgetFailureCode.RUNTIME_EXCEEDED,
                            resource="active_runtime_ns",
                            message="active runtime expired during isolated tool execution",
                            limit=runtime_limit_ns,
                            observed=(active_runtime_ns + run_deadline_ns - budget_observed_at_ns),
                        )
                    )
                return self._isolated_failure_result(
                    name,
                    payload,
                    "isolated tool execution exceeded hard runtime limit "
                    f"{self.max_duration_seconds} seconds",
                    bind_versioned_range_failure=_bind_versioned_range_failure,
                )

            receive_failure: str | None
            try:
                envelope_bytes = parent_connection.recv_bytes(
                    self.max_payload_bytes + self.max_output_bytes + 262_144
                )
            except (EOFError, OSError) as exc:
                envelope_bytes = b""
                receive_failure = type(exc).__name__
            else:
                receive_failure = None
            process.join(timeout=_PHASE_PROCESS_JOIN_SECONDS)
            if process.is_alive():
                stopped = _stop_phase_process(process)
                if not stopped:
                    raise RuntimeError("phase-isolated worker could not be confirmed stopped")
                receive_failure = "worker did not exit after result"
            elif process.exitcode != 0:
                receive_failure = f"worker exited with code {process.exitcode}"
            if receive_failure is not None:
                return self._isolated_failure_result(
                    name,
                    payload,
                    f"isolated worker returned no readable result: {receive_failure}",
                    bind_versioned_range_failure=_bind_versioned_range_failure,
                )

            if run_deadline_ns is not None:
                if (
                    budget_observed_at_ns is None
                    or runtime_limit_ns is None
                    or active_runtime_ns is None
                ):
                    raise ValueError("tool runtime boundary values must be provided together")
                self._read_phase_clock(
                    not_before_ns=effect_started_ns,
                    budget_observed_at_ns=budget_observed_at_ns,
                    run_deadline_ns=run_deadline_ns,
                    runtime_limit_ns=runtime_limit_ns,
                    active_runtime_ns=active_runtime_ns,
                    observation_sink=observation_sink,
                )

            try:
                header_bytes, body = envelope_bytes.split(b"\n", 1)
                header = json.loads(header_bytes)
            except (ValueError, TypeError, json.JSONDecodeError):
                header = None
                body = b""
            if not isinstance(header, dict) or header.get("correlation_id") != correlation_id:
                return self._isolated_failure_result(
                    name,
                    payload,
                    "isolated worker returned a malformed result envelope",
                    bind_versioned_range_failure=_bind_versioned_range_failure,
                )
            kind = header.get("kind")
            if kind == "byte_limit" and set(header) == {
                "correlation_id",
                "kind",
                "resource",
                "limit",
                "observed",
            }:
                if body or header["resource"] not in {"tool_input_bytes", "tool_output_bytes"}:
                    raise RuntimeError("isolated worker returned malformed byte-limit evidence")
                raise ToolByteLimitError(
                    str(header["resource"]),
                    limit=int(header["limit"]),
                    observed=int(header["observed"]),
                )
            if kind == "result" and set(header) == {"correlation_id", "kind"}:
                try:
                    result = ToolResult.model_validate_json(body, strict=False)
                except (ValueError, TypeError):
                    return self._isolated_failure_result(
                        name,
                        payload,
                        "isolated worker returned an invalid ToolResult payload",
                        bind_versioned_range_failure=_bind_versioned_range_failure,
                    )
                if (
                    result.model_dump_json().encode("utf-8") != body
                    or result.tool_name != name
                    or result.input != payload
                ):
                    return self._isolated_failure_result(
                        name,
                        payload,
                        "isolated worker returned a mismatched ToolResult payload",
                        bind_versioned_range_failure=_bind_versioned_range_failure,
                    )
                if result.output and definition.contract is not None:
                    try:
                        typed_output = definition.contract.output_model.model_validate_json(
                            json.dumps(
                                result.output,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                            strict=True,
                        )
                    except (ValueError, TypeError):
                        return self._isolated_failure_result(
                            name,
                            payload,
                            "isolated worker returned an output outside its schema",
                            bind_versioned_range_failure=_bind_versioned_range_failure,
                        )
                    # Preserve the public in-process ToolResult contract after
                    # the JSON IPC boundary (notably tuple-valued outputs), but
                    # do not synthesize absent optional fields into the mapping.
                    restored_output = typed_output.model_dump(mode="python")
                    result = result.model_copy(
                        update={"output": {key: restored_output[key] for key in result.output}}
                    )
                return result
            if kind == "worker_failure" and set(header) == {
                "correlation_id",
                "kind",
                "error_type",
                "message",
            }:
                return self._isolated_failure_result(
                    name,
                    payload,
                    f"isolated worker {header['error_type']}: {header['message']}",
                    bind_versioned_range_failure=_bind_versioned_range_failure,
                )
            return self._isolated_failure_result(
                name,
                payload,
                "isolated worker returned a malformed result envelope",
                bind_versioned_range_failure=_bind_versioned_range_failure,
            )
        finally:
            parent_connection.close()
            if process.is_alive():
                _stop_phase_process(process)


def _extract_warnings(output: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    warning = output.get("warning")
    if warning:
        warnings.append(str(warning))
    many = output.get("warnings")
    if isinstance(many, list):
        warnings.extend(str(item) for item in many)
    return warnings


def _legacy_numeric_exactness(output: dict[str, Any], declared: str) -> NumericalExactness:
    if output.get("exact") is False or output.get("exact_algorithm") is False:
        return NumericalExactness.APPROXIMATE
    if declared == "approximate":
        return NumericalExactness.APPROXIMATE
    if declared == "unavailable":
        return NumericalExactness.UNAVAILABLE
    return NumericalExactness.EXACT


def _legacy_exactness_projection(numeric: NumericalExactness) -> Exactness:
    if numeric is NumericalExactness.APPROXIMATE:
        return Exactness.APPROXIMATE
    if numeric is NumericalExactness.UNAVAILABLE:
        return Exactness.UNAVAILABLE
    return Exactness.EXACT


def _verification_metadata(
    contract: ToolContract | None,
    numeric: NumericalExactness,
    payload: dict[str, Any],
    output: dict[str, Any],
) -> VerificationMetadata | None:
    if numeric is not NumericalExactness.FLOATING_VERIFIED:
        return None
    if contract is None:
        raise ValueError("floating-verified result lacks a typed verification policy")
    evidence = contract.verify_floating(payload, output)
    return VerificationMetadata(
        method="executed tool-specific invariant checks",
        checks=list(evidence.checks),
        observations=list(evidence.observations),
        tolerance=evidence.tolerance,
        passed=True,
    )


def _approximate_metadata(
    name: str,
    output: dict[str, Any],
    numeric: NumericalExactness,
) -> dict[str, Any]:
    if numeric is not NumericalExactness.APPROXIMATE:
        return {}
    method = str(output.get("method", ""))
    if name == "holdem_equity" and method == "monte_carlo":
        return {
            "stochastic": True,
            "confidence_level": 0.95,
            "stopping_condition": "fixed requested sample count",
        }
    if name == "matrix_game" and method == "fictitious_play_fallback":
        return {
            "stochastic": False,
            "error_metadata": NumericalErrorMetadata(
                metric="duality_gap",
                value=float(output["duality_gap"]),
                unit="caller payoff unit",
            ),
            "stopping_condition": "fixed fictitious-play iteration count",
        }
    raise ValueError(f"{name} approximate output lacks a registered metadata adapter")


def _pot_odds_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return pot_odds(**payload)


def _break_even_fold_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return break_even_fold_frequency(**payload)


def _mdf_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return minimum_defense_frequency(**payload)


def _spr_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return stack_to_pot_ratio(**payload)


def _effective_stack_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return effective_stack(**payload)


def _rake_amount_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return rake_amount(**payload)


def _raked_call_ev_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return raked_call_ev(**payload)


def _bluff_ev_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return bluff_ev(**payload)


def _polar_river_bluff_fraction_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return polar_river_bluff_fraction(**payload)


def _bayes_update_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return bayes_update(**payload)


def _pot_reconstruction_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return reconstruct_pot(**payload)


def _icm_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return calculate_icm(
        list(map(float, payload["stacks"])),
        list(map(float, payload["payouts"])),
    )


def _matrix_game_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return solve_zero_sum_matrix(
        [[float(value) for value in row] for row in payload["matrix"]],
        tolerance=float(payload.get("tolerance", 1e-9)),
        max_support_size=int(payload.get("max_support_size", 8)),
        fallback_iterations=int(payload.get("fallback_iterations", 50_000)),
    )


def _fixed_strategy_best_response_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return best_response_to_fixed_strategy(
        payload["game"],
        payload["fixed_strategy"],
        best_responder=int(payload.get("best_responder", 0)),
        max_pure_policies=int(payload.get("max_pure_policies", 1_000_000)),
    )


def _sensitivity_tool(payload: dict[str, Any]) -> dict[str, Any]:
    return analyze_scenarios(
        payload["scenarios"],
        decision_threshold=float(payload.get("decision_threshold", 0.0)),
    )


def _combo_tool(payload: dict[str, Any]) -> dict[str, Any]:
    if "range" in payload:
        combos = parse_weighted_range(
            str(payload["range"]), tuple(map(str, payload.get("dead_cards", [])))
        )
        total_weight = sum(combo.weight for combo in combos)
        return {
            "range": payload["range"],
            "combo_count": len(combos),
            "total_combo_weight": total_weight,
            "normalized_weights": [
                {"cards": list(combo.cards), "weight": combo.weight / total_weight}
                for combo in combos
            ],
        }
    return combo_summary(str(payload["hand_class"]), tuple(map(str, payload.get("dead_cards", []))))


def _range_validate_tool(payload: dict[str, Any]) -> dict[str, Any]:
    from poker_deliberation.range_grammar import validate_versioned_range

    request = RangeValidateInput.model_validate(payload)
    return validate_versioned_range(
        request.hand,
        request.range_definition,
    ).model_dump(mode="python")


def _equity_tool(payload: dict[str, Any]) -> dict[str, Any]:
    game_type = str(payload.get("game_type", "NLHE")).upper()
    if game_type != "NLHE":
        raise ValueError("holdem_equity supports NLHE only")
    if "opponent_ranges" in payload or "villain_ranges" in payload:
        raise ValueError("holdem_equity supports exactly one villain")
    return holdem_equity(
        hero_range=str(payload["hero_range"]),
        villain_range=str(payload["villain_range"]),
        board=tuple(map(str, payload.get("board", []))),
        dead_cards=tuple(map(str, payload.get("dead_cards", []))),
        mode=str(payload.get("mode", "auto")),
        max_exact_evaluations=int(payload.get("max_exact_evaluations", 250_000)),
        samples=int(payload.get("samples", 50_000)),
        seed=int(payload.get("seed", 0)),
    )


def _solver_status(_payload: dict[str, Any]) -> dict[str, Any]:
    response = UnavailableSolverAdapter().health_check().model_dump(mode="json")
    return {**response, "unavailable": True}


def _hand_validator_tool(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    tolerance = normalized.pop("tolerance", None)
    hand = CanonicalHand.model_validate(normalized)
    return validate_hand(hand, tolerance=float(tolerance) if tolerance is not None else None)


_CANONICAL_PHASE_FUNCTIONS: dict[str, ToolFunction] = {
    "pot_odds": _pot_odds_tool,
    "break_even_fold": _break_even_fold_tool,
    "mdf": _mdf_tool,
    "spr": _spr_tool,
    "effective_stack": _effective_stack_tool,
    "rake_amount": _rake_amount_tool,
    "raked_call_ev": _raked_call_ev_tool,
    "bluff_ev": _bluff_ev_tool,
    "polar_river_bluff_fraction": _polar_river_bluff_fraction_tool,
    "bayes_update": _bayes_update_tool,
    "pot_reconstruction": _pot_reconstruction_tool,
    "range_validate": _range_validate_tool,
    "combos": _combo_tool,
    "holdem_equity": _equity_tool,
    "ev_tree": evaluate_ev_tree,
    "icm": _icm_tool,
    "matrix_game": _matrix_game_tool,
    "fixed_strategy_best_response": _fixed_strategy_best_response_tool,
    "hand_validator": _hand_validator_tool,
    "hand_pot_ledger": calculate_hand_pot_ledger,
    "sensitivity": _sensitivity_tool,
    "solver_status": _solver_status,
}


def default_registry(
    *,
    max_payload_bytes: int = 1_000_000,
    max_output_bytes: int = 1_000_000,
    max_duration_seconds: float = 30.0,
    monotonic_clock: MonotonicClock | None = None,
) -> ToolRegistry:
    registry = ToolRegistry(
        max_payload_bytes=max_payload_bytes,
        max_output_bytes=max_output_bytes,
        max_duration_seconds=max_duration_seconds,
        monotonic_clock=monotonic_clock,
    )
    definitions = [
        ToolDefinition(
            "pot_odds",
            "Pot odds and required equity after a bet and optional rake.",
            "floating-verified",
            ("NLHE", "PLO", "generic"),
            _pot_odds_tool,
        ),
        ToolDefinition(
            "break_even_fold",
            "Break-even fold frequency for a zero-equity bluff.",
            "floating-verified",
            ("generic",),
            _break_even_fold_tool,
            ("Called branch has zero equity unless represented elsewhere in an EV tree.",),
        ),
        ToolDefinition(
            "mdf",
            "Minimum defense frequency against one bet in the zero-equity-bluff toy model.",
            "floating-verified",
            ("generic",),
            _mdf_tool,
            ("Single bet; no future action; MDF is not a complete strategy prescription.",),
        ),
        ToolDefinition(
            "spr",
            "Stack-to-pot ratio from a supplied effective stack and pot.",
            "floating-verified",
            ("NLHE", "PLO", "generic"),
            _spr_tool,
        ),
        ToolDefinition(
            "effective_stack",
            "Effective stack as the minimum supplied remaining stack.",
            "floating-verified",
            ("NLHE", "PLO", "generic"),
            _effective_stack_tool,
        ),
        ToolDefinition(
            "rake_amount",
            "Declared percentage rake with an optional cap.",
            "floating-verified",
            ("cash", "generic"),
            _rake_amount_tool,
            ("rake_percent is expressed as percentage points, for example 5 for 5%.",),
        ),
        ToolDefinition(
            "raked_call_ev",
            "Call EV with declared final-pot rake in a no-future-betting model.",
            "floating-verified",
            ("cash", "generic"),
            _raked_call_ev_tool,
            ("No future betting; equity is supplied; rake is taken from the final pot.",),
        ),
        ToolDefinition(
            "bluff_ev",
            "Bet EV against a supplied fold frequency and called-branch equity.",
            "floating-verified",
            ("generic",),
            _bluff_ev_tool,
            ("Single street; opponent calls or folds; no rake or future betting.",),
        ),
        ToolDefinition(
            "polar_river_bluff_fraction",
            "Indifference bluff fraction for a polarized river toy model.",
            "floating-verified",
            ("NLHE", "PLO", "generic"),
            _polar_river_bluff_fraction_tool,
            ("River only; polarized value/bluff range versus a bluff-catcher; no rake.",),
        ),
        ToolDefinition(
            "bayes_update",
            "Bayesian posterior from a supplied prior and likelihoods.",
            "floating-verified",
            ("generic",),
            _bayes_update_tool,
            ("The supplied prior and likelihoods are assumptions, not inferred population data.",),
        ),
        ToolDefinition(
            "pot_reconstruction",
            "Reconstruct a pot from incremental contributions.",
            "floating-verified",
            ("generic",),
            _pot_reconstruction_tool,
        ),
        ToolDefinition(
            "range_validate",
            "Validate and canonicalize one provenance-qualified versioned NLHE range.",
            "exact",
            ("NLHE",),
            _range_validate_tool,
        ),
        ToolDefinition(
            "combos",
            "Expand pairs, suited, offsuit, and weighted Hold'em ranges with blockers.",
            "mixed",
            ("NLHE",),
            _combo_tool,
        ),
        ToolDefinition(
            "holdem_equity",
            "Heads-up Hold'em equity by complete enumeration or seeded Monte Carlo.",
            "mixed",
            ("NLHE",),
            _equity_tool,
            ("Heads-up only in the MVP.",),
        ),
        ToolDefinition(
            "ev_tree",
            "Expected value of a finite tree with supplied branch probabilities.",
            "floating-verified",
            ("generic",),
            evaluate_ev_tree,
        ),
        ToolDefinition(
            "icm",
            "Independent Chip Model expected payouts.",
            "floating-verified",
            ("tournament",),
            _icm_tool,
            ("ICM independence assumption; no future-game simulation.",),
        ),
        ToolDefinition(
            "matrix_game",
            "Small two-player zero-sum matrix equilibrium and exploitability gap.",
            "mixed",
            ("matrix",),
            _matrix_game_tool,
        ),
        ToolDefinition(
            "fixed_strategy_best_response",
            "Exhaustive small-game best response with shared information-set actions.",
            "floating-verified",
            ("finite_extensive_form",),
            _fixed_strategy_best_response_tool,
            ("Opponent strategy is fixed at every opponent information set.",),
        ),
        ToolDefinition(
            "hand_validator",
            "Validate canonical hand cards, action order, stacks, and pots.",
            "floating-verified",
            ("NLHE", "PLO"),
            _hand_validator_tool,
        ),
        ToolDefinition(
            "hand_pot_ledger",
            "Exact profiled NLHE contribution, return, side-pot, and eligibility ledger.",
            "exact-under-model",
            ("NLHE cash",),
            calculate_hand_pot_ledger,
        ),
        ToolDefinition(
            "sensitivity",
            "Bounds and influence ranking over a supplied scenario grid.",
            "floating-verified",
            ("generic",),
            _sensitivity_tool,
            ("Bounds apply only to the supplied scenario grid.",),
        ),
        ToolDefinition(
            "solver_status",
            "Discover external solver availability without fabricating output.",
            "unavailable",
            ("NLHE",),
            _solver_status,
        ),
    ]
    contracts = contract_by_name()
    if {definition.name for definition in definitions} != set(contracts):
        raise RuntimeError("registry function map and canonical tool contracts differ")
    for definition in definitions:
        contract = contracts[definition.name]
        registry.register(
            ToolDefinition(
                name=contract.name,
                purpose=contract.purpose,
                exact_or_approximate="/".join(
                    item.value for item in contract.numeric_exactness_modes
                ),
                supported_games=contract.supported_games,
                function=definition.function,
                assumptions=contract.assumptions,
                version=contract.version,
                contract=contract,
                phase_isolated=True,
            )
        )
    return registry
