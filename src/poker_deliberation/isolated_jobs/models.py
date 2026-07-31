"""Strict P2-028A contracts for repository-owned synthetic isolated jobs.

The models in this module contain bounded control metadata only.  They do not
provide a generic subprocess surface and never contain secret values.
"""

from __future__ import annotations

import ntpath
import re
import unicodedata
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from poker_deliberation.local_data_policy import contains_restricted_secret_shape

ISOLATED_JOB_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
ISOLATED_JOB_CANONICALIZATION: Final[Literal["poker-isolated-job-json-v1"]] = (
    "poker-isolated-job-json-v1"
)
ISOLATED_JOB_HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"
ISOLATED_JOB_PRODUCER_ID: Final[Literal["p2-028a-isolated-job-control"]] = (
    "p2-028a-isolated-job-control"
)
ISOLATED_JOB_PRODUCER_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
ISOLATED_JOB_ARTIFACT_SCHEMA: Final[Literal["poker-isolated-job-state-artifact-v1"]] = (
    "poker-isolated-job-state-artifact-v1"
)
MAX_APPROVED_INPUT_BYTES: Final = 2 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
_WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)


def _safe_control(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("isolated-job control metadata must be NFC")
    if any(unicodedata.category(character) in {"Cc", "Cf"} for character in value):
        raise ValueError(
            "isolated-job control metadata cannot contain control or format characters"
        )
    if contains_restricted_secret_shape(value):
        raise ValueError("isolated-job control metadata must not contain a secret shape")
    return value


def _portable_id(value: str) -> str:
    _safe_control(value)
    if not _PORTABLE_ID.fullmatch(value):
        raise ValueError("isolated-job identifier must use the portable format")
    return value


def _absolute_path(value: str) -> str:
    _safe_control(value)
    if len(value) > 512 or "/" in value or not re.fullmatch(r"[A-Za-z]:\\[^:*?\"<>|]*", value):
        raise ValueError("isolated-job path must be a bounded absolute Windows path")
    segments = value[3:].split("\\")
    if not segments or any(
        not segment
        or segment in {".", ".."}
        or segment.endswith((" ", "."))
        or segment.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED
        for segment in segments
    ):
        raise ValueError("isolated-job path contains a forbidden Windows segment")
    return value


def _utc(value: datetime, field_name: str) -> datetime:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")
    return value


Sha256 = Annotated[str, Field(pattern=_SHA256.pattern)]
PortableId = Annotated[str, AfterValidator(_portable_id)]
Version = Annotated[str, Field(pattern=_VERSION.pattern), AfterValidator(_safe_control)]
AbsoluteWindowsPath = Annotated[str, AfterValidator(_absolute_path)]


class _JobModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class SyntheticOperation(StrEnum):
    SUCCESS = "success"
    KNOWN_FAILURE = "known_failure"
    HANG = "hang"
    SPAWN_TREE = "spawn_tree"
    STDOUT_FLOOD = "stdout_flood"
    STDERR_FLOOD = "stderr_flood"
    MEMORY_PRESSURE = "memory_pressure"
    CPU_SPIN = "cpu_spin"
    COPY_HANDLES = "copy_handles"
    STDIN_EOF = "stdin_eof"
    MODULE_INVENTORY = "module_inventory"


class IsolatedJobStatus(StrEnum):
    PREPARED = "prepared"
    LAUNCH_COMMITTED = "launch_committed"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    EFFECT_UNKNOWN = "effect_unknown"
    RECONCILED = "reconciled"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_JOB_STATUSES: Final[frozenset[IsolatedJobStatus]] = frozenset(
    {
        IsolatedJobStatus.CANCELLED,
        IsolatedJobStatus.RECONCILED,
        IsolatedJobStatus.COMPLETED,
        IsolatedJobStatus.FAILED,
    }
)
NONTERMINAL_JOB_STATUSES: Final[frozenset[IsolatedJobStatus]] = frozenset(
    set(IsolatedJobStatus) - TERMINAL_JOB_STATUSES
)


class JobFailureCode(StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    INVALID_REQUEST = "invalid_request"
    IDENTITY_MISMATCH = "identity_mismatch"
    PATH_CONFINEMENT_FAILED = "path_confinement_failed"
    LINK_OR_REPARSE_DETECTED = "link_or_reparse_detected"
    HARDLINK_DETECTED = "hardlink_detected"
    NETWORK_NOT_ENFORCEABLE = "network_not_enforceable"
    SECRET_REJECTED = "secret_rejected"
    APPROVAL_MISSING = "approval_missing"
    APPROVAL_MISMATCH = "approval_mismatch"
    CONTEXT_MISMATCH = "context_mismatch"
    BUDGET_MISMATCH = "budget_mismatch"
    STALE_REPLAY = "stale_replay"
    PROCESS_LIMIT = "process_limit"
    MEMORY_LIMIT = "memory_limit"
    CPU_LIMIT = "cpu_limit"
    WALL_CLOCK_LIMIT = "wall_clock_limit"
    STDOUT_LIMIT = "stdout_limit"
    STDERR_LIMIT = "stderr_limit"
    COMBINED_OUTPUT_LIMIT = "combined_output_limit"
    CHILD_EXIT_NONZERO = "child_exit_nonzero"
    CANCELLED = "cancelled"
    EFFECT_UNKNOWN = "effect_unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    STORAGE_FAILURE = "storage_failure"
    RUN_LOCKED = "run_locked"
    INTERNAL_INVARIANT_ERROR = "internal_invariant_error"


class SyntheticArgumentsV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    duration_ms: int | None = Field(default=None, ge=1, le=600_000)
    output_bytes: int | None = Field(default=None, ge=1, le=64 * 1024 * 1024)
    memory_bytes: int | None = Field(default=None, ge=1, le=4 * 1024 * 1024 * 1024)
    child_count: int | None = Field(default=None, ge=1, le=8)
    exit_code: int | None = Field(default=None, ge=1, le=125)


class JobLimitsV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    wall_clock_ms: int = Field(ge=50, le=600_000)
    process_cpu_time_ms: int = Field(ge=10, le=600_000)
    job_cpu_time_ms: int = Field(ge=10, le=600_000)
    process_memory_bytes: int = Field(ge=8 * 1024 * 1024, le=4 * 1024 * 1024 * 1024)
    job_memory_bytes: int = Field(ge=8 * 1024 * 1024, le=8 * 1024 * 1024 * 1024)
    maximum_processes: int = Field(ge=1, le=9)
    stdout_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    stderr_bytes: int = Field(ge=1, le=64 * 1024 * 1024)
    combined_output_bytes: int = Field(ge=1, le=64 * 1024 * 1024)

    @model_validator(mode="after")
    def combined_limits_are_coherent(self) -> JobLimitsV1:
        if self.job_memory_bytes < self.process_memory_bytes:
            raise ValueError("job memory limit cannot be below the per-process limit")
        if self.combined_output_bytes > self.stdout_bytes + self.stderr_bytes:
            raise ValueError("combined output limit exceeds the stream-limit sum")
        return self


class SecretReferenceV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    reference_id: PortableId
    reference_sha256: Sha256
    purpose_sha256: Sha256


class FileIdentityV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    absolute_path: AbsoluteWindowsPath
    size_bytes: int = Field(ge=0, le=2**63 - 1)
    sha256: Sha256
    device_id: int = Field(ge=0)
    file_id: int = Field(ge=0)
    link_count: int = Field(ge=1)
    modified_time_ns: int = Field(ge=0)


class DirectoryIdentityV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    absolute_path: AbsoluteWindowsPath
    device_id: int = Field(ge=0)
    file_id: int = Field(ge=0)
    modified_time_ns: int = Field(ge=0)


class ExecutionIdentityV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    identity_scope: Literal["base-python-fixed-helper-v1"] = "base-python-fixed-helper-v1"
    interpreter: FileIdentityV1
    python_dll: FileIdentityV1
    encoding_files: tuple[FileIdentityV1, FileIdentityV1, FileIdentityV1]
    synthetic_helper: FileIdentityV1
    python_version: Version
    architecture: Literal["AMD64"]
    identity_sha256: Sha256

    @model_validator(mode="after")
    def exact_identity_hash(self) -> ExecutionIdentityV1:
        from poker_deliberation.isolated_jobs.canonical import isolated_job_sha256

        payload = self.model_dump(mode="json")
        payload.pop("identity_sha256")
        if self.identity_sha256 != isolated_job_sha256(payload):
            raise ValueError("execution identity hash mismatch")
        return self


class FilesystemPolicyV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    workspace_root: DirectoryIdentityV1
    approved_input: FileIdentityV1 | None = None
    input_handle_required: bool = False
    write_access_kind: Literal["none"] = "none"
    network_access_requested: Literal[False] = False
    network_isolation_claimed: Literal[False] = False

    @model_validator(mode="after")
    def handle_contract_is_exact(self) -> FilesystemPolicyV1:
        if self.input_handle_required != (self.approved_input is not None):
            raise ValueError("approved input and explicit handle requirement must be paired")
        approved = self.approved_input
        if approved is not None:
            workspace = ntpath.normcase(self.workspace_root.absolute_path)
            candidate = ntpath.normcase(approved.absolute_path)
            try:
                common = ntpath.commonpath((workspace, candidate))
            except ValueError as exc:
                raise ValueError("approved input must be beneath the isolated workspace") from exc
            if common != workspace or candidate == workspace:
                raise ValueError("approved input must be beneath the isolated workspace")
            if approved.link_count != 1:
                raise ValueError("approved input must have exactly one hard link")
            if approved.size_bytes > MAX_APPROVED_INPUT_BYTES:
                raise ValueError("approved input exceeds the bounded input size")
        return self


class IsolatedJobRequestV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    run_id: PortableId
    execution_id: PortableId
    attempt_id: PortableId
    context_id: PortableId
    budget_run_id: PortableId
    budget_permit_id: PortableId
    execution_ordinal: Literal[0] = 0
    operation: SyntheticOperation
    arguments: SyntheticArgumentsV1 = Field(default_factory=SyntheticArgumentsV1)
    secret_references: tuple[SecretReferenceV1, ...] = Field(default=(), max_length=16)
    requested_network_access: Literal[False] = False

    @field_validator("secret_references")
    @classmethod
    def canonical_secret_references(
        cls, value: tuple[SecretReferenceV1, ...]
    ) -> tuple[SecretReferenceV1, ...]:
        identifiers = tuple(item.reference_id for item in value)
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("secret-reference identifiers must be unique")
        if identifiers != tuple(sorted(identifiers, key=lambda item: item.encode("utf-8"))):
            raise ValueError("secret references must be UTF-8 identifier ordered")
        return value

    @model_validator(mode="after")
    def closed_operation_arguments(self) -> IsolatedJobRequestV1:
        values = self.arguments
        present = {
            name
            for name in ("duration_ms", "output_bytes", "memory_bytes", "child_count", "exit_code")
            if getattr(values, name) is not None
        }
        allowed: dict[SyntheticOperation, frozenset[str]] = {
            SyntheticOperation.SUCCESS: frozenset(),
            SyntheticOperation.KNOWN_FAILURE: frozenset({"exit_code"}),
            SyntheticOperation.HANG: frozenset({"duration_ms"}),
            SyntheticOperation.SPAWN_TREE: frozenset({"duration_ms", "child_count"}),
            SyntheticOperation.STDOUT_FLOOD: frozenset({"output_bytes"}),
            SyntheticOperation.STDERR_FLOOD: frozenset({"output_bytes"}),
            SyntheticOperation.MEMORY_PRESSURE: frozenset({"memory_bytes", "duration_ms"}),
            SyntheticOperation.CPU_SPIN: frozenset({"duration_ms"}),
            SyntheticOperation.COPY_HANDLES: frozenset(),
            SyntheticOperation.STDIN_EOF: frozenset(),
            SyntheticOperation.MODULE_INVENTORY: frozenset(),
        }
        required = allowed[self.operation]
        if present != required:
            raise ValueError(
                "synthetic operation arguments do not match the closed operation matrix"
            )
        return self


class IsolatedJobPolicyV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    backend: Literal["windows_job_object_v1"] = "windows_job_object_v1"
    limits: JobLimitsV1
    execution_identity: ExecutionIdentityV1
    filesystem: FilesystemPolicyV1
    environment_name_allowlist: tuple[()] = ()
    stdin_mode: Literal["nul_eof"] = "nul_eof"
    initial_handle_policy: Literal["explicit_handle_list_only"] = "explicit_handle_list_only"
    child_scope: Literal["repository_synthetic_helper_only"] = "repository_synthetic_helper_only"
    automatic_retry_allowed: Literal[False] = False


class ContextJobBindingV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    context_id: PortableId
    attempt_id: PortableId
    assignment_id: PortableId
    role: PortableId
    root_attempt_id: PortableId
    parent_attempt_id: PortableId | None = None
    root_context_id: PortableId
    parent_context_id: PortableId | None = None
    payload_sha256: Sha256
    source_sha256: Sha256
    policy_sha256: Sha256
    integrity_sha256: Sha256
    expires_at: datetime

    _expiry_utc = field_validator("expires_at")(lambda value: _utc(value, "expires_at"))

    @model_validator(mode="after")
    def exact_lineage_shape(self) -> ContextJobBindingV1:
        if (self.parent_attempt_id is None) != (self.parent_context_id is None):
            raise ValueError("context attempt and parent lineage must be paired")
        if self.parent_attempt_id is None:
            if self.root_attempt_id != self.attempt_id or self.root_context_id != self.context_id:
                raise ValueError("initial context must equal its root lineage")
        elif self.attempt_id in {
            self.root_attempt_id,
            self.parent_attempt_id,
        } or self.context_id in {self.root_context_id, self.parent_context_id}:
            raise ValueError("retry context and attempt identities must be fresh")
        return self


class BudgetJobBindingV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    budget_run_id: PortableId
    permit_id: PortableId
    policy_sha256: Sha256
    activation_sha256: Sha256
    reservation_sha256: Sha256
    lineage_sha256: Sha256
    isolation_requirement_sha256: Sha256
    isolation_evidence_sha256: Sha256
    isolation_boundary_id: PortableId


class ApprovalJobReferenceV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    approval_run_id: PortableId
    approval_revision: int = Field(ge=1)
    approval_pointer_sha256: Sha256
    approval_manifest_sha256: Sha256
    request_id: PortableId


class ReconciliationReferenceV1(_JobModel):
    """Opaque operator/audit reference metadata; never raw reconciliation evidence."""

    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    reference_id: PortableId
    evidence_sha256: Sha256


class JobEvidenceV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    backend: Literal["windows_job_object_v1"] = "windows_job_object_v1"
    process_id: int | None = Field(default=None, ge=1)
    process_creation_time_100ns: int | None = Field(default=None, ge=0)
    exit_code: int | None = Field(default=None, ge=0, le=2**32 - 1)
    termination_reason: PortableId | None = None
    wall_clock_ms: int = Field(default=0, ge=0)
    job_user_time_100ns: int = Field(default=0, ge=0)
    job_kernel_time_100ns: int = Field(default=0, ge=0)
    process_user_time_100ns: int = Field(default=0, ge=0)
    process_kernel_time_100ns: int = Field(default=0, ge=0)
    peak_process_memory_bytes: int = Field(default=0, ge=0)
    peak_job_memory_bytes: int = Field(default=0, ge=0)
    total_processes: int = Field(default=0, ge=0)
    active_processes: int = Field(default=0, ge=0)
    stdout_bytes: int = Field(default=0, ge=0)
    stderr_bytes: int = Field(default=0, ge=0)
    stdout_sha256: Sha256
    stderr_sha256: Sha256
    command_line_sha256: Sha256
    inherited_handle_count: int = Field(ge=3, le=4)
    process_tree_termination_confirmed: bool
    job_limits_requeried: bool
    executable_identity_rechecked: bool
    output_complete: bool
    network_isolation_enforced: Literal[False] = False
    network_access_requested: Literal[False] = False

    @model_validator(mode="after")
    def exact_process_matrix(self) -> JobEvidenceV1:
        if (self.process_id is None) != (self.process_creation_time_100ns is None):
            raise ValueError("job evidence process identity must be paired")
        if self.process_id is not None and self.total_processes < 1:
            raise ValueError("job evidence process count is inconsistent")
        if self.active_processes > self.total_processes:
            raise ValueError("job evidence active process count exceeds total processes")
        if self.process_tree_termination_confirmed != (self.active_processes == 0):
            raise ValueError("job evidence tree-termination flag is inconsistent")
        if self.output_complete and not self.process_tree_termination_confirmed:
            raise ValueError("complete output requires a terminated process tree")
        return self


class JobEventV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    ordinal: int = Field(ge=0)
    previous_status: IsolatedJobStatus | None = None
    status: IsolatedJobStatus
    occurred_at: datetime
    reason_code: PortableId
    state_sha256: Sha256

    _occurred_utc = field_validator("occurred_at")(lambda value: _utc(value, "occurred_at"))


class DurableIsolatedJobStateV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    artifact_schema: Literal["poker-isolated-job-state-artifact-v1"] = ISOLATED_JOB_ARTIFACT_SCHEMA
    canonicalization: Literal["poker-isolated-job-json-v1"] = ISOLATED_JOB_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = ISOLATED_JOB_HASH_ALGORITHM
    producer_id: Literal["p2-028a-isolated-job-control"] = ISOLATED_JOB_PRODUCER_ID
    producer_version: Literal["1.0.0"] = ISOLATED_JOB_PRODUCER_VERSION
    run_id: PortableId
    execution_id: PortableId
    generation: int = Field(ge=1)
    previous_state_sha256: Sha256 | None = None
    request: IsolatedJobRequestV1
    request_sha256: Sha256
    policy: IsolatedJobPolicyV1
    policy_sha256: Sha256
    action_digest_sha256: Sha256
    context_binding: ContextJobBindingV1
    budget_binding: BudgetJobBindingV1
    approval_reference: ApprovalJobReferenceV1
    approval_recheck_binding_sha256: Sha256
    effect_admission_recheck_binding_sha256: Sha256 | None = None
    status: IsolatedJobStatus
    process_id: int | None = Field(default=None, ge=1)
    process_creation_time_100ns: int | None = Field(default=None, ge=0)
    evidence: JobEvidenceV1 | None = None
    failure_code: JobFailureCode | None = None
    reconciliation_evidence_sha256: Sha256 | None = None
    events: tuple[JobEventV1, ...] = Field(min_length=1, max_length=64)
    automatic_retry_allowed: Literal[False] = False

    @model_validator(mode="after")
    def exact_state_matrix(self) -> DurableIsolatedJobStateV1:
        from poker_deliberation.isolated_jobs.canonical import isolated_job_sha256

        if (self.generation == 1) != (self.previous_state_sha256 is None):
            raise ValueError("only generation one may omit previous state identity")
        if self.request.run_id != self.run_id or self.request.execution_id != self.execution_id:
            raise ValueError("job state/request identity mismatch")
        if self.request_sha256 != isolated_job_sha256(self.request):
            raise ValueError("job request hash mismatch")
        if self.policy_sha256 != isolated_job_sha256(self.policy):
            raise ValueError("job policy hash mismatch")
        if self.budget_binding.budget_run_id != self.request.budget_run_id:
            raise ValueError("job budget run mismatch")
        if self.budget_binding.permit_id != self.request.budget_permit_id:
            raise ValueError("job budget permit mismatch")
        if self.context_binding.context_id != self.request.context_id:
            raise ValueError("job context ID mismatch")
        if self.context_binding.attempt_id != self.request.attempt_id:
            raise ValueError("job attempt ID mismatch")
        if len(self.events) != self.generation:
            raise ValueError("job event count must match state generation")
        for index, event in enumerate(self.events):
            if event.ordinal != index:
                raise ValueError("job events are not canonically ordered")
            expected_event_sha256 = isolated_job_sha256(
                {
                    "ordinal": event.ordinal,
                    "previous_status": (
                        None if event.previous_status is None else event.previous_status.value
                    ),
                    "status": event.status.value,
                    "occurred_at": event.occurred_at.isoformat(),
                    "reason_code": event.reason_code,
                }
            )
            if event.state_sha256 != expected_event_sha256:
                raise ValueError("job event hash mismatch")
            if index == 0:
                if (
                    event.previous_status is not None
                    or event.status is not IsolatedJobStatus.PREPARED
                ):
                    raise ValueError("first job event must be prepared")
            elif event.previous_status is not self.events[index - 1].status:
                raise ValueError("job event transition lineage mismatch")
        if self.events[-1].status is not self.status:
            raise ValueError("last job event must match current status")
        if self.status in {
            IsolatedJobStatus.PREPARED,
            IsolatedJobStatus.LAUNCH_COMMITTED,
        }:
            if self.effect_admission_recheck_binding_sha256 is not None:
                raise ValueError("pre-effect job cannot claim effect-admission recheck")
        elif (
            self.status
            in {
                IsolatedJobStatus.RUNNING,
                IsolatedJobStatus.CANCEL_REQUESTED,
                IsolatedJobStatus.CANCELLED,
                IsolatedJobStatus.COMPLETED,
            }
            and self.effect_admission_recheck_binding_sha256 is None
        ):
            raise ValueError("effectful job requires effect-admission recheck identity")
        needs_process = self.status in {
            IsolatedJobStatus.LAUNCH_COMMITTED,
            IsolatedJobStatus.RUNNING,
            IsolatedJobStatus.CANCEL_REQUESTED,
            IsolatedJobStatus.CANCELLED,
            IsolatedJobStatus.COMPLETED,
        }
        if needs_process and self.process_id is None:
            raise ValueError("job process identity/status matrix mismatch")
        if self.status is IsolatedJobStatus.PREPARED and self.process_id is not None:
            raise ValueError("prepared job cannot have a process identity")
        if (self.process_id is None) != (self.process_creation_time_100ns is None):
            raise ValueError("job process ID and creation time must be paired")
        if self.evidence is not None and (
            self.process_id is None
            or self.evidence.process_id != self.process_id
            or self.evidence.process_creation_time_100ns != self.process_creation_time_100ns
        ):
            raise ValueError("job evidence/process identity mismatch")
        if self.status is IsolatedJobStatus.COMPLETED and (
            self.evidence is None
            or self.evidence.exit_code != 0
            or self.evidence.termination_reason is not None
            or self.evidence.active_processes != 0
            or not self.evidence.process_tree_termination_confirmed
            or not self.evidence.job_limits_requeried
            or not self.evidence.executable_identity_rechecked
            or not self.evidence.output_complete
            or self.failure_code is not None
        ):
            raise ValueError("completed job lacks exact successful evidence")
        if self.status is IsolatedJobStatus.CANCELLED and (
            self.evidence is None
            or self.evidence.active_processes != 0
            or not self.evidence.process_tree_termination_confirmed
            or not self.evidence.job_limits_requeried
            or not self.evidence.executable_identity_rechecked
            or self.failure_code is not JobFailureCode.CANCELLED
            or self.evidence.termination_reason != JobFailureCode.CANCELLED.value
        ):
            raise ValueError("cancelled job lacks whole-tree termination evidence")
        if (
            self.status is IsolatedJobStatus.FAILED
            and self.process_id is not None
            and (
                self.evidence is None
                or (
                    self.evidence.active_processes != 0
                    or not self.evidence.process_tree_termination_confirmed
                    or not self.evidence.job_limits_requeried
                    or not self.evidence.executable_identity_rechecked
                )
            )
        ):
            raise ValueError("failed job lacks closed-process evidence")
        if self.status is IsolatedJobStatus.EFFECT_UNKNOWN and (
            self.failure_code is not JobFailureCode.EFFECT_UNKNOWN
            or self.reconciliation_evidence_sha256 is not None
        ):
            raise ValueError("effect-unknown state must remain unreconciled")
        if self.status is IsolatedJobStatus.RECONCILED and (
            self.failure_code is not JobFailureCode.RECONCILIATION_REQUIRED
            or self.reconciliation_evidence_sha256 is None
        ):
            raise ValueError("reconciled state requires explicit non-success evidence")
        if (
            self.status in TERMINAL_JOB_STATUSES
            and self.status
            not in {
                IsolatedJobStatus.RECONCILED,
                IsolatedJobStatus.COMPLETED,
            }
            and self.failure_code is None
        ):
            raise ValueError("non-success terminal state requires a failure code")
        return self

    @property
    def canonical_bytes(self) -> bytes:
        from poker_deliberation.isolated_jobs.canonical import isolated_job_bytes

        return isolated_job_bytes(self)

    @property
    def canonical_sha256(self) -> str:
        from poker_deliberation.isolated_jobs.canonical import isolated_job_sha256

        return isolated_job_sha256(self)


class IsolatedJobResultV1(_JobModel):
    schema_version: Literal["1.0.0"] = ISOLATED_JOB_SCHEMA_VERSION
    execution_id: PortableId
    status: IsolatedJobStatus
    state_sha256: Sha256
    stdout: bytes
    stderr: bytes
    failure_code: JobFailureCode | None = None

    @field_validator("stdout", "stderr")
    @classmethod
    def own_bytes(cls, value: bytes) -> bytes:
        return bytes(bytearray(value))


class IsolatedJobError(ValueError):
    """A redacted typed refusal or isolated-job failure."""

    def __init__(self, code: JobFailureCode) -> None:
        super().__init__(code.value)
        self.code = code
