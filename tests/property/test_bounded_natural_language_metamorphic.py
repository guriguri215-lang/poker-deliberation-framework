from __future__ import annotations

from poker_deliberation.bounded_natural_language import (
    prepare_bounded_natural_language_intake,
)
from tests.bounded_natural_language_support import SOURCE_BYTES


def _prepare(source: bytes, intake_id: str):
    result = prepare_bounded_natural_language_intake(
        source,
        intake_id=intake_id,
        source_id="fixture-property-1",
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        classification="public",
    )
    assert result.status == "ready"
    assert result.candidate is not None
    return result.candidate


def test_supported_fullwidth_separator_preserves_hand_and_tool_plan() -> None:
    base = _prepare(SOURCE_BYTES, "intake-property-base")
    variant = _prepare(
        SOURCE_BYTES.replace(b"1/2", "1\uff0f2".encode()),
        "intake-property-variant",
    )

    assert variant.projection.hand == base.projection.hand
    assert variant.projection.focal_decision == base.projection.focal_decision
    assert variant.projection.tool_plan == base.projection.tool_plan
    assert variant.projection.source.content_sha256 != base.projection.source.content_sha256
    assert variant.candidate_sha256 != base.candidate_sha256


def test_supported_ideographic_outer_space_preserves_semantic_projection() -> None:
    text = SOURCE_BYTES.decode("utf-8")
    spaced = "".join(f"　{line.rstrip()}　\n" for line in text.splitlines())
    base = _prepare(SOURCE_BYTES, "intake-property-space-base")
    variant = _prepare(spaced.encode(), "intake-property-space-variant")

    assert variant.projection.hand == base.projection.hand
    assert variant.projection.focal_decision == base.projection.focal_decision
    assert variant.projection.tool_plan == base.projection.tool_plan


def test_redundant_matching_pot_assertions_do_not_change_ledger_derivation() -> None:
    without_assertions = SOURCE_BYTES
    for line in (
        "判断直前のポットは12です。\n",
        "コール額は8です。\n",
        "コール後の争点ポットは28です。\n",
    ):
        without_assertions = without_assertions.replace(line.encode(), b"")
    declared = _prepare(SOURCE_BYTES, "intake-property-declared")
    derived = _prepare(without_assertions, "intake-property-derived")

    assert derived.projection.hand == declared.projection.hand
    assert derived.projection.tool_plan == declared.projection.tool_plan
    assert derived.projection.declared_pot_assertions.model_dump(mode="json") == {
        "pot_before_bet": None,
        "call_cost": None,
        "contestable_pot": None,
    }
    assert declared.projection.declared_pot_assertions.model_dump(mode="json") == {
        "pot_before_bet": 12.0,
        "call_cost": 8.0,
        "contestable_pot": 28.0,
    }
