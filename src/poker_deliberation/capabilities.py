"""Canonical high-level capability states exposed by doctor and documentation tests."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

CapabilityState = Literal["implemented", "disabled", "unavailable", "planned"]


@dataclass(frozen=True, slots=True)
class Capability:
    capability_id: str
    state: CapabilityState
    summary: str


CAPABILITIES: tuple[Capability, ...] = (
    Capability(
        "local_calculators",
        "implemented",
        "Registered deterministic calculators execute locally with typed ToolResult output.",
    ),
    Capability(
        "local_provider",
        "implemented",
        "LocalProvider validates boundaries but does not generate specialist prose.",
    ),
    Capability(
        "openai_agents_outbound",
        "disabled",
        "The provider boundary exists, but outbound analyze is not implemented.",
    ),
    Capability(
        "external_solver",
        "unavailable",
        "Only an honest unavailable adapter is bundled; no external solver executes.",
    ),
    Capability(
        "full_nlhe_equilibrium",
        "unavailable",
        "No full NLHE game tree, CFR, node locking, or qualified equilibrium solver is present.",
    ),
    Capability(
        "heads_up_nlhe_equity",
        "implemented",
        "Heads-up NLHE equity supports bounded exact enumeration and seeded Monte Carlo.",
    ),
    Capability(
        "multiway_or_plo_equity",
        "unavailable",
        "Multiway and PLO equity are outside the implemented calculator scope.",
    ),
    Capability(
        "documented_hand_parser",
        "implemented",
        "The conservative key-value/player/action grammar is supported.",
    ),
    Capability(
        "natural_language_or_site_parser",
        "unavailable",
        "Natural-language and site-specific hand histories are not parsed.",
    ),
    Capability(
        "process_sandbox",
        "unavailable",
        "Local tools have structural caps but no OS-level CPU or memory sandbox.",
    ),
    Capability(
        "parallel_deliberation_and_tool_retry",
        "disabled",
        "Budget fields exist, but ordinary orchestration does not run parallel rounds or retries.",
    ),
    Capability(
        "phase_1_hardening",
        "planned",
        "Phase 1 contracts remain roadmap work and are not implemented by Phase 0.",
    ),
)


def capability_snapshot() -> list[dict[str, str]]:
    return [asdict(capability) for capability in CAPABILITIES]
