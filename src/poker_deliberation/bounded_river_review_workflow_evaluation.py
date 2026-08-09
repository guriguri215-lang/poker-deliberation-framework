"""Deterministic exact-evidence evaluation for the P3-030D workflow."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation.bounded_river_review_workflow import (
    bounded_river_confirmation_hashes,
    bounded_river_review_confirmation_preview,
    confirm_bounded_river_review_workflow,
    prepare_bounded_river_review_workflow,
    replay_bounded_river_review_workflow,
    resume_bounded_river_review_workflow,
    run_bounded_river_review_workflow,
)
from poker_deliberation.codex_bridge.controller import BoundedCodexBridgeController
from poker_deliberation.codex_bridge.models import RuntimeAuthModeV1
from poker_deliberation.codex_bridge.storage import BoundedCodexBridgeStore
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


__all__ = [
    "BoundedRiverReviewWorkflowEvaluationResultV1",
    "BoundedRiverReviewWorkflowFixtureV1",
    "load_bounded_river_review_workflow_fixture",
    "run_bounded_river_review_workflow_evaluation",
]
