"""Read-only flat-v1 inspection and exact-byte copy planning."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter, ValidationError

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
)
from poker_deliberation.normalization import NormalizationResultV1
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
from poker_deliberation.security import redact_sensitive
from poker_deliberation.state_machine import RunState
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    artifact_table_entry,
    canonical_domain_sha256,
    classification_evidence_sha256,
    legacy_root_identity_sha256,
    run_id_sha256,
    sha256_bytes,
    upstream_source_sha256,
    validate_logical_name,
    validate_run_id,
)
from poker_deliberation.storage.revision_lock import (
    verify_directory,
    verify_regular_single_link,
)
from poker_deliberation.storage.revision_models import (
    LocalDataBindingV1,
    PayloadInventoryEntryV1,
)
from poker_deliberation.storage.terminal_canonical import (
    legacy_source_inventory_sha256,
)
from poker_deliberation.storage.terminal_models import (
    LegacySourceBindingV2,
    ProductRunError,
    ProductRunFailureCode,
    ProductRunFailureV2,
    RunReadStatus,
    VerifiedPayloadV2,
)

LEGACY_ADAPTER_VERSION = "1.0.0"
LEGACY_SENTINEL = ".poker-deliberation-run"
LEGACY_SENTINEL_BYTES = b"v1\n"
LEGACY_MAX_FILES = 4096
LEGACY_MISSING_GUARANTEES = tuple(
    sorted(
        (
            "no_atomic_current_pointer",
            "no_completion_marker",
            "no_durable_budget_settlement",
            "no_hash_inventory",
            "no_lineage_proof",
            "no_versioned_manifest",
        ),
        key=lambda item: item.encode("utf-8"),
    )
)

_LIST_MODELS: dict[str, type[BaseModel]] = {
    "assumptions.json": Assumption,
    "approvals.json": ApprovalRequest,
    "assignments.json": AgentAssignment,
    "agent_execution_records.json": AgentExecutionRecord,
    "security_events.json": SecurityEvent,
    "disputes.json": Dispute,
}
_SINGLE_MODELS: dict[str, type[BaseModel]] = {
    "input.json": CaseInput,
    "normalization.json": NormalizationResultV1,
    "normalized_case.json": CaseInput,
    "final_report.json": FinalReport,
}


@dataclass(frozen=True, slots=True)
class LegacySourceArtifact:
    relative_path: str
    media_type: str
    artifact_schema_version: str
    exact_bytes: bytes

    @property
    def inventory_observation(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "artifact_schema_version": self.artifact_schema_version,
            "size_bytes": len(self.exact_bytes),
            "sha256": sha256_bytes(self.exact_bytes),
        }


@dataclass(frozen=True, slots=True)
class LegacyRunSnapshot:
    run_id: str
    source_root_identity_sha256: str
    source_run_id_sha256: str
    source_inventory_sha256: str
    artifacts: tuple[LegacySourceArtifact, ...]
    final_report: FinalReport | None

    def payload_bytes(self) -> dict[str, bytes]:
        return {
            artifact.relative_path: bytes(artifact.exact_bytes)
            for artifact in self.artifacts
            if artifact.relative_path != LEGACY_SENTINEL
        }


def legacy_failure(
    run_id: str,
    code: ProductRunFailureCode,
    *,
    stage: str,
    filesystem_effect: str = "none",
    reconciliation_required: bool = False,
) -> ProductRunError:
    try:
        digest = run_id_sha256(run_id)
    except ValueError:
        digest = canonical_domain_sha256(
            "poker-invalid-legacy-run-id-v2",
            {"run_id": run_id},
        )
    return ProductRunError(
        ProductRunFailureV2(
            code=code,
            stage=stage,
            read_status=(
                RunReadStatus.LEGACY_UNVERIFIED
                if code is ProductRunFailureCode.LEGACY_RUN_UNVERIFIED
                else None
            ),
            message_code=code.value,
            retryable=False,
            reconciliation_required=reconciliation_required,
            filesystem_effect=filesystem_effect,  # type: ignore[arg-type]
            domain_effect="current_unchanged",
            previous_revision_effect="not_applicable",
            run_id_sha256=digest,
        )
    )


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result or unicodedata.normalize("NFC", key) != key:
            raise CanonicalStorageError("legacy JSON contains a duplicate or non-NFC key")
        result[key] = value
    return result


def _legacy_json(data: bytes) -> Any:
    if data.startswith(b"\xef\xbb\xbf"):
        raise CanonicalStorageError("legacy JSON cannot contain a BOM")
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CanonicalStorageError("legacy JSON is not UTF-8") from exc
    if unicodedata.normalize("NFC", text) != text:
        raise CanonicalStorageError("legacy JSON is not NFC")
    try:
        return json.loads(
            text,
            object_pairs_hook=_pairs_no_duplicates,
            parse_constant=lambda token: (_ for _ in ()).throw(
                CanonicalStorageError(f"legacy JSON contains {token}")
            ),
        )
    except json.JSONDecodeError as exc:
        raise CanonicalStorageError("legacy JSON is invalid") from exc


def _validate_models(data: bytes, model: type[BaseModel], *, many: bool) -> object:
    value = _legacy_json(data)
    adapter = TypeAdapter(list[model] if many else model)  # type: ignore[valid-type]
    try:
        return adapter.validate_python(value, strict=False)
    except ValidationError as exc:
        raise CanonicalStorageError("legacy JSON violates its current strict schema") from exc


def _validate_state(data: bytes) -> None:
    value = _legacy_json(data)
    if not isinstance(value, dict) or set(value) != {
        "state",
        "events",
        "deliberation_rounds",
        "tool_retries",
        "elapsed_seconds",
    }:
        raise CanonicalStorageError("legacy state has an unknown schema")
    if value["state"] not in {item.value for item in RunState}:
        raise CanonicalStorageError("legacy state has an unknown workflow state")
    if (
        not isinstance(value["events"], list)
        or not isinstance(value["deliberation_rounds"], int)
        or isinstance(value["deliberation_rounds"], bool)
        or not isinstance(value["tool_retries"], dict)
        or not isinstance(value["elapsed_seconds"], (int, float))
        or isinstance(value["elapsed_seconds"], bool)
    ):
        raise CanonicalStorageError("legacy state field types are invalid")
    for event in value["events"]:
        if (
            not isinstance(event, dict)
            or set(event) != {"source", "target", "reason"}
            or event["source"] not in {item.value for item in RunState}
            or event["target"] not in {item.value for item in RunState}
            or not isinstance(event["reason"], str)
        ):
            raise CanonicalStorageError("legacy state event is invalid")


def _validate_legacy_artifact(
    run_id: str,
    relative_path: str,
    data: bytes,
) -> FinalReport | None:
    if relative_path in _SINGLE_MODELS:
        parsed = _validate_models(data, _SINGLE_MODELS[relative_path], many=False)
        if relative_path == "final_report.json":
            report = parsed
            if not isinstance(report, FinalReport) or report.run_id != run_id:
                raise CanonicalStorageError("legacy report run identity mismatch")
            return report
        return None
    if relative_path in _LIST_MODELS:
        _validate_models(data, _LIST_MODELS[relative_path], many=True)
        return None
    if relative_path == "state.json":
        _validate_state(data)
        return None
    if relative_path == "evidence.jsonl":
        if data and (b"\r" in data or not data.endswith(b"\n")):
            raise CanonicalStorageError("legacy JSONL must be LF-terminated")
        for line in data.splitlines():
            _validate_models(line, EvidenceRecord, many=False)
        return None
    if relative_path == "final_report.md":
        try:
            text = data.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise CanonicalStorageError("legacy markdown is not UTF-8") from exc
        if "\r" in text or unicodedata.normalize("NFC", text) != text:
            raise CanonicalStorageError("legacy markdown is not canonical UTF-8 text")
        return None
    if relative_path.startswith("agent_reports/"):
        report = _validate_models(data, AgentReport, many=False)
        identifier = relative_path.removeprefix("agent_reports/").removesuffix(".json")
        if not isinstance(report, AgentReport) or report.report_id != identifier:
            raise CanonicalStorageError("legacy agent report path identity mismatch")
        return None
    if relative_path.startswith("tool_results/") and relative_path.endswith(".input.json"):
        if not isinstance(_legacy_json(data), dict):
            raise CanonicalStorageError("legacy tool input is not an object")
        return None
    if relative_path.startswith("tool_results/"):
        result = _validate_models(data, ToolResult, many=False)
        identifier = relative_path.removeprefix("tool_results/").removesuffix(".json")
        if not isinstance(result, ToolResult) or result.result_id != identifier:
            raise CanonicalStorageError("legacy tool result path identity mismatch")
        return None
    raise CanonicalStorageError("legacy artifact is not admitted")


class LegacyRunAdapter:
    """Inspect exact flat-v1 bytes without creating, repairing, or rewriting them."""

    def __init__(
        self,
        root: Path,
        *,
        max_artifact_bytes: int = 1_000_000,
        max_run_bytes: int = 10_000_000,
    ) -> None:
        self.root = root.resolve()
        self.max_artifact_bytes = max_artifact_bytes
        self.max_run_bytes = max_run_bytes

    def inspect(self, run_id: str) -> LegacyRunSnapshot:
        try:
            validate_run_id(run_id)
            verify_directory(self.root)
            run = self.root / run_id
            if run.parent != self.root:
                raise CanonicalStorageError("legacy run path escaped its root")
            verify_directory(run)
            paths = tuple(run.rglob("*"))
            if len(paths) > LEGACY_MAX_FILES + 2:
                raise CanonicalStorageError("legacy run has too many entries")
            allowed_directories = {"agent_reports", "tool_results"}
            artifacts: list[LegacySourceArtifact] = []
            final_report: FinalReport | None = None
            total = 0
            aliases: set[str] = set()
            for path in paths:
                relative = path.relative_to(run).as_posix()
                if path.is_dir():
                    verify_directory(path)
                    if relative not in allowed_directories:
                        raise CanonicalStorageError("legacy run has an unknown directory")
                    continue
                info = verify_regular_single_link(path)
                if info.st_size > self.max_artifact_bytes:
                    raise CanonicalStorageError("legacy artifact exceeds its byte limit")
                data = path.read_bytes()
                if len(data) != info.st_size:
                    raise CanonicalStorageError("legacy artifact changed during read")
                total += len(data)
                if total > self.max_run_bytes:
                    raise CanonicalStorageError("legacy run exceeds its byte limit")
                alias = relative.lower()
                if alias in aliases:
                    raise CanonicalStorageError("legacy artifact has a case alias")
                aliases.add(alias)
                if relative == LEGACY_SENTINEL:
                    if data != LEGACY_SENTINEL_BYTES:
                        raise CanonicalStorageError("legacy sentinel version is unsupported")
                    artifacts.append(
                        LegacySourceArtifact(
                            relative_path=relative,
                            media_type="application/octet-stream",
                            artifact_schema_version="poker-flat-run-sentinel-v1",
                            exact_bytes=data,
                        )
                    )
                    continue
                validate_logical_name(relative)
                if relative == "state.json":
                    media_type = "application/json"
                    schema = "poker-workflow-state-artifact-v1"
                else:
                    media_type, _serialization, schema, _origin = artifact_table_entry(relative)
                parsed_report = _validate_legacy_artifact(run_id, relative, data)
                if parsed_report is not None:
                    final_report = parsed_report
                artifacts.append(
                    LegacySourceArtifact(
                        relative_path=relative,
                        media_type=media_type,
                        artifact_schema_version=schema,
                        exact_bytes=data,
                    )
                )
            if not artifacts or artifacts[0].relative_path == "":
                raise CanonicalStorageError("legacy run has no admitted bytes")
            by_name = {item.relative_path: item for item in artifacts}
            if (
                LEGACY_SENTINEL not in by_name
                or by_name[LEGACY_SENTINEL].exact_bytes != LEGACY_SENTINEL_BYTES
            ):
                raise CanonicalStorageError("legacy run lacks its exact v1 sentinel")
            ordered = tuple(sorted(artifacts, key=lambda item: item.relative_path.encode("utf-8")))
            observations = tuple(item.inventory_observation for item in ordered)
            return LegacyRunSnapshot(
                run_id=run_id,
                source_root_identity_sha256=legacy_root_identity_sha256(self.root),
                source_run_id_sha256=run_id_sha256(run_id),
                source_inventory_sha256=legacy_source_inventory_sha256(observations),
                artifacts=ordered,
                final_report=final_report,
            )
        except ProductRunError:
            raise
        except (CanonicalStorageError, OSError, ValidationError) as exc:
            raise legacy_failure(
                run_id,
                ProductRunFailureCode.LEGACY_RUN_UNVERIFIED,
                stage="legacy_read",
            ) from exc

    def load_report_projection(self, run_id: str) -> FinalReport:
        snapshot = self.inspect(run_id)
        if snapshot.final_report is None:
            raise legacy_failure(
                run_id,
                ProductRunFailureCode.ARTIFACT_MISSING,
                stage="legacy_projection",
            )
        report = FinalReport.model_validate(snapshot.final_report.model_dump(mode="python"))
        report.run_status = "failed_with_limitations"
        limitation = "legacy_unverified_integrity_guarantees_missing"
        if limitation not in report.limitations:
            report.limitations.append(limitation)
        return report


def legacy_copy_payloads(snapshot: LegacyRunSnapshot) -> tuple[VerifiedPayloadV2, ...]:
    payloads: list[VerifiedPayloadV2] = []
    evidence = ClassificationEvidence(restricted_secret_check_completed=True)
    evidence_sha = classification_evidence_sha256(evidence)
    for artifact in snapshot.artifacts:
        if artifact.relative_path == LEGACY_SENTINEL:
            continue
        try:
            text = artifact.exact_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise legacy_failure(
                snapshot.run_id,
                ProductRunFailureCode.ARTIFACT_SCHEMA_ERROR,
                stage="legacy_copy_admission",
            ) from exc
        if redact_sensitive(text, enabled=True) != text:
            raise legacy_failure(
                snapshot.run_id,
                ProductRunFailureCode.LIFECYCLE_POLICY_FAILED,
                stage="legacy_copy_admission",
            )
        local = LocalDataBindingV1(
            logical_name=artifact.relative_path,
            classification=ContextClassification.INTERNAL,
            classification_source=ClassificationSource.DEFAULT_INTERNAL,
            classification_evidence=evidence,
            classification_evidence_sha256=evidence_sha,
        )
        entry = PayloadInventoryEntryV1(
            logical_name=artifact.relative_path,
            revision_relative_path=f"payload/{artifact.relative_path}",
            media_type=artifact.media_type,  # type: ignore[arg-type]
            artifact_schema_version=artifact.artifact_schema_version,
            serialization="opaque-bytes-v1",
            size_bytes=len(artifact.exact_bytes),
            sha256=sha256_bytes(artifact.exact_bytes),
            required=True,
            classification=ContextClassification.INTERNAL,
            classification_source=ClassificationSource.DEFAULT_INTERNAL,
            classification_evidence=evidence,
            classification_evidence_sha256=evidence_sha,
            source_sha256=upstream_source_sha256((local,)),
            provenance_bindings=(local,),
        )
        payloads.append(
            VerifiedPayloadV2(
                inventory=entry,
                exact_bytes=artifact.exact_bytes,
            )
        )
    if not payloads:
        raise legacy_failure(
            snapshot.run_id,
            ProductRunFailureCode.ARTIFACT_MISSING,
            stage="legacy_copy_admission",
        )
    return tuple(
        sorted(
            payloads,
            key=lambda item: item.inventory.revision_relative_path.encode("utf-8"),
        )
    )


def legacy_source_binding(snapshot: LegacyRunSnapshot) -> LegacySourceBindingV2:
    return LegacySourceBindingV2(
        adapter_version=LEGACY_ADAPTER_VERSION,
        source_root_identity_sha256=snapshot.source_root_identity_sha256,
        source_run_id_sha256=snapshot.source_run_id_sha256,
        source_inventory_sha256=snapshot.source_inventory_sha256,
        source_quiescence_acknowledged=True,
        missing_guarantees=LEGACY_MISSING_GUARANTEES,
    )


def same_legacy_snapshot(
    left: LegacyRunSnapshot,
    right: LegacyRunSnapshot,
) -> bool:
    return (
        left.run_id == right.run_id
        and left.source_root_identity_sha256 == right.source_root_identity_sha256
        and left.source_run_id_sha256 == right.source_run_id_sha256
        and left.source_inventory_sha256 == right.source_inventory_sha256
        and tuple(item.exact_bytes for item in left.artifacts)
        == tuple(item.exact_bytes for item in right.artifacts)
    )


__all__ = [
    "LEGACY_ADAPTER_VERSION",
    "LEGACY_MISSING_GUARANTEES",
    "LEGACY_SENTINEL",
    "LEGACY_SENTINEL_BYTES",
    "LegacyRunAdapter",
    "LegacyRunSnapshot",
    "LegacySourceArtifact",
    "legacy_copy_payloads",
    "legacy_failure",
    "legacy_source_binding",
    "same_legacy_snapshot",
]
