"""Opt-in, provenance-bound river equity bridge for one versioned NLHE range."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, NoReturn

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from poker_deliberation.range_equity_models import (
    BINDING_HASH_DOMAIN,
    CANDIDATE_HASH_DOMAIN,
    COMBOS_INPUT_HASH_DOMAIN,
    COMBOS_OUTPUT_HASH_DOMAIN,
    EQUITY_INPUT_HASH_DOMAIN,
    EQUITY_OUTPUT_HASH_DOMAIN,
    ORACLE_HASH_DOMAIN,
    RANGE_EQUITY_MARKER,
    RANGE_EQUITY_MAX_EVALUATIONS,
    RANGE_EQUITY_TOOL_PLAN,
    RESULT_HASH_DOMAIN,
    SOURCE_RANGE_HASH_DOMAIN,
    VALIDATION_INPUT_HASH_DOMAIN,
    VALIDATION_OUTPUT_HASH_DOMAIN,
    RangeEquityDiagnosticCode,
    VersionedRangeRiverEquityBindingV1,
    VersionedRangeRiverEquityOracleProjectionV1,
    VersionedRangeRiverEquityResultV1,
    canonical_domain_sha256,
)
from poker_deliberation.range_grammar import (
    validate_versioned_range,
    verify_versioned_range_tool_chain,
)
from poker_deliberation.range_models import RangeValidationResultV1, VersionedRangeDefinitionV1
from poker_deliberation.schemas import (
    CaseInput,
    Exactness,
    NumericalExactness,
    ToolResult,
    ToolStatus,
    VerificationMetadata,
)
from poker_deliberation.security import redact_sensitive
from poker_deliberation.tools.cards import evaluate_holdem, normalize_cards
from poker_deliberation.tools.contracts import (
    HoldemEquityOutput,
    contract_by_name,
    versioned_range_bridge_failure_error,
)
from poker_deliberation.tools.numeric import close_ulps


class VersionedRangeRiverEquityError(ValueError):
    """Stable fail-closed error raised at bridge admission or replay."""

    def __init__(self, code: RangeEquityDiagnosticCode, field: str, detail: str) -> None:
        self.code = code
        self.field = field
        self.detail = detail
        super().__init__(f"{code.value}: {field}: {detail}")


@dataclass(frozen=True, slots=True)
class VersionedRangeRiverEquityAdmissionV1:
    candidate: CaseInput
    binding: VersionedRangeRiverEquityBindingV1
    case: CaseInput


@dataclass(frozen=True, slots=True)
class _OracleTotals:
    win_combo_count: int
    tie_combo_count: int
    loss_combo_count: int
    win_weight_millionths: int
    tie_weight_millionths: int
    loss_weight_millionths: int
    equity: Fraction


def _fail(code: RangeEquityDiagnosticCode, field: str, detail: str) -> NoReturn:
    raise VersionedRangeRiverEquityError(code, field, detail)


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except RecursionError as exc:
        raise ValueError("JSON value exceeds the supported nesting depth") from exc


def _canonical_tree_matches(observed: object, expected: object) -> bool:
    """Compare JSON trees without collapsing distinct JSON number encodings."""

    try:
        return _canonical_json_bytes(observed) == _canonical_json_bytes(expected)
    except (TypeError, ValueError):
        return False


def _nfc_tree(value: object) -> bool:
    pending = [value]
    seen_containers: set[int] = set()
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if unicodedata.normalize("NFC", item) != item:
                return False
            continue
        if isinstance(item, dict):
            identity = id(item)
            if identity in seen_containers:
                return False
            seen_containers.add(identity)
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in seen_containers:
                return False
            seen_containers.add(identity)
            pending.extend(item)
    return True


def _json_tree_depth_is_supported(value: object, *, maximum_depth: int = 128) -> bool:
    """Bound replay inputs before recursive model equality or serialization."""

    pending = [(value, 0)]
    seen_containers: set[int] = set()
    while pending:
        item, depth = pending.pop()
        if depth > maximum_depth:
            return False
        if isinstance(item, dict):
            identity = id(item)
            if identity in seen_containers:
                return False
            seen_containers.add(identity)
            pending.extend((key, depth + 1) for key in item)
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, (list, tuple)):
            identity = id(item)
            if identity in seen_containers:
                return False
            seen_containers.add(identity)
            pending.extend((child, depth + 1) for child in item)
    return True


def _require_supported_replay_trees(
    case: CaseInput,
    tool_results: tuple[ToolResult, ...] | list[ToolResult],
) -> None:
    if not _json_tree_depth_is_supported(case.metadata):
        _fail(
            RangeEquityDiagnosticCode.SCHEMA,
            "case.metadata",
            "bridge metadata exceeds the supported JSON nesting depth",
        )
    for result in tool_results:
        for field, value in (("input", result.input), ("output", result.output)):
            if not _json_tree_depth_is_supported(value):
                _fail(
                    RangeEquityDiagnosticCode.CHAIN,
                    f"{result.tool_name}.{field}",
                    "tool result exceeds the supported JSON nesting depth",
                )


def _binding_from_case(case: CaseInput) -> VersionedRangeRiverEquityBindingV1 | None:
    if RANGE_EQUITY_MARKER not in case.metadata:
        return None
    raw = case.metadata[RANGE_EQUITY_MARKER]
    try:
        return VersionedRangeRiverEquityBindingV1.model_validate_json(
            _canonical_json_bytes(raw),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        errors = (
            exc.errors(include_url=False, include_context=False, include_input=False)
            if isinstance(exc, ValidationError)
            else []
        )
        code = (
            RangeEquityDiagnosticCode.PROVENANCE
            if len(errors) == 1
            and errors[0].get("loc") == ()
            and errors[0].get("type") == "value_error"
            and errors[0].get("msg") == "Value error, REQ_E_PROVENANCE: binding hash mismatch"
            else RangeEquityDiagnosticCode.SCHEMA
        )
        raise VersionedRangeRiverEquityError(
            code,
            f"metadata.{RANGE_EQUITY_MARKER}",
            "invalid bridge binding",
        ) from exc


def versioned_range_river_equity_binding(
    case: CaseInput,
) -> VersionedRangeRiverEquityBindingV1 | None:
    """Return and strictly validate the opt-in marker, if present."""

    return _binding_from_case(case)


def verify_versioned_range_river_equity_case_correlation(
    input_case: CaseInput,
    normalized_case: CaseInput,
    reconstructed_input: dict[str, object],
) -> None:
    """Require every persisted bridge case projection to be identical and bound."""

    report_metadata = reconstructed_input.get("metadata")
    marker_present = (
        RANGE_EQUITY_MARKER in input_case.metadata
        or RANGE_EQUITY_MARKER in normalized_case.metadata
        or (isinstance(report_metadata, dict) and RANGE_EQUITY_MARKER in report_metadata)
    )
    if not marker_present:
        return
    try:
        report_case = CaseInput.model_validate_json(
            _canonical_json_bytes(reconstructed_input),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.SCHEMA,
            "final_report.reconstructed_input",
            "bridge report input is not a strict CaseInput",
        ) from exc
    bindings = tuple(
        _binding_from_case(case) for case in (input_case, normalized_case, report_case)
    )
    if bindings[0] is None or bindings[1] is None or bindings[2] is None:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            f"metadata.{RANGE_EQUITY_MARKER}",
            "input, normalized case, and report must all carry the bridge binding",
        )
    if bindings[0] != bindings[1] or bindings[0] != bindings[2]:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            f"metadata.{RANGE_EQUITY_MARKER}",
            "persisted bridge bindings differ",
        )
    if input_case != normalized_case or input_case != report_case:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "final_report.reconstructed_input",
            "persisted bridge CaseInput projections differ",
        )


def verify_versioned_range_river_equity_binding_artifact(
    input_case: CaseInput,
    normalized_case: CaseInput,
    reconstructed_input: dict[str, object],
    artifact: VersionedRangeRiverEquityBindingV1 | None,
) -> None:
    """Bind the dedicated durable admission artifact to every case projection."""

    report_metadata = reconstructed_input.get("metadata")
    marker_present = (
        RANGE_EQUITY_MARKER in input_case.metadata
        or RANGE_EQUITY_MARKER in normalized_case.metadata
        or (isinstance(report_metadata, dict) and RANGE_EQUITY_MARKER in report_metadata)
    )
    if marker_present != (artifact is not None):
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "range_equity_binding.json",
            "bridge marker and durable binding artifact must appear together",
        )
    if artifact is None:
        return
    try:
        report_case = CaseInput.model_validate_json(
            _canonical_json_bytes(reconstructed_input),
            strict=True,
        )
    except (TypeError, ValueError) as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.SCHEMA,
            "final_report.reconstructed_input",
            "bridge report input is not a strict CaseInput",
        ) from exc
    bindings = (
        _binding_from_case(input_case),
        _binding_from_case(normalized_case),
        _binding_from_case(report_case),
    )
    if any(binding != artifact for binding in bindings):
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "range_equity_binding.json",
            "durable binding artifact differs from the persisted bridge cases",
        )


def _one_definition(case: CaseInput) -> tuple[Any, VersionedRangeDefinitionV1]:
    if case.hand is None:
        _fail(RangeEquityDiagnosticCode.HAND, "hand", "canonical hand is required")
    hand = case.hand
    if hand.game_type != "NLHE" or hand.format != "cash":
        _fail(
            RangeEquityDiagnosticCode.HAND,
            "hand.game_type",
            "only NLHE cash is admitted",
        )
    if hand.hero_player_id is None or len(hand.hero_cards) != 2 or len(hand.board) != 5:
        _fail(
            RangeEquityDiagnosticCode.HAND,
            "hand.hero_cards",
            "one identified Hero, two hole cards, and a five-card river board are required",
        )
    if len(hand.known_ranges) != 1 or not isinstance(
        hand.known_ranges[0], VersionedRangeDefinitionV1
    ):
        _fail(
            RangeEquityDiagnosticCode.RANGE,
            "hand.known_ranges",
            "exactly one VersionedRangeDefinitionV1 is required",
        )
    return hand, hand.known_ranges[0]


def _canonical_known_cards(hand: Any) -> tuple[tuple[str, str], tuple[str, str, str, str, str]]:
    try:
        hero = normalize_cards(hand.hero_cards)
        board = normalize_cards(hand.board)
        normalize_cards((*hero, *board))
    except ValueError as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.CARD,
            "hand.cards",
            "cards must be canonical, unique, and non-overlapping",
        ) from exc
    if hero != tuple(hand.hero_cards) or board != tuple(hand.board):
        _fail(
            RangeEquityDiagnosticCode.CARD,
            "hand.cards",
            "cards must already use canonical two-character notation",
        )
    return (hero[0], hero[1]), (board[0], board[1], board[2], board[3], board[4])


def _validate_decision_binding(hand: Any, definition: VersionedRangeDefinitionV1) -> None:
    conditions = definition.game_conditions
    if conditions.street != "river" or conditions.format != "cash":
        _fail(
            RangeEquityDiagnosticCode.DECISION,
            "range.game_conditions.street",
            "the bridge accepts only a river cash decision",
        )
    action_index = conditions.as_of_action_index
    if action_index < 1 or action_index > len(hand.actions):
        _fail(
            RangeEquityDiagnosticCode.DECISION,
            "range.game_conditions.as_of_action_index",
            "the bound action prefix must include the facing river action",
        )
    facing = hand.actions[action_index - 1]
    if (
        facing.street.value != "river"
        or facing.actor != definition.target_player_id
        or facing.action not in {"bet", "raise"}
    ):
        _fail(
            RangeEquityDiagnosticCode.DECISION,
            "hand.actions",
            "the final bound prefix action must be a target river bet or raise",
        )
    eligible = {player.player_id for player in hand.players}
    for action in hand.actions[:action_index]:
        if action.action == "fold":
            eligible.discard(action.actor)
    required = {hand.hero_player_id, definition.target_player_id}
    if eligible != required:
        _fail(
            RangeEquityDiagnosticCode.TARGET,
            "hand.actions",
            "only Hero and the range target may remain eligible at the decision point",
        )


def _oracle_totals(
    hero_cards: tuple[str, str],
    board: tuple[str, str, str, str, str],
    validation: RangeValidationResultV1,
) -> _OracleTotals:
    if validation.status != "success" or not validation.combos:
        _fail(RangeEquityDiagnosticCode.RANGE, "range_validate", "range validation failed")
    if validation.combo_count > RANGE_EQUITY_MAX_EVALUATIONS:
        _fail(
            RangeEquityDiagnosticCode.LIMIT,
            "range_validate.combo_count",
            f"river enumeration exceeds {RANGE_EQUITY_MAX_EVALUATIONS}",
        )
    hero_rank = evaluate_holdem(hero_cards, board)
    wins = ties = losses = 0
    win_weight = tie_weight = loss_weight = 0
    for combo in validation.combos:
        villain_rank = evaluate_holdem(combo.cards, board)
        if hero_rank > villain_rank:
            wins += 1
            win_weight += combo.weight_millionths
        elif hero_rank == villain_rank:
            ties += 1
            tie_weight += combo.weight_millionths
        else:
            losses += 1
            loss_weight += combo.weight_millionths
    return _OracleTotals(
        win_combo_count=wins,
        tie_combo_count=ties,
        loss_combo_count=losses,
        win_weight_millionths=win_weight,
        tie_weight_millionths=tie_weight,
        loss_weight_millionths=loss_weight,
        equity=Fraction(2 * win_weight + tie_weight, 2 * validation.total_weight_millionths),
    )


def _oracle_payload(
    *,
    hand: Any,
    definition: VersionedRangeDefinitionV1,
    validation: RangeValidationResultV1,
    hero_cards: tuple[str, str],
    board: tuple[str, str, str, str, str],
    oracle: _OracleTotals,
) -> dict[str, object]:
    if validation.condition_binding_sha256 is None or validation.canonical_combo_sha256 is None:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "range_validate",
            "successful validation lacks binding hashes",
        )
    return {
        "range_id": definition.range_id,
        "target_player_id": definition.target_player_id,
        "hero_player_id": hand.hero_player_id,
        "condition_binding_sha256": validation.condition_binding_sha256,
        "hero_cards": hero_cards,
        "board": board,
        "canonical_combo_sha256": validation.canonical_combo_sha256,
        "combo_count": validation.combo_count,
        "total_weight_millionths": validation.total_weight_millionths,
        "win_combo_count": oracle.win_combo_count,
        "tie_combo_count": oracle.tie_combo_count,
        "loss_combo_count": oracle.loss_combo_count,
        "win_weight_millionths": oracle.win_weight_millionths,
        "tie_weight_millionths": oracle.tie_weight_millionths,
        "loss_weight_millionths": oracle.loss_weight_millionths,
        "equity_numerator": oracle.equity.numerator,
        "equity_denominator": oracle.equity.denominator,
    }


def _candidate_without_marker(case: CaseInput) -> CaseInput:
    metadata = dict(case.metadata)
    metadata.pop(RANGE_EQUITY_MARKER, None)
    return CaseInput.model_validate(
        {**case.model_dump(mode="python"), "metadata": metadata},
        strict=True,
    )


def admit_versioned_range_river_equity(
    candidate: CaseInput,
) -> VersionedRangeRiverEquityAdmissionV1:
    """Admit one explicit, exact-only river range-equity calculation."""

    try:
        candidate_json = candidate.model_dump(mode="json")
        candidate_bytes = _canonical_json_bytes(candidate_json)
        candidate = CaseInput.model_validate_json(candidate_bytes, strict=True)
    except (
        PydanticSerializationError,
        RecursionError,
        TypeError,
        ValueError,
    ) as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.SCHEMA,
            "case",
            "the admitted candidate must be strict canonical JSON",
        ) from exc
    if not _nfc_tree(candidate_json):
        _fail(
            RangeEquityDiagnosticCode.SCHEMA,
            "case",
            "the admitted candidate must already use NFC strings and keys",
        )
    if redact_sensitive(candidate_json, enabled=True) != candidate_json:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "case",
            "the admitted candidate must remain unchanged by default artifact redaction",
        )
    if RANGE_EQUITY_MARKER in candidate.metadata:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            f"metadata.{RANGE_EQUITY_MARKER}",
            "callers may not supply an admission marker",
        )
    if candidate.kind != "calculation" or candidate.analysis_scope != "retrospective":
        _fail(
            RangeEquityDiagnosticCode.CASE,
            "case",
            "kind=calculation and analysis_scope=retrospective are required",
        )
    if (
        candidate.realized_result is not None
        or candidate.raw_text is not None
        or candidate.focal_decision is not None
    ):
        _fail(
            RangeEquityDiagnosticCode.CASE,
            "case.realized_result",
            "raw text, focal selectors, and realized results are outside this contract",
        )
    if candidate.claims or candidate.evidence or candidate.assumptions:
        _fail(
            RangeEquityDiagnosticCode.CASE,
            "case.claims",
            "claims, evidence, and free-form assumptions are not admitted",
        )
    if tuple(candidate.requested_tools) != RANGE_EQUITY_TOOL_PLAN[1:]:
        _fail(
            RangeEquityDiagnosticCode.TOOL_PLAN,
            "case.requested_tools",
            "requested tools must be exactly combos then holdem_equity",
        )
    if candidate.metadata not in ({}, {"tool_inputs": {}}):
        _fail(
            RangeEquityDiagnosticCode.TOOL_PLAN,
            "case.metadata",
            "manual tool inputs and unrelated metadata are not admitted",
        )
    hand, definition = _one_definition(candidate)
    hero_cards, board = _canonical_known_cards(hand)
    _validate_decision_binding(hand, definition)
    validation = validate_versioned_range(hand, definition)
    if validation.status != "success":
        codes = ",".join(diagnostic.code.value for diagnostic in validation.diagnostics)
        _fail(
            RangeEquityDiagnosticCode.RANGE,
            "hand.known_ranges",
            f"versioned range failed validation: {codes}",
        )
    oracle = _oracle_totals(hero_cards, board, validation)
    if validation.condition_binding_sha256 is None or validation.canonical_combo_sha256 is None:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "range_validate",
            "successful range validation lacks exact hashes",
        )
    candidate_sha256 = canonical_domain_sha256(
        CANDIDATE_HASH_DOMAIN,
        candidate.model_dump(mode="json"),
    )
    source_range_sha256 = canonical_domain_sha256(
        SOURCE_RANGE_HASH_DOMAIN,
        definition.model_dump(mode="json"),
    )
    oracle_sha256 = canonical_domain_sha256(
        ORACLE_HASH_DOMAIN,
        _oracle_payload(
            hand=hand,
            definition=definition,
            validation=validation,
            hero_cards=hero_cards,
            board=board,
            oracle=oracle,
        ),
    )
    binding_payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "poker-versioned-range-river-equity",
        "contract_version": "1.0.0",
        "hash_algorithm": "sha256",
        "range_id": definition.range_id,
        "target_player_id": definition.target_player_id,
        "hero_player_id": hand.hero_player_id,
        "as_of_action_index": definition.game_conditions.as_of_action_index,
        "source_range_sha256": source_range_sha256,
        "candidate_sha256": candidate_sha256,
        "condition_binding_sha256": validation.condition_binding_sha256,
        "canonical_combo_sha256": validation.canonical_combo_sha256,
        "oracle_sha256": oracle_sha256,
        "combo_count": validation.combo_count,
        "total_weight_millionths": validation.total_weight_millionths,
        "exact_evaluation_cap": RANGE_EQUITY_MAX_EVALUATIONS,
        "tool_plan": RANGE_EQUITY_TOOL_PLAN,
    }
    binding = VersionedRangeRiverEquityBindingV1.model_validate(
        {
            **binding_payload,
            "binding_sha256": canonical_domain_sha256(BINDING_HASH_DOMAIN, binding_payload),
        },
        strict=True,
    )
    admitted_metadata = dict(candidate.metadata)
    admitted_metadata[RANGE_EQUITY_MARKER] = binding.model_dump(mode="json")
    admitted_case = candidate.model_copy(update={"metadata": admitted_metadata})
    return VersionedRangeRiverEquityAdmissionV1(
        candidate=candidate,
        binding=binding,
        case=admitted_case,
    )


def exact_versioned_range_river_equity_oracle(
    case: CaseInput,
) -> VersionedRangeRiverEquityOracleProjectionV1:
    """Return the exact integer/rational oracle for one admitted P3-016B case."""

    binding = _binding_from_case(case)
    if binding is None:
        _fail(RangeEquityDiagnosticCode.SCHEMA, "case.metadata", "bridge marker is missing")
    admitted = admit_versioned_range_river_equity(_candidate_without_marker(case))
    if admitted.binding != binding or admitted.case != case:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "case.metadata",
            "admission binding does not match the canonical candidate",
        )
    hand, definition = _one_definition(case)
    hero_cards, board = _canonical_known_cards(hand)
    validation = validate_versioned_range(hand, definition)
    oracle = _oracle_totals(hero_cards, board, validation)
    oracle_payload = _oracle_payload(
        hand=hand,
        definition=definition,
        validation=validation,
        hero_cards=hero_cards,
        board=board,
        oracle=oracle,
    )
    oracle_sha256 = canonical_domain_sha256(ORACLE_HASH_DOMAIN, oracle_payload)
    if oracle_sha256 != binding.oracle_sha256 or hand.hero_player_id is None:
        _fail(
            RangeEquityDiagnosticCode.ORACLE,
            "metadata.versioned_range_river_equity.oracle_sha256",
            "admitted oracle hash differs from replay",
        )
    return VersionedRangeRiverEquityOracleProjectionV1(
        binding_sha256=binding.binding_sha256,
        oracle_sha256=oracle_sha256,
        range_id=definition.range_id,
        target_player_id=definition.target_player_id,
        hero_player_id=hand.hero_player_id,
        combo_count=validation.combo_count,
        total_weight_millionths=validation.total_weight_millionths,
        win_combo_count=oracle.win_combo_count,
        tie_combo_count=oracle.tie_combo_count,
        loss_combo_count=oracle.loss_combo_count,
        win_weight_millionths=oracle.win_weight_millionths,
        tie_weight_millionths=oracle.tie_weight_millionths,
        loss_weight_millionths=oracle.loss_weight_millionths,
        equity_numerator=oracle.equity.numerator,
        equity_denominator=oracle.equity.denominator,
    )


def expected_versioned_range_equity_input(
    case: CaseInput,
    validation: RangeValidationResultV1,
) -> dict[str, object]:
    """Derive the sole permitted legacy ``holdem_equity`` projection."""

    if case.hand is None or validation.canonical_notation is None:
        _fail(RangeEquityDiagnosticCode.CHAIN, "holdem_equity", "range projection is missing")
    hero_cards, board = _canonical_known_cards(case.hand)
    return {
        "hero_range": "".join(hero_cards),
        "villain_range": validation.canonical_notation,
        "board": list(board),
        "dead_cards": [],
        "game_type": "NLHE",
        "mode": "exact",
        "max_exact_evaluations": RANGE_EQUITY_MAX_EVALUATIONS,
    }


def _tool_result(
    tool_results: tuple[ToolResult, ...] | list[ToolResult],
    name: str,
) -> ToolResult:
    matches = [result for result in tool_results if result.tool_name == name]
    if len(matches) != 1:
        _fail(RangeEquityDiagnosticCode.CHAIN, name, "exactly one result is required")
    return matches[0]


def _strict_validation_output(value: object) -> RangeValidationResultV1:
    try:
        encoded = _canonical_json_bytes(value)
        observed = RangeValidationResultV1.model_validate_json(
            encoded,
            strict=True,
        )
        if _canonical_json_bytes(observed.model_dump(mode="python")) != encoded:
            raise ValueError("validation output is not in its unique canonical model form")
        return observed
    except (TypeError, ValueError) as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.CHAIN,
            "range_validate.output",
            "validation output is not a strict bridge result",
        ) from exc


def _strict_equity_output(value: object) -> HoldemEquityOutput:
    try:
        encoded = _canonical_json_bytes(value)
        observed = HoldemEquityOutput.model_validate_json(
            encoded,
            strict=True,
        )
        if _canonical_json_bytes(observed.model_dump(mode="python", exclude_none=True)) != encoded:
            raise ValueError("equity output is not in its unique canonical model form")
        return observed
    except (TypeError, ValueError) as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.CHAIN,
            "holdem_equity.output",
            "equity output is not a strict bridge result",
        ) from exc


def _exact_tool_result_envelope_matches(
    result: ToolResult,
    *,
    method: str | None,
) -> bool:
    """Match every deterministic top-level provenance field for an exact bridge tool."""

    try:
        contract = contract_by_name()[result.tool_name]
    except KeyError:
        return False
    expected_warnings = (
        [
            "legacy exactness='exact' is only a compatibility projection; "
            f"use numeric_exactness='{result.numeric_exactness.value}'"
        ]
        if result.numeric_exactness
        in {
            NumericalExactness.EXACT_UNDER_MODEL,
            NumericalExactness.FLOATING_VERIFIED,
        }
        else []
    )
    return (
        result.assumptions == list(contract.assumptions)
        and result.version == contract.version
        and result.model_qualifier == contract.model_qualifier
        and result.method == method
        and result.stochastic is None
        and result.seed is None
        and result.samples is None
        and result.iterations is None
        and result.confidence_interval is None
        and result.confidence_level is None
        and result.error_metadata is None
        and result.stopping_condition is None
        and result.warnings == expected_warnings
        and result.error is None
        and result.reproduce_command
        == (
            f"poker-deliberate calculate {result.tool_name} "
            "--analysis-scope retrospective --input <input.json>"
        )
    )


def _failed_exact_tool_result_envelope_matches(result: ToolResult) -> bool:
    """Match the deterministic failure envelope emitted by an exact bridge tool."""

    try:
        contract = contract_by_name()[result.tool_name]
    except KeyError:
        return False
    return (
        result.status is ToolStatus.FAILED
        and result.exactness is Exactness.UNAVAILABLE
        and result.numeric_exactness is NumericalExactness.UNAVAILABLE
        and result.contract_version == contract.contract_version
        and result.assumptions == list(contract.assumptions)
        and result.version == contract.version
        and result.model_qualifier is None
        and result.method is None
        and result.stochastic is None
        and result.seed is None
        and result.samples is None
        and result.iterations is None
        and result.confidence_interval is None
        and result.confidence_level is None
        and result.error_metadata is None
        and result.stopping_condition is None
        and result.verification is None
        and result.output == {}
        and result.warnings == []
        and result.error == versioned_range_bridge_failure_error(result.tool_name)
        and result.reproduce_command
        == (
            f"poker-deliberate calculate {result.tool_name} "
            "--analysis-scope retrospective --input <input.json>"
        )
    )


def _verification_matches_contract(result: ToolResult) -> None:
    try:
        contract = contract_by_name()[result.tool_name]
        evidence = contract.verify_floating(result.input, result.output)
    except (KeyError, TypeError, ValueError) as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.CHAIN,
            f"{result.tool_name}.output",
            "tool verification inputs are not a strict bridge result",
        ) from exc
    expected = VerificationMetadata(
        method="executed tool-specific invariant checks",
        checks=list(evidence.checks),
        observations=list(evidence.observations),
        tolerance=evidence.tolerance,
        passed=True,
    )
    if result.verification != expected:
        _fail(
            RangeEquityDiagnosticCode.REPLAY,
            f"{result.tool_name}.verification",
            "verification metadata differs from deterministic replay",
        )


def build_versioned_range_river_equity_result(
    case: CaseInput,
    tool_results: tuple[ToolResult, ...] | list[ToolResult],
) -> VersionedRangeRiverEquityResultV1:
    """Rebuild and verify the exact bridge result from persisted artifacts."""

    _require_supported_replay_trees(case, tool_results)
    binding = _binding_from_case(case)
    if binding is None:
        _fail(RangeEquityDiagnosticCode.SCHEMA, "case.metadata", "bridge marker is missing")
    candidate = _candidate_without_marker(case)
    admitted = admit_versioned_range_river_equity(candidate)
    if admitted.binding != binding or admitted.case != case:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "case.metadata",
            "admission binding does not match the canonical candidate",
        )
    names = tuple(result.tool_name for result in tool_results)
    if names != RANGE_EQUITY_TOOL_PLAN:
        _fail(
            RangeEquityDiagnosticCode.CHAIN,
            "tool_results",
            "tool results must be exactly range_validate, combos, holdem_equity",
        )
    try:
        verify_versioned_range_tool_chain(case, tool_results, run_status="completed")
    except (TypeError, ValueError) as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.CHAIN,
            "tool_results",
            "versioned range prerequisite replay failed",
        ) from exc
    hand, definition = _one_definition(case)
    hero_cards, board = _canonical_known_cards(hand)
    validation_result = _tool_result(tool_results, "range_validate")
    combos_result = _tool_result(tool_results, "combos")
    equity_result = _tool_result(tool_results, "holdem_equity")
    expected_validation_input = {
        "schema_version": "1.0.0",
        "hand": hand.model_dump(mode="json"),
        "range_definition": definition.model_dump(mode="json"),
    }
    if (
        validation_result.status is not ToolStatus.SUCCESS
        or validation_result.exactness is not Exactness.EXACT
        or validation_result.numeric_exactness is not NumericalExactness.EXACT
        or validation_result.contract_version != "2.0.0"
        or not _canonical_tree_matches(validation_result.input, expected_validation_input)
        or validation_result.verification is not None
        or not _exact_tool_result_envelope_matches(validation_result, method=None)
    ):
        _fail(
            RangeEquityDiagnosticCode.CHAIN,
            "range_validate",
            "validation result is not exactly bound",
        )
    observed_validation = _strict_validation_output(validation_result.output)
    expected_validation = validate_versioned_range(hand, definition)
    if (
        observed_validation != expected_validation
        or not _canonical_tree_matches(
            observed_validation.model_dump(mode="python"),
            expected_validation.model_dump(mode="python"),
        )
        or observed_validation.status != "success"
    ):
        _fail(
            RangeEquityDiagnosticCode.REPLAY,
            "range_validate.output",
            "validation output differs from deterministic replay",
        )
    expected_combos_input: dict[str, object] = {
        "range": observed_validation.canonical_notation,
        "dead_cards": [],
    }
    if (
        combos_result.status is not ToolStatus.SUCCESS
        or combos_result.exactness is not Exactness.EXACT
        or combos_result.numeric_exactness is not NumericalExactness.FLOATING_VERIFIED
        or combos_result.contract_version != "2.0.0"
        or not _canonical_tree_matches(combos_result.input, expected_combos_input)
        or not _exact_tool_result_envelope_matches(combos_result, method=None)
    ):
        _fail(
            RangeEquityDiagnosticCode.CHAIN,
            "combos",
            "combos result is not exactly bound",
        )
    _verification_matches_contract(combos_result)
    expected_equity_input = expected_versioned_range_equity_input(case, observed_validation)
    if (
        equity_result.status is not ToolStatus.SUCCESS
        or equity_result.exactness is not Exactness.EXACT
        or equity_result.numeric_exactness is not NumericalExactness.FLOATING_VERIFIED
        or equity_result.contract_version != "2.0.0"
        or not _canonical_tree_matches(equity_result.input, expected_equity_input)
        or not _exact_tool_result_envelope_matches(
            equity_result,
            method="exact_enumeration",
        )
    ):
        _fail(
            RangeEquityDiagnosticCode.CHAIN,
            "holdem_equity",
            "equity result is not the derived exact-only projection",
        )
    equity_output = _strict_equity_output(equity_result.output)
    oracle = _oracle_totals(hero_cards, board, observed_validation)
    if (
        equity_output.method != "exact_enumeration"
        or not equity_output.exact
        or equity_output.cards_to_come != 0
        or equity_output.evaluations != observed_validation.combo_count
        or equity_output.range_pair_count != observed_validation.combo_count
        or equity_output.unweighted_wins != oracle.win_combo_count
        or equity_output.unweighted_ties != oracle.tie_combo_count
        or equity_output.unweighted_losses != oracle.loss_combo_count
        or not close_ulps(
            equity_output.hero_equity,
            float(oracle.equity),
            ulps=128,
        )
    ):
        _fail(
            RangeEquityDiagnosticCode.ORACLE,
            "holdem_equity.output",
            "legacy output differs from the integer/rational river oracle",
        )
    _verification_matches_contract(equity_result)
    oracle_payload = _oracle_payload(
        hand=hand,
        definition=definition,
        validation=observed_validation,
        hero_cards=hero_cards,
        board=board,
        oracle=oracle,
    )
    oracle_sha256 = canonical_domain_sha256(ORACLE_HASH_DOMAIN, oracle_payload)
    if oracle_sha256 != binding.oracle_sha256:
        _fail(
            RangeEquityDiagnosticCode.ORACLE,
            "metadata.versioned_range_river_equity.oracle_sha256",
            "admitted oracle hash differs from replay",
        )
    if (
        binding.combo_count != observed_validation.combo_count
        or binding.total_weight_millionths != observed_validation.total_weight_millionths
        or binding.condition_binding_sha256 != observed_validation.condition_binding_sha256
        or binding.canonical_combo_sha256 != observed_validation.canonical_combo_sha256
    ):
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "metadata.versioned_range_river_equity",
            "validated range binding differs from admission",
        )
    result_payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "contract_id": "poker-versioned-range-river-equity",
        "contract_version": "1.0.0",
        "hash_algorithm": "sha256",
        "binding_sha256": binding.binding_sha256,
        "range_id": definition.range_id,
        "target_player_id": definition.target_player_id,
        "hero_player_id": hand.hero_player_id,
        "source": definition.source.model_dump(mode="json"),
        "game_conditions": definition.game_conditions.model_dump(mode="json"),
        "condition_binding_sha256": observed_validation.condition_binding_sha256,
        "canonical_combo_sha256": observed_validation.canonical_combo_sha256,
        "hero_cards": hero_cards,
        "board": board,
        "combo_count": observed_validation.combo_count,
        "total_weight_millionths": observed_validation.total_weight_millionths,
        "win_combo_count": oracle.win_combo_count,
        "tie_combo_count": oracle.tie_combo_count,
        "loss_combo_count": oracle.loss_combo_count,
        "win_weight_millionths": oracle.win_weight_millionths,
        "tie_weight_millionths": oracle.tie_weight_millionths,
        "loss_weight_millionths": oracle.loss_weight_millionths,
        "equity_numerator": oracle.equity.numerator,
        "equity_denominator": oracle.equity.denominator,
        "legacy_hero_equity": equity_output.hero_equity,
        "oracle_numeric_exactness": "exact",
        "legacy_numeric_exactness": "floating-verified",
        "validation_input_sha256": canonical_domain_sha256(
            VALIDATION_INPUT_HASH_DOMAIN, validation_result.input
        ),
        "validation_output_sha256": canonical_domain_sha256(
            VALIDATION_OUTPUT_HASH_DOMAIN, validation_result.output
        ),
        "combos_input_sha256": canonical_domain_sha256(
            COMBOS_INPUT_HASH_DOMAIN, combos_result.input
        ),
        "combos_output_sha256": canonical_domain_sha256(
            COMBOS_OUTPUT_HASH_DOMAIN, combos_result.output
        ),
        "equity_input_sha256": canonical_domain_sha256(
            EQUITY_INPUT_HASH_DOMAIN, equity_result.input
        ),
        "equity_output_sha256": canonical_domain_sha256(
            EQUITY_OUTPUT_HASH_DOMAIN, equity_result.output
        ),
        "oracle_sha256": oracle_sha256,
    }
    return VersionedRangeRiverEquityResultV1.model_validate(
        {
            **result_payload,
            "result_sha256": canonical_domain_sha256(RESULT_HASH_DOMAIN, result_payload),
        },
        strict=True,
    )


def verify_versioned_range_river_equity_tool_chain(
    case: CaseInput,
    tool_results: tuple[ToolResult, ...] | list[ToolResult],
    *,
    run_status: str = "completed",
) -> VersionedRangeRiverEquityResultV1 | None:
    """Replay a completed bridge or validate a fail-closed partial prefix."""

    _require_supported_replay_trees(case, tool_results)
    binding = _binding_from_case(case)
    if binding is None:
        return None
    if run_status not in {"completed", "failed_with_limitations"}:
        _fail(
            RangeEquityDiagnosticCode.REPLAY,
            "run_status",
            "the bridge supports only completed or failed_with_limitations replay",
        )
    try:
        verify_versioned_range_tool_chain(case, tool_results, run_status=run_status)
    except (TypeError, ValueError) as exc:
        raise VersionedRangeRiverEquityError(
            RangeEquityDiagnosticCode.CHAIN,
            "tool_results",
            "versioned range prerequisite replay failed",
        ) from exc
    admitted = admit_versioned_range_river_equity(_candidate_without_marker(case))
    if admitted.binding != binding or admitted.case != case:
        _fail(
            RangeEquityDiagnosticCode.PROVENANCE,
            "case.metadata",
            "stored bridge admission does not replay",
        )
    names = tuple(result.tool_name for result in tool_results)
    if run_status == "completed":
        return build_versioned_range_river_equity_result(case, tool_results)
    if names != RANGE_EQUITY_TOOL_PLAN[: len(names)]:
        _fail(
            RangeEquityDiagnosticCode.CHAIN,
            "tool_results",
            "failed run does not contain a valid tool-plan prefix",
        )
    if len(names) == len(RANGE_EQUITY_TOOL_PLAN) and all(
        result.status is ToolStatus.SUCCESS for result in tool_results
    ):
        return build_versioned_range_river_equity_result(case, tool_results)
    if names and names[-1] == "holdem_equity":
        prior = tool_results[:-1]
        if not all(result.status is ToolStatus.SUCCESS for result in prior):
            _fail(
                RangeEquityDiagnosticCode.CHAIN,
                "holdem_equity",
                "equity execution followed a failed prerequisite",
            )
        validation = _strict_validation_output(prior[0].output)
        expected = expected_versioned_range_equity_input(case, validation)
        equity = tool_results[-1]
        if not _canonical_tree_matches(
            equity.input, expected
        ) or not _failed_exact_tool_result_envelope_matches(equity):
            _fail(
                RangeEquityDiagnosticCode.CHAIN,
                "holdem_equity",
                "failed equity result lacks the derived input binding",
            )
    return None


__all__ = [
    "VersionedRangeRiverEquityAdmissionV1",
    "VersionedRangeRiverEquityError",
    "admit_versioned_range_river_equity",
    "build_versioned_range_river_equity_result",
    "exact_versioned_range_river_equity_oracle",
    "expected_versioned_range_equity_input",
    "verify_versioned_range_river_equity_binding_artifact",
    "verify_versioned_range_river_equity_case_correlation",
    "verify_versioned_range_river_equity_tool_chain",
    "versioned_range_river_equity_binding",
]
