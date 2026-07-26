"""Executable verification for ``floating-verified`` calculator results."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from poker_deliberation.schemas import CanonicalHand, TolerancePolicy
from poker_deliberation.tools.combinations import parse_weighted_range
from poker_deliberation.tools.hand_validator import validate_hand
from poker_deliberation.tools.icm import icm_floating_error_bound
from poker_deliberation.tools.numeric import effective_matrix_tolerance, ulp_bound
from poker_deliberation.tools.sensitivity import analyze_scenarios


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    checks: tuple[str, ...]
    observations: tuple[str, ...]
    tolerance: TolerancePolicy


Verifier = Callable[[dict[str, Any], dict[str, Any], TolerancePolicy], tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _VerifierSpec:
    checks: tuple[str, ...]
    function: Verifier


def _comparison_bound(
    actual: float,
    expected: float,
    policy: TolerancePolicy,
    *,
    multiplier: float = 1.0,
) -> float:
    scale = max(abs(actual), abs(expected))
    if policy.kind == "absolute":
        if policy.absolute is None:
            raise ValueError("applied absolute tolerance has no bound")
        bound = float(policy.absolute)
    elif policy.kind == "relative":
        if policy.relative is None:
            raise ValueError("applied relative tolerance has no bound")
        bound = float(policy.relative) * scale
    elif policy.kind == "absolute-or-relative":
        if policy.absolute is None or policy.relative is None:
            raise ValueError("applied absolute-or-relative tolerance is incomplete")
        bound = max(float(policy.absolute), float(policy.relative) * scale)
    elif policy.kind == "ulp":
        if policy.ulps is None:
            raise ValueError("applied ULP tolerance has no bound")
        bound = ulp_bound(actual, expected, ulps=policy.ulps)
        if policy.absolute is not None:
            bound = max(bound, float(policy.absolute))
    else:
        if policy.absolute is None:
            raise ValueError("applied caller-supplied tolerance has no resolved absolute bound")
        bound = float(policy.absolute)
    return bound * multiplier


def within_tolerance(
    actual: float,
    expected: float,
    policy: TolerancePolicy,
    *,
    multiplier: float = 1.0,
) -> bool:
    """Apply exactly the comparison semantics recorded by ``policy``."""

    return abs(actual - expected) <= _comparison_bound(
        actual,
        expected,
        policy,
        multiplier=multiplier,
    )


def _expect_close(
    observations: list[str],
    label: str,
    actual: Any,
    expected: Any,
    policy: TolerancePolicy,
    *,
    multiplier: float = 1.0,
) -> None:
    actual_number = float(actual)
    expected_number = float(expected)
    bound = _comparison_bound(actual_number, expected_number, policy, multiplier=multiplier)
    difference = abs(actual_number - expected_number)
    if not math.isfinite(actual_number) or not within_tolerance(
        actual_number,
        expected_number,
        policy,
        multiplier=multiplier,
    ):
        raise ValueError(
            f"verification failed for {label}: actual={actual_number!r}, "
            f"expected={expected_number!r}, difference={difference!r}, bound={bound!r}"
        )
    observations.append(
        f"{label}: actual={actual_number!r}, expected={expected_number!r}, bound={bound!r}"
    )


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"verification failed: {message}")


def _materialize_policy(
    name: str,
    payload: dict[str, Any],
    output: dict[str, Any],
    declared: TolerancePolicy,
) -> TolerancePolicy:
    if name == "matrix_game":
        matrix = [[float(value) for value in row] for row in payload["matrix"]]
        effective = effective_matrix_tolerance(matrix, float(payload.get("tolerance", 1e-9)))
        return declared.model_copy(update={"absolute": effective})
    if name == "hand_validator":
        applied = float(output["verification_tolerance"])
        if payload.get("tolerance") is not None:
            return TolerancePolicy(
                fields=list(declared.fields),
                kind="caller-supplied",
                absolute=applied,
                formula="input.tolerance; comparisons use rel_tol=0.0",
                unit=declared.unit,
                rationale=declared.rationale,
            )
        operation_bound = max(32, 4 * (len(payload.get("actions", [])) + len(payload["players"])))
        return declared.model_copy(update={"ulps": operation_bound, "absolute": applied})
    if name == "icm":
        active_count = sum(float(stack) > 0 for stack in payload["stacks"])
        payout_count = min(active_count, len(payload["payouts"]))
        payable = sum(map(float, payload["payouts"][:active_count]))
        error_bound = icm_floating_error_bound(
            active_count,
            len(payload["stacks"]),
            payout_count,
            payable,
        )
        return declared.model_copy(
            update={
                "ulps": error_bound.ulps,
                "absolute": error_bound.absolute,
            }
        )
    return declared


def _verify_pot_odds(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    pot_after = float(payload["pot_before_bet"]) + float(payload["opponent_bet"])
    final_before = pot_after + float(payload["call_cost"])
    rake = float(payload.get("expected_rake", 0.0))
    final_after = final_before - rake
    equity = float(payload["call_cost"]) / final_after
    expected = {
        "pot_after_opponent_bet": pot_after,
        "final_pot_before_rake": final_before,
        "expected_rake": rake,
        "final_pot_after_rake": final_after,
        "required_equity": equity,
        "required_equity_percent": equity * 100.0,
        "pot_odds_against": (final_after - float(payload["call_cost"]))
        / float(payload["call_cost"]),
    }
    for field, value in expected.items():
        _expect_close(observations, field, output[field], value, policy)
    return (*observations, "finite typed output: passed")


def _verify_break_even_fold(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    risk, reward = float(payload["risk"]), float(payload["reward"])
    frequency = risk / (risk + reward)
    for field, value in {
        "risk": risk,
        "reward": reward,
        "break_even_fold_frequency": frequency,
        "break_even_fold_percent": frequency * 100.0,
    }.items():
        _expect_close(observations, field, output[field], value, policy)
    _expect(0.0 <= float(output["break_even_fold_frequency"]) <= 1.0, "frequency domain")
    return (*observations, "frequency domain: [0,1]")


def _verify_mdf(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    pot, bet = float(payload["pot_before_bet"]), float(payload["bet"])
    frequency = pot / (pot + bet)
    _expect_close(
        observations,
        "minimum_defense_frequency",
        output["minimum_defense_frequency"],
        frequency,
        policy,
    )
    _expect_close(
        observations,
        "minimum_defense_percent",
        output["minimum_defense_percent"],
        frequency * 100.0,
        policy,
    )
    _expect(output["formula"] == "pot_before_bet / (pot_before_bet + bet)", "MDF formula metadata")
    return (*observations, "frequency domain and formula metadata: passed")


def _verify_spr(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    expected = float(payload["effective_stack"]) / float(payload["pot"])
    _expect_close(observations, "spr", output["spr"], expected, policy)
    _expect(output["formula"] == "effective_stack / pot", "SPR formula metadata")
    return (*observations, "formula metadata: passed")


def _verify_effective_stack(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    expected = min(map(float, payload["stacks"]))
    _expect_close(observations, "effective_stack", output["effective_stack"], expected, policy)
    _expect(list(map(float, output["stacks"])) == list(map(float, payload["stacks"])), "stack echo")
    return (*observations,)


def _verify_rake_amount(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    raw = float(payload["pot_total"]) * float(payload["rake_percent"]) / 100.0
    cap = payload.get("rake_cap")
    rake = min(raw, float(cap)) if cap is not None else raw
    _expect_close(observations, "raw_rake", output["raw_rake"], raw, policy)
    _expect_close(observations, "rake_amount", output["rake_amount"], rake, policy)
    _expect(output.get("rake_cap") == cap, "rake cap echo")
    _expect("pot_total" in str(output["formula"]), "rake formula metadata")
    return (*observations, "cap and formula metadata: passed")


def _verify_raked_call_ev(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    total = float(payload["pot_after_bet"]) + float(payload["call_cost"])
    raw_rake = total * float(payload["rake_percent"]) / 100.0
    cap = payload.get("rake_cap")
    rake = min(raw_rake, float(cap)) if cap is not None else raw_rake
    final = total - rake
    ev = float(payload["equity"]) * final - float(payload["call_cost"])
    for field, value in {"rake_amount": rake, "final_pot_after_rake": final, "ev": ev}.items():
        _expect_close(observations, field, output[field], value, policy)
    _expect("no future betting" in str(output["model"]), "raked-call model metadata")
    return (*observations, "model and formula metadata: passed")


def _verify_bluff_ev(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    folds = float(payload["fold_frequency"])
    pot, bet = float(payload["pot_before_bet"]), float(payload["bet"])
    equity = float(payload.get("equity_when_called", 0.0))
    called = equity * (pot + 2.0 * bet) - bet
    ev = folds * pot + (1.0 - folds) * called
    _expect_close(observations, "called_branch_ev", output["called_branch_ev"], called, policy)
    _expect_close(observations, "ev", output["ev"], ev, policy)
    _expect("call-or-fold" in str(output["model"]), "bluff model metadata")
    return (*observations, "model and formula metadata: passed")


def _verify_polar(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    pot, bet = float(payload["pot_before_bet"]), float(payload["bet"])
    frequency = bet / (pot + 2.0 * bet)
    _expect_close(observations, "bluff_fraction", output["bluff_fraction"], frequency, policy)
    _expect_close(observations, "bluff_percent", output["bluff_percent"], frequency * 100.0, policy)
    _expect("bluff-catcher" in str(output["model"]), "polar model metadata")
    return (*observations, "fraction domain and metadata: passed")


def _verify_bayes(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    prior = float(payload["prior"])
    likelihood_h = float(payload["likelihood_given_h"])
    likelihood_not = float(payload["likelihood_given_not_h"])
    evidence = prior * likelihood_h + (1.0 - prior) * likelihood_not
    posterior = prior * likelihood_h / evidence
    _expect_close(
        observations, "evidence_probability", output["evidence_probability"], evidence, policy
    )
    _expect_close(observations, "posterior", output["posterior"], posterior, policy)
    _expect("P(E|H)" in str(output["formula"]), "Bayes formula metadata")
    return (*observations, "probability domain and formula metadata: passed")


def _verify_pot_reconstruction(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    running = float(payload["starting_pot"])
    expected_pots: list[float] = []
    for contribution in payload["contributions"]:
        running += float(contribution)
        expected_pots.append(running)
    actual_pots = output["pots_after_each_contribution"]
    _expect(len(actual_pots) == len(expected_pots), "running-pot length")
    for index, (actual, expected) in enumerate(zip(actual_pots, expected_pots, strict=True)):
        _expect_close(
            observations, f"pots_after_each_contribution[{index}]", actual, expected, policy
        )
    _expect_close(observations, "final_pot", output["final_pot"], running, policy)
    return (*observations,)


def _verify_combos(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    combos = parse_weighted_range(
        str(payload["range"]), tuple(map(str, payload.get("dead_cards", [])))
    )
    total = sum(combo.weight for combo in combos)
    expected = {combo.cards: combo.weight / total for combo in combos}
    records = output["normalized_weights"]
    _expect(int(output["combo_count"]) == len(combos) == len(records), "weighted combo count")
    _expect_close(observations, "total_combo_weight", output["total_combo_weight"], total, policy)
    actual: dict[tuple[str, str], float] = {}
    for record in records:
        cards = tuple(map(str, record.get("cards", [])))
        _expect(len(cards) == 2, "normalized combo cards")
        actual[(cards[0], cards[1])] = float(record["weight"])
    _expect(set(actual) == set(expected), "normalized combo identities")
    for cards, expected_weight in expected.items():
        _expect_close(
            observations, f"normalized_weight[{cards}]", actual[cards], expected_weight, policy
        )
    _expect_close(observations, "normalized_weight_sum", sum(actual.values()), 1.0, policy)
    return (*observations,)


def _verify_equity(
    _payload: dict[str, Any], output: dict[str, Any], _policy: TolerancePolicy
) -> tuple[str, ...]:
    _expect(
        output["method"] == "exact_enumeration" and output["exact"] is True, "enumeration method"
    )
    evaluations = int(output["evaluations"])
    count_sum = sum(
        int(output[field]) for field in ("unweighted_wins", "unweighted_ties", "unweighted_losses")
    )
    _expect(count_sum == evaluations, "equity outcome counts")
    _expect(0.0 <= float(output["hero_equity"]) <= 1.0, "equity domain")
    return (
        f"outcome counts: {count_sum} == evaluations {evaluations}",
        f"equity domain: hero_equity={float(output['hero_equity'])!r}",
        "seeded Monte Carlo metadata: not applicable to complete enumeration",
    )


def _verify_ev_tree(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    nodes = payload["nodes"]
    root = str(payload.get("root", "root"))
    visiting: set[str] = set()
    expected_values: dict[str, float] = {}

    def visit(node_id: str) -> float:
        if node_id in expected_values:
            return expected_values[node_id]
        _expect(node_id not in visiting, "EV tree acyclic traversal")
        visiting.add(node_id)
        node = nodes[node_id]
        if node.get("payoff") is not None:
            value = float(node["payoff"])
        else:
            branches = node["branches"]
            probability_sum = sum(float(branch["probability"]) for branch in branches)
            _expect_close(observations, f"probability_sum[{node_id}]", probability_sum, 1.0, policy)
            value = sum(
                float(branch["probability"]) * visit(str(branch["child"])) for branch in branches
            )
        visiting.remove(node_id)
        expected_values[node_id] = value
        return value

    expected_root = visit(root)
    _expect(set(output["node_values"]) == set(expected_values), "EV node value coverage")
    for node_id, expected in expected_values.items():
        _expect_close(
            observations, f"node_value[{node_id}]", output["node_values"][node_id], expected, policy
        )
    _expect_close(observations, "expected_value", output["expected_value"], expected_root, policy)
    return ("acyclic traversal: passed", *observations)


def _verify_icm(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    stacks = list(map(float, payload["stacks"]))
    payouts = list(map(float, payload["payouts"]))
    equities = list(map(float, output["equities"]))
    active_count = sum(stack > 0 for stack in stacks)
    payable = sum(payouts[:active_count])
    equity_sum = sum(equities)
    error_bound = icm_floating_error_bound(
        active_count,
        len(stacks),
        min(active_count, len(payouts)),
        payable,
    )
    _expect(len(equities) == len(stacks), "ICM equity vector length")
    _expect(
        float(output["verification_tolerance"]) == error_bound.absolute,
        "ICM derived verification tolerance",
    )
    _expect(output["conservation_verified"] is True, "ICM conservation metadata")
    _expect_close(observations, "equity_sum", output["equity_sum"], equity_sum, policy)
    _expect_close(observations, "payable_prize_sum", output["payable_prize_sum"], payable, policy)
    _expect_close(observations, "sum_error", output["sum_error"], equity_sum - payable, policy)
    _expect_close(observations, "prize_conservation", equity_sum, payable, policy)
    for first in range(len(stacks)):
        for second in range(first + 1, len(stacks)):
            if stacks[first] == stacks[second]:
                _expect_close(
                    observations,
                    f"symmetry[{first},{second}]",
                    equities[first],
                    equities[second],
                    policy,
                )
    expected_zero = [index for index, stack in enumerate(stacks) if stack == 0]
    _expect(output["zero_stack_players"] == expected_zero, "ICM zero-stack index metadata")
    for index in expected_zero:
        _expect_close(observations, f"zero_stack_equity[{index}]", equities[index], 0.0, policy)
    _expect(all(math.isfinite(value) for value in equities), "finite ICM recursion output")
    return (
        *observations,
        f"cached-subset binary64 operation upper bound: {error_bound.operation_upper_bound}",
        f"derived conservation ULP count: {error_bound.ulps}",
        "finite recursion: passed",
    )


def _verify_matrix(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    matrix = [[float(value) for value in row] for row in payload["matrix"]]
    row_strategy = list(map(float, output["row_strategy"]))
    column_strategy = list(map(float, output["column_strategy"]))
    _expect_close(observations, "row_strategy_sum", sum(row_strategy), 1.0, policy)
    _expect_close(observations, "column_strategy_sum", sum(column_strategy), 1.0, policy)
    against_columns = [
        sum(row_strategy[row] * matrix[row][column] for row in range(len(matrix)))
        for column in range(len(matrix[0]))
    ]
    for_rows = [
        sum(matrix[row][column] * column_strategy[column] for column in range(len(matrix[0])))
        for row in range(len(matrix))
    ]
    lower, upper = min(against_columns), max(for_rows)
    value = float(output["value"])
    _expect(
        lower >= value - _comparison_bound(lower, value, policy, multiplier=20),
        "matrix lower support feasibility",
    )
    _expect(
        upper <= value + _comparison_bound(upper, value, policy, multiplier=20),
        "matrix upper support feasibility",
    )
    _expect_close(
        observations,
        "duality_gap",
        output["duality_gap"],
        max(0.0, upper - lower),
        policy,
        multiplier=20,
    )
    _expect(
        int(output["row_best_response"]) == max(range(len(for_rows)), key=for_rows.__getitem__),
        "row best-response index",
    )
    _expect(
        int(output["column_best_response"])
        == min(range(len(against_columns)), key=against_columns.__getitem__),
        "column best-response index",
    )
    _expect_close(
        observations,
        "verification_tolerance",
        output["verification_tolerance"],
        policy.absolute,
        policy,
    )
    return (*observations, "support feasibility and best-response bounds: passed")


def _verify_best_response(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    observations: list[str] = []
    game = payload["game"]
    nodes = game["nodes"]
    responder = int(payload.get("best_responder", 0))
    fixed = payload["fixed_strategy"]
    pure_policy = {str(key): str(value) for key, value in output["pure_policy"].items()}
    responder_sets: dict[str, tuple[str, ...]] = {}
    for node in nodes.values():
        if node["type"] == "chance":
            total = sum(float(item["probability"]) for item in node["actions"].values())
            _expect_close(observations, "chance_probability_sum", total, 1.0, policy)
        elif node["type"] == "player":
            info_set = str(node["information_set"])
            actions = tuple(sorted(map(str, node["actions"])))
            if int(node["player"]) == responder:
                responder_sets[info_set] = actions
            else:
                _expect(
                    info_set in fixed and set(fixed[info_set]) == set(actions),
                    "fixed strategy action coverage",
                )
                total = sum(map(float, fixed[info_set].values()))
                _expect_close(
                    observations, f"fixed_probability_sum[{info_set}]", total, 1.0, policy
                )
    _expect(set(pure_policy) == set(responder_sets), "one action per responder information set")
    for info_set, action in pure_policy.items():
        _expect(action in responder_sets[info_set], "reported pure-policy action")

    def evaluate(node_id: str, memo: dict[str, float]) -> float:
        if node_id in memo:
            return memo[node_id]
        node = nodes[node_id]
        if node["type"] == "terminal":
            value = float(node["payoff"])
        elif node["type"] == "chance":
            value = sum(
                float(item["probability"]) * evaluate(str(item["child"]), memo)
                for item in node["actions"].values()
            )
        elif int(node["player"]) == responder:
            info_set = str(node["information_set"])
            value = evaluate(str(node["actions"][pure_policy[info_set]]), memo)
        else:
            info_set = str(node["information_set"])
            value = sum(
                float(probability) * evaluate(str(node["actions"][action]), memo)
                for action, probability in fixed[info_set].items()
            )
        memo[node_id] = value
        return value

    player0 = evaluate(str(game.get("root", "root")), {})
    responder_value = player0 if responder == 0 else -player0
    _expect_close(observations, "player0_value", output["player0_value"], player0, policy)
    _expect_close(
        observations,
        "best_responder_value",
        output["best_responder_value"],
        responder_value,
        policy,
    )
    _expect_close(observations, "value", output["value"], responder_value, policy)
    _expect(output["equilibrium_claim"] is False, "explicit non-equilibrium flag")
    return (*observations, "information-set policy and non-equilibrium flag: passed")


def _verify_hand(
    payload: dict[str, Any], output: dict[str, Any], policy: TolerancePolicy
) -> tuple[str, ...]:
    normalized = dict(payload)
    caller_tolerance = normalized.pop("tolerance", None)
    hand = CanonicalHand.model_validate(normalized)
    expected = validate_hand(
        hand,
        tolerance=float(caller_tolerance) if caller_tolerance is not None else None,
    )
    _expect(output == expected, "hand validation reconstruction matches executable validator")
    _expect(output["valid"] == (not output["errors"]), "hand valid/errors identity")
    validity_observation = (
        f"card/action/pot reconstruction: valid={output['valid']!r}, errors={len(output['errors'])}"
    )
    return (
        validity_observation,
        f"applied chip tolerance: {float(output['verification_tolerance'])!r} ({policy.kind})",
        "limitation disclosure: present",
    )


def _verify_sensitivity(
    payload: dict[str, Any], output: dict[str, Any], _policy: TolerancePolicy
) -> tuple[str, ...]:
    expected = analyze_scenarios(
        payload["scenarios"],
        decision_threshold=float(payload.get("decision_threshold", 0.0)),
    )
    _expect(output == expected, "sensitivity output matches executable grid invariants")
    impacts = [float(item["impact"]) for item in output["influence_ranking"]]
    _expect(impacts == sorted(impacts, reverse=True), "descending influence ranking")
    return (
        f"ordered bounds: {float(output['lower_bound'])!r} <= {float(output['upper_bound'])!r}",
        f"scenario count: {int(output['scenario_count'])}",
        "descending influence ranking: passed",
    )


_SPECS: dict[str, _VerifierSpec] = {
    "pot_odds": _VerifierSpec(("formula identities", "finite typed output"), _verify_pot_odds),
    "break_even_fold": _VerifierSpec(
        ("frequency/percent identity", "frequency lies in [0,1]"), _verify_break_even_fold
    ),
    "mdf": _VerifierSpec(
        ("frequency/percent identity", "frequency domain and formula metadata"), _verify_mdf
    ),
    "spr": _VerifierSpec(("ratio identity", "formula metadata"), _verify_spr),
    "effective_stack": _VerifierSpec(
        ("effective_stack equals min(stacks)",), _verify_effective_stack
    ),
    "rake_amount": _VerifierSpec(("rake/cap identities", "formula metadata"), _verify_rake_amount),
    "raked_call_ev": _VerifierSpec(
        ("EV/rake identities", "model and formula metadata"), _verify_raked_call_ev
    ),
    "bluff_ev": _VerifierSpec(
        ("EV branch identities", "model and formula metadata"), _verify_bluff_ev
    ),
    "polar_river_bluff_fraction": _VerifierSpec(
        ("fraction/percent identity", "model and formula metadata"), _verify_polar
    ),
    "bayes_update": _VerifierSpec(
        ("posterior/evidence identity", "probability domain and formula metadata"), _verify_bayes
    ),
    "pot_reconstruction": _VerifierSpec(
        ("running-pot length and ordering", "final-pot sum invariant"), _verify_pot_reconstruction
    ),
    "combos": _VerifierSpec(
        ("combo count matches list", "normalized weights sum to one"), _verify_combos
    ),
    "holdem_equity": _VerifierSpec(
        (
            "outcome counts equal evaluations/samples",
            "equity and interval lie in [0,1]",
            "seeded Monte Carlo metadata",
        ),
        _verify_equity,
    ),
    "ev_tree": _VerifierSpec(
        ("acyclic traversal", "probability normalization", "node value identities"), _verify_ev_tree
    ),
    "icm": _VerifierSpec(
        ("prize conservation", "player symmetry", "zero-stack boundary", "finite recursion"),
        _verify_icm,
    ),
    "matrix_game": _VerifierSpec(
        ("strategy normalization", "support feasibility", "duality gap", "best-response bounds"),
        _verify_matrix,
    ),
    "fixed_strategy_best_response": _VerifierSpec(
        (
            "one responder action per information set",
            "chance and fixed opponent distributions",
            "reported policy value",
            "explicit non-equilibrium flag",
        ),
        _verify_best_response,
    ),
    "hand_validator": _VerifierSpec(
        ("card uniqueness", "stack/pot reconstruction", "action legality", "limitation disclosure"),
        _verify_hand,
    ),
    "sensitivity": _VerifierSpec(
        ("ordered bounds", "scenario count", "descending influence ranking"), _verify_sensitivity
    ),
}


def verify_floating_result(
    name: str,
    payload: dict[str, Any],
    output: dict[str, Any],
    declared_policy: TolerancePolicy,
    declared_checks: tuple[str, ...],
) -> VerificationEvidence:
    spec = _SPECS.get(name)
    if spec is None:
        raise ValueError(f"floating-verified tool {name!r} has no executable verifier")
    if spec.checks != declared_checks:
        raise ValueError(f"floating verifier checks differ from the canonical contract for {name}")
    applied_policy = _materialize_policy(name, payload, output, declared_policy)
    observations = spec.function(payload, output, applied_policy)
    if not observations:
        raise ValueError(f"floating verifier for {name} produced no observations")
    return VerificationEvidence(
        checks=spec.checks,
        observations=observations,
        tolerance=applied_policy,
    )
