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
    parse_canonical_json,
    parse_canonical_model,
    run_id_sha256,
    run_lock_key_sha256,
    sha256_bytes,
    transaction_sha256,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    LocalDataBindingV1,
    LockMetadataV1,
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
    verified_second = RunRevisionStore(revision, legacy).read_current("Run-storage")

    assert second.outcome_kind == "published"
    assert verified_second.current_revision == 2
    assert [item.revision for item in verified_second.reachable_history] == [2, 1]
    assert store.publish(first_request).outcome_kind == "historical_committed"
    assert store.publish(second_request).outcome_kind == "current_committed"
    historical_claim = RecoveryClaimRequestV1(
        run_id_sha256=first.run_id_sha256,
        transaction_id=first_request.transaction_id,
        transaction_sha256=first.transaction_sha256,
        observed_pointer_sha256=verified_second.current_pointer_sha256,
        orphan_form="unreferenced_revision",
        claim_id="claim-" + "8" * 32,
        claimant_token="owner-" + "9" * 32,
        claimed_at=NOW,
    )
    with pytest.raises(RunStorageError) as historical:
        store.claim_orphan(first_request.run_id, historical_claim)
    assert historical.value.failure.code is RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT
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


def test_same_transaction_with_different_digest_is_idempotency_conflict(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    request = _request("9", revision=1, artifact=_input_artifact("first bytes"))
    store.publish(request)
    before = store.read_current(request.run_id)

    conflicting = _request(
        "9",
        revision=1,
        artifact=_input_artifact("different bytes"),
    )
    with pytest.raises(RunStorageError) as error:
        store.publish(conflicting)
    assert error.value.failure.code is RunStorageFailureCode.IDEMPOTENCY_CONFLICT
    after = store.read_current(request.run_id)
    assert after.current_pointer_sha256 == before.current_pointer_sha256
    assert after.reachable_history == before.reachable_history


def test_existing_exact_claim_remains_idempotent_after_current_advances(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    orphan_request = _request("a", revision=1, artifact=_input_artifact("orphan"))

    def leave_unreferenced(hook: str) -> None:
        if hook == "current.before_replace":
            raise OSError("leave unreferenced revision")

    with pytest.raises(RunStorageError):
        RunRevisionStore(
            revision,
            legacy,
            fault_injector=leave_unreferenced,
        ).publish(orphan_request)

    store = RunRevisionStore(revision, legacy)
    orphan = store.inspect_orphans(orphan_request.run_id).revision_orphans[0]
    assert orphan.transaction_sha256 is not None
    claim_request = RecoveryClaimRequestV1(
        run_id_sha256=run_id_sha256(orphan_request.run_id),
        transaction_id=orphan_request.transaction_id,
        transaction_sha256=orphan.transaction_sha256,
        observed_pointer_sha256=None,
        orphan_form="unreferenced_revision",
        claim_id="claim-" + "a" * 32,
        claimant_token="owner-" + "a" * 32,
        claimed_at=NOW,
    )
    claim = store.claim_orphan(orphan_request.run_id, claim_request)

    current_request = _request(
        "b",
        revision=1,
        artifact=_input_artifact("current"),
    )
    store.publish(current_request)
    assert store.read_current(current_request.run_id).current_revision == 1
    assert (
        RunRevisionStore(revision, legacy).claim_orphan(
            orphan_request.run_id,
            claim_request,
        )
        == claim
    )


def test_corrupt_advisory_metadata_is_replaced_only_during_a_new_locked_publish(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    first_request = _request("a", revision=1, artifact=_input_artifact())
    store.publish(first_request)
    first = store.read_current(first_request.run_id)
    metadata = (
        revision
        / ".revision-control"
        / "locks"
        / f"{run_lock_key_sha256(first_request.run_id)}.metadata.json"
    )
    metadata.write_bytes(b"{")

    second_request = _request(
        "b",
        revision=2,
        artifact=_input_artifact(),
        expected_revision=1,
        expected_manifest=first.manifest_sha256,
        expected_pointer=first.current_pointer_sha256,
    )
    assert store.publish(second_request).outcome_kind == "published"
    parsed = parse_canonical_model(metadata.read_bytes(), LockMetadataV1)
    assert parsed.transaction_id == second_request.transaction_id


def test_lineage_self_backedge_attempt_fails_closed(short_tmp: Path) -> None:
    legacy = short_tmp / "legacy-cycle"
    legacy.mkdir()
    revision = short_tmp / "revision-cycle"
    initialize_revision_root(_root_request(revision, legacy))
    store = RunRevisionStore(revision, legacy)
    artifact = _input_artifact()
    first_request = _request("c", revision=1, artifact=artifact)
    store.publish(first_request)
    first = store.read_current(first_request.run_id)
    second_request = _request(
        "d",
        revision=2,
        artifact=artifact,
        expected_revision=1,
        expected_manifest=first.manifest_sha256,
        expected_pointer=first.current_pointer_sha256,
    )
    store.publish(second_request)
    control = revision / "runs" / first_request.run_id / ".revision-store"
    revision_dir = control / "revisions" / f"r2-{second_request.transaction_id}"
    transaction_path = revision_dir / "transaction.json"
    manifest_path = revision_dir / "manifest.json"
    current_path = control / "current.json"
    old_manifest_sha = sha256_bytes(manifest_path.read_bytes())
    transaction = parse_canonical_json(transaction_path.read_bytes())
    manifest = parse_canonical_json(manifest_path.read_bytes())
    pointer = parse_canonical_json(current_path.read_bytes())
    assert isinstance(transaction, dict)
    assert isinstance(manifest, dict)
    assert isinstance(pointer, dict)
    transaction["expected_manifest_sha256"] = old_manifest_sha
    transaction_projection = dict(transaction)
    transaction_projection.pop("transaction_sha256")
    transaction["transaction_sha256"] = transaction_sha256(transaction_projection)
    manifest["previous_manifest_sha256"] = old_manifest_sha
    manifest["transaction_sha256"] = transaction["transaction_sha256"]
    transaction_path.write_bytes(canonical_json_bytes(transaction))
    manifest_bytes = canonical_json_bytes(manifest)
    manifest_path.write_bytes(manifest_bytes)
    pointer["transaction_sha256"] = transaction["transaction_sha256"]
    pointer["manifest_sha256"] = sha256_bytes(manifest_bytes)
    current_path.write_bytes(canonical_json_bytes(pointer))

    with pytest.raises(RunStorageError) as error:
        store.read_current(first_request.run_id)
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
    assert error.value.failure.reconciliation_required is True
