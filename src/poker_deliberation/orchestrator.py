"""Deterministic workflow owner for state, artifacts, budgets, and synthesis."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar, Literal, NoReturn, cast

from poker_deliberation import __version__
from poker_deliberation.agents import select_roles
from poker_deliberation.approval_canonical import (
    approval_actor_sha256,
    approval_decision_batch_sha256,
    historical_approval_v1_binding_sha256,
)
from poker_deliberation.approval_models import (
    ApprovalDecisionBatch,
    ApprovalDecisionFailureV2,
    ApprovalDecisionItemV2,
    ApprovalDecisionOutcome,
    ApprovalFailureCode,
    ApprovalLedgerV2,
    DecisionValue,
    HistoricalApprovalV1Binding,
)
from poker_deliberation.approvals import (
    SENSITIVE_ACTIONS,
    ApprovalDecisionValidationError,
    ApprovalLedger,
    DecisionAuthorityProvider,
    LocalCliAuthorityProvider,
    add_approval_request_v2,
    approval_failure_v2,
    approval_reference_sha256,
    approval_transaction_id,
    build_approval_decision_update,
    build_approval_request_v2,
    empty_approval_ledger_v2,
    encode_approval_state_v2,
    project_v1_approvals,
    read_approval_state_v2,
    reverify_approval_authority,
    validate_approval_decision,
)
from poker_deliberation.budgets import (
    BudgetLimitError,
    BudgetPolicyV2,
    CancellationStatus,
    ExecutionClass,
    MonotonicClock,
    SystemMonotonicClock,
    V1BudgetMigrationResult,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetStore,
    initialize_durable_budget_root,
)
from poker_deliberation.config import AppConfig, migrate_budget_config
from poker_deliberation.context_lifecycle import (
    new_attempt_id,
    new_context_id,
)
from poker_deliberation.isolation import IsolationError, build_blind_decision_context
from poker_deliberation.phases import (
    AdjudicationService,
    AnalysisExecutor,
    ContextBuildService,
    CritiqueService,
    IntakeValidationService,
    NormalizationService,
    PhaseContractError,
    PhaseId,
    RoutingService,
    SynthesisService,
    ToolResearchExecutor,
    canonical_sha256,
    make_phase_request,
    revalidate_outcome,
    validate_analysis_output,
    validate_tool_research_output,
)
from poker_deliberation.phases.models import (
    AdjudicationInput,
    AdjudicationOutput,
    AnalysisInput,
    AnalysisOutput,
    ApprovalProposalV2,
    ContextBuildInput,
    ContextBuildOutput,
    CritiqueInput,
    CritiqueOutput,
    IntakeValidationInput,
    IntakeValidationOutput,
    NormalizationInput,
    NormalizationOutput,
    ProviderSnapshot,
    RoutingInput,
    RoutingOutput,
    SynthesisInput,
    SynthesisOutput,
    ToolResearchInput,
    ToolResearchOutput,
)
from poker_deliberation.phases.revision_coordinator import (
    TRANSITION_REASON as _TRANSITION_REASON,
)
from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionBundleV1 as _PhaseRevisionBundleV1,
)
from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionCoordinator as _PhaseRevisionCoordinator,
)
from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionFailureCode as _PhaseRevisionFailureCode,
)
from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionFailureV1 as _PhaseRevisionFailureV1,
)
from poker_deliberation.phases.revision_coordinator import (
    PhaseRevisionTraceV1 as _PhaseRevisionTraceV1,
)
from poker_deliberation.phases.revision_coordinator import (
    PhaseTransitionApplyResultV1 as _PhaseTransitionApplyResultV1,
)
from poker_deliberation.phases.revision_coordinator import (
    PhaseTransitionAuthorizationV1 as _PhaseTransitionAuthorizationV1,
)
from poker_deliberation.phases.revision_coordinator import (
    PhaseTransitionPlanV1 as _PhaseTransitionPlanV1,
)
from poker_deliberation.phases.revision_coordinator import (
    _domain_digest,
    _failure,
    _is_issued_plan,
    _issue_transition_plan,
)
from poker_deliberation.providers import AgentProvider, LocalProvider
from poker_deliberation.reporting import render_markdown
from poker_deliberation.research import EvidenceLedger
from poker_deliberation.schemas import (
    AgentExecutionRecord,
    AgentReport,
    ApprovalRequest,
    ApprovalStatus,
    CaseInput,
    Claim,
    ConfidenceGrade,
    Dispute,
    EvidenceRecord,
    FinalReport,
    SecurityEvent,
    ToolRequest,
    ToolResult,
)
from poker_deliberation.security import redact_sensitive, screen_case
from poker_deliberation.state_machine import RunState, StateEvent, WorkflowStateMachine
from poker_deliberation.storage.legacy_migration import (
    LegacyRunAdapter,
    legacy_copy_payloads,
    legacy_failure,
    legacy_source_binding,
    same_legacy_snapshot,
)
from poker_deliberation.storage.lifecycle_hooks import build_terminal_lifecycle_audit
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_domain_sha256,
    run_id_sha256,
    sha256_bytes,
    validate_run_id,
)
from poker_deliberation.storage.revision_canonical import (
    canonical_json_bytes as canonical_storage_json_bytes,
)
from poker_deliberation.storage.revision_models import RunStorageError
from poker_deliberation.storage.revision_store import inspect_root_initialization
from poker_deliberation.storage.run_store import BufferedRunStore
from poker_deliberation.storage.terminal_canonical import (
    empty_lineage_head_sha256,
    inventory_entry,
    product_payload_commitments,
)
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    ProductRunFailureV2,
    RunReadStatus,
    ToolContractVersionV2,
    VerifiedPayloadV2,
    VerifiedRunReadV2,
)
from poker_deliberation.storage.terminal_store import (
    ApprovalFailureAuditError,
    ApprovalFailureAuditRequest,
    DurableBudgetCoordinator,
    TerminalPublishRequest,
    TerminalRunStore,
    provisional_budget_binding,
)
from poker_deliberation.tools import ToolRegistry, default_registry


def new_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-{stamp}-{secrets.token_hex(4)}"


def _new_phase_attempt_id(phase_id: PhaseId) -> str:
    return f"phase-{phase_id.value}-{secrets.token_hex(8)}"


def _new_internal_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(12)}"


def _append_observed_budget_failure(
    target: list[str],
    machine: WorkflowStateMachine,
) -> None:
    failure = machine.last_budget_failure
    if failure is None:
        return
    message = f"strict budget failure: {failure.code.value}"
    if message not in target:
        target.append(message)


class Orchestrator:
    _APPROVAL_V2_SCHEMAS: ClassVar[dict[str, tuple[str, str, str]]] = {
        "approval_ledger_v2.json": (
            "application/json",
            "poker-run-storage-json-v1",
            "poker-approval-ledger-artifact-v2",
        ),
        "approval_decisions_v2.jsonl": (
            "application/x-ndjson",
            "poker-run-storage-jsonl-v1",
            "poker-approval-decision-log-artifact-v2",
        ),
        "approval_audit_v2.jsonl": (
            "application/x-ndjson",
            "poker-run-storage-jsonl-v1",
            "poker-approval-domain-audit-log-artifact-v2",
        ),
    }

    def __init__(
        self,
        config: AppConfig | None = None,
        registry: ToolRegistry | None = None,
        provider: AgentProvider | None = None,
        context_clock: Callable[[], datetime] | None = None,
        *,
        monotonic_clock: MonotonicClock | None = None,
        budget_policy: BudgetPolicyV2 | None = None,
        intake_service: IntakeValidationService | None = None,
        normalization_service: NormalizationService | None = None,
        routing_service: RoutingService | None = None,
        context_build_service: ContextBuildService | None = None,
        analysis_executor: AnalysisExecutor | None = None,
        tool_research_executor: ToolResearchExecutor | None = None,
        critique_service: CritiqueService | None = None,
        adjudication_service: AdjudicationService | None = None,
        synthesis_service: SynthesisService | None = None,
        product_store: TerminalRunStore | None = None,
        budget_store: DurableBudgetStore | None = None,
        terminal_clock: Callable[[], datetime] | None = None,
        terminal_id_factory: Callable[[str], str] | None = None,
        decision_authority_provider: DecisionAuthorityProvider | None = None,
    ) -> None:
        self.config = config or AppConfig.from_env()
        self.budget_migration: V1BudgetMigrationResult | None
        if budget_policy is None:
            self.budget_migration = migrate_budget_config(self.config.budgets)
            self.budget_policy = self.budget_migration.policy
        else:
            self.budget_migration = None
            self.budget_policy = BudgetPolicyV2.model_validate(
                budget_policy.model_dump(mode="python")
            )
        injected_registry = (
            registry
            if registry is not None
            else (tool_research_executor.registry if tool_research_executor is not None else None)
        )
        injected_clocks = [
            clock
            for clock in (
                analysis_executor.monotonic_clock if analysis_executor is not None else None,
                injected_registry.monotonic_clock if injected_registry is not None else None,
            )
            if clock is not None
        ]
        if monotonic_clock is None and injected_clocks:
            self.monotonic_clock = injected_clocks[0]
        else:
            self.monotonic_clock = monotonic_clock or SystemMonotonicClock()
        if any(clock is not self.monotonic_clock for clock in injected_clocks):
            raise ValueError("injected effect clocks must match orchestrator monotonic clock")
        if registry is None and tool_research_executor is not None:
            self.registry = tool_research_executor.registry
        else:
            self.registry = registry or default_registry(
                max_payload_bytes=self.budget_policy.max_tool_input_bytes,
                max_output_bytes=self.budget_policy.max_tool_output_bytes,
                max_duration_seconds=min(30.0, self.budget_policy.max_runtime_seconds),
                monotonic_clock=self.monotonic_clock,
            )
        if provider is None and analysis_executor is not None:
            self.provider = analysis_executor.provider
        else:
            self.provider = provider or LocalProvider()
        self.context_clock = context_clock or (lambda: datetime.now(UTC))
        self.sensitive_action_categories = tuple(sorted(SENSITIVE_ACTIONS))
        self.tool_contract_versions = {
            str(description["name"]): str(description["contract_version"])
            for description in self.registry.describe()
            if description.get("name") is not None
            and description.get("contract_version") is not None
        }
        self.intake_service = intake_service or IntakeValidationService()
        self.normalization_service = normalization_service or NormalizationService()
        self.routing_service = routing_service or RoutingService()
        self.context_build_service = context_build_service or ContextBuildService(
            blind_context_builder=build_blind_decision_context
        )
        if analysis_executor is not None and analysis_executor.provider is not self.provider:
            raise ValueError("analysis executor provider must match orchestrator provider")
        if (
            analysis_executor is not None
            and analysis_executor.monotonic_clock is not self.monotonic_clock
        ):
            raise ValueError("analysis executor clock must match orchestrator monotonic clock")
        self.analysis_executor = analysis_executor or AnalysisExecutor(
            self.provider,
            context_clock=self.context_clock,
            record_clock=lambda: datetime.now(UTC),
            monotonic_clock=self.monotonic_clock,
        )
        if tool_research_executor is not None and (
            tool_research_executor.registry is not self.registry
        ):
            raise ValueError("tool research executor registry must match orchestrator registry")
        if (
            tool_research_executor is not None
            and tool_research_executor.record_sensitive_data != self.config.record_sensitive_data
        ):
            raise ValueError(
                "tool research executor redaction policy must match orchestrator config"
            )
        if self.registry.monotonic_clock is not self.monotonic_clock:
            raise ValueError("tool registry clock must match orchestrator monotonic clock")
        self.tool_research_executor = tool_research_executor or ToolResearchExecutor(
            self.registry,
            record_sensitive_data=self.config.record_sensitive_data,
        )
        self.critique_service = critique_service or CritiqueService()
        self.adjudication_service = adjudication_service or AdjudicationService()
        self.synthesis_service = synthesis_service or SynthesisService()
        self.phase_policy_snapshot_hash = canonical_sha256(
            {
                "record_sensitive_data": self.config.record_sensitive_data,
                "registered_tools": self.registry.names(),
                "tool_contract_versions": self.tool_contract_versions,
                "sensitive_action_categories": self.sensitive_action_categories,
                "context_retention_policy": "attempt-memory-only-v1",
                "execution": "serial",
                "budget_schema_version": self.budget_policy.schema_version,
                "budget_policy_sha256": self.budget_policy.canonical_sha256,
            }
        )
        self._run_machines: dict[str, WorkflowStateMachine] = {}
        self.terminal_clock = terminal_clock or (lambda: datetime.now(UTC))
        self.terminal_id_factory = terminal_id_factory or (
            lambda prefix: f"{prefix}-{secrets.token_hex(16)}"
        )
        self.decision_authority_provider = decision_authority_provider or LocalCliAuthorityProvider(
            "local-cli-user"
        )
        legacy_root, revision_root, budget_root = self.config.resolved_storage_roots()
        self.config._validate_nonoverlapping_roots((legacy_root, revision_root, budget_root))
        legacy_root.mkdir(parents=True, exist_ok=True)
        self.legacy_runs_root = legacy_root
        self.revision_runs_root = revision_root
        self.durable_budget_runs_root = budget_root
        self.legacy_adapter = LegacyRunAdapter(
            legacy_root,
            max_artifact_bytes=self.budget_policy.max_artifact_bytes,
            max_run_bytes=self.budget_policy.max_run_bytes,
        )
        if budget_store is not None and (
            budget_store.revisions.revision_root != budget_root
            or budget_store.revisions.legacy_runs_root != legacy_root
        ):
            raise ValueError("durable budget store roots must match AppConfig")
        self.durable_budget_store = budget_store or DurableBudgetStore(
            budget_root,
            legacy_root,
            wall_clock=self.terminal_clock,
        )
        self.durable_budget = DurableBudgetCoordinator(
            self.durable_budget_store,
            self.budget_policy,
        )
        if product_store is not None and (
            product_store.revision_root != revision_root
            or product_store.legacy_runs_root != legacy_root
        ):
            raise ValueError("product store roots must match AppConfig")
        self.product_store = product_store or TerminalRunStore(
            revision_root,
            legacy_root,
            budget=self.durable_budget,
            max_artifact_bytes=self.budget_policy.max_artifact_bytes,
            max_run_bytes=self.budget_policy.max_run_bytes,
            clock=self.terminal_clock,
            id_factory=self.terminal_id_factory,
            framework_version=__version__,
            source_commit_id="0" * 64,
        )
        self.store = BufferedRunStore(
            revision_root / "buffer",
            max_artifact_bytes=self.budget_policy.max_artifact_bytes,
            max_run_bytes=self.budget_policy.max_run_bytes,
            usage_observer=self._observe_storage_usage,
        )
        self._product_storage_initialized = False
        self._publication_plans: dict[str, tuple[int, str]] = {}
        self._approval_v2_payloads: dict[str, dict[str, bytes]] = {}

    def _observe_storage_usage(self, run_id: str, artifact_bytes: int, run_bytes: int) -> None:
        machine = self._run_machines.get(run_id)
        if machine is not None and not machine.ledger.observation_failed:
            machine.ledger.observe_storage(
                artifact_bytes=artifact_bytes,
                run_bytes=run_bytes,
            )

    @staticmethod
    def _product_error(
        run_id: str,
        code: ProductRunFailureCode,
        *,
        stage: str,
        read_status: RunReadStatus | None = None,
    ) -> ProductRunError:
        try:
            hashed_run_id = run_id_sha256(run_id)
        except ValueError:
            hashed_run_id = canonical_domain_sha256(
                "poker-invalid-product-run-id-v2",
                {"run_id": run_id},
            )
        return ProductRunError(
            ProductRunFailureV2(
                code=code,
                stage=stage,
                read_status=read_status,
                message_code=code.value,
                retryable=code is ProductRunFailureCode.RUN_LOCKED,
                reconciliation_required=read_status
                in {
                    RunReadStatus.INCOMPLETE,
                    RunReadStatus.CORRUPT,
                },
                filesystem_effect="none",
                domain_effect="not_started",
                previous_revision_effect="not_applicable",
                run_id_sha256=hashed_run_id,
            )
        )

    def _initialize_product_storage(self, run_id: str) -> None:
        if self._product_storage_initialized:
            return
        if not self.legacy_runs_root.exists():
            self.legacy_runs_root.mkdir(parents=True)
        budget_root_digest = canonical_domain_sha256(
            "poker-product-budget-root-id-v1",
            {
                "budget_root": str(self.durable_budget_runs_root),
                "legacy_root": str(self.legacy_runs_root),
            },
        )
        try:
            budget_inspection = inspect_root_initialization(
                self.durable_budget_runs_root,
                self.legacy_runs_root,
                max_artifact_bytes=self.budget_policy.max_artifact_bytes,
            )
            if budget_inspection.status == "uninitialized":
                initialize_durable_budget_root(
                    self.durable_budget_runs_root,
                    self.legacy_runs_root,
                    root_id=f"root-{budget_root_digest[:32]}",
                    initialized_at=self.terminal_clock(),
                )
            elif budget_inspection.status != "initialized":
                raise self._product_error(
                    run_id,
                    (
                        ProductRunFailureCode.RUN_INCOMPLETE
                        if budget_inspection.status == "incomplete"
                        else ProductRunFailureCode.RUN_CORRUPT
                    ),
                    stage="budget_root_preflight",
                    read_status=(
                        RunReadStatus.INCOMPLETE
                        if budget_inspection.status == "incomplete"
                        else RunReadStatus.CORRUPT
                    ),
                )
            self.durable_budget_store.revisions._ownership(run_id)

            product_inspection = inspect_root_initialization(
                self.revision_runs_root,
                self.legacy_runs_root,
                max_artifact_bytes=self.budget_policy.max_artifact_bytes,
            )
            if product_inspection.status == "uninitialized":
                self.product_store.initialize(initialized_at=self.terminal_clock())
            elif product_inspection.status != "initialized":
                raise self._product_error(
                    run_id,
                    (
                        ProductRunFailureCode.RUN_INCOMPLETE
                        if product_inspection.status == "incomplete"
                        else ProductRunFailureCode.RUN_CORRUPT
                    ),
                    stage="product_root_preflight",
                    read_status=(
                        RunReadStatus.INCOMPLETE
                        if product_inspection.status == "incomplete"
                        else RunReadStatus.CORRUPT
                    ),
                )
            self.product_store.foundation._ownership(run_id)
        except ProductRunError:
            raise
        except (CanonicalStorageError, RunStorageError, OSError) as exc:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.RUN_CORRUPT,
                stage="product_root_verification",
                read_status=RunReadStatus.CORRUPT,
            ) from exc
        self._product_storage_initialized = True

    def _namespace_kind(self, run_id: str) -> str | None:
        try:
            validate_run_id(run_id)
        except CanonicalStorageError as exc:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.PATH_CONFINEMENT_FAILED,
                stage="namespace",
            ) from exc
        expected = run_id.lower()
        matches: dict[str, list[str]] = {"product": [], "legacy": []}
        for kind, root in (
            ("product", self.product_store.runs_root),
            ("legacy", self.legacy_runs_root),
        ):
            if not root.exists():
                continue
            for entry in root.iterdir():
                if entry.name.lower() == expected:
                    matches[kind].append(entry.name)
        all_matches = matches["product"] + matches["legacy"]
        if (
            len(all_matches) > 1
            or any(name != run_id for name in all_matches)
            or (matches["product"] and matches["legacy"])
        ):
            raise self._product_error(
                run_id,
                ProductRunFailureCode.CROSS_RUN_MISMATCH,
                stage="namespace",
            )
        if matches["product"]:
            return "product"
        if matches["legacy"]:
            return "legacy"
        return None

    def _tool_versions(self) -> tuple[ToolContractVersionV2, ...]:
        values = []
        for description in self.registry.describe():
            name = description.get("name")
            contract_version = description.get("contract_version")
            if name is None or contract_version is None:
                continue
            values.append(
                ToolContractVersionV2(
                    tool_name=str(name),
                    tool_version=str(description.get("version") or contract_version),
                    contract_version=str(contract_version),
                )
            )
        return tuple(sorted(values, key=lambda item: item.tool_name.encode("utf-8")))

    def _terminal_payloads(
        self,
        run_id: str,
        *,
        terminal: bool,
        revision: int,
        published_at: datetime,
    ) -> tuple[tuple[VerifiedPayloadV2, ...], str | None]:
        payloads = self.store.verified_payloads(run_id)
        approval_payloads = tuple(
            VerifiedPayloadV2(
                inventory=inventory_entry(
                    logical_name=logical_name,
                    data=data,
                    media_type=self._APPROVAL_V2_SCHEMAS[logical_name][0],
                    serialization=self._APPROVAL_V2_SCHEMAS[logical_name][1],
                    artifact_schema_version=self._APPROVAL_V2_SCHEMAS[logical_name][2],
                ),
                exact_bytes=data,
            )
            for logical_name, data in self._approval_v2_payloads.get(run_id, {}).items()
        )
        payloads = tuple(
            sorted(
                (*payloads, *approval_payloads),
                key=lambda item: item.inventory.revision_relative_path.encode("utf-8"),
            )
        )
        if not terminal:
            return payloads, None
        lifecycle = build_terminal_lifecycle_audit(
            run_id=run_id,
            revision=revision,
            published_at=published_at,
            inventory=tuple(
                item.inventory
                for item in payloads
                if item.inventory.logical_name not in self._APPROVAL_V2_SCHEMAS
            ),
        )
        lifecycle_payload = VerifiedPayloadV2(
            inventory=inventory_entry(
                logical_name="lifecycle_audit.json",
                data=lifecycle.canonical_bytes,
                media_type="application/json",
                artifact_schema_version="poker-lifecycle-audit-artifact-v1",
                serialization="poker-run-storage-json-v1",
            ),
            exact_bytes=lifecycle.canonical_bytes,
        )
        all_payloads = tuple(
            sorted(
                (*payloads, lifecycle_payload),
                key=lambda item: item.inventory.revision_relative_path.encode("utf-8"),
            )
        )
        return all_payloads, lifecycle.sha256

    def _load_verified_buffer(self, read: VerifiedRunReadV2) -> None:
        """Keep additive V2 artifacts beside the unchanged V1 buffer contract."""

        approval_payloads = {
            payload.inventory.logical_name: payload.exact_bytes
            for payload in read.payloads
            if payload.inventory.logical_name in self._APPROVAL_V2_SCHEMAS
        }
        if approval_payloads:
            if set(approval_payloads) != set(self._APPROVAL_V2_SCHEMAS):
                raise self._product_error(
                    read.run_id,
                    ProductRunFailureCode.RUN_CORRUPT,
                    stage="approval_payload_set",
                    read_status=RunReadStatus.CORRUPT,
                )
            self._approval_v2_payloads[read.run_id] = approval_payloads
        compatible = read.model_copy(
            update={
                "payloads": tuple(
                    payload
                    for payload in read.payloads
                    if payload.inventory.logical_name not in self._APPROVAL_V2_SCHEMAS
                )
            }
        )
        self.store.load_verified(compatible)

    def _publish_buffer(
        self,
        run_id: str,
        report: FinalReport,
        *,
        previous_read: VerifiedRunReadV2 | None = None,
        transaction_id_override: str | None = None,
        authority_verifier: Callable[[], None] | None = None,
    ) -> VerifiedRunReadV2:
        plan = self._publication_plans.pop(run_id, None)
        namespace = self._namespace_kind(run_id)
        if namespace == "legacy":
            raise self._product_error(
                run_id,
                ProductRunFailureCode.LEGACY_RUN_UNVERIFIED,
                stage="publish_namespace",
                read_status=RunReadStatus.LEGACY_UNVERIFIED,
            )
        previous = (
            previous_read
            if previous_read is not None
            else self.product_store.read_current(run_id)
            if namespace == "product"
            else None
        )
        if previous is not None and previous.run_id != run_id:
            raise ValueError("previous product snapshot run mismatch")
        publication_kind: Literal["product_checkpoint", "product_terminal"]
        status: Literal["approval_required", "succeeded", "failed"]
        if report.run_status == "approval_required":
            publication_kind = "product_checkpoint"
            status = "approval_required"
        elif report.run_status == "completed":
            publication_kind = "product_terminal"
            status = "succeeded"
        else:
            publication_kind = "product_terminal"
            status = "failed"
        terminal = publication_kind == "product_terminal"
        revision = 1 if previous is None else previous.revision + 1
        planned_revision = revision if plan is None else plan[0]
        if revision != planned_revision:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.RUN_CONFLICT,
                stage="publication_plan",
            )
        published_at = self.terminal_clock()
        transaction_id = (
            transaction_id_override
            if transaction_id_override is not None
            else self.terminal_id_factory("txn")
            if plan is None
            else plan[1]
        )
        payloads, lifecycle_sha = self._terminal_payloads(
            run_id,
            terminal=terminal,
            revision=revision,
            published_at=published_at,
        )
        payload_map = {payload.inventory.logical_name: payload.exact_bytes for payload in payloads}
        (
            canonical_input_sha,
            state_checkpoint_sha,
            event_head_sha,
            approval_head_sha,
            context_head_sha,
            execution_head_sha,
        ) = product_payload_commitments(
            payload_map,
            run_id=run_id,
            status=status,
            revision=revision,
        )
        created_at = published_at if previous is None else previous.manifest.created_at
        request = TerminalPublishRequest(
            run_id=run_id,
            transaction_id=transaction_id,
            publication_kind=publication_kind,
            status=status,
            proposed_revision=revision,
            expected_revision=None if previous is None else previous.revision,
            expected_manifest_sha256=(None if previous is None else previous.manifest_sha256),
            expected_pointer_sha256=(None if previous is None else previous.current_pointer_sha256),
            created_at=created_at,
            updated_at=published_at,
            published_at=published_at,
            framework_version=__version__,
            source_commit_id="0" * 64,
            tool_contract_versions=self._tool_versions(),
            canonical_input_sha256=canonical_input_sha,
            config_sha256=canonical_domain_sha256(
                "poker-product-config-v2",
                self.config.model_dump(mode="json"),
            ),
            budget_binding=provisional_budget_binding(
                run_id,
                transaction_id,
                self.budget_policy,
            ),
            redaction_policy_sha256=canonical_domain_sha256(
                "poker-product-redaction-policy-v2",
                {"record_sensitive_data": self.config.record_sensitive_data},
            ),
            state_checkpoint_sha256=state_checkpoint_sha,
            event_head_sha256=event_head_sha,
            approval_lineage_head_sha256=approval_head_sha,
            context_lineage_head_sha256=context_head_sha,
            execution_lineage_head_sha256=execution_head_sha,
            legacy_source=None,
            lifecycle_audit_sha256=lifecycle_sha,
            payloads=payloads,
        )
        frozen = self.product_store.freeze_budget_binding(request)
        if authority_verifier is None:
            self.product_store.publish(frozen)
        else:
            self.product_store.publish_approval_decision(
                frozen,
                authority_verifier=authority_verifier,
            )
        return self.product_store.read_current(run_id)

    @staticmethod
    def _revision_event_prefix(machine: WorkflowStateMachine) -> tuple[dict[str, str], ...]:
        return tuple(
            {
                "source": event.source.value,
                "target": event.target.value,
                "reason": event.reason,
            }
            for event in machine.events
        )

    def _prepare_revision_bundle(
        self,
        machine: WorkflowStateMachine,
        *,
        run_id: str,
        trace: _PhaseRevisionTraceV1,
        request: object,
    ) -> _PhaseRevisionBundleV1 | _PhaseRevisionFailureV1:
        """Purely preview one internal P2-010B revision/transition bundle."""

        from poker_deliberation.storage.revision_models import RevisionPublishRequestV1

        try:
            with machine.transition_authority():
                if machine.state is not RunState.FINAL_SYNTHESIS:
                    return _failure(_PhaseRevisionFailureCode.INVALID_PLAN)
                plan = _issue_transition_plan(
                    run_id=run_id,
                    events=self._revision_event_prefix(machine),
                    owner=machine,
                )
            if not isinstance(request, RevisionPublishRequestV1):
                return _failure(_PhaseRevisionFailureCode.INVALID_TRACE)
            return _PhaseRevisionBundleV1(trace=trace, request=request, plan=plan)
        except Exception:
            return _failure(_PhaseRevisionFailureCode.INVALID_PLAN)

    @staticmethod
    def _revision_plan_matches_machine(
        machine: WorkflowStateMachine,
        plan: _PhaseTransitionPlanV1,
    ) -> bool:
        events = tuple(
            {
                "source": event.source.value,
                "target": event.target.value,
                "reason": event.reason,
            }
            for event in machine.events[: plan.event_count]
        )
        return (
            _is_issued_plan(plan, owner=machine)
            and len(machine.events) == plan.event_count
            and machine.state is RunState.FINAL_SYNTHESIS
            and plan.source == machine.state.value
            and plan.target == RunState.COMPLETED.value
            and plan.reason == _TRANSITION_REASON
            and plan.event_prefix_sha256
            == _domain_digest("poker-phase-transition-event-prefix-v1", events)
        )

    @staticmethod
    def _revision_transition_already_applied(
        machine: WorkflowStateMachine,
        plan: _PhaseTransitionPlanV1,
    ) -> bool:
        if machine.state is not RunState.COMPLETED or len(machine.events) != plan.event_count + 1:
            return False
        prefix = tuple(
            {
                "source": event.source.value,
                "target": event.target.value,
                "reason": event.reason,
            }
            for event in machine.events[: plan.event_count]
        )
        event = machine.events[-1]
        return (
            _is_issued_plan(plan, owner=machine)
            and plan.event_prefix_sha256
            == _domain_digest("poker-phase-transition-event-prefix-v1", prefix)
            and event.source is RunState.FINAL_SYNTHESIS
            and event.target is RunState.COMPLETED
            and event.reason == _TRANSITION_REASON
        )

    def _apply_revision_transition(
        self,
        machine: WorkflowStateMachine,
        *,
        coordinator: _PhaseRevisionCoordinator,
        bundle: _PhaseRevisionBundleV1,
        authorization: _PhaseTransitionAuthorizationV1,
        fault_injector: Callable[[str], None] | None = None,
    ) -> _PhaseTransitionApplyResultV1 | _PhaseRevisionFailureV1:
        """Apply one verified same-process authorization under the state lock."""

        with machine.transition_authority():
            if self._revision_transition_already_applied(machine, bundle.plan):
                if coordinator.authorization_matches(bundle, authorization):
                    return _PhaseTransitionApplyResultV1(outcome_kind="already_applied")
                return _failure(_PhaseRevisionFailureCode.AUTHORIZATION_MISMATCH)
            if not self._revision_plan_matches_machine(
                machine, bundle.plan
            ) or not coordinator.authorization_matches(bundle, authorization):
                return _failure(_PhaseRevisionFailureCode.AUTHORIZATION_MISMATCH)
            try:
                if fault_injector is not None:
                    fault_injector("before_transition")
                machine.transition(RunState.COMPLETED, _TRANSITION_REASON)
                if fault_injector is not None:
                    fault_injector("after_transition")
                return _PhaseTransitionApplyResultV1(outcome_kind="applied")
            except Exception:
                if self._revision_transition_already_applied(machine, bundle.plan):
                    return _PhaseTransitionApplyResultV1(outcome_kind="already_applied")
                if machine.state is RunState.FINAL_SYNTHESIS:
                    return _failure(_PhaseRevisionFailureCode.APPLY_FAILED)
                return _failure(_PhaseRevisionFailureCode.APPLY_UNKNOWN)

    def run(self, case: CaseInput, *, run_id: str | None = None) -> FinalReport:
        case = CaseInput.model_validate(case.model_dump(mode="python"))
        actual_run_id = run_id or new_run_id()
        try:
            return self._run(case, actual_run_id)
        except BudgetLimitError as exc:
            machine = self._run_machines.get(actual_run_id)
            if machine is not None and machine.state is not RunState.FAILED_WITH_LIMITATIONS:
                machine.events.append(
                    StateEvent(
                        source=machine.state,
                        target=RunState.FAILED_WITH_LIMITATIONS,
                        reason=f"strict budget failure: {exc.failure.code}",
                    )
                )
                machine.state = RunState.FAILED_WITH_LIMITATIONS
            limitation = f"strict budget failure: {exc.failure.code}"
            return FinalReport(
                run_id=actual_run_id,
                run_status="failed_with_limitations",
                conclusion="The run stopped because a strict budget boundary was reached.",
                reconstructed_input=redact_sensitive(
                    case.model_dump(mode="json"),
                    enabled=not self.config.record_sensitive_data,
                ),
                data_quality=[limitation],
                limitations=[limitation],
                confidence=ConfidenceGrade.D,
            )

    def _run(self, case: CaseInput, actual_run_id: str) -> FinalReport:
        try:
            validate_run_id(actual_run_id)
        except CanonicalStorageError as exc:
            raise self._product_error(
                actual_run_id,
                ProductRunFailureCode.PATH_CONFINEMENT_FAILED,
                stage="new_run_preflight",
            ) from exc
        self._initialize_product_storage(actual_run_id)
        namespace = self._namespace_kind(actual_run_id)
        if namespace is not None:
            code = (
                ProductRunFailureCode.LEGACY_RUN_UNVERIFIED
                if namespace == "legacy"
                else ProductRunFailureCode.RUN_CONFLICT
            )
            status = RunReadStatus.LEGACY_UNVERIFIED if namespace == "legacy" else None
            raise self._product_error(
                actual_run_id,
                code,
                stage="new_run_namespace",
                read_status=status,
            )
        self.store.create_run(actual_run_id)
        machine = WorkflowStateMachine(self.budget_policy, clock=self.monotonic_clock)
        self._run_machines[actual_run_id] = machine
        approvals = ApprovalLedger()
        approval_ledger_v2: ApprovalLedgerV2 | None = None
        disputes: list[Dispute] = []
        tool_results: list[ToolResult] = []
        data_quality: list[str] = []
        execution_records: list[AgentExecutionRecord] = []
        security_events: list[SecurityEvent] = []
        evidence = EvidenceLedger()
        self.store.ensure_directory(actual_run_id, "agent_reports")
        self.store.ensure_directory(actual_run_id, "tool_results")
        raw_approvals = case.metadata.get("approval_requests", [])
        fallback_approval_ids = (
            tuple(_new_internal_id("approval") for _ in raw_approvals)
            if isinstance(raw_approvals, list)
            else ()
        )
        intake_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.INTAKE_VALIDATION,
            attempt_id=_new_phase_attempt_id(PhaseId.INTAKE_VALIDATION),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=IntakeValidationInput(
                case=case,
                record_sensitive_data=self.config.record_sensitive_data,
                sensitive_action_categories=self.sensitive_action_categories,
                fallback_approval_ids=fallback_approval_ids,
            ),
        )
        intake_outcome = revalidate_outcome(
            intake_request,
            self.intake_service.run(intake_request),
            output_type=IntakeValidationOutput,
        )
        if intake_outcome.output is None:
            raise PhaseContractError("intake validation returned no output")
        intake = intake_outcome.output
        case = intake.case
        safe_case = intake.safe_case
        data_quality.extend(intake.data_quality)
        self.store.write_json(actual_run_id, "input.json", safe_case)
        self.store.write_text(actual_run_id, "evidence.jsonl", "")
        for record in intake.accepted_evidence:
            evidence.add(record)
            self.store.append_jsonl(
                actual_run_id,
                "evidence.jsonl",
                redact_sensitive(record, enabled=not self.config.record_sensitive_data),
            )
        for proposal in intake.approval_proposals:
            if isinstance(proposal, ApprovalProposalV2):
                if approvals.all():
                    raise PhaseContractError(
                        "V1 and V2 approval proposals cannot share one checkpoint"
                    )
                if approval_ledger_v2 is None:
                    approval_ledger_v2 = empty_approval_ledger_v2(actual_run_id)
                request_v2 = build_approval_request_v2(
                    run_id=actual_run_id,
                    created_run_revision=1,
                    ledger_revision=approval_ledger_v2.ledger_revision + 1,
                    stable_proposal_id=proposal.stable_proposal_id,
                    action_plan=proposal.action_plan,
                    display=proposal.display,
                    source_phase_id=intake_request.phase_id.value,
                    source_attempt_id=intake_request.attempt_id,
                    created_at=self.terminal_clock(),
                )
                approval_ledger_v2, _, _ = add_approval_request_v2(
                    approval_ledger_v2,
                    request_v2,
                )
                continue
            if approval_ledger_v2 is not None:
                raise PhaseContractError("V1 and V2 approval proposals cannot share one checkpoint")
            approval_request = ApprovalRequest.model_validate(proposal.model_dump())
            approvals.add(
                ApprovalRequest.model_validate(
                    redact_sensitive(
                        approval_request,
                        enabled=not self.config.record_sensitive_data,
                    )
                )
            )
        if approval_ledger_v2 is not None:
            approval_bytes = encode_approval_state_v2(approval_ledger_v2, (), ())
            approval_state = read_approval_state_v2(*approval_bytes)
            for request in project_v1_approvals(approval_state):
                approvals.add(request)
            self._approval_v2_payloads[actual_run_id] = dict(
                zip(self._APPROVAL_V2_SCHEMAS, approval_bytes, strict=True)
            )

        machine.transition(RunState.NORMALIZE, "input parsed into CaseInput")
        normalization_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.NORMALIZATION,
            attempt_id=_new_phase_attempt_id(PhaseId.NORMALIZATION),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=NormalizationInput(
                safe_case=safe_case,
                assumptions=tuple(
                    assumption.model_dump(mode="json") for assumption in case.assumptions
                ),
            ),
        )
        normalization_outcome = revalidate_outcome(
            normalization_request,
            self.normalization_service.run(normalization_request),
            output_type=NormalizationOutput,
        )
        if normalization_outcome.output is None:
            raise PhaseContractError("normalization returned no output")
        normalized = normalization_outcome.output.normalized_case
        self.store.write_json(actual_run_id, "normalized_case.json", normalized)
        self.store.write_json(
            actual_run_id,
            "assumptions.json",
            redact_sensitive(case.assumptions, enabled=not self.config.record_sensitive_data),
        )

        machine.transition(RunState.DATA_VALIDATION, "canonical schema validation completed")
        security_events = screen_case(case)
        self.store.write_json(actual_run_id, "security_events.json", security_events)
        if any(event.category == "prompt_injection" for event in security_events):
            data_quality.append(
                "プロンプトインジェクションらしき文字列を無害な入力として記録しました。"
            )
        if any(event.blocked for event in security_events):
            data_quality.append(
                "事後検討専用の範囲外です。リアルタイム支援、非公開カード取得、共謀、"
                "自動プレイ、検出回避には対応しません。"
            )
            machine.transition(RunState.FAILED_WITH_LIMITATIONS, "prohibited use refused")
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                list(case.claims),
                [],
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
            )
        if case.kind == "hand":
            if case.hand is None:
                data_quality.append(
                    "自由文だけでは正確なポット・スタック・合法性を確定できません。CanonicalHandが必要です。"
                )
            else:
                if not machine.enforce_runtime():
                    data_quality.append("strict runtime refused before hand validation")
                    _append_observed_budget_failure(data_quality, machine)
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        [],
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                hand_usage, hand_observed_at_ns, hand_deadline_ns = machine.runtime_window()
                hand_request = ToolRequest(
                    request_id=_new_internal_id("tool-request"),
                    tool_name="hand_validator",
                    input=case.hand.model_dump(mode="json"),
                    contract_version=self.tool_contract_versions.get("hand_validator"),
                )
                tool_phase_request = make_phase_request(
                    run_id=actual_run_id,
                    phase_id=PhaseId.TOOL_RESEARCH,
                    attempt_id=_new_phase_attempt_id(PhaseId.TOOL_RESEARCH),
                    policy_snapshot_hash=self.phase_policy_snapshot_hash,
                    input_value=ToolResearchInput(
                        requests=(hand_request,),
                        fallback_result_ids=(_new_internal_id("tool-result"),),
                        budget_policy=self.budget_policy,
                        budget_snapshot=hand_usage,
                        budget_observed_at_ns=hand_observed_at_ns,
                        run_deadline_ns=hand_deadline_ns,
                    ),
                )
                tool_phase_outcome = revalidate_outcome(
                    tool_phase_request,
                    self.tool_research_executor.run(tool_phase_request),
                    output_type=ToolResearchOutput,
                )
                if tool_phase_outcome.output is None:
                    raise PhaseContractError("hand validation returned no output")
                validate_tool_research_output(tool_phase_request, tool_phase_outcome.output)
                try:
                    if tool_phase_outcome.output.usage_observed_at_ns is None:
                        raise PhaseContractError(
                            "budgeted hand validation returned no usage observation"
                        )
                    machine.apply_usage_at(
                        tool_phase_outcome.output.usage_delta,
                        observed_at_ns=tool_phase_outcome.output.usage_observed_at_ns,
                    )
                except BudgetLimitError as exc:
                    data_quality.append(f"strict usage settlement failed: {exc.failure.code}")
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "hand validation usage settlement failed",
                    )
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        [],
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                data_quality.extend(tool_phase_outcome.output.data_quality)
                validation = tool_phase_outcome.output.bindings[0].result
                tool_results.append(validation)
                self.store.write_json(
                    actual_run_id,
                    f"tool_results/{validation.result_id}.json",
                    validation,
                )
                self.store.write_json(
                    actual_run_id,
                    f"tool_results/{validation.result_id}.input.json",
                    validation.input,
                )
                if tool_phase_outcome.output.budget_failure is not None:
                    data_quality.append(
                        f"strict budget failure: {tool_phase_outcome.output.budget_failure.code}"
                    )
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "tool execution budget refused",
                    )
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        [],
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                if not validation.output.get("valid", False):
                    data_quality.extend(map(str, validation.output.get("errors", [])))
                data_quality.extend(map(str, validation.output.get("warnings", [])))
        if case.raw_text and case.hand is None and case.kind == "hand":
            data_quality.append("不足情報を捏造せず、自由文を未正規化入力として保存しました。")

        machine.transition(RunState.TASK_ROUTING, "roles selected by case kind")
        registered_tools = tuple(self.registry.names())
        routing_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.ROUTING,
            attempt_id=_new_phase_attempt_id(PhaseId.ROUTING),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=RoutingInput(
                case_kind=case.kind,
                role_snapshot=tuple(select_roles(case)),
                registered_tools=registered_tools,
            ),
        )
        routing_outcome = revalidate_outcome(
            routing_request,
            self.routing_service.run(routing_request),
            output_type=RoutingOutput,
        )
        if routing_outcome.output is None:
            raise PhaseContractError("routing returned no output")
        assignments = list(routing_outcome.output.assignments)
        self.store.write_json(actual_run_id, "assignments.json", assignments)
        reports: list[AgentReport] = []
        if case.kind != "calculation":
            machine.transition(RunState.INDEPENDENT_ANALYSIS, "selected roles run independently")
            if not machine.start_deliberation_round():
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "provider analysis round budget is zero",
                )
                data_quality.append("provider analysis skipped because round budget is zero")
                return self._synthesize(
                    actual_run_id,
                    case,
                    data_quality,
                    list(case.claims),
                    reports,
                    execution_records,
                    tool_results,
                    disputes,
                    evidence.all(),
                    approvals,
                    security_events,
                    completed=False,
                    machine=machine,
                )
            report_ids: set[str] = set()
            for index, assignment in enumerate(assignments):
                if not machine.enforce_runtime():
                    data_quality.append("maximum runtime reached before provider analysis")
                    _append_observed_budget_failure(data_quality, machine)
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                usage_before, budget_observed_at_ns, run_deadline_ns = machine.runtime_window()
                remaining_ns = run_deadline_ns - budget_observed_at_ns
                if remaining_ns <= 0:
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "maximum runtime reached before provider analysis",
                    )
                    data_quality.append("maximum runtime reached before provider analysis")
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                remaining_runtime = remaining_ns / 1_000_000_000
                started_at = datetime.now(UTC)
                provider_timeout = min(30.0, remaining_runtime)
                lifecycle_now = self.context_clock()
                expected_context_id = new_context_id()
                expected_attempt_id = new_attempt_id()
                context_request = make_phase_request(
                    run_id=actual_run_id,
                    phase_id=PhaseId.CONTEXT_BUILD,
                    attempt_id=_new_phase_attempt_id(PhaseId.CONTEXT_BUILD),
                    policy_snapshot_hash=self.phase_policy_snapshot_hash,
                    context_ids=(expected_context_id,),
                    input_value=ContextBuildInput(
                        case=case,
                        assignment=assignment,
                        registered_tools=registered_tools,
                        created_at=lifecycle_now,
                        expires_at=lifecycle_now + timedelta(seconds=provider_timeout),
                        context_id=expected_context_id,
                        context_attempt_id=expected_attempt_id,
                    ),
                )
                try:
                    context_outcome = revalidate_outcome(
                        context_request,
                        self.context_build_service.run(context_request),
                        output_type=ContextBuildOutput,
                    )
                except IsolationError as exc:
                    data_quality.append(f"blind decision isolation failed: {exc}")
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "blind decision isolation failed",
                    )
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                if context_outcome.output is None or len(context_outcome.output.dispatches) != 1:
                    raise PhaseContractError("context build returned an invalid dispatch batch")
                dispatch = context_outcome.output.dispatches[0]
                assignments[index] = dispatch.assignment
                self.store.write_json(actual_run_id, "assignments.json", assignments)
                if not machine.enforce_runtime():
                    data_quality.append("maximum runtime reached during context build")
                    _append_observed_budget_failure(data_quality, machine)
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                usage_before, budget_observed_at_ns, run_deadline_ns = machine.runtime_window()
                remaining_ns = run_deadline_ns - budget_observed_at_ns
                if remaining_ns <= 0:
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "maximum runtime reached during context build",
                    )
                    data_quality.append("maximum runtime reached during context build")
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                provider_info = self.provider.availability()
                if not machine.enforce_runtime():
                    data_quality.append("maximum runtime reached during provider preflight")
                    _append_observed_budget_failure(data_quality, machine)
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                usage_before, budget_observed_at_ns, run_deadline_ns = machine.runtime_window()
                remaining_ns = run_deadline_ns - budget_observed_at_ns
                if remaining_ns <= 0:
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "maximum runtime reached during provider preflight",
                    )
                    data_quality.append("maximum runtime reached during provider preflight")
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                provider_timeout = min(30.0, remaining_ns / 1_000_000_000)
                legacy_provider_contract = "execution_class" not in provider_info.model_fields_set
                effective_provider_info = (
                    provider_info.model_copy(
                        update={"execution_class": ExecutionClass.LOCAL_FREE},
                        deep=True,
                    )
                    if legacy_provider_contract
                    else provider_info
                )
                analysis_request = make_phase_request(
                    run_id=actual_run_id,
                    phase_id=PhaseId.ANALYSIS,
                    attempt_id=_new_phase_attempt_id(PhaseId.ANALYSIS),
                    policy_snapshot_hash=self.phase_policy_snapshot_hash,
                    context_ids=(expected_context_id,),
                    input_value=AnalysisInput(
                        dispatch=dispatch,
                        provider_timeout_seconds=provider_timeout,
                        registered_tools=registered_tools,
                        max_output_bytes=self.budget_policy.max_provider_output_bytes,
                        record_sensitive_data=self.config.record_sensitive_data,
                        started_at=started_at,
                        execution_id=_new_internal_id("execution"),
                        fallback_report_id=_new_internal_id("report"),
                        existing_report_ids=tuple(sorted(report_ids)),
                        provider_availability=effective_provider_info,
                        legacy_provider_contract=legacy_provider_contract,
                        budget_policy=self.budget_policy,
                        budget_snapshot=usage_before,
                        budget_observed_at_ns=budget_observed_at_ns,
                        run_deadline_ns=run_deadline_ns,
                    ),
                )
                analysis_outcome = revalidate_outcome(
                    analysis_request,
                    self.analysis_executor.run(analysis_request),
                    output_type=AnalysisOutput,
                )
                if analysis_outcome.output is None:
                    raise PhaseContractError("analysis returned no output")
                validate_analysis_output(analysis_request, analysis_outcome.output)
                analysis = analysis_outcome.output
                execution_records.append(analysis.execution_record)
                data_quality.extend(analysis.data_quality)
                try:
                    machine.apply_usage_at(
                        analysis.usage_delta,
                        observed_at_ns=analysis.usage_observed_at_ns,
                    )
                except BudgetLimitError as exc:
                    data_quality.append(f"strict usage settlement failed: {exc.failure.code}")
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "provider usage settlement failed",
                    )
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                if analysis.budget_failure is not None:
                    data_quality.append(f"strict budget failure: {analysis.budget_failure.code}")
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "provider execution budget refused",
                    )
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                if analysis.cancellation_status is CancellationStatus.CANCEL_UNCONFIRMED:
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "provider cancellation was not confirmed",
                    )
                    data_quality.append("provider cancellation was not confirmed")
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                if analysis.timed_out:
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "provider deadline exceeded",
                    )
                    return self._synthesize(
                        actual_run_id,
                        case,
                        data_quality,
                        list(case.claims),
                        reports,
                        execution_records,
                        tool_results,
                        disputes,
                        evidence.all(),
                        approvals,
                        security_events,
                        completed=False,
                        machine=machine,
                    )
                reports.append(analysis.report)
                report_ids.add(analysis.report.report_id)
                self.store.write_json(
                    actual_run_id,
                    f"agent_reports/{analysis.report.report_id}.json",
                    analysis.report,
                )
                objection_request = make_phase_request(
                    run_id=actual_run_id,
                    phase_id=PhaseId.CRITIQUE,
                    attempt_id=_new_phase_attempt_id(PhaseId.CRITIQUE),
                    policy_snapshot_hash=self.phase_policy_snapshot_hash,
                    input_value=CritiqueInput(
                        case=case,
                        reports=(analysis.report,),
                        tool_results=(),
                        evidence_ids=(),
                        existing_disputes=tuple(disputes),
                        include_objections=True,
                        include_provider_claims=False,
                        include_auxiliary_findings=False,
                    ),
                )
                objection_outcome = revalidate_outcome(
                    objection_request,
                    self.critique_service.run(objection_request),
                    output_type=CritiqueOutput,
                )
                if objection_outcome.output is None:
                    raise PhaseContractError("objection critique returned no output")
                disputes = list(objection_outcome.output.disputes)
                data_quality.extend(objection_outcome.output.data_quality)
            if not machine.enforce_runtime():
                data_quality.append("maximum runtime exceeded after provider analysis")
                _append_observed_budget_failure(data_quality, machine)
                return self._synthesize(
                    actual_run_id,
                    case,
                    data_quality,
                    list(case.claims),
                    reports,
                    execution_records,
                    tool_results,
                    disputes,
                    evidence.all(),
                    approvals,
                    security_events,
                    completed=False,
                    machine=machine,
                )
            machine.transition(RunState.TOOL_AND_RESEARCH, "independent reports collected")
        else:
            machine.transition(
                RunState.TOOL_AND_RESEARCH, "calculation case routes directly to tools"
            )
        tool_inputs = case.metadata.get("tool_inputs", {})
        if not isinstance(tool_inputs, dict):
            data_quality.append(
                "metadata.tool_inputs must be an object; requested tools were not run"
            )
            tool_inputs = {}
        already_run = {result.tool_name for result in tool_results}
        requested_tool_calls: list[ToolRequest] = []
        for tool_name in case.requested_tools:
            if tool_name in already_run and tool_name == "hand_validator":
                continue
            payload = tool_inputs.get(tool_name, {})
            if not isinstance(payload, dict):
                payload = {}
            requested_tool_calls.append(
                ToolRequest(
                    request_id=_new_internal_id("tool-request"),
                    tool_name=tool_name,
                    input=payload,
                    contract_version=self.tool_contract_versions.get(tool_name),
                )
            )
        if not machine.enforce_runtime():
            data_quality.append("strict runtime refused before requested tool execution")
            _append_observed_budget_failure(data_quality, machine)
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                list(case.claims),
                reports,
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
            )
        tool_usage, tool_observed_at_ns, tool_deadline_ns = machine.runtime_window()
        requested_tools_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.TOOL_RESEARCH,
            attempt_id=_new_phase_attempt_id(PhaseId.TOOL_RESEARCH),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=ToolResearchInput(
                requests=tuple(requested_tool_calls),
                start_ordinal=len(tool_results),
                existing_result_ids=tuple(result.result_id for result in tool_results),
                fallback_result_ids=tuple(
                    _new_internal_id("tool-result") for _ in requested_tool_calls
                ),
                budget_policy=self.budget_policy,
                budget_snapshot=tool_usage,
                budget_observed_at_ns=tool_observed_at_ns,
                run_deadline_ns=tool_deadline_ns,
            ),
        )
        requested_tools_outcome = revalidate_outcome(
            requested_tools_request,
            self.tool_research_executor.run(requested_tools_request),
            output_type=ToolResearchOutput,
        )
        if requested_tools_outcome.output is None:
            raise PhaseContractError("tool research returned no output")
        validate_tool_research_output(requested_tools_request, requested_tools_outcome.output)
        try:
            if requested_tools_outcome.output.usage_observed_at_ns is None:
                raise PhaseContractError("budgeted tool research returned no usage observation")
            machine.apply_usage_at(
                requested_tools_outcome.output.usage_delta,
                observed_at_ns=requested_tools_outcome.output.usage_observed_at_ns,
            )
        except BudgetLimitError as exc:
            data_quality.append(f"strict usage settlement failed: {exc.failure.code}")
            machine.transition(
                RunState.FAILED_WITH_LIMITATIONS,
                "tool usage settlement failed",
            )
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                list(case.claims),
                reports,
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
            )
        data_quality.extend(requested_tools_outcome.output.data_quality)
        tool_results.extend(binding.result for binding in requested_tools_outcome.output.bindings)
        for result in tool_results:
            self.store.write_json(actual_run_id, f"tool_results/{result.result_id}.json", result)
            self.store.write_json(
                actual_run_id, f"tool_results/{result.result_id}.input.json", result.input
            )
        if requested_tools_outcome.output.budget_failure is not None:
            data_quality.append(
                f"strict budget failure: {requested_tools_outcome.output.budget_failure.code}"
            )
            machine.transition(
                RunState.FAILED_WITH_LIMITATIONS,
                "tool execution budget refused",
            )
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                list(case.claims),
                reports,
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
            )
        if not machine.enforce_runtime():
            data_quality.append("maximum runtime exceeded after tool execution")
            _append_observed_budget_failure(data_quality, machine)
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                list(case.claims),
                reports,
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
            )

        machine.transition(RunState.CRITIQUE, "tool failures and unsupported claims checked")
        critique_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.CRITIQUE,
            attempt_id=_new_phase_attempt_id(PhaseId.CRITIQUE),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=CritiqueInput(
                case=case,
                reports=tuple(reports),
                tool_results=tuple(tool_results),
                evidence_ids=tuple(record.evidence_id for record in evidence.all()),
                existing_disputes=tuple(disputes),
                include_objections=False,
                include_provider_claims=True,
                include_auxiliary_findings=True,
            ),
        )
        critique_outcome = revalidate_outcome(
            critique_request,
            self.critique_service.run(critique_request),
            output_type=CritiqueOutput,
        )
        if critique_outcome.output is None:
            raise PhaseContractError("critique returned no output")
        disputes = list(critique_outcome.output.disputes)
        data_quality.extend(critique_outcome.output.data_quality)

        machine.transition(RunState.ADJUDICATION, "evidence strength, not vote count, used")
        adjudication_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.ADJUDICATION,
            attempt_id=_new_phase_attempt_id(PhaseId.ADJUDICATION),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=AdjudicationInput(
                case=case,
                tool_results=tuple(tool_results),
            ),
        )
        adjudication_outcome = revalidate_outcome(
            adjudication_request,
            self.adjudication_service.run(adjudication_request),
            output_type=AdjudicationOutput,
        )
        if adjudication_outcome.output is None:
            raise PhaseContractError("adjudication returned no output")
        claim_assessments = list(adjudication_outcome.output.claim_assessments)
        data_quality.extend(adjudication_outcome.output.data_quality)
        known_evidence_ids = {record.evidence_id for record in evidence.all()}
        for claim in case.claims:
            missing_evidence = set(claim.evidence_ids) - known_evidence_ids
            if missing_evidence:
                data_quality.append(
                    f"{claim.claim_id}: unknown evidence IDs: {sorted(missing_evidence)}"
                )

        if approvals.pending():
            machine.transition(RunState.HUMAN_REVIEW_REQUIRED, "sensitive action needs approval")
            return self._synthesize(
                actual_run_id,
                case,
                data_quality,
                claim_assessments,
                reports,
                execution_records,
                tool_results,
                disputes,
                evidence.all(),
                approvals,
                security_events,
                completed=False,
                machine=machine,
                pause_before_return=True,
            )
        machine.transition(RunState.FINAL_SYNTHESIS, "no pending approval blocks synthesis")
        final_report = self._synthesize(
            actual_run_id,
            case,
            data_quality,
            claim_assessments,
            reports,
            execution_records,
            tool_results,
            disputes,
            evidence.all(),
            approvals,
            security_events,
            completed=True,
            machine=machine,
        )
        return final_report

    def _write_common_artifacts(
        self,
        run_id: str,
        machine: WorkflowStateMachine,
        approvals: ApprovalLedger,
        disputes: list[Dispute],
    ) -> None:
        self.store.write_json(run_id, "state.json", machine.snapshot())
        redacted_approvals = redact_sensitive(
            approvals.all(),
            enabled=not self.config.record_sensitive_data,
        )
        self.store.write_json(
            run_id,
            "approvals.json",
            [ApprovalRequest.model_validate(item) for item in redacted_approvals],
        )
        self.store.write_json(run_id, "disputes.json", disputes)

    def _synthesize(
        self,
        run_id: str,
        case: CaseInput,
        data_quality: list[str],
        claim_assessments: list[Claim],
        reports: list[AgentReport],
        execution_records: list[AgentExecutionRecord],
        tool_results: list[ToolResult],
        disputes: list[Dispute],
        evidence_records: list[EvidenceRecord],
        approvals: ApprovalLedger,
        security_events: list[SecurityEvent],
        *,
        completed: bool,
        machine: WorkflowStateMachine,
        pause_before_return: bool = False,
    ) -> FinalReport:
        namespace = self._namespace_kind(run_id)
        previous = self.product_store.read_current(run_id) if namespace == "product" else None
        planned_revision = 1 if previous is None else previous.revision + 1
        transaction_id = self.terminal_id_factory("txn")
        provider_info = self.provider.availability()
        provider_reason = provider_info.reason
        synthesis_request = make_phase_request(
            run_id=run_id,
            phase_id=PhaseId.SYNTHESIS,
            attempt_id=_new_phase_attempt_id(PhaseId.SYNTHESIS),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=SynthesisInput(
                run_id=run_id,
                machine_state=machine.state.value,
                completed=completed,
                case=case,
                data_quality=tuple(data_quality),
                claim_assessments=tuple(claim_assessments),
                reports=tuple(reports),
                execution_records=tuple(execution_records),
                tool_results=tuple(tool_results),
                disputes=tuple(disputes),
                evidence_records=tuple(evidence_records),
                approvals=tuple(ApprovalRequest.model_validate(item) for item in approvals.all()),
                security_events=tuple(security_events),
                provider_snapshot=ProviderSnapshot(
                    available=provider_info.available,
                    reason=provider_reason,
                ),
                tool_input_artifact_paths=tuple(
                    str(
                        self.product_store.planned_payload_path(
                            run_id,
                            revision=planned_revision,
                            transaction_id=transaction_id,
                            logical_name=(f"tool_results/{result.result_id}.input.json"),
                        )
                    )
                    for result in tool_results
                ),
                record_sensitive_data=self.config.record_sensitive_data,
                generated_at=datetime.now(UTC),
            ),
        )
        synthesis_outcome = revalidate_outcome(
            synthesis_request,
            self.synthesis_service.run(synthesis_request),
            output_type=SynthesisOutput,
        )
        if synthesis_outcome.output is None:
            raise PhaseContractError("synthesis returned no output")
        expected_intents = (
            ("agent_execution_records", "agent_execution_records.json", "application/json"),
            ("security_events", "security_events.json", "application/json"),
            ("state", "state.json", "application/json"),
            ("approvals", "approvals.json", "application/json"),
            ("disputes", "disputes.json", "application/json"),
            ("final_report_json", "final_report.json", "application/json"),
            ("final_report_markdown", "final_report.md", "text/markdown"),
        )
        actual_intents = tuple(
            (intent.kind.value, intent.relative_path, intent.media_type)
            for intent in synthesis_outcome.artifact_intents
        )
        if actual_intents != expected_intents:
            raise PhaseContractError("synthesis artifact intent allowlist mismatch")
        expected_next_state = "completed" if completed else None
        if synthesis_outcome.requested_next_state != expected_next_state:
            raise PhaseContractError("synthesis requested an illegal next state")
        report = synthesis_outcome.output.report
        if not machine.enforce_runtime():
            runtime_message = "maximum runtime exceeded during final synthesis"
            if runtime_message not in report.data_quality:
                report.data_quality.append(runtime_message)
            if runtime_message not in report.limitations:
                report.limitations.append(runtime_message)
            _append_observed_budget_failure(report.data_quality, machine)
            _append_observed_budget_failure(report.limitations, machine)
            report.run_status = "failed_with_limitations"
            completed = False
            pause_before_return = False
        self.store.write_json(run_id, "agent_execution_records.json", execution_records)
        self.store.write_json(run_id, "security_events.json", security_events)
        if completed and not machine.terminal:
            machine.transition(RunState.COMPLETED, "final report artifacts written")
        self._write_common_artifacts(run_id, machine, approvals, disputes)
        self.store.write_json(run_id, "final_report.json", report)
        self.store.write_text(run_id, "final_report.md", render_markdown(report))
        if not machine.enforce_runtime():
            runtime_message = "maximum runtime exceeded during final artifact writes"
            if runtime_message not in report.data_quality:
                report.data_quality.append(runtime_message)
            if runtime_message not in report.limitations:
                report.limitations.append(runtime_message)
            _append_observed_budget_failure(report.data_quality, machine)
            _append_observed_budget_failure(report.limitations, machine)
            report.run_status = "failed_with_limitations"
            pause_before_return = False
            self.store.write_json(run_id, "state.json", machine.snapshot())
            self.store.write_json(run_id, "final_report.json", report)
            self.store.write_text(run_id, "final_report.md", render_markdown(report))
        if pause_before_return:
            machine.pause_active_runtime()
            self.store.write_json(run_id, "state.json", machine.snapshot())
        self._publication_plans[run_id] = (planned_revision, transaction_id)
        try:
            verified = self._publish_buffer(run_id, report)
        except ProductRunError as exc:
            allowed_ephemeral_failure = (
                exc.failure.stage == "preflight"
                and exc.failure.code is ProductRunFailureCode.ARTIFACT_SCHEMA_ERROR
            ) or (
                exc.failure.stage == "budget_verify"
                and exc.failure.code is ProductRunFailureCode.BUDGET_SETTLEMENT_FAILED
            )
            if not allowed_ephemeral_failure:
                raise
            persistence_failure = f"product persistence failed: {exc.failure.code.value}"
            if persistence_failure not in report.data_quality:
                report.data_quality.append(persistence_failure)
            if persistence_failure not in report.limitations:
                report.limitations.append(persistence_failure)
            report.run_status = "failed_with_limitations"
            return report
        expected_status = (
            RunReadStatus.APPROVAL_REQUIRED
            if report.run_status == "approval_required"
            else (
                RunReadStatus.SUCCEEDED
                if report.run_status == "completed"
                else RunReadStatus.FAILED
            )
        )
        if verified.read_status is not expected_status:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.INTERNAL_INVARIANT_ERROR,
                stage="product_status_projection",
            )
        return report

    def _read_product(self, run_id: str) -> VerifiedRunReadV2:
        namespace = self._namespace_kind(run_id)
        if namespace is None:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.RUN_NOT_FOUND,
                stage="product_lookup",
            )
        if namespace == "legacy":
            raise self._product_error(
                run_id,
                ProductRunFailureCode.LEGACY_RUN_UNVERIFIED,
                stage="legacy_lookup",
                read_status=RunReadStatus.LEGACY_UNVERIFIED,
            )
        return self.product_store.read_current(run_id)

    def load_report(self, run_id: str) -> FinalReport:
        if self._namespace_kind(run_id) == "legacy":
            return self.legacy_adapter.load_report_projection(run_id)
        read = self._read_product(run_id)
        if read.read_status is RunReadStatus.IN_PROGRESS:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.RUN_INCOMPLETE,
                stage="load_report",
                read_status=RunReadStatus.INCOMPLETE,
            )
        report = FinalReport.model_validate_json(read.payload_bytes("final_report.json"))
        if read.read_status is RunReadStatus.SUCCEEDED:
            if report.run_status != "completed":
                raise self._product_error(
                    run_id,
                    ProductRunFailureCode.RUN_CORRUPT,
                    stage="load_report_status",
                    read_status=RunReadStatus.CORRUPT,
                )
            return report
        if read.read_status is RunReadStatus.APPROVAL_REQUIRED:
            if report.run_status != "approval_required":
                raise self._product_error(
                    run_id,
                    ProductRunFailureCode.RUN_CORRUPT,
                    stage="load_report_status",
                    read_status=RunReadStatus.CORRUPT,
                )
            return report
        if read.read_status in {
            RunReadStatus.FAILED,
            RunReadStatus.CANCELLED,
            RunReadStatus.CANCEL_UNCONFIRMED,
        }:
            report.run_status = "failed_with_limitations"
            limitation = f"verified product run status: {read.read_status.value}"
            if limitation not in report.limitations:
                report.limitations.append(limitation)
            return report
        if read.read_status is RunReadStatus.LEGACY_UNVERIFIED:
            report.run_status = "failed_with_limitations"
            limitation = "legacy_unverified_integrity_guarantees_missing"
            if limitation not in report.limitations:
                report.limitations.append(limitation)
            return report
        raise self._product_error(
            run_id,
            ProductRunFailureCode.INTERNAL_INVARIANT_ERROR,
            stage="load_report_projection",
        )

    def migrate_legacy_run(
        self,
        source_run_id: str,
        destination_run_id: str,
        *,
        source_quiescence_acknowledged: bool,
    ) -> VerifiedRunReadV2:
        if source_quiescence_acknowledged is not True:
            raise legacy_failure(
                destination_run_id,
                ProductRunFailureCode.MIGRATION_SOURCE_BUSY,
                stage="migration_preflight",
            )
        try:
            validate_run_id(source_run_id)
            validate_run_id(destination_run_id)
        except CanonicalStorageError as exc:
            raise legacy_failure(
                destination_run_id,
                ProductRunFailureCode.PATH_CONFINEMENT_FAILED,
                stage="migration_preflight",
            ) from exc
        if source_run_id.lower() == destination_run_id.lower():
            raise legacy_failure(
                destination_run_id,
                ProductRunFailureCode.MIGRATION_CONFLICT,
                stage="migration_preflight",
            )
        source_namespace = self._namespace_kind(source_run_id)
        if source_namespace != "legacy":
            raise legacy_failure(
                destination_run_id,
                (
                    ProductRunFailureCode.RUN_NOT_FOUND
                    if source_namespace is None
                    else ProductRunFailureCode.MIGRATION_CONFLICT
                ),
                stage="migration_source",
            )
        snapshot = self.legacy_adapter.inspect(source_run_id)
        binding = legacy_source_binding(snapshot)
        payloads = legacy_copy_payloads(snapshot)
        self._initialize_product_storage(destination_run_id)
        destination_namespace = self._namespace_kind(destination_run_id)
        if destination_namespace == "product":
            current = self.product_store.read_current(destination_run_id)
            if (
                current.read_status is RunReadStatus.LEGACY_UNVERIFIED
                and current.manifest.legacy_source == binding
                and {
                    payload.inventory.logical_name: payload.exact_bytes
                    for payload in current.payloads
                }
                == snapshot.payload_bytes()
            ):
                return current
            raise legacy_failure(
                destination_run_id,
                ProductRunFailureCode.MIGRATION_CONFLICT,
                stage="migration_replay",
            )
        if destination_namespace is not None:
            raise legacy_failure(
                destination_run_id,
                ProductRunFailureCode.MIGRATION_CONFLICT,
                stage="migration_destination",
            )
        identity_sha = canonical_domain_sha256(
            "poker-legacy-copy-identity-v2",
            {
                "source_root_identity_sha256": snapshot.source_root_identity_sha256,
                "source_run_id_sha256": snapshot.source_run_id_sha256,
                "source_inventory_sha256": snapshot.source_inventory_sha256,
                "destination_run_id": destination_run_id,
            },
        )
        transaction_id = f"txn-{identity_sha[:32]}"
        published_at = self.terminal_clock()
        source_payloads = snapshot.payload_bytes()
        input_sha = sha256_bytes(
            source_payloads.get("input.json", snapshot.source_inventory_sha256.encode("ascii"))
        )
        state_sha = sha256_bytes(
            source_payloads.get("state.json", snapshot.source_inventory_sha256.encode("ascii"))
        )
        request = TerminalPublishRequest(
            run_id=destination_run_id,
            transaction_id=transaction_id,
            publication_kind="legacy_copy",
            status="legacy_unverified",
            proposed_revision=1,
            expected_revision=None,
            expected_manifest_sha256=None,
            expected_pointer_sha256=None,
            created_at=published_at,
            updated_at=published_at,
            published_at=published_at,
            framework_version=__version__,
            source_commit_id=self.product_store.source_commit_id,
            tool_contract_versions=(),
            canonical_input_sha256=input_sha,
            config_sha256=canonical_domain_sha256(
                "poker-legacy-copy-config-v2",
                {"adapter_version": binding.adapter_version},
            ),
            budget_binding=provisional_budget_binding(
                destination_run_id,
                transaction_id,
                self.budget_policy,
            ),
            redaction_policy_sha256=canonical_domain_sha256(
                "poker-legacy-copy-redaction-v2",
                {"copy_exact_bytes": True},
            ),
            state_checkpoint_sha256=state_sha,
            event_head_sha256=empty_lineage_head_sha256("event"),
            approval_lineage_head_sha256=empty_lineage_head_sha256("approval"),
            context_lineage_head_sha256=empty_lineage_head_sha256("context"),
            execution_lineage_head_sha256=empty_lineage_head_sha256("execution"),
            legacy_source=binding,
            lifecycle_audit_sha256=None,
            payloads=payloads,
        )
        frozen = self.product_store.freeze_budget_binding(request)

        def verify_source_unchanged() -> None:
            try:
                observed = self.legacy_adapter.inspect(source_run_id)
            except ProductRunError as exc:
                raise legacy_failure(
                    destination_run_id,
                    ProductRunFailureCode.MIGRATION_SOURCE_CHANGED,
                    stage="migration_source_reread",
                    filesystem_effect="staging_orphan",
                    reconciliation_required=True,
                ) from exc
            if not same_legacy_snapshot(snapshot, observed):
                raise legacy_failure(
                    destination_run_id,
                    ProductRunFailureCode.MIGRATION_SOURCE_CHANGED,
                    stage="migration_source_reread",
                    filesystem_effect="staging_orphan",
                    reconciliation_required=True,
                )

        self.product_store.publish(
            frozen,
            pre_manifest_verifier=verify_source_unchanged,
        )
        migrated = self.product_store.read_current(destination_run_id)
        if (
            migrated.read_status is not RunReadStatus.LEGACY_UNVERIFIED
            or migrated.resume_eligible
            or migrated.completion_marker is not None
        ):
            raise self._product_error(
                destination_run_id,
                ProductRunFailureCode.INTERNAL_INVARIANT_ERROR,
                stage="migration_projection",
            )
        return migrated

    def _raise_audited_approval_failure(
        self,
        batch: ApprovalDecisionBatch,
        failure: ApprovalDecisionFailureV2,
    ) -> NoReturn:
        request = ApprovalFailureAuditRequest(
            run_id=batch.run_id,
            actor_sha256=approval_actor_sha256(batch.actor),
            decision_id_sha256=approval_reference_sha256(
                "decision_id",
                batch.decision_id,
            ),
            idempotency_key_sha256=approval_reference_sha256(
                "idempotency_key",
                batch.idempotency_key,
            ),
            batch_sha256=(
                None
                if failure.code is ApprovalFailureCode.APPROVAL_LEDGER_CORRUPT
                else approval_decision_batch_sha256(batch)
            ),
            failure_code=failure.code,
            observed_run_revision=failure.observed_run_revision,
            observed_ledger_revision=failure.observed_ledger_revision,
            occurred_at=self.terminal_clock(),
        )
        try:
            self.product_store.append_approval_failure_audit(request)
        except ApprovalFailureAuditError as exc:
            raise ApprovalDecisionValidationError(exc.failure) from exc
        raise ApprovalDecisionValidationError(failure.model_copy(update={"audit_confirmed": True}))

    def _approval_failure_from_product_error(
        self,
        batch: ApprovalDecisionBatch,
        error: ProductRunError,
    ) -> ApprovalDecisionFailureV2:
        if error.failure.code is ProductRunFailureCode.RUN_LOCKED:
            code = ApprovalFailureCode.RUN_LOCKED
            message = "Approval decision authority is currently locked."
        elif error.failure.code in {
            ProductRunFailureCode.RUN_CONFLICT,
            ProductRunFailureCode.IDEMPOTENCY_CONFLICT,
        }:
            code = ApprovalFailureCode.STALE_DECISION
            message = "Approval decision lost the exact current-revision CAS."
        else:
            code = ApprovalFailureCode.RESUME_TRANSACTION_FAILED
            message = "Approval decision transaction failed without an external effect."
        return approval_failure_v2(
            code,
            message,
            run_id=batch.run_id,
            decision_id=batch.decision_id,
            idempotency_key=batch.idempotency_key,
            observed_run_revision=error.failure.observed_revision,
            observed_ledger_revision=batch.expected_ledger_revision,
        )

    def decide_approvals(
        self,
        batch: ApprovalDecisionBatch,
    ) -> ApprovalDecisionOutcome:
        """Validate and publish one all-or-nothing authoritative V2 decision."""

        try:
            read = self._read_product(batch.run_id)
            try:
                state = read_approval_state_v2(
                    read.payload_bytes("approval_ledger_v2.json"),
                    read.payload_bytes("approval_decisions_v2.jsonl"),
                    read.payload_bytes("approval_audit_v2.jsonl"),
                )
            except (FileNotFoundError, ValueError) as exc:
                code = (
                    ApprovalFailureCode.LEGACY_APPROVAL_HISTORICAL_ONLY
                    if "approval_ledger_v2.json"
                    not in {item.inventory.logical_name for item in read.payloads}
                    else ApprovalFailureCode.APPROVAL_LEDGER_CORRUPT
                )
                failure = approval_failure_v2(
                    code,
                    (
                        "V1 approval artifacts are historical-only."
                        if code is ApprovalFailureCode.LEGACY_APPROVAL_HISTORICAL_ONLY
                        else "Authoritative approval artifacts are corrupt."
                    ),
                    run_id=batch.run_id,
                    decision_id=batch.decision_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=read.revision,
                    observed_ledger_revision=None,
                )
                self._raise_audited_approval_failure(batch, failure)
                raise AssertionError("unreachable") from exc
            admission = validate_approval_decision(
                state,
                batch,
                self.decision_authority_provider,
                observed_run_revision=read.revision,
                evaluated_at=batch.decision_at,
            )
            if admission.kind == "replay":
                if admission.replay_outcome is None:
                    raise RuntimeError("approval replay outcome is absent")
                return admission.replay_outcome
            if str(redact_sensitive(batch.reason, enabled=True)) != batch.reason:
                failure = approval_failure_v2(
                    ApprovalFailureCode.RESUME_CONFLICT,
                    "Decision reason must already be redacted.",
                    run_id=batch.run_id,
                    decision_id=batch.decision_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=read.revision,
                    observed_ledger_revision=state.ledger.ledger_revision,
                )
                self._raise_audited_approval_failure(batch, failure)
            update = build_approval_decision_update(admission)
            self._load_verified_buffer(read)
            self._approval_v2_payloads[batch.run_id] = dict(
                zip(
                    self._APPROVAL_V2_SCHEMAS,
                    encode_approval_state_v2(
                        update.ledger,
                        update.decision_records,
                        update.domain_audit_events,
                    ),
                    strict=True,
                )
            )
            snapshot = self.store.read_json(batch.run_id, "state.json")
            machine = WorkflowStateMachine.from_snapshot(
                self.budget_policy,
                snapshot,
                clock=self.monotonic_clock,
            )
            self._run_machines[batch.run_id] = machine
            if machine.state is not RunState.HUMAN_REVIEW_REQUIRED:
                failure = approval_failure_v2(
                    ApprovalFailureCode.RESUME_CONFLICT,
                    "Run is not at an approval checkpoint.",
                    run_id=batch.run_id,
                    decision_id=batch.decision_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=read.revision,
                    observed_ledger_revision=state.ledger.ledger_revision,
                )
                self._raise_audited_approval_failure(batch, failure)
            report = FinalReport.model_validate_json(read.payload_bytes("final_report.json"))
            next_state = read_approval_state_v2(
                *encode_approval_state_v2(
                    update.ledger,
                    update.decision_records,
                    update.domain_audit_events,
                )
            )
            report.approvals = project_v1_approvals(next_state)
            report.generated_at = batch.decision_at
            if update.outcome.run_status == "approval_required":
                report.run_status = "approval_required"
            elif update.outcome.run_status == "completed":
                machine.transition(
                    RunState.FINAL_SYNTHESIS,
                    "rejected actions replaced by the safe no-action path",
                )
                machine.transition(RunState.COMPLETED, "safe alternative report finalized")
                report.run_status = "completed"
                report.conclusion = (
                    "承認対象は拒否され、外部操作を行わない安全な代替結果を確定しました。"
                )
                if "Rejected external actions were not executed." not in report.limitations:
                    report.limitations.append("Rejected external actions were not executed.")
            else:
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "approval recorded but no external action executor is configured",
                )
                report.run_status = "failed_with_limitations"
                report.conclusion = (
                    "承認は記録されましたが、外部実行器がないため操作は実行されていません。"
                )
                limitation = "external_executor_unavailable"
                if limitation not in report.limitations:
                    report.limitations.append(limitation)
            self.store.write_json(batch.run_id, "state.json", machine.snapshot())
            self.store.write_json(batch.run_id, "approvals.json", report.approvals)
            self.store.write_json(batch.run_id, "final_report.json", report)
            self.store.write_text(
                batch.run_id,
                "final_report.md",
                render_markdown(report),
            )

            def verify_authority() -> None:
                reverify_approval_authority(
                    admission,
                    self.decision_authority_provider,
                    evaluated_at=self.terminal_clock(),
                )

            try:
                published = self._publish_buffer(
                    batch.run_id,
                    report,
                    previous_read=read,
                    transaction_id_override=approval_transaction_id(
                        batch.run_id,
                        batch.idempotency_key,
                        admission.batch_sha256,
                    ),
                    authority_verifier=verify_authority,
                )
            except ApprovalDecisionValidationError as exc:
                self._raise_audited_approval_failure(batch, exc.failure)
            except ProductRunError as exc:
                try:
                    winner = self.product_store.read_current(batch.run_id)
                    winner_state = read_approval_state_v2(
                        winner.payload_bytes("approval_ledger_v2.json"),
                        winner.payload_bytes("approval_decisions_v2.jsonl"),
                        winner.payload_bytes("approval_audit_v2.jsonl"),
                    )
                    replay = validate_approval_decision(
                        winner_state,
                        batch,
                        self.decision_authority_provider,
                        observed_run_revision=winner.revision,
                        evaluated_at=batch.decision_at,
                    )
                    if replay.kind == "replay" and replay.replay_outcome is not None:
                        return replay.replay_outcome
                except ApprovalDecisionValidationError as replay_error:
                    self._raise_audited_approval_failure(
                        batch,
                        replay_error.failure,
                    )
                except (ProductRunError, ValueError):
                    pass
                self._raise_audited_approval_failure(
                    batch,
                    self._approval_failure_from_product_error(batch, exc),
                )
            committed_state = read_approval_state_v2(
                published.payload_bytes("approval_ledger_v2.json"),
                published.payload_bytes("approval_decisions_v2.jsonl"),
                published.payload_bytes("approval_audit_v2.jsonl"),
            )
            committed = next(
                (
                    record.outcome
                    for record in committed_state.decision_records
                    if record.idempotency_key == batch.idempotency_key
                ),
                None,
            )
            if committed != update.outcome:
                failure = approval_failure_v2(
                    ApprovalFailureCode.RESUME_TRANSACTION_FAILED,
                    "Published approval outcome could not be verified.",
                    run_id=batch.run_id,
                    decision_id=batch.decision_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=published.revision,
                    observed_ledger_revision=committed_state.ledger.ledger_revision,
                )
                self._raise_audited_approval_failure(batch, failure)
            return update.outcome
        except ProductRunError as exc:
            self._raise_audited_approval_failure(
                batch,
                self._approval_failure_from_product_error(batch, exc),
            )
        except ApprovalDecisionValidationError as exc:
            if exc.failure.audit_confirmed:
                raise
            self._raise_audited_approval_failure(batch, exc.failure)

    def resume(
        self,
        run_id: str,
        *,
        approve_ids: list[str] | None = None,
        reject_ids: list[str] | None = None,
        reason: str = "human decision recorded by CLI",
        decision_batch: ApprovalDecisionBatch | None = None,
    ) -> FinalReport:
        read = self._read_product(run_id)
        if not read.resume_eligible:
            return self.load_report(run_id)
        if decision_batch is not None:
            if approve_ids or reject_ids:
                raise ValueError("decision_batch cannot be combined with approve_ids/reject_ids")
            if decision_batch.run_id != run_id:
                raise ValueError("decision_batch run_id mismatch")
            self.decide_approvals(decision_batch)
            return self.load_report(run_id)
        approval_names = {payload.inventory.logical_name for payload in read.payloads}
        if "approval_ledger_v2.json" in approval_names:
            if not approve_ids and not reject_ids:
                return self.load_report(run_id)
            state = read_approval_state_v2(
                read.payload_bytes("approval_ledger_v2.json"),
                read.payload_bytes("approval_decisions_v2.jsonl"),
                read.payload_bytes("approval_audit_v2.jsonl"),
            )
            requests = {request.request_id: request for request in state.ledger.requests}
            decision_at = self.terminal_clock()
            actor = self.decision_authority_provider.resolve_actor(
                "local-cli-user",
                decision_at=decision_at,
            ).actor
            items = []
            for request_id, decision in (
                *((request_id, "approved") for request_id in approve_ids or []),
                *((request_id, "rejected") for request_id in reject_ids or []),
            ):
                request_v2 = requests.get(request_id)
                items.append(
                    ApprovalDecisionItemV2(
                        request_id=request_id,
                        expected_request_revision=(
                            1 if request_v2 is None else request_v2.request_revision
                        ),
                        action_digest_sha256=(
                            "0" * 64 if request_v2 is None else request_v2.action_digest_sha256
                        ),
                        decision=cast(DecisionValue, decision),
                    )
                )
            items.sort(
                key=lambda item: (
                    item.request_id.encode("utf-8"),
                    item.decision.encode("ascii"),
                )
            )
            batch = ApprovalDecisionBatch(
                run_id=run_id,
                expected_run_revision=read.revision,
                expected_ledger_revision=state.ledger.ledger_revision,
                actor=actor,
                decision_id=self.terminal_id_factory("decision"),
                idempotency_key=self.terminal_id_factory("decision-key"),
                items=tuple(items),
                reason=str(
                    redact_sensitive(
                        reason,
                        enabled=not self.config.record_sensitive_data,
                    )
                ),
                decision_at=decision_at,
            )
            self.decide_approvals(batch)
            return self.load_report(run_id)
        if approve_ids:
            decision_id = self.terminal_id_factory("decision")
            idempotency_key = self.terminal_id_factory("decision-key")
            decision_at = self.terminal_clock()
            actor = LocalCliAuthorityProvider("local-cli-user").actor()
            failure = approval_failure_v2(
                ApprovalFailureCode.LEGACY_APPROVAL_HISTORICAL_ONLY,
                "V1 approval artifacts are historical-only and cannot authorize approval.",
                run_id=run_id,
                decision_id=decision_id,
                idempotency_key=idempotency_key,
                observed_run_revision=read.revision,
                observed_ledger_revision=None,
            )
            try:
                self.product_store.append_approval_failure_audit(
                    ApprovalFailureAuditRequest(
                        run_id=run_id,
                        actor_sha256=approval_actor_sha256(actor),
                        decision_id_sha256=approval_reference_sha256(
                            "decision_id",
                            decision_id,
                        ),
                        idempotency_key_sha256=approval_reference_sha256(
                            "idempotency_key",
                            idempotency_key,
                        ),
                        batch_sha256=None,
                        failure_code=failure.code,
                        observed_run_revision=read.revision,
                        observed_ledger_revision=None,
                        occurred_at=decision_at,
                    )
                )
            except ApprovalFailureAuditError as exc:
                raise ApprovalDecisionValidationError(exc.failure) from exc
            raise ApprovalDecisionValidationError(
                failure.model_copy(update={"audit_confirmed": True})
            )
        self._load_verified_buffer(read)
        snapshot = self.store.read_json(run_id, "state.json")
        machine = WorkflowStateMachine.from_snapshot(
            self.budget_policy,
            snapshot,
            clock=self.monotonic_clock,
        )
        self._run_machines[run_id] = machine
        if machine.state is not RunState.HUMAN_REVIEW_REQUIRED:
            return self.load_report(run_id)
        v1_requests = [
            ApprovalRequest.model_validate(item)
            for item in self.store.read_json(run_id, "approvals.json")
        ]
        ledger = ApprovalLedger(v1_requests)
        for approval_id in approve_ids or []:
            ledger.decide(
                approval_id,
                True,
                str(redact_sensitive(reason, enabled=not self.config.record_sensitive_data)),
            )
        for approval_id in reject_ids or []:
            prior_request = next(item for item in ledger.all() if item.approval_id == approval_id)
            historical_binding = HistoricalApprovalV1Binding(
                run_id=run_id,
                approval_id=approval_id,
                v1_request_sha256=sha256_bytes(canonical_storage_json_bytes(prior_request)),
                v1_status=prior_request.status.value,
            )
            ledger.decide(
                approval_id,
                False,
                (
                    "historical_v1_rejection:"
                    f"{historical_approval_v1_binding_sha256(historical_binding)}"
                ),
            )
        report = self.load_report(run_id)
        report.approvals = ledger.all()
        report.generated_at = datetime.now(UTC)
        if ledger.pending():
            report.run_status = "approval_required"
            self.store.write_json(run_id, "approvals.json", ledger.all())
            self.store.write_json(run_id, "final_report.json", report)
            self.store.write_text(run_id, "final_report.md", render_markdown(report))
            self._publish_buffer(run_id, report)
            return report
        rejected = [item for item in ledger.all() if item.status is ApprovalStatus.REJECTED]
        approved = [item for item in ledger.all() if item.status is ApprovalStatus.APPROVED]
        if approved:
            machine.transition(
                RunState.FAILED_WITH_LIMITATIONS,
                "approval recorded but no external action executor is configured in the MVP",
            )
            report.conclusion = (
                "承認は記録しましたが、MVPは外部操作を自動実行しないため制限付きで終了します。"
            )
            report.run_status = "failed_with_limitations"
            report.limitations.append(
                "承認済み外部操作は、人間が再現コマンドを確認して別途実行する必要があります。"
            )
        else:
            machine.transition(
                RunState.FINAL_SYNTHESIS, "rejected actions replaced by the safe no-action path"
            )
            machine.transition(RunState.COMPLETED, "safe alternative report finalized")
            report.conclusion = (
                "承認が拒否されたため、外部操作を行わない安全な代替結果を確定しました。"
            )
            report.run_status = "completed"
            if rejected:
                report.limitations.append("拒否された外部操作に依存する分析は未実行です。")
        self.store.write_json(run_id, "state.json", machine.snapshot())
        self.store.write_json(run_id, "approvals.json", ledger.all())
        self.store.write_json(run_id, "final_report.json", report)
        self.store.write_text(run_id, "final_report.md", render_markdown(report))
        self._publish_buffer(run_id, report)
        return report

    def report_path(self, run_id: str, format_name: str) -> Path:
        read = self._read_product(run_id)
        if read.read_status is RunReadStatus.LEGACY_UNVERIFIED:
            raise legacy_failure(
                run_id,
                ProductRunFailureCode.LEGACY_RUN_UNVERIFIED,
                stage="report_path",
            )
        return self.product_store.report_path(read, format_name)
