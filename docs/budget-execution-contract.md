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
| provider/tool/artifact caps | UTF-8 bytes | Separate canonical limits; equality is allowed and over-cap is rejected. |
| `max_run_bytes` | bytes | Existing `RunStore` whole-run fail-closed behavior is retained. |

The legacy `BudgetConfig` is a v1 input surface. It is copied and validated once, then explicitly
migrated to `BudgetPolicyV2`. Historical fields that did not control the ordinary run resolve to the
effective baseline: one analysis batch, zero automatic retry, and peak concurrency one. The legacy
`max_output_bytes` expands once into provider-output, tool-input, tool-output, and artifact caps;
it is not a second active v2 source. `max_agent_depth` has no v2 active field, and unsupported legacy
claims fail closed. Decimal USD is accepted only by the conversion helper and must map exactly to an
integer number of micro-USD.

## Accounting and execution classes

`SerialUsageLedger` owns an attempt/run-local `BudgetSnapshot`. It keeps provider and tool attempts,
retry candidates, active runtime, external cost, the four byte classes, run bytes, and peak
concurrency in separate fields; unlike units are never summed. A rejected preflight does not commit
the proposed usage. Clock rollback and policy-hash substitution fail closed.

Provider availability declares `local_free`, `external`, or `unknown` execution. Unknown execution,
unknown external cost, a zero external-cost cap, and over-cap estimated cost are rejected before
`analyze`. `local_free` providers and deterministic local calculators remain usable with a zero
external-cost cap. P2-011A trusts the injected provider's declared class and estimate; metering and
durable settlement are not implemented.

The state machine and effect executors share an injected monotonic clock. Active run time is observed
at serial boundaries. Entering human approval wait pauses the ledger, so waiting time is excluded.
The ledger is not written to a manifest and is reconstructed only from the legacy elapsed-time
snapshot on the existing resume surface.

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

`AnalysisOutput` and `ToolResearchOutput` carry typed usage and budget failure values. Effect
executors cannot write artifacts or transition workflow state. The orchestrator settles usage first,
then decides state and fixed artifact writes. Oversized, malformed, or budget-refused provider/tool
values cannot choose a path or become a successful result.
