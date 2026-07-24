from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
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
    run_lock_key_sha256,
)
from poker_deliberation.storage.revision_lock import (
    LockBusyError,
    acquire_authority,
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
    with TemporaryDirectory(prefix="p2c-", dir=parent) as directory:
        yield Path(directory)


def _artifact() -> RevisionArtifactV1:
    data = canonical_json_bytes(
        CaseInput(case_id="case-concurrency", kind="claim", raw_text="concurrency")
    )
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


def _root(tmp_path: Path) -> tuple[Path, Path]:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    revision = tmp_path / "revision"
    initialize_revision_root(
        RootInitializationRequestV1(
            revision_root=revision,
            legacy_runs_root=legacy,
            root_id="root-" + "c" * 32,
            initialized_at=NOW,
            producer_id="poker-deliberation",
            producer_version="0.1.0",
        )
    )
    return revision, legacy


def _first_request() -> RevisionPublishRequestV1:
    return RevisionPublishRequestV1(
        run_id="Run-concurrency",
        transaction_id="txn-" + "d" * 32,
        proposed_revision=1,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(_artifact(),),
    )


def test_same_process_separate_store_instances_overlap_as_run_locked(
    short_tmp: Path,
) -> None:
    revision, legacy = _root(short_tmp)
    acquired = threading.Event()
    release = threading.Event()
    result: list[str] = []

    def inject(hook: str) -> None:
        if hook == "authority.after_kernel_acquire" and not acquired.is_set():
            acquired.set()
            assert release.wait(timeout=10)

    first_store = RunRevisionStore(revision, legacy, fault_injector=inject)
    second_store = RunRevisionStore(revision, legacy)

    def publish_first() -> None:
        result.append(first_store.publish(_first_request()).outcome_kind)

    thread = threading.Thread(target=publish_first)
    thread.start()
    assert acquired.wait(timeout=10)
    with pytest.raises(RunStorageError) as loser:
        second_store.publish(_first_request())
    assert loser.value.failure.code is RunStorageFailureCode.RUN_LOCKED
    assert loser.value.failure.automatic_retry_allowed is False
    release.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert result == ["published"]
    assert second_store.publish(_first_request()).outcome_kind == "current_committed"


def test_reader_observes_complete_old_then_complete_new_pointer(short_tmp: Path) -> None:
    revision, legacy = _root(short_tmp)
    base_store = RunRevisionStore(revision, legacy)
    base_store.publish(_first_request())
    first = base_store.read_current("Run-concurrency")
    before_replace = threading.Event()
    allow_replace = threading.Event()

    def inject(hook: str) -> None:
        if hook == "current.before_replace" and not before_replace.is_set():
            before_replace.set()
            assert allow_replace.wait(timeout=10)

    second_request = RevisionPublishRequestV1(
        run_id="Run-concurrency",
        transaction_id="txn-" + "e" * 32,
        proposed_revision=2,
        expected_revision=1,
        expected_manifest_sha256=first.manifest_sha256,
        expected_pointer_sha256=first.current_pointer_sha256,
        created_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
        artifacts=(_artifact(),),
    )
    writer = RunRevisionStore(revision, legacy, fault_injector=inject)
    result: list[str] = []
    thread = threading.Thread(
        target=lambda: result.append(writer.publish(second_request).outcome_kind)
    )
    thread.start()
    assert before_replace.wait(timeout=10)
    assert base_store.read_current("Run-concurrency").current_revision == 1
    allow_replace.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert result == ["published"]
    assert base_store.read_current("Run-concurrency").current_revision == 2


def test_root_initializers_serialize_and_different_root_id_is_refused(
    short_tmp: Path,
) -> None:
    legacy = short_tmp / "legacy"
    legacy.mkdir()
    revision = short_tmp / "revision"
    first_request = RootInitializationRequestV1(
        revision_root=revision,
        legacy_runs_root=legacy,
        root_id="root-" + "1" * 32,
        initialized_at=NOW,
        producer_id="poker-deliberation",
        producer_version="0.1.0",
    )
    second_request = first_request.model_copy(
        update={"root_id": "root-" + "2" * 32},
    )
    acquired = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    def inject(hook: str) -> None:
        if hook == "authority.after_kernel_acquire" and not acquired.is_set():
            acquired.set()
            assert release.wait(timeout=10)

    thread = threading.Thread(
        target=lambda: outcomes.append(
            initialize_revision_root(first_request, fault_injector=inject).outcome_kind
        )
    )
    thread.start()
    assert acquired.wait(timeout=10)
    with pytest.raises(RunStorageError) as overlap:
        initialize_revision_root(second_request)
    assert overlap.value.failure.code is RunStorageFailureCode.RUN_LOCKED
    release.set()
    thread.join(timeout=10)
    assert outcomes == ["initialized"]

    with pytest.raises(RunStorageError) as different:
        initialize_revision_root(second_request)
    assert different.value.failure.code is RunStorageFailureCode.RUN_NAMESPACE_CONFLICT
    assert initialize_revision_root(first_request).outcome_kind == "already_initialized"


def test_first_use_case_aliases_share_one_authority(short_tmp: Path) -> None:
    revision, legacy = _root(short_tmp)
    acquired = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    def inject(hook: str) -> None:
        if hook == "authority.after_kernel_acquire" and not acquired.is_set():
            acquired.set()
            assert release.wait(timeout=10)

    first_store = RunRevisionStore(revision, legacy, fault_injector=inject)
    alias_store = RunRevisionStore(revision, legacy)
    first_request = _first_request()
    alias_request = first_request.model_copy(update={"run_id": "run-CONCURRENCY"})
    thread = threading.Thread(
        target=lambda: outcomes.append(first_store.publish(first_request).outcome_kind)
    )
    thread.start()
    assert acquired.wait(timeout=10)
    with pytest.raises(RunStorageError) as overlap:
        alias_store.publish(alias_request)
    assert overlap.value.failure.code is RunStorageFailureCode.RUN_LOCKED
    release.set()
    thread.join(timeout=10)
    assert outcomes == ["published"]

    with pytest.raises(RunStorageError) as alias:
        alias_store.publish(alias_request)
    assert alias.value.failure.code is RunStorageFailureCode.RUN_NAMESPACE_CONFLICT


def test_subprocess_holds_the_same_kernel_authority(short_tmp: Path) -> None:
    revision, _legacy = _root(short_tmp)
    authority = revision / ".revision-init.authority.lock"
    ready = short_tmp / "child.ready"
    release = short_tmp / "child.release"
    script = (
        "import sys,time\n"
        "from pathlib import Path\n"
        "from poker_deliberation.storage.revision_lock import acquire_authority\n"
        "lease=acquire_authority(Path(sys.argv[1]),registry_keys=('child',),bootstrap=False)\n"
        "Path(sys.argv[2]).write_text('ready',encoding='utf-8')\n"
        "while not Path(sys.argv[3]).exists(): time.sleep(0.01)\n"
        "lease.release()\n"
    )
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(ROOT / "src"), existing_pythonpath) if part
    )
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(authority), str(ready), str(release)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), process.stderr.read() if process.stderr is not None else ""
        with pytest.raises(LockBusyError):
            acquire_authority(
                authority,
                registry_keys=("parent",),
                bootstrap=False,
            )
    finally:
        release.write_text("release", encoding="utf-8")
        process.wait(timeout=10)
    assert process.returncode == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows byte-range adapter contract")
def test_windows_adapter_locks_exactly_byte_zero_through_one(
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import msvcrt

    authority = short_tmp / "authority.lock"
    authority.write_bytes(b"\0")
    calls: list[tuple[int, int, int]] = []

    def recording_lock(fd: int, mode: int, length: int) -> None:
        calls.append((os.lseek(fd, 0, os.SEEK_CUR), mode, length))

    monkeypatch.setattr(msvcrt, "locking", recording_lock)
    lease = acquire_authority(
        authority,
        registry_keys=("windows-byte-range", run_lock_key_sha256("Run-byte-range")),
        bootstrap=False,
    )
    lease.release()

    assert calls == [
        (0, msvcrt.LK_NBLCK, 1),
        (0, msvcrt.LK_UNLCK, 1),
    ]
