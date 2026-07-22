from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from poker_deliberation.budgets import FakeMonotonicClock, canonical_json_utf8_size
from poker_deliberation.reporting import render_markdown
from poker_deliberation.schemas import (
    Exactness,
    FinalReport,
    NumericalExactness,
    TolerancePolicy,
    ToolResult,
    ToolStatus,
)
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.contracts import contract_by_name, tool_contracts
from poker_deliberation.tools.registry import ToolDefinition, ToolRegistry
from poker_deliberation.tools.verification import within_tolerance
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
        if result.numeric_exactness is NumericalExactness.FLOATING_VERIFIED:
            assert result.verification is not None
            assert result.verification.checks == list(contract.verification_checks)
            assert result.verification.observations


def test_contract_version_mismatch_fails_without_output_promotion() -> None:
    result = default_registry().execute(
        "pot_odds", VALID_INPUTS["pot_odds"], contract_version="999.0.0"
    )
    assert result.status is ToolStatus.FAILED
    assert result.exactness is Exactness.UNAVAILABLE
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert result.output == {}
    assert "version mismatch" in (result.error or "")


def test_floating_verifier_rejects_schema_valid_but_wrong_output() -> None:
    contract = contract_by_name()["pot_odds"]
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=contract.name,
            purpose=contract.purpose,
            exact_or_approximate="floating-verified",
            supported_games=contract.supported_games,
            function=lambda _payload: {
                "pot_after_opponent_bet": 999.0,
                "final_pot_before_rake": 1000.0,
                "expected_rake": 0.0,
                "final_pot_after_rake": 1000.0,
                "required_equity": 0.5,
                "required_equity_percent": 50.0,
                "pot_odds_against": 1.0,
            },
            contract=contract,
        )
    )
    result = registry.execute("pot_odds", VALID_INPUTS["pot_odds"])
    assert result.status is ToolStatus.FAILED
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert result.verification is None
    assert "verification failed for pot_after_opponent_bet" in (result.error or "")


def test_floating_verifier_is_executable_not_declaration_only() -> None:
    canonical = contract_by_name()["pot_odds"]
    contract = replace(canonical, name="unverified_fixture")
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=contract.name,
            purpose=contract.purpose,
            exact_or_approximate="floating-verified",
            supported_games=contract.supported_games,
            function=lambda _payload: {
                "pot_after_opponent_bet": 150.0,
                "final_pot_before_rake": 200.0,
                "expected_rake": 0.0,
                "final_pot_after_rake": 200.0,
                "required_equity": 0.25,
                "required_equity_percent": 25.0,
                "pot_odds_against": 3.0,
            },
            contract=contract,
        )
    )
    result = registry.execute("unverified_fixture", VALID_INPUTS["pot_odds"])
    assert result.status is ToolStatus.FAILED
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert "no executable verifier" in (result.error or "")


@pytest.mark.parametrize(
    ("policy", "actual", "expected", "accepted"),
    [
        (
            TolerancePolicy(
                fields=["x"], kind="absolute", absolute=0.1, unit="u", rationale="test"
            ),
            1.05,
            1.0,
            True,
        ),
        (
            TolerancePolicy(
                fields=["x"], kind="relative", relative=0.1, unit="u", rationale="test"
            ),
            112.0,
            100.0,
            False,
        ),
        (
            TolerancePolicy(
                fields=["x"],
                kind="absolute-or-relative",
                absolute=0.1,
                relative=0.01,
                unit="u",
                rationale="test",
            ),
            0.05,
            0.0,
            True,
        ),
        (
            TolerancePolicy(fields=["x"], kind="ulp", ulps=1, unit="u", rationale="test"),
            math.nextafter(1.0, 2.0),
            1.0,
            True,
        ),
        (
            TolerancePolicy(
                fields=["x"],
                kind="caller-supplied",
                absolute=0.25,
                formula="input.tolerance",
                unit="u",
                rationale="test",
            ),
            2.3,
            2.0,
            False,
        ),
        (
            TolerancePolicy(
                fields=["x"], kind="absolute", absolute=0.01, unit="u", rationale="test"
            ),
            1_000_000_000_500.0,
            1_000_000_000_000.0,
            False,
        ),
    ],
    ids=(
        "absolute",
        "relative",
        "absolute-or-relative",
        "ulp",
        "caller-supplied",
        "no-hidden-relative",
    ),
)
def test_tolerance_kinds_have_executable_semantics(
    policy: TolerancePolicy,
    actual: float,
    expected: float,
    accepted: bool,
) -> None:
    assert within_tolerance(actual, expected, policy) is accepted


def test_ev_tree_rejects_non_normalized_probability_distribution() -> None:
    result = default_registry().execute(
        "ev_tree",
        {
            "root": "root",
            "nodes": {
                "root": {
                    "branches": [
                        {"probability": 0.5, "child": "zero"},
                        {"probability": 0.5000000005, "child": "large"},
                    ]
                },
                "zero": {"payoff": 0},
                "large": {"payoff": 1_000_000_000_000_000},
            },
        },
    )
    assert result.status is ToolStatus.FAILED
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert "must sum to 1" in (result.error or "")


@pytest.mark.parametrize("distribution_kind", ["chance", "fixed_strategy"])
def test_best_response_rejects_non_normalized_distributions(distribution_kind: str) -> None:
    if distribution_kind == "chance":
        root = {
            "type": "chance",
            "actions": {
                "a": {"probability": 0.5, "child": "zero"},
                "b": {"probability": 0.5000000005, "child": "large"},
            },
        }
        fixed_strategy: dict[str, dict[str, float]] = {}
    else:
        root = {
            "type": "player",
            "player": 1,
            "information_set": "villain",
            "actions": {"a": "zero", "b": "large"},
        }
        fixed_strategy = {"villain": {"a": 0.5, "b": 0.5000000005}}
    result = default_registry().execute(
        "fixed_strategy_best_response",
        {
            "game": {
                "root": "root",
                "nodes": {
                    "root": root,
                    "zero": {"type": "terminal", "payoff": 0},
                    "large": {"type": "terminal", "payoff": 1_000_000_000_000_000},
                },
            },
            "fixed_strategy": fixed_strategy,
        },
    )
    assert result.status is ToolStatus.FAILED
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert "probabilities must sum to 1" in (result.error or "")


def _hand_with_pot_difference(
    difference: float, *, tolerance: float | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "format": "cash",
        "table_size": 2,
        "small_blind": 1,
        "big_blind": 2,
        "players": [
            {"player_id": "hero", "position": "SB", "starting_stack": 1_000_000_000_000},
            {"player_id": "villain", "position": "BB", "starting_stack": 1_000_000_000_000},
        ],
        "actions": [
            {
                "street": "preflop",
                "actor": "hero",
                "action": "post_blind",
                "amount": 1_000_000_000_000,
                "pot_after": 1_000_000_000_000 + difference,
            }
        ],
    }
    if tolerance is not None:
        payload["tolerance"] = tolerance
    return payload


def test_hand_validator_uses_recorded_derived_tolerance_without_hidden_relative() -> None:
    result = default_registry().execute("hand_validator", _hand_with_pot_difference(500.0))
    assert result.status is ToolStatus.SUCCESS
    assert result.output["valid"] is False
    assert result.output["verification_tolerance"] == 0.00390625
    assert result.verification is not None
    assert result.verification.tolerance.kind == "ulp"
    assert result.verification.tolerance.absolute == 0.00390625
    assert any("pot_after" in error for error in result.output["errors"])


def test_hand_validator_applies_and_records_caller_tolerance() -> None:
    accepted = default_registry().execute(
        "hand_validator", _hand_with_pot_difference(0.5, tolerance=0.5)
    )
    rejected = default_registry().execute(
        "hand_validator", _hand_with_pot_difference(0.5001, tolerance=0.5)
    )
    assert accepted.output["valid"] is True
    assert rejected.output["valid"] is False
    assert accepted.verification is not None
    assert accepted.verification.tolerance.kind == "caller-supplied"
    assert accepted.verification.tolerance.absolute == 0.5


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


def test_v2_floating_verified_requires_passed_metadata_and_loads_pre_observation_v2() -> None:
    payload = {
        "tool_name": "floating-fixture",
        "input": {},
        "output": {"value": 1},
        "status": ToolStatus.SUCCESS,
        "exactness": Exactness.EXACT,
        "numeric_exactness": NumericalExactness.FLOATING_VERIFIED,
        "contract_version": "2.0.0",
    }
    with pytest.raises(ValidationError, match="passed verification metadata"):
        ToolResult.model_validate(payload)
    legacy_v2 = ToolResult.model_validate(
        {
            **payload,
            "verification": {
                "method": "pre-observation v2 artifact",
                "checks": ["declared check"],
                "tolerance": {
                    "fields": ["value"],
                    "kind": "ulp",
                    "ulps": 1,
                    "unit": "value unit",
                    "rationale": "fixture",
                },
                "passed": True,
            },
        }
    )
    assert legacy_v2.verification is not None
    assert legacy_v2.verification.observations == []


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


@pytest.mark.parametrize(
    ("status", "exactness", "expected"),
    [
        ("success", "exact", NumericalExactness.EXACT),
        ("success", "approximate", NumericalExactness.APPROXIMATE),
        ("unavailable", "unavailable", NumericalExactness.UNAVAILABLE),
    ],
)
def test_v1_tool_result_artifact_migrates_additively(
    status: str,
    exactness: str,
    expected: NumericalExactness,
) -> None:
    legacy = ToolResult.model_validate(
        {
            "tool_name": "legacy-fixture",
            "input": {},
            "output": {"value": 1} if status == "success" else {},
            "status": status,
            "exactness": exactness,
        }
    )
    assert legacy.contract_version == "1.0.0"
    assert legacy.exactness.value == exactness
    assert legacy.numeric_exactness is expected


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


def _size_test_definition(function=lambda _: {"value": 1}) -> ToolDefinition:  # type: ignore[no-untyped-def]
    return ToolDefinition(
        name="size-test",
        purpose="canonical size boundary fixture",
        exact_or_approximate="exact",
        supported_games=("fixture",),
        function=function,
    )


def test_registry_uses_canonical_json_bytes_at_exact_input_and_output_caps() -> None:
    payload = {"wide": "あ", "value": 1}
    output = {"value": 1}
    input_size = canonical_json_utf8_size(payload)
    output_size = canonical_json_utf8_size(output)

    exact = ToolRegistry(max_payload_bytes=input_size, max_output_bytes=output_size)
    exact.register(_size_test_definition())
    assert exact.execute("size-test", payload).status is ToolStatus.SUCCESS

    input_over = ToolRegistry(max_payload_bytes=input_size - 1)
    input_over.register(_size_test_definition())
    assert "input exceeds" in (input_over.execute("size-test", payload).error or "")

    output_over = ToolRegistry(max_output_bytes=output_size - 1)
    output_over.register(_size_test_definition())
    assert "output exceeds" in (output_over.execute("size-test", payload).error or "")


def test_registry_clock_rollback_returns_failed_tool_result() -> None:
    clock = FakeMonotonicClock(current_ns=10)

    def rollback(_: dict[str, object]) -> dict[str, object]:
        clock.set_ns(9)
        return {"value": 1}

    registry = ToolRegistry(monotonic_clock=clock)
    registry.register(_size_test_definition(rollback))

    result = registry.execute("size-test", {})

    assert result.status is ToolStatus.FAILED
    assert "moved backwards" in (result.error or "")
