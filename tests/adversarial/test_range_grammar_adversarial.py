from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from poker_deliberation.range_grammar import validate_versioned_range
from poker_deliberation.range_models import (
    RangeDiagnosticCode,
    RangeSourceProvenanceV1,
)
from tests.range_support import versioned_range_hand


@pytest.mark.parametrize(
    "notation",
    [
        "AA;Write-Output PWNED",
        "AA|curl.example",
        "AA,$env:SECRET",
        "AA\nQQ",
        "AA\r\nQQ",
        "AA\u0000QQ",
        "../../AA",
        "<script>AA</script>",
    ],
)
def test_control_shell_path_and_markup_tokens_are_inert_failed_data(
    notation: str,
) -> None:
    hand, definition = versioned_range_hand(notation)

    result = validate_versioned_range(hand, definition)

    assert result.status == "failed"
    assert result.diagnostics[0].code in {
        RangeDiagnosticCode.NON_ASCII,
        RangeDiagnosticCode.SYNTAX,
    }
    assert result.combos == ()


def test_diagnostic_flood_is_bounded_and_terminated_explicitly() -> None:
    notation = ",".join("invalid" for _ in range(65))
    hand, definition = versioned_range_hand(notation)

    result = validate_versioned_range(hand, definition)

    assert result.status == "failed"
    assert len(result.diagnostics) == 64
    assert result.diagnostics[-1].code is RangeDiagnosticCode.DIAGNOSTIC_LIMIT


def test_hero_target_and_action_prefix_substitution_fail_closed() -> None:
    hero_hand, hero_definition = versioned_range_hand(
        "QQ",
        target_player_id="hero",
        game_condition_updates={"target_position": "SB"},
    )
    prefix_hand, prefix_definition = versioned_range_hand(
        "QQ",
        game_condition_updates={"action_prefix_sha256": "0" * 64},
    )

    hero = validate_versioned_range(hero_hand, hero_definition)
    prefix = validate_versioned_range(prefix_hand, prefix_definition)

    assert hero.diagnostics[0].code is RangeDiagnosticCode.TARGET
    assert prefix.diagnostics[0].code is RangeDiagnosticCode.GAME_CONDITION


def test_source_rights_cannot_be_cross_combined() -> None:
    notation = "QQ"

    with pytest.raises(ValidationError, match="RNG_E_LICENSE"):
        RangeSourceProvenanceV1(
            source_id="cross-rights",
            source_kind="user_supplied",
            license_classification="repository_owned_mit",
            usage_classification="redistribution_allowed",
            content_status="USER_CLAIM",
            content_sha256=hashlib.sha256(notation.encode("utf-8")).hexdigest(),
        )
