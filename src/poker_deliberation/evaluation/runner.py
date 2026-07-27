"""Pure, bounded execution of the P3-017A offline evaluation suite."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from poker_deliberation.capabilities import CAPABILITIES
from poker_deliberation.evaluation.canonical import (
    CASE_INPUT_DOMAIN,
    DATASET_CONTENT_DOMAIN,
    SOURCE_CONFIG_DOMAIN,
    TOOL_CONTRACT_DOMAIN,
    TOOL_EVIDENCE_DOMAIN,
    TOOL_INPUT_DOMAIN,
    TOOL_OUTPUT_DOMAIN,
    canonical_domain_sha256,
    canonical_json_bytes,
    domain_sha256,
    parse_canonical_model,
    sha256_bytes,
)
from poker_deliberation.evaluation.models import (
    CaseOutcomeV1,
    DatasetManifestV1,
    EvaluationCaseV1,
    EvaluationDatasetV1,
    EvaluationMetadataProbeV1,
    EvaluationResultV1,
    EvaluationSourceBindingV1,
    EvaluationSuiteV1,
    EvaluationSummaryV1,
    ScorerConfigV1,
    StructuredFailureV1,
    ToolEvidenceV1,
    ratio_decimal,
)
from poker_deliberation.runtime_conformance import (
    ApprovalBindingV1,
    AssignmentV1,
    BudgetReferenceV1,
    ConformanceRecordV1,
    ContextProvenanceV1,
    ContextReferenceV1,
    ExecutionAuditV1,
    ExecutionState,
    ReproductionMetadataV1,
    ResultStatus,
    ResultV1,
    RuntimeId,
    RuntimeInventoryV1,
    SemanticRole,
    ToolCapabilityAllowlistV1,
    ToolResultReferenceV1,
    build_runtime_inventories,
    compare_records,
    runtime_inventory_sha256,
)
from poker_deliberation.runtime_conformance.canonical import (
    ALLOWLIST_DOMAIN,
    APPROVAL_BINDING_DOMAIN,
    CONTEXT_REFERENCE_DOMAIN,
)
from poker_deliberation.runtime_conformance.canonical import (
    canonical_domain_sha256 as conformance_domain_sha256,
)
from poker_deliberation.runtime_conformance.canonical import (
    domain_sha256 as conformance_sha256,
)
from poker_deliberation.schemas import EpistemicLabel, ToolResult, ToolStatus
from poker_deliberation.tools import default_registry


class EvaluationLoadError(ValueError):
    """A suite or its repository-owned inputs failed closed."""

    def __init__(self, code: str, path: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.path = path


@dataclass(frozen=True, slots=True)
class LoadedEvaluation:
    suite: EvaluationSuiteV1
    suite_sha256: str
    manifest: DatasetManifestV1
    manifest_sha256: str
    dataset: EvaluationDatasetV1
    scorer: ScorerConfigV1
    scorer_sha256: str


@dataclass(frozen=True, slots=True)
class _ExecutionContext:
    repository_root: Path
    loaded: LoadedEvaluation
    now: datetime
    codex_inventory: RuntimeInventoryV1
    python_inventory: RuntimeInventoryV1
    source_revision_sha256: str
    source_binding: EvaluationSourceBindingV1


def _resolve_repository_file(root: Path, relative: str, *, field: str) -> Path:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise EvaluationLoadError(
            "repository-path-escape",
            field,
            "evaluation input path escapes the repository",
        ) from exc
    if not candidate.is_file():
        raise EvaluationLoadError(
            "evaluation-input-missing",
            field,
            "evaluation input file is missing",
        )
    return candidate


def _read_canonical_model(
    path: Path,
    model: type[DatasetManifestV1]
    | type[EvaluationDatasetV1]
    | type[EvaluationSuiteV1]
    | type[ScorerConfigV1],
) -> tuple[Any, bytes]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise EvaluationLoadError(
            "evaluation-input-unreadable",
            path.name,
            "evaluation input file could not be read",
        ) from exc
    try:
        return parse_canonical_model(data, model), data
    except ValueError as exc:
        raise EvaluationLoadError(
            "evaluation-input-invalid",
            path.name,
            "evaluation input is not canonical or violates its strict schema",
        ) from exc


def load_evaluation_suite(repository_root: Path, suite_relative: str) -> LoadedEvaluation:
    """Load and cross-check all canonical suite inputs without executing cases."""

    root = repository_root.resolve()
    suite_path = _resolve_repository_file(root, suite_relative, field="suite")
    suite, suite_bytes = _read_canonical_model(suite_path, EvaluationSuiteV1)
    assert isinstance(suite, EvaluationSuiteV1)

    manifest_path = _resolve_repository_file(
        root,
        suite.dataset_manifest_path,
        field="dataset_manifest_path",
    )
    manifest, manifest_bytes = _read_canonical_model(manifest_path, DatasetManifestV1)
    assert isinstance(manifest, DatasetManifestV1)
    manifest_sha256 = sha256_bytes(manifest_bytes)
    if manifest_sha256 != suite.dataset_manifest_sha256:
        raise EvaluationLoadError(
            "dataset-manifest-hash-mismatch",
            "dataset_manifest_sha256",
            "dataset manifest differs from the suite binding",
        )

    scorer_path = _resolve_repository_file(root, suite.scorer_path, field="scorer_path")
    scorer, scorer_bytes = _read_canonical_model(scorer_path, ScorerConfigV1)
    assert isinstance(scorer, ScorerConfigV1)
    scorer_sha256 = sha256_bytes(scorer_bytes)
    if scorer_sha256 != suite.scorer_sha256:
        raise EvaluationLoadError(
            "scorer-hash-mismatch",
            "scorer_sha256",
            "scorer config differs from the suite binding",
        )

    dataset_path = _resolve_repository_file(root, manifest.cases_path, field="cases_path")
    dataset, _dataset_bytes = _read_canonical_model(dataset_path, EvaluationDatasetV1)
    assert isinstance(dataset, EvaluationDatasetV1)
    if (dataset.dataset_id, dataset.dataset_version) != (
        manifest.dataset_id,
        manifest.dataset_version,
    ):
        raise EvaluationLoadError(
            "dataset-identity-mismatch",
            "dataset_id",
            "dataset identity differs from its manifest",
        )
    if len(dataset.cases) != manifest.case_count:
        raise EvaluationLoadError(
            "dataset-case-count-mismatch",
            "case_count",
            "declared and observed dataset case counts differ",
        )
    for case in dataset.cases:
        if canonical_domain_sha256(CASE_INPUT_DOMAIN, case.input) != case.input_sha256:
            raise EvaluationLoadError(
                "case-input-hash-mismatch",
                "input_sha256",
                "evaluation case input differs from its declared binding",
            )
    if canonical_domain_sha256(DATASET_CONTENT_DOMAIN, dataset) != manifest.content_sha256:
        raise EvaluationLoadError(
            "dataset-content-hash-mismatch",
            "content_sha256",
            "dataset content differs from its manifest",
        )

    license_path = _resolve_repository_file(root, manifest.license_path, field="license_path")
    if sha256_bytes(license_path.read_bytes()) != manifest.license_sha256:
        raise EvaluationLoadError(
            "dataset-license-hash-mismatch",
            "license_sha256",
            "dataset license differs from its manifest",
        )

    metrics_path = _resolve_repository_file(root, "evals/metrics.json", field="metric_id")
    try:
        metrics_raw = json.loads(metrics_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvaluationLoadError(
            "metric-registry-invalid",
            "metric_id",
            "metric registry could not be read",
        ) from exc
    metrics = metrics_raw.get("metrics") if isinstance(metrics_raw, dict) else None
    if (
        not isinstance(metrics, list)
        or not all(isinstance(item, str) for item in metrics)
        or scorer.metric_id not in metrics
    ):
        raise EvaluationLoadError(
            "metric-not-registered",
            "metric_id",
            "scorer metric is absent from the metric registry",
        )

    return LoadedEvaluation(
        suite=suite,
        suite_sha256=sha256_bytes(suite_bytes),
        manifest=manifest,
        manifest_sha256=manifest_sha256,
        dataset=dataset,
        scorer=scorer,
        scorer_sha256=scorer_sha256,
    )


def _evaluation_time(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise EvaluationLoadError(
            "evaluation-time-invalid",
            "evaluation_time_utc",
            "evaluation time is not a valid UTC second",
        ) from exc


def _tool_evidence(result: ToolResult) -> ToolEvidenceV1:
    return ToolEvidenceV1(
        tool_name=result.tool_name,
        contract_version=result.contract_version,
        status=result.status.value,
        exactness=result.exactness.value,
        numeric_exactness=result.numeric_exactness.value,
        input_sha256=canonical_domain_sha256(TOOL_INPUT_DOMAIN, result.input),
        output_sha256=canonical_domain_sha256(TOOL_OUTPUT_DOMAIN, result.output),
        verification_passed=result.verification is not None and result.verification.passed,
        reproduce_command=(
            result.reproduce_command
            or (
                f"poker-deliberate calculate {result.tool_name} "
                "--analysis-scope retrospective --input <input.json>"
            )
        ),
    )


def _context_reference(
    *,
    now: datetime,
    producer_runtime: RuntimeId,
    consumer_runtime: RuntimeId,
) -> ContextReferenceV1:
    return ContextReferenceV1(
        reference_kind="fixture",
        context_id="offline-evaluation-context",
        context_schema_version="1.0.0",
        classification="internal",
        created_at=now,
        expires_at=None,
        payload_sha256=conformance_sha256(
            "poker-offline-evaluation-context-payload-v1",
            b"retrospective-fixture",
        ),
        policy_sha256=conformance_sha256(
            "poker-offline-evaluation-context-policy-v1",
            b"internal-read-only",
        ),
        envelope_sha256=None,
        provenance=ContextProvenanceV1(
            source_kind="fixture",
            source_sha256=conformance_sha256(
                "poker-offline-evaluation-context-source-v1",
                b"repository-owned-synthetic-fixture",
            ),
            producer_runtime=producer_runtime,
            consumer_runtime=consumer_runtime,
            parent_context_id="offline-evaluation-root",
        ),
        budget=BudgetReferenceV1(
            policy_schema_version="1.0.0",
            policy_sha256=conformance_sha256(
                "poker-offline-evaluation-budget-policy-v1",
                b"runtime-ms=5000;output-bytes=16384",
            ),
            maximum_runtime_ms=5_000,
            maximum_output_bytes=16_384,
            reference_kind="exact-policy",
        ),
    )


def _conformance_record(
    inventory: RuntimeInventoryV1,
    *,
    now: datetime,
    source_revision_sha256: str,
    producer_runtime: RuntimeId,
    runtime_role_id: str,
    context_producer: RuntimeId,
    context_consumer: RuntimeId,
    catalog_status: str,
) -> ConformanceRecordV1:
    context = _context_reference(
        now=now,
        producer_runtime=context_producer,
        consumer_runtime=context_consumer,
    )
    allowlist = ToolCapabilityAllowlistV1(
        policy_version="1.0.0",
        allowed_tools=(),
        allowed_capabilities=(),
        catalog_status=catalog_status,  # type: ignore[arg-type]
        policy_source="fixture",
    )
    approval = ApprovalBindingV1(
        requirement="not-required",
        decision="not-applicable",
    )
    objective = "Validate the same retrospective synthetic context without external execution."
    assignment = AssignmentV1(
        assignment_id=f"{producer_runtime.value}-offline-evaluation",
        producer_runtime=producer_runtime,
        runtime_role_id=runtime_role_id,
        semantic_role=SemanticRole.INTAKE,
        objective=objective,
        objective_sha256=conformance_sha256(
            "poker-runtime-conformance-objective-v1",
            objective.encode("utf-8"),
        ),
        parent_assignment_id="offline-evaluation-root",
        context=context,
        allowlist=allowlist,
        approval=approval,
        role_inventory_sha256=runtime_inventory_sha256(inventory),
    )
    result = ResultV1(
        result_id=f"{producer_runtime.value}-offline-evaluation-result",
        status=ResultStatus.LIMITED,
        summary="The synthetic context was checked without a provider, solver, or bridge.",
        epistemic_label=EpistemicLabel.UNKNOWN,
    )
    audit = ExecutionAuditV1(
        execution_id=f"{producer_runtime.value}-offline-evaluation-execution",
        producer_runtime=producer_runtime,
        execution_kind="fixture",
        terminal_status="succeeded",
        external_effect=False,
        started_at=now,
        completed_at=now,
        timing_evidence="complete",
        context_sha256=conformance_domain_sha256(CONTEXT_REFERENCE_DOMAIN, context),
        allowlist_sha256=conformance_domain_sha256(ALLOWLIST_DOMAIN, allowlist),
        approval_binding_sha256=conformance_domain_sha256(
            APPROVAL_BINDING_DOMAIN,
            approval,
        ),
        reproduction=ReproductionMetadataV1(
            framework_version="0.1.0",
            source_commit_id=source_revision_sha256,
            source_commit_status="known",
            tool_contract_versions=(),
        ),
    )
    return ConformanceRecordV1(
        producer_runtime=producer_runtime,
        assignment=assignment,
        result=result,
        error=None,
        execution_state=ExecutionState.EXECUTED,
        execution_audit=audit,
    )


def _record_pair(context: _ExecutionContext) -> tuple[ConformanceRecordV1, ConformanceRecordV1]:
    source = _conformance_record(
        context.codex_inventory,
        now=context.now,
        source_revision_sha256=context.source_revision_sha256,
        producer_runtime=RuntimeId.CODEX_NATIVE,
        runtime_role_id="intake-reconstructor",
        context_producer=RuntimeId.CODEX_NATIVE,
        context_consumer=RuntimeId.CODEX_NATIVE,
        catalog_status="undeclared",
    )
    target = _conformance_record(
        context.python_inventory,
        now=context.now,
        source_revision_sha256=context.source_revision_sha256,
        producer_runtime=RuntimeId.PYTHON_ORCHESTRATOR,
        runtime_role_id="intake",
        context_producer=RuntimeId.CODEX_NATIVE,
        context_consumer=RuntimeId.PYTHON_ORCHESTRATOR,
        catalog_status="declared",
    )
    return source, target


def _failure(
    code: str,
    category: str,
    path: str,
    message: str,
    *,
    retryable: bool = False,
) -> StructuredFailureV1:
    return StructuredFailureV1(
        code=code,
        category=category,  # type: ignore[arg-type]
        path=path,
        retryable=retryable,
        message=message,
    )


def _outcome(
    case: EvaluationCaseV1,
    actual: tuple[str, ...],
    *,
    observed_status: str,
    failure: StructuredFailureV1 | None = None,
    tool_evidence: ToolEvidenceV1 | None = None,
) -> CaseOutcomeV1:
    ordered = tuple(sorted(actual, key=lambda item: item.encode("utf-8")))
    expected = case.expected_evidence.tokens
    exact = ordered == expected
    return CaseOutcomeV1(
        case_id=case.case_id,
        case_kind=case.case_kind,
        input_sha256=case.input_sha256,
        observed_status=observed_status,  # type: ignore[arg-type]
        expected_evidence=expected,
        actual_evidence=ordered,
        exact_match=exact,
        matched_case_count=int(exact),  # type: ignore[arg-type]
        failure=failure,
        tool_evidence=tool_evidence,
    )


def _pot_odds_case(case: EvaluationCaseV1, context: _ExecutionContext) -> CaseOutcomeV1:
    inputs = case.input
    payload = {
        "pot_before_bet": inputs.pot_before_bet,
        "opponent_bet": inputs.opponent_bet,
        "call_cost": inputs.call_cost,
        "expected_rake": inputs.expected_rake,
    }
    result = default_registry().execute("pot_odds", payload)
    evidence = _tool_evidence(result)
    if (
        result.status is not ToolStatus.SUCCESS
        or result.output is None
        or result.verification is None
        or not result.verification.passed
    ):
        return _outcome(
            case,
            ("calculator:execution-failed",),
            observed_status="rejected",
            failure=_failure(
                "calculator-execution-failed",
                "tool",
                "tool_result",
                "The typed calculator result did not pass its executable verification.",
            ),
            tool_evidence=evidence,
        )

    required_values = (
        inputs.pot_before_bet,
        inputs.opponent_bet,
        inputs.call_cost,
        inputs.expected_rake,
        inputs.oracle_numerator,
        inputs.oracle_denominator,
    )
    if any(value is None for value in required_values):
        raise AssertionError("validated calculator case lost required values")
    pot, bet, call, rake, oracle_numerator, oracle_denominator = required_values
    final_pot = pot + bet + call - rake  # type: ignore[operator]
    independent_oracle = Fraction(call, final_pot)  # type: ignore[arg-type]
    declared_oracle = Fraction(oracle_numerator, oracle_denominator)  # type: ignore[arg-type]
    actual_oracle = Fraction(str(result.output["required_equity"]))
    if case.case_kind == "normal" and actual_oracle == independent_oracle == declared_oracle:
        tool_reference = ToolResultReferenceV1(
            result_id="offline-evaluation-pot-odds",
            tool_name=result.tool_name,
            contract_version=result.contract_version,
            status="success",
            exactness=result.numeric_exactness.value,
            result_sha256=canonical_domain_sha256(TOOL_EVIDENCE_DOMAIN, evidence),
        )
        epistemic_result = ResultV1(
            result_id="offline-evaluation-calculated-result",
            status=ResultStatus.SUCCEEDED,
            summary="The verified pot-odds output supports a CALCULATED result.",
            epistemic_label=EpistemicLabel.CALCULATED,
            tool_results=(tool_reference,),
        )
        source, target = _record_pair(context)
        check = compare_records(
            source,
            target,
            context.codex_inventory,
            context.python_inventory,
            now=context.now,
        )
        if (
            check.status == "conformant"
            and epistemic_result.epistemic_label is EpistemicLabel.CALCULATED
        ):
            actual: tuple[str, ...] = (
                "calculator:oracle-match",
                "calculator:pot_odds:floating-verified",
                "context:semantics-preserved",
                "epistemic-label:calculated",
                "external-effect:false",
                "routing:python-orchestrator",
                "runtime-bridge:false",
            )
            return _outcome(
                case,
                actual,
                observed_status="succeeded",
                tool_evidence=evidence,
            )
        actual = tuple(f"runtime-conformance:{violation.code}" for violation in check.violations)
        return _outcome(
            case,
            actual,
            observed_status="rejected",
            failure=_failure(
                "runtime-conformance-failed",
                "integrity",
                "runtime.conformance",
                "The normal runtime conformance pair was not conformant.",
            ),
            tool_evidence=evidence,
        )
    return _outcome(
        case,
        ("calculator:oracle-mismatch",),
        observed_status="rejected",
        failure=_failure(
            "calculator-oracle-mismatch",
            "tool",
            "input.oracle",
            "The declared rational oracle differs from the verified calculator output.",
        ),
        tool_evidence=evidence,
    )


def _runtime_mismatch_case(
    case: EvaluationCaseV1,
    context: _ExecutionContext,
) -> CaseOutcomeV1:
    source, target = _record_pair(context)
    if case.case_kind == "context-provenance-mismatch":
        provenance = target.assignment.context.provenance.model_copy(
            update={"source_sha256": "f" * 64}
        )
        changed_context = target.assignment.context.model_copy(update={"provenance": provenance})
        assignment = target.assignment.model_copy(update={"context": changed_context})
        if target.execution_audit is None:
            raise AssertionError("validated fixture record lacks execution audit")
        audit = target.execution_audit.model_copy(
            update={
                "context_sha256": conformance_domain_sha256(
                    CONTEXT_REFERENCE_DOMAIN,
                    changed_context,
                )
            }
        )
        target = target.model_copy(update={"assignment": assignment, "execution_audit": audit})
        expected_code = "context-provenance-mismatch"
        failure_path = "runtime.context.provenance"
    else:
        allowlist = ToolCapabilityAllowlistV1(
            policy_version="1.0.0",
            allowed_tools=("pot_odds",),
            allowed_capabilities=(),
            catalog_status="declared",
            policy_source="fixture",
        )
        assignment = target.assignment.model_copy(update={"allowlist": allowlist})
        if target.execution_audit is None:
            raise AssertionError("validated fixture record lacks execution audit")
        audit = target.execution_audit.model_copy(
            update={
                "allowlist_sha256": conformance_domain_sha256(
                    ALLOWLIST_DOMAIN,
                    allowlist,
                )
            }
        )
        target = target.model_copy(update={"assignment": assignment, "execution_audit": audit})
        expected_code = "allowlist-semantic-mismatch"
        failure_path = "runtime.assignment.allowlist"
    check = compare_records(
        source,
        target,
        context.codex_inventory,
        context.python_inventory,
        now=context.now,
    )
    codes = tuple(item.code for item in check.violations)
    actual = tuple(f"runtime-conformance:{code}" for code in codes)
    return _outcome(
        case,
        actual,
        observed_status="rejected",
        failure=_failure(
            expected_code,
            "integrity",
            failure_path,
            "The synthetic runtime mismatch was rejected with structured evidence.",
        ),
    )


def _contract_probe_case(
    case: EvaluationCaseV1,
    context: _ExecutionContext,
) -> CaseOutcomeV1:
    loaded = context.loaded
    model: type[ScorerConfigV1] | type[EvaluationSuiteV1] | type[EvaluationDatasetV1]
    if case.case_kind == "missing-denominator":
        raw = loaded.scorer.model_dump(mode="python")
        raw.pop("denominator_policy")
        model = ScorerConfigV1
        missing_field = "denominator_policy"
        token = "contract-rejection:denominator_policy"
    elif case.case_kind == "missing-scorer":
        raw = loaded.suite.model_dump(mode="python")
        raw.pop("scorer_path")
        model = EvaluationSuiteV1
        missing_field = "scorer_path"
        token = "contract-rejection:scorer_path"
    else:
        raw = loaded.dataset.model_dump(mode="python")
        raw.pop("dataset_version")
        model = EvaluationDatasetV1
        missing_field = "dataset_version"
        token = "contract-rejection:dataset_version"
    try:
        model.model_validate(raw, strict=True)
    except ValidationError as exc:
        locations = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
        if missing_field in locations:
            return _outcome(
                case,
                (token,),
                observed_status="rejected",
                failure=_failure(
                    "missing-required-contract-field",
                    "configuration",
                    missing_field,
                    "A required evaluation contract field was absent.",
                ),
            )
    return _outcome(
        case,
        ("contract-rejection:failed-open",),
        observed_status="rejected",
        failure=_failure(
            "contract-probe-failed-open",
            "configuration",
            missing_field,
            "A malformed evaluation contract was unexpectedly accepted.",
        ),
    )


def _unsupported_solver_case(case: EvaluationCaseV1) -> CaseOutcomeV1:
    result = default_registry().execute("solver_status", {})
    evidence = _tool_evidence(result)
    limited_result = ResultV1(
        result_id="unsupported-solver-status",
        status=ResultStatus.LIMITED,
        summary="No qualified solver evidence is available.",
        epistemic_label=EpistemicLabel.UNKNOWN,
    )
    rejected = False
    try:
        ResultV1(
            result_id="unsupported-equilibrium-claim",
            status=ResultStatus.LIMITED,
            summary="No qualified solver evidence is available.",
            epistemic_label=EpistemicLabel.UNKNOWN,
            strategy_claim="equilibrium",
            solver_evidence=None,
        )
    except ValidationError:
        rejected = True
    if (
        result.status is ToolStatus.UNAVAILABLE
        and limited_result.epistemic_label is EpistemicLabel.UNKNOWN
        and rejected
    ):
        return _outcome(
            case,
            (
                "epistemic-label:unknown",
                "solver-claim:rejected",
                "solver-status:unavailable",
            ),
            observed_status="rejected",
            failure=_failure(
                "unsupported-solver-claim",
                "unsupported",
                "result.strategy_claim",
                "An equilibrium claim without qualified solver evidence was rejected.",
            ),
            tool_evidence=evidence,
        )
    return _outcome(
        case,
        ("solver-claim:failed-open",),
        observed_status="rejected",
        failure=_failure(
            "solver-claim-failed-open",
            "unsupported",
            "result.strategy_claim",
            "The unsupported solver claim did not fail closed.",
        ),
        tool_evidence=evidence,
    )


def _secret_metadata_case(case: EvaluationCaseV1) -> CaseOutcomeV1:
    synthetic = "api" + "_" + "key=" + "synthetic" + "-" + "canary" + "-" + "value"
    try:
        EvaluationMetadataProbeV1(label=synthetic)
    except ValidationError:
        return _outcome(
            case,
            ("metadata-canary:rejected",),
            observed_status="rejected",
            failure=_failure(
                "synthetic-secret-metadata",
                "security",
                "metadata.label",
                "Secret-shaped metadata was rejected without echoing its value.",
            ),
        )
    return _outcome(
        case,
        ("metadata-canary:failed-open",),
        observed_status="rejected",
        failure=_failure(
            "secret-metadata-failed-open",
            "security",
            "metadata.label",
            "Secret-shaped metadata was unexpectedly accepted.",
        ),
    )


def _timeout_case(case: EvaluationCaseV1) -> CaseOutcomeV1:
    timeout_ms = case.input.timeout_ms
    elapsed_ms = case.input.simulated_elapsed_ms
    if timeout_ms is None or elapsed_ms is None:
        raise AssertionError("validated timeout case lost its bounds")
    if elapsed_ms > timeout_ms:
        return _outcome(
            case,
            ("external-effect:false", "timeout:structured"),
            observed_status="timed-out",
            failure=_failure(
                "evaluation-timeout",
                "timeout",
                "execution.timeout",
                "The synthetic bounded execution exceeded its declared timeout.",
                retryable=False,
            ),
        )
    return _outcome(
        case,
        ("timeout:failed-open",),
        observed_status="rejected",
        failure=_failure(
            "timeout-probe-failed-open",
            "timeout",
            "execution.timeout",
            "The timeout fixture did not exceed its bound.",
        ),
    )


def _execute_case(case: EvaluationCaseV1, context: _ExecutionContext) -> CaseOutcomeV1:
    if case.case_kind in {"normal", "calculator-oracle-mismatch"}:
        return _pot_odds_case(case, context)
    if case.case_kind in {
        "context-provenance-mismatch",
        "role-allowlist-mismatch",
    }:
        return _runtime_mismatch_case(case, context)
    if case.case_kind in {
        "missing-denominator",
        "missing-scorer",
        "missing-version",
    }:
        return _contract_probe_case(case, context)
    if case.case_kind == "unsupported-solver-claim":
        return _unsupported_solver_case(case)
    if case.case_kind == "synthetic-secret-metadata":
        return _secret_metadata_case(case)
    if case.case_kind == "structured-timeout":
        return _timeout_case(case)
    raise AssertionError(f"unsupported validated case kind: {case.case_kind}")


def run_evaluation(
    repository_root: Path,
    suite_relative: str,
    *,
    source_commit_id: str,
    source_tree_id: str,
) -> EvaluationResultV1:
    """Execute a loaded suite entirely offline and return a deterministic result."""

    loaded = load_evaluation_suite(repository_root, suite_relative)
    registry = default_registry()
    descriptions = registry.describe()
    tool_names = tuple(sorted(registry.names(), key=lambda item: item.encode("utf-8")))
    capability_ids = tuple(
        sorted(
            (capability.capability_id for capability in CAPABILITIES),
            key=lambda item: item.encode("utf-8"),
        )
    )
    source_revision_sha256 = domain_sha256(
        "poker-offline-evaluation-source-revision-v1",
        source_commit_id.encode("ascii"),
    )
    codex_inventory, python_inventory = build_runtime_inventories(
        repository_root.resolve(),
        source_revision=source_revision_sha256,
        python_tool_catalog=tool_names,
        python_capability_catalog=capability_ids,
    )
    versions = tuple(
        sorted(
            (
                (str(description["name"]), str(description["contract_version"]))
                for description in descriptions
            ),
            key=lambda item: item[0].encode("utf-8"),
        )
    )
    binding = EvaluationSourceBindingV1(
        source_commit_id=source_commit_id,
        source_tree_id=source_tree_id,
        config_sha256=canonical_domain_sha256(
            SOURCE_CONFIG_DOMAIN,
            {
                "dataset_manifest_sha256": loaded.manifest_sha256,
                "dataset_content_sha256": loaded.manifest.content_sha256,
                "scorer_sha256": loaded.scorer_sha256,
                "suite_sha256": loaded.suite_sha256,
            },
        ),
        suite_sha256=loaded.suite_sha256,
        dataset_manifest_sha256=loaded.manifest_sha256,
        dataset_content_sha256=loaded.manifest.content_sha256,
        scorer_sha256=loaded.scorer_sha256,
        tool_contract_sha256=canonical_domain_sha256(
            TOOL_CONTRACT_DOMAIN,
            descriptions,
        ),
        codex_runtime_inventory_sha256=runtime_inventory_sha256(codex_inventory),
        python_runtime_inventory_sha256=runtime_inventory_sha256(python_inventory),
        tool_contract_versions=versions,
    )
    context = _ExecutionContext(
        repository_root=repository_root.resolve(),
        loaded=loaded,
        now=_evaluation_time(loaded.suite.evaluation_time_utc),
        codex_inventory=codex_inventory,
        python_inventory=python_inventory,
        source_revision_sha256=source_revision_sha256,
        source_binding=binding,
    )
    outcomes = tuple(_execute_case(case, context) for case in loaded.dataset.cases)
    matched = sum(item.matched_case_count for item in outcomes)
    denominator = loaded.manifest.case_count
    summary = EvaluationSummaryV1(
        declared_case_count=denominator,
        observed_case_count=len(outcomes),
        matched_case_count=matched,
        mismatched_case_count=len(outcomes) - matched,
        numerator=matched,
        denominator=denominator,
        score=ratio_decimal(matched, denominator),
        threshold=loaded.scorer.threshold,
        decision=(
            "pass"
            if Fraction(matched, denominator) >= Fraction(loaded.scorer.threshold)
            else "fail"
        ),
    )
    return EvaluationResultV1(
        suite_id=loaded.suite.suite_id,
        suite_version=loaded.suite.suite_version,
        dataset_id=loaded.dataset.dataset_id,
        dataset_version=loaded.dataset.dataset_version,
        scorer_id=loaded.scorer.scorer_id,
        scorer_version=loaded.scorer.scorer_version,
        aggregation=loaded.scorer.aggregation,
        denominator_policy=loaded.scorer.denominator_policy,
        source=binding,
        outcomes=outcomes,
        summary=summary,
    )


def result_bytes(result: EvaluationResultV1) -> bytes:
    return canonical_json_bytes(result)


__all__ = [
    "EvaluationLoadError",
    "LoadedEvaluation",
    "load_evaluation_suite",
    "result_bytes",
    "run_evaluation",
]
