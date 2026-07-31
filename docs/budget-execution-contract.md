# P2-011A / P2-011B budget execution contract

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

## P2-012Aとの関係

revision storage の `BudgetPolicyBindingV1` は current `BudgetPolicyV2` schema version と
policy SHA-256 の correlation evidence だけを保存する。P2-012A の物理 byte admission は
dedicated revision root の独立 hard cap であり、P2-011A の in-memory usage ledger、
cost/runtime settlement、retry classification を復元・予約・永続化しない。

durable usage reservation、resume settlement、multi-process budget CAS、parallel slot、
automatic retry、cancellation state は通常のP2-011A product経路には追加されない。

## P2-011B internal durable contract

P2-011BはP2-011Aを変更せず、schema `1.0.0`のstrict/frozenな内部contractを別moduleとして
追加する。`DurableBudgetPolicyV1`はexactな`BudgetPolicyV2`と、既定値
`max_concurrent_agents=1`、`max_automatic_retries=0`の`ExecutionActivationV1`を束縛する。
1から32のbounded concurrencyと、既存`max_tool_retries`以下のautomatic retryは、この内部APIを
明示利用した場合だけ有効である。

canonical resource orderは
`active_runtime_ns`, `provider_attempts`, `tool_attempts`, `retry_attempts`,
`external_cost_micro_usd`, `provider_output_bytes`, `tool_input_bytes`,
`tool_output_bytes`, `artifact_bytes`, `run_bytes`, `concurrency_slots`である。全resourceとslotを
1回のimmutable successorとrevision CASで予約し、一部だけのpermitは作らない。admissionはsettled
usageと全active reservationを含む。settlementはobserved actual、exact unused release、result/effect/
cancellation evidence、statusを一度だけ記録する。actual overrunは切り詰めず、
`settlement_overrun`をlatchして新規workを拒否する。

active runtimeは注入monotonic clockから測り、process-localの絶対値ではなくrun-local累積値と
remaining durationだけを保存する。restartではclockをrebaseしてwall downtimeを除外し、人間承認待ちも
callerが明示rebaseする。rollbackと期限超過は新規work前にfail closedになる。permit/settlement/
cancellationに保存する時刻も累積active-runtime observationであり、OS monotonic epochではない。

external executionは正のinteger micro-USD estimateと認証済みestimate flagを予約前に要求する。
settlementはcaller-supplied actualと別の認証済みactual flagを要求し、externalであることだけから
認証を推論しない。これはprovider invoice meteringやbilling-source authenticityの主張ではない。
`local_free`はcost 0だけを許可する。

各mutationは`operation_id`とcanonical request SHA-256を持つ。exact replayは記録済み結果を返し、
bytesが異なるkey reuseは`idempotency_conflict`になる。committed reserved-not-started permitは
明示的なno-effect releaseだけで閉じる。started-unsettled permitはrestart時に`effect_unknown`/
`reconciliation_required`となり、blind release、retry、success扱いをしない。run lock、revision CAS、
current-replace ambiguity、durability uncertainty、effect unknownは別のtyped failureである。
budget binding付きreserveも記録済みoperationのexact replayを先に判定し、未記録mutationだけを
current policy/activation digestへCAS相関する。後続の正当なpolicy tighteningは過去のexact replayを
invalid inputへ変換しない。

retry admissionは実行から分離し、明示的transientかつidempotent、またはauthoritative reconciliation
済みのeffectだけを最大N+1 attemptまで許可する。retryごとにfresh attempt/context IDと既存
`ContextEnvelope` lifecycleを使い、root/parent/source/owner/role/phase/assignment/ordinal lineageを
再検証する。raw context/provider/tool payloadやsecretはbudget stateへ保存しない。

`DurableBoundedExecutor`はreserve-before-start、bounded worker数、ordinal順reduction、peak concurrency、
exact settlement replayを実装する内部callable adapterである。完了済みdeterministic calculatorは
durable settlementから復元し、callable再実行、二重課金、二重settleを行わない。cancellationは
`requested`、`acknowledged`、`cancelled`、`unconfirmed`、`effect_unknown`を別revisionで記録する。
acknowledgmentなしのsuccessやlive workerはsuccessにならない。
P2-028Aのprocess不在回復では`requested`/`unconfirmed`をworker非liveの`effect_unknown`へ閉じ、
exact ACK evidenceは`cancelled`まで完遂してから対応permitをsettleする。
effect admission後またはresume成否不明のclosureではattempt 1、approved input bytes、concurrency 1を
必ずactual usageへ含め、保存済みevidence/outputがあるrestart closureでは既知のoutput usageも含める。
tree停止を確認できない場合はworker-liveの`effect_unknown`としてpermitをsettleしない。
二度目のapproval拒否がpermit start前に確定した場合は`released_no_effect`、start後でも
`ResumeThread`前のexpiry/identity拒否が確定した場合は`failed`として閉じ、effect不明とは記録しない。

P2-011B自体はtyped RM-028 isolation requirement/evidence interfaceだけを提供する。P2-028Aは別の
Windows backendとして、固定repository synthetic helperに限りprocess-tree kill、Job Object
CPU/memory/process cap、bounded output、durable cancellation/reconciliationを実装する。remote
cancellation、network isolation、任意external code、provider/solverには使えず、それらを要求する
requestは引き続きreservation前に`isolation_required`となる。P2-011Bはexternal provider/solver、
completion marker、product reader/status、flat-v1 migration、通常run/resume統合を実装しない。

P2-028AがP2-011Bへsettleするstorage usageはcaptured output payloadを単位とし、
`artifact_bytes=max(stdout, stderr)`、`run_bytes=stdout+stderr`である。isolated-job state、
manifest、transaction、current pointer等のrevision構造byteはこの値に含めず、専用revision storeの
physical artifact/run admissionで別に制限する。

## P2-010B budget correlation

P2-010Bは現在の`BudgetPolicyV2` schema versionとcanonical SHA-256を
`BudgetPolicyBindingV1`としてfinal-report-v2 provenanceへ相関するだけである。phase traceの
usage、retry classification、deadline/cancellation値は既存validationを通るが、coordinatorは
usageを再settleせず、reservation、durable ledger、retry、wait、parallel slot、budget CASを
開始しない。publishまたはapply失敗も自動retryへ変換しない。

## P2-012B terminal publication settlement

P2-012Bの通常product経路は、専用terminal revision rootとは別のP2-011B budget rootを使う。
terminal requestをfreezeした後、revision filesystem mutationより前に`local_free`、external cost 0、
concurrency slot 1、verified remaining active runtime、最大planned artifact bytes、exact persistent
deltaを一つのreservationとして確保する。provider/tool/retry/cost resourceはterminal publicationで
二重計上しない。

pointer publication後はactual terminal I/O runtime、artifact/run bytesを、verified pointer SHA-256と
completion marker SHA-256（checkpoint/migrationではmanifest SHA-256）へexactly-once settleする。
readerはpolicy/run/operation/permit/settlement ID、reservation request hash、result/effect hash、
terminal settlement statusを再検証する。missing、overrun、conflict、effect unknown、reconciliation
requiredではterminal statusを返さない。

2 rootを跨ぐatomic filesystem transactionはない。pointer後settlement前のcrashは物理revisionを
残し得るが、completedへ昇格しない。prewrite no-effect failureだけがexact releaseを許される。
terminalization自体が短いruntime/artifact hard capを超えた場合、ordinary callはdurabilityを主張せず
`failed_with_limitations`へdowngradeする。
