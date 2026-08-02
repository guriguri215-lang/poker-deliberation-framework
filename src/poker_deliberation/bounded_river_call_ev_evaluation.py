"""Deterministic exact-evidence evaluation for the P3-030C product slice."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from tempfile import mkdtemp
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

import poker_deliberation
from poker_deliberation.bounded_natural_language import (
    prepare_bounded_natural_language_intake,
)
from poker_deliberation.bounded_river_call_ev import (
    BoundedRiverCallEvAdmission,
    BoundedRiverCallEvError,
    admit_bounded_river_call_ev_review,
    build_bounded_river_call_ev_result,
    create_bounded_river_call_ev_authority,
    create_bounded_river_call_ev_confirmation,
    prepare_bounded_river_call_ev_intake,
    verify_bounded_river_call_ev_candidate,
    verify_bounded_river_call_ev_tool_chain,
)
from poker_deliberation.bounded_river_call_ev_models import (
    BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
    BoundedRiverCallEvCandidateV1,
    BoundedRiverCallEvDiagnosticCode,
    BoundedRiverCallEvResultV1,
)
from poker_deliberation.config import AppConfig
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.range_equity_models import canonical_domain_sha256
from poker_deliberation.range_grammar import action_prefix_sha256
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.schemas import Exactness, NumericalExactness, ToolStatus
from poker_deliberation.storage.bounded_river_call_ev_admission_store import (
    read_bounded_river_call_ev_admission_record,
    verify_bounded_river_call_ev_admission_record,
)
from poker_deliberation.storage.range_equity_admission_store import (
    read_range_equity_admission_record,
    verify_range_equity_admission_record,
)
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_json_bytes,
    parse_canonical_model,
    run_lock_key_sha256,
)
from poker_deliberation.storage.terminal_canonical import product_payload_commitments

EVALUATION_FAMILY_ID: Literal["poker-bounded-river-call-ev-evaluation-json-v1"] = (
    "poker-bounded-river-call-ev-evaluation-json-v1"
)
EVALUATION_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
EVALUATION_THRESHOLD: Literal["1.0"] = "1.0"
EvaluationMetric = Literal[
    "exact_decision_math",
    "admission_security",
    "runtime_replay",
]

REQUIRED_CASE_EVIDENCE = (
    (
        "exact-decision-math",
        "exact_decision_math",
        (
            "call-positive-exact-and-ulp",
            "fold-positive-exact-and-ulp",
            "zero-delta-exact-and-ulp",
            "actual-fold-counterfactual-call-ev",
            "three-and-six-player-heads-up-at-focal",
            "blocker-reduction-and-empty-range-refusal",
        ),
    ),
    (
        "intake-and-range-boundaries",
        "admission_security",
        (
            "range-grammar-version-provenance-refused",
            "range-target-action-prefix-street-refused",
            "unsupported-source-and-card-boundaries-refused",
            "multiple-opponent-and-multiple-range-shapes-refused",
        ),
    ),
    (
        "confirmation-and-tool-replay",
        "admission_security",
        (
            "all-independent-hash-domains-bound",
            "expiry-source-and-range-mutation-refused",
            "cross-run-replay-refused",
            "manual-input-and-tool-order-refused",
            "ulp-and-partial-prefix-tamper-refused",
            "exact-cap-and-monte-carlo-metadata-refused",
        ),
    ),
    (
        "context-storage-and-compatibility",
        "runtime_replay",
        (
            "local-provider-role-and-tool-allowlists",
            "canonical-context-only-with-lineage-expiry-and-budget",
            "preexecution-record-and-seven-tool-order",
            "typed-terminal-artifacts-and-immutable-replay",
            "artifact-removal-and-context-tamper-refused",
            "p3-016b-binding-reused-without-generic-marker-bypass",
        ),
    ),
)
REQUIRED_CASE_IDS = tuple(item[0] for item in REQUIRED_CASE_EVIDENCE)
REQUIRED_METRICS: tuple[EvaluationMetric, ...] = (
    "exact_decision_math",
    "admission_security",
    "runtime_replay",
)
_SOURCE_ID_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_EVALUATION_IMPLEMENTATION_MODULES = (
    "poker_deliberation.bounded_natural_language",
    "poker_deliberation.bounded_river_call_ev",
    "poker_deliberation.bounded_river_call_ev_evaluation",
    "poker_deliberation.bounded_river_call_ev_provenance",
    "poker_deliberation.orchestrator",
    "poker_deliberation.range_equity",
    "poker_deliberation.storage.bounded_river_call_ev_admission_store",
    "poker_deliberation.storage.range_equity_admission_store",
)


def _git_stdout(repository_root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), *arguments],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("bounded river call-EV evaluation git inspection failed") from exc
    return completed.stdout.strip()


def verify_bounded_river_call_ev_evaluation_checkout(
    repository_root: Path,
    *,
    source_commit_id: str,
    source_tree_id: str,
) -> None:
    """Bind the CLI evaluation label to one clean, unmodified checkout."""

    if (
        _SOURCE_ID_RE.fullmatch(source_commit_id) is None
        or _SOURCE_ID_RE.fullmatch(source_tree_id) is None
    ):
        raise ValueError("bounded river call-EV evaluation source identity is invalid")
    root = repository_root.resolve()
    actual_commit = _git_stdout(root, "rev-parse", "HEAD")
    actual_tree = _git_stdout(root, "rev-parse", "HEAD^{tree}")
    status = _git_stdout(root, "status", "--porcelain=v1", "--untracked-files=all")
    replace_refs = _git_stdout(root, "replace", "-l")
    index_flags = _git_stdout(root, "ls-files", "-v")
    flagged = any(
        line and (line[0].islower() or line[0] == "S") for line in index_flags.splitlines()
    )
    if (
        actual_commit != source_commit_id
        or actual_tree != source_tree_id
        or status
        or replace_refs
        or flagged
    ):
        raise ValueError("bounded river call-EV evaluation checkout binding mismatch")


def verify_bounded_river_call_ev_evaluation_module_origins(repository_root: Path) -> None:
    """Require every loaded implementation module to originate in the claimed checkout."""

    package_root = (repository_root.resolve() / "src" / "poker_deliberation").resolve()
    package_file = getattr(poker_deliberation, "__file__", None)
    if package_file is None or Path(package_file).resolve() != package_root / "__init__.py":
        raise ValueError("bounded river call-EV evaluation module origin mismatch")
    for module_name in _EVALUATION_IMPLEMENTATION_MODULES:
        module_file = getattr(importlib.import_module(module_name), "__file__", None)
        if module_file is None or not Path(module_file).resolve().is_relative_to(package_root):
            raise ValueError("bounded river call-EV evaluation module origin mismatch")


class _EvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class BoundedRiverCallEvEvaluationCaseV1(_EvaluationModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,95}$")
    metric: EvaluationMetric
    expected_evidence: tuple[str, ...] = Field(min_length=1)


class BoundedRiverCallEvEvaluationFixtureV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    family_id: Literal["poker-bounded-river-call-ev-evaluation-json-v1"] = EVALUATION_FAMILY_ID
    fixture_id: Literal["bounded-river-call-ev-cases-v1"] = "bounded-river-call-ev-cases-v1"
    source_kind: Literal["repository_fixture"] = "repository_fixture"
    license_classification: Literal["repository_owned_mit"] = "repository_owned_mit"
    usage_classification: Literal["redistribution_allowed"] = "redistribution_allowed"
    content_classification: Literal["public"] = "public"
    scoring: Literal["exact-evidence-set-v1"] = "exact-evidence-set-v1"
    threshold: Literal["1.0"] = EVALUATION_THRESHOLD
    cases: tuple[BoundedRiverCallEvEvaluationCaseV1, ...]

    @model_validator(mode="after")
    def exact_inventory(self) -> BoundedRiverCallEvEvaluationFixtureV1:
        observed = tuple((item.case_id, item.metric, item.expected_evidence) for item in self.cases)
        if observed != REQUIRED_CASE_EVIDENCE:
            raise ValueError("bounded river call-EV evaluation case inventory mismatch")
        return self


class BoundedRiverCallEvEvaluationCaseResultV1(_EvaluationModel):
    case_id: str
    metric: str
    expected_evidence: tuple[str, ...]
    observed_evidence: tuple[str, ...]
    score: Literal["0.0", "1.0"]
    passed: bool

    @model_validator(mode="after")
    def exact_case_result(self) -> BoundedRiverCallEvEvaluationCaseResultV1:
        expected = next((item for item in REQUIRED_CASE_EVIDENCE if item[0] == self.case_id), None)
        if expected is None or (self.metric, self.expected_evidence) != expected[1:]:
            raise ValueError("bounded river call-EV case result inventory mismatch")
        matched = self.observed_evidence == self.expected_evidence
        if self.passed is not matched or self.score != ("1.0" if matched else "0.0"):
            raise ValueError("bounded river call-EV case score mismatch")
        return self


class BoundedRiverCallEvEvaluationMetricV1(_EvaluationModel):
    metric: EvaluationMetric
    declared_checks: int = Field(gt=0)
    passed_checks: int = Field(ge=0)
    score: Literal["0.0", "1.0"]


class BoundedRiverCallEvEvaluationResultV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    family_id: Literal["poker-bounded-river-call-ev-evaluation-json-v1"] = EVALUATION_FAMILY_ID
    fixture_id: Literal["bounded-river-call-ev-cases-v1"]
    scoring: Literal["exact-evidence-set-v1"]
    threshold: Literal["1.0"]
    source_commit_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_tree_id: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    case_results: tuple[BoundedRiverCallEvEvaluationCaseResultV1, ...]
    metrics: tuple[BoundedRiverCallEvEvaluationMetricV1, ...]
    overall_score: Literal["0.0", "1.0"]
    passed: bool
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def exact_result(self) -> BoundedRiverCallEvEvaluationResultV1:
        inventory = tuple(
            (item.case_id, item.metric, item.expected_evidence) for item in self.case_results
        )
        if (
            inventory != REQUIRED_CASE_EVIDENCE
            or tuple(item.metric for item in self.metrics) != REQUIRED_METRICS
        ):
            raise ValueError("bounded river call-EV evaluation result inventory mismatch")
        for metric in self.metrics:
            selected = tuple(item for item in self.case_results if item.metric == metric.metric)
            declared = sum(len(item.expected_evidence) for item in selected)
            passed = sum(len(item.expected_evidence) if item.passed else 0 for item in selected)
            if (
                metric.declared_checks != declared
                or metric.passed_checks != passed
                or metric.score != ("1.0" if declared == passed else "0.0")
            ):
                raise ValueError("bounded river call-EV metric summary mismatch")
        passed = all(item.passed for item in self.case_results) and all(
            item.score == "1.0" for item in self.metrics
        )
        if self.passed is not passed or self.overall_score != ("1.0" if passed else "0.0"):
            raise ValueError("bounded river call-EV evaluation summary mismatch")
        payload = self.model_dump(mode="json", exclude={"result_sha256"})
        if self.result_sha256 != canonical_domain_sha256(EVALUATION_FAMILY_ID, payload):
            raise ValueError("bounded river call-EV evaluation result hash mismatch")
        return self


def load_bounded_river_call_ev_evaluation_fixture(
    path: Path,
) -> BoundedRiverCallEvEvaluationFixtureV1:
    return BoundedRiverCallEvEvaluationFixtureV1.model_validate_json(path.read_bytes(), strict=True)


def _source(table_size: int = 2) -> bytes:
    if table_size == 2:
        lines = [
            "これは完了済みのNLHEキャッシュゲームです。参加者は2人です。",
            "ブラインドは1/2で、アンティは0、レーキは0です。",
            "HeroはSBで開始スタック100です。",
            "VillainはBBで開始スタック100です。",
            "HeroのホールカードはAs Kdです。",
            "プリフロップです。",
            "Heroが1をSBとしてポストしました。",
            "Villainが2をBBとしてポストしました。",
            "Heroが1をコールしました。",
            "Villainがチェックしました。",
            "フロップはAh 7d 2cです。",
            "Villainがチェックしました。",
            "Heroが4をベットしました。",
            "Villainが4をコールしました。",
            "ターンは9sです。",
            "Villainが8をベットしました。",
            "Heroが8をコールしました。",
            "リバーは3hです。",
            "Villainが10をベットしました。",
            "Heroがフォールドしました。",
        ]
    else:
        positions = {
            3: (("SeatBTN", "BTN"), ("Hero", "SB"), ("Villain", "BB")),
            6: (
                ("SeatUTG", "UTG"),
                ("SeatHJ", "HJ"),
                ("SeatCO", "CO"),
                ("SeatBTN", "BTN"),
                ("Hero", "SB"),
                ("Villain", "BB"),
            ),
        }[table_size]
        lines = [
            f"これは完了済みのNLHEキャッシュゲームです。参加者は{table_size}人です。",
            "ブラインドは1/2で、アンティは0、レーキは0です。",
            *(f"{player}は{position}で開始スタック100です。" for player, position in positions),
            "HeroのホールカードはAs Kdです。",
            "プリフロップです。",
            "Heroが1をSBとしてポストしました。",
            "Villainが2をBBとしてポストしました。",
            *(
                f"{player}がフォールドしました。"
                for player, position in positions
                if position not in {"SB", "BB"}
            ),
            "Heroが1をコールしました。",
            "Villainがチェックしました。",
            "フロップはAh 7d 2cです。",
            "Heroが4をベットしました。",
            "Villainが4をコールしました。",
            "ターンは9sです。",
            "Heroがチェックしました。",
            "Villainが8をベットしました。",
            "Heroが8をコールしました。",
            "リバーは3hです。",
            "Heroがチェックしました。",
            "Villainが10をベットしました。",
            "Heroがフォールドしました。",
        ]
    lines.extend(
        (
            "判断直前のポットは28です。",
            "コール額は10です。",
            "コール後の争点ポットは48です。",
            "検討対象は、リバーでVillainが10をベットした直後のHeroのコールまたはフォールド判断です。",
        )
    )
    return ("\n".join(lines) + "\n").encode()


def _range(source: bytes, notation: str) -> VersionedRangeDefinitionV1:
    bounded = prepare_bounded_natural_language_intake(
        source,
        intake_id="intake-river-evaluation-range",
        source_id="fixture-river-evaluation-range",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    if bounded.status != "ready" or bounded.candidate is None:
        raise ValueError("evaluation source is not a bounded-language candidate")
    hand = bounded.candidate.projection.hand
    focal = bounded.candidate.projection.focal_decision
    return VersionedRangeDefinitionV1.model_validate(
        {
            "range_id": "villain-river-evaluation",
            "target_player_id": "Villain",
            "notation": notation,
            "source": {
                "source_id": "bounded-river-call-ev-evaluation",
                "source_kind": "repository_fixture",
                "license_classification": "repository_owned_mit",
                "usage_classification": "redistribution_allowed",
                "content_status": "ASSUMPTION",
                "content_sha256": hashlib.sha256(notation.encode()).hexdigest(),
            },
            "game_conditions": {
                "game_type": "NLHE",
                "format": "cash",
                "table_size": hand.table_size,
                "target_position": "BB",
                "street": "river",
                "starting_stack_min_bb_milli": 50_000,
                "starting_stack_max_bb_milli": 50_000,
                "as_of_action_index": focal.facing_action_index + 1,
                "action_prefix_sha256": action_prefix_sha256(hand, focal.facing_action_index + 1),
            },
        }
    )


def _ready(source: bytes, notation: str, token: str) -> BoundedRiverCallEvCandidateV1:
    prepared = prepare_bounded_river_call_ev_intake(
        source,
        _range(source, notation),
        intake_id=f"intake-river-evaluation-{token}",
        source_id=f"fixture-river-evaluation-{token}",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    if prepared.status != "ready" or prepared.candidate is None:
        raise ValueError("evaluation candidate was blocked")
    return prepared.candidate


def _hashes(candidate: BoundedRiverCallEvCandidateV1) -> tuple[str, ...]:
    projection = candidate.projection
    return (
        projection.source_sha256,
        projection.bounded_candidate_sha256,
        projection.source_bindings_sha256,
        projection.focal_sha256,
        projection.extractor_sha256,
        projection.tool_plan_sha256,
        projection.range_definition_sha256,
        projection.range_target_sha256,
        projection.range_binding_sha256,
        projection.equity_model_sha256,
        projection.call_ev_model_sha256,
        candidate.candidate_sha256,
    )


def _admission(notation: str, run_id: str) -> BoundedRiverCallEvAdmission:
    source = _source()
    candidate = _ready(source, notation, run_id)
    confirmation = create_bounded_river_call_ev_confirmation(
        candidate,
        run_id=run_id,
        confirmation_id=f"confirmation-{run_id}",
        idempotency_key=f"idempotency-{run_id}",
        authority=create_bounded_river_call_ev_authority(
            authority_id="local-evaluation-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        expected_hashes=_hashes(candidate),
    )
    return admit_bounded_river_call_ev_review(source, candidate, confirmation)


def _config(root: Path, token: str) -> AppConfig:
    return AppConfig(
        runs_dir=root / token / "l",
        revision_runs_dir=root / token / "p",
        durable_budget_runs_dir=root / token / "b",
    )


class _EvaluationContext:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.completed: dict[str, tuple[Any, Any, Orchestrator]] = {}

    def run(self, notation: str, token: str) -> tuple[Any, Any, Orchestrator]:
        if token not in self.completed:
            admitted = _admission(notation, f"river-eval-{token}")
            orchestrator = Orchestrator(_config(self.root, token))
            report = orchestrator.run_bounded_river_call_ev_review(admitted)
            self.completed[token] = admitted, report, orchestrator
        return self.completed[token]


def _exact_decision_math(context: _EvaluationContext) -> tuple[str, ...]:
    evidence: list[str] = []
    actual_fold_counterfactual = False
    cases = (
        ("QcJc", "positive", Fraction(1), Fraction(38), "call", "call-positive-exact-and-ulp"),
        ("9c9d", "negative", Fraction(0), Fraction(-10), "fold", "fold-positive-exact-and-ulp"),
        (
            "QcJc@0.05,9c9d@0.19",
            "tie",
            Fraction(5, 24),
            Fraction(0),
            "tie",
            "zero-delta-exact-and-ulp",
        ),
    )
    for notation, token, equity, call_ev, comparison, label in cases:
        admitted, report, orchestrator = context.run(notation, token)
        read = orchestrator.product_store.read_current(report.run_id)
        result = parse_canonical_model(
            read.payload_bytes("bounded_river_call_ev_result.json"),
            BoundedRiverCallEvResultV1,
        )
        observed_equity = Fraction(result.equity.numerator, result.equity.denominator)
        observed_ev = Fraction(result.call_ev_units.numerator, result.call_ev_units.denominator)
        if (
            report.run_status == "completed"
            and observed_equity == equity
            and observed_ev == call_ev
            and result.action_comparison == comparison
            and result.required_equity.numerator == 5
            and result.required_equity.denominator == 24
        ):
            evidence.append(label)
        if token == "positive" and (
            admitted.candidate.projection.bounded_candidate.projection.focal_decision.hero_response
            == "fold"
            and result.action_comparison == "call"
        ):
            actual_fold_counterfactual = True
    if actual_fold_counterfactual:
        evidence.append("actual-fold-counterfactual-call-ev")
    multiplayer = tuple(
        _ready(_source(size), "QcJc", f"players-{size}").projection.range_target for size in (3, 6)
    )
    if all(target.eligible_player_ids == ("Hero", "Villain") for target in multiplayer):
        evidence.append("three-and-six-player-heads-up-at-focal")
    reduced = _ready(_source(), "AKs", "blocker")
    empty = prepare_bounded_river_call_ev_intake(
        _source(),
        _range(_source(), "AsKd"),
        intake_id="intake-river-evaluation-empty",
        source_id="fixture-river-evaluation-empty",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    if reduced.projection.range_equity_binding.combo_count < 4 and empty.status == "blocked":
        evidence.append("blocker-reduction-and-empty-range-refusal")
    return tuple(evidence)


def _intake_and_range_boundaries(_context: _EvaluationContext) -> tuple[str, ...]:
    evidence: list[str] = []
    source = _source()
    invalid_grammar = prepare_bounded_river_call_ev_intake(
        source,
        _range(source, "not-a-range"),
        intake_id="intake-invalid-grammar",
        source_id="fixture-invalid-grammar",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    version_and_provenance_refused = 0
    for key, value in (
        ("grammar_version", "2.0.0"),
        ("license_classification", "unknown"),
        ("usage_classification", "local_analysis_only"),
    ):
        payload = _range(source, "QcJc").model_dump(mode="python")
        if key == "grammar_version":
            payload[key] = value
        else:
            payload["source"][key] = value
        try:
            VersionedRangeDefinitionV1.model_validate(payload)
        except ValidationError:
            version_and_provenance_refused += 1
    wrong_content_hash = _range(source, "QcJc").model_copy(
        update={
            "source": _range(source, "QcJc").source.model_copy(update={"content_sha256": "0" * 64})
        }
    )
    content_hash_result = prepare_bounded_river_call_ev_intake(
        source,
        wrong_content_hash,
        intake_id="intake-content-hash",
        source_id="fixture-content-hash",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    if (
        invalid_grammar.status == "blocked"
        and version_and_provenance_refused == 3
        and content_hash_result.status == "blocked"
    ):
        evidence.append("range-grammar-version-provenance-refused")

    refused = 0
    for mutation in ("target", "prefix", "street"):
        payload = _range(source, "QcJc").model_dump(mode="python")
        if mutation == "target":
            payload["target_player_id"] = "Hero"
        elif mutation == "prefix":
            payload["game_conditions"]["action_prefix_sha256"] = "0" * 64
        else:
            payload["game_conditions"]["street"] = "turn"
        result = prepare_bounded_river_call_ev_intake(
            source,
            VersionedRangeDefinitionV1.model_validate(payload),
            intake_id=f"intake-{mutation}",
            source_id=f"fixture-{mutation}",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
        )
        refused += result.status == "blocked"
    if refused == 3:
        evidence.append("range-target-action-prefix-street-refused")

    mutations = (
        source.replace("リバーは3hです。\n".encode(), b""),
        source.replace("HeroのホールカードはAs Kdです。\n".encode(), b""),
        source.replace("リバーは3hです。".encode(), "リバーはAsです。".encode()),
        source.replace("アンティは0".encode(), "アンティは1".encode()),
        source.replace("レーキは0".encode(), "レーキは1".encode()),
        source.replace(
            "Villainが10をベットしました。".encode(),
            "Villainが10をオールインしました。".encode(),
        ),
        source + "サイドポットは10です。\n".encode(),
        source + "Heroがチェックしました。\n".encode(),
    )
    blocked_count = 0
    for index, mutated in enumerate(mutations):
        bounded = prepare_bounded_natural_language_intake(
            mutated,
            intake_id=f"intake-source-boundary-{index}",
            source_id=f"fixture-source-boundary-{index}",
            source_kind="repository_fixture",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            classification="public",
        )
        blocked_count += bounded.status == "blocked"
    if blocked_count == len(mutations):
        evidence.append("unsupported-source-and-card-boundaries-refused")

    active_third = _source(3).replace("SeatBTNがフォールドしました。\n".encode(), b"")
    active_result = prepare_bounded_natural_language_intake(
        active_third,
        intake_id="intake-active-third",
        source_id="fixture-active-third",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    projection = _ready(source, "QcJc", "single-range").projection.model_dump(mode="python")
    projection["range_definitions"] = [projection["range_definition"]]
    try:
        type(_ready(source, "QcJc", "single-range-schema").projection).model_validate(projection)
    except ValidationError:
        multiple_schema_refused = True
    else:
        multiple_schema_refused = False
    if active_result.status == "blocked" and multiple_schema_refused:
        evidence.append("multiple-opponent-and-multiple-range-shapes-refused")
    return tuple(evidence)


def _confirmation_and_tool_replay(context: _EvaluationContext) -> tuple[str, ...]:
    evidence: list[str] = []
    admitted, report, orchestrator = context.run("QcJc", "positive")
    candidate = admitted.candidate
    hash_fields = (
        "source_sha256",
        "bounded_candidate_sha256",
        "source_bindings_sha256",
        "focal_sha256",
        "extractor_sha256",
        "tool_plan_sha256",
        "range_definition_sha256",
        "range_target_sha256",
        "range_binding_sha256",
        "equity_model_sha256",
        "call_ev_model_sha256",
    )
    rejected = 0
    for field in hash_fields:
        projection = candidate.projection.model_copy(update={field: "0" * 64})
        try:
            verify_bounded_river_call_ev_candidate(
                candidate.model_copy(update={"projection": projection})
            )
        except BoundedRiverCallEvError:
            rejected += 1
    try:
        verify_bounded_river_call_ev_candidate(
            candidate.model_copy(update={"candidate_sha256": "0" * 64})
        )
    except BoundedRiverCallEvError:
        rejected += 1
    if rejected == len(hash_fields) + 1:
        evidence.append("all-independent-hash-domains-bound")

    confirmed_at = datetime.now(UTC) - timedelta(hours=2)
    expired = create_bounded_river_call_ev_confirmation(
        candidate,
        run_id="river-eval-expired",
        confirmation_id="confirmation-river-eval-expired",
        idempotency_key="idempotency-river-eval-expired",
        authority=create_bounded_river_call_ev_authority(
            authority_id="local-evaluation-user",
            authority_kind="local_user",
            authentication="self_asserted",
        ),
        expected_hashes=_hashes(candidate),
        confirmed_at=confirmed_at,
        expires_at=confirmed_at + timedelta(hours=1),
    )
    expiry_and_source = 0
    for changed_source in (admitted.source_bytes, admitted.source_bytes + b"\n"):
        try:
            admit_bounded_river_call_ev_review(changed_source, candidate, expired)
        except BoundedRiverCallEvError:
            expiry_and_source += 1
    changed_range_candidate = _ready(_source(), "9c9d", "changed-range")
    try:
        admit_bounded_river_call_ev_review(
            admitted.source_bytes,
            changed_range_candidate,
            admitted.confirmation,
        )
    except BoundedRiverCallEvError:
        range_refused = True
    else:
        range_refused = False
    if expiry_and_source == 2 and range_refused:
        evidence.append("expiry-source-and-range-mutation-refused")

    try:
        orchestrator.run_bounded_river_call_ev_review(
            _admission("9c9d", admitted.confirmation.run_id)
        )
    except BoundedRiverCallEvError:
        evidence.append("cross-run-replay-refused")

    results = list(report.tool_results)
    payload = admitted.case.model_dump(mode="python")
    payload["metadata"]["tool_inputs"]["raked_call_ev"]["equity"] = 0.5
    manual = replace(admitted, case=admitted.case.model_validate(payload))
    try:
        Orchestrator(_config(context.root, "manual")).run_bounded_river_call_ev_review(manual)
    except BoundedRiverCallEvError:
        manual_refused = True
    else:
        manual_refused = False
    try:
        build_bounded_river_call_ev_result(admitted, [results[1], results[0], *results[2:]])
    except BoundedRiverCallEvError:
        order_refused = True
    else:
        order_refused = False
    if manual_refused and order_refused:
        evidence.append("manual-input-and-tool-order-refused")

    pot = results[2]
    output = dict(pot.output)
    output["required_equity"] = 0.5
    ulp_refused = False
    try:
        build_bounded_river_call_ev_result(
            admitted, [*results[:2], pot.model_copy(update={"output": output}), *results[3:]]
        )
    except BoundedRiverCallEvError:
        ulp_refused = True
    forged_prefix = results[0].model_copy(update={"output": {"valid": True}})
    failed = results[1].model_copy(
        update={
            "status": ToolStatus.FAILED,
            "exactness": Exactness.UNAVAILABLE,
            "numeric_exactness": NumericalExactness.UNAVAILABLE,
            "output": {},
            "verification": None,
            "error": "fixture failure",
        }
    )
    try:
        verify_bounded_river_call_ev_tool_chain(
            admitted,
            [forged_prefix, failed],
            run_status="failed_with_limitations",
        )
    except BoundedRiverCallEvError:
        partial_refused = True
    else:
        partial_refused = False
    if ulp_refused and partial_refused:
        evidence.append("ulp-and-partial-prefix-tamper-refused")

    exact = results[5]
    monte_carlo = dict(exact.output)
    monte_carlo.update({"samples": 1, "seed": 7})
    cap_input = dict(exact.input)
    cap_input["max_exact_evaluations"] = 0
    refused_count = 0
    for forged in (
        exact.model_copy(update={"output": monte_carlo}),
        exact.model_copy(update={"input": cap_input}),
    ):
        try:
            build_bounded_river_call_ev_result(admitted, [*results[:5], forged, results[6]])
        except (BoundedRiverCallEvError, ValueError):
            refused_count += 1
    if refused_count == 2:
        evidence.append("exact-cap-and-monte-carlo-metadata-refused")
    return tuple(evidence)


def _preexecution_guard_refuses_before_dispatch(
    context: _EvaluationContext,
    *,
    missing_record: Literal["bounded", "range"],
) -> bool:
    admitted = _admission("QcJc", f"river-eval-preexecution-{missing_record}")
    orchestrator = Orchestrator(_config(context.root, f"preexecution-{missing_record}"))
    run_id = admitted.confirmation.run_id
    orchestrator._initialize_product_storage(run_id)
    with orchestrator._new_run_authority(run_id):
        orchestrator._reserve_new_run_under_authority(admitted.case, run_id)
    journal_directory = {
        "bounded": "bounded-river-call-ev-admissions",
        "range": "range-equity-admissions",
    }[missing_record]
    record_path = (
        orchestrator.product_store.revision_root
        / ".revision-control"
        / journal_directory
        / f"{run_lock_key_sha256(run_id)}.json"
    )
    record_path.unlink()
    orchestrator._bounded_river_call_ev_admissions[run_id] = admitted
    try:
        orchestrator._run(
            admitted.case,
            run_id,
            normalization=None,
            new_run_reserved=True,
        )
    except BoundedRiverCallEvError as exc:
        refused = exc.code is BoundedRiverCallEvDiagnosticCode.STORAGE
    else:
        refused = False
    finally:
        orchestrator._bounded_river_call_ev_admissions.pop(run_id, None)
        orchestrator._bounded_river_call_ev_results.pop(run_id, None)
    artifact_names = {
        item.inventory.logical_name for item in orchestrator.store.verified_payloads(run_id)
    }
    return (
        refused
        and "bounded_river_call_ev_source.txt" not in artifact_names
        and not any(name.startswith("tool_results/") for name in artifact_names)
    )


def _context_storage_and_compatibility(context: _EvaluationContext) -> tuple[str, ...]:
    evidence: list[str] = []
    admitted, report, orchestrator = context.run("QcJc", "positive")
    read = orchestrator.product_store.read_current(report.run_id)
    assignments = json.loads(read.payload_bytes("assignments.json"))
    records = json.loads(read.payload_bytes("agent_execution_records.json"))
    math_records = [item for item in records if item["agent_role"] == "math-auditor"]
    if (
        all(item["provider"] == "local" and item["provider_version"] == "1.0.0" for item in records)
        and len(math_records) == 1
        and tuple(math_records[0]["allowed_tools"]) == BOUNDED_RIVER_CALL_EV_TOOL_ORDER
        and all(item["allowed_tools"] == [] for item in records if item not in math_records)
    ):
        evidence.append("local-provider-role-and-tool-allowlists")
    if (
        admitted.case.raw_text is None
        and all(item["context_classification"] == "internal" for item in records)
        and all(item["context_consumer_runtime"] == "python-local" for item in records)
        and all(item["context_producer_runtime"] == "python-local" for item in records)
        and all(item["context_expires_at"] > item["started_at"] for item in records)
        and all(item["context_id"] and item["context_attempt_id"] for item in records)
        and all(item["context_source_sha256"] for item in records)
        and all(item["context_policy_sha256"] for item in records)
        and all(item["context_envelope_sha256"] for item in records)
        and all("raw_text" not in item["context_keys"] for item in assignments)
        and read.manifest.budget_policy_sha256 == orchestrator.budget_policy.canonical_sha256
        and read.manifest.budget_binding.budget_policy_sha256
        == orchestrator.budget_policy.canonical_sha256
    ):
        evidence.append("canonical-context-only-with-lineage-expiry-and-budget")
    artifact_names = {item.inventory.logical_name for item in read.payloads}
    range_record = read_range_equity_admission_record(
        orchestrator.product_store.revision_root,
        report.run_id,
        maximum_bytes=orchestrator.budget_policy.max_artifact_bytes,
    )
    bounded_record = read_bounded_river_call_ev_admission_record(
        orchestrator.product_store.revision_root,
        report.run_id,
        maximum_bytes=orchestrator.budget_policy.max_artifact_bytes,
    )
    records_verified = range_record is not None and bounded_record is not None
    if range_record is not None:
        verify_range_equity_admission_record(
            range_record,
            admitted.range_equity_admission.binding,
        )
    if bounded_record is not None:
        verify_bounded_river_call_ev_admission_record(
            bounded_record,
            admitted.binding,
        )
    if (
        records_verified
        and _preexecution_guard_refuses_before_dispatch(
            context,
            missing_record="bounded",
        )
        and _preexecution_guard_refuses_before_dispatch(
            context,
            missing_record="range",
        )
        and tuple(item.tool_name for item in report.tool_results)
        == BOUNDED_RIVER_CALL_EV_TOOL_ORDER
        and "bounded_river_call_ev_binding.json" in artifact_names
    ):
        evidence.append("preexecution-record-and-seven-tool-order")
    typed = {
        "bounded_river_call_ev_source.txt",
        "bounded_river_call_ev_range.json",
        "bounded_river_call_ev_candidate.json",
        "bounded_river_call_ev_confirmation.json",
        "bounded_river_call_ev_binding.json",
        "bounded_river_call_ev_result.json",
        "bounded_river_call_ev_provenance.json",
    }
    replay = orchestrator.run_bounded_river_call_ev_review(admitted)
    if typed <= artifact_names and replay == report:
        evidence.append("typed-terminal-artifacts-and-immutable-replay")

    payloads = {
        item.inventory.logical_name: item.exact_bytes
        for item in read.payloads
        if item.inventory.logical_name != "lifecycle_audit.json"
    }
    del payloads["bounded_river_call_ev_result.json"]
    removal_refused = False
    try:
        product_payload_commitments(
            payloads,
            run_id=report.run_id,
            status="succeeded",
            revision=read.revision,
            revision_root=orchestrator.product_store.revision_root,
            transaction_id=read.transaction_id,
        )
    except CanonicalStorageError:
        removal_refused = True
    payloads = {
        item.inventory.logical_name: item.exact_bytes
        for item in read.payloads
        if item.inventory.logical_name != "lifecycle_audit.json"
    }
    mutated_records = json.loads(payloads["agent_execution_records.json"])
    mutated_records[0]["context_consumer_runtime"] = "forged-runtime"
    payloads["agent_execution_records.json"] = canonical_json_bytes(mutated_records)
    try:
        product_payload_commitments(
            payloads,
            run_id=report.run_id,
            status="succeeded",
            revision=read.revision,
            revision_root=orchestrator.product_store.revision_root,
            transaction_id=read.transaction_id,
        )
    except CanonicalStorageError:
        context_refused = True
    else:
        context_refused = False
    if removal_refused and context_refused:
        evidence.append("artifact-removal-and-context-tamper-refused")
    try:
        Orchestrator(_config(context.root, "generic")).run(
            admitted.case,
            run_id="river-eval-generic-marker",
        )
    except BoundedRiverCallEvError:
        marker_refused = True
    else:
        marker_refused = False
    if (
        marker_refused
        and admitted.range_equity_admission.binding
        == admitted.candidate.projection.range_equity_binding
    ):
        evidence.append("p3-016b-binding-reused-without-generic-marker-bypass")
    return tuple(evidence)


_HANDLERS = MappingProxyType(
    {
        "exact-decision-math": _exact_decision_math,
        "intake-and-range-boundaries": _intake_and_range_boundaries,
        "confirmation-and-tool-replay": _confirmation_and_tool_replay,
        "context-storage-and-compatibility": _context_storage_and_compatibility,
    }
)
_HANDLER_IDENTITIES = tuple(_HANDLERS.items())


def _evaluation_work_root(path: Path) -> Path:
    resolved = path.resolve(strict=False)
    if os.name != "nt":
        return resolved
    value = str(resolved)
    if value.startswith("\\\\?\\"):
        return resolved
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)


def run_bounded_river_call_ev_evaluation(
    fixture: BoundedRiverCallEvEvaluationFixtureV1,
    *,
    repository_root: Path,
    work_root: Path,
    source_commit_id: str,
    source_tree_id: str,
) -> BoundedRiverCallEvEvaluationResultV1:
    verify_bounded_river_call_ev_evaluation_module_origins(repository_root)
    verify_bounded_river_call_ev_evaluation_checkout(
        repository_root,
        source_commit_id=source_commit_id,
        source_tree_id=source_tree_id,
    )
    current = tuple(_HANDLERS.items())
    if (
        tuple(_HANDLERS) != REQUIRED_CASE_IDS
        or len(current) != len(_HANDLER_IDENTITIES)
        or any(
            name != expected_name or handler is not expected_handler
            for (name, handler), (expected_name, expected_handler) in zip(
                current, _HANDLER_IDENTITIES, strict=True
            )
        )
    ):
        raise ValueError("bounded river call-EV evaluation handler inventory mismatch")
    root = _evaluation_work_root(work_root)
    root.mkdir(parents=True, exist_ok=True)
    context = _EvaluationContext(Path(mkdtemp(prefix="i-", dir=root)))
    case_results: list[BoundedRiverCallEvEvaluationCaseResultV1] = []
    for case in fixture.cases:
        try:
            observed = _HANDLERS[case.case_id](context)
        except Exception:
            observed = ("evaluation-observation-failed",)
        passed = observed == case.expected_evidence
        case_results.append(
            BoundedRiverCallEvEvaluationCaseResultV1(
                case_id=case.case_id,
                metric=case.metric,
                expected_evidence=case.expected_evidence,
                observed_evidence=observed,
                score="1.0" if passed else "0.0",
                passed=passed,
            )
        )
    metrics: list[BoundedRiverCallEvEvaluationMetricV1] = []
    for metric_name in REQUIRED_METRICS:
        selected = tuple(item for item in case_results if item.metric == metric_name)
        declared = sum(len(item.expected_evidence) for item in selected)
        passed_checks = sum(len(item.expected_evidence) if item.passed else 0 for item in selected)
        metrics.append(
            BoundedRiverCallEvEvaluationMetricV1(
                metric=metric_name,
                declared_checks=declared,
                passed_checks=passed_checks,
                score="1.0" if declared == passed_checks else "0.0",
            )
        )
    passed = all(item.passed for item in case_results) and all(
        item.score == "1.0" for item in metrics
    )
    payload: dict[str, object] = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "family_id": EVALUATION_FAMILY_ID,
        "fixture_id": fixture.fixture_id,
        "scoring": fixture.scoring,
        "threshold": fixture.threshold,
        "source_commit_id": source_commit_id,
        "source_tree_id": source_tree_id,
        "case_results": tuple(item.model_dump(mode="json") for item in case_results),
        "metrics": tuple(item.model_dump(mode="json") for item in metrics),
        "overall_score": "1.0" if passed else "0.0",
        "passed": passed,
    }
    return BoundedRiverCallEvEvaluationResultV1.model_validate(
        {
            **payload,
            "case_results": tuple(case_results),
            "metrics": tuple(metrics),
            "result_sha256": canonical_domain_sha256(EVALUATION_FAMILY_ID, payload),
        },
        strict=True,
    )


__all__ = [
    "EVALUATION_FAMILY_ID",
    "REQUIRED_CASE_EVIDENCE",
    "REQUIRED_CASE_IDS",
    "REQUIRED_METRICS",
    "BoundedRiverCallEvEvaluationFixtureV1",
    "BoundedRiverCallEvEvaluationResultV1",
    "load_bounded_river_call_ev_evaluation_fixture",
    "run_bounded_river_call_ev_evaluation",
    "verify_bounded_river_call_ev_evaluation_checkout",
    "verify_bounded_river_call_ev_evaluation_module_origins",
]
