from __future__ import annotations

import hashlib
import json

import pytest

from poker_deliberation.range_grammar import validate_versioned_range
from poker_deliberation.range_models import (
    CanonicalWeightedComboV1,
    RangeDiagnosticCode,
    RangeValidationResultV1,
    VersionedRangeDefinitionV1,
)
from poker_deliberation.schemas import CanonicalHand, RangeDefinition
from tests.range_support import versioned_range_hand


def test_valid_range_is_bound_expanded_blocked_and_canonicalized() -> None:
    hand, definition = versioned_range_hand()

    result = validate_versioned_range(hand, definition)

    assert result.status == "success"
    assert result.combo_count == 8
    assert result.total_weight_millionths == 3_500_000
    assert result.blockers == ("As", "Kh")
    assert result.canonical_notation is not None
    assert "AsKs" not in result.canonical_notation
    assert all(combo.weight_millionths in {250_000, 500_000} for combo in result.combos)
    assert (
        result.canonical_combo_sha256
        == hashlib.sha256(
            json.dumps(
                [
                    {
                        "cards": combo.cards,
                        "weight_millionths": combo.weight_millionths,
                    }
                    for combo in result.combos
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.parametrize(
    ("notation", "expected_code"),
    [
        ("QQ+", RangeDiagnosticCode.SYNTAX),
        ("KAs", RangeDiagnosticCode.CLASS_ORDER),
        ("AK", RangeDiagnosticCode.SYNTAX),
        ("AKs:0.5", RangeDiagnosticCode.SYNTAX),
        ("AKs@+0.5", RangeDiagnosticCode.WEIGHT_LEXEME),
        ("AKs@5e-1", RangeDiagnosticCode.WEIGHT_LEXEME),
        ("AKs@.5", RangeDiagnosticCode.WEIGHT_LEXEME),
        ("AKs@0", RangeDiagnosticCode.WEIGHT_RANGE),
        ("AKs@0.000000", RangeDiagnosticCode.WEIGHT_RANGE),
        ("\uff21\uff2bs", RangeDiagnosticCode.NON_ASCII),
    ],
)
def test_unsupported_notation_fails_with_stable_diagnostic(
    notation: str,
    expected_code: RangeDiagnosticCode,
) -> None:
    hand, definition = versioned_range_hand(notation)

    result = validate_versioned_range(hand, definition)

    assert result.status == "failed"
    assert result.diagnostics[0].code is expected_code
    assert result.combos == ()
    assert result.canonical_notation is None


def test_overlap_is_rejected_before_blocker_removal() -> None:
    hand, definition = versioned_range_hand("AKs,AsKs")

    result = validate_versioned_range(hand, definition)

    assert result.status == "failed"
    assert result.diagnostics[0].code is RangeDiagnosticCode.OVERLAP


def test_provenance_and_game_condition_mismatches_fail_closed() -> None:
    provenance_hand, provenance_definition = versioned_range_hand(source_content_sha256="0" * 64)
    condition_hand, condition_definition = versioned_range_hand(
        game_condition_updates={"target_position": "BTN"}
    )

    provenance = validate_versioned_range(provenance_hand, provenance_definition)
    condition = validate_versioned_range(condition_hand, condition_definition)

    assert provenance.diagnostics[0].code is RangeDiagnosticCode.PROVENANCE
    assert condition.diagnostics[0].code is RangeDiagnosticCode.GAME_CONDITION


def test_range_notation_byte_limit_is_a_typed_failed_result() -> None:
    notation = ",".join("QcQd" for _ in range(3_278))
    hand, definition = versioned_range_hand(notation)

    result = validate_versioned_range(hand, definition)

    assert len(notation.encode("utf-8")) > 16_384
    assert result.status == "failed"
    assert result.diagnostics[0].code is RangeDiagnosticCode.LIMIT


def test_canonical_hand_accepts_legacy_and_versioned_ranges_additively() -> None:
    hand, definition = versioned_range_hand()
    payload = hand.model_dump(mode="json")
    payload["known_ranges"] = [
        {
            "player_id": "villain",
            "notation": "QQ",
            "source": "legacy",
            "assumed": True,
        },
        definition.model_dump(mode="json"),
    ]

    reparsed = CanonicalHand.model_validate(payload)

    assert isinstance(reparsed.known_ranges[0], RangeDefinition)
    assert isinstance(reparsed.known_ranges[1], VersionedRangeDefinitionV1)


def test_result_models_reject_noncanonical_tokens_and_combo_hashes() -> None:
    with pytest.raises(ValueError, match="canonical combo token"):
        CanonicalWeightedComboV1(
            cards=("Qc", "Qd"),
            weight_millionths=500_000,
            canonical_token="QcQd@0.25",
        )
    hand, definition = versioned_range_hand()
    valid = validate_versioned_range(hand, definition)
    payload = valid.model_dump(mode="python")
    payload["canonical_combo_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="incomplete"):
        RangeValidationResultV1.model_validate(payload)
