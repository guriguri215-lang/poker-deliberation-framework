# Architecture

## P2-027A local-data policy boundary

`local_data_policy.py` は strict versioned values、canonical hash、分類、retention、expiry、
protection、quarantine/disposition candidate、bounded audit metadata を提供する pure domain
module である。injected UTC clock 以外の effect を持たず、filesystem、RunStore、orchestrator、
provider、approval ledger、CLI へ接続しない。cleanup executor は P2-027B の別承認対象である。

## Components

The Codex-native layer and the Python orchestrator are separate execution surfaces. The Codex-native
surface provides durable repository instructions, project configuration, specialist definitions,
and reusable skills. The Python surface owns its deterministic state and audit records. Python does
not launch `.codex/agents/*.toml`, and Codex sub-agent executions are not automatically converted into
Python `AgentExecutionRecord` entries or run artifacts.

- `schemas.py`: strict Pydantic contracts.
- `state_machine.py`: legal bounded transitions and budget counters.
- `budgets/`: strict policy, canonical usage values, injected monotonic clocks, serial ledger,
  retry classification, and deadline/cancellation vocabulary.
- `orchestrator.py`: routing, provider calls, tools, adjudication inputs, synthesis, artifacts.
- `phases/`: strict internal phase contracts, deterministic pure services, and serial Analysis /
  ToolResearch effect adapters. See `docs/phase-services.md`.
- `context_lifecycle.py`: attempt-scoped policy, immutable envelope, integrity, expiry, and lineage.
- `normalization.py`: conservative documented free-text hand format to canonical schema.
- `approvals.py`: explicit pending/approved/rejected ledger.
- `storage/`: run-root confinement and atomic JSON/text writes.
- `tools/`: deterministic calculators and an honest solver adapter.
- `providers/`: local non-generative provider and optional Agents SDK boundary.
- `reporting/`: renders only the structured FinalReport.

## State machine

`INTAKE -> NORMALIZE -> DATA_VALIDATION -> TASK_ROUTING` then either analysis or direct tools,
followed by `CRITIQUE -> ADJUDICATION`. Sensitive requests enter `HUMAN_REVIEW_REQUIRED`.
Safe paths end through `FINAL_SYNTHESIS -> COMPLETED`; unavailable approved external execution may
end at `FAILED_WITH_LIMITATIONS`.

The application, not an SDK or model, owns transition legality, runtime checks, output/run caps,
cost preflight, and terminal conditions. The v2 budget baseline is one serial analysis batch, zero
automatic retries, and peak concurrency one. A zero batch cap skips provider analysis; retry counts
remain classification-only. See `docs/budget-execution-contract.md`.

## Provider boundary

`LocalProvider` is the default and produces no specialist prose. `OpenAIAgentsProvider` reports
`disabled` and `available=false` for every SDK/API-key combination because outbound `analyze` is not
implemented. Package and key probes are diagnostics, not proof of execution capability. No external
data is sent.

Every provider receives a role-specific `AgentContext`, not the complete `CaseInput`. Before a call,
the orchestrator builds and validates a strict versioned immutable attempt envelope covering exact
top-level allowlist, UTC use-expiry, classification, canonical payload/policy/integrity hashes, and
run/assignment/attempt/runtime lineage. A fresh `AgentContext` is materialized only for an allowed
handoff. Provider prose and claims remain `UNKNOWN` and cannot become a final conclusion without
tool/evidence adjudication. The provider contract receives a cooperative deadline/cancellation
control; the disabled external provider cannot bypass this boundary, and the local provider checks
it. See `docs/context-lifecycle.md` for the exact contract and explicit non-goals.

The envelope is attempt-memory-only and is not a new artifact. Execution records preserve the exact
legacy full-`AgentContext` hash calculation in `context_sha256` and add separate sparse payload/source
and envelope audit metadata. Persistence, retention duration, deletion, cleanup, durable trust
anchors, and cross-runtime execution remain outside P2-024A.

## P2-010A phase boundary

`Orchestrator.run` invokes IntakeValidation, Normalization, Routing, ContextBuild, Analysis,
ToolResearch, Critique, Adjudication, and Synthesis through strict versioned request/outcome values.
The seven compute phases are deterministic and have no filesystem, state, provider, registry,
network, approval-ledger, ambient-time, or ambient-random ownership. Analysis and ToolResearch are
the only effect adapters; they remain serial and cannot write artifacts or transition state.

The orchestrator validates request/outcome correlation, exact synthesis artifact intents, and the
only permitted requested terminal state before performing the existing fixed writes/transitions.
Calculation still assigns `math-auditor` and `report-writer` without executing either provider role.
Phase values are internal and are not persisted as new artifacts. P2-010A does not change the known
whole-run atomicity limitation; durable transition ordering belongs to P2-012A/P2-010B.

P2-011A extends Analysis and ToolResearch values with policy-bound usage deltas, typed budget
failures, retry classification, and deadline/cancellation status. Each effect adapter preflights its
own provider or tool boundary, but only the orchestrator settles run usage and decides subsequent
state/artifact effects. Accounting is in-memory and serial; no reservation, durable manifest,
transaction, CAS, resume settlement, scheduler, or automatic retry is introduced.

## Trust boundaries

Web pages, GitHub content, hand histories, ranges, and model output are untrusted data. Only typed
input, validated tool results, primary evidence records, and explicit approvals cross into synthesis.
New run IDs are created exclusively; only resume may mutate an existing run.

High-level `implemented / disabled / unavailable / planned` states are centralized in
`capabilities.py`, exposed by `doctor`, documented in `docs/capabilities.md`, and checked by contract
tests. The offline public preflight scans only Git-tracked and non-ignored public candidates; it does
not enumerate ignored user data or run artifacts.
