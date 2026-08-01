from __future__ import annotations

from dataclasses import replace
from itertools import combinations

import pytest

from poker_deliberation.range_equity import (
    VersionedRangeRiverEquityError,
    admit_versioned_range_river_equity,
    build_versioned_range_river_equity_result,
    expected_versioned_range_equity_input,
    verify_versioned_range_river_equity_binding_artifact,
    verify_versioned_range_river_equity_tool_chain,
    versioned_range_river_equity_binding,
)
from poker_deliberation.range_equity_models import (
    BINDING_HASH_DOMAIN,
    ORACLE_HASH_DOMAIN,
    RANGE_EQUITY_MAX_EVALUATIONS,
    RESULT_HASH_DOMAIN,
    VersionedRangeRiverEquityBindingV1,
    VersionedRangeRiverEquityResultV1,
    canonical_domain_sha256,
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
from poker_deliberation.tools import default_registry
from poker_deliberation.tools.cards import DECK
from poker_deliberation.tools.contracts import versioned_range_bridge_failure_error
from poker_deliberation.tools.numeric import close_ulps
from tests.range_support import versioned_river_equity_case


def _run_bound_tools(case: CaseInput) -> list[ToolResult]:
    registry = default_registry()
    assert case.hand is not None
    definition = case.hand.known_ranges[0]
    assert isinstance(definition, VersionedRangeDefinitionV1)
    validation_input = {
        "schema_version": "1.0.0",
        "hand": case.hand.model_dump(mode="json"),
        "range_definition": definition.model_dump(mode="json"),
    }
    validation_result = registry.execute(
        "range_validate",
        validation_input,
        contract_version="2.0.0",
    )
    validation = RangeValidationResultV1.model_validate(validation_result.output)
    combos_result = registry.execute(
        "combos",
        {"range": validation.canonical_notation, "dead_cards": []},
        contract_version="2.0.0",
    )
    equity_result = registry.execute(
        "holdem_equity",
        expected_versioned_range_equity_input(case, validation),
        contract_version="2.0.0",
    )
    return [validation_result, combos_result, equity_result]


def test_admission_binds_exact_river_range_and_oracle() -> None:
    candidate = versioned_river_equity_case()

    admission = admit_versioned_range_river_equity(candidate)

    assert admission.binding.tool_plan == ("range_validate", "combos", "holdem_equity")
    assert admission.binding.exact_evaluation_cap == RANGE_EQUITY_MAX_EVALUATIONS
    assert admission.binding.range_id == "villain-river"
    assert admission.binding.target_player_id == "villain"
    assert admission.case.metadata["versioned_range_river_equity"]["binding_sha256"] == (
        admission.binding.binding_sha256
    )


def test_exact_integer_oracle_preserves_weighted_win_and_loss() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)

    bridge = build_versioned_range_river_equity_result(admission.case, results)

    assert bridge.combo_count == 2
    assert bridge.win_combo_count == 1
    assert bridge.loss_combo_count == 1
    assert bridge.tie_combo_count == 0
    assert bridge.win_weight_millionths == 750_000
    assert bridge.loss_weight_millionths == 250_000
    assert (bridge.equity_numerator, bridge.equity_denominator) == (3, 4)
    assert bridge.legacy_hero_equity == 0.75
    assert bridge.oracle_numeric_exactness == "exact"
    assert bridge.legacy_numeric_exactness == "floating-verified"


def test_exact_integer_oracle_preserves_weighted_ties() -> None:
    candidate = versioned_river_equity_case(
        "4d5d@0.2,6h7h@0.8",
        hero_cards=("2d", "3d"),
        board=("Ac", "Kc", "Qc", "Jc", "Tc"),
    )
    admission = admit_versioned_range_river_equity(candidate)

    bridge = build_versioned_range_river_equity_result(
        admission.case,
        _run_bound_tools(admission.case),
    )

    assert bridge.tie_combo_count == 2
    assert bridge.tie_weight_millionths == 1_000_000
    assert (bridge.equity_numerator, bridge.equity_denominator) == (1, 2)
    assert bridge.legacy_hero_equity == 0.5


def test_extreme_990_combo_weights_remain_within_legacy_ulp_contract() -> None:
    hero_cards = ("2c", "Jd")
    board = ("Kd", "8h", "4c", "5d", "Qh")
    available = [card for card in DECK if card not in {*hero_cards, *board}]
    dominant = tuple(sorted(("2d", "3h")))
    notation = ",".join(
        f"{first}{second}@{'1' if tuple(sorted((first, second))) == dominant else '0.000001'}"
        for first, second in combinations(available, 2)
    )
    candidate = versioned_river_equity_case(
        notation,
        hero_cards=hero_cards,
        board=board,
    )
    admission = admit_versioned_range_river_equity(candidate)

    bridge = build_versioned_range_river_equity_result(
        admission.case,
        _run_bound_tools(admission.case),
    )

    assert bridge.combo_count == 990
    assert (bridge.win_combo_count, bridge.tie_combo_count, bridge.loss_combo_count) == (
        204,
        21,
        765,
    )
    assert (
        bridge.win_weight_millionths,
        bridge.tie_weight_millionths,
        bridge.loss_weight_millionths,
    ) == (1_000_203, 21, 765)
    assert (bridge.equity_numerator, bridge.equity_denominator) == (60_619, 60_666)
    assert close_ulps(
        bridge.legacy_hero_equity,
        bridge.equity_numerator / bridge.equity_denominator,
        ulps=128,
    )


def test_admission_rejects_manual_tool_inputs() -> None:
    candidate = versioned_river_equity_case(
        metadata={"tool_inputs": {"holdem_equity": {"villain_range": "AA"}}}
    )

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_TOOL_PLAN"):
        admit_versioned_range_river_equity(candidate)


def test_admission_rejects_provenance_changed_by_default_redaction() -> None:
    candidate = versioned_river_equity_case()
    payload = candidate.model_dump(mode="python")
    payload["hand"]["known_ranges"][0]["source"]["source_id"] = "sk-abcdefgh"

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_PROVENANCE"):
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))


def test_admission_rejects_non_nfc_candidate_before_storage() -> None:
    payload = versioned_river_equity_case().model_dump(mode="python")
    payload["case_id"] = "case-e\u0301"

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_SCHEMA"):
        admit_versioned_range_river_equity(CaseInput.model_validate(payload))


@pytest.mark.parametrize(
    "metadata_value",
    (
        object(),
        {"unsupported": object()},
    ),
)
def test_admission_maps_non_json_metadata_to_stable_schema_error(
    metadata_value: object,
) -> None:
    payload = versioned_river_equity_case().model_dump(mode="python")
    payload["metadata"]["extra"] = metadata_value
    candidate = CaseInput.model_validate(payload, strict=True)

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        admit_versioned_range_river_equity(candidate)

    assert error.value.code.value == "REQ_E_SCHEMA"


def test_admission_maps_recursive_metadata_to_stable_schema_error() -> None:
    payload = versioned_river_equity_case().model_dump(mode="python")
    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    payload["metadata"]["extra"] = recursive
    candidate = CaseInput.model_validate(payload, strict=True)

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        admit_versioned_range_river_equity(candidate)

    assert error.value.code.value == "REQ_E_SCHEMA"


def test_binding_parser_maps_non_json_marker_to_stable_schema_error() -> None:
    candidate = versioned_river_equity_case()
    payload = candidate.model_dump(mode="python")
    payload["metadata"] = {"versioned_range_river_equity": {1}}
    case = CaseInput.model_validate(payload)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_SCHEMA"):
        versioned_range_river_equity_binding(case)


def test_empty_tool_inputs_preserve_candidate_replay() -> None:
    candidate = versioned_river_equity_case(metadata={"tool_inputs": {}})
    admission = admit_versioned_range_river_equity(candidate)

    bridge = build_versioned_range_river_equity_result(
        admission.case,
        _run_bound_tools(admission.case),
    )

    assert admission.case.metadata["tool_inputs"] == {}
    assert (bridge.equity_numerator, bridge.equity_denominator) == (3, 4)


def test_result_builder_rejects_schema_invalid_combos_output() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    forged_output = dict(results[1].output)
    forged_output["unexpected"] = "field"
    results[1] = results[1].model_copy(update={"output": forged_output})

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        build_versioned_range_river_equity_result(admission.case, results)


def test_result_builder_rejects_monte_carlo_metadata_on_exact_output() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    forged_output = dict(results[2].output)
    forged_output.update(
        {
            "samples": 1,
            "seed": 7,
            "wins": 1,
            "ties": 0,
            "losses": 0,
            "estimated_exact_evaluations": 1,
        }
    )
    results[2] = results[2].model_copy(update={"output": forged_output})

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        build_versioned_range_river_equity_result(admission.case, results)


def test_result_builder_rejects_monte_carlo_top_level_envelope_on_exact_output() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    payload = results[2].model_dump(mode="python")
    payload.update(
        {
            "method": "monte_carlo",
            "stochastic": True,
            "seed": 7,
            "samples": 1,
            "confidence_interval": (0.0, 1.0),
            "confidence_level": 0.95,
            "stopping_condition": "fixed requested sample count",
        }
    )
    results[2] = ToolResult.model_validate(payload, strict=True)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        build_versioned_range_river_equity_result(admission.case, results)


def test_failed_equity_replay_rejects_monte_carlo_top_level_envelope() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    payload = results[2].model_dump(mode="python")
    payload.update(
        {
            "status": ToolStatus.FAILED,
            "exactness": Exactness.UNAVAILABLE,
            "numeric_exactness": NumericalExactness.UNAVAILABLE,
            "output": {},
            "verification": None,
            "error": "synthetic failure",
            "method": "monte_carlo",
            "stochastic": True,
            "seed": 7,
            "samples": 1,
            "confidence_interval": (0.0, 1.0),
            "confidence_level": 0.95,
            "stopping_condition": "fixed requested sample count",
        }
    )
    results[2] = ToolResult.model_validate(payload, strict=True)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            results,
            run_status="failed_with_limitations",
        )


@pytest.mark.parametrize("failed_index", [0, 1], ids=["range_validate", "combos"])
def test_failed_prerequisite_replay_rejects_monte_carlo_top_level_envelope(
    failed_index: int,
) -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    payload = results[failed_index].model_dump(mode="python")
    payload.update(
        {
            "status": ToolStatus.FAILED,
            "exactness": Exactness.UNAVAILABLE,
            "numeric_exactness": NumericalExactness.UNAVAILABLE,
            "output": {},
            "model_qualifier": None,
            "verification": None,
            "warnings": [],
            "error": "synthetic failure",
            "method": "monte_carlo",
            "stochastic": True,
            "seed": 7,
            "samples": 1,
            "confidence_interval": (0.0, 1.0),
            "confidence_level": 0.95,
            "stopping_condition": "fixed requested sample count",
        }
    )
    results[failed_index] = ToolResult.model_validate(payload, strict=True)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            results[: failed_index + 1],
            run_status="failed_with_limitations",
        )


def test_failed_equity_replay_accepts_only_the_deterministic_failure_envelope() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    payload = results[2].model_dump(mode="python")
    payload.update(
        {
            "status": ToolStatus.FAILED,
            "exactness": Exactness.UNAVAILABLE,
            "numeric_exactness": NumericalExactness.UNAVAILABLE,
            "output": {},
            "verification": None,
            "method": None,
            "warnings": [],
            "error": versioned_range_bridge_failure_error("holdem_equity"),
        }
    )
    results[2] = ToolResult.model_validate(payload, strict=True)

    assert (
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            results,
            run_status="failed_with_limitations",
        )
        is None
    )


@pytest.mark.parametrize(
    ("failed_index", "tool_name"),
    [(0, "range_validate"), (1, "combos"), (2, "holdem_equity")],
)
def test_failed_bridge_tool_replay_rejects_error_tampering(
    failed_index: int,
    tool_name: str,
) -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    registry = default_registry()
    definition = registry._tools[tool_name]

    def fail(_payload: dict[str, object]) -> dict[str, object]:
        raise ValueError("private diagnostic must not enter the replay envelope")

    registry._tools[tool_name] = replace(definition, function=fail)
    failure = registry.execute(
        tool_name,
        results[failed_index].input,
        contract_version="2.0.0",
        _bind_versioned_range_failure=True,
    )
    prefix = [*results[:failed_index], failure]

    assert failure.error == versioned_range_bridge_failure_error(tool_name)
    assert (
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            prefix,
            run_status="failed_with_limitations",
        )
        is None
    )

    prefix[-1] = failure.model_copy(update={"error": "forged failure"})
    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        verify_versioned_range_river_equity_tool_chain(
            admission.case,
            prefix,
            run_status="failed_with_limitations",
        )


def test_result_builder_rejects_byte_distinct_numeric_output() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    assert results[1].output["total_combo_weight"] == 1.0
    forged_output = dict(results[1].output)
    forged_output["total_combo_weight"] = 1
    results[1] = results[1].model_copy(update={"output": forged_output})

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        build_versioned_range_river_equity_result(admission.case, results)


def test_result_builder_rejects_validation_verification_envelope() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    assert results[1].verification is not None
    payload = results[0].model_dump(mode="python")
    payload["verification"] = VerificationMetadata(
        method="unexecuted verifier",
        checks=["unbound claim"],
        observations=["not executed"],
        tolerance=results[1].verification.tolerance,
        passed=False,
    )
    results[0] = ToolResult.model_validate(payload, strict=True)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_CHAIN"):
        build_versioned_range_river_equity_result(admission.case, results)


def test_admission_maps_excessive_json_depth_to_stable_schema_error() -> None:
    nested: object = []
    for _ in range(5_000):
        nested = [nested]
    payload = versioned_river_equity_case().model_dump(mode="python")
    payload["metadata"]["deep"] = nested
    candidate = CaseInput.model_validate(payload, strict=True)

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        admit_versioned_range_river_equity(candidate)

    assert error.value.code.value == "REQ_E_SCHEMA"


def test_result_builder_maps_excessive_tool_output_depth_to_chain_error() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    results = _run_bound_tools(admission.case)
    nested: object = []
    for _ in range(5_000):
        nested = [nested]
    payload = results[0].model_dump(mode="python")
    payload["output"] = {**payload["output"], "deep": nested}
    results[0] = ToolResult.model_validate(payload, strict=True)

    with pytest.raises(VersionedRangeRiverEquityError) as error:
        build_versioned_range_river_equity_result(admission.case, results)

    assert error.value.code.value == "REQ_E_CHAIN"


def test_binding_artifact_verifier_rejects_report_from_another_admission() -> None:
    first = admit_versioned_range_river_equity(versioned_river_equity_case())
    second = admit_versioned_range_river_equity(versioned_river_equity_case("6c6d@0.5,QcQd@0.5"))

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_PROVENANCE"):
        verify_versioned_range_river_equity_binding_artifact(
            first.case,
            first.case,
            second.case.model_dump(mode="json"),
            first.binding,
        )


def test_admission_rejects_non_target_facing_action() -> None:
    candidate = versioned_river_equity_case()
    assert candidate.hand is not None
    payload = candidate.model_dump(mode="python")
    payload["hand"]["actions"][-1]["actor"] = "hero"
    modified = CaseInput.model_validate(payload)

    with pytest.raises(VersionedRangeRiverEquityError, match="REQ_E_DECISION"):
        admit_versioned_range_river_equity(modified)


def test_result_model_rejects_legacy_equity_inconsistent_with_exact_fraction() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    bridge = build_versioned_range_river_equity_result(
        admission.case,
        _run_bound_tools(admission.case),
    )
    payload = bridge.model_dump(mode="python")
    payload["legacy_hero_equity"] = 0.0
    payload.pop("result_sha256")
    payload["result_sha256"] = canonical_domain_sha256(RESULT_HASH_DOMAIN, payload)

    with pytest.raises(ValueError, match="REQ_E_ORACLE"):
        VersionedRangeRiverEquityResultV1.model_validate(payload, strict=True)


def test_binding_model_rejects_impossible_combo_weight_aggregate() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    payload = admission.binding.model_dump(mode="python")
    payload["total_weight_millionths"] = 1
    payload.pop("binding_sha256")
    payload["binding_sha256"] = canonical_domain_sha256(BINDING_HASH_DOMAIN, payload)

    with pytest.raises(ValueError, match="REQ_E_RANGE"):
        VersionedRangeRiverEquityBindingV1.model_validate(payload, strict=True)


def test_result_model_rejects_impossible_outcome_count_weight_aggregate() -> None:
    admission = admit_versioned_range_river_equity(versioned_river_equity_case())
    bridge = build_versioned_range_river_equity_result(
        admission.case,
        _run_bound_tools(admission.case),
    )
    payload = bridge.model_dump(mode="python")
    payload["win_combo_count"] = 0
    payload["loss_combo_count"] = 2
    oracle_keys = (
        "range_id",
        "target_player_id",
        "hero_player_id",
        "condition_binding_sha256",
        "hero_cards",
        "board",
        "canonical_combo_sha256",
        "combo_count",
        "total_weight_millionths",
        "win_combo_count",
        "tie_combo_count",
        "loss_combo_count",
        "win_weight_millionths",
        "tie_weight_millionths",
        "loss_weight_millionths",
        "equity_numerator",
        "equity_denominator",
    )
    payload["oracle_sha256"] = canonical_domain_sha256(
        ORACLE_HASH_DOMAIN,
        {key: payload[key] for key in oracle_keys},
    )
    payload.pop("result_sha256")
    payload["result_sha256"] = canonical_domain_sha256(RESULT_HASH_DOMAIN, payload)

    with pytest.raises(ValueError, match="REQ_E_ORACLE"):
        VersionedRangeRiverEquityResultV1.model_validate(payload, strict=True)
