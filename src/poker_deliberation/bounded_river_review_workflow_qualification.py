"""Canonical sanitized qualification evidence for one completed P3-030F workflow.

This module is deliberately a projector, not an execution harness.  It reads an
already completed workflow through the production verification path and publishes
only hashes, fixed status codes, counts, and runtime-source inventory.  In
particular it never serializes source, prompt, outbound, credential, narrative, or
model-trace values.
"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation import bounded_river_review_workflow as workflow
from poker_deliberation.bounded_river_call_ev_models import (
    RANGE_DEFINITION_HASH_DOMAIN,
    SOURCE_HASH_DOMAIN,
    BoundedRiverCallEvConfirmationV1,
)
from poker_deliberation.bounded_river_review_workflow_models import (
    BoundedRiverReviewReportViewV1,
    BoundedRiverReviewRoleConfirmationBindingV1,
    BoundedRiverReviewWorkflowLinkageV1,
    BoundedRiverReviewWorkflowPlanV1,
    BoundedRiverReviewWorkflowStatusV1,
)
from poker_deliberation.codex_bridge.canonical import (
    canonical_json_bytes,
    domain_sha256,
    parse_canonical_model,
    sha256_bytes,
    without_field,
)
from poker_deliberation.codex_bridge.controller import role_artifact_name
from poker_deliberation.codex_bridge.identity import (
    BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
    BridgeRuntimeSourceFile,
    bridge_runtime_source_inventory,
    bridge_runtime_source_inventory_sha256,
    verify_bridge_checkout,
    verify_bridge_module_origins,
)
from poker_deliberation.codex_bridge.models import (
    BRIDGE_ROLE_ORDER,
    BoundedCodexBridgeRequestV1,
    BridgeEffectState,
    BridgeExecutionAuditV1,
    BridgePreExecutionAdmissionV1,
    BridgeRole,
    BridgeRoleConfirmationV1,
    BridgeRoleResultV1,
    BridgeRunPlanV1,
    BridgeSourceContextV1,
    GitObjectId,
    PortableId,
    RuntimeAuthModeV1,
    Sha256,
)
from poker_deliberation.codex_bridge.qualification import SanitizedRuntimeSourceFileV1
from poker_deliberation.codex_bridge.replay import BridgeReplayResult
from poker_deliberation.codex_bridge.storage import VerifiedBridgeRead
from poker_deliberation.config import AppConfig
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes as terminal_canonical_json_bytes,
)
from poker_deliberation.storage.terminal_models import VerifiedRunReadV2

if TYPE_CHECKING:
    from poker_deliberation.bounded_river_review_workflow_evaluation import (
        BoundedRiverReviewWorkflowEvaluationResultV2,
    )


QUALIFICATION_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
QUALIFICATION_MANIFEST_HASH_DOMAIN: Final = (
    "poker-bounded-river-review-workflow-qualification-manifest-v1"
)
QUALIFICATION_BRIDGE_REPLAY_HASH_DOMAIN: Final = (
    "poker-bounded-river-review-workflow-qualified-bridge-replay-v1"
)
QUALIFICATION_CONFIRMATION_FIELDS_HASH_DOMAIN: Final = (
    "poker-bounded-river-review-workflow-confirmation-fields-v1"
)
_EVALUATION_CONFIRMATION_HASH_DOMAIN: Final = (
    "poker-bounded-river-review-workflow-evaluation-confirmation-v2"
)
_EVALUATION_RECEIPTS_HASH_DOMAIN: Final = (
    "poker-bounded-river-review-workflow-evaluation-receipts-v2"
)
_EVALUATION_P2_LINEAGE_HASH_DOMAIN: Final = (
    "poker-bounded-river-review-workflow-evaluation-p2-lineage-v2"
)
_EVALUATION_TERMINAL_HASH_DOMAIN: Final = (
    "poker-bounded-river-review-workflow-evaluation-terminal-v2"
)
_EVALUATION_CASE_IDS: Final = (
    "source-workflow-identity",
    "workflow-confirmation-binding",
    "five-role-supervision",
    "p2-artifact-lineage",
    "terminal-replay-report",
    "repository-runtime-identity",
)
_EVALUATION_METRIC_IDS: Final = (
    "source_workflow_identity",
    "workflow_confirmation_binding",
    "five_role_supervision",
    "p2_artifact_lineage",
    "terminal_replay_report",
    "repository_runtime_identity",
)
_EVALUATION_MUTATION_CLAIM_SHA256: Final = domain_sha256(
    "poker-bounded-river-review-workflow-evaluation-claim-token-v1",
    "exact-seventeen-field-contract-and-production-mismatch-refused",
)
_EVALUATION_FIXTURE_PATH: Final = "tests/fixtures/bounded_river_review_workflow/v2/scenarios.json"
_EVALUATION_SOURCE_PATH: Final = "tests/fixtures/bounded_river_review_workflow/v1/source-ja.txt"
_EVALUATION_RANGE_PATH: Final = "tests/fixtures/bounded_river_review_workflow/v1/range.json"
QUALIFICATION_LIMITATIONS: Final = (
    "actual_backend_model_input_UNKNOWN",
    "current_live_model_provider_qualification_UNKNOWN",
    "human_usefulness_UNKNOWN",
    "strategy_quality_UNKNOWN",
    "solver_not_executed",
)
_MAX_MANIFEST_BYTES = 2_000_000


class BoundedRiverReviewWorkflowQualificationError(ValueError):
    """Stable fail-closed error that never embeds excluded evidence values."""


def _fail(code: str) -> NoReturn:
    raise BoundedRiverReviewWorkflowQualificationError(code)


def _verify_workflow_module_origins(repository_root: Path) -> None:
    """Bind the workflow/evaluator/projector modules absent from the P2 origin gate."""

    try:
        package_root = (
            repository_root.resolve(strict=True) / "src" / "poker_deliberation"
        ).resolve(strict=True)
        modules = {
            "poker_deliberation.bounded_river_review_workflow": (
                package_root / "bounded_river_review_workflow.py"
            ),
            "poker_deliberation.bounded_river_review_workflow_evaluation": (
                package_root / "bounded_river_review_workflow_evaluation.py"
            ),
            __name__: package_root / "bounded_river_review_workflow_qualification.py",
        }
        for module_name, expected in modules.items():
            module_file = getattr(importlib.import_module(module_name), "__file__", None)
            if module_file is None:
                _fail("BRWQ_E_MODULE_ORIGIN")
            candidate = Path(module_file)
            resolved = candidate.resolve(strict=True)
            expected_resolved = expected.resolve(strict=True)
            if (
                candidate.is_symlink()
                or resolved != expected_resolved
                or candidate.absolute() != expected.absolute()
                or not resolved.is_relative_to(package_root)
            ):
                _fail("BRWQ_E_MODULE_ORIGIN")
    except (ImportError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowQualificationError):
            raise
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_MODULE_ORIGIN") from exc


def _verify_qualification_checkout(
    repository_root: Path,
    *,
    repository_commit_id: str,
    repository_tree_id: str,
) -> None:
    try:
        verify_bridge_checkout(
            repository_root,
            repository_commit_id=repository_commit_id,
            repository_tree_id=repository_tree_id,
        )
        verify_bridge_module_origins(repository_root)
        _verify_workflow_module_origins(repository_root)
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowQualificationError):
            raise
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_CHECKOUT") from exc


def _runtime_inventory_snapshot(
    repository_root: Path,
) -> tuple[tuple[BridgeRuntimeSourceFile, ...], str]:
    try:
        inventory = bridge_runtime_source_inventory(repository_root)
        inventory_sha256 = domain_sha256(
            BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
            [{"path": item.path, "size": item.size, "sha256": item.sha256} for item in inventory],
        )
        if inventory_sha256 != bridge_runtime_source_inventory_sha256(repository_root):
            _fail("BRWQ_E_RUNTIME_INVENTORY")
        return inventory, inventory_sha256
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowQualificationError):
            raise
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_RUNTIME_INVENTORY") from exc


class _QualificationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


@dataclass(frozen=True, slots=True)
class _VerifiedQualificationInput:
    directory: Path
    plan: BoundedRiverReviewWorkflowPlanV1
    confirmation: BoundedRiverCallEvConfirmationV1
    linkage: BoundedRiverReviewWorkflowLinkageV1
    source_read: VerifiedRunReadV2
    bridge: VerifiedBridgeRead
    replayed: BridgeReplayResult
    status: BoundedRiverReviewWorkflowStatusV1
    workflow_replay: BoundedRiverReviewWorkflowStatusV1
    bindings: Mapping[BridgeRole, BoundedRiverReviewRoleConfirmationBindingV1]
    report_view: BoundedRiverReviewReportViewV1


class SanitizedBoundedRiverReviewWorkflowSourceIdentityV1(_QualificationModel):
    """Hash-only identity of the verified P3 source terminal used by the workflow."""

    source_run_id: PortableId
    source_sha256: Sha256
    source_terminal_revision: int = Field(ge=1)
    source_terminal_revision_root_sha256: Sha256
    source_terminal_pointer_sha256: Sha256
    source_terminal_manifest_sha256: Sha256
    source_terminal_inventory_sha256: Sha256
    source_terminal_completion_marker_sha256: Sha256
    source_candidate_sha256: Sha256
    source_binding_sha256: Sha256
    source_result_sha256: Sha256
    source_provenance_sha256: Sha256
    source_context_sha256: Sha256


class SanitizedBoundedRiverReviewWorkflowConfirmationHashesV1(_QualificationModel):
    """The exact twelve P3 confirmation hashes, with no authority or source content."""

    source_sha256: Sha256
    bounded_candidate_sha256: Sha256
    source_bindings_sha256: Sha256
    focal_sha256: Sha256
    extractor_sha256: Sha256
    tool_plan_sha256: Sha256
    range_definition_sha256: Sha256
    range_target_sha256: Sha256
    range_binding_sha256: Sha256
    equity_model_sha256: Sha256
    call_ev_model_sha256: Sha256
    candidate_sha256: Sha256


class SanitizedBoundedRiverReviewWorkflowLineageV1(_QualificationModel):
    """Workflow plan/linkage and the verified current bridge revision."""

    plan_sha256: Sha256
    workflow_confirmation_sha256: Sha256
    linkage_sha256: Sha256
    linked_source_terminal_manifest_sha256: Sha256
    linked_source_terminal_inventory_sha256: Sha256
    linked_bridge_manifest_sha256: Sha256
    linked_bridge_inventory_sha256: Sha256
    current_bridge_revision: int = Field(ge=1)
    current_bridge_manifest_sha256: Sha256
    current_bridge_inventory_sha256: Sha256
    current_bridge_pointer_sha256: Sha256
    current_bridge_previous_manifest_sha256: Sha256
    current_bridge_expected_pointer_sha256: Sha256
    current_bridge_completion_marker_sha256: Sha256


class SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1(_QualificationModel):
    """One workflow receipt and the five exact P2 artifact hashes it authorizes."""

    role: BridgeRole
    role_ordinal: int = Field(ge=0, le=4)
    workflow_role_confirmation_binding_sha256: Sha256
    workflow_role_confirmation_receipt_sha256: Sha256
    confirmation_field_count: Literal[17]
    confirmation_fields_sha256: Sha256
    request_sha256: Sha256
    request_bytes_sha256: Sha256
    envelope_sha256: Sha256
    runtime_policy_sha256: Sha256
    confirmation_sha256: Sha256
    admission_sha256: Sha256
    result_sha256: Sha256
    execution_audit_sha256: Sha256
    preview_bridge_revision: int = Field(ge=1)
    preview_bridge_manifest_sha256: Sha256
    preview_bridge_inventory_sha256: Sha256
    preview_bridge_pointer_sha256: Sha256
    confirmed_bridge_revision: int = Field(ge=1)
    confirmed_bridge_manifest_sha256: Sha256
    confirmed_bridge_inventory_sha256: Sha256
    confirmed_bridge_pointer_sha256: Sha256
    effect_state: Literal[BridgeEffectState.SUCCEEDED]
    transport_qualification: Literal["deterministic_fixture"]
    live_execution_evidence_sha256: Literal[None] = None

    @model_validator(mode="after")
    def exact_role_ordinal(self) -> SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1:
        if self.role is not BRIDGE_ROLE_ORDER[
            self.role_ordinal
        ] or self.confirmed_bridge_revision not in {
            self.preview_bridge_revision,
            self.preview_bridge_revision + 1,
        }:
            raise ValueError("workflow qualification role ordinal mismatch")
        return self


class SanitizedBoundedRiverReviewWorkflowTerminalEvidenceV1(_QualificationModel):
    """Hash-only terminal replay and report projection."""

    workflow_state: Literal["completed"]
    bridge_status: Literal["succeeded"]
    completed_roles: tuple[BridgeRole, ...] = Field(min_length=5, max_length=5)
    pending_role_count: Literal[0]
    reconciliation_required: Literal[False]
    total_input_tokens: int = Field(ge=0)
    total_output_tokens: int = Field(ge=0)
    total_estimated_cost_micro_usd: Literal[None] = None
    bridge_replay_sha256: Sha256
    workflow_status_sha256: Sha256
    workflow_replay_sha256: Sha256
    report_view_sha256: Sha256
    final_report_artifact_sha256: Sha256

    @model_validator(mode="after")
    def exact_completed_roles(self) -> SanitizedBoundedRiverReviewWorkflowTerminalEvidenceV1:
        if (
            self.completed_roles != BRIDGE_ROLE_ORDER
            or self.workflow_status_sha256 != self.workflow_replay_sha256
        ):
            raise ValueError("workflow qualification terminal role order mismatch")
        return self


class SanitizedBoundedRiverReviewWorkflowEvaluationEvidenceV1(_QualificationModel):
    """Safe projection of the independently self-hashed deterministic evaluation."""

    schema_version: Literal["2.0.0"]
    evaluation_id: Literal["p3-030g-bounded-river-review-workflow-evaluation-v2"]
    fixture_id: Literal["p3-030g-bounded-river-review-workflow-v2"]
    fixture_sha256: Sha256
    source_fixture_sha256: Sha256
    range_fixture_sha256: Sha256
    range_definition_sha256: Sha256
    source_projection_sha256: Sha256
    range_definition_projection_sha256: Sha256
    result_sha256: Sha256
    source_commit_id: GitObjectId
    source_tree_id: GitObjectId
    workflow_id: PortableId
    plan_sha256: Sha256
    workflow_confirmation_sha256: Sha256
    linkage_sha256: Sha256
    source_terminal_manifest_sha256: Sha256
    source_terminal_inventory_sha256: Sha256
    bridge_terminal_manifest_sha256: Sha256
    bridge_terminal_inventory_sha256: Sha256
    final_report_artifact_sha256: Sha256
    confirmation_hashes_sha256: Sha256
    role_confirmation_receipts_sha256: Sha256
    role_confirmation_fields_sha256: tuple[Sha256, ...] = Field(
        min_length=5,
        max_length=5,
    )
    all_confirmation_field_mutations_sha256: Sha256
    p2_artifact_lineage_sha256: Sha256
    terminal_replay_report_sha256: Sha256
    runtime_source_inventory_sha256: Sha256
    case_ids: tuple[PortableId, ...] = Field(min_length=6, max_length=6)
    metric_ids: tuple[PortableId, ...] = Field(min_length=6, max_length=6)
    case_evidence_sha256: tuple[tuple[Sha256, ...], ...] = Field(
        min_length=6,
        max_length=6,
    )
    metric_evidence_sha256: tuple[tuple[Sha256, ...], ...] = Field(
        min_length=6,
        max_length=6,
    )
    case_count: Literal[6]
    passed_case_count: Literal[6]
    metric_count: Literal[6]
    passed_metric_count: Literal[6]
    score_milli: Literal[1000]
    status: Literal["pass"]
    passed: Literal[True]
    transport_qualification: Literal["deterministic_fixture"]
    live_qualification_status: Literal["UNKNOWN"]
    actual_backend_model_input: Literal["UNKNOWN"]
    api_live_executed: Literal[False]

    @model_validator(mode="after")
    def every_case_and_metric_passed(
        self,
    ) -> SanitizedBoundedRiverReviewWorkflowEvaluationEvidenceV1:
        if (
            self.case_ids != _EVALUATION_CASE_IDS
            or self.metric_ids != _EVALUATION_METRIC_IDS
            or self.case_evidence_sha256 != self.metric_evidence_sha256
            or len(set(self.role_confirmation_fields_sha256)) != 5
            or any(not evidence for evidence in self.case_evidence_sha256)
            or len(self.case_ids) != self.case_count
            or len(self.metric_ids) != self.metric_count
            or self.case_count != self.passed_case_count
            or self.metric_count != self.passed_metric_count
        ):
            raise ValueError("workflow qualification evaluation is incomplete")
        return self


class SanitizedBoundedRiverReviewWorkflowQualificationManifestV1(_QualificationModel):
    """Canonical P3-030G deterministic qualification manifest."""

    schema_version: Literal["1.0.0"] = QUALIFICATION_SCHEMA_VERSION
    contract_id: Literal["poker-bounded-river-review-workflow-qualification"] = (
        "poker-bounded-river-review-workflow-qualification"
    )
    qualification_id: PortableId
    qualification_status: Literal["passed"]
    qualified_scope: Literal["p3_030f_completed_deterministic_workflow"]
    auth_mode: Literal[RuntimeAuthModeV1.CODEX_SUBSCRIPTION]
    transport_qualification: Literal["deterministic_fixture"]
    live_qualification_status: Literal["UNKNOWN"]
    actual_backend_model_input: Literal["UNKNOWN"]
    api_live_executed: Literal[False]
    api_production_qualified: Literal[False]
    repository_commit_id: GitObjectId
    repository_tree_id: GitObjectId
    runtime_source_inventory_hash_domain: Literal["poker-bounded-codex-runtime-source-inventory-v1"]
    runtime_source_inventory: tuple[SanitizedRuntimeSourceFileV1, ...] = Field(
        min_length=1,
        max_length=512,
    )
    runtime_source_inventory_sha256: Sha256
    codex_runtime_inventory_sha256: Sha256
    python_runtime_inventory_sha256: Sha256
    semantic_mapping_sha256: Sha256
    workflow_id: PortableId
    bridge_run_id: PortableId
    source_identity: SanitizedBoundedRiverReviewWorkflowSourceIdentityV1
    confirmation_hashes: SanitizedBoundedRiverReviewWorkflowConfirmationHashesV1
    lineage: SanitizedBoundedRiverReviewWorkflowLineageV1
    roles: tuple[SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1, ...] = Field(
        min_length=5,
        max_length=5,
    )
    terminal: SanitizedBoundedRiverReviewWorkflowTerminalEvidenceV1
    deterministic_evaluation: SanitizedBoundedRiverReviewWorkflowEvaluationEvidenceV1
    limitations: tuple[str, ...] = Field(min_length=5, max_length=5)
    manifest_sha256: Sha256

    @model_validator(mode="after")
    def exact_sanitized_qualification(
        self,
    ) -> SanitizedBoundedRiverReviewWorkflowQualificationManifestV1:
        inventory_payload = [item.model_dump(mode="json") for item in self.runtime_source_inventory]
        confirmation_hash_values = [
            self.confirmation_hashes.source_sha256,
            self.confirmation_hashes.bounded_candidate_sha256,
            self.confirmation_hashes.source_bindings_sha256,
            self.confirmation_hashes.focal_sha256,
            self.confirmation_hashes.extractor_sha256,
            self.confirmation_hashes.tool_plan_sha256,
            self.confirmation_hashes.range_definition_sha256,
            self.confirmation_hashes.range_target_sha256,
            self.confirmation_hashes.range_binding_sha256,
            self.confirmation_hashes.equity_model_sha256,
            self.confirmation_hashes.call_ev_model_sha256,
            self.confirmation_hashes.candidate_sha256,
        ]
        role_receipts_payload = {
            "field_hashes": [item.confirmation_fields_sha256 for item in self.roles],
            "receipt_hashes": [
                item.workflow_role_confirmation_receipt_sha256 for item in self.roles
            ],
        }
        p2_artifact_hashes = [
            artifact_sha256
            for item in self.roles
            for artifact_sha256 in (
                item.request_sha256,
                item.confirmation_sha256,
                item.admission_sha256,
                item.result_sha256,
                item.execution_audit_sha256,
            )
        ]
        terminal_payload = {
            "workflow_status_sha256": self.terminal.workflow_status_sha256,
            "workflow_replay_sha256": self.terminal.workflow_replay_sha256,
            "bridge_manifest_sha256": self.lineage.current_bridge_manifest_sha256,
            "bridge_inventory_sha256": self.lineage.current_bridge_inventory_sha256,
            "report_sha256": self.terminal.report_view_sha256,
        }
        inventory_by_path = {item.path: item.sha256 for item in self.runtime_source_inventory}
        expected_evaluation_evidence = (
            (
                self.deterministic_evaluation.fixture_sha256,
                self.lineage.plan_sha256,
                self.source_identity.source_terminal_manifest_sha256,
            ),
            (
                self.deterministic_evaluation.confirmation_hashes_sha256,
                self.lineage.workflow_confirmation_sha256,
            ),
            (
                self.deterministic_evaluation.role_confirmation_receipts_sha256,
                *(item.confirmation_fields_sha256 for item in self.roles),
                _EVALUATION_MUTATION_CLAIM_SHA256,
            ),
            (self.deterministic_evaluation.p2_artifact_lineage_sha256,),
            (
                self.deterministic_evaluation.terminal_replay_report_sha256,
                self.terminal.final_report_artifact_sha256,
            ),
            (
                self.runtime_source_inventory_sha256,
                self.runtime_source_inventory_sha256,
            ),
        )
        if (
            tuple(item.role for item in self.roles) != BRIDGE_ROLE_ORDER
            or tuple(item.role_ordinal for item in self.roles) != tuple(range(5))
            or len({item.workflow_role_confirmation_binding_sha256 for item in self.roles}) != 5
            or len({item.workflow_role_confirmation_receipt_sha256 for item in self.roles}) != 5
            or len({item.confirmation_fields_sha256 for item in self.roles}) != 5
            or len({item.request_sha256 for item in self.roles}) != 5
            or len({item.confirmation_sha256 for item in self.roles}) != 5
            or len({item.admission_sha256 for item in self.roles}) != 5
            or len({item.result_sha256 for item in self.roles}) != 5
            or len({item.execution_audit_sha256 for item in self.roles}) != 5
            or any(
                item.confirmed_bridge_revision > self.lineage.current_bridge_revision
                for item in self.roles
            )
            or tuple(item.path for item in self.runtime_source_inventory)
            != tuple(sorted(item.path for item in self.runtime_source_inventory))
            or len({item.path for item in self.runtime_source_inventory})
            != len(self.runtime_source_inventory)
            or self.runtime_source_inventory_sha256
            != domain_sha256(
                BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
                inventory_payload,
            )
            or self.source_identity.source_sha256
            != self.deterministic_evaluation.source_fixture_sha256
            or self.deterministic_evaluation.source_projection_sha256
            != self.confirmation_hashes.source_sha256
            or self.deterministic_evaluation.range_definition_projection_sha256
            != self.confirmation_hashes.range_definition_sha256
            or inventory_by_path.get(_EVALUATION_FIXTURE_PATH)
            != self.deterministic_evaluation.fixture_sha256
            or inventory_by_path.get(_EVALUATION_SOURCE_PATH)
            != self.deterministic_evaluation.source_fixture_sha256
            or inventory_by_path.get(_EVALUATION_RANGE_PATH)
            != self.deterministic_evaluation.range_fixture_sha256
            or self.confirmation_hashes.candidate_sha256
            != self.source_identity.source_candidate_sha256
            or self.lineage.linked_source_terminal_manifest_sha256
            != self.source_identity.source_terminal_manifest_sha256
            or self.lineage.linked_source_terminal_inventory_sha256
            != self.source_identity.source_terminal_inventory_sha256
            or self.deterministic_evaluation.source_commit_id != self.repository_commit_id
            or self.deterministic_evaluation.source_tree_id != self.repository_tree_id
            or self.deterministic_evaluation.workflow_id != self.workflow_id
            or self.deterministic_evaluation.plan_sha256 != self.lineage.plan_sha256
            or self.deterministic_evaluation.workflow_confirmation_sha256
            != self.lineage.workflow_confirmation_sha256
            or self.deterministic_evaluation.linkage_sha256 != self.lineage.linkage_sha256
            or self.deterministic_evaluation.source_terminal_manifest_sha256
            != self.source_identity.source_terminal_manifest_sha256
            or self.deterministic_evaluation.source_terminal_inventory_sha256
            != self.source_identity.source_terminal_inventory_sha256
            or self.deterministic_evaluation.bridge_terminal_manifest_sha256
            != self.lineage.current_bridge_manifest_sha256
            or self.deterministic_evaluation.bridge_terminal_inventory_sha256
            != self.lineage.current_bridge_inventory_sha256
            or self.deterministic_evaluation.final_report_artifact_sha256
            != self.terminal.final_report_artifact_sha256
            or self.deterministic_evaluation.confirmation_hashes_sha256
            != domain_sha256(
                _EVALUATION_CONFIRMATION_HASH_DOMAIN,
                confirmation_hash_values,
            )
            or self.deterministic_evaluation.role_confirmation_receipts_sha256
            != domain_sha256(
                _EVALUATION_RECEIPTS_HASH_DOMAIN,
                role_receipts_payload,
            )
            or self.deterministic_evaluation.role_confirmation_fields_sha256
            != tuple(item.confirmation_fields_sha256 for item in self.roles)
            or self.deterministic_evaluation.all_confirmation_field_mutations_sha256
            != _EVALUATION_MUTATION_CLAIM_SHA256
            or self.deterministic_evaluation.p2_artifact_lineage_sha256
            != domain_sha256(
                _EVALUATION_P2_LINEAGE_HASH_DOMAIN,
                p2_artifact_hashes,
            )
            or self.deterministic_evaluation.terminal_replay_report_sha256
            != domain_sha256(
                _EVALUATION_TERMINAL_HASH_DOMAIN,
                terminal_payload,
            )
            or self.deterministic_evaluation.case_evidence_sha256 != expected_evaluation_evidence
            or self.deterministic_evaluation.metric_evidence_sha256 != expected_evaluation_evidence
            or self.deterministic_evaluation.runtime_source_inventory_sha256
            != self.runtime_source_inventory_sha256
        ):
            raise ValueError("workflow qualification evidence correlation mismatch")
        if self.limitations != QUALIFICATION_LIMITATIONS:
            raise ValueError("workflow qualification limitations mismatch")
        if self.manifest_sha256 != domain_sha256(
            QUALIFICATION_MANIFEST_HASH_DOMAIN,
            without_field(self, "manifest_sha256"),
        ):
            raise ValueError("workflow qualification manifest hash mismatch")
        return self


def _confirmation_hash_projection(
    confirmation: BoundedRiverCallEvConfirmationV1,
) -> SanitizedBoundedRiverReviewWorkflowConfirmationHashesV1:
    try:
        return SanitizedBoundedRiverReviewWorkflowConfirmationHashesV1(
            source_sha256=confirmation.source_sha256,
            bounded_candidate_sha256=confirmation.bounded_candidate_sha256,
            source_bindings_sha256=confirmation.source_bindings_sha256,
            focal_sha256=confirmation.focal_sha256,
            extractor_sha256=confirmation.extractor_sha256,
            tool_plan_sha256=confirmation.tool_plan_sha256,
            range_definition_sha256=confirmation.range_definition_sha256,
            range_target_sha256=confirmation.range_target_sha256,
            range_binding_sha256=confirmation.range_binding_sha256,
            equity_model_sha256=confirmation.equity_model_sha256,
            call_ev_model_sha256=confirmation.call_ev_model_sha256,
            candidate_sha256=confirmation.candidate_sha256,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_CONFIRMATION") from exc


def _bridge_replay_sha256(replayed: BridgeReplayResult) -> str:
    try:
        payload = {
            "bridge_run_id": replayed.bridge_run_id,
            "auth_mode": replayed.auth_mode,
            "revision": replayed.revision,
            "status": replayed.status,
            "completed_roles": replayed.completed_roles,
            "pending_roles": replayed.pending_roles,
            "reconciliation_required": replayed.reconciliation_required,
            "total_input_tokens": replayed.total_input_tokens,
            "total_output_tokens": replayed.total_output_tokens,
            "total_estimated_cost_micro_usd": replayed.total_estimated_cost_micro_usd,
        }
        return domain_sha256(QUALIFICATION_BRIDGE_REPLAY_HASH_DOMAIN, payload)
    except (AttributeError, TypeError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_TERMINAL") from exc


def _role_projection(
    *,
    role: BridgeRole,
    binding: BoundedRiverReviewRoleConfirmationBindingV1,
    artifacts: Mapping[str, object],
) -> SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1:
    request = artifacts.get(role_artifact_name(role, "request"))
    confirmation = artifacts.get(role_artifact_name(role, "confirmation"))
    admission = artifacts.get(role_artifact_name(role, "admission"))
    result = artifacts.get(role_artifact_name(role, "result"))
    audit = artifacts.get(role_artifact_name(role, "audit"))
    if (
        not isinstance(request, BoundedCodexBridgeRequestV1)
        or not isinstance(confirmation, BridgeRoleConfirmationV1)
        or not isinstance(admission, BridgePreExecutionAdmissionV1)
        or not isinstance(result, BridgeRoleResultV1)
        or not isinstance(audit, BridgeExecutionAuditV1)
        or audit.effect_state is not BridgeEffectState.SUCCEEDED
        or audit.transport_qualification != "deterministic_fixture"
        or audit.live_execution_evidence is not None
    ):
        _fail("BRWQ_E_ROLE_EVIDENCE")
    try:
        if (
            binding.role is not role
            or binding.role_ordinal != BRIDGE_ROLE_ORDER.index(role)
            or binding.request_sha256 != request.request_sha256
            or binding.request_bytes_sha256 != request.request_bytes_sha256
            or binding.envelope_sha256 != request.context.envelope_sha256
            or binding.runtime_policy_sha256 != request.context.runtime_policy.policy_sha256
            or binding.runtime_identity != request.context.runtime_policy.runtime_identity
            or binding.model_provider != request.context.runtime_policy.model_provider
            or binding.model != request.context.runtime_policy.model
            or binding.credential_reference != request.context.runtime_policy.credential_reference
            or binding.remote_retention_policy
            != request.context.runtime_policy.remote_retention_policy
            or binding.bridge_confirmation_sha256 != confirmation.confirmation_sha256
            or admission.request_sha256 != request.request_sha256
            or admission.confirmation_sha256 != confirmation.confirmation_sha256
            or audit.request_sha256 != request.request_sha256
            or audit.confirmation_sha256 != confirmation.confirmation_sha256
            or audit.admission_sha256 != admission.admission_sha256
            or audit.result_sha256 != result.result_sha256
        ):
            _fail("BRWQ_E_ROLE_BINDING")
        confirmation_fields = {
            "expected_plan_sha256": binding.plan_sha256,
            "expected_linkage_sha256": binding.linkage_sha256,
            "expected_bridge_revision": binding.preview_bridge_revision,
            "expected_bridge_manifest_sha256": binding.preview_bridge_manifest_sha256,
            "expected_bridge_inventory_sha256": binding.preview_bridge_inventory_sha256,
            "expected_bridge_pointer_sha256": binding.preview_bridge_pointer_sha256,
            "expected_role": role,
            "expected_auth_mode": binding.auth_mode,
            "expected_request_sha256": binding.request_sha256,
            "expected_request_bytes_sha256": binding.request_bytes_sha256,
            "expected_envelope_sha256": binding.envelope_sha256,
            "expected_runtime_policy_sha256": binding.runtime_policy_sha256,
            "expected_runtime_identity": binding.runtime_identity,
            "expected_model_provider": binding.model_provider,
            "expected_model": binding.model,
            "expected_credential_reference": binding.credential_reference,
            "expected_remote_retention_policy": binding.remote_retention_policy,
        }
        if len(confirmation_fields) != 17:
            _fail("BRWQ_E_ROLE_BINDING")
        return SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1(
            role=role,
            role_ordinal=binding.role_ordinal,
            workflow_role_confirmation_binding_sha256=binding.binding_sha256,
            workflow_role_confirmation_receipt_sha256=sha256_bytes(
                terminal_canonical_json_bytes(binding)
            ),
            confirmation_field_count=17,
            confirmation_fields_sha256=domain_sha256(
                QUALIFICATION_CONFIRMATION_FIELDS_HASH_DOMAIN,
                confirmation_fields,
            ),
            request_sha256=request.request_sha256,
            request_bytes_sha256=request.request_bytes_sha256,
            envelope_sha256=request.context.envelope_sha256,
            runtime_policy_sha256=request.context.runtime_policy.policy_sha256,
            confirmation_sha256=confirmation.confirmation_sha256,
            admission_sha256=admission.admission_sha256,
            result_sha256=result.result_sha256,
            execution_audit_sha256=audit.audit_sha256,
            preview_bridge_revision=binding.preview_bridge_revision,
            preview_bridge_manifest_sha256=binding.preview_bridge_manifest_sha256,
            preview_bridge_inventory_sha256=binding.preview_bridge_inventory_sha256,
            preview_bridge_pointer_sha256=binding.preview_bridge_pointer_sha256,
            confirmed_bridge_revision=binding.confirmed_bridge_revision,
            confirmed_bridge_manifest_sha256=binding.confirmed_bridge_manifest_sha256,
            confirmed_bridge_inventory_sha256=binding.confirmed_bridge_inventory_sha256,
            confirmed_bridge_pointer_sha256=binding.confirmed_bridge_pointer_sha256,
            effect_state=BridgeEffectState.SUCCEEDED,
            transport_qualification="deterministic_fixture",
            live_execution_evidence_sha256=None,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowQualificationError):
            raise
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_ROLE_BINDING") from exc


def _load_verified_workflow_evidence(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
) -> _VerifiedQualificationInput:
    """Use the workflow's production verification path without exposing its raw tuple."""

    try:
        observed = workflow._verified_role_workflow(
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
            pure_read=True,
        )
        (
            directory,
            plan,
            confirmation,
            linkage,
            _source_read,
            bridge,
            replayed,
            _status,
        ) = observed
        bindings = workflow._verified_role_confirmation_bindings(
            directory,
            plan,
            confirmation,
            linkage,
            bridge,
            replayed,
        )
        report_view = workflow.bounded_river_review_report_view(
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
        )
        workflow_replay = workflow.replay_bounded_river_review_workflow(
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
        )
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_WORKFLOW") from exc
    return _VerifiedQualificationInput(
        directory=directory,
        plan=plan,
        confirmation=confirmation,
        linkage=linkage,
        source_read=_source_read,
        bridge=bridge,
        replayed=replayed,
        status=_status,
        workflow_replay=workflow_replay,
        bindings=bindings,
        report_view=report_view,
    )


def _validated_workflow_projections(
    evidence: _VerifiedQualificationInput,
    *,
    runtime_inventory_snapshot: tuple[BridgeRuntimeSourceFile, ...],
    runtime_inventory_sha256: str,
) -> tuple[
    BridgeRunPlanV1,
    BridgeSourceContextV1,
    tuple[SanitizedRuntimeSourceFileV1, ...],
    str,
    SanitizedBoundedRiverReviewWorkflowSourceIdentityV1,
    SanitizedBoundedRiverReviewWorkflowConfirmationHashesV1,
    SanitizedBoundedRiverReviewWorkflowLineageV1,
    tuple[SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1, ...],
    SanitizedBoundedRiverReviewWorkflowTerminalEvidenceV1,
]:
    plan = evidence.plan
    linkage = evidence.linkage
    source_read = evidence.source_read
    bridge = evidence.bridge
    replayed = evidence.replayed
    status = evidence.status
    workflow_replay = evidence.workflow_replay
    report_view = evidence.report_view
    artifacts = {item.logical_name: item.model for item in bridge.decoded_artifacts()}
    bridge_plan = artifacts.get("run_plan.json")
    source_context = artifacts.get("source_context.json")
    bridge_marker_sha256 = bridge.pointer.completion_marker_sha256
    if (
        not isinstance(bridge_plan, BridgeRunPlanV1)
        or not isinstance(source_context, BridgeSourceContextV1)
        or plan.auth_mode is not RuntimeAuthModeV1.CODEX_SUBSCRIPTION
        or bridge_plan.auth_mode is not RuntimeAuthModeV1.CODEX_SUBSCRIPTION
        or status.state != "completed"
        or status.bridge_status != "succeeded"
        or status.completed_roles != BRIDGE_ROLE_ORDER
        or status.pending_roles
        or status.reconciliation_required
        or workflow_replay != status
        or replayed.status != "succeeded"
        or replayed.completed_roles != BRIDGE_ROLE_ORDER
        or replayed.pending_roles
        or replayed.reconciliation_required
        or replayed.total_estimated_cost_micro_usd is not None
        or replayed.revision != bridge.pointer.revision
        or report_view.state != "completed"
        or report_view.bridge_status != "succeeded"
        or report_view.completed_roles != BRIDGE_ROLE_ORDER
        or set(evidence.bindings) != set(BRIDGE_ROLE_ORDER)
        or source_read.completion_marker_sha256 is None
        or bridge.completion_marker is None
        or bridge.completion_marker_bytes is None
        or bridge_marker_sha256 is None
        or sha256_bytes(bridge.completion_marker_bytes) != bridge_marker_sha256
        or bridge.pointer.bridge_run_id != bridge.manifest.bridge_run_id
        or bridge.pointer.auth_mode is not bridge.manifest.auth_mode
        or bridge.pointer.revision != bridge.manifest.revision
        or bridge.pointer.transaction_id != bridge.manifest.transaction_id
        or bridge.pointer.status != bridge.manifest.status
        or bridge.pointer.manifest_sha256 != bridge.manifest.manifest_sha256
        or bridge.pointer.inventory_sha256 != bridge.manifest.inventory_sha256
        or bridge.completion_marker.bridge_run_id != bridge.pointer.bridge_run_id
        or bridge.completion_marker.auth_mode is not bridge.pointer.auth_mode
        or bridge.completion_marker.terminal_revision != bridge.pointer.revision
        or bridge.completion_marker.terminal_transaction_id != bridge.pointer.transaction_id
        or bridge.completion_marker.terminal_status != bridge.pointer.status
        or bridge.completion_marker.terminal_manifest_sha256 != bridge.pointer.manifest_sha256
        or bridge.completion_marker.inventory_sha256 != bridge.pointer.inventory_sha256
        or bridge.manifest.previous_manifest_sha256 is None
        or bridge.manifest.expected_pointer_sha256 is None
        or plan.repository_commit_id != bridge_plan.repository_commit_id
        or plan.repository_tree_id != bridge_plan.repository_tree_id
        or plan.bridge_run_id != bridge_plan.bridge_run_id
        or plan.source_run_id != source_context.source.source_terminal_run_id
        or bridge_plan.source != source_context.source
        or source_context.source.source_terminal_manifest_sha256 != source_read.manifest_sha256
        or source_context.source.source_terminal_inventory_sha256 != source_read.inventory_sha256
        or linkage.source_terminal_manifest_sha256 != source_read.manifest_sha256
        or linkage.source_terminal_inventory_sha256 != source_read.inventory_sha256
        or status.workflow_id != plan.workflow_id
        or status.plan_sha256 != plan.plan_sha256
        or status.confirmation_sha256 != evidence.confirmation.confirmation_sha256
        or status.bridge_manifest_sha256 != bridge.manifest.manifest_sha256
        or report_view.workflow_id != plan.workflow_id
        or report_view.source_run_id != plan.source_run_id
        or report_view.bridge_run_id != plan.bridge_run_id
        or report_view.plan_sha256 != plan.plan_sha256
        or report_view.confirmation_sha256 != evidence.confirmation.confirmation_sha256
        or report_view.linkage_sha256 != linkage.linkage_sha256
        or report_view.source_terminal_manifest_sha256 != source_read.manifest_sha256
        or report_view.source_terminal_inventory_sha256 != source_read.inventory_sha256
        or report_view.bridge_manifest_sha256 != bridge.manifest.manifest_sha256
        or report_view.bridge_inventory_sha256 != bridge.manifest.inventory_sha256
    ):
        _fail("BRWQ_E_TERMINAL")

    source_identity = SanitizedBoundedRiverReviewWorkflowSourceIdentityV1(
        source_run_id=source_context.source.source_terminal_run_id,
        source_sha256=plan.source_sha256,
        source_terminal_revision=source_context.source.source_terminal_revision,
        source_terminal_revision_root_sha256=(
            source_context.source.source_terminal_revision_root_sha256
        ),
        source_terminal_pointer_sha256=source_read.current_pointer_sha256,
        source_terminal_manifest_sha256=source_read.manifest_sha256,
        source_terminal_inventory_sha256=source_read.inventory_sha256,
        source_terminal_completion_marker_sha256=source_read.completion_marker_sha256,
        source_candidate_sha256=source_context.source.source_candidate_sha256,
        source_binding_sha256=source_context.source.source_binding_sha256,
        source_result_sha256=source_context.source.source_result_sha256,
        source_provenance_sha256=source_context.source.source_provenance_sha256,
        source_context_sha256=source_context.context_payload_sha256,
    )
    confirmation_hashes = _confirmation_hash_projection(evidence.confirmation)
    lineage = SanitizedBoundedRiverReviewWorkflowLineageV1(
        plan_sha256=plan.plan_sha256,
        workflow_confirmation_sha256=evidence.confirmation.confirmation_sha256,
        linkage_sha256=linkage.linkage_sha256,
        linked_source_terminal_manifest_sha256=linkage.source_terminal_manifest_sha256,
        linked_source_terminal_inventory_sha256=linkage.source_terminal_inventory_sha256,
        linked_bridge_manifest_sha256=linkage.bridge_manifest_sha256,
        linked_bridge_inventory_sha256=linkage.bridge_inventory_sha256,
        current_bridge_revision=bridge.pointer.revision,
        current_bridge_manifest_sha256=bridge.manifest.manifest_sha256,
        current_bridge_inventory_sha256=bridge.manifest.inventory_sha256,
        current_bridge_pointer_sha256=bridge.pointer_sha256,
        current_bridge_previous_manifest_sha256=bridge.manifest.previous_manifest_sha256,
        current_bridge_expected_pointer_sha256=bridge.manifest.expected_pointer_sha256,
        current_bridge_completion_marker_sha256=bridge_marker_sha256,
    )
    roles = tuple(
        _role_projection(
            role=role,
            binding=evidence.bindings[role],
            artifacts=artifacts,
        )
        for role in BRIDGE_ROLE_ORDER
    )
    terminal = SanitizedBoundedRiverReviewWorkflowTerminalEvidenceV1(
        workflow_state="completed",
        bridge_status="succeeded",
        completed_roles=BRIDGE_ROLE_ORDER,
        pending_role_count=0,
        reconciliation_required=False,
        total_input_tokens=replayed.total_input_tokens,
        total_output_tokens=replayed.total_output_tokens,
        total_estimated_cost_micro_usd=None,
        bridge_replay_sha256=_bridge_replay_sha256(replayed),
        workflow_status_sha256=sha256_bytes(terminal_canonical_json_bytes(status)),
        workflow_replay_sha256=sha256_bytes(terminal_canonical_json_bytes(workflow_replay)),
        report_view_sha256=sha256_bytes(terminal_canonical_json_bytes(report_view)),
        final_report_artifact_sha256=report_view.final_report_artifact_sha256,
    )
    runtime_inventory = tuple(
        SanitizedRuntimeSourceFileV1(path=item.path, size=item.size, sha256=item.sha256)
        for item in runtime_inventory_snapshot
    )
    if runtime_inventory_sha256 != domain_sha256(
        BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN,
        [item.model_dump(mode="json") for item in runtime_inventory],
    ):
        _fail("BRWQ_E_RUNTIME_INVENTORY")
    return (
        bridge_plan,
        source_context,
        runtime_inventory,
        runtime_inventory_sha256,
        source_identity,
        confirmation_hashes,
        lineage,
        roles,
        terminal,
    )


def _verified_evaluation_fixture_projection(
    *,
    repository_root: Path,
    runtime_inventory_snapshot: tuple[BridgeRuntimeSourceFile, ...],
) -> tuple[str, str, str, str, str, str]:
    """Bind the V2 fixture and its source/range files to the verified inventory."""

    try:
        from poker_deliberation.bounded_river_review_workflow_evaluation import (
            load_bounded_river_review_workflow_fixture_v2,
        )

        inventory_by_path = {item.path: item for item in runtime_inventory_snapshot}
        if len(inventory_by_path) != len(runtime_inventory_snapshot):
            _fail("BRWQ_E_EVALUATION_FIXTURE")
        fixture_entry = inventory_by_path[_EVALUATION_FIXTURE_PATH]
        source_entry = inventory_by_path[_EVALUATION_SOURCE_PATH]
        range_entry = inventory_by_path[_EVALUATION_RANGE_PATH]
        fixture_path = repository_root.joinpath(*_EVALUATION_FIXTURE_PATH.split("/"))
        source_path = repository_root.joinpath(*_EVALUATION_SOURCE_PATH.split("/"))
        range_path = repository_root.joinpath(*_EVALUATION_RANGE_PATH.split("/"))
        fixture, fixture_sha256 = load_bounded_river_review_workflow_fixture_v2(fixture_path)
        source_bytes = source_path.read_bytes()
        range_file_bytes = range_path.read_bytes()
        range_definition_bytes = range_file_bytes.removesuffix(b"\n")
        range_definition = parse_canonical_model(
            range_definition_bytes,
            VersionedRangeDefinitionV1,
        )
        if (
            fixture_sha256 != fixture_entry.sha256
            or sha256_bytes(fixture_path.read_bytes()) != fixture_sha256
            or sha256_bytes(source_bytes) != source_entry.sha256
            or sha256_bytes(range_file_bytes) != range_entry.sha256
            or canonical_json_bytes(range_definition) != range_definition_bytes
            or fixture.source_sha256 != source_entry.sha256
            or fixture.range_sha256 != sha256_bytes(range_definition_bytes)
        ):
            _fail("BRWQ_E_EVALUATION_FIXTURE")
        return (
            fixture_sha256,
            fixture.source_sha256,
            range_entry.sha256,
            fixture.range_sha256,
            hashlib.sha256(SOURCE_HASH_DOMAIN.encode("ascii") + b"\0" + source_bytes).hexdigest(),
            domain_sha256(
                RANGE_DEFINITION_HASH_DOMAIN,
                terminal_canonical_json_bytes(range_definition),
            ),
        )
    except (KeyError, ImportError, OSError, TypeError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowQualificationError):
            raise
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_EVALUATION_FIXTURE") from exc


def _evaluation_projection(
    evaluation: BoundedRiverReviewWorkflowEvaluationResultV2,
    *,
    repository_root: Path,
    runtime_inventory_snapshot: tuple[BridgeRuntimeSourceFile, ...],
) -> SanitizedBoundedRiverReviewWorkflowEvaluationEvidenceV1:
    """Verify V2 independently before retaining its safe codes, counts, and hashes."""

    try:
        from poker_deliberation.bounded_river_review_workflow_evaluation import (
            BoundedRiverReviewWorkflowEvaluationResultV2,
            verify_bounded_river_review_workflow_evaluation_result_v2,
        )

        if not isinstance(evaluation, BoundedRiverReviewWorkflowEvaluationResultV2):
            _fail("BRWQ_E_EVALUATION")
        verified = verify_bounded_river_review_workflow_evaluation_result_v2(
            evaluation,
            repository_root=repository_root,
            fixture_path=repository_root.joinpath(*_EVALUATION_FIXTURE_PATH.split("/")),
            source_path=repository_root.joinpath(*_EVALUATION_SOURCE_PATH.split("/")),
            range_path=repository_root.joinpath(*_EVALUATION_RANGE_PATH.split("/")),
            source_commit_id=evaluation.source_commit_id,
            source_tree_id=evaluation.source_tree_id,
        )
        if verified is not True:
            _fail("BRWQ_E_EVALUATION")
        (
            fixture_sha256,
            source_fixture_sha256,
            range_fixture_sha256,
            range_definition_sha256,
            source_projection_sha256,
            range_definition_projection_sha256,
        ) = _verified_evaluation_fixture_projection(
            repository_root=repository_root,
            runtime_inventory_snapshot=runtime_inventory_snapshot,
        )
        cases = tuple(evaluation.cases)
        metrics = tuple(evaluation.metrics)
        if (
            tuple(item.case_id for item in cases) != _EVALUATION_CASE_IDS
            or tuple(item.metric for item in metrics) != _EVALUATION_METRIC_IDS
            or not evaluation.passed
            or evaluation.status != "pass"
            or evaluation.score_milli != 1000
            or evaluation.transport_qualification != "deterministic_fixture"
            or evaluation.live_qualification_status != "UNKNOWN"
            or evaluation.actual_backend_model_input != "UNKNOWN"
            or evaluation.api_live_executed is not False
            or any(not item.passed for item in cases)
            or any(not item.passed for item in metrics)
            or any(item.expected_evidence_sha256 != item.observed_evidence_sha256 for item in cases)
            or any(
                item.expected_evidence_sha256 != item.observed_evidence_sha256 for item in metrics
            )
            or tuple(item.expected_evidence_sha256 for item in cases)
            != tuple(item.expected_evidence_sha256 for item in metrics)
            or evaluation.fixture_sha256 != fixture_sha256
            or evaluation.source_fixture_sha256 != source_fixture_sha256
            or evaluation.range_fixture_sha256 != range_fixture_sha256
            or evaluation.range_definition_sha256 != range_definition_sha256
        ):
            _fail("BRWQ_E_EVALUATION")
        return SanitizedBoundedRiverReviewWorkflowEvaluationEvidenceV1(
            schema_version=evaluation.schema_version,
            evaluation_id=evaluation.evaluation_id,
            fixture_id=evaluation.fixture_id,
            fixture_sha256=evaluation.fixture_sha256,
            source_fixture_sha256=source_fixture_sha256,
            range_fixture_sha256=range_fixture_sha256,
            range_definition_sha256=range_definition_sha256,
            source_projection_sha256=source_projection_sha256,
            range_definition_projection_sha256=range_definition_projection_sha256,
            result_sha256=evaluation.result_sha256,
            source_commit_id=evaluation.source_commit_id,
            source_tree_id=evaluation.source_tree_id,
            workflow_id=evaluation.workflow_id,
            plan_sha256=evaluation.plan_sha256,
            workflow_confirmation_sha256=evaluation.workflow_confirmation_sha256,
            linkage_sha256=evaluation.linkage_sha256,
            source_terminal_manifest_sha256=evaluation.source_terminal_manifest_sha256,
            source_terminal_inventory_sha256=evaluation.source_terminal_inventory_sha256,
            bridge_terminal_manifest_sha256=evaluation.bridge_terminal_manifest_sha256,
            bridge_terminal_inventory_sha256=evaluation.bridge_terminal_inventory_sha256,
            final_report_artifact_sha256=evaluation.final_report_artifact_sha256,
            confirmation_hashes_sha256=evaluation.confirmation_hashes_sha256,
            role_confirmation_receipts_sha256=(evaluation.role_confirmation_receipts_sha256),
            role_confirmation_fields_sha256=evaluation.role_confirmation_fields_sha256,
            all_confirmation_field_mutations_sha256=(
                evaluation.all_confirmation_field_mutations_sha256
            ),
            p2_artifact_lineage_sha256=evaluation.p2_artifact_lineage_sha256,
            terminal_replay_report_sha256=evaluation.terminal_replay_report_sha256,
            runtime_source_inventory_sha256=evaluation.runtime_source_inventory_sha256,
            case_ids=tuple(item.case_id for item in cases),
            metric_ids=tuple(item.metric for item in metrics),
            case_evidence_sha256=tuple(item.expected_evidence_sha256 for item in cases),
            metric_evidence_sha256=tuple(item.expected_evidence_sha256 for item in metrics),
            case_count=6,
            passed_case_count=6,
            metric_count=6,
            passed_metric_count=6,
            score_milli=1000,
            status="pass",
            passed=True,
            transport_qualification="deterministic_fixture",
            live_qualification_status="UNKNOWN",
            actual_backend_model_input="UNKNOWN",
            api_live_executed=False,
        )
    except (AttributeError, ImportError, TypeError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowQualificationError):
            raise
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_EVALUATION") from exc


def build_sanitized_bounded_river_review_workflow_qualification_manifest(
    *,
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    qualification_id: str,
    deterministic_evaluation: BoundedRiverReviewWorkflowEvaluationResultV2,
) -> SanitizedBoundedRiverReviewWorkflowQualificationManifestV1:
    """Project one verified, completed deterministic workflow into public evidence."""

    try:
        claimed_commit_id = deterministic_evaluation.source_commit_id
        claimed_tree_id = deterministic_evaluation.source_tree_id
    except AttributeError as exc:
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_EVALUATION") from exc
    _verify_qualification_checkout(
        repository_root,
        repository_commit_id=claimed_commit_id,
        repository_tree_id=claimed_tree_id,
    )
    starting_inventory, starting_inventory_sha256 = _runtime_inventory_snapshot(repository_root)
    evaluation = _evaluation_projection(
        deterministic_evaluation,
        repository_root=repository_root,
        runtime_inventory_snapshot=starting_inventory,
    )
    evidence = _load_verified_workflow_evidence(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
    )
    if (
        evidence.plan.repository_commit_id != evaluation.source_commit_id
        or evidence.plan.repository_tree_id != evaluation.source_tree_id
    ):
        _fail("BRWQ_E_CHECKOUT")
    (
        bridge_plan,
        _source_context,
        runtime_inventory,
        runtime_inventory_sha256,
        source_identity,
        confirmation_hashes,
        lineage,
        roles,
        terminal,
    ) = _validated_workflow_projections(
        evidence,
        runtime_inventory_snapshot=starting_inventory,
        runtime_inventory_sha256=starting_inventory_sha256,
    )
    payload: dict[str, object] = {
        "schema_version": QUALIFICATION_SCHEMA_VERSION,
        "contract_id": "poker-bounded-river-review-workflow-qualification",
        "qualification_id": qualification_id,
        "qualification_status": "passed",
        "qualified_scope": "p3_030f_completed_deterministic_workflow",
        "auth_mode": RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        "transport_qualification": "deterministic_fixture",
        "live_qualification_status": "UNKNOWN",
        "actual_backend_model_input": "UNKNOWN",
        "api_live_executed": False,
        "api_production_qualified": False,
        "repository_commit_id": evidence.plan.repository_commit_id,
        "repository_tree_id": evidence.plan.repository_tree_id,
        "runtime_source_inventory_hash_domain": (BRIDGE_RUNTIME_SOURCE_INVENTORY_HASH_DOMAIN),
        "runtime_source_inventory": runtime_inventory,
        "runtime_source_inventory_sha256": runtime_inventory_sha256,
        "codex_runtime_inventory_sha256": bridge_plan.codex_runtime_inventory_sha256,
        "python_runtime_inventory_sha256": bridge_plan.python_runtime_inventory_sha256,
        "semantic_mapping_sha256": bridge_plan.semantic_mapping_sha256,
        "workflow_id": evidence.plan.workflow_id,
        "bridge_run_id": evidence.plan.bridge_run_id,
        "source_identity": source_identity,
        "confirmation_hashes": confirmation_hashes,
        "lineage": lineage,
        "roles": roles,
        "terminal": terminal,
        "deterministic_evaluation": evaluation,
        "limitations": QUALIFICATION_LIMITATIONS,
    }
    try:
        manifest = SanitizedBoundedRiverReviewWorkflowQualificationManifestV1.model_validate(
            {
                **payload,
                "manifest_sha256": domain_sha256(
                    QUALIFICATION_MANIFEST_HASH_DOMAIN,
                    payload,
                ),
            },
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_CORRELATION") from exc
    ending_inventory, ending_inventory_sha256 = _runtime_inventory_snapshot(repository_root)
    if (
        ending_inventory != starting_inventory
        or ending_inventory_sha256 != starting_inventory_sha256
    ):
        _fail("BRWQ_E_RUNTIME_INVENTORY")
    _verify_qualification_checkout(
        repository_root,
        repository_commit_id=evaluation.source_commit_id,
        repository_tree_id=evaluation.source_tree_id,
    )
    return manifest


def load_sanitized_bounded_river_review_workflow_qualification_manifest(
    path: Path,
) -> SanitizedBoundedRiverReviewWorkflowQualificationManifestV1:
    """Load only bounded, byte-canonical, strictly self-hashed manifest JSON."""

    try:
        size = path.stat().st_size
        if size < 1 or size > _MAX_MANIFEST_BYTES:
            _fail("BRWQ_E_MANIFEST_STORAGE")
        data = path.read_bytes()
        if len(data) != size:
            _fail("BRWQ_E_MANIFEST_STORAGE")
        return parse_canonical_model(
            data,
            SanitizedBoundedRiverReviewWorkflowQualificationManifestV1,
        )
    except (OSError, TypeError, ValueError) as exc:
        if isinstance(exc, BoundedRiverReviewWorkflowQualificationError):
            raise
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_MANIFEST_STORAGE") from exc


def write_sanitized_bounded_river_review_workflow_qualification_manifest(
    path: Path,
    manifest: SanitizedBoundedRiverReviewWorkflowQualificationManifestV1,
) -> None:
    """Exclusively write one validated manifest using the canonical JSON encoding."""

    try:
        validated = SanitizedBoundedRiverReviewWorkflowQualificationManifestV1.model_validate(
            manifest.model_dump(mode="python"),
            strict=True,
        )
        data = canonical_json_bytes(validated)
    except (TypeError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_MANIFEST_STORAGE") from exc
    if len(data) > _MAX_MANIFEST_BYTES:
        _fail("BRWQ_E_MANIFEST_STORAGE")
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except OSError as exc:
        raise BoundedRiverReviewWorkflowQualificationError("BRWQ_E_MANIFEST_STORAGE") from exc


__all__ = [
    "QUALIFICATION_BRIDGE_REPLAY_HASH_DOMAIN",
    "QUALIFICATION_CONFIRMATION_FIELDS_HASH_DOMAIN",
    "QUALIFICATION_LIMITATIONS",
    "QUALIFICATION_MANIFEST_HASH_DOMAIN",
    "QUALIFICATION_SCHEMA_VERSION",
    "BoundedRiverReviewWorkflowQualificationError",
    "SanitizedBoundedRiverReviewWorkflowConfirmationHashesV1",
    "SanitizedBoundedRiverReviewWorkflowEvaluationEvidenceV1",
    "SanitizedBoundedRiverReviewWorkflowLineageV1",
    "SanitizedBoundedRiverReviewWorkflowQualificationManifestV1",
    "SanitizedBoundedRiverReviewWorkflowRoleEvidenceV1",
    "SanitizedBoundedRiverReviewWorkflowSourceIdentityV1",
    "SanitizedBoundedRiverReviewWorkflowTerminalEvidenceV1",
    "build_sanitized_bounded_river_review_workflow_qualification_manifest",
    "load_sanitized_bounded_river_review_workflow_qualification_manifest",
    "write_sanitized_bounded_river_review_workflow_qualification_manifest",
]
