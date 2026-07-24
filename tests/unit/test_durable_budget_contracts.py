"""Contract tests for internal durable budget models."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.budgets.durable_models import (
    RESOURCE_ORDER,
    DurableBudgetFailureV1,
    DurableBudgetPolicyV1,
    DurableBudgetStateV1,
    DurableFailureCode,
    DurablePermitV1,
    DurableSettlementV1,
    ExecutionActivationV1,
    ExecutionLineageV1,
    OperationKind,
    OwnerKind,
    PermitStatus,
    ResourceAmountsV1,
    ResourceReservationV1,
    SettlementStatus,
    canonical_durable_sha256,
)

HASH = "a" * 64


def _lineage(*, attempt: str = "attempt-1", ordinal: int = 0) -> ExecutionLineageV1:
    return ExecutionLineageV1(
        owner_kind=OwnerKind.AGENT,
        owner_id="owner-1",
        role="strategy_analyst",
        phase_id="analysis",
        assignment_id="assignment-1",
        root_attempt_id=attempt,
        attempt_id=attempt,
        root_context_id=f"context-{ordinal}",
        context_id=f"context-{ordinal}",
        context_source_sha256=HASH,
        context_policy_sha256=HASH,
        context_integrity_sha256=HASH,
        execution_ordinal=ordinal,
        idempotency_key=f"effect-{ordinal}",
        idempotency_request_sha256=HASH,
    )


def _policy() -> DurableBudgetPolicyV1:
    return DurableBudgetPolicyV1(
        base_policy=BudgetPolicyV2(max_tool_retries=2),
        activation=ExecutionActivationV1(
            max_concurrent_agents=2,
            max_automatic_retries=1,
        ),
    )


def test_activation_is_opt_in_and_cannot_exceed_the_base_retry_limit() -> None:
    assert DurableBudgetPolicyV1().activation == ExecutionActivationV1()
    assert DurableBudgetPolicyV1().base_policy == BudgetPolicyV2()

    with pytest.raises(ValidationError, match="automatic retries exceed"):
        DurableBudgetPolicyV1(
            base_policy=BudgetPolicyV2(max_tool_retries=0),
            activation=ExecutionActivationV1(max_automatic_retries=1),
        )
    with pytest.raises(ValidationError):
        ExecutionActivationV1(max_concurrent_agents=33)


def test_resource_order_and_canonical_hash_are_exact_and_deterministic() -> None:
    assert RESOURCE_ORDER == (
        "active_runtime_ns",
        "provider_attempts",
        "tool_attempts",
        "retry_attempts",
        "external_cost_micro_usd",
        "provider_output_bytes",
        "tool_input_bytes",
        "tool_output_bytes",
        "artifact_bytes",
        "run_bytes",
        "concurrency_slots",
    )
    amounts = ResourceAmountsV1(tool_attempts=1, concurrency_slots=1)
    assert tuple(name for name, _value in amounts.ordered_items()) == RESOURCE_ORDER
    assert canonical_durable_sha256(amounts) == canonical_durable_sha256(
        json.loads(amounts.model_dump_json())
    )
    with pytest.raises(ValidationError):
        ResourceAmountsV1(tool_attempts=-1)
    with pytest.raises(ValidationError):
        ResourceAmountsV1(tool_attempts=True)


def test_permit_requires_exactly_one_slot_and_valid_start_order() -> None:
    reservation = ResourceReservationV1(
        reservation_id="reservation-1",
        requested=ResourceAmountsV1(tool_attempts=1, concurrency_slots=1),
        request_sha256=HASH,
    )
    permit = DurablePermitV1(
        permit_id="permit-1",
        reservation=reservation,
        lineage=_lineage(),
        reserved_monotonic_ns=10,
    )
    assert permit.status is PermitStatus.RESERVED

    with pytest.raises(ValidationError, match="exactly one"):
        ResourceReservationV1(
            reservation_id="reservation-2",
            requested=ResourceAmountsV1(concurrency_slots=0),
            request_sha256=HASH,
        )
    with pytest.raises(ValidationError, match="precedes"):
        DurablePermitV1(
            permit_id="permit-1",
            reservation=reservation,
            lineage=_lineage(),
            status=PermitStatus.STARTED,
            reserved_monotonic_ns=10,
            started_monotonic_ns=9,
        )


def test_settlement_records_exact_release_and_overrun_without_truncation() -> None:
    reserved = ResourceAmountsV1(
        tool_attempts=1,
        tool_output_bytes=100,
        concurrency_slots=1,
    )
    normal = DurableSettlementV1(
        settlement_id="settlement-1",
        permit_id="permit-1",
        operation_id="settle-1",
        operation_request_sha256=HASH,
        reserved=reserved,
        actual=ResourceAmountsV1(
            tool_attempts=1,
            tool_output_bytes=80,
            concurrency_slots=1,
        ),
        released=ResourceAmountsV1(tool_output_bytes=20),
        status=SettlementStatus.SUCCEEDED,
        result_sha256=HASH,
        effect_evidence_sha256=HASH,
        settled_monotonic_ns=20,
    )
    assert normal.actual.tool_output_bytes == 80

    overrun = DurableSettlementV1(
        settlement_id="settlement-2",
        permit_id="permit-2",
        operation_id="settle-2",
        operation_request_sha256=HASH,
        reserved=reserved,
        actual=ResourceAmountsV1(
            tool_attempts=1,
            tool_output_bytes=101,
            concurrency_slots=1,
        ),
        released=ResourceAmountsV1(),
        status=SettlementStatus.OVERRUN,
        settled_monotonic_ns=20,
    )
    assert overrun.actual.tool_output_bytes == 101
    with pytest.raises(ValidationError, match="overrun status"):
        DurableSettlementV1.model_validate(
            {**overrun.model_dump(mode="python"), "status": SettlementStatus.FAILED}
        )


def test_state_binds_policy_lineage_and_unique_deterministic_identities() -> None:
    policy = _policy()
    reservation = ResourceReservationV1(
        reservation_id="reservation-1",
        requested=ResourceAmountsV1(tool_attempts=1, concurrency_slots=1),
        request_sha256=HASH,
    )
    permit = DurablePermitV1(
        permit_id="permit-1",
        reservation=reservation,
        lineage=_lineage(),
        reserved_monotonic_ns=10,
    )
    state = DurableBudgetStateV1(
        run_id="run-1",
        generation=1,
        policy=policy,
        policy_sha256=policy.policy_sha256,
        activation_sha256=policy.activation_sha256,
        active_permits=(permit,),
        active_runtime_remaining_ns=policy.base_policy.runtime_limit_ns,
    )
    assert DurableBudgetStateV1.model_validate_json(state.canonical_bytes()) == state
    assert len(state.canonical_sha256) == 64

    with pytest.raises(ValidationError, match="policy hash mismatch"):
        DurableBudgetStateV1.model_validate(
            {**state.model_dump(mode="python"), "policy_sha256": "b" * 64}
        )
    with pytest.raises(ValidationError, match="duplicate durable permit"):
        DurableBudgetStateV1.model_validate(
            {**state.model_dump(mode="python"), "active_permits": (permit, permit)}
        )
    with pytest.raises(ValidationError, match="previous state hash"):
        DurableBudgetStateV1.model_validate(
            {**state.model_dump(mode="python"), "generation": 2}
        )


def test_failure_latch_cannot_downgrade_unknown_effect() -> None:
    failure = DurableBudgetFailureV1(
        code=DurableFailureCode.EFFECT_UNKNOWN,
        operation_id="settle-1",
        reconciliation_required=True,
        effect_unknown=True,
        evidence_sha256=HASH,
    )
    assert failure.effect_unknown
    with pytest.raises(ValidationError, match="effect flag"):
        DurableBudgetFailureV1(code=DurableFailureCode.EFFECT_UNKNOWN)


def test_retry_lineage_requires_fresh_attempt_and_context_ids() -> None:
    retry = ExecutionLineageV1(
        owner_kind=OwnerKind.AGENT,
        owner_id="owner-1",
        role="strategy_analyst",
        phase_id="analysis",
        assignment_id="assignment-1",
        root_attempt_id="attempt-1",
        parent_attempt_id="attempt-1",
        attempt_id="attempt-2",
        root_context_id="context-1",
        parent_context_id="context-1",
        context_id="context-2",
        context_source_sha256=HASH,
        context_policy_sha256=HASH,
        context_integrity_sha256=HASH,
        execution_ordinal=1,
        idempotency_key="effect-1",
        idempotency_request_sha256=HASH,
    )
    assert retry.parent_attempt_id == "attempt-1"
    with pytest.raises(ValidationError, match="retry attempt ID must be fresh"):
        ExecutionLineageV1.model_validate(
            {**retry.model_dump(mode="python"), "attempt_id": "attempt-1"}
        )


def test_models_are_frozen_strict_and_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ExecutionActivationV1.model_validate(
            {"max_concurrent_agents": "1", "max_automatic_retries": 0}
        )
    with pytest.raises(ValidationError):
        ExecutionActivationV1.model_validate(
            {
                "max_concurrent_agents": 1,
                "max_automatic_retries": 0,
                "automatic_parallel_product_path": True,
            }
        )
    assert OperationKind.RESERVE.value == "reserve"
