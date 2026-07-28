from __future__ import annotations

from poker_deliberation.range_grammar import validate_versioned_range
from poker_deliberation.range_models import RangeDiagnosticCode
from poker_deliberation.schemas import RangeDefinition
from poker_deliberation.tools.combinations import parse_weighted_range
from tests.range_support import versioned_range_hand


def test_legacy_range_definition_dump_is_unchanged() -> None:
    legacy = RangeDefinition(
        player_id="villain",
        notation="AKs@0.25,QQ@0.5",
        source="user",
        game_conditions={"street": "preflop"},
        assumed=True,
    )

    assert legacy.model_dump(mode="json") == {
        "player_id": "villain",
        "notation": "AKs@0.25,QQ@0.5",
        "source": "user",
        "game_conditions": {"street": "preflop"},
        "assumed": True,
    }


def test_legacy_blocker_overlap_behavior_remains_while_v1_rejects_overlap() -> None:
    legacy = parse_weighted_range("AKs,AsKs", ("As",))
    hand, definition = versioned_range_hand("AKs,AsKs")
    versioned = validate_versioned_range(hand, definition)

    assert len(legacy) == 3
    assert versioned.status == "failed"
    assert versioned.diagnostics[0].code is RangeDiagnosticCode.OVERLAP
