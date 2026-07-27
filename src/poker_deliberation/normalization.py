"""Versioned, conservative key-value hand normalization."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from poker_deliberation.schemas import CanonicalHand, CaseInput
from poker_deliberation.security import redact_sensitive

NORMALIZATION_CONTRACT_VERSION: Final = "1.0.0"
NORMALIZATION_RESULT_VERSION: Final = "1.0.0"
NORMALIZATION_PARSER_ID: Final = "poker-deliberation.generic-key-value-hand"
NORMALIZATION_PARSER_VERSION: Final = "1.0.0"
NORMALIZATION_SOURCE_KIND: Final = "documented-key-value-hand"
NORMALIZATION_ENCODING: Final = "utf-8"
NORMALIZATION_SUPPORTED_SITE: Final = "none"
NORMALIZATION_METADATA_KEY: Final = "_poker_normalization_result_v1"

MAX_SOURCE_BYTES: Final = 1_048_576
MAX_LINES: Final = 10_000
MAX_LINE_BYTES: Final = 16_384
MAX_PLAYERS: Final = 10
MAX_ACTIONS: Final = 2_000
MAX_DIAGNOSTICS: Final = 256
MAX_IDENTIFIER_CODEPOINTS: Final = 256

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LINE = re.compile(r"^[ \t]*([A-Za-z_]+)[ \t]*:[ \t]*(.*?)[ \t]*$")
_NON_NEGATIVE_DECIMAL = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_NON_NEGATIVE_INTEGER = re.compile(r"^[0-9]+$")
_CARD_SEPARATOR = re.compile(r"[\t ,]+")


class NormalizationDiagnosticCode(StrEnum):
    SOURCE_TOO_LARGE = "NRM_E_SOURCE_TOO_LARGE"
    UTF8 = "NRM_E_UTF8"
    BOM = "NRM_E_BOM"
    NEWLINE = "NRM_E_NEWLINE"
    NON_NFC = "NRM_E_NON_NFC"
    CONTROL_CHARACTER = "NRM_E_CONTROL_CHARACTER"
    SECRET_SHAPE = "NRM_E_SECRET_SHAPE"
    TOO_MANY_LINES = "NRM_E_TOO_MANY_LINES"
    LINE_TOO_LONG = "NRM_E_LINE_TOO_LONG"
    MALFORMED_LINE = "NRM_E_MALFORMED_LINE"
    UNKNOWN_KEY = "NRM_E_UNKNOWN_KEY"
    DUPLICATE_KEY = "NRM_E_DUPLICATE_KEY"
    NUMERIC_LEXEME = "NRM_E_NUMERIC_LEXEME"
    CSV = "NRM_E_CSV"
    FIELD_COUNT = "NRM_E_FIELD_COUNT"
    VALUE_TOO_LONG = "NRM_E_VALUE_TOO_LONG"
    TOO_MANY_PLAYERS = "NRM_E_TOO_MANY_PLAYERS"
    TOO_MANY_ACTIONS = "NRM_E_TOO_MANY_ACTIONS"
    UNSUPPORTED_FORMAT = "NRM_E_UNSUPPORTED_FORMAT"
    CANONICAL_FIELD = "NRM_E_CANONICAL_FIELD"
    DIAGNOSTIC_LIMIT = "NRM_E_DIAGNOSTIC_LIMIT"


_DIAGNOSTIC_MESSAGES: Final[dict[NormalizationDiagnosticCode, str]] = {
    NormalizationDiagnosticCode.SOURCE_TOO_LARGE: "source exceeds the byte limit",
    NormalizationDiagnosticCode.UTF8: "source is not strict UTF-8",
    NormalizationDiagnosticCode.BOM: "UTF-8 BOM is not allowed",
    NormalizationDiagnosticCode.NEWLINE: "newline style is mixed or contains bare CR",
    NormalizationDiagnosticCode.NON_NFC: "source must be Unicode NFC",
    NormalizationDiagnosticCode.CONTROL_CHARACTER: "source contains a prohibited control character",
    NormalizationDiagnosticCode.SECRET_SHAPE: "source contains a prohibited secret shape",
    NormalizationDiagnosticCode.TOO_MANY_LINES: "source exceeds the logical-line limit",
    NormalizationDiagnosticCode.LINE_TOO_LONG: "line exceeds the byte limit",
    NormalizationDiagnosticCode.MALFORMED_LINE: "line does not match the key-value grammar",
    NormalizationDiagnosticCode.UNKNOWN_KEY: "key is not part of grammar version 1",
    NormalizationDiagnosticCode.DUPLICATE_KEY: "scalar key occurs more than once",
    NormalizationDiagnosticCode.NUMERIC_LEXEME: "number is not an invariant non-negative decimal",
    NormalizationDiagnosticCode.CSV: "CSV record is malformed",
    NormalizationDiagnosticCode.FIELD_COUNT: "record has the wrong number of fields",
    NormalizationDiagnosticCode.VALUE_TOO_LONG: "identifier exceeds the code-point limit",
    NormalizationDiagnosticCode.TOO_MANY_PLAYERS: "player record count exceeds the limit",
    NormalizationDiagnosticCode.TOO_MANY_ACTIONS: "action record count exceeds the limit",
    NormalizationDiagnosticCode.UNSUPPORTED_FORMAT: (
        "tournament context requires structured JSON in grammar version 1"
    ),
    NormalizationDiagnosticCode.CANONICAL_FIELD: "value violates the canonical hand schema",
    NormalizationDiagnosticCode.DIAGNOSTIC_LIMIT: (
        "additional diagnostics were deterministically omitted"
    ),
}

NormalizationField: TypeAlias = Literal[
    "source",
    "document",
    "game_type",
    "format",
    "table_size",
    "small_blind",
    "big_blind",
    "ante",
    "rake",
    "players",
    "hero_player_id",
    "hero_cards",
    "board",
    "actions",
    "analysis_objective",
]

_KNOWN_FIELDS: Final[set[str]] = {
    "game_type",
    "format",
    "table_size",
    "small_blind",
    "big_blind",
    "ante",
    "rake",
    "hero_player_id",
    "hero_cards",
    "board",
    "analysis_objective",
}
_REPEATABLE_FIELDS: Final[set[str]] = {"player", "action"}
_ALL_KEYS: Final[set[str]] = _KNOWN_FIELDS | _REPEATABLE_FIELDS


class _NormalizationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class NormalizationRequestV1(_NormalizationModel):
    contract_version: Literal["1.0.0"] = NORMALIZATION_CONTRACT_VERSION
    parser_id: Literal["poker-deliberation.generic-key-value-hand"] = NORMALIZATION_PARSER_ID
    parser_version: Literal["1.0.0"] = NORMALIZATION_PARSER_VERSION
    source_kind: Literal["documented-key-value-hand"] = NORMALIZATION_SOURCE_KIND
    encoding: Literal["utf-8"] = NORMALIZATION_ENCODING
    supported_site: Literal["none"] = NORMALIZATION_SUPPORTED_SITE
    source_bytes: bytes


class NormalizationDiagnosticV1(_NormalizationModel):
    severity: Literal["error", "warning"]
    code: NormalizationDiagnosticCode
    line: int | None = Field(default=None, ge=1, le=MAX_LINES)
    field: NormalizationField | None = None

    @property
    def message(self) -> str:
        return _DIAGNOSTIC_MESSAGES[self.code]


class NormalizationProvenanceV1(_NormalizationModel):
    contract_version: Literal["1.0.0"] = NORMALIZATION_CONTRACT_VERSION
    parser_id: Literal["poker-deliberation.generic-key-value-hand"] = NORMALIZATION_PARSER_ID
    parser_version: Literal["1.0.0"] = NORMALIZATION_PARSER_VERSION
    source_kind: Literal["documented-key-value-hand"] = NORMALIZATION_SOURCE_KIND
    encoding: Literal["utf-8"] = NORMALIZATION_ENCODING
    supported_site: Literal["none"] = NORMALIZATION_SUPPORTED_SITE
    hash_algorithm: Literal["sha256"] = "sha256"
    source_bytes_length: int = Field(ge=0)
    source_bytes_sha256: str = Field(pattern=_SHA256.pattern)
    normalized_hand_bytes_length: int | None = Field(default=None, ge=0)
    normalized_hand_sha256: str | None = Field(default=None, pattern=_SHA256.pattern)

    @model_validator(mode="after")
    def normalized_hash_pair(self) -> NormalizationProvenanceV1:
        if (self.normalized_hand_bytes_length is None) != (self.normalized_hand_sha256 is None):
            raise ValueError("normalized hand length and hash must be present together")
        return self


class NormalizationResultV1(_NormalizationModel):
    result_version: Literal["1.0.0"] = NORMALIZATION_RESULT_VERSION
    contract_version: Literal["1.0.0"] = NORMALIZATION_CONTRACT_VERSION
    status: Literal["success", "failed"]
    hand: CanonicalHand | None
    diagnostics: tuple[NormalizationDiagnosticV1, ...] = Field(max_length=MAX_DIAGNOSTICS)
    provenance: NormalizationProvenanceV1

    @model_validator(mode="after")
    def closed_result(self) -> NormalizationResultV1:
        errors = tuple(item for item in self.diagnostics if item.severity == "error")
        if self.status == "success":
            if self.hand is None or errors:
                raise ValueError("successful normalization requires one hand and no errors")
            normalized = canonical_normalized_hand_bytes(self.hand)
            if self.provenance.normalized_hand_bytes_length != len(
                normalized
            ) or self.provenance.normalized_hand_sha256 != _sha256(normalized):
                raise ValueError("successful normalization hand provenance mismatch")
        elif (
            self.hand is not None
            or not errors
            or self.provenance.normalized_hand_bytes_length is not None
            or self.provenance.normalized_hand_sha256 is not None
        ):
            raise ValueError("failed normalization must contain errors and no normalized hand")
        return self


class UnsupportedNormalizationVersion(ValueError):
    """The normalization envelope is syntactically versioned but unsupported."""


@dataclass(frozen=True, slots=True)
class HandNormalizationResult:
    """Compatibility projection retained for callers of ``normalize_hand_text``."""

    hand: CanonicalHand | None
    warnings: tuple[str, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_normalized_hand_bytes(hand: CanonicalHand) -> bytes:
    return json.dumps(
        hand.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def normalization_result_json_bytes(result: NormalizationResultV1) -> bytes:
    return json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def normalization_result_from_value(value: object) -> NormalizationResultV1:
    if not isinstance(value, dict):
        raise ValueError("normalization transport is not an object")
    if (
        value.get("result_version") != NORMALIZATION_RESULT_VERSION
        or value.get("contract_version") != NORMALIZATION_CONTRACT_VERSION
    ):
        raise UnsupportedNormalizationVersion("unsupported normalization version")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        result = NormalizationResultV1.model_validate_json(encoded, strict=True)
    except (TypeError, ValueError, ValidationError) as exc:
        raise ValueError("normalization transport violates its strict schema") from exc
    if normalization_result_json_bytes(result) != encoded:
        raise ValueError("normalization transport is not canonical")
    return result


def _provenance(
    source: bytes,
    normalized: bytes | None = None,
) -> NormalizationProvenanceV1:
    return NormalizationProvenanceV1(
        source_bytes_length=len(source),
        source_bytes_sha256=_sha256(source),
        normalized_hand_bytes_length=None if normalized is None else len(normalized),
        normalized_hand_sha256=None if normalized is None else _sha256(normalized),
    )


def _diagnostic(
    code: NormalizationDiagnosticCode,
    *,
    line: int | None = None,
    field: NormalizationField | None = None,
) -> NormalizationDiagnosticV1:
    return NormalizationDiagnosticV1(
        severity="error",
        code=code,
        line=line,
        field=field,
    )


def _bounded(
    diagnostics: list[NormalizationDiagnosticV1],
) -> tuple[NormalizationDiagnosticV1, ...]:
    if len(diagnostics) <= MAX_DIAGNOSTICS:
        return tuple(diagnostics)
    return (
        *diagnostics[: MAX_DIAGNOSTICS - 1],
        _diagnostic(NormalizationDiagnosticCode.DIAGNOSTIC_LIMIT, field="document"),
    )


def _failed(
    source: bytes,
    diagnostics: list[NormalizationDiagnosticV1],
) -> NormalizationResultV1:
    return NormalizationResultV1(
        status="failed",
        hand=None,
        diagnostics=_bounded(diagnostics),
        provenance=_provenance(source),
    )


def _contains_prohibited_control(text: str) -> bool:
    for character in text:
        if character in "\t\r\n":
            continue
        codepoint = ord(character)
        if codepoint < 0x20 or 0x7F <= codepoint <= 0x9F or unicodedata.category(character) == "Cf":
            return True
    return False


def _split_lines(text: str) -> tuple[list[str], NormalizationDiagnosticV1 | None]:
    crlf_count = text.count("\r\n")
    if "\r" in text.replace("\r\n", ""):
        return [], _diagnostic(NormalizationDiagnosticCode.NEWLINE, field="document")
    bare_lf_count = text.count("\n") - crlf_count
    if crlf_count and bare_lf_count:
        return [], _diagnostic(NormalizationDiagnosticCode.NEWLINE, field="document")
    return (text.split("\r\n") if crlf_count else text.split("\n")), None


def _parse_number(
    value: str,
    *,
    integer: bool,
) -> int | float:
    pattern = _NON_NEGATIVE_INTEGER if integer else _NON_NEGATIVE_DECIMAL
    if pattern.fullmatch(value) is None:
        raise ValueError("invalid numeric lexeme")
    return int(value) if integer else float(value)


def _csv_fields(value: str) -> list[str]:
    fields = next(csv.reader([value], skipinitialspace=True, strict=True))
    return [item.strip(" \t") for item in fields]


def _identifier_too_long(value: str) -> bool:
    return len(value) > MAX_IDENTIFIER_CODEPOINTS


def _canonical_field(raw: object) -> NormalizationField:
    value = str(raw)
    if value == "player":
        return "players"
    if value == "action":
        return "actions"
    if value in _KNOWN_FIELDS:
        return value  # type: ignore[return-value]
    return "document"


def normalize_hand_request(request: NormalizationRequestV1) -> NormalizationResultV1:
    isolated = NormalizationRequestV1.model_validate(
        request.model_dump(mode="python"),
        strict=True,
    )
    source = isolated.source_bytes
    if len(source) > MAX_SOURCE_BYTES:
        return _failed(
            source,
            [
                _diagnostic(
                    NormalizationDiagnosticCode.SOURCE_TOO_LARGE,
                    field="source",
                )
            ],
        )
    if source.startswith(b"\xef\xbb\xbf"):
        return _failed(
            source,
            [_diagnostic(NormalizationDiagnosticCode.BOM, field="source")],
        )
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _failed(
            source,
            [_diagnostic(NormalizationDiagnosticCode.UTF8, field="source")],
        )
    if unicodedata.normalize("NFC", text) != text:
        return _failed(
            source,
            [_diagnostic(NormalizationDiagnosticCode.NON_NFC, field="document")],
        )
    if _contains_prohibited_control(text):
        return _failed(
            source,
            [
                _diagnostic(
                    NormalizationDiagnosticCode.CONTROL_CHARACTER,
                    field="document",
                )
            ],
        )
    if redact_sensitive(text, enabled=True) != text:
        return _failed(
            source,
            [_diagnostic(NormalizationDiagnosticCode.SECRET_SHAPE, field="document")],
        )
    lines, newline_error = _split_lines(text)
    if newline_error is not None:
        return _failed(source, [newline_error])
    if len(lines) > MAX_LINES:
        return _failed(
            source,
            [_diagnostic(NormalizationDiagnosticCode.TOO_MANY_LINES, field="document")],
        )

    data: dict[str, Any] = {"players": [], "actions": []}
    seen_scalars: set[str] = set()
    field_lines: dict[str, int] = {}
    diagnostics: list[NormalizationDiagnosticV1] = []
    scalar_types: dict[str, Literal["string", "integer", "number"]] = {
        "game_type": "string",
        "format": "string",
        "table_size": "integer",
        "small_blind": "number",
        "big_blind": "number",
        "ante": "number",
        "rake": "number",
        "hero_player_id": "string",
        "analysis_objective": "string",
    }

    for line_number, raw_line in enumerate(lines, start=1):
        if len(raw_line.encode("utf-8")) > MAX_LINE_BYTES:
            diagnostics.append(
                _diagnostic(
                    NormalizationDiagnosticCode.LINE_TOO_LONG,
                    line=line_number,
                    field="document",
                )
            )
            continue
        if not raw_line.strip(" \t") or raw_line.lstrip(" \t").startswith("#"):
            continue
        match = _LINE.fullmatch(raw_line)
        if match is None:
            diagnostics.append(
                _diagnostic(
                    NormalizationDiagnosticCode.MALFORMED_LINE,
                    line=line_number,
                    field="document",
                )
            )
            continue
        key = match.group(1).lower()
        value = match.group(2)
        if key not in _ALL_KEYS:
            diagnostics.append(
                _diagnostic(
                    NormalizationDiagnosticCode.UNKNOWN_KEY,
                    line=line_number,
                    field="document",
                )
            )
            continue
        field = _canonical_field(key)
        if key in _KNOWN_FIELDS:
            if key in seen_scalars:
                diagnostics.append(
                    _diagnostic(
                        NormalizationDiagnosticCode.DUPLICATE_KEY,
                        line=line_number,
                        field=field,
                    )
                )
                continue
            seen_scalars.add(key)
            field_lines[key] = line_number
        try:
            if key in scalar_types:
                scalar_type = scalar_types[key]
                if scalar_type == "integer":
                    data[key] = _parse_number(value, integer=True)
                elif scalar_type == "number":
                    data[key] = _parse_number(value, integer=False)
                else:
                    if _identifier_too_long(value) and key != "analysis_objective":
                        raise OverflowError
                    data[key] = value
            elif key in {"hero_cards", "board"}:
                data[key] = [item for item in _CARD_SEPARATOR.split(value) if item]
            elif key == "player":
                items = _csv_fields(value)
                if len(items) != 3:
                    raise IndexError
                if any(_identifier_too_long(item) for item in items[:2]):
                    raise OverflowError
                players = data["players"]
                if len(players) >= MAX_PLAYERS:
                    diagnostics.append(
                        _diagnostic(
                            NormalizationDiagnosticCode.TOO_MANY_PLAYERS,
                            line=line_number,
                            field="players",
                        )
                    )
                    continue
                players.append(
                    {
                        "player_id": items[0],
                        "position": items[1],
                        "starting_stack": _parse_number(items[2], integer=False),
                    }
                )
            else:
                items = _csv_fields(value)
                if len(items) not in {4, 5}:
                    raise IndexError
                if any(_identifier_too_long(item) for item in items[:3]):
                    raise OverflowError
                actions = data["actions"]
                if len(actions) >= MAX_ACTIONS:
                    diagnostics.append(
                        _diagnostic(
                            NormalizationDiagnosticCode.TOO_MANY_ACTIONS,
                            line=line_number,
                            field="actions",
                        )
                    )
                    continue
                action: dict[str, object] = {
                    "street": items[0],
                    "actor": items[1],
                    "action": items[2],
                    "amount": _parse_number(items[3], integer=False),
                }
                if len(items) == 5 and items[4]:
                    action["to_amount"] = _parse_number(items[4], integer=False)
                actions.append(action)
        except csv.Error:
            diagnostics.append(
                _diagnostic(
                    NormalizationDiagnosticCode.CSV,
                    line=line_number,
                    field=field,
                )
            )
        except IndexError:
            diagnostics.append(
                _diagnostic(
                    NormalizationDiagnosticCode.FIELD_COUNT,
                    line=line_number,
                    field=field,
                )
            )
        except OverflowError:
            diagnostics.append(
                _diagnostic(
                    NormalizationDiagnosticCode.VALUE_TOO_LONG,
                    line=line_number,
                    field=field,
                )
            )
        except (TypeError, ValueError):
            diagnostics.append(
                _diagnostic(
                    NormalizationDiagnosticCode.NUMERIC_LEXEME,
                    line=line_number,
                    field=field,
                )
            )

    if data.get("format") == "tournament":
        diagnostics.append(
            _diagnostic(
                NormalizationDiagnosticCode.UNSUPPORTED_FORMAT,
                line=field_lines.get("format"),
                field="format",
            )
        )
    if diagnostics:
        return _failed(source, diagnostics)

    try:
        hand = CanonicalHand.model_validate(data)
    except ValidationError as exc:
        seen: set[tuple[int | None, NormalizationField]] = set()
        for error in sorted(
            exc.errors(include_url=False, include_context=False, include_input=False),
            key=lambda item: (tuple(map(str, item["loc"])), str(item["type"])),
        ):
            raw_field = error["loc"][0] if error["loc"] else "document"
            field = _canonical_field(raw_field)
            identity = (field_lines.get(str(raw_field)), field)
            if identity in seen:
                continue
            seen.add(identity)
            diagnostics.append(
                _diagnostic(
                    NormalizationDiagnosticCode.CANONICAL_FIELD,
                    line=identity[0],
                    field=field,
                )
            )
        return _failed(source, diagnostics)

    normalized = canonical_normalized_hand_bytes(hand)
    return NormalizationResultV1(
        status="success",
        hand=hand,
        diagnostics=(),
        provenance=_provenance(source, normalized),
    )


def normalize_hand_bytes(source_bytes: bytes) -> NormalizationResultV1:
    request = NormalizationRequestV1(source_bytes=source_bytes)
    return normalize_hand_request(request)


def normalization_diagnostic_text(value: NormalizationDiagnosticV1) -> str:
    location = ""
    if value.line is not None:
        location += f" line={value.line}"
    if value.field is not None:
        location += f" field={value.field}"
    return f"{value.code.value}{location}: {value.message}"


def normalize_hand_text(text: str) -> HandNormalizationResult:
    """Compatibility projection over the strict byte-oriented version-1 parser."""

    try:
        result = normalize_hand_bytes(text.encode("utf-8", errors="strict"))
    except UnicodeEncodeError:
        return HandNormalizationResult(
            hand=None,
            warnings=(
                f"{NormalizationDiagnosticCode.UTF8.value}: "
                f"{_DIAGNOSTIC_MESSAGES[NormalizationDiagnosticCode.UTF8]}",
            ),
        )
    return HandNormalizationResult(
        hand=result.hand,
        warnings=tuple(normalization_diagnostic_text(item) for item in result.diagnostics),
    )


def normalization_transport(result: NormalizationResultV1) -> dict[str, object]:
    return {NORMALIZATION_METADATA_KEY: result.model_dump(mode="json")}


def extract_normalization_result(case: CaseInput) -> tuple[CaseInput, NormalizationResultV1 | None]:
    metadata = dict(case.metadata)
    raw = metadata.pop(NORMALIZATION_METADATA_KEY, None)
    clean = CaseInput.model_validate({**case.model_dump(mode="python"), "metadata": metadata})
    if raw is None:
        return clean, None
    result = normalization_result_from_value(raw)
    verify_normalization_binding(clean, clean, result)
    return clean, result


def verify_normalization_binding(
    input_case: CaseInput,
    normalized_case: CaseInput,
    result: NormalizationResultV1,
) -> None:
    if input_case.kind != "hand" or normalized_case.kind != "hand":
        raise ValueError("normalization binding requires hand cases")
    if input_case.raw_text is None:
        raise ValueError("normalization binding requires source text")
    try:
        source = input_case.raw_text.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError("normalization source text is not UTF-8 encodable") from exc
    if (
        len(source) != result.provenance.source_bytes_length
        or _sha256(source) != result.provenance.source_bytes_sha256
    ):
        raise ValueError("normalization source provenance mismatch")
    if input_case.hand != result.hand or normalized_case.hand != result.hand:
        raise ValueError("normalization hand binding mismatch")
    if NORMALIZATION_METADATA_KEY in input_case.metadata:
        raise ValueError("normalization transport metadata must not persist")


__all__ = [
    "MAX_ACTIONS",
    "MAX_DIAGNOSTICS",
    "MAX_IDENTIFIER_CODEPOINTS",
    "MAX_LINES",
    "MAX_LINE_BYTES",
    "MAX_PLAYERS",
    "MAX_SOURCE_BYTES",
    "NORMALIZATION_CONTRACT_VERSION",
    "NORMALIZATION_ENCODING",
    "NORMALIZATION_METADATA_KEY",
    "NORMALIZATION_PARSER_ID",
    "NORMALIZATION_PARSER_VERSION",
    "NORMALIZATION_RESULT_VERSION",
    "NORMALIZATION_SOURCE_KIND",
    "NORMALIZATION_SUPPORTED_SITE",
    "HandNormalizationResult",
    "NormalizationDiagnosticCode",
    "NormalizationDiagnosticV1",
    "NormalizationProvenanceV1",
    "NormalizationRequestV1",
    "NormalizationResultV1",
    "UnsupportedNormalizationVersion",
    "canonical_normalized_hand_bytes",
    "extract_normalization_result",
    "normalization_diagnostic_text",
    "normalization_result_from_value",
    "normalization_result_json_bytes",
    "normalization_transport",
    "normalize_hand_bytes",
    "normalize_hand_request",
    "normalize_hand_text",
    "verify_normalization_binding",
]
