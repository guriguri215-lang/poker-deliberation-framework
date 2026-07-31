"""Immutable full-snapshot state store for P2-028A isolated jobs."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import Final, Literal

from pydantic import ValidationError

from poker_deliberation.context_lifecycle import ContextClassification
from poker_deliberation.isolated_jobs.canonical import isolated_job_sha256
from poker_deliberation.isolated_jobs.models import (
    ISOLATED_JOB_ARTIFACT_SCHEMA,
    ISOLATED_JOB_PRODUCER_ID,
    ISOLATED_JOB_PRODUCER_VERSION,
    TERMINAL_JOB_STATUSES,
    ApprovalJobReferenceV1,
    BudgetJobBindingV1,
    ContextJobBindingV1,
    DurableIsolatedJobStateV1,
    IsolatedJobError,
    IsolatedJobPolicyV1,
    IsolatedJobRequestV1,
    IsolatedJobStatus,
    JobEventV1,
    JobEvidenceV1,
    JobFailureCode,
)
from poker_deliberation.local_data_policy import (
    ClassificationEvidence,
    ClassificationSource,
    contains_restricted_secret_shape,
)
from poker_deliberation.storage.revision_canonical import classification_evidence_sha256
from poker_deliberation.storage.revision_models import (
    APPROVED_LOCAL_DATA_POLICY_SHA256,
    BudgetPolicyBindingV1,
    ContextBindingV1,
    LocalDataBindingV1,
    OriginKind,
    RevisionArtifactV1,
    RevisionPublishRequestV1,
    RootInitializationOutcomeV1,
    RootInitializationRequestV1,
    RunStorageError,
    RunStorageFailureCode,
    Serialization,
)
from poker_deliberation.storage.revision_store import (
    RunRevisionStore,
    initialize_revision_root,
)

_STATE_LOGICAL_NAME: Final = "isolated_job_state.json"
_STDOUT_LOGICAL_NAME: Final = "stdout.txt"
_STDERR_LOGICAL_NAME: Final = "stderr.txt"

_ALLOWED_TRANSITIONS: Final[dict[IsolatedJobStatus, frozenset[IsolatedJobStatus]]] = {
    IsolatedJobStatus.PREPARED: frozenset(
        {
            IsolatedJobStatus.LAUNCH_COMMITTED,
            IsolatedJobStatus.FAILED,
            IsolatedJobStatus.EFFECT_UNKNOWN,
        }
    ),
    IsolatedJobStatus.LAUNCH_COMMITTED: frozenset(
        {
            IsolatedJobStatus.RUNNING,
            IsolatedJobStatus.CANCEL_REQUESTED,
            IsolatedJobStatus.FAILED,
            IsolatedJobStatus.EFFECT_UNKNOWN,
        }
    ),
    IsolatedJobStatus.RUNNING: frozenset(
        {
            IsolatedJobStatus.CANCEL_REQUESTED,
            IsolatedJobStatus.CANCELLED,
            IsolatedJobStatus.COMPLETED,
            IsolatedJobStatus.FAILED,
            IsolatedJobStatus.EFFECT_UNKNOWN,
        }
    ),
    IsolatedJobStatus.CANCEL_REQUESTED: frozenset(
        {
            IsolatedJobStatus.CANCELLED,
            IsolatedJobStatus.EFFECT_UNKNOWN,
        }
    ),
    IsolatedJobStatus.EFFECT_UNKNOWN: frozenset({IsolatedJobStatus.RECONCILED}),
    IsolatedJobStatus.CANCELLED: frozenset(),
    IsolatedJobStatus.RECONCILED: frozenset(),
    IsolatedJobStatus.COMPLETED: frozenset(),
    IsolatedJobStatus.FAILED: frozenset(),
}


def initialize_isolated_job_root(
    revision_root: Path,
    legacy_runs_root: Path,
    *,
    root_id: str,
    initialized_at: datetime,
) -> RootInitializationOutcomeV1:
    """Explicitly initialize one dedicated P2-028A structural root."""

    return initialize_revision_root(
        RootInitializationRequestV1(
            revision_root=revision_root,
            legacy_runs_root=legacy_runs_root,
            root_id=root_id,
            initialized_at=initialized_at,
            producer_id=ISOLATED_JOB_PRODUCER_ID,
            producer_version=ISOLATED_JOB_PRODUCER_VERSION,
        )
    )


def _event_sha256(
    *,
    ordinal: int,
    previous_status: IsolatedJobStatus | None,
    status: IsolatedJobStatus,
    occurred_at: datetime,
    reason_code: str,
) -> str:
    return isolated_job_sha256(
        {
            "ordinal": ordinal,
            "previous_status": (None if previous_status is None else previous_status.value),
            "status": status.value,
            "occurred_at": occurred_at.isoformat(),
            "reason_code": reason_code,
        }
    )


def _new_event(
    *,
    ordinal: int,
    previous_status: IsolatedJobStatus | None,
    status: IsolatedJobStatus,
    occurred_at: datetime,
    reason_code: str,
) -> JobEventV1:
    return JobEventV1(
        ordinal=ordinal,
        previous_status=previous_status,
        status=status,
        occurred_at=occurred_at,
        reason_code=reason_code,
        state_sha256=_event_sha256(
            ordinal=ordinal,
            previous_status=previous_status,
            status=status,
            occurred_at=occurred_at,
            reason_code=reason_code,
        ),
    )


def _validate_successor(
    older: DurableIsolatedJobStateV1,
    newer: DurableIsolatedJobStateV1,
) -> None:
    if (
        newer.generation != older.generation + 1
        or newer.previous_state_sha256 != older.canonical_sha256
        or newer.status not in _ALLOWED_TRANSITIONS[older.status]
        or newer.events[:-1] != older.events
        or newer.events[-1].previous_status is not older.status
    ):
        raise ValueError("isolated-job state transition lineage mismatch")
    stable_fields = (
        "run_id",
        "execution_id",
        "request",
        "request_sha256",
        "policy",
        "policy_sha256",
        "action_digest_sha256",
        "context_binding",
        "budget_binding",
        "approval_reference",
        "approval_recheck_binding_sha256",
        "automatic_retry_allowed",
    )
    if any(getattr(newer, name) != getattr(older, name) for name in stable_fields):
        raise ValueError("isolated-job immutable binding changed")
    older_process = (older.process_id, older.process_creation_time_100ns)
    newer_process = (newer.process_id, newer.process_creation_time_100ns)
    if older_process != (None, None):
        if newer_process != older_process:
            raise ValueError("isolated-job process identity changed")
    elif newer_process != (None, None) and (
        older.status is not IsolatedJobStatus.PREPARED
        or newer.status is not IsolatedJobStatus.LAUNCH_COMMITTED
    ):
        raise ValueError("isolated-job process identity was introduced out of order")
    older_effect = older.effect_admission_recheck_binding_sha256
    newer_effect = newer.effect_admission_recheck_binding_sha256
    if older_effect is not None and newer_effect != older_effect:
        raise ValueError("isolated-job effect-admission identity changed")
    if (
        older_effect is None
        and newer_effect is not None
        and (
            older.status is not IsolatedJobStatus.LAUNCH_COMMITTED
            or older.process_id is None
            or newer.status
            not in {
                IsolatedJobStatus.RUNNING,
                IsolatedJobStatus.EFFECT_UNKNOWN,
            }
        )
    ):
        raise ValueError("isolated-job effect-admission identity was introduced out of order")
    if older.evidence is not None and newer.evidence != older.evidence:
        raise ValueError("isolated-job process evidence changed")
    if (
        older.evidence is None
        and newer.evidence is not None
        and (
            newer.status
            not in {
                IsolatedJobStatus.CANCELLED,
                IsolatedJobStatus.COMPLETED,
                IsolatedJobStatus.FAILED,
                IsolatedJobStatus.EFFECT_UNKNOWN,
            }
        )
    ):
        raise ValueError("isolated-job process evidence was introduced before terminal state")


def _validate_output_binding(
    state: DurableIsolatedJobStateV1,
    stdout: bytes,
    stderr: bytes,
) -> None:
    evidence = state.evidence
    if evidence is None:
        if stdout or stderr:
            raise ValueError("isolated-job output requires exact process evidence")
        return
    if (
        evidence.stdout_bytes != len(stdout)
        or evidence.stderr_bytes != len(stderr)
        or evidence.stdout_sha256 != hashlib.sha256(stdout).hexdigest()
        or evidence.stderr_sha256 != hashlib.sha256(stderr).hexdigest()
    ):
        raise ValueError("isolated-job output/evidence binding mismatch")


class IsolatedJobStore:
    """Exact-CAS isolated-job state and bounded-output store."""

    def __init__(
        self,
        revision_root: Path,
        legacy_runs_root: Path,
        *,
        clock: Callable[[], datetime],
        fault_injector: Callable[[str], None] | None = None,
        max_artifact_bytes: int = 70 * 1024 * 1024,
        max_run_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self.clock = clock
        self.revisions = RunRevisionStore(
            revision_root,
            legacy_runs_root,
            max_artifact_bytes=max_artifact_bytes,
            max_run_bytes=max_run_bytes,
            clock=clock,
            fault_injector=fault_injector,
            producer_id=ISOLATED_JOB_PRODUCER_ID,
            producer_version=ISOLATED_JOB_PRODUCER_VERSION,
        )

    def _bindings(
        self,
        state: DurableIsolatedJobStateV1,
        logical_name: str,
    ) -> tuple[LocalDataBindingV1 | ContextBindingV1 | BudgetPolicyBindingV1, ...]:
        evidence = ClassificationEvidence(
            source_classifications=(ContextClassification.INTERNAL,),
            restricted_secret_check_completed=True,
        )
        local = LocalDataBindingV1(
            logical_name=logical_name,
            classification=ContextClassification.INTERNAL,
            classification_source=ClassificationSource.SOURCE_INHERITANCE,
            classification_evidence=evidence,
            classification_evidence_sha256=classification_evidence_sha256(evidence),
        )
        context = state.context_binding
        context_binding = ContextBindingV1(
            context_sha256=context.integrity_sha256,
            context_id=context.context_id,
            attempt_id=context.attempt_id,
            parent_context_id=context.parent_context_id,
            schema_version=context.schema_version,
            classification=ContextClassification.INTERNAL,
            payload_sha256=context.payload_sha256,
            source_sha256=context.source_sha256,
            policy_sha256=context.policy_sha256,
            envelope_sha256=context.integrity_sha256,
            expires_at=context.expires_at,
            producer_runtime="python-local",
            consumer_runtime="python-local",
        )
        budget = BudgetPolicyBindingV1(
            policy_schema_version="2.0.0",
            policy_sha256=state.budget_binding.policy_sha256,
        )
        return (local, context_binding, budget)

    def _artifact(
        self,
        state: DurableIsolatedJobStateV1,
        *,
        logical_name: str,
        data: bytes,
        schema: str,
        origin_kind: OriginKind,
        serialization: Serialization,
        media_type: Literal[
            "application/json",
            "application/x-ndjson",
            "text/markdown",
            "text/plain",
        ],
    ) -> RevisionArtifactV1:
        evidence = ClassificationEvidence(
            source_classifications=(ContextClassification.INTERNAL,),
            restricted_secret_check_completed=True,
        )
        return RevisionArtifactV1(
            logical_name=logical_name,
            media_type=media_type,
            artifact_schema_version=schema,
            serialization=serialization,
            exact_bytes=data,
            required=True,
            classification=ContextClassification.INTERNAL,
            classification_source=ClassificationSource.SOURCE_INHERITANCE,
            classification_evidence=evidence,
            policy_sha256=APPROVED_LOCAL_DATA_POLICY_SHA256,
            origin_kind=origin_kind,
            provenance_bindings=self._bindings(state, logical_name),
        )

    def _publish(
        self,
        previous: DurableIsolatedJobStateV1 | None,
        state: DurableIsolatedJobStateV1,
        *,
        stdout: bytes,
        stderr: bytes,
    ) -> DurableIsolatedJobStateV1:
        try:
            stdout_text = stdout.decode("utf-8", errors="strict")
            stderr_text = stderr.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise IsolatedJobError(JobFailureCode.SECRET_REJECTED) from exc
        if contains_restricted_secret_shape(stdout_text) or contains_restricted_secret_shape(
            stderr_text
        ):
            raise IsolatedJobError(JobFailureCode.SECRET_REJECTED)
        try:
            _validate_output_binding(state, stdout, stderr)
        except ValueError as exc:
            raise IsolatedJobError(JobFailureCode.STORAGE_FAILURE) from exc
        if previous is None:
            expected_revision = None
            expected_manifest_sha256 = None
            expected_pointer_sha256 = None
        else:
            current = self.revisions.read_current(state.execution_id)
            if (
                current.current_revision != previous.generation
                or current.current_revision != state.generation - 1
            ):
                raise IsolatedJobError(JobFailureCode.STALE_REPLAY)
            expected_revision = current.current_revision
            expected_manifest_sha256 = current.manifest_sha256
            expected_pointer_sha256 = current.current_pointer_sha256
        transaction_digest = hashlib.sha256(
            (
                state.execution_id + ":" + str(state.generation) + ":" + state.canonical_sha256
            ).encode("utf-8")
        ).hexdigest()
        request = RevisionPublishRequestV1(
            run_id=state.execution_id,
            transaction_id="txn-" + transaction_digest[:32],
            proposed_revision=state.generation,
            expected_revision=expected_revision,
            expected_manifest_sha256=expected_manifest_sha256,
            expected_pointer_sha256=expected_pointer_sha256,
            created_at=self.clock(),
            producer_id=ISOLATED_JOB_PRODUCER_ID,
            producer_version=ISOLATED_JOB_PRODUCER_VERSION,
            artifacts=(
                self._artifact(
                    state,
                    logical_name=_STATE_LOGICAL_NAME,
                    data=state.canonical_bytes,
                    schema=ISOLATED_JOB_ARTIFACT_SCHEMA,
                    origin_kind="isolated_job_state",
                    serialization="poker-run-storage-json-v1",
                    media_type="application/json",
                ),
                self._artifact(
                    state,
                    logical_name=_STDOUT_LOGICAL_NAME,
                    data=stdout,
                    schema="poker-isolated-job-stdout-artifact-v1",
                    origin_kind="isolated_job_stdout",
                    serialization="poker-run-storage-utf8-text-v1",
                    media_type="text/plain",
                ),
                self._artifact(
                    state,
                    logical_name=_STDERR_LOGICAL_NAME,
                    data=stderr,
                    schema="poker-isolated-job-stderr-artifact-v1",
                    origin_kind="isolated_job_stderr",
                    serialization="poker-run-storage-utf8-text-v1",
                    media_type="text/plain",
                ),
            ),
        )
        try:
            outcome = self.revisions.publish(request)
        except RunStorageError as exc:
            if exc.failure.code is RunStorageFailureCode.EFFECT_UNKNOWN or (
                exc.failure.filesystem_effect == "current_advanced"
                and exc.failure.domain_effect == "current_advanced"
            ):
                try:
                    history = self._load_history(state.execution_id)
                    confirmed_stdout, confirmed_stderr = self.load_outputs(state.execution_id)
                    current = self.revisions.read_current(state.execution_id)
                    if (
                        history
                        and history[0] == state
                        and current.current_revision == state.generation
                        and confirmed_stdout == stdout
                        and confirmed_stderr == stderr
                    ):
                        return state
                except IsolatedJobError:
                    pass
            code = (
                JobFailureCode.STALE_REPLAY
                if exc.failure.code
                in {
                    RunStorageFailureCode.RUN_CONFLICT,
                    RunStorageFailureCode.IDEMPOTENCY_CONFLICT,
                }
                else JobFailureCode.STORAGE_FAILURE
            )
            raise IsolatedJobError(code) from exc
        if previous is None and outcome.outcome_kind != "published":
            raise IsolatedJobError(JobFailureCode.RUN_LOCKED)
        return state

    def _load_artifact(self, execution_id: str, logical_name: str, schema: str) -> bytes:
        try:
            history = self.revisions._read_structural_artifact_history(
                execution_id,
                logical_name,
                artifact_schema_version=schema,
            )
        except RunStorageError as exc:
            raise IsolatedJobError(JobFailureCode.STORAGE_FAILURE) from exc
        return history.revisions[0].exact_bytes

    def _load_history(self, execution_id: str) -> tuple[DurableIsolatedJobStateV1, ...]:
        try:
            state_history = self.revisions._read_structural_artifact_history(
                execution_id,
                _STATE_LOGICAL_NAME,
                artifact_schema_version=ISOLATED_JOB_ARTIFACT_SCHEMA,
            )
            stdout_history = self.revisions._read_structural_artifact_history(
                execution_id,
                _STDOUT_LOGICAL_NAME,
                artifact_schema_version="poker-isolated-job-stdout-artifact-v1",
            )
            stderr_history = self.revisions._read_structural_artifact_history(
                execution_id,
                _STDERR_LOGICAL_NAME,
                artifact_schema_version="poker-isolated-job-stderr-artifact-v1",
            )
            states = tuple(
                DurableIsolatedJobStateV1.model_validate_json(entry.exact_bytes)
                for entry in state_history.revisions
            )
            if tuple(state.generation for state in states) != tuple(
                entry.revision for entry in state_history.revisions
            ):
                raise ValueError("isolated-job generation/storage revision mismatch")
            state_revisions = tuple(entry.revision for entry in state_history.revisions)
            if state_revisions != tuple(
                entry.revision for entry in stdout_history.revisions
            ) or state_revisions != tuple(entry.revision for entry in stderr_history.revisions):
                raise ValueError("isolated-job output/state revision lineage mismatch")
            for state, stdout_entry, stderr_entry in zip(
                states,
                stdout_history.revisions,
                stderr_history.revisions,
                strict=True,
            ):
                _validate_output_binding(
                    state,
                    stdout_entry.exact_bytes,
                    stderr_entry.exact_bytes,
                )
            if any(state.execution_id != execution_id for state in states):
                raise ValueError("isolated-job cross-execution replay")
            for newer, older in pairwise(states):
                _validate_successor(older, newer)
            return states
        except IsolatedJobError:
            raise
        except (RunStorageError, TypeError, ValueError, ValidationError) as exc:
            raise IsolatedJobError(JobFailureCode.STORAGE_FAILURE) from exc

    def load(self, execution_id: str) -> DurableIsolatedJobStateV1:
        return self._load_history(execution_id)[0]

    def load_outputs(self, execution_id: str) -> tuple[bytes, bytes]:
        return (
            self._load_artifact(
                execution_id,
                _STDOUT_LOGICAL_NAME,
                "poker-isolated-job-stdout-artifact-v1",
            ),
            self._load_artifact(
                execution_id,
                _STDERR_LOGICAL_NAME,
                "poker-isolated-job-stderr-artifact-v1",
            ),
        )

    def create(
        self,
        request: IsolatedJobRequestV1,
        policy: IsolatedJobPolicyV1,
        *,
        action_digest_sha256: str,
        context_binding: ContextJobBindingV1,
        budget_binding: BudgetJobBindingV1,
        approval_reference: ApprovalJobReferenceV1,
        approval_recheck_binding_sha256: str,
    ) -> DurableIsolatedJobStateV1:
        event = _new_event(
            ordinal=0,
            previous_status=None,
            status=IsolatedJobStatus.PREPARED,
            occurred_at=self.clock(),
            reason_code="admission_verified",
        )
        state = DurableIsolatedJobStateV1(
            run_id=request.run_id,
            execution_id=request.execution_id,
            generation=1,
            request=request,
            request_sha256=isolated_job_sha256(request),
            policy=policy,
            policy_sha256=isolated_job_sha256(policy),
            action_digest_sha256=action_digest_sha256,
            context_binding=context_binding,
            budget_binding=budget_binding,
            approval_reference=approval_reference,
            approval_recheck_binding_sha256=approval_recheck_binding_sha256,
            status=IsolatedJobStatus.PREPARED,
            events=(event,),
        )
        return self._publish(None, state, stdout=b"", stderr=b"")

    def transition(
        self,
        execution_id: str,
        *,
        status: IsolatedJobStatus,
        reason_code: str,
        process_id: int | None = None,
        process_creation_time_100ns: int | None = None,
        evidence: JobEvidenceV1 | None = None,
        failure_code: JobFailureCode | None = None,
        reconciliation_evidence_sha256: str | None = None,
        effect_admission_recheck_binding_sha256: str | None = None,
        stdout: bytes | None = None,
        stderr: bytes | None = None,
    ) -> DurableIsolatedJobStateV1:
        current = self.load(execution_id)
        if current.status in TERMINAL_JOB_STATUSES:
            raise IsolatedJobError(JobFailureCode.STALE_REPLAY)
        if status not in _ALLOWED_TRANSITIONS[current.status]:
            raise IsolatedJobError(JobFailureCode.STALE_REPLAY)
        output_stdout, output_stderr = self.load_outputs(execution_id)
        event = _new_event(
            ordinal=current.generation,
            previous_status=current.status,
            status=status,
            occurred_at=self.clock(),
            reason_code=reason_code,
        )
        state = DurableIsolatedJobStateV1.model_validate(
            {
                **current.model_dump(mode="python"),
                "generation": current.generation + 1,
                "previous_state_sha256": current.canonical_sha256,
                "status": status,
                "effect_admission_recheck_binding_sha256": (
                    current.effect_admission_recheck_binding_sha256
                    if effect_admission_recheck_binding_sha256 is None
                    else effect_admission_recheck_binding_sha256
                ),
                "process_id": (current.process_id if process_id is None else process_id),
                "process_creation_time_100ns": (
                    current.process_creation_time_100ns
                    if process_creation_time_100ns is None
                    else process_creation_time_100ns
                ),
                "evidence": current.evidence if evidence is None else evidence,
                "failure_code": failure_code,
                "reconciliation_evidence_sha256": reconciliation_evidence_sha256,
                "events": (*current.events, event),
            },
            strict=True,
        )
        _validate_successor(current, state)
        return self._publish(
            current,
            state,
            stdout=output_stdout if stdout is None else stdout,
            stderr=output_stderr if stderr is None else stderr,
        )
