"""Deterministic task-to-role routing; agent count is not a quality signal."""

from __future__ import annotations

from dataclasses import dataclass

from poker_deliberation.schemas import AgentAssignment, CaseInput


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    name: str
    purpose: str
    read_only: bool = True


ROLE_CATALOG = {
    "intake": RoleDefinition("intake", "Normalize and validate hand or claim input."),
    "strategy-analyst": RoleDefinition(
        "strategy-analyst", "Compare objectives, lines, ranges, and practical robustness."
    ),
    "math-auditor": RoleDefinition(
        "math-auditor", "Verify formulas and deterministic calculator inputs/outputs."
    ),
    "evidence-researcher": RoleDefinition(
        "evidence-researcher", "Map material claims to primary sources."
    ),
    "skeptic": RoleDefinition("skeptic", "Find concrete counterexamples and missing premises."),
    "adjudicator": RoleDefinition(
        "adjudicator", "Resolve disputes by evidence strength, never majority vote."
    ),
    "report-writer": RoleDefinition(
        "report-writer", "Render the adjudicated record without adding claims."
    ),
}


def select_roles(case: CaseInput) -> list[AgentAssignment]:
    if case.kind == "calculation":
        selected = ["math-auditor", "report-writer"]
    elif case.kind == "hand":
        selected = ["intake", "strategy-analyst", "math-auditor", "skeptic", "adjudicator"]
    elif case.kind == "claim":
        selected = ["math-auditor", "evidence-researcher", "skeptic", "adjudicator"]
    else:
        selected = ["strategy-analyst", "math-auditor", "skeptic", "adjudicator"]
    return [
        AgentAssignment(
            agent_role=name,
            task=ROLE_CATALOG[name].purpose,
            read_only=ROLE_CATALOG[name].read_only,
        )
        for name in selected
    ]
