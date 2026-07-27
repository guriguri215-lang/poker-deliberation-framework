from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from poker_deliberation.normalization import (
    MAX_DIAGNOSTICS,
    MAX_LINE_BYTES,
    MAX_LINES,
    MAX_SOURCE_BYTES,
    NormalizationDiagnosticCode,
    NormalizationRequestV1,
    NormalizationResultV1,
    UnsupportedNormalizationVersion,
    canonical_normalized_hand_bytes,
    normalization_result_from_value,
    normalization_result_json_bytes,
    normalize_hand_bytes,
    normalize_hand_text,
)

VALID_LINES = (
    "game_type: NLHE",
    "format: cash",
    "table_size: 2",
    "small_blind: 1",
    "big_blind: 2",
    "player: hero, SB, 100",
    "player: villain, BB, 100",
    "hero_player_id: hero",
    "hero_cards: As Kh",
    "action: preflop, hero, post_blind, 1",
    "action: preflop, villain, post_blind, 2",
    "action: preflop, hero, call, 1",
)
VALID_LF = ("\n".join(VALID_LINES) + "\n").encode()


def _codes(source: bytes) -> tuple[NormalizationDiagnosticCode, ...]:
    return tuple(item.code for item in normalize_hand_bytes(source).diagnostics)


def test_success_has_exact_source_and_normalized_hand_provenance() -> None:
    result = normalize_hand_bytes(VALID_LF)

    assert result.status == "success"
    assert result.hand is not None
    assert result.diagnostics == ()
    normalized = canonical_normalized_hand_bytes(result.hand)
    assert result.provenance.source_bytes_length == len(VALID_LF)
    assert result.provenance.source_bytes_sha256 == hashlib.sha256(VALID_LF).hexdigest()
    assert result.provenance.normalized_hand_bytes_length == len(normalized)
    assert result.provenance.normalized_hand_sha256 == hashlib.sha256(normalized).hexdigest()
    assert (
        NormalizationResultV1.model_validate_json(
            normalization_result_json_bytes(result),
            strict=True,
        )
        == result
    )


def test_lf_and_crlf_preserve_source_identity_but_normalize_to_the_same_hand() -> None:
    crlf = ("\r\n".join(VALID_LINES) + "\r\n").encode()
    lf_result = normalize_hand_bytes(VALID_LF)
    crlf_result = normalize_hand_bytes(crlf)

    assert lf_result.hand == crlf_result.hand
    assert (
        lf_result.provenance.normalized_hand_sha256 == crlf_result.provenance.normalized_hand_sha256
    )
    assert lf_result.provenance.source_bytes_sha256 != crlf_result.provenance.source_bytes_sha256


@pytest.mark.parametrize(
    ("source", "code"),
    [
        (b"\xef\xbb\xbf" + VALID_LF, NormalizationDiagnosticCode.BOM),
        (b"\xff", NormalizationDiagnosticCode.UTF8),
        (b"format: cash\r\ntable_size: 2\n", NormalizationDiagnosticCode.NEWLINE),
        ("# cafe\u0301\n".encode(), NormalizationDiagnosticCode.NON_NFC),
        (b"format:\x00 cash\n", NormalizationDiagnosticCode.CONTROL_CHARACTER),
        (VALID_LF + b"site: x\n", NormalizationDiagnosticCode.UNKNOWN_KEY),
        (VALID_LF + b"table_size: 2\n", NormalizationDiagnosticCode.DUPLICATE_KEY),
        (
            VALID_LF.replace(b"small_blind: 1", b"small_blind: 1e0"),
            NormalizationDiagnosticCode.NUMERIC_LEXEME,
        ),
        (
            VALID_LF.replace(b"player: hero, SB, 100", b"player: hero, SB"),
            (NormalizationDiagnosticCode.FIELD_COUNT),
        ),
        (
            VALID_LF.replace(b"format: cash", b"format: tournament"),
            NormalizationDiagnosticCode.UNSUPPORTED_FORMAT,
        ),
    ],
)
def test_malformed_and_unsupported_inputs_fail_with_stable_codes(
    source: bytes,
    code: NormalizationDiagnosticCode,
) -> None:
    result = normalize_hand_bytes(source)

    assert result.status == "failed"
    assert result.hand is None
    assert code in tuple(item.code for item in result.diagnostics)
    assert result.provenance.normalized_hand_sha256 is None


def test_resource_boundaries_are_explicit_and_diagnostics_are_bounded() -> None:
    assert _codes(b"x" * (MAX_SOURCE_BYTES + 1)) == (NormalizationDiagnosticCode.SOURCE_TOO_LARGE,)
    assert _codes(b"x" * (MAX_LINE_BYTES + 1)) == (NormalizationDiagnosticCode.LINE_TOO_LONG,)
    assert _codes(b"\n" * MAX_LINES) == (NormalizationDiagnosticCode.TOO_MANY_LINES,)

    many_unknown = b"".join(f"unknown_{index}: x\n".encode() for index in range(300))
    result = normalize_hand_bytes(many_unknown)
    assert len(result.diagnostics) == MAX_DIAGNOSTICS
    assert result.diagnostics[-1].code is NormalizationDiagnosticCode.DIAGNOSTIC_LIMIT


def test_diagnostic_order_and_locations_follow_source_order() -> None:
    result = normalize_hand_bytes(
        VALID_LF + b"unknown_key: hidden value\n" + b"table_size: 3\n" + b"malformed raw value\n"
    )

    assert tuple((item.code, item.line, item.field) for item in result.diagnostics) == (
        (NormalizationDiagnosticCode.UNKNOWN_KEY, 13, "document"),
        (NormalizationDiagnosticCode.DUPLICATE_KEY, 14, "table_size"),
        (NormalizationDiagnosticCode.MALFORMED_LINE, 15, "document"),
    )


def test_contracts_are_frozen_strict_and_reject_future_versions() -> None:
    request = NormalizationRequestV1(source_bytes=VALID_LF)
    with pytest.raises(ValidationError):
        request.source_bytes = b"changed"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        NormalizationRequestV1(source_bytes="not-bytes")  # type: ignore[arg-type]

    value = normalize_hand_bytes(VALID_LF).model_dump(mode="json")
    value["result_version"] = "2.0.0"
    with pytest.raises(UnsupportedNormalizationVersion):
        normalization_result_from_value(value)


def test_legacy_projection_keeps_shape_but_uses_sanitized_codes() -> None:
    result = normalize_hand_text("unknown input")

    assert result.hand is None
    assert result.warnings
    assert result.warnings[0].startswith("NRM_E_MALFORMED_LINE")
    assert "unknown input" not in json.dumps(result.warnings)
