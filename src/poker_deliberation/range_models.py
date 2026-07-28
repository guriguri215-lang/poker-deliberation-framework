"""Strict versioned models for bounded NLHE range grammar and provenance."""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RANGE_SCHEMA_VERSION = "1.0.0"
RANGE_RESULT_VERSION = "1.0.0"
RANGE_GRAMMAR_ID = "poker-deliberation.nlhe-range"
RANGE_GRAMMAR_VERSION = "1.0.0"
RANGE_HASH_ALGORITHM: Literal["sha256"] = "sha256"

MAX_RANGE_NOTATION_BYTES = 16_384
MAX_RANGE_TOKENS = 1_326
MAX_EXPANDED_COMBOS = 1_326
MAX_RANGE_DIAGNOSTICS = 64
MAX_RANGE_ACTION_PREFIX = 512
WEIGHT_SCALE = 1_000_000

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_PORTABLE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_POSITION_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_+-]{0,31}$"
_CARD_PATTERN = r"^[2-9TJQKA][cdhs]$"
_RANKS = "23456789TJQKA"
_SUITS = "cdhs"

RangeDiagnosticField = Literal[
    "envelope",
    "notation",
    "weight",
    "card",
    "blockers",
    "provenance",
    "license",
    "target_player",
    "game_conditions",
]


class _RangeModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class RangeDiagnosticCode(StrEnum):
    UNSUPPORTED_VERSION = "RNG_E_UNSUPPORTED_VERSION"
    LIMIT = "RNG_E_LIMIT"
    NON_ASCII = "RNG_E_NON_ASCII"
    SYNTAX = "RNG_E_SYNTAX"
    CARD = "RNG_E_CARD"
    CLASS_ORDER = "RNG_E_CLASS_ORDER"
    WEIGHT_LEXEME = "RNG_E_WEIGHT_LEXEME"
    WEIGHT_RANGE = "RNG_E_WEIGHT_RANGE"
    OVERLAP = "RNG_E_OVERLAP"
    BLOCKER = "RNG_E_BLOCKER"
    EMPTY = "RNG_E_EMPTY"
    PROVENANCE = "RNG_E_PROVENANCE"
    LICENSE = "RNG_E_LICENSE"
    TARGET = "RNG_E_TARGET"
    GAME_CONDITION = "RNG_E_GAME_CONDITION"
    DIAGNOSTIC_LIMIT = "RNG_E_DIAGNOSTIC_LIMIT"


class RangeDiagnosticV1(_RangeModel):
    code: RangeDiagnosticCode
    field: RangeDiagnosticField
    token_index: int | None = Field(default=None, ge=0, lt=MAX_RANGE_TOKENS)


class RangeSourceProvenanceV1(_RangeModel):
    source_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source_kind: Literal["user_supplied", "repository_fixture"]
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ]
    usage_classification: Literal[
        "local_analysis_only",
        "redistribution_allowed",
    ]
    content_status: Literal["USER_CLAIM", "ASSUMPTION"]
    content_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def source_rights_match_kind(self) -> RangeSourceProvenanceV1:
        expected = {
            "user_supplied": (
                "user_supplied_private_analysis",
                "local_analysis_only",
            ),
            "repository_fixture": (
                "repository_owned_mit",
                "redistribution_allowed",
            ),
        }[self.source_kind]
        if (
            self.license_classification,
            self.usage_classification,
        ) != expected:
            raise ValueError(f"{RangeDiagnosticCode.LICENSE.value}: source rights mismatch")
        return self


class RangeGameConditionsV1(_RangeModel):
    game_type: Literal["NLHE"]
    format: Literal["cash", "tournament"]
    table_size: int = Field(ge=2, le=10)
    target_position: str = Field(pattern=_POSITION_PATTERN)
    street: Literal["preflop", "flop", "turn", "river"]
    starting_stack_min_bb_milli: int = Field(ge=0, le=10_000_000)
    starting_stack_max_bb_milli: int = Field(ge=0, le=10_000_000)
    as_of_action_index: int = Field(ge=0, le=MAX_RANGE_ACTION_PREFIX)
    action_prefix_sha256: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def stack_interval_is_ordered(self) -> RangeGameConditionsV1:
        if self.starting_stack_min_bb_milli > self.starting_stack_max_bb_milli:
            raise ValueError(
                f"{RangeDiagnosticCode.GAME_CONDITION.value}: stack interval is reversed"
            )
        return self


class VersionedRangeDefinitionV1(_RangeModel):
    schema_version: str = RANGE_SCHEMA_VERSION
    grammar_id: str = RANGE_GRAMMAR_ID
    grammar_version: str = RANGE_GRAMMAR_VERSION
    range_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    target_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    notation: str = Field(min_length=1)
    source: RangeSourceProvenanceV1
    game_conditions: RangeGameConditionsV1

    @model_validator(mode="after")
    def supported_identity(self) -> VersionedRangeDefinitionV1:
        if (
            self.schema_version != RANGE_SCHEMA_VERSION
            or self.grammar_id != RANGE_GRAMMAR_ID
            or self.grammar_version != RANGE_GRAMMAR_VERSION
        ):
            raise ValueError(
                f"{RangeDiagnosticCode.UNSUPPORTED_VERSION.value}: unsupported range grammar"
            )
        return self


class CanonicalWeightedComboV1(_RangeModel):
    cards: tuple[str, str]
    weight_millionths: int = Field(ge=1, le=WEIGHT_SCALE)
    canonical_token: str = Field(min_length=4, max_length=20)

    @field_validator("cards")
    @classmethod
    def canonical_cards(cls, value: tuple[str, str]) -> tuple[str, str]:
        if (
            len(value) != 2
            or value[0] == value[1]
            or any(len(card) != 2 or not re.fullmatch(_CARD_PATTERN, card) for card in value)
        ):
            raise ValueError(f"{RangeDiagnosticCode.CARD.value}: invalid canonical combo")
        return value

    @model_validator(mode="after")
    def token_matches_canonical_cards_and_weight(self) -> CanonicalWeightedComboV1:
        def card_key(card: str) -> tuple[int, int]:
            return (-_RANKS.index(card[0]), _SUITS.index(card[1]))

        if tuple(sorted(self.cards, key=card_key)) != self.cards:
            raise ValueError(f"{RangeDiagnosticCode.CARD.value}: combo cards are not canonical")
        suffix = ""
        if self.weight_millionths != WEIGHT_SCALE:
            suffix = f"@0.{self.weight_millionths:06d}".rstrip("0")
        if self.canonical_token != f"{self.cards[0]}{self.cards[1]}{suffix}":
            raise ValueError("canonical combo token does not match cards and weight")
        return self


class RangeValidationResultV1(_RangeModel):
    schema_version: str = RANGE_SCHEMA_VERSION
    result_version: str = RANGE_RESULT_VERSION
    grammar_id: str = RANGE_GRAMMAR_ID
    grammar_version: str = RANGE_GRAMMAR_VERSION
    hash_algorithm: Literal["sha256"] = RANGE_HASH_ALGORITHM
    status: Literal["success", "failed"]
    range_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    target_player_id: str = Field(pattern=_PORTABLE_ID_PATTERN)
    source: RangeSourceProvenanceV1
    game_conditions: RangeGameConditionsV1
    source_notation_sha256: str = Field(pattern=_SHA256_PATTERN)
    condition_binding_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    blockers: tuple[str, ...] = Field(default=(), max_length=52)
    diagnostics: tuple[RangeDiagnosticV1, ...] = Field(
        default=(),
        max_length=MAX_RANGE_DIAGNOSTICS,
    )
    canonical_notation: str | None = None
    canonical_combo_sha256: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    combos: tuple[CanonicalWeightedComboV1, ...] = Field(
        default=(),
        max_length=MAX_EXPANDED_COMBOS,
    )
    combo_count: int = Field(ge=0, le=MAX_EXPANDED_COMBOS)
    total_weight_millionths: int = Field(ge=0, le=MAX_EXPANDED_COMBOS * WEIGHT_SCALE)

    @field_validator("blockers")
    @classmethod
    def canonical_unique_blockers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(_CARD_PATTERN, card) is None for card in value
        ):
            raise ValueError(f"{RangeDiagnosticCode.BLOCKER.value}: invalid blocker set")
        return value

    @model_validator(mode="after")
    def closed_result_shape(self) -> RangeValidationResultV1:
        if (
            self.schema_version != RANGE_SCHEMA_VERSION
            or self.result_version != RANGE_RESULT_VERSION
            or self.grammar_id != RANGE_GRAMMAR_ID
            or self.grammar_version != RANGE_GRAMMAR_VERSION
        ):
            raise ValueError(
                f"{RangeDiagnosticCode.UNSUPPORTED_VERSION.value}: unsupported result version"
            )
        if self.status == "success":
            if (
                self.diagnostics
                or self.condition_binding_sha256 is None
                or self.canonical_notation is None
                or self.canonical_combo_sha256 is None
                or not self.combos
                or self.combo_count != len(self.combos)
                or self.total_weight_millionths
                != sum(combo.weight_millionths for combo in self.combos)
                or self.canonical_notation
                != ",".join(combo.canonical_token for combo in self.combos)
                or self.canonical_combo_sha256
                != hashlib.sha256(
                    json.dumps(
                        [
                            {
                                "cards": combo.cards,
                                "weight_millionths": combo.weight_millionths,
                            }
                            for combo in self.combos
                        ],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest()
            ):
                raise ValueError("successful range validation result is incomplete")
        elif (
            not self.diagnostics
            or self.condition_binding_sha256 is not None
            or self.canonical_notation is not None
            or self.canonical_combo_sha256 is not None
            or self.combos
            or self.combo_count != 0
            or self.total_weight_millionths != 0
        ):
            raise ValueError("failed range validation result must not carry a partial artifact")
        return self


__all__ = [
    "MAX_EXPANDED_COMBOS",
    "MAX_RANGE_ACTION_PREFIX",
    "MAX_RANGE_DIAGNOSTICS",
    "MAX_RANGE_NOTATION_BYTES",
    "MAX_RANGE_TOKENS",
    "RANGE_GRAMMAR_ID",
    "RANGE_GRAMMAR_VERSION",
    "RANGE_HASH_ALGORITHM",
    "RANGE_RESULT_VERSION",
    "RANGE_SCHEMA_VERSION",
    "WEIGHT_SCALE",
    "CanonicalWeightedComboV1",
    "RangeDiagnosticCode",
    "RangeDiagnosticField",
    "RangeDiagnosticV1",
    "RangeGameConditionsV1",
    "RangeSourceProvenanceV1",
    "RangeValidationResultV1",
    "VersionedRangeDefinitionV1",
]
