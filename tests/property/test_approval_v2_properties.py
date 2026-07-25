from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from poker_deliberation.approval_canonical import (
    CanonicalApprovalError,
    action_digest_sha256,
    approval_actor_sha256,
    canonical_json_bytes,
    canonical_jsonl_bytes,
    parse_canonical_json,
    parse_canonical_jsonl,
)
from poker_deliberation.approval_models import ApprovalActor, CanonicalActionPlanV2

NOW = datetime(2026, 7, 25, 0, 0, tzinfo=UTC)
HASH_A = "a" * 64


def _plan(**changes: object) -> CanonicalActionPlanV2:
    values: dict[str, object] = {
        "operation": "No raw payload is stored.",
        "action_category": "external_service",
        "executor_kind": "provider",
        "executor_identifier": "provider.example",
        "executor_version": "1.0.0",
        "executor_sha256": HASH_A,
        "executor_availability": "unavailable",
        "outbound_fields": (),
        "destination_kind": "provider",
        "destination_identifier": "provider.example/review",
        "retention_policy_id": "retention-none",
        "trace_policy_id": "trace-redacted-v1",
        "maximum_cost_microunits": 0,
        "maximum_runtime_ms": 1,
        "maximum_memory_bytes": 1,
        "maximum_output_bytes": 1,
        "maximum_processes": 1,
        "working_directory": None,
        "environment_name_allowlist": (),
        "expected_result_type": "none",
        "execution_id": "execution-1",
        "remote_idempotency_key": "remote-1",
        "expires_at": NOW + timedelta(hours=1),
    }
    values.update(changes)
    return CanonicalActionPlanV2(**values)


def test_mapping_insertion_order_does_not_change_canonical_bytes() -> None:
    first = {"z": 1, "a": {"y": 2, "b": 3}}
    second = {"a": {"b": 3, "y": 2}, "z": 1}
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


@pytest.mark.parametrize(
    "data",
    [
        b'{"a":1,"a":2}',
        b"\xef\xbb\xbf{}",
        b"{}\n",
        b'{ "a":1}',
        b'{"value":NaN}',
        b'{"b":1,"a":2}',
    ],
)
def test_strict_reader_rejects_duplicate_bom_newline_spacing_nonfinite_and_order(
    data: bytes,
) -> None:
    with pytest.raises(CanonicalApprovalError):
        parse_canonical_json(data)


def test_non_nfc_text_is_rejected_before_hashing() -> None:
    with pytest.raises(CanonicalApprovalError, match="NFC"):
        canonical_json_bytes({"name": "e\u0301"})


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("operation", "Changed operation."),
        ("executor_identifier", "provider.changed"),
        ("executor_version", "2.0.0"),
        ("executor_sha256", "b" * 64),
        ("destination_identifier", "provider.example/changed"),
        ("retention_policy_id", "retention-30d"),
        ("trace_policy_id", "trace-redacted-v2"),
        ("maximum_cost_microunits", 1),
        ("maximum_runtime_ms", 2),
        ("maximum_memory_bytes", 2),
        ("maximum_output_bytes", 2),
        ("maximum_processes", 2),
        ("working_directory", "workspace"),
        ("environment_name_allowlist", ("LANG",)),
        ("expected_result_type", "review"),
        ("execution_id", "execution-2"),
        ("remote_idempotency_key", "remote-2"),
        ("expires_at", NOW + timedelta(hours=2)),
    ],
)
def test_every_action_plan_change_invalidates_the_digest(field: str, replacement: object) -> None:
    original = _plan()
    changed = _plan(**{field: replacement})
    assert action_digest_sha256(original) != action_digest_sha256(changed)


def test_actor_canonical_jsonl_round_trip_is_exact() -> None:
    actor = ApprovalActor(
        actor_id="local-user",
        actor_type="human",
        authority_source="local_cli",
        authority_scopes=("reject:any",),
        verification_status="unverified",
        session_reference_sha256=HASH_A,
        revocation_status="unknown",
    )
    data = canonical_jsonl_bytes((actor,))
    assert parse_canonical_jsonl(data, ApprovalActor) == (actor,)
    assert approval_actor_sha256(actor) == approval_actor_sha256(
        parse_canonical_jsonl(data, ApprovalActor)[0]
    )


@pytest.mark.parametrize("data", [b"{}", b"{}\r\n", b"{}\n\n"])
def test_jsonl_requires_lf_terminated_nonblank_records(data: bytes) -> None:
    with pytest.raises(CanonicalApprovalError):
        parse_canonical_jsonl(data, ApprovalActor)
