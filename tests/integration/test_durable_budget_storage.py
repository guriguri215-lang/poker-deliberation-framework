"""Integration tests for durable budget storage."""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.budgets.durable_models import (
    DURABLE_BUDGET_ARTIFACT_SCHEMA,
    DURABLE_BUDGET_PRODUCER_ID,
    DURABLE_BUDGET_PRODUCER_VERSION,
    DurableBudgetPolicyV1,
    DurableBudgetStateV1,
)
from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.storage.revision_canonical import (
    classification_evidence_sha256,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    LocalDataBindingV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    RootInitializationRequestV1,
    RunStorageError,
    RunStorageFailureCode,
)
from poker_deliberation.storage.revision_store import (
    RunRevisionStore,
    initialize_revision_root,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2b-", dir=parent) as directory:
        yield Path(directory)


def _state(
    generation: int,
    *,
    previous_state_sha256: str | None = None,
) -> DurableBudgetStateV1:
    policy = DurableBudgetPolicyV1()
    return DurableBudgetStateV1(
        run_id="Run-budget",
        generation=generation,
        previous_state_sha256=previous_state_sha256,
        policy=policy,
        policy_sha256=policy.policy_sha256,
        activation_sha256=policy.activation_sha256,
        active_runtime_remaining_ns=policy.base_policy.runtime_limit_ns,
    )


def _artifact(state: DurableBudgetStateV1) -> RevisionArtifactV1:
    evidence = ClassificationEvidence(restricted_secret_check_completed=True)
    local = LocalDataBindingV1(
        logical_name="budget_state.json",
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.DEFAULT_INTERNAL,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
    )
    return RevisionArtifactV1(
        logical_name="budget_state.json",
        media_type="application/json",
        artifact_schema_version=DURABLE_BUDGET_ARTIFACT_SCHEMA,
        serialization="poker-run-storage-json-v1",
        exact_bytes=state.canonical_bytes(),
        required=True,
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.DEFAULT_INTERNAL,
        classification_evidence=evidence,
        policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
        origin_kind="budget_state",
        provenance_bindings=(local,),
    )


def _request(
    state: DurableBudgetStateV1,
    digit: str,
    *,
    expected_revision: int | None = None,
    expected_manifest_sha256: str | None = None,
    expected_pointer_sha256: str | None = None,
) -> RevisionPublishRequestV1:
    return RevisionPublishRequestV1(
        run_id=state.run_id,
        transaction_id="txn-" + digit * 32,
        proposed_revision=state.generation,
        expected_revision=expected_revision,
        expected_manifest_sha256=expected_manifest_sha256,
        expected_pointer_sha256=expected_pointer_sha256,
        created_at=NOW,
        producer_id=DURABLE_BUDGET_PRODUCER_ID,
        producer_version=DURABLE_BUDGET_PRODUCER_VERSION,
        artifacts=(_artifact(state),),
    )


def _store(short_tmp: Path) -> RunRevisionStore:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "durable-revisions"
    initialize_revision_root(
        RootInitializationRequestV1(
            revision_root=revision,
            legacy_runs_root=legacy,
            root_id="root-" + "b" * 32,
            initialized_at=NOW,
            producer_id=DURABLE_BUDGET_PRODUCER_ID,
            producer_version=DURABLE_BUDGET_PRODUCER_VERSION,
        )
    )
    return RunRevisionStore(
        revision,
        legacy,
        producer_id=DURABLE_BUDGET_PRODUCER_ID,
        producer_version=DURABLE_BUDGET_PRODUCER_VERSION,
    )


def test_dedicated_budget_artifact_publishes_and_reads_verified_history(
    short_tmp: Path,
) -> None:
    store = _store(short_tmp)
    first_state = _state(1)
    first_request = _request(first_state, "1")
    store.publish(first_request)
    current = store.read_current(first_state.run_id)
    second_state = _state(
        2,
        previous_state_sha256=first_state.canonical_sha256,
    )
    second_request = _request(
        second_state,
        "2",
        expected_revision=1,
        expected_manifest_sha256=current.manifest_sha256,
        expected_pointer_sha256=current.current_pointer_sha256,
    )
    store.publish(second_request)

    history = store._read_structural_artifact_history(
        first_state.run_id,
        "budget_state.json",
        artifact_schema_version=DURABLE_BUDGET_ARTIFACT_SCHEMA,
    )

    assert history.verification_kind == "structural_artifact_history"
    assert [entry.revision for entry in history.revisions] == [2, 1]
    assert [
        DurableBudgetStateV1.model_validate_json(entry.exact_bytes).generation
        for entry in history.revisions
    ] == [2, 1]
    assert store.publish(first_request).outcome_kind == "historical_committed"
    assert store.publish(second_request).outcome_kind == "current_committed"


def test_budget_artifact_requires_exclusive_dedicated_producer(short_tmp: Path) -> None:
    store = _store(short_tmp)
    state = _state(1)
    wrong = RevisionPublishRequestV1.model_validate(
        {
            **_request(state, "3").model_dump(mode="python"),
            "producer_id": "poker-deliberation",
        }
    )
    with pytest.raises(RunStorageError) as error:
        store.publish(wrong)
    assert error.value.failure.code is RunStorageFailureCode.INVALID_STORAGE_INPUT
    assert not (store.runs_root / state.run_id).exists()


def test_structural_history_rejects_payload_tamper(short_tmp: Path) -> None:
    store = _store(short_tmp)
    state = _state(1)
    request = _request(state, "4")
    store.publish(request)
    payload = (
        store.runs_root
        / state.run_id
        / ".revision-store"
        / "revisions"
        / f"r1-{request.transaction_id}"
        / "payload"
        / "budget_state.json"
    )
    payload.write_bytes(b"{}")

    with pytest.raises(RunStorageError) as error:
        store._read_structural_artifact_history(
            state.run_id,
            "budget_state.json",
            artifact_schema_version=DURABLE_BUDGET_ARTIFACT_SCHEMA,
        )
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
