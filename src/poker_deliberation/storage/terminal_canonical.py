"""Canonical bytes, hashes, version dispatch, and inventory checks for P2-012B."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, TypeAdapter, ValidationError

from poker_deliberation.approval_canonical import (
    parse_canonical_jsonl as parse_approval_jsonl,
)
from poker_deliberation.approval_canonical import (
    parse_canonical_model as parse_approval_model,
)
from poker_deliberation.approval_models import (
    ApprovalDecisionRecordV2,
    ApprovalDomainAuditEventV2,
    ApprovalLedgerV2,
    ApprovalReissueRecordV2,
)
from poker_deliberation.approvals import (
    project_v1_approvals,
    read_approval_state_v2,
)
from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
    LifecycleAuditMetadata,
)
from poker_deliberation.normalization import (
    NORMALIZATION_CONTRACT_VERSION,
    NORMALIZATION_RESULT_VERSION,
    NormalizationResultV1,
    normalization_result_json_bytes,
    verify_normalization_binding,
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
PRODUCT_LINEAGE_DOMAIN_PREFIX = "poker-product"
APPROVAL_AUTHORITY_LINEAGE_DOMAIN = "poker-product-approval-authority-lineage-v2"
APPROVAL_V2_CORE_ARTIFACTS = frozenset(
    {
        "approval_ledger_v2.json",
        "approval_decisions_v2.jsonl",
        "approval_audit_v2.jsonl",
    }
)
APPROVAL_REISSUE_ARTIFACT = "approval_reissues_v2.jsonl"
APPROVAL_V2_ARTIFACTS = APPROVAL_V2_CORE_ARTIFACTS | {APPROVAL_REISSUE_ARTIFACT}

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


def _lineage_head(kind: str, value: Sequence[object]) -> str:
    if not value:
        return empty_lineage_head_sha256(kind)
    return canonical_domain_sha256(
        f"{PRODUCT_LINEAGE_DOMAIN_PREFIX}-{kind}-lineage-v2",
        value,
    )


def product_payload_commitments(
    payloads: Mapping[str, bytes],
    *,
    run_id: str,
    status: str,
    revision: int | None = None,
    previous_manifest_sha256: str | None = None,
    previous_pointer_sha256: str | None = None,
) -> tuple[str, str, str, str, str, str]:
    """Recompute product input, checkpoint, and scalar lineage commitments."""

    required = {"input.json", "state.json", "final_report.json"}
    if not required <= set(payloads):
        raise CanonicalStorageError("product publication lacks a required core payload")
    input_case = parse_canonical_model(payloads["input.json"], CaseInput)
    if "normalization.json" in payloads:
        if "normalized_case.json" not in payloads:
            raise CanonicalStorageError("normalization payload lacks the normalized case artifact")
        normalization = _parse_normalization_result(payloads["normalization.json"])
        normalized_case = parse_canonical_model(payloads["normalized_case.json"], CaseInput)
        try:
            verify_normalization_binding(input_case, normalized_case, normalization)
        except ValueError as exc:
            raise CanonicalStorageError("normalization payload binding mismatch") from exc
    report = _parse_json_model(payloads["final_report.json"], FinalReport)
    state = parse_canonical_json(payloads["state.json"])
    if not isinstance(state, dict):
        raise CanonicalStorageError("state checkpoint must be an object")
    events = state.get("events")
    if not isinstance(events, list) or any(not isinstance(item, dict) for item in events):
        raise CanonicalStorageError("state checkpoint events must be an object list")
    if report.run_id != run_id:
        raise CanonicalStorageError("final report run ID mismatch")

    expected_public_status = {
        "approval_required": "approval_required",
        "succeeded": "completed",
        "failed": "failed_with_limitations",
        "cancelled": "failed_with_limitations",
        "cancel_unconfirmed": "failed_with_limitations",
    }.get(status)
    expected_machine_state = {
        "approval_required": "HUMAN_REVIEW_REQUIRED",
        "succeeded": "COMPLETED",
        "failed": "FAILED_WITH_LIMITATIONS",
        "cancelled": "FAILED_WITH_LIMITATIONS",
        "cancel_unconfirmed": "FAILED_WITH_LIMITATIONS",
    }.get(status)
    if expected_public_status is not None and (
        report.run_status != expected_public_status or state.get("state") != expected_machine_state
    ):
        raise CanonicalStorageError("product state/report/status correlation mismatch")

    approvals = TypeAdapter(list[ApprovalRequest]).validate_json(
        payloads.get("approvals.json", b"[]")
    )
    present_approval_v2 = APPROVAL_V2_ARTIFACTS & set(payloads)
    approval_authority_head: str | None = None
    if present_approval_v2:
        if not present_approval_v2 >= APPROVAL_V2_CORE_ARTIFACTS:
            raise CanonicalStorageError("authoritative approval artifacts must be complete")
        try:
            approval_state = read_approval_state_v2(
                payloads["approval_ledger_v2.json"],
                payloads["approval_decisions_v2.jsonl"],
                payloads["approval_audit_v2.jsonl"],
                payloads.get(APPROVAL_REISSUE_ARTIFACT, b""),
            )
            projected = project_v1_approvals(approval_state)
        except ValueError as exc:
            raise CanonicalStorageError("authoritative approval artifacts are invalid") from exc
        if approval_state.ledger.run_id != run_id or projected != approvals:
            raise CanonicalStorageError(
                "V1 approval projection differs from authoritative V2 state"
            )
        if revision is not None:
            decisions = approval_state.decision_records
            reissues = approval_state.reissue_records
            checkpoint_revision = revision - len(decisions) - len(reissues)
            successor_creation_revisions = {
                result.successor_request_id: result.successor_created_run_revision
                for record in reissues
                for result in record.outcome.results
            }
            if any(
                request.created_run_revision
                != successor_creation_revisions.get(request.request_id, checkpoint_revision)
                for request in approval_state.ledger.requests
            ):
                raise CanonicalStorageError(
                    "approval request creation is not bound to its checkpoint revision"
                )
            transitions = [
                (
                    record.outcome.previous_run_revision,
                    record.outcome.current_run_revision,
                )
                for record in decisions
            ]
            transitions.extend(
                (
                    record.outcome.previous_run_revision,
                    record.outcome.current_run_revision,
                )
                for record in reissues
            )
            transitions.sort()
            if transitions and (
                transitions[0][0] != checkpoint_revision or transitions[-1][1] != revision
            ):
                raise CanonicalStorageError(
                    "approval mutation chain is not bound to the current revision"
                )
        current_reissue = next(
            (
                record
                for record in approval_state.reissue_records
                if revision is not None and record.outcome.current_run_revision == revision
            ),
            None,
        )
        if current_reissue is not None and (
            current_reissue.previous_manifest_sha256 != previous_manifest_sha256
            or current_reissue.previous_pointer_sha256 != previous_pointer_sha256
        ):
            raise CanonicalStorageError(
                "approval reissue is not bound to the previous terminal lineage"
            )
        authority_commitment = {
            "ledger_sha256": approval_state.ledger_sha256,
            "decision_count": approval_state.ledger.decision_count,
            "decision_log_head_sha256": approval_state.ledger.decision_log_head_sha256,
            "domain_audit_count": approval_state.ledger.domain_audit_count,
            "domain_audit_log_head_sha256": (approval_state.ledger.domain_audit_log_head_sha256),
        }
        if approval_state.reissue_records:
            authority_commitment.update(
                {
                    "reissue_count": len(approval_state.reissue_records),
                    "reissue_log_head_sha256": (approval_state.reissue_records[-1].record_sha256),
                }
            )
        approval_authority_head = canonical_domain_sha256(
            APPROVAL_AUTHORITY_LINEAGE_DOMAIN,
            authority_commitment,
        )
    execution_records = TypeAdapter(list[AgentExecutionRecord]).validate_json(
        payloads.get("agent_execution_records.json", b"[]")
    )
    security_events = TypeAdapter(list[SecurityEvent]).validate_json(
        payloads.get("security_events.json", b"[]")
    )
    disputes = TypeAdapter(list[Dispute]).validate_json(payloads.get("disputes.json", b"[]"))
    evidence = parse_canonical_jsonl(payloads.get("evidence.jsonl", b""), EvidenceRecord)
    tool_results = {
        logical_name.removeprefix("tool_results/").removesuffix(".json"): (
            _parse_json_model(data, ToolResult)
        )
        for logical_name, data in payloads.items()
        if logical_name.startswith("tool_results/")
        and logical_name.endswith(".json")
        and not logical_name.endswith(".input.json")
    }
    tool_inputs = {
        logical_name.removeprefix("tool_results/").removesuffix(".input.json")
        for logical_name in payloads
        if logical_name.startswith("tool_results/") and logical_name.endswith(".input.json")
    }
    if tool_inputs != set(tool_results):
        raise CanonicalStorageError("tool input/result inventory pairing mismatch")
    if report.approvals != approvals:
        raise CanonicalStorageError("final report approval payload correlation mismatch")
    if report.agent_execution_records != execution_records:
        raise CanonicalStorageError("final report execution payload correlation mismatch")
    if report.security_events != security_events:
        raise CanonicalStorageError("final report security payload correlation mismatch")
    if report.disputes != disputes:
        raise CanonicalStorageError("final report dispute payload correlation mismatch")
    if report.evidence != list(evidence):
        raise CanonicalStorageError("final report evidence payload correlation mismatch")
    if len(report.tool_results) != len(tool_results) or any(
        tool_results.get(item.result_id) != item for item in report.tool_results
    ):
        raise CanonicalStorageError("final report tool payload correlation mismatch")
    for result in report.tool_results:
        input_name = f"tool_results/{result.result_id}.input.json"
        if input_name not in payloads or parse_canonical_json(payloads[input_name]) != result.input:
            raise CanonicalStorageError("tool input/result correlation mismatch")

    context_bindings = [
        {
            key: value
            for key, value in record.model_dump(mode="json").items()
            if key == "context_sha256" or key.startswith("context_") or key == "parent_context_id"
        }
        for record in execution_records
    ]
    execution_bindings: list[object] = [
        record.model_dump(mode="json") for record in execution_records
    ]
    execution_bindings.extend(item.model_dump(mode="json") for item in report.tool_results)
    return (
        sha256_bytes(payloads["input.json"]),
        sha256_bytes(payloads["state.json"]),
        _lineage_head("event", events),
        (
            approval_authority_head
            if approval_authority_head is not None
            else _lineage_head(
                "approval",
                [item.model_dump(mode="json") for item in approvals],
            )
        ),
        _lineage_head("context", context_bindings),
        _lineage_head("execution", execution_bindings),
    )


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
    *,
    allow_opaque: bool = False,
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
        validate_payload_bytes(entry, data, allow_opaque=allow_opaque)
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


def _parse_json_model(data: bytes, model: type[T]) -> T:
    parse_canonical_json(data)
    adapter = TypeAdapter(model)
    try:
        value = adapter.validate_json(data, strict=False)
    except ValidationError as exc:
        raise CanonicalStorageError("terminal payload schema mismatch") from exc
    if canonical_json_bytes(value) != data:
        raise CanonicalStorageError("terminal payload canonical bytes mismatch")
    return value


def _validate_json_model(data: bytes, model: type[BaseModel]) -> None:
    _parse_json_model(data, model)


def _parse_normalization_result(data: bytes) -> NormalizationResultV1:
    value = parse_canonical_json(data)
    if not isinstance(value, dict):
        raise CanonicalStorageError("normalization payload must be an object")
    if (
        value.get("result_version") != NORMALIZATION_RESULT_VERSION
        or value.get("contract_version") != NORMALIZATION_CONTRACT_VERSION
    ):
        raise UnsupportedTerminalVersion("unsupported normalization payload version")
    result = _parse_json_model(data, NormalizationResultV1)
    if normalization_result_json_bytes(result) != data:
        raise CanonicalStorageError("normalization payload canonical bytes mismatch")
    return result


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
        _validate_json_model(data, model)
        return
    if logical_name == "normalization.json":
        _parse_normalization_result(data)
        return
    if logical_name == "approval_ledger_v2.json":
        parse_approval_model(data, ApprovalLedgerV2)
        return
    model = list_models.get(logical_name)
    if model is not None:
        _validate_json_models(data, model)
        return
    if logical_name.startswith("agent_reports/"):
        parse_canonical_model(data, AgentReport)
        return
    if logical_name.startswith("tool_results/") and not logical_name.endswith(".input.json"):
        _validate_json_model(data, ToolResult)
        return
    value = parse_canonical_json(data)
    if logical_name == "state.json" and not isinstance(value, dict):
        raise CanonicalStorageError("state checkpoint must be an object")
    if logical_name.startswith("tool_results/") and not isinstance(value, dict):
        raise CanonicalStorageError("tool input payload must be an object")


def validate_payload_bytes(
    entry: PayloadInventoryEntryV1,
    data: bytes,
    *,
    allow_opaque: bool = False,
) -> None:
    if entry.serialization == CONTROL_CANONICALIZATION:
        _validate_json_value(entry.logical_name, data)
    elif entry.serialization == JSONL_SERIALIZATION:
        if entry.logical_name == "approval_decisions_v2.jsonl":
            parse_approval_jsonl(data, ApprovalDecisionRecordV2)
        elif entry.logical_name == "approval_audit_v2.jsonl":
            parse_approval_jsonl(data, ApprovalDomainAuditEventV2)
        elif entry.logical_name == APPROVAL_REISSUE_ARTIFACT:
            parse_approval_jsonl(data, ApprovalReissueRecordV2)
        elif entry.logical_name == "evidence.jsonl":
            parse_canonical_jsonl(data, EvidenceRecord)
        else:
            raise CanonicalStorageError("unsupported terminal JSONL payload")
    elif entry.serialization == TEXT_SERIALIZATION:
        validate_canonical_text(data)
    elif entry.serialization == "opaque-bytes-v1" and allow_opaque:
        if not isinstance(data, bytes):
            raise CanonicalStorageError("legacy opaque payload must be exact bytes")
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
    "product_payload_commitments",
    "required_inventory_sha256",
    "terminal_inventory_sha256",
    "verify_payload_inventory",
]
