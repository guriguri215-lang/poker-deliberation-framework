from __future__ import annotations

import json
import math
import time
from dataclasses import replace
from pathlib import Path
from threading import Thread

import pytest
import yaml
from pydantic import ValidationError

import poker_deliberation.tools.registry as registry_module
from poker_deliberation.budgets import (
    BudgetFailure,
    BudgetFailureCode,
    FakeMonotonicClock,
    canonical_json_utf8_size,
)
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
from poker_deliberation.tools.contracts import (
    contract_by_name,
    tool_contracts,
    versioned_range_bridge_failure_error,
)
from poker_deliberation.tools.icm import calculate_icm
from poker_deliberation.tools.registry import ToolDefinition, ToolRegistry
from poker_deliberation.tools.verification import within_tolerance
from scripts.generate_tool_contracts import main as generate_tool_contracts
from scripts.generate_tool_contracts import manifest_document, render_docs
from tests.hand_pot_ledger_support import heads_up_hand, request
from tests.range_support import versioned_range_hand

ROOT = Path(__file__).resolve().parents[2]
RANGE_HAND, RANGE_DEFINITION = versioned_range_hand()


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
    "range_validate": {
        "schema_version": "1.0.0",
        "hand": RANGE_HAND.model_dump(mode="json"),
        "range_definition": RANGE_DEFINITION.model_dump(mode="json"),
    },
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
    "hand_pot_ledger": request(heads_up_hand()),
    "sensitivity": {"scenarios": [{"name": "base", "parameters": {"x": 1}, "value": 2}]},
    "solver_status": {},
}


def _phase_hanging_tool(_payload: dict[str, object]) -> dict[str, object]:
    time.sleep(60.0)
    return {"value": 0}


def _phase_success_tool(_payload: dict[str, object]) -> dict[str, object]:
    return {"value": 1}


def _phase_wrong_pot_odds_tool(_payload: dict[str, object]) -> dict[str, object]:
    return {
        "pot_after_opponent_bet": 999.0,
        "final_pot_before_rake": 1000.0,
        "expected_rake": 0.0,
        "final_pot_after_rake": 1000.0,
        "required_equity": 0.5,
        "required_equity_percent": 50.0,
        "pot_odds_against": 1.0,
    }


class _VerifierHangingOutput(dict[str, object]):
    def __getitem__(self, key: str) -> object:
        if key == "pot_after_opponent_bet":
            time.sleep(60.0)
        return super().__getitem__(key)


def _phase_verifier_hanging_pot_odds_tool(
    _payload: dict[str, object],
) -> dict[str, object]:
    return _VerifierHangingOutput(
        {
            "pot_after_opponent_bet": 150.0,
            "final_pot_before_rake": 200.0,
            "expected_rake": 0.0,
            "final_pot_after_rake": 200.0,
            "required_equity": 0.25,
            "required_equity_percent": 25.0,
            "pot_odds_against": 3.0,
        }
    )


def test_canonical_inventory_has_twenty_two_unique_complete_contracts() -> None:
    contracts = tool_contracts()
    assert len(contracts) == 22
    assert len({contract.name for contract in contracts}) == 22
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
    assert generate_tool_contracts(["--check"]) == 0


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


def test_icm_verifier_rejects_schema_valid_error_beyond_derived_bound() -> None:
    contract = contract_by_name()["icm"]

    def injected_icm(_payload: dict[str, object]) -> dict[str, object]:
        output = calculate_icm([5.0, 3.0, 2.0], [9.0, 4.0, 1.0])
        equities = list(map(float, output["equities"]))
        tolerance = float(output["verification_tolerance"])
        equities[0] += 2 * tolerance
        equity_sum = sum(equities)
        payable = float(output["payable_prize_sum"])
        return {
            **output,
            "equities": equities,
            "equity_sum": equity_sum,
            "sum_error": equity_sum - payable,
            "conservation_verified": True,
        }

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name=contract.name,
            purpose=contract.purpose,
            exact_or_approximate="floating-verified",
            supported_games=contract.supported_games,
            function=injected_icm,
            contract=contract,
        )
    )

    result = registry.execute("icm", {"stacks": [5, 3, 2], "payouts": [9, 4, 1]})

    assert result.status is ToolStatus.FAILED
    assert result.numeric_exactness is NumericalExactness.UNAVAILABLE
    assert result.verification is None
    assert "verification failed for prize_conservation" in (result.error or "")


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


def test_registry_preserves_measurable_duration_for_failed_tool() -> None:
    clock = FakeMonotonicClock()

    def fail_after_work(_: dict[str, object]) -> dict[str, object]:
        clock.advance_ns(100_000_000)
        raise ValueError("deterministic fixture failure")

    registry = ToolRegistry(monotonic_clock=clock)
    registry.register(_size_test_definition(fail_after_work))

    result = registry.execute("size-test", {})

    assert result.status is ToolStatus.FAILED
    assert result.duration_seconds == 0.1


def test_public_execute_terminates_isolated_worker_and_registry_remains_usable() -> None:
    registry = ToolRegistry(max_duration_seconds=2.0)
    registry.register(
        ToolDefinition(
            name="hang",
            purpose="hard timeout fixture",
            exact_or_approximate="exact",
            supported_games=("fixture",),
            function=_phase_hanging_tool,
            phase_isolated=True,
        )
    )
    registry.register(
        ToolDefinition(
            name="after-timeout",
            purpose="post-timeout success fixture",
            exact_or_approximate="exact",
            supported_games=("fixture",),
            function=_phase_success_tool,
            phase_isolated=True,
        )
    )

    started = time.monotonic()
    timed_out = registry.execute("hang", {})
    elapsed = time.monotonic() - started
    after_timeout = registry.execute("after-timeout", {})

    assert elapsed < 6.0
    assert timed_out.status is ToolStatus.FAILED
    assert "exceeded hard runtime limit" in (timed_out.error or "")
    assert after_timeout.status is ToolStatus.SUCCESS
    assert after_timeout.output == {"value": 1}


def test_public_execute_terminates_worker_when_floating_verifier_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = contract_by_name()["pot_odds"]
    monkeypatch.setitem(
        registry_module._CANONICAL_PHASE_FUNCTIONS,
        contract.name,
        _phase_verifier_hanging_pot_odds_tool,
    )
    registry = ToolRegistry(max_duration_seconds=2.0)
    registry.register(
        ToolDefinition(
            name=contract.name,
            purpose=contract.purpose,
            exact_or_approximate="floating-verified",
            supported_games=contract.supported_games,
            function=_phase_verifier_hanging_pot_odds_tool,
            assumptions=contract.assumptions,
            version=contract.version,
            contract=contract,
            phase_isolated=True,
        )
    )

    started = time.monotonic()
    result = registry.execute(contract.name, VALID_INPUTS[contract.name])
    elapsed = time.monotonic() - started

    assert elapsed < 6.0
    assert result.status is ToolStatus.FAILED
    assert "exceeded hard runtime limit" in (result.error or "")


def test_phase_isolation_rejects_non_picklable_callable_before_execution() -> None:
    executed = False

    def local_callable(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal executed
        executed = True
        return {"value": 1}

    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="local-callable",
            purpose="spawn qualification fixture",
            exact_or_approximate="exact",
            supported_games=("fixture",),
            function=local_callable,
            phase_isolated=True,
        )
    )

    result = registry.execute_for_phase("local-callable", {})

    assert result.status is ToolStatus.FAILED
    assert "not spawn-picklable" in (result.error or "")
    assert not executed


def test_phase_isolated_output_is_verified_by_parent_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = contract_by_name()["pot_odds"]
    monkeypatch.setitem(
        registry_module._CANONICAL_PHASE_FUNCTIONS,
        contract.name,
        _phase_wrong_pot_odds_tool,
    )
    registry = ToolRegistry(max_duration_seconds=5.0)
    registry.register(
        ToolDefinition(
            name=contract.name,
            purpose=contract.purpose,
            exact_or_approximate="floating-verified",
            supported_games=contract.supported_games,
            function=_phase_wrong_pot_odds_tool,
            contract=contract,
            phase_isolated=True,
        )
    )

    result = registry.execute_for_phase(contract.name, VALID_INPUTS["pot_odds"])

    assert result.status is ToolStatus.FAILED
    assert result.verification is None
    assert "verification failed for pot_after_opponent_bet" in (result.error or "")


def test_materialized_exact_result_is_replayed_before_trust() -> None:
    registry = default_registry()
    result = registry.execute("combos", VALID_INPUTS["combos"])
    mutated = result.model_copy(
        update={"output": {**result.output, "count": result.output["count"] + 1}}
    )

    with pytest.raises(ValueError, match="canonical replay"):
        registry.reverify_materialized_result(mutated)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", "999.0.0"),
        ("assumptions", ["forged"]),
        ("model_qualifier", "forged"),
        ("reproduce_command", "forged"),
    ],
)
def test_materialized_floating_result_rejects_contract_metadata_tampering(
    field: str,
    value: object,
) -> None:
    registry = default_registry()
    result = registry.execute("pot_odds", VALID_INPUTS["pot_odds"])
    mutated = result.model_copy(update={field: value})

    with pytest.raises(ValueError, match="metadata"):
        registry.reverify_materialized_result(mutated)


def test_materialized_floating_result_rejects_verification_tampering() -> None:
    registry = default_registry()
    result = registry.execute("pot_odds", VALID_INPUTS["pot_odds"])
    assert result.verification is not None
    mutated = result.model_copy(
        update={
            "verification": result.verification.model_copy(
                update={"observations": ["self-attested"]}
            )
        }
    )

    with pytest.raises(ValueError, match="canonical replay"):
        registry.reverify_materialized_result(mutated)


def test_materialized_result_rejects_json_distinct_integer_for_float_tampering() -> None:
    registry = default_registry()
    result = registry.execute("pot_odds", VALID_INPUTS["pot_odds"])
    original = result.output["final_pot_before_rake"]
    assert isinstance(original, float) and original.is_integer()
    mutated = result.model_copy(
        update={
            "output": {
                **result.output,
                "final_pot_before_rake": int(original),
            }
        }
    )
    assert result.output == mutated.output

    with pytest.raises(ValueError, match="canonical JSON representation"):
        registry.reverify_materialized_result(mutated)


def test_materialized_unknown_failed_result_is_rejected() -> None:
    result = ToolResult(
        tool_name="unknown",
        input={},
        status=ToolStatus.FAILED,
        exactness=Exactness.UNAVAILABLE,
        numeric_exactness=NumericalExactness.UNAVAILABLE,
        error="forged failure",
    )

    with pytest.raises(ValueError, match="no canonical replay"):
        default_registry().reverify_materialized_result(result)


def test_materialized_normal_failed_result_requires_exact_replay() -> None:
    registry = default_registry()
    result = registry.execute("pot_odds", {"pot_before_bet": 100})
    assert result.status is ToolStatus.FAILED

    registry.reverify_materialized_result(result)

    with pytest.raises(ValueError, match="canonical replay"):
        registry.reverify_materialized_result(result.model_copy(update={"error": "forged failure"}))


@pytest.mark.parametrize("tool_name", ["range_validate", "combos", "holdem_equity"])
def test_materialized_versioned_range_failure_requires_exact_replay(
    tool_name: str,
) -> None:
    registry = default_registry()
    contract = contract_by_name()[tool_name]
    success = registry.execute(
        tool_name,
        VALID_INPUTS[tool_name],
        contract_version=contract.contract_version,
    )
    assert success.status is ToolStatus.SUCCESS
    forged = ToolResult(
        result_id=success.result_id,
        tool_name=tool_name,
        input=success.input,
        status=ToolStatus.FAILED,
        exactness=Exactness.UNAVAILABLE,
        numeric_exactness=NumericalExactness.UNAVAILABLE,
        contract_version=contract.contract_version,
        assumptions=list(contract.assumptions),
        version=contract.version,
        error=versioned_range_bridge_failure_error(tool_name),
        reproduce_command=(
            f"poker-deliberate calculate {tool_name} "
            "--analysis-scope retrospective --input <input.json>"
        ),
    )

    with pytest.raises(ValueError, match="canonical replay"):
        registry.reverify_materialized_result(forged)


def test_fresh_phase_failure_authority_is_exact_and_one_shot() -> None:
    registry = default_registry(max_duration_seconds=0.001)
    result = registry.execute_for_phase(
        "hand_validator",
        VALID_INPUTS["hand_validator"],
        contract_version=contract_by_name()["hand_validator"].contract_version,
    )
    assert result.status is ToolStatus.FAILED

    forged = result.model_copy(update={"error": "forged failure"})
    with pytest.raises(ValueError, match="immediately executed result"):
        registry.reverify_materialized_result(
            forged,
            allow_fresh_execution_failure=True,
        )

    fresh = registry.execute_for_phase(
        "hand_validator",
        VALID_INPUTS["hand_validator"],
        contract_version=contract_by_name()["hand_validator"].contract_version,
    )
    assert fresh.status is ToolStatus.FAILED
    registry.reverify_materialized_result(
        fresh,
        allow_fresh_execution_failure=True,
    )
    with pytest.raises(ValueError, match="immediate execution authority"):
        registry.reverify_materialized_result(
            fresh,
            allow_fresh_execution_failure=True,
        )


def test_fresh_phase_failure_snapshot_rejects_nested_in_place_mutation() -> None:
    registry = default_registry(max_duration_seconds=0.001)
    result = registry.execute_for_phase(
        "hand_validator",
        VALID_INPUTS["hand_validator"],
        contract_version=contract_by_name()["hand_validator"].contract_version,
    )
    assert result.status is ToolStatus.FAILED
    result.input["players"][0]["stack"] = 999  # type: ignore[index]

    with pytest.raises(ValueError, match="immediately executed result"):
        registry.reverify_materialized_result(
            result,
            allow_fresh_execution_failure=True,
        )


def test_fresh_phase_failure_rejects_cross_thread_consumption() -> None:
    registry = default_registry(max_duration_seconds=0.001)
    result = registry.execute_for_phase(
        "hand_validator",
        VALID_INPUTS["hand_validator"],
        contract_version=contract_by_name()["hand_validator"].contract_version,
    )
    failures: list[Exception] = []

    def consume() -> None:
        try:
            registry.reverify_materialized_result(
                result,
                allow_fresh_execution_failure=True,
            )
        except Exception as exc:
            failures.append(exc)

    thread = Thread(target=consume)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert "another thread" in str(failures[0])


def test_fresh_phase_success_avoids_only_the_immediate_duplicate_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    original = ToolRegistry._execute_isolated

    def counted(self: ToolRegistry, *args: object, **kwargs: object) -> ToolResult:
        nonlocal calls
        calls += 1
        return original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ToolRegistry, "_execute_isolated", counted)
    registry = default_registry()
    contract = contract_by_name()["pot_odds"]
    result = registry.execute_for_phase(
        "pot_odds",
        VALID_INPUTS["pot_odds"],
        contract_version=contract.contract_version,
    )
    assert result.status is ToolStatus.SUCCESS
    assert calls == 1

    registry.reverify_materialized_result(
        result,
        allow_fresh_phase_success=True,
    )
    assert calls == 1

    # The authority is one-shot. A later or storage-style verification still
    # performs a canonical hard replay rather than trusting the old result.
    registry.reverify_materialized_result(
        result,
        allow_fresh_phase_success=True,
    )
    assert calls == 2


def test_fresh_phase_success_rejects_mutation_before_consuming_authority() -> None:
    registry = default_registry()
    contract = contract_by_name()["pot_odds"]
    result = registry.execute_for_phase(
        "pot_odds",
        VALID_INPUTS["pot_odds"],
        contract_version=contract.contract_version,
    )
    assert result.status is ToolStatus.SUCCESS
    mutated = result.model_copy(update={"output": {**result.output, "required_equity": 0.99}})

    with pytest.raises(ValueError, match="immediately executed phase result"):
        registry.reverify_materialized_result(
            mutated,
            allow_fresh_phase_success=True,
        )


def test_fresh_phase_success_snapshot_rejects_nested_in_place_mutation() -> None:
    registry = default_registry()
    contract = contract_by_name()["pot_odds"]
    result = registry.execute_for_phase(
        "pot_odds",
        VALID_INPUTS["pot_odds"],
        contract_version=contract.contract_version,
    )
    assert result.status is ToolStatus.SUCCESS
    result.output["required_equity"] = 0.99

    with pytest.raises(ValueError, match="immediately executed phase result"):
        registry.reverify_materialized_result(
            result,
            allow_fresh_phase_success=True,
        )


def test_fresh_phase_success_rejects_result_id_change() -> None:
    registry = default_registry()
    contract = contract_by_name()["pot_odds"]
    result = registry.execute_for_phase(
        "pot_odds",
        VALID_INPUTS["pot_odds"],
        contract_version=contract.contract_version,
    )

    with pytest.raises(ValueError, match="result ID changed"):
        registry.reverify_materialized_result(
            result.model_copy(update={"result_id": "tool-result-replaced"}),
            allow_fresh_phase_success=True,
        )


def test_fresh_phase_success_rejects_cross_thread_consumption() -> None:
    registry = default_registry()
    contract = contract_by_name()["pot_odds"]
    result = registry.execute_for_phase(
        "pot_odds",
        VALID_INPUTS["pot_odds"],
        contract_version=contract.contract_version,
    )
    failures: list[Exception] = []

    def consume() -> None:
        try:
            registry.reverify_materialized_result(
                result,
                allow_fresh_phase_success=True,
            )
        except Exception as exc:
            failures.append(exc)

    thread = Thread(target=consume)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert len(failures) == 1
    assert "another thread" in str(failures[0])


def test_fresh_custom_nonisolated_success_is_immediate_and_one_shot() -> None:
    calls = 0

    def custom_tool(_payload: dict[str, object]) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"value": 1}

    registry = ToolRegistry()
    registry.register(_size_test_definition(custom_tool))
    result = registry.execute_for_phase("size-test", {})

    assert result.status is ToolStatus.SUCCESS
    registry.reverify_materialized_result(
        result,
        allow_fresh_phase_success=True,
    )
    assert calls == 1

    with pytest.raises(ValueError, match="no canonical replay"):
        registry.reverify_materialized_result(
            result,
            allow_fresh_phase_success=True,
        )
    assert calls == 1

    fresh = registry.execute_for_phase("size-test", {})
    with pytest.raises(ValueError, match="no canonical replay"):
        registry.reverify_materialized_result(fresh)


def test_fresh_custom_nonisolated_success_rejects_tamper_and_consumes_authority() -> None:
    registry = ToolRegistry()
    registry.register(_size_test_definition())
    result = registry.execute_for_phase("size-test", {})
    mutated = result.model_copy(update={"output": {"value": 2}})

    with pytest.raises(ValueError, match="immediately executed phase result"):
        registry.reverify_materialized_result(
            mutated,
            allow_fresh_phase_success=True,
        )
    with pytest.raises(ValueError, match="no canonical replay"):
        registry.reverify_materialized_result(
            result,
            allow_fresh_phase_success=True,
        )


def test_budget_failed_result_requires_explicit_storage_authority() -> None:
    contract = contract_by_name()["pot_odds"]
    result = ToolResult(
        tool_name=contract.name,
        input=dict(VALID_INPUTS[contract.name]),
        status=ToolStatus.FAILED,
        exactness=Exactness.UNAVAILABLE,
        numeric_exactness=NumericalExactness.UNAVAILABLE,
        contract_version=contract.contract_version,
        error="strict budget failure: tool_input_exceeded",
    )
    registry = default_registry()

    with pytest.raises(ValueError, match="storage authority"):
        registry.reverify_materialized_result(result)
    authority = BudgetFailure(
        code=BudgetFailureCode.TOOL_INPUT_EXCEEDED,
        resource="tool_input_bytes",
        message="tool input exceeded its strict budget",
        limit=10,
        observed=11,
    )
    registry.reverify_materialized_result(
        result,
        authoritative_budget_failure=authority,
    )
    with pytest.raises(ValueError, match="differs from its authority"):
        registry.reverify_materialized_result(
            result,
            authoritative_budget_failure=authority.model_copy(
                update={"code": BudgetFailureCode.TOOL_OUTPUT_EXCEEDED}
            ),
        )
