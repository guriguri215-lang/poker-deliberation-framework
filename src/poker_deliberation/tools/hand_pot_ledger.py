"""Exact-under-model hand ledger for one explicit repository-owned NLHE profile."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from decimal import Decimal, DecimalException
from fractions import Fraction
from typing import Annotated, Any, Literal, NoReturn, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from poker_deliberation.schemas import CanonicalHand, HandAction, Street
from poker_deliberation.tools.cards import normalize_cards

LEDGER_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
PROFILE_SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"
PROFILE_ID: Literal["generic_nlhe_cash_no_rake_v1"] = "generic_nlhe_cash_no_rake_v1"
PROFILE_VERSION: Literal["1.0.0"] = "1.0.0"
SUPPORTED_SITE: Literal["none"] = "none"
MAX_LEDGER_UNITS = 2**63 - 1
MAX_ACTIONS = 512

_CHIP_UNIT_PATTERN = re.compile(r"^(?:[1-9][0-9]{0,17}|(?:0|[1-9][0-9]{0,17})\.[0-9]{0,8}[1-9])$")
_STREET_ORDER = {
    Street.PREFLOP: 0,
    Street.FLOP: 1,
    Street.TURN: 2,
    Street.RIVER: 3,
}
_CONTRIBUTION_ACTIONS = {
    "post_blind",
    "post_ante",
    "call",
    "bet",
    "raise",
    "all_in",
}

LedgerUnit = Annotated[int, Field(ge=0, le=MAX_LEDGER_UNITS)]
PositiveLedgerUnit = Annotated[int, Field(gt=0, le=MAX_LEDGER_UNITS)]


class HandPotLedgerError(ValueError):
    """Stable fail-closed error whose message never includes caller content."""


def _fail(code: str) -> NoReturn:
    raise HandPotLedgerError(code)


def _require(condition: bool, code: str) -> None:
    if not condition:
        _fail(code)


class FrozenLedgerModel(BaseModel):
    """Strict, immutable contract base used only by the versioned ledger slice."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        revalidate_instances="always",
    )


class HandRuleProfileV1(FrozenLedgerModel):
    schema_version: Literal["1.0.0"]
    profile_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    profile_version: str = Field(min_length=1, max_length=16, pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")
    supported_site: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")
    chip_unit: str = Field(min_length=1, max_length=28)


class HandPotLedgerInputV1(FrozenLedgerModel):
    schema_version: Literal["1.0.0"]
    rule_profile: HandRuleProfileV1
    hand: CanonicalHand

    @model_validator(mode="before")
    @classmethod
    def canonical_hand_uses_strict_json_types(cls, value: object) -> object:
        if not isinstance(value, dict) or not isinstance(value.get("hand"), dict):
            raise ValueError("hand-pot-ledger-input-must-be-a-json-object")
        encoded = json.dumps(
            value["hand"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        strict_hand = CanonicalHand.model_validate_json(encoded, strict=True)
        return {**value, "hand": strict_hand}


class LedgerActionV1(FrozenLedgerModel):
    schema_version: Literal["1.0.0"] = LEDGER_SCHEMA_VERSION
    action_index: int = Field(ge=0, le=MAX_ACTIONS - 1)
    street: Literal["preflop", "flop", "turn", "river"]
    actor: str = Field(min_length=1)
    action: Literal["post_blind", "post_ante", "fold", "check", "call", "bet", "raise", "all_in"]
    committed_units: LedgerUnit
    street_contribution_units_after: LedgerUnit
    total_contribution_units_after: LedgerUnit
    remaining_stack_units_after: LedgerUnit
    pot_units_after: LedgerUnit
    current_bet_units_after: LedgerUnit
    raise_rights_before: bool
    full_raise: bool
    minimum_full_raise_units_after: PositiveLedgerUnit


class UncalledReturnV1(FrozenLedgerModel):
    schema_version: Literal["1.0.0"] = LEDGER_SCHEMA_VERSION
    return_id: str = Field(pattern=r"^return-[0-9]+$")
    street: Literal["preflop", "flop", "turn", "river"]
    player_id: str = Field(min_length=1)
    amount_units: PositiveLedgerUnit
    source_action_indexes: tuple[int, ...] = Field(min_length=1)


class PotLayerV1(FrozenLedgerModel):
    schema_version: Literal["1.0.0"] = LEDGER_SCHEMA_VERSION
    pot_id: str = Field(pattern=r"^pot-[0-9]+$")
    layer_index: int = Field(ge=0, le=MAX_ACTIONS - 1)
    kind: Literal["main", "side"]
    lower_bound_units: LedgerUnit
    upper_bound_units: PositiveLedgerUnit
    amount_units: PositiveLedgerUnit
    contributors: tuple[str, ...] = Field(min_length=1)
    eligible_players: tuple[str, ...] = Field(min_length=1)
    evidence_action_indexes: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def bounds_and_amount_are_consistent(self) -> PotLayerV1:
        if self.upper_bound_units <= self.lower_bound_units:
            raise ValueError("pot-layer-bounds-invalid")
        expected = (self.upper_bound_units - self.lower_bound_units) * len(self.contributors)
        if self.amount_units != expected:
            raise ValueError("pot-layer-amount-invalid")
        if not set(self.eligible_players).issubset(self.contributors):
            raise ValueError("pot-layer-eligibility-invalid")
        return self


class PlayerEligibilityV1(FrozenLedgerModel):
    schema_version: Literal["1.0.0"] = LEDGER_SCHEMA_VERSION
    player_id: str = Field(min_length=1)
    eligible_for_contested_pots: bool
    folded_at_action_index: int | None = Field(default=None, ge=0, le=MAX_ACTIONS - 1)


class HandPotLedgerOutputV1(FrozenLedgerModel):
    schema_version: Literal["1.0.0"] = LEDGER_SCHEMA_VERSION
    profile_id: Literal["generic_nlhe_cash_no_rake_v1"]
    profile_version: Literal["1.0.0"]
    supported_site: Literal["none"]
    chip_unit: str
    ledger_actions: tuple[LedgerActionV1, ...] = Field(min_length=1, max_length=MAX_ACTIONS)
    uncalled_returns: tuple[UncalledReturnV1, ...]
    pot_layers: tuple[PotLayerV1, ...] = Field(min_length=1)
    player_eligibility: tuple[PlayerEligibilityV1, ...] = Field(min_length=2, max_length=10)
    gross_contributions_units: dict[str, LedgerUnit]
    net_contributions_units: dict[str, LedgerUnit]
    remaining_stacks_units: dict[str, LedgerUnit]
    gross_committed_units: LedgerUnit
    total_returned_units: LedgerUnit
    final_pot_units: PositiveLedgerUnit
    starting_chips_units: PositiveLedgerUnit
    conservation_verified: Literal[True]
    oracle_verified: Literal[True]
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def accounting_identities_hold(self) -> HandPotLedgerOutputV1:
        if sum(self.gross_contributions_units.values()) != self.gross_committed_units:
            raise ValueError("gross-contribution-sum-invalid")
        if sum(self.net_contributions_units.values()) != self.final_pot_units:
            raise ValueError("net-contribution-sum-invalid")
        if sum(item.amount_units for item in self.uncalled_returns) != self.total_returned_units:
            raise ValueError("return-sum-invalid")
        if self.gross_committed_units != self.final_pot_units + self.total_returned_units:
            raise ValueError("gross-net-return-conservation-invalid")
        if sum(item.amount_units for item in self.pot_layers) != self.final_pot_units:
            raise ValueError("pot-layer-sum-invalid")
        if sum(self.remaining_stacks_units.values()) + self.final_pot_units != (
            self.starting_chips_units
        ):
            raise ValueError("stack-pot-conservation-invalid")
        return self


def _validate_chip_unit(raw: str) -> Decimal:
    _require(_CHIP_UNIT_PATTERN.fullmatch(raw) is not None, "chip-unit-grammar-unsupported")
    try:
        value = Decimal(raw)
    except DecimalException:
        _fail("chip-unit-grammar-unsupported")
    _require(value.is_finite() and value > 0, "chip-unit-grammar-unsupported")
    return value


def _decimal_units(value: float, unit: Decimal) -> int:
    try:
        decimal_value = Decimal(str(value))
        _require(decimal_value.is_finite() and decimal_value >= 0, "amount-not-finite")
        _require(
            decimal_value <= unit * MAX_LEDGER_UNITS,
            "ledger-unit-limit-exceeded",
        )
        quotient = decimal_value / unit
    except (DecimalException, ArithmeticError):
        _fail("amount-conversion-failed")
    _require(quotient == quotient.to_integral_value(), "amount-not-integral-in-chip-unit")
    return int(quotient)


def _fraction_units(value: float, unit: Fraction) -> int:
    quotient = Fraction(str(value)) / unit
    _require(quotient.denominator == 1, "oracle-non-integral-chip-unit")
    units = quotient.numerator
    _require(0 <= units <= MAX_LEDGER_UNITS, "oracle-ledger-unit-limit-exceeded")
    return units


def _checked_add(left: int, right: int) -> int:
    _require(left >= 0 and right >= 0 and left <= MAX_LEDGER_UNITS - right, "ledger-overflow")
    return left + right


def _canonical_players(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted(values))


def _street_value(
    street: Street,
) -> Literal["preflop", "flop", "turn", "river"]:
    if street is Street.PREFLOP:
        return "preflop"
    if street is Street.FLOP:
        return "flop"
    if street is Street.TURN:
        return "turn"
    if street is Street.RIVER:
        return "river"
    _fail("showdown-actions-unsupported")


def _validate_profile(profile: HandRuleProfileV1) -> Decimal:
    _require(profile.schema_version == PROFILE_SCHEMA_VERSION, "unknown-profile-schema-version")
    _require(profile.profile_id == PROFILE_ID, "unsupported-rule-profile")
    _require(profile.profile_version == PROFILE_VERSION, "unknown-rule-profile-version")
    _require(profile.supported_site == SUPPORTED_SITE, "unsupported-site-profile")
    return _validate_chip_unit(profile.chip_unit)


def _validate_profile_hand(hand: CanonicalHand, unit: Decimal) -> tuple[int, int, int]:
    _require(hand.game_type == "NLHE", "unsupported-game-type")
    _require(hand.format == "cash" and hand.tournament is None, "unsupported-non-cash-context")
    if hand.rake is None:
        _fail("explicit-zero-rake-required")
    _require(_decimal_units(hand.rake, unit) == 0, "unsupported-rake")
    _require(hand.table_size == len(hand.players), "table-size-player-count-mismatch")
    _require(len(hand.actions) <= MAX_ACTIONS, "action-count-limit-exceeded")
    _require(hand.small_blind < hand.big_blind, "blind-structure-invalid")
    _require(len(hand.board) in {0, 3, 4, 5}, "board-card-count-invalid")
    try:
        normalize_cards((*hand.hero_cards, *hand.board))
    except ValueError:
        _fail("card-set-invalid")

    small_blind = _decimal_units(hand.small_blind, unit)
    big_blind = _decimal_units(hand.big_blind, unit)
    ante = _decimal_units(hand.ante, unit)
    _require(0 < small_blind < big_blind, "blind-structure-invalid")

    blind_actions = [action for action in hand.actions if action.action == "post_blind"]
    _require(len(blind_actions) == 2, "unsupported-blind-or-straddle-structure")
    blind_amounts = sorted(_decimal_units(action.amount, unit) for action in blind_actions)
    _require(
        blind_amounts == [small_blind, big_blind],
        "unsupported-blind-or-straddle-structure",
    )
    _require(
        len({action.actor for action in blind_actions}) == 2
        and all(action.street is Street.PREFLOP for action in blind_actions),
        "unsupported-blind-or-straddle-structure",
    )

    ante_actions = [action for action in hand.actions if action.action == "post_ante"]
    if ante == 0:
        _require(not ante_actions, "unexpected-ante-post")
    else:
        _require(
            len(ante_actions) == len(hand.players)
            and {action.actor for action in ante_actions}
            == {player.player_id for player in hand.players}
            and all(
                action.street is Street.PREFLOP and _decimal_units(action.amount, unit) == ante
                for action in ante_actions
            ),
            "unsupported-ante-structure",
        )
    return small_blind, big_blind, ante


def _street_supported_by_board(street: Street, board_size: int) -> bool:
    if street is Street.PREFLOP:
        return True
    if street is Street.FLOP:
        return board_size >= 3
    if street is Street.TURN:
        return board_size >= 4
    if street is Street.RIVER:
        return board_size >= 5
    return False


class _LedgerBuilder:
    def __init__(self, hand: CanonicalHand, unit: Decimal, big_blind: int) -> None:
        self.hand = hand
        self.unit = unit
        self.big_blind = big_blind
        self.player_ids = tuple(player.player_id for player in hand.players)
        self.starting = {
            player.player_id: _decimal_units(player.starting_stack, unit) for player in hand.players
        }
        self.starting_total = 0
        for value in self.starting.values():
            self.starting_total = _checked_add(self.starting_total, value)
        _require(self.starting_total > 0, "starting-chips-required")
        self.remaining = dict(self.starting)
        self.gross = dict.fromkeys(self.player_ids, 0)
        self.net = dict.fromkeys(self.player_ids, 0)
        self.active = set(self.player_ids)
        self.all_in: set[str] = set()
        self.folded_at: dict[str, int | None] = dict.fromkeys(self.player_ids)
        self.current_street: Street | None = None
        self.last_street_order = -1
        self.street_contribution = dict.fromkeys(self.player_ids, 0)
        self.current_bet = 0
        self.minimum_full_raise = big_blind
        self.acted_level: dict[str, int] = {}
        self.acted_this_street: set[str] = set()
        self.voluntary_action_seen = False
        self.action_intervals: dict[str, list[tuple[int, int, int]]] = {
            player_id: [] for player_id in self.player_ids
        }
        self.street_intervals: dict[str, list[tuple[int, int, int]]] = {
            player_id: [] for player_id in self.player_ids
        }
        self.actions: list[LedgerActionV1] = []
        self.returns: list[UncalledReturnV1] = []
        self.gross_total = 0
        self.net_total = 0
        self.returned_total = 0

    def _start_street(self, street: Street) -> None:
        if street not in _STREET_ORDER:
            _fail("showdown-actions-unsupported")
        order = _STREET_ORDER[street]
        _require(order > self.last_street_order, "street-order-invalid")
        _require(_street_supported_by_board(street, len(self.hand.board)), "street-board-mismatch")
        if self.current_street is not None:
            self._close_street()
            _require(len(self.active) > 1, "action-after-hand-ended")
        self.current_street = street
        self.last_street_order = order
        self.street_contribution = dict.fromkeys(self.player_ids, 0)
        self.current_bet = 0
        self.minimum_full_raise = self.big_blind
        self.acted_level = {}
        self.acted_this_street = set()
        self.street_intervals = {player_id: [] for player_id in self.player_ids}

    def _raise_rights(self, actor: str) -> bool:
        prior_level = self.acted_level.get(actor)
        return prior_level is None or self.current_bet - prior_level >= self.minimum_full_raise

    def _commit(
        self,
        index: int,
        actor: str,
        amount: int,
        *,
        counts_for_bet: bool,
    ) -> None:
        _require(amount <= self.remaining[actor], "stack-underflow")
        total_before = self.gross[actor]
        total_after = _checked_add(total_before, amount)
        street_before = self.street_contribution[actor]
        street_after = street_before
        if counts_for_bet:
            street_after = _checked_add(street_before, amount)
            self.street_contribution[actor] = street_after
            if amount:
                self.street_intervals[actor].append((index, street_before, street_after))
        self.gross[actor] = total_after
        self.net[actor] = _checked_add(self.net[actor], amount)
        self.remaining[actor] -= amount
        self.gross_total = _checked_add(self.gross_total, amount)
        self.net_total = _checked_add(self.net_total, amount)
        if amount:
            self.action_intervals[actor].append((index, total_before, total_after))
        if self.remaining[actor] == 0:
            self.all_in.add(actor)

    def _validate_declared_pot(self, declared: float | None, expected: int, code: str) -> None:
        if declared is not None:
            _require(_decimal_units(declared, self.unit) == expected, code)

    def _validate_to_amount(
        self,
        action: HandAction,
        expected: int,
        *,
        required: bool = False,
    ) -> None:
        if required:
            _require(action.to_amount is not None, "to-amount-required")
        if action.to_amount is not None:
            _require(
                _decimal_units(action.to_amount, self.unit) == expected,
                "to-amount-mismatch",
            )

    def _close_street(self) -> None:
        if self.current_street is None:
            _fail("internal-street-state-invalid")
        current_street = self.current_street
        if len(self.active) > 1:
            for player_id in self.active - self.all_in:
                _require(
                    player_id in self.acted_this_street,
                    "incomplete-betting-round-missing-action",
                )
                _require(
                    self.street_contribution[player_id] == self.current_bet,
                    "incomplete-betting-round-outstanding-call",
                )

        highest = max(self.street_contribution.values(), default=0)
        highest_players = [
            player_id
            for player_id, contribution in self.street_contribution.items()
            if contribution == highest and highest > 0
        ]
        if len(highest_players) == 1:
            player_id = highest_players[0]
            second = max(
                (
                    contribution
                    for other, contribution in self.street_contribution.items()
                    if other != player_id
                ),
                default=0,
            )
            amount = highest - second
            if amount > 0:
                _require(player_id in self.active, "ambiguous-uncalled-return")
                source_indexes = tuple(
                    index
                    for index, before, after in self.street_intervals[player_id]
                    if before < highest and after > second
                )
                _require(bool(source_indexes), "ambiguous-uncalled-return")
                self.street_contribution[player_id] -= amount
                self.net[player_id] -= amount
                self.net_total -= amount
                self.remaining[player_id] = _checked_add(self.remaining[player_id], amount)
                self.returned_total = _checked_add(self.returned_total, amount)
                if self.remaining[player_id] > 0:
                    self.all_in.discard(player_id)
                self.returns.append(
                    UncalledReturnV1(
                        return_id=f"return-{len(self.returns)}",
                        street=_street_value(current_street),
                        player_id=player_id,
                        amount_units=amount,
                        source_action_indexes=source_indexes,
                    )
                )

    def _append_action(
        self,
        index: int,
        action: HandAction,
        amount: int,
        raise_rights_before: bool,
        full_raise: bool,
    ) -> None:
        self.actions.append(
            LedgerActionV1(
                action_index=index,
                street=_street_value(action.street),
                actor=action.actor,
                action=action.action,
                committed_units=amount,
                street_contribution_units_after=self.street_contribution[action.actor],
                total_contribution_units_after=self.net[action.actor],
                remaining_stack_units_after=self.remaining[action.actor],
                pot_units_after=self.net_total,
                current_bet_units_after=self.current_bet,
                raise_rights_before=raise_rights_before,
                full_raise=full_raise,
                minimum_full_raise_units_after=self.minimum_full_raise,
            )
        )

    def apply(self, index: int, action: HandAction) -> None:
        _require(action.actor in self.remaining, "unknown-actor")
        if action.street is not self.current_street:
            self._start_street(action.street)
        _require(action.actor in self.active, "folded-player-acts")
        _require(action.actor not in self.all_in, "all-in-player-acts")
        if self.voluntary_action_seen:
            _require(len(self.active) > 1, "action-after-hand-ended")

        amount = _decimal_units(action.amount, self.unit)
        self._validate_declared_pot(action.pot_before, self.net_total, "pot-before-mismatch")
        outstanding = self.current_bet - self.street_contribution[action.actor]
        _require(outstanding >= 0, "negative-outstanding-bet")
        raise_rights_before = self._raise_rights(action.actor)
        full_raise = False

        if action.action in {"post_blind", "post_ante"}:
            _require(
                self.current_street is Street.PREFLOP and not self.voluntary_action_seen,
                "forced-post-order-invalid",
            )
            _require(action.to_amount is None, "forced-post-to-amount-unsupported")
            _require(amount > 0, "forced-post-must-be-positive")
            if action.action == "post_ante":
                self._commit(index, action.actor, amount, counts_for_bet=False)
            else:
                self._commit(index, action.actor, amount, counts_for_bet=True)
                self.current_bet = max(
                    self.current_bet,
                    self.street_contribution[action.actor],
                )
        else:
            self.voluntary_action_seen = True
            _require(action.action not in {"post_blind", "post_ante"}, "forced-post-order-invalid")
            if action.action == "check":
                _require(amount == 0 and action.to_amount is None, "check-shape-invalid")
                _require(outstanding == 0, "check-facing-bet")
                self.acted_this_street.add(action.actor)
                self.acted_level[action.actor] = self.current_bet
            elif action.action == "fold":
                _require(amount == 0 and action.to_amount is None, "fold-shape-invalid")
                _require(outstanding > 0, "fold-without-outstanding-bet")
                self.active.remove(action.actor)
                self.folded_at[action.actor] = index
                self.acted_this_street.add(action.actor)
                self.acted_level[action.actor] = self.current_bet
            elif action.action == "call":
                required = min(outstanding, self.remaining[action.actor])
                _require(outstanding > 0 and amount == required, "call-amount-invalid")
                expected_to = self.street_contribution[action.actor] + amount
                self._validate_to_amount(action, expected_to)
                self._commit(index, action.actor, amount, counts_for_bet=True)
                self.acted_this_street.add(action.actor)
                self.acted_level[action.actor] = self.current_bet
            elif action.action == "bet":
                _require(self.current_bet == 0, "bet-when-bet-open")
                _require(amount >= self.big_blind, "opening-bet-below-minimum")
                _require(amount < self.remaining[action.actor], "bet-must-use-all-in-action")
                expected_to = self.street_contribution[action.actor] + amount
                self._validate_to_amount(action, expected_to, required=True)
                self._commit(index, action.actor, amount, counts_for_bet=True)
                self.current_bet = expected_to
                self.minimum_full_raise = expected_to
                self.acted_this_street.add(action.actor)
                self.acted_level[action.actor] = self.current_bet
                full_raise = True
            elif action.action == "raise":
                _require(self.current_bet > 0, "raise-without-open-bet")
                _require(raise_rights_before, "betting-not-reopened")
                _require(amount < self.remaining[action.actor], "raise-must-use-all-in-action")
                expected_to = self.street_contribution[action.actor] + amount
                self._validate_to_amount(action, expected_to, required=True)
                increment = expected_to - self.current_bet
                _require(increment >= self.minimum_full_raise, "raise-below-full-minimum")
                self._commit(index, action.actor, amount, counts_for_bet=True)
                self.current_bet = expected_to
                self.minimum_full_raise = increment
                self.acted_this_street.add(action.actor)
                self.acted_level[action.actor] = self.current_bet
                full_raise = True
            elif action.action == "all_in":
                _require(
                    amount > 0 and amount == self.remaining[action.actor],
                    "all-in-amount-invalid",
                )
                expected_to = self.street_contribution[action.actor] + amount
                self._validate_to_amount(action, expected_to)
                if expected_to > self.current_bet:
                    _require(raise_rights_before, "betting-not-reopened")
                    increment = expected_to - self.current_bet
                    full_raise = increment >= self.minimum_full_raise
                    self.current_bet = expected_to
                    if full_raise:
                        self.minimum_full_raise = increment
                self._commit(index, action.actor, amount, counts_for_bet=True)
                self.acted_this_street.add(action.actor)
                self.acted_level[action.actor] = self.current_bet
            else:
                _fail("unsupported-action")

        self._validate_declared_pot(action.pot_after, self.net_total, "pot-after-mismatch")
        self._append_action(index, action, amount, raise_rights_before, full_raise)

    def finish(self) -> HandPotLedgerOutputV1:
        _require(self.current_street is not None and bool(self.actions), "ledger-actions-required")
        self._close_street()
        _require(self.net_total > 0, "positive-final-pot-required")
        _require(
            self.gross_total == self.net_total + self.returned_total,
            "gross-net-return-conservation-failed",
        )
        _require(
            sum(self.remaining.values()) + self.net_total == self.starting_total,
            "stack-pot-conservation-failed",
        )

        positive_levels = sorted({value for value in self.net.values() if value > 0})
        layers: list[PotLayerV1] = []
        lower = 0
        for layer_index, upper in enumerate(positive_levels):
            contributors = _canonical_players(
                player_id for player_id, value in self.net.items() if value >= upper
            )
            eligible = _canonical_players(
                player_id for player_id in contributors if self.folded_at[player_id] is None
            )
            _require(bool(eligible), "pot-layer-has-no-eligible-player")
            amount = (upper - lower) * len(contributors)
            _require(0 < amount <= MAX_LEDGER_UNITS, "pot-layer-limit-exceeded")
            evidence = tuple(
                sorted(
                    {
                        index
                        for player_id in contributors
                        for index, before, after in self.action_intervals[player_id]
                        if before < upper and after > lower
                    }
                )
            )
            _require(bool(evidence), "pot-layer-evidence-missing")
            layers.append(
                PotLayerV1(
                    pot_id=f"pot-{layer_index}",
                    layer_index=layer_index,
                    kind="main" if layer_index == 0 else "side",
                    lower_bound_units=lower,
                    upper_bound_units=upper,
                    amount_units=amount,
                    contributors=contributors,
                    eligible_players=eligible,
                    evidence_action_indexes=evidence,
                )
            )
            lower = upper

        eligibility = tuple(
            PlayerEligibilityV1(
                player_id=player_id,
                eligible_for_contested_pots=(
                    self.folded_at[player_id] is None and self.net[player_id] > 0
                ),
                folded_at_action_index=self.folded_at[player_id],
            )
            for player_id in _canonical_players(self.player_ids)
        )
        return HandPotLedgerOutputV1(
            profile_id=PROFILE_ID,
            profile_version=PROFILE_VERSION,
            supported_site=SUPPORTED_SITE,
            chip_unit=str(self.unit),
            ledger_actions=tuple(self.actions),
            uncalled_returns=tuple(self.returns),
            pot_layers=tuple(layers),
            player_eligibility=eligibility,
            gross_contributions_units={
                player_id: self.gross[player_id]
                for player_id in _canonical_players(self.player_ids)
            },
            net_contributions_units={
                player_id: self.net[player_id] for player_id in _canonical_players(self.player_ids)
            },
            remaining_stacks_units={
                player_id: self.remaining[player_id]
                for player_id in _canonical_players(self.player_ids)
            },
            gross_committed_units=self.gross_total,
            total_returned_units=self.returned_total,
            final_pot_units=self.net_total,
            starting_chips_units=self.starting_total,
            conservation_verified=True,
            oracle_verified=True,
            limitations=(
                "No winner assignment, payout split, or hand-strength evaluation is performed.",
                "Only the explicit generic no-rake NLHE cash profile and supported site none "
                "apply.",
            ),
        )


def _oracle_expected(
    hand: CanonicalHand,
    profile: HandRuleProfileV1,
) -> dict[str, object]:
    unit = Fraction(profile.chip_unit)
    player_ids = tuple(player.player_id for player in hand.players)
    starting = {
        player.player_id: _fraction_units(player.starting_stack, unit) for player in hand.players
    }
    gross = dict.fromkeys(player_ids, 0)
    street = dict.fromkeys(player_ids, 0)
    total_intervals: dict[str, list[tuple[int, int, int]]] = {
        player_id: [] for player_id in player_ids
    }
    street_intervals: dict[str, list[tuple[int, int, int]]] = {
        player_id: [] for player_id in player_ids
    }
    folded: dict[str, int | None] = dict.fromkeys(player_ids)
    current_street: Street | None = None
    returns: list[tuple[str, str, int, tuple[int, ...]]] = []

    def close_street() -> None:
        if current_street is None:
            return
        highest = max(street.values(), default=0)
        leaders = [
            player_id for player_id, contribution in street.items() if contribution == highest
        ]
        if highest <= 0 or len(leaders) != 1:
            return
        player_id = leaders[0]
        second = max(
            (value for other, value in street.items() if other != player_id),
            default=0,
        )
        amount = highest - second
        if amount <= 0:
            return
        source = tuple(
            index
            for index, before, after in street_intervals[player_id]
            if before < highest and after > second
        )
        returns.append((_street_value(current_street), player_id, amount, source))
        street[player_id] -= amount

    for index, action in enumerate(hand.actions):
        if action.street is not current_street:
            close_street()
            current_street = action.street
            street = dict.fromkeys(player_ids, 0)
            street_intervals = {player_id: [] for player_id in player_ids}
        if action.action == "fold":
            folded[action.actor] = index
        if action.action not in _CONTRIBUTION_ACTIONS:
            continue
        amount = _fraction_units(action.amount, unit)
        before_total = gross[action.actor]
        after_total = before_total + amount
        gross[action.actor] = after_total
        if amount:
            total_intervals[action.actor].append((index, before_total, after_total))
        if action.action != "post_ante":
            before_street = street[action.actor]
            after_street = before_street + amount
            street[action.actor] = after_street
            if amount:
                street_intervals[action.actor].append((index, before_street, after_street))
    close_street()

    returned_by_player = dict.fromkeys(player_ids, 0)
    for _street, player_id, amount, _source in returns:
        returned_by_player[player_id] += amount
    net = {player_id: gross[player_id] - returned_by_player[player_id] for player_id in player_ids}
    remaining = {
        player_id: starting[player_id] - gross[player_id] + returned_by_player[player_id]
        for player_id in player_ids
    }
    levels = sorted({value for value in net.values() if value > 0})
    layers: list[tuple[int, int, int, tuple[str, ...], tuple[str, ...], tuple[int, ...]]] = []
    lower = 0
    for upper in levels:
        contributors = _canonical_players(
            player_id for player_id, value in net.items() if value >= upper
        )
        eligible = _canonical_players(
            player_id for player_id in contributors if folded[player_id] is None
        )
        evidence = tuple(
            sorted(
                {
                    index
                    for player_id in contributors
                    for index, before, after in total_intervals[player_id]
                    if before < upper and after > lower
                }
            )
        )
        layers.append(
            (
                lower,
                upper,
                (upper - lower) * len(contributors),
                contributors,
                eligible,
                evidence,
            )
        )
        lower = upper
    return {
        "starting": starting,
        "gross": gross,
        "net": net,
        "remaining": remaining,
        "returns": returns,
        "layers": layers,
        "final_pot": sum(net.values()),
        "gross_total": sum(gross.values()),
        "returned_total": sum(item[2] for item in returns),
    }


def _verify_oracle(
    hand: CanonicalHand,
    profile: HandRuleProfileV1,
    result: HandPotLedgerOutputV1,
) -> None:
    expected = _oracle_expected(hand, profile)
    _require(result.gross_contributions_units == expected["gross"], "oracle-gross-mismatch")
    _require(result.net_contributions_units == expected["net"], "oracle-net-mismatch")
    _require(result.remaining_stacks_units == expected["remaining"], "oracle-stack-mismatch")
    _require(result.final_pot_units == expected["final_pot"], "oracle-pot-mismatch")
    _require(result.gross_committed_units == expected["gross_total"], "oracle-gross-total-mismatch")
    _require(
        result.total_returned_units == expected["returned_total"],
        "oracle-return-total-mismatch",
    )
    actual_returns = [
        (item.street, item.player_id, item.amount_units, item.source_action_indexes)
        for item in result.uncalled_returns
    ]
    _require(actual_returns == expected["returns"], "oracle-return-mismatch")
    actual_layers = [
        (
            item.lower_bound_units,
            item.upper_bound_units,
            item.amount_units,
            item.contributors,
            item.eligible_players,
            item.evidence_action_indexes,
        )
        for item in result.pot_layers
    ]
    _require(actual_layers == expected["layers"], "oracle-layer-mismatch")
    starting_total = sum(cast(dict[str, int], expected["starting"]).values())
    remaining_total = sum(cast(dict[str, int], expected["remaining"]).values())
    _require(
        starting_total == remaining_total + result.final_pot_units,
        "oracle-conservation-failed",
    )


def calculate_hand_pot_ledger(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one explicit profile and return only independently verified integer accounting."""

    request = HandPotLedgerInputV1.model_validate(payload)
    unit = _validate_profile(request.rule_profile)
    _small_blind, big_blind, _ante = _validate_profile_hand(request.hand, unit)
    builder = _LedgerBuilder(request.hand, unit, big_blind)
    for index, action in enumerate(request.hand.actions):
        builder.apply(index, action)
    result = builder.finish()
    _verify_oracle(request.hand, request.rule_profile, result)
    return result.model_dump(mode="python")
