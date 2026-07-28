from __future__ import annotations

import pytest
from pydantic import ValidationError

from poker_deliberation.normalization import (
    NORMALIZATION_PARSER_VERSION,
    NORMALIZATION_SUPPORTED_SITE,
)
from poker_deliberation.schemas import CanonicalHand, NumericalExactness, ToolStatus
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.contracts import contract_by_name
from tests.hand_pot_ledger_support import heads_up_hand, request


def test_legacy_canonical_hand_and_normalization_v1_have_no_implicit_profile() -> None:
    assert NORMALIZATION_PARSER_VERSION == "1.0.0"
    assert NORMALIZATION_SUPPORTED_SITE == "none"
    fields = set(CanonicalHand.model_fields)
    assert "rule_profile" not in fields
    assert "chip_unit" not in fields
    assert "profile_version" not in fields


def test_legacy_hand_validator_semantics_remain_separate_and_floating_verified() -> None:
    hand = heads_up_hand()
    legacy = default_registry().execute("hand_validator", hand)
    ledger = default_registry().execute("hand_pot_ledger", request(hand))

    assert legacy.status is ToolStatus.SUCCESS
    assert legacy.numeric_exactness is NumericalExactness.FLOATING_VERIFIED
    assert legacy.output["valid"] is True
    assert legacy.output["final_pot"] == 4
    assert "pot_layers" not in legacy.output
    assert "uncalled_returns" not in legacy.output
    assert ledger.status is ToolStatus.SUCCESS
    assert ledger.numeric_exactness is NumericalExactness.EXACT_UNDER_MODEL


def test_profile_and_ledger_contracts_are_strict_frozen_and_versioned() -> None:
    contract = contract_by_name()["hand_pot_ledger"]
    validated = contract.input_model.model_validate(request(heads_up_hand()))

    assert validated.schema_version == "1.0.0"
    assert validated.rule_profile.schema_version == "1.0.0"
    assert validated.rule_profile.profile_version == "1.0.0"
    with pytest.raises(ValidationError, match="frozen"):
        validated.rule_profile.profile_id = "another_profile"
    with pytest.raises(ValidationError, match="extra"):
        contract.input_model.model_validate(
            {
                **request(heads_up_hand()),
                "implicit_profile_default": True,
            }
        )

    result = default_registry().execute("hand_pot_ledger", request(heads_up_hand()))
    output = contract.output_model.model_validate(result.output)
    assert output.schema_version == "1.0.0"
    assert {item.schema_version for item in output.ledger_actions} == {"1.0.0"}
    assert {item.schema_version for item in output.pot_layers} == {"1.0.0"}
    assert {item.schema_version for item in output.player_eligibility} == {"1.0.0"}
