from __future__ import annotations

import os
import shutil
import subprocess
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
    CanonicalStorageError,
    build_inventory,
    canonical_json_bytes,
    classification_evidence_sha256,
    domain_sha256,
    parse_canonical_json,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    LocalDataBindingV1,
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
)

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def short_tmp() -> Generator[Path, None, None]:
    parent = ROOT / "tmp"
    parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="p2a-", dir=parent) as directory:
        yield Path(directory)


def _artifact(
    *,
    data: bytes | None = None,
    source: ClassificationSource = ClassificationSource.SOURCE_INHERITANCE,
    evidence: ClassificationEvidence | None = None,
) -> RevisionArtifactV1:
    exact = data or canonical_json_bytes(
        CaseInput(case_id="case-security", kind="claim", raw_text="security")
    )
    classification_evidence = evidence or ClassificationEvidence(
        source_classifications=(ContextClassification.PUBLIC,),
        restricted_secret_check_completed=True,
    )
    local = LocalDataBindingV1(
        logical_name="input.json",
        classification=ContextClassification.INTERNAL,
        classification_source=source,
        classification_evidence=classification_evidence,
        classification_evidence_sha256=classification_evidence_sha256(classification_evidence),
    )
    user_source = SourceBindingV1(
        source_id="user-input",
        source_kind="user_input",
        trust_kind="trusted_user_input",
        source_sha256=domain_sha256("poker-user-input-source-v1", exact),
    )
    return RevisionArtifactV1(
        logical_name="input.json",
        media_type="application/json",
        artifact_schema_version="poker-case-input-artifact-v1",
        serialization="poker-run-storage-json-v1",
        exact_bytes=exact,
        required=True,
        classification=ContextClassification.INTERNAL,
        classification_source=source,
        classification_evidence=classification_evidence,
        policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
        origin_kind="case_input",
        provenance_bindings=(local, user_source),
    )


def _request(artifact: RevisionArtifactV1) -> RevisionPublishRequestV1:
    return RevisionPublishRequestV1(
        run_id="Run-security",
        transaction_id="txn-" + "5" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(artifact,),
    )


def _initialized_store(tmp_path: Path) -> tuple[RunRevisionStore, Path]:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    revision = tmp_path / "revision"
    initialize_revision_root(
        RootInitializationRequestV1(
            revision_root=revision,
            legacy_runs_root=legacy,
            root_id="root-" + "4" * 32,
            initialized_at=NOW,
            producer_id="poker-deliberation",
            producer_version="0.1.0",
        )
    )
    return RunRevisionStore(revision, legacy), revision


def _create_directory_link(target: Path, link: Path) -> None:
    if os.name != "nt":
        os.symlink(target, link, target_is_directory=True)
        return
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (
        f"junction creation failed: {completed.stdout!r} {completed.stderr!r}"
    )


def test_noncanonical_valid_json_and_default_internal_classification_fail_preflight() -> None:
    noncanonical = _artifact(data=b'{"kind":"claim", "case_id":"case-security"}')
    with pytest.raises(CanonicalStorageError):
        build_inventory(_request(noncanonical), max_artifact_bytes=1_000_000)

    default_evidence = ClassificationEvidence(restricted_secret_check_completed=True)
    default_internal = _artifact(
        source=ClassificationSource.DEFAULT_INTERNAL,
        evidence=default_evidence,
    )
    with pytest.raises(CanonicalStorageError):
        build_inventory(_request(default_internal), max_artifact_bytes=1_000_000)


def test_flat_v1_case_alias_is_refused_before_run_namespace_mutation(short_tmp: Path) -> None:
    store, revision = _initialized_store(short_tmp)
    legacy_alias = store.legacy_runs_root / "run-SECURITY"
    legacy_alias.mkdir()
    (legacy_alias / ".poker-deliberation-run").write_bytes(b"v1\n")

    with pytest.raises(RunStorageError) as error:
        store.publish(_request(_artifact()))

    assert error.value.failure.code is RunStorageFailureCode.LEGACY_RUN_UNVERIFIED
    assert not (revision / "runs" / "Run-security").exists()


def test_manifest_tamper_is_never_called_a_verified_partial_chain(short_tmp: Path) -> None:
    store, revision = _initialized_store(short_tmp)
    request = _request(_artifact())
    store.publish(request)
    manifest = (
        revision
        / "runs"
        / request.run_id
        / ".revision-store"
        / "revisions"
        / f"r1-{request.transaction_id}"
        / "manifest.json"
    )
    data = manifest.read_bytes()
    manifest.write_bytes(data[:-1] + (b" " if data[-1:] != b" " else b"\t"))

    with pytest.raises(RunStorageError) as error:
        store.read_current(request.run_id)
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
    assert error.value.failure.reconciliation_required is True


def test_constructor_rejects_overlapping_roots_without_creating_revision_state(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "runs"
    legacy.mkdir()
    nested = legacy / "revision"

    with pytest.raises(CanonicalStorageError):
        RunRevisionStore(nested, legacy)
    assert not nested.exists()


def test_unknown_existing_namespace_is_refused_without_adoption(short_tmp: Path) -> None:
    store, revision = _initialized_store(short_tmp)
    run = revision / "runs" / "Run-security"
    run.mkdir()
    unexpected = run / "foreign"
    unexpected.mkdir()

    with pytest.raises(RunStorageError) as error:
        store.publish(_request(_artifact()))
    assert error.value.failure.code is RunStorageFailureCode.RUN_NAMESPACE_CONFLICT
    assert unexpected.is_dir()
    assert not (run / ".revision-store").exists()


def test_hardlinked_payload_and_cross_run_pointer_swap_fail_closed(
    short_tmp: Path,
) -> None:
    store, revision = _initialized_store(short_tmp)
    first_request = _request(_artifact())
    store.publish(first_request)
    first_payload = (
        revision
        / "runs"
        / first_request.run_id
        / ".revision-store"
        / "revisions"
        / f"r1-{first_request.transaction_id}"
        / "payload"
        / "input.json"
    )
    outside_link = short_tmp / "payload-link"
    os.link(first_payload, outside_link)
    with pytest.raises(RunStorageError) as hardlink:
        store.read_current(first_request.run_id)
    assert hardlink.value.failure.code is RunStorageFailureCode.RUN_CORRUPT

    outside_link.unlink()
    second_request = first_request.model_copy(
        update={
            "run_id": "Run-security-b",
            "transaction_id": "txn-" + "6" * 32,
        }
    )
    store.publish(second_request)
    first_current = revision / "runs" / first_request.run_id / ".revision-store" / "current.json"
    second_current = revision / "runs" / second_request.run_id / ".revision-store" / "current.json"
    first_current.write_bytes(second_current.read_bytes())
    with pytest.raises(RunStorageError) as swapped:
        store.read_current(first_request.run_id)
    assert swapped.value.failure.code is RunStorageFailureCode.RUN_CORRUPT


def test_untrusted_producer_identity_fails_before_authority_mutation(
    short_tmp: Path,
) -> None:
    store, revision = _initialized_store(short_tmp)
    request = _request(_artifact()).model_copy(
        update={"producer_id": "caller-controlled"},
    )

    with pytest.raises(RunStorageError) as error:
        store.publish(request)
    assert error.value.failure.code is RunStorageFailureCode.INVALID_STORAGE_INPUT
    assert not list((revision / ".revision-control" / "locks").iterdir())


def test_reader_rejects_a_linked_run_ancestor_instead_of_following_it(
    short_tmp: Path,
) -> None:
    store, revision = _initialized_store(short_tmp)
    request = _request(_artifact())
    store.publish(request)
    run = revision / "runs" / request.run_id
    outside = short_tmp / "outside-run"
    run.rename(outside)
    _create_directory_link(outside, run)

    with pytest.raises(RunStorageError) as error:
        store.read_current(request.run_id)
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT


def test_root_initialization_maps_a_linked_root_to_the_link_failure_code(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy-root-link"
    legacy.mkdir()
    outside = short_tmp / "outside-root-link"
    outside.mkdir()
    revision = short_tmp / "revision-root-link"
    _create_directory_link(outside, revision)
    request = RootInitializationRequestV1(
        revision_root=revision,
        legacy_runs_root=legacy,
        root_id="root-" + "e" * 32,
        initialized_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
    )
    with pytest.raises(RunStorageError) as error:
        initialize_revision_root(request)
    assert error.value.failure.code is RunStorageFailureCode.LINK_OR_REPARSE_DETECTED


def test_unknown_entry_inside_partial_staging_fails_closed(
    short_tmp: Path,
) -> None:
    store, revision = _initialized_store(short_tmp)
    request = _request(_artifact())

    def inject(hook: str) -> None:
        if hook == "transaction.before_open":
            raise OSError("leave partial staging")

    with pytest.raises(RunStorageError):
        RunRevisionStore(revision, store.legacy_runs_root, fault_injector=inject).publish(request)
    staging = (
        revision
        / "runs"
        / request.run_id
        / ".revision-store"
        / "transactions"
        / request.transaction_id
    )
    (staging / "unlisted.control").write_bytes(b"x")

    different = request.model_copy(update={"transaction_id": "txn-" + "7" * 32})
    with pytest.raises(RunStorageError) as error:
        store.publish(different)
    assert error.value.failure.code is RunStorageFailureCode.RUN_NAMESPACE_CONFLICT


def test_unknown_pointer_version_and_hash_algorithm_fail_closed(
    short_tmp: Path,
) -> None:
    store, revision = _initialized_store(short_tmp)
    request = _request(_artifact())
    store.publish(request)
    current = revision / "runs" / request.run_id / ".revision-store" / "current.json"
    value = parse_canonical_json(current.read_bytes())
    assert isinstance(value, dict)
    value["schema_version"] = "2.0.0"
    value["hash_algorithm"] = "sha512"
    current.write_bytes(canonical_json_bytes(value))

    with pytest.raises(RunStorageError) as error:
        store.read_current(request.run_id)
    assert error.value.failure.code is RunStorageFailureCode.RUN_CORRUPT
    assert error.value.failure.reconciliation_required is True


def test_missing_inventory_payload_and_replaced_root_identity_fail_closed(
    short_tmp: Path,
) -> None:
    store, revision = _initialized_store(short_tmp)
    request = _request(_artifact())
    store.publish(request)
    payload = (
        revision
        / "runs"
        / request.run_id
        / ".revision-store"
        / "revisions"
        / f"r1-{request.transaction_id}"
        / "payload"
        / "input.json"
    )
    payload.unlink()
    with pytest.raises(RunStorageError) as missing:
        store.read_current(request.run_id)
    assert missing.value.failure.code is RunStorageFailureCode.RUN_CORRUPT

    replacement = short_tmp / "replacement"
    shutil.copytree(revision, replacement)
    displaced = short_tmp / "displaced"
    revision.rename(displaced)
    replacement.rename(revision)
    with pytest.raises(RunStorageError) as replaced:
        store.read_current(request.run_id)
    assert replaced.value.failure.code is RunStorageFailureCode.ROOT_INITIALIZATION_INCOMPLETE
