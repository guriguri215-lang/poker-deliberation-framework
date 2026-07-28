from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest
from pydantic import ValidationError

from poker_deliberation.schemas import NumericalExactness, ToolStatus
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.contracts import contract_by_name
from tests.hand_pot_ledger_support import heads_up_hand, request


def _failed(payload: dict[str, object]):
    result = default_registry().execute("hand_pot_ledger", payload)
    assert result.status is ToolStatus.FAILED
    assert result.output == {}
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    return result


def test_unknown_profile_fails_closed_without_echoing_caller_content() -> None:
    payload = request(heads_up_hand())
    canary = "privateprofilecanary"
    cast(dict[str, Any], payload["rule_profile"])["profile_id"] = canary

    result = _failed(payload)

    assert result.error == "HandPotLedgerError: unsupported-rule-profile"
    assert canary not in (result.error or "")


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("profile_version", "9.9.9", "unknown-rule-profile-version"),
        ("supported_site", "site-x", "unsupported-site-profile"),
    ],
)
def test_unknown_profile_version_or_site_fails_closed(
    field: str,
    value: str,
    expected: str,
) -> None:
    payload = request(heads_up_hand())
    cast(dict[str, Any], payload["rule_profile"])[field] = value

    result = _failed(payload)

    assert result.error == f"HandPotLedgerError: {expected}"


@pytest.mark.parametrize(
    ("mutation", "expected"),
    [
        ({"game_type": "PLO"}, "unsupported-game-type"),
        ({"rake": 1}, "unsupported-rake"),
        ({"rake": None}, "explicit-zero-rake-required"),
    ],
)
def test_unsupported_game_and_rake_inputs_fail_closed(
    mutation: dict[str, object],
    expected: str,
) -> None:
    hand = heads_up_hand()
    hand.update(mutation)

    result = _failed(request(hand))

    assert result.error == f"HandPotLedgerError: {expected}"


def test_tournament_and_bounty_context_is_not_accepted_by_cash_profile() -> None:
    hand = heads_up_hand()
    hand["format"] = "tournament"
    hand["tournament"] = {
        "payouts": [100, 0],
        "remaining_players": 2,
        "bounty_type": "progressive",
        "hero_bounty": 25,
    }

    result = _failed(request(hand))

    assert result.error == "HandPotLedgerError: unsupported-non-cash-context"


def test_extra_run_it_twice_or_split_fields_are_rejected_by_strict_input_schema() -> None:
    payload = request(heads_up_hand())
    cast(dict[str, Any], payload["hand"])["run_it_twice"] = True

    result = _failed(payload)

    assert "extra_forbidden" in (result.error or "")


def test_legacy_numeric_coercion_is_not_allowed_at_profiled_ledger_boundary() -> None:
    payload = request(heads_up_hand())
    hand = cast(dict[str, Any], payload["hand"])
    hand["actions"][0]["amount"] = "1"

    result = _failed(payload)

    assert "float_type" in (result.error or "")


def test_third_blind_is_rejected_as_unsupported_straddle_structure() -> None:
    hand = heads_up_hand()
    hand["actions"].insert(
        2,
        {"street": "preflop", "actor": "a", "action": "post_blind", "amount": 4},
    )

    result = _failed(request(hand))

    assert result.error == "HandPotLedgerError: unsupported-blind-or-straddle-structure"


def test_non_integral_caller_unit_and_action_cap_fail_before_accounting() -> None:
    non_integral = _failed(request(heads_up_hand(), chip_unit="0.3"))
    assert non_integral.error == "HandPotLedgerError: amount-not-integral-in-chip-unit"

    hand = heads_up_hand()
    hand["actions"].extend(
        {"street": "preflop", "actor": "a", "action": "check", "amount": 0} for _ in range(509)
    )
    over_cap = _failed(request(hand))
    assert over_cap.error == "HandPotLedgerError: action-count-limit-exceeded"


def test_output_contract_rejects_conservation_and_oracle_tampering() -> None:
    result = default_registry().execute("hand_pot_ledger", request(heads_up_hand()))
    assert result.status is ToolStatus.SUCCESS
    output_model = contract_by_name()["hand_pot_ledger"].output_model

    corrupted = deepcopy(result.output)
    corrupted["final_pot_units"] = 5
    with pytest.raises(ValidationError, match="net-contribution-sum-invalid"):
        output_model.model_validate(corrupted)

    corrupted = deepcopy(result.output)
    corrupted["oracle_verified"] = False
    with pytest.raises(ValidationError):
        output_model.model_validate(corrupted)
