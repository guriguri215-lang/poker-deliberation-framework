"""Retry classification without automatic execution."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation.budgets.contracts import BUDGET_SCHEMA_VERSION


class FailureCategory(StrEnum):
    VALIDATION = "validation"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    BUDGET = "budget"
    DEADLINE = "deadline"
    CANCEL = "cancel"
    POLICY = "policy"
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_PERMANENT = "provider_permanent"
    TOOL_TRANSIENT = "tool_transient"
    TOOL_DETERMINISTIC = "tool_deterministic"
    VERIFICATION = "verification"
    EXTERNAL_EFFECT_UNKNOWN = "external_effect_unknown"
    INTERNAL = "internal"


class IdempotencyStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    IDEMPOTENT = "idempotent"
    RECONCILABLE = "reconcilable"
    UNKNOWN = "unknown"


class RetryDisposition(StrEnum):
    RETRYABLE_CANDIDATE = "retryable_candidate"
    NON_RETRYABLE = "non_retryable"


class RetryClassification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal["2.0.0"] = BUDGET_SCHEMA_VERSION
    category: FailureCategory
    idempotency: IdempotencyStatus
    disposition: RetryDisposition
    retryable: bool
    automatic_retry: bool = False
    max_retries: int = Field(ge=0)
    max_attempt_candidates: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def values_are_consistent(self) -> RetryClassification:
        if self.max_attempt_candidates != self.max_retries + 1:
            raise ValueError("max attempts must equal max retries plus one")
        if self.retryable != (self.disposition is RetryDisposition.RETRYABLE_CANDIDATE):
            raise ValueError("retryable must match disposition")
        if self.automatic_retry:
            raise ValueError("P2-011A does not execute automatic retries")
        return self


def classify_retry(
    category: FailureCategory,
    *,
    idempotency: IdempotencyStatus = IdempotencyStatus.UNKNOWN,
    max_retries: int = 0,
) -> RetryClassification:
    if isinstance(max_retries, bool) or not isinstance(max_retries, int):
        raise TypeError("max_retries must be an integer")
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    transient = category in {
        FailureCategory.PROVIDER_TRANSIENT,
        FailureCategory.TOOL_TRANSIENT,
    }
    idempotent = idempotency in {
        IdempotencyStatus.NOT_APPLICABLE,
        IdempotencyStatus.IDEMPOTENT,
        IdempotencyStatus.RECONCILABLE,
    }
    retryable = transient and idempotent
    return RetryClassification(
        category=category,
        idempotency=idempotency,
        disposition=(
            RetryDisposition.RETRYABLE_CANDIDATE if retryable else RetryDisposition.NON_RETRYABLE
        ),
        retryable=retryable,
        max_retries=max_retries,
        max_attempt_candidates=max_retries + 1,
        reason=(
            "transient failure is a retry candidate with an idempotent or reconcilable effect"
            if retryable
            else "failure category or effect idempotency forbids automatic retry"
        ),
    )
