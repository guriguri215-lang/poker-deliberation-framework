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
    legacy_root_identity_sha256,
    run_id_sha256,
    run_lock_key_sha256,
    sha256_bytes,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    LocalDataBindingV1,
    OwnershipMarkerV1,
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
    reconcile_revision_root,
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2f-", dir=parent) as directory:
        yield Path(directory)


def _artifact() -> RevisionArtifactV1:
    data = canonical_json_bytes(CaseInput(case_id="case-fault", kind="claim", raw_text="fault"))
    evidence = ClassificationEvidence(
        source_classifications=(ContextClassification.PUBLIC,),
        restricted_secret_check_completed=True,
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
        provenance_bindings=(
            LocalDataBindingV1(
                logical_name="input.json",
                classification=ContextClassification.INTERNAL,
                classification_source=ClassificationSource.SOURCE_INHERITANCE,
                classification_evidence=evidence,
                classification_evidence_sha256=classification_evidence_sha256(evidence),
            ),
            SourceBindingV1(
                source_id="user-input",
                source_kind="user_input",
                trust_kind="trusted_user_input",
                source_sha256=domain_sha256("poker-user-input-source-v1", data),
            ),
        ),
    )


def _request(digit: str) -> RevisionPublishRequestV1:
    return RevisionPublishRequestV1(
        run_id="Run-fault",
        transaction_id="txn-" + digit * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(_artifact(),),
    )


def _roots(tmp_path: Path) -> tuple[Path, Path, RootInitializationRequestV1]:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    revision = tmp_path / "revision"
    request = RootInitializationRequestV1(
        revision_root=revision,
        legacy_runs_root=legacy,
        root_id="root-" + "6" * 32,
        initialized_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
    )
    initialize_revision_root(request)
    return revision, legacy, request


def test_transaction_boundary_fault_leaves_typed_staging_orphan_and_no_current(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(hook: str) -> None:
        if hook == "transaction.before_open":
            raise OSError("synthetic boundary")

    store = RunRevisionStore(revision, legacy, fault_injector=inject)
    with pytest.raises(RunStorageError) as error:
        store.publish(_request("7"))

    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_WRITE_FAILED
    assert error.value.failure.filesystem_effect == "staging_orphan"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is True
    assert not (revision / "runs" / "Run-fault" / ".revision-store" / "current.json").exists()
    inspection = RunRevisionStore(revision, legacy).inspect_orphans("Run-fault")
    assert inspection.staging_orphans[0].verification_state == "path_only"


def test_pre_replace_fault_preserves_verified_unreferenced_revision_for_metadata_claim(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    fired = False

    def inject(hook: str) -> None:
        nonlocal fired
        if hook == "current.before_replace" and not fired:
            fired = True
            raise OSError("synthetic before replace")

    request = _request("8")
    store = RunRevisionStore(revision, legacy, fault_injector=inject)
    with pytest.raises(RunStorageError) as error:
        store.publish(request)
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED
    assert error.value.failure.filesystem_effect == "unreferenced_revision"

    recovery_store = RunRevisionStore(revision, legacy)
    inspection = recovery_store.inspect_orphans(request.run_id)
    orphan = inspection.revision_orphans[0]
    assert orphan.verification_state == "manifest_verified"
    assert orphan.transaction_sha256 is not None
    claim_request = RecoveryClaimRequestV1(
        run_id_sha256=run_id_sha256(request.run_id),
        transaction_id=request.transaction_id,
        transaction_sha256=orphan.transaction_sha256,
        observed_pointer_sha256=None,
        orphan_form="unreferenced_revision",
        claim_id="claim-" + "9" * 32,
        claimant_token="owner-" + "a" * 32,
        claimed_at=NOW,
    )
    claim = recovery_store.claim_orphan(request.run_id, claim_request)
    assert recovery_store.claim_orphan(request.run_id, claim_request) == claim
    assert not list(revision.rglob("completion.json"))


def test_post_replace_fault_reports_current_may_have_advanced_but_reader_reconciles(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    fired = False

    def inject(hook: str) -> None:
        nonlocal fired
        if hook == "current.after_replace" and not fired:
            fired = True
            raise OSError("synthetic after replace")

    request = _request("b")
    store = RunRevisionStore(revision, legacy, fault_injector=inject)
    with pytest.raises(RunStorageError) as error:
        store.publish(request)

    assert error.value.failure.code is RunStorageFailureCode.EFFECT_UNKNOWN
    assert error.value.failure.filesystem_effect == "current_advanced"
    assert error.value.failure.reconciliation_required is True
    assert RunRevisionStore(revision, legacy).read_current(request.run_id).current_revision == 1


def test_zero_length_root_authority_is_repaired_in_place(short_tmp: Path) -> None:
    revision, _legacy, root_request = _roots(short_tmp)
    authority = revision / ".revision-init.authority.lock"
    identity = (authority.stat().st_dev, authority.stat().st_ino)
    authority.write_bytes(b"")

    outcome = initialize_revision_root(root_request)

    assert outcome.outcome_kind == "already_initialized"
    assert authority.read_bytes() == b"\0"
    assert (authority.stat().st_dev, authority.stat().st_ino) == identity


def test_zero_length_run_authority_is_repaired_in_place(short_tmp: Path) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request = _request("c")
    authority = (
        revision
        / ".revision-control"
        / "locks"
        / f"{run_lock_key_sha256(request.run_id)}.authority.lock"
    )
    authority.write_bytes(b"")
    identity = (authority.stat().st_dev, authority.stat().st_ino)

    outcome = RunRevisionStore(revision, legacy).publish(request)

    assert outcome.outcome_kind == "published"
    assert authority.read_bytes() == b"\0"
    assert (authority.stat().st_dev, authority.stat().st_ino) == identity


def test_exact_partial_root_is_reconciled_only_with_expected_marker(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    request = RootInitializationRequestV1(
        revision_root=revision,
        legacy_runs_root=legacy,
        root_id="root-" + "d" * 32,
        initialized_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
    )
    fired = False

    def inject(hook: str) -> None:
        nonlocal fired
        if hook == "root.before_runs_rename" and not fired:
            fired = True
            raise OSError("synthetic partial initialization")

    with pytest.raises(RunStorageError):
        initialize_revision_root(request, fault_injector=inject)
    inspection = inspect_root_initialization(revision, legacy)
    assert inspection.status == "incomplete"
    marker = OwnershipMarkerV1(
        root_id=request.root_id,
        legacy_runs_root_identity_sha256=legacy_root_identity_sha256(legacy),
        initialized_at=request.initialized_at,
        producer_id=request.producer_id,
        producer_version=request.producer_version,
    )
    marker_sha = sha256_bytes(canonical_json_bytes(marker))

    with pytest.raises(RunStorageError) as wrong:
        reconcile_revision_root(
            request,
            expected_ownership_marker_sha256="0" * 64,
        )
    assert wrong.value.failure.code is RunStorageFailureCode.RUN_NAMESPACE_CONFLICT
    assert inspect_root_initialization(revision, legacy).status == "incomplete"

    outcome = reconcile_revision_root(
        request,
        expected_ownership_marker_sha256=marker_sha,
    )
    assert outcome.outcome_kind == "initialized"
    assert inspect_root_initialization(revision, legacy).status == "initialized"
