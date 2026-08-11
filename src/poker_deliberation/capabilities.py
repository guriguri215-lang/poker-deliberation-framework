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
        "Version 1 of the strict provenance-bound key-value/player/action grammar is supported; "
        "supported site: none.",
    ),
    Capability(
        "versioned_nlhe_range_grammar",
        "implemented",
        "Version 1 of the bounded provenance-qualified NLHE range grammar validates and "
        "canonicalizes one opponent range before combo expansion.",
    ),
    Capability(
        "versioned_nlhe_river_equity_bridge",
        "implemented",
        "P3-016B binds one validated versioned opponent range to exact-only heads-up river "
        "enumeration, an exact integer/rational oracle, a per-run-authority-serialized product "
        "namespace reservation and pre-execution admission commitment, and durable semantic "
        "replay.",
    ),
    Capability(
        "profiled_nlhe_side_pot_ledger",
        "implemented",
        "generic_nlhe_cash_no_rake_v1 provides an integer contribution, uncalled-return, "
        "side-pot, and eligibility ledger with an independent oracle.",
    ),
    Capability(
        "natural_language_or_site_parser",
        "unavailable",
        "General natural-language and site-specific hand histories are not parsed; the "
        "separately named bounded Japanese grammar does not change this state.",
    ),
    Capability(
        "bounded_japanese_nlhe_cash_parser",
        "implemented",
        "Version 1 deterministically parses one documented Japanese retrospective NLHE cash "
        "grammar with exact UTF-8 source spans and six-hash confirmation; it is not a general "
        "natural-language or site parser.",
    ),
    Capability(
        "bounded_japanese_river_call_ev_review",
        "implemented",
        "P3-030C binds one terminal river fold history and one explicit opponent range to "
        "the no-rake ledger, exact-only heads-up equity, exact Fraction call-EV oracle, "
        "LocalProvider context controls, and durable semantic replay.",
    ),
    Capability(
        "bounded_river_review_workflow",
        "implemented",
        "P3-030D composes one confirmed P3-030C review with one mode-bound P2-025B "
        "five-role bridge plan through canonical status, resume, linkage, and replay. P3-030E "
        "adds a pure read-only view of the existing verified FinalReport plus workflow and "
        "bridge hashes/state. P3-030F adds workflow-bound preview, explicit all-field "
        "confirmation, and one confirmed next-role execution at a time for supervised "
        "nonlocal mode; local_only role operations reject without transport, and automatic "
        "confirmation, bulk or parallel execution, retry, skip, and fallback are absent.",
    ),
    Capability(
        "confirmed_natural_language_review_intake",
        "implemented",
        "A caller-supplied complete projection can be explicitly hash-confirmed, admitted to "
        "the exact local-only review path, and bound to a verified durable report; no semantic "
        "natural-language parser is implied.",
    ),
    Capability(
        "process_sandbox",
        "unavailable",
        "No general sandbox exists for ordinary tools, arbitrary external code, providers, "
        "solvers, or network isolation.",
    ),
    Capability(
        "repository_synthetic_isolated_job_control",
        "implemented",
        "A Windows Job Object backend runs only the fixed repository synthetic helper with "
        "approval, context, budget, identity, handle, resource, durable-state, cancellation, "
        "and reconciliation controls; it is not a general process sandbox.",
    ),
    Capability(
        "parallel_deliberation_and_tool_retry",
        "disabled",
        "Budget fields exist, but ordinary orchestration does not run parallel rounds or retries.",
    ),
    Capability(
        "runtime_conformance_contract",
        "implemented",
        "P2-025A provides dedicated role inventories, canonical assignment/context/result "
        "contracts, pure cross-runtime checks, and verified Python product projection.",
    ),
    Capability(
        "local_only_runtime_mode",
        "implemented",
        "The explicit local_only mode runs deterministic parsing, calculators, LocalProvider, "
        "storage, replay, evaluation, and verified report projection without starting Codex, "
        "a model or nonlocal runtime, an API key, or network access.",
    ),
    Capability(
        "bounded_codex_river_review_bridge",
        "implemented",
        "The P2-025B bounded bridge is implemented only for one verified P3-030C river review "
        "and five fresh, serial, read-only turns with durable replay. Candidate-bound historical "
        "live evidence is preserved, but current-tree live qualification is UNKNOWN.",
    ),
    Capability(
        "codex_subscription_bounded_river_review",
        "implemented",
        "The explicit saved-ChatGPT-login route has a bounded no-fallback implementation and "
        "candidate-bound historical live evidence; it is not currently live-qualified until "
        "fresh evidence matches the current runtime inventory and role conformance.",
    ),
    Capability(
        "openai_api_bounded_river_review_adapter",
        "disabled",
        "The optional explicit openai_api adapter has deterministic no-network contract tests; "
        "it is disabled by default and live-unqualified in this milestone.",
    ),
    Capability(
        "codex_python_runtime_bridge",
        "unavailable",
        "No general Codex/Python bridge exists; the separately named P2-025B bounded river-only "
        "bridge does not provide broad interoperability.",
    ),
    Capability(
        "local_data_lifecycle_policy",
        "implemented",
        "P2-027A provides strict versioned local-data policy values, canonical hashes, and "
        "pure lifecycle evaluation without filesystem mutation.",
    ),
    Capability(
        "local_data_cleanup_executor",
        "implemented",
        "P2-027B provides bounded authorized quarantine, delayed staged deletion, immutable "
        "receipts and tombstones, revision CAS, idempotency, and read-only reconciliation.",
    ),
    Capability(
        "immutable_revision_storage_foundation",
        "implemented",
        "P2-012A immutable revision, manifest, transaction, lock, recovery-claim, and "
        "revision-CAS foundation plus the P2-010B internal revision-only phase transition "
        "authorization seam are implemented without product run integration.",
    ),
    Capability(
        "product_integrated_durable_run",
        "implemented",
        "P2-012B marker-last terminal publication, verified product reader/status mapping, "
        "approval-checkpoint resume, read-only flat-v1 adapter, copy-only migration, durable "
        "budget settlement, and lifecycle metadata integration are implemented.",
    ),
    Capability(
        "offline_evaluation_harness",
        "implemented",
        "P3-017A provides strict offline datasets, deterministic exact-evidence scoring, "
        "provenance binding, and reproducible result artifacts without external execution.",
    ),
    Capability(
        "phase_1_hardening",
        "implemented",
        "Phase 1 typed tool contracts, numeric exactness, executable verification, and local "
        "oracle/metamorphic tests are implemented.",
    ),
)


def capability_snapshot() -> list[dict[str, str]]:
    return [asdict(capability) for capability in CAPABILITIES]
