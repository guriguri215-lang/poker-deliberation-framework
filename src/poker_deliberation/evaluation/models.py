"""Strict versioned contracts for deterministic offline evaluation."""

from __future__ import annotations

import re
import unicodedata
from fractions import Fraction
from typing import Annotated, Final, Literal, TypeAlias

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

EVALUATION_SCHEMA_VERSION: Final[Literal["1.0.0"]] = "1.0.0"
EVALUATION_CANONICALIZATION: Final[Literal["poker-offline-evaluation-json-v1"]] = (
    "poker-offline-evaluation-json-v1"
)
EVALUATION_HASH_ALGORITHM: Final[Literal["sha256"]] = "sha256"

_PORTABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OBJECT_ID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SOURCE_PATH = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$")
_EVIDENCE_TOKEN = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
_UTC_SECOND = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_DECIMAL_SCORE = re.compile(r"^(?:0(?:\.[0-9]+)?|1(?:\.0+)?)$")
_SECRET_VALUE = re.compile(
    r"(?:\bsk-[A-Za-z0-9_-]{8,}\b|\bgh[pousr]_[A-Za-z0-9]{20,}\b|"
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b|"
    r"(?:api[_-]?key|password|passwd|secret|token)\s*[:=]\s*[^\s,;]+)",
    re.IGNORECASE,
)


def _safe_text(value: str) -> str:
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError("evaluation text must be NFC")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ValueError("evaluation text cannot contain control characters")
    if _SECRET_VALUE.search(value):
        raise ValueError("evaluation text must not contain a secret shape")
    return value


def ratio_decimal(numerator: int, denominator: int) -> str:
    """Render a bounded ratio without binary floating-point."""

    if denominator <= 0 or numerator < 0 or numerator > denominator:
        raise ValueError("score ratio is outside [0, 1]")
    if numerator == denominator:
        return "1.0"
    if numerator == 0:
        return "0.0"
    value = Fraction(numerator, denominator)
    digits: list[str] = []
    remainder = value.numerator
    for _ in range(12):
        remainder *= 10
        digit, remainder = divmod(remainder, value.denominator)
        digits.append(str(digit))
        if remainder == 0:
            break
    return f"0.{''.join(digits).rstrip('0') or '0'}"


PortableId = Annotated[str, Field(pattern=_PORTABLE_ID.pattern), AfterValidator(_safe_text)]
Version = Annotated[str, Field(pattern=_VERSION.pattern), AfterValidator(_safe_text)]
Sha256 = Annotated[str, Field(pattern=_SHA256.pattern)]
GitObjectId = Annotated[str, Field(pattern=_GIT_OBJECT_ID.pattern)]
SourcePath = Annotated[str, Field(pattern=_SOURCE_PATH.pattern), AfterValidator(_safe_text)]
EvidenceToken = Annotated[
    str,
    Field(pattern=_EVIDENCE_TOKEN.pattern),
    AfterValidator(_safe_text),
]
BoundedText = Annotated[
    str,
    Field(min_length=1, max_length=1024),
    AfterValidator(_safe_text),
]
UtcSecond = Annotated[str, Field(pattern=_UTC_SECOND.pattern)]
ScoreDecimal = Annotated[str, Field(pattern=_DECIMAL_SCORE.pattern)]

CaseKind: TypeAlias = Literal[
    "normal",
    "context-provenance-mismatch",
    "role-allowlist-mismatch",
    "calculator-oracle-mismatch",
    "missing-denominator",
    "missing-scorer",
    "missing-version",
    "unsupported-solver-claim",
    "synthetic-secret-metadata",
    "structured-timeout",
]
CaseMutation: TypeAlias = Literal[
    "none",
    "change-context-source",
    "expand-tool-allowlist",
    "change-oracle",
    "remove-denominator-policy",
    "remove-scorer-path",
    "remove-schema-version",
    "claim-equilibrium-without-evidence",
    "insert-synthetic-secret-shape",
    "exceed-declared-timeout",
]


class _EvaluationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
        hide_input_in_errors=True,
    )


def _utf8_sorted_unique(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) != len(set(values)):
        raise ValueError(f"{field_name} must be unique")
    if values != tuple(sorted(values, key=lambda item: item.encode("utf-8"))):
        raise ValueError(f"{field_name} must be UTF-8 sorted")
    return values


class EvaluationCaseInputV1(_EvaluationModel):
    scenario: CaseKind
    mutation: CaseMutation
    tool_name: Literal["pot_odds", "solver_status"] | None = None
    pot_before_bet: int | None = Field(default=None, ge=0, le=10**12)
    opponent_bet: int | None = Field(default=None, ge=0, le=10**12)
    call_cost: int | None = Field(default=None, ge=1, le=10**12)
    expected_rake: int | None = Field(default=None, ge=0, le=10**12)
    oracle_numerator: int | None = Field(default=None, ge=0, le=10**12)
    oracle_denominator: int | None = Field(default=None, ge=1, le=10**12)
    timeout_ms: int | None = Field(default=None, ge=1, le=60_000)
    simulated_elapsed_ms: int | None = Field(default=None, ge=0, le=60_001)

    @model_validator(mode="after")
    def scenario_has_exact_input_shape(self) -> EvaluationCaseInputV1:
        calculator_fields = (
            self.pot_before_bet,
            self.opponent_bet,
            self.call_cost,
            self.expected_rake,
            self.oracle_numerator,
            self.oracle_denominator,
        )
        calculator_case = self.scenario in {"normal", "calculator-oracle-mismatch"}
        if calculator_case:
            if self.tool_name != "pot_odds" or any(value is None for value in calculator_fields):
                raise ValueError("calculator case requires complete pot-odds input and oracle")
        elif any(value is not None for value in calculator_fields):
            raise ValueError("non-calculator case cannot carry pot-odds input or oracle")
        if self.scenario == "unsupported-solver-claim":
            if self.tool_name != "solver_status":
                raise ValueError("solver claim case requires solver_status")
        elif not calculator_case and self.tool_name is not None:
            raise ValueError("case has an unexpected tool")
        timeout_fields = (self.timeout_ms, self.simulated_elapsed_ms)
        if self.scenario == "structured-timeout":
            if any(value is None for value in timeout_fields):
                raise ValueError("timeout case requires timeout and elapsed values")
            if self.simulated_elapsed_ms <= self.timeout_ms:  # type: ignore[operator]
                raise ValueError("timeout fixture must exceed its declared bound")
        elif any(value is not None for value in timeout_fields):
            raise ValueError("non-timeout case cannot carry timeout values")
        return self


class ExpectedEvidenceV1(_EvaluationModel):
    tokens: tuple[EvidenceToken, ...] = Field(min_length=1, max_length=64)

    @field_validator("tokens")
    @classmethod
    def canonical_tokens(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _utf8_sorted_unique(value, "expected evidence tokens")


class EvaluationCaseV1(_EvaluationModel):
    case_id: PortableId
    case_kind: CaseKind
    input: EvaluationCaseInputV1
    expected_evidence: ExpectedEvidenceV1

    @model_validator(mode="after")
    def kind_matches_input(self) -> EvaluationCaseV1:
        if self.case_kind != self.input.scenario:
            raise ValueError("case kind/input scenario mismatch")
        return self


class EvaluationDatasetV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    canonicalization: Literal["poker-offline-evaluation-json-v1"] = EVALUATION_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = EVALUATION_HASH_ALGORITHM
    dataset_id: PortableId
    dataset_version: Version
    cases: tuple[EvaluationCaseV1, ...] = Field(min_length=1, max_length=10_000)

    @field_validator("cases")
    @classmethod
    def canonical_cases(cls, value: tuple[EvaluationCaseV1, ...]) -> tuple[EvaluationCaseV1, ...]:
        _utf8_sorted_unique(
            tuple(item.case_id for item in value),
            "dataset case IDs",
        )
        return value


class DatasetManifestV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    canonicalization: Literal["poker-offline-evaluation-json-v1"] = EVALUATION_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = EVALUATION_HASH_ALGORITHM
    dataset_id: PortableId
    dataset_version: Version
    ownership: Literal["repository-owned"]
    license_spdx: Literal["MIT"]
    license_path: SourcePath
    license_sha256: Sha256
    cases_path: SourcePath
    case_count: int = Field(ge=1, le=10_000)
    content_sha256: Sha256


class ScorerConfigV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    canonicalization: Literal["poker-offline-evaluation-json-v1"] = EVALUATION_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = EVALUATION_HASH_ALGORITHM
    scorer_id: Literal["exact-evidence-match"]
    scorer_version: Version
    metric_id: Literal["reproducibility"]
    direction: Literal["higher-is-better"]
    aggregation: Literal["micro-mean"]
    denominator_policy: Literal["all-declared-cases"]
    invalid_or_missing_count_policy: Literal["fail-closed"]
    threshold: ScoreDecimal
    human_review_rubric: None = None


class EvaluationSuiteV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    canonicalization: Literal["poker-offline-evaluation-json-v1"] = EVALUATION_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = EVALUATION_HASH_ALGORITHM
    suite_id: PortableId
    suite_version: Version
    dataset_manifest_path: SourcePath
    dataset_manifest_sha256: Sha256
    scorer_path: SourcePath
    scorer_sha256: Sha256
    evaluation_time_utc: UtcSecond
    network_access: Literal[False] = False
    provider_execution: Literal[False] = False
    solver_execution: Literal[False] = False
    runtime_bridge: Literal[False] = False


class StructuredFailureV1(_EvaluationModel):
    code: PortableId
    category: Literal[
        "configuration",
        "integrity",
        "security",
        "timeout",
        "tool",
        "unsupported",
        "validation",
    ]
    path: SourcePath
    retryable: bool
    message: BoundedText


class ToolEvidenceV1(_EvaluationModel):
    tool_name: PortableId
    contract_version: Version
    status: Literal["success", "failed", "unavailable"]
    exactness: Literal[
        "exact",
        "exact-under-model",
        "approximate",
        "unavailable",
    ]
    numeric_exactness: Literal[
        "exact",
        "exact-under-model",
        "floating-verified",
        "approximate",
        "unavailable",
    ]
    input_sha256: Sha256
    output_sha256: Sha256
    verification_passed: bool
    reproduce_command: BoundedText


class CaseOutcomeV1(_EvaluationModel):
    case_id: PortableId
    case_kind: CaseKind
    observed_status: Literal["rejected", "succeeded", "timed-out"]
    expected_evidence: tuple[EvidenceToken, ...] = Field(min_length=1, max_length=64)
    actual_evidence: tuple[EvidenceToken, ...] = Field(min_length=1, max_length=64)
    exact_match: bool
    matched_case_count: Literal[0, 1]
    failure: StructuredFailureV1 | None
    tool_evidence: ToolEvidenceV1 | None

    @field_validator("expected_evidence", "actual_evidence")
    @classmethod
    def canonical_evidence(cls, value: tuple[str, ...], info: object) -> tuple[str, ...]:
        return _utf8_sorted_unique(
            value,
            getattr(info, "field_name", "case evidence"),
        )

    @model_validator(mode="after")
    def outcome_is_internally_consistent(self) -> CaseOutcomeV1:
        exact = self.expected_evidence == self.actual_evidence
        if self.exact_match != exact or self.matched_case_count != int(exact):
            raise ValueError("case score differs from exact evidence equality")
        if (self.observed_status == "succeeded") != (self.failure is None):
            raise ValueError("non-success case requires exactly one structured failure")
        return self


class EvaluationSourceBindingV1(_EvaluationModel):
    source_commit_id: GitObjectId
    source_tree_id: GitObjectId
    config_sha256: Sha256
    suite_sha256: Sha256
    dataset_manifest_sha256: Sha256
    dataset_content_sha256: Sha256
    scorer_sha256: Sha256
    tool_contract_sha256: Sha256
    codex_runtime_inventory_sha256: Sha256
    python_runtime_inventory_sha256: Sha256
    tool_contract_versions: tuple[tuple[PortableId, Version], ...]

    @field_validator("tool_contract_versions")
    @classmethod
    def canonical_tool_versions(
        cls, value: tuple[tuple[str, str], ...]
    ) -> tuple[tuple[str, str], ...]:
        _utf8_sorted_unique(
            tuple(item[0] for item in value),
            "tool contract names",
        )
        return value


class EvaluationSummaryV1(_EvaluationModel):
    declared_case_count: int = Field(ge=1, le=10_000)
    observed_case_count: int = Field(ge=0, le=10_000)
    matched_case_count: int = Field(ge=0, le=10_000)
    mismatched_case_count: int = Field(ge=0, le=10_000)
    numerator: int = Field(ge=0, le=10_000)
    denominator: int = Field(ge=1, le=10_000)
    score: ScoreDecimal
    threshold: ScoreDecimal
    decision: Literal["fail", "pass"]

    @model_validator(mode="after")
    def counts_and_decision_are_exact(self) -> EvaluationSummaryV1:
        if self.observed_case_count != self.declared_case_count:
            raise ValueError("observed count differs from declared case count")
        if self.denominator != self.declared_case_count:
            raise ValueError("denominator must contain all declared cases")
        if self.matched_case_count + self.mismatched_case_count != self.observed_case_count:
            raise ValueError("matched and mismatched counts do not cover observed cases")
        if self.numerator != self.matched_case_count:
            raise ValueError("micro-mean numerator must equal matched case count")
        if self.score != ratio_decimal(self.numerator, self.denominator):
            raise ValueError("score differs from exact count ratio")
        score_fraction = Fraction(self.numerator, self.denominator)
        threshold_fraction = Fraction(self.threshold)
        expected_decision = "pass" if score_fraction >= threshold_fraction else "fail"
        if self.decision != expected_decision:
            raise ValueError("decision differs from exact threshold comparison")
        return self


class EvaluationResultV1(_EvaluationModel):
    schema_version: Literal["1.0.0"] = EVALUATION_SCHEMA_VERSION
    canonicalization: Literal["poker-offline-evaluation-json-v1"] = EVALUATION_CANONICALIZATION
    hash_algorithm: Literal["sha256"] = EVALUATION_HASH_ALGORITHM
    suite_id: PortableId
    suite_version: Version
    dataset_id: PortableId
    dataset_version: Version
    scorer_id: PortableId
    scorer_version: Version
    aggregation: Literal["micro-mean"]
    denominator_policy: Literal["all-declared-cases"]
    source: EvaluationSourceBindingV1
    outcomes: tuple[CaseOutcomeV1, ...] = Field(min_length=1, max_length=10_000)
    summary: EvaluationSummaryV1

    @field_validator("outcomes")
    @classmethod
    def canonical_outcomes(cls, value: tuple[CaseOutcomeV1, ...]) -> tuple[CaseOutcomeV1, ...]:
        _utf8_sorted_unique(
            tuple(item.case_id for item in value),
            "result case IDs",
        )
        return value

    @model_validator(mode="after")
    def result_matches_outcomes(self) -> EvaluationResultV1:
        if len(self.outcomes) != self.summary.observed_case_count:
            raise ValueError("result outcomes do not match observed count")
        matched = sum(item.matched_case_count for item in self.outcomes)
        if matched != self.summary.matched_case_count:
            raise ValueError("result outcomes do not match summary numerator")
        return self


class EvaluationMetadataProbeV1(_EvaluationModel):
    label: BoundedText


__all__ = [
    "EVALUATION_CANONICALIZATION",
    "EVALUATION_HASH_ALGORITHM",
    "EVALUATION_SCHEMA_VERSION",
    "DatasetManifestV1",
    "EvaluationCaseInputV1",
    "EvaluationCaseV1",
    "EvaluationDatasetV1",
    "EvaluationMetadataProbeV1",
    "EvaluationResultV1",
    "EvaluationSourceBindingV1",
    "EvaluationSuiteV1",
    "EvaluationSummaryV1",
    "ExpectedEvidenceV1",
    "ScorerConfigV1",
    "StructuredFailureV1",
    "ToolEvidenceV1",
    "ratio_decimal",
]
