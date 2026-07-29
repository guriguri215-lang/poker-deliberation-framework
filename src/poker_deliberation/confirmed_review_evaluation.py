"""Deterministic 17-case evaluation for confirmed-review intake contracts."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import mkdtemp
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from poker_deliberation.config import AppConfig
from poker_deliberation.confirmed_review import (
    ConfirmedReviewAdmission,
    ConfirmedReviewError,
    _admit_confirmed_review_at,
    build_confirmed_review_provenance,
    create_review_confirmation,
    prepare_review_intake,
)
from poker_deliberation.confirmed_review_models import (
    MAX_CONFIRMED_REVIEW_SOURCE_BYTES,
    ConfirmedReviewDiagnosticCode,
    ReviewConfirmationAuthorityV1,
    ReviewIntakeCandidateV1,
    ReviewIntakeConfirmationV1,
    ReviewIntakePreparationResultV1,
)
from poker_deliberation.orchestrator import Orchestrator
from poker_deliberation.providers import LocalProvider
from poker_deliberation.range_grammar import action_prefix_sha256
from poker_deliberation.schemas import EpistemicLabel, FinalReport
from poker_deliberation.storage.revision_canonical import (
    CanonicalStorageError,
    canonical_domain_sha256,
)
from poker_deliberation.storage.terminal_canonical import product_payload_commitments

EVALUATION_FAMILY_ID: Literal["poker-confirmed-review-evaluation-json-v1"] = (
    "poker-confirmed-review-evaluation-json-v1"
)
EVALUATION_SCHEMA_VERSION = "1.0.0"
EVALUATION_THRESHOLD = "1.0"

REQUIRED_CASE_EVIDENCE = (
    ("confirmed-complete-hand", ("complete-local-terminal",)),
    ("confirmed-hand-one-range", ("range-validate-then-combos",)),
    ("missing-required-fact", ("missing-fact-rejected",)),
    ("source-candidate-hash-mismatch", ("hash-mismatch-rejected",)),
    ("source-mutation-after-confirmation", ("source-mutation-rejected",)),
    ("candidate-mutation-after-confirmation", ("candidate-mutation-rejected",)),
    ("missing-invalid-confirmation", ("missing-confirmation-rejected",)),
    ("expired-confirmation", ("expired-confirmation-rejected",)),
    ("authority-scope-mismatch", ("authority-scope-rejected",)),
    ("cross-run-replay", ("cross-run-replay-rejected",)),
    ("unsupported-range", ("unsupported-range-rejected",)),
    ("multiple-ranges", ("multiple-ranges-rejected",)),
    ("external-provider-request", ("external-provider-rejected",)),
    ("solver-request", ("solver-request-rejected",)),
    ("report-claim-overreach", ("report-overreach-rejected",)),
    ("missing-tampered-storage-artifact", ("storage-tamper-rejected",)),
    (
        "size-encoding-security-boundary",
        (
            "size-boundary-rejected",
            "encoding-boundary-rejected",
            "security-boundary-rejected",
        ),
    ),
)
REQUIRED_CASE_IDS = tuple(case_id for case_id, _evidence in REQUIRED_CASE_EVIDENCE)

_SOURCE = b"Synthetic retrospective NLHE hand for contract evaluation.\n"


class _EvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class ConfirmedReviewEvaluationCaseV1(_EvaluationModel):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,95}$")
    expected_evidence: tuple[str, ...] = Field(min_length=1)


class ConfirmedReviewEvaluationFixtureV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    family_id: Literal["poker-confirmed-review-evaluation-json-v1"] = EVALUATION_FAMILY_ID
    fixture_id: Literal["confirmed-review-contract-cases-v1"] = "confirmed-review-contract-cases-v1"
    source_kind: Literal["repository_fixture"] = "repository_fixture"
    license_classification: Literal["repository_owned_mit"] = "repository_owned_mit"
    usage_classification: Literal["redistribution_allowed"] = "redistribution_allowed"
    content_classification: Literal["public"] = "public"
    scoring: Literal["exact-evidence-set-v1"] = "exact-evidence-set-v1"
    threshold: Literal["1.0"] = "1.0"
    cases: tuple[ConfirmedReviewEvaluationCaseV1, ...]

    @model_validator(mode="after")
    def exact_case_inventory(self) -> ConfirmedReviewEvaluationFixtureV1:
        inventory = tuple((item.case_id, item.expected_evidence) for item in self.cases)
        case_ids = tuple(case_id for case_id, _evidence in inventory)
        if (
            inventory != REQUIRED_CASE_EVIDENCE
            or case_ids != REQUIRED_CASE_IDS
            or len(case_ids) != len(set(case_ids))
        ):
            raise ValueError("confirmed-review evaluation case inventory mismatch")
        return self


class ConfirmedReviewEvaluationCaseResultV1(_EvaluationModel):
    case_id: str
    expected_evidence: tuple[str, ...]
    observed_evidence: tuple[str, ...]
    score: Literal["0.0", "1.0"]
    passed: bool

    @model_validator(mode="after")
    def evidence_and_score_are_exact(self) -> ConfirmedReviewEvaluationCaseResultV1:
        expected_passed = self.expected_evidence == self.observed_evidence
        if self.passed is not expected_passed or self.score != (
            "1.0" if expected_passed else "0.0"
        ):
            raise ValueError("confirmed-review evaluation case result mismatch")
        return self


class ConfirmedReviewEvaluationResultV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = "1.0.0"
    family_id: Literal["poker-confirmed-review-evaluation-json-v1"] = EVALUATION_FAMILY_ID
    fixture_id: Literal["confirmed-review-contract-cases-v1"] = "confirmed-review-contract-cases-v1"
    scoring: Literal["exact-evidence-set-v1"] = "exact-evidence-set-v1"
    threshold: Literal["1.0"] = "1.0"
    case_results: tuple[ConfirmedReviewEvaluationCaseResultV1, ...]
    overall_score: Literal["0.0", "1.0"]
    passed: bool
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def inventory_score_and_digest_are_exact(self) -> ConfirmedReviewEvaluationResultV1:
        inventory = tuple((item.case_id, item.expected_evidence) for item in self.case_results)
        case_ids = tuple(case_id for case_id, _evidence in inventory)
        if (
            inventory != REQUIRED_CASE_EVIDENCE
            or case_ids != REQUIRED_CASE_IDS
            or len(case_ids) != len(set(case_ids))
        ):
            raise ValueError("confirmed-review evaluation result inventory mismatch")
        all_passed = all(item.passed and item.score == "1.0" for item in self.case_results)
        if self.passed is not all_passed or self.overall_score != ("1.0" if all_passed else "0.0"):
            raise ValueError("confirmed-review evaluation overall result mismatch")
        expected_digest = canonical_domain_sha256(
            EVALUATION_FAMILY_ID,
            self.model_dump(mode="json", exclude={"result_sha256"}),
        )
        if self.result_sha256 != expected_digest:
            raise ValueError("confirmed-review evaluation result digest mismatch")
        return self


def load_confirmed_review_evaluation_fixture(
    path: Path,
) -> ConfirmedReviewEvaluationFixtureV1:
    return ConfirmedReviewEvaluationFixtureV1.model_validate_json(
        path.read_bytes(),
        strict=True,
    )


def load_confirmed_review_evaluation_result(
    path: Path,
) -> ConfirmedReviewEvaluationResultV1:
    return ConfirmedReviewEvaluationResultV1.model_validate_json(
        path.read_bytes(),
        strict=True,
    )


def _candidate_payload(*, intake_id: str) -> dict[str, Any]:
    return {
        "intake_id": intake_id,
        "hand": {
            "game_type": "NLHE",
            "format": "cash",
            "table_size": 2,
            "small_blind": 1,
            "big_blind": 2,
            "players": [
                {"player_id": "hero", "position": "SB", "starting_stack": 100},
                {"player_id": "villain", "position": "BB", "starting_stack": 100},
            ],
            "hero_player_id": "hero",
            "hero_cards": ["As", "Kh"],
            "actions": [
                {
                    "street": "preflop",
                    "actor": "hero",
                    "action": "post_blind",
                    "amount": 1,
                },
                {
                    "street": "preflop",
                    "actor": "villain",
                    "action": "post_blind",
                    "amount": 2,
                },
                {
                    "street": "preflop",
                    "actor": "hero",
                    "action": "raise",
                    "amount": 5,
                    "to_amount": 6,
                },
                {
                    "street": "preflop",
                    "actor": "villain",
                    "action": "fold",
                    "amount": 0,
                },
            ],
        },
        "ambiguities": [],
        "claims": [
            {
                "claim_id": "claim-evaluation-1",
                "text": "The raise was best.",
                "label": "USER_CLAIM",
                "confidence": "C",
            }
        ],
    }


def _prepare(
    payload: object,
    *,
    source: bytes = _SOURCE,
    source_id: str = "source-evaluation-1",
) -> ReviewIntakePreparationResultV1:
    return prepare_review_intake(
        source,
        payload,
        source_id=source_id,
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )


def _confirmation(
    candidate: ReviewIntakeCandidateV1,
    *,
    run_id: str,
    confirmed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> ReviewIntakeConfirmationV1:
    authority = ReviewConfirmationAuthorityV1(
        authority_id="evaluation-authority",
        authority_kind="verified_application",
        authentication="verified",
    )
    return create_review_confirmation(
        candidate,
        run_id=run_id,
        confirmation_id=f"confirmation-{run_id}",
        idempotency_key=f"idempotency-{run_id}",
        authority=authority,
        expected_source_sha256=candidate.projection.source.content_sha256,
        expected_candidate_sha256=candidate.candidate_sha256,
        confirmed_at=confirmed_at or datetime.now(UTC),
        expires_at=expires_at,
    )


def _admission(
    *,
    run_id: str,
    payload: object | None = None,
) -> ConfirmedReviewAdmission:
    prepared = _prepare(
        _candidate_payload(intake_id=f"intake-{run_id}") if payload is None else payload
    )
    if prepared.candidate is None:
        raise AssertionError("evaluation fixture did not prepare")
    confirmation = _confirmation(prepared.candidate, run_id=run_id)
    return _admit_confirmed_review_at(
        _SOURCE,
        prepared.candidate,
        confirmation,
        admitted_at=confirmation.confirmed_at,
    )


def _range_payload(*, invalid: bool = False, multiple: bool = False) -> dict[str, Any]:
    payload = _candidate_payload(intake_id="intake-evaluation-range")
    hand_payload = payload["hand"]
    hand = _prepare(payload).candidate
    if hand is None:
        raise AssertionError("base evaluation hand did not prepare")
    canonical_hand = hand.projection.candidate_input.hand
    notation = "AKs@0.25,QQ@0.5" if not invalid else "AKs,,QQ"
    definition = {
        "schema_version": "1.0.0",
        "grammar_id": "poker-deliberation.nlhe-range",
        "grammar_version": "1.0.0",
        "range_id": "evaluation-range-1",
        "target_player_id": "villain",
        "notation": notation,
        "source": {
            "source_id": "evaluation-range-source",
            "source_kind": "repository_fixture",
            "license_classification": "repository_owned_mit",
            "usage_classification": "redistribution_allowed",
            "content_status": "ASSUMPTION",
            "content_sha256": hashlib.sha256(notation.encode()).hexdigest(),
        },
        "game_conditions": {
            "game_type": "NLHE",
            "format": "cash",
            "table_size": 2,
            "target_position": "BB",
            "street": "preflop",
            "starting_stack_min_bb_milli": 50_000,
            "starting_stack_max_bb_milli": 50_000,
            "as_of_action_index": 2,
            "action_prefix_sha256": action_prefix_sha256(canonical_hand, 2),
        },
    }
    ranges = [definition]
    if multiple:
        second = json.loads(json.dumps(definition))
        second["range_id"] = "evaluation-range-2"
        ranges.append(second)
    hand_payload["known_ranges"] = ranges
    return payload


class _EvaluationContext:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._base: (
            tuple[
                ConfirmedReviewAdmission,
                FinalReport,
                Orchestrator,
                dict[str, bytes],
            ]
            | None
        ) = None

    def base(
        self,
    ) -> tuple[
        ConfirmedReviewAdmission,
        FinalReport,
        Orchestrator,
        dict[str, bytes],
    ]:
        if self._base is None:
            admission = _admission(run_id="run-evaluation-complete")
            config = AppConfig(
                runs_dir=self.root / "complete" / "legacy",
                revision_runs_dir=self.root / "complete" / "product",
                durable_budget_runs_dir=self.root / "complete" / "budget",
            )
            orchestrator = Orchestrator(config, provider=LocalProvider())
            report = orchestrator.run_confirmed_review(admission)
            read = orchestrator.product_store.read_current(report.run_id)
            payloads = {
                item.inventory.logical_name: item.exact_bytes
                for item in read.payloads
                if item.inventory.logical_name != "lifecycle_audit.json"
            }
            self._base = admission, report, orchestrator, payloads
        return self._base


def _complete(context: _EvaluationContext) -> tuple[str, ...]:
    _admission_value, report, _orchestrator, _payloads = context.base()
    return ("complete-local-terminal",) if report.run_status == "completed" else ()


def _one_range(context: _EvaluationContext) -> tuple[str, ...]:
    admission = _admission(
        run_id="run-evaluation-range",
        payload=_range_payload(),
    )
    config = AppConfig(
        runs_dir=context.root / "range" / "legacy",
        revision_runs_dir=context.root / "range" / "product",
        durable_budget_runs_dir=context.root / "range" / "budget",
    )
    report = Orchestrator(config, provider=LocalProvider()).run_confirmed_review(admission)
    names = tuple(item.tool_name for item in report.tool_results)
    return (
        ("range-validate-then-combos",)
        if names == ("hand_validator", "range_validate", "combos")
        else ()
    )


def _missing_fact(_context: _EvaluationContext) -> tuple[str, ...]:
    payload = _candidate_payload(intake_id="intake-missing")
    payload["hand"]["hero_cards"] = []
    result = _prepare(payload)
    return (
        ("missing-fact-rejected",)
        if result.diagnostics
        and result.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_MISSING
        else ()
    )


def _hash_mismatch(_context: _EvaluationContext) -> tuple[str, ...]:
    prepared = _prepare(_candidate_payload(intake_id="intake-hash"))
    if prepared.candidate is None:
        return ()
    authority = ReviewConfirmationAuthorityV1(
        authority_id="evaluation-authority",
        authority_kind="verified_application",
        authentication="verified",
    )
    try:
        create_review_confirmation(
            prepared.candidate,
            run_id="run-evaluation-hash",
            confirmation_id="confirmation-evaluation-hash",
            idempotency_key="idempotency-evaluation-hash",
            authority=authority,
            expected_source_sha256="0" * 64,
            expected_candidate_sha256=prepared.candidate.candidate_sha256,
        )
    except ConfirmedReviewError as exc:
        if exc.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING:
            return ("hash-mismatch-rejected",)
    return ()


def _source_mutation(_context: _EvaluationContext) -> tuple[str, ...]:
    admission = _admission(run_id="run-evaluation-source-mutation")
    try:
        _admit_confirmed_review_at(
            _SOURCE + b"mutation\n",
            admission.candidate,
            admission.confirmation,
            admitted_at=admission.admitted_at,
        )
    except ConfirmedReviewError as exc:
        if exc.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING:
            return ("source-mutation-rejected",)
    return ()


def _candidate_mutation(_context: _EvaluationContext) -> tuple[str, ...]:
    admission = _admission(run_id="run-evaluation-candidate-mutation")
    projection = admission.candidate.projection
    candidate_input = projection.candidate_input
    changed_claim = candidate_input.claims[0].model_copy(update={"text": "changed"})
    changed_input = candidate_input.model_copy(update={"claims": (changed_claim,)})
    changed_projection = projection.model_copy(update={"candidate_input": changed_input})
    changed_candidate = admission.candidate.model_copy(update={"projection": changed_projection})
    try:
        _admit_confirmed_review_at(
            _SOURCE,
            changed_candidate,
            admission.confirmation,
            admitted_at=admission.admitted_at,
        )
    except ConfirmedReviewError as exc:
        if exc.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_BINDING:
            return ("candidate-mutation-rejected",)
    return ()


def _missing_confirmation(_context: _EvaluationContext) -> tuple[str, ...]:
    try:
        ReviewIntakeConfirmationV1.model_validate(None, strict=True)
    except ValidationError:
        return ("missing-confirmation-rejected",)
    return ()


def _expired(_context: _EvaluationContext) -> tuple[str, ...]:
    prepared = _prepare(_candidate_payload(intake_id="intake-expired"))
    if prepared.candidate is None:
        return ()
    reference_time = datetime.now(UTC)
    confirmation = _confirmation(
        prepared.candidate,
        run_id="run-evaluation-expired",
        confirmed_at=reference_time - timedelta(seconds=2),
        expires_at=reference_time - timedelta(seconds=1),
    )
    try:
        _admit_confirmed_review_at(
            _SOURCE,
            prepared.candidate,
            confirmation,
            admitted_at=reference_time,
        )
    except ConfirmedReviewError as exc:
        if exc.code is ConfirmedReviewDiagnosticCode.CONFIRMATION_EXPIRED:
            return ("expired-confirmation-rejected",)
    return ()


def _authority_scope(_context: _EvaluationContext) -> tuple[str, ...]:
    try:
        ReviewConfirmationAuthorityV1.model_validate(
            {
                "authority_id": "evaluation-authority",
                "authority_kind": "verified_application",
                "authentication": "verified",
                "scope": "approve_external_effect",
            },
            strict=True,
        )
    except ValidationError:
        return ("authority-scope-rejected",)
    return ()


def _cross_run(context: _EvaluationContext) -> tuple[str, ...]:
    _admission_value, _report, _orchestrator, payloads = context.base()
    try:
        product_payload_commitments(
            payloads,
            run_id="run-evaluation-other",
            status="succeeded",
        )
    except CanonicalStorageError:
        return ("cross-run-replay-rejected",)
    return ()


def _unsupported_range(_context: _EvaluationContext) -> tuple[str, ...]:
    prepared = _prepare(_range_payload(invalid=True))
    if prepared.candidate is None:
        return ()
    confirmation = _confirmation(
        prepared.candidate,
        run_id="run-evaluation-unsupported-range",
    )
    try:
        _admit_confirmed_review_at(
            _SOURCE,
            prepared.candidate,
            confirmation,
            admitted_at=confirmation.confirmed_at,
        )
    except ConfirmedReviewError as exc:
        if exc.code is ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_UNSUPPORTED:
            return ("unsupported-range-rejected",)
    return ()


def _multiple_ranges(_context: _EvaluationContext) -> tuple[str, ...]:
    prepared = _prepare(_range_payload(multiple=True))
    return (
        ("multiple-ranges-rejected",)
        if prepared.diagnostics
        and prepared.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_RANGE_COUNT
        else ()
    )


def _external_provider(context: _EvaluationContext) -> tuple[str, ...]:
    admission = _admission(run_id="run-evaluation-external-provider")

    class AlternateLocalProvider(LocalProvider):
        pass

    config = AppConfig(
        runs_dir=context.root / "external" / "legacy",
        revision_runs_dir=context.root / "external" / "product",
        durable_budget_runs_dir=context.root / "external" / "budget",
    )
    try:
        Orchestrator(
            config,
            provider=AlternateLocalProvider(),
        ).run_confirmed_review(admission)
    except ConfirmedReviewError as exc:
        if exc.code is ConfirmedReviewDiagnosticCode.LOCAL_PROVIDER:
            return ("external-provider-rejected",)
    return ()


def _solver(_context: _EvaluationContext) -> tuple[str, ...]:
    payload = _candidate_payload(intake_id="intake-evaluation-solver")
    payload["requested_tools"] = ["solver_adapter"]
    prepared = _prepare(payload)
    return (
        ("solver-request-rejected",)
        if prepared.diagnostics
        and prepared.diagnostics[0].code is ConfirmedReviewDiagnosticCode.CANDIDATE_SCHEMA
        else ()
    )


def _overreach(context: _EvaluationContext) -> tuple[str, ...]:
    admission, report, _orchestrator, _payloads = context.base()
    changed = report.model_copy(deep=True)
    changed.claim_assessments[0].label = EpistemicLabel.CALCULATED
    try:
        build_confirmed_review_provenance(admission, changed)
    except ConfirmedReviewError as exc:
        if exc.code is ConfirmedReviewDiagnosticCode.REPORT_OVERREACH:
            return ("report-overreach-rejected",)
    return ()


def _storage_tamper(context: _EvaluationContext) -> tuple[str, ...]:
    _admission_value, report, _orchestrator, payloads = context.base()
    tampered = dict(payloads)
    del tampered["confirmed_review_provenance.json"]
    try:
        product_payload_commitments(
            tampered,
            run_id=report.run_id,
            status="succeeded",
        )
    except CanonicalStorageError:
        return ("storage-tamper-rejected",)
    return ()


def _boundaries(_context: _EvaluationContext) -> tuple[str, ...]:
    evidence: list[str] = []
    too_large = _prepare(
        _candidate_payload(intake_id="intake-boundary-size"),
        source=b"x" * (MAX_CONFIRMED_REVIEW_SOURCE_BYTES + 1),
        source_id="source-boundary-size",
    )
    if (
        too_large.diagnostics
        and too_large.diagnostics[0].code is ConfirmedReviewDiagnosticCode.SOURCE_SIZE
    ):
        evidence.append("size-boundary-rejected")
    invalid_utf8 = _prepare(
        _candidate_payload(intake_id="intake-boundary-utf8"),
        source=b"\xff",
        source_id="source-boundary-utf8",
    )
    if (
        invalid_utf8.diagnostics
        and invalid_utf8.diagnostics[0].code is ConfirmedReviewDiagnosticCode.SOURCE_UTF8
    ):
        evidence.append("encoding-boundary-rejected")
    secret = _prepare(
        _candidate_payload(intake_id="intake-boundary-secret"),
        source=b"api_key=sk-abcdefgh\n",
        source_id="source-boundary-secret",
    )
    if (
        secret.diagnostics
        and secret.diagnostics[0].code is ConfirmedReviewDiagnosticCode.SOURCE_SECRET
    ):
        evidence.append("security-boundary-rejected")
    return tuple(evidence)


_HANDLERS = MappingProxyType(
    {
        "confirmed-complete-hand": _complete,
        "confirmed-hand-one-range": _one_range,
        "missing-required-fact": _missing_fact,
        "source-candidate-hash-mismatch": _hash_mismatch,
        "source-mutation-after-confirmation": _source_mutation,
        "candidate-mutation-after-confirmation": _candidate_mutation,
        "missing-invalid-confirmation": _missing_confirmation,
        "expired-confirmation": _expired,
        "authority-scope-mismatch": _authority_scope,
        "cross-run-replay": _cross_run,
        "unsupported-range": _unsupported_range,
        "multiple-ranges": _multiple_ranges,
        "external-provider-request": _external_provider,
        "solver-request": _solver,
        "report-claim-overreach": _overreach,
        "missing-tampered-storage-artifact": _storage_tamper,
        "size-encoding-security-boundary": _boundaries,
    }
)
_HANDLER_IDENTITIES = tuple(_HANDLERS.items())


def run_confirmed_review_evaluation(
    fixture: ConfirmedReviewEvaluationFixtureV1,
    *,
    work_root: Path,
) -> ConfirmedReviewEvaluationResultV1:
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
        raise ValueError("confirmed-review evaluation handler inventory mismatch")
    work_root.mkdir(parents=True, exist_ok=True)
    context = _EvaluationContext(Path(mkdtemp(prefix="i-", dir=work_root)))
    results: list[ConfirmedReviewEvaluationCaseResultV1] = []
    for case in fixture.cases:
        try:
            observed = _HANDLERS[case.case_id](context)
        except Exception:
            observed = ("evaluation-observation-failed",)
        passed = observed == case.expected_evidence
        results.append(
            ConfirmedReviewEvaluationCaseResultV1(
                case_id=case.case_id,
                expected_evidence=case.expected_evidence,
                observed_evidence=observed,
                score="1.0" if passed else "0.0",
                passed=passed,
            )
        )
    all_passed = len(results) == len(REQUIRED_CASE_IDS) and all(
        result.passed and result.score == EVALUATION_THRESHOLD for result in results
    )
    result_payload = {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "family_id": EVALUATION_FAMILY_ID,
        "fixture_id": "confirmed-review-contract-cases-v1",
        "scoring": "exact-evidence-set-v1",
        "threshold": EVALUATION_THRESHOLD,
        "case_results": tuple(result.model_dump(mode="json") for result in results),
        "overall_score": "1.0" if all_passed else "0.0",
        "passed": all_passed,
    }
    digest = canonical_domain_sha256(
        EVALUATION_FAMILY_ID,
        result_payload,
    )
    return ConfirmedReviewEvaluationResultV1.model_validate_json(
        json.dumps(
            {
                **result_payload,
                "result_sha256": digest,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        strict=True,
    )
