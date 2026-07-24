from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.schemas import CaseInput
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes,
    classification_evidence_sha256,
    domain_sha256,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    LocalDataBindingV1,
    RecoveryClaimRequestV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    RootInitializationRequestV1,
    RunStorageError,
    RunStorageFailureCode,
    SourceBindingV1,
)
from poker_deliberation.storage.revision_store import (
    RunRevisionStore,
    initialize_revision_root,
    inspect_root_initialization,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2i-", dir=parent) as directory:
        yield Path(directory)


def _root_request(revision: Path, legacy: Path) -> RootInitializationRequestV1:
    return RootInitializationRequestV1(
        revision_root=revision,
        legacy_runs_root=legacy,
        root_id="root-" + "1" * 32,
        initialized_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
    )


def _input_artifact(text: str = "exact payload") -> RevisionArtifactV1:
    data = canonical_json_bytes(CaseInput(case_id="case-storage", kind="claim", raw_text=text))
    evidence = ClassificationEvidence(
        source_classifications=(ContextClassification.PUBLIC,),
        restricted_secret_check_completed=True,
    )
    local = LocalDataBindingV1(
        logical_name="input.json",
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
    )
    source = SourceBindingV1(
        source_id="user-input",
        source_kind="user_input",
        trust_kind="trusted_user_input",
        source_sha256=domain_sha256("poker-user-input-source-v1", data),
    )
    return RevisionArtifactV1(
        logical_name="input.json",
        media_type="application/json",
        artifact_schema_version="poker-case-input-artifact-v1",
        serialization="poker-run-storage-json-v1",
        exact_bytes=data,
        required=True,
        classification=ContextClassification.INTERNAL,
        classification_source=ClassificationSource.SOURCE_INHERITANCE,
        classification_evidence=evidence,
        policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
        origin_kind="case_input",
        provenance_bindings=(local, source),
    )


def _request(
    transaction_digit: str,
    *,
    revision: int,
    artifact: RevisionArtifactV1,
    expected_revision: int | None = None,
    expected_manifest: str | None = None,
    expected_pointer: str | None = None,
) -> RevisionPublishRequestV1:
    return RevisionPublishRequestV1(
        run_id="Run-storage",
        transaction_id="txn-" + transaction_digit * 32,
        proposed_revision=revision,
        expected_revision=expected_revision,
        expected_manifest_sha256=expected_manifest,
        expected_pointer_sha256=expected_pointer,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(artifact,),
    )


def test_explicit_root_init_publish_two_revisions_read_and_replay(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "dedicated revisions"
    root_request = _root_request(revision, legacy)

    assert inspect_root_initialization(revision, legacy).status == "uninitialized"
    assert not revision.exists()
    assert initialize_revision_root(root_request).outcome_kind == "initialized"
    assert initialize_revision_root(root_request).outcome_kind == "already_initialized"
    assert inspect_root_initialization(revision, legacy).status == "initialized"

    artifact = _input_artifact()
    first_request = _request("2", revision=1, artifact=artifact)
    store = RunRevisionStore(revision, legacy)
    first = store.publish(first_request)
    verified_first = store.read_current("Run-storage")

    assert first.outcome_kind == "published"
    assert verified_first.current_revision == 1
    payload_path = (
        revision
        / "runs"
        / "Run-storage"
        / ".revision-store"
        / "revisions"
        / f"r1-{first_request.transaction_id}"
        / "payload"
        / "input.json"
    )
    assert payload_path.read_bytes() == artifact.exact_bytes

    second_request = _request(
        "3",
        revision=2,
        artifact=artifact,
        expected_revision=1,
        expected_manifest=verified_first.manifest_sha256,
        expected_pointer=verified_first.current_pointer_sha256,
    )
    second = store.publish(second_request)
    monkeypatch.chdir(short_tmp)
    verified_second = store.read_current("Run-storage")

    assert second.outcome_kind == "published"
    assert verified_second.current_revision == 2
    assert [item.revision for item in verified_second.reachable_history] == [2, 1]
    assert store.publish(first_request).outcome_kind == "historical_committed"
    assert store.publish(second_request).outcome_kind == "current_committed"
    assert not list(revision.rglob("completion.json"))


def test_stale_cas_and_recreated_bound_legacy_root_fail_closed(short_tmp: Path) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    artifact = _input_artifact()
    first_request = _request("2", revision=1, artifact=artifact)
    store.publish(first_request)

    stale = _request(
        "3",
        revision=2,
        artifact=artifact,
        expected_revision=1,
        expected_manifest="a" * 64,
        expected_pointer="b" * 64,
    )
    with pytest.raises(RunStorageError) as conflict:
        store.publish(stale)
    assert conflict.value.failure.code is RunStorageFailureCode.RUN_CONFLICT
    assert store.read_current("Run-storage").current_revision == 1

    legacy.rmdir()
    legacy.mkdir()
    with pytest.raises(RunStorageError) as identity:
        store.read_current("Run-storage")
    assert identity.value.failure.code is RunStorageFailureCode.ROOT_INITIALIZATION_INCOMPLETE


def test_run_quota_accepts_exact_physical_peak_and_rejects_one_over(
    short_tmp: Path,
) -> None:
    def provision(name: str) -> tuple[Path, Path]:
        base = short_tmp / name
        base.mkdir()
        legacy = base / "legacy"
        legacy.mkdir()
        revision = base / "revision"
        initialize_revision_root(_root_request(revision, legacy))
        return revision, legacy

    def fixed_owner(prefix: str) -> str:
        return f"{prefix}-" + "d" * 32
    request = _request("e", revision=1, artifact=_input_artifact())
    reference_revision, reference_legacy = provision("reference")
    reference_store = RunRevisionStore(
        reference_revision,
        reference_legacy,
        id_factory=fixed_owner,
        clock=lambda: NOW,
    )
    reference_store.publish(request)
    exact_peak = reference_store._run_physical_bytes(request.run_id)

    equal_revision, equal_legacy = provision("equal")
    equal_store = RunRevisionStore(
        equal_revision,
        equal_legacy,
        max_run_bytes=exact_peak,
        id_factory=fixed_owner,
        clock=lambda: NOW,
    )
    assert equal_store.publish(request).outcome_kind == "published"

    over_revision, over_legacy = provision("one-over")
    over_store = RunRevisionStore(
        over_revision,
        over_legacy,
        max_run_bytes=exact_peak - 1,
        id_factory=fixed_owner,
        clock=lambda: NOW,
    )
    with pytest.raises(RunStorageError) as error:
        over_store.publish(request)
    assert error.value.failure.code is RunStorageFailureCode.RUN_BUDGET_EXCEEDED
    assert not (over_revision / "runs" / request.run_id).exists()


def test_reachable_revision_cannot_be_claimed(short_tmp: Path) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    request = _request("f", revision=1, artifact=_input_artifact())
    outcome = store.publish(request)
    claim = RecoveryClaimRequestV1(
        run_id_sha256=outcome.run_id_sha256,
        transaction_id=request.transaction_id,
        transaction_sha256=outcome.transaction_sha256,
        observed_pointer_sha256=outcome.pointer_sha256,
        orphan_form="unreferenced_revision",
        claim_id="claim-" + "1" * 32,
        claimant_token="owner-" + "2" * 32,
        claimed_at=NOW,
    )

    with pytest.raises(RunStorageError) as error:
        store.claim_orphan(request.run_id, claim)
    assert error.value.failure.code is RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT
    assert not list(revision.rglob(f"{request.transaction_id}.json"))
