"""Strict contracts for the bounded P3-030D river-review workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BridgeConclusionCode,
    BridgeRole,
    BridgeTerminalStatus,
    RuntimeAuthModeV1,
)
from poker_deliberation.schemas import FinalReport

BOUNDED_RIVER_REVIEW_WORKFLOW_SCHEMA_VERSION = "1.0.0"
BOUNDED_RIVER_REVIEW_WORKFLOW_MAX_ARTIFACT_BYTES = 1_000_000

_PORTABLE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_SOURCE_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"


class _WorkflowModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class BoundedRiverReviewWorkflowPlanV1(_WorkflowModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    contract_id: Literal["poker-bounded-river-review-workflow"] = (
        "poker-bounded-river-review-workflow"
    )
    contract_version: Literal["1.0.0"] = "1.0.0"
    workflow_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    intake_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    bridge_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    auth_mode: RuntimeAuthModeV1 = RuntimeAuthModeV1.LOCAL_ONLY
    api_max_cost_micro_usd: int | None = Field(default=None, gt=0)
    repository_commit_id: str = Field(pattern=_SOURCE_ID_PATTERN)
    repository_tree_id: str = Field(pattern=_SOURCE_ID_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    preparation_sha256: str = Field(pattern=_SHA256_PATTERN)
    created_at: datetime
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("created_at")
    @classmethod
    def utc_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("BRW_E_SCHEMA")
        return value

    @model_validator(mode="after")
    def api_budget_matches_mode(self) -> BoundedRiverReviewWorkflowPlanV1:
        if (self.auth_mode is RuntimeAuthModeV1.OPENAI_API) != (
            self.api_max_cost_micro_usd is not None
        ):
            raise ValueError("BRW_E_AUTH_MODE")
        return self


class BoundedRiverReviewWorkflowLinkageV1(_WorkflowModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    contract_id: Literal["poker-bounded-river-review-workflow-linkage"] = (
        "poker-bounded-river-review-workflow-linkage"
    )
    workflow_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_terminal_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_terminal_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    auth_mode: RuntimeAuthModeV1
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    linked_at: datetime
    linkage_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("linked_at")
    @classmethod
    def utc_required(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("BRW_E_SCHEMA")
        return value


WorkflowState = Literal[
    "awaiting_confirmation",
    "ready_to_run",
    "ready_to_resume",
    "completed_local_only",
    "awaiting_role_review",
    "role_review_in_progress",
    "completed",
    "failed",
]
WorkflowNextAction = Literal[
    "confirm",
    "run",
    "resume",
    "use_existing_bridge_commands",
    "show_role_request",
    "execute_role",
    "none",
]
WorkflowRoleState = Literal[
    "awaiting_confirmation",
    "executable",
    "expired",
    "in_progress",
    "terminal",
]


class BoundedRiverReviewWorkflowStatusV1(_WorkflowModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    contract_id: Literal["poker-bounded-river-review-workflow-status"] = (
        "poker-bounded-river-review-workflow-status"
    )
    workflow_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    state: WorkflowState
    auth_mode: RuntimeAuthModeV1
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_terminal_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    bridge_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    bridge_manifest_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    bridge_status: str | None = None
    completed_roles: tuple[BridgeRole, ...] = ()
    pending_roles: tuple[BridgeRole, ...] = ()
    next_role: BridgeRole | None = None
    role_state: WorkflowRoleState | None = None
    role_request_expires_at: datetime | None = None
    role_confirmation_expires_at: datetime | None = None
    reconciliation_required: bool = False
    next_action: WorkflowNextAction

    @field_validator("role_request_expires_at", "role_confirmation_expires_at")
    @classmethod
    def role_expiry_utc_required(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("BRW_E_SCHEMA")
        return value


class BoundedRiverReviewRoleConfirmationBindingV1(_WorkflowModel):
    """Workflow-owned proof that one exact P2 role confirmation was authorized here."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    contract_id: Literal["poker-bounded-river-review-role-confirmation-binding"] = (
        "poker-bounded-river-review-role-confirmation-binding"
    )
    workflow_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    workflow_confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    linkage_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    auth_mode: RuntimeAuthModeV1
    role: BridgeRole
    role_ordinal: int = Field(ge=0, le=4)
    request_sha256: str = Field(pattern=_SHA256_PATTERN)
    request_bytes_sha256: str = Field(pattern=_SHA256_PATTERN)
    envelope_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_policy_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_identity: str = Field(min_length=1, max_length=128)
    model_provider: str = Field(min_length=1, max_length=128)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    credential_reference: str = Field(min_length=1, max_length=128)
    remote_retention_policy: str = Field(min_length=1, max_length=128)
    authority_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    confirmation_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    idempotency_key: str = Field(pattern=_PORTABLE_ID_PATTERN)
    bridge_confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_confirmation_confirmed_at: datetime
    bridge_confirmation_expires_at: datetime
    preview_bridge_revision: int = Field(ge=1)
    preview_bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    preview_bridge_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    preview_bridge_pointer_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmed_bridge_revision: int = Field(ge=1)
    confirmed_bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmed_bridge_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmed_bridge_pointer_sha256: str = Field(pattern=_SHA256_PATTERN)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator(
        "bridge_confirmation_confirmed_at",
        "bridge_confirmation_expires_at",
    )
    @classmethod
    def bridge_confirmation_times_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("BRW_E_SCHEMA")
        return value

    @model_validator(mode="after")
    def role_and_lineage_are_ordered(
        self,
    ) -> BoundedRiverReviewRoleConfirmationBindingV1:
        if self.role is not BRIDGE_ROLE_ORDER[self.role_ordinal]:
            raise ValueError("BRW_E_ROLE_BINDING")
        if self.bridge_confirmation_confirmed_at >= self.bridge_confirmation_expires_at:
            raise ValueError("BRW_E_ROLE_BINDING")
        if self.confirmed_bridge_revision not in {
            self.preview_bridge_revision,
            self.preview_bridge_revision + 1,
        }:
            raise ValueError("BRW_E_ROLE_BINDING")
        return self


class BoundedRiverReviewReportWriterEvidenceV1(_WorkflowModel):
    """One validated report-writer conclusion/evidence-hash pair."""

    conclusion_code: BridgeConclusionCode
    referenced_evidence_sha256: str = Field(pattern=_SHA256_PATTERN)


class BoundedRiverReviewReportViewV1(_WorkflowModel):
    """Read-only projection of one fully linked bounded river review."""

    schema_version: Literal["1.0.0"] = "1.0.0"
    contract_id: Literal["poker-bounded-river-review-report-view"] = (
        "poker-bounded-river-review-report-view"
    )
    workflow_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    state: WorkflowState
    bridge_mode: RuntimeAuthModeV1
    bridge_status: BridgeTerminalStatus
    completed_roles: tuple[BridgeRole, ...] = ()
    source_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    bridge_run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    linkage_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_terminal_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_terminal_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    final_report_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    report_writer_additive_evidence: tuple[BoundedRiverReviewReportWriterEvidenceV1, ...] = ()
    final_report: FinalReport

    @model_validator(mode="after")
    def exact_report_and_optional_writer_evidence(
        self,
    ) -> BoundedRiverReviewReportViewV1:
        if self.final_report.run_id != self.source_run_id:
            raise ValueError("BRW_E_REPORT_BINDING")
        writer_completed = BridgeRole.REPORT_WRITER in self.completed_roles
        if writer_completed != bool(self.report_writer_additive_evidence):
            raise ValueError("BRW_E_REPORT_WRITER")
        return self


__all__ = [
    "BOUNDED_RIVER_REVIEW_WORKFLOW_MAX_ARTIFACT_BYTES",
    "BOUNDED_RIVER_REVIEW_WORKFLOW_SCHEMA_VERSION",
    "BoundedRiverReviewReportViewV1",
    "BoundedRiverReviewReportWriterEvidenceV1",
    "BoundedRiverReviewRoleConfirmationBindingV1",
    "BoundedRiverReviewWorkflowLinkageV1",
    "BoundedRiverReviewWorkflowPlanV1",
    "BoundedRiverReviewWorkflowStatusV1",
    "WorkflowNextAction",
    "WorkflowRoleState",
    "WorkflowState",
]
