"""Deterministic bounded NLHE range grammar and CanonicalHand binding."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from decimal import Decimal
from itertools import combinations
from typing import Any, cast

from poker_deliberation.range_models import (
    MAX_EXPANDED_COMBOS,
    MAX_RANGE_ACTION_PREFIX,
    MAX_RANGE_DIAGNOSTICS,
    MAX_RANGE_NOTATION_BYTES,
    MAX_RANGE_TOKENS,
    RANGE_GRAMMAR_ID,
    RANGE_GRAMMAR_VERSION,
    WEIGHT_SCALE,
    CanonicalWeightedComboV1,
    RangeDiagnosticCode,
    RangeDiagnosticField,
    RangeDiagnosticV1,
    RangeValidationResultV1,
    VersionedRangeDefinitionV1,
)
from poker_deliberation.schemas import (
    CanonicalHand,
    CaseInput,
    Exactness,
    NumericalExactness,
    ToolResult,
    ToolStatus,
    VerificationMetadata,
)
from poker_deliberation.tools.cards import RANKS, SUITS, normalize_cards

_EXPLICIT_COMBO = re.compile(r"^([2-9TJQKA][cdhs])([2-9TJQKA][cdhs])$")
_HAND_CLASS = re.compile(r"^([2-9TJQKA])([2-9TJQKA])([so])?$")
_WEIGHT = re.compile(r"^(?:0(?:\.[0-9]{1,6})?|1(?:\.0{1,6})?)$")
_ASCII = re.compile(r"^[\x20-\x7e]*$")
_CARD_SUIT_ORDER = {suit: index for index, suit in enumerate(SUITS)}
_VISIBLE_BOARD_COUNT = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def action_prefix_sha256(hand: CanonicalHand, as_of_action_index: int) -> str:
    """Hash the exact canonical action prefix used by range conditions."""

    if not 0 <= as_of_action_index <= min(len(hand.actions), MAX_RANGE_ACTION_PREFIX):
        raise ValueError(
            f"{RangeDiagnosticCode.GAME_CONDITION.value}: action prefix is out of range"
        )
    payload = [action.model_dump(mode="json") for action in hand.actions[:as_of_action_index]]
    return _sha256(_canonical_json_bytes(payload))


def _card_key(card: str) -> tuple[int, int]:
    return (-RANKS.index(card[0]), _CARD_SUIT_ORDER[card[1]])


def _canonical_combo(first: str, second: str) -> tuple[str, str]:
    if first == second:
        raise ValueError(f"{RangeDiagnosticCode.CARD.value}: duplicate combo card")
    ordered = sorted((first, second), key=_card_key)
    return (ordered[0], ordered[1])


def _combo_sort_key(combo: tuple[str, str]) -> tuple[tuple[int, int], tuple[int, int]]:
    return (_card_key(combo[0]), _card_key(combo[1]))


def _weight_millionths(text: str | None) -> int:
    if text is None:
        return WEIGHT_SCALE
    if not _WEIGHT.fullmatch(text):
        raise ValueError(f"{RangeDiagnosticCode.WEIGHT_LEXEME.value}: unsupported weight lexeme")
    value = int(Decimal(text) * WEIGHT_SCALE)
    if not 0 < value <= WEIGHT_SCALE:
        raise ArithmeticError(f"{RangeDiagnosticCode.WEIGHT_RANGE.value}: weight is outside (0, 1]")
    return value


def _weight_text(weight_millionths: int) -> str:
    if weight_millionths == WEIGHT_SCALE:
        return ""
    digits = f"{weight_millionths:06d}".rstrip("0")
    return f"@0.{digits}"


def _expanded_token(token: str) -> tuple[list[tuple[str, str]], int]:
    if token.count("@") > 1:
        raise ValueError(f"{RangeDiagnosticCode.SYNTAX.value}: multiple weight separators")
    notation, separator, raw_weight = token.partition("@")
    weight = _weight_millionths(raw_weight if separator else None)
    explicit = _EXPLICIT_COMBO.fullmatch(notation)
    if explicit is not None:
        return [_canonical_combo(explicit.group(1), explicit.group(2))], weight
    hand_class = _HAND_CLASS.fullmatch(notation)
    if hand_class is None:
        raise ValueError(f"{RangeDiagnosticCode.SYNTAX.value}: unsupported range token")
    first_rank, second_rank, suffix = hand_class.groups()
    if first_rank == second_rank:
        if suffix is not None:
            raise ValueError(f"{RangeDiagnosticCode.SYNTAX.value}: pair cannot carry suitedness")
        combos = [
            _canonical_combo(first_rank + first_suit, first_rank + second_suit)
            for first_suit, second_suit in combinations(SUITS, 2)
        ]
        return combos, weight
    if suffix is None:
        raise ValueError(f"{RangeDiagnosticCode.SYNTAX.value}: non-pair requires suitedness")
    if RANKS.index(first_rank) < RANKS.index(second_rank):
        raise ValueError(f"{RangeDiagnosticCode.CLASS_ORDER.value}: class ranks are not descending")
    if suffix == "s":
        combos = [_canonical_combo(first_rank + suit, second_rank + suit) for suit in SUITS]
    else:
        combos = [
            _canonical_combo(first_rank + first_suit, second_rank + second_suit)
            for first_suit in SUITS
            for second_suit in SUITS
            if first_suit != second_suit
        ]
    return combos, weight


def _diagnostic_from_exception(exc: Exception, token_index: int) -> RangeDiagnosticV1:
    raw = str(exc).split(":", 1)[0]
    try:
        code = RangeDiagnosticCode(raw)
    except ValueError:
        code = RangeDiagnosticCode.SYNTAX
    field = cast(
        RangeDiagnosticField,
        {
            RangeDiagnosticCode.WEIGHT_LEXEME: "weight",
            RangeDiagnosticCode.WEIGHT_RANGE: "weight",
            RangeDiagnosticCode.CARD: "card",
            RangeDiagnosticCode.CLASS_ORDER: "notation",
        }.get(code, "notation"),
    )
    return RangeDiagnosticV1(code=code, field=field, token_index=token_index)


def _bounded_diagnostics(
    values: list[RangeDiagnosticV1],
) -> tuple[RangeDiagnosticV1, ...]:
    if len(values) <= MAX_RANGE_DIAGNOSTICS:
        return tuple(values)
    return (
        *values[: MAX_RANGE_DIAGNOSTICS - 1],
        RangeDiagnosticV1(
            code=RangeDiagnosticCode.DIAGNOSTIC_LIMIT,
            field="notation",
        ),
    )


def _failed(
    definition: VersionedRangeDefinitionV1,
    source_hash: str,
    diagnostics: list[RangeDiagnosticV1],
) -> RangeValidationResultV1:
    return RangeValidationResultV1(
        status="failed",
        range_id=definition.range_id,
        target_player_id=definition.target_player_id,
        source=definition.source,
        game_conditions=definition.game_conditions,
        source_notation_sha256=source_hash,
        diagnostics=_bounded_diagnostics(diagnostics),
        combo_count=0,
        total_weight_millionths=0,
    )


def _condition_binding(
    hand: CanonicalHand,
    definition: VersionedRangeDefinitionV1,
) -> tuple[str | None, tuple[str, ...], RangeDiagnosticV1 | None]:
    conditions = definition.game_conditions
    players = {player.player_id: player for player in hand.players}
    player = players.get(definition.target_player_id)
    if (
        hand.hero_player_id is None
        or player is None
        or definition.target_player_id == hand.hero_player_id
    ):
        return (
            None,
            (),
            RangeDiagnosticV1(
                code=RangeDiagnosticCode.TARGET,
                field="target_player",
            ),
        )
    if (
        hand.game_type != conditions.game_type
        or hand.format != conditions.format
        or hand.table_size != conditions.table_size
        or player.position != conditions.target_position
        or conditions.as_of_action_index > len(hand.actions)
    ):
        return (
            None,
            (),
            RangeDiagnosticV1(
                code=RangeDiagnosticCode.GAME_CONDITION,
                field="game_conditions",
            ),
        )
    action_index = conditions.as_of_action_index
    if action_index < len(hand.actions):
        observed_street = hand.actions[action_index].street.value
    elif hand.actions:
        observed_street = hand.actions[-1].street.value
    else:
        observed_street = "preflop"
    if observed_street != conditions.street:
        return (
            None,
            (),
            RangeDiagnosticV1(
                code=RangeDiagnosticCode.GAME_CONDITION,
                field="game_conditions",
            ),
        )
    try:
        prefix_hash = action_prefix_sha256(hand, action_index)
    except ValueError:
        prefix_hash = ""
    stack_bb_milli = (
        Decimal(str(player.starting_stack)) * Decimal(1000) / Decimal(str(hand.big_blind))
    )
    if (
        prefix_hash != conditions.action_prefix_sha256
        or stack_bb_milli < conditions.starting_stack_min_bb_milli
        or stack_bb_milli > conditions.starting_stack_max_bb_milli
    ):
        return (
            None,
            (),
            RangeDiagnosticV1(
                code=RangeDiagnosticCode.GAME_CONDITION,
                field="game_conditions",
            ),
        )
    board_count = _VISIBLE_BOARD_COUNT[conditions.street]
    if len(hand.board) < board_count:
        return (
            None,
            (),
            RangeDiagnosticV1(
                code=RangeDiagnosticCode.GAME_CONDITION,
                field="game_conditions",
            ),
        )
    try:
        blockers = normalize_cards((*hand.hero_cards, *hand.board[:board_count]))
    except ValueError:
        return (
            None,
            (),
            RangeDiagnosticV1(
                code=RangeDiagnosticCode.BLOCKER,
                field="blockers",
            ),
        )
    binding_value: dict[str, Any] = {
        "range_id": definition.range_id,
        "target_player_id": definition.target_player_id,
        "hero_player_id": hand.hero_player_id,
        "game_conditions": conditions.model_dump(mode="json"),
        "game_type": hand.game_type,
        "format": hand.format,
        "table_size": hand.table_size,
        "target_position": player.position,
        "starting_stack": player.starting_stack,
        "big_blind": hand.big_blind,
        "street": observed_street,
        "as_of_action_index": action_index,
        "action_prefix_sha256": prefix_hash,
        "blockers": blockers,
    }
    return _sha256(_canonical_json_bytes(binding_value)), blockers, None


def validate_versioned_range(
    hand: CanonicalHand,
    definition: VersionedRangeDefinitionV1,
) -> RangeValidationResultV1:
    """Validate, expand, block, and canonicalize one approved versioned range."""

    source_bytes = definition.notation.encode("utf-8")
    source_hash = _sha256(source_bytes)
    if (
        definition.schema_version != "1.0.0"
        or definition.grammar_id != RANGE_GRAMMAR_ID
        or definition.grammar_version != RANGE_GRAMMAR_VERSION
    ):
        return _failed(
            definition,
            source_hash,
            [
                RangeDiagnosticV1(
                    code=RangeDiagnosticCode.UNSUPPORTED_VERSION,
                    field="envelope",
                )
            ],
        )
    if len(source_bytes) > MAX_RANGE_NOTATION_BYTES:
        return _failed(
            definition,
            source_hash,
            [RangeDiagnosticV1(code=RangeDiagnosticCode.LIMIT, field="notation")],
        )
    if _ASCII.fullmatch(definition.notation) is None:
        return _failed(
            definition,
            source_hash,
            [RangeDiagnosticV1(code=RangeDiagnosticCode.NON_ASCII, field="notation")],
        )
    if source_hash != definition.source.content_sha256:
        return _failed(
            definition,
            source_hash,
            [RangeDiagnosticV1(code=RangeDiagnosticCode.PROVENANCE, field="provenance")],
        )
    versioned = [item for item in hand.known_ranges if isinstance(item, VersionedRangeDefinitionV1)]
    if len(versioned) != 1 or versioned[0] != definition:
        return _failed(
            definition,
            source_hash,
            [RangeDiagnosticV1(code=RangeDiagnosticCode.TARGET, field="target_player")],
        )
    condition_hash, blockers, condition_error = _condition_binding(hand, definition)
    if condition_error is not None or condition_hash is None:
        return _failed(
            definition,
            source_hash,
            [
                condition_error
                or RangeDiagnosticV1(
                    code=RangeDiagnosticCode.GAME_CONDITION,
                    field="game_conditions",
                )
            ],
        )
    raw_tokens = definition.notation.split(",")
    if not raw_tokens or len(raw_tokens) > MAX_RANGE_TOKENS:
        return _failed(
            definition,
            source_hash,
            [RangeDiagnosticV1(code=RangeDiagnosticCode.LIMIT, field="notation")],
        )
    expanded: dict[tuple[str, str], int] = {}
    diagnostics: list[RangeDiagnosticV1] = []
    for token_index, raw_token in enumerate(raw_tokens):
        token = raw_token.strip(" \t")
        if not token or token != raw_token.strip(" \t\r\n"):
            diagnostics.append(
                RangeDiagnosticV1(
                    code=RangeDiagnosticCode.SYNTAX,
                    field="notation",
                    token_index=token_index,
                )
            )
            continue
        try:
            combos, weight = _expanded_token(token)
        except (ValueError, ArithmeticError) as exc:
            diagnostics.append(_diagnostic_from_exception(exc, token_index))
            continue
        for combo in combos:
            if combo in expanded:
                diagnostics.append(
                    RangeDiagnosticV1(
                        code=RangeDiagnosticCode.OVERLAP,
                        field="notation",
                        token_index=token_index,
                    )
                )
                continue
            expanded[combo] = weight
    if diagnostics:
        return _failed(definition, source_hash, diagnostics)
    if len(expanded) > MAX_EXPANDED_COMBOS:
        return _failed(
            definition,
            source_hash,
            [RangeDiagnosticV1(code=RangeDiagnosticCode.LIMIT, field="notation")],
        )
    blocker_set = set(blockers)
    surviving = [
        (combo, weight) for combo, weight in expanded.items() if not blocker_set.intersection(combo)
    ]
    if not surviving:
        return _failed(
            definition,
            source_hash,
            [RangeDiagnosticV1(code=RangeDiagnosticCode.EMPTY, field="blockers")],
        )
    canonical_combos = tuple(
        CanonicalWeightedComboV1(
            cards=combo,
            weight_millionths=weight,
            canonical_token=f"{combo[0]}{combo[1]}{_weight_text(weight)}",
        )
        for combo, weight in sorted(surviving, key=lambda item: _combo_sort_key(item[0]))
    )
    canonical_notation = ",".join(combo.canonical_token for combo in canonical_combos)
    canonical_hash = _sha256(
        _canonical_json_bytes(
            [
                {
                    "cards": combo.cards,
                    "weight_millionths": combo.weight_millionths,
                }
                for combo in canonical_combos
            ]
        )
    )
    return RangeValidationResultV1(
        status="success",
        range_id=definition.range_id,
        target_player_id=definition.target_player_id,
        source=definition.source,
        game_conditions=definition.game_conditions,
        source_notation_sha256=source_hash,
        condition_binding_sha256=condition_hash,
        blockers=blockers,
        diagnostics=(),
        canonical_notation=canonical_notation,
        canonical_combo_sha256=canonical_hash,
        combos=canonical_combos,
        combo_count=len(canonical_combos),
        total_weight_millionths=sum(combo.weight_millionths for combo in canonical_combos),
    )


def _replay_range_validate_result(result: ToolResult) -> RangeValidationResultV1:
    from poker_deliberation.tools.contracts import RangeValidateInput

    if (
        result.status is not ToolStatus.SUCCESS
        or result.numeric_exactness is not NumericalExactness.EXACT
        or result.contract_version != "2.0.0"
    ):
        raise ValueError("range_validate result lacks an exact successful contract binding")
    request = RangeValidateInput.model_validate_json(
        _canonical_json_bytes(result.input),
        strict=True,
    )
    observed = RangeValidationResultV1.model_validate_json(
        _canonical_json_bytes(result.output),
        strict=True,
    )
    expected = validate_versioned_range(request.hand, request.range_definition)
    if observed != expected:
        raise ValueError("range_validate result differs from deterministic replay")
    return observed


def _replay_combos_result(
    result: ToolResult,
    *,
    expected_payload: dict[str, object],
) -> None:
    from poker_deliberation.tools.combinations import parse_weighted_range
    from poker_deliberation.tools.contracts import CombosInput, CombosOutput, contract_by_name

    if (
        result.status is not ToolStatus.SUCCESS
        or result.numeric_exactness is not NumericalExactness.FLOATING_VERIFIED
        or result.contract_version != "2.0.0"
        or result.input != expected_payload
    ):
        raise ValueError("versioned range combos result lacks the required product binding")
    payload = CombosInput.model_validate_json(
        _canonical_json_bytes(result.input),
        strict=True,
    )
    if payload.range is None or payload.hand_class is not None or payload.dead_cards:
        raise ValueError("versioned range combos input is not a canonical range projection")
    combos = parse_weighted_range(payload.range, ())
    total_weight = sum(combo.weight for combo in combos)
    expected = CombosOutput.model_validate_json(
        _canonical_json_bytes(
            {
                "range": payload.range,
                "combo_count": len(combos),
                "total_combo_weight": total_weight,
                "normalized_weights": [
                    {
                        "cards": list(combo.cards),
                        "weight": combo.weight / total_weight,
                    }
                    for combo in combos
                ],
            }
        ),
        strict=True,
    )
    observed = CombosOutput.model_validate_json(
        _canonical_json_bytes(result.output),
        strict=True,
    )
    if observed != expected:
        raise ValueError("versioned range combos result differs from deterministic replay")
    contract = contract_by_name()["combos"]
    evidence = contract.verify_floating(result.input, result.output)
    expected_verification = VerificationMetadata(
        method="executed tool-specific invariant checks",
        checks=list(evidence.checks),
        observations=list(evidence.observations),
        tolerance=evidence.tolerance,
        passed=True,
    )
    if result.verification != expected_verification:
        raise ValueError("versioned range combos verification metadata is invalid")


def _verify_failed_product_result(
    result: ToolResult,
    *,
    expected_payload: dict[str, object],
) -> None:
    if (
        result.status is ToolStatus.SUCCESS
        or result.contract_version != "2.0.0"
        or result.input != expected_payload
        or result.output
        or result.exactness is not Exactness.UNAVAILABLE
        or result.numeric_exactness is not NumericalExactness.UNAVAILABLE
        or result.verification is not None
        or not result.error
    ):
        raise ValueError("failed versioned range product result lacks its exact failure binding")


def verify_versioned_range_tool_chain(
    case: CaseInput,
    tool_results: Sequence[ToolResult],
    *,
    run_status: str = "completed",
) -> None:
    """Replay versioned range artifacts and enforce the product-chain boundary."""

    range_results = [result for result in tool_results if result.tool_name == "range_validate"]
    replayed = {
        id(result): _replay_range_validate_result(result)
        for result in range_results
        if result.status is ToolStatus.SUCCESS
    }
    if case.hand is None:
        return
    versioned = [
        item for item in case.hand.known_ranges if isinstance(item, VersionedRangeDefinitionV1)
    ]
    if "combos" not in case.requested_tools or not versioned:
        return
    combo_results = [result for result in tool_results if result.tool_name == "combos"]
    if len(versioned) != 1:
        if combo_results:
            raise ValueError("multiple versioned ranges must fail closed before combos")
        return
    if (
        run_status in {"approval_required", "failed_with_limitations"}
        and not range_results
        and not combo_results
    ):
        # A terminal run may fail closed before the requested-tool phase starts.
        # Absence of both product results is therefore valid only for a
        # non-completed report; partial chains remain subject to replay below.
        return
    definition = versioned[0]
    expected_range_payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "hand": case.hand.model_dump(mode="json"),
        "range_definition": definition.model_dump(mode="json"),
    }
    product_results = [result for result in range_results if result.input == expected_range_payload]
    if len(range_results) != 1 or len(product_results) != 1:
        raise ValueError("versioned range product chain requires one bound validation result")
    validation_result = product_results[0]
    validated = replayed.get(id(validation_result))
    if validated is None:
        if run_status != "failed_with_limitations" or combo_results:
            raise ValueError("versioned range product validation did not complete successfully")
        _verify_failed_product_result(
            validation_result,
            expected_payload=expected_range_payload,
        )
        return
    if validated.status == "failed":
        if combo_results:
            raise ValueError("failed versioned range validation must not reach combos")
        return
    if validated.canonical_notation is None:
        raise ValueError("successful versioned range validation lacks canonical notation")
    expected_combo_payload: dict[str, object] = {
        "range": validated.canonical_notation,
        "dead_cards": [],
    }
    tool_inputs = case.metadata.get("tool_inputs", {})
    if not isinstance(tool_inputs, dict):
        tool_inputs = {}
    supplied_validation = tool_inputs.get("range_validate")
    supplied_combos = tool_inputs.get("combos")
    conflicted = supplied_validation not in (
        None,
        {},
        expected_range_payload,
    ) or supplied_combos not in (None, {}, expected_combo_payload)
    if conflicted:
        if combo_results:
            raise ValueError("conflicting versioned range inputs must fail closed")
        return
    if not combo_results and run_status in {
        "approval_required",
        "failed_with_limitations",
    }:
        return
    if len(combo_results) != 1:
        raise ValueError("successful versioned range validation requires one combos result")
    range_index = next(
        index for index, result in enumerate(tool_results) if result is validation_result
    )
    combo_index = next(
        index for index, result in enumerate(tool_results) if result is combo_results[0]
    )
    if range_index >= combo_index:
        raise ValueError("versioned range validation must precede combos")
    if combo_results[0].status is not ToolStatus.SUCCESS:
        if run_status != "failed_with_limitations":
            raise ValueError("completed versioned range product requires successful combos")
        _verify_failed_product_result(
            combo_results[0],
            expected_payload=expected_combo_payload,
        )
        return
    _replay_combos_result(
        combo_results[0],
        expected_payload=expected_combo_payload,
    )


__all__ = [
    "action_prefix_sha256",
    "validate_versioned_range",
    "verify_versioned_range_tool_chain",
]
