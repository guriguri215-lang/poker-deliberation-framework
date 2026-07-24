# P2-010A phase service contract

## Status and scope

- **FACT**: `poker_deliberation.phases` is a versioned internal API with schema `1.0.0`; it is not
  declared as a stable public API.
- **FACT**: `Orchestrator.run` remains the only owner of workflow transitions and run-artifact
  writes. Phase services return values and never receive `RunStore` or `WorkflowStateMachine`.
- **FACT**: execution remains serial. P2-011A adds typed in-memory budget accounting and retry
  classification around the P2-010A effect adapters, but no automatic retry, parallelism,
  persistence transaction, manifest revision, lock, recovery, CAS, durable resume, or cleanup.

## Common boundary

Every call uses a strict `PhaseRequest[T]` and `PhaseOutcome[U]`. The boundary binds run ID, phase
ID, schema version, phase-attempt ID, canonical input hash, policy-snapshot hash, and ordered context
IDs. Missing/extra fields, unsupported versions, non-canonical values, hash mismatch, phase/input
mismatch, and request/outcome correlation mismatch fail closed. Inputs and outputs are dumped and
revalidated at each boundary so caller-owned nested values are not used as phase working memory.
Strict mode also rejects Python-side scalar and collection coercion; policy such as sensitive-action
categories is passed as a canonical sorted snapshot instead of read from a mutable global registry.

An outcome is either output-bearing (`succeeded` or `completed_with_failures`) or a top-level typed
failure. It can request a next state and carry `ArtifactIntent` values, but cannot perform either
effect. Artifact intents reject absolute paths, drive/URI forms, backslashes, empty segments, and
`.`/`..`. The orchestrator compares synthesis intents with an exact fixed allowlist before writing.

## Pure phases

`IntakeValidation`, `Normalization`, `Routing`, `ContextBuild`, `Critique`, `Adjudication`, and
`Synthesis` are deterministic services. Time, identity, policy, capability/tool-name snapshots, and
provider availability are explicit inputs. The pure service module does not import storage, state
machine, provider, tool registry, filesystem paths, ambient clock, UUID/random/secrets, or a mutable
global registry.

- Intake validates evidence links and approval proposals as values and produces the redacted case.
- Normalization returns the isolated canonical case, assumptions, and warnings.
- Routing validates the canonical role order. Calculation retains `math-auditor` and the assigned
  but unexecuted `report-writer`, both with empty `context_keys`.
- ContextBuild produces a fresh P2-024A `ContextEnvelope` from injected UTC time and IDs and preserves
  `attempt-memory-only-v1`; the dispatch context must exactly match the envelope's canonical payload
  and assignment hash, and no envelope is persisted.
- Critique rejects provider claims from adjudicated conclusions until typed evidence/tool checks
  exist and applies the deterministic results-orientation rules.
- Adjudication consumes complete `ToolResult` values and preserves tool-specific exactness and
  tolerance rules. It does not infer GTO, equilibrium, ranges, or solver availability.
- Synthesis builds a `FinalReport` draft and fixed artifact intents. It does not query provider,
  state, or storage and does not render or write Markdown.

## Effect executors

`AnalysisExecutor` and `ToolResearchExecutor` are explicit serial adapters. Neither writes artifacts
or transitions workflow state.

Analysis validates the context envelope and run/assignment/context/attempt lineage before provider
handoff. It checks role/task correlation, output size, and portable unique report IDs. Unsafe or
duplicate IDs cannot select an artifact path and become safe fallback reports. Provider-supplied
epistemic labels are untrusted and normalized to `USER_CLAIM`/at most confidence C; synthesis exposes
unadjudicated provider sections as `UNKNOWN`. The executor and orchestrator both bind the returned
assignment, context, envelope, report, execution record, context hashes, and report ID back to the
exact Analysis request before any artifact path is selected.
The input also binds a strict v2 budget policy, current policy-hashed usage snapshot, and a single
captured provider-availability value. Unknown/disabled/over-cap external execution is refused before
`analyze`; accepted provider output bytes and attempt/cost usage are returned as typed values. Raw
provider reports and typed tool outputs are capped before redaction. A provider availability created
through the legacy API without `execution_class` retains its historical in-process/local behavior;
an explicitly declared `unknown` class remains refused.
Deadline, cancellation, budget, and retry classification are explicit output fields.

ToolResearch binds the full original `ToolRequest`, ordinal, run/phase attempt, canonical input hash,
requested/supported/result contract versions, validated/materialized input hashes, and complete
unprojected `ToolResult`. The orchestrator revalidates every binding against the outer phase request
before writing. Contract-version mismatches and unsafe/duplicate result IDs become safe failed
results. Exactness, assumptions, warnings, confidence interval, error and
verification metadata, seed/sample/iteration/stopping data, and reproduction metadata are preserved.
The executor is used once for structured-hand validation before provider analysis and once for the
ordered requested-tool batch after analysis; repeated `hand_validator` remains skipped.
Tool input and complete result bytes are charged to separate UTF-8 caps. A budget failure stops later
tool calls in the batch and returns correlated failed results; it cannot write or transition state.

## Compatibility and deferred work

`Orchestrator` constructor's original four positional parameters, `run`, `resume`, `load_report`,
`report_path`, CLI commands/exit codes, `CaseInput`, `FinalReport`, public `ToolRequest`/`ToolResult`,
artifact names, normalized order, provider/tool call order, and P2-024A context enforcement remain
compatible. Phase requests/outcomes are not new run artifacts.

P2-011A usage values are schema `2.0.0` internal fields and preserve the phase contract's `1.0.0`
outer request/outcome version. No durable usage artifact is added. See
`docs/budget-execution-contract.md` for units, migration, and deferred behavior.

The existing whole-run atomicity limitation is unchanged: the completion transition can be written
before a later final-report write fails. P2-010B adds a separate opt-in revision-only seam; it does
not retrofit ordinary flat-v1 execution or claim whole-run fault-atomic completion.

## P2-010B internal revision-only seam

`PhaseRevisionCoordinator` consumes a frozen trace of already-computed ContextBuild, Analysis,
ToolResearch, and Synthesis requests/outcomes. It revalidates hashes, context and tool provenance,
exact admitted artifact bytes, classification, and the final-report-v2 graph before
`RunRevisionStore.publish`. It never invokes a provider, tool, retry loop, state callback, or
ordinary `RunStore`.

The orchestrator previews a `FINAL_SYNTHESIS` to `COMPLETED` plan without mutation. Only a confirmed
`published` or exact same-process `current_committed` outcome can create a nonserializable
authorization. The orchestrator then applies that exact plan under the state-machine lock.
Validation, storage uncertainty, stale/reconstructed authority, and pre-apply faults produce no
transition. A committed structural revision followed by apply failure is not rolled back and is not
a terminal product run. P2-011Bのinternal budget APIは別root/boundaryであり、P2-012B product
integrationは引き続き別承認である。

## P2-012A phase provenance

P2-012A の `PhaseBindingV1` は phase request/outcome から run/phase/attempt、context IDs、
input/policy/output hash、ordered `ArtifactIntent` snapshot を相関用に保存できる。
nonnull intent content hash は同じ revision の admitted inventory bytes と一致しなければならない。
`state.json` intent は provenance には残せるが P2-012A payload table には入らない。

この binding は phase 実行、transition、artifact write authority、parallel scheduling を与えない。
P2-010A の pure/effect ownership と既存 orchestrator order は変更しない。

## P2-011B execution boundary

P2-011Bの`DurableBoundedExecutor`は内部opt-in callable adapterであり、P2-010AのAnalysis/
ToolResearch executorや通常Orchestratorへ注入されない。typed ownership、fresh retry lineage、
reserve-before-start、bounded fan-out、ordinal順reduction、cancel fan-out、exactly-once settlementを
専用budget rootへ記録するが、phase request/outcome schema、provider/tool call order、artifact write、
state transition authorityを変更しない。

したがって通常product経路はserialのままで、automatic retryを実行しない。P2-011Bのworkerは
shared mutable payloadを受け取らず、in-process cooperative tokenをhard stopと表現しない。
