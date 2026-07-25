"""Canonical bytes, hashes, version dispatch, and inventory checks for P2-012B."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
    LifecycleAuditMetadata,
)
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
    ToolResult,
)
from poker_deliberation.storage.revision_canonical import (
    CONTROL_CANONICALIZATION,
    JSONL_SERIALIZATION,
    TEXT_SERIALIZATION,
    CanonicalStorageError,
    canonical_domain_sha256,
    canonical_json_bytes,
    canonicalize_bindings,
    classification_evidence_sha256,
    domain_sha256,
    parse_canonical_json,
    parse_canonical_jsonl,
    parse_canonical_model,
    sha256_bytes,
    upstream_source_sha256,
    validate_canonical_text,
    validate_logical_name,
)
from poker_deliberation.storage.revision_models import (
    LocalDataBindingV1,
    PayloadInventoryEntryV1,
    ProvenanceBindingV1,
)
from poker_deliberation.storage.terminal_models import (
    TERMINAL_SCHEMA_VERSION,
    BudgetSettlementBindingV2,
    CompletionMarkerV2,
    RunCurrentPointerV2,
    RunManifestV2,
)

TERMINAL_INVENTORY_DOMAIN = "poker-run-terminal-inventory-v2"
REQUIRED_INVENTORY_DOMAIN = "poker-run-required-inventory-v2"
BUDGET_BINDING_DOMAIN = "poker-run-budget-settlement-binding-v2"
EMPTY_HEAD_DOMAIN = "poker-run-terminal-empty-head-v2"
LIFECYCLE_AUDIT_DOMAIN = "poker-run-lifecycle-audit-v2"
LEGACY_SOURCE_INVENTORY_DOMAIN = "poker-run-legacy-source-inventory-v2"

_SUPPORTED_VERSION = TERMINAL_SCHEMA_VERSION
_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){2}$")
T = TypeVar("T", bound=BaseModel)


class UnsupportedTerminalVersion(CanonicalStorageError):
    """A syntactically valid terminal control object uses an unknown version."""


def canonical_terminal_bytes(value: Any) -> bytes:
    return canonical_json_bytes(value)


def _version(data: bytes, field: str) -> str:
    value = parse_canonical_json(data)
    if not isinstance(value, dict):
        raise CanonicalStorageError("terminal control value must be an object")
    version = value.get(field)
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise CanonicalStorageError("terminal control version is invalid")
    if version != _SUPPORTED_VERSION:
        raise UnsupportedTerminalVersion("unsupported terminal storage version")
    return version


def _parse_versioned(data: bytes, model: type[T], *, version_field: str) -> T:
    _version(data, version_field)
    try:
        return parse_canonical_model(data, model)
    except (CanonicalStorageError, ValidationError) as exc:
        raise CanonicalStorageError("terminal control schema is invalid") from exc


def parse_run_manifest(data: bytes) -> RunManifestV2:
    return _parse_versioned(data, RunManifestV2, version_field="run_schema_version")


def parse_completion_marker(data: bytes) -> CompletionMarkerV2:
    return _parse_versioned(data, CompletionMarkerV2, version_field="schema_version")


def parse_current_pointer(data: bytes) -> RunCurrentPointerV2:
    return _parse_versioned(data, RunCurrentPointerV2, version_field="schema_version")


def manifest_sha256(manifest: RunManifestV2 | bytes) -> str:
    data = manifest if isinstance(manifest, bytes) else canonical_terminal_bytes(manifest)
    return sha256_bytes(data)


def completion_marker_sha256(marker: CompletionMarkerV2 | bytes) -> str:
    data = marker if isinstance(marker, bytes) else canonical_terminal_bytes(marker)
    return sha256_bytes(data)


def current_pointer_sha256(pointer: RunCurrentPointerV2 | bytes) -> str:
    data = pointer if isinstance(pointer, bytes) else canonical_terminal_bytes(pointer)
    return sha256_bytes(data)


def budget_binding_sha256(binding: BudgetSettlementBindingV2) -> str:
    return canonical_domain_sha256(BUDGET_BINDING_DOMAIN, binding)


def lifecycle_audit_sha256(data: bytes) -> str:
    return domain_sha256(LIFECYCLE_AUDIT_DOMAIN, data)


def empty_lineage_head_sha256(kind: str) -> str:
    try:
        encoded = kind.encode("ascii")
    except UnicodeEncodeError as exc:
        raise CanonicalStorageError("lineage head kind must be ASCII") from exc
    return domain_sha256(EMPTY_HEAD_DOMAIN, encoded + b"\0" + b"[]")


def _canonical_entries(
    inventory: Sequence[PayloadInventoryEntryV1],
) -> tuple[PayloadInventoryEntryV1, ...]:
    entries = tuple(sorted(inventory, key=lambda item: item.revision_relative_path.encode("utf-8")))
    paths: set[str] = set()
    logical_names: set[str] = set()
    aliases: set[str] = set()
    for entry in entries:
        logical_name = validate_logical_name(entry.logical_name)
        expected_path = f"payload/{logical_name}"
        if entry.revision_relative_path != expected_path:
            raise CanonicalStorageError("terminal inventory relative path mismatch")
        alias = logical_name.lower()
        if (
            entry.revision_relative_path in paths
            or logical_name in logical_names
            or alias in aliases
        ):
            raise CanonicalStorageError("duplicate terminal inventory identity")
        paths.add(entry.revision_relative_path)
        logical_names.add(logical_name)
        aliases.add(alias)
        if entry.classification_evidence_sha256 != classification_evidence_sha256(
            entry.classification_evidence
        ):
            raise CanonicalStorageError("classification evidence hash mismatch")
        if entry.provenance_bindings != canonicalize_bindings(entry.provenance_bindings):
            raise CanonicalStorageError("provenance bindings are not canonical")
        if entry.source_sha256 != upstream_source_sha256(entry.provenance_bindings):
            raise CanonicalStorageError("payload source hash mismatch")
    return entries


def terminal_inventory_sha256(
    inventory: Sequence[PayloadInventoryEntryV1],
) -> str:
    return canonical_domain_sha256(TERMINAL_INVENTORY_DOMAIN, _canonical_entries(inventory))


def required_inventory_sha256(
    inventory: Sequence[PayloadInventoryEntryV1],
) -> str:
    required = tuple(item for item in _canonical_entries(inventory) if item.required)
    if not required:
        raise CanonicalStorageError("terminal revision requires at least one required payload")
    return canonical_domain_sha256(REQUIRED_INVENTORY_DOMAIN, required)


def verify_payload_inventory(
    inventory: Sequence[PayloadInventoryEntryV1],
    payloads: Mapping[str, bytes],
) -> tuple[PayloadInventoryEntryV1, ...]:
    entries = _canonical_entries(inventory)
    expected = {entry.logical_name for entry in entries}
    if set(payloads) != expected:
        raise CanonicalStorageError("terminal payload inventory membership mismatch")
    for entry in entries:
        data = payloads[entry.logical_name]
        if (
            not isinstance(data, bytes)
            or len(data) != entry.size_bytes
            or sha256_bytes(data) != entry.sha256
        ):
            raise CanonicalStorageError("terminal payload size or hash mismatch")
        validate_payload_bytes(entry, data)
    return entries


def _validate_json_models(data: bytes, model: type[BaseModel]) -> None:
    parse_canonical_json(data)
    adapter = TypeAdapter(list[model])  # type: ignore[valid-type]
    try:
        values = adapter.validate_json(data, strict=True)
    except ValidationError:
        try:
            values = adapter.validate_json(data, strict=False)
        except ValidationError as fallback_exc:
            raise CanonicalStorageError("terminal payload list schema mismatch") from fallback_exc
    if canonical_json_bytes(values) != data:
        raise CanonicalStorageError("terminal payload list canonical bytes mismatch")


def _validate_json_value(logical_name: str, data: bytes) -> None:
    single_models: dict[str, type[BaseModel]] = {
        "input.json": CaseInput,
        "normalized_case.json": CaseInput,
        "final_report.json": FinalReport,
    }
    list_models: dict[str, type[BaseModel]] = {
        "assumptions.json": Assumption,
        "approvals.json": ApprovalRequest,
        "assignments.json": AgentAssignment,
        "agent_execution_records.json": AgentExecutionRecord,
        "security_events.json": SecurityEvent,
        "disputes.json": Dispute,
        "lifecycle_audit.json": LifecycleAuditMetadata,
    }
    model = single_models.get(logical_name)
    if model is not None:
        parse_canonical_model(data, model)
        return
    model = list_models.get(logical_name)
    if model is not None:
        _validate_json_models(data, model)
        return
    if logical_name.startswith("agent_reports/"):
        parse_canonical_model(data, AgentReport)
        return
    if logical_name.startswith("tool_results/") and not logical_name.endswith(".input.json"):
        parse_canonical_model(data, ToolResult)
        return
    value = parse_canonical_json(data)
    if logical_name == "state.json" and not isinstance(value, dict):
        raise CanonicalStorageError("state checkpoint must be an object")
    if logical_name.startswith("tool_results/") and not isinstance(value, dict):
        raise CanonicalStorageError("tool input payload must be an object")


def validate_payload_bytes(entry: PayloadInventoryEntryV1, data: bytes) -> None:
    if entry.serialization == CONTROL_CANONICALIZATION:
        _validate_json_value(entry.logical_name, data)
    elif entry.serialization == JSONL_SERIALIZATION:
        if entry.logical_name != "evidence.jsonl":
            raise CanonicalStorageError("unsupported terminal JSONL payload")
        parse_canonical_jsonl(data, EvidenceRecord)
    elif entry.serialization == TEXT_SERIALIZATION:
        validate_canonical_text(data)
    else:
        raise CanonicalStorageError("opaque terminal payloads are not admitted")


def inventory_entry(
    *,
    logical_name: str,
    data: bytes,
    media_type: str,
    artifact_schema_version: str,
    serialization: str,
    required: bool = True,
    classification: ContextClassification = ContextClassification.INTERNAL,
    classification_source: ClassificationSource = ClassificationSource.DEFAULT_INTERNAL,
    classification_evidence: ClassificationEvidence | None = None,
    provenance_bindings: Sequence[ProvenanceBindingV1] = (),
) -> PayloadInventoryEntryV1:
    """Build one exact inventory entry after pure byte/schema admission."""

    validate_logical_name(logical_name)
    evidence = classification_evidence or ClassificationEvidence(
        restricted_secret_check_completed=True
    )
    bindings: tuple[ProvenanceBindingV1, ...]
    if provenance_bindings:
        bindings = canonicalize_bindings(provenance_bindings)
    else:
        local = LocalDataBindingV1(
            logical_name=logical_name,
            classification=classification,
            classification_source=classification_source,
            classification_evidence=evidence,
            classification_evidence_sha256=classification_evidence_sha256(evidence),
        )
        bindings = (local,)
    entry = PayloadInventoryEntryV1(
        logical_name=logical_name,
        revision_relative_path=f"payload/{logical_name}",
        media_type=media_type,  # type: ignore[arg-type]
        artifact_schema_version=artifact_schema_version,
        serialization=serialization,  # type: ignore[arg-type]
        size_bytes=len(data),
        sha256=sha256_bytes(data),
        required=required,
        classification=classification,
        classification_source=classification_source,
        classification_evidence=evidence,
        classification_evidence_sha256=classification_evidence_sha256(evidence),
        source_sha256=upstream_source_sha256(bindings),
        provenance_bindings=bindings,
    )
    validate_payload_bytes(entry, data)
    if classification in {
        ContextClassification.SENSITIVE,
        ContextClassification.RESTRICTED,
    }:
        raise CanonicalStorageError("terminal persistence requires separately verified encryption")
    return entry


def legacy_source_inventory_sha256(
    entries: Sequence[Mapping[str, object]],
) -> str:
    ordered = tuple(
        sorted(
            (dict(item) for item in entries),
            key=lambda item: str(item["relative_path"]).encode("utf-8"),
        )
    )
    return canonical_domain_sha256(LEGACY_SOURCE_INVENTORY_DOMAIN, ordered)


__all__ = [
    "UnsupportedTerminalVersion",
    "budget_binding_sha256",
    "canonical_terminal_bytes",
    "completion_marker_sha256",
    "current_pointer_sha256",
    "empty_lineage_head_sha256",
    "inventory_entry",
    "legacy_source_inventory_sha256",
    "lifecycle_audit_sha256",
    "manifest_sha256",
    "parse_completion_marker",
    "parse_current_pointer",
    "parse_run_manifest",
    "required_inventory_sha256",
    "terminal_inventory_sha256",
    "verify_payload_inventory",
]
