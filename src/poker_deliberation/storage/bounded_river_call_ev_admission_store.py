"""Append-only pre-execution commitment for the P3-030C integration contract."""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from poker_deliberation.bounded_river_call_ev_models import (
    ADMISSION_RECORD_HASH_DOMAIN,
    BOUNDED_RIVER_CALL_EV_ADMISSION_RECORD_SCHEMA,
    BoundedRiverCallEvAdmissionRecordV1,
    BoundedRiverCallEvBindingV1,
)
from poker_deliberation.range_equity_models import canonical_domain_sha256
from poker_deliberation.storage.directory_durability import sync_directory
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
    run_lock_key_sha256,
    validate_run_id,
)
from poker_deliberation.storage.revision_lock import verify_directory, verify_regular_single_link

_JOURNAL_DIRECTORY = "bounded-river-call-ev-admissions"


def _journal_path(revision_root: Path, run_id: str) -> Path:
    validate_run_id(run_id)
    root = Path(os.path.abspath(revision_root))
    verify_directory(root)
    control = root / ".revision-control"
    verify_directory(control)
    journal = control / _JOURNAL_DIRECTORY
    with suppress(FileExistsError):
        journal.mkdir()
    verify_directory(journal)
    sync_directory(control, hook="bounded_river_call_ev_admission.journal_parent")
    sync_directory(root, hook="bounded_river_call_ev_admission.control_parent")
    return journal / f"{run_lock_key_sha256(run_id)}.json"


def build_bounded_river_call_ev_admission_record(
    run_id: str,
    binding: BoundedRiverCallEvBindingV1,
) -> BoundedRiverCallEvAdmissionRecordV1:
    validate_run_id(run_id)
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "record_schema": BOUNDED_RIVER_CALL_EV_ADMISSION_RECORD_SCHEMA,
        "run_id": run_id,
        "binding_sha256": binding.binding_sha256,
        "candidate_sha256": binding.candidate_sha256,
        "confirmation_sha256": binding.confirmation_sha256,
        "range_binding_sha256": binding.range_binding_sha256,
        "tool_plan": binding.ordered_tools,
    }
    return BoundedRiverCallEvAdmissionRecordV1.model_validate(
        {
            **payload,
            "record_sha256": canonical_domain_sha256(
                ADMISSION_RECORD_HASH_DOMAIN,
                payload,
            ),
        },
        strict=True,
    )


def commit_bounded_river_call_ev_admission_record(
    revision_root: Path,
    run_id: str,
    binding: BoundedRiverCallEvBindingV1,
    *,
    maximum_bytes: int,
) -> BoundedRiverCallEvAdmissionRecordV1:
    record = build_bounded_river_call_ev_admission_record(run_id, binding)
    data = canonical_json_bytes(record)
    if len(data) > maximum_bytes:
        raise CanonicalStorageError("bounded river call-EV admission record exceeds byte limit")
    path = _journal_path(revision_root, run_id)
    stream = None
    try:
        stream = path.open("xb")
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    finally:
        if stream is not None:
            stream.close()
    verify_regular_single_link(path)
    if path.read_bytes() != data:
        raise CanonicalStorageError("bounded river call-EV admission record reread mismatch")
    sync_directory(path.parent, hook="bounded_river_call_ev_admission.record_parent")
    return record


def read_bounded_river_call_ev_admission_record(
    revision_root: Path,
    run_id: str,
    *,
    maximum_bytes: int,
) -> BoundedRiverCallEvAdmissionRecordV1 | None:
    validate_run_id(run_id)
    root = Path(os.path.abspath(revision_root))
    verify_directory(root)
    control = root / ".revision-control"
    verify_directory(control)
    journal = control / _JOURNAL_DIRECTORY
    if not journal.exists():
        return None
    verify_directory(journal)
    path = journal / f"{run_lock_key_sha256(run_id)}.json"
    if not path.exists():
        return None
    info = verify_regular_single_link(path)
    if info.st_size > maximum_bytes:
        raise CanonicalStorageError("bounded river call-EV admission record exceeds byte limit")
    data = path.read_bytes()
    try:
        record = BoundedRiverCallEvAdmissionRecordV1.model_validate_json(data, strict=True)
    except ValueError as exc:
        raise CanonicalStorageError("bounded river call-EV admission record is invalid") from exc
    if record.run_id != run_id or canonical_json_bytes(record) != data:
        raise CanonicalStorageError("bounded river call-EV admission record is noncanonical")
    return record


def verify_bounded_river_call_ev_admission_record(
    record: BoundedRiverCallEvAdmissionRecordV1,
    binding: BoundedRiverCallEvBindingV1,
) -> None:
    if (
        record.binding_sha256 != binding.binding_sha256
        or record.candidate_sha256 != binding.candidate_sha256
        or record.confirmation_sha256 != binding.confirmation_sha256
        or record.range_binding_sha256 != binding.range_binding_sha256
        or record.tool_plan != binding.ordered_tools
    ):
        raise CanonicalStorageError("bounded river call-EV admission commitment mismatch")


__all__ = [
    "build_bounded_river_call_ev_admission_record",
    "commit_bounded_river_call_ev_admission_record",
    "read_bounded_river_call_ev_admission_record",
    "verify_bounded_river_call_ev_admission_record",
]
