"""Deterministic exact-evidence evaluation for the P3-030D workflow."""

from __future__ import annotations

import hashlib
import importlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation import bounded_river_review_workflow as workflow_api
from poker_deliberation.bounded_river_review_workflow import (
    BoundedRiverReviewWorkflowError,
    bounded_river_confirmation_hashes,
    bounded_river_review_confirmation_preview,
    bounded_river_review_report_view,
    bounded_river_review_role_request_preview,
    confirm_bounded_river_review_role_request,
    confirm_bounded_river_review_workflow,
    execute_bounded_river_review_role,
    prepare_bounded_river_review_workflow,
    replay_bounded_river_review_workflow,
    resume_bounded_river_review_workflow,
    run_bounded_river_review_workflow,
)
from poker_deliberation.bounded_river_review_workflow_models import (
    BoundedRiverReviewRoleConfirmationBindingV1,
    BoundedRiverReviewWorkflowStatusV1,
)
from poker_deliberation.codex_bridge.controller import (
    BoundedCodexBridgeController,
    role_artifact_name,
)
from poker_deliberation.codex_bridge.identity import (
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
    RuntimeAuthModeV1,
)
from poker_deliberation.codex_bridge.replay import replay_bridge
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore, VerifiedBridgeRead
from poker_deliberation.codex_bridge.transport import DeterministicReadOnlyTransport
from poker_deliberation.config import AppConfig
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.storage.revision_canonical import (
    canonical_domain_sha256,
    canonical_json_bytes,
    parse_canonical_model,
    sha256_bytes,
)

_RESULT_HASH_DOMAIN = "poker-bounded-river-review-workflow-evaluation-result-v1"
_SOURCE_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_METRIC_ORDER = (
    "confirmation_binding",
    "exact_decision_math",
    "runtime_mode_and_roles",
    "resume_and_replay",
    "local_data_separation",
)
MetricName = Literal[
    "confirmation_binding",
    "exact_decision_math",
    "runtime_mode_and_roles",
    "resume_and_replay",
    "local_data_separation",
]


class _EvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class BoundedRiverReviewWorkflowFixtureV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    fixture_id: Literal["p3-030d-bounded-river-review-workflow-v1"]
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_equity_numerator: int
    expected_equity_denominator: int = Field(gt=0)
    expected_required_equity_numerator: int
    expected_required_equity_denominator: int = Field(gt=0)
    expected_call_ev_numerator: int
    expected_call_ev_denominator: int = Field(gt=0)
    expected_action_comparison: Literal["call", "fold", "tie"]
    expected_pending_roles: tuple[
        Literal["strategy-analyst"],
        Literal["math-tool-auditor"],
        Literal["skeptic-falsifier"],
        Literal["adjudicator"],
        Literal["report-writer"],
    ]


class BoundedRiverReviewWorkflowMetricV1(_EvaluationModel):
    metric: MetricName
    passed: bool
    evidence: tuple[str, ...]


class BoundedRiverReviewWorkflowEvaluationResultV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    evaluation_id: Literal["p3-030d-bounded-river-review-workflow-evaluation-v1"] = (
        "p3-030d-bounded-river-review-workflow-evaluation-v1"
    )
    source_commit_id: str = Field(pattern=_SOURCE_ID_PATTERN)
    source_tree_id: str = Field(pattern=_SOURCE_ID_PATTERN)
    fixture_id: Literal["p3-030d-bounded-river-review-workflow-v1"]
    fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    metrics: tuple[BoundedRiverReviewWorkflowMetricV1, ...]
    score_milli: int = Field(ge=0, le=1000)
    passed: bool
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def closed_score(self) -> BoundedRiverReviewWorkflowEvaluationResultV1:
        if tuple(item.metric for item in self.metrics) != _METRIC_ORDER:
            raise ValueError("workflow evaluation metric order mismatch")
        passed_count = sum(item.passed for item in self.metrics)
        expected_score = passed_count * 1000 // len(self.metrics)
        if self.score_milli != expected_score or self.passed != (passed_count == len(self.metrics)):
            raise ValueError("workflow evaluation score mismatch")
        return self


def load_bounded_river_review_workflow_fixture(
    path: Path,
) -> tuple[BoundedRiverReviewWorkflowFixtureV1, str]:
    data = path.read_bytes()
    fixture = BoundedRiverReviewWorkflowFixtureV1.model_validate_json(data, strict=True)
    return fixture, sha256_bytes(data)


def _metric(
    name: MetricName,
    passed: bool,
    evidence: tuple[str, ...],
) -> BoundedRiverReviewWorkflowMetricV1:
    return BoundedRiverReviewWorkflowMetricV1(
        metric=name,
        passed=passed,
        evidence=evidence if passed else (),
    )


def _without_result_hash(
    result: BoundedRiverReviewWorkflowEvaluationResultV1,
) -> dict[str, object]:
    value = result.model_dump(mode="json")
    value.pop("result_sha256")
    return value


def run_bounded_river_review_workflow_evaluation(
    fixture: BoundedRiverReviewWorkflowFixtureV1,
    *,
    fixture_sha256: str,
    source_path: Path,
    range_path: Path,
    repository_root: Path,
    work_root: Path,
    source_commit_id: str,
    source_tree_id: str,
) -> BoundedRiverReviewWorkflowEvaluationResultV1:
    if work_root.exists():
        raise ValueError("workflow evaluation work root must not already exist")
    work_root.mkdir(parents=True)
    source_bytes = source_path.read_bytes()
    range_bytes = range_path.read_bytes().removesuffix(b"\n")
    range_definition = parse_canonical_model(range_bytes, VersionedRangeDefinitionV1)
    fixture_binding = (
        hashlib.sha256(source_bytes).hexdigest() == fixture.source_sha256
        and hashlib.sha256(range_bytes).hexdigest() == fixture.range_sha256
        and canonical_json_bytes(range_definition) == range_bytes
    )
    storage = work_root / "s"
    storage.mkdir()
    config = AppConfig(
        runs_dir=storage / "l",
        revision_runs_dir=storage / "p",
        durable_budget_runs_dir=storage / "b",
    )
    workflow_root = work_root / "w"
    fixed_time = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
    plan, preparation = prepare_bounded_river_review_workflow(
        source_bytes,
        range_definition,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id="p3-030d-evaluation",
        intake_id="intake-p3-030d-evaluation",
        source_run_id="run-p3-030d-evaluation",
        bridge_run_id="bridge-p3-030d-evaluation",
        source_id="fixture-p3-030d-evaluation",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
        repository_commit_id=source_commit_id,
        repository_tree_id=source_tree_id,
        auth_mode=RuntimeAuthModeV1.LOCAL_ONLY,
        clock=lambda: fixed_time,
    )
    if preparation.candidate is None:
        raise ValueError("workflow evaluation preparation was blocked")
    preview = bounded_river_review_confirmation_preview(plan, preparation)
    preview_hashes = preview["expected_hashes"]
    if not isinstance(preview_hashes, dict):
        raise ValueError("workflow evaluation confirmation preview is malformed")
    hashes = bounded_river_confirmation_hashes(preparation.candidate)
    now = datetime.now(UTC)
    confirmation = confirm_bounded_river_review_workflow(
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=plan.workflow_id,
        authority_id="local-evaluation-user",
        confirmation_id="confirmation-p3-030d-evaluation",
        idempotency_key="idempotency-p3-030d-evaluation",
        expected_plan_sha256=plan.plan_sha256,
        expected_hashes=hashes,
        confirmed_at=now,
        expires_at=now + timedelta(hours=1),
    )
    completed = run_bounded_river_review_workflow(
        source_bytes,
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=plan.workflow_id,
        clock=lambda: fixed_time + timedelta(minutes=2),
    )
    resumed = resume_bounded_river_review_workflow(
        None,
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=plan.workflow_id,
    )
    replayed = replay_bounded_river_review_workflow(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=plan.workflow_id,
    )
    directories = tuple(path.parent for path in workflow_root.rglob("plan.json"))
    if len(directories) != 1:
        raise ValueError("workflow evaluation namespace is ambiguous")
    directory = directories[0]
    bridge_source = BoundedCodexBridgeController(
        BoundedCodexBridgeStore(directory / "bridge")
    ).read_source_context(plan.bridge_run_id)
    math = bridge_source.math
    control_files = tuple(path for path in directory.rglob("*") if path.is_file())
    metrics = (
        _metric(
            "confirmation_binding",
            fixture_binding
            and len(hashes) == 12
            and tuple(preview_hashes.values()) == hashes
            and confirmation.candidate_sha256 == preparation.candidate.candidate_sha256,
            ("fixture-and-twelve-hash-confirmation-bound",),
        ),
        _metric(
            "exact_decision_math",
            (
                math.equity.numerator,
                math.equity.denominator,
                math.required_equity.numerator,
                math.required_equity.denominator,
                math.call_ev_units.numerator,
                math.call_ev_units.denominator,
                math.action_comparison,
            )
            == (
                fixture.expected_equity_numerator,
                fixture.expected_equity_denominator,
                fixture.expected_required_equity_numerator,
                fixture.expected_required_equity_denominator,
                fixture.expected_call_ev_numerator,
                fixture.expected_call_ev_denominator,
                fixture.expected_action_comparison,
            ),
            ("stored-p3-030c-exact-math-matches-fixture",),
        ),
        _metric(
            "runtime_mode_and_roles",
            completed.state == "completed_local_only"
            and completed.auth_mode is RuntimeAuthModeV1.LOCAL_ONLY
            and completed.completed_roles == ()
            and tuple(item.value for item in completed.pending_roles)
            == fixture.expected_pending_roles
            and completed.bridge_status == "approval_required",
            ("local-only-five-role-plan-without-execution",),
        ),
        _metric(
            "resume_and_replay",
            resumed == completed
            and replayed == completed
            and (directory / "linkage.json").is_file(),
            ("status-resume-replay-and-linkage-agree",),
        ),
        _metric(
            "local_data_separation",
            all(source_bytes not in path.read_bytes() for path in control_files)
            and not tuple(directory.rglob("runtime")),
            ("workflow-and-bridge-omit-raw-source-and-runtime-scratch",),
        ),
    )
    passed_count = sum(item.passed for item in metrics)
    provisional = BoundedRiverReviewWorkflowEvaluationResultV1(
        source_commit_id=source_commit_id,
        source_tree_id=source_tree_id,
        fixture_id=fixture.fixture_id,
        fixture_sha256=fixture_sha256,
        source_sha256=fixture.source_sha256,
        range_sha256=fixture.range_sha256,
        candidate_sha256=preparation.candidate.candidate_sha256,
        plan_sha256=plan.plan_sha256,
        metrics=metrics,
        score_milli=passed_count * 1000 // len(metrics),
        passed=passed_count == len(metrics),
        result_sha256="0" * 64,
    )
    return provisional.model_copy(
        update={
            "result_sha256": canonical_domain_sha256(
                _RESULT_HASH_DOMAIN,
                _without_result_hash(provisional),
            )
        }
    )


# P3-030G deliberately keeps the P3-030D V1 result above stable.  V2 is a
# separate, stricter public contract that exercises the supervised P3-030F
# lifecycle and records only hashes, counts, and closed status codes.
_RESULT_V2_HASH_DOMAIN = "poker-bounded-river-review-workflow-evaluation-result-v2"
_CONFIRMATION_FIELDS_HASH_DOMAIN = "poker-bounded-river-review-workflow-confirmation-fields-v1"
_CASE_ORDER_V2 = (
    "source-workflow-identity",
    "workflow-confirmation-binding",
    "five-role-supervision",
    "p2-artifact-lineage",
    "terminal-replay-report",
    "repository-runtime-identity",
)
_METRIC_ORDER_V2 = (
    "source_workflow_identity",
    "workflow_confirmation_binding",
    "five_role_supervision",
    "p2_artifact_lineage",
    "terminal_replay_report",
    "repository_runtime_identity",
)
_CONFIRMATION_FIELD_ORDER = (
    "expected_plan_sha256",
    "expected_linkage_sha256",
    "expected_bridge_revision",
    "expected_bridge_manifest_sha256",
    "expected_bridge_inventory_sha256",
    "expected_bridge_pointer_sha256",
    "expected_role",
    "expected_auth_mode",
    "expected_request_sha256",
    "expected_request_bytes_sha256",
    "expected_envelope_sha256",
    "expected_runtime_policy_sha256",
    "expected_runtime_identity",
    "expected_model_provider",
    "expected_model",
    "expected_credential_reference",
    "expected_remote_retention_policy",
)
_FIXTURE_V2_RELATIVE = "tests/fixtures/bounded_river_review_workflow/v2/scenarios.json"
_SOURCE_V1_RELATIVE = "tests/fixtures/bounded_river_review_workflow/v1/source-ja.txt"
_RANGE_V1_RELATIVE = "tests/fixtures/bounded_river_review_workflow/v1/range.json"
EvaluationStatusV2 = Literal["pass", "fail", "UNKNOWN"]
CaseIdV2 = Literal[
    "source-workflow-identity",
    "workflow-confirmation-binding",
    "five-role-supervision",
    "p2-artifact-lineage",
    "terminal-replay-report",
    "repository-runtime-identity",
]
MetricNameV2 = Literal[
    "source_workflow_identity",
    "workflow_confirmation_binding",
    "five_role_supervision",
    "p2_artifact_lineage",
    "terminal_replay_report",
    "repository_runtime_identity",
]


class BoundedRiverReviewWorkflowEvaluationError(ValueError):
    """Stable fail-closed evaluator error without source or credential values."""


class BoundedRiverReviewWorkflowFixtureV2(_EvaluationModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    fixture_id: Literal["p3-030g-bounded-river-review-workflow-v2"]
    source_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_sha256: str = Field(pattern=_SHA256_PATTERN)
    expected_confirmation_hash_count: Literal[12]
    expected_confirmation_field_count: Literal[17]
    expected_roles: tuple[BridgeRole, ...] = Field(min_length=5, max_length=5)
    case_ids: tuple[CaseIdV2, ...] = Field(min_length=6, max_length=6)
    metric_ids: tuple[MetricNameV2, ...] = Field(min_length=6, max_length=6)

    @model_validator(mode="after")
    def exact_fixture_inventory(self) -> BoundedRiverReviewWorkflowFixtureV2:
        if (
            self.expected_roles != BRIDGE_ROLE_ORDER
            or self.case_ids != _CASE_ORDER_V2
            or self.metric_ids != _METRIC_ORDER_V2
        ):
            raise ValueError("workflow V2 fixture inventory mismatch")
        return self


class BoundedRiverReviewWorkflowCaseV2(_EvaluationModel):
    case_id: CaseIdV2
    status: EvaluationStatusV2
    passed: bool
    failure_code: str | None = Field(default=None, pattern=r"^BRWE_E_[A-Z0-9_]+$")
    expected_evidence_sha256: tuple[str, ...] = Field(min_length=1, max_length=32)
    observed_evidence_sha256: tuple[str, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def closed_status(self) -> BoundedRiverReviewWorkflowCaseV2:
        if self.passed != (self.status == "pass"):
            raise ValueError("workflow V2 case status mismatch")
        if (self.status == "pass") != (self.failure_code is None):
            raise ValueError("workflow V2 case failure-code mismatch")
        exact = self.expected_evidence_sha256 == self.observed_evidence_sha256
        if (self.status == "pass") != exact:
            raise ValueError("workflow V2 case evidence mismatch")
        return self


class BoundedRiverReviewWorkflowMetricV2(_EvaluationModel):
    metric: MetricNameV2
    status: EvaluationStatusV2
    passed: bool
    failure_code: str | None = Field(default=None, pattern=r"^BRWE_E_[A-Z0-9_]+$")
    expected_evidence_sha256: tuple[str, ...] = Field(min_length=1, max_length=32)
    observed_evidence_sha256: tuple[str, ...] = Field(max_length=32)

    @model_validator(mode="after")
    def closed_status(self) -> BoundedRiverReviewWorkflowMetricV2:
        if self.passed != (self.status == "pass"):
            raise ValueError("workflow V2 metric status mismatch")
        if (self.status == "pass") != (self.failure_code is None):
            raise ValueError("workflow V2 metric failure-code mismatch")
        exact = self.expected_evidence_sha256 == self.observed_evidence_sha256
        if (self.status == "pass") != exact:
            raise ValueError("workflow V2 metric evidence mismatch")
        return self


class BoundedRiverReviewWorkflowEvaluationResultV2(_EvaluationModel):
    schema_version: Literal["2.0.0"] = "2.0.0"
    evaluation_id: Literal["p3-030g-bounded-river-review-workflow-evaluation-v2"] = (
        "p3-030g-bounded-river-review-workflow-evaluation-v2"
    )
    fixture_id: Literal["p3-030g-bounded-river-review-workflow-v2"]
    fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_fixture_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_definition_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_commit_id: str = Field(pattern=_SOURCE_ID_PATTERN)
    source_tree_id: str = Field(pattern=_SOURCE_ID_PATTERN)
    workflow_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    plan_sha256: str = Field(pattern=_SHA256_PATTERN)
    workflow_confirmation_sha256: str = Field(pattern=_SHA256_PATTERN)
    linkage_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_terminal_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_terminal_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_terminal_manifest_sha256: str = Field(pattern=_SHA256_PATTERN)
    bridge_terminal_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    final_report_artifact_sha256: str = Field(pattern=_SHA256_PATTERN)
    confirmation_hashes_sha256: str = Field(pattern=_SHA256_PATTERN)
    role_confirmation_receipts_sha256: str = Field(pattern=_SHA256_PATTERN)
    role_confirmation_fields_sha256: tuple[str, ...] = Field(min_length=5, max_length=5)
    all_confirmation_field_mutations_sha256: str = Field(pattern=_SHA256_PATTERN)
    p2_artifact_lineage_sha256: str = Field(pattern=_SHA256_PATTERN)
    terminal_replay_report_sha256: str = Field(pattern=_SHA256_PATTERN)
    runtime_source_inventory_sha256: str = Field(pattern=_SHA256_PATTERN)
    cases: tuple[BoundedRiverReviewWorkflowCaseV2, ...] = Field(
        min_length=6,
        max_length=6,
    )
    metrics: tuple[BoundedRiverReviewWorkflowMetricV2, ...] = Field(
        min_length=6,
        max_length=6,
    )
    score_milli: int = Field(ge=0, le=1000)
    status: EvaluationStatusV2
    passed: bool
    transport_qualification: Literal["deterministic_fixture"]
    live_qualification_status: Literal["UNKNOWN"]
    actual_backend_model_input: Literal["UNKNOWN"]
    api_live_executed: Literal[False]
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def exact_closed_result(self) -> BoundedRiverReviewWorkflowEvaluationResultV2:
        if tuple(item.case_id for item in self.cases) != _CASE_ORDER_V2:
            raise ValueError("workflow V2 evaluation case order mismatch")
        if tuple(item.metric for item in self.metrics) != _METRIC_ORDER_V2:
            raise ValueError("workflow V2 evaluation metric order mismatch")
        passed_metrics = sum(item.passed for item in self.metrics)
        every_check_passed = all(item.passed for item in self.cases) and all(
            item.passed for item in self.metrics
        )
        expected_score = passed_metrics * 1000 // len(self.metrics)
        any_failed = any(item.status == "fail" for item in self.cases) or any(
            item.status == "fail" for item in self.metrics
        )
        any_unknown = any(item.status == "UNKNOWN" for item in self.cases) or any(
            item.status == "UNKNOWN" for item in self.metrics
        )
        expected_status = "fail" if any_failed else ("UNKNOWN" if any_unknown else "pass")
        evidence = (
            (self.fixture_sha256, self.plan_sha256, self.source_terminal_manifest_sha256),
            (self.confirmation_hashes_sha256, self.workflow_confirmation_sha256),
            (
                self.role_confirmation_receipts_sha256,
                *self.role_confirmation_fields_sha256,
                self.all_confirmation_field_mutations_sha256,
            ),
            (self.p2_artifact_lineage_sha256,),
            (self.terminal_replay_report_sha256, self.final_report_artifact_sha256),
            (self.runtime_source_inventory_sha256, self.runtime_source_inventory_sha256),
        )
        if (
            self.score_milli != expected_score
            or self.status != expected_status
            or self.passed != every_check_passed
            or tuple(item.expected_evidence_sha256 for item in self.cases) != evidence
            or tuple(item.expected_evidence_sha256 for item in self.metrics) != evidence
            or any(
                case.observed_evidence_sha256 != metric.observed_evidence_sha256
                for case, metric in zip(self.cases, self.metrics, strict=True)
            )
            or self.result_sha256
            != canonical_domain_sha256(
                _RESULT_V2_HASH_DOMAIN,
                _without_result_hash_v2(self),
            )
        ):
            raise ValueError("workflow V2 evaluation result mismatch")
        return self


def _without_result_hash_v2(
    result: BoundedRiverReviewWorkflowEvaluationResultV2,
) -> dict[str, object]:
    value = result.model_dump(mode="json")
    value.pop("result_sha256")
    return value


def load_bounded_river_review_workflow_fixture_v2(
    path: Path,
) -> tuple[BoundedRiverReviewWorkflowFixtureV2, str]:
    try:
        data = path.read_bytes()
        fixture = BoundedRiverReviewWorkflowFixtureV2.model_validate_json(data, strict=True)
    except (OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_FIXTURE") from exc
    return fixture, sha256_bytes(data)


def load_bounded_river_review_workflow_evaluation_result_v2(
    path: Path,
) -> BoundedRiverReviewWorkflowEvaluationResultV2:
    try:
        data = path.read_bytes()
        result = parse_canonical_model(
            data,
            BoundedRiverReviewWorkflowEvaluationResultV2,
        )
        if canonical_json_bytes(result) != data:
            raise ValueError("non-canonical workflow V2 evaluation result")
        return result
    except (OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_RESULT") from exc


def verify_bounded_river_review_workflow_evaluation_result_v2(
    result: BoundedRiverReviewWorkflowEvaluationResultV2,
    *,
    repository_root: Path,
    fixture_path: Path,
    source_path: Path,
    range_path: Path,
    source_commit_id: str,
    source_tree_id: str,
) -> bool:
    """Rebind one self-hashed result to trusted checkout and fixture context."""

    try:
        validated = BoundedRiverReviewWorkflowEvaluationResultV2.model_validate(
            result.model_dump(mode="python"),
            strict=True,
        )
        if (
            validated != result
            or result.source_commit_id != source_commit_id
            or result.source_tree_id != source_tree_id
        ):
            return False
        _verify_v2_repository_identity(
            repository_root,
            source_commit_id=source_commit_id,
            source_tree_id=source_tree_id,
        )
        runtime_inventory = bridge_runtime_source_inventory(repository_root)
        runtime_inventory_sha256 = bridge_runtime_source_inventory_sha256(repository_root)
        inventory_by_path = {item.path: item.sha256 for item in runtime_inventory}
        if (
            len(inventory_by_path) != len(runtime_inventory)
            or runtime_inventory_sha256 != result.runtime_source_inventory_sha256
        ):
            return False
        repository = repository_root.resolve(strict=True)
        resolved_fixture = fixture_path.resolve(strict=True)
        resolved_source = source_path.resolve(strict=True)
        resolved_range = range_path.resolve(strict=True)
        if (
            resolved_fixture != repository.joinpath(*_FIXTURE_V2_RELATIVE.split("/"))
            or resolved_source != repository.joinpath(*_SOURCE_V1_RELATIVE.split("/"))
            or resolved_range != repository.joinpath(*_RANGE_V1_RELATIVE.split("/"))
        ):
            return False
        fixture, fixture_sha256 = load_bounded_river_review_workflow_fixture_v2(resolved_fixture)
        source_bytes = resolved_source.read_bytes()
        range_file_bytes = resolved_range.read_bytes()
        range_definition_bytes = range_file_bytes.removesuffix(b"\n")
        range_definition = parse_canonical_model(
            range_definition_bytes,
            VersionedRangeDefinitionV1,
        )
        source_sha256 = sha256_bytes(source_bytes)
        range_file_sha256 = sha256_bytes(range_file_bytes)
        range_definition_sha256 = sha256_bytes(range_definition_bytes)
        return (
            canonical_json_bytes(range_definition) == range_definition_bytes
            and fixture.fixture_id == result.fixture_id
            and fixture_sha256 == result.fixture_sha256
            and fixture.source_sha256 == source_sha256
            and fixture.range_sha256 == range_definition_sha256
            and fixture.case_ids == tuple(item.case_id for item in result.cases)
            and fixture.metric_ids == tuple(item.metric for item in result.metrics)
            and result.source_fixture_sha256 == source_sha256
            and result.range_fixture_sha256 == range_file_sha256
            and result.range_definition_sha256 == range_definition_sha256
            and inventory_by_path.get(_FIXTURE_V2_RELATIVE) == fixture_sha256
            and inventory_by_path.get(_SOURCE_V1_RELATIVE) == source_sha256
            and inventory_by_path.get(_RANGE_V1_RELATIVE) == range_file_sha256
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False


class _StepClockV2:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        value = self.current
        self.current += timedelta(seconds=1)
        return value


def bounded_river_review_workflow_evaluation_config(work_root: Path) -> AppConfig:
    storage = work_root / "s"
    return AppConfig(
        runs_dir=storage / "l",
        revision_runs_dir=storage / "p",
        durable_budget_runs_dir=storage / "b",
    )


def _verify_v2_repository_identity(
    repository_root: Path,
    *,
    source_commit_id: str,
    source_tree_id: str,
) -> None:
    try:
        verify_bridge_checkout(
            repository_root,
            repository_commit_id=source_commit_id,
            repository_tree_id=source_tree_id,
        )
        verify_bridge_module_origins(repository_root)
        module = importlib.import_module(__name__)
        actual = Path(str(module.__file__)).resolve(strict=True)
        expected = (
            repository_root.resolve(strict=True)
            / "src"
            / "poker_deliberation"
            / "bounded_river_review_workflow_evaluation.py"
        ).resolve(strict=True)
        if actual != expected:
            raise ValueError("evaluator module origin mismatch")
        workflow_actual = Path(str(workflow_api.__file__)).resolve(strict=True)
        workflow_expected = (
            repository_root.resolve(strict=True)
            / "src"
            / "poker_deliberation"
            / "bounded_river_review_workflow.py"
        ).resolve(strict=True)
        if workflow_actual != workflow_expected:
            raise ValueError("workflow module origin mismatch")
    except (OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_REPOSITORY_IDENTITY") from exc


def _v2_check(
    identifier: str,
    passed: bool,
    evidence: tuple[str, ...],
    failure_code: str,
    *,
    metric: bool,
    observed_evidence: tuple[str, ...] | None = None,
) -> BoundedRiverReviewWorkflowCaseV2 | BoundedRiverReviewWorkflowMetricV2:
    expected = evidence
    observed = (
        evidence
        if passed
        else observed_evidence
        or (
            canonical_domain_sha256(
                "poker-bounded-river-review-workflow-evaluation-observed-failure-v1",
                {"identifier": identifier, "evidence": evidence},
            ),
            *evidence,
        )
    )
    common = {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "failure_code": None if passed else failure_code,
        "expected_evidence_sha256": expected,
        "observed_evidence_sha256": observed,
    }
    if metric:
        return BoundedRiverReviewWorkflowMetricV2(metric=identifier, **common)  # type: ignore[arg-type]
    return BoundedRiverReviewWorkflowCaseV2(case_id=identifier, **common)  # type: ignore[arg-type]


def _workflow_directory_v2(workflow_root: Path, workflow_id: str) -> Path:
    directories = tuple(
        path.parent
        for path in workflow_root.rglob("plan.json")
        if f'"workflow_id":"{workflow_id}"'.encode("ascii") in path.read_bytes()
    )
    if len(directories) != 1:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_WORKFLOW_STORAGE")
    return directories[0]


def _workflow_control_tree_sha256(directory: Path) -> str:
    return canonical_domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-control-tree-v1",
        {
            path.relative_to(directory).as_posix(): sha256_bytes(path.read_bytes())
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        },
    )


def _wrong_confirmation_field_value(name: str, value: object) -> object:
    if name == "expected_bridge_revision":
        if not isinstance(value, int):
            raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_ROLE_FIELDS")
        return value + 1
    if name == "expected_role":
        return BridgeRole.MATH_TOOL_AUDITOR
    if name == "expected_auth_mode":
        return RuntimeAuthModeV1.OPENAI_API
    if name == "expected_model":
        return None if value is not None else "gpt-5.6-terra"
    if name.startswith("expected_") and name.endswith("sha256"):
        return "0" * 64 if value != "0" * 64 else "1" * 64
    return "p3-030g-type-valid-mismatch"


def _verify_confirmation_fields_contract(
    *,
    fields: dict[str, object],
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    directory: Path,
) -> str:
    authoritative = tuple(fields[name] for name in _CONFIRMATION_FIELD_ORDER)
    if not workflow_api._exact_role_confirmation_fields_match(authoritative, authoritative):
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_ROLE_FIELDS")
    for name in _CONFIRMATION_FIELD_ORDER:
        mutated = dict(fields)
        mutated[name] = _wrong_confirmation_field_value(name, fields[name])
        supplied = tuple(mutated[item] for item in _CONFIRMATION_FIELD_ORDER)
        if workflow_api._exact_role_confirmation_fields_match(supplied, authoritative):
            raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_ROLE_FIELDS")

    before = _workflow_control_tree_sha256(directory)
    mutated = dict(fields)
    mutated["expected_plan_sha256"] = _wrong_confirmation_field_value(
        "expected_plan_sha256", fields["expected_plan_sha256"]
    )
    try:
        _confirm_v2_role_fields(
            fields=mutated,
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
            authority_id="local-p3-030g-mutation-check",
            confirmation_id="confirmation-p3-030g-mutation-check",
            idempotency_key="idempotency-p3-030g-mutation-check",
        )
    except BoundedRiverReviewWorkflowError as exc:
        if str(exc) != "BRW_E_ROLE_BINDING":
            raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_ROLE_FIELD_MUTATION") from exc
    else:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_ROLE_FIELD_MUTATION")
    if _workflow_control_tree_sha256(directory) != before:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_ROLE_FIELD_MUTATION")
    return canonical_domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-claim-token-v1",
        "exact-seventeen-field-contract-and-production-mismatch-refused",
    )


def _confirm_v2_role_fields(
    *,
    fields: dict[str, object],
    config: AppConfig,
    repository_root: Path,
    workflow_root: Path,
    workflow_id: str,
    authority_id: str,
    confirmation_id: str,
    idempotency_key: str,
) -> BoundedRiverReviewWorkflowStatusV1:
    return confirm_bounded_river_review_role_request(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        authority_id=authority_id,
        confirmation_id=confirmation_id,
        idempotency_key=idempotency_key,
        expected_plan_sha256=cast(str, fields["expected_plan_sha256"]),
        expected_linkage_sha256=cast(str, fields["expected_linkage_sha256"]),
        expected_bridge_revision=cast(int, fields["expected_bridge_revision"]),
        expected_bridge_manifest_sha256=cast(str, fields["expected_bridge_manifest_sha256"]),
        expected_bridge_inventory_sha256=cast(str, fields["expected_bridge_inventory_sha256"]),
        expected_bridge_pointer_sha256=cast(str, fields["expected_bridge_pointer_sha256"]),
        expected_role=cast(BridgeRole, fields["expected_role"]),
        expected_auth_mode=cast(RuntimeAuthModeV1, fields["expected_auth_mode"]),
        expected_request_sha256=cast(str, fields["expected_request_sha256"]),
        expected_request_bytes_sha256=cast(str, fields["expected_request_bytes_sha256"]),
        expected_envelope_sha256=cast(str, fields["expected_envelope_sha256"]),
        expected_runtime_policy_sha256=cast(str, fields["expected_runtime_policy_sha256"]),
        expected_runtime_identity=cast(str, fields["expected_runtime_identity"]),
        expected_model_provider=cast(str, fields["expected_model_provider"]),
        expected_model=cast(str | None, fields["expected_model"]),
        expected_credential_reference=cast(str, fields["expected_credential_reference"]),
        expected_remote_retention_policy=cast(str, fields["expected_remote_retention_policy"]),
    )


def _deterministic_role_executor_kwargs_match(
    kwargs: dict[str, object],
    *,
    config: AppConfig,
    repository_root: Path,
    bridge_root: Path,
    runtime_root: Path,
    bridge_run_id: str,
    role: BridgeRole,
) -> bool:
    expected_keys = {
        "config",
        "repository_root",
        "bridge_root",
        "runtime_root",
        "bridge_run_id",
        "role",
        "auth_mode",
        "codex_binary",
    }
    try:
        return (
            set(kwargs) == expected_keys
            and kwargs["config"] is config
            and isinstance(kwargs["repository_root"], Path)
            and kwargs["repository_root"] == repository_root
            and ".." not in kwargs["repository_root"].parts
            and isinstance(kwargs["bridge_root"], Path)
            and kwargs["bridge_root"] == bridge_root
            and ".." not in kwargs["bridge_root"].parts
            and isinstance(kwargs["runtime_root"], Path)
            and kwargs["runtime_root"] == runtime_root
            and ".." not in kwargs["runtime_root"].parts
            and kwargs["bridge_run_id"] == bridge_run_id
            and kwargs["role"] is role
            and kwargs["auth_mode"] is RuntimeAuthModeV1.CODEX_SUBSCRIPTION
            and kwargs["codex_binary"] is None
        )
    except (KeyError, OSError, ValueError):
        return False


def run_bounded_river_review_workflow_evaluation_v2(
    fixture: BoundedRiverReviewWorkflowFixtureV2,
    *,
    fixture_sha256: str,
    source_path: Path,
    range_path: Path,
    repository_root: Path,
    work_root: Path,
    source_commit_id: str,
    source_tree_id: str,
) -> BoundedRiverReviewWorkflowEvaluationResultV2:
    """Evaluate P3-030F through its public supervised workflow API, without network."""

    if work_root.exists():
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_WORK_ROOT")
    _verify_v2_repository_identity(
        repository_root,
        source_commit_id=source_commit_id,
        source_tree_id=source_tree_id,
    )
    runtime_inventory_before = bridge_runtime_source_inventory_sha256(repository_root)
    try:
        source_bytes = source_path.read_bytes()
        range_bytes = range_path.read_bytes().removesuffix(b"\n")
        range_definition = parse_canonical_model(range_bytes, VersionedRangeDefinitionV1)
    except (OSError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_FIXTURE") from exc
    fixture_bound = (
        sha256_bytes(source_bytes) == fixture.source_sha256
        and sha256_bytes(range_bytes) == fixture.range_sha256
        and canonical_json_bytes(range_definition) == range_bytes
    )
    runtime_inventory = {
        item.path: item.sha256 for item in bridge_runtime_source_inventory(repository_root)
    }
    fixture_bound = fixture_bound and (
        runtime_inventory.get(_FIXTURE_V2_RELATIVE) == fixture_sha256
        and runtime_inventory.get(_SOURCE_V1_RELATIVE) == sha256_bytes(source_bytes)
        and runtime_inventory.get(_RANGE_V1_RELATIVE) == sha256_bytes(range_path.read_bytes())
    )
    work_root.mkdir(parents=True)
    (work_root / "s").mkdir()
    config = bounded_river_review_workflow_evaluation_config(work_root)
    workflow_root = work_root / "w"
    workflow_id = "p3-030g-evaluation"
    evaluation_started_at = datetime.now(UTC)
    plan, preparation = prepare_bounded_river_review_workflow(
        source_bytes,
        range_definition,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        intake_id="intake-p3-030g-evaluation",
        source_run_id="run-p3-030g-evaluation",
        bridge_run_id="bridge-p3-030g-evaluation",
        source_id="fixture-p3-030g-evaluation",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
        repository_commit_id=source_commit_id,
        repository_tree_id=source_tree_id,
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        clock=lambda: evaluation_started_at,
    )
    if preparation.candidate is None:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_PREPARATION")
    preview = bounded_river_review_confirmation_preview(plan, preparation)
    preview_hashes = preview.get("expected_hashes")
    if not isinstance(preview_hashes, dict):
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_CONFIRMATION")
    confirmation_hashes = bounded_river_confirmation_hashes(preparation.candidate)
    confirmed_at = datetime.now(UTC)
    workflow_confirmation = confirm_bounded_river_review_workflow(
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        authority_id="local-p3-030g-evaluator",
        confirmation_id="confirmation-p3-030g-evaluation",
        idempotency_key="idempotency-p3-030g-evaluation",
        expected_plan_sha256=plan.plan_sha256,
        expected_hashes=confirmation_hashes,
        confirmed_at=confirmed_at,
        expires_at=confirmed_at + timedelta(hours=1),
    )
    status = run_bounded_river_review_workflow(
        source_bytes,
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
        clock=lambda: evaluation_started_at + timedelta(minutes=2),
    )
    directory = _workflow_directory_v2(workflow_root, workflow_id)
    bridge_store = BoundedCodexBridgeStore(directory / "bridge")
    execution_clock = _StepClockV2(datetime.now(UTC))
    transport = DeterministicReadOnlyTransport(
        auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
        clock=execution_clock,
    )
    executed_roles: list[BridgeRole] = []

    def deterministic_role_executor(**kwargs: object) -> VerifiedBridgeRead:
        ordinal = len(executed_roles)
        if ordinal >= len(BRIDGE_ROLE_ORDER) or not _deterministic_role_executor_kwargs_match(
            kwargs,
            config=config,
            repository_root=repository_root,
            bridge_root=directory / "bridge",
            runtime_root=work_root / f"runtime-{ordinal}",
            bridge_run_id=plan.bridge_run_id,
            role=BRIDGE_ROLE_ORDER[ordinal],
        ):
            raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_ROLE_EXECUTOR")
        execution_clock.current = max(execution_clock.current, datetime.now(UTC))
        role = kwargs["role"]
        if not isinstance(role, BridgeRole):
            raise ValueError("invalid deterministic role")
        controller = BoundedCodexBridgeController(
            BoundedCodexBridgeStore(Path(str(kwargs["bridge_root"]))),
            clock=execution_clock,
        )
        source = controller.read_source_context(str(kwargs["bridge_run_id"]))
        executed_roles.append(role)
        return controller.execute_confirmed_role(
            str(kwargs["bridge_run_id"]),
            role,
            auth_mode=RuntimeAuthModeV1.CODEX_SUBSCRIPTION,
            current_source_terminal_manifest_sha256=(source.source.source_terminal_manifest_sha256),
            transport=transport,
        )

    role_field_hashes: list[str] = []
    receipt_hashes: list[str] = []
    p2_hashes: list[str] = []
    role_bindings_ok = True
    p2_lineage_ok = True
    all_field_mutations_sha256: str | None = None
    for ordinal, expected_role in enumerate(BRIDGE_ROLE_ORDER):
        role_preview = bounded_river_review_role_request_preview(
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
        )
        fields = role_preview.get("confirmation_fields")
        if (
            not isinstance(fields, dict)
            or tuple(fields) != _CONFIRMATION_FIELD_ORDER
            or len(fields) != fixture.expected_confirmation_field_count
        ):
            raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_ROLE_FIELDS")
        field_hash = canonical_domain_sha256(_CONFIRMATION_FIELDS_HASH_DOMAIN, fields)
        role_field_hashes.append(field_hash)
        if ordinal == 0:
            all_field_mutations_sha256 = _verify_confirmation_fields_contract(
                fields=fields,
                config=config,
                repository_root=repository_root,
                workflow_root=workflow_root,
                workflow_id=workflow_id,
                directory=directory,
            )
        if role_preview.get("next_role") is not expected_role:
            role_bindings_ok = False
        confirmed = _confirm_v2_role_fields(
            fields=fields,
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
            authority_id="local-p3-030g-evaluator",
            confirmation_id=f"confirmation-p3-030g-{ordinal}",
            idempotency_key=f"idempotency-p3-030g-{ordinal}",
        )
        role_bindings_ok = role_bindings_ok and (
            confirmed.next_role is expected_role
            and confirmed.role_state == "executable"
            and executed_roles == list(BRIDGE_ROLE_ORDER[:ordinal])
        )
        runtime_root = work_root / f"runtime-{ordinal}"
        if runtime_root.exists():
            raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_RUNTIME_SCRATCH")
        status = execute_bounded_river_review_role(
            config=config,
            repository_root=repository_root,
            workflow_root=workflow_root,
            workflow_id=workflow_id,
            runtime_root=runtime_root,
            _role_executor=deterministic_role_executor,
        )
        if runtime_root.exists():
            raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_RUNTIME_SCRATCH")
        current = bridge_store.read_current(plan.bridge_run_id)
        receipt_path = directory / f"role-confirmation-binding-{ordinal}-{expected_role.value}.json"
        receipt_bytes = receipt_path.read_bytes()
        binding = parse_canonical_model(
            receipt_bytes,
            BoundedRiverReviewRoleConfirmationBindingV1,
        )
        receipt_hashes.append(sha256_bytes(receipt_bytes))
        request = parse_canonical_model(
            current.artifact_bytes(role_artifact_name(expected_role, "request")),
            BoundedCodexBridgeRequestV1,
        )
        role_confirmation = parse_canonical_model(
            current.artifact_bytes(role_artifact_name(expected_role, "confirmation")),
            BridgeRoleConfirmationV1,
        )
        admission = parse_canonical_model(
            current.artifact_bytes(role_artifact_name(expected_role, "admission")),
            BridgePreExecutionAdmissionV1,
        )
        result = parse_canonical_model(
            current.artifact_bytes(role_artifact_name(expected_role, "result")),
            BridgeRoleResultV1,
        )
        audit = parse_canonical_model(
            current.artifact_bytes(role_artifact_name(expected_role, "audit")),
            BridgeExecutionAuditV1,
        )
        p2_hashes.extend(
            (
                request.request_sha256,
                role_confirmation.confirmation_sha256,
                admission.admission_sha256,
                result.result_sha256,
                audit.audit_sha256,
            )
        )
        role_bindings_ok = role_bindings_ok and (
            binding.role is expected_role
            and binding.role_ordinal == ordinal
            and binding.request_sha256 == request.request_sha256
            and binding.request_bytes_sha256 == request.request_bytes_sha256
            and binding.envelope_sha256 == request.context.envelope_sha256
            and binding.runtime_policy_sha256 == request.context.runtime_policy.policy_sha256
            and binding.bridge_confirmation_sha256 == role_confirmation.confirmation_sha256
        )
        p2_lineage_ok = p2_lineage_ok and (
            role_confirmation.request_sha256 == request.request_sha256
            and admission.request_sha256 == request.request_sha256
            and admission.confirmation_sha256 == role_confirmation.confirmation_sha256
            and audit.request_sha256 == request.request_sha256
            and audit.confirmation_sha256 == role_confirmation.confirmation_sha256
            and audit.admission_sha256 == admission.admission_sha256
            and audit.result_sha256 == result.result_sha256
            and audit.effect_state is BridgeEffectState.SUCCEEDED
            and audit.transport_qualification == "deterministic_fixture"
            and audit.live_execution_evidence is None
        )
        role_bindings_ok = role_bindings_ok and (
            status.completed_roles == BRIDGE_ROLE_ORDER[: ordinal + 1]
            and status.pending_roles == BRIDGE_ROLE_ORDER[ordinal + 1 :]
        )

    terminal_bridge = bridge_store.read_current(plan.bridge_run_id)
    replayed_bridge = replay_bridge(terminal_bridge)
    replayed_workflow = replay_bounded_river_review_workflow(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
    )
    report = bounded_river_review_report_view(
        config=config,
        repository_root=repository_root,
        workflow_root=workflow_root,
        workflow_id=workflow_id,
    )
    terminal_replay_report_sha256 = canonical_domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-terminal-v2",
        {
            "workflow_status_sha256": sha256_bytes(canonical_json_bytes(status)),
            "workflow_replay_sha256": sha256_bytes(canonical_json_bytes(replayed_workflow)),
            "bridge_manifest_sha256": terminal_bridge.manifest.manifest_sha256,
            "bridge_inventory_sha256": terminal_bridge.manifest.inventory_sha256,
            "report_sha256": sha256_bytes(canonical_json_bytes(report)),
        },
    )
    confirmation_hashes_sha256 = canonical_domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-confirmation-v2",
        list(confirmation_hashes),
    )
    receipts_sha256 = canonical_domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-receipts-v2",
        {"field_hashes": role_field_hashes, "receipt_hashes": receipt_hashes},
    )
    p2_lineage_sha256 = canonical_domain_sha256(
        "poker-bounded-river-review-workflow-evaluation-p2-lineage-v2",
        p2_hashes,
    )
    terminal_ok = (
        status == replayed_workflow
        and status.state == "completed"
        and status.bridge_status == "succeeded"
        and status.completed_roles == BRIDGE_ROLE_ORDER
        and not status.pending_roles
        and not status.reconciliation_required
        and replayed_bridge.completed_roles == BRIDGE_ROLE_ORDER
        and replayed_bridge.status == "succeeded"
        and report.completed_roles == BRIDGE_ROLE_ORDER
        and report.bridge_manifest_sha256 == terminal_bridge.manifest.manifest_sha256
        and report.bridge_inventory_sha256 == terminal_bridge.manifest.inventory_sha256
    )
    runtime_inventory_after = bridge_runtime_source_inventory_sha256(repository_root)
    _verify_v2_repository_identity(
        repository_root,
        source_commit_id=source_commit_id,
        source_tree_id=source_tree_id,
    )
    source_identity_ok = fixture_bound and (
        plan.repository_commit_id == source_commit_id
        and plan.repository_tree_id == source_tree_id
        and status.source_terminal_manifest_sha256 == report.source_terminal_manifest_sha256
    )
    confirmation_ok = (
        len(confirmation_hashes) == fixture.expected_confirmation_hash_count
        and tuple(preview_hashes.values()) == confirmation_hashes
        and workflow_confirmation.candidate_sha256 == preparation.candidate.candidate_sha256
    )
    five_roles_ok = (
        role_bindings_ok
        and all_field_mutations_sha256 is not None
        and tuple(executed_roles) == fixture.expected_roles
        and len(role_field_hashes) == 5
        and len(receipt_hashes) == 5
    )
    runtime_ok = runtime_inventory_before == runtime_inventory_after
    check_values = (
        source_identity_ok,
        confirmation_ok,
        five_roles_ok,
        p2_lineage_ok and len(p2_hashes) == 25,
        terminal_ok,
        runtime_ok,
    )
    evidence = (
        (fixture_sha256, plan.plan_sha256, report.source_terminal_manifest_sha256),
        (confirmation_hashes_sha256, workflow_confirmation.confirmation_sha256),
        (
            receipts_sha256,
            *tuple(role_field_hashes),
            all_field_mutations_sha256 or "0" * 64,
        ),
        (p2_lineage_sha256,),
        (terminal_replay_report_sha256, report.final_report_artifact_sha256),
        (runtime_inventory_before, runtime_inventory_before),
    )
    failure_codes = (
        "BRWE_E_SOURCE_IDENTITY",
        "BRWE_E_CONFIRMATION",
        "BRWE_E_ROLE_BINDING",
        "BRWE_E_P2_LINEAGE",
        "BRWE_E_TERMINAL",
        "BRWE_E_REPOSITORY_IDENTITY",
    )
    cases = tuple(
        _v2_check(
            case_id,
            passed,
            evidence_values,
            failure_code,
            metric=False,
            observed_evidence=(
                (runtime_inventory_before, runtime_inventory_after)
                if case_id == "repository-runtime-identity" and not passed
                else None
            ),
        )
        for case_id, passed, evidence_values, failure_code in zip(
            _CASE_ORDER_V2,
            check_values,
            evidence,
            failure_codes,
            strict=True,
        )
    )
    metrics = tuple(
        _v2_check(
            metric,
            passed,
            evidence_values,
            failure_code,
            metric=True,
            observed_evidence=(
                (runtime_inventory_before, runtime_inventory_after)
                if metric == "repository_runtime_identity" and not passed
                else None
            ),
        )
        for metric, passed, evidence_values, failure_code in zip(
            _METRIC_ORDER_V2,
            check_values,
            evidence,
            failure_codes,
            strict=True,
        )
    )
    passed_count = sum(item.passed for item in metrics)
    all_passed = all(item.passed for item in cases) and all(item.passed for item in metrics)
    payload: dict[str, object] = {
        "schema_version": "2.0.0",
        "evaluation_id": "p3-030g-bounded-river-review-workflow-evaluation-v2",
        "fixture_id": fixture.fixture_id,
        "fixture_sha256": fixture_sha256,
        "source_fixture_sha256": sha256_bytes(source_bytes),
        "range_fixture_sha256": sha256_bytes(range_path.read_bytes()),
        "range_definition_sha256": sha256_bytes(range_bytes),
        "source_commit_id": source_commit_id,
        "source_tree_id": source_tree_id,
        "workflow_id": workflow_id,
        "plan_sha256": plan.plan_sha256,
        "workflow_confirmation_sha256": workflow_confirmation.confirmation_sha256,
        "linkage_sha256": report.linkage_sha256,
        "source_terminal_manifest_sha256": report.source_terminal_manifest_sha256,
        "source_terminal_inventory_sha256": report.source_terminal_inventory_sha256,
        "bridge_terminal_manifest_sha256": terminal_bridge.manifest.manifest_sha256,
        "bridge_terminal_inventory_sha256": terminal_bridge.manifest.inventory_sha256,
        "final_report_artifact_sha256": report.final_report_artifact_sha256,
        "confirmation_hashes_sha256": confirmation_hashes_sha256,
        "role_confirmation_receipts_sha256": receipts_sha256,
        "role_confirmation_fields_sha256": tuple(role_field_hashes),
        "all_confirmation_field_mutations_sha256": (all_field_mutations_sha256 or "0" * 64),
        "p2_artifact_lineage_sha256": p2_lineage_sha256,
        "terminal_replay_report_sha256": terminal_replay_report_sha256,
        "runtime_source_inventory_sha256": runtime_inventory_before,
        "cases": cases,
        "metrics": metrics,
        "score_milli": passed_count * 1000 // len(metrics),
        "status": "pass" if all_passed else "fail",
        "passed": all_passed,
        "transport_qualification": "deterministic_fixture",
        "live_qualification_status": "UNKNOWN",
        "actual_backend_model_input": "UNKNOWN",
        "api_live_executed": False,
    }
    try:
        return BoundedRiverReviewWorkflowEvaluationResultV2.model_validate(
            {
                **payload,
                "result_sha256": canonical_domain_sha256(_RESULT_V2_HASH_DOMAIN, payload),
            },
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise BoundedRiverReviewWorkflowEvaluationError("BRWE_E_RESULT") from exc


__all__ = [
    "BoundedRiverReviewWorkflowCaseV2",
    "BoundedRiverReviewWorkflowEvaluationError",
    "BoundedRiverReviewWorkflowEvaluationResultV1",
    "BoundedRiverReviewWorkflowEvaluationResultV2",
    "BoundedRiverReviewWorkflowFixtureV1",
    "BoundedRiverReviewWorkflowFixtureV2",
    "BoundedRiverReviewWorkflowMetricV2",
    "bounded_river_review_workflow_evaluation_config",
    "load_bounded_river_review_workflow_evaluation_result_v2",
    "load_bounded_river_review_workflow_fixture",
    "load_bounded_river_review_workflow_fixture_v2",
    "run_bounded_river_review_workflow_evaluation",
    "run_bounded_river_review_workflow_evaluation_v2",
    "verify_bounded_river_review_workflow_evaluation_result_v2",
]
