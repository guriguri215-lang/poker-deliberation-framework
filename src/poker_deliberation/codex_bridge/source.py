"""Verified P3-030C terminal projection into the minimal bridge context."""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from poker_deliberation.bounded_river_call_ev import (
    bounded_river_terminal_revision_root_sha256,
)
from poker_deliberation.codex_bridge.canonical import domain_sha256
from poker_deliberation.codex_bridge.models import (
    BridgeActionV1,
    BridgeFocalDecisionV1,
    BridgeHandV1,
    BridgeMathEvidenceV1,
    BridgePlayerV1,
    BridgeRangeProvenanceV1,
    BridgeSourceBindingV1,
    BridgeSourceContextV1,
    BridgeToolEvidenceV1,
    ExactRationalV1,
)
from poker_deliberation.codex_bridge.source_reader import (
    P3TerminalSourceReadError,
    read_verified_p3_terminal_source,
)
from poker_deliberation.schemas import CanonicalHand
from poker_deliberation.storage.terminal_models import RunReadStatus, VerifiedRunReadV2


class BridgeSourceError(ValueError):
    """Raised when a source terminal is not the one bounded P3-030C authority."""


def _rational(value: int | float) -> ExactRationalV1:
    if isinstance(value, bool):
        raise BridgeSourceError("boolean is not a canonical chip amount")
    fraction = Fraction(value) if isinstance(value, int) else Fraction(Decimal(str(value)))
    return ExactRationalV1(numerator=fraction.numerator, denominator=fraction.denominator)


def _optional_rational(value: int | float | None) -> ExactRationalV1 | None:
    return None if value is None else _rational(value)


def _project_hand(hand: CanonicalHand) -> BridgeHandV1:
    if (
        hand.game_type != "NLHE"
        or hand.format != "cash"
        or hand.tournament is not None
        or hand.hero_player_id is None
        or len(hand.hero_cards) != 2
        or len(hand.board) != 5
    ):
        raise BridgeSourceError("source hand is outside the bounded heads-up river cash scope")
    return BridgeHandV1(
        game_type="NLHE",
        format="cash",
        table_size=hand.table_size,
        small_blind=_rational(hand.small_blind),
        big_blind=_rational(hand.big_blind),
        ante=_rational(hand.ante),
        rake=_optional_rational(hand.rake),
        players=tuple(
            BridgePlayerV1(
                player_id=player.player_id,
                position=player.position,
                starting_stack=_rational(player.starting_stack),
            )
            for player in hand.players
        ),
        hero_player_id=hand.hero_player_id,
        hero_cards=(hand.hero_cards[0], hand.hero_cards[1]),
        board=(hand.board[0], hand.board[1], hand.board[2], hand.board[3], hand.board[4]),
        actions=tuple(
            BridgeActionV1(
                street=action.street.value,
                actor=action.actor,
                action=action.action,
                amount=_rational(action.amount),
                to_amount=_optional_rational(action.to_amount),
                pot_before=_optional_rational(action.pot_before),
                pot_after=_optional_rational(action.pot_after),
            )
            for action in hand.actions
        ),
    )


def project_verified_p3_terminal(
    read: VerifiedRunReadV2,
    *,
    source_revision_root: Path,
) -> BridgeSourceContextV1:
    """Project one fully verified successful P3-030C terminal without raw source text."""

    if read.read_status is not RunReadStatus.SUCCEEDED or not read.lifecycle_verified:
        raise BridgeSourceError("source terminal must be terminal-verified and succeeded")
    try:
        verified = read_verified_p3_terminal_source(
            read,
            source_revision_root=source_revision_root,
        )
    except P3TerminalSourceReadError as exc:
        raise BridgeSourceError("P3-030C terminal semantic replay failed") from exc
    candidate = verified.candidate
    binding = verified.binding
    result = verified.result
    provenance = verified.provenance
    projection = candidate.projection
    bounded = projection.bounded_candidate.projection
    range_definition = projection.range_definition
    call_ev = projection.call_ev_model
    checks = {
        "result_run": read.run_id == result.run_id,
        "binding_run": read.run_id == binding.run_id,
        "provenance_run": read.run_id == provenance.run_id,
        "result_binding": result.binding_sha256 == binding.binding_sha256,
        "provenance_binding": provenance.binding_sha256 == binding.binding_sha256,
        "provenance_candidate": provenance.candidate_sha256 == candidate.candidate_sha256,
        "provenance_result": provenance.result_sha256 == result.result_sha256,
        "provenance_revision": provenance.terminal_revision == read.revision,
        "provenance_transaction": provenance.terminal_transaction_id == read.transaction_id,
        "provenance_status": provenance.terminal_status == "completed",
        "repository_fixture": range_definition.source.source_kind == "repository_fixture",
        "repository_owned_mit": (
            range_definition.source.license_classification == "repository_owned_mit"
        ),
        "redistribution_allowed": (
            range_definition.source.usage_classification == "redistribution_allowed"
        ),
        "hand_repository_fixture": bounded.source.source_kind == "repository_fixture",
        "hand_repository_owned_mit": (
            bounded.source.license_classification == "repository_owned_mit"
        ),
        "hand_redistribution_allowed": (
            bounded.source.usage_classification == "redistribution_allowed"
        ),
        "hand_public": bounded.source.classification == "public",
        "tool_chain_succeeded": all(item.status == "success" for item in result.tool_support),
        "comparison_binding": result.action_comparison == call_ev.action_comparison,
        "equity_binding": result.equity == call_ev.equity,
        "required_equity_binding": result.required_equity == call_ev.required_equity,
        "call_ev_units_binding": result.call_ev_units == call_ev.call_ev_units,
        "call_ev_amount_binding": result.call_ev_amount == call_ev.call_ev_amount,
        "fold_ev_binding": result.fold_ev_units == call_ev.fold_ev_units,
        "delta_binding": (result.call_minus_fold_ev_units == call_ev.call_minus_fold_ev_units),
    }
    failed = tuple(name for name, accepted in checks.items() if not accepted)
    if failed:
        raise BridgeSourceError(
            "P3-030C terminal bindings do not form one exact source: " + ",".join(failed)
        )
    root_hash = bounded_river_terminal_revision_root_sha256(source_revision_root)
    if provenance.terminal_revision_root_sha256 != root_hash:
        raise BridgeSourceError("source terminal root identity mismatch")

    source = BridgeSourceBindingV1(
        source_terminal_run_id=read.run_id,
        source_terminal_revision=read.revision,
        source_terminal_transaction_id=read.transaction_id,
        source_terminal_revision_root_sha256=root_hash,
        source_terminal_manifest_sha256=read.manifest_sha256,
        source_terminal_inventory_sha256=read.inventory_sha256,
        source_candidate_sha256=candidate.candidate_sha256,
        source_binding_sha256=binding.binding_sha256,
        source_result_sha256=result.result_sha256,
        source_provenance_sha256=provenance.provenance_sha256,
    )
    focal = bounded.focal_decision
    range_conditions = range_definition.game_conditions
    if (
        range_definition.schema_version != "1.0.0"
        or range_definition.grammar_id != "poker-deliberation.nlhe-range"
        or range_definition.grammar_version != "1.0.0"
        or focal.hero_response != "fold"
    ):
        raise BridgeSourceError("source terminal uses an unsupported P3-030C semantic version")
    range_projection = BridgeRangeProvenanceV1(
        schema_version="1.0.0",
        grammar_id="poker-deliberation.nlhe-range",
        grammar_version="1.0.0",
        range_id=range_definition.range_id,
        target_player_id=range_definition.target_player_id,
        notation=range_definition.notation,
        source_id=range_definition.source.source_id,
        source_kind="repository_fixture",
        license_classification="repository_owned_mit",
        usage_classification="redistribution_allowed",
        content_status=range_definition.source.content_status,
        content_sha256=range_definition.source.content_sha256,
        table_size=range_conditions.table_size,
        target_position=range_conditions.target_position,
        street="river",
        starting_stack_min_bb_milli=range_conditions.starting_stack_min_bb_milli,
        starting_stack_max_bb_milli=range_conditions.starting_stack_max_bb_milli,
        as_of_action_index=range_conditions.as_of_action_index,
        action_prefix_sha256=range_conditions.action_prefix_sha256,
        range_definition_sha256=projection.range_definition_sha256,
        range_equity_binding_sha256=provenance.range_equity_binding_sha256,
    )
    math = BridgeMathEvidenceV1(
        equity_model_id=projection.equity_model.model_id,
        exact_only=True,
        exact_evaluation_cap=projection.equity_model.exact_evaluation_cap,
        equity=ExactRationalV1(**result.equity.model_dump()),
        call_ev_model_id=call_ev.model_id,
        fold_ev_reference=call_ev.fold_ev_reference,
        chip_unit=ExactRationalV1(**call_ev.chip_unit.model_dump()),
        pot_before_bet_units=call_ev.pot_before_bet_units,
        opponent_bet_units=call_ev.opponent_bet_units,
        pot_after_bet_units=call_ev.pot_after_bet_units,
        call_cost_units=call_ev.call_cost_units,
        contestable_pot_units=call_ev.contestable_pot_units,
        required_equity=ExactRationalV1(**result.required_equity.model_dump()),
        call_ev_units=ExactRationalV1(**result.call_ev_units.model_dump()),
        call_ev_amount=ExactRationalV1(**result.call_ev_amount.model_dump()),
        fold_ev_units=ExactRationalV1(**result.fold_ev_units.model_dump()),
        call_minus_fold_ev_units=ExactRationalV1(**result.call_minus_fold_ev_units.model_dump()),
        action_comparison=result.action_comparison,
        range_source_status=result.range_source_status,
        call_ev_model_sha256=projection.call_ev_model_sha256,
        source_result_sha256=result.result_sha256,
        tool_support=tuple(
            BridgeToolEvidenceV1(
                evidence_id=f"tool-{ordinal}-{item.tool_name}",
                tool_name=item.tool_name,
                status="success",
                result_sha256=item.result_sha256,
            )
            for ordinal, item in enumerate(result.tool_support)
        ),
    )
    payload: dict[str, object] = {
        "source": source,
        "hand": _project_hand(bounded.hand),
        "focal_decision": BridgeFocalDecisionV1(
            selector_street="river",
            selector_actor=focal.selector_actor,
            selector_action=focal.selector_action,
            selector_amount=_rational(focal.selector_amount),
            facing_action_index=focal.facing_action_index,
            hero_action_index=focal.hero_action_index,
            hero_response="fold",
            focal_sha256=focal.focal_sha256,
        ),
        "range": range_projection,
        "math": math,
    }
    return BridgeSourceContextV1.model_validate(
        {
            **payload,
            "context_payload_sha256": domain_sha256(
                "poker-bounded-codex-bridge-source-context-v1",
                payload,
            ),
        },
        strict=True,
    )


__all__ = ["BridgeSourceError", "project_verified_p3_terminal"]
