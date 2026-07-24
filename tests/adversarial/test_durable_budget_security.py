"""Adversarial tests for durable budget validation."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from pydantic import ValidationError

from poker_deliberation.budgets import BudgetPolicyV2, ExecutionClass
from poker_deliberation.budgets.durable_models import (
    DURABLE_BUDGET_PRODUCER_ID,
    DURABLE_BUDGET_PRODUCER_VERSION,
    DurableBudgetPolicyV1,
    DurableBudgetStateV1,
    DurableEventV1,
    DurableFailureCode,
    DurableUsageV1,
    ExecutionActivationV1,
    ExecutionLineageV1,
    IdempotencyRecordV1,
    OperationKind,
    OperationOutcome,
    OwnerKind,
    ResourceAmountsV1,
    ResourceReservationV1,
    SettlementStatus,
    canonical_durable_sha256,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetError,
    DurableBudgetStore,
    _artifact,
    build_resource_reservation,
    initialize_durable_budget_root,
)
from poker_deliberation.storage.revision_models import (
    RevisionPublishRequestV1,
    RunStorageError,
    RunStorageFailureCode,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
HASH = "a" * 64
RUN_ID = "Run-durable-security"


@pytest.fixture
def store() -> Generator[DurableBudgetStore, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2ba-", dir=parent) as directory:
        base = Path(directory)
        legacy = base / "legacy"
        legacy.mkdir()
        revision = base / "revision"
        initialize_durable_budget_root(
            revision,
            legacy,
            root_id="root-" + "6" * 32,
            initialized_at=NOW,
        )
        durable = DurableBudgetStore(
            revision,
            legacy,
            clock=lambda: 100,
            wall_clock=lambda: NOW,
        )
        durable.create(
            RUN_ID,
            DurableBudgetPolicyV1(
                base_policy=BudgetPolicyV2(
                    max_deliberation_rounds=4,
                    max_tool_retries=2,
                    max_external_cost_micro_usd=100,
                    max_tool_input_bytes=2_000,
                    max_tool_output_bytes=2_000,
                    max_artifact_bytes=2_000,
                    max_run_bytes=20_000,
                ),
                activation=ExecutionActivationV1(
                    max_concurrent_agents=2,
                    max_automatic_retries=1,
                ),
            ),
            operation_id="initialize-security",
        )
        yield durable


def _lineage(
    ordinal: int = 0,
    *,
    suffix: str = "0",
    root_suffix: str | None = None,
    parent_suffix: str | None = None,
    owner_id: str = "owner-security",
) -> ExecutionLineageV1:
    root = root_suffix if root_suffix is not None else suffix
    parent = parent_suffix
    return ExecutionLineageV1(
        owner_kind=OwnerKind.TOOL,
        owner_id=owner_id,
        role="calculator",
        phase_id="tool_research",
        assignment_id="assignment-security",
        root_attempt_id=f"attempt-{root}",
        parent_attempt_id=None if parent is None else f"attempt-{parent}",
        attempt_id=f"attempt-{suffix}",
        root_context_id=f"context-{root}",
        parent_context_id=None if parent is None else f"context-{parent}",
        context_id=f"context-{suffix}",
        context_source_sha256=HASH,
        context_policy_sha256=HASH,
        context_integrity_sha256=HASH,
        execution_ordinal=ordinal,
        idempotency_key=f"effect-{suffix}",
        idempotency_request_sha256=HASH,
    )


def _reservation(suffix: str = "0") -> ResourceReservationV1:
    return build_resource_reservation(
        reservation_id=f"reservation-{suffix}",
        requested=ResourceAmountsV1(
            tool_attempts=1,
            tool_input_bytes=100,
            tool_output_bytes=100,
            run_bytes=100,
            concurrency_slots=1,
        ),
    )


def _reserve(
    store: DurableBudgetStore,
    suffix: str = "0",
    *,
    lineage: ExecutionLineageV1 | None = None,
) -> None:
    store.reserve(
        RUN_ID,
        operation_id=f"reserve-{suffix}",
        permit_id=f"permit-{suffix}",
        reservation=_reservation(suffix),
        lineage=lineage or _lineage(suffix=suffix),
    )


def test_secret_shaped_identifiers_and_unknown_external_cost_fail_closed() -> None:
    with pytest.raises(ValidationError, match="secret shape"):
        _lineage(owner_id="sk-abcdefghijklmnop")
    with pytest.raises(ValidationError, match="unknown execution class"):
        build_resource_reservation(
            reservation_id="unknown-external",
            requested=ResourceAmountsV1(concurrency_slots=1),
            execution_class=ExecutionClass.UNKNOWN,
        )
    with pytest.raises(ValidationError, match="authenticated positive estimate"):
        build_resource_reservation(
            reservation_id="zero-external",
            requested=ResourceAmountsV1(concurrency_slots=1),
            execution_class=ExecutionClass.EXTERNAL,
            external_cost_estimate_authenticated=True,
        )
    with pytest.raises(ValidationError, match="authenticated positive estimate"):
        build_resource_reservation(
            reservation_id="unauthenticated-external",
            requested=ResourceAmountsV1(
                external_cost_micro_usd=10,
                concurrency_slots=1,
            ),
            execution_class=ExecutionClass.EXTERNAL,
        )


def test_forged_reservation_hash_is_mutation_zero(
    store: DurableBudgetStore,
) -> None:
    requested = ResourceAmountsV1(
        tool_attempts=1,
        tool_input_bytes=100,
        tool_output_bytes=100,
        run_bytes=100,
        concurrency_slots=1,
    )
    forged = ResourceReservationV1(
        reservation_id="reservation-forged",
        requested=requested,
        request_sha256="f" * 64,
    )

    with pytest.raises(DurableBudgetError) as error:
        store.reserve(
            RUN_ID,
            operation_id="reserve-forged",
            permit_id="permit-forged",
            reservation=forged,
            lineage=_lineage(),
        )

    assert error.value.failure.code is DurableFailureCode.INVALID_INPUT
    state = store.load(RUN_ID)
    assert state.generation == 1
    assert state.active_permits == ()


def test_duplicate_ordinal_and_context_reuse_are_mutation_zero(
    store: DurableBudgetStore,
) -> None:
    _reserve(store)
    generation = store.load(RUN_ID).generation

    with pytest.raises(DurableBudgetError) as duplicate_ordinal:
        _reserve(store, "1", lineage=_lineage(0, suffix="1"))
    assert duplicate_ordinal.value.failure.code is DurableFailureCode.INVALID_INPUT
    assert store.load(RUN_ID).generation == generation

    reused_context = _lineage(1, suffix="2").model_copy(
        update={
            "root_context_id": "context-0",
            "context_id": "context-0",
        }
    )
    with pytest.raises(DurableBudgetError) as duplicate_context:
        _reserve(store, "2", lineage=reused_context)
    assert duplicate_context.value.failure.code is DurableFailureCode.IDEMPOTENCY_CONFLICT
    assert store.load(RUN_ID).generation == generation


def test_retry_owner_substitution_is_mutation_zero(
    store: DurableBudgetStore,
) -> None:
    _reserve(store)
    store.release_no_effect(
        RUN_ID,
        operation_id="release-0",
        settlement_id="release-settlement-0",
        permit_id="permit-0",
        evidence_sha256=HASH,
    )
    generation = store.load(RUN_ID).generation
    substituted = _lineage(
        0,
        suffix="retry",
        root_suffix="0",
        parent_suffix="0",
        owner_id="other-owner",
    )

    with pytest.raises(DurableBudgetError) as error:
        _reserve(store, "retry", lineage=substituted)

    assert error.value.failure.code is DurableFailureCode.INVALID_INPUT
    assert store.load(RUN_ID).generation == generation


def test_unauthenticated_external_actual_cannot_settle(
    store: DurableBudgetStore,
) -> None:
    reservation = build_resource_reservation(
        reservation_id="reservation-external",
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
    store.reserve(
        RUN_ID,
        operation_id="reserve-external",
        permit_id="permit-external",
        reservation=reservation,
        lineage=_lineage(suffix="external"),
    )
    store.start(
        RUN_ID,
        operation_id="start-external",
        permit_id="permit-external",
    )
    generation = store.load(RUN_ID).generation

    with pytest.raises(DurableBudgetError) as error:
        store.settle(
            RUN_ID,
            operation_id="settle-external",
            settlement_id="settlement-external",
            permit_id="permit-external",
            actual=ResourceAmountsV1(
                provider_attempts=1,
                external_cost_micro_usd=10,
                provider_output_bytes=80,
                run_bytes=80,
                concurrency_slots=1,
            ),
            status=SettlementStatus.SUCCEEDED,
            result_sha256=HASH,
            effect_evidence_sha256=HASH,
        )

    assert error.value.failure.code is DurableFailureCode.INVALID_INPUT
    state = store.load(RUN_ID)
    assert state.generation == generation
    assert state.settlements == ()
    assert state.active_permits[0].status.value == "started"


def test_cross_run_budget_state_replay_is_rejected(
    store: DurableBudgetStore,
) -> None:
    state = store.load(RUN_ID)
    request = RevisionPublishRequestV1(
        run_id="Run-cross-target",
        transaction_id="txn-" + "9" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id=DURABLE_BUDGET_PRODUCER_ID,
        producer_version=DURABLE_BUDGET_PRODUCER_VERSION,
        artifacts=(_artifact(state),),
    )

    with pytest.raises(RunStorageError) as error:
        store.revisions.publish(request)

    assert error.value.failure.code is RunStorageFailureCode.INVALID_STORAGE_INPUT
    assert not (store.revisions.runs_root / "Run-cross-target").exists()


def test_semantically_forged_hash_chained_successor_fails_closed_on_load(
    store: DurableBudgetStore,
) -> None:
    state = store.load(RUN_ID)
    operation_id = "forged-nonsettlement-usage"
    result_sha256 = canonical_durable_sha256({"forged": True})
    operation = IdempotencyRecordV1(
        operation_id=operation_id,
        kind=OperationKind.REQUEST_CANCEL,
        request_sha256=canonical_durable_sha256({"forged": operation_id}),
        outcome=OperationOutcome.APPLIED,
        result_sha256=result_sha256,
        subject_id="permit-never-created",
    )
    event = DurableEventV1(
        ordinal=1,
        kind=operation.kind,
        operation_id=operation.operation_id,
        subject_id=operation.subject_id,
        event_sha256=canonical_durable_sha256(
            {
                "ordinal": 1,
                "kind": operation.kind.value,
                "operation_id": operation.operation_id,
                "subject_id": operation.subject_id,
                "result_sha256": result_sha256,
            }
        ),
    )
    forged = DurableBudgetStateV1.model_validate(
        {
            **state.model_dump(mode="python"),
            "generation": 2,
            "previous_state_sha256": state.canonical_sha256,
            "usage": DurableUsageV1(run_bytes=1),
            "operations": (*state.operations, operation),
            "events": (*state.events, event),
        },
        strict=True,
    )
    current = store.revisions.read_current(RUN_ID)
    store.revisions.publish(
        RevisionPublishRequestV1(
            run_id=RUN_ID,
            transaction_id="txn-" + "7" * 32,
            proposed_revision=2,
            expected_revision=1,
            expected_manifest_sha256=current.manifest_sha256,
            expected_pointer_sha256=current.current_pointer_sha256,
            created_at=NOW,
            producer_id=DURABLE_BUDGET_PRODUCER_ID,
            producer_version=DURABLE_BUDGET_PRODUCER_VERSION,
            artifacts=(_artifact(forged),),
        )
    )

    with pytest.raises(DurableBudgetError) as error:
        store.load(RUN_ID)
    assert error.value.failure.code is DurableFailureCode.RECONCILIATION_REQUIRED
    assert error.value.failure.reconciliation_required is True
