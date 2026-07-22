# P2-011A budget execution contract

## Scope

P2-011A adds a strict, versioned, in-memory contract for one serial run. It does not add a
parallel scheduler, automatic retry, durable reservation, manifest settlement, resume accounting,
or process-level hard stop. Those concerns remain in later milestones.

`BudgetPolicyV2`, `BudgetSnapshot`, `UsageDelta`, and `BudgetFailure` use schema `2.0.0`, reject
unknown fields and coercion, and are immutable Pydantic values. Boolean-as-integer, numeric strings,
NaN, infinity, negative counts, unsupported concurrency, and runtime values that cannot be
represented as integer nanoseconds fail validation. Canonical JSON uses sorted keys and compact
UTF-8 encoding; its SHA-256 binds snapshots to the exact policy.

## Units and limits

| Field | Unit | P2-011A behavior |
|---|---:|---|
| `max_deliberation_rounds` | analysis batches | The ordinary run starts one batch; zero skips provider analysis with a structured limitation. |
| `max_tool_retries` | additional candidate attempts | Used in classification only; no retry is executed automatically. |
| `max_concurrent_agents` | in-flight attempts | Exactly `1`; every other value is unsupported. |
| `max_runtime_seconds` | monotonic seconds | Positive and finite; converted exactly to integer nanoseconds. |
| `max_external_cost_micro_usd` | micro-USD | Integer cap checked before an external provider attempt. |
| provider/tool caps | canonical JSON UTF-8 bytes | Per-value peak limits use sorted compact JSON; equality is allowed and over-cap is rejected. Tool output means the calculator's typed output payload, not its audit envelope. |
| `max_artifact_bytes` | serialized artifact UTF-8 bytes | Per-file peak limit measured from the exact bytes written by `RunStore`. |
| `max_run_bytes` | stored bytes | Current whole-run size, including the run sentinel; overwrites replace rather than double-count prior bytes. |

The legacy `BudgetConfig` is a v1 input surface. It is copied and validated once, then explicitly
migrated to `BudgetPolicyV2`. Historical fields that did not control the ordinary run resolve to the
effective baseline: one analysis batch, zero automatic retry, and peak concurrency one. The legacy
`max_output_bytes` expands once into provider-output, tool-input, tool-output, and artifact caps;
it is not a second active v2 source. `max_agent_depth` has no v2 active field, and unsupported legacy
claims fail closed. Decimal USD is accepted only by the conversion helper and must map exactly to an
integer number of micro-USD.

## Accounting and execution classes

`SerialUsageLedger` owns an attempt/run-local `BudgetSnapshot`. While budget observations remain
healthy, it keeps provider and tool attempts, retry candidates, active runtime, external cost, peak
canonical provider/tool values, peak artifact size, current run size, and peak concurrency in
separate fields; unlike units are never summed. A
rejected preflight or storage projection does not commit proposed usage. Actual runtime and completed
effect usage are settled together and remain in the snapshot even when the settlement exceeds a
limit; the typed failure then remains sticky and blocks later effects. `RunStore` reports the exact
projected artifact and run sizes to the in-memory ledger before each write. Clock rollback and
policy-hash substitution fail closed.

After a sticky runtime or clock-observation failure, the orchestrator stops forwarding storage
observations to the ledger. Failure-report writes still pass through `RunStore`'s independent
artifact/run hard caps, but those later physical bytes are not represented in the settled in-memory
snapshot.

Provider availability declares `local_free`, `external`, or `unknown` execution. Unknown execution,
unknown external cost, a zero external-cost cap, and over-cap estimated cost are rejected before
`analyze`. `local_free` providers and deterministic local calculators remain usable with a zero
external-cost cap. P2-011A trusts the injected provider's declared class and estimate; metering and
durable settlement are not implemented.

For compatibility with the pre-P2-011A injected-provider API, an availability value constructed
without the new `execution_class` field is treated as a legacy in-process `local_free` declaration.
An explicitly supplied `unknown` value remains fail closed. New or external providers must declare
their class explicitly; the compatibility rule does not infer or meter an external invoice.

The state machine and effect executors share an injected monotonic clock. Active run time is observed
at serial boundaries. Each provider/tool output carries the greatest validated clock observation
made at that effect boundary. The orchestrator settles that high-water mark with the effect usage,
so a later clock rollback cannot be accepted between serial effects. Entering human approval wait
pauses the ledger, so waiting time is excluded. The ledger is not written to a manifest. The existing
resume surface reconstructs elapsed time only; subsequent storage writes observe current physical
artifact sizes without restoring external cost, attempt, or prior provider/tool byte accounting.

## Failure, retry, deadline, and cancellation

Retry classification and retry execution are separate. Validation, unsupported, unavailable,
budget, deadline, cancellation, policy, deterministic tool, verification, and unknown external
effect failures are non-retryable. A transient provider/tool failure is only a retry candidate when
its effect is idempotent, reconcilable, or not applicable. For `N` declared retries the classifier
exposes at most `N + 1` candidate attempts, while `automatic_retry` is always false.

Provider control distinguishes `timed_out`, `cancel_requested`, `cancel_unconfirmed`, and
`cancelled`. A worker that acknowledges cancellation is recorded separately from one still alive or
exiting without acknowledgment. In-process cooperative cancellation is not described as a hard
stop; process-tree termination, durable cancellation, and remote reconciliation remain deferred.

`AnalysisOutput` and `ToolResearchOutput` carry typed usage, retry classification, and budget
failure values. Effect
executors cannot write artifacts or transition workflow state. The orchestrator settles usage first,
then decides state and fixed artifact writes. Oversized, malformed, or budget-refused provider/tool
values cannot choose a path or become a successful result. Provider reports and typed tool outputs
are measured before redaction as well as after normalization, so a secret-shaped value cannot shrink
below a cap through redaction. Artifact/run cap errors are typed budget failures; `Orchestrator.run`
returns a minimal `failed_with_limitations` value when the cap prevents writing the ordinary report.
