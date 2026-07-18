"""Exact expectation over a finite, fully specified action tree."""

from __future__ import annotations

import math
from typing import Any

HARD_MAX_NODES = 10_000
HARD_MAX_DEPTH = 256


def evaluate_ev_tree(tree: dict[str, Any]) -> dict[str, Any]:
    root = str(tree.get("root", "root"))
    nodes = tree.get("nodes")
    if not isinstance(nodes, dict) or root not in nodes:
        raise ValueError("tree must contain a nodes mapping and a valid root")
    if len(nodes) > HARD_MAX_NODES:
        raise ValueError(f"EV tree contains more than {HARD_MAX_NODES} nodes")
    visiting: set[str] = set()
    memo: dict[str, float] = {}
    branch_values: dict[str, list[dict[str, float | str]]] = {}

    def visit(node_id: str, depth: int = 0) -> float:
        if depth > HARD_MAX_DEPTH:
            raise ValueError(f"EV tree depth exceeds hard limit {HARD_MAX_DEPTH}")
        if node_id in memo:
            return memo[node_id]
        if node_id in visiting:
            raise ValueError("EV tree contains a cycle")
        if node_id not in nodes:
            raise ValueError(f"unknown EV tree node: {node_id}")
        visiting.add(node_id)
        node = nodes[node_id]
        if "payoff" in node:
            payoff = float(node["payoff"])
            if not math.isfinite(payoff):
                raise ValueError("terminal payoff must be finite")
            value = payoff
        else:
            branches = node.get("branches")
            if not isinstance(branches, list) or not branches:
                raise ValueError(f"node {node_id} requires payoff or non-empty branches")
            probability_sum = sum(float(branch.get("probability", -1)) for branch in branches)
            if not math.isclose(probability_sum, 1.0, abs_tol=1e-9):
                raise ValueError(f"branch probabilities at {node_id} must sum to 1")
            details: list[dict[str, float | str]] = []
            value = 0.0
            for branch in branches:
                probability = float(branch["probability"])
                if probability < 0 or not math.isfinite(probability):
                    raise ValueError("branch probabilities must be finite and non-negative")
                child = str(branch["child"])
                child_value = visit(child, depth + 1)
                contribution = probability * child_value
                value += contribution
                details.append(
                    {
                        "label": str(branch.get("label", child)),
                        "probability": probability,
                        "child_value": child_value,
                        "contribution": contribution,
                    }
                )
            branch_values[node_id] = details
        visiting.remove(node_id)
        memo[node_id] = value
        return value

    root_value = visit(root)
    return {
        "root": root,
        "expected_value": root_value,
        "node_values": memo,
        "branches": branch_values,
    }
