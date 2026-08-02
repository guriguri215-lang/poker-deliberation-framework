"""Admission, exact-oracle, execution binding, and replay for P3-030C."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, NoReturn, TypeVar, cast
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from poker_deliberation.bounded_natural_language import (
    prepare_bounded_natural_language_intake,
    verify_bounded_candidate,
    verify_bounded_source_candidate,
)
from poker_deliberation.bounded_natural_language_models import (
    BOUNDED_NL_EXTRACTOR_ID,
    BOUNDED_NL_EXTRACTOR_VERSION,
    BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID,
    BoundedIntakeCandidateV1,
)
from poker_deliberation.bounded_river_call_ev_models import (
    AUTHORITY_HASH_DOMAIN,
    BINDING_HASH_DOMAIN,
    BOUNDED_CANDIDATE_HASH_DOMAIN,
    BOUNDED_RIVER_CALL_EV_MARKER,
    BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
    CALL_EV_MODEL_HASH_DOMAIN,
    CANDIDATE_HASH_DOMAIN,
    CONFIRMATION_HASH_DOMAIN,
    EQUITY_MODEL_HASH_DOMAIN,
    EXTRACTOR_HASH_DOMAIN,
    FOCAL_HASH_DOMAIN,
    RANGE_BINDING_HASH_DOMAIN,
    RANGE_DEFINITION_HASH_DOMAIN,
    RANGE_TARGET_HASH_DOMAIN,
    RESULT_HASH_DOMAIN,
    SOURCE_BINDINGS_HASH_DOMAIN,
    SOURCE_HASH_DOMAIN,
    TOOL_PLAN_HASH_DOMAIN,
    TOOL_RESULT_HASH_DOMAIN,
    BoundedRiverCallEvBindingV1,
    BoundedRiverCallEvCandidateProjectionV1,
    BoundedRiverCallEvCandidateV1,
    BoundedRiverCallEvConfirmationAuthorityV1,
    BoundedRiverCallEvConfirmationV1,
    BoundedRiverCallEvDiagnosticCode,
    BoundedRiverCallEvDiagnosticV1,
    BoundedRiverCallEvModelV1,
    BoundedRiverCallEvPreparationResultV1,
    BoundedRiverCallEvResultV1,
    BoundedRiverEquityModelV1,
    BoundedRiverRangeTargetBindingV1,
    BoundedRiverToolSupportV1,
    ExactRationalV1,
)
from poker_deliberation.range_equity import (
    VersionedRangeRiverEquityAdmissionV1,
    admit_versioned_range_river_equity,
    build_versioned_range_river_equity_result,
    exact_versioned_range_river_equity_oracle,
    verify_versioned_range_river_equity_tool_chain,
)
from poker_deliberation.range_equity_models import (
    RANGE_EQUITY_MARKER,
    RANGE_EQUITY_TOOL_PLAN,
    canonical_domain_sha256,
)
from poker_deliberation.range_grammar import action_prefix_sha256
from poker_deliberation.range_models import VersionedRangeDefinitionV1
from poker_deliberation.schemas import (
    Assumption,
    CanonicalHand,
    CaseInput,
    Claim,
    ConfidenceGrade,
    EpistemicLabel,
    Exactness,
    FinalReport,
    FocalDecision,
    NumericalExactness,
    Street,
    ToolResult,
    ToolStatus,
    VerificationMetadata,
)
from poker_deliberation.security import redact_sensitive
from poker_deliberation.storage.revision_canonical import canonical_json_bytes, validate_run_id
from poker_deliberation.tools.contracts import contract_by_name
from poker_deliberation.tools.hand_pot_ledger import (
    HandPotLedgerOutputV1,
    calculate_hand_pot_ledger,
)
from poker_deliberation.tools.hand_validator import validate_hand
from poker_deliberation.tools.numeric import close_ulps
from poker_deliberation.tools.pot_odds import pot_odds
from poker_deliberation.tools.strategy_math import raked_call_ev

_CONTROL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIRECT_TOOL_NAMES = (
    "hand_validator",
    "hand_pot_ledger",
    "pot_odds",
    "raked_call_ev",
)
_DIRECT_TOOL_MAX_DURATION_SECONDS = 30.0


class BoundedRiverCallEvError(ValueError):
    """Stable fail-closed integration error."""

    def __init__(
        self,
        code: BoundedRiverCallEvDiagnosticCode,
        field_path: str,
        detail: str = "refused",
    ) -> None:
        self.code = code
        self.field_path = field_path
        self.detail = detail
        super().__init__(f"{code.value}: {field_path}: {detail}")


def _fail(
    code: BoundedRiverCallEvDiagnosticCode,
    field_path: str,
    detail: str = "refused",
) -> NoReturn:
    raise BoundedRiverCallEvError(code, field_path, detail)


def _validate_control_id(value: object, field_path: str) -> str:
    if not isinstance(value, str) or _CONTROL_ID_RE.fullmatch(value) is None:
        _fail(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING, field_path)
    if redact_sensitive(value) != value:
        _fail(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_AUTHORITY, field_path)
    return value


_ModelT = TypeVar("_ModelT", bound=BaseModel)


def _rational(value: Fraction) -> ExactRationalV1:
    return ExactRationalV1(numerator=value.numerator, denominator=value.denominator)


def _domain_hash(domain: str, value: object) -> str:
    return canonical_domain_sha256(domain, value)


def _source_hash(source_bytes: bytes) -> str:
    return hashlib.sha256(SOURCE_HASH_DOMAIN.encode("ascii") + b"\0" + source_bytes).hexdigest()


def _model_hash(domain: str, value: BaseModel | object) -> str:
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return _domain_hash(domain, payload)


def _without_hash(model: BaseModel, field: str) -> dict[str, Any]:
    payload = model.model_dump(mode="json")
    payload.pop(field)
    return payload


def bounded_river_candidate_sha256(projection: BoundedRiverCallEvCandidateProjectionV1) -> str:
    return _model_hash(CANDIDATE_HASH_DOMAIN, projection)


def bounded_river_authority_sha256(
    authority: BoundedRiverCallEvConfirmationAuthorityV1,
) -> str:
    return _model_hash(AUTHORITY_HASH_DOMAIN, authority)


def bounded_river_confirmation_sha256(confirmation: BoundedRiverCallEvConfirmationV1) -> str:
    return _domain_hash(
        CONFIRMATION_HASH_DOMAIN,
        _without_hash(confirmation, "confirmation_sha256"),
    )


def bounded_river_binding_sha256(binding: BoundedRiverCallEvBindingV1) -> str:
    return _domain_hash(BINDING_HASH_DOMAIN, _without_hash(binding, "binding_sha256"))


def bounded_river_result_sha256(result: BoundedRiverCallEvResultV1) -> str:
    return _domain_hash(RESULT_HASH_DOMAIN, _without_hash(result, "result_sha256"))


def _strict_model(value: _ModelT, model: type[_ModelT], *, field_path: str) -> _ModelT:
    try:
        payload = canonical_json_bytes(value)
        return model.model_validate_json(payload, strict=True)
    except (TypeError, ValueError, ValidationError) as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.SCHEMA,
            field_path,
            "strict canonical JSON is required",
        ) from exc


def _range_equity_candidate(
    bounded_candidate: BoundedIntakeCandidateV1,
    definition: VersionedRangeDefinitionV1,
) -> CaseInput:
    base = bounded_candidate.projection.hand
    payload = base.model_dump(mode="json")
    if payload.get("known_ranges"):
        _fail(
            BoundedRiverCallEvDiagnosticCode.RANGE,
            "candidate.hand.known_ranges",
            "the bounded candidate must not already carry a range",
        )
    payload["known_ranges"] = [definition.model_dump(mode="json")]
    try:
        hand = CanonicalHand.model_validate(payload)
    except ValidationError as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.RANGE,
            "range_definition",
            "range cannot be attached to the canonical hand",
        ) from exc
    return CaseInput.model_validate(
        {
            "case_id": f"range-equity-{bounded_candidate.projection.intake_id}",
            "kind": "calculation",
            "hand": hand.model_dump(mode="json"),
            "analysis_scope": "retrospective",
            "objective": "exact_single_range_river_equity",
            "requested_tools": ["combos", "holdem_equity"],
            "metadata": {},
        }
    )


def _validate_bounded_river_boundary(
    bounded_candidate: BoundedIntakeCandidateV1,
    definition: VersionedRangeDefinitionV1,
) -> BoundedRiverRangeTargetBindingV1:
    projection = bounded_candidate.projection
    hand = projection.hand
    focal = projection.focal_decision
    if (
        hand.game_type != "NLHE"
        or hand.format != "cash"
        or hand.ante != 0
        or hand.rake not in (None, 0)
        or hand.tournament is not None
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.UNSUPPORTED, "candidate.hand")
    if focal.selector_street != "river" or len(hand.board) != 5 or len(hand.hero_cards) != 2:
        _fail(BoundedRiverCallEvDiagnosticCode.FOCAL, "candidate.focal_decision")
    if (
        focal.hero_action_index != len(hand.actions) - 1
        or focal.hero_response not in {"call", "fold"}
        or any(action.action in {"all_in", "post_ante"} for action in hand.actions)
    ):
        _fail(
            BoundedRiverCallEvDiagnosticCode.UNSUPPORTED,
            "candidate.hand.actions",
            "the focal response must be the final non-all-in river action",
        )
    facing = hand.actions[focal.facing_action_index]
    hero_response = hand.actions[focal.hero_action_index]
    if (
        facing.street is not Street.RIVER
        or facing.actor != focal.selector_actor
        or facing.action not in {"bet", "raise"}
        or hero_response.actor != hand.hero_player_id
        or hero_response.action != focal.hero_response
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.FOCAL, "candidate.hand.actions")
    conditions = definition.game_conditions
    if (
        definition.target_player_id != facing.actor
        or conditions.street != "river"
        or conditions.format != "cash"
        or conditions.game_type != "NLHE"
        or conditions.table_size != hand.table_size
        or conditions.as_of_action_index != focal.facing_action_index + 1
        or conditions.action_prefix_sha256
        != action_prefix_sha256(hand, conditions.as_of_action_index)
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.TARGET, "range_definition.game_conditions")
    eligible = {player.player_id for player in hand.players}
    for action in hand.actions[: conditions.as_of_action_index]:
        if action.action == "fold":
            eligible.discard(action.actor)
    required = {hand.hero_player_id, definition.target_player_id}
    if None in required or eligible != required:
        _fail(BoundedRiverCallEvDiagnosticCode.TARGET, "candidate.hand.actions")
    assert hand.hero_player_id is not None
    return BoundedRiverRangeTargetBindingV1(
        hero_player_id=hand.hero_player_id,
        facing_actor=facing.actor,
        target_player_id=definition.target_player_id,
        facing_action_index=focal.facing_action_index,
        as_of_action_index=conditions.as_of_action_index,
        action_prefix_sha256=conditions.action_prefix_sha256,
        eligible_player_ids=(hand.hero_player_id, definition.target_player_id),
    )


def _exact_models(
    bounded_candidate: BoundedIntakeCandidateV1,
    range_admission: VersionedRangeRiverEquityAdmissionV1,
) -> tuple[BoundedRiverEquityModelV1, BoundedRiverCallEvModelV1]:
    oracle = exact_versioned_range_river_equity_oracle(range_admission.case)
    equity = Fraction(oracle.equity_numerator, oracle.equity_denominator)
    plan = bounded_candidate.projection.tool_plan
    chip_unit = Fraction(Decimal(plan.ledger_profile.chip_unit))
    required = Fraction(plan.call_cost_units, plan.contestable_pot_units)
    call_ev = equity * plan.contestable_pot_units - plan.call_cost_units
    equity_model = BoundedRiverEquityModelV1(
        binding_sha256=range_admission.binding.binding_sha256,
        oracle_sha256=range_admission.binding.oracle_sha256,
        equity=_rational(equity),
        source_content_status=(
            range_admission.case.hand.known_ranges[0].source.content_status
            if range_admission.case.hand is not None
            and isinstance(range_admission.case.hand.known_ranges[0], VersionedRangeDefinitionV1)
            else "ASSUMPTION"
        ),
    )
    call_model = BoundedRiverCallEvModelV1(
        chip_unit=_rational(chip_unit),
        pot_before_bet_units=plan.pot_before_bet_units,
        opponent_bet_units=plan.opponent_bet_units,
        pot_after_bet_units=plan.pot_before_bet_units + plan.opponent_bet_units,
        call_cost_units=plan.call_cost_units,
        contestable_pot_units=plan.contestable_pot_units,
        equity=_rational(equity),
        required_equity=_rational(required),
        call_ev_units=_rational(call_ev),
        call_ev_amount=_rational(call_ev * chip_unit),
        fold_ev_units=_rational(Fraction(0)),
        call_minus_fold_ev_units=_rational(call_ev),
        action_comparison="call" if call_ev > 0 else "fold" if call_ev < 0 else "tie",
    )
    return equity_model, call_model


def _candidate_from_components(
    source_bytes: bytes,
    bounded_candidate: BoundedIntakeCandidateV1,
    definition: VersionedRangeDefinitionV1,
) -> BoundedRiverCallEvCandidateV1:
    bounded_candidate = verify_bounded_candidate(bounded_candidate)
    definition = _strict_model(
        definition,
        VersionedRangeDefinitionV1,
        field_path="range_definition",
    )
    target = _validate_bounded_river_boundary(bounded_candidate, definition)
    range_admission = admit_versioned_range_river_equity(
        _range_equity_candidate(bounded_candidate, definition)
    )
    oracle = exact_versioned_range_river_equity_oracle(range_admission.case)
    equity_model, call_model = _exact_models(bounded_candidate, range_admission)
    bounded_projection = bounded_candidate.projection
    tool_plan_payload = {
        "ordered_tools": BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
        "bounded_tool_plan_sha256": bounded_projection.tool_plan.tool_plan_sha256,
        "range_equity_binding_sha256": range_admission.binding.binding_sha256,
        "range_equity_tool_plan": RANGE_EQUITY_TOOL_PLAN,
        "raked_call_ev_input": {
            "equity": float(
                Fraction(equity_model.equity.numerator, equity_model.equity.denominator)
            ),
            "pot_after_bet": float(
                Fraction(call_model.pot_after_bet_units)
                * Fraction(call_model.chip_unit.numerator, call_model.chip_unit.denominator)
            ),
            "call_cost": float(
                Fraction(call_model.call_cost_units)
                * Fraction(call_model.chip_unit.numerator, call_model.chip_unit.denominator)
            ),
            "rake_percent": 0.0,
        },
    }
    projection = BoundedRiverCallEvCandidateProjectionV1(
        intake_id=bounded_projection.intake_id,
        bounded_candidate=bounded_candidate,
        range_definition=definition,
        range_target=target,
        range_equity_binding=range_admission.binding,
        range_equity_oracle=oracle,
        equity_model=equity_model,
        call_ev_model=call_model,
        source_sha256=_source_hash(source_bytes),
        bounded_candidate_sha256=_model_hash(
            BOUNDED_CANDIDATE_HASH_DOMAIN,
            bounded_candidate,
        ),
        source_bindings_sha256=_domain_hash(
            SOURCE_BINDINGS_HASH_DOMAIN,
            [item.model_dump(mode="json") for item in bounded_projection.source_bindings],
        ),
        focal_sha256=_model_hash(FOCAL_HASH_DOMAIN, bounded_projection.focal_decision),
        extractor_sha256=_domain_hash(
            EXTRACTOR_HASH_DOMAIN,
            {
                "extractor_id": BOUNDED_NL_EXTRACTOR_ID,
                "extractor_version": BOUNDED_NL_EXTRACTOR_VERSION,
                "bounded_extractor_sha256": bounded_projection.extractor_sha256,
            },
        ),
        tool_plan_sha256=_domain_hash(TOOL_PLAN_HASH_DOMAIN, tool_plan_payload),
        range_definition_sha256=_model_hash(RANGE_DEFINITION_HASH_DOMAIN, definition),
        range_target_sha256=_model_hash(RANGE_TARGET_HASH_DOMAIN, target),
        range_binding_sha256=_model_hash(RANGE_BINDING_HASH_DOMAIN, range_admission.binding),
        equity_model_sha256=_model_hash(EQUITY_MODEL_HASH_DOMAIN, equity_model),
        call_ev_model_sha256=_model_hash(CALL_EV_MODEL_HASH_DOMAIN, call_model),
    )
    return BoundedRiverCallEvCandidateV1(
        projection=projection,
        candidate_sha256=bounded_river_candidate_sha256(projection),
    )


def prepare_bounded_river_call_ev_intake(
    source_bytes: bytes,
    range_definition: VersionedRangeDefinitionV1,
    *,
    intake_id: str,
    source_id: str,
    source_kind: Literal["user_supplied", "repository_fixture"],
    license_classification: Literal[
        "user_supplied_private_analysis",
        "repository_owned_mit",
    ],
    usage_classification: Literal["local_analysis_only", "redistribution_allowed"],
    classification: Literal["internal", "public"],
) -> BoundedRiverCallEvPreparationResultV1:
    """Parse bounded Japanese source and bind one separate explicit range."""

    bounded = prepare_bounded_natural_language_intake(
        source_bytes,
        intake_id=intake_id,
        source_id=source_id,
        source_kind=source_kind,
        license_classification=license_classification,
        usage_classification=usage_classification,
        classification=classification,
    )
    if bounded.status != "ready" or bounded.source is None or bounded.candidate is None:
        return BoundedRiverCallEvPreparationResultV1(
            status="blocked",
            source=bounded.source,
            diagnostics=(
                BoundedRiverCallEvDiagnosticV1(
                    code=BoundedRiverCallEvDiagnosticCode.SOURCE,
                    field_path="source",
                ),
            ),
        )
    try:
        candidate = _candidate_from_components(source_bytes, bounded.candidate, range_definition)
    except (BoundedRiverCallEvError, ValueError, ValidationError) as exc:
        code = (
            exc.code
            if isinstance(exc, BoundedRiverCallEvError)
            else BoundedRiverCallEvDiagnosticCode.RANGE
        )
        field_path = (
            exc.field_path if isinstance(exc, BoundedRiverCallEvError) else "range_definition"
        )
        return BoundedRiverCallEvPreparationResultV1(
            status="blocked",
            source=bounded.source,
            diagnostics=(BoundedRiverCallEvDiagnosticV1(code=code, field_path=field_path),),
        )
    return BoundedRiverCallEvPreparationResultV1(
        status="ready",
        source=bounded.source,
        candidate=candidate,
    )


def verify_bounded_river_call_ev_candidate(
    candidate: BoundedRiverCallEvCandidateV1,
) -> BoundedRiverCallEvCandidateV1:
    strict = _strict_model(candidate, BoundedRiverCallEvCandidateV1, field_path="candidate")
    projection = strict.projection
    if strict.candidate_sha256 != bounded_river_candidate_sha256(projection):
        _fail(BoundedRiverCallEvDiagnosticCode.CANDIDATE, "candidate.candidate_sha256")
    bounded = verify_bounded_candidate(projection.bounded_candidate)
    definition = _strict_model(
        projection.range_definition,
        VersionedRangeDefinitionV1,
        field_path="candidate.projection.range_definition",
    )
    target = _validate_bounded_river_boundary(bounded, definition)
    range_admission = admit_versioned_range_river_equity(
        _range_equity_candidate(bounded, definition)
    )
    oracle = exact_versioned_range_river_equity_oracle(range_admission.case)
    equity_model, call_model = _exact_models(bounded, range_admission)
    bounded_projection = bounded.projection
    tool_plan_payload = {
        "ordered_tools": BOUNDED_RIVER_CALL_EV_TOOL_ORDER,
        "bounded_tool_plan_sha256": bounded_projection.tool_plan.tool_plan_sha256,
        "range_equity_binding_sha256": range_admission.binding.binding_sha256,
        "range_equity_tool_plan": RANGE_EQUITY_TOOL_PLAN,
        "raked_call_ev_input": {
            "equity": float(
                Fraction(equity_model.equity.numerator, equity_model.equity.denominator)
            ),
            "pot_after_bet": float(
                Fraction(call_model.pot_after_bet_units)
                * Fraction(call_model.chip_unit.numerator, call_model.chip_unit.denominator)
            ),
            "call_cost": float(
                Fraction(call_model.call_cost_units)
                * Fraction(call_model.chip_unit.numerator, call_model.chip_unit.denominator)
            ),
            "rake_percent": 0.0,
        },
    }
    expected_values = {
        "bounded_candidate": bounded,
        "range_definition": definition,
        "range_target": target,
        "range_equity_binding": range_admission.binding,
        "range_equity_oracle": oracle,
        "equity_model": equity_model,
        "call_ev_model": call_model,
        "bounded_candidate_sha256": _model_hash(BOUNDED_CANDIDATE_HASH_DOMAIN, bounded),
        "source_bindings_sha256": _domain_hash(
            SOURCE_BINDINGS_HASH_DOMAIN,
            [item.model_dump(mode="json") for item in bounded_projection.source_bindings],
        ),
        "focal_sha256": _model_hash(FOCAL_HASH_DOMAIN, bounded_projection.focal_decision),
        "extractor_sha256": _domain_hash(
            EXTRACTOR_HASH_DOMAIN,
            {
                "extractor_id": BOUNDED_NL_EXTRACTOR_ID,
                "extractor_version": BOUNDED_NL_EXTRACTOR_VERSION,
                "bounded_extractor_sha256": bounded_projection.extractor_sha256,
            },
        ),
        "tool_plan_sha256": _domain_hash(TOOL_PLAN_HASH_DOMAIN, tool_plan_payload),
        "range_definition_sha256": _model_hash(RANGE_DEFINITION_HASH_DOMAIN, definition),
        "range_target_sha256": _model_hash(RANGE_TARGET_HASH_DOMAIN, target),
        "range_binding_sha256": _model_hash(RANGE_BINDING_HASH_DOMAIN, range_admission.binding),
        "equity_model_sha256": _model_hash(EQUITY_MODEL_HASH_DOMAIN, equity_model),
        "call_ev_model_sha256": _model_hash(CALL_EV_MODEL_HASH_DOMAIN, call_model),
    }
    if any(getattr(projection, name) != value for name, value in expected_values.items()):
        _fail(BoundedRiverCallEvDiagnosticCode.CANDIDATE, "candidate.projection")
    return strict


def create_bounded_river_call_ev_authority(
    *,
    authority_id: str,
    authority_kind: str,
    authentication: str,
) -> BoundedRiverCallEvConfirmationAuthorityV1:
    _validate_control_id(authority_id, "confirmation.authority.authority_id")
    try:
        return BoundedRiverCallEvConfirmationAuthorityV1.model_validate(
            {
                "authority_id": authority_id,
                "authority_kind": authority_kind,
                "authentication": authentication,
            },
            strict=True,
        )
    except ValidationError as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.CONFIRMATION_AUTHORITY,
            "confirmation.authority",
        ) from exc


def _candidate_hash_tuple(candidate: BoundedRiverCallEvCandidateV1) -> tuple[str, ...]:
    p = candidate.projection
    return (
        p.source_sha256,
        p.bounded_candidate_sha256,
        p.source_bindings_sha256,
        p.focal_sha256,
        p.extractor_sha256,
        p.tool_plan_sha256,
        p.range_definition_sha256,
        p.range_target_sha256,
        p.range_binding_sha256,
        p.equity_model_sha256,
        p.call_ev_model_sha256,
        candidate.candidate_sha256,
    )


def create_bounded_river_call_ev_confirmation(
    candidate: BoundedRiverCallEvCandidateV1,
    *,
    run_id: str,
    confirmation_id: str,
    idempotency_key: str,
    authority: BoundedRiverCallEvConfirmationAuthorityV1,
    expected_hashes: tuple[str, ...],
    confirmed_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> BoundedRiverCallEvConfirmationV1:
    candidate = _strict_model(candidate, BoundedRiverCallEvCandidateV1, field_path="candidate")
    authority = _strict_model(
        authority,
        BoundedRiverCallEvConfirmationAuthorityV1,
        field_path="confirmation.authority",
    )
    for value, field_path in (
        (run_id, "confirmation.run_id"),
        (confirmation_id, "confirmation.confirmation_id"),
        (idempotency_key, "confirmation.idempotency_key"),
        (authority.authority_id, "confirmation.authority.authority_id"),
    ):
        _validate_control_id(value, field_path)
    try:
        validate_run_id(run_id)
    except ValueError as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING,
            "confirmation.run_id",
        ) from exc
    if expected_hashes != _candidate_hash_tuple(candidate):
        _fail(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING, "confirmation.expected_hashes")
    confirmed = confirmed_at or datetime.now(UTC)
    expiry = expires_at or confirmed + timedelta(hours=24)
    p = candidate.projection
    provisional = BoundedRiverCallEvConfirmationV1(
        run_id=run_id,
        intake_id=p.intake_id,
        confirmation_id=confirmation_id,
        idempotency_key=idempotency_key,
        source_sha256=p.source_sha256,
        bounded_candidate_sha256=p.bounded_candidate_sha256,
        source_bindings_sha256=p.source_bindings_sha256,
        focal_sha256=p.focal_sha256,
        extractor_sha256=p.extractor_sha256,
        tool_plan_sha256=p.tool_plan_sha256,
        range_definition_sha256=p.range_definition_sha256,
        range_target_sha256=p.range_target_sha256,
        range_binding_sha256=p.range_binding_sha256,
        equity_model_sha256=p.equity_model_sha256,
        call_ev_model_sha256=p.call_ev_model_sha256,
        candidate_sha256=candidate.candidate_sha256,
        authority=authority,
        authority_snapshot_sha256=bounded_river_authority_sha256(authority),
        confirmed_at=confirmed,
        expires_at=expiry,
        confirmation_sha256="0" * 64,
    )
    return provisional.model_copy(
        update={"confirmation_sha256": bounded_river_confirmation_sha256(provisional)}
    )


def _binding_from_confirmation(
    confirmation: BoundedRiverCallEvConfirmationV1,
) -> BoundedRiverCallEvBindingV1:
    provisional = BoundedRiverCallEvBindingV1.model_construct(
        run_id=confirmation.run_id,
        intake_id=confirmation.intake_id,
        source_sha256=confirmation.source_sha256,
        bounded_candidate_sha256=confirmation.bounded_candidate_sha256,
        source_bindings_sha256=confirmation.source_bindings_sha256,
        focal_sha256=confirmation.focal_sha256,
        extractor_sha256=confirmation.extractor_sha256,
        tool_plan_sha256=confirmation.tool_plan_sha256,
        range_definition_sha256=confirmation.range_definition_sha256,
        range_target_sha256=confirmation.range_target_sha256,
        range_binding_sha256=confirmation.range_binding_sha256,
        equity_model_sha256=confirmation.equity_model_sha256,
        call_ev_model_sha256=confirmation.call_ev_model_sha256,
        candidate_sha256=confirmation.candidate_sha256,
        confirmation_sha256=confirmation.confirmation_sha256,
        binding_sha256="0" * 64,
    )
    final = provisional.model_copy(
        update={"binding_sha256": bounded_river_binding_sha256(provisional)}
    )
    return BoundedRiverCallEvBindingV1.model_validate(final, strict=True)


def _outer_case(
    candidate: BoundedRiverCallEvCandidateV1,
    binding: BoundedRiverCallEvBindingV1,
    range_admission: VersionedRangeRiverEquityAdmissionV1,
) -> CaseInput:
    p = candidate.projection
    hand = range_admission.case.hand
    if hand is None:
        _fail(BoundedRiverCallEvDiagnosticCode.RANGE, "candidate.hand")
    focal = p.bounded_candidate.projection.focal_decision
    call_model = p.call_ev_model
    chip_unit = Fraction(call_model.chip_unit.numerator, call_model.chip_unit.denominator)
    metadata = {
        BOUNDED_RIVER_CALL_EV_MARKER: binding.model_dump(mode="json"),
        RANGE_EQUITY_MARKER: range_admission.binding.model_dump(mode="json"),
        "tool_inputs": {
            "hand_pot_ledger": {
                "schema_version": "1.0.0",
                "rule_profile": p.bounded_candidate.projection.tool_plan.ledger_profile.model_dump(
                    mode="json"
                ),
            },
            "pot_odds": p.bounded_candidate.projection.tool_plan.pot_odds_input.model_dump(
                mode="json"
            ),
            "raked_call_ev": {
                "equity": float(
                    Fraction(call_model.equity.numerator, call_model.equity.denominator)
                ),
                "pot_after_bet": float(call_model.pot_after_bet_units * chip_unit),
                "call_cost": float(call_model.call_cost_units * chip_unit),
                "rake_percent": 0.0,
            },
        },
    }
    claims: list[Claim] = []
    assumptions: list[Assumption] = []
    if p.range_definition.source.content_status == "USER_CLAIM":
        claims.append(
            Claim(
                claim_id=f"range-source-{p.intake_id}",
                text="明示された相手レンジはユーザー提供情報です。",
                label=EpistemicLabel.USER_CLAIM,
                confidence=ConfidenceGrade.C,
                limitations=["実戦での正確性は未検証です。"],
            )
        )
    else:
        assumptions.append(
            Assumption(
                assumption_id=f"range-source-{p.intake_id}",
                text="明示された相手レンジを計算モデルの仮定として使用します。",
                reason="自然言語から推測せず、別入力のVersionedRangeDefinitionV1を使用するため。",
                sensitivity="レンジを変更するとequityとcall EVが変化します。",
            )
        )
    return CaseInput(
        case_id=f"bounded-river-call-ev-{p.intake_id}",
        kind="hand",
        raw_text=None,
        hand=hand,
        focal_decision=FocalDecision(
            street=Street.RIVER,
            action_index=focal.hero_action_index,
            actor=hand.hero_player_id or "Hero",
        ),
        analysis_scope="retrospective",
        claims=claims,
        assumptions=assumptions,
        objective="bounded_river_call_or_fold_exact_ev",
        requested_tools=list(BOUNDED_RIVER_CALL_EV_TOOL_ORDER),
        metadata=metadata,
    )


@dataclass(frozen=True, slots=True)
class BoundedRiverCallEvAdmission:
    source_bytes: bytes
    candidate: BoundedRiverCallEvCandidateV1
    confirmation: BoundedRiverCallEvConfirmationV1
    binding: BoundedRiverCallEvBindingV1
    admitted_at: datetime
    range_equity_admission: VersionedRangeRiverEquityAdmissionV1
    case: CaseInput


def _admit_at(
    source_bytes: bytes,
    candidate: BoundedRiverCallEvCandidateV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
    *,
    admitted_at: datetime,
) -> BoundedRiverCallEvAdmission:
    candidate = _strict_model(candidate, BoundedRiverCallEvCandidateV1, field_path="candidate")
    confirmation = _strict_model(
        confirmation,
        BoundedRiverCallEvConfirmationV1,
        field_path="confirmation",
    )
    p = candidate.projection
    try:
        replay_candidate = verify_bounded_source_candidate(
            source_bytes,
            p.bounded_candidate,
        )
    except ValueError:
        _fail(BoundedRiverCallEvDiagnosticCode.SOURCE, "source")
    if replay_candidate != p.bounded_candidate or _source_hash(source_bytes) != p.source_sha256:
        _fail(BoundedRiverCallEvDiagnosticCode.SOURCE, "source")
    for value, field_path in (
        (confirmation.run_id, "confirmation.run_id"),
        (confirmation.intake_id, "confirmation.intake_id"),
        (confirmation.confirmation_id, "confirmation.confirmation_id"),
        (confirmation.idempotency_key, "confirmation.idempotency_key"),
        (confirmation.authority.authority_id, "confirmation.authority.authority_id"),
    ):
        _validate_control_id(value, field_path)
    expected_candidate = _candidate_from_components(
        source_bytes,
        p.bounded_candidate,
        p.range_definition,
    )
    if expected_candidate != candidate:
        _fail(BoundedRiverCallEvDiagnosticCode.CANDIDATE, "candidate")
    if (
        confirmation.confirmation_sha256 != bounded_river_confirmation_sha256(confirmation)
        or confirmation.authority_snapshot_sha256
        != bounded_river_authority_sha256(confirmation.authority)
        or _candidate_hash_tuple(candidate)
        != (
            confirmation.source_sha256,
            confirmation.bounded_candidate_sha256,
            confirmation.source_bindings_sha256,
            confirmation.focal_sha256,
            confirmation.extractor_sha256,
            confirmation.tool_plan_sha256,
            confirmation.range_definition_sha256,
            confirmation.range_target_sha256,
            confirmation.range_binding_sha256,
            confirmation.equity_model_sha256,
            confirmation.call_ev_model_sha256,
            confirmation.candidate_sha256,
        )
        or confirmation.intake_id != p.intake_id
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING, "confirmation")
    if admitted_at.tzinfo is None or admitted_at.utcoffset() is None:
        _fail(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_BINDING, "admission.admitted_at")
    if confirmation.confirmed_at > admitted_at or admitted_at > confirmation.expires_at:
        _fail(BoundedRiverCallEvDiagnosticCode.CONFIRMATION_EXPIRED, "confirmation.expires_at")
    range_admission = admit_versioned_range_river_equity(
        _range_equity_candidate(p.bounded_candidate, p.range_definition)
    )
    if range_admission.binding != p.range_equity_binding:
        _fail(BoundedRiverCallEvDiagnosticCode.RANGE, "candidate.range_equity_binding")
    binding = _binding_from_confirmation(confirmation)
    case = _outer_case(candidate, binding, range_admission)
    return BoundedRiverCallEvAdmission(
        source_bytes=source_bytes,
        candidate=candidate,
        confirmation=confirmation,
        binding=binding,
        admitted_at=admitted_at,
        range_equity_admission=range_admission,
        case=case,
    )


def admit_bounded_river_call_ev_review(
    source_bytes: bytes,
    candidate: BoundedRiverCallEvCandidateV1,
    confirmation: BoundedRiverCallEvConfirmationV1,
) -> BoundedRiverCallEvAdmission:
    return _admit_at(source_bytes, candidate, confirmation, admitted_at=datetime.now(UTC))


def bounded_river_call_ev_binding(case: CaseInput) -> BoundedRiverCallEvBindingV1 | None:
    if BOUNDED_RIVER_CALL_EV_MARKER not in case.metadata:
        return None
    try:
        return BoundedRiverCallEvBindingV1.model_validate_json(
            canonical_json_bytes(case.metadata[BOUNDED_RIVER_CALL_EV_MARKER]),
            strict=True,
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.SCHEMA,
            f"metadata.{BOUNDED_RIVER_CALL_EV_MARKER}",
        ) from exc


def expected_bounded_river_tool_inputs(
    admission: BoundedRiverCallEvAdmission,
) -> dict[str, dict[str, object]]:
    case = admission.case
    hand = case.hand
    raw = case.metadata.get("tool_inputs")
    if hand is None or not isinstance(raw, dict):
        _fail(BoundedRiverCallEvDiagnosticCode.TOOL_PLAN, "case.metadata.tool_inputs")
    ledger = raw.get("hand_pot_ledger")
    pot_odds = raw.get("pot_odds")
    raked = raw.get("raked_call_ev")
    if not all(isinstance(item, dict) for item in (ledger, pot_odds, raked)):
        _fail(BoundedRiverCallEvDiagnosticCode.TOOL_PLAN, "case.metadata.tool_inputs")
    assert isinstance(ledger, dict)
    assert isinstance(pot_odds, dict)
    assert isinstance(raked, dict)
    return {
        "hand_validator": hand.model_dump(mode="json"),
        "hand_pot_ledger": {**ledger, "hand": hand.model_dump(mode="json")},
        "pot_odds": pot_odds,
        "raked_call_ev": raked,
    }


def _tool_hash(result: ToolResult) -> str:
    return _model_hash(TOOL_RESULT_HASH_DOMAIN, result)


def _direct_tool_oracles(
    admission: BoundedRiverCallEvAdmission,
) -> tuple[dict[str, dict[str, object]], dict[str, dict[str, object]]]:
    expected_inputs = expected_bounded_river_tool_inputs(admission)
    hand = admission.case.hand
    if hand is None:
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "case.hand")
    pot_input = expected_inputs["pot_odds"]
    call_ev_input = expected_inputs["raked_call_ev"]
    ledger_output = calculate_hand_pot_ledger(expected_inputs["hand_pot_ledger"])
    ledger_output = HandPotLedgerOutputV1.model_validate(ledger_output).model_dump(mode="json")
    oracles: dict[str, dict[str, object]] = {
        "hand_validator": validate_hand(hand),
        "hand_pot_ledger": cast(dict[str, object], ledger_output),
        "pot_odds": cast(
            dict[str, object],
            pot_odds(
                pot_before_bet=cast(float, pot_input["pot_before_bet"]),
                opponent_bet=cast(float, pot_input["opponent_bet"]),
                call_cost=cast(float, pot_input["call_cost"]),
                expected_rake=cast(float, pot_input["expected_rake"]),
            ),
        ),
        "raked_call_ev": cast(
            dict[str, object],
            raked_call_ev(
                equity=cast(float, call_ev_input["equity"]),
                pot_after_bet=cast(float, call_ev_input["pot_after_bet"]),
                call_cost=cast(float, call_ev_input["call_cost"]),
                rake_percent=cast(float, call_ev_input["rake_percent"]),
                rake_cap=(
                    None
                    if call_ev_input.get("rake_cap") is None
                    else cast(float, call_ev_input["rake_cap"])
                ),
            ),
        ),
    }
    return expected_inputs, oracles


def _expected_direct_tool_warnings(
    output: dict[str, object],
    numeric_exactness: NumericalExactness,
) -> list[str]:
    warnings: list[str] = []
    warning = output.get("warning")
    if warning:
        warnings.append(str(warning))
    many = output.get("warnings")
    if isinstance(many, list):
        warnings.extend(str(item) for item in many)
    if numeric_exactness in {
        NumericalExactness.EXACT_UNDER_MODEL,
        NumericalExactness.FLOATING_VERIFIED,
    }:
        warnings.append(
            "legacy exactness='exact' is only a compatibility projection; "
            f"use numeric_exactness='{numeric_exactness.value}'"
        )
    return warnings


def _verify_successful_direct_tool_result(
    admission: BoundedRiverCallEvAdmission,
    result: ToolResult,
    *,
    expected_input: dict[str, object],
    expected_output: dict[str, object],
) -> None:
    name = result.tool_name
    if name not in _DIRECT_TOOL_NAMES:
        _fail(BoundedRiverCallEvDiagnosticCode.TOOL_PLAN, "tool_results")
    contract = contract_by_name()[name]
    try:
        contract.input_model.model_validate_json(
            canonical_json_bytes(expected_input),
            strict=True,
        )
        contract.output_model.model_validate_json(
            canonical_json_bytes(expected_output),
            strict=True,
        )
        numeric = contract.resolve_numeric_exactness(expected_output)
        verification = None
        if numeric is NumericalExactness.FLOATING_VERIFIED:
            evidence = contract.verify_floating(expected_input, expected_output)
            verification = VerificationMetadata(
                method="executed tool-specific invariant checks",
                checks=list(evidence.checks),
                observations=list(evidence.observations),
                tolerance=evidence.tolerance,
                passed=True,
            )
    except (TypeError, ValueError, ValidationError) as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.REPLAY,
            f"tool_results.{name}.oracle",
        ) from exc
    expected_exactness = Exactness.EXACT
    expected_method = (
        str(expected_output["method"]) if expected_output.get("method") is not None else None
    )
    if result.input != expected_input:
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, f"tool_results.{name}.input")
    if result.output != expected_output:
        code = (
            BoundedRiverCallEvDiagnosticCode.NUMERIC
            if name in {"pot_odds", "raked_call_ev"}
            else BoundedRiverCallEvDiagnosticCode.REPLAY
        )
        _fail(code, f"tool_results.{name}.output")
    if (
        result.status is not ToolStatus.SUCCESS
        or result.exactness is not expected_exactness
        or result.numeric_exactness is not numeric
        or result.contract_version != contract.contract_version
        or result.version != contract.version
        or result.assumptions != list(contract.assumptions)
        or result.model_qualifier != contract.model_qualifier
        or result.method != expected_method
        or result.stochastic is not None
        or result.seed is not None
        or result.samples is not None
        or result.iterations is not None
        or result.confidence_interval is not None
        or result.confidence_level is not None
        or result.error_metadata is not None
        or result.stopping_condition is not None
        or result.verification != verification
        or result.duration_seconds > _DIRECT_TOOL_MAX_DURATION_SECONDS
        or result.warnings != _expected_direct_tool_warnings(expected_output, numeric)
        or result.error is not None
        or result.reproduce_command
        != (
            f"poker-deliberate calculate {name} --analysis-scope retrospective --input <input.json>"
        )
        or (name == "hand_validator" and result.output.get("valid") is not True)
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, f"tool_results.{name}")


def _verify_direct_successes(
    admission: BoundedRiverCallEvAdmission,
    tool_results: list[ToolResult] | tuple[ToolResult, ...],
) -> None:
    expected_inputs, direct_oracles = _direct_tool_oracles(admission)
    for result in tool_results:
        if result.tool_name in direct_oracles and result.status is ToolStatus.SUCCESS:
            _verify_successful_direct_tool_result(
                admission,
                result,
                expected_input=expected_inputs[result.tool_name],
                expected_output=direct_oracles[result.tool_name],
            )


def build_bounded_river_call_ev_result(
    admission: BoundedRiverCallEvAdmission,
    tool_results: list[ToolResult] | tuple[ToolResult, ...],
) -> BoundedRiverCallEvResultV1:
    names = tuple(result.tool_name for result in tool_results)
    if names != BOUNDED_RIVER_CALL_EV_TOOL_ORDER or any(
        result.status is not ToolStatus.SUCCESS for result in tool_results
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.TOOL_PLAN, "tool_results")
    _verify_direct_successes(admission, tool_results)
    expected_inputs = expected_bounded_river_tool_inputs(admission)
    by_name = {result.tool_name: result for result in tool_results}
    for name in ("hand_validator", "hand_pot_ledger", "pot_odds", "raked_call_ev"):
        if by_name[name].input != expected_inputs[name]:
            _fail(BoundedRiverCallEvDiagnosticCode.TOOL_PLAN, f"tool_results.{name}.input")
    try:
        ledger = HandPotLedgerOutputV1.model_validate_json(
            canonical_json_bytes(by_name["hand_pot_ledger"].output),
            strict=True,
        )
    except ValidationError as exc:
        raise BoundedRiverCallEvError(
            BoundedRiverCallEvDiagnosticCode.LEDGER,
            "tool_results.hand_pot_ledger.output",
        ) from exc
    plan = admission.candidate.projection.bounded_candidate.projection.tool_plan
    if (
        ledger.chip_unit != plan.ledger_profile.chip_unit
        or _domain_hash(
            f"{BOUNDED_NL_TOOL_PLAN_CANONICALIZATION_ID}:ledger-output",
            ledger.model_dump(mode="json"),
        )
        != plan.ledger_output_sha256
        or by_name["pot_odds"].output.get("required_equity") is None
        or by_name["raked_call_ev"].output.get("ev") is None
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.LEDGER, "tool_results")
    range_tools = [by_name[name] for name in RANGE_EQUITY_TOOL_PLAN]
    range_result = build_versioned_range_river_equity_result(
        admission.range_equity_admission.case,
        range_tools,
    )
    model = admission.candidate.projection.call_ev_model
    equity = Fraction(model.equity.numerator, model.equity.denominator)
    required = Fraction(model.required_equity.numerator, model.required_equity.denominator)
    call_ev = Fraction(model.call_ev_amount.numerator, model.call_ev_amount.denominator)
    equity_binary64 = float(by_name["holdem_equity"].output["hero_equity"])
    required_binary64 = float(by_name["pot_odds"].output["required_equity"])
    call_ev_binary64 = float(by_name["raked_call_ev"].output["ev"])
    if (
        not close_ulps(equity_binary64, float(equity), ulps=128)
        or not close_ulps(required_binary64, float(required), ulps=16)
        or not close_ulps(call_ev_binary64, float(call_ev), ulps=32)
    ):
        _fail(BoundedRiverCallEvDiagnosticCode.NUMERIC, "tool_results")
    provisional = BoundedRiverCallEvResultV1.model_construct(
        run_id=admission.confirmation.run_id,
        binding_sha256=admission.binding.binding_sha256,
        range_equity_result=range_result,
        equity=model.equity,
        required_equity=model.required_equity,
        call_ev_units=model.call_ev_units,
        call_ev_amount=model.call_ev_amount,
        fold_ev_units=model.fold_ev_units,
        call_minus_fold_ev_units=model.call_minus_fold_ev_units,
        action_comparison=model.action_comparison,
        equity_binary64=equity_binary64,
        required_equity_binary64=required_binary64,
        call_ev_binary64=call_ev_binary64,
        range_source_status=admission.candidate.projection.range_definition.source.content_status,
        tool_support=tuple(
            BoundedRiverToolSupportV1(
                result_id=result.result_id,
                tool_name=result.tool_name,  # type: ignore[arg-type]
                status=result.status.value,
                result_sha256=_tool_hash(result),
            )
            for result in tool_results
        ),
        result_sha256="0" * 64,
    )
    final = provisional.model_copy(
        update={"result_sha256": bounded_river_result_sha256(provisional)}
    )
    return BoundedRiverCallEvResultV1.model_validate(final, strict=True)


def bounded_river_call_ev_report_projection(
    result: BoundedRiverCallEvResultV1,
) -> tuple[str, Claim, list[str], tuple[str, str]]:
    action_ja = {"call": "コール", "fold": "フォールド", "tie": "同値"}[result.action_comparison]
    conclusion = (
        "明示確認された1つの相手レンジ、レーキ0、将来ベッティングなしの限定モデルでは、"
        f"{action_ja}がcall/fold比較結果です。"
    )
    claim = Claim(
        claim_id=f"bounded-river-comparison-{result.run_id}",
        text=(
            "限定モデルのcall-minus-fold EVは "
            f"{result.call_minus_fold_ev_units.numerator}/"
            f"{result.call_minus_fold_ev_units.denominator} chip unitsで、"
            f"比較結果は{action_ja}です。"
        ),
        label=EpistemicLabel.CALCULATED,
        confidence=ConfidenceGrade.A,
        limitations=[
            "明示された単一レンジ、river heads-up、レーキ0、"
            "将来ベッティングなしのモデル内だけで有効です。",
            "GTO、均衡、一般戦略、実戦レンジの正確性を示しません。",
        ],
    )
    alternatives = [
        (
            "call EV: "
            f"{result.call_ev_units.numerator}/{result.call_ev_units.denominator} chip units"
        ),
        "fold EV: 0/1 chip units (focal decision時点基準)",
    ]
    limitations = (
        "range sourceはUSER_CLAIMまたはASSUMPTIONであり、実戦での正確性はUNKNOWNです。",
        "戦略的解釈はINFERENCEであり、外部solverやGTO/均衡の主張ではありません。",
    )
    return conclusion, claim, alternatives, limitations


def apply_bounded_river_call_ev_report(
    report: FinalReport,
    result: BoundedRiverCallEvResultV1 | None,
) -> FinalReport:
    if result is None or report.run_status != "completed":
        return report
    conclusion, claim, alternatives, limitations = bounded_river_call_ev_report_projection(result)
    report.conclusion = conclusion
    if all(item.claim_id != claim.claim_id for item in report.claim_assessments):
        report.claim_assessments.append(claim)
    report.alternatives = alternatives
    for limitation in limitations:
        if limitation not in report.limitations:
            report.limitations.append(limitation)
    return FinalReport.model_validate(report.model_dump(mode="python"))


def verify_bounded_river_call_ev_tool_chain(
    admission: BoundedRiverCallEvAdmission,
    tool_results: list[ToolResult] | tuple[ToolResult, ...],
    *,
    run_status: str,
) -> BoundedRiverCallEvResultV1 | None:
    names = tuple(result.tool_name for result in tool_results)
    if names != BOUNDED_RIVER_CALL_EV_TOOL_ORDER[: len(names)]:
        _fail(BoundedRiverCallEvDiagnosticCode.TOOL_PLAN, "tool_results")
    if run_status == "completed":
        return build_bounded_river_call_ev_result(admission, tool_results)
    if run_status != "failed_with_limitations":
        _fail(BoundedRiverCallEvDiagnosticCode.REPLAY, "run_status")
    if any(result.status is not ToolStatus.SUCCESS for result in tool_results[:-1]):
        _fail(BoundedRiverCallEvDiagnosticCode.TOOL_PLAN, "tool_results.failed_prefix")

    expected_inputs, direct_oracles = _direct_tool_oracles(admission)
    for result in tool_results:
        if result.tool_name not in direct_oracles:
            continue
        expected_input = expected_inputs[result.tool_name]
        if result.input != expected_input:
            _fail(
                BoundedRiverCallEvDiagnosticCode.TOOL_PLAN,
                f"tool_results.{result.tool_name}.input",
            )
        if result.status is ToolStatus.SUCCESS:
            _verify_successful_direct_tool_result(
                admission,
                result,
                expected_input=expected_input,
                expected_output=direct_oracles[result.tool_name],
            )
        elif (
            result is not tool_results[-1]
            or result.status is not ToolStatus.FAILED
            or result.output
            or result.exactness is not Exactness.UNAVAILABLE
            or result.numeric_exactness is not NumericalExactness.UNAVAILABLE
            or result.verification is not None
            or not result.error
        ):
            _fail(
                BoundedRiverCallEvDiagnosticCode.TOOL_PLAN,
                f"tool_results.{result.tool_name}.failure",
            )
    range_results = [
        result for result in tool_results if result.tool_name in RANGE_EQUITY_TOOL_PLAN
    ]
    if range_results:
        range_completed = tuple(
            result.tool_name for result in range_results
        ) == RANGE_EQUITY_TOOL_PLAN and all(
            result.status is ToolStatus.SUCCESS for result in range_results
        )
        verify_versioned_range_river_equity_tool_chain(
            admission.range_equity_admission.case,
            range_results,
            run_status="completed" if range_completed else "failed_with_limitations",
        )
    if len(tool_results) == len(BOUNDED_RIVER_CALL_EV_TOOL_ORDER) and all(
        result.status is ToolStatus.SUCCESS for result in tool_results
    ):
        return build_bounded_river_call_ev_result(admission, tool_results)
    return None


def default_bounded_river_confirmation_ids() -> tuple[str, str]:
    suffix = uuid4().hex
    return f"confirmation-{suffix[:12]}", f"idempotency-{suffix[12:24]}"


def bounded_river_terminal_revision_root_sha256(storage_root: Path | str) -> str:
    root = Path(storage_root)
    if not root.is_absolute():
        _fail(BoundedRiverCallEvDiagnosticCode.STORAGE, "storage_authority.revision_root")
    return _domain_hash(
        "poker-bounded-river-call-ev-terminal-revision-root-v1",
        str(root.resolve(strict=False)).replace("\\", "/"),
    )


def review_bounded_river_call_ev_intake(
    admission: BoundedRiverCallEvAdmission,
    *,
    config: object | None = None,
) -> Any:
    from poker_deliberation.config import AppConfig
    from poker_deliberation.orchestrator import Orchestrator
    from poker_deliberation.providers import LocalProvider

    if config is not None and not isinstance(config, AppConfig):
        raise TypeError("config must be AppConfig")
    return Orchestrator(config=config, provider=LocalProvider()).run_bounded_river_call_ev_review(
        admission
    )


__all__ = [
    "BoundedRiverCallEvAdmission",
    "BoundedRiverCallEvError",
    "admit_bounded_river_call_ev_review",
    "bounded_river_authority_sha256",
    "bounded_river_binding_sha256",
    "bounded_river_call_ev_binding",
    "bounded_river_candidate_sha256",
    "bounded_river_confirmation_sha256",
    "bounded_river_result_sha256",
    "bounded_river_terminal_revision_root_sha256",
    "build_bounded_river_call_ev_result",
    "create_bounded_river_call_ev_authority",
    "create_bounded_river_call_ev_confirmation",
    "default_bounded_river_confirmation_ids",
    "expected_bounded_river_tool_inputs",
    "prepare_bounded_river_call_ev_intake",
    "review_bounded_river_call_ev_intake",
    "verify_bounded_river_call_ev_candidate",
    "verify_bounded_river_call_ev_tool_chain",
]
