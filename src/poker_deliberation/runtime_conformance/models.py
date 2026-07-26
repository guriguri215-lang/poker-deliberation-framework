"""Strict versioned values for cross-runtime semantic conformance."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from poker_deliberation.schemas import EpistemicLabel

CONFORMANCE_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
CONFORMANCE_CANONICALIZATION: Final[Literal["poker-runtime-conformance-json-v1"]] = (
    "poker-runtime-conformance-json-v1"
)
CONFORMANCE_HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
_SECRET_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|"
    r"(?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*[^\s,;]+)",
    re.IGNORECASE,
)


def _safe_text(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("conformance text must be NFC")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("conformance text cannot contain control characters")
    if _SECRET_VALUE.search(value):
        raise ValueError("conformance text must not contain a secret shape")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


PortableId = Annotated[
    str,
    Field(pattern=_PORTABLE_ID.pattern),
    AfterValidator(_safe_text),
]
Version = Annotated[
    str,
    Field(pattern=_VERSION.pattern),
    AfterValidator(_safe_text),
]
Sha256 = Annotated[str, Field(pattern=_SHA256.pattern)]
BoundedText = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_safe_text),
]
SourcePath = Annotated[
    str,
    Field(pattern=_SOURCE_PATH.pattern),
    AfterValidator(_safe_text),
]


class RuntimeId(StrEnum):
    CODEX_NATIVE = "codex-native"
    PYTHON_ORCHESTRATOR = "python-orchestrator"


class SemanticRole(StrEnum):
    INTAKE = "intake"
    STRATEGY_ANALYSIS = "strategy-analysis"
    MATH_AUDIT = "math-audit"
    EVIDENCE_RESEARCH = "evidence-research"
    SKEPTICISM = "skepticism"
    ADJUDICATION = "adjudication"
    REPORT_WRITING = "report-writing"
    ORCHESTRATION = "orchestration"
    CALCULATOR_DEVELOPMENT = "calculator-development"


class RoleKind(StrEnum):
    ANALYSIS = "analysis"
    ORCHESTRATOR = "orchestrator"
    DEVELOPMENT = "development"


class RoleRelationship(StrEnum):
    SEMANTIC_PEER = "semantic-peer"
    RUNTIME_SPECIFIC = "runtime-specific"
    INTENTIONALLY_UNMAPPED = "intentionally-unmapped"


class ResultStatus(StrEnum):
    SUCCEEDED = "succeeded"
    LIMITED = "limited"
    FAILED = "failed"
    REFUSED = "refused"
    APPROVAL_REQUIRED = "approval-required"
    TIMED_OUT = "timed-out"
    CANCELLED = "cancelled"


class ExecutionState(StrEnum):
    NOT_EXECUTED = "not-executed"
    EXECUTED = "executed"


class _ConformanceModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


def _utf8_sorted_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must be unique")
    if values != tuple(sorted(values, key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{field_name} must be UTF-8 sorted")
    return values


class RoleInventoryEntryV1(_ConformanceModel):
    runtime: RuntimeId
    runtime_role_id: PortableId
    semantic_role: SemanticRole
    role_kind: RoleKind
    purpose: BoundedText
    read_only: bool
    source_path: SourcePath
    source_definition_sha256: Sha256
    catalog_member: bool
    sandbox_mode: Literal["read-only", "workspace-write"] | None = None
    declared_tools: tuple[PortableId, ...] | None = None
    tool_policy_source: Literal[
        "ambient-runtime-undeclared",
        "assignment-and-registry",
        "development-role-definition",
    ]
    approval_policy_source: Literal[
        "codex-runtime-policy",
        "python-approval-contract",
        "development-approval-contract",
    ]
    expected_result_kind: PortableId
    execution_audit_requirement: Literal[
        "runtime-native-audit",
        "python-agent-execution-record",
        "python-product-run-audit",
    ]

    @field_validator("declared_tools")
    @classmethod
    def canonical_tools(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        return _utf8_sorted_unique(value, "declared_tools")

    @model_validator(mode="after")
    def undeclared_tools_are_not_invented(self) -> RoleInventoryEntryV1:
        if (
            self.tool_policy_source == "ambient-runtime-undeclared"
            and self.declared_tools is not None
        ):
            raise ValueError("ambient runtime tools must remain undeclared")
        return self


class RoleMappingV1(_ConformanceModel):
    semantic_role: SemanticRole
    relationship: RoleRelationship
    codex_role_id: PortableId | None
    python_role_id: PortableId | None
    rationale: BoundedText

    @model_validator(mode="after")
    def relationship_has_valid_endpoints(self) -> RoleMappingV1:
        if self.codex_role_id is None and self.python_role_id is None:
            raise ValueError("role mapping requires at least one runtime role")
        if self.relationship is RoleRelationship.SEMANTIC_PEER and (
            self.codex_role_id is None or self.python_role_id is None
        ):
            raise ValueError("semantic peer mapping requires both runtime roles")
        if self.relationship is RoleRelationship.INTENTIONALLY_UNMAPPED and (
            (self.codex_role_id is None) == (self.python_role_id is None)
        ):
            raise ValueError("intentionally unmapped role requires exactly one endpoint")
        return self


class RuntimeInventoryV1(_ConformanceModel):
    schema_version: Literal["1.0.0"] = CONFORMANCE_SCHEMA_VERSION
    canonicalization: Literal["poker-runtime-conformance-json-v1"] = CONFORMANCE_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = CONFORMANCE_HASH_ALGORITHM
    runtime: RuntimeId
    roles: tuple[RoleInventoryEntryV1, ...]
    role_mappings: tuple[RoleMappingV1, ...]
    tool_catalog: tuple[PortableId, ...] | None
    capability_catalog: tuple[PortableId, ...]
    source_revision: Sha256

    @field_validator("roles")
    @classmethod
    def canonical_roles(
        cls, value: tuple[RoleInventoryEntryV1, ...]
    ) -> tuple[RoleInventoryEntryV1, ...]:
        ids = tuple(item.runtime_role_id for item in value)
        _utf8_sorted_unique(ids, "runtime role IDs")
        if value and any(item.runtime is not value[0].runtime for item in value[1:]):
            raise ValueError("runtime inventory cannot mix runtime identities")
        return value

    @field_validator("role_mappings")
    @classmethod
    def canonical_mappings(cls, value: tuple[RoleMappingV1, ...]) -> tuple[RoleMappingV1, ...]:
        keys = tuple(
            (
                item.semantic_role.value,
                item.codex_role_id or "",
                item.python_role_id or "",
            )
            for item in value
        )
        if len(keys) != len(set(keys)):
            raise ValueError("role mappings must be unique")
        if keys != tuple(
            sorted(
                keys,
                key=lambda item: tuple(part.encode("utf-8") for part in item),
            )
        ):
            raise ValueError("role mappings must be canonically ordered")
        return value

    @field_validator("tool_catalog", "capability_catalog")
    @classmethod
    def canonical_catalog(
        cls, value: tuple[str, ...] | None, info: object
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "catalog")
        return _utf8_sorted_unique(value, field_name)

    @model_validator(mode="after")
    def roles_match_runtime(self) -> RuntimeInventoryV1:
        if any(role.runtime is not self.runtime for role in self.roles):
            raise ValueError("inventory role runtime mismatch")
        role_ids = {role.runtime_role_id for role in self.roles}
        for mapping in self.role_mappings:
            endpoint = (
                mapping.codex_role_id
                if self.runtime is RuntimeId.CODEX_NATIVE
                else mapping.python_role_id
            )
            if endpoint is not None and endpoint not in role_ids:
                raise ValueError("role mapping endpoint is absent from the runtime inventory")
        return self


class BudgetReferenceV1(_ConformanceModel):
    policy_schema_version: Version
    policy_sha256: Sha256
    maximum_runtime_ms: int | None = Field(default=None, ge=1, le=31_536_000_000)
    maximum_output_bytes: int | None = Field(default=None, ge=1, le=2**63 - 1)
    reference_kind: Literal["exact-policy", "verified-policy-hash"]

    @model_validator(mode="after")
    def exact_policy_has_limits(self) -> BudgetReferenceV1:
        limits = (self.maximum_runtime_ms, self.maximum_output_bytes)
        if self.reference_kind == "exact-policy" and any(value is None for value in limits):
            raise ValueError("exact budget policy requires runtime and output limits")
        if self.reference_kind == "verified-policy-hash" and any(
            value is not None for value in limits
        ):
            raise ValueError("hash-only budget reference cannot invent policy limits")
        return self


class ContextProvenanceV1(_ConformanceModel):
    source_kind: Literal[
        "context-envelope",
        "verified-product-input",
        "fixture",
    ]
    source_sha256: Sha256
    producer_runtime: RuntimeId
    consumer_runtime: RuntimeId
    parent_context_id: PortableId | None = None


class ContextReferenceV1(_ConformanceModel):
    reference_kind: Literal["context-envelope", "verified-product-input", "fixture"]
    context_id: PortableId
    context_schema_version: Version
    classification: Literal["public", "internal", "sensitive", "restricted"]
    created_at: datetime
    expires_at: datetime | None
    payload_sha256: Sha256
    policy_sha256: Sha256
    envelope_sha256: Sha256 | None
    provenance: ContextProvenanceV1
    budget: BudgetReferenceV1

    _created_utc = field_validator("created_at")(lambda value: _utc(value, "created_at"))

    @field_validator("expires_at")
    @classmethod
    def optional_expiry_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value, "expires_at")

    @model_validator(mode="after")
    def reference_shape_matches_kind(self) -> ContextReferenceV1:
        if self.reference_kind != self.provenance.source_kind:
            raise ValueError("context reference/provenance kind mismatch")
        if self.reference_kind == "context-envelope":
            if self.envelope_sha256 is None or self.expires_at is None:
                raise ValueError("context envelope reference requires envelope hash and expiry")
            if self.expires_at <= self.created_at:
                raise ValueError("context expiry must follow creation")
        elif self.envelope_sha256 is not None or self.expires_at is not None:
            raise ValueError("non-envelope context reference cannot invent envelope expiry")
        return self


class ToolCapabilityAllowlistV1(_ConformanceModel):
    policy_version: Version
    allowed_tools: tuple[PortableId, ...]
    allowed_capabilities: tuple[PortableId, ...]
    catalog_status: Literal["declared", "undeclared"]
    policy_source: Literal[
        "assignment",
        "verified-product-result-bindings",
        "fixture",
        "ambient-runtime",
    ]
    interpretation: Literal["exact"] = "exact"

    @field_validator("allowed_tools", "allowed_capabilities")
    @classmethod
    def canonical_allowlist(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        return _utf8_sorted_unique(
            value,
            getattr(info, "field_name", "allowlist"),
        )

    @model_validator(mode="after")
    def undeclared_catalog_has_no_claimed_permissions(self) -> ToolCapabilityAllowlistV1:
        if self.catalog_status == "undeclared" and (
            self.allowed_tools or self.allowed_capabilities
        ):
            raise ValueError("undeclared catalog cannot claim permissions")
        return self


ApprovalDecision: TypeAlias = Literal[
    "not-applicable",
    "pending",
    "approved",
    "rejected",
    "expired",
]


class ApprovalBindingV1(_ConformanceModel):
    requirement: Literal["not-required", "required"]
    request_id: PortableId | None = None
    action_digest_sha256: Sha256 | None = None
    decision: ApprovalDecision
    decision_at: datetime | None = None
    expires_at: datetime | None = None
    authority_snapshot_sha256: Sha256 | None = None

    @field_validator("decision_at", "expires_at")
    @classmethod
    def optional_approval_utc(cls, value: datetime | None, info: object) -> datetime | None:
        if value is None:
            return None
        return _utc(value, getattr(info, "field_name", "approval time"))

    @model_validator(mode="after")
    def closed_approval_matrix(self) -> ApprovalBindingV1:
        if self.requirement == "not-required":
            if self.decision != "not-applicable" or any(
                value is not None
                for value in (
                    self.request_id,
                    self.action_digest_sha256,
                    self.decision_at,
                    self.expires_at,
                    self.authority_snapshot_sha256,
                )
            ):
                raise ValueError("non-required approval must not carry approval authority")
            return self
        if (
            self.request_id is None
            or self.action_digest_sha256 is None
            or self.expires_at is None
            or self.decision == "not-applicable"
        ):
            raise ValueError("required approval lacks request binding")
        if self.decision in {"approved", "rejected"} and (
            self.decision_at is None or self.authority_snapshot_sha256 is None
        ):
            raise ValueError("approval decision requires time and authority snapshot")
        if self.decision in {"pending", "expired"} and (
            self.decision_at is not None or self.authority_snapshot_sha256 is not None
        ):
            raise ValueError("pending/expired approval cannot carry a decision authority")
        if (
            self.decision in {"approved", "rejected"}
            and self.decision_at is not None
            and self.decision_at >= self.expires_at
        ):
            raise ValueError("approval decision must precede expiry")
        return self


class AssignmentV1(_ConformanceModel):
    assignment_id: PortableId
    producer_runtime: RuntimeId
    runtime_role_id: PortableId
    semantic_role: SemanticRole
    objective: BoundedText
    objective_sha256: Sha256
    parent_assignment_id: PortableId | None
    context: ContextReferenceV1
    allowlist: ToolCapabilityAllowlistV1
    approval: ApprovalBindingV1
    role_inventory_sha256: Sha256

    @model_validator(mode="after")
    def objective_hash_matches(self) -> AssignmentV1:
        from poker_deliberation.runtime_conformance.canonical import domain_sha256

        expected = domain_sha256(
            "poker-runtime-conformance-objective-v1",
            self.objective.encode("utf-8"),
        )
        if self.objective_sha256 != expected:
            raise ValueError("assignment objective hash mismatch")
        return self


class ToolResultReferenceV1(_ConformanceModel):
    result_id: PortableId
    tool_name: PortableId
    contract_version: Version
    status: Literal["success", "failed", "unavailable"]
    exactness: Literal[
        "exact",
        "exact-under-model",
        "floating-verified",
        "approximate",
        "unavailable",
    ]
    result_sha256: Sha256


class EvidenceReferenceV1(_ConformanceModel):
    evidence_id: PortableId
    evidence_sha256: Sha256
    verification: Literal["declared", "verified"]


class ProviderConclusionReferenceV1(_ConformanceModel):
    execution_id: PortableId
    provider_id: PortableId
    verification: Literal["unverified"] = "unverified"


class SolverEvidenceV1(_ConformanceModel):
    solver_id: PortableId
    solver_version: Version
    game_identity_sha256: Sha256
    input_sha256: Sha256
    output_sha256: Sha256
    convergence_definition: BoundedText
    convergence_trajectory_sha256: Sha256
    exploitability_evidence_sha256: Sha256
    qualification_status: Literal["qualified"] = "qualified"


StrategyClaim: TypeAlias = Literal[
    "none",
    "general-strategy",
    "gto",
    "equilibrium",
    "exact-range",
]


class ResultV1(_ConformanceModel):
    result_id: PortableId
    status: ResultStatus
    summary: BoundedText
    epistemic_label: EpistemicLabel
    strategy_claim: StrategyClaim = "none"
    tool_results: tuple[ToolResultReferenceV1, ...] = ()
    evidence: tuple[EvidenceReferenceV1, ...] = ()
    provider_conclusions: tuple[ProviderConclusionReferenceV1, ...] = ()
    solver_evidence: SolverEvidenceV1 | None = None
    limitations: tuple[BoundedText, ...] = ()

    @field_validator("tool_results")
    @classmethod
    def canonical_tool_results(
        cls, value: tuple[ToolResultReferenceV1, ...]
    ) -> tuple[ToolResultReferenceV1, ...]:
        ids = tuple(item.result_id for item in value)
        _utf8_sorted_unique(ids, "tool result IDs")
        return value

    @field_validator("evidence")
    @classmethod
    def canonical_evidence(
        cls, value: tuple[EvidenceReferenceV1, ...]
    ) -> tuple[EvidenceReferenceV1, ...]:
        ids = tuple(item.evidence_id for item in value)
        _utf8_sorted_unique(ids, "evidence IDs")
        return value

    @field_validator("provider_conclusions")
    @classmethod
    def canonical_provider_references(
        cls, value: tuple[ProviderConclusionReferenceV1, ...]
    ) -> tuple[ProviderConclusionReferenceV1, ...]:
        ids = tuple(item.execution_id for item in value)
        _utf8_sorted_unique(ids, "provider execution IDs")
        return value

    @field_validator("limitations")
    @classmethod
    def canonical_limitations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _utf8_sorted_unique(value, "limitations")

    @model_validator(mode="after")
    def epistemic_and_strategy_claims_are_supported(self) -> ResultV1:
        successful_tools = [item for item in self.tool_results if item.status == "success"]
        verified_evidence = [item for item in self.evidence if item.verification == "verified"]
        if self.epistemic_label is EpistemicLabel.CALCULATED and not successful_tools:
            raise ValueError("CALCULATED result requires a successful tool reference")
        if self.epistemic_label is EpistemicLabel.FACT and not verified_evidence:
            raise ValueError("FACT result requires verified evidence")
        if (
            self.provider_conclusions
            and self.epistemic_label
            in {
                EpistemicLabel.FACT,
                EpistemicLabel.CALCULATED,
            }
            and not (successful_tools or verified_evidence)
        ):
            raise ValueError("unverified provider prose cannot become a verified conclusion")
        solver_claims = {"gto", "equilibrium", "exact-range"}
        if self.strategy_claim in solver_claims and self.solver_evidence is None:
            raise ValueError("solver-backed strategy claim requires qualified solver evidence")
        if self.strategy_claim not in solver_claims and self.solver_evidence is not None:
            raise ValueError("solver evidence must bind an explicit solver-backed claim")
        return self


class StructuredErrorV1(_ConformanceModel):
    code: PortableId
    category: Literal[
        "validation",
        "unsupported",
        "provider",
        "tool",
        "timeout",
        "cancellation",
        "approval",
        "audit",
    ]
    retryable: bool
    message: BoundedText


class ReproductionMetadataV1(_ConformanceModel):
    framework_version: Version
    source_commit_id: Sha256
    source_commit_status: Literal["known", "unknown"]
    tool_contract_versions: tuple[tuple[PortableId, Version], ...]

    @field_validator("tool_contract_versions")
    @classmethod
    def canonical_tool_versions(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        names = tuple(item[0] for item in value)
        _utf8_sorted_unique(names, "reproduction tool names")
        return value

    @model_validator(mode="after")
    def source_commit_status_matches(self) -> ReproductionMetadataV1:
        is_unknown = self.source_commit_id == "0" * 64
        if is_unknown != (self.source_commit_status == "unknown"):
            raise ValueError("source commit status/hash mismatch")
        return self


class ExecutionAuditV1(_ConformanceModel):
    execution_id: PortableId
    producer_runtime: RuntimeId
    execution_kind: Literal[
        "runtime-assignment",
        "python-product-run",
        "fixture",
    ]
    terminal_status: Literal[
        "succeeded",
        "failed",
        "cancelled",
        "cancel-unconfirmed",
        "refused",
    ]
    external_effect: bool
    started_at: datetime | None
    completed_at: datetime
    timing_evidence: Literal["complete", "completion-only"]
    context_sha256: Sha256
    allowlist_sha256: Sha256
    approval_binding_sha256: Sha256
    current_pointer_sha256: Sha256 | None = None
    manifest_sha256: Sha256 | None = None
    inventory_sha256: Sha256 | None = None
    agent_execution_ids: tuple[PortableId, ...] = ()
    tool_result_ids: tuple[PortableId, ...] = ()
    reproduction: ReproductionMetadataV1

    _completed_utc = field_validator("completed_at")(lambda value: _utc(value, "completed_at"))

    @field_validator("started_at")
    @classmethod
    def optional_started_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value, "started_at")

    @field_validator("agent_execution_ids", "tool_result_ids")
    @classmethod
    def canonical_execution_ids(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        return _utf8_sorted_unique(
            value,
            getattr(info, "field_name", "execution IDs"),
        )

    @model_validator(mode="after")
    def timing_and_product_bindings_match(self) -> ExecutionAuditV1:
        if self.timing_evidence == "complete":
            if self.started_at is None or self.started_at > self.completed_at:
                raise ValueError("complete timing requires an ordered start time")
        elif self.started_at is not None:
            raise ValueError("completion-only timing cannot invent a start time")
        product_hashes = (
            self.current_pointer_sha256,
            self.manifest_sha256,
            self.inventory_sha256,
        )
        if self.execution_kind == "python-product-run" and any(
            value is None for value in product_hashes
        ):
            raise ValueError("product-run audit requires verified product hashes")
        if self.execution_kind != "python-product-run" and any(
            value is not None for value in product_hashes
        ):
            raise ValueError("non-product audit cannot carry product storage hashes")
        return self


class ConformanceRecordV1(_ConformanceModel):
    schema_version: Literal["1.0.0"] = CONFORMANCE_SCHEMA_VERSION
    canonicalization: Literal["poker-runtime-conformance-json-v1"] = CONFORMANCE_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = CONFORMANCE_HASH_ALGORITHM
    producer_runtime: RuntimeId
    assignment: AssignmentV1
    result: ResultV1
    error: StructuredErrorV1 | None
    execution_state: ExecutionState
    execution_audit: ExecutionAuditV1 | None
    runtime_bridge_used: Literal[False] = False

    @model_validator(mode="after")
    def record_has_honest_terminal_and_execution_state(self) -> ConformanceRecordV1:
        if self.assignment.producer_runtime is not self.producer_runtime:
            raise ValueError("record and assignment runtime mismatch")
        executed = self.execution_state is ExecutionState.EXECUTED
        if executed != (self.execution_audit is not None):
            raise ValueError("executed record requires exactly one execution audit")
        if self.execution_audit is not None:
            if self.execution_audit.producer_runtime is not self.producer_runtime:
                raise ValueError("execution audit runtime mismatch")
            if self.execution_audit.external_effect and (
                self.assignment.approval.requirement != "required"
                or self.assignment.approval.decision != "approved"
            ):
                raise ValueError("external effect requires an approved binding")
        error_statuses = {
            ResultStatus.FAILED,
            ResultStatus.TIMED_OUT,
            ResultStatus.CANCELLED,
        }
        if (self.result.status in error_statuses) != (self.error is not None):
            raise ValueError("terminal error/status mismatch")
        if self.result.status is ResultStatus.TIMED_OUT and (
            self.error is None or self.error.category != "timeout"
        ):
            raise ValueError("timed-out result requires a timeout error")
        if self.result.status is ResultStatus.CANCELLED and (
            self.error is None or self.error.category != "cancellation"
        ):
            raise ValueError("cancelled result requires a cancellation error")
        if self.result.status is ResultStatus.APPROVAL_REQUIRED and not (
            self.assignment.approval.requirement == "required"
            and self.assignment.approval.decision == "pending"
            and self.execution_state is ExecutionState.NOT_EXECUTED
        ):
            raise ValueError("approval-required result requires a pending no-execution binding")
        return self


class ConformanceViolationV1(_ConformanceModel):
    code: PortableId
    path: BoundedText
    summary: BoundedText


class ConformanceCheckV1(_ConformanceModel):
    schema_version: Literal["1.0.0"] = CONFORMANCE_SCHEMA_VERSION
    status: Literal["conformant", "nonconformant"]
    violations: tuple[ConformanceViolationV1, ...]

    @model_validator(mode="after")
    def status_matches_violations(self) -> ConformanceCheckV1:
        if (self.status == "conformant") != (not self.violations):
            raise ValueError("conformance status/violation mismatch")
        return self


__all__ = [
    "CONFORMANCE_CANONICALIZATION",
    "CONFORMANCE_HASH_ALGORITHM",
    "CONFORMANCE_SCHEMA_VERSION",
    "ApprovalBindingV1",
    "AssignmentV1",
    "BoundedText",
    "BudgetReferenceV1",
    "ConformanceCheckV1",
    "ConformanceRecordV1",
    "ConformanceViolationV1",
    "ContextProvenanceV1",
    "ContextReferenceV1",
    "EvidenceReferenceV1",
    "ExecutionAuditV1",
    "ExecutionState",
    "ProviderConclusionReferenceV1",
    "ReproductionMetadataV1",
    "ResultStatus",
    "ResultV1",
    "RoleInventoryEntryV1",
    "RoleKind",
    "RoleMappingV1",
    "RoleRelationship",
    "RuntimeId",
    "RuntimeInventoryV1",
    "SemanticRole",
    "SolverEvidenceV1",
    "StructuredErrorV1",
    "ToolCapabilityAllowlistV1",
    "ToolResultReferenceV1",
]
