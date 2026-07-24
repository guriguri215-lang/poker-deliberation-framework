"""Internal P2-010B structural revision coordination.

This module is intentionally not exported from ``poker_deliberation.phases``.
It validates frozen phase preimages and a complete canonical revision request,
publishes one structural revision, and returns same-process authority data.  It
does not own a state machine, provider/tool execution, retry, or product run
integration.
"""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, Literal, Never, SupportsIndex, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation.budgets import BudgetPolicyV2
from poker_deliberation.context_lifecycle import (
    ContextClassification,
    legacy_context_sha256,
    validate_context_envelope,
)
from poker_deliberation.phases.contracts import (
    PhaseId,
    PhaseOutcome,
    PhaseRequest,
    PhaseStatus,
    revalidate_outcome,
    revalidate_request,
)
from poker_deliberation.phases.contracts import (
    canonical_sha256 as phase_canonical_sha256,
)
from poker_deliberation.phases.executors import (
    validate_analysis_output,
    validate_tool_research_output,
)
from poker_deliberation.phases.models import (
    AnalysisInput,
    AnalysisOutput,
    ContextBuildInput,
    ContextBuildOutput,
    SynthesisInput,
    SynthesisOutput,
    ToolResearchInput,
    ToolResearchOutput,
)
from poker_deliberation.phases.services import ContextBuildService, SynthesisService
from poker_deliberation.schemas import (
    Exactness,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.storage.revision_canonical import (
    CONTROL_CANONICALIZATION,
    JSONL_SERIALIZATION,
    TEXT_SERIALIZATION,
    build_inventory,
    canonical_json_bytes,
    parse_canonical_json,
    run_id_sha256,
    sha256_bytes,
    transaction_sha256,
)
from poker_deliberation.storage.revision_models import (
    ArtifactIntentSnapshotV1,
    BudgetPolicyBindingV1,
    ContextBindingV1,
    PhaseBindingV1,
    RevisionArtifactV1,
    RevisionPublishOutcomeV1,
    RevisionPublishRequestV1,
    RunStorageError,
    RunStorageFailureCode,
    ToolBindingV1,
)
from poker_deliberation.storage.revision_store import RunRevisionStore
from poker_deliberation.tools.contracts import contract_by_name

TRANSITION_REASON = "durable synthesis revision committed"
TRANSITION_PLAN_SCHEMA = "1.0.0"
TRANSITION_PLAN_DOMAIN = "poker-phase-transition-plan-v1"
TRANSITION_EVENT_PREFIX_DOMAIN = "poker-phase-transition-event-prefix-v1"
COORDINATOR_PRODUCER_ID = "p2-010b-phase-revision"
COORDINATOR_PRODUCER_VERSION = "0.2.0"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_KEY = re.compile(
    r"api[-_]?key|authorization|bearer|cookie|password|passwd|secret|token|"
    r"private[-_]?key|client[-_]?(?:secret|credential)|credential",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\bAIza[A-Za-z0-9_-]{20,}\b|"
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|\bnpm_[A-Za-z0-9]{20,}\b|"
    r"\b(?:rk|sk)_(?:live|test)_[A-Za-z0-9]{10,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|"
    r"(?:api[-_]?key|password|passwd|secret|token)\s*[:=]|"
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\b(?:api[-_]?key|access[-_]?token|password|client[-_]?secret)\b"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9_./+=-]{12,})",
    re.IGNORECASE,
)


class _CoordinatorModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _domain_digest(domain: str, value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain.encode("ascii") + b"\x00" + encoded).hexdigest()


class PhaseTransitionPlanV1(_CoordinatorModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    run_id: str
    source: Literal["FINAL_SYNTHESIS"] = "FINAL_SYNTHESIS"
    target: Literal["COMPLETED"] = "COMPLETED"
    reason: Literal["durable synthesis revision committed"] = "durable synthesis revision committed"
    event_count: int = Field(ge=0)
    event_prefix_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def self_hash_matches(self) -> PhaseTransitionPlanV1:
        projection = self.model_dump(mode="json")
        projection.pop("plan_sha256")
        if self.plan_sha256 != _domain_digest(TRANSITION_PLAN_DOMAIN, projection):
            raise ValueError("phase transition plan digest mismatch")
        return self


class PhaseRevisionFailureCode(StrEnum):
    INVALID_TRACE = "invalid_trace"
    INVALID_PLAN = "invalid_plan"
    SECRET_DETECTED = "secret_detected"
    UNSUPPORTED_PAYLOAD = "unsupported_payload"
    PERSISTENCE_DENIED = "persistence_denied"
    PUBLISH_CONFLICT = "publish_conflict"
    PUBLISH_UNCERTAIN = "publish_uncertain"
    AUTHORIZATION_MISMATCH = "authorization_mismatch"
    APPLY_FAILED = "apply_failed"
    APPLY_UNKNOWN = "apply_unknown"


class PhaseRevisionFailureV1(_CoordinatorModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    code: PhaseRevisionFailureCode


class PhaseTransitionApplyResultV1(_CoordinatorModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    outcome_kind: Literal[
        "applied",
        "already_applied",
        "apply_failed",
        "apply_unknown",
    ]


RequestT = TypeVar("RequestT")
OutcomeT = TypeVar("OutcomeT")


@dataclass(frozen=True, slots=True)
class PhaseTracePair(Generic[RequestT, OutcomeT]):
    request: PhaseRequest[RequestT]
    outcome: PhaseOutcome[OutcomeT]


@dataclass(frozen=True, slots=True)
class PhaseRevisionTraceV1:
    synthesis: PhaseTracePair[SynthesisInput, SynthesisOutput]
    context_builds: tuple[PhaseTracePair[ContextBuildInput, ContextBuildOutput], ...] = ()
    analyses: tuple[PhaseTracePair[AnalysisInput, AnalysisOutput], ...] = ()
    tool_research: tuple[PhaseTracePair[ToolResearchInput, ToolResearchOutput], ...] = ()


@dataclass(frozen=True, slots=True)
class PhaseRevisionBundleV1:
    trace: PhaseRevisionTraceV1
    request: RevisionPublishRequestV1
    plan: PhaseTransitionPlanV1


@dataclass(frozen=True, slots=True, init=False)
class PhaseTransitionAuthorizationV1:
    schema_version: Literal["1.0.0"]
    plan: PhaseTransitionPlanV1
    run_id_sha256: str
    transaction_id: str
    transaction_sha256: str
    revision: int
    manifest_sha256: str
    pointer_sha256: str
    outcome_kind: Literal["published", "current_committed"]
    _issuer_capability: object = field(repr=False, compare=False)

    def __reduce_ex__(self, protocol: SupportsIndex) -> Never:
        del protocol
        raise TypeError("phase transition authorization is same-process only")


@dataclass(frozen=True, slots=True)
class _AuthorizationRecord:
    capability: object
    bundle: PhaseRevisionBundleV1
    authorization: PhaseTransitionAuthorizationV1
    data: tuple[object, ...]


_ISSUED_PLANS: dict[
    int,
    tuple[weakref.ReferenceType[PhaseTransitionPlanV1], object],
] = {}


def _plan_projection(
    *,
    run_id: str,
    event_count: int,
    event_prefix_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": TRANSITION_PLAN_SCHEMA,
        "run_id": run_id,
        "source": "FINAL_SYNTHESIS",
        "target": "COMPLETED",
        "reason": TRANSITION_REASON,
        "event_count": event_count,
        "event_prefix_sha256": event_prefix_sha256,
    }


def _issue_transition_plan(
    *,
    run_id: str,
    events: tuple[dict[str, str], ...],
    owner: object,
) -> PhaseTransitionPlanV1:
    event_prefix_sha256 = _domain_digest(TRANSITION_EVENT_PREFIX_DOMAIN, events)
    projection = _plan_projection(
        run_id=run_id,
        event_count=len(events),
        event_prefix_sha256=event_prefix_sha256,
    )
    plan = PhaseTransitionPlanV1(
        schema_version="1.0.0",
        run_id=run_id,
        source="FINAL_SYNTHESIS",
        target="COMPLETED",
        reason="durable synthesis revision committed",
        event_count=len(events),
        event_prefix_sha256=event_prefix_sha256,
        plan_sha256=_domain_digest(TRANSITION_PLAN_DOMAIN, projection),
    )
    identity = id(plan)

    def discard(_reference: object, *, key: int = identity) -> None:
        _ISSUED_PLANS.pop(key, None)

    _ISSUED_PLANS[identity] = (weakref.ref(plan, discard), owner)
    return plan


def _is_issued_plan(
    plan: PhaseTransitionPlanV1,
    *,
    owner: object | None = None,
) -> bool:
    record = _ISSUED_PLANS.get(id(plan))
    if record is None:
        return False
    reference, issued_owner = record
    if reference() is not plan or (owner is not None and issued_owner is not owner):
        return False
    try:
        return PhaseTransitionPlanV1.model_validate(plan.model_dump(mode="python")) == plan
    except Exception:
        return False


def _authorization_data(
    authorization: PhaseTransitionAuthorizationV1,
) -> tuple[object, ...]:
    return (
        authorization.schema_version,
        authorization.plan,
        authorization.run_id_sha256,
        authorization.transaction_id,
        authorization.transaction_sha256,
        authorization.revision,
        authorization.manifest_sha256,
        authorization.pointer_sha256,
        authorization.outcome_kind,
    )


def _failure(code: PhaseRevisionFailureCode) -> PhaseRevisionFailureV1:
    return PhaseRevisionFailureV1(code=code)


class _SecretDetected(ValueError):
    pass


class _UnsupportedPayload(ValueError):
    pass


class _PersistenceDenied(ValueError):
    pass


def _scan_value(value: object) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or _SECRET_KEY.search(key):
                raise _SecretDetected
            _scan_value(item)
    elif isinstance(value, list):
        for item in value:
            _scan_value(item)
    elif isinstance(value, str) and _SECRET_VALUE.search(value):
        raise _SecretDetected


def _scan_artifact(artifact: RevisionArtifactV1, max_artifact_bytes: int) -> None:
    if len(artifact.exact_bytes) > max_artifact_bytes:
        raise _UnsupportedPayload
    try:
        text = artifact.exact_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise _UnsupportedPayload from None
    if (
        artifact.classification
        not in {ContextClassification.PUBLIC, ContextClassification.INTERNAL}
        or not artifact.classification_evidence.restricted_secret_check_completed
        or artifact.classification_evidence.contains_restricted_secret
        or any(
            source in {ContextClassification.SENSITIVE, ContextClassification.RESTRICTED}
            for source in artifact.classification_evidence.source_classifications
        )
    ):
        raise _PersistenceDenied
    try:
        if (
            artifact.media_type == "application/json"
            and artifact.serialization == CONTROL_CANONICALIZATION
        ):
            _scan_value(parse_canonical_json(artifact.exact_bytes))
        elif (
            artifact.media_type == "application/x-ndjson"
            and artifact.serialization == JSONL_SERIALIZATION
        ):
            for line in text.splitlines():
                if line:
                    _scan_value(parse_canonical_json(line.encode("utf-8")))
        elif (
            artifact.media_type == "text/markdown" and artifact.serialization == TEXT_SERIALIZATION
        ):
            if _SECRET_VALUE.search(text):
                raise _SecretDetected
        else:
            raise _UnsupportedPayload
    except _SecretDetected:
        raise
    except Exception:
        raise _UnsupportedPayload from None


def _phase_binding(
    request: PhaseRequest[Any],
    outcome: PhaseOutcome[Any],
    *,
    artifact_intents: tuple[ArtifactIntentSnapshotV1, ...] = (),
) -> PhaseBindingV1:
    return PhaseBindingV1(
        run_id=request.run_id,
        phase_id=request.phase_id.value,
        phase_schema_version=request.phase_schema_version,
        attempt_id=request.attempt_id,
        context_ids=request.context_ids,
        input_hash=request.input_hash,
        policy_snapshot_hash=request.policy_snapshot_hash,
        output_hash=outcome.output_hash,
        artifact_intents=artifact_intents,
    )


def _context_binding(pair: PhaseTracePair[AnalysisInput, AnalysisOutput]) -> ContextBindingV1:
    output = cast(AnalysisOutput, pair.outcome.output)
    envelope = output.envelope
    return ContextBindingV1(
        context_sha256=legacy_context_sha256(output.context),
        context_id=envelope.lineage.context_id,
        attempt_id=envelope.lineage.attempt_id,
        parent_context_id=envelope.lineage.parent_context_id,
        schema_version=envelope.schema_version,
        classification=envelope.policy.classification,
        payload_sha256=envelope.payload_sha256,
        source_sha256=envelope.lineage.source_sha256,
        policy_sha256=envelope.policy_sha256,
        envelope_sha256=envelope.integrity_sha256,
        expires_at=envelope.policy.expires_at,
        producer_runtime=envelope.lineage.producer_runtime.value,
        consumer_runtime=envelope.lineage.consumer_runtime.value,
    )


def _tool_binding(
    pair: PhaseTracePair[ToolResearchInput, ToolResearchOutput],
    binding: Any,
    by_name: dict[str, RevisionArtifactV1],
) -> ToolBindingV1:
    result_id = binding.result.result_id
    input_name = f"tool_results/{result_id}.input.json"
    result_name = f"tool_results/{result_id}.json"
    input_artifact = by_name[input_name]
    result_artifact = by_name[result_name]
    return ToolBindingV1(
        run_id=pair.request.run_id,
        phase_attempt_id=pair.request.attempt_id,
        ordinal=binding.ordinal,
        request_id=binding.request.request_id,
        request_tool_name=binding.request.tool_name,
        requested_by=binding.request.requested_by,
        requires_approval=binding.request.requires_approval,
        requested_contract_version=binding.requested_contract_version,
        tool_request_sha256=phase_canonical_sha256(binding.request),
        request_input_artifact_sha256=sha256_bytes(input_artifact.exact_bytes),
        result_id=result_id,
        result_tool_name=binding.result.tool_name,
        result_artifact_sha256=sha256_bytes(result_artifact.exact_bytes),
        request_input_sha256=binding.request_input_sha256,
        validated_result_input_sha256=binding.validated_result_input_sha256,
        materialized_result_input_sha256=binding.materialized_result_input_sha256,
        supported_contract_version=binding.supported_contract_version,
        result_contract_version=binding.result_contract_version,
    )


def _validate_tool_result_contract(result: ToolResult) -> None:
    contract = contract_by_name().get(result.tool_name)
    if contract is None:
        raise ValueError
    expected_command = (
        f"poker-deliberate calculate {result.tool_name} --analysis-scope retrospective "
        "--input <input.json>"
    )
    if (
        result.contract_version != contract.contract_version
        or result.version != contract.version
        or tuple(result.assumptions) != contract.assumptions
        or result.model_qualifier != contract.model_qualifier
        or result.reproduce_command != expected_command
    ):
        raise ValueError
    try:
        contract.input_model.model_validate(result.input, strict=True)
    except Exception:
        if result.status is not ToolStatus.FAILED:
            raise ValueError from None
    if result.status is ToolStatus.FAILED:
        if (
            result.output
            or result.numeric_exactness is not NumericalExactness.UNAVAILABLE
            or result.exactness is not Exactness.UNAVAILABLE
            or not result.error
        ):
            raise ValueError
        return
    validated_output = contract.output_model.model_validate(result.output, strict=True)
    if validated_output.model_dump(mode="python") != result.output:
        raise ValueError
    unavailable = bool(result.output.get("unavailable", False))
    expected_numeric = (
        NumericalExactness.UNAVAILABLE
        if unavailable
        else contract.resolve_numeric_exactness(result.output)
    )
    expected_exactness = (
        Exactness.UNAVAILABLE
        if expected_numeric is NumericalExactness.UNAVAILABLE
        else Exactness.APPROXIMATE
        if expected_numeric is NumericalExactness.APPROXIMATE
        else Exactness.EXACT
    )
    expected_status = ToolStatus.UNAVAILABLE if unavailable else ToolStatus.SUCCESS
    if (
        result.status is not expected_status
        or result.numeric_exactness is not expected_numeric
        or result.exactness is not expected_exactness
    ):
        raise ValueError
    if result.tool_name == "solver_status":
        capability = result.output.get("capability")
        if (
            result.status is not ToolStatus.UNAVAILABLE
            or result.output.get("status") != "unavailable"
            or result.output.get("result") != {}
            or not isinstance(capability, dict)
            or capability.get("available") is not False
            or result.numeric_exactness is not NumericalExactness.UNAVAILABLE
        ):
            raise ValueError


def _bindings_of_type(
    artifact: RevisionArtifactV1,
    binding_type: type[RequestT],
) -> tuple[RequestT, ...]:
    return tuple(
        cast(RequestT, binding)
        for binding in artifact.provenance_bindings
        if isinstance(binding, binding_type)
    )


def _transaction_digest(
    request: RevisionPublishRequestV1,
    inventories: tuple[Any, ...],
    heads: tuple[Any, ...],
) -> str:
    return transaction_sha256(
        {
            "schema_version": "1.0.0",
            "storage_protocol": "poker-run-revision-v1",
            "canonicalization": CONTROL_CANONICALIZATION,
            "hash_algorithm": "sha256",
            "run_id": request.run_id,
            "transaction_id": request.transaction_id,
            "proposed_revision": request.proposed_revision,
            "expected_revision": request.expected_revision,
            "expected_manifest_sha256": request.expected_manifest_sha256,
            "expected_pointer_sha256": request.expected_pointer_sha256,
            "created_at": request.created_at,
            "producer_id": request.producer_id,
            "producer_version": request.producer_version,
            "artifact_plan": inventories,
            "provenance_heads": heads,
        }
    )


class PhaseRevisionCoordinator:
    """Validate and publish one P2-010B same-process structural bundle."""

    def __init__(
        self,
        store: RunRevisionStore,
        *,
        budget_policy: BudgetPolicyV2,
        expected_policy_snapshot_hash: str,
    ) -> None:
        if (
            store.producer_id != COORDINATOR_PRODUCER_ID
            or store.producer_version != COORDINATOR_PRODUCER_VERSION
            or not _SHA256.fullmatch(expected_policy_snapshot_hash)
        ):
            raise ValueError("coordinator store or policy identity mismatch")
        self.store = store
        self.budget_policy = BudgetPolicyV2.model_validate(budget_policy.model_dump(mode="python"))
        self.expected_policy_snapshot_hash = expected_policy_snapshot_hash
        self._published_bundles: dict[int, PhaseRevisionBundleV1] = {}
        self._authorizations: dict[int, _AuthorizationRecord] = {}

    def _validate_pair(
        self,
        pair: PhaseTracePair[Any, Any],
        *,
        phase_id: PhaseId,
        input_type: type[Any],
        output_type: type[Any],
        expected_run_id: str,
    ) -> tuple[PhaseRequest[Any], PhaseOutcome[Any]]:
        request = revalidate_request(
            pair.request,
            phase_id=phase_id,
            input_type=input_type,
        )
        outcome = revalidate_outcome(
            request,
            pair.outcome,
            output_type=output_type,
        )
        if (
            request.run_id != expected_run_id
            or request.policy_snapshot_hash != self.expected_policy_snapshot_hash
        ):
            raise ValueError
        return request, outcome

    def _validate_trace(
        self,
        bundle: PhaseRevisionBundleV1,
    ) -> tuple[tuple[Any, ...], tuple[Any, ...], str]:
        if not _is_issued_plan(bundle.plan):
            raise ValueError
        request = RevisionPublishRequestV1.model_validate(bundle.request.model_dump(mode="python"))
        if (
            request.run_id != bundle.plan.run_id
            or request.producer_id != COORDINATOR_PRODUCER_ID
            or request.producer_version != COORDINATOR_PRODUCER_VERSION
            or request.proposed_revision != 1
            or request.expected_revision is not None
            or request.expected_manifest_sha256 is not None
            or request.expected_pointer_sha256 is not None
        ):
            raise ValueError
        by_name = {artifact.logical_name: artifact for artifact in request.artifacts}
        if len(by_name) != len(request.artifacts) or "state.json" in by_name:
            raise ValueError
        final_artifact = by_name.get("final_report.json")
        if (
            final_artifact is None
            or final_artifact.artifact_schema_version != "poker-final-report-artifact-v2"
        ):
            raise ValueError
        for artifact in request.artifacts:
            _scan_artifact(artifact, self.store.max_artifact_bytes)
        inventories, heads, parsed = build_inventory(
            request,
            max_artifact_bytes=self.store.max_artifact_bytes,
        )

        trace = bundle.trace
        synthesis_request, synthesis_outcome = self._validate_pair(
            trace.synthesis,
            phase_id=PhaseId.SYNTHESIS,
            input_type=SynthesisInput,
            output_type=SynthesisOutput,
            expected_run_id=request.run_id,
        )
        synthesis_output = cast(SynthesisOutput, synthesis_outcome.output)
        replayed_synthesis = revalidate_outcome(
            synthesis_request,
            SynthesisService().run(synthesis_request),
            output_type=SynthesisOutput,
        )
        if (
            synthesis_outcome.status is not PhaseStatus.SUCCEEDED
            or synthesis_outcome.warnings
            or synthesis_outcome.requested_next_state != "completed"
            or synthesis_output is None
            or synthesis_request.input.run_id != request.run_id
            or synthesis_request.input.machine_state != "FINAL_SYNTHESIS"
            or not synthesis_request.input.completed
            or synthesis_output.report != parsed["final_report.json"]
            or synthesis_outcome != replayed_synthesis
        ):
            raise ValueError

        seen_attempts = {synthesis_request.attempt_id}
        context_dispatches: list[Any] = []
        seen_context_ids: set[str] = set()
        seen_context_attempt_ids: set[str] = set()
        for context_pair in trace.context_builds:
            context_request, context_outcome = self._validate_pair(
                context_pair,
                phase_id=PhaseId.CONTEXT_BUILD,
                input_type=ContextBuildInput,
                output_type=ContextBuildOutput,
                expected_run_id=request.run_id,
            )
            if context_request.attempt_id in seen_attempts:
                raise ValueError
            seen_attempts.add(context_request.attempt_id)
            context_output = cast(ContextBuildOutput, context_outcome.output)
            replayed_context = revalidate_outcome(
                context_request,
                ContextBuildService().run(context_request),
                output_type=ContextBuildOutput,
            )
            if (
                context_outcome.status is not PhaseStatus.SUCCEEDED
                or context_outcome != replayed_context
                or context_request.input.case != parsed["normalized_case.json"]
                or context_request.context_ids != (context_request.input.context_id,)
                or context_request.input.context_id in seen_context_ids
                or context_request.input.context_attempt_id in seen_context_attempt_ids
            ):
                raise ValueError
            seen_context_ids.add(context_request.input.context_id)
            seen_context_attempt_ids.add(context_request.input.context_attempt_id)
            for dispatch in context_output.dispatches:
                envelope = dispatch.envelope
                validated = validate_context_envelope(
                    envelope,
                    dispatch.assignment,
                    run_id=request.run_id,
                    expected_context_id=envelope.lineage.context_id,
                    attempt_id=envelope.lineage.attempt_id,
                    now=envelope.created_at,
                    expected_parent_context_id=envelope.lineage.parent_context_id,
                    expected_source_sha256=(
                        envelope.lineage.source_sha256
                        if envelope.lineage.parent_context_id is not None
                        else None
                    ),
                )
                if validated != dispatch.context:
                    raise ValueError
                context_dispatches.append(dispatch)

        analysis_outputs: list[AnalysisOutput] = []
        expected_context_bindings: list[ContextBindingV1] = []
        used_dispatches: list[Any] = []
        for analysis_pair in trace.analyses:
            analysis_request, analysis_outcome = self._validate_pair(
                analysis_pair,
                phase_id=PhaseId.ANALYSIS,
                input_type=AnalysisInput,
                output_type=AnalysisOutput,
                expected_run_id=request.run_id,
            )
            if analysis_request.attempt_id in seen_attempts:
                raise ValueError
            seen_attempts.add(analysis_request.attempt_id)
            analysis_output = cast(AnalysisOutput, analysis_outcome.output)
            validate_analysis_output(analysis_request, analysis_output)
            if analysis_request.context_ids != (
                analysis_request.input.dispatch.envelope.lineage.context_id,
            ):
                raise ValueError
            analysis_outputs.append(analysis_output)
            used_dispatches.append(analysis_request.input.dispatch)
            expected_context_bindings.append(_context_binding(analysis_pair))
            report_artifact = by_name.get(f"agent_reports/{analysis_output.report.report_id}.json")
            if (
                report_artifact is None
                or report_artifact.exact_bytes != canonical_json_bytes(analysis_output.report)
                or _phase_binding(analysis_request, analysis_outcome)
                not in _bindings_of_type(report_artifact, PhaseBindingV1)
                or expected_context_bindings[-1]
                not in _bindings_of_type(report_artifact, ContextBindingV1)
            ):
                raise ValueError
        if Counter(phase_canonical_sha256(dispatch) for dispatch in context_dispatches) != Counter(
            phase_canonical_sha256(dispatch) for dispatch in used_dispatches
        ):
            raise ValueError
        if tuple(output.assignment for output in analysis_outputs) != tuple(
            parsed["assignments.json"]
        ):
            raise ValueError
        expected_report_names = {
            f"agent_reports/{output.report.report_id}.json" for output in analysis_outputs
        }
        actual_report_names = {
            name for name in by_name if name.startswith("agent_reports/") and name.endswith(".json")
        }
        if actual_report_names != expected_report_names:
            raise ValueError

        tool_results: list[Any] = []
        expected_tool_bindings: list[ToolBindingV1] = []
        for tool_pair in trace.tool_research:
            tool_request, tool_outcome = self._validate_pair(
                tool_pair,
                phase_id=PhaseId.TOOL_RESEARCH,
                input_type=ToolResearchInput,
                output_type=ToolResearchOutput,
                expected_run_id=request.run_id,
            )
            if tool_request.attempt_id in seen_attempts:
                raise ValueError
            seen_attempts.add(tool_request.attempt_id)
            tool_output = cast(ToolResearchOutput, tool_outcome.output)
            validate_tool_research_output(tool_request, tool_output)
            expected_phase = _phase_binding(tool_request, tool_outcome)
            for binding in tool_output.bindings:
                _validate_tool_result_contract(binding.result)
                expected_tool = _tool_binding(tool_pair, binding, by_name)
                input_artifact = by_name[f"tool_results/{binding.result.result_id}.input.json"]
                result_artifact = by_name[f"tool_results/{binding.result.result_id}.json"]
                if (
                    input_artifact.exact_bytes != canonical_json_bytes(binding.request.input)
                    or result_artifact.exact_bytes != canonical_json_bytes(binding.result)
                    or _bindings_of_type(input_artifact, ToolBindingV1) != (expected_tool,)
                    or _bindings_of_type(result_artifact, ToolBindingV1) != (expected_tool,)
                    or _bindings_of_type(input_artifact, PhaseBindingV1) != (expected_phase,)
                    or _bindings_of_type(result_artifact, PhaseBindingV1) != (expected_phase,)
                ):
                    raise ValueError
                expected_tool_bindings.append(expected_tool)
                tool_results.append(binding.result)
        if sorted(binding.ordinal for binding in expected_tool_bindings) != list(
            range(len(expected_tool_bindings))
        ):
            raise ValueError
        tool_results = [
            result
            for _ordinal, result in sorted(
                zip(
                    (binding.ordinal for binding in expected_tool_bindings),
                    tool_results,
                    strict=True,
                )
            )
        ]

        synthesis_input = synthesis_request.input
        if (
            synthesis_input.case != parsed["normalized_case.json"]
            or synthesis_input.evidence_records != tuple(parsed["evidence.jsonl"])
            or synthesis_input.approvals != tuple(parsed["approvals.json"])
            or synthesis_input.execution_records != tuple(parsed["agent_execution_records.json"])
            or synthesis_input.security_events != tuple(parsed["security_events.json"])
            or synthesis_input.disputes != tuple(parsed["disputes.json"])
            or tuple(output.report for output in analysis_outputs) != synthesis_input.reports
            or tuple(output.execution_record for output in analysis_outputs)
            != synthesis_input.execution_records
            or tuple(tool_results) != synthesis_input.tool_results
            or tuple(synthesis_output.report.tool_results) != tuple(tool_results)
            or tuple(synthesis_output.report.agent_execution_records)
            != synthesis_input.execution_records
        ):
            raise ValueError

        expected_intents = (
            ("agent_execution_records", "agent_execution_records.json", "application/json"),
            ("security_events", "security_events.json", "application/json"),
            ("state", "state.json", "application/json"),
            ("approvals", "approvals.json", "application/json"),
            ("disputes", "disputes.json", "application/json"),
            ("final_report_json", "final_report.json", "application/json"),
            ("final_report_markdown", "final_report.md", "text/markdown"),
        )
        observed_intents = tuple(
            (intent.kind.value, intent.relative_path, intent.media_type)
            for intent in synthesis_outcome.artifact_intents
        )
        if observed_intents != expected_intents:
            raise ValueError
        artifact_intents: list[ArtifactIntentSnapshotV1] = []
        for intent in synthesis_outcome.artifact_intents:
            if intent.relative_path == "state.json":
                if intent.kind.value != "state" or intent.content_sha256 is not None:
                    raise ValueError
                content_sha256 = None
            else:
                materialized_artifact = by_name.get(intent.relative_path)
                if (
                    materialized_artifact is None
                    or materialized_artifact.media_type != intent.media_type
                ):
                    raise ValueError
                content_sha256 = sha256_bytes(materialized_artifact.exact_bytes)
                if intent.content_sha256 not in {None, content_sha256}:
                    raise ValueError
            artifact_intents.append(
                ArtifactIntentSnapshotV1(
                    kind=intent.kind.value,
                    relative_path=intent.relative_path,
                    media_type=intent.media_type,
                    content_sha256=content_sha256,
                )
            )
        expected_synthesis_phase = _phase_binding(
            synthesis_request,
            synthesis_outcome,
            artifact_intents=tuple(artifact_intents),
        )
        if _bindings_of_type(final_artifact, PhaseBindingV1) != (expected_synthesis_phase,):
            raise ValueError
        final_contexts = _bindings_of_type(final_artifact, ContextBindingV1)
        if set(final_contexts) != set(expected_context_bindings):
            raise ValueError
        budget_bindings = _bindings_of_type(final_artifact, BudgetPolicyBindingV1)
        if budget_bindings != (
            BudgetPolicyBindingV1(
                policy_schema_version=self.budget_policy.schema_version,
                policy_sha256=self.budget_policy.canonical_sha256,
            ),
        ):
            raise ValueError
        return inventories, heads, _transaction_digest(request, inventories, heads)

    def _initial_history_is_empty(self, request: RevisionPublishRequestV1) -> bool:
        control = self.store.runs_root / request.run_id / ".revision-store"
        current = control / "current.json"
        revisions = control / "revisions"
        return not current.exists() and (not revisions.exists() or not any(revisions.iterdir()))

    def publish(
        self,
        bundle: PhaseRevisionBundleV1,
    ) -> PhaseTransitionAuthorizationV1 | PhaseRevisionFailureV1:
        try:
            inventories, heads, expected_transaction_sha256 = self._validate_trace(bundle)
            del inventories, heads
            prior = self._published_bundles.get(id(bundle))
            if prior is not bundle and not self._initial_history_is_empty(bundle.request):
                return _failure(PhaseRevisionFailureCode.PUBLISH_CONFLICT)
        except _SecretDetected:
            return _failure(PhaseRevisionFailureCode.SECRET_DETECTED)
        except _UnsupportedPayload:
            return _failure(PhaseRevisionFailureCode.UNSUPPORTED_PAYLOAD)
        except _PersistenceDenied:
            return _failure(PhaseRevisionFailureCode.PERSISTENCE_DENIED)
        except Exception:
            return _failure(PhaseRevisionFailureCode.INVALID_TRACE)

        try:
            raw_outcome = self.store.publish(bundle.request)
        except RunStorageError as error:
            failure = error.failure
            if (
                failure.reconciliation_required
                or failure.domain_effect in {"current_may_have_advanced", "current_advanced"}
                or failure.code
                in {
                    RunStorageFailureCode.EFFECT_UNKNOWN,
                    RunStorageFailureCode.DURABILITY_UNCONFIRMED,
                }
            ):
                return _failure(PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
            if failure.code in {
                RunStorageFailureCode.PERSISTENCE_FORBIDDEN,
                RunStorageFailureCode.ENCRYPTION_REQUIRED,
            }:
                return _failure(PhaseRevisionFailureCode.PERSISTENCE_DENIED)
            return _failure(PhaseRevisionFailureCode.PUBLISH_CONFLICT)
        except Exception:
            return _failure(PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
        try:
            if not isinstance(raw_outcome, RevisionPublishOutcomeV1):
                raise TypeError
            outcome = RevisionPublishOutcomeV1.model_validate(raw_outcome.model_dump(mode="python"))
            current = self.store.read_current(bundle.request.run_id)
            if (
                outcome.outcome_kind not in {"published", "current_committed"}
                or outcome.run_id_sha256 != run_id_sha256(bundle.request.run_id)
                or outcome.transaction_id != bundle.request.transaction_id
                or outcome.transaction_sha256 != expected_transaction_sha256
                or outcome.revision != bundle.request.proposed_revision
                or outcome.observed_current_revision != bundle.request.proposed_revision
                or outcome.manifest_sha256 is None
                or outcome.pointer_sha256 is None
                or outcome.durability_evidence.reconciliation != "confirmed"
                or current.run_id != bundle.request.run_id
                or current.current_revision != outcome.revision
                or current.manifest_sha256 != outcome.manifest_sha256
                or current.current_pointer_sha256 != outcome.pointer_sha256
                or not current.reachable_history
                or current.reachable_history[0].transaction_id != bundle.request.transaction_id
                or current.reachable_history[0].transaction_sha256 != expected_transaction_sha256
            ):
                raise ValueError
        except Exception:
            return _failure(PhaseRevisionFailureCode.PUBLISH_UNCERTAIN)
        if outcome.outcome_kind == "current_committed" and (
            self._published_bundles.get(id(bundle)) is not bundle
        ):
            return _failure(PhaseRevisionFailureCode.PUBLISH_CONFLICT)

        capability = object()
        authorization = self._issue_authorization(
            bundle=bundle,
            outcome=outcome,
            capability=capability,
        )
        self._published_bundles[id(bundle)] = bundle
        self._authorizations[id(capability)] = _AuthorizationRecord(
            capability=capability,
            bundle=bundle,
            authorization=authorization,
            data=_authorization_data(authorization),
        )
        return authorization

    def _issue_authorization(
        self,
        *,
        bundle: PhaseRevisionBundleV1,
        outcome: RevisionPublishOutcomeV1,
        capability: object,
    ) -> PhaseTransitionAuthorizationV1:
        authorization = object.__new__(PhaseTransitionAuthorizationV1)
        values = {
            "schema_version": "1.0.0",
            "plan": bundle.plan,
            "run_id_sha256": outcome.run_id_sha256,
            "transaction_id": outcome.transaction_id,
            "transaction_sha256": outcome.transaction_sha256,
            "revision": outcome.revision,
            "manifest_sha256": cast(str, outcome.manifest_sha256),
            "pointer_sha256": cast(str, outcome.pointer_sha256),
            "outcome_kind": outcome.outcome_kind,
            "_issuer_capability": capability,
        }
        for name, value in values.items():
            object.__setattr__(authorization, name, value)
        return authorization

    def authorization_matches(
        self,
        bundle: PhaseRevisionBundleV1,
        authorization: PhaseTransitionAuthorizationV1,
    ) -> bool:
        try:
            capability = authorization._issuer_capability
            record = self._authorizations.get(id(capability))
            if (
                record is None
                or record.capability is not capability
                or record.bundle is not bundle
                or record.authorization is not authorization
                or record.data != _authorization_data(authorization)
                or authorization.plan is not bundle.plan
                or not _is_issued_plan(bundle.plan)
                or authorization.schema_version != "1.0.0"
                or authorization.run_id_sha256 != run_id_sha256(bundle.request.run_id)
                or authorization.transaction_id != bundle.request.transaction_id
                or authorization.revision != bundle.request.proposed_revision
                or authorization.outcome_kind not in {"published", "current_committed"}
            ):
                return False
            inventories, heads, expected_transaction_sha256 = self._validate_trace(bundle)
            del inventories, heads
            return authorization.transaction_sha256 == expected_transaction_sha256
        except Exception:
            return False


PhaseRevisionPublishResult = PhaseTransitionAuthorizationV1 | PhaseRevisionFailureV1


__all__ = [
    "COORDINATOR_PRODUCER_ID",
    "COORDINATOR_PRODUCER_VERSION",
    "TRANSITION_REASON",
    "PhaseRevisionBundleV1",
    "PhaseRevisionCoordinator",
    "PhaseRevisionFailureCode",
    "PhaseRevisionFailureV1",
    "PhaseRevisionPublishResult",
    "PhaseRevisionTraceV1",
    "PhaseTracePair",
    "PhaseTransitionApplyResultV1",
    "PhaseTransitionAuthorizationV1",
    "PhaseTransitionPlanV1",
]
