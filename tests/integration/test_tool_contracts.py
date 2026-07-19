from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import (
    Exactness,
    FinalReport,
    NumericalExactness,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.contracts import contract_by_name, tool_contracts
from scripts.generate_tool_contracts import manifest_document, render_docs

ROOT = Path(__file__).resolve().parents[2]


VALID_INPUTS: dict[str, dict[str, object]] = {
    "pot_odds": {"pot_before_bet": 100, "opponent_bet": 50, "call_cost": 50},
    "break_even_fold": {"risk": 50, "reward": 100},
    "mdf": {"pot_before_bet": 100, "bet": 50},
    "spr": {"effective_stack": 100, "pot": 20},
    "effective_stack": {"stacks": [100, 80]},
    "rake_amount": {"pot_total": 100, "rake_percent": 5, "rake_cap": 3},
    "raked_call_ev": {
        "equity": 0.5,
        "pot_after_bet": 150,
        "call_cost": 50,
        "rake_percent": 5,
        "rake_cap": 3,
    },
    "bluff_ev": {"fold_frequency": 0.5, "pot_before_bet": 100, "bet": 50},
    "polar_river_bluff_fraction": {"pot_before_bet": 100, "bet": 100},
    "bayes_update": {
        "prior": 0.5,
        "likelihood_given_h": 0.8,
        "likelihood_given_not_h": 0.2,
    },
    "pot_reconstruction": {"starting_pot": 10, "contributions": [5, 15]},
    "combos": {"hand_class": "AA", "dead_cards": []},
    "holdem_equity": {
        "hero_range": "AsAh",
        "villain_range": "KcKd",
        "board": ["2c", "3d", "4h", "5s", "9c"],
        "mode": "exact",
    },
    "ev_tree": {"root": "end", "nodes": {"end": {"payoff": 2}}},
    "icm": {"stacks": [1, 1], "payouts": [1]},
    "matrix_game": {"matrix": [[1]]},
    "fixed_strategy_best_response": {
        "game": {"root": "end", "nodes": {"end": {"type": "terminal", "payoff": 2}}},
        "fixed_strategy": {},
    },
    "hand_validator": {
        "format": "cash",
        "table_size": 2,
        "small_blind": 1,
        "big_blind": 2,
        "players": [
            {"player_id": "hero", "position": "SB", "starting_stack": 100},
            {"player_id": "villain", "position": "BB", "starting_stack": 100},
        ],
    },
    "sensitivity": {"scenarios": [{"name": "base", "parameters": {"x": 1}, "value": 2}]},
    "solver_status": {},
}


def test_canonical_inventory_has_twenty_unique_complete_contracts() -> None:
    contracts = tool_contracts()
    assert len(contracts) == 20
    assert len({contract.name for contract in contracts}) == 20
    assert {contract.name for contract in contracts} == set(VALID_INPUTS)
    for contract in contracts:
        assert contract.assumptions is not None
        assert contract.preconditions
        assert contract.limits
        assert contract.failure_modes
        assert contract.version
        assert contract.contract_version == "2.0.0"
        assert contract.input_model.model_json_schema()
        assert contract.output_model.model_json_schema()


def test_manifest_docs_and_registry_are_full_canonical_projections() -> None:
    manifest = yaml.safe_load((ROOT / "tools" / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest == manifest_document()
    assert (ROOT / "docs" / "tool-contracts.md").read_text(encoding="utf-8") == render_docs()
    assert default_registry().describe() == sorted(
        [contract.manifest_entry() for contract in tool_contracts()], key=lambda item: item["name"]
    )


@pytest.mark.parametrize("tool_name", sorted(VALID_INPUTS))
def test_all_inputs_round_trip_and_reject_extra_fields(tool_name: str) -> None:
    contract = contract_by_name()[tool_name]
    payload = VALID_INPUTS[tool_name]
    validated = contract.input_model.model_validate(payload)
    round_tripped = validated.model_dump(mode="json", exclude_unset=True)
    contract.input_model.model_validate(round_tripped)
    with pytest.raises(ValidationError, match="extra"):
        contract.input_model.model_validate({**payload, "unexpected_contract_field": True})


@pytest.mark.parametrize("tool_name", sorted(VALID_INPUTS))
def test_all_required_input_fields_are_enforced(tool_name: str) -> None:
    contract = contract_by_name()[tool_name]
    required = contract.input_model.model_json_schema().get("required", [])
    if not required:
        if tool_name == "solver_status":
            contract.input_model.model_validate({})
        else:
            with pytest.raises(ValidationError):
                contract.input_model.model_validate({})
        return
    missing = dict(VALID_INPUTS[tool_name])
    missing.pop(str(required[0]))
    with pytest.raises(ValidationError, match="required"):
        contract.input_model.model_validate(missing)


@pytest.mark.parametrize("tool_name", sorted(VALID_INPUTS))
def test_registry_validates_every_output_against_its_typed_contract(tool_name: str) -> None:
    registry = default_registry()
    result = registry.execute(tool_name, VALID_INPUTS[tool_name])
    contract = contract_by_name()[tool_name]
    assert result.contract_version == contract.contract_version
    contract.output_model.model_validate(result.output)
    if tool_name == "solver_status":
        assert result.status is ToolStatus.UNAVAILABLE
        assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    else:
        assert result.status is ToolStatus.SUCCESS
        assert result.numeric_exactness in contract.numeric_exactness_modes


def test_contract_version_mismatch_fails_without_output_promotion() -> None:
    result = default_registry().execute(
        "pot_odds", VALID_INPUTS["pot_odds"], contract_version="999.0.0"
    )
    assert result.status is ToolStatus.FAILED
    assert result.exactness is Exactness.UNAVAILABLE
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert result.output == {}
    assert "version mismatch" in (result.error or "")


def test_approximate_success_requires_complete_metadata_for_both_methods() -> None:
    registry = default_registry()
    monte_carlo = registry.execute(
        "holdem_equity",
        {
            "hero_range": "AsAh",
            "villain_range": "KcKd",
            "mode": "monte_carlo",
            "samples": 10,
            "seed": 7,
        },
    )
    fallback = registry.execute(
        "matrix_game",
        {
            "matrix": [[0, -1, 1], [1, 0, -1], [-1, 1, 0]],
            "max_support_size": 1,
            "fallback_iterations": 100,
        },
    )
    for result in (monte_carlo, fallback):
        assert result.status is ToolStatus.SUCCESS
        assert result.numeric_exactness is NumericalExactness.APPROXIMATE
        assert result.method
        assert result.stochastic is not None
        assert (result.samples or 0) > 0 or (result.iterations or 0) > 0
        assert result.confidence_interval is not None or result.error_metadata is not None
        assert result.stopping_condition
    assert monte_carlo.seed == 7
    assert monte_carlo.confidence_level == 0.95
    assert fallback.stochastic is False
    assert fallback.error_metadata is not None
    assert fallback.error_metadata.metric == "duality_gap"


def test_v2_schema_rejects_invalid_status_and_approximation_combinations() -> None:
    base = {
        "tool_name": "fixture",
        "input": {},
        "output": {"value": 1},
        "status": ToolStatus.SUCCESS,
        "exactness": Exactness.APPROXIMATE,
        "numeric_exactness": NumericalExactness.APPROXIMATE,
        "contract_version": "2.0.0",
    }
    with pytest.raises(ValidationError, match="require method"):
        ToolResult.model_validate(base)
    with pytest.raises(ValidationError, match="successful results"):
        ToolResult.model_validate(
            {
                **base,
                "exactness": Exactness.UNAVAILABLE,
                "numeric_exactness": NumericalExactness.UNAVAILABLE,
            }
        )
    with pytest.raises(ValidationError, match="failed results cannot carry output"):
        ToolResult.model_validate(
            {
                **base,
                "status": ToolStatus.FAILED,
                "exactness": Exactness.UNAVAILABLE,
                "numeric_exactness": NumericalExactness.UNAVAILABLE,
            }
        )


def test_exact_under_model_requires_and_preserves_model_qualifier() -> None:
    payload = {
        "tool_name": "model-fixture",
        "input": {},
        "output": {"value": 1},
        "status": ToolStatus.SUCCESS,
        "exactness": Exactness.EXACT,
        "numeric_exactness": NumericalExactness.EXACT_UNDER_MODEL,
        "contract_version": "2.0.0",
    }
    with pytest.raises(ValidationError, match="model_qualifier"):
        ToolResult.model_validate(payload)
    result = ToolResult.model_validate(
        {**payload, "model_qualifier": "finite declared fixture model"}
    )
    assert result.numeric_exactness is NumericalExactness.EXACT_UNDER_MODEL
    assert result.exactness is Exactness.EXACT


def test_v1_tool_result_artifact_migrates_additively() -> None:
    legacy = ToolResult.model_validate(
        {
            "tool_name": "legacy-fixture",
            "input": {},
            "output": {"value": 1},
            "status": "success",
            "exactness": "exact",
        }
    )
    assert legacy.contract_version == "1.0.0"
    assert legacy.exactness is Exactness.EXACT
    assert legacy.numeric_exactness is NumericalExactness.EXACT


def test_markdown_exposes_every_v2_result_metadata_value() -> None:
    result = default_registry().execute("pot_odds", VALID_INPUTS["pot_odds"])
    assert any("compatibility projection" in warning for warning in result.warnings)
    rendered = render_markdown(
        FinalReport(run_id="contract-parity", conclusion="fixture", tool_results=[result])
    )
    serialized = json.loads(result.model_dump_json())
    assert serialized["numeric_exactness"] in rendered
    assert serialized["contract_version"] in rendered
    assert serialized["verification"]["method"] in rendered
    assert "- 数値区分:" in rendered
    assert "- 検証metadata:" in rendered
