"""Strict immutable values for the P2-011A budget contract."""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BUDGET_SCHEMA_VERSION: Literal["2.0.0"] = "2.0.0"
MICRO_USD_PER_USD = Decimal(1_000_000)
NANOSECONDS_PER_SECOND = Decimal(1_000_000_000)


class _BudgetModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        strict=True,
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def canonical_budget_json(value: Any) -> str:
    try:
        return json.dumps(
            _json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("budget value is not canonical JSON") from exc


def canonical_budget_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_budget_json(value).encode("utf-8")).hexdigest()


def canonical_json_utf8_size(value: Any) -> int:
    """Measure one JSON value with the canonical UTF-8 representation used by caps."""

    try:
        return len(
            json.dumps(
                _json_value(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not canonical JSON") from exc


def decimal_usd_to_micro_usd(value: Decimal) -> int:
    if not isinstance(value, Decimal):
        raise TypeError("USD cost must be provided as Decimal")
    if not value.is_finite() or value < 0:
        raise ValueError("USD cost must be finite and non-negative")
    micro_usd = value * MICRO_USD_PER_USD
    if micro_usd != micro_usd.to_integral_value():
        raise ValueError("USD cost must be exactly representable in integer micro-USD")
    return int(micro_usd)


class ExecutionClass(StrEnum):
    LOCAL_FREE = "local_free"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


class DeadlineStatus(StrEnum):
    ACTIVE = "active"
    TIMED_OUT = "timed_out"


class CancellationStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    CANCEL_REQUESTED = "cancel_requested"
    CANCEL_UNCONFIRMED = "cancel_unconfirmed"
    CANCELLED = "cancelled"


class BudgetFailureCode(StrEnum):
    INVALID_POLICY = "invalid_budget_policy"
    UNSUPPORTED_CONCURRENCY = "unsupported_concurrency"
    UNSUPPORTED_LEGACY_FIELD = "unsupported_legacy_field"
    CLOCK_ROLLBACK = "clock_rollback"
    RUNTIME_EXCEEDED = "runtime_exceeded"
    EXTERNAL_EXECUTION_UNKNOWN = "external_execution_unknown"
    EXTERNAL_COST_UNKNOWN = "external_cost_unknown"
    EXTERNAL_COST_DISABLED = "external_cost_disabled"
    EXTERNAL_COST_EXCEEDED = "external_cost_exceeded"
    PROVIDER_OUTPUT_EXCEEDED = "provider_output_exceeded"
    TOOL_INPUT_EXCEEDED = "tool_input_exceeded"
    TOOL_OUTPUT_EXCEEDED = "tool_output_exceeded"
    ARTIFACT_EXCEEDED = "artifact_exceeded"
    RUN_EXCEEDED = "run_exceeded"
    USAGE_MALFORMED = "usage_malformed"


class BudgetFailure(_BudgetModel):
    schema_version: Literal["2.0.0"] = BUDGET_SCHEMA_VERSION
    code: BudgetFailureCode
    resource: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_-]+$")
    message: str = Field(min_length=1, max_length=512)
    limit: int | None = Field(default=None, ge=0)
    observed: int | None = Field(default=None, ge=0)
    retryable: Literal[False] = False


class BudgetLimitError(RuntimeError):
    def __init__(self, failure: BudgetFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


class BudgetPolicyV2(_BudgetModel):
    schema_version: Literal["2.0.0"] = BUDGET_SCHEMA_VERSION
    max_deliberation_rounds: int = Field(default=1, ge=0, le=10)
    max_tool_retries: int = Field(default=0, ge=0, le=10)
    max_concurrent_agents: Literal[1] = 1
    max_runtime_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
    max_external_cost_micro_usd: int = Field(default=0, ge=0)
    max_provider_output_bytes: int = Field(default=1_000_000, ge=1_024)
    max_tool_input_bytes: int = Field(default=1_000_000, ge=1_024)
    max_tool_output_bytes: int = Field(default=1_000_000, ge=1_024)
    max_artifact_bytes: int = Field(default=1_000_000, ge=1_024)
    max_run_bytes: int = Field(default=10_000_000, ge=10_240)

    @field_validator("max_runtime_seconds", mode="before")
    @classmethod
    def strict_finite_seconds(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("max_runtime_seconds must be a numeric seconds value")
        result = float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError("max_runtime_seconds must be finite and positive")
        nanoseconds = Decimal(str(result)) * NANOSECONDS_PER_SECOND
        if nanoseconds != nanoseconds.to_integral_value():
            raise ValueError("max_runtime_seconds must be representable in integer nanoseconds")
        return result

    @model_validator(mode="after")
    def run_cap_can_hold_one_artifact(self) -> BudgetPolicyV2:
        if self.max_run_bytes < self.max_artifact_bytes:
            raise ValueError("max_run_bytes must be at least max_artifact_bytes")
        return self

    @property
    def runtime_limit_ns(self) -> int:
        return int(Decimal(str(self.max_runtime_seconds)) * NANOSECONDS_PER_SECOND)

    @property
    def canonical_sha256(self) -> str:
        return canonical_budget_sha256(self)


class UsageDelta(_BudgetModel):
    schema_version: Literal["2.0.0"] = BUDGET_SCHEMA_VERSION
    provider_attempts: int = Field(default=0, ge=0)
    tool_attempts: int = Field(default=0, ge=0)
    retry_attempts: int = Field(default=0, ge=0)
    active_runtime_ns: int = Field(default=0, ge=0)
    external_cost_micro_usd: int = Field(default=0, ge=0)
    provider_output_bytes: int = Field(default=0, ge=0)
    tool_input_bytes: int = Field(default=0, ge=0)
    tool_output_bytes: int = Field(default=0, ge=0)
    artifact_bytes: int = Field(default=0, ge=0)
    run_bytes: int = Field(default=0, ge=0)
    peak_concurrency: int = Field(default=0, ge=0)

    def combine(self, other: UsageDelta) -> UsageDelta:
        return UsageDelta(
            provider_attempts=self.provider_attempts + other.provider_attempts,
            tool_attempts=self.tool_attempts + other.tool_attempts,
            retry_attempts=self.retry_attempts + other.retry_attempts,
            active_runtime_ns=self.active_runtime_ns + other.active_runtime_ns,
            external_cost_micro_usd=(self.external_cost_micro_usd + other.external_cost_micro_usd),
            provider_output_bytes=max(self.provider_output_bytes, other.provider_output_bytes),
            tool_input_bytes=max(self.tool_input_bytes, other.tool_input_bytes),
            tool_output_bytes=max(self.tool_output_bytes, other.tool_output_bytes),
            artifact_bytes=max(self.artifact_bytes, other.artifact_bytes),
            run_bytes=self.run_bytes + other.run_bytes,
            peak_concurrency=max(self.peak_concurrency, other.peak_concurrency),
        )


class BudgetSnapshot(UsageDelta):
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    def apply(self, delta: UsageDelta) -> BudgetSnapshot:
        combined = UsageDelta(
            **self.model_dump(
                exclude={"schema_version", "policy_sha256"},
                mode="python",
            )
        ).combine(delta)
        return BudgetSnapshot(policy_sha256=self.policy_sha256, **combined.model_dump())


class V1BudgetMigrationResult(_BudgetModel):
    source_schema_version: Literal["1.0.0"] = "1.0.0"
    target_schema_version: Literal["2.0.0"] = BUDGET_SCHEMA_VERSION
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: BudgetPolicyV2
    ignored_legacy_fields: tuple[str, ...]
    warnings: tuple[str, ...]

    @field_validator("ignored_legacy_fields")
    @classmethod
    def sorted_unique_fields(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("ignored_legacy_fields must be sorted and unique")
        return value

    @property
    def canonical_sha256(self) -> str:
        return canonical_budget_sha256(self)
