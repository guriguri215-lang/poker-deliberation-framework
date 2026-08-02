"""Strict contracts for the opt-in versioned river range-equity bridge."""

from __future__ import annotations

import hashlib
import json
import math
from enum import StrEnum
from fractions import Fraction
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from poker_deliberation.range_models import RangeGameConditionsV1, RangeSourceProvenanceV1
from poker_deliberation.tools.numeric import close_ulps

RANGE_EQUITY_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
RANGE_EQUITY_CONTRACT_ID: Literal["poker-versioned-range-river-equity"] = (
    "poker-versioned-range-river-equity"
)
RANGE_EQUITY_CONTRACT_VERSION: Literal["1.0.0"] = "1.0.0"
RANGE_EQUITY_HASH_ALGORITHM: Literal["sha256"] = "sha256"
RANGE_EQUITY_MARKER = "versioned_range_river_equity"
RANGE_EQUITY_BINDING_ARTIFACT = "range_equity_binding.json"
RANGE_EQUITY_BINDING_ARTIFACT_SCHEMA = "poker-versioned-range-river-equity-binding-artifact-v1"
RANGE_EQUITY_ADMISSION_RECORD_SCHEMA: Literal[
    "poker-versioned-range-river-equity-admission-record-v1"
] = "poker-versioned-range-river-equity-admission-record-v1"
RANGE_EQUITY_MAX_EVALUATIONS: Literal[990] = 990
RANGE_EQUITY_TOOL_PLAN: tuple[
    Literal["range_validate"],
    Literal["combos"],
    Literal["holdem_equity"],
] = ("range_validate", "combos", "holdem_equity")

SOURCE_RANGE_HASH_DOMAIN = "poker-versioned-range-river-equity-source-range-v1"
VALIDATION_INPUT_HASH_DOMAIN = "poker-versioned-range-river-equity-validation-input-v1"
VALIDATION_OUTPUT_HASH_DOMAIN = "poker-versioned-range-river-equity-validation-output-v1"
COMBOS_INPUT_HASH_DOMAIN = "poker-versioned-range-river-equity-combos-input-v1"
COMBOS_OUTPUT_HASH_DOMAIN = "poker-versioned-range-river-equity-combos-output-v1"
EQUITY_INPUT_HASH_DOMAIN = "poker-versioned-range-river-equity-equity-input-v1"
EQUITY_OUTPUT_HASH_DOMAIN = "poker-versioned-range-river-equity-equity-output-v1"
ORACLE_HASH_DOMAIN = "poker-versioned-range-river-equity-oracle-v1"
BINDING_HASH_DOMAIN = "poker-versioned-range-river-equity-binding-v1"
RESULT_HASH_DOMAIN = "poker-versioned-range-river-equity-result-v1"
CANDIDATE_HASH_DOMAIN = "poker-versioned-range-river-equity-candidate-v1"
ADMISSION_RECORD_HASH_DOMAIN = "poker-versioned-range-river-equity-admission-record-v1"

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PORTABLE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
FiniteEquity = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]


def canonical_domain_sha256(domain: str, value: Any) -> str:
    """Hash canonical JSON under an ASCII domain separator."""

    prefix = domain.encode("ascii")
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(prefix + b"\0" + payload).hexdigest()


class _RangeEquityModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class RangeEquityDiagnosticCode(StrEnum):
    SCHEMA = "REQ_E_SCHEMA"
    CASE = "REQ_E_CASE"
    HAND = "REQ_E_HAND"
    RANGE = "REQ_E_RANGE"
    TARGET = "REQ_E_TARGET"
    DECISION = "REQ_E_DECISION"
    CARD = "REQ_E_CARD"
    TOOL_PLAN = "REQ_E_TOOL_PLAN"
    PROVENANCE = "REQ_E_PROVENANCE"
    LIMIT = "REQ_E_LIMIT"
    CHAIN = "REQ_E_CHAIN"
    ORACLE = "REQ_E_ORACLE"
    REPLAY = "REQ_E_REPLAY"


class VersionedRangeRiverEquityBindingV1(_RangeEquityModel):
    schema_version: Literal["1.0.0"] = RANGE_EQUITY_SCHEMA_VERSION
    contract_id: Literal["poker-versioned-range-river-equity"] = RANGE_EQUITY_CONTRACT_ID
    contract_version: Literal["1.0.0"] = RANGE_EQUITY_CONTRACT_VERSION
    hash_algorithm: Literal["sha256"] = RANGE_EQUITY_HASH_ALGORITHM
    range_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    target_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    hero_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    as_of_action_index: int = Field(ge=1, le=512)
    source_range_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    condition_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    canonical_combo_sha256: str = Field(pattern=_SHA256_PATTERN)
    oracle_sha256: str = Field(pattern=_SHA256_PATTERN)
    combo_count: int = Field(ge=1, le=RANGE_EQUITY_MAX_EVALUATIONS)
    total_weight_millionths: int = Field(ge=1, le=1_326_000_000)
    exact_evaluation_cap: Literal[990] = RANGE_EQUITY_MAX_EVALUATIONS
    tool_plan: tuple[
        Literal["range_validate"],
        Literal["combos"],
        Literal["holdem_equity"],
    ] = RANGE_EQUITY_TOOL_PLAN
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def binding_hash_matches_payload(self) -> VersionedRangeRiverEquityBindingV1:
        if not (self.combo_count <= self.total_weight_millionths <= self.combo_count * 1_000_000):
            raise ValueError(
                f"{RangeEquityDiagnosticCode.RANGE.value}: "
                "combo count and total weight are inconsistent"
            )
        payload = self.model_dump(mode="json")
        payload.pop("binding_sha256")
        expected = canonical_domain_sha256(BINDING_HASH_DOMAIN, payload)
        if self.binding_sha256 != expected:
            raise ValueError(f"{RangeEquityDiagnosticCode.PROVENANCE.value}: binding hash mismatch")
        return self


class VersionedRangeRiverEquityOracleProjectionV1(_RangeEquityModel):
    """Exact oracle data exposed additively for a previously admitted bridge case."""

    schema_version: Literal["1.0.0"] = RANGE_EQUITY_SCHEMA_VERSION
    contract_id: Literal["poker-versioned-range-river-equity"] = RANGE_EQUITY_CONTRACT_ID
    contract_version: Literal["1.0.0"] = RANGE_EQUITY_CONTRACT_VERSION
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    oracle_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    target_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    hero_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    combo_count: int = Field(ge=1, le=RANGE_EQUITY_MAX_EVALUATIONS)
    total_weight_millionths: int = Field(ge=1, le=1_326_000_000)
    win_combo_count: int = Field(ge=0, le=RANGE_EQUITY_MAX_EVALUATIONS)
    tie_combo_count: int = Field(ge=0, le=RANGE_EQUITY_MAX_EVALUATIONS)
    loss_combo_count: int = Field(ge=0, le=RANGE_EQUITY_MAX_EVALUATIONS)
    win_weight_millionths: int = Field(ge=0, le=1_326_000_000)
    tie_weight_millionths: int = Field(ge=0, le=1_326_000_000)
    loss_weight_millionths: int = Field(ge=0, le=1_326_000_000)
    equity_numerator: int = Field(ge=0)
    equity_denominator: int = Field(gt=0)

    @model_validator(mode="after")
    def totals_and_fraction_match(self) -> VersionedRangeRiverEquityOracleProjectionV1:
        if (
            self.win_combo_count + self.tie_combo_count + self.loss_combo_count != self.combo_count
            or self.win_weight_millionths + self.tie_weight_millionths + self.loss_weight_millionths
            != self.total_weight_millionths
        ):
            raise ValueError(f"{RangeEquityDiagnosticCode.ORACLE.value}: oracle totals mismatch")
        expected = Fraction(
            2 * self.win_weight_millionths + self.tie_weight_millionths,
            2 * self.total_weight_millionths,
        )
        if (self.equity_numerator, self.equity_denominator) != (
            expected.numerator,
            expected.denominator,
        ):
            raise ValueError(f"{RangeEquityDiagnosticCode.ORACLE.value}: equity fraction mismatch")
        return self


class VersionedRangeRiverEquityAdmissionRecordV1(_RangeEquityModel):
    """Minimal append-only commitment written before bridge tool execution."""

    schema_version: Literal["1.0.0"] = RANGE_EQUITY_SCHEMA_VERSION
    record_schema: Literal["poker-versioned-range-river-equity-admission-record-v1"] = (
        RANGE_EQUITY_ADMISSION_RECORD_SCHEMA
    )
    run_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    candidate_sha256: str = Field(pattern=_SHA256_PATTERN)
    tool_plan: tuple[
        Literal["range_validate"],
        Literal["combos"],
        Literal["holdem_equity"],
    ] = RANGE_EQUITY_TOOL_PLAN
    record_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def record_hash_matches_payload(self) -> VersionedRangeRiverEquityAdmissionRecordV1:
        payload = self.model_dump(mode="json")
        payload.pop("record_sha256")
        expected = canonical_domain_sha256(ADMISSION_RECORD_HASH_DOMAIN, payload)
        if self.record_sha256 != expected:
            raise ValueError(
                f"{RangeEquityDiagnosticCode.PROVENANCE.value}: admission record hash mismatch"
            )
        return self


class VersionedRangeRiverEquityResultV1(_RangeEquityModel):
    schema_version: Literal["1.0.0"] = RANGE_EQUITY_SCHEMA_VERSION
    contract_id: Literal["poker-versioned-range-river-equity"] = RANGE_EQUITY_CONTRACT_ID
    contract_version: Literal["1.0.0"] = RANGE_EQUITY_CONTRACT_VERSION
    hash_algorithm: Literal["sha256"] = RANGE_EQUITY_HASH_ALGORITHM
    binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    range_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    target_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    hero_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source: RangeSourceProvenanceV1
    game_conditions: RangeGameConditionsV1
    condition_binding_sha256: str = Field(pattern=_SHA256_PATTERN)
    canonical_combo_sha256: str = Field(pattern=_SHA256_PATTERN)
    hero_cards: tuple[str, str]
    board: tuple[str, str, str, str, str]
    combo_count: int = Field(ge=1, le=RANGE_EQUITY_MAX_EVALUATIONS)
    total_weight_millionths: int = Field(ge=1, le=1_326_000_000)
    win_combo_count: int = Field(ge=0, le=RANGE_EQUITY_MAX_EVALUATIONS)
    tie_combo_count: int = Field(ge=0, le=RANGE_EQUITY_MAX_EVALUATIONS)
    loss_combo_count: int = Field(ge=0, le=RANGE_EQUITY_MAX_EVALUATIONS)
    win_weight_millionths: int = Field(ge=0, le=1_326_000_000)
    tie_weight_millionths: int = Field(ge=0, le=1_326_000_000)
    loss_weight_millionths: int = Field(ge=0, le=1_326_000_000)
    equity_numerator: int = Field(ge=0)
    equity_denominator: int = Field(gt=0)
    legacy_hero_equity: FiniteEquity
    oracle_numeric_exactness: Literal["exact"] = "exact"
    legacy_numeric_exactness: Literal["floating-verified"] = "floating-verified"
    validation_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    validation_output_sha256: str = Field(pattern=_SHA256_PATTERN)
    combos_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    combos_output_sha256: str = Field(pattern=_SHA256_PATTERN)
    equity_input_sha256: str = Field(pattern=_SHA256_PATTERN)
    equity_output_sha256: str = Field(pattern=_SHA256_PATTERN)
    oracle_sha256: str = Field(pattern=_SHA256_PATTERN)
    result_sha256: str = Field(pattern=_SHA256_PATTERN)

    @field_validator("hero_cards", "board")
    @classmethod
    def canonical_cards(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            len(card) != 2 or card[0] not in "23456789TJQKA" or card[1] not in "cdhs"
            for card in value
        ):
            raise ValueError(f"{RangeEquityDiagnosticCode.CARD.value}: non-canonical cards")
        return value

    @model_validator(mode="after")
    def exact_totals_and_hashes_match(self) -> VersionedRangeRiverEquityResultV1:
        if set(self.hero_cards).intersection(self.board):
            raise ValueError(f"{RangeEquityDiagnosticCode.CARD.value}: overlapping known cards")
        if self.win_combo_count + self.tie_combo_count + self.loss_combo_count != self.combo_count:
            raise ValueError(f"{RangeEquityDiagnosticCode.ORACLE.value}: combo totals mismatch")
        if (
            self.win_weight_millionths + self.tie_weight_millionths + self.loss_weight_millionths
            != self.total_weight_millionths
        ):
            raise ValueError(f"{RangeEquityDiagnosticCode.ORACLE.value}: weight totals mismatch")
        if not (self.combo_count <= self.total_weight_millionths <= self.combo_count * 1_000_000):
            raise ValueError(
                f"{RangeEquityDiagnosticCode.ORACLE.value}: "
                "combo count and total weight are inconsistent"
            )
        for label, count, weight in (
            ("win", self.win_combo_count, self.win_weight_millionths),
            ("tie", self.tie_combo_count, self.tie_weight_millionths),
            ("loss", self.loss_combo_count, self.loss_weight_millionths),
        ):
            if (count == 0) != (weight == 0) or (
                count > 0 and not count <= weight <= count * 1_000_000
            ):
                raise ValueError(
                    f"{RangeEquityDiagnosticCode.ORACLE.value}: "
                    f"{label} count and weight are inconsistent"
                )
        exact = Fraction(
            2 * self.win_weight_millionths + self.tie_weight_millionths,
            2 * self.total_weight_millionths,
        )
        if (
            self.equity_numerator != exact.numerator
            or self.equity_denominator != exact.denominator
            or not math.isfinite(self.legacy_hero_equity)
            or not close_ulps(self.legacy_hero_equity, float(exact), ulps=128)
        ):
            raise ValueError(f"{RangeEquityDiagnosticCode.ORACLE.value}: equity fraction mismatch")
        oracle_payload = {
            "range_id": self.range_id,
            "target_player_id": self.target_player_id,
            "hero_player_id": self.hero_player_id,
            "condition_binding_sha256": self.condition_binding_sha256,
            "hero_cards": self.hero_cards,
            "board": self.board,
            "canonical_combo_sha256": self.canonical_combo_sha256,
            "combo_count": self.combo_count,
            "total_weight_millionths": self.total_weight_millionths,
            "win_combo_count": self.win_combo_count,
            "tie_combo_count": self.tie_combo_count,
            "loss_combo_count": self.loss_combo_count,
            "win_weight_millionths": self.win_weight_millionths,
            "tie_weight_millionths": self.tie_weight_millionths,
            "loss_weight_millionths": self.loss_weight_millionths,
            "equity_numerator": self.equity_numerator,
            "equity_denominator": self.equity_denominator,
        }
        if self.oracle_sha256 != canonical_domain_sha256(ORACLE_HASH_DOMAIN, oracle_payload):
            raise ValueError(f"{RangeEquityDiagnosticCode.ORACLE.value}: oracle hash mismatch")
        payload = self.model_dump(mode="json")
        payload.pop("result_sha256")
        if self.result_sha256 != canonical_domain_sha256(RESULT_HASH_DOMAIN, payload):
            raise ValueError(f"{RangeEquityDiagnosticCode.REPLAY.value}: result hash mismatch")
        return self


__all__ = [
    "ADMISSION_RECORD_HASH_DOMAIN",
    "BINDING_HASH_DOMAIN",
    "CANDIDATE_HASH_DOMAIN",
    "COMBOS_INPUT_HASH_DOMAIN",
    "COMBOS_OUTPUT_HASH_DOMAIN",
    "EQUITY_INPUT_HASH_DOMAIN",
    "EQUITY_OUTPUT_HASH_DOMAIN",
    "ORACLE_HASH_DOMAIN",
    "RANGE_EQUITY_ADMISSION_RECORD_SCHEMA",
    "RANGE_EQUITY_BINDING_ARTIFACT",
    "RANGE_EQUITY_BINDING_ARTIFACT_SCHEMA",
    "RANGE_EQUITY_CONTRACT_ID",
    "RANGE_EQUITY_CONTRACT_VERSION",
    "RANGE_EQUITY_MARKER",
    "RANGE_EQUITY_MAX_EVALUATIONS",
    "RANGE_EQUITY_SCHEMA_VERSION",
    "RANGE_EQUITY_TOOL_PLAN",
    "RESULT_HASH_DOMAIN",
    "SOURCE_RANGE_HASH_DOMAIN",
    "VALIDATION_INPUT_HASH_DOMAIN",
    "VALIDATION_OUTPUT_HASH_DOMAIN",
    "RangeEquityDiagnosticCode",
    "VersionedRangeRiverEquityAdmissionRecordV1",
    "VersionedRangeRiverEquityBindingV1",
    "VersionedRangeRiverEquityOracleProjectionV1",
    "VersionedRangeRiverEquityResultV1",
    "canonical_domain_sha256",
]
