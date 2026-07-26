"""Integration tests for bounded durable execution."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.budgets import BudgetPolicyV2, ExecutionClass
from poker_deliberation.budgets.durable_models import (
    DeterministicToolEvidenceV1,
    DurableBudgetPolicyV1,
    DurableFailureCode,
    ExecutionLineageV1,
    OwnerKind,
    ResourceAmountsV1,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetError,
    DurableBudgetStore,
    build_resource_reservation,
    initialize_durable_budget_root,
)
from poker_deliberation.budgets.execution import (
    DurableBoundedExecutor,
    DurableExecutionTask,
    EffectResultV1,
    EffectStatus,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64


@pytest.fixture
def store() -> Generator[DurableBudgetStore, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2bi-", dir=parent) as directory:
        base = Path(directory)
        legacy = base / "legacy"
        legacy.mkdir()
        revision = base / "revision"
        initialize_durable_budget_root(
            revision,
            legacy,
            root_id="root-" + "e" * 32,
            initialized_at=NOW,
        )
        ticks = iter(range(1_000, 20_000))
        value = DurableBudgetStore(
            revision,
            legacy,
            clock=lambda: next(ticks),
            wall_clock=lambda: NOW,
        )
        value.create(
            "Run-deterministic",
            DurableBudgetPolicyV1(
                base_policy=BudgetPolicyV2(
                    max_deliberation_rounds=2,
                    max_tool_output_bytes=2_000,
                    max_tool_input_bytes=2_000,
                    max_artifact_bytes=2_000,
                    max_run_bytes=20_000,
                )
            ),
            operation_id="initialize-deterministic",
        )
        yield value


def test_deterministic_settlement_replay_does_not_rerun_or_double_charge(
    store: DurableBudgetStore,
) -> None:
    calls = 0
    request_hash = "b" * 64
    result_hash = "c" * 64
    reproduction_hash = "d" * 64

    def calculator(_token, _lineage: ExecutionLineageV1) -> EffectResultV1:
        nonlocal calls
        calls += 1
        return EffectResultV1(
            status=EffectStatus.SUCCEEDED,
            actual=ResourceAmountsV1(
                active_runtime_ns=50,
                tool_attempts=1,
                tool_input_bytes=50,
                tool_output_bytes=50,
                run_bytes=50,
                concurrency_slots=1,
            ),
            result_sha256=result_hash,
            effect_evidence_sha256="e" * 64,
            deterministic_tool_evidence=DeterministicToolEvidenceV1(
                tool_request_bytes_sha256=request_hash,
                tool_result_bytes_sha256=result_hash,
                contract_version="1.0.0",
                reproduction_metadata_sha256=reproduction_hash,
                execution_ordinal=0,
            ),
        )

    lineage = ExecutionLineageV1(
        owner_kind=OwnerKind.TOOL,
        owner_id="calculator-owner",
        role="calculator",
        phase_id="tool_research",
        assignment_id="assignment-calculator",
        root_attempt_id="attempt-calculator",
        attempt_id="attempt-calculator",
        root_context_id="context-calculator",
        context_id="context-calculator",
        context_source_sha256=HASH,
        context_policy_sha256=HASH,
        context_integrity_sha256=HASH,
        execution_ordinal=0,
        idempotency_key="calculator-effect",
        idempotency_request_sha256=request_hash,
    )
    reservation = build_resource_reservation(
        reservation_id="calculator.reservation-0",
        requested=ResourceAmountsV1(
            active_runtime_ns=100,
            tool_attempts=1,
            tool_input_bytes=100,
            tool_output_bytes=100,
            run_bytes=100,
            concurrency_slots=1,
        ),
    )
    task = DurableExecutionTask(
        task_id="calculator",
        execution_ordinal=0,
        reservation=reservation,
        lineage=lineage,
        effect=calculator,
    )
    executor = DurableBoundedExecutor(store, "Run-deterministic")

    first = executor.execute((task,))
    second = executor.execute((task,))
    state = store.load("Run-deterministic")

    assert calls == 1
    assert first.records == second.records
    assert len(state.settlements) == 1
    assert state.usage.tool_attempts == 1
    assert state.usage.run_bytes == 50


def test_external_executor_requires_caller_authenticated_actual(
    store: DurableBudgetStore,
) -> None:
    policy = DurableBudgetPolicyV1(
        base_policy=BudgetPolicyV2(
            max_deliberation_rounds=2,
            max_external_cost_micro_usd=100,
            max_provider_output_bytes=2_000,
            max_tool_input_bytes=2_000,
            max_tool_output_bytes=2_000,
            max_artifact_bytes=2_000,
            max_run_bytes=20_000,
        )
    )
    for run_id in ("Run-external-unauth", "Run-external-auth"):
        store.create(
            run_id,
            policy,
            operation_id=f"initialize-{run_id}",
        )

    def task_for(
        run_id: str,
        *,
        authenticated: bool,
    ) -> DurableExecutionTask:
        lineage = ExecutionLineageV1(
            owner_kind=OwnerKind.PROVIDER,
            owner_id=f"provider-{run_id}",
            role="strategy_analyst",
            phase_id="analysis",
            assignment_id=f"assignment-{run_id}",
            root_attempt_id=f"attempt-{run_id}",
            attempt_id=f"attempt-{run_id}",
            root_context_id=f"context-{run_id}",
            context_id=f"context-{run_id}",
            context_source_sha256=HASH,
            context_policy_sha256=HASH,
            context_integrity_sha256=HASH,
            execution_ordinal=0,
            idempotency_key=f"effect-{run_id}",
            idempotency_request_sha256=HASH,
        )
        reservation = build_resource_reservation(
            reservation_id=f"reservation-{run_id}",
            requested=ResourceAmountsV1(
                provider_attempts=1,
                external_cost_micro_usd=20,
                provider_output_bytes=100,
                run_bytes=100,
                concurrency_slots=1,
            ),
            execution_class=ExecutionClass.EXTERNAL,
            external_cost_estimate_authenticated=True,
        )

        def provider(
            _token,
            _lineage: ExecutionLineageV1,
        ) -> EffectResultV1:
            return EffectResultV1(
                status=EffectStatus.SUCCEEDED,
                actual=ResourceAmountsV1(
                    provider_attempts=1,
                    external_cost_micro_usd=10,
                    provider_output_bytes=80,
                    run_bytes=80,
                    concurrency_slots=1,
                ),
                result_sha256="f" * 64,
                effect_evidence_sha256="1" * 64,
                external_cost_actual_authenticated=authenticated,
            )

        return DurableExecutionTask(
            task_id=f"external-{run_id}",
            execution_ordinal=0,
            reservation=reservation,
            lineage=lineage,
            effect=provider,
        )

    with pytest.raises(DurableBudgetError) as refused:
        DurableBoundedExecutor(store, "Run-external-unauth").execute(
            (task_for("Run-external-unauth", authenticated=False),)
        )
    assert refused.value.failure.code is DurableFailureCode.INVALID_INPUT
    refused_state = store.load("Run-external-unauth")
    assert refused_state.settlements == ()
    assert refused_state.active_permits[0].status.value == "started"

    accepted = DurableBoundedExecutor(store, "Run-external-auth").execute(
        (task_for("Run-external-auth", authenticated=True),)
    )
    accepted_state = store.load("Run-external-auth")
    assert accepted.records[0].final_status is EffectStatus.SUCCEEDED
    assert accepted_state.usage.external_cost_micro_usd == 10
    assert not accepted_state.active_permits
