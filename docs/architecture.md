# Architecture

## Components

The Codex-native layer provides durable repository instructions, project configuration, specialist
definitions, and reusable skills. The Python layer owns all deterministic state and audit records.

- `schemas.py`: strict Pydantic contracts.
- `state_machine.py`: legal bounded transitions and budget counters.
- `orchestrator.py`: routing, provider calls, tools, adjudication inputs, synthesis, artifacts.
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

The application, not an SDK or model, owns transition legality, rounds, retries, runtime, concurrency,
cost defaults, and terminal conditions.

## Provider boundary

`LocalProvider` is the default and produces no invented expert prose. Optional OpenAI Agents SDK
support first checks the package and API key. Its outbound implementation is intentionally disabled
until explicit approval, data-handling design, and integration tests exist.

Every provider receives a role-specific `AgentContext`, not the complete `CaseInput`. Provider prose
and claims remain `UNKNOWN` and cannot become a final conclusion without tool/evidence adjudication.
Contexts are deep copies. The provider contract receives a cooperative deadline/cancellation control;
the disabled external provider cannot bypass this boundary, and the local provider checks it.

## Trust boundaries

Web pages, GitHub content, hand histories, ranges, and model output are untrusted data. Only typed
input, validated tool results, primary evidence records, and explicit approvals cross into synthesis.
New run IDs are created exclusively; only resume may mutate an existing run.
