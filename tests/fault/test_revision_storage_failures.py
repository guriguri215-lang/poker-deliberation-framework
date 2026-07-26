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
    parse_canonical_json,
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
    assert error.value.failure.filesystem_effect == "current_replace_attempted"
    assert error.value.failure.domain_effect == "current_may_have_advanced"
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


@pytest.mark.parametrize(
    ("hook", "code", "stage", "effect"),
    [
        (
            "metadata.before_flush",
            RunStorageFailureCode.DURABILITY_UNCONFIRMED,
            "lock_metadata",
            "control_only",
        ),
        (
            "transaction.before_flush",
            RunStorageFailureCode.DURABILITY_UNCONFIRMED,
            "transaction",
            "staging_orphan",
        ),
        (
            "pointer.before_open",
            RunStorageFailureCode.TRANSACTION_WRITE_FAILED,
            "pointer",
            "unreferenced_revision",
        ),
    ],
)
def test_write_boundary_failure_code_stage_and_effect_are_exact(
    short_tmp: Path,
    hook: str,
    code: RunStorageFailureCode,
    stage: str,
    effect: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic exact boundary")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("e"))

    assert error.value.failure.code is code
    assert error.value.failure.stage == stage
    assert error.value.failure.filesystem_effect == effect
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.automatic_retry_allowed is False


@pytest.mark.parametrize(
    ("hook", "code", "reconciliation"),
    [
        (
            "metadata.before_replace",
            RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED,
            False,
        ),
        ("metadata.after_replace", RunStorageFailureCode.EFFECT_UNKNOWN, True),
    ],
)
def test_metadata_replace_boundary_distinguishes_before_and_after_invocation(
    short_tmp: Path,
    hook: str,
    code: RunStorageFailureCode,
    reconciliation: bool,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic metadata replace")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("d"))
    assert error.value.failure.code is code
    assert error.value.failure.stage == "lock_metadata"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.reconciliation_required is reconciliation


def test_partial_metadata_temp_is_recognized_and_a_fresh_owner_can_continue(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request = _request("e")

    def inject(hook: str) -> None:
        if hook == "metadata.after_open":
            raise OSError("synthetic partial metadata")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(request)
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_WRITE_FAILED
    assert error.value.failure.stage == "lock_metadata"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.reconciliation_required is False
    assert RunRevisionStore(revision, legacy).publish(request).outcome_kind == "published"


@pytest.mark.parametrize(
    ("hook", "code"),
    [
        ("authority.before_write", RunStorageFailureCode.TRANSACTION_WRITE_FAILED),
        ("authority.before_flush", RunStorageFailureCode.DURABILITY_UNCONFIRMED),
        ("authority.before_reread", RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED),
    ],
)
def test_run_authority_boundaries_have_closed_failure_codes(
    short_tmp: Path,
    hook: str,
    code: RunStorageFailureCode,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic authority boundary")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("1"))
    assert error.value.failure.code is code
    assert error.value.failure.stage == "lock_bootstrap"
    assert error.value.failure.domain_effect == "current_unchanged"


@pytest.mark.parametrize(
    "hook",
    [
        *(f"namespace.before_mkdir.{index}" for index in range(6)),
        *(f"namespace.after_mkdir.{index}" for index in range(6)),
    ],
)
def test_each_namespace_parent_fault_is_reusable_without_current(
    short_tmp: Path,
    hook: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic namespace boundary")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("2"))
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_WRITE_FAILED
    assert error.value.failure.stage == "namespace_bootstrap"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.reconciliation_required is False
    assert not (revision / "runs" / "Run-fault" / ".revision-store" / "current.json").exists()


def test_lock_release_fault_preserves_the_prior_staging_effect(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(hook: str) -> None:
        if hook in {"transaction.before_open", "authority.before_kernel_release"}:
            raise OSError(f"synthetic {hook}")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("3"))
    assert error.value.failure.code is RunStorageFailureCode.EFFECT_UNKNOWN
    assert error.value.failure.stage == "lock_release"
    assert error.value.failure.filesystem_effect == "staging_orphan"
    assert error.value.failure.domain_effect == "current_unchanged"


def test_lock_release_fault_preserves_the_prior_replace_uncertainty(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(hook: str) -> None:
        if hook in {"current.after_replace", "authority.before_kernel_release"}:
            raise OSError(f"synthetic {hook}")

    request = _request("c")
    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(request)
    assert error.value.failure.code is RunStorageFailureCode.EFFECT_UNKNOWN
    assert error.value.failure.stage == "lock_release"
    assert error.value.failure.filesystem_effect == "current_replace_attempted"
    assert error.value.failure.domain_effect == "current_may_have_advanced"
    assert RunRevisionStore(revision, legacy).read_current(request.run_id).current_revision == 1


def test_confirmed_current_with_corrupt_reconciliation_is_run_corrupt(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request = _request("d")
    manifest = (
        revision
        / "runs"
        / request.run_id
        / ".revision-store"
        / "revisions"
        / f"r1-{request.transaction_id}"
        / "manifest.json"
    )

    def inject(hook: str) -> None:
        if hook == "current.after_replace":
            manifest.write_bytes(b"{")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(request)
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
    assert error.value.failure.stage == "reconciliation"
    assert error.value.failure.filesystem_effect == "current_advanced"
    assert error.value.failure.domain_effect == "current_advanced"
    assert error.value.failure.reconciliation_required is True


def test_reconciliation_does_not_confirm_a_replaced_old_pointer(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    first = RunRevisionStore(revision, legacy).publish(_request("1"))
    current = revision / "runs" / "Run-fault" / ".revision-store" / "current.json"
    first_pointer_bytes = current.read_bytes()
    second = _request("2").model_copy(
        update={
            "proposed_revision": 2,
            "expected_revision": 1,
            "expected_manifest_sha256": first.manifest_sha256,
            "expected_pointer_sha256": first.pointer_sha256,
        }
    )

    def inject(hook: str) -> None:
        if hook == "current.after_replace":
            current.write_bytes(first_pointer_bytes)

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(second)
    assert error.value.failure.code is RunStorageFailureCode.EFFECT_UNKNOWN
    assert error.value.failure.stage == "reconciliation"
    assert error.value.failure.observed_revision is None
    assert error.value.failure.filesystem_effect == "current_replace_attempted"
    assert error.value.failure.domain_effect == "current_may_have_advanced"
    assert error.value.failure.reconciliation_required is True
    assert RunRevisionStore(revision, legacy).read_current(second.run_id).current_revision == 1


def _unreferenced_claim(
    revision: Path,
    legacy: Path,
    *,
    digit: str,
) -> tuple[RevisionPublishRequestV1, RecoveryClaimRequestV1]:
    request = _request(digit)

    def stop_before_current(hook: str) -> None:
        if hook == "current.before_replace":
            raise OSError("leave unreferenced revision")

    with pytest.raises(RunStorageError):
        RunRevisionStore(revision, legacy, fault_injector=stop_before_current).publish(request)
    inspection = RunRevisionStore(revision, legacy).inspect_orphans(request.run_id)
    orphan = inspection.revision_orphans[0]
    assert orphan.transaction_sha256 is not None
    return request, RecoveryClaimRequestV1(
        run_id_sha256=run_id_sha256(request.run_id),
        transaction_id=request.transaction_id,
        transaction_sha256=orphan.transaction_sha256,
        observed_pointer_sha256=None,
        orphan_form="unreferenced_revision",
        claim_id="claim-" + digit * 32,
        claimant_token="owner-" + digit * 32,
        claimed_at=NOW,
    )


@pytest.mark.parametrize(
    ("hook", "code"),
    [
        ("recovery_claim.before_finalize", RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED),
        ("recovery_claim.after_finalize", RunStorageFailureCode.EFFECT_UNKNOWN),
        (
            "recovery_claim.before_final_reread",
            RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE,
        ),
        (
            "recovery_claim.before_post_reconcile",
            RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE,
        ),
    ],
)
def test_recovery_finalization_boundaries_are_typed(
    short_tmp: Path,
    hook: str,
    code: RunStorageFailureCode,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request, claim_request = _unreferenced_claim(revision, legacy, digit="4")

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic recovery boundary")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).claim_orphan(
            request.run_id,
            claim_request,
        )
    assert error.value.failure.code is code
    assert error.value.failure.stage == "recovery_claim"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.reconciliation_required is True
    if hook == "recovery_claim.after_finalize":
        retried = RunRevisionStore(revision, legacy).claim_orphan(
            request.run_id,
            claim_request,
        )
        assert retried.transaction_id == claim_request.transaction_id
        assert retried.claim_id == claim_request.claim_id
        claim_root = revision / "runs" / request.run_id / ".revision-store" / "recovery-claims"
        final = claim_root / f"{request.transaction_id}.json"
        assert final.stat().st_nlink == 1
        assert not list((claim_root / ".tmp").iterdir())


def test_partial_claim_temp_has_exact_write_then_incomplete_retry_effect(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request, claim_request = _unreferenced_claim(revision, legacy, digit="7")

    def inject(hook: str) -> None:
        if hook == "recovery_claim.after_open":
            raise OSError("synthetic partial claim temp")

    with pytest.raises(RunStorageError) as first:
        RunRevisionStore(revision, legacy, fault_injector=inject).claim_orphan(
            request.run_id,
            claim_request,
        )
    assert first.value.failure.code is RunStorageFailureCode.TRANSACTION_WRITE_FAILED
    assert first.value.failure.filesystem_effect == "control_only"
    assert first.value.failure.reconciliation_required is True

    with pytest.raises(RunStorageError) as retry:
        RunRevisionStore(revision, legacy).claim_orphan(
            request.run_id,
            claim_request,
        )
    assert retry.value.failure.code is RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE
    assert retry.value.failure.filesystem_effect == "control_only"
    assert retry.value.failure.reconciliation_required is True


@pytest.mark.parametrize(
    ("hook", "code"),
    [
        (
            "immutable_payload.input.json.before_hash",
            RunStorageFailureCode.ARTIFACT_HASH_MISMATCH,
        ),
        (
            "immutable_payload.input.json.before_schema",
            RunStorageFailureCode.ARTIFACT_SCHEMA_ERROR,
        ),
        (
            "immutable_control.before_manifest_reread",
            RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
        ),
    ],
)
def test_immutable_reread_boundaries_keep_specific_failure_codes(
    short_tmp: Path,
    hook: str,
    code: RunStorageFailureCode,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic immutable verification")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("5"))
    assert error.value.failure.code is code
    assert error.value.failure.stage == (
        "manifest" if code is RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED else "payload"
    )
    assert error.value.failure.filesystem_effect == "staging_orphan"
    assert error.value.failure.reconciliation_required is True


@pytest.mark.parametrize(
    "hook",
    [
        "immutable_control.manifest.before_correlation",
        "immutable_control.manifest.before_inventory_digest",
        "immutable_control.manifest.before_provenance_heads",
        "immutable_control.manifest.before_inventory_paths",
        "immutable_control.manifest.before_inventory_replay",
    ],
)
def test_immutable_inventory_verification_faults_are_typed(
    short_tmp: Path,
    hook: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic immutable inventory verification")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("e"))
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
    assert error.value.failure.stage == "manifest"
    assert error.value.failure.filesystem_effect == "staging_orphan"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is True


@pytest.mark.parametrize(
    "hook",
    [
        "locked_admission.before_byte_count",
        "locked_admission.after_byte_count",
        "locked_admission.before_peak_check",
        "locked_admission.after_peak_check",
    ],
)
def test_publish_locked_byte_admission_faults_are_mutation_free(
    short_tmp: Path,
    hook: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic locked byte admission")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("f"))
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
    assert error.value.failure.stage == "locked_admission"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is False
    assert not (revision / "runs" / "Run-fault").exists()


@pytest.mark.parametrize(
    ("orphan_form", "leave_hook", "expected_effect"),
    [
        ("staging", "payload.input.json.before_open", "staging_orphan"),
        ("revision", "current.before_replace", "unreferenced_revision"),
    ],
)
def test_publish_byte_admission_fault_preserves_existing_orphan_effect(
    short_tmp: Path,
    orphan_form: str,
    leave_hook: str,
    expected_effect: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request = _request("a")

    def leave_orphan(hook: str) -> None:
        if hook == leave_hook:
            raise OSError(f"leave {orphan_form} orphan")

    with pytest.raises(RunStorageError):
        RunRevisionStore(revision, legacy, fault_injector=leave_orphan).publish(request)

    def inject(hook: str) -> None:
        if hook == "locked_admission.before_byte_count":
            raise OSError("synthetic orphan retry admission")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(request)
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
    assert error.value.failure.stage == "locked_admission"
    assert error.value.failure.filesystem_effect == expected_effect
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is True


@pytest.mark.parametrize(
    "hook",
    [
        "replay_orphan.transaction.before_reread",
        "replay_orphan.transaction.before_hash",
        "replay_orphan.transaction.before_schema",
    ],
)
def test_replay_orphan_read_fault_is_not_downgraded_to_an_outcome(
    short_tmp: Path,
    hook: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request = _request("b")

    def leave_staging(observed: str) -> None:
        if observed == "payload.input.json.before_open":
            raise OSError("leave descriptor-complete staging")

    with pytest.raises(RunStorageError):
        RunRevisionStore(revision, legacy, fault_injector=leave_staging).publish(request)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic replay orphan read")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(request)
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
    assert error.value.failure.stage == "initial_read"
    assert error.value.failure.filesystem_effect == "staging_orphan"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is True


@pytest.mark.parametrize(
    "hook",
    [
        "recovery_claim.admission.before_byte_count",
        "recovery_claim.admission.after_byte_count",
        "recovery_claim.admission.before_peak_check",
        "recovery_claim.admission.after_peak_check",
    ],
)
def test_recovery_claim_locked_byte_admission_faults_are_mutation_free(
    short_tmp: Path,
    hook: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request, claim_request = _unreferenced_claim(revision, legacy, digit="d")

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic recovery claim byte admission")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).claim_orphan(
            request.run_id,
            claim_request,
        )
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
    assert error.value.failure.stage == "recovery_claim"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is False
    claim_root = revision / "runs" / request.run_id / ".revision-store" / "recovery-claims"
    assert not (claim_root / f"{request.transaction_id}.json").exists()
    assert not list((claim_root / ".tmp").iterdir())


@pytest.mark.parametrize(
    "hook",
    [
        "recovery_claim.admission.before_byte_count",
        "recovery_claim.admission.after_byte_count",
        "recovery_claim.admission.before_peak_check",
        "recovery_claim.admission.after_peak_check",
    ],
)
def test_recovery_claim_admission_fault_preserves_existing_temp_reconciliation(
    short_tmp: Path,
    hook: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request, claim_request = _unreferenced_claim(revision, legacy, digit="c")

    def stop_before_finalize(observed: str) -> None:
        if observed == "recovery_claim.before_finalize":
            raise OSError("leave verified claim temp")

    with pytest.raises(RunStorageError):
        RunRevisionStore(
            revision,
            legacy,
            fault_injector=stop_before_finalize,
        ).claim_orphan(request.run_id, claim_request)

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic retry admission fault")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).claim_orphan(
            request.run_id,
            claim_request,
        )
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
    assert error.value.failure.stage == "recovery_claim"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is True
    claim_temp = (
        revision
        / "runs"
        / request.run_id
        / ".revision-store"
        / "recovery-claims"
        / ".tmp"
        / f"{request.transaction_id}.{claim_request.claim_id}.json"
    )
    assert claim_temp.is_file()
    assert claim_temp.stat().st_nlink == 1


def test_recovery_claim_admission_fault_requires_reconciliation_for_existing_final(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request, claim_request = _unreferenced_claim(revision, legacy, digit="3")
    store = RunRevisionStore(revision, legacy)
    store.claim_orphan(request.run_id, claim_request)

    def inject(hook: str) -> None:
        if hook == "recovery_claim.admission.before_byte_count":
            raise OSError("synthetic existing-final admission fault")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).claim_orphan(
            request.run_id,
            claim_request,
        )
    assert error.value.failure.code is RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED
    assert error.value.failure.stage == "recovery_claim"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.reconciliation_required is True


def test_recovery_claim_conflict_preserves_existing_temp_reconciliation(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request, claim_request = _unreferenced_claim(revision, legacy, digit="5")

    def stop_before_finalize(hook: str) -> None:
        if hook == "recovery_claim.before_finalize":
            raise OSError("leave verified claim temp")

    with pytest.raises(RunStorageError):
        RunRevisionStore(
            revision,
            legacy,
            fault_injector=stop_before_finalize,
        ).claim_orphan(request.run_id, claim_request)

    RunRevisionStore(revision, legacy).publish(_request("6"))
    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy).claim_orphan(
            request.run_id,
            claim_request,
        )
    assert error.value.failure.code is RunStorageFailureCode.RECOVERY_CLAIM_CONFLICT
    assert error.value.failure.stage == "recovery_claim"
    assert error.value.failure.filesystem_effect == "control_only"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is True


@pytest.mark.parametrize(
    "hook",
    [
        "reconciliation.current.before_reread",
        "reconciliation.current.before_hash",
        "reconciliation.current.before_schema",
    ],
)
def test_reconciliation_fault_before_strict_current_verification_is_effect_unknown(
    short_tmp: Path,
    hook: str,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request = _request("0")

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic pre-reread boundary")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(request)
    assert error.value.failure.code is RunStorageFailureCode.EFFECT_UNKNOWN
    assert error.value.failure.stage == "reconciliation"
    assert error.value.failure.filesystem_effect == "current_replace_attempted"
    assert error.value.failure.domain_effect == "current_may_have_advanced"
    assert error.value.failure.reconciliation_required is True
    assert RunRevisionStore(revision, legacy).read_current(request.run_id).current_revision == 1


def test_orphan_transaction_read_fault_is_not_downgraded_to_path_only(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request = _request("9")

    def leave_staging(hook: str) -> None:
        if hook == "transaction.before_write":
            raise OSError("leave staging transaction")

    with pytest.raises(RunStorageError):
        RunRevisionStore(revision, legacy, fault_injector=leave_staging).publish(request)

    def inject(hook: str) -> None:
        if hook == f"orphan_inspect.transaction.{request.transaction_id}.before_reread":
            raise OSError("synthetic orphan transaction read")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).inspect_orphans(request.run_id)
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
    assert error.value.failure.stage == "orphan_inspect"
    assert error.value.failure.filesystem_effect == "none"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is True


def test_cross_run_staging_descriptor_is_only_path_verified(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    first = _request("8")
    second = first.model_copy(update={"run_id": "Run-other"})

    def leave_staging(hook: str) -> None:
        if hook == "payload.input.json.before_open":
            raise OSError("leave descriptor-complete staging")

    failing_store = RunRevisionStore(revision, legacy, fault_injector=leave_staging)
    with pytest.raises(RunStorageError):
        failing_store.publish(first)
    with pytest.raises(RunStorageError):
        failing_store.publish(second)

    first_transaction = (
        revision
        / "runs"
        / first.run_id
        / ".revision-store"
        / "transactions"
        / first.transaction_id
        / "transaction.json"
    )
    second_transaction = (
        revision
        / "runs"
        / second.run_id
        / ".revision-store"
        / "transactions"
        / second.transaction_id
        / "transaction.json"
    )
    first_transaction.write_bytes(second_transaction.read_bytes())

    inspection = RunRevisionStore(revision, legacy).inspect_orphans(first.run_id)
    assert inspection.staging_orphans[0].verification_state == "path_only"
    assert inspection.staging_orphans[0].transaction_sha256 is None


def test_unreferenced_revision_target_fault_is_not_downgraded_to_path_only(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request, _claim_request = _unreferenced_claim(revision, legacy, digit="2")
    target_hook = (
        f"orphan_inspect.revision.r1-{request.transaction_id}."
        "immutable_control.transaction.before_hash"
    )

    def inject(hook: str) -> None:
        if hook == target_hook:
            raise OSError("synthetic orphan revision target read")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).inspect_orphans(request.run_id)
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
    assert error.value.failure.stage == "orphan_inspect"
    assert error.value.failure.filesystem_effect == "none"
    assert error.value.failure.domain_effect == "current_unchanged"
    assert error.value.failure.reconciliation_required is True


def test_missing_payload_during_immutable_reread_is_artifact_missing(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    payload = (
        revision
        / "runs"
        / "Run-fault"
        / ".revision-store"
        / "transactions"
        / ("txn-" + "6" * 32)
        / "payload"
        / "input.json"
    )

    def inject(observed: str) -> None:
        if observed == "immutable_payload.input.json.before_reread":
            payload.unlink()

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).publish(_request("6"))
    assert error.value.failure.code is RunStorageFailureCode.ARTIFACT_MISSING
    assert error.value.failure.stage == "payload"
    assert error.value.failure.filesystem_effect == "staging_orphan"


@pytest.mark.parametrize(
    ("hook", "code"),
    [
        ("root.before_mkdir", RunStorageFailureCode.TRANSACTION_WRITE_FAILED),
        ("authority.before_write", RunStorageFailureCode.TRANSACTION_WRITE_FAILED),
        ("authority.before_flush", RunStorageFailureCode.DURABILITY_UNCONFIRMED),
        (
            "authority.before_reread",
            RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
        ),
        ("root.ownership.before_open", RunStorageFailureCode.TRANSACTION_WRITE_FAILED),
        ("root.ownership.before_flush", RunStorageFailureCode.DURABILITY_UNCONFIRMED),
        (
            "root.ownership.before_reread",
            RunStorageFailureCode.TRANSACTION_VERIFICATION_FAILED,
        ),
        ("root.control_temp.before_mkdir", RunStorageFailureCode.TRANSACTION_WRITE_FAILED),
        ("root.before_runs_rename", RunStorageFailureCode.TRANSACTION_PUBLISH_FAILED),
        ("root.runs_rename.after", RunStorageFailureCode.EFFECT_UNKNOWN),
        ("root.marker_replace.after", RunStorageFailureCode.EFFECT_UNKNOWN),
    ],
)
def test_root_initialization_boundaries_have_exact_typed_codes(
    short_tmp: Path,
    hook: str,
    code: RunStorageFailureCode,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    request = RootInitializationRequestV1(
        revision_root=revision,
        legacy_runs_root=legacy,
        root_id="root-" + "f" * 32,
        initialized_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
    )

    def inject(observed: str) -> None:
        if observed == hook:
            raise OSError("synthetic root boundary")

    with pytest.raises(RunStorageError) as error:
        initialize_revision_root(request, fault_injector=inject)
    assert error.value.failure.code is code
    assert error.value.failure.stage == "root_initialization"
    assert error.value.failure.domain_effect == "not_started"


def test_corrupt_existing_claim_digest_is_recovery_incomplete(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    request, claim_request = _unreferenced_claim(revision, legacy, digit="a")
    store = RunRevisionStore(revision, legacy)
    store.claim_orphan(request.run_id, claim_request)
    final = (
        revision
        / "runs"
        / request.run_id
        / ".revision-store"
        / "recovery-claims"
        / f"{request.transaction_id}.json"
    )
    value = parse_canonical_json(final.read_bytes())
    assert isinstance(value, dict)
    value["claim_sha256"] = "0" * 64
    final.write_bytes(canonical_json_bytes(value))

    with pytest.raises(RunStorageError) as error:
        store.claim_orphan(request.run_id, claim_request)
    assert error.value.failure.code is RunStorageFailureCode.RECOVERY_CLAIM_INCOMPLETE
    assert error.value.failure.reconciliation_required is True


def test_orphan_inspection_io_boundary_is_typed_and_mutation_free(
    short_tmp: Path,
) -> None:
    revision, legacy, _root_request = _roots(short_tmp)
    _unreferenced_claim(revision, legacy, digit="b")

    def inject(hook: str) -> None:
        if hook == "orphan_inspect.before_current_and_lineage":
            raise OSError("synthetic orphan inspection read")

    with pytest.raises(RunStorageError) as error:
        RunRevisionStore(revision, legacy, fault_injector=inject).inspect_orphans("Run-fault")
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
    assert error.value.failure.stage == "orphan_inspect"
    assert error.value.failure.filesystem_effect == "none"
    assert error.value.failure.domain_effect == "current_unchanged"
