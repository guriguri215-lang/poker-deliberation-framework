"""Strict contracts for the bounded P3-030D river-review workflow."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.codex_bridge.models import (
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
    "none",
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
    reconciliation_required: bool = False
    next_action: WorkflowNextAction


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
    "BoundedRiverReviewWorkflowLinkageV1",
    "BoundedRiverReviewWorkflowPlanV1",
    "BoundedRiverReviewWorkflowStatusV1",
    "WorkflowNextAction",
    "WorkflowState",
]
