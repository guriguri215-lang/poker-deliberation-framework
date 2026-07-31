"""Canonical bytes, admission tables, hashes, and pure P2-012A preflight."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar, cast

from pydantic import AnyUrl, BaseModel, TypeAdapter, ValidationError

from poker_deliberation.budgets.durable_models import (
    DURABLE_BUDGET_ARTIFACT_SCHEMA,
    DURABLE_BUDGET_PRODUCER_ID,
    DURABLE_BUDGET_PRODUCER_VERSION,
    DurableBudgetStateV1,
)
from poker_deliberation.confirmed_review_models import (
    CANDIDATE_ARTIFACT_SCHEMA,
    CONFIRMATION_ARTIFACT_SCHEMA,
    PROVENANCE_ARTIFACT_SCHEMA,
    SOURCE_ARTIFACT_SCHEMA,
    ConfirmedReviewProvenanceV1,
    ReviewIntakeCandidateV1,
    ReviewIntakeConfirmationV1,
)
from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.isolated_jobs.models import (
    ISOLATED_JOB_ARTIFACT_SCHEMA,
    ISOLATED_JOB_PRODUCER_ID,
    ISOLATED_JOB_PRODUCER_VERSION,
    DurableIsolatedJobStateV1,
)
from poker_deliberation.local_data_policy import (
    DEFAULT_LOCAL_DATA_POLICY,
    ClassificationEvidence,
    ClassificationSource,
    LifecyclePolicyError,
    classify_artifact,
)
from poker_deliberation.normalization import (
    NormalizationResultV1,
    verify_normalization_binding,
)
from poker_deliberation.phases.contracts import canonical_sha256 as phase_canonical_sha256
from poker_deliberation.phases.models import ToolExecutionBinding
from poker_deliberation.range_grammar import verify_versioned_range_tool_chain
from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionRecord,
    AgentReport,
    ApprovalRequest,
    Assumption,
    CaseInput,
    Dispute,
    EvidenceRecord,
    FinalReport,
    SecurityEvent,
    ToolRequest,
    ToolResult,
)
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_ID,
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    APPROVED_LOCAL_DATA_POLICY_VERSION,
    ApprovalDecisionBindingV1,
    BudgetPolicyBindingV1,
    ContextBindingV1,
    LocalDataBindingV1,
    PayloadInventoryEntryV1,
    PhaseBindingV1,
    ProvenanceBindingV1,
    ProvenanceHeadV1,
    ReportBindingV1,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    SourceBindingV1,
    ToolBindingV1,
    validate_control_string,
)
from poker_deliberation.tools.contracts import contract_by_name

CONTROL_CANONICALIZATION = "poker-run-storage-json-v1"
JSONL_SERIALIZATION = "poker-run-storage-jsonl-v1"
TEXT_SERIALIZATION = "poker-run-storage-utf8-text-v1"
TRANSACTION_HASH_DOMAIN = "poker-run-storage-transaction-v1"
INVENTORY_HASH_DOMAIN = "poker-run-storage-inventory-v1"
CLASSIFICATION_EVIDENCE_HASH_DOMAIN = "poker-run-classification-evidence-v1"
PROVENANCE_BINDING_HASH_DOMAIN = "poker-run-provenance-binding-v1"
PROVENANCE_HEAD_HASH_DOMAIN = "poker-run-provenance-head-v1"
UPSTREAM_BINDINGS_HASH_DOMAIN = "poker-run-upstream-bindings-v1"
USER_INPUT_SOURCE_HASH_DOMAIN = "poker-user-input-source-v1"
PAYLOAD_SOURCE_ID_HASH_DOMAIN = "poker-payload-source-id-v1"
LEGACY_ROOT_IDENTITY_HASH_DOMAIN = "poker-run-legacy-root-identity-v1"
AUTHORITY_IDENTITY_HASH_DOMAIN = "poker-run-authority-identity-v1"
RUN_LOCK_KEY_HASH_DOMAIN = "poker-run-lock-key-v1"
RECOVERY_CLAIM_HASH_DOMAIN = "poker-run-recovery-claim-v1"
APPROVAL_REASON_HASH_DOMAIN = "poker-approval-decision-reason-v1"
APPROVAL_EVIDENCE_HASH_DOMAIN = "poker-approval-decision-evidence-v1"
EVIDENCE_SOURCE_HASH_DOMAIN = "poker-evidence-record-source-v1"
FINAL_REPORT_ARTIFACT_V1 = "poker-final-report-artifact-v1"
FINAL_REPORT_ARTIFACT_V2 = "poker-final-report-artifact-v2"

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VARIABLE_AGENT_REPORT = re.compile(
    r"^agent_reports/(?P<identifier>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$"
)
_VARIABLE_TOOL_INPUT = re.compile(
    r"^tool_results/(?P<identifier>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.input\.json$"
)
_VARIABLE_TOOL_RESULT = re.compile(
    r"^tool_results/(?P<identifier>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.json$"
)
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)

_ArtifactTableValue = tuple[str, str, str, str]
_FIXED_ARTIFACT_TABLE: dict[str, _ArtifactTableValue] = {
    "confirmed_review_source.txt": (
        "text/plain",
        TEXT_SERIALIZATION,
        SOURCE_ARTIFACT_SCHEMA,
        "confirmed_review_source",
    ),
    "confirmed_review_candidate.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        CANDIDATE_ARTIFACT_SCHEMA,
        "confirmed_review_candidate",
    ),
    "confirmed_review_confirmation.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        CONFIRMATION_ARTIFACT_SCHEMA,
        "confirmed_review_confirmation",
    ),
    "input.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-case-input-artifact-v1",
        "case_input",
    ),
    "normalization.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-normalization-result-artifact-v1",
        "normalization_record",
    ),
    "normalized_case.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-normalized-case-artifact-v1",
        "normalization_output",
    ),
    "assumptions.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-assumption-list-artifact-v1",
        "assumption_ledger",
    ),
    "evidence.jsonl": (
        "application/x-ndjson",
        JSONL_SERIALIZATION,
        "poker-evidence-record-jsonl-artifact-v1",
        "evidence_ledger",
    ),
    "approvals.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-approval-request-list-artifact-v1",
        "approval_ledger",
    ),
    "assignments.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-agent-assignment-list-artifact-v1",
        "assignment_ledger",
    ),
    "agent_execution_records.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-agent-execution-record-list-artifact-v1",
        "agent_execution_ledger",
    ),
    "security_events.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-security-event-list-artifact-v1",
        "security_event_ledger",
    ),
    "disputes.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        "poker-dispute-list-artifact-v1",
        "dispute_ledger",
    ),
    "final_report.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        FINAL_REPORT_ARTIFACT_V1,
        "final_report_json",
    ),
    "final_report.md": (
        "text/markdown",
        TEXT_SERIALIZATION,
        "poker-final-report-markdown-artifact-v1",
        "final_report_markdown",
    ),
    "confirmed_review_provenance.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        PROVENANCE_ARTIFACT_SCHEMA,
        "confirmed_review_provenance",
    ),
    "budget_state.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        DURABLE_BUDGET_ARTIFACT_SCHEMA,
        "budget_state",
    ),
    "isolated_job_state.json": (
        "application/json",
        CONTROL_CANONICALIZATION,
        ISOLATED_JOB_ARTIFACT_SCHEMA,
        "isolated_job_state",
    ),
    "stdout.txt": (
        "text/plain",
        TEXT_SERIALIZATION,
        "poker-isolated-job-stdout-artifact-v1",
        "isolated_job_stdout",
    ),
    "stderr.txt": (
        "text/plain",
        TEXT_SERIALIZATION,
        "poker-isolated-job-stderr-artifact-v1",
        "isolated_job_stderr",
    ),
}

_CONFIRMED_REVIEW_ARTIFACTS = frozenset(
    {
        "confirmed_review_source.txt",
        "confirmed_review_candidate.json",
        "confirmed_review_confirmation.json",
        "confirmed_review_provenance.json",
    }
)

_PAYLOAD_ORDER_PREFIX = (
    "confirmed_review_source.txt",
    "confirmed_review_candidate.json",
    "confirmed_review_confirmation.json",
    "input.json",
    "normalization.json",
    "normalized_case.json",
    "assumptions.json",
    "evidence.jsonl",
    "approvals.json",
    "assignments.json",
    "agent_execution_records.json",
    "security_events.json",
)

T = TypeVar("T")


class CanonicalStorageError(ValueError):
    """Pure admission or canonical-byte failure; it never mutates storage."""


def platform_adapter() -> str:
    return "windows_msvcrt" if os.name == "nt" else "posix_fcntl"


def format_storage_utc(value: datetime) -> str:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise CanonicalStorageError("storage datetime must be timezone-aware UTC")
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _require_nfc_string(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise CanonicalStorageError("storage strings and keys must already be NFC")
    return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _jsonable(value.model_dump(mode="python"))
    if isinstance(value, datetime):
        return format_storage_utc(value)
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, AnyUrl):
        return _require_nfc_string(str(value))
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        aliases: set[str] = set()
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalStorageError("canonical JSON object keys must be strings")
            _require_nfc_string(key)
            alias = unicodedata.normalize("NFC", key)
            if alias in aliases:
                raise CanonicalStorageError("duplicate canonical JSON object key")
            aliases.add(alias)
            result[key] = _jsonable(item)
        return result
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, str):
        return _require_nfc_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise CanonicalStorageError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            _jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        if isinstance(exc, CanonicalStorageError):
            raise
        raise CanonicalStorageError("value is not canonical storage JSON") from exc


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    normalized: set[str] = set()
    for key, value in pairs:
        _require_nfc_string(key)
        alias = unicodedata.normalize("NFC", key)
        if alias in normalized:
            raise CanonicalStorageError("duplicate canonical JSON key")
        normalized.add(alias)
        result[key] = value
    return result


def parse_canonical_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise CanonicalStorageError("canonical JSON cannot contain a BOM")
    if data.endswith(b"\n") or data.endswith(b"\r"):
        raise CanonicalStorageError("canonical JSON cannot contain a trailing newline")
    try:
        text = data.decode("utf-8", errors="strict")
        _require_nfc_string(text)
        value = json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CanonicalStorageError(f"non-finite JSON number: {token}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CanonicalStorageError("invalid canonical JSON") from exc
    if canonical_json_bytes(value) != data:
        raise CanonicalStorageError("JSON bytes are not in canonical storage form")
    return value


def parse_canonical_model(data: bytes, model: type[T]) -> T:
    parse_canonical_json(data)
    adapter = TypeAdapter(model)
    try:
        value = adapter.validate_json(data, strict=True)
    except ValidationError as exc:
        if not exc.errors() or any(error["type"] != "datetime_type" for error in exc.errors()):
            raise CanonicalStorageError("canonical JSON violates its strict schema") from exc
        try:
            value = adapter.validate_json(data, strict=False)
        except ValidationError as fallback_exc:
            raise CanonicalStorageError(
                "canonical JSON violates its strict datetime schema"
            ) from fallback_exc
    if canonical_json_bytes(value) != data:
        raise CanonicalStorageError("strict model canonical bytes mismatch")
    return value


def parse_canonical_model_list(data: bytes, model: type[T]) -> tuple[T, ...]:
    parse_canonical_json(data)
    adapter = TypeAdapter(list[model])  # type: ignore[valid-type]
    try:
        values = adapter.validate_json(data, strict=True)
    except ValidationError as exc:
        if not exc.errors() or any(error["type"] != "datetime_type" for error in exc.errors()):
            raise CanonicalStorageError("canonical JSON list violates its strict schema") from exc
        try:
            values = adapter.validate_json(data, strict=False)
        except ValidationError as fallback_exc:
            raise CanonicalStorageError(
                "canonical JSON list violates its strict datetime schema"
            ) from fallback_exc
    if canonical_json_bytes(values) != data:
        raise CanonicalStorageError("strict model list canonical bytes mismatch")
    return tuple(values)


def parse_canonical_jsonl(data: bytes, model: type[T]) -> tuple[T, ...]:
    if data == b"":
        return ()
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data or not data.endswith(b"\n"):
        raise CanonicalStorageError("JSONL requires UTF-8, LF, and a terminated final record")
    lines = data.split(b"\n")[:-1]
    if any(line == b"" for line in lines):
        raise CanonicalStorageError("JSONL cannot contain blank records")
    return tuple(parse_canonical_model(line, model) for line in lines)


def validate_canonical_text(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        raise CanonicalStorageError("text requires UTF-8 without BOM and LF-only newlines")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CanonicalStorageError("text payload is not UTF-8") from exc
    _require_nfc_string(text)
    return text


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def domain_sha256(domain: str, data: bytes) -> str:
    try:
        prefix = domain.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CanonicalStorageError("hash domain must be ASCII") from exc
    return sha256_bytes(prefix + b"\0" + data)


def canonical_domain_sha256(domain: str, value: Any) -> str:
    return domain_sha256(domain, canonical_json_bytes(value))


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise CanonicalStorageError("invalid portable run ID")
    _require_nfc_string(run_id)
    try:
        validate_control_string(run_id)
    except ValueError as exc:
        raise CanonicalStorageError("run ID contains unsafe control metadata") from exc
    if run_id.endswith((".", " ")):
        raise CanonicalStorageError("run ID cannot end in dot or space")
    if run_id.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED:
        raise CanonicalStorageError("run ID uses a reserved device alias")
    return run_id


def ascii_casefold(value: str) -> str:
    return "".join(
        chr(ord(character) + 32) if "A" <= character <= "Z" else character for character in value
    )


def run_id_sha256(run_id: str) -> str:
    return sha256_bytes(validate_run_id(run_id).encode("utf-8"))


def run_lock_key_sha256(run_id: str) -> str:
    normalized = ascii_casefold(validate_run_id(run_id))
    return domain_sha256(RUN_LOCK_KEY_HASH_DOMAIN, normalized.encode("utf-8"))


def _validate_segment(segment: str) -> None:
    _require_nfc_string(segment)
    if segment in {"", ".", ".."} or segment.endswith((".", " ")):
        raise CanonicalStorageError("unsafe portable path segment")
    if ":" in segment or "\\" in segment:
        raise CanonicalStorageError("portable path cannot contain colon or backslash")
    if segment.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED:
        raise CanonicalStorageError("portable path uses a reserved device alias")


def validate_logical_name(logical_name: str) -> str:
    if (
        not isinstance(logical_name, str)
        or not logical_name
        or logical_name.startswith(("/", "\\"))
        or "://" in logical_name
        or PurePosixPath(logical_name).is_absolute()
    ):
        raise CanonicalStorageError("logical name is not a relative POSIX path")
    try:
        validate_control_string(logical_name)
    except ValueError as exc:
        raise CanonicalStorageError("logical name contains unsafe control metadata") from exc
    for segment in logical_name.split("/"):
        _validate_segment(segment)
    return logical_name


def artifact_table_entry(
    logical_name: str,
    artifact_schema_version: str | None = None,
) -> _ArtifactTableValue:
    validate_logical_name(logical_name)
    fixed = _FIXED_ARTIFACT_TABLE.get(logical_name)
    if fixed is not None:
        if logical_name == "final_report.json":
            version = (
                FINAL_REPORT_ARTIFACT_V1
                if artifact_schema_version is None
                else artifact_schema_version
            )
            if version == FINAL_REPORT_ARTIFACT_V1:
                return fixed
            if version == FINAL_REPORT_ARTIFACT_V2:
                return (
                    fixed[0],
                    fixed[1],
                    FINAL_REPORT_ARTIFACT_V2,
                    fixed[3],
                )
            raise CanonicalStorageError("unknown final-report artifact schema version")
        if artifact_schema_version is not None and artifact_schema_version != fixed[2]:
            raise CanonicalStorageError("logical artifact schema version mismatch")
        return fixed
    match = _VARIABLE_AGENT_REPORT.fullmatch(logical_name)
    if match is not None:
        validate_run_id(match.group("identifier"))
        value = (
            "application/json",
            CONTROL_CANONICALIZATION,
            "poker-agent-report-artifact-v1",
            "agent_report",
        )
        if artifact_schema_version is not None and artifact_schema_version != value[2]:
            raise CanonicalStorageError("logical artifact schema version mismatch")
        return value
    match = _VARIABLE_TOOL_INPUT.fullmatch(logical_name)
    if match is not None:
        validate_run_id(match.group("identifier"))
        value = (
            "application/json",
            CONTROL_CANONICALIZATION,
            "poker-tool-input-artifact-v1",
            "tool_input",
        )
        if artifact_schema_version is not None and artifact_schema_version != value[2]:
            raise CanonicalStorageError("logical artifact schema version mismatch")
        return value
    match = _VARIABLE_TOOL_RESULT.fullmatch(logical_name)
    if match is not None:
        validate_run_id(match.group("identifier"))
        value = (
            "application/json",
            CONTROL_CANONICALIZATION,
            "poker-tool-result-artifact-v1",
            "tool_result",
        )
        if artifact_schema_version is not None and artifact_schema_version != value[2]:
            raise CanonicalStorageError("logical artifact schema version mismatch")
        return value
    raise CanonicalStorageError("logical artifact is not admitted by P2-012A")


def payload_order_key(logical_name: str) -> tuple[int, bytes]:
    if logical_name in _PAYLOAD_ORDER_PREFIX:
        return (_PAYLOAD_ORDER_PREFIX.index(logical_name), b"")
    if logical_name.endswith(".input.json") and logical_name.startswith("tool_results/"):
        return (12, logical_name.encode("utf-8"))
    if logical_name.startswith("tool_results/"):
        return (13, logical_name.encode("utf-8"))
    if logical_name.startswith("agent_reports/"):
        return (14, logical_name.encode("utf-8"))
    if logical_name == "disputes.json":
        return (15, b"")
    if logical_name == "final_report.json":
        return (16, b"")
    if logical_name == "final_report.md":
        return (17, b"")
    if logical_name == "confirmed_review_provenance.json":
        return (18, b"")
    if logical_name == "budget_state.json":
        return (19, b"")
    if logical_name == "isolated_job_state.json":
        return (20, b"")
    if logical_name == "stdout.txt":
        return (21, b"")
    if logical_name == "stderr.txt":
        return (22, b"")
    raise CanonicalStorageError("logical artifact has no approved dependency order")


def classification_evidence_sha256(evidence: ClassificationEvidence) -> str:
    return canonical_domain_sha256(CLASSIFICATION_EVIDENCE_HASH_DOMAIN, evidence)


def binding_sha256(binding: ProvenanceBindingV1) -> str:
    return canonical_domain_sha256(PROVENANCE_BINDING_HASH_DOMAIN, binding)


def _binding_primary_key(binding: ProvenanceBindingV1) -> tuple[Any, ...]:
    kind = binding.kind
    if isinstance(binding, ApprovalDecisionBindingV1):
        primary: tuple[Any, ...] = (binding.approval_id,)
    elif isinstance(binding, ContextBindingV1):
        primary = (binding.context_id, binding.attempt_id)
    elif isinstance(binding, PhaseBindingV1):
        primary = (binding.run_id, binding.phase_id, binding.attempt_id)
    elif isinstance(binding, BudgetPolicyBindingV1):
        primary = (binding.policy_sha256,)
    elif isinstance(binding, ToolBindingV1):
        primary = (
            binding.run_id,
            binding.phase_attempt_id,
            binding.ordinal,
            binding.result_id,
        )
    elif isinstance(binding, LocalDataBindingV1):
        primary = (binding.policy_id, binding.logical_name)
    elif isinstance(binding, SourceBindingV1):
        primary = (binding.source_id,)
    else:
        assert isinstance(binding, ReportBindingV1)
        primary = (binding.report_id,)
    return (kind, *primary)


def canonicalize_bindings(
    bindings: Iterable[ProvenanceBindingV1],
) -> tuple[ProvenanceBindingV1, ...]:
    owned = tuple(bindings)
    decorated = sorted(
        (
            (
                _binding_primary_key(binding),
                canonical_json_bytes(binding),
                binding_sha256(binding),
                binding,
            )
            for binding in owned
        ),
        key=lambda item: (
            tuple(part if isinstance(part, int) else str(part).encode("utf-8") for part in item[0]),
            item[1],
        ),
    )
    seen_digests: set[str] = set()
    seen_primary: dict[tuple[Any, ...], bytes] = {}
    for primary, canonical, digest, _binding in decorated:
        if digest in seen_digests:
            raise CanonicalStorageError("duplicate provenance binding")
        seen_digests.add(digest)
        previous = seen_primary.setdefault(primary, canonical)
        if previous != canonical:
            raise CanonicalStorageError("conflicting provenance primary identity")
    return tuple(item[3] for item in decorated)


def provenance_heads(
    inventories: Sequence[PayloadInventoryEntryV1],
) -> tuple[ProvenanceHeadV1, ...]:
    per_kind: dict[str, dict[str, ProvenanceBindingV1]] = {}
    primary_bytes: dict[tuple[Any, ...], bytes] = {}
    for inventory in inventories:
        for binding in inventory.provenance_bindings:
            digest = binding_sha256(binding)
            canonical = canonical_json_bytes(binding)
            primary = _binding_primary_key(binding)
            previous = primary_bytes.setdefault(primary, canonical)
            if previous != canonical:
                raise CanonicalStorageError("conflicting cross-artifact provenance identity")
            per_kind.setdefault(binding.kind, {})[digest] = binding
    heads: list[ProvenanceHeadV1] = []
    for kind in sorted(per_kind, key=lambda item: item.encode("utf-8")):
        ordered = canonicalize_bindings(per_kind[kind].values())
        digests = tuple(binding_sha256(binding) for binding in ordered)
        digest = domain_sha256(
            PROVENANCE_HEAD_HASH_DOMAIN,
            kind.encode("utf-8") + b"\0" + canonical_json_bytes(digests),
        )
        heads.append(
            ProvenanceHeadV1(
                kind=kind,  # type: ignore[arg-type]
                binding_count=len(digests),
                bindings_sha256=digest,
            )
        )
    return tuple(heads)


def _source_sort_key(binding: SourceBindingV1) -> tuple[Any, ...]:
    logical_key = (
        (0, b"")
        if binding.source_logical_name is None
        else (1, binding.source_logical_name.encode("utf-8"))
    )
    consumer_key = (
        (0, b"")
        if binding.consumer_record_id is None
        else (1, binding.consumer_record_id.encode("utf-8"))
    )
    return (logical_key, binding.source_id.encode("utf-8"), consumer_key)


def upstream_source_sha256(bindings: Iterable[ProvenanceBindingV1]) -> str:
    sources = sorted(
        (binding for binding in bindings if isinstance(binding, SourceBindingV1)),
        key=_source_sort_key,
    )
    return canonical_domain_sha256(UPSTREAM_BINDINGS_HASH_DOMAIN, sources)


def payload_source_id(logical_name: str) -> str:
    return "payload-" + domain_sha256(
        PAYLOAD_SOURCE_ID_HASH_DOMAIN,
        validate_logical_name(logical_name).encode("utf-8"),
    )


def _classify_and_verify(artifact: RevisionArtifactV1) -> None:
    if artifact.policy_sha256 != APPROVED_LOCAL_DATA_POLICY_SHA256:
        raise CanonicalStorageError("local-data policy digest mismatch")
    evidence = artifact.classification_evidence
    try:
        replay = classify_artifact(
            artifact.logical_name,
            source_classifications=evidence.source_classifications,
            explicit_classification=evidence.explicit_classification,
            explicit_source_trusted=evidence.explicit_source_trusted,
            restricted_secret_check_completed=evidence.restricted_secret_check_completed,
            contains_restricted_secret=evidence.contains_restricted_secret,
            policy=DEFAULT_LOCAL_DATA_POLICY,
        )
    except LifecyclePolicyError as exc:
        raise CanonicalStorageError("artifact classification replay failed") from exc
    if (
        replay.classification is not artifact.classification
        or replay.classification_source is not artifact.classification_source
        or replay.classification_evidence != evidence
    ):
        raise CanonicalStorageError("artifact classification does not replay exactly")
    if artifact.classification not in {
        ContextClassification.PUBLIC,
        ContextClassification.INTERNAL,
    }:
        raise CanonicalStorageError("artifact classification cannot be persisted")
    allowed_sources = {
        ClassificationSource.EXPLICIT_TRUSTED,
        ClassificationSource.SOURCE_INHERITANCE,
    }
    if artifact.logical_name == "budget_state.json":
        allowed_sources.add(ClassificationSource.DEFAULT_INTERNAL)
    if artifact.classification_source not in allowed_sources:
        raise CanonicalStorageError(
            "artifact requires trusted explicit or inherited classification"
        )
    if not evidence.restricted_secret_check_completed or evidence.contains_restricted_secret:
        raise CanonicalStorageError("artifact requires a completed clean restricted-secret check")


def _local_data_binding(artifact: RevisionArtifactV1) -> LocalDataBindingV1:
    bindings = [
        binding
        for binding in artifact.provenance_bindings
        if isinstance(binding, LocalDataBindingV1)
    ]
    if len(bindings) != 1:
        raise CanonicalStorageError("artifact requires exactly one local-data binding")
    binding = bindings[0]
    expected_hash = classification_evidence_sha256(artifact.classification_evidence)
    if (
        binding.policy_id != APPROVED_LOCAL_DATA_POLICY_ID
        or binding.policy_version != APPROVED_LOCAL_DATA_POLICY_VERSION
        or binding.policy_sha256 != artifact.policy_sha256
        or binding.logical_name != artifact.logical_name
        or binding.classification is not artifact.classification
        or binding.classification_source is not artifact.classification_source
        or binding.classification_evidence != artifact.classification_evidence
        or binding.classification_evidence_sha256 != expected_hash
    ):
        raise CanonicalStorageError("local-data binding does not match its artifact")
    return binding


def _validated_payload(artifact: RevisionArtifactV1, run_id: str) -> Any:
    logical_name = artifact.logical_name
    data = artifact.exact_bytes
    if logical_name == "confirmed_review_source.txt":
        return validate_canonical_text(data)
    if logical_name == "confirmed_review_candidate.json":
        return parse_canonical_model(data, ReviewIntakeCandidateV1)
    if logical_name == "confirmed_review_confirmation.json":
        confirmation = parse_canonical_model(data, ReviewIntakeConfirmationV1)
        if confirmation.run_id != run_id:
            raise CanonicalStorageError("confirmed-review confirmation run ID mismatch")
        return confirmation
    if logical_name in {"input.json", "normalized_case.json"}:
        return parse_canonical_model(data, CaseInput)
    if logical_name == "normalization.json":
        return parse_canonical_model(data, NormalizationResultV1)
    if logical_name == "assumptions.json":
        return parse_canonical_model_list(data, Assumption)
    if logical_name == "evidence.jsonl":
        return parse_canonical_jsonl(data, EvidenceRecord)
    if logical_name == "approvals.json":
        return parse_canonical_model_list(data, ApprovalRequest)
    if logical_name == "assignments.json":
        return parse_canonical_model_list(data, AgentAssignment)
    if logical_name == "agent_execution_records.json":
        return parse_canonical_model_list(data, AgentExecutionRecord)
    if logical_name == "security_events.json":
        return parse_canonical_model_list(data, SecurityEvent)
    if logical_name == "disputes.json":
        return parse_canonical_model_list(data, Dispute)
    report_match = _VARIABLE_AGENT_REPORT.fullmatch(logical_name)
    if report_match is not None:
        agent_report = parse_canonical_model(data, AgentReport)
        if agent_report.report_id != report_match.group("identifier"):
            raise CanonicalStorageError("agent report ID does not match its path")
        return agent_report
    tool_result_match = _VARIABLE_TOOL_RESULT.fullmatch(logical_name)
    if tool_result_match is not None and not logical_name.endswith(".input.json"):
        result = parse_canonical_model(data, ToolResult)
        if result.result_id != tool_result_match.group("identifier"):
            raise CanonicalStorageError("tool result ID does not match its path")
        return result
    if _VARIABLE_TOOL_INPUT.fullmatch(logical_name):
        value = parse_canonical_json(data)
        if not isinstance(value, dict):
            raise CanonicalStorageError("tool input artifact must be one canonical object")
        return value
    if logical_name == "final_report.json":
        final_report = parse_canonical_model(data, FinalReport)
        if final_report.run_id != run_id:
            raise CanonicalStorageError("final report run ID mismatch")
        return final_report
    if logical_name == "final_report.md":
        return validate_canonical_text(data)
    if logical_name == "confirmed_review_provenance.json":
        provenance = parse_canonical_model(data, ConfirmedReviewProvenanceV1)
        if provenance.run_id != run_id:
            raise CanonicalStorageError("confirmed-review provenance run ID mismatch")
        return provenance
    if logical_name == "budget_state.json":
        state = parse_canonical_model(data, DurableBudgetStateV1)
        if state.run_id != run_id:
            raise CanonicalStorageError("durable budget state run ID mismatch")
        return state
    if logical_name == "isolated_job_state.json":
        job_state = parse_canonical_model(data, DurableIsolatedJobStateV1)
        if job_state.execution_id != run_id:
            raise CanonicalStorageError("isolated-job state execution ID mismatch")
        return job_state
    if logical_name in {"stdout.txt", "stderr.txt"}:
        return validate_canonical_text(data)
    raise CanonicalStorageError("artifact payload has no strict validator")


def validate_artifact(
    artifact: RevisionArtifactV1,
    run_id: str,
    max_artifact_bytes: int,
) -> Any:
    validate_logical_name(artifact.logical_name)
    expected = artifact_table_entry(
        artifact.logical_name,
        artifact.artifact_schema_version,
    )
    actual = (
        artifact.media_type,
        artifact.serialization,
        artifact.artifact_schema_version,
        artifact.origin_kind,
    )
    if actual != expected:
        raise CanonicalStorageError("logical media/schema/origin admission mismatch")
    if artifact.serialization == "opaque-bytes-v1":
        raise CanonicalStorageError("opaque payloads are not admitted in P2-012A")
    if len(artifact.exact_bytes) > max_artifact_bytes:
        raise CanonicalStorageError("artifact exceeds the exact byte limit")
    _classify_and_verify(artifact)
    _local_data_binding(artifact)
    canonicalize_bindings(artifact.provenance_bindings)
    return _validated_payload(artifact, run_id)


def validate_assignment_execution_correlation(
    assignments: Sequence[AgentAssignment],
    execution_records: Sequence[AgentExecutionRecord],
) -> None:
    """Require every execution to resolve to one same-role durable assignment."""

    assignment_ids = [assignment.assignment_id for assignment in assignments]
    if len(set(assignment_ids)) != len(assignment_ids):
        raise CanonicalStorageError("assignment ledger IDs must be unique")
    assignment_by_id = {assignment.assignment_id: assignment for assignment in assignments}
    for record in execution_records:
        assignment = assignment_by_id.get(record.assignment_id)
        if assignment is None or assignment.agent_role != record.agent_role:
            raise CanonicalStorageError(
                "agent execution does not correlate to its assignment ledger"
            )


def _validate_source_graph(
    inventories: Sequence[PayloadInventoryEntryV1],
    parsed: Mapping[str, Any],
    *,
    run_id: str,
) -> None:
    by_name = {entry.logical_name: entry for entry in inventories}
    order = {entry.logical_name: index for index, entry in enumerate(inventories)}
    final_report_schema_version = (
        by_name["final_report.json"].artifact_schema_version
        if "final_report.json" in by_name
        else None
    )
    final_report_v2 = final_report_schema_version == FINAL_REPORT_ARTIFACT_V2
    confirmed_names = set(by_name) & _CONFIRMED_REVIEW_ARTIFACTS
    input_case = parsed.get("input.json")
    final_report = parsed.get("final_report.json")
    input_marker_present = isinstance(input_case, CaseInput) and (
        "confirmed_review" in input_case.metadata
    )
    input_marker = (
        input_case.metadata.get("confirmed_review") if isinstance(input_case, CaseInput) else None
    )
    report_metadata = (
        final_report.reconstructed_input.get("metadata")
        if isinstance(final_report, FinalReport)
        else None
    )
    report_marker_present = isinstance(report_metadata, dict) and (
        "confirmed_review" in report_metadata
    )
    report_marker = (
        report_metadata.get("confirmed_review") if isinstance(report_metadata, dict) else None
    )
    if input_marker_present != report_marker_present or (
        input_marker_present and report_marker != input_marker
    ):
        raise CanonicalStorageError("confirmed-review input and report markers must match exactly")
    confirmed_marker = input_marker_present or report_marker_present
    if confirmed_marker != bool(confirmed_names) or (
        confirmed_names and confirmed_names != _CONFIRMED_REVIEW_ARTIFACTS
    ):
        raise CanonicalStorageError(
            "confirmed-review marker and complete artifact set must appear together"
        )
    if confirmed_marker and not {"input.json", "final_report.json"} <= set(by_name):
        raise CanonicalStorageError(
            "confirmed-review structural revision requires input and final report"
        )
    if confirmed_marker and "assignments.json" not in parsed:
        raise CanonicalStorageError(
            "confirmed-review structural revision requires the assignment ledger"
        )
    validate_assignment_execution_correlation(
        cast(Sequence[AgentAssignment], parsed.get("assignments.json", ())),
        cast(
            Sequence[AgentExecutionRecord],
            parsed.get("agent_execution_records.json", ()),
        ),
    )
    for entry in inventories:
        allowed_binding_kinds = {"local_data", "source"}
        if entry.logical_name == "approvals.json":
            allowed_binding_kinds.add("approval_decision")
        elif (
            entry.logical_name == "agent_execution_records.json"
            or _VARIABLE_AGENT_REPORT.fullmatch(entry.logical_name)
        ):
            allowed_binding_kinds.update({"context", "phase"})
        elif _VARIABLE_TOOL_INPUT.fullmatch(entry.logical_name) or _VARIABLE_TOOL_RESULT.fullmatch(
            entry.logical_name
        ):
            allowed_binding_kinds.update({"phase", "tool"})
        elif entry.logical_name == "final_report.json":
            allowed_binding_kinds.update({"context", "phase", "budget_policy"})
        elif entry.logical_name == "final_report.md":
            allowed_binding_kinds.add("report")
        elif entry.logical_name in {
            "isolated_job_state.json",
            "stdout.txt",
            "stderr.txt",
        }:
            allowed_binding_kinds.update({"context", "budget_policy"})
        actual_binding_kinds = {binding.kind for binding in entry.provenance_bindings}
        if not actual_binding_kinds <= allowed_binding_kinds:
            raise CanonicalStorageError(
                f"{entry.logical_name} carries a provenance binding kind outside its contract"
            )
    for entry in inventories:
        sources = [
            binding for binding in entry.provenance_bindings if isinstance(binding, SourceBindingV1)
        ]
        if entry.logical_name == "input.json":
            if len(sources) != 1:
                raise CanonicalStorageError("input.json requires one user-input source")
            source = sources[0]
            if (
                source.source_id != "user-input"
                or source.source_kind != "user_input"
                or source.source_logical_name is not None
                or source.source_schema_version is not None
                or source.consumer_record_id is not None
                or source.source_sha256
                != domain_sha256(USER_INPUT_SOURCE_HASH_DOMAIN, parsed_bytes(entry, parsed))
            ):
                raise CanonicalStorageError("input.json user-input source mismatch")
        elif any(source.source_kind == "user_input" for source in sources):
            raise CanonicalStorageError("user-input source is only admitted for input.json")
        if entry.logical_name not in {"evidence.jsonl", "approvals.json"} and any(
            source.source_kind == "external_evidence" for source in sources
        ):
            raise CanonicalStorageError(
                "external-evidence source is not admitted for this artifact"
            )
        for source in sources:
            if source.source_kind != "payload_artifact":
                continue
            source_name = source.source_logical_name
            if source_name is None or source_name not in by_name:
                raise CanonicalStorageError("payload source does not resolve")
            source_entry = by_name[source_name]
            if order[source_name] >= order[entry.logical_name]:
                raise CanonicalStorageError("payload source must precede its consumer")
            if (
                source.source_id != payload_source_id(source_name)
                or source.source_schema_version != source_entry.artifact_schema_version
                or source.source_sha256 != source_entry.sha256
            ):
                raise CanonicalStorageError("payload source correlation mismatch")
        if entry.source_sha256 != upstream_source_sha256(entry.provenance_bindings):
            raise CanonicalStorageError("artifact upstream source digest mismatch")
        if entry.logical_name == "budget_state.json" and sources:
            raise CanonicalStorageError("durable budget state does not persist source payloads")

    def require_payload_sources(logical_name: str, expected: set[str]) -> None:
        entry = by_name[logical_name]
        actual = [
            binding.source_logical_name
            for binding in entry.provenance_bindings
            if isinstance(binding, SourceBindingV1) and binding.source_kind == "payload_artifact"
        ]
        if len(actual) != len(expected) or set(actual) != expected:
            raise CanonicalStorageError(
                f"{logical_name} does not have its exact direct payload source graph"
            )
        if not expected <= set(by_name):
            raise CanonicalStorageError(f"{logical_name} has a missing direct payload source")

    def bindings_of_type(logical_name: str, binding_type: type[T]) -> tuple[T, ...]:
        return tuple(
            binding
            for binding in by_name[logical_name].provenance_bindings
            if isinstance(binding, binding_type)
        )

    report_names = sorted(
        (name for name in by_name if _VARIABLE_AGENT_REPORT.fullmatch(name)),
        key=lambda item: item.encode("utf-8"),
    )
    if confirmed_marker:
        require_payload_sources(
            "confirmed_review_candidate.json",
            {"confirmed_review_source.txt"},
        )
        require_payload_sources(
            "confirmed_review_confirmation.json",
            {
                "confirmed_review_source.txt",
                "confirmed_review_candidate.json",
            },
        )
        require_payload_sources(
            "confirmed_review_provenance.json",
            {
                "confirmed_review_source.txt",
                "confirmed_review_candidate.json",
                "confirmed_review_confirmation.json",
                "input.json",
                "final_report.json",
            },
        )
        try:
            # Delayed import preserves the canonical-storage dependency direction.
            from poker_deliberation.confirmed_review import (
                verify_confirmed_review_structural_provenance,
            )

            agent_reports = [
                cast(AgentReport, parsed[logical_name]) for logical_name in report_names
            ]
            reports_by_role = {
                agent_report.agent_role: agent_report for agent_report in agent_reports
            }
            execution_records = cast(
                Sequence[AgentExecutionRecord],
                parsed["agent_execution_records.json"],
            )
            ordered_agent_reports = [
                reports_by_role[record.agent_role]
                for record in execution_records
                if record.agent_role in reports_by_role
            ]
            if (
                len(reports_by_role) != len(agent_reports)
                or len(ordered_agent_reports) != len(execution_records)
                or len(agent_reports) != len(execution_records)
            ):
                raise CanonicalStorageError(
                    "confirmed-review agent reports do not match executions"
                )
            verify_confirmed_review_structural_provenance(
                source_bytes=cast(str, parsed["confirmed_review_source.txt"]).encode("utf-8"),
                candidate=cast(
                    ReviewIntakeCandidateV1,
                    parsed["confirmed_review_candidate.json"],
                ),
                confirmation=cast(
                    ReviewIntakeConfirmationV1,
                    parsed["confirmed_review_confirmation.json"],
                ),
                case=cast(CaseInput, parsed["input.json"]),
                report=cast(FinalReport, parsed["final_report.json"]),
                provenance=cast(
                    ConfirmedReviewProvenanceV1,
                    parsed["confirmed_review_provenance.json"],
                ),
                assignments=cast(
                    Sequence[AgentAssignment],
                    parsed["assignments.json"],
                ),
                agent_reports=ordered_agent_reports,
            )
        except (KeyError, ValueError) as exc:
            raise CanonicalStorageError(
                "confirmed-review structural source-to-report replay failed"
            ) from exc

    for entry in inventories:
        phase_bindings = bindings_of_type(entry.logical_name, PhaseBindingV1)
        for phase in phase_bindings:
            if phase.run_id != run_id:
                raise CanonicalStorageError("phase binding run identity mismatch")
            for intent in phase.artifact_intents:
                if intent.relative_path == "state.json":
                    continue
                admitted = by_name.get(intent.relative_path)
                if admitted is None:
                    if intent.content_sha256 is not None:
                        raise CanonicalStorageError(
                            "hashed phase artifact intent has no admitted payload"
                        )
                    continue
                expected_kind = {
                    "agent_execution_records.json": "agent_execution_records",
                    "security_events.json": "security_events",
                    "approvals.json": "approvals",
                    "disputes.json": "disputes",
                    "final_report.json": "final_report_json",
                    "final_report.md": "final_report_markdown",
                }.get(intent.relative_path)
                if (
                    expected_kind is None
                    or intent.kind != expected_kind
                    or intent.media_type != admitted.media_type
                    or (
                        intent.content_sha256 is not None
                        and intent.content_sha256 != admitted.sha256
                    )
                ):
                    raise CanonicalStorageError("phase artifact intent correlation mismatch")
        for tool in bindings_of_type(entry.logical_name, ToolBindingV1):
            if tool.run_id != run_id:
                raise CanonicalStorageError("tool binding run identity mismatch")

    fixed_dependencies = {
        "normalization.json": {"input.json"},
        "normalized_case.json": (
            {"input.json", "normalization.json"}
            if "normalization.json" in by_name
            else {"input.json"}
        ),
        "assumptions.json": {"input.json"},
        "evidence.jsonl": {"input.json"},
        "approvals.json": {"input.json"},
        "assignments.json": {"normalized_case.json"},
        "agent_execution_records.json": {"assignments.json", "normalized_case.json"},
        "security_events.json": {"input.json"},
    }
    for logical_name, dependencies in fixed_dependencies.items():
        if logical_name in by_name:
            require_payload_sources(logical_name, dependencies)
    if "normalization.json" in by_name:
        if "input.json" not in parsed or "normalized_case.json" not in parsed:
            raise CanonicalStorageError(
                "normalization artifact requires input and normalized case artifacts"
            )
        try:
            verify_normalization_binding(
                cast(CaseInput, parsed["input.json"]),
                cast(CaseInput, parsed["normalized_case.json"]),
                cast(NormalizationResultV1, parsed["normalization.json"]),
            )
        except ValueError as exc:
            raise CanonicalStorageError("normalization artifact binding mismatch") from exc

    tool_input_names = sorted(
        (name for name in by_name if _VARIABLE_TOOL_INPUT.fullmatch(name)),
        key=lambda item: item.encode("utf-8"),
    )
    tool_result_names = sorted(
        (
            name
            for name in by_name
            if _VARIABLE_TOOL_RESULT.fullmatch(name) and not name.endswith(".input.json")
        ),
        key=lambda item: item.encode("utf-8"),
    )
    tool_bindings_by_result: dict[str, ToolBindingV1] = {}
    for logical_name in tool_input_names:
        require_payload_sources(logical_name, {"input.json", "normalized_case.json"})
        entry = by_name[logical_name]
        identifier = cast(
            re.Match[str],
            _VARIABLE_TOOL_INPUT.fullmatch(logical_name),
        ).group("identifier")
        bindings = [
            binding for binding in entry.provenance_bindings if isinstance(binding, ToolBindingV1)
        ]
        if len(bindings) != 1:
            raise CanonicalStorageError("tool input requires exactly one tool binding")
        binding = bindings[0]
        contract = contract_by_name().get(binding.request_tool_name)
        if contract is None:
            raise CanonicalStorageError("tool input names an unknown registry contract")
        try:
            contract.input_model.model_validate_json(
                canonical_json_bytes(parsed[logical_name]),
                strict=True,
            )
        except ValidationError as exc:
            raise CanonicalStorageError("tool input fails its registry contract") from exc
        if (
            binding.requested_contract_version is not None
            and binding.requested_contract_version != contract.contract_version
        ):
            raise CanonicalStorageError("tool request contract version is unsupported")
        tool_request = ToolRequest(
            request_id=binding.request_id,
            tool_name=binding.request_tool_name,
            input=cast(dict[str, Any], parsed[logical_name]),
            requested_by=binding.requested_by,
            requires_approval=binding.requires_approval,
            contract_version=binding.requested_contract_version,
        )
        input_sha = phase_canonical_sha256(tool_request.input)
        if (
            binding.result_id != identifier
            or binding.request_input_artifact_sha256 != entry.sha256
            or binding.request_input_sha256 != input_sha
            or binding.validated_result_input_sha256 != input_sha
            or binding.tool_request_sha256 != phase_canonical_sha256(tool_request)
        ):
            raise CanonicalStorageError("tool input binding correlation mismatch")

    for logical_name in tool_result_names:
        identifier = cast(
            re.Match[str],
            _VARIABLE_TOOL_RESULT.fullmatch(logical_name),
        ).group("identifier")
        paired_input = f"tool_results/{identifier}.input.json"
        require_payload_sources(logical_name, {paired_input})
        entry = by_name[logical_name]
        result = cast(ToolResult, parsed[logical_name])
        bindings = [
            binding for binding in entry.provenance_bindings if isinstance(binding, ToolBindingV1)
        ]
        if len(bindings) != 1:
            raise CanonicalStorageError("tool result requires exactly one tool binding")
        binding = bindings[0]
        input_bindings = bindings_of_type(paired_input, ToolBindingV1)
        if final_report_v2 and (
            len(input_bindings) != 1
            or canonical_json_bytes(input_bindings[0]) != canonical_json_bytes(binding)
        ):
            raise CanonicalStorageError(
                "final-report v2 tool input/result bindings must be byte-identical"
            )
        input_entry = by_name.get(paired_input)
        input_value = cast(dict[str, Any], parsed.get(paired_input))
        tool_request = ToolRequest(
            request_id=binding.request_id,
            tool_name=binding.request_tool_name,
            input=input_value,
            requested_by=binding.requested_by,
            requires_approval=binding.requires_approval,
            contract_version=binding.requested_contract_version,
        )
        if (
            input_entry is None
            or binding.result_id != result.result_id
            or binding.result_id != identifier
            or binding.result_tool_name != result.tool_name
            or binding.result_artifact_sha256 != entry.sha256
            or binding.request_input_artifact_sha256 != input_entry.sha256
            or binding.materialized_result_input_sha256 != phase_canonical_sha256(result.input)
            or binding.result_contract_version != result.contract_version
            or binding.supported_contract_version != binding.result_contract_version
        ):
            raise CanonicalStorageError("tool result binding correlation mismatch")
        try:
            ToolExecutionBinding(
                run_id=binding.run_id,
                phase_attempt_id=binding.phase_attempt_id,
                ordinal=binding.ordinal,
                request=tool_request,
                request_input_sha256=binding.request_input_sha256,
                validated_result_input_sha256=binding.validated_result_input_sha256,
                materialized_result_input_sha256=binding.materialized_result_input_sha256,
                requested_contract_version=binding.requested_contract_version,
                supported_contract_version=binding.supported_contract_version,
                result_contract_version=binding.result_contract_version,
                result=result,
            )
        except ValidationError as exc:
            raise CanonicalStorageError("tool execution binding replay failed") from exc
        tool_bindings_by_result[logical_name] = binding

    if final_report_v2:
        input_ids = {
            cast(re.Match[str], _VARIABLE_TOOL_INPUT.fullmatch(name)).group("identifier")
            for name in tool_input_names
        }
        result_ids = {
            cast(re.Match[str], _VARIABLE_TOOL_RESULT.fullmatch(name)).group("identifier")
            for name in tool_result_names
        }
        if input_ids != result_ids:
            raise CanonicalStorageError(
                "final-report v2 requires exactly one input/result pair per tool execution"
            )
        ordinals = sorted(binding.ordinal for binding in tool_bindings_by_result.values())
        if ordinals != list(range(len(tool_bindings_by_result))):
            raise CanonicalStorageError(
                "final-report v2 tool ordinals must be unique and contiguous from zero"
            )

    evidence_records = {
        record.evidence_id: record
        for record in cast(tuple[EvidenceRecord, ...], parsed.get("evidence.jsonl", ()))
    }
    if "evidence.jsonl" in by_name:
        entry = by_name["evidence.jsonl"]
        external = {
            binding.source_id: binding
            for binding in entry.provenance_bindings
            if isinstance(binding, SourceBindingV1) and binding.source_kind == "external_evidence"
        }
        if set(external) != set(evidence_records):
            raise CanonicalStorageError("evidence JSONL external source set mismatch")
        for evidence_id, evidence_record in evidence_records.items():
            source = external[evidence_id]
            if (
                source.consumer_record_id != evidence_id
                or source.source_logical_name is not None
                or source.source_schema_version is not None
                or source.source_sha256
                != canonical_domain_sha256(EVIDENCE_SOURCE_HASH_DOMAIN, evidence_record)
            ):
                raise CanonicalStorageError("evidence record source correlation mismatch")

    for logical_name in report_names:
        agent_report = cast(AgentReport, parsed[logical_name])
        expected = {"assignments.json", "normalized_case.json"}
        referenced_evidence = tuple(
            dict.fromkeys(
                (
                    *agent_report.evidence_ids,
                    *(
                        evidence_id
                        for claim in agent_report.claims
                        for evidence_id in claim.evidence_ids
                    ),
                )
            )
        )
        if referenced_evidence:
            if any(evidence_id not in evidence_records for evidence_id in referenced_evidence):
                raise CanonicalStorageError("agent report references unknown evidence")
            expected.add("evidence.jsonl")
        for result_id in dict.fromkeys(agent_report.tool_result_ids):
            result_name = f"tool_results/{result_id}.json"
            if result_name not in by_name:
                raise CanonicalStorageError("agent report references unknown tool result")
            expected.add(result_name)
        require_payload_sources(logical_name, expected)

    execution_contexts: set[bytes] = set()
    execution_phases: set[bytes] = set()
    if "agent_execution_records.json" in by_name:
        execution_entry_name = "agent_execution_records.json"
        context_bindings = bindings_of_type(execution_entry_name, ContextBindingV1)
        phase_bindings = bindings_of_type(execution_entry_name, PhaseBindingV1)
        records = cast(tuple[AgentExecutionRecord, ...], parsed[execution_entry_name])
        for execution_record in records:
            if (
                execution_record.context_id is None
                or execution_record.context_attempt_id is None
                or execution_record.context_schema_version is None
                or execution_record.context_classification is None
                or execution_record.context_payload_sha256 is None
                or execution_record.context_source_sha256 is None
                or execution_record.context_policy_sha256 is None
                or execution_record.context_envelope_sha256 is None
                or execution_record.context_expires_at is None
                or execution_record.context_producer_runtime is None
                or execution_record.context_consumer_runtime is None
            ):
                raise CanonicalStorageError(
                    "agent execution record lacks complete context correlation"
                )
            matches = tuple(
                binding
                for binding in context_bindings
                if binding.context_id == execution_record.context_id
                and binding.attempt_id == execution_record.context_attempt_id
            )
            if len(matches) != 1:
                raise CanonicalStorageError("agent execution context binding is not unique")
            context_binding = matches[0]
            if (
                context_binding.context_sha256 != execution_record.context_sha256
                or context_binding.parent_context_id != execution_record.parent_context_id
                or context_binding.schema_version != execution_record.context_schema_version
                or context_binding.classification.value != execution_record.context_classification
                or context_binding.payload_sha256 != execution_record.context_payload_sha256
                or context_binding.source_sha256 != execution_record.context_source_sha256
                or context_binding.policy_sha256 != execution_record.context_policy_sha256
                or context_binding.envelope_sha256 != execution_record.context_envelope_sha256
                or context_binding.expires_at != execution_record.context_expires_at
                or context_binding.producer_runtime != execution_record.context_producer_runtime
                or context_binding.consumer_runtime != execution_record.context_consumer_runtime
            ):
                raise CanonicalStorageError("agent execution context correlation mismatch")
        if records and not phase_bindings:
            raise CanonicalStorageError("agent execution ledger lacks phase provenance")
        execution_contexts = {canonical_json_bytes(binding) for binding in context_bindings}
        execution_phases = {canonical_json_bytes(binding) for binding in phase_bindings}

    for logical_name in report_names:
        contexts = bindings_of_type(logical_name, ContextBindingV1)
        phases = bindings_of_type(logical_name, PhaseBindingV1)
        if not contexts or not phases:
            raise CanonicalStorageError("agent report lacks context or phase provenance")
        if execution_contexts and any(
            canonical_json_bytes(binding) not in execution_contexts for binding in contexts
        ):
            raise CanonicalStorageError("agent report context is absent from execution ledger")
        if execution_phases and any(
            canonical_json_bytes(binding) not in execution_phases for binding in phases
        ):
            raise CanonicalStorageError("agent report phase is absent from execution ledger")

    for logical_name in (*tool_input_names, *tool_result_names):
        if len(bindings_of_type(logical_name, PhaseBindingV1)) != 1:
            raise CanonicalStorageError("tool artifact requires exactly one phase binding")

    if "disputes.json" in by_name:
        require_payload_sources(
            "disputes.json",
            set(report_names) | set(tool_result_names),
        )

    if "final_report.json" in by_name:
        required_base = {
            "input.json",
            "normalized_case.json",
            "assumptions.json",
            "evidence.jsonl",
            "approvals.json",
            "assignments.json",
            "agent_execution_records.json",
            "security_events.json",
            "disputes.json",
        }
        if not required_base <= set(by_name):
            raise CanonicalStorageError("final report is missing a required direct ledger")
        require_payload_sources(
            "final_report.json",
            required_base | set(report_names) | set(tool_input_names) | set(tool_result_names),
        )
        final_report_json = cast(FinalReport, parsed["final_report.json"])
        ledger_pairs = (
            ("evidence.jsonl", tuple(final_report_json.evidence)),
            ("approvals.json", tuple(final_report_json.approvals)),
            (
                "agent_execution_records.json",
                tuple(final_report_json.agent_execution_records),
            ),
            ("security_events.json", tuple(final_report_json.security_events)),
            ("disputes.json", tuple(final_report_json.disputes)),
        )
        for ledger_name, embedded in ledger_pairs:
            if tuple(cast(Sequence[Any], parsed[ledger_name])) != embedded:
                raise CanonicalStorageError("final report embedded ledger mismatch")
        if final_report_v2:
            ordered_tool_result_names = sorted(
                tool_result_names,
                key=lambda name: tool_bindings_by_result[name].ordinal,
            )
        else:
            ordered_tool_result_names = tool_result_names
        tool_results = tuple(cast(ToolResult, parsed[name]) for name in ordered_tool_result_names)
        if tuple(final_report_json.tool_results) != tool_results:
            raise CanonicalStorageError("final report embedded tool results mismatch")
        input_case = cast(CaseInput, parsed["input.json"])
        try:
            verify_versioned_range_tool_chain(
                input_case,
                tool_results,
                run_status=final_report_json.run_status,
            )
        except ValueError as exc:
            raise CanonicalStorageError("versioned range tool chain replay failed") from exc
        final_contexts = bindings_of_type("final_report.json", ContextBindingV1)
        if final_report_v2:
            provider_trace_present = bool(report_names) or bool(
                cast(Sequence[Any], parsed["agent_execution_records.json"])
            )
            if provider_trace_present:
                if not final_contexts:
                    raise CanonicalStorageError(
                        "final-report v2 lacks required provider context provenance"
                    )
                if not execution_contexts or any(
                    canonical_json_bytes(binding) not in execution_contexts
                    for binding in final_contexts
                ):
                    raise CanonicalStorageError(
                        "final-report v2 context is absent from the execution ledger"
                    )
            elif final_contexts:
                raise CanonicalStorageError(
                    "final-report v2 carries spurious provider context provenance"
                )
        elif not final_contexts:
            raise CanonicalStorageError("final report lacks context provenance")
        if not bindings_of_type("final_report.json", PhaseBindingV1):
            raise CanonicalStorageError("final report lacks phase provenance")
        if len(bindings_of_type("final_report.json", BudgetPolicyBindingV1)) != 1:
            raise CanonicalStorageError("final report requires one budget policy binding")

    if "final_report.md" in by_name:
        require_payload_sources("final_report.md", {"final_report.json"})
        if "final_report.json" not in parsed:
            raise CanonicalStorageError("markdown report requires final_report.json")
        final_report = cast(FinalReport, parsed["final_report.json"])
        markdown_entry = by_name["final_report.md"]
        json_entry = by_name["final_report.json"]
        report_bindings = [
            binding
            for binding in markdown_entry.provenance_bindings
            if isinstance(binding, ReportBindingV1)
        ]
        if len(report_bindings) != 1 or cast(str, parsed["final_report.md"]) != render_markdown(
            final_report
        ):
            raise CanonicalStorageError("markdown report renderer correlation mismatch")
        report_binding = report_bindings[0]
        if (
            report_binding.report_id != final_report.run_id
            or report_binding.report_schema_version != json_entry.artifact_schema_version
            or report_binding.report_json_sha256 != json_entry.sha256
            or report_binding.rendered_markdown_sha256 != markdown_entry.sha256
            or report_binding.upstream_source_sha256 != json_entry.source_sha256
        ):
            raise CanonicalStorageError("markdown report binding correlation mismatch")

    approvals = parsed.get("approvals.json")
    if approvals is not None:
        approval_entry = by_name["approvals.json"]
        decision_bindings = {
            binding.approval_id: binding
            for binding in approval_entry.provenance_bindings
            if isinstance(binding, ApprovalDecisionBindingV1)
        }
        external_sources = {
            binding.source_id: binding
            for binding in approval_entry.provenance_bindings
            if isinstance(binding, SourceBindingV1) and binding.source_kind == "external_evidence"
        }
        decided_ids = {
            approval.approval_id for approval in approvals if approval.status.value != "pending"
        }
        if set(decision_bindings) != decided_ids:
            raise CanonicalStorageError("approval decision provenance set mismatch")
        expected_external_ids = {
            decision.external_source_id for decision in decision_bindings.values()
        }
        if set(external_sources) != expected_external_ids:
            raise CanonicalStorageError("approval external decision source set mismatch")
        for approval in approvals:
            if approval.status.value == "pending":
                if approval.approval_id in decision_bindings:
                    raise CanonicalStorageError("pending approval cannot carry decision evidence")
                continue
            decision = decision_bindings.get(approval.approval_id)
            if (
                decision is None
                or approval.decision_reason is None
                or approval.decided_at is None
                or decision.decision != approval.status.value
                or decision.decided_at != approval.decided_at
                or decision.external_source_id != f"approval-decision-{approval.approval_id}"
                or decision.decision_reason_sha256
                != domain_sha256(
                    APPROVAL_REASON_HASH_DOMAIN,
                    approval.decision_reason.encode("utf-8"),
                )
            ):
                raise CanonicalStorageError("decided approval lacks exact decision provenance")
            projection = {
                "approval_id": approval.approval_id,
                "status": approval.status,
                "decision_reason": approval.decision_reason,
                "decided_at": approval.decided_at,
            }
            decision_source = external_sources.get(decision.external_source_id)
            if (
                decision_source is None
                or decision_source.source_logical_name is not None
                or decision_source.source_schema_version is not None
                or decision_source.consumer_record_id is not None
                or decision_source.source_sha256
                != canonical_domain_sha256(APPROVAL_EVIDENCE_HASH_DOMAIN, projection)
            ):
                raise CanonicalStorageError("approval decision source evidence mismatch")


def parsed_bytes(entry: PayloadInventoryEntryV1, parsed: Mapping[str, Any]) -> bytes:
    value = parsed[entry.logical_name]
    if entry.serialization == JSONL_SERIALIZATION:
        return b"".join(canonical_json_bytes(item) + b"\n" for item in value)
    if entry.serialization == TEXT_SERIALIZATION:
        return cast(str, value).encode("utf-8")
    return canonical_json_bytes(value)


def build_inventory(
    request: RevisionPublishRequestV1,
    *,
    max_artifact_bytes: int,
) -> tuple[
    tuple[PayloadInventoryEntryV1, ...],
    tuple[ProvenanceHeadV1, ...],
    dict[str, Any],
]:
    validate_run_id(request.run_id)
    if not request.artifacts:
        raise CanonicalStorageError("revision requires at least one artifact")
    budget_artifacts = tuple(
        artifact for artifact in request.artifacts if artifact.logical_name == "budget_state.json"
    )
    if budget_artifacts and (
        len(request.artifacts) != 1
        or len(budget_artifacts) != 1
        or request.producer_id != DURABLE_BUDGET_PRODUCER_ID
        or request.producer_version != DURABLE_BUDGET_PRODUCER_VERSION
    ):
        raise CanonicalStorageError(
            "durable budget state requires its dedicated producer and exclusive revision"
        )
    isolated_names = {
        "isolated_job_state.json",
        "stdout.txt",
        "stderr.txt",
    }
    present_isolated = isolated_names & {artifact.logical_name for artifact in request.artifacts}
    isolated_producer = request.producer_id == ISOLATED_JOB_PRODUCER_ID
    if (
        isolated_producer
        and (
            present_isolated != isolated_names
            or len(request.artifacts) != len(isolated_names)
            or request.producer_version != ISOLATED_JOB_PRODUCER_VERSION
        )
    ) or (present_isolated and not isolated_producer):
        raise CanonicalStorageError(
            "isolated-job state requires its dedicated producer and complete artifact set"
        )
    parsed: dict[str, Any] = {}
    artifacts = sorted(request.artifacts, key=lambda item: payload_order_key(item.logical_name))
    logical_names: set[str] = set()
    path_aliases: set[str] = set()
    inventories: list[PayloadInventoryEntryV1] = []
    for artifact in artifacts:
        if artifact.logical_name in logical_names:
            raise CanonicalStorageError("duplicate logical artifact")
        logical_names.add(artifact.logical_name)
        alias = ascii_casefold(artifact.logical_name)
        if alias in path_aliases:
            raise CanonicalStorageError("ASCII-case artifact path alias")
        path_aliases.add(alias)
        parsed[artifact.logical_name] = validate_artifact(
            artifact,
            request.run_id,
            max_artifact_bytes,
        )
        inventory = PayloadInventoryEntryV1(
            logical_name=artifact.logical_name,
            revision_relative_path=f"payload/{artifact.logical_name}",
            media_type=artifact.media_type,
            artifact_schema_version=artifact.artifact_schema_version,
            serialization=artifact.serialization,
            size_bytes=len(artifact.exact_bytes),
            sha256=sha256_bytes(artifact.exact_bytes),
            required=artifact.required,
            classification=artifact.classification,
            classification_source=artifact.classification_source,
            classification_evidence=artifact.classification_evidence,
            classification_evidence_sha256=classification_evidence_sha256(
                artifact.classification_evidence
            ),
            source_sha256=upstream_source_sha256(artifact.provenance_bindings),
            provenance_bindings=canonicalize_bindings(artifact.provenance_bindings),
        )
        inventories.append(inventory)
    if isolated_producer:
        state = cast(
            DurableIsolatedJobStateV1,
            parsed["isolated_job_state.json"],
        )
        exact = {artifact.logical_name: artifact.exact_bytes for artifact in artifacts}
        stdout = exact["stdout.txt"]
        stderr = exact["stderr.txt"]
        evidence = state.evidence
        if evidence is None:
            if stdout or stderr:
                raise CanonicalStorageError("isolated-job output requires exact process evidence")
        elif (
            evidence.stdout_bytes != len(stdout)
            or evidence.stderr_bytes != len(stderr)
            or evidence.stdout_sha256 != hashlib.sha256(stdout).hexdigest()
            or evidence.stderr_sha256 != hashlib.sha256(stderr).hexdigest()
        ):
            raise CanonicalStorageError("isolated-job output/evidence binding mismatch")
        context = state.context_binding
        expected_context = ContextBindingV1(
            context_sha256=context.integrity_sha256,
            context_id=context.context_id,
            attempt_id=context.attempt_id,
            parent_context_id=context.parent_context_id,
            schema_version=context.schema_version,
            classification=ContextClassification.INTERNAL,
            payload_sha256=context.payload_sha256,
            source_sha256=context.source_sha256,
            policy_sha256=context.policy_sha256,
            envelope_sha256=context.integrity_sha256,
            expires_at=context.expires_at,
            producer_runtime="python-local",
            consumer_runtime="python-local",
        )
        expected_budget = BudgetPolicyBindingV1(
            policy_schema_version="2.0.0",
            policy_sha256=state.budget_binding.policy_sha256,
        )
        for entry in inventories:
            local_bindings = tuple(
                binding
                for binding in entry.provenance_bindings
                if isinstance(binding, LocalDataBindingV1)
            )
            context_bindings = tuple(
                binding
                for binding in entry.provenance_bindings
                if isinstance(binding, ContextBindingV1)
            )
            budget_bindings = tuple(
                binding
                for binding in entry.provenance_bindings
                if isinstance(binding, BudgetPolicyBindingV1)
            )
            if (
                len(entry.provenance_bindings) != 3
                or len(local_bindings) != 1
                or context_bindings != (expected_context,)
                or budget_bindings != (expected_budget,)
            ):
                raise CanonicalStorageError(
                    "isolated-job artifacts require exact local/context/budget provenance"
                )
    result = tuple(inventories)
    _validate_source_graph(result, parsed, run_id=request.run_id)
    return result, provenance_heads(result), parsed


def inventory_sha256(inventory: Sequence[PayloadInventoryEntryV1]) -> str:
    return canonical_domain_sha256(INVENTORY_HASH_DOMAIN, tuple(inventory))


def transaction_sha256(transaction_without_digest: Mapping[str, Any]) -> str:
    if "transaction_sha256" in transaction_without_digest:
        raise CanonicalStorageError("transaction digest input must omit its digest")
    return canonical_domain_sha256(TRANSACTION_HASH_DOMAIN, transaction_without_digest)


def recovery_claim_sha256(claim_without_digest: Mapping[str, Any]) -> str:
    if "claim_sha256" in claim_without_digest:
        raise CanonicalStorageError("claim digest input must omit its digest")
    return canonical_domain_sha256(RECOVERY_CLAIM_HASH_DOMAIN, claim_without_digest)


def ownership_marker_sha256(marker: BaseModel) -> str:
    return sha256_bytes(canonical_json_bytes(marker))


def legacy_root_identity_sha256(path: Path) -> str:
    resolved = path.resolve(strict=True)
    stat = resolved.stat()
    normalized_path = unicodedata.normalize("NFC", os.path.normcase(str(resolved)))
    value = {
        "adapter": platform_adapter(),
        "path": normalized_path,
        "st_dev": int(stat.st_dev),
        "st_ino": int(stat.st_ino),
    }
    return canonical_domain_sha256(LEGACY_ROOT_IDENTITY_HASH_DOMAIN, value)


def authority_identity_sha256(stat: os.stat_result) -> str:
    return canonical_domain_sha256(
        AUTHORITY_IDENTITY_HASH_DOMAIN,
        {
            "adapter": platform_adapter(),
            "st_dev": int(stat.st_dev),
            "st_ino": int(stat.st_ino),
        },
    )


def check_path_lengths(paths: Iterable[Path]) -> None:
    for path in paths:
        resolved = path.resolve(strict=False)
        if os.name == "nt":
            absolute_units = len(str(resolved).encode("utf-16-le")) // 2 + 1
            if absolute_units > 260:
                raise CanonicalStorageError("Windows path exceeds conservative legacy limit")
            for segment in resolved.parts:
                segment_units = len(segment.encode("utf-16-le")) // 2 + 1
                if segment_units > 256:
                    raise CanonicalStorageError("Windows segment exceeds conservative limit")
        else:
            parent = resolved.anchor or "/"
            pathconf = getattr(os, "pathconf", None)
            if pathconf is None:
                raise CanonicalStorageError("platform path bounds are unavailable")
            try:
                path_max = int(pathconf(parent, "PC_PATH_MAX"))
                name_max = int(pathconf(parent, "PC_NAME_MAX"))
            except (OSError, ValueError) as exc:
                raise CanonicalStorageError("platform path bounds are unavailable") from exc
            if len(os.fsencode(resolved)) + 1 > path_max:
                raise CanonicalStorageError("POSIX path exceeds PATH_MAX")
            if any(len(os.fsencode(segment)) + 1 > name_max for segment in resolved.parts):
                raise CanonicalStorageError("POSIX segment exceeds NAME_MAX")


def ensure_strict_positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CanonicalStorageError(f"{name} must be a strict positive integer")
    return value


__all__ = [
    "AUTHORITY_IDENTITY_HASH_DOMAIN",
    "CanonicalStorageError",
    "artifact_table_entry",
    "authority_identity_sha256",
    "binding_sha256",
    "build_inventory",
    "canonical_domain_sha256",
    "canonical_json_bytes",
    "check_path_lengths",
    "classification_evidence_sha256",
    "domain_sha256",
    "ensure_strict_positive_int",
    "format_storage_utc",
    "inventory_sha256",
    "legacy_root_identity_sha256",
    "parse_canonical_json",
    "parse_canonical_jsonl",
    "parse_canonical_model",
    "payload_order_key",
    "payload_source_id",
    "platform_adapter",
    "provenance_heads",
    "recovery_claim_sha256",
    "run_id_sha256",
    "run_lock_key_sha256",
    "sha256_bytes",
    "transaction_sha256",
    "upstream_source_sha256",
    "validate_artifact",
    "validate_canonical_text",
    "validate_logical_name",
    "validate_run_id",
]
