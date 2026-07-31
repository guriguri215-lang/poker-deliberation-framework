"""Approval/context/budget/storage coordinator for P2-028A isolated jobs."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from poker_deliberation.approval_canonical import action_digest_sha256
from poker_deliberation.approval_models import (
    ApprovalExecutionRecheckBindingV2,
    CanonicalActionPlanV2,
)
from poker_deliberation.approvals import (
    ApprovalExecutionValidationError,
    DecisionAuthorityProvider,
    read_approval_state_v2,
    recheck_approval_for_execution,
)
from poker_deliberation.budgets.durable_models import (
    CancellationState,
    ExecutionLineageV1,
    PermitStatus,
    ResourceAmountsV1,
    ResourceReservationV1,
    SettlementStatus,
    canonical_durable_sha256,
)
from poker_deliberation.budgets.durable_store import (
    DurableBudgetError,
    DurableBudgetStore,
    build_resource_reservation,
)
from poker_deliberation.budgets.execution import (
    IsolationRequirementV1,
    RM028IsolationEvidenceV1,
)
from poker_deliberation.budgets.retry import FailureCategory
from poker_deliberation.context_lifecycle import (
    ContextEnvelope,
    ContextLifecycleError,
    validate_context_envelope,
)
from poker_deliberation.isolated_jobs.canonical import (
    build_action_plan,
    isolated_job_sha256,
)
from poker_deliberation.isolated_jobs.identity import build_execution_identity, sha256_file
from poker_deliberation.isolated_jobs.models import (
    TERMINAL_JOB_STATUSES,
    ApprovalJobReferenceV1,
    BudgetJobBindingV1,
    ContextJobBindingV1,
    FilesystemPolicyV1,
    IsolatedJobError,
    IsolatedJobPolicyV1,
    IsolatedJobRequestV1,
    IsolatedJobResultV1,
    IsolatedJobStatus,
    JobFailureCode,
    JobLimitsV1,
    ReconciliationReferenceV1,
)
from poker_deliberation.isolated_jobs.paths import (
    canonical_existing_path,
    directory_identity,
    file_identity,
)
from poker_deliberation.isolated_jobs.store import IsolatedJobStore
from poker_deliberation.isolated_jobs.windows_backend import (
    PreparationLease,
    PreparedWindowsJob,
    WindowsJobBackend,
    WindowsJobOutcome,
)
from poker_deliberation.schemas import AgentAssignment
from poker_deliberation.storage.terminal_models import ProductRunError
from poker_deliberation.storage.terminal_store import TerminalRunStore


@dataclass(frozen=True, slots=True)
class JobAdmissionPreview:
    action_plan: CanonicalActionPlanV2
    context_binding: ContextJobBindingV1
    budget_binding: BudgetJobBindingV1
    reservation: ResourceReservationV1
    isolation_requirement: IsolationRequirementV1
    isolation_evidence: RM028IsolationEvidenceV1


def _operation_id(execution_id: str, operation: str) -> str:
    digest = hashlib.sha256(f"{execution_id}:{operation}".encode()).hexdigest()
    return f"job-{digest[:24]}-{operation}"


def qualify_isolated_job_policy(
    limits: JobLimitsV1,
    *,
    workspace_root: Path,
    approved_input: Path | None = None,
) -> IsolatedJobPolicyV1:
    """Build an exact policy from verified local identities without launching."""

    workspace = directory_identity(workspace_root)
    input_identity = None
    if approved_input is not None:
        candidate = canonical_existing_path(approved_input, directory=False)
        workspace_path = Path(workspace.absolute_path)
        try:
            common = Path(os.path.commonpath((workspace_path, candidate)))
        except ValueError as exc:
            raise ValueError("approved input is outside the isolated workspace volume") from exc
        if os.path.normcase(str(common)) != os.path.normcase(str(workspace_path)):
            raise ValueError("approved input must be beneath the isolated workspace")
        input_identity = file_identity(
            candidate,
            sha256=sha256_file(candidate),
            require_single_link=True,
        )
    return IsolatedJobPolicyV1(
        limits=limits,
        execution_identity=build_execution_identity(),
        filesystem=FilesystemPolicyV1(
            workspace_root=workspace,
            approved_input=input_identity,
            input_handle_required=input_identity is not None,
        ),
    )


class TerminalApprovalVerifier:
    """P2-012B verified reader plus P2-013B live execution recheck."""

    def __init__(self, terminal_store: TerminalRunStore) -> None:
        self.terminal_store = terminal_store

    def verify(
        self,
        *,
        approval_run_id: str,
        request_id: str,
        expected_action_plan: CanonicalActionPlanV2,
        authority_provider: DecisionAuthorityProvider,
        evaluated_at: datetime,
    ) -> tuple[ApprovalJobReferenceV1, ApprovalExecutionRecheckBindingV2]:
        try:
            read = self.terminal_store.read_current(approval_run_id)
            names = {item.inventory.logical_name for item in read.payloads}
            reissue_bytes = (
                read.payload_bytes("approval_reissues_v2.jsonl")
                if "approval_reissues_v2.jsonl" in names
                else b""
            )
            approval_state = read_approval_state_v2(
                read.payload_bytes("approval_ledger_v2.json"),
                read.payload_bytes("approval_decisions_v2.jsonl"),
                read.payload_bytes("approval_audit_v2.jsonl"),
                reissue_bytes,
            )
            binding = recheck_approval_for_execution(
                approval_state,
                approval_run_id=approval_run_id,
                approval_run_revision=read.revision,
                approval_pointer_sha256=read.current_pointer_sha256,
                approval_manifest_sha256=read.manifest_sha256,
                request_id=request_id,
                expected_action_plan=expected_action_plan,
                authority_provider=authority_provider,
                evaluated_at=evaluated_at,
            )
            reference = ApprovalJobReferenceV1(
                approval_run_id=approval_run_id,
                approval_revision=read.revision,
                approval_pointer_sha256=read.current_pointer_sha256,
                approval_manifest_sha256=read.manifest_sha256,
                request_id=request_id,
            )
            return reference, binding
        except ApprovalExecutionValidationError:
            raise
        except (ProductRunError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(JobFailureCode.APPROVAL_MISSING.value) from exc


class IsolatedJobCoordinator:
    """Execute exactly one approved fixed-helper attempt with no automatic retry."""

    def __init__(
        self,
        job_store: IsolatedJobStore,
        budget_store: DurableBudgetStore,
        terminal_store: TerminalRunStore,
        *,
        backend: WindowsJobBackend | None = None,
        clock: Callable[[], datetime],
    ) -> None:
        self.job_store = job_store
        self.budget_store = budget_store
        self.approvals = TerminalApprovalVerifier(terminal_store)
        self.backend = backend or WindowsJobBackend()
        self.clock = clock

    def _context_binding(
        self,
        request: IsolatedJobRequestV1,
        envelope: ContextEnvelope,
        assignment: AgentAssignment,
        lineage: ExecutionLineageV1,
        *,
        now: datetime,
    ) -> ContextJobBindingV1:
        validate_context_envelope(
            envelope,
            assignment,
            run_id=request.run_id,
            expected_context_id=request.context_id,
            attempt_id=request.attempt_id,
            now=now,
            expected_parent_context_id=lineage.parent_context_id,
            expected_source_sha256=(
                lineage.context_source_sha256 if lineage.parent_context_id is not None else None
            ),
        )
        if (
            envelope.lineage.assignment_id != assignment.assignment_id
            or lineage.assignment_id != assignment.assignment_id
            or lineage.role != assignment.agent_role
            or lineage.owner_kind.value != "internal"
            or lineage.owner_id != "p2-028a"
            or lineage.phase_id != "isolated_job"
            or lineage.root_context_id
            != (
                envelope.lineage.context_id
                if lineage.parent_context_id is None
                else lineage.root_context_id
            )
            or lineage.parent_context_id != envelope.lineage.parent_context_id
            or lineage.context_id != envelope.lineage.context_id
            or lineage.attempt_id != envelope.lineage.attempt_id
        ):
            raise ContextLifecycleError("context and budget execution lineage mismatch")
        return ContextJobBindingV1(
            context_id=envelope.lineage.context_id,
            attempt_id=envelope.lineage.attempt_id,
            assignment_id=assignment.assignment_id,
            role=assignment.agent_role,
            root_attempt_id=lineage.root_attempt_id,
            parent_attempt_id=lineage.parent_attempt_id,
            root_context_id=lineage.root_context_id,
            parent_context_id=envelope.lineage.parent_context_id,
            payload_sha256=envelope.payload_sha256,
            source_sha256=envelope.lineage.source_sha256,
            policy_sha256=envelope.policy_sha256,
            integrity_sha256=envelope.integrity_sha256,
            expires_at=envelope.policy.expires_at,
        )

    def _reservation(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
    ) -> ResourceReservationV1:
        input_bytes = (
            0
            if policy.filesystem.approved_input is None
            else policy.filesystem.approved_input.size_bytes
        )
        controller_runtime_ns = (policy.limits.wall_clock_ms + 10_000) * 1_000_000
        return build_resource_reservation(
            reservation_id=_operation_id(request.execution_id, "reservation"),
            requested=ResourceAmountsV1(
                active_runtime_ns=controller_runtime_ns,
                tool_attempts=1,
                tool_input_bytes=input_bytes,
                tool_output_bytes=policy.limits.combined_output_bytes,
                artifact_bytes=max(
                    policy.limits.stdout_bytes,
                    policy.limits.stderr_bytes,
                ),
                run_bytes=policy.limits.combined_output_bytes,
                concurrency_slots=1,
            ),
        )

    def _budget_binding(
        self,
        request: IsolatedJobRequestV1,
        reservation: ResourceReservationV1,
        lineage: ExecutionLineageV1,
        context: ContextJobBindingV1,
        isolation_requirement: IsolationRequirementV1,
        isolation_evidence: RM028IsolationEvidenceV1,
    ) -> BudgetJobBindingV1:
        state = self.budget_store.load(request.budget_run_id)
        if (
            lineage.attempt_id != request.attempt_id
            or lineage.context_id != request.context_id
            or lineage.assignment_id != context.assignment_id
            or lineage.role != context.role
            or lineage.root_attempt_id != context.root_attempt_id
            or lineage.parent_attempt_id != context.parent_attempt_id
            or lineage.root_context_id != context.root_context_id
            or lineage.parent_context_id != context.parent_context_id
            or lineage.owner_kind.value != "internal"
            or lineage.owner_id != "p2-028a"
            or lineage.phase_id != "isolated_job"
            or lineage.execution_ordinal != request.execution_ordinal
            or lineage.idempotency_key != request.execution_id
            or lineage.idempotency_request_sha256 != isolated_job_sha256(request)
            or lineage.context_source_sha256 != context.source_sha256
            or lineage.context_policy_sha256 != context.policy_sha256
            or lineage.context_integrity_sha256 != context.integrity_sha256
        ):
            raise ValueError(JobFailureCode.BUDGET_MISMATCH.value)
        return BudgetJobBindingV1(
            budget_run_id=request.budget_run_id,
            permit_id=request.budget_permit_id,
            policy_sha256=state.policy_sha256,
            activation_sha256=state.activation_sha256,
            reservation_sha256=canonical_durable_sha256(reservation),
            lineage_sha256=canonical_durable_sha256(lineage),
            isolation_requirement_sha256=isolation_requirement.request_sha256,
            isolation_evidence_sha256=isolation_evidence.isolation_evidence_sha256,
            isolation_boundary_id=isolation_evidence.boundary_id,
        )

    def preview(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
        *,
        context_envelope: ContextEnvelope,
        assignment: AgentAssignment,
        budget_lineage: ExecutionLineageV1,
        action_expires_at: datetime,
    ) -> JobAdmissionPreview:
        now = self.clock()
        context = self._context_binding(
            request,
            context_envelope,
            assignment,
            budget_lineage,
            now=now,
        )
        if action_expires_at > context.expires_at or action_expires_at <= now:
            raise ContextLifecycleError("action expiry exceeds the validated context lifetime")
        if (request.operation.value == "copy_handles") != (
            policy.filesystem.approved_input is not None
        ):
            raise ValueError("copy_handles and approved input must be paired")
        reservation = self._reservation(request, policy)
        isolation_requirement = IsolationRequirementV1(
            process_tree_termination=True,
            os_resource_isolation=True,
        )
        isolation_evidence = self.backend.inspect(isolation_requirement)
        if not isolation_evidence.satisfies(isolation_requirement):
            raise ValueError(JobFailureCode.IDENTITY_MISMATCH.value)
        budget = self._budget_binding(
            request,
            reservation,
            budget_lineage,
            context,
            isolation_requirement,
            isolation_evidence,
        )
        action_plan = build_action_plan(
            request,
            policy,
            context,
            budget,
            expires_at=action_expires_at,
        )
        return JobAdmissionPreview(
            action_plan=action_plan,
            context_binding=context,
            budget_binding=budget,
            reservation=reservation,
            isolation_requirement=isolation_requirement,
            isolation_evidence=isolation_evidence,
        )

    def _result_from_state(self, execution_id: str) -> IsolatedJobResultV1:
        state = self.job_store.load(execution_id)
        stdout, stderr = self.job_store.load_outputs(execution_id)
        return IsolatedJobResultV1(
            execution_id=execution_id,
            status=state.status,
            state_sha256=state.canonical_sha256,
            stdout=stdout,
            stderr=stderr,
            failure_code=state.failure_code,
        )

    def _release_reserved_no_effect(
        self,
        request: IsolatedJobRequestV1,
        *,
        evidence_sha256: str,
    ) -> None:
        self.budget_store.release_no_effect(
            request.budget_run_id,
            operation_id=_operation_id(request.execution_id, "release"),
            settlement_id=_operation_id(request.execution_id, "settlement"),
            permit_id=request.budget_permit_id,
            evidence_sha256=evidence_sha256,
        )

    def _close_known_no_effect(
        self,
        request: IsolatedJobRequestV1,
        *,
        evidence_sha256: str,
    ) -> None:
        state = self.budget_store.load(request.budget_run_id)
        permit = next(
            (item for item in state.active_permits if item.permit_id == request.budget_permit_id),
            None,
        )
        if permit is None:
            settlement = next(
                (item for item in state.settlements if item.permit_id == request.budget_permit_id),
                None,
            )
            if settlement is not None and settlement.status in {
                SettlementStatus.FAILED,
                SettlementStatus.RELEASED_NO_EFFECT,
            }:
                return
            raise ValueError("known-no-effect permit is missing without settlement")
        if permit.status is PermitStatus.RESERVED:
            self._release_reserved_no_effect(
                request,
                evidence_sha256=evidence_sha256,
            )
            return
        self.budget_store.settle(
            request.budget_run_id,
            operation_id=_operation_id(request.execution_id, "settle-known-no-effect"),
            settlement_id=_operation_id(request.execution_id, "settlement"),
            permit_id=request.budget_permit_id,
            actual=self._started_actual(request),
            status=SettlementStatus.FAILED,
            effect_evidence_sha256=evidence_sha256,
            failure_category=FailureCategory.POLICY,
            observed_peak_concurrency=1,
        )

    def _settle(
        self,
        request: IsolatedJobRequestV1,
        outcome: WindowsJobOutcome,
        *,
        status: SettlementStatus,
        failure_category: FailureCategory | None,
    ) -> SettlementStatus:
        evidence_sha = isolated_job_sha256(outcome.evidence)
        result_sha = isolated_job_sha256(
            {
                "stdout_sha256": outcome.evidence.stdout_sha256,
                "stderr_sha256": outcome.evidence.stderr_sha256,
                "exit_code": outcome.evidence.exit_code,
            }
        )
        actual = self._started_actual(
            request,
            tool_output_bytes=len(outcome.stdout) + len(outcome.stderr),
            artifact_bytes=max(len(outcome.stdout), len(outcome.stderr)),
            run_bytes=len(outcome.stdout) + len(outcome.stderr),
        )
        result = self.budget_store.settle(
            request.budget_run_id,
            operation_id=_operation_id(request.execution_id, "settle"),
            settlement_id=_operation_id(request.execution_id, "settlement"),
            permit_id=request.budget_permit_id,
            actual=actual,
            status=status,
            result_sha256=result_sha if status is SettlementStatus.SUCCEEDED else None,
            effect_evidence_sha256=evidence_sha,
            cancellation_evidence_sha256=(
                evidence_sha if status is SettlementStatus.CANCELLED else None
            ),
            failure_category=failure_category,
            observed_peak_concurrency=1,
        )
        return result.state.settlements[-1].status

    def _started_actual(
        self,
        request: IsolatedJobRequestV1,
        *,
        tool_output_bytes: int = 0,
        artifact_bytes: int = 0,
        run_bytes: int = 0,
    ) -> ResourceAmountsV1:
        policy = self.job_store.load(request.execution_id).policy
        approved_input = policy.filesystem.approved_input
        return ResourceAmountsV1(
            tool_attempts=1,
            tool_input_bytes=0 if approved_input is None else approved_input.size_bytes,
            tool_output_bytes=tool_output_bytes,
            artifact_bytes=artifact_bytes,
            run_bytes=run_bytes,
            concurrency_slots=1,
        )

    def _recovery_actual(self, request: IsolatedJobRequestV1) -> ResourceAmountsV1:
        current = self.job_store.load(request.execution_id)
        if current.evidence is None:
            return self._started_actual(request)
        stdout, stderr = self.job_store.load_outputs(request.execution_id)
        return self._started_actual(
            request,
            tool_output_bytes=len(stdout) + len(stderr),
            artifact_bytes=max(len(stdout), len(stderr)),
            run_bytes=len(stdout) + len(stderr),
        )

    def _record_budget_cancellation(
        self,
        request: IsolatedJobRequestV1,
        outcome: WindowsJobOutcome,
    ) -> bool:
        evidence_sha = isolated_job_sha256(outcome.evidence)
        state = self.budget_store.load(request.budget_run_id)
        cancellation = next(
            (item for item in state.cancellations if item.permit_id == request.budget_permit_id),
            None,
        )
        if cancellation is None:
            return False
        if cancellation.state is CancellationState.REQUESTED:
            self.budget_store.record_cancellation(
                request.budget_run_id,
                operation_id=_operation_id(request.execution_id, "cancel-ack"),
                permit_id=request.budget_permit_id,
                state_value=CancellationState.ACKNOWLEDGED,
                evidence_sha256=evidence_sha,
                worker_live=False,
            )
            state = self.budget_store.load(request.budget_run_id)
            cancellation = next(
                item for item in state.cancellations if item.permit_id == request.budget_permit_id
            )
        if cancellation.state is CancellationState.ACKNOWLEDGED:
            cancellation_evidence = cancellation.evidence_sha256 or evidence_sha
            self.budget_store.record_cancellation(
                request.budget_run_id,
                operation_id=_operation_id(request.execution_id, "cancel-confirm"),
                permit_id=request.budget_permit_id,
                state_value=CancellationState.CANCELLED,
                evidence_sha256=cancellation_evidence,
                worker_live=False,
            )
            state = self.budget_store.load(request.budget_run_id)
            cancellation = next(
                item for item in state.cancellations if item.permit_id == request.budget_permit_id
            )
        return cancellation.state is CancellationState.CANCELLED

    def _close_cancellation_as_effect_unknown(
        self,
        request: IsolatedJobRequestV1,
        *,
        evidence_sha256: str,
        worker_live: bool,
    ) -> None:
        state = self.budget_store.load(request.budget_run_id)
        cancellation = next(
            (item for item in state.cancellations if item.permit_id == request.budget_permit_id),
            None,
        )
        if cancellation is None:
            return
        if cancellation.state in {
            CancellationState.REQUESTED,
            CancellationState.UNCONFIRMED,
        }:
            self.budget_store.record_cancellation(
                request.budget_run_id,
                operation_id=_operation_id(request.execution_id, "cancel-unknown"),
                permit_id=request.budget_permit_id,
                state_value=CancellationState.EFFECT_UNKNOWN,
                evidence_sha256=evidence_sha256,
                worker_live=worker_live,
            )
        elif (
            cancellation.state is CancellationState.EFFECT_UNKNOWN
            and cancellation.worker_live
            and not worker_live
        ):
            self.budget_store.record_cancellation(
                request.budget_run_id,
                operation_id=_operation_id(request.execution_id, "cancel-unknown-closed"),
                permit_id=request.budget_permit_id,
                state_value=CancellationState.EFFECT_UNKNOWN,
                evidence_sha256=cancellation.evidence_sha256,
                worker_live=False,
            )

    def _settle_ambiguous_resume(
        self,
        request: IsolatedJobRequestV1,
        *,
        evidence_sha256: str,
        worker_live: bool = False,
    ) -> None:
        state = self.budget_store.load(request.budget_run_id)
        permit = next(
            (item for item in state.active_permits if item.permit_id == request.budget_permit_id),
            None,
        )
        if permit is None:
            return
        if permit.status is PermitStatus.RESERVED:
            self._release_reserved_no_effect(
                request,
                evidence_sha256=evidence_sha256,
            )
            return
        self._close_cancellation_as_effect_unknown(
            request,
            evidence_sha256=evidence_sha256,
            worker_live=worker_live,
        )
        if worker_live:
            return
        self.budget_store.settle(
            request.budget_run_id,
            operation_id=_operation_id(request.execution_id, "settle-resume-unknown"),
            settlement_id=_operation_id(request.execution_id, "settlement"),
            permit_id=request.budget_permit_id,
            actual=self._started_actual(request),
            status=SettlementStatus.EFFECT_UNKNOWN,
            effect_evidence_sha256=evidence_sha256,
            failure_category=FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
            observed_peak_concurrency=1,
        )

    def _map_failure_category(self, code: JobFailureCode) -> FailureCategory:
        if code is JobFailureCode.CANCELLED:
            return FailureCategory.CANCEL
        if code in {
            JobFailureCode.WALL_CLOCK_LIMIT,
            JobFailureCode.CPU_LIMIT,
        }:
            return FailureCategory.DEADLINE
        if code in {
            JobFailureCode.MEMORY_LIMIT,
            JobFailureCode.PROCESS_LIMIT,
            JobFailureCode.STDOUT_LIMIT,
            JobFailureCode.STDERR_LIMIT,
            JobFailureCode.COMBINED_OUTPUT_LIMIT,
        }:
            return FailureCategory.BUDGET
        if code is JobFailureCode.EFFECT_UNKNOWN:
            return FailureCategory.EXTERNAL_EFFECT_UNKNOWN
        return FailureCategory.TOOL_DETERMINISTIC

    def _settle_outcome(
        self,
        request: IsolatedJobRequestV1,
        outcome: WindowsJobOutcome,
    ) -> tuple[IsolatedJobStatus, JobFailureCode | None]:
        tree_termination_confirmed = (
            outcome.evidence.active_processes == 0
            and outcome.evidence.process_tree_termination_confirmed
        )
        if not tree_termination_confirmed:
            self._close_cancellation_as_effect_unknown(
                request,
                evidence_sha256=isolated_job_sha256(outcome.evidence),
                worker_live=True,
            )
            return IsolatedJobStatus.EFFECT_UNKNOWN, JobFailureCode.EFFECT_UNKNOWN
        if outcome.cancelled:
            cancellation_confirmed = self._record_budget_cancellation(request, outcome)
            if not cancellation_confirmed:
                evidence_sha = isolated_job_sha256(outcome.evidence)
                self._close_cancellation_as_effect_unknown(
                    request,
                    evidence_sha256=evidence_sha,
                    worker_live=False,
                )
                self._settle(
                    request,
                    outcome,
                    status=SettlementStatus.EFFECT_UNKNOWN,
                    failure_category=FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
                )
                return IsolatedJobStatus.EFFECT_UNKNOWN, JobFailureCode.EFFECT_UNKNOWN
            settled = self._settle(
                request,
                outcome,
                status=SettlementStatus.CANCELLED,
                failure_category=FailureCategory.CANCEL,
            )
            if settled is SettlementStatus.CANCELLED:
                return IsolatedJobStatus.CANCELLED, JobFailureCode.CANCELLED
            return IsolatedJobStatus.EFFECT_UNKNOWN, JobFailureCode.EFFECT_UNKNOWN
        if outcome.failure_code is JobFailureCode.EFFECT_UNKNOWN:
            self._close_cancellation_as_effect_unknown(
                request,
                evidence_sha256=isolated_job_sha256(outcome.evidence),
                worker_live=False,
            )
            self._settle(
                request,
                outcome,
                status=SettlementStatus.EFFECT_UNKNOWN,
                failure_category=FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
            )
            return IsolatedJobStatus.EFFECT_UNKNOWN, JobFailureCode.EFFECT_UNKNOWN
        if outcome.failure_code is None:
            settled = self._settle(
                request,
                outcome,
                status=SettlementStatus.SUCCEEDED,
                failure_category=None,
            )
            if settled is SettlementStatus.SUCCEEDED:
                return IsolatedJobStatus.COMPLETED, None
            return IsolatedJobStatus.FAILED, JobFailureCode.BUDGET_MISMATCH
        self._settle(
            request,
            outcome,
            status=SettlementStatus.FAILED,
            failure_category=self._map_failure_category(outcome.failure_code),
        )
        return IsolatedJobStatus.FAILED, outcome.failure_code

    def _budget_is_terminal(self, request: IsolatedJobRequestV1) -> bool:
        state = self.budget_store.load(request.budget_run_id)
        active = any(
            permit.permit_id == request.budget_permit_id for permit in state.active_permits
        )
        settled = any(
            settlement.permit_id == request.budget_permit_id for settlement in state.settlements
        )
        return not active and settled

    def _recover_nonterminal(
        self,
        request: IsolatedJobRequestV1,
    ) -> IsolatedJobResultV1:
        current = self.job_store.load(request.execution_id)
        evidence_sha = isolated_job_sha256(
            {
                "execution_id": request.execution_id,
                "observed_status": current.status.value,
                "recovery": "new_coordinator_nonterminal_effect_unknown",
            }
        )
        try:
            budget_state = self.budget_store.load(request.budget_run_id)
            permit = next(
                (
                    item
                    for item in budget_state.active_permits
                    if item.permit_id == request.budget_permit_id
                ),
                None,
            )
            if permit is not None and permit.status is PermitStatus.RESERVED:
                self._release_reserved_no_effect(
                    request,
                    evidence_sha256=evidence_sha,
                )
            elif permit is not None:
                cancellation = next(
                    (
                        item
                        for item in budget_state.cancellations
                        if item.permit_id == request.budget_permit_id
                    ),
                    None,
                )
                if cancellation is not None and cancellation.state in {
                    CancellationState.ACKNOWLEDGED,
                    CancellationState.CANCELLED,
                }:
                    cancellation_evidence = cancellation.evidence_sha256
                    assert cancellation_evidence is not None
                    if cancellation.state is CancellationState.ACKNOWLEDGED:
                        self.budget_store.record_cancellation(
                            request.budget_run_id,
                            operation_id=_operation_id(
                                request.execution_id,
                                "cancel-confirm",
                            ),
                            permit_id=request.budget_permit_id,
                            state_value=CancellationState.CANCELLED,
                            evidence_sha256=cancellation_evidence,
                            worker_live=False,
                        )
                    self.budget_store.settle(
                        request.budget_run_id,
                        operation_id=_operation_id(
                            request.execution_id,
                            "settle-cancel-recovery",
                        ),
                        settlement_id=_operation_id(request.execution_id, "settlement"),
                        permit_id=request.budget_permit_id,
                        actual=self._recovery_actual(request),
                        status=SettlementStatus.CANCELLED,
                        effect_evidence_sha256=cancellation_evidence,
                        cancellation_evidence_sha256=cancellation_evidence,
                        failure_category=FailureCategory.CANCEL,
                        observed_peak_concurrency=1,
                    )
                else:
                    self._close_cancellation_as_effect_unknown(
                        request,
                        evidence_sha256=evidence_sha,
                        worker_live=False,
                    )
                    self.budget_store.settle(
                        request.budget_run_id,
                        operation_id=_operation_id(request.execution_id, "settle-unknown"),
                        settlement_id=_operation_id(request.execution_id, "settlement"),
                        permit_id=request.budget_permit_id,
                        actual=self._recovery_actual(request),
                        status=SettlementStatus.EFFECT_UNKNOWN,
                        effect_evidence_sha256=evidence_sha,
                        failure_category=FailureCategory.EXTERNAL_EFFECT_UNKNOWN,
                        observed_peak_concurrency=1,
                    )
        except (DurableBudgetError, ValueError):
            pass
        if current.status is not IsolatedJobStatus.EFFECT_UNKNOWN:
            self.job_store.transition(
                request.execution_id,
                status=IsolatedJobStatus.EFFECT_UNKNOWN,
                reason_code="restart_nonterminal",
                failure_code=JobFailureCode.EFFECT_UNKNOWN,
            )
        return self._result_from_state(request.execution_id)

    def execute(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
        *,
        context_envelope: ContextEnvelope,
        assignment: AgentAssignment,
        budget_lineage: ExecutionLineageV1,
        action_expires_at: datetime,
        approval_run_id: str,
        approval_request_id: str,
        authority_provider: DecisionAuthorityProvider,
        cancelled: Callable[[], bool] | None = None,
    ) -> IsolatedJobResultV1:
        preparation_lease = self.backend.new_preparation_lease(request.execution_id)
        try:
            return self._execute_impl(
                request,
                policy,
                preparation_lease=preparation_lease,
                context_envelope=context_envelope,
                assignment=assignment,
                budget_lineage=budget_lineage,
                action_expires_at=action_expires_at,
                approval_run_id=approval_run_id,
                approval_request_id=approval_request_id,
                authority_provider=authority_provider,
                cancelled=cancelled,
            )
        finally:
            preparation_lease.abort_and_join()

    def _execute_impl(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
        *,
        preparation_lease: PreparationLease,
        context_envelope: ContextEnvelope,
        assignment: AgentAssignment,
        budget_lineage: ExecutionLineageV1,
        action_expires_at: datetime,
        approval_run_id: str,
        approval_request_id: str,
        authority_provider: DecisionAuthorityProvider,
        cancelled: Callable[[], bool] | None = None,
    ) -> IsolatedJobResultV1:
        run_path = self.job_store.revisions.runs_root / request.execution_id
        if run_path.exists():
            current = self.job_store.load(request.execution_id)
            if current.request != request or current.policy != policy:
                raise ValueError(JobFailureCode.STALE_REPLAY.value)
            if current.status in TERMINAL_JOB_STATUSES:
                return self._result_from_state(request.execution_id)
            raise ValueError(JobFailureCode.RUN_LOCKED.value)

        preview = self.preview(
            request,
            policy,
            context_envelope=context_envelope,
            assignment=assignment,
            budget_lineage=budget_lineage,
            action_expires_at=action_expires_at,
        )
        action_digest = action_digest_sha256(preview.action_plan)
        try:
            approval_reference, initial_recheck = self.approvals.verify(
                approval_run_id=approval_run_id,
                request_id=approval_request_id,
                expected_action_plan=preview.action_plan,
                authority_provider=authority_provider,
                evaluated_at=self.clock(),
            )
        except (ApprovalExecutionValidationError, ValueError) as exc:
            raise ValueError(JobFailureCode.APPROVAL_MISMATCH.value) from exc

        try:
            self.budget_store.reserve(
                request.budget_run_id,
                operation_id=_operation_id(request.execution_id, "reserve"),
                permit_id=request.budget_permit_id,
                reservation=preview.reservation,
                lineage=budget_lineage,
                expected_policy_sha256=preview.budget_binding.policy_sha256,
                expected_activation_sha256=preview.budget_binding.activation_sha256,
            )
        except DurableBudgetError as exc:
            raise ValueError(exc.failure.code.value) from exc
        try:
            self.job_store.create(
                request,
                policy,
                action_digest_sha256=action_digest,
                context_binding=preview.context_binding,
                budget_binding=preview.budget_binding,
                approval_reference=approval_reference,
                approval_recheck_binding_sha256=initial_recheck.binding_sha256,
            )
        except IsolatedJobError as exc:
            if exc.code in {
                JobFailureCode.RUN_LOCKED,
                JobFailureCode.STALE_REPLAY,
            }:
                raise ValueError(JobFailureCode.RUN_LOCKED.value) from exc
            try:
                current = self.job_store.load(request.execution_id)
            except IsolatedJobError:
                evidence_sha = isolated_job_sha256(
                    {
                        "execution_id": request.execution_id,
                        "stage": "prepared_publication_failed",
                    }
                )
                with suppress(DurableBudgetError):
                    self._release_reserved_no_effect(
                        request,
                        evidence_sha256=evidence_sha,
                    )
                raise ValueError(JobFailureCode.STORAGE_FAILURE.value) from exc
            if current.request == request and current.policy == policy:
                raise ValueError(JobFailureCode.RUN_LOCKED.value) from exc
            raise ValueError(JobFailureCode.STALE_REPLAY.value) from exc

        prepared: PreparedWindowsJob | None = None
        try:
            prepared = self.backend._prepare_with_lease(
                request,
                policy,
                preparation_lease,
            )
        except BaseException as exc:
            failure_code = (
                exc.code if isinstance(exc, IsolatedJobError) else JobFailureCode.IDENTITY_MISMATCH
            )
            evidence_sha = isolated_job_sha256(
                {
                    "execution_id": request.execution_id,
                    "stage": "backend_prepare",
                }
            )
            try:
                self._release_reserved_no_effect(
                    request,
                    evidence_sha256=evidence_sha,
                )
                self.job_store.transition(
                    request.execution_id,
                    status=IsolatedJobStatus.FAILED,
                    reason_code="prelaunch_refused",
                    failure_code=failure_code,
                )
            except Exception:
                with suppress(Exception):
                    self.job_store.transition(
                        request.execution_id,
                        status=IsolatedJobStatus.EFFECT_UNKNOWN,
                        reason_code="prelaunch_durability_unknown",
                        failure_code=JobFailureCode.EFFECT_UNKNOWN,
                    )
            if not isinstance(exc, Exception):
                raise
            raise ValueError(failure_code.value) from exc

        try:
            self.job_store.transition(
                request.execution_id,
                status=IsolatedJobStatus.LAUNCH_COMMITTED,
                reason_code="suspended_child_assigned",
                process_id=prepared.process_id,
                process_creation_time_100ns=prepared.creation_time_100ns,
            )
        except BaseException as exc:
            prepared.terminate_before_resume()
            evidence_sha = isolated_job_sha256(
                {
                    "execution_id": request.execution_id,
                    "stage": "launch_commit_publication_failed",
                }
            )
            release_succeeded = False
            try:
                self._release_reserved_no_effect(
                    request,
                    evidence_sha256=evidence_sha,
                )
                release_succeeded = True
            except DurableBudgetError:
                pass
            try:
                self.job_store.load(request.execution_id)
                self.job_store.transition(
                    request.execution_id,
                    status=(
                        IsolatedJobStatus.FAILED
                        if release_succeeded
                        else IsolatedJobStatus.EFFECT_UNKNOWN
                    ),
                    reason_code=(
                        "launch_commit_storage_failure"
                        if release_succeeded
                        else "launch_commit_durability_unknown"
                    ),
                    failure_code=(
                        JobFailureCode.STORAGE_FAILURE
                        if release_succeeded
                        else JobFailureCode.EFFECT_UNKNOWN
                    ),
                )
            except Exception:
                with suppress(Exception):
                    self.job_store.transition(
                        request.execution_id,
                        status=IsolatedJobStatus.EFFECT_UNKNOWN,
                        reason_code="launch_commit_durability_unknown",
                        failure_code=JobFailureCode.EFFECT_UNKNOWN,
                    )
            if not isinstance(exc, Exception):
                raise
            raise ValueError(JobFailureCode.STORAGE_FAILURE.value) from exc
        effect_recheck_sha256: str | None = None
        effect_admission_refused = False
        try:
            try:
                prepared.verify_identity_before_admission()
                second_reference, effect_recheck = self.approvals.verify(
                    approval_run_id=approval_run_id,
                    request_id=approval_request_id,
                    expected_action_plan=preview.action_plan,
                    authority_provider=authority_provider,
                    evaluated_at=self.clock(),
                )
                if second_reference != approval_reference:
                    raise ValueError("approval current changed before effect admission")
            except BaseException:
                effect_admission_refused = True
                raise
            effect_recheck_sha256 = effect_recheck.binding_sha256
            self.budget_store.start(
                request.budget_run_id,
                operation_id=_operation_id(request.execution_id, "start"),
                permit_id=request.budget_permit_id,
            )
            prepared.resume(
                approval_valid_until=effect_recheck.valid_until,
                clock=self.clock,
            )
            self.job_store.transition(
                request.execution_id,
                status=IsolatedJobStatus.RUNNING,
                reason_code="primary_thread_resumed",
                effect_admission_recheck_binding_sha256=effect_recheck_sha256,
            )
        except BaseException as exc:
            if isinstance(exc, IsolatedJobError) and exc.code is JobFailureCode.APPROVAL_MISMATCH:
                effect_admission_refused = True
            known_no_effect = not prepared.resume_effect_possible
            failure_code = (
                exc.code
                if isinstance(exc, IsolatedJobError)
                else (
                    JobFailureCode.APPROVAL_MISMATCH
                    if effect_admission_refused and isinstance(exc, Exception)
                    else JobFailureCode.INTERNAL_INVARIANT_ERROR
                )
            )
            termination_evidence = prepared.terminate_before_resume(reason=failure_code)
            evidence_sha = isolated_job_sha256(
                {
                    "execution_id": request.execution_id,
                    "stage": (
                        "effect_admission_refused_no_effect"
                        if known_no_effect
                        else "resume_or_running_publication_unknown"
                    ),
                }
            )
            if known_no_effect:
                closure_succeeded = False
                termination_confirmed = (
                    termination_evidence is not None
                    and termination_evidence.active_processes == 0
                    and termination_evidence.process_tree_termination_confirmed
                    and termination_evidence.job_limits_requeried
                    and termination_evidence.executable_identity_rechecked
                    and termination_evidence.output_complete
                )
                if termination_confirmed:
                    try:
                        self._close_known_no_effect(
                            request,
                            evidence_sha256=evidence_sha,
                        )
                        closure_succeeded = True
                    except (DurableBudgetError, ValueError):
                        pass
                try:
                    self.job_store.transition(
                        request.execution_id,
                        status=(
                            IsolatedJobStatus.FAILED
                            if closure_succeeded
                            else IsolatedJobStatus.EFFECT_UNKNOWN
                        ),
                        reason_code=(
                            "effect_admission_refused_no_effect"
                            if closure_succeeded
                            else "effect_admission_budget_or_process_unknown"
                        ),
                        evidence=(termination_evidence if closure_succeeded else None),
                        failure_code=(
                            failure_code if closure_succeeded else JobFailureCode.EFFECT_UNKNOWN
                        ),
                    )
                except Exception:
                    with suppress(Exception):
                        self.job_store.transition(
                            request.execution_id,
                            status=IsolatedJobStatus.EFFECT_UNKNOWN,
                            reason_code="effect_admission_durability_unknown",
                            failure_code=JobFailureCode.EFFECT_UNKNOWN,
                        )
            else:
                with suppress(DurableBudgetError):
                    self._settle_ambiguous_resume(
                        request,
                        evidence_sha256=evidence_sha,
                    )
                self.job_store.transition(
                    request.execution_id,
                    status=IsolatedJobStatus.EFFECT_UNKNOWN,
                    reason_code="resume_publication_unknown",
                    effect_admission_recheck_binding_sha256=effect_recheck_sha256,
                    failure_code=JobFailureCode.EFFECT_UNKNOWN,
                )
            code = (
                failure_code
                if known_no_effect
                else (
                    JobFailureCode.APPROVAL_MISMATCH
                    if effect_admission_refused
                    else JobFailureCode.EFFECT_UNKNOWN
                )
            )
            if not isinstance(exc, Exception):
                raise
            raise ValueError(code.value) from exc

        def on_cancel_requested() -> None:
            self.job_store.transition(
                request.execution_id,
                status=IsolatedJobStatus.CANCEL_REQUESTED,
                reason_code="caller_cancel_requested",
            )
            self.budget_store.request_cancellation(
                request.budget_run_id,
                operation_id=_operation_id(request.execution_id, "cancel-request"),
                permit_id=request.budget_permit_id,
            )

        try:
            outcome = prepared.wait(
                cancelled=cancelled,
                on_cancel_requested=on_cancel_requested,
            )
        except BaseException:
            prepared.terminate_before_resume()
            evidence_sha = isolated_job_sha256(
                {
                    "execution_id": request.execution_id,
                    "stage": "controller_exit_while_waiting",
                }
            )
            with suppress(DurableBudgetError):
                self._settle_ambiguous_resume(
                    request,
                    evidence_sha256=evidence_sha,
                    worker_live=False,
                )
            with suppress(Exception):
                self.job_store.transition(
                    request.execution_id,
                    status=IsolatedJobStatus.EFFECT_UNKNOWN,
                    reason_code="controller_exit_while_waiting",
                    failure_code=JobFailureCode.EFFECT_UNKNOWN,
                )
            raise
        try:
            final_status, final_code = self._settle_outcome(request, outcome)
        except DurableBudgetError:
            try:
                final_status, final_code = self._settle_outcome(request, outcome)
            except DurableBudgetError:
                evidence_sha = isolated_job_sha256(outcome.evidence)
                with suppress(DurableBudgetError):
                    self._settle_ambiguous_resume(
                        request,
                        evidence_sha256=evidence_sha,
                        worker_live=not outcome.evidence.process_tree_termination_confirmed,
                    )
                self.job_store.transition(
                    request.execution_id,
                    status=IsolatedJobStatus.EFFECT_UNKNOWN,
                    reason_code="budget_settlement_unknown",
                    evidence=outcome.evidence,
                    failure_code=JobFailureCode.EFFECT_UNKNOWN,
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                )
                return self._result_from_state(request.execution_id)

        try:
            self.job_store.transition(
                request.execution_id,
                status=final_status,
                reason_code=(
                    "effect_unknown"
                    if final_status is IsolatedJobStatus.EFFECT_UNKNOWN
                    else "job_terminal"
                ),
                evidence=outcome.evidence,
                failure_code=final_code,
                stdout=outcome.stdout,
                stderr=outcome.stderr,
            )
        except Exception:
            try:
                self.job_store.transition(
                    request.execution_id,
                    status=IsolatedJobStatus.EFFECT_UNKNOWN,
                    reason_code="terminal_publication_unknown",
                    evidence=outcome.evidence,
                    failure_code=JobFailureCode.EFFECT_UNKNOWN,
                    stdout=outcome.stdout,
                    stderr=outcome.stderr,
                )
            except Exception as recovery_exc:
                raise ValueError(JobFailureCode.STORAGE_FAILURE.value) from recovery_exc
            return self._result_from_state(request.execution_id)
        return self._result_from_state(request.execution_id)

    def recover_after_restart(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
    ) -> IsolatedJobResultV1:
        """Conservatively latch an abandoned nonterminal attempt as effect-unknown."""

        current = self.job_store.load(request.execution_id)
        if current.request != request or current.policy != policy:
            raise ValueError(JobFailureCode.STALE_REPLAY.value)
        if (
            current.status in TERMINAL_JOB_STATUSES
            and current.status is not IsolatedJobStatus.EFFECT_UNKNOWN
        ):
            return self._result_from_state(request.execution_id)
        if current.process_id is not None:
            assert current.process_creation_time_100ns is not None
            process_status = self.backend.process_identity_status(
                current.process_id,
                current.process_creation_time_100ns,
            )
            if process_status not in {"absent", "different_live_process"}:
                raise ValueError(JobFailureCode.RUN_LOCKED.value)
        return self._recover_nonterminal(request)

    def reconcile(
        self,
        execution_id: str,
        *,
        reconciliation_reference: ReconciliationReferenceV1,
    ) -> IsolatedJobResultV1:
        current = self.job_store.load(execution_id)
        if current.status is not IsolatedJobStatus.EFFECT_UNKNOWN:
            raise ValueError(JobFailureCode.RECONCILIATION_REQUIRED.value)
        if current.process_id is not None:
            assert current.process_creation_time_100ns is not None
            process_status = self.backend.process_identity_status(
                current.process_id,
                current.process_creation_time_100ns,
            )
            if process_status not in {"absent", "different_live_process"}:
                raise ValueError(JobFailureCode.RECONCILIATION_REQUIRED.value)
        else:
            process_status = "absent"
        try:
            if not self._budget_is_terminal(current.request):
                raise ValueError(JobFailureCode.RECONCILIATION_REQUIRED.value)
        except DurableBudgetError as exc:
            raise ValueError(JobFailureCode.RECONCILIATION_REQUIRED.value) from exc
        evidence_sha = isolated_job_sha256(
            {
                "execution_id": execution_id,
                "reconciliation_reference": reconciliation_reference,
                "process_identity_status": process_status,
                "result": "non_success_reconciled",
            }
        )
        self.job_store.transition(
            execution_id,
            status=IsolatedJobStatus.RECONCILED,
            reason_code="manual_reconciliation",
            failure_code=JobFailureCode.RECONCILIATION_REQUIRED,
            reconciliation_evidence_sha256=evidence_sha,
        )
        return self._result_from_state(execution_id)
