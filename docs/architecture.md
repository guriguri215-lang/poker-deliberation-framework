# Architecture

## Components

The Codex-native layer and the Python orchestrator are separate execution surfaces. The Codex-native
surface provides durable repository instructions, project configuration, specialist definitions,
and reusable skills. The Python surface owns its deterministic state and audit records. Python does
not launch `.codex/agents/*.toml`, and Codex sub-agent executions are not automatically converted into
Python `AgentExecutionRecord` entries or run artifacts.

- `schemas.py`: strict Pydantic contracts.
- `state_machine.py`: legal bounded transitions and budget counters.
- `orchestrator.py`: routing, provider calls, tools, adjudication inputs, synthesis, artifacts.
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
cost defaults, and terminal conditions. Budget fields for deliberation rounds, tool retries, and
concurrency exist, but ordinary orchestration does not execute those controls; they are documented as
disabled until their semantics are implemented and contract-tested.

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

## Trust boundaries

Web pages, GitHub content, hand histories, ranges, and model output are untrusted data. Only typed
input, validated tool results, primary evidence records, and explicit approvals cross into synthesis.
New run IDs are created exclusively; only resume may mutate an existing run.

High-level `implemented / disabled / unavailable / planned` states are centralized in
`capabilities.py`, exposed by `doctor`, documented in `docs/capabilities.md`, and checked by contract
tests. The offline public preflight scans only Git-tracked and non-ignored public candidates; it does
not enumerate ignored user data or run artifacts.
