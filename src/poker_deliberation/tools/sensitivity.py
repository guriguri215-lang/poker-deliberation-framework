"""Summarize a precomputed parameter grid without evaluating arbitrary code."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from typing import Any


def analyze_scenarios(
    scenarios: list[dict[str, Any]], *, decision_threshold: float = 0.0
) -> dict[str, object]:
    if not scenarios:
        raise ValueError("at least one scenario is required")
    validated: list[tuple[str, dict[str, object], float]] = []
    for index, scenario in enumerate(scenarios):
        parameters = scenario.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError(f"scenario[{index}] parameters must be a mapping")
        raw_value = scenario.get("value")
        if not isinstance(raw_value, (int, float)):
            raise ValueError(f"scenario[{index}] value must be numeric")
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError("scenario values must be finite")
        validated.append((str(scenario.get("name", f"scenario-{index}")), parameters, value))
    values = [item[2] for item in validated]
    by_parameter: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for _name, parameters, value in validated:
        for parameter, setting in parameters.items():
            canonical_setting = json.dumps(
                setting, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            by_parameter[str(parameter)][canonical_setting].append(value)
    impacts: list[dict[str, object]] = []
    for parameter, settings in by_parameter.items():
        setting_means = {setting: sum(items) / len(items) for setting, items in settings.items()}
        impact = max(setting_means.values()) - min(setting_means.values())
        impacts.append({"parameter": parameter, "impact": impact, "setting_means": setting_means})

    def impact_value(item: dict[str, object]) -> float:
        value = item["impact"]
        if not isinstance(value, (int, float)):
            raise TypeError("internal impact must be numeric")
        return float(value)

    impacts.sort(key=impact_value, reverse=True)
    crossings = [
        {"name": name, "value": value, "parameters": parameters}
        for name, parameters, value in validated
        if value >= decision_threshold
    ]
    return {
        "lower_bound": min(values),
        "upper_bound": max(values),
        "decision_threshold": decision_threshold,
        "scenarios_at_or_above_threshold": crossings,
        "influence_ranking": impacts,
        "scenario_count": len(validated),
        "warning": (
            "The grid is user-supplied; bounds apply only to sampled scenarios. "
            "Influence is an association (spread of setting means), not a causal effect, "
            "and can be confounded by an unbalanced grid."
        ),
    }
