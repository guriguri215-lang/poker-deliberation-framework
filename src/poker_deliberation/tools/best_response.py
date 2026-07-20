"""Exhaustive pure-policy best response in a small finite extensive-form game."""

from __future__ import annotations

import math
from itertools import product
from typing import Any

from poker_deliberation.tools.numeric import (
    BEST_RESPONSE_VERIFICATION_ULPS,
    normalized_probability_sum,
)

HARD_MAX_PURE_POLICIES = 1_000_000
HARD_MAX_NODES = 10_000
HARD_MAX_DEPTH = 256
HARD_MAX_POLICY_NODE_EVALUATIONS = 5_000_000


def best_response_to_fixed_strategy(
    game: dict[str, Any],
    fixed_strategy: dict[str, dict[str, float]],
    *,
    best_responder: int = 0,
    max_pure_policies: int = 1_000_000,
) -> dict[str, Any]:
    if best_responder not in {0, 1}:
        raise ValueError("best_responder must be player 0 or 1")
    if not 1 <= max_pure_policies <= HARD_MAX_PURE_POLICIES:
        raise ValueError(f"max_pure_policies must be between 1 and {HARD_MAX_PURE_POLICIES}")
    root = str(game.get("root", "root"))
    nodes = game.get("nodes")
    if not isinstance(nodes, dict) or root not in nodes:
        raise ValueError("game requires a nodes mapping and valid root")
    if len(nodes) > HARD_MAX_NODES:
        raise ValueError(f"game contains more than {HARD_MAX_NODES} nodes")
    info_sets: dict[str, tuple[int, tuple[str, ...]]] = {}
    visiting: set[str] = set()
    visited: set[str] = set()

    def validate_node(node_id: str, depth: int = 0) -> None:
        if depth > HARD_MAX_DEPTH:
            raise ValueError(f"game depth exceeds hard limit {HARD_MAX_DEPTH}")
        if node_id in visiting:
            raise ValueError("game graph contains a cycle")
        if node_id in visited:
            return
        if node_id not in nodes:
            raise ValueError(f"unknown game node: {node_id}")
        visiting.add(node_id)
        node = nodes[node_id]
        node_type = node.get("type")
        if node_type == "terminal":
            payoff = float(node.get("payoff"))
            if not math.isfinite(payoff):
                raise ValueError("terminal payoff must be finite")
        elif node_type == "chance":
            actions = node.get("actions")
            if not isinstance(actions, dict) or not actions:
                raise ValueError("chance node requires actions")
            probabilities = [float(item.get("probability", -1)) for item in actions.values()]
            if not normalized_probability_sum(
                probabilities,
                ulps=BEST_RESPONSE_VERIFICATION_ULPS,
            ):
                raise ValueError("chance probabilities must sum to 1")
            for item in actions.values():
                if float(item.get("probability", -1)) < 0:
                    raise ValueError("chance probabilities must be non-negative")
                validate_node(str(item["child"]), depth + 1)
        elif node_type == "player":
            player = int(node.get("player"))
            if player not in {0, 1}:
                raise ValueError("player nodes must identify player 0 or 1")
            info_set = str(node.get("information_set", ""))
            actions = node.get("actions")
            if not info_set or not isinstance(actions, dict) or not actions:
                raise ValueError("player node requires information_set and actions")
            action_names = tuple(sorted(map(str, actions)))
            existing = info_sets.get(info_set)
            definition = (player, action_names)
            if existing is not None and existing != definition:
                raise ValueError("all nodes in an information set must share player and actions")
            info_sets[info_set] = definition
            for child in actions.values():
                validate_node(str(child), depth + 1)
        else:
            raise ValueError(f"unsupported node type at {node_id}: {node_type!r}")
        visiting.remove(node_id)
        visited.add(node_id)

    validate_node(root)
    responder_info_sets = sorted(
        (name, definition[1])
        for name, definition in info_sets.items()
        if definition[0] == best_responder
    )
    policy_count = math.prod(len(actions) for _, actions in responder_info_sets)
    if policy_count > max_pure_policies:
        raise ValueError(
            f"best response requires {policy_count} pure policies, above limit {max_pure_policies}"
        )
    work_estimate = max(1, policy_count) * len(visited)
    if work_estimate > HARD_MAX_POLICY_NODE_EVALUATIONS:
        raise ValueError(
            "best-response work estimate exceeds hard limit: "
            f"{work_estimate} > {HARD_MAX_POLICY_NODE_EVALUATIONS}"
        )
    opponent = 1 - best_responder
    for info_set, (player, actions) in info_sets.items():
        if player != opponent:
            continue
        if info_set not in fixed_strategy:
            raise ValueError(f"fixed strategy missing information set: {info_set}")
        probabilities = fixed_strategy[info_set]
        if set(probabilities) != set(actions):
            raise ValueError(f"fixed strategy action mismatch at information set: {info_set}")
        if any(
            not math.isfinite(float(probability)) or float(probability) < 0
            for probability in probabilities.values()
        ):
            raise ValueError("fixed strategy probabilities must be finite and non-negative")
        if not normalized_probability_sum(
            map(float, probabilities.values()),
            ulps=BEST_RESPONSE_VERIFICATION_ULPS,
        ):
            raise ValueError("fixed strategy probabilities must sum to 1")

    def evaluate(node_id: str, policy: dict[str, str], memo: dict[str, float]) -> float:
        if node_id in memo:
            return memo[node_id]
        node = nodes[node_id]
        node_type = node["type"]
        if node_type == "terminal":
            value = float(node["payoff"])
        elif node_type == "chance":
            value = sum(
                float(item["probability"]) * evaluate(str(item["child"]), policy, memo)
                for item in node["actions"].values()
            )
        else:
            info_set = str(node["information_set"])
            if int(node["player"]) == best_responder:
                action = policy[info_set]
                value = evaluate(str(node["actions"][action]), policy, memo)
            else:
                value = sum(
                    float(probability) * evaluate(str(node["actions"][action]), policy, memo)
                    for action, probability in fixed_strategy[info_set].items()
                )
        memo[node_id] = value
        return value

    best_objective_value = -math.inf
    best_player0_value = -math.inf
    best_policy: dict[str, str] = {}
    action_products = product(*(actions for _, actions in responder_info_sets))
    for selected_actions in action_products:
        policy = {
            info_set: action
            for (info_set, _actions), action in zip(
                responder_info_sets, selected_actions, strict=True
            )
        }
        player0_value = evaluate(root, policy, {})
        objective_value = player0_value if best_responder == 0 else -player0_value
        if objective_value > best_objective_value:
            best_objective_value = objective_value
            best_player0_value = player0_value
            best_policy = policy
    if not responder_info_sets:
        best_player0_value = evaluate(root, {}, {})
        best_objective_value = best_player0_value if best_responder == 0 else -best_player0_value
    return {
        "best_responder": best_responder,
        "value": best_objective_value,
        "best_responder_value": best_objective_value,
        "player0_value": best_player0_value,
        "payoff_convention": "terminal payoff is player 0 utility in a two-player zero-sum game",
        "pure_policy": best_policy,
        "evaluated_policies": max(1, policy_count),
        "policy_node_work_upper_bound": work_estimate,
        "information_set_constraint_enforced": True,
        "opponent_strategy_fixed": True,
        "equilibrium_claim": False,
    }
