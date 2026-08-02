"""Append-only independent budget-failure evidence for P3-030C."""

from __future__ import annotations

import hashlib
import os
from contextlib import suppress
from pathlib import Path

from poker_deliberation.bounded_river_call_ev_models import (
    BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_RECORD_SCHEMA,
    BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
    FAILURE_EVIDENCE_HASH_DOMAIN,
    TOOL_RESULT_HASH_DOMAIN,
    BoundedRiverCallEvAdmissionRecordV1,
    BoundedRiverCallEvBindingV1,
    BoundedRiverCallEvBudgetFailureEvidenceV1,
)
from poker_deliberation.budgets.contracts import (
    BudgetFailure,
    BudgetFailureCode,
    BudgetPolicyV2,
    canonical_json_utf8_size,
)
from poker_deliberation.phases.contracts import canonical_sha256 as phase_canonical_sha256
from poker_deliberation.phases.models import ToolExecutionBinding
from poker_deliberation.range_equity_models import canonical_domain_sha256
from poker_deliberation.schemas import ToolResult, ToolStatus
from poker_deliberation.storage.bounded_river_call_ev_admission_store import (
    read_bounded_river_call_ev_admission_record,
    verify_bounded_river_call_ev_admission_record,
)
from poker_deliberation.storage.directory_durability import sync_directory
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
    run_lock_key_sha256,
    validate_run_id,
)
from poker_deliberation.storage.revision_lock import verify_directory, verify_regular_single_link

_JOURNAL_DIRECTORY = "bounded-river-call-ev-budget-failures"


def _journal_directory(revision_root: Path, *, create: bool) -> Path | None:
    root = Path(os.path.abspath(revision_root))
    verify_directory(root)
    control = root / ".revision-control"
    verify_directory(control)
    journal = control / _JOURNAL_DIRECTORY
    if not journal.exists() and create:
        with suppress(FileExistsError):
            journal.mkdir()
        verify_directory(journal)
        sync_directory(control, hook="bounded_river_call_ev_failure.journal_parent")
        sync_directory(root, hook="bounded_river_call_ev_failure.control_parent")
    if not journal.exists():
        return None
    verify_directory(journal)
    return journal


def _record_path(revision_root: Path, run_id: str, tool_ordinal: int) -> Path:
    validate_run_id(run_id)
    if tool_ordinal < 0 or tool_ordinal >= len(BOUNDED_RIVER_CALL_EV_TOOL_ORDER):
        raise CanonicalStorageError("bounded river call-EV failure ordinal is invalid")
    journal = _journal_directory(revision_root, create=True)
    assert journal is not None
    return journal / f"{run_lock_key_sha256(run_id)}.{tool_ordinal}.json"


def _validate_failure_against_policy(
    failure: BudgetFailure,
    policy: BudgetPolicyV2,
    *,
    request_input: dict[str, object],
) -> None:
    expected: dict[BudgetFailureCode, tuple[str, int]] = {
        BudgetFailureCode.TOOL_INPUT_EXCEEDED: (
            "tool_input_bytes",
            policy.max_tool_input_bytes,
        ),
        BudgetFailureCode.TOOL_OUTPUT_EXCEEDED: (
            "tool_output_bytes",
            policy.max_tool_output_bytes,
        ),
        BudgetFailureCode.RUN_EXCEEDED: ("run_bytes", policy.max_run_bytes),
        BudgetFailureCode.RUNTIME_EXCEEDED: (
            "active_runtime_ns",
            policy.runtime_limit_ns,
        ),
    }
    if failure.code in expected:
        resource, limit = expected[failure.code]
        if (
            failure.resource != resource
            or failure.limit != limit
            or failure.observed is None
            or (
                failure.observed < limit
                if failure.code is BudgetFailureCode.RUNTIME_EXCEEDED
                else failure.observed <= limit
            )
        ):
            raise CanonicalStorageError(
                "bounded river call-EV budget failure differs from its policy"
            )
        if (
            failure.code is BudgetFailureCode.TOOL_INPUT_EXCEEDED
            and failure.observed != canonical_json_utf8_size(request_input)
        ):
            raise CanonicalStorageError(
                "bounded river call-EV input failure byte observation mismatch"
            )
        return
    if failure.code is BudgetFailureCode.CLOCK_ROLLBACK:
        if (
            failure.resource != "active_runtime_ns"
            or failure.limit is not None
            or failure.observed is None
            or failure.observed <= 0
        ):
            raise CanonicalStorageError("bounded river call-EV clock failure evidence is invalid")
        return
    if failure.code is BudgetFailureCode.USAGE_MALFORMED:
        if failure.limit is not None:
            raise CanonicalStorageError("bounded river call-EV malformed-usage evidence is invalid")
        return
    raise CanonicalStorageError("bounded river call-EV budget failure code is unsupported")


def build_bounded_river_call_ev_budget_failure_evidence(
    run_id: str,
    binding: BoundedRiverCallEvBindingV1,
    admission_record: BoundedRiverCallEvAdmissionRecordV1,
    execution: ToolExecutionBinding,
    failure: BudgetFailure,
    policy: BudgetPolicyV2,
    *,
    usage_observed_at_ns: int | None,
) -> BoundedRiverCallEvBudgetFailureEvidenceV1:
    validate_run_id(run_id)
    verify_bounded_river_call_ev_admission_record(admission_record, binding)
    if execution.run_id != run_id:
        raise CanonicalStorageError("bounded river call-EV failure run mismatch")
    result = execution.result
    request = execution.request
    if (
        execution.ordinal >= len(BOUNDED_RIVER_CALL_EV_TOOL_ORDER)
        or request.tool_name != BOUNDED_RIVER_CALL_EV_TOOL_ORDER[execution.ordinal]
        or result.tool_name != request.tool_name
        or result.input != request.input
        or result.status is not ToolStatus.FAILED
        or result.error != f"strict budget failure: {failure.code.value}"
        or execution.request_input_sha256 != phase_canonical_sha256(request.input)
    ):
        raise CanonicalStorageError("bounded river call-EV failure tool binding mismatch")
    _validate_failure_against_policy(failure, policy, request_input=request.input)
    result_bytes = canonical_json_bytes(result)
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "record_schema": BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_RECORD_SCHEMA,
        "run_id": run_id,
        "binding_sha256": binding.binding_sha256,
        "admission_record_sha256": admission_record.record_sha256,
        "phase_attempt_id": execution.phase_attempt_id,
        "tool_ordinal": execution.ordinal,
        "stage": request.tool_name,
        "tool_name": request.tool_name,
        "tool_request_id": request.request_id,
        "tool_request_sha256": phase_canonical_sha256(request),
        "request_input_sha256": execution.request_input_sha256,
        "result_id": result.result_id,
        "tool_result_sha256": canonical_domain_sha256(
            TOOL_RESULT_HASH_DOMAIN,
            result.model_dump(mode="json"),
        ),
        "tool_result_bytes_sha256": hashlib.sha256(result_bytes).hexdigest(),
        "budget_policy_sha256": policy.canonical_sha256,
        "failure_code": failure.code.value,
        "failure": failure.model_dump(mode="json"),
        "usage_observed_at_ns": usage_observed_at_ns,
    }
    strict_payload = {
        **payload,
        "failure_code": failure.code,
        "failure": failure,
    }
    return BoundedRiverCallEvBudgetFailureEvidenceV1.model_validate(
        {
            **strict_payload,
            "record_sha256": canonical_domain_sha256(
                FAILURE_EVIDENCE_HASH_DOMAIN,
                payload,
            ),
        },
        strict=True,
    )


def commit_bounded_river_call_ev_budget_failure_evidence(
    revision_root: Path,
    run_id: str,
    binding: BoundedRiverCallEvBindingV1,
    execution: ToolExecutionBinding,
    failure: BudgetFailure,
    policy: BudgetPolicyV2,
    *,
    usage_observed_at_ns: int | None,
    maximum_bytes: int,
) -> BoundedRiverCallEvBudgetFailureEvidenceV1:
    admission_record = read_bounded_river_call_ev_admission_record(
        revision_root,
        run_id,
        maximum_bytes=maximum_bytes,
    )
    if admission_record is None:
        raise CanonicalStorageError("bounded river call-EV failure lacks its admission record")
    record = build_bounded_river_call_ev_budget_failure_evidence(
        run_id,
        binding,
        admission_record,
        execution,
        failure,
        policy,
        usage_observed_at_ns=usage_observed_at_ns,
    )
    data = canonical_json_bytes(record)
    if len(data) > maximum_bytes:
        raise CanonicalStorageError("bounded river call-EV failure evidence exceeds byte limit")
    if read_bounded_river_call_ev_budget_failure_evidence(
        revision_root,
        run_id,
        maximum_bytes=maximum_bytes,
    ):
        raise CanonicalStorageError("bounded river call-EV failure evidence already exists")
    path = _record_path(revision_root, run_id, execution.ordinal)
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
        raise CanonicalStorageError("bounded river call-EV failure evidence reread mismatch")
    sync_directory(path.parent, hook="bounded_river_call_ev_failure.record_parent")
    return record


def read_bounded_river_call_ev_budget_failure_evidence(
    revision_root: Path,
    run_id: str,
    *,
    maximum_bytes: int,
) -> tuple[BoundedRiverCallEvBudgetFailureEvidenceV1, ...]:
    validate_run_id(run_id)
    journal = _journal_directory(revision_root, create=False)
    if journal is None:
        return ()
    prefix = f"{run_lock_key_sha256(run_id)}."
    records: list[BoundedRiverCallEvBudgetFailureEvidenceV1] = []
    for path in journal.iterdir():
        if not path.name.startswith(prefix):
            continue
        suffix = path.name[len(prefix) :]
        if not suffix.endswith(".json") or not suffix[:-5].isdigit():
            raise CanonicalStorageError(
                "bounded river call-EV failure evidence filename is invalid"
            )
        ordinal = int(suffix[:-5])
        if ordinal < 0 or ordinal >= len(BOUNDED_RIVER_CALL_EV_TOOL_ORDER):
            raise CanonicalStorageError("bounded river call-EV failure evidence ordinal is invalid")
        info = verify_regular_single_link(path)
        if info.st_size > maximum_bytes:
            raise CanonicalStorageError("bounded river call-EV failure evidence exceeds byte limit")
        data = path.read_bytes()
        try:
            record = BoundedRiverCallEvBudgetFailureEvidenceV1.model_validate_json(
                data,
                strict=True,
            )
        except ValueError as exc:
            raise CanonicalStorageError(
                "bounded river call-EV failure evidence is invalid"
            ) from exc
        if (
            record.run_id != run_id
            or record.tool_ordinal != ordinal
            or canonical_json_bytes(record) != data
        ):
            raise CanonicalStorageError("bounded river call-EV failure evidence is noncanonical")
        records.append(record)
    records.sort(key=lambda item: item.tool_ordinal)
    if len(records) > 1 or len({item.tool_ordinal for item in records}) != len(records):
        raise CanonicalStorageError("bounded river call-EV has multiple budget failure records")
    return tuple(records)


def verify_bounded_river_call_ev_budget_failure_evidence(
    record: BoundedRiverCallEvBudgetFailureEvidenceV1,
    *,
    binding: BoundedRiverCallEvBindingV1,
    admission_record: BoundedRiverCallEvAdmissionRecordV1,
    result: ToolResult,
    policy: BudgetPolicyV2,
) -> None:
    verify_bounded_river_call_ev_admission_record(admission_record, binding)
    _validate_failure_against_policy(record.failure, policy, request_input=result.input)
    result_bytes = canonical_json_bytes(result)
    if (
        record.binding_sha256 != binding.binding_sha256
        or record.admission_record_sha256 != admission_record.record_sha256
        or record.budget_policy_sha256 != policy.canonical_sha256
        or record.result_id != result.result_id
        or record.tool_name != result.tool_name
        or record.request_input_sha256 != phase_canonical_sha256(result.input)
        or record.tool_result_sha256
        != canonical_domain_sha256(
            TOOL_RESULT_HASH_DOMAIN,
            result.model_dump(mode="json"),
        )
        or record.tool_result_bytes_sha256 != hashlib.sha256(result_bytes).hexdigest()
        or result.status is not ToolStatus.FAILED
        or result.error != f"strict budget failure: {record.failure_code.value}"
    ):
        raise CanonicalStorageError("bounded river call-EV failure evidence commitment mismatch")


__all__ = [
    "build_bounded_river_call_ev_budget_failure_evidence",
    "commit_bounded_river_call_ev_budget_failure_evidence",
    "read_bounded_river_call_ev_budget_failure_evidence",
    "verify_bounded_river_call_ev_budget_failure_evidence",
]
