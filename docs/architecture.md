# Architecture

## P2-027A local-data policy boundary

`local_data_policy.py` は strict versioned values、canonical hash、分類、retention、expiry、
protection、quarantine/disposition candidate、bounded audit metadata を提供する pure domain
module である。injected UTC clock 以外の effect を持たず、filesystem、RunStore、orchestrator、
provider、approval ledger、CLI へ接続しない。後続 P2-027B はこの pure policy を変更せず、
別 module と専用 cleanup root に effect 境界を追加する。

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
Phase values are internal and are not persisted as new artifacts. P2-010A単独は当時のwhole-run
atomicity limitationを変更せず、通常product terminal publicationは後段のP2-012Bが所有する。

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

## P2-012A immutable revision foundation

`storage/revision_models.py`、`revision_canonical.py`、`revision_lock.py`、
`revision_store.py` は、既存 product 経路から切り離した opt-in の内部 storage layer である。
専用 root の ownership、domain-separated identity/hash、strict canonical inventory、
immutable transaction/manifest、serialized CAS、current-to-genesis structural read、
metadata-only recovery claim を担当する。

この layer は `RunStore` や orchestrator に注入されず、completion marker、terminal state、
resume、migration、retention action を所有しない。P2-010A/P2-011A の phase/budget value は
typed provenance として相関できるが、durable scheduler、budget reservation、phase state
persistence には変換しない。詳細は `docs/run-revision-storage.md` を参照する。

### Versioned final-report admission

`revision_canonical.py` は `final_report.json` の inventory
`artifact_schema_version` を唯一の semantic dispatch とし、完全な admission tuple を選ぶ。
v1 は immutable/readable のまま lexical tool-result comparison と mandatory context を使う。
v2 だけが tool binding の pair equality、contiguous execution ordinal、ordinal-based report
correlation、および provider trace と一致する context presence を追加する。storage protocol、
manifest/pointer/transaction、canonicalization、path/inventory ordering は変更しない。

この v2 artifact は internal `structural_nonterminal` で、terminal/product reader/resume
interface ではない。P2-010B coordinator は専用 producer-owned root と初回 target-run
history 条件を強制するが、same-build/no-rolling は trusted deployment assumption である。
現行 schema は mixed build を attest、detect、prevent せず、old v1-only reader は v2 を
unknown version として拒否する。product integration は P2-012B に残る。

## P2-010B revision transition ordering

P2-010Bは通常の`Orchestrator.run`経路とは別のinternal APIとして、
`_prepare_revision_bundle`と`_apply_revision_transition`を追加する。前者は
`FINAL_SYNTHESIS` machine snapshot、完全なphase preimage trace、canonical
`RevisionPublishRequestV1`を相関し、mutation-free planを作る。coordinatorは専用rootへの
structural revision publishを完了してからだけsame-process authorizationを発行する。後者は
machine lock内で作成元machine identity、fresh preview、plan hash、issuer identity、bundle
identityを再確認し、exact eventを最大1回だけ追加する。

ContextBuildはcanonical serviceで入力から再生し、normalized case、context/attempt ID、時刻、
dispatch、Analysis input、assignment ledger、agent-report artifact集合を一対一で照合する。
Synthesisもcanonical serviceで再生する。ToolResearchは各結果をcanonical `ToolContract`の
input/output schema、contract version、numeric/legacy exactness、status、assumptions、
model qualifier、再現コマンドへ照合する。`solver_status`は`unavailable`、空のsolver result、
`capability.available=false`以外を拒否する。storage outcomeはstrict modelで再検証した後、
verified current chainのrevision、transaction、manifest、pointerと一致した場合だけauthorityに
変換する。

このseamはterminal manifest、completion marker、product reader/status、durable resume、
migration、budget CAS、parallel schedulingを持たない。これはP2-010B seam単独の歴史的境界であり、
通常product runのmarker-last publicationは後段のP2-012B節に従う。

## P2-011B durable budget architecture（完了時点の履歴境界）

`budgets/durable_models.py`、`durable_store.py`、`execution.py`は通常product経路から切り離した
内部layerである。model layerはstrict immutable snapshotとcanonical hash、store layerはP2-012A
revision/CAS上のtyped mutationとhistory verification、execution layerはbounded callable、
retry admission、cooperative cancellation、RM-028 evidence interfaceを担当する。依存方向は
execution → durable store → revision storageで、storage package rootからproduct readerを公開しない。

各mutationはfull state snapshotを1 revisionとしてpublishする。policy/activation、run/generation/
previous hash、usage、active permits、settlements、attempt/context/owner lineage、cancellation、
idempotency operations、failure latch、deterministic eventsを結合する。resourceとslotは1 CASで
atomic reservationされ、result reductionはcompletion timingでなくexecution ordinalに従う。

P2-011B完了時点では、通常`Orchestrator`、P2-010A phase executor、P2-010B coordinator、
`RunStore`、CLI、flat-v1 artifact orderにこのlayerを注入しなかった。後続P2-012Bは下記の
product terminal publicationだけにこのstoreをcompositionし、P2-011Bの一般purpose executorや
P2-028A isolation implementationを通常経路へ有効化しない。

## P2-012B product durable-run architecture

P2-012Bでは`Orchestrator`の既存4 positional constructor parameterとpublic method signatureを維持し、
新しいstorage/budget/clock/ID dependencyをkeyword-onlyにする。通常実行中のartifactは
`BufferedRunStore`へcanonical bytesとして保持し、terminal boundaryで一つのfrozen
`TerminalPublishRequest`に変換する。flat-v1 `RunStore`はlegacy writer/reader互換面に限定され、
通常product runはそこへmaterializeしない。

依存方向はorchestrator → terminal store → P2-012A revision foundation、およびterminal store →
P2-011B durable budget storeである。product payload commitmentsはinput/state/report、
event、approval、context、execution、ToolResult/evidence/security/dispute ledgerを再計算する。
ToolResultの再現argvはpublish前にfreezeしたimmutable revision内のinput payload pathを指す。

`load_report`、`show`、`report_path`はverified V2 readerの結果だけを信頼する。同じrun IDまたは
ASCII-case aliasがlegacy/product両namespaceにある場合はconflictであり、自動選択しない。
v1-onlyはread-only projectionとして`failed_with_limitations`へdowngradeし、明示copy migration後も
`legacy_unverified`を維持する。

cross-rootのrevision publicationとbudget settlementは一つのfilesystem transactionではない。
そのためpointer/marker hashへ束縛したdeterministic settlementを必須とし、settlement未確認時は
物理的に完全なrevisionでも`incomplete`として扱う。このavailability lossはsuccess誤認を避ける
fail-closed境界である。
## Approval authority V2

P2-013A では phase intake が V1 proposal と V2 proposal を version dispatch する。V2 proposal は application-owned request identity に変換され、Orchestrator が唯一の transaction coordinator になる。

`DecisionAuthorityProvider` は actor の信頼境界、`TerminalRunStore` は per-run authority と CAS の所有者である。pure validation は lookup より前に ledger/log chain 全体を検証する。`UnavailableExternalExecutionBindingProvider` は承認済み request/outcome/authority lineage の一致だけを拘束し、外部 effect は起動しない。

V2 artifact は product manifest と approval lineage commitment に含まれる。既存の V1 `FinalReport.approvals` は authoritative V2 state の projection であり、public schema を拡張しない。

## P2-027B authorized cleanup architecture

`local_data_cleanup.py` は一つの明示 run を対象とする plan/execute/reconcile coordinator、
`local_data_cleanup_models.py` は strict immutable plan/transaction/manifest/receipt/tombstone、
`storage/local_data_cleanup_store.py` は専用 cleanup root の CAS と filesystem effect を所有する。
依存方向は cleanup coordinator → cleanup store → revision/terminal store であり、
通常 `Orchestrator`、CLI、flat-v1 writer、provider/solver 経路へは注入しない。

第1段階は verified terminal product run を same-volume rename で quarantine し、第2段階は固定30日後、
別 plan と別 destructive approval を要求して transaction-specific staging から bottom-up unlink する。
delete plan の entry/eligibility 時刻は live tombstone と policy の固定 window に再拘束し、各 revision
の tombstone retention は対応 receipt の commit 時刻から365日とする。
product namespace が detach 済みの間は `RunRevisionStore.acquire_detached_run_authority` が既存
P2-012A kernel lock を非bootstrappingで取得する。cleanup current は
`quarantined -> delete_prepared -> deleted` だけを許可し、各 revision は前 revision まで
current-to-genesis 検証される。

dry-run は effect-free で、実行は exact plan、P2-013A approval evidence、live actor/provider
authority、source/current/tree identity、same-volume、capacity を lock 内で再検証する。
final authority/hold admission は journal 公開後かつ effect 直前に行い、その後に local identity を
再走査する。pending/dangling control state は absent と推定せず effect unknown とする。
不確実な effect は成功へ丸めず、read-only reconciliation に停止する。詳細は
[`local-data-cleanup.md`](local-data-cleanup.md)を正本とする。
