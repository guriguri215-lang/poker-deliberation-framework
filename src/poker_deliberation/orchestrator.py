"""Deterministic workflow owner for state, artifacts, budgets, and synthesis."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FunctionType
from typing import Any, ClassVar, Literal, NoReturn, cast

from poker_deliberation import __version__
from poker_deliberation.agents import select_roles
from poker_deliberation.approval_canonical import (
    approval_actor_sha256,
    approval_decision_batch_sha256,
    approval_reissue_batch_sha256,
    historical_approval_v1_binding_sha256,
)
from poker_deliberation.approval_models import (
    ApprovalDecisionBatch,
    ApprovalDecisionFailureV2,
    ApprovalDecisionItemV2,
    ApprovalDecisionOutcome,
    ApprovalDecisionRecordV2,
    ApprovalDomainAuditEventV2,
    ApprovalFailureCode,
    ApprovalLedgerV2,
    ApprovalReissueBatchV2,
    ApprovalReissueOutcomeV2,
    ApprovalReissueRecordV2,
    DecisionValue,
    HistoricalApprovalV1Binding,
)
from poker_deliberation.approvals import (
    SENSITIVE_ACTIONS,
    ApprovalDecisionValidationError,
    ApprovalLedger,
    DecisionAuthorityProvider,
    LocalCliAuthorityProvider,
    VerifiedApprovalStateV2,
    add_approval_request_v2,
    approval_failure_v2,
    approval_reference_sha256,
    approval_reissue_transaction_id,
    approval_transaction_id,
    build_approval_decision_update,
    build_approval_reissue_update,
    build_approval_request_v2,
    empty_approval_ledger_v2,
    encode_approval_reissue_log_v2,
    encode_approval_state_v2,
    project_v1_approvals,
    read_approval_state_v2,
    reverify_approval_authority,
    validate_approval_decision,
    validate_approval_reissue,
)
from poker_deliberation.bounded_natural_language import (
    BoundedNaturalLanguageAdmission,
    BoundedNaturalLanguageError,
    admit_bounded_natural_language_review,
)
from poker_deliberation.bounded_natural_language_models import (
    BOUNDED_NL_TOOL_ORDER,
    MAX_BOUNDED_NL_ARTIFACT_BYTES,
    MAX_BOUNDED_NL_RUN_BYTES,
    BoundedNaturalLanguageDiagnosticCode,
)
from poker_deliberation.bounded_natural_language_provenance import (
    build_bounded_natural_language_provenance,
)
from poker_deliberation.bounded_river_call_ev import (
    BoundedRiverCallEvAdmission,
    BoundedRiverCallEvError,
    admit_bounded_river_call_ev_review,
    apply_bounded_river_call_ev_report,
    bounded_river_call_ev_binding,
    build_bounded_river_call_ev_result,
    expected_bounded_river_tool_inputs,
)
from poker_deliberation.bounded_river_call_ev_models import (
    BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_MARKER,
    BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT,
    BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
    MAX_BOUNDED_RIVER_CALL_EV_ARTIFACT_BYTES,
    MAX_BOUNDED_RIVER_CALL_EV_RUN_BYTES,
    BoundedRiverCallEvDiagnosticCode,
    BoundedRiverCallEvResultV1,
)
from poker_deliberation.bounded_river_call_ev_provenance import (
    build_bounded_river_call_ev_provenance,
    verify_bounded_river_call_ev_structural_provenance,
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
from poker_deliberation.confirmed_review import (
    ConfirmedReviewAdmission,
    ConfirmedReviewError,
    admit_confirmed_review,
    build_confirmed_review_provenance,
)
from poker_deliberation.confirmed_review_models import (
    MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES,
    MAX_CONFIRMED_REVIEW_RUN_BYTES,
    ConfirmedReviewDiagnosticCode,
)
from poker_deliberation.context_lifecycle import (
    new_attempt_id,
    new_context_id,
)
from poker_deliberation.isolation import IsolationError, build_blind_decision_context
from poker_deliberation.normalization import (
    NormalizationResultV1,
    extract_normalization_result,
    verify_normalization_binding,
)
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
from poker_deliberation.phases.services import PurePhaseService
from poker_deliberation.providers import AgentProvider, LocalProvider
from poker_deliberation.range_equity import (
    VersionedRangeRiverEquityAdmissionV1,
    VersionedRangeRiverEquityError,
    admit_versioned_range_river_equity,
    build_versioned_range_river_equity_result,
    expected_versioned_range_equity_input,
    versioned_range_river_equity_binding,
)
from poker_deliberation.range_equity_models import (
    RANGE_EQUITY_BINDING_ARTIFACT,
    RANGE_EQUITY_MARKER,
    RANGE_EQUITY_TOOL_PLAN,
    RangeEquityDiagnosticCode,
)
from poker_deliberation.range_grammar import validate_versioned_range
from poker_deliberation.range_models import RangeValidationResultV1, VersionedRangeDefinitionV1
from poker_deliberation.reporting import render_markdown
from poker_deliberation.research import EvidenceLedger
from poker_deliberation.schemas import (
    AgentAssignment,
    AgentExecutionRecord,
    AgentExecutionStatus,
    AgentReport,
    ApprovalRequest,
    ApprovalStatus,
    CaseInput,
    Claim,
    ConfidenceGrade,
    Dispute,
    EvidenceRecord,
    FinalReport,
    NumericalExactness,
    SecurityEvent,
    ToolRequest,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.security import (
    blocked_security_guidance,
    redact_sensitive,
    screen_case,
)
from poker_deliberation.state_machine import RunState, StateEvent, WorkflowStateMachine
from poker_deliberation.storage.bounded_river_call_ev_admission_store import (
    commit_bounded_river_call_ev_admission_record,
    read_bounded_river_call_ev_admission_record,
    verify_bounded_river_call_ev_admission_record,
)
from poker_deliberation.storage.bounded_river_call_ev_failure_store import (
    commit_bounded_river_call_ev_budget_failure_evidence,
)
from poker_deliberation.storage.legacy_migration import (
    LegacyRunAdapter,
    legacy_copy_payloads,
    legacy_failure,
    legacy_source_binding,
    same_legacy_snapshot,
)
from poker_deliberation.storage.lifecycle_hooks import build_terminal_lifecycle_audit
from poker_deliberation.storage.range_equity_admission_store import (
    commit_range_equity_admission_record,
    read_range_equity_admission_record,
    verify_range_equity_admission_record,
)
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
from poker_deliberation.storage.revision_lock import AuthorityLease
from poker_deliberation.storage.revision_models import RunStorageError
from poker_deliberation.storage.revision_store import RunRevisionStore, inspect_root_initialization
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

_CONFIRMED_LOCAL_PROVIDER_AVAILABILITY = LocalProvider.availability
_CONFIRMED_LOCAL_PROVIDER_ANALYZE = LocalProvider.analyze
_CONFIRMED_ANALYSIS_EXECUTOR_RUN = AnalysisExecutor.run
_CONFIRMED_TOOL_EXECUTOR_RUN = ToolResearchExecutor.run
_CONFIRMED_REGISTRY_DESCRIBE = ToolRegistry.describe
_CONFIRMED_REGISTRY_EXECUTE = ToolRegistry.execute
_CONFIRMED_REGISTRY_EXECUTE_FOR_PHASE = ToolRegistry.execute_for_phase
_CONFIRMED_REGISTRY_NAMES = ToolRegistry.names
_CONFIRMED_REGISTRY_RUNTIME_IDENTITY = ToolRegistry.runtime_identity_snapshot
_CONFIRMED_SYSTEM_MONOTONIC_NOW = SystemMonotonicClock.now_ns
_CONFIRMED_PURE_PHASE_ISOLATE = PurePhaseService.isolate
_CONFIRMED_INTAKE_RUN = IntakeValidationService.run
_CONFIRMED_NORMALIZATION_RUN = NormalizationService.run
_CONFIRMED_ROUTING_RUN = RoutingService.run
_CONFIRMED_CONTEXT_BUILD_RUN = ContextBuildService.run
_CONFIRMED_CRITIQUE_RUN = CritiqueService.run
_CONFIRMED_ADJUDICATION_RUN = AdjudicationService.run
_CONFIRMED_SYNTHESIS_RUN = SynthesisService.run


def _callable_execution_token(value: object) -> str:
    target = getattr(value, "__func__", value)
    if not isinstance(target, FunctionType):
        return ""
    kwdefaults = target.__kwdefaults__ or {}
    closure = target.__closure__ or ()
    closure_tokens: list[str] = []
    for cell in closure:
        try:
            cell_value = cell.cell_contents
        except ValueError:
            closure_tokens.append(f"{id(cell)}:empty")
        else:
            closure_tokens.append(f"{id(cell)}:{id(cell_value)}")
    kwdefault_tokens = ",".join(f"{name}:{id(item)}" for name, item in sorted(kwdefaults.items()))
    return "|".join(
        (
            str(id(target.__code__)),
            str(id(target.__defaults__)),
            str(id(target.__kwdefaults__)),
            kwdefault_tokens,
            ",".join(closure_tokens),
        )
    )


def _class_callable_snapshot(
    cls: type[Any],
) -> tuple[tuple[type[Any], str, object, str], ...]:
    snapshot: list[tuple[type[Any], str, object, str]] = []
    for owner in cls.__mro__:
        for name, raw_value in vars(owner).items():
            value = (
                raw_value.__func__
                if isinstance(raw_value, (classmethod, staticmethod))
                else raw_value
            )
            if callable(value):
                snapshot.append((owner, name, value, _callable_execution_token(value)))
            if isinstance(raw_value, property):
                for suffix, accessor in (
                    ("fget", raw_value.fget),
                    ("fset", raw_value.fset),
                    ("fdel", raw_value.fdel),
                ):
                    if accessor is not None:
                        snapshot.append(
                            (
                                owner,
                                f"{name}.{suffix}",
                                accessor,
                                _callable_execution_token(accessor),
                            )
                        )
    return tuple(
        sorted(
            snapshot,
            key=lambda item: (item[0].__module__, item[0].__qualname__, item[1]),
        )
    )


def _instance_callable_snapshot(instance: object) -> tuple[tuple[str, object, str], ...]:
    return tuple(
        sorted(
            (
                (name, value, _callable_execution_token(value))
                for name, value in vars(instance).items()
                if callable(value)
            ),
            key=lambda item: item[0],
        )
    )


def _module_callable_snapshot() -> tuple[tuple[str, object, str], ...]:
    return tuple(
        sorted(
            (
                (name, value, _callable_execution_token(value))
                for name, value in globals().items()
                if callable(value)
            ),
            key=lambda item: item[0],
        )
    )


def _callable_snapshot_is_exact(
    current: tuple[tuple[Any, ...], ...],
    expected: tuple[tuple[Any, ...], ...],
) -> bool:
    return len(current) == len(expected) and all(
        len(current_item) == len(expected_item)
        and all(
            current_part == expected_part
            if isinstance(current_part, str)
            else (current_part is expected_part)
            for current_part, expected_part in zip(
                current_item,
                expected_item,
                strict=True,
            )
        )
        for current_item, expected_item in zip(current, expected, strict=True)
    )


_CONFIRMED_RUNTIME_CLASS_CALLABLES = tuple(
    (cls, _class_callable_snapshot(cls))
    for cls in (
        LocalProvider,
        AnalysisExecutor,
        ToolResearchExecutor,
        ToolRegistry,
        SystemMonotonicClock,
        PurePhaseService,
        IntakeValidationService,
        NormalizationService,
        RoutingService,
        ContextBuildService,
        CritiqueService,
        AdjudicationService,
        SynthesisService,
        TerminalRunStore,
        DurableBudgetCoordinator,
        DurableBudgetStore,
        RunRevisionStore,
        BufferedRunStore,
        LegacyRunAdapter,
    )
)


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
    _APPROVAL_V2_CORE_SCHEMAS: ClassVar[dict[str, tuple[str, str, str]]] = {
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
    _APPROVAL_REISSUE_ARTIFACT: ClassVar[str] = "approval_reissues_v2.jsonl"
    _APPROVAL_V2_SCHEMAS: ClassVar[dict[str, tuple[str, str, str]]] = {
        **_APPROVAL_V2_CORE_SCHEMAS,
        _APPROVAL_REISSUE_ARTIFACT: (
            "application/x-ndjson",
            "poker-run-storage-jsonl-v1",
            "poker-approval-reissue-log-artifact-v2",
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
        self._confirmed_review_persistence_was_injected = any(
            dependency is not None
            for dependency in (
                product_store,
                budget_store,
                terminal_clock,
                terminal_id_factory,
            )
        )
        self._confirmed_review_clock_was_injected = (
            context_clock is not None or monotonic_clock is not None
        )
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
        self._registry_was_injected = injected_registry is not None
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
            enforce_context_expiry=context_clock is None,
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
        self._confirmed_review_admissions: dict[str, ConfirmedReviewAdmission] = {}
        self._bounded_nl_admissions: dict[str, BoundedNaturalLanguageAdmission] = {}
        self._bounded_river_call_ev_admissions: dict[str, BoundedRiverCallEvAdmission] = {}
        self._bounded_river_call_ev_results: dict[str, BoundedRiverCallEvResultV1] = {}
        self._confirmed_review_provider = self.provider
        self._confirmed_review_registry = self.registry
        self._confirmed_review_registry_sha256 = canonical_domain_sha256(
            "poker-confirmed-review-registry-v1",
            self.registry.describe(),
        )
        self._confirmed_review_registry_runtime_snapshot = ToolRegistry.runtime_identity_snapshot(
            self.registry
        )
        self._confirmed_review_registry_mapping = self.registry._tools
        self._confirmed_review_registry_limits = (
            self.registry.max_payload_bytes,
            self.registry.max_output_bytes,
            self.registry.max_duration_seconds,
        )
        self._confirmed_review_analysis_context_clock = self.analysis_executor.context_clock
        self._confirmed_review_analysis_record_clock = self.analysis_executor.record_clock
        self._confirmed_review_product_store = self.product_store
        self._confirmed_review_product_foundation = self.product_store.foundation
        self._confirmed_review_durable_budget = self.durable_budget
        self._confirmed_review_durable_budget_store = self.durable_budget_store
        self._confirmed_review_buffer_store = self.store
        self._confirmed_review_terminal_clock = self.terminal_clock
        self._confirmed_review_terminal_id_factory = self.terminal_id_factory
        self._confirmed_review_product_store_snapshot = (
            self.product_store.revision_root,
            self.product_store.legacy_runs_root,
            self.product_store.max_artifact_bytes,
            self.product_store.max_run_bytes,
            self.product_store.framework_version,
            self.product_store.source_commit_id,
        )
        self._confirmed_review_persistence_objects = (
            self.product_store,
            self.product_store.foundation,
            self.durable_budget,
            self.durable_budget_store,
            self.durable_budget_store.revisions,
            self.store,
            self.legacy_adapter,
        )
        self._confirmed_review_persistence_types = tuple(
            type(instance) for instance in self._confirmed_review_persistence_objects
        )
        self._confirmed_review_persistence_instance_callables = tuple(
            _instance_callable_snapshot(instance)
            for instance in self._confirmed_review_persistence_objects
        )
        self._confirmed_review_boundary_callables = (
            (
                "analysis_context_clock",
                self.analysis_executor.context_clock,
                _callable_execution_token(self.analysis_executor.context_clock),
            ),
            (
                "analysis_record_clock",
                self.analysis_executor.record_clock,
                _callable_execution_token(self.analysis_executor.record_clock),
            ),
            (
                "terminal_clock",
                self.terminal_clock,
                _callable_execution_token(self.terminal_clock),
            ),
            (
                "terminal_id_factory",
                self.terminal_id_factory,
                _callable_execution_token(self.terminal_id_factory),
            ),
        )
        self._confirmed_review_durable_budget_policy = self.durable_budget.policy
        self._confirmed_review_persistence_configuration = (
            self.legacy_runs_root,
            self.revision_runs_root,
            self.durable_budget_runs_root,
            self.legacy_adapter.root,
            self.legacy_adapter.max_artifact_bytes,
            self.legacy_adapter.max_run_bytes,
            self.product_store.foundation.revision_root,
            self.product_store.foundation.legacy_runs_root,
            self.product_store.foundation.max_artifact_bytes,
            self.product_store.foundation.max_run_bytes,
            self.product_store.foundation.fault_injector,
            self.product_store.foundation.producer_id,
            self.product_store.foundation.producer_version,
            self.durable_budget_store.revisions.revision_root,
            self.durable_budget_store.revisions.legacy_runs_root,
            self.durable_budget_store.revisions.max_artifact_bytes,
            self.durable_budget_store.revisions.max_run_bytes,
            self.durable_budget_store.revisions.fault_injector,
            self.durable_budget_store.revisions.producer_id,
            self.durable_budget_store.revisions.producer_version,
            self.store.root,
            self.store.max_artifact_bytes,
            self.store.max_run_bytes,
        )

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
        control = self.revision_runs_root / ".revision-control"
        if control.is_dir():
            try:
                bounded_river_record = read_bounded_river_call_ev_admission_record(
                    self.revision_runs_root,
                    run_id,
                    maximum_bytes=self.budget_policy.max_artifact_bytes,
                )
                admission_record = read_range_equity_admission_record(
                    self.revision_runs_root,
                    run_id,
                    maximum_bytes=self.budget_policy.max_artifact_bytes,
                )
            except (CanonicalStorageError, OSError) as exc:
                raise self._product_error(
                    run_id,
                    ProductRunFailureCode.RUN_CORRUPT,
                    stage="namespace_admission_record",
                    read_status=RunReadStatus.CORRUPT,
                ) from exc
            if bounded_river_record is not None:
                return "bounded_river_call_ev_admission"
            if admission_record is not None:
                return "range_equity_admission"
        return None

    def _current_product_or_none(self, run_id: str) -> VerifiedRunReadV2 | None:
        try:
            return self.product_store.read_current(run_id)
        except ProductRunError as exc:
            if exc.failure.code is ProductRunFailureCode.RUN_NOT_FOUND:
                return None
            raise

    def _new_run_authority(self, run_id: str) -> AuthorityLease:
        try:
            _marker, marker_sha = self.product_store.foundation._ownership(run_id)
            return self.product_store.foundation._authority(
                run_id,
                marker_sha,
                bootstrap=True,
            )
        except RunStorageError as exc:
            code = (
                ProductRunFailureCode.RUN_LOCKED
                if exc.failure.code.value == "run_locked"
                else ProductRunFailureCode.LOCK_UNAVAILABLE
            )
            raise self._product_error(
                run_id,
                code,
                stage="new_run_authority",
            ) from exc
        except (CanonicalStorageError, OSError) as exc:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.RUN_CORRUPT,
                stage="new_run_reservation",
                read_status=RunReadStatus.CORRUPT,
            ) from exc

    def _reserve_new_run_under_authority(self, case: CaseInput, run_id: str) -> None:
        binding = versioned_range_river_equity_binding(case)
        bounded_river_binding = bounded_river_call_ev_binding(case)
        if bounded_river_binding is not None and binding is None:
            raise BoundedRiverCallEvError(
                BoundedRiverCallEvDiagnosticCode.RANGE,
                f"case.metadata.{RANGE_EQUITY_MARKER}",
                "P3-030C requires its admitted P3-016B binding",
            )
        try:
            namespace = self._namespace_kind(run_id)
            if namespace is not None or self.store.exists(run_id):
                code = (
                    ProductRunFailureCode.LEGACY_RUN_UNVERIFIED
                    if namespace == "legacy"
                    else ProductRunFailureCode.RUN_CONFLICT
                )
                status = RunReadStatus.LEGACY_UNVERIFIED if namespace == "legacy" else None
                raise self._product_error(
                    run_id,
                    code,
                    stage="new_run_namespace",
                    read_status=status,
                )
            self.product_store._bootstrap_namespace(run_id)
            self.product_store._sync_bootstrap_namespace(run_id)
            if binding is not None:
                commit_range_equity_admission_record(
                    self.revision_runs_root,
                    run_id,
                    binding,
                    maximum_bytes=self.budget_policy.max_artifact_bytes,
                )
            if bounded_river_binding is not None:
                commit_bounded_river_call_ev_admission_record(
                    self.revision_runs_root,
                    run_id,
                    bounded_river_binding,
                    maximum_bytes=self.budget_policy.max_artifact_bytes,
                )
            self.store.create_run(run_id)
        except ProductRunError:
            raise
        except RunStorageError as exc:
            code = (
                ProductRunFailureCode.RUN_LOCKED
                if exc.failure.code.value == "run_locked"
                else ProductRunFailureCode.LOCK_UNAVAILABLE
            )
            raise self._product_error(
                run_id,
                code,
                stage="new_run_reservation",
            ) from exc
        except FileExistsError as exc:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.RUN_CONFLICT,
                stage="new_run_reservation",
            ) from exc
        except (CanonicalStorageError, OSError) as exc:
            raise self._product_error(
                run_id,
                ProductRunFailureCode.RUN_CORRUPT,
                stage="new_run_reservation",
                read_status=RunReadStatus.CORRUPT,
            ) from exc

    def _reserve_new_run(self, case: CaseInput, run_id: str) -> None:
        """Reserve one product namespace before any run-local tool execution."""

        with self._new_run_authority(run_id):
            self._reserve_new_run_under_authority(case, run_id)

    def _verify_bounded_river_preexecution_records(
        self,
        admission: BoundedRiverCallEvAdmission,
    ) -> None:
        """Require both durable commitments before the first P3-030C tool dispatch."""

        run_id = admission.confirmation.run_id
        try:
            range_record = read_range_equity_admission_record(
                self.revision_runs_root,
                run_id,
                maximum_bytes=self.budget_policy.max_artifact_bytes,
            )
            bounded_record = read_bounded_river_call_ev_admission_record(
                self.revision_runs_root,
                run_id,
                maximum_bytes=self.budget_policy.max_artifact_bytes,
            )
            if range_record is None or bounded_record is None:
                raise CanonicalStorageError(
                    "bounded river call-EV pre-execution admission record is missing"
                )
            verify_range_equity_admission_record(
                range_record,
                admission.range_equity_admission.binding,
            )
            verify_bounded_river_call_ev_admission_record(
                bounded_record,
                admission.binding,
            )
        except (CanonicalStorageError, OSError) as exc:
            raise BoundedRiverCallEvError(
                BoundedRiverCallEvDiagnosticCode.STORAGE,
                "preexecution_admission",
            ) from exc

    def _commit_bounded_river_budget_failure(
        self,
        run_id: str,
        output: ToolResearchOutput,
    ) -> None:
        """Persist one independent typed failure before buffered tool artifacts."""

        failure = output.budget_failure
        admission = self._bounded_river_call_ev_admissions.get(run_id)
        if failure is None or admission is None:
            return
        if len(output.bindings) != 1:
            raise PhaseContractError(
                "bounded river budget failure requires one tool execution binding"
            )
        try:
            record = commit_bounded_river_call_ev_budget_failure_evidence(
                self.revision_runs_root,
                run_id,
                admission.binding,
                output.bindings[0],
                failure,
                self.budget_policy,
                usage_observed_at_ns=output.usage_observed_at_ns,
                maximum_bytes=self.budget_policy.max_artifact_bytes,
            )
            self.store.write_json(
                run_id,
                BOUNDED_RIVER_CALL_EV_FAILURE_EVIDENCE_ARTIFACT,
                record,
            )
        except (CanonicalStorageError, OSError) as exc:
            raise BoundedRiverCallEvError(
                BoundedRiverCallEvDiagnosticCode.STORAGE,
                "budget_failure_evidence",
            ) from exc

    def _reserve_legacy_migration_destination(self, run_id: str) -> None:
        """Atomically reserve an empty product namespace for one legacy migration."""

        try:
            _marker, marker_sha = self.product_store.foundation._ownership(run_id)
            with self.product_store.foundation._authority(
                run_id,
                marker_sha,
                bootstrap=True,
            ):
                if self._namespace_kind(run_id) is not None or self.store.exists(run_id):
                    raise legacy_failure(
                        run_id,
                        ProductRunFailureCode.MIGRATION_CONFLICT,
                        stage="migration_destination_reservation",
                    )
                self.product_store._bootstrap_namespace(run_id)
        except ProductRunError:
            raise
        except RunStorageError as exc:
            raise legacy_failure(
                run_id,
                ProductRunFailureCode.MIGRATION_CONFLICT,
                stage="migration_destination_authority",
            ) from exc
        except (CanonicalStorageError, OSError) as exc:
            raise legacy_failure(
                run_id,
                ProductRunFailureCode.MIGRATION_CONFLICT,
                stage="migration_destination_reservation",
            ) from exc

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

    def _execute_tool_requests(
        self,
        *,
        run_id: str,
        requests: tuple[ToolRequest, ...],
        existing_results: list[ToolResult],
        machine: WorkflowStateMachine,
    ) -> ToolResearchOutput:
        tool_usage, tool_observed_at_ns, tool_deadline_ns = machine.runtime_window()
        phase_request = make_phase_request(
            run_id=run_id,
            phase_id=PhaseId.TOOL_RESEARCH,
            attempt_id=_new_phase_attempt_id(PhaseId.TOOL_RESEARCH),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=ToolResearchInput(
                requests=requests,
                start_ordinal=len(existing_results),
                existing_result_ids=tuple(result.result_id for result in existing_results),
                fallback_result_ids=tuple(_new_internal_id("tool-result") for _ in requests),
                budget_policy=self.budget_policy,
                budget_snapshot=tool_usage,
                budget_observed_at_ns=tool_observed_at_ns,
                run_deadline_ns=tool_deadline_ns,
            ),
        )
        outcome = revalidate_outcome(
            phase_request,
            self.tool_research_executor.run(phase_request),
            output_type=ToolResearchOutput,
        )
        if outcome.output is None:
            raise PhaseContractError("tool research returned no output")
        validate_tool_research_output(phase_request, outcome.output)
        if outcome.output.usage_observed_at_ns is None:
            raise PhaseContractError("budgeted tool research returned no usage observation")
        machine.apply_usage_at(
            outcome.output.usage_delta,
            observed_at_ns=outcome.output.usage_observed_at_ns,
        )
        return outcome.output

    def _read_approval_state(
        self,
        read: VerifiedRunReadV2,
    ) -> VerifiedApprovalStateV2:
        payloads = {
            payload.inventory.logical_name: payload.exact_bytes
            for payload in read.payloads
            if payload.inventory.logical_name in self._APPROVAL_V2_SCHEMAS
        }
        return read_approval_state_v2(
            payloads["approval_ledger_v2.json"],
            payloads["approval_decisions_v2.jsonl"],
            payloads["approval_audit_v2.jsonl"],
            payloads.get(self._APPROVAL_REISSUE_ARTIFACT, b""),
        )

    def _approval_payload_map(
        self,
        ledger: ApprovalLedgerV2,
        decisions: tuple[ApprovalDecisionRecordV2, ...],
        events: tuple[ApprovalDomainAuditEventV2, ...],
        reissues: tuple[ApprovalReissueRecordV2, ...] = (),
    ) -> dict[str, bytes]:
        payloads = dict(
            zip(
                self._APPROVAL_V2_CORE_SCHEMAS,
                encode_approval_state_v2(ledger, decisions, events),
                strict=True,
            )
        )
        if reissues:
            payloads[self._APPROVAL_REISSUE_ARTIFACT] = encode_approval_reissue_log_v2(reissues)
        return payloads

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
            if not set(self._APPROVAL_V2_CORE_SCHEMAS) <= set(approval_payloads) or not set(
                approval_payloads
            ) <= set(self._APPROVAL_V2_SCHEMAS):
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
            else self._current_product_or_none(run_id)
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
            revision_root=self.product_store.revision_root,
            transaction_id=transaction_id,
            previous_manifest_sha256=(None if previous is None else previous.manifest_sha256),
            previous_pointer_sha256=(None if previous is None else previous.current_pointer_sha256),
            budget_policy=self.budget_policy,
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

    def run_versioned_range_river_equity(
        self,
        admission: VersionedRangeRiverEquityAdmissionV1,
        *,
        run_id: str | None = None,
    ) -> FinalReport:
        """Execute a strictly admitted, exact-only river range-equity bridge."""

        verified = admit_versioned_range_river_equity(admission.candidate)
        if (
            verified.candidate != admission.candidate
            or verified.binding != admission.binding
            or verified.case != admission.case
        ):
            raise VersionedRangeRiverEquityError(
                RangeEquityDiagnosticCode.PROVENANCE,
                "admission",
                "admission differs from deterministic reconstruction",
            )
        actual_run_id = run_id or new_run_id()
        try:
            return self._run(
                CaseInput.model_validate(verified.case.model_dump(mode="python"), strict=True),
                actual_run_id,
                normalization=None,
            )
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
                    verified.case.model_dump(mode="json"),
                    enabled=not self.config.record_sensitive_data,
                ),
                data_quality=[limitation],
                limitations=[limitation],
                confidence=ConfidenceGrade.D,
            )

    def run(self, case: CaseInput, *, run_id: str | None = None) -> FinalReport:
        case = CaseInput.model_validate(case.model_dump(mode="python"))
        if BOUNDED_RIVER_CALL_EV_MARKER in case.metadata:
            raise BoundedRiverCallEvError(
                BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING,
                f"case.metadata.{BOUNDED_RIVER_CALL_EV_MARKER}",
                "use run_bounded_river_call_ev_review with a verified admission",
            )
        if RANGE_EQUITY_MARKER in case.metadata:
            raise VersionedRangeRiverEquityError(
                RangeEquityDiagnosticCode.PROVENANCE,
                f"case.metadata.{RANGE_EQUITY_MARKER}",
                "use run_versioned_range_river_equity with a verified admission",
            )
        if "confirmed_review" in case.metadata:
            raise ConfirmedReviewError(
                ConfirmedReviewDiagnosticCode.CONFIRMATION_MISSING,
                "case.metadata.confirmed_review",
            )
        if "bounded_natural_language_review" in case.metadata:
            raise BoundedNaturalLanguageError(
                BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_MISSING,
                "case.metadata.bounded_natural_language_review",
            )
        case, normalization = extract_normalization_result(case)
        actual_run_id = run_id or new_run_id()
        try:
            return self._run(
                case,
                actual_run_id,
                normalization=normalization,
            )
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

    def _confirmed_review_runtime_is_exact(self) -> bool:
        phase_services = (
            (self.intake_service, IntakeValidationService, _CONFIRMED_INTAKE_RUN),
            (
                self.normalization_service,
                NormalizationService,
                _CONFIRMED_NORMALIZATION_RUN,
            ),
            (self.routing_service, RoutingService, _CONFIRMED_ROUTING_RUN),
            (
                self.context_build_service,
                ContextBuildService,
                _CONFIRMED_CONTEXT_BUILD_RUN,
            ),
            (self.critique_service, CritiqueService, _CONFIRMED_CRITIQUE_RUN),
            (
                self.adjudication_service,
                AdjudicationService,
                _CONFIRMED_ADJUDICATION_RUN,
            ),
            (self.synthesis_service, SynthesisService, _CONFIRMED_SYNTHESIS_RUN),
        )
        persistence_objects = (
            self.product_store,
            self.product_store.foundation,
            self.durable_budget,
            self.durable_budget_store,
            self.durable_budget_store.revisions,
            self.store,
            self.legacy_adapter,
        )
        if (
            not _callable_snapshot_is_exact(
                _module_callable_snapshot(),
                _CONFIRMED_ORCHESTRATOR_MODULE_CALLABLES,
            )
            or LocalProvider.availability is not _CONFIRMED_LOCAL_PROVIDER_AVAILABILITY
            or LocalProvider.analyze is not _CONFIRMED_LOCAL_PROVIDER_ANALYZE
            or AnalysisExecutor.run is not _CONFIRMED_ANALYSIS_EXECUTOR_RUN
            or ToolResearchExecutor.run is not _CONFIRMED_TOOL_EXECUTOR_RUN
            or ToolRegistry.describe is not _CONFIRMED_REGISTRY_DESCRIBE
            or ToolRegistry.execute is not _CONFIRMED_REGISTRY_EXECUTE
            or ToolRegistry.execute_for_phase is not _CONFIRMED_REGISTRY_EXECUTE_FOR_PHASE
            or ToolRegistry.names is not _CONFIRMED_REGISTRY_NAMES
            or ToolRegistry.runtime_identity_snapshot is not _CONFIRMED_REGISTRY_RUNTIME_IDENTITY
            or SystemMonotonicClock.now_ns is not _CONFIRMED_SYSTEM_MONOTONIC_NOW
            or PurePhaseService.isolate is not _CONFIRMED_PURE_PHASE_ISOLATE
            or type(self.monotonic_clock) is not SystemMonotonicClock
            or "now_ns" in getattr(self.monotonic_clock, "__dict__", {})
            or any(name in vars(self.provider) for name in ("availability", "analyze"))
            or "run" in vars(self.analysis_executor)
            or "run" in vars(self.tool_research_executor)
            or any(
                name in vars(self.registry)
                for name in (
                    "describe",
                    "execute",
                    "execute_for_phase",
                    "names",
                    "runtime_identity_snapshot",
                )
            )
            or self.analysis_executor.context_clock
            is not self._confirmed_review_analysis_context_clock
            or self.analysis_executor.record_clock
            is not self._confirmed_review_analysis_record_clock
            or not self.analysis_executor.enforce_context_expiry
            or self.registry.monotonic_clock is not self.monotonic_clock
            or type(self.registry._tools) is not dict
            or self.registry._tools is not self._confirmed_review_registry_mapping
            or (
                self.registry.max_payload_bytes,
                self.registry.max_output_bytes,
                self.registry.max_duration_seconds,
            )
            != self._confirmed_review_registry_limits
            or any(
                type(service) is not expected_type
                or expected_type.run is not expected_run
                or "run" in vars(service)
                or "isolate" in vars(service)
                for service, expected_type, expected_run in phase_services
            )
            or self.context_build_service.blind_context_builder is not build_blind_decision_context
            or self._confirmed_review_clock_was_injected
            or self._confirmed_review_persistence_was_injected
            or self.terminal_clock is not self._confirmed_review_terminal_clock
            or self.terminal_id_factory is not self._confirmed_review_terminal_id_factory
            or any(
                current is not expected
                for current, expected in zip(
                    persistence_objects,
                    self._confirmed_review_persistence_objects,
                    strict=True,
                )
            )
            or tuple(type(instance) for instance in persistence_objects)
            != self._confirmed_review_persistence_types
            or any(
                not _callable_snapshot_is_exact(
                    _instance_callable_snapshot(instance),
                    expected,
                )
                for instance, expected in zip(
                    persistence_objects,
                    self._confirmed_review_persistence_instance_callables,
                    strict=True,
                )
            )
            or any(
                not _callable_snapshot_is_exact(
                    _class_callable_snapshot(cls),
                    expected,
                )
                for cls, expected in _CONFIRMED_RUNTIME_CLASS_CALLABLES
            )
            or not _callable_snapshot_is_exact(
                (
                    (
                        "analysis_context_clock",
                        self.analysis_executor.context_clock,
                        _callable_execution_token(self.analysis_executor.context_clock),
                    ),
                    (
                        "analysis_record_clock",
                        self.analysis_executor.record_clock,
                        _callable_execution_token(self.analysis_executor.record_clock),
                    ),
                    (
                        "terminal_clock",
                        self.terminal_clock,
                        _callable_execution_token(self.terminal_clock),
                    ),
                    (
                        "terminal_id_factory",
                        self.terminal_id_factory,
                        _callable_execution_token(self.terminal_id_factory),
                    ),
                ),
                self._confirmed_review_boundary_callables,
            )
            or self.durable_budget.policy is not self._confirmed_review_durable_budget_policy
            or (
                self.legacy_runs_root,
                self.revision_runs_root,
                self.durable_budget_runs_root,
                self.legacy_adapter.root,
                self.legacy_adapter.max_artifact_bytes,
                self.legacy_adapter.max_run_bytes,
                self.product_store.foundation.revision_root,
                self.product_store.foundation.legacy_runs_root,
                self.product_store.foundation.max_artifact_bytes,
                self.product_store.foundation.max_run_bytes,
                self.product_store.foundation.fault_injector,
                self.product_store.foundation.producer_id,
                self.product_store.foundation.producer_version,
                self.durable_budget_store.revisions.revision_root,
                self.durable_budget_store.revisions.legacy_runs_root,
                self.durable_budget_store.revisions.max_artifact_bytes,
                self.durable_budget_store.revisions.max_run_bytes,
                self.durable_budget_store.revisions.fault_injector,
                self.durable_budget_store.revisions.producer_id,
                self.durable_budget_store.revisions.producer_version,
                self.store.root,
                self.store.max_artifact_bytes,
                self.store.max_run_bytes,
            )
            != self._confirmed_review_persistence_configuration
            or type(self.product_store) is not TerminalRunStore
            or self.product_store is not self._confirmed_review_product_store
            or self.product_store.foundation is not self._confirmed_review_product_foundation
            or type(self.product_store.foundation) is not RunRevisionStore
            or self.product_store.budget is not self._confirmed_review_durable_budget
            or self.product_store.clock is not self._confirmed_review_terminal_clock
            or self.product_store.id_factory is not self._confirmed_review_terminal_id_factory
            or self.product_store.fault_injector is not None
            or (
                self.product_store.revision_root,
                self.product_store.legacy_runs_root,
                self.product_store.max_artifact_bytes,
                self.product_store.max_run_bytes,
                self.product_store.framework_version,
                self.product_store.source_commit_id,
            )
            != self._confirmed_review_product_store_snapshot
            or type(self.durable_budget) is not DurableBudgetCoordinator
            or self.durable_budget is not self._confirmed_review_durable_budget
            or self.durable_budget.store is not self._confirmed_review_durable_budget_store
            or type(self.durable_budget_store) is not DurableBudgetStore
            or self.durable_budget_store is not self._confirmed_review_durable_budget_store
            or type(self.store) is not BufferedRunStore
            or self.store is not self._confirmed_review_buffer_store
        ):
            return False
        current = ToolRegistry.runtime_identity_snapshot(self.registry)
        expected = self._confirmed_review_registry_runtime_snapshot
        return len(current) == len(expected) and all(
            current_name == expected_name
            and current_definition is expected_definition
            and current_function is expected_function
            and current_contract is expected_contract
            for (
                current_name,
                current_definition,
                current_function,
                current_contract,
            ), (
                expected_name,
                expected_definition,
                expected_function,
                expected_contract,
            ) in zip(current, expected, strict=True)
        )

    def _prepare_confirmed_review_run(
        self,
        admission: ConfirmedReviewAdmission,
    ) -> FinalReport | None:
        run_id = admission.confirmation.run_id
        case = CaseInput.model_validate(admission.case.model_dump(mode="python"))
        self._initialize_product_storage(run_id)
        with self._new_run_authority(run_id):
            namespace = self._namespace_kind(run_id)
            if namespace is not None:
                if namespace != "product":
                    raise ConfirmedReviewError(
                        ConfirmedReviewDiagnosticCode.CONFIRMATION_REPLAY,
                        "confirmation.run_id",
                    )
                current = self.product_store.read_current(run_id)
                expected = {
                    "confirmed_review_source.txt": admission.source_bytes,
                    "confirmed_review_candidate.json": canonical_storage_json_bytes(
                        admission.candidate
                    ),
                    "confirmed_review_confirmation.json": canonical_storage_json_bytes(
                        admission.confirmation
                    ),
                }
                try:
                    exact_replay = all(
                        current.payload_bytes(name) == payload for name, payload in expected.items()
                    )
                except KeyError:
                    exact_replay = False
                if not exact_replay:
                    raise ConfirmedReviewError(
                        ConfirmedReviewDiagnosticCode.CONFIRMATION_REPLAY,
                        "confirmation.idempotency_key",
                    )
                return self._exact_terminal_report(current)
            hand_payload = (
                admission.case.hand.model_dump(mode="json")
                if admission.case.hand is not None
                else {}
            )
            hand_definition = self.registry._tools.get("hand_validator")
            hand_contract = hand_definition.contract if hand_definition is not None else None
            try:
                if hand_definition is None or hand_contract is None:
                    raise ValueError("hand validator contract is unavailable")
                validated_hand = hand_contract.input_model.model_validate(hand_payload)
                hand_output = hand_definition.function(
                    validated_hand.model_dump(mode="python", exclude_unset=True)
                )
                hand_contract.output_model.model_validate(hand_output)
            except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError):
                hand_output = {}
            if hand_output.get("valid") is not True:
                raise ConfirmedReviewError(
                    ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING,
                    "candidate.hand",
                )
            if "hand_pot_ledger" in admission.case.requested_tools:
                raw_tool_inputs = admission.case.metadata.get("tool_inputs", {})
                ledger_payload = (
                    raw_tool_inputs.get("hand_pot_ledger", {})
                    if isinstance(raw_tool_inputs, dict)
                    else {}
                )
                if not isinstance(ledger_payload, dict) or admission.case.hand is None:
                    raise ConfirmedReviewError(
                        ConfirmedReviewDiagnosticCode.CANDIDATE_TOOL,
                        "candidate.ledger_profile",
                    )
                ledger_validation = self.registry.execute(
                    "hand_pot_ledger",
                    {
                        **ledger_payload,
                        "hand": admission.case.hand.model_dump(mode="json"),
                    },
                    contract_version=self.tool_contract_versions.get("hand_pot_ledger"),
                )
                if ledger_validation.status is not ToolStatus.SUCCESS:
                    raise ConfirmedReviewError(
                        ConfirmedReviewDiagnosticCode.CANDIDATE_TOOL,
                        "candidate.ledger_profile",
                    )
            self._reserve_new_run_under_authority(case, run_id)
        return None

    def _prepare_bounded_natural_language_run(
        self,
        admission: BoundedNaturalLanguageAdmission,
    ) -> FinalReport | None:
        run_id = admission.confirmation.run_id
        case = CaseInput.model_validate(admission.case.model_dump(mode="python"))
        self._initialize_product_storage(run_id)
        with self._new_run_authority(run_id):
            namespace = self._namespace_kind(run_id)
            if namespace is not None:
                if namespace != "product":
                    raise BoundedNaturalLanguageError(
                        BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_REPLAY,
                        "confirmation.run_id",
                    )
                current = self.product_store.read_current(run_id)
                expected = {
                    "bounded_nl_source.txt": admission.source_bytes,
                    "bounded_nl_candidate.json": canonical_storage_json_bytes(admission.candidate),
                    "bounded_nl_confirmation.json": canonical_storage_json_bytes(
                        admission.confirmation
                    ),
                }
                try:
                    exact_replay = all(
                        current.payload_bytes(name) == payload for name, payload in expected.items()
                    )
                except KeyError:
                    exact_replay = False
                if not exact_replay:
                    raise BoundedNaturalLanguageError(
                        BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_REPLAY,
                        "confirmation.idempotency_key",
                    )
                return self._exact_terminal_report(current)
            hand = admission.case.hand
            raw_inputs = admission.case.metadata.get("tool_inputs")
            if hand is None or not isinstance(raw_inputs, dict):
                raise BoundedNaturalLanguageError(
                    BoundedNaturalLanguageDiagnosticCode.TOOL,
                    "candidate.tool_plan",
                )
            ledger_input = raw_inputs.get("hand_pot_ledger")
            pot_odds_input = raw_inputs.get("pot_odds")
            if not isinstance(ledger_input, dict) or not isinstance(pot_odds_input, dict):
                raise BoundedNaturalLanguageError(
                    BoundedNaturalLanguageDiagnosticCode.TOOL,
                    "candidate.tool_plan",
                )
            preflight_inputs = {
                "hand_validator": hand.model_dump(mode="json"),
                "hand_pot_ledger": {
                    **ledger_input,
                    "hand": hand.model_dump(mode="json"),
                },
                "pot_odds": pot_odds_input,
            }
            for tool_name in BOUNDED_NL_TOOL_ORDER:
                result = self.registry.execute(
                    tool_name,
                    preflight_inputs[tool_name],
                    contract_version=self.tool_contract_versions.get(tool_name),
                )
                if result.status is not ToolStatus.SUCCESS or (
                    result.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
                    and (result.verification is None or not result.verification.passed)
                ):
                    raise BoundedNaturalLanguageError(
                        BoundedNaturalLanguageDiagnosticCode.TOOL,
                        f"candidate.tool_plan.{tool_name}",
                    )
            self._reserve_new_run_under_authority(case, run_id)
        return None

    def _prepare_bounded_river_call_ev_run(
        self,
        admission: BoundedRiverCallEvAdmission,
    ) -> FinalReport | None:
        run_id = admission.confirmation.run_id
        case = CaseInput.model_validate(admission.case.model_dump(mode="python"))
        self._initialize_product_storage(run_id)
        with self._new_run_authority(run_id):
            namespace = self._namespace_kind(run_id)
            if namespace is not None:
                if namespace != "product":
                    raise BoundedRiverCallEvError(
                        BoundedRiverCallEvDiagnosticCode.CONFIRMATION_REPLAY,
                        "confirmation.run_id",
                    )
                current = self.product_store.read_current(run_id)
                expected = {
                    BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT: admission.source_bytes,
                    BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT: canonical_storage_json_bytes(
                        admission.candidate
                    ),
                    BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT: canonical_storage_json_bytes(
                        admission.confirmation
                    ),
                    BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT: canonical_storage_json_bytes(
                        admission.candidate.projection.range_definition
                    ),
                    BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT: canonical_storage_json_bytes(
                        admission.binding
                    ),
                }
                try:
                    exact_replay = all(
                        current.payload_bytes(name) == payload for name, payload in expected.items()
                    )
                except KeyError:
                    exact_replay = False
                if not exact_replay:
                    raise BoundedRiverCallEvError(
                        BoundedRiverCallEvDiagnosticCode.CONFIRMATION_REPLAY,
                        "confirmation.idempotency_key",
                    )
                return self._exact_terminal_report(current)
            if tuple(admission.case.requested_tools) != BOUNDED_RIVER_CALL_EV_TOOL_ORDER:
                raise BoundedRiverCallEvError(
                    BoundedRiverCallEvDiagnosticCode.TOOL_PLAN,
                    "candidate.tool_plan",
                )
            described = {item["name"] for item in self.registry.describe()}
            if not set(BOUNDED_RIVER_CALL_EV_TOOL_ORDER).issubset(described):
                raise BoundedRiverCallEvError(
                    BoundedRiverCallEvDiagnosticCode.TOOL_PLAN,
                    "runtime.registry",
                )
            self._reserve_new_run_under_authority(case, run_id)
        return None

    def run_confirmed_review(
        self,
        admission: ConfirmedReviewAdmission,
    ) -> FinalReport:
        """Execute a pre-admitted review with exact local runtime dependencies."""

        verified_admission = admit_confirmed_review(
            admission.source_bytes,
            admission.candidate,
            admission.confirmation,
        )
        if (
            verified_admission.source_bytes != admission.source_bytes
            or verified_admission.candidate != admission.candidate
            or verified_admission.confirmation != admission.confirmation
            or verified_admission.case != admission.case
        ):
            raise ConfirmedReviewError(
                ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING,
                "admission",
            )
        admission = verified_admission
        run_id = admission.confirmation.run_id
        if not self._confirmed_review_runtime_is_exact():
            raise ConfirmedReviewError(
                ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER,
                "runtime",
            )
        provider_availability = self.provider.availability()
        if (
            type(self.provider) is not LocalProvider
            or self.provider is not self._confirmed_review_provider
            or provider_availability.provider != "local"
            or provider_availability.version != "1.0.0"
            or self._registry_was_injected
            or type(self.registry) is not ToolRegistry
            or self.registry is not self._confirmed_review_registry
            or type(self.analysis_executor) is not AnalysisExecutor
            or self.analysis_executor.provider is not self.provider
            or self.analysis_executor.monotonic_clock is not self.monotonic_clock
            or type(self.tool_research_executor) is not ToolResearchExecutor
            or self.tool_research_executor.registry is not self.registry
            or self.tool_research_executor.record_sensitive_data
            != self.config.record_sensitive_data
            or canonical_domain_sha256(
                "poker-confirmed-review-registry-v1",
                self.registry.describe(),
            )
            != self._confirmed_review_registry_sha256
        ):
            raise ConfirmedReviewError(
                ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER,
                "runtime",
            )
        if (
            self.budget_policy.max_artifact_bytes > MAX_CONFIRMED_REVIEW_ARTIFACT_BYTES
            or self.budget_policy.max_run_bytes > MAX_CONFIRMED_REVIEW_RUN_BYTES
        ):
            raise ConfirmedReviewError(
                ConfirmedReviewDiagnosticCode.RUNTIME_BUDGET,
                "runtime.budget",
            )
        replay = self._prepare_confirmed_review_run(admission)
        if replay is not None:
            return replay
        self._confirmed_review_admissions[run_id] = admission
        try:
            return self._run(
                CaseInput.model_validate(admission.case.model_dump(mode="python")),
                run_id,
                normalization=None,
                new_run_reserved=True,
            )
        finally:
            self._confirmed_review_admissions.pop(run_id, None)

    def run_bounded_natural_language_review(
        self,
        admission: BoundedNaturalLanguageAdmission,
    ) -> FinalReport:
        """Execute a confirmed bounded-language review on the exact local runtime."""

        verified = admit_bounded_natural_language_review(
            admission.source_bytes,
            admission.candidate,
            admission.confirmation,
        )
        if (
            verified.source_bytes != admission.source_bytes
            or verified.candidate != admission.candidate
            or verified.confirmation != admission.confirmation
            or verified.case != admission.case
        ):
            raise BoundedNaturalLanguageError(
                BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING,
                "admission",
            )
        admission = verified
        run_id = admission.confirmation.run_id
        if not self._confirmed_review_runtime_is_exact():
            raise BoundedNaturalLanguageError(
                BoundedNaturalLanguageDiagnosticCode.LOCAL_PROVIDER,
                "runtime",
            )
        availability = self.provider.availability()
        if (
            type(self.provider) is not LocalProvider
            or self.provider is not self._confirmed_review_provider
            or availability.provider != "local"
            or availability.version != "1.0.0"
            or self._registry_was_injected
            or type(self.registry) is not ToolRegistry
            or self.registry is not self._confirmed_review_registry
            or type(self.analysis_executor) is not AnalysisExecutor
            or self.analysis_executor.provider is not self.provider
            or self.analysis_executor.monotonic_clock is not self.monotonic_clock
            or type(self.tool_research_executor) is not ToolResearchExecutor
            or self.tool_research_executor.registry is not self.registry
            or self.tool_research_executor.record_sensitive_data
            != self.config.record_sensitive_data
            or canonical_domain_sha256(
                "poker-confirmed-review-registry-v1",
                self.registry.describe(),
            )
            != self._confirmed_review_registry_sha256
        ):
            raise BoundedNaturalLanguageError(
                BoundedNaturalLanguageDiagnosticCode.LOCAL_PROVIDER,
                "runtime",
            )
        if (
            self.budget_policy.max_artifact_bytes > MAX_BOUNDED_NL_ARTIFACT_BYTES
            or self.budget_policy.max_run_bytes > MAX_BOUNDED_NL_RUN_BYTES
        ):
            raise BoundedNaturalLanguageError(
                BoundedNaturalLanguageDiagnosticCode.RUNTIME_BUDGET,
                "runtime.budget",
            )
        replay = self._prepare_bounded_natural_language_run(admission)
        if replay is not None:
            return replay
        self._bounded_nl_admissions[run_id] = admission
        try:
            return self._run(
                CaseInput.model_validate(admission.case.model_dump(mode="python")),
                run_id,
                normalization=None,
                new_run_reserved=True,
            )
        finally:
            self._bounded_nl_admissions.pop(run_id, None)

    def run_bounded_river_call_ev_review(
        self,
        admission: BoundedRiverCallEvAdmission,
    ) -> FinalReport:
        """Execute one confirmed bounded river call/fold EV comparison locally."""

        verified = admit_bounded_river_call_ev_review(
            admission.source_bytes,
            admission.candidate,
            admission.confirmation,
        )
        if (
            verified.source_bytes != admission.source_bytes
            or verified.candidate != admission.candidate
            or verified.confirmation != admission.confirmation
            or verified.binding != admission.binding
            or verified.range_equity_admission != admission.range_equity_admission
            or verified.case != admission.case
        ):
            raise BoundedRiverCallEvError(
                BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING,
                "admission",
            )
        admission = verified
        run_id = admission.confirmation.run_id
        if not self._confirmed_review_runtime_is_exact():
            raise BoundedRiverCallEvError(
                BoundedRiverCallEvDiagnosticCode.LOCAL_PROVIDER,
                "runtime",
            )
        availability = self.provider.availability()
        if (
            type(self.provider) is not LocalProvider
            or self.provider is not self._confirmed_review_provider
            or availability.provider != "local"
            or availability.version != "1.0.0"
            or self._registry_was_injected
            or type(self.registry) is not ToolRegistry
            or self.registry is not self._confirmed_review_registry
            or type(self.analysis_executor) is not AnalysisExecutor
            or self.analysis_executor.provider is not self.provider
            or type(self.tool_research_executor) is not ToolResearchExecutor
            or self.tool_research_executor.registry is not self.registry
            or canonical_domain_sha256(
                "poker-confirmed-review-registry-v1",
                self.registry.describe(),
            )
            != self._confirmed_review_registry_sha256
        ):
            raise BoundedRiverCallEvError(
                BoundedRiverCallEvDiagnosticCode.LOCAL_PROVIDER,
                "runtime",
            )
        if (
            self.budget_policy.max_artifact_bytes > MAX_BOUNDED_RIVER_CALL_EV_ARTIFACT_BYTES
            or self.budget_policy.max_run_bytes > MAX_BOUNDED_RIVER_CALL_EV_RUN_BYTES
        ):
            raise BoundedRiverCallEvError(
                BoundedRiverCallEvDiagnosticCode.BUDGET,
                "runtime.budget",
            )
        replay = self._prepare_bounded_river_call_ev_run(admission)
        if replay is not None:
            return replay
        self._bounded_river_call_ev_admissions[run_id] = admission
        try:
            return self._run(
                CaseInput.model_validate(admission.case.model_dump(mode="python")),
                run_id,
                normalization=None,
                new_run_reserved=True,
            )
        finally:
            self._bounded_river_call_ev_admissions.pop(run_id, None)
            self._bounded_river_call_ev_results.pop(run_id, None)

    def _run(
        self,
        case: CaseInput,
        actual_run_id: str,
        *,
        normalization: NormalizationResultV1 | None,
        new_run_reserved: bool = False,
    ) -> FinalReport:
        try:
            validate_run_id(actual_run_id)
        except CanonicalStorageError as exc:
            raise self._product_error(
                actual_run_id,
                ProductRunFailureCode.PATH_CONFINEMENT_FAILED,
                stage="new_run_preflight",
            ) from exc
        self._initialize_product_storage(actual_run_id)
        if not new_run_reserved:
            self._reserve_new_run(case, actual_run_id)
        confirmed_admission = self._confirmed_review_admissions.get(actual_run_id)
        bounded_admission = self._bounded_nl_admissions.get(actual_run_id)
        bounded_river_admission = self._bounded_river_call_ev_admissions.get(actual_run_id)
        if (
            sum(
                item is not None
                for item in (confirmed_admission, bounded_admission, bounded_river_admission)
            )
            > 1
        ):
            raise PhaseContractError("multiple confirmed intake contracts share one run")
        if confirmed_admission is not None:
            if confirmed_admission.confirmation.run_id != actual_run_id:
                raise ConfirmedReviewError(
                    ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING,
                    "confirmation.run_id",
                )
            self.store.write_text(
                actual_run_id,
                "confirmed_review_source.txt",
                confirmed_admission.source_bytes.decode("utf-8", errors="strict"),
            )
            self.store.write_json(
                actual_run_id,
                "confirmed_review_candidate.json",
                confirmed_admission.candidate,
            )
            self.store.write_json(
                actual_run_id,
                "confirmed_review_confirmation.json",
                confirmed_admission.confirmation,
            )
        if bounded_admission is not None:
            if bounded_admission.confirmation.run_id != actual_run_id:
                raise BoundedNaturalLanguageError(
                    BoundedNaturalLanguageDiagnosticCode.CONFIRMATION_BINDING,
                    "confirmation.run_id",
                )
            self.store.write_text(
                actual_run_id,
                "bounded_nl_source.txt",
                bounded_admission.source_bytes.decode("utf-8", errors="strict"),
            )
            self.store.write_json(
                actual_run_id,
                "bounded_nl_candidate.json",
                bounded_admission.candidate,
            )
            self.store.write_json(
                actual_run_id,
                "bounded_nl_confirmation.json",
                bounded_admission.confirmation,
            )
        if bounded_river_admission is not None:
            if bounded_river_admission.confirmation.run_id != actual_run_id:
                raise BoundedRiverCallEvError(
                    BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING,
                    "confirmation.run_id",
                )
            self._verify_bounded_river_preexecution_records(bounded_river_admission)
            self.store.write_text(
                actual_run_id,
                BOUNDED_RIVER_CALL_EV_SOURCE_ARTIFACT,
                bounded_river_admission.source_bytes.decode("utf-8", errors="strict"),
            )
            self.store.write_json(
                actual_run_id,
                BOUNDED_RIVER_CALL_EV_CANDIDATE_ARTIFACT,
                bounded_river_admission.candidate,
            )
            self.store.write_json(
                actual_run_id,
                BOUNDED_RIVER_CALL_EV_CONFIRMATION_ARTIFACT,
                bounded_river_admission.confirmation,
            )
            self.store.write_json(
                actual_run_id,
                BOUNDED_RIVER_CALL_EV_RANGE_ARTIFACT,
                bounded_river_admission.candidate.projection.range_definition,
            )
            self.store.write_json(
                actual_run_id,
                BOUNDED_RIVER_CALL_EV_BINDING_ARTIFACT,
                bounded_river_admission.binding,
            )
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
        range_equity_admission_binding = versioned_range_river_equity_binding(case)
        if range_equity_admission_binding is not None:
            self.store.write_json(
                actual_run_id,
                RANGE_EQUITY_BINDING_ARTIFACT,
                range_equity_admission_binding,
            )
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
                zip(self._APPROVAL_V2_CORE_SCHEMAS, approval_bytes, strict=True)
            )

        machine.transition(RunState.NORMALIZE, "input parsed into CaseInput")
        normalization_request = make_phase_request(
            run_id=actual_run_id,
            phase_id=PhaseId.NORMALIZATION,
            attempt_id=_new_phase_attempt_id(PhaseId.NORMALIZATION),
            policy_snapshot_hash=self.phase_policy_snapshot_hash,
            input_value=NormalizationInput(
                safe_case=safe_case,
                normalization=normalization,
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
        normalization = normalization_outcome.output.normalization
        data_quality.extend(normalization_outcome.output.warnings)
        if normalization is not None:
            verify_normalization_binding(case, normalized, normalization)
            self.store.write_json(actual_run_id, "normalization.json", normalization)
        self.store.write_json(actual_run_id, "normalized_case.json", normalized)
        case = CaseInput.model_validate(
            {
                **case.model_dump(mode="python"),
                "hand": normalized.hand,
            }
        )
        self.store.write_json(
            actual_run_id,
            "assumptions.json",
            redact_sensitive(case.assumptions, enabled=not self.config.record_sensitive_data),
        )
        assignments = list(select_roles(case))
        self.store.write_json(actual_run_id, "assignments.json", assignments)

        machine.transition(RunState.DATA_VALIDATION, "canonical schema validation completed")
        security_events = screen_case(case)
        self.store.write_json(actual_run_id, "security_events.json", security_events)
        if any(event.category == "prompt_injection" for event in security_events):
            data_quality.append(
                "プロンプトインジェクションらしき文字列を無害な入力として記録しました。"
            )
        if any(event.blocked for event in security_events):
            data_quality.extend(blocked_security_guidance(security_events))
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
                hand_window = machine.checked_runtime_window()
                if hand_window is None:
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
                hand_usage, hand_observed_at_ns, hand_deadline_ns = hand_window
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
                if bounded_river_admission is None:
                    data_quality.extend(tool_phase_outcome.output.data_quality)
                self._commit_bounded_river_budget_failure(
                    actual_run_id,
                    tool_phase_outcome.output,
                )
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
                role_snapshot=tuple(assignments),
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
                analysis_window = machine.checked_runtime_window()
                if analysis_window is None:
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
                usage_before, budget_observed_at_ns, run_deadline_ns = analysis_window
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
                provider_timeout = min(30.0, remaining_runtime)
                lifecycle_now = self.context_clock()
                started_at = lifecycle_now
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
                context_window = machine.checked_runtime_window()
                if context_window is None:
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
                usage_before, budget_observed_at_ns, run_deadline_ns = context_window
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
                preflight_window = machine.checked_runtime_window()
                if preflight_window is None:
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
                usage_before, budget_observed_at_ns, run_deadline_ns = preflight_window
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
                reports.append(analysis.report)
                report_ids.add(analysis.report.report_id)
                self.store.write_json(
                    actual_run_id,
                    f"agent_reports/{analysis.report.report_id}.json",
                    analysis.report,
                )
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
                if (
                    confirmed_admission is not None or bounded_admission is not None
                ) and analysis.execution_record.status is not AgentExecutionStatus.COMPLETED:
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "confirmed local analysis did not complete within its context",
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
        if bounded_river_admission is not None:
            if (
                case != bounded_river_admission.case
                or bounded_river_call_ev_binding(case) != bounded_river_admission.binding
                or tuple(case.requested_tools) != BOUNDED_RIVER_CALL_EV_TOOL_ORDER
            ):
                raise BoundedRiverCallEvError(
                    BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING,
                    "runtime.case",
                )
            expected_bounded_inputs = expected_bounded_river_tool_inputs(bounded_river_admission)
            for tool_name in ("hand_pot_ledger", "pot_odds"):
                if not machine.enforce_runtime():
                    data_quality.append(f"strict runtime refused before bounded river {tool_name}")
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
                bounded_request = ToolRequest(
                    request_id=_new_internal_id("tool-request"),
                    tool_name=tool_name,
                    input=expected_bounded_inputs[tool_name],
                    requested_by="bounded-river-call-ev",
                    contract_version=self.tool_contract_versions.get(tool_name),
                )
                try:
                    bounded_prefix_output = self._execute_tool_requests(
                        run_id=actual_run_id,
                        requests=(bounded_request,),
                        existing_results=tool_results,
                        machine=machine,
                    )
                except BudgetLimitError as exc:
                    data_quality.append(f"strict usage settlement failed: {exc.failure.code}")
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        f"bounded river {tool_name} usage settlement failed",
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
                if bounded_river_admission is None:
                    data_quality.extend(bounded_prefix_output.data_quality)
                if len(bounded_prefix_output.bindings) != 1:
                    raise PhaseContractError(
                        "bounded river prerequisite returned an invalid binding count"
                    )
                self._commit_bounded_river_budget_failure(
                    actual_run_id,
                    bounded_prefix_output,
                )
                result = bounded_prefix_output.bindings[0].result
                tool_results.append(result)
                self.store.write_json(
                    actual_run_id,
                    f"tool_results/{result.result_id}.json",
                    result,
                )
                self.store.write_json(
                    actual_run_id,
                    f"tool_results/{result.result_id}.input.json",
                    result.input,
                )
                if (
                    bounded_prefix_output.budget_failure is not None
                    or result.status is not ToolStatus.SUCCESS
                ):
                    if bounded_prefix_output.budget_failure is not None:
                        data_quality.append(
                            f"strict budget failure: {bounded_prefix_output.budget_failure.code}"
                        )
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        f"bounded river {tool_name} prerequisite failed",
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
                already_run.add(result.tool_name)
        requested_tool_calls: list[ToolRequest] = []
        versioned_ranges = (
            [
                item
                for item in case.hand.known_ranges
                if isinstance(item, VersionedRangeDefinitionV1)
            ]
            if case.hand is not None
            else []
        )
        auto_range_validation: RangeValidationResultV1 | None = None
        auto_range_payload: dict[str, object] | None = None
        auto_range_request: ToolRequest | None = None
        auto_combo_payload: dict[str, object] | None = None
        auto_equity_payload: dict[str, object] | None = None
        auto_equity_request: ToolRequest | None = None
        auto_call_ev_request: ToolRequest | None = None
        skip_versioned_combos = False
        range_equity_binding = versioned_range_river_equity_binding(case)
        if case.hand is not None and "combos" in case.requested_tools and versioned_ranges:
            if len(versioned_ranges) != 1:
                data_quality.append(
                    "RNG_E_TARGET: versioned range product execution requires exactly one range"
                )
                skip_versioned_combos = True
            else:
                range_definition = versioned_ranges[0]
                auto_range_payload = {
                    "schema_version": "1.0.0",
                    "hand": case.hand.model_dump(mode="json"),
                    "range_definition": range_definition.model_dump(mode="json"),
                }
                supplied_validation = tool_inputs.get("range_validate")
                if supplied_validation not in (None, {}, auto_range_payload):
                    data_quality.append(
                        "RNG_E_PROVENANCE: conflicting range_validate input was refused"
                    )
                    skip_versioned_combos = True
                auto_range_request = ToolRequest(
                    request_id=_new_internal_id("tool-request"),
                    tool_name="range_validate",
                    input=auto_range_payload,
                    requested_by="versioned-range-product",
                    contract_version=self.tool_contract_versions.get("range_validate"),
                )
                auto_range_validation = validate_versioned_range(
                    case.hand,
                    range_definition,
                )
                if auto_range_validation.status == "failed":
                    codes = ",".join(
                        diagnostic.code.value for diagnostic in auto_range_validation.diagnostics
                    )
                    data_quality.append(f"versioned range validation failed closed: {codes}")
                    skip_versioned_combos = True
        if auto_range_request is not None:
            if not machine.enforce_runtime():
                data_quality.append("strict runtime refused before versioned range validation")
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
            try:
                range_output = self._execute_tool_requests(
                    run_id=actual_run_id,
                    requests=(auto_range_request,),
                    existing_results=tool_results,
                    machine=machine,
                )
            except BudgetLimitError as exc:
                data_quality.append(f"strict usage settlement failed: {exc.failure.code}")
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "versioned range validation usage settlement failed",
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
            if bounded_river_admission is None:
                data_quality.extend(range_output.data_quality)
            self._commit_bounded_river_budget_failure(actual_run_id, range_output)
            validation = range_output.bindings[0].result
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
            if range_output.budget_failure is not None:
                data_quality.append(f"strict budget failure: {range_output.budget_failure.code}")
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "versioned range validation budget refused",
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
            if validation.status is not ToolStatus.SUCCESS:
                data_quality.append("versioned range validation tool failed closed")
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "versioned range validation tool failed",
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
            if auto_range_validation is None:
                raise PhaseContractError("versioned range deterministic preflight is missing")
            if validation.output != auto_range_validation.model_dump(mode="json"):
                raise PhaseContractError(
                    "versioned range validation differs from deterministic preflight"
                )
        for tool_name in case.requested_tools:
            if tool_name in already_run and (
                tool_name == "hand_validator" or bounded_river_admission is not None
            ):
                continue
            if tool_name == "range_validate" and auto_range_payload is not None:
                continue
            if tool_name == "combos" and auto_range_validation is not None:
                if skip_versioned_combos:
                    continue
                auto_combo_payload = {
                    "range": auto_range_validation.canonical_notation,
                    "dead_cards": [],
                }
                supplied_payload = tool_inputs.get(tool_name)
                if supplied_payload not in (None, {}, auto_combo_payload):
                    data_quality.append("RNG_E_PROVENANCE: conflicting combos input was refused")
                    skip_versioned_combos = True
                    auto_combo_payload = None
                    continue
                requested_tool_calls.append(
                    ToolRequest(
                        request_id=_new_internal_id("tool-request"),
                        tool_name=tool_name,
                        input=auto_combo_payload,
                        requested_by="versioned-range-product",
                        contract_version=self.tool_contract_versions.get(tool_name),
                    )
                )
                continue
            if tool_name == "combos" and versioned_ranges and skip_versioned_combos:
                continue
            if tool_name == "holdem_equity" and range_equity_binding is not None:
                if skip_versioned_combos or auto_range_validation is None:
                    continue
                auto_equity_payload = expected_versioned_range_equity_input(
                    case,
                    auto_range_validation,
                )
                supplied_payload = tool_inputs.get(tool_name)
                if supplied_payload not in (None, {}, auto_equity_payload):
                    data_quality.append(
                        "REQ_E_PROVENANCE: conflicting holdem_equity input was refused"
                    )
                    auto_equity_payload = None
                    continue
                auto_equity_request = ToolRequest(
                    request_id=_new_internal_id("tool-request"),
                    tool_name=tool_name,
                    input=auto_equity_payload,
                    requested_by="versioned-range-bridge",
                    contract_version=self.tool_contract_versions.get(tool_name),
                )
                continue
            if tool_name == "raked_call_ev" and bounded_river_admission is not None:
                expected_bounded_inputs = expected_bounded_river_tool_inputs(
                    bounded_river_admission
                )
                auto_call_ev_payload = expected_bounded_inputs["raked_call_ev"]
                supplied_payload = tool_inputs.get(tool_name)
                if supplied_payload != auto_call_ev_payload:
                    raise BoundedRiverCallEvError(
                        BoundedRiverCallEvDiagnosticCode.TOOL_PLAN,
                        "case.metadata.tool_inputs.raked_call_ev",
                        "manual or mutated call-EV input was refused",
                    )
                auto_call_ev_request = ToolRequest(
                    request_id=_new_internal_id("tool-request"),
                    tool_name=tool_name,
                    input=auto_call_ev_payload,
                    requested_by="bounded-river-call-ev",
                    contract_version=self.tool_contract_versions.get(tool_name),
                )
                continue
            payload = tool_inputs.get(tool_name, {})
            if not isinstance(payload, dict):
                payload = {}
            if tool_name == "hand_pot_ledger" and case.hand is not None:
                canonical_hand = case.hand.model_dump(mode="json")
                supplied_hand = payload.get("hand")
                if supplied_hand is not None and supplied_hand != canonical_hand:
                    data_quality.append(
                        "hand_pot_ledger input hand does not match the canonical case hand"
                    )
                    payload = {}
                else:
                    payload = {**payload, "hand": canonical_hand}
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
        try:
            requested_tools_output = self._execute_tool_requests(
                run_id=actual_run_id,
                requests=tuple(requested_tool_calls),
                existing_results=tool_results,
                machine=machine,
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
        if bounded_river_admission is None:
            data_quality.extend(requested_tools_output.data_quality)
        self._commit_bounded_river_budget_failure(actual_run_id, requested_tools_output)
        tool_results.extend(binding.result for binding in requested_tools_output.bindings)
        for result in tool_results:
            self.store.write_json(actual_run_id, f"tool_results/{result.result_id}.json", result)
            self.store.write_json(
                actual_run_id, f"tool_results/{result.result_id}.input.json", result.input
            )
        if requested_tools_output.budget_failure is not None:
            data_quality.append(
                f"strict budget failure: {requested_tools_output.budget_failure.code}"
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
        if auto_combo_payload is not None:
            auto_combo_results = [
                binding.result
                for binding in requested_tools_output.bindings
                if binding.result.tool_name == "combos"
                and binding.result.input == auto_combo_payload
            ]
            if len(auto_combo_results) != 1:
                raise PhaseContractError("versioned range product requires one bound combos result")
            if auto_combo_results[0].status is not ToolStatus.SUCCESS:
                data_quality.append("versioned range combos tool failed closed")
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "versioned range combos tool failed",
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
        if range_equity_binding is not None:
            if auto_equity_request is None or auto_equity_payload is None:
                raise PhaseContractError(
                    "versioned range river equity admission lacks its derived equity request"
                )
            if not machine.enforce_runtime():
                data_quality.append("strict runtime refused before versioned range river equity")
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
            try:
                equity_tools_output = self._execute_tool_requests(
                    run_id=actual_run_id,
                    requests=(auto_equity_request,),
                    existing_results=tool_results,
                    machine=machine,
                )
            except BudgetLimitError as exc:
                data_quality.append(f"strict usage settlement failed: {exc.failure.code}")
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "versioned range river equity usage settlement failed",
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
            if bounded_river_admission is None:
                data_quality.extend(equity_tools_output.data_quality)
            if len(equity_tools_output.bindings) != 1:
                raise PhaseContractError(
                    "versioned range river equity requires one bound equity result"
                )
            self._commit_bounded_river_budget_failure(actual_run_id, equity_tools_output)
            equity_result = equity_tools_output.bindings[0].result
            tool_results.append(equity_result)
            self.store.write_json(
                actual_run_id,
                f"tool_results/{equity_result.result_id}.json",
                equity_result,
            )
            self.store.write_json(
                actual_run_id,
                f"tool_results/{equity_result.result_id}.input.json",
                equity_result.input,
            )
            if equity_tools_output.budget_failure is not None:
                data_quality.append(
                    f"strict budget failure: {equity_tools_output.budget_failure.code}"
                )
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "versioned range river equity budget refused",
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
            if equity_result.status is not ToolStatus.SUCCESS:
                data_quality.append("versioned range river equity tool failed closed")
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "versioned range river equity tool failed",
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
            range_tool_results = [
                result for result in tool_results if result.tool_name in RANGE_EQUITY_TOOL_PLAN
            ]
            if bounded_river_admission is not None:
                build_versioned_range_river_equity_result(
                    bounded_river_admission.range_equity_admission.case,
                    range_tool_results,
                )
            else:
                build_versioned_range_river_equity_result(case, range_tool_results)
        if bounded_river_admission is not None:
            if auto_call_ev_request is None:
                raise BoundedRiverCallEvError(
                    BoundedRiverCallEvDiagnosticCode.TOOL_PLAN,
                    "raked_call_ev",
                    "the derived no-rake call-EV request is missing",
                )
            if not machine.enforce_runtime():
                data_quality.append("strict runtime refused before bounded river call-EV")
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
            try:
                call_ev_output = self._execute_tool_requests(
                    run_id=actual_run_id,
                    requests=(auto_call_ev_request,),
                    existing_results=tool_results,
                    machine=machine,
                )
            except BudgetLimitError as exc:
                data_quality.append(f"strict usage settlement failed: {exc.failure.code}")
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "bounded river call-EV usage settlement failed",
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
            if bounded_river_admission is None:
                data_quality.extend(call_ev_output.data_quality)
            if len(call_ev_output.bindings) != 1:
                raise PhaseContractError("bounded river call-EV requires one tool result")
            self._commit_bounded_river_budget_failure(actual_run_id, call_ev_output)
            call_ev_result = call_ev_output.bindings[0].result
            tool_results.append(call_ev_result)
            self.store.write_json(
                actual_run_id,
                f"tool_results/{call_ev_result.result_id}.json",
                call_ev_result,
            )
            self.store.write_json(
                actual_run_id,
                f"tool_results/{call_ev_result.result_id}.input.json",
                call_ev_result.input,
            )
            if call_ev_output.budget_failure is not None or (
                call_ev_result.status is not ToolStatus.SUCCESS
            ):
                if call_ev_output.budget_failure is not None:
                    data_quality.append(
                        f"strict budget failure: {call_ev_output.budget_failure.code}"
                    )
                machine.transition(
                    RunState.FAILED_WITH_LIMITATIONS,
                    "bounded river call-EV failed",
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
            bounded_result = build_bounded_river_call_ev_result(
                bounded_river_admission,
                tool_results,
            )
            self._bounded_river_call_ev_results[actual_run_id] = bounded_result
            self.store.write_json(
                actual_run_id,
                BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
                bounded_result,
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

    def _run_synthesis_service(
        self,
        *,
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
        completed: bool,
        machine: WorkflowStateMachine,
        planned_revision: int,
        transaction_id: str,
    ) -> FinalReport:
        provider_info = self.provider.availability()
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
                    reason=provider_info.reason,
                ),
                tool_input_artifact_paths=tuple(
                    str(
                        self.product_store.planned_payload_path(
                            run_id,
                            revision=planned_revision,
                            transaction_id=transaction_id,
                            logical_name=f"tool_results/{result.result_id}.input.json",
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
        return synthesis_outcome.output.report

    @staticmethod
    def _apply_bounded_river_call_ev_report(
        report: FinalReport,
        result: BoundedRiverCallEvResultV1 | None,
    ) -> FinalReport:
        return apply_bounded_river_call_ev_report(report, result)

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
        previous = self._current_product_or_none(run_id) if namespace == "product" else None
        planned_revision = 1 if previous is None else previous.revision + 1
        transaction_id = self.terminal_id_factory("txn")
        confirmed_admission = self._confirmed_review_admissions.get(run_id)
        bounded_admission = self._bounded_nl_admissions.get(run_id)
        bounded_river_admission = self._bounded_river_call_ev_admissions.get(run_id)
        strict_admission_present = any(
            item is not None
            for item in (confirmed_admission, bounded_admission, bounded_river_admission)
        )
        if (
            sum(
                item is not None
                for item in (confirmed_admission, bounded_admission, bounded_river_admission)
            )
            > 1
        ):
            raise PhaseContractError("multiple confirmed intake contracts share one run")
        if completed and strict_admission_present:
            try:
                _, observed_at_ns, deadline_ns = machine.runtime_window()
            except BudgetLimitError:
                machine.enforce_runtime()
                observed_at_ns = 0
                deadline_ns = 0
            if deadline_ns - observed_at_ns < 250_000_000:
                if machine.state is not RunState.FAILED_WITH_LIMITATIONS:
                    machine.transition(
                        RunState.FAILED_WITH_LIMITATIONS,
                        "confirmed terminal publication safety reserve was exhausted",
                    )
                data_quality.append(
                    "confirmed terminal publication refused with less than 0.25 seconds remaining"
                )
                _append_observed_budget_failure(data_quality, machine)
                completed = False
                pause_before_return = False
        report = self._run_synthesis_service(
            run_id=run_id,
            case=case,
            data_quality=data_quality,
            claim_assessments=claim_assessments,
            reports=reports,
            execution_records=execution_records,
            tool_results=tool_results,
            disputes=disputes,
            evidence_records=evidence_records,
            approvals=approvals,
            security_events=security_events,
            completed=completed,
            machine=machine,
            planned_revision=planned_revision,
            transaction_id=transaction_id,
        )
        report = self._apply_bounded_river_call_ev_report(
            report,
            self._bounded_river_call_ev_results.get(run_id),
        )
        if not machine.enforce_runtime():
            runtime_message = "maximum runtime exceeded during final synthesis"
            if runtime_message not in data_quality:
                data_quality.append(runtime_message)
            _append_observed_budget_failure(data_quality, machine)
            completed = False
            pause_before_return = False
            report = self._run_synthesis_service(
                run_id=run_id,
                case=case,
                data_quality=data_quality,
                claim_assessments=claim_assessments,
                reports=reports,
                execution_records=execution_records,
                tool_results=tool_results,
                disputes=disputes,
                evidence_records=evidence_records,
                approvals=approvals,
                security_events=security_events,
                completed=False,
                machine=machine,
                planned_revision=planned_revision,
                transaction_id=transaction_id,
            )
            report = self._apply_bounded_river_call_ev_report(
                report,
                self._bounded_river_call_ev_results.get(run_id),
            )
        self.store.write_json(run_id, "agent_execution_records.json", execution_records)
        self.store.write_json(run_id, "security_events.json", security_events)
        if completed and not machine.terminal:
            machine.transition(RunState.COMPLETED, "final report artifacts written")
        self._write_common_artifacts(run_id, machine, approvals, disputes)
        self.store.write_json(run_id, "final_report.json", report)
        self.store.write_text(run_id, "final_report.md", render_markdown(report))
        if not machine.enforce_runtime():
            runtime_message = "maximum runtime exceeded during final artifact writes"
            if runtime_message not in data_quality:
                data_quality.append(runtime_message)
            _append_observed_budget_failure(data_quality, machine)
            completed = False
            pause_before_return = False
            report = self._run_synthesis_service(
                run_id=run_id,
                case=case,
                data_quality=data_quality,
                claim_assessments=claim_assessments,
                reports=reports,
                execution_records=execution_records,
                tool_results=tool_results,
                disputes=disputes,
                evidence_records=evidence_records,
                approvals=approvals,
                security_events=security_events,
                completed=False,
                machine=machine,
                planned_revision=planned_revision,
                transaction_id=transaction_id,
            )
            report = self._apply_bounded_river_call_ev_report(
                report,
                self._bounded_river_call_ev_results.get(run_id),
            )
            self.store.write_json(run_id, "state.json", machine.snapshot())
            self.store.write_json(run_id, "final_report.json", report)
            self.store.write_text(run_id, "final_report.md", render_markdown(report))
        if pause_before_return:
            machine.pause_active_runtime()
            self.store.write_json(run_id, "state.json", machine.snapshot())
        if confirmed_admission is not None:
            raw_assignments = self.store.read_json(run_id, "assignments.json")
            if not isinstance(raw_assignments, list):
                raise self._product_error(
                    run_id,
                    ProductRunFailureCode.ARTIFACT_SCHEMA_ERROR,
                    stage="confirmed_review_provenance",
                )
            assignments = [
                AgentAssignment.model_validate(assignment) for assignment in raw_assignments
            ]
            provenance = build_confirmed_review_provenance(
                confirmed_admission,
                report,
                assignments=assignments,
                agent_reports=reports,
                storage_root=self.product_store.revision_root,
                storage_revision=planned_revision,
                storage_transaction_id=transaction_id,
            )
            self.store.write_json(
                run_id,
                "confirmed_review_provenance.json",
                provenance,
            )
        if bounded_admission is not None:
            raw_assignments = self.store.read_json(run_id, "assignments.json")
            if not isinstance(raw_assignments, list):
                raise self._product_error(
                    run_id,
                    ProductRunFailureCode.ARTIFACT_SCHEMA_ERROR,
                    stage="bounded_nl_provenance",
                )
            assignments = [
                AgentAssignment.model_validate(assignment) for assignment in raw_assignments
            ]
            bounded_provenance = build_bounded_natural_language_provenance(
                bounded_admission,
                report,
                assignments=assignments,
                agent_reports=reports,
                storage_root=self.product_store.revision_root,
                storage_revision=planned_revision,
                storage_transaction_id=transaction_id,
            )
            self.store.write_json(
                run_id,
                "bounded_nl_provenance.json",
                bounded_provenance,
            )
        if bounded_river_admission is not None:
            bounded_river_result = self._bounded_river_call_ev_results.get(run_id)
            if report.run_status == "completed" and bounded_river_result is None:
                raise BoundedRiverCallEvError(
                    BoundedRiverCallEvDiagnosticCode.REPLAY,
                    BOUNDED_RIVER_CALL_EV_RESULT_ARTIFACT,
                )
            raw_assignments = self.store.read_json(run_id, "assignments.json")
            if not isinstance(raw_assignments, list):
                raise self._product_error(
                    run_id,
                    ProductRunFailureCode.ARTIFACT_SCHEMA_ERROR,
                    stage="bounded_river_call_ev_provenance",
                )
            assignments = [
                AgentAssignment.model_validate(assignment) for assignment in raw_assignments
            ]
            verify_bounded_river_call_ev_structural_provenance(
                source_bytes=bounded_river_admission.source_bytes,
                candidate=bounded_river_admission.candidate,
                confirmation=bounded_river_admission.confirmation,
                case=bounded_river_admission.case,
                result=bounded_river_result,
                report=report,
                admitted_at=bounded_river_admission.admitted_at,
                assignments=assignments,
                agent_reports=reports,
                storage_root=self.product_store.revision_root,
                storage_revision=planned_revision,
                storage_transaction_id=transaction_id,
            )
            if report.run_status == "completed":
                assert bounded_river_result is not None
                bounded_river_provenance = build_bounded_river_call_ev_provenance(
                    bounded_river_admission,
                    bounded_river_result,
                    report,
                    assignments=assignments,
                    agent_reports=reports,
                    storage_root=self.product_store.revision_root,
                    storage_revision=planned_revision,
                    storage_transaction_id=transaction_id,
                )
                self.store.write_json(
                    run_id,
                    BOUNDED_RIVER_CALL_EV_PROVENANCE_ARTIFACT,
                    bounded_river_provenance,
                )
        self._publication_plans[run_id] = (planned_revision, transaction_id)
        try:
            verified = self._publish_buffer(run_id, report)
        except ProductRunError as exc:
            if (
                strict_admission_present
                and exc.failure.code is ProductRunFailureCode.BUDGET_SETTLEMENT_FAILED
            ):
                raise
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

    def _exact_terminal_report(self, read: VerifiedRunReadV2) -> FinalReport:
        report = FinalReport.model_validate_json(read.payload_bytes("final_report.json"))
        expected_status = {
            RunReadStatus.SUCCEEDED: "completed",
            RunReadStatus.APPROVAL_REQUIRED: "approval_required",
            RunReadStatus.FAILED: "failed_with_limitations",
            RunReadStatus.CANCELLED: "failed_with_limitations",
            RunReadStatus.CANCEL_UNCONFIRMED: "failed_with_limitations",
        }.get(read.read_status)
        if expected_status is None or report.run_status != expected_status:
            raise self._product_error(
                read.run_id,
                ProductRunFailureCode.RUN_CORRUPT,
                stage="load_report_status",
                read_status=RunReadStatus.CORRUPT,
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
        self._reserve_legacy_migration_destination(destination_run_id)
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

        def verify_migration_authority() -> None:
            destination_namespace = self._namespace_kind(destination_run_id)
            if (
                destination_namespace != "product"
                or self._current_product_or_none(destination_run_id) is not None
                or self.store.exists(destination_run_id)
            ):
                raise legacy_failure(
                    destination_run_id,
                    ProductRunFailureCode.MIGRATION_CONFLICT,
                    stage="migration_destination_revalidation",
                    filesystem_effect="staging_orphan",
                    reconciliation_required=True,
                )
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
            pre_manifest_verifier=verify_migration_authority,
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

    def _raise_audited_reissue_failure(
        self,
        batch: ApprovalReissueBatchV2,
        failure: ApprovalDecisionFailureV2,
    ) -> NoReturn:
        batch_sha256 = approval_reissue_batch_sha256(batch)
        request = ApprovalFailureAuditRequest(
            run_id=batch.run_id,
            actor_sha256=canonical_domain_sha256(
                "poker-approval-reissue-requester-v2",
                {
                    "run_id": batch.run_id,
                    "batch_sha256": batch_sha256,
                },
            ),
            decision_id_sha256=approval_reference_sha256(
                "decision_id",
                batch.reissue_id,
            ),
            idempotency_key_sha256=approval_reference_sha256(
                "idempotency_key",
                batch.idempotency_key,
            ),
            batch_sha256=(
                None
                if failure.code is ApprovalFailureCode.APPROVAL_LEDGER_CORRUPT
                else batch_sha256
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

    def _reissue_failure_from_product_error(
        self,
        batch: ApprovalReissueBatchV2,
        error: ProductRunError,
    ) -> ApprovalDecisionFailureV2:
        if error.failure.code is ProductRunFailureCode.RUN_LOCKED:
            code = ApprovalFailureCode.RUN_LOCKED
            message = "Approval reissue authority is currently locked."
        elif error.failure.code in {
            ProductRunFailureCode.RUN_CONFLICT,
            ProductRunFailureCode.IDEMPOTENCY_CONFLICT,
        }:
            code = ApprovalFailureCode.STALE_DECISION
            message = "Approval reissue lost the exact current-revision CAS."
        else:
            code = ApprovalFailureCode.RESUME_TRANSACTION_FAILED
            message = "Approval reissue transaction failed without an external effect."
        return approval_failure_v2(
            code,
            message,
            run_id=batch.run_id,
            decision_id=batch.reissue_id,
            idempotency_key=batch.idempotency_key,
            observed_run_revision=error.failure.observed_revision,
            observed_ledger_revision=batch.expected_ledger_revision,
        )

    def reissue_approvals(
        self,
        batch: ApprovalReissueBatchV2,
    ) -> ApprovalReissueOutcomeV2:
        """Publish one explicit historical or expired-request successor checkpoint."""

        try:
            read = self._read_product(batch.run_id)
            names = {payload.inventory.logical_name for payload in read.payloads}
            state: VerifiedApprovalStateV2 | None
            legacy_requests: tuple[ApprovalRequest, ...]
            if "approval_ledger_v2.json" in names:
                try:
                    state = self._read_approval_state(read)
                except ValueError as exc:
                    failure = approval_failure_v2(
                        ApprovalFailureCode.APPROVAL_LEDGER_CORRUPT,
                        "Authoritative approval artifacts are corrupt.",
                        run_id=batch.run_id,
                        decision_id=batch.reissue_id,
                        idempotency_key=batch.idempotency_key,
                        observed_run_revision=read.revision,
                        observed_ledger_revision=None,
                    )
                    self._raise_audited_reissue_failure(batch, failure)
                    raise AssertionError("unreachable") from exc
                legacy_requests = ()
            else:
                state = None
                legacy_report = FinalReport.model_validate_json(
                    read.payload_bytes("final_report.json")
                )
                legacy_requests = tuple(legacy_report.approvals)
            admission = validate_approval_reissue(
                state,
                legacy_requests,
                batch,
                observed_run_revision=read.revision,
                previous_manifest_sha256=read.manifest_sha256,
                previous_pointer_sha256=read.current_pointer_sha256,
            )
            if admission.kind == "replay":
                if admission.replay_outcome is None:
                    raise RuntimeError("approval reissue replay outcome is absent")
                return admission.replay_outcome
            observed_at = self.terminal_clock()
            if batch.reissued_at > observed_at:
                failure = approval_failure_v2(
                    ApprovalFailureCode.RESUME_CONFLICT,
                    "Reissue time is ahead of the trusted run clock.",
                    run_id=batch.run_id,
                    decision_id=batch.reissue_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=read.revision,
                    observed_ledger_revision=(
                        None if state is None else state.ledger.ledger_revision
                    ),
                )
                self._raise_audited_reissue_failure(batch, failure)
            if not read.resume_eligible:
                failure = approval_failure_v2(
                    ApprovalFailureCode.RESUME_CONFLICT,
                    "Run is not at a resumable approval checkpoint.",
                    run_id=batch.run_id,
                    decision_id=batch.reissue_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=read.revision,
                    observed_ledger_revision=(
                        None if state is None else state.ledger.ledger_revision
                    ),
                )
                self._raise_audited_reissue_failure(batch, failure)
            if redact_sensitive(batch, enabled=True) != batch.model_dump(mode="json"):
                failure = approval_failure_v2(
                    ApprovalFailureCode.RESUME_CONFLICT,
                    "Reissue batch must already be redacted.",
                    run_id=batch.run_id,
                    decision_id=batch.reissue_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=read.revision,
                    observed_ledger_revision=(
                        None if state is None else state.ledger.ledger_revision
                    ),
                )
                self._raise_audited_reissue_failure(batch, failure)
            update = build_approval_reissue_update(admission)
            self._load_verified_buffer(read)
            self._approval_v2_payloads[batch.run_id] = self._approval_payload_map(
                update.ledger,
                update.decision_records,
                update.domain_audit_events,
                update.reissue_records,
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
                    decision_id=batch.reissue_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=read.revision,
                    observed_ledger_revision=update.ledger.ledger_revision,
                )
                self._raise_audited_reissue_failure(batch, failure)
            next_state = read_approval_state_v2(
                *encode_approval_state_v2(
                    update.ledger,
                    update.decision_records,
                    update.domain_audit_events,
                ),
                encode_approval_reissue_log_v2(update.reissue_records),
            )
            report = FinalReport.model_validate_json(read.payload_bytes("final_report.json"))
            report.approvals = project_v1_approvals(next_state)
            report.generated_at = batch.reissued_at
            report.run_status = "approval_required"
            self.store.write_json(batch.run_id, "state.json", machine.snapshot())
            self.store.write_json(batch.run_id, "approvals.json", report.approvals)
            self.store.write_json(batch.run_id, "final_report.json", report)
            self.store.write_text(
                batch.run_id,
                "final_report.md",
                render_markdown(report),
            )
            try:
                published = self._publish_buffer(
                    batch.run_id,
                    report,
                    previous_read=read,
                    transaction_id_override=approval_reissue_transaction_id(
                        batch.run_id,
                        batch.idempotency_key,
                        approval_reissue_batch_sha256(batch),
                    ),
                )
            except ProductRunError as exc:
                try:
                    winner = self.product_store.read_current(batch.run_id)
                    winner_names = {payload.inventory.logical_name for payload in winner.payloads}
                    winner_state = (
                        self._read_approval_state(winner)
                        if "approval_ledger_v2.json" in winner_names
                        else None
                    )
                    winner_legacy = (
                        ()
                        if winner_state is not None
                        else tuple(
                            FinalReport.model_validate_json(
                                winner.payload_bytes("final_report.json")
                            ).approvals
                        )
                    )
                    replay = validate_approval_reissue(
                        winner_state,
                        winner_legacy,
                        batch,
                        observed_run_revision=winner.revision,
                        previous_manifest_sha256=winner.manifest_sha256,
                        previous_pointer_sha256=winner.current_pointer_sha256,
                    )
                    if replay.kind == "replay" and replay.replay_outcome is not None:
                        return replay.replay_outcome
                except ApprovalDecisionValidationError as replay_error:
                    self._raise_audited_reissue_failure(batch, replay_error.failure)
                except (ProductRunError, ValueError):
                    pass
                self._raise_audited_reissue_failure(
                    batch,
                    self._reissue_failure_from_product_error(batch, exc),
                )
            committed_state = self._read_approval_state(published)
            committed = next(
                (
                    record.outcome
                    for record in committed_state.reissue_records
                    if record.idempotency_key == batch.idempotency_key
                ),
                None,
            )
            if committed != update.outcome:
                failure = approval_failure_v2(
                    ApprovalFailureCode.RESUME_TRANSACTION_FAILED,
                    "Published reissue outcome could not be verified.",
                    run_id=batch.run_id,
                    decision_id=batch.reissue_id,
                    idempotency_key=batch.idempotency_key,
                    observed_run_revision=published.revision,
                    observed_ledger_revision=committed_state.ledger.ledger_revision,
                )
                self._raise_audited_reissue_failure(batch, failure)
            return update.outcome
        except ProductRunError as exc:
            self._raise_audited_reissue_failure(
                batch,
                self._reissue_failure_from_product_error(batch, exc),
            )
        except ApprovalDecisionValidationError as exc:
            if exc.failure.audit_confirmed:
                raise
            self._raise_audited_reissue_failure(batch, exc.failure)
        except ValueError:
            failure = approval_failure_v2(
                ApprovalFailureCode.RESUME_TRANSACTION_FAILED,
                "Approval reissue construction failed without an external effect.",
                run_id=batch.run_id,
                decision_id=batch.reissue_id,
                idempotency_key=batch.idempotency_key,
                observed_run_revision=batch.expected_run_revision,
                observed_ledger_revision=batch.expected_ledger_revision,
            )
            self._raise_audited_reissue_failure(batch, failure)

    def decide_approvals(
        self,
        batch: ApprovalDecisionBatch,
    ) -> ApprovalDecisionOutcome:
        """Validate and publish one all-or-nothing authoritative V2 decision."""

        try:
            read = self._read_product(batch.run_id)
            try:
                state = self._read_approval_state(read)
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
            self._approval_v2_payloads[batch.run_id] = self._approval_payload_map(
                update.ledger,
                update.decision_records,
                update.domain_audit_events,
                state.reissue_records,
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
            next_payloads = self._approval_payload_map(
                update.ledger,
                update.decision_records,
                update.domain_audit_events,
                state.reissue_records,
            )
            next_state = read_approval_state_v2(
                next_payloads["approval_ledger_v2.json"],
                next_payloads["approval_decisions_v2.jsonl"],
                next_payloads["approval_audit_v2.jsonl"],
                next_payloads.get(self._APPROVAL_REISSUE_ARTIFACT, b""),
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
                    winner_state = self._read_approval_state(winner)
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
            committed_state = self._read_approval_state(published)
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
        reissue_batch: ApprovalReissueBatchV2 | None = None,
    ) -> FinalReport:
        if decision_batch is not None and reissue_batch is not None:
            raise ValueError("decision_batch cannot be combined with reissue_batch")
        if reissue_batch is not None:
            if approve_ids or reject_ids:
                raise ValueError("reissue_batch cannot be combined with approve_ids/reject_ids")
            if reissue_batch.run_id != run_id:
                raise ValueError("reissue_batch run_id mismatch")
            self.reissue_approvals(reissue_batch)
            return self.load_report(run_id)
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
            state = self._read_approval_state(read)
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


_CONFIRMED_ORCHESTRATOR_MODULE_CALLABLES = _module_callable_snapshot()
