"""Deterministic exact-evidence evaluation for the P3-016B bridge."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from itertools import combinations
from pathlib import Path
from tempfile import mkdtemp
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_equity import (
    VersionedRangeRiverEquityError,
    admit_versioned_range_river_equity,
    build_versioned_range_river_equity_result,
    expected_versioned_range_equity_input,
    verify_versioned_range_river_equity_tool_chain,
    versioned_range_river_equity_binding,
)
from poker_deliberation.range_equity_models import (
    RANGE_EQUITY_MAX_EVALUATIONS,
    VersionedRangeRiverEquityBindingV1,
    canonical_domain_sha256,
)
from poker_deliberation.range_grammar import (
    action_prefix_sha256,
    validate_versioned_range,
)
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import (
    CanonicalHand,
    CaseInput,
    Exactness,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.storage.range_equity_admission_store import (
    commit_range_equity_admission_record,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
)
from poker_deliberation.storage.terminal_canonical import product_payload_commitments
from poker_deliberation.storage.terminal_models import (
    ProductRunError,
    ProductRunFailureCode,
    RunReadStatus,
)
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.cards import DECK
from poker_deliberation.tools.numeric import close_ulps

EVALUATION_FAMILY_ID: Literal["poker-versioned-range-river-equity-evaluation-json-v1"] = (
    "poker-versioned-range-river-equity-evaluation-json-v1"
)
EVALUATION_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
EVALUATION_THRESHOLD: Literal["1.0"] = "1.0"
EvaluationMetric = Literal[
    "exact_weight_and_oracle",
    "admission_boundaries",
    "replay_and_storage",
]

REQUIRED_CASE_EVIDENCE = (
    (
        "weighted-exact-oracle",
        "exact_weight_and_oracle",
        (
            "fixed-tool-order",
            "weighted-fraction-3-over-4",
            "extreme-990-weight-ulp-contract",
            "exact-monte-carlo-metadata-rejected",
            "exact-toolresult-envelope-rejected",
        ),
    ),
    (
        "tie-policy",
        "exact_weight_and_oracle",
        ("tie-fraction-1-over-2",),
    ),
    (
        "blockers-and-cap",
        "exact_weight_and_oracle",
        ("seven-card-blocker-cap-990", "cap-overflow-rejected"),
    ),
    (
        "invalid-range-boundaries",
        "admission_boundaries",
        (
            "empty-post-blocker-rejected",
            "grammar-rejected",
            "version-rejected",
            "provenance-rejected",
            "non-json-metadata-rejected",
        ),
    ),
    (
        "scope-boundaries",
        "admission_boundaries",
        (
            "street-rejected",
            "all-in-rejected",
            "third-eligible-rejected",
            "multiple-ranges-rejected",
            "target-mismatch-rejected",
            "manual-input-rejected",
            "duplicate-card-rejected",
        ),
    ),
    (
        "tamper-and-failure-replay",
        "replay_and_storage",
        (
            "binding-tamper-rejected",
            "range-mutation-rejected",
            "partial-tamper-rejected",
            "malformed-output-reason-code",
            "post-failure-execution-rejected",
            "approval-lifecycle-rejected",
        ),
    ),
    (
        "storage-and-compatibility",
        "replay_and_storage",
        (
            "terminal-replay-verified",
            "binding-artifact-persisted",
            "binding-artifact-removal-rejected",
            "report-marker-tamper-rejected",
            "report-case-tamper-rejected",
            "normalized-case-tamper-rejected",
            "marker-downgrade-rejected",
            "reshaped-marker-downgrade-rejected",
            "preexecution-record-downgrade-rejected",
            "orphan-record-ordinary-reuse-rejected-preexecution",
            "orphan-record-case-alias-rejected-preexecution",
            "failed-prefix-marker-downgrade-rejected",
            "null-marker-rejected",
            "ordinary-range-path-unchanged",
            "legacy-unmarked-run-unchanged",
            "legacy-manual-exact-run-unchanged",
        ),
    ),
)
REQUIRED_CASE_IDS = tuple(case_id for case_id, _metric, _evidence in REQUIRED_CASE_EVIDENCE)
REQUIRED_METRICS: tuple[EvaluationMetric, ...] = (
    "exact_weight_and_oracle",
    "admission_boundaries",
    "replay_and_storage",
)


class _EvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class RangeEquityEvaluationCaseV1(_EvaluationModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,95}$")
    metric: EvaluationMetric
    expected_evidence: tuple[str, ...] = Field(min_length=1)


class RangeEquityEvaluationFixtureV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    family_id: Literal["poker-versioned-range-river-equity-evaluation-json-v1"] = (
        EVALUATION_FAMILY_ID
    )
    fixture_id: Literal["versioned-range-river-equity-cases-v1"] = (
        "versioned-range-river-equity-cases-v1"
    )
    source_kind: Literal["repository_fixture"] = "repository_fixture"
    license_classification: Literal["repository_owned_mit"] = "repository_owned_mit"
    usage_classification: Literal["redistribution_allowed"] = "redistribution_allowed"
    content_classification: Literal["public"] = "public"
    scoring: Literal["exact-evidence-set-v1"] = "exact-evidence-set-v1"
    threshold: Literal["1.0"] = EVALUATION_THRESHOLD
    cases: tuple[RangeEquityEvaluationCaseV1, ...]

    @model_validator(mode="after")
    def exact_inventory(self) -> RangeEquityEvaluationFixtureV1:
        observed = tuple((case.case_id, case.metric, case.expected_evidence) for case in self.cases)
        if observed != REQUIRED_CASE_EVIDENCE:
            raise ValueError("range-equity evaluation case inventory mismatch")
        return self


class RangeEquityEvaluationCaseResultV1(_EvaluationModel):
    case_id: str
    metric: str
    expected_evidence: tuple[str, ...]
    observed_evidence: tuple[str, ...]
    score: Literal["0.0", "1.0"]
    passed: bool

    @model_validator(mode="after")
    def exact_case_result(self) -> RangeEquityEvaluationCaseResultV1:
        expected = next(
            (item for item in REQUIRED_CASE_EVIDENCE if item[0] == self.case_id),
            None,
        )
        if expected is None or (self.metric, self.expected_evidence) != expected[1:]:
            raise ValueError("range-equity evaluation case result inventory mismatch")
        evidence_matches = self.observed_evidence == self.expected_evidence
        expected_score = EVALUATION_THRESHOLD if evidence_matches else "0.0"
        if self.passed != evidence_matches or self.score != expected_score:
            raise ValueError("range-equity evaluation case result summary mismatch")
        return self


class RangeEquityEvaluationMetricV1(_EvaluationModel):
    metric: EvaluationMetric
    declared_checks: int = Field(gt=0)
    passed_checks: int = Field(ge=0)
    score: Literal["0.0", "1.0"]


class RangeEquityEvaluationResultV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    family_id: Literal["poker-versioned-range-river-equity-evaluation-json-v1"] = (
        EVALUATION_FAMILY_ID
    )
    fixture_id: Literal["versioned-range-river-equity-cases-v1"]
    scoring: Literal["exact-evidence-set-v1"]
    threshold: Literal["1.0"]
    source_commit_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_tree_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    case_results: tuple[RangeEquityEvaluationCaseResultV1, ...]
    metrics: tuple[RangeEquityEvaluationMetricV1, ...]
    overall_score: Literal["0.0", "1.0"]
    passed: bool
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_result(self) -> RangeEquityEvaluationResultV1:
        observed_inventory = tuple(
            (result.case_id, result.metric, result.expected_evidence)
            for result in self.case_results
        )
        if observed_inventory != REQUIRED_CASE_EVIDENCE:
            raise ValueError("range-equity evaluation result inventory mismatch")
        if tuple(metric.metric for metric in self.metrics) != REQUIRED_METRICS:
            raise ValueError("range-equity evaluation metric inventory mismatch")
        for metric in self.metrics:
            selected = tuple(
                result for result in self.case_results if result.metric == metric.metric
            )
            declared = sum(len(result.expected_evidence) for result in selected)
            passed_checks = sum(
                len(result.expected_evidence) if result.passed else 0 for result in selected
            )
            expected_score = EVALUATION_THRESHOLD if passed_checks == declared else "0.0"
            if (
                metric.declared_checks != declared
                or metric.passed_checks != passed_checks
                or metric.score != expected_score
            ):
                raise ValueError("range-equity evaluation metric summary mismatch")
        expected_pass = all(result.passed for result in self.case_results) and all(
            metric.score == EVALUATION_THRESHOLD for metric in self.metrics
        )
        if self.passed != expected_pass or self.overall_score != (
            EVALUATION_THRESHOLD if expected_pass else "0.0"
        ):
            raise ValueError("range-equity evaluation summary mismatch")
        payload = self.model_dump(mode="json")
        payload.pop("result_sha256")
        if self.result_sha256 != canonical_domain_sha256(EVALUATION_FAMILY_ID, payload):
            raise ValueError("range-equity evaluation result hash mismatch")
        return self


def load_range_equity_evaluation_fixture(path: Path) -> RangeEquityEvaluationFixtureV1:
    return RangeEquityEvaluationFixtureV1.model_validate_json(path.read_bytes(), strict=True)


def _candidate(
    notation: str = "6c6d@0.25,QcQd@0.75",
    *,
    hero_cards: tuple[str, str] = ("As", "Kd"),
    board: tuple[str, str, str, str, str] = ("2c", "3d", "4h", "5s", "9c"),
    source_sha256: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> CaseInput:
    base = CanonicalHand.model_validate(
        {
            "game_type": "NLHE",
            "format": "cash",
            "table_size": 3,
            "small_blind": 1,
            "big_blind": 2,
            "players": [
                {"player_id": "hero", "position": "BTN", "starting_stack": 100},
                {"player_id": "folded", "position": "SB", "starting_stack": 100},
                {"player_id": "villain", "position": "BB", "starting_stack": 100},
            ],
            "hero_player_id": "hero",
            "hero_cards": hero_cards,
            "board": board,
            "actions": [
                {"street": "preflop", "actor": "folded", "action": "fold", "amount": 0},
                {"street": "river", "actor": "villain", "action": "bet", "amount": 20},
            ],
        }
    )
    definition = VersionedRangeDefinitionV1.model_validate(
        {
            "range_id": "villain-river",
            "target_player_id": "villain",
            "notation": notation,
            "source": {
                "source_id": "range-equity-evaluation",
                "source_kind": "repository_fixture",
                "license_classification": "repository_owned_mit",
                "usage_classification": "redistribution_allowed",
                "content_status": "ASSUMPTION",
                "content_sha256": source_sha256
                or hashlib.sha256(notation.encode("utf-8")).hexdigest(),
            },
            "game_conditions": {
                "game_type": "NLHE",
                "format": "cash",
                "table_size": 3,
                "target_position": "BB",
                "street": "river",
                "starting_stack_min_bb_milli": 50_000,
                "starting_stack_max_bb_milli": 50_000,
                "as_of_action_index": 2,
                "action_prefix_sha256": action_prefix_sha256(base, 2),
            },
        }
    )
    hand_payload = base.model_dump(mode="json")
    hand_payload["known_ranges"] = [definition.model_dump(mode="json")]
    return CaseInput.model_validate(
        {
            "kind": "calculation",
            "hand": hand_payload,
            "analysis_scope": "retrospective",
            "requested_tools": ["combos", "holdem_equity"],
            "metadata": metadata or {},
        }
    )


def _all_starting_classes() -> str:
    ranks = "AKQJT98765432"
    tokens = [rank + rank for rank in ranks]
    for first_index, first in enumerate(ranks):
        for second in ranks[first_index + 1 :]:
            tokens.extend((first + second + "s", first + second + "o"))
    return ",".join(tokens)


def _config(root: Path, name: str) -> AppConfig:
    token = {
        "base": "b",
        "tie": "t",
        "cap": "c",
        "ordinary": "o",
        "legacy-unmarked": "l",
        "failed-prefix": "f",
        "legacy-manual-exact": "m",
        "orphan-record": "r",
        "extreme": "x",
    }[name]
    return AppConfig(
        runs_dir=root / token / "l",
        revision_runs_dir=root / token / "p",
        durable_budget_runs_dir=root / token / "b",
    )


class _EvaluationContext:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._base: tuple[Any, Any, Orchestrator] | None = None

    def base(self) -> tuple[Any, Any, Orchestrator]:
        if self._base is None:
            admission = admit_versioned_range_river_equity(_candidate())
            orchestrator = Orchestrator(_config(self.root, "base"))
            report = orchestrator.run_versioned_range_river_equity(
                admission,
                run_id="re-eval-base",
            )
            self._base = admission, report, orchestrator
        return self._base


def _weighted_exact_oracle(context: _EvaluationContext) -> tuple[str, ...]:
    admission, report, _orchestrator = context.base()
    result = build_versioned_range_river_equity_result(admission.case, report.tool_results)
    evidence: list[str] = []
    if tuple(tool.tool_name for tool in report.tool_results) == (
        "range_validate",
        "combos",
        "holdem_equity",
    ):
        evidence.append("fixed-tool-order")
    if (
        result.win_weight_millionths == 750_000
        and result.loss_weight_millionths == 250_000
        and (result.equity_numerator, result.equity_denominator) == (3, 4)
    ):
        evidence.append("weighted-fraction-3-over-4")

    hero_cards = ("2c", "Jd")
    board = ("Kd", "8h", "4c", "5d", "Qh")
    available = [card for card in DECK if card not in {*hero_cards, *board}]
    dominant = tuple(sorted(("2d", "3h")))
    notation = ",".join(
        f"{first}{second}@{'1' if tuple(sorted((first, second))) == dominant else '0.000001'}"
        for first, second in combinations(available, 2)
    )
    extreme_admission = admit_versioned_range_river_equity(
        _candidate(notation, hero_cards=hero_cards, board=board)
    )
    extreme_report = Orchestrator(
        _config(context.root, "extreme")
    ).run_versioned_range_river_equity(
        extreme_admission,
        run_id="re-eval-extreme",
    )
    extreme = build_versioned_range_river_equity_result(
        extreme_admission.case,
        extreme_report.tool_results,
    )
    if (
        extreme.combo_count == 990
        and (extreme.win_combo_count, extreme.tie_combo_count, extreme.loss_combo_count)
        == (204, 21, 765)
        and (
            extreme.win_weight_millionths,
            extreme.tie_weight_millionths,
            extreme.loss_weight_millionths,
        )
        == (1_000_203, 21, 765)
        and (extreme.equity_numerator, extreme.equity_denominator) == (60_619, 60_666)
        and close_ulps(
            extreme.legacy_hero_equity,
            extreme.equity_numerator / extreme.equity_denominator,
            ulps=128,
        )
    ):
        evidence.append("extreme-990-weight-ulp-contract")

    forged_results = list(report.tool_results)
    forged_output = dict(forged_results[-1].output)
    forged_output.update(
        {
            "samples": 1,
            "seed": 7,
            "wins": 1,
            "ties": 0,
            "losses": 0,
            "estimated_exact_evaluations": 1,
        }
    )
    forged_results[-1] = forged_results[-1].model_copy(update={"output": forged_output})
    try:
        build_versioned_range_river_equity_result(admission.case, forged_results)
    except VersionedRangeRiverEquityError:
        evidence.append("exact-monte-carlo-metadata-rejected")

    forged_results = list(report.tool_results)
    forged_payload = forged_results[-1].model_dump(mode="python")
    forged_payload.update(
        {
            "method": "monte_carlo",
            "stochastic": True,
            "seed": 7,
            "samples": 1,
            "confidence_interval": (0.0, 1.0),
            "confidence_level": 0.95,
            "stopping_condition": "fixed requested sample count",
        }
    )
    forged_results[-1] = ToolResult.model_validate(forged_payload, strict=True)
    try:
        build_versioned_range_river_equity_result(admission.case, forged_results)
    except VersionedRangeRiverEquityError:
        evidence.append("exact-toolresult-envelope-rejected")
    return tuple(evidence)


def _tie_policy(context: _EvaluationContext) -> tuple[str, ...]:
    candidate = _candidate(
        "4d5d@0.2,6h7h@0.8",
        hero_cards=("2d", "3d"),
        board=("Ac", "Kc", "Qc", "Jc", "Tc"),
    )
    admission = admit_versioned_range_river_equity(candidate)
    report = Orchestrator(_config(context.root, "tie")).run_versioned_range_river_equity(
        admission,
        run_id="re-eval-tie",
    )
    result = build_versioned_range_river_equity_result(admission.case, report.tool_results)
    return (
        ("tie-fraction-1-over-2",)
        if result.tie_combo_count == 2
        and result.tie_weight_millionths == 1_000_000
        and (result.equity_numerator, result.equity_denominator) == (1, 2)
        else ()
    )


def _blockers_and_cap(context: _EvaluationContext) -> tuple[str, ...]:
    admission = admit_versioned_range_river_equity(_candidate(_all_starting_classes()))
    report = Orchestrator(_config(context.root, "cap")).run_versioned_range_river_equity(
        admission,
        run_id="re-eval-cap",
    )
    result = build_versioned_range_river_equity_result(admission.case, report.tool_results)
    evidence: list[str] = []
    if (
        admission.binding.combo_count == RANGE_EQUITY_MAX_EVALUATIONS
        and result.combo_count == RANGE_EQUITY_MAX_EVALUATIONS
        and report.tool_results[-1].output["evaluations"] == RANGE_EQUITY_MAX_EVALUATIONS
    ):
        evidence.append("seven-card-blocker-cap-990")
    payload = admission.binding.model_dump(mode="python")
    payload["combo_count"] = RANGE_EQUITY_MAX_EVALUATIONS + 1
    try:
        VersionedRangeRiverEquityBindingV1.model_validate(payload)
    except ValueError:
        evidence.append("cap-overflow-rejected")
    return tuple(evidence)


def _invalid_range_boundaries(_context: _EvaluationContext) -> tuple[str, ...]:
    evidence: list[str] = []
    checks = (
        ("empty-post-blocker-rejected", _candidate("AsKd")),
        ("grammar-rejected", _candidate("AA+")),
        ("provenance-rejected", _candidate(source_sha256="0" * 64)),
    )
    for label, candidate in checks:
        try:
            admit_versioned_range_river_equity(candidate)
        except VersionedRangeRiverEquityError:
            evidence.append(label)
    payload = _candidate().model_dump(mode="python")
    payload["hand"]["known_ranges"][0]["grammar_version"] = "2.0.0"
    try:
        CaseInput.model_validate(payload)
    except ValueError:
        evidence.insert(2, "version-rejected")
    payload = _candidate().model_dump(mode="python")
    payload["metadata"]["extra"] = object()
    try:
        admit_versioned_range_river_equity(CaseInput.model_validate(payload, strict=True))
    except VersionedRangeRiverEquityError:
        evidence.append("non-json-metadata-rejected")
    return tuple(evidence)


def _scope_boundaries(_context: _EvaluationContext) -> tuple[str, ...]:
    evidence: list[str] = []

    payload = _candidate().model_dump(mode="python")
    payload["hand"]["known_ranges"][0]["game_conditions"]["street"] = "turn"
    try:
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))
    except VersionedRangeRiverEquityError:
        evidence.append("street-rejected")

    payload = _candidate().model_dump(mode="python")
    payload["hand"]["actions"][-1]["action"] = "all_in"
    changed_hand = CanonicalHand.model_validate(payload["hand"])
    payload["hand"]["known_ranges"][0]["game_conditions"]["action_prefix_sha256"] = (
        action_prefix_sha256(changed_hand, 2)
    )
    try:
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))
    except VersionedRangeRiverEquityError:
        evidence.append("all-in-rejected")

    payload = _candidate().model_dump(mode="python")
    payload["hand"]["actions"][0]["action"] = "check"
    try:
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))
    except VersionedRangeRiverEquityError:
        evidence.append("third-eligible-rejected")

    payload = _candidate().model_dump(mode="python")
    duplicate = dict(payload["hand"]["known_ranges"][0])
    duplicate["range_id"] = "second-range"
    payload["hand"]["known_ranges"].append(duplicate)
    try:
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))
    except VersionedRangeRiverEquityError:
        evidence.append("multiple-ranges-rejected")

    payload = _candidate().model_dump(mode="python")
    payload["hand"]["known_ranges"][0]["target_player_id"] = "folded"
    try:
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))
    except VersionedRangeRiverEquityError:
        evidence.append("target-mismatch-rejected")

    try:
        admit_versioned_range_river_equity(
            _candidate(metadata={"tool_inputs": {"holdem_equity": {"mode": "exact"}}})
        )
    except VersionedRangeRiverEquityError:
        evidence.append("manual-input-rejected")

    payload = _candidate().model_dump(mode="python")
    payload["hand"]["board"][0] = payload["hand"]["hero_cards"][0]
    try:
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))
    except VersionedRangeRiverEquityError:
        evidence.append("duplicate-card-rejected")
    return tuple(evidence)


def _tamper_and_failure_replay(context: _EvaluationContext) -> tuple[str, ...]:
    admission, report, _orchestrator = context.base()
    evidence: list[str] = []

    payload = admission.case.model_dump(mode="python")
    payload["metadata"]["versioned_range_river_equity"]["binding_sha256"] = "0" * 64
    try:
        versioned_range_river_equity_binding(CaseInput.model_validate(payload))
    except VersionedRangeRiverEquityError:
        evidence.append("binding-tamper-rejected")

    payload = admission.case.model_dump(mode="python")
    payload["hand"]["known_ranges"][0]["notation"] = "QcQd"
    try:
        verify_versioned_range_river_equity_tool_chain(
            CaseInput.model_validate(payload),
            report.tool_results,
        )
    except VersionedRangeRiverEquityError:
        evidence.append("range-mutation-rejected")

    forged = report.tool_results[0].model_copy(update={"output": {"forged": True}})
    try:
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [forged],
            run_status="failed_with_limitations",
        )
    except VersionedRangeRiverEquityError:
        evidence.append("partial-tamper-rejected")

    forged_equity = report.tool_results[-1].model_copy(update={"output": {"forged": True}})
    try:
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [*report.tool_results[:-1], forged_equity],
        )
    except VersionedRangeRiverEquityError as exc:
        if exc.code.value == "REQ_E_CHAIN":
            evidence.append("malformed-output-reason-code")

    failed = report.tool_results[0].model_copy(
        update={
            "status": ToolStatus.FAILED,
            "exactness": Exactness.UNAVAILABLE,
            "numeric_exactness": NumericalExactness.UNAVAILABLE,
            "output": {},
            "verification": None,
            "error": "evaluation failure",
        }
    )
    try:
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [failed, report.tool_results[1]],
            run_status="failed_with_limitations",
        )
    except VersionedRangeRiverEquityError:
        evidence.append("post-failure-execution-rejected")

    try:
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            [],
            run_status="approval_required",
        )
    except VersionedRangeRiverEquityError:
        evidence.append("approval-lifecycle-rejected")
    return tuple(evidence)


def _storage_and_compatibility(context: _EvaluationContext) -> tuple[str, ...]:
    admission, report, orchestrator = context.base()
    evidence: list[str] = []
    read = orchestrator.product_store.read_current(report.run_id)
    if (
        read.read_status is RunReadStatus.SUCCEEDED
        and orchestrator.load_report(report.run_id) == report
    ):
        evidence.append("terminal-replay-verified")
    if (
        VersionedRangeRiverEquityBindingV1.model_validate_json(
            read.payload_bytes("range_equity_binding.json")
        )
        == admission.binding
    ):
        evidence.append("binding-artifact-persisted")

    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name
        not in {"lifecycle_audit.json", "range_equity_binding.json"}
    }
    try:
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")
    except CanonicalStorageError:
        evidence.append("binding-artifact-removal-rejected")

    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    forged = report.model_copy(deep=True)
    del forged.reconstructed_input["metadata"]["versioned_range_river_equity"]
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")
    try:
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")
    except CanonicalStorageError:
        evidence.append("report-marker-tamper-rejected")

    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    forged = report.model_copy(deep=True)
    forged.reconstructed_input["hand"]["known_ranges"][0]["notation"] = "AcAd"
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")
    try:
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")
    except CanonicalStorageError:
        evidence.append("report-case-tamper-rejected")

    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    normalized_payload = json.loads(payloads["normalized_case.json"])
    normalized_payload["hand"]["known_ranges"][0]["notation"] = "AcAd"
    payloads["normalized_case.json"] = canonical_json_bytes(normalized_payload)
    try:
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")
    except CanonicalStorageError:
        evidence.append("normalized-case-tamper-rejected")

    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    input_payload = json.loads(payloads["input.json"])
    del input_payload["metadata"]["versioned_range_river_equity"]
    forged = report.model_copy(deep=True)
    del forged.reconstructed_input["metadata"]["versioned_range_river_equity"]
    payloads["input.json"] = canonical_json_bytes(input_payload)
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")
    try:
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")
    except CanonicalStorageError:
        evidence.append("marker-downgrade-rejected")

    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    input_payload = json.loads(payloads["input.json"])
    normalized_payload = json.loads(payloads["normalized_case.json"])
    forged = report.model_copy(deep=True)
    for case_payload in (
        input_payload,
        normalized_payload,
        forged.reconstructed_input,
    ):
        del case_payload["metadata"]["versioned_range_river_equity"]
        case_payload["requested_tools"] = ["combos"]
    payloads["input.json"] = canonical_json_bytes(input_payload)
    payloads["normalized_case.json"] = canonical_json_bytes(normalized_payload)
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")
    try:
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")
    except CanonicalStorageError:
        evidence.append("reshaped-marker-downgrade-rejected")

    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name
        not in {"lifecycle_audit.json", "range_equity_binding.json"}
    }
    input_payload = json.loads(payloads["input.json"])
    normalized_payload = json.loads(payloads["normalized_case.json"])
    forged = report.model_copy(deep=True)
    for case_payload in (
        input_payload,
        normalized_payload,
        forged.reconstructed_input,
    ):
        del case_payload["metadata"]["versioned_range_river_equity"]
        case_payload["requested_tools"] = ["combos"]
    payloads["input.json"] = canonical_json_bytes(input_payload)
    payloads["normalized_case.json"] = canonical_json_bytes(normalized_payload)
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")
    try:
        product_payload_commitments(
            payloads,
            run_id=report.run_id,
            status="succeeded",
            revision_root=orchestrator.product_store.revision_root,
        )
    except CanonicalStorageError:
        evidence.append("preexecution-record-downgrade-rejected")

    orphan_orchestrator = Orchestrator(_config(context.root, "orphan-record"))
    orphan_run_id = "re-eval-orphan-record"
    orphan_orchestrator._initialize_product_storage(orphan_run_id)
    commit_range_equity_admission_record(
        orphan_orchestrator.product_store.revision_root,
        orphan_run_id,
        admission.binding,
        maximum_bytes=orphan_orchestrator.budget_policy.max_artifact_bytes,
    )
    try:
        orphan_orchestrator.run(admission.candidate, run_id=orphan_run_id)
    except ProductRunError as exc:
        if (
            exc.failure.code is ProductRunFailureCode.RUN_CONFLICT
            and not orphan_orchestrator.store.exists(orphan_run_id)
            and not (orphan_orchestrator.product_store.runs_root / orphan_run_id).exists()
        ):
            evidence.append("orphan-record-ordinary-reuse-rejected-preexecution")
    alias_run_id = orphan_run_id.upper()
    try:
        orphan_orchestrator.run(admission.candidate, run_id=alias_run_id)
    except ProductRunError as exc:
        if (
            exc.failure.code is ProductRunFailureCode.RUN_CORRUPT
            and not orphan_orchestrator.store.exists(alias_run_id)
            and not (orphan_orchestrator.product_store.runs_root / alias_run_id).exists()
        ):
            evidence.append("orphan-record-case-alias-rejected-preexecution")

    registry = default_registry()
    original_combos = registry._tools["combos"]

    def failing_combos(_payload: dict[str, object]) -> dict[str, object]:
        raise ValueError("evaluation combos failure")

    registry._tools["combos"] = replace(original_combos, function=failing_combos)
    failed_orchestrator = Orchestrator(
        _config(context.root, "failed-prefix"),
        registry=registry,
    )
    failed_report = failed_orchestrator.run_versioned_range_river_equity(
        admission,
        run_id="re-eval-failed-prefix",
    )
    failed_read = failed_orchestrator.product_store.read_current(failed_report.run_id)
    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in failed_read.payloads
        if payload.inventory.logical_name
        not in {"lifecycle_audit.json", "range_equity_binding.json"}
    }
    input_payload = json.loads(payloads["input.json"])
    normalized_payload = json.loads(payloads["normalized_case.json"])
    forged_failed = failed_report.model_copy(deep=True)
    for case_payload in (
        input_payload,
        normalized_payload,
        forged_failed.reconstructed_input,
    ):
        del case_payload["metadata"]["versioned_range_river_equity"]
        case_payload["requested_tools"] = ["combos"]
    payloads["input.json"] = canonical_json_bytes(input_payload)
    payloads["normalized_case.json"] = canonical_json_bytes(normalized_payload)
    payloads["final_report.json"] = canonical_json_bytes(forged_failed)
    payloads["final_report.md"] = render_markdown(forged_failed).encode("utf-8")
    try:
        product_payload_commitments(
            payloads,
            run_id=failed_report.run_id,
            status="failed",
            revision_root=failed_orchestrator.product_store.revision_root,
        )
    except CanonicalStorageError:
        evidence.append("failed-prefix-marker-downgrade-rejected")

    payloads = {
        payload.inventory.logical_name: payload.exact_bytes
        for payload in read.payloads
        if payload.inventory.logical_name != "lifecycle_audit.json"
    }
    input_payload = json.loads(payloads["input.json"])
    input_payload["metadata"]["versioned_range_river_equity"] = None
    forged = report.model_copy(deep=True)
    forged.reconstructed_input["metadata"]["versioned_range_river_equity"] = None
    payloads["input.json"] = canonical_json_bytes(input_payload)
    payloads["final_report.json"] = canonical_json_bytes(forged)
    payloads["final_report.md"] = render_markdown(forged).encode("utf-8")
    try:
        product_payload_commitments(payloads, run_id=report.run_id, status="succeeded")
    except CanonicalStorageError:
        evidence.append("null-marker-rejected")

    ordinary_payload = admission.candidate.model_dump(mode="python")
    ordinary_payload["requested_tools"] = ["combos"]
    ordinary = CaseInput.model_validate(ordinary_payload)
    ordinary_report = Orchestrator(_config(context.root, "ordinary")).run(
        ordinary,
        run_id="re-eval-ordinary",
    )
    if tuple(tool.tool_name for tool in ordinary_report.tool_results) == (
        "range_validate",
        "combos",
    ):
        evidence.append("ordinary-range-path-unchanged")
    legacy_orchestrator = Orchestrator(_config(context.root, "legacy-unmarked"))
    legacy_report = legacy_orchestrator.run(
        admission.candidate,
        run_id="re-eval-legacy-unmarked",
    )
    if (
        tuple((tool.tool_name, tool.status) for tool in legacy_report.tool_results)
        == (
            ("range_validate", ToolStatus.SUCCESS),
            ("combos", ToolStatus.SUCCESS),
            ("holdem_equity", ToolStatus.FAILED),
        )
        and legacy_orchestrator.product_store.read_current(legacy_report.run_id).read_status
        is RunReadStatus.SUCCEEDED
    ):
        evidence.append("legacy-unmarked-run-unchanged")
    assert admission.candidate.hand is not None
    validation = validate_versioned_range(
        admission.candidate.hand,
        admission.candidate.hand.known_ranges[0],
    )
    manual_payload = admission.candidate.model_dump(mode="python")
    manual_payload["metadata"] = {
        "tool_inputs": {
            "holdem_equity": expected_versioned_range_equity_input(
                admission.candidate,
                validation,
            )
        }
    }
    manual_orchestrator = Orchestrator(_config(context.root, "legacy-manual-exact"))
    manual_report = manual_orchestrator.run(
        CaseInput.model_validate(manual_payload),
        run_id="re-eval-legacy-manual-exact",
    )
    if (
        tuple((tool.tool_name, tool.status) for tool in manual_report.tool_results)
        == (
            ("range_validate", ToolStatus.SUCCESS),
            ("combos", ToolStatus.SUCCESS),
            ("holdem_equity", ToolStatus.SUCCESS),
        )
        and manual_orchestrator.product_store.read_current(manual_report.run_id).read_status
        is RunReadStatus.SUCCEEDED
    ):
        evidence.append("legacy-manual-exact-run-unchanged")
    return tuple(evidence)


_HANDLERS = MappingProxyType(
    {
        "weighted-exact-oracle": _weighted_exact_oracle,
        "tie-policy": _tie_policy,
        "blockers-and-cap": _blockers_and_cap,
        "invalid-range-boundaries": _invalid_range_boundaries,
        "scope-boundaries": _scope_boundaries,
        "tamper-and-failure-replay": _tamper_and_failure_replay,
        "storage-and-compatibility": _storage_and_compatibility,
    }
)
_HANDLER_IDENTITIES = tuple(_HANDLERS.items())


def _evaluation_work_root(path: Path) -> Path:
    """Resolve a work root and opt in to Win32 extended-length paths."""

    resolved = path.resolve(strict=False)
    if os.name != "nt":
        return resolved
    value = str(resolved)
    if value.startswith("\\\\?\\"):
        return resolved
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def run_range_equity_evaluation(
    fixture: RangeEquityEvaluationFixtureV1,
    *,
    work_root: Path,
    source_commit_id: str,
    source_tree_id: str,
) -> RangeEquityEvaluationResultV1:
    current_handlers = tuple(_HANDLERS.items())
    if (
        tuple(_HANDLERS) != REQUIRED_CASE_IDS
        or len(current_handlers) != len(_HANDLER_IDENTITIES)
        or any(
            current_name != expected_name or current_handler is not expected_handler
            for (current_name, current_handler), (expected_name, expected_handler) in zip(
                current_handlers,
                _HANDLER_IDENTITIES,
                strict=True,
            )
        )
    ):
        raise ValueError("range-equity evaluation handler inventory mismatch")
    work_root = _evaluation_work_root(work_root)
    work_root.mkdir(parents=True, exist_ok=True)
    context = _EvaluationContext(Path(mkdtemp(prefix="i-", dir=work_root)))
    case_results: list[RangeEquityEvaluationCaseResultV1] = []
    for case in fixture.cases:
        try:
            observed = _HANDLERS[case.case_id](context)
        except Exception:
            observed = ("evaluation-observation-failed",)
        passed = observed == case.expected_evidence
        case_score: Literal["0.0", "1.0"] = EVALUATION_THRESHOLD if passed else "0.0"
        case_results.append(
            RangeEquityEvaluationCaseResultV1(
                case_id=case.case_id,
                metric=case.metric,
                expected_evidence=case.expected_evidence,
                observed_evidence=observed,
                score=case_score,
                passed=passed,
            )
        )
    metrics: list[RangeEquityEvaluationMetricV1] = []
    for metric_name in REQUIRED_METRICS:
        selected = [result for result in case_results if result.metric == metric_name]
        declared = sum(len(result.expected_evidence) for result in selected)
        passed_checks = sum(
            len(result.expected_evidence) if result.passed else 0 for result in selected
        )
        metric_score: Literal["0.0", "1.0"] = (
            EVALUATION_THRESHOLD if passed_checks == declared else "0.0"
        )
        metrics.append(
            RangeEquityEvaluationMetricV1(
                metric=metric_name,
                declared_checks=declared,
                passed_checks=passed_checks,
                score=metric_score,
            )
        )
    passed = all(result.passed for result in case_results) and all(
        metric.score == EVALUATION_THRESHOLD for metric in metrics
    )
    payload: dict[str, object] = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "family_id": EVALUATION_FAMILY_ID,
        "fixture_id": fixture.fixture_id,
        "scoring": fixture.scoring,
        "threshold": fixture.threshold,
        "source_commit_id": source_commit_id,
        "source_tree_id": source_tree_id,
        "case_results": tuple(result.model_dump(mode="json") for result in case_results),
        "metrics": tuple(metric.model_dump(mode="json") for metric in metrics),
        "overall_score": EVALUATION_THRESHOLD if passed else "0.0",
        "passed": passed,
    }
    return RangeEquityEvaluationResultV1.model_validate(
        {
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "family_id": EVALUATION_FAMILY_ID,
            "fixture_id": fixture.fixture_id,
            "scoring": fixture.scoring,
            "threshold": fixture.threshold,
            "source_commit_id": source_commit_id,
            "source_tree_id": source_tree_id,
            "case_results": tuple(case_results),
            "metrics": tuple(metrics),
            "overall_score": EVALUATION_THRESHOLD if passed else "0.0",
            "passed": passed,
            "result_sha256": canonical_domain_sha256(EVALUATION_FAMILY_ID, payload),
        },
        strict=True,
    )


__all__ = [
    "EVALUATION_FAMILY_ID",
    "EVALUATION_THRESHOLD",
    "REQUIRED_CASE_EVIDENCE",
    "REQUIRED_CASE_IDS",
    "REQUIRED_METRICS",
    "RangeEquityEvaluationFixtureV1",
    "RangeEquityEvaluationResultV1",
    "load_range_equity_evaluation_fixture",
    "run_range_equity_evaluation",
]
