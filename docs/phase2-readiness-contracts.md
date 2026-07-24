# Phase 2 implementation contracts

## 文書状態

- **FACT**: この文書はRM-010〜013の実装前contractであり、本体実装ではない。
- **FACT**: 基準実装では`Orchestrator.run()`にphase処理、state transition、provider/tool実行、
  artifact書込、裁定、synthesisが混在する。
- **FACT**: 現在のrunには検証されるversioned manifest、artifact inventory、whole-run completion
  marker、cross-process lock/CASがない。
- **FACT**: 現在のprovider timeoutは協調cancelであり、無視するin-process処理を停止できない。
- **FACT**: 現在のapprovalにはactor、authority、revision、idempotency key、action digestがない。
- **INFERENCE**: 下記contractとacceptance testを満たすまで、RM-010〜013をimplemented/completedと表示しない。
- **ASSUMPTION**: このdraftは記載済みhuman approval事項を条件にfreezeする。未承認事項を実装判断で
  補完せず、承認で意味が変わる場合はcontractとcanonical statusを同じ変更で改訂する。

## 共通原則

1. stateは「現在実行中の処理」ではなく、**最後にdurably commitされたcheckpoint**を表す。
2. pure phaseはfilesystem、clock、network、state machine、approval ledgerを直接変更しない。
3. effect executorはprovider/tool/storageを呼ぶが、state transitionを決定しない。
4. orchestratorだけが次stateを要求し、state machineが合法性を検査する。
5. artifact transactionが成功した後だけcheckpointを進める。`COMPLETED`はimmutable revision内の
   terminal marker検証と、小さな`current` pointerのCAS publishが成功した場合だけ外部表示する。
6. retry、resume、approval、external actionはrun/phase/attempt/revision/idempotency lineageを持つ。
7. validation、安全拒否、unsupported、approval待ち、budget exhaustion、cancel、決定論的計算失敗を
   transient failureとして再試行しない。
8. hard stopを保証する外部処理はRM-028のprocess isolationを必須とする。協調cancelだけを
   `cancelled`または停止保証として表示しない。
9. existing public API、CLI exit code、artifact名/意味は明示したmigrationなしに破壊しない。
10. すべてのfailureは構造化し、成功またはGTO/均衡へ昇格させない。

---

## RM-010 — Orchestrator phase services

### 目的と非目標

**目的**は、既存behaviorを保持したままorchestrationをtyped phaseへ分割し、各phaseの入力、出力、
pre/postcondition、state要求、artifact intentを単独検証可能にすること。

**非目標**:

- parallel execution、automatic retry、external provider/solver、run migrationをこのRMだけで有効化しない。
- context retention/deletionをRM-010内で独自決定しない。RM-024を正とする。
- persistence atomicityをphase serviceへ埋め込まない。RM-012のartifact transactionを使う。
- 現在未実装のCodex/Python runtime bridgeを追加しない。

### 対象module

- `src/poker_deliberation/orchestrator.py`
- 新規の`phases` packageとtyped phase schema
- routing/provider/tool/adjudication/synthesis境界
- `state_machine.py`とのtransition request境界
- RM-012 artifact transaction interface

### Phase service境界とtyped I/O

| phase | typed input | typed output | effect |
|---|---|---|---|
| IntakeValidation | validated `CaseInput`, policy snapshot | `IntakeOutcome` | pure。安全性、evidence request、approval proposalを構造化 |
| Normalization | `IntakeOutcome` | `NormalizedCaseOutcome` | pure |
| Routing | normalized case、capability snapshot | `RoutingPlan` | pure |
| ContextBuild | plan、approved context policy | `ContextEnvelope[]` | pure。fresh immutable envelope/attempt |
| Analysis | envelopes、provider capability | `AnalysisBatchOutcome` | provider executor経由のeffect |
| ToolResearch | typed `ToolRequest[]`、registry snapshot | `ToolBatchOutcome` | tool executor経由のeffect |
| Critique | reports/results/evidence refs | `CritiqueOutcome` | pure |
| Adjudication | claims/disputes/verification | `AdjudicationOutcome` | pure |
| Synthesis | adjudicated record | `FinalReportDraft` | pure。filesystem/stateを変更しない |

全phaseは`PhaseRequest[T]`と`PhaseOutcome[U]`を使う。共通field:

- `run_id`, `phase_id`, `phase_schema_version`, `attempt_id`
- `input_hash`, `policy_snapshot_hash`, `context_ids`
- status (`succeeded`, `failed`, `cancel_requested`, `cancel_unconfirmed`, `cancelled`)
- typed outputまたはtyped failure、warnings、budget delta
- requested next state、artifact intents（内容/hash/分類だけ。書込はしない）

### Context ownership

- `ContextBuild`が各provider attempt専用のfresh immutable `ContextEnvelope`を所有する。
- providerへ渡したcopyはprovider所有だが、canonical envelopeを変更できない。
- assignmentの`context_keys`と実際のfield allowlistを機械的に一致させる。
- envelopeはRM-024のschema version、run/assignment/attempt lineage、source artifact hash、
  classification、expiry、retention-policy IDを持つ。
- retryは同じcanonical sourceから新しいattempt/envelopeを作り、前attemptのmutable objectを再利用しない。

### State transitionの責任主体

- phaseは`requested_next_state`を返せるが、stateを変更しない。
- orchestratorがoutcome、budget、artifact intentを検証し、RM-012 writerへtransactionを依頼する。
- writerがrequired artifactをcommitした後、orchestratorがstate machineへtransitionを要求する。
- state checkpointとmanifest revisionは同じtransaction generationへ結び付ける。
- illegal transitionまたはwrite failureではcheckpointを進めず、構造化failureを残す。

### Public / backward compatibility

保持対象:

- `Orchestrator.run()`, `resume()`, `load_report()`の呼出契約
- CLI command、exit code、`CaseInput`、`FinalReport`、`ToolResult`の意味
- redaction/security boundary、現在のdeterministic local calculator結果
- ID/timestamp/durationを正規化した既存goldenとartifact parity

変更候補で人間判断を要するもの:

- calculation caseで割り当てるが実行しない`report-writer`を互換artifactとして残すか、
  schema versionを上げて除去するか。
- legacy artifact順序を保持するか、RM-012 v2 manifestで意味だけを保持するか。

### Precondition / postcondition

Precondition:

- typed inputとschema versionがsupportedである。
- policy/capability/budget snapshotのhashがrequestと一致する。
- required dependency outcomeが同じrun/revisionに属する。
- expired/tampered context、unknown assignment/tool/providerを拒否する。

Postcondition:

- pure phaseではfilesystem/state/provider/tool call countが0。
- effect phaseは全attemptをexecution recordへ対応付ける。
- assignment/context/report、tool request/result/contract versionが相関する。
- output hash、warnings、failures、budget deltaが決定的に記録される。

### Failure taxonomy

- `PhaseInputError`, `UnsupportedPhaseVersion`, `PolicyMismatch`
- `ContextIntegrityError`, `AssignmentCorrelationError`
- `ProviderUnavailable`, `ProviderFailed`, `ToolFailed`, `VerificationFailed`
- `BudgetExceeded`, `DeadlineReached`, `CancelUnconfirmed`
- `ArtifactCommitFailed`, `IllegalTransition`, `InternalInvariantError`

failureをexception textだけで保存せず、code、phase/attempt/revision、retryable、cause chainを持つ。

### Artifact side effects / idempotency

- phase serviceのartifact side effectは0。
- artifact intentはcontent hashとlogical nameを持ち、RM-012 writerだけがmaterializeする。
- 同じ`phase_id + attempt_id + input_hash + policy_hash`は同じoutcome hashを返すか、
  nondeterministic providerでは同じexecution recordを再取得する。
- 異なるpayloadで同じidempotency keyを使った場合はconflict。

### Cancellation / timeout / concurrency

- pure phaseは呼出境界でcancel tokenを検査する。
- effect phaseはRM-011のdeadline/cancel stateを返す。
- RM-010の分割commitでは実行順を現在どおりserialに保つ。
- parallel化はRM-011のreservation、stable reduction order、cancel fan-out後にのみ有効化する。

### Migration / security boundary

- phase schemaはversionedとし、unknown future versionをfail closedにする。
- v1 monolithからの内部refactorはpublic result parityで検証し、artifact migrationはRM-012に委ねる。
- raw case、secrets、unredacted contextを不要なphaseへ渡さない。
- provider outputはUSER_CLAIM/UNKNOWN相当のuntrusted inputとして検証する。

### Acceptance criteriaとtest

- **unit**: 各phaseのstrict typed I/O、extra/missing field、pre/postcondition。
- **property**: 全合法/違法state sequence、mutation isolation、stable ordering。
- **integration**: existing `run/resume/show`、CLI exit code、normalized artifact parity。
- **adversarial**: assignment/report mismatch、context allowlist bypass、tampered hash、injected state。
- **fault injection**: 各phase compute、effect、artifact commit、transition境界。
- failure後に`COMPLETED`を表示せず、orphan attemptを識別できる。
- existing security、approval、context mutation、golden、tool contract testを維持する。

### Dependencies / human approval / safe commit units

Dependencies: P2-024A後にP2-010A（serial pure phase）を実装し、P2-012A後にP2-010B
（durable integration）を行う。RM-010完了gateはP2-010Bである。

P2-010Bの内部opt-in seamでは、`structural_nonterminal` revisionのpublicationが検証されて
初めて同一process内のstate transitionをauthorizeできる。このauthorization自体はterminal
checkpointやproduct statusではない。state checkpointをterminal manifest generationへbindし、
completion marker、verified product reader/status、migration、resume integration、lifecycle hookを
提供する責務はP2-012Bに残る。

この seam が使用する `poker-final-report-artifact-v2` は internal
`structural_nonterminal` contract である。v1 の canonical bytes、lexical tool-result order、
mandatory final-report context は変更しない。v2 だけが byte-identical な tool input/result
binding、unique contiguous ordinal、ordinal 順の embedded result、provider trace の有無と
一致する final-report context を要求する。inventory の schema version が唯一の dispatch で、
unknown/cross-labelled version は拒否する。

P2-010B は producer `p2-010b-phase-revision` version `0.2.0` が所有する専用 root と、
初回 publication 前に revision がない target run を使用する。同一 process で freeze した
元 request/plan の exact `current_committed` replay だけを例外とする。same-build、
no-mixed-build、no-rolling access は trusted deployment assumption であり、現行 schema は
検出・防止を証明しない。old build は v2 を support せず、`product_integrated_durable_run` は
P2-012B が完了するまで planned のままである。

Human approval:

- calculation assignment artifactの互換方針
- context retention/classification
- public phase schemaを安定APIとするか

Safe commit units:

1. phase schema/interfaceとcharacterization testsのみ。
2. pure intake/normalization/routing/context phases。
3. provider/tool effect adaptersとcorrelation checks。
4. critique/adjudication/synthesis pure phases。
5. orchestrator integrationとnormalized artifact parity。

---

## RM-011 — Budget and execution control semantics

### 目的と非目標

**目的**は、activeなbudget fieldをfinite、strict、実効的、監査可能にし、対応しないfieldをv2 active
schemaから除去すること。timeout/cancel/retry/concurrency/costを成功扱いせずfail closedにする。

**非目標**:

- 外部provider/solverを有効化しない。
- 協調thread cancelをhard process stopと呼ばない。
- Codex sub-agent depthをPython budgetとして管理しない。

### Budget field freeze

| current field | v2 decision | frozen semantics |
|---|---|---|
| `max_deliberation_rounds` | 実装 | provider analysis batchの開始回数。0ならprovider analysisなし。初回を1 roundと数える |
| `max_tool_retries` | 実装、v2 default 0 | 初回以外の追加attempt数。retryableかつidempotentなtransient failureだけ |
| `max_concurrent_agents` | 実装、v2 default 1 | in-flight provider attemptのpeak hard cap。serial baselineとrouting順reductionを保持 |
| `max_agent_depth` | v2 active schemaから削除 | Python providerはagentをspawnしない。legacy default 1はmigrationで無視、他値はunsupported |
| `max_runtime_seconds` | 強化実装 | finite positive。active execution/storage時間をmonotonic clockで累積。人間待ちは除外 |
| `max_external_cost_usd` | fixed-pointで実装 | external callだけに適用。decimal inputをinteger micro-USDへ正規化し、unknown、0、over-capはexternal call前拒否。free local executionは拒否しない |
| `max_output_bytes` | v2で4 fieldへ分割 | tool input、tool output、provider output、artifactのUTF-8 byte上限 |
| `max_run_bytes` | 強化実装 | run全体のatomic reservation。terminal artifact reserveを先に確保 |

Legacy `max_output_bytes`はv1 config migration時だけ4上限の共通値へ写す。v2 schemaはcoercive string、
NaN、Infinity、負値、field間矛盾を拒否する。`max_run_bytes`はrequired terminal reserve以上でなければならない。
v1の実効baselineはanalysis batch 1回、automatic retry 0、peak concurrency 1としてadapterへ固定し、
設定値の名前から未実装だったmulti-round/parallel behaviorを遡及的に有効化しない。

### Public / backward compatibility

- `BudgetConfig`の既存fieldを黙って別意味で有効化しない。v1入力は明示versioned adapterを通す。
- 現在の実効behaviorであるserial execution/no automatic retryを、v2 default 1/0で保持する。
- field削除・分割はdeprecation/migration errorとrelease noteを伴い、unknown fieldを無視しない。
- `Orchestrator.run/resume`とCLIの結果/exit codeはbudget failureのstructured code追加以外を保持する。

### Typed input/outputと対象module

- 対象: `config.py`, `state_machine.py`, provider/tool executors、RunStore reservation、execution record。
- `BudgetPolicyV2`: strict limits、clock/currency/unit/version。
- `BudgetSnapshot`: effective policy hash、reserved/used/released、attempt counters、active ns。
- `ExecutionPermit`: phase/attempt/resource reservation、deadline、cost/output reservation。
- `ExecutionSettlement`: actual usage、released reservation、status、cancel acknowledgment。

### Clock and accounting units

- enforcement clockはinjected `monotonic_ns()`。UTC wall timeはaudit表示だけ。
- active runtimeはphase compute、provider/tool attempt、retry backoff、serialization、artifact writeを含む。
- `approval_required`でprocessを返してからresumeするまでの人間待ちはactive runtimeに含めない。
- resumeはmanifestの元policy/usageを復元し、policy差替えを拒否する。tightenは新revisionと理由を
  auditし、既使用量未満へのtightenは即`BudgetExceeded`。
- costはinteger micro-USDでestimate → atomic reserve → actual settle。unknown estimateは許可しない。
- attempt count、retry count、peak concurrency、UTF-8 bytesを別単位で保存する。

### Retry contract

Retryable:

- 明示的`transient`分類のtransport/provider failure。
- side effectがない、またはremote側idempotency key/authoritative reconciliationで既存outcomeを
  検証できるoperation。local keyだけでexactly-onceを主張しない。

Non-retryable:

- schema/validation、security/policy rejection、approval required/denied
- unsupported/unavailable、budget/deadline/cancel
- deterministic local calculator failure、verification failure、non-convergence
- partial external action、unknown idempotency、corrupt artifact

`retries=0`は最大1 attempt、`retries=N`は最大N+1 attempt。各attemptはfresh context、attempt ID、
input hash、elapsed/cost/output settlementを持つ。

### Cancellation / timeout / process isolation

状態を`running → cancel_requested → cancelled | cancel_unconfirmed | failed`とする。

- deadline到達後に新attemptを開始しない。
- cooperative providerはack後にのみ`cancelled`。
- grace終了後もthread/processが生存する場合は`cancel_unconfirmed`でrunを成功にしない。
- external code、solver、non-cooperative/課金継続可能providerで停止保証を主張する場合はRM-028必須。
- RM-028はprocess tree cleanup、resource cap、job state reconciliationの証拠を返す。

### Concurrency

- scheduler semaphore、cost/output/run-byte reservationを同じpermit取得順でatomicに行う。
- actual peakはexecution recordへ保存し、上限超過をproperty testする。
- result reductionはrouting orderでstable。completion timingやdict iterationへ依存しない。
- cancelは全in-flight attemptへfan-outし、全ack/unknownを集約してからterminal判断する。
- artifact writerはrun revision lock下で容量をreserveし、scan-then-write raceを禁止する。

### Preconditions / postconditions / failure taxonomy

Precondition:

- strict finite policy、整合する単位、元runと一致するpolicy hash。
- reservation可能なremaining runtime/cost/bytes/concurrency slot。

Postcondition:

- permitは必ずsettle/releaseされ、leakはfailureとしてreconcileされる。
- actual usageはreservation以下、超過時はstructured failureかつ新規処理停止。

Failures:

- `InvalidBudgetPolicy`, `BudgetPolicyMismatch`, `ReservationDenied`
- `RuntimeExceeded`, `CostExceeded`, `OutputLimitExceeded`, `RunLimitExceeded`
- `RetryNotAllowed`, `RetryExhausted`, `ConcurrencyLimitExceeded`
- `CancelRequested`, `Cancelled`, `CancelUnconfirmed`, `IsolationRequired`

### Artifact / idempotency / migration / security

- policy、usage、reservation、settlementはRM-012 manifest/audit eventへ書く。
- 同じexecution idempotency keyとpayloadは既存settlementを返す。payload mismatchはconflict。
- v1 configはversioned adapterでv2へ変換し、暗黙coercionしない。
- external cost=0/unknown、unapproved external data、unisolated hard-stop要求をexternal call前に拒否する。
  local calculatorと`LocalProvider`のfree local処理にはexternal cost capを適用しない。

### Acceptance criteriaとtest

- **unit**: 全field境界、NaN/Infinity/string coercion拒否、単位変換、reservation/settlement。
- **property**: retries=Nで最大N+1、peak concurrency<=N、予約合計<=cap、stable reduction。
- **integration**: run/resumeで同じpolicy/usage、external cost=0でexternal provider call count 0。
  free `LocalProvider`は引き続きcall可能であることも検証する。
- **adversarial**: cost/output虚偽、cancel無視、reservation race、policy差替え。
- **fault injection**: permit取得後crash、settlement失敗、artifact cap直前の並行write。
- fake clockでphase/retry/backoff/serialization/writeのaccountingを検証する。
- cooperative/uncooperative cancelを区別し、返却後に継続するjobを成功扱いしない。

### Dependencies / human approval / safe commit units

Dependencies: canonical milestone DAGのP2-010A → P2-011A → P2-012A → P2-011Bを使う。
hard-stopはP2-028A、persistenceはP2-012A。RM-011完了gateはP2-011Bである。

Human approval:

- multi-round/parallel deliberationを1より大きく有効化する製品判断
- cost精度とprovider estimate policy
- legacy field deprecation/support window

Safe commit units:

1. strict v2 budget schemaとv1 migration/characterization tests。
2. clock、usage、reservation/settlement ledger。
3. retry classifierとserial attempt loop。
4. concurrency schedulerとstable reduction。
5. cancel state、RM-028 interface、run manifest integration。

---

## RM-012 — Versioned run manifest and failure atomicity

### 目的と非目標

**目的**は、completed/incomplete/corrupt/unsupported/resumable runを区別し、artifactのrun ID、version、
inventory、hash、revision、completionをfail closedに検証すること。

**非目標**:

- platform証拠なしにpower-loss durabilityやsecure eraseを保証しない。
- v1 runをin-place変更しない。
- individual file replacementだけをwhole-run atomicityと呼ばない。

### Public / backward compatibility

- `run`, `resume`, `show` commandと`Orchestrator.run/resume/load_report`入口を保持する。
- v1 artifactはread-only `legacy_unverified`として識別し、明示migrationなしにv2へ昇格しない。
- v2 writerはschema/versioned artifactを追加する。既存artifact名の変更はmanifest mappingとrelease noteを要する。
- corrupt/incomplete/unsupportedを従来のcompleted reportとして返さず、structured errorと既定exit code 2でfail closedにする。

### 対象moduleとtyped manifest

- `storage/run_store.py`, `schemas.py`, orchestrator persistence、resume/show reader。
- `RunManifestV2` required fields:
  - `run_schema_version`, framework/build/commit/tool contract versions
  - `run_id`, `revision`, status、created/updated UTC
  - canonical input/config/budget/redaction/policy hashes
  - state checkpointとevent head hash
  - payload artifact inventory: logical name、immutable revision-relative path、media/schema version、size、
    SHA-256、required flag
  - approval/context/execution lineage heads
  - previous manifest hash、writer transaction ID
- `CompletionMarkerV2`:
  - run ID、terminal revision、terminal manifest hash、required inventory root hash、published UTC
- hash domainはpayload artifactsだけをinventory Merkle/rootへ含める。manifest、marker、`current` pointer、
  lock、temp、quarantineは含めない。relative pathはPOSIX separator、Unicode NFC、bytewise UTF-8順、
  duplicate禁止とし、canonical JSONとhash algorithm/versionをmanifestに固定する。
- storage-level `RunReadStatus`はpublic `FinalReport.run_status`と別typeにする。verified terminal
  `succeeded`だけをpublic success、verified terminal `failed`を`failed_with_limitations`、verified
  `cancelled`をcancel exit、`cancel_unconfirmed`をfailureへ明示mappingする。terminal markerは成功ではなく
  terminal artifact setの完全commitを証明し、failed/cancelled revisionにも必要とする。

### Immutable revision / completion / current pointer protocol

1. run revision lockを取得し、expected previous revision/hashをCAS検証する。
2. `revisions/<revision>-<transaction-id>/`へ全new payload artifactをwrite、flush、size/hash/schema検証する。
   既存revision pathは上書きせず、過去manifestが参照するartifactをimmutableに保つ。
3. manifestを同じimmutable revisionへwriteし、payload inventoryを再読込してrun ID、size、hash、schema、
   terminal stateを検証する。
4. terminal revisionなら`completion.json`をそのrevision内の**最後のdata artifact**としてpublishする。
5. manifest/marker/payloadを再検証後、小さな`current` pointer（revision、manifest hash、marker hash）だけを
   expected previous pointerとのCASでatomic replaceする。CAS失敗時はnew revisionをorphan扱いにする。
6. readerは`current` pointerから選択したimmutable revisionのmarker、manifest hash、payload inventory root、
   全required artifact一致時だけterminal statusを返す。過去verified revisionも引き続き検証可能である。

whole-run failure atomicityの意味は「crashでpartial filesが残ってもcompletedとして可視化しない」であり、
すべてのfilesystemでdirectory全体のrename durabilityを保証する主張ではない。

### Lock / CAS / concurrency

- platform adapterでexclusive kernel run lockをauthorityとして使う。kernel primitiveがないadapterは、
  atomic recovery-claim/CASを必須とする。owner token、PIDだけでなくprocess start identity/boot ID、
  acquired/lease UTC、heartbeat、revisionを記録する。
- Windows/POSIXのsupported matrixごとにlockとflush behaviorをintegration testする。
- stale判定はpolicy version、lease、process identity/boot ID、heartbeatを使い、単なるmtimeやPIDだけで盗まない。
- lease判定の経過時間はmonotonic clock、audit時刻はUTC wall clockを使う。renew cadence、lock wait timeout、
  cancellationをpolicy化する。stale recoveryは単一winnerのrecovery claimを取得してからorphan transactionを
  quarantineし、last verified manifestから再開する。
- concurrent writerはexpected revision/hash mismatchで`RunConflict`。last-write-winsにしない。
- temp名はtransaction IDを含み、共有`artifact.tmp`を使わない。

### Crash / corrupt / partial run taxonomy

Reader status:

- `in_progress`, `approval_required`, `succeeded`, `failed`, `cancelled`, `cancel_unconfirmed`
- `incomplete`, `corrupt`, `unsupported_version`, `legacy_unverified`

具体条件:

- pre-publish transaction/orphanのmarkerなしterminal state、manifestなしmarker、missing required artifact
  → `incomplete`
- `current` pointerが参照する既公開revisionのrequired artifact消失 → `corrupt`
- invalid JSON/schema、run ID mismatch、cross-run swap、hash/size mismatch → `corrupt`
- future version → `unsupported_version`
- verified nonterminal checkpoint → `in_progress`または`approval_required`
- v1 markerのみ → `legacy_unverified`。v2 completedへ自動昇格しない。
- initial directoryだけを作成し最初のmanifest前にcrashしたrun IDは`incomplete`とし、同じIDを暗黙再利用
  しない。recovery claim後にquarantineするか、明示resume transactionで初期化する。

### Old-version migration

- v1はread-only adapterでschema-valid artifactとdirectory/run ID相関を確認する。
- migrationは新しいrun IDまたは明示migration destinationへcopyし、元runを変更しない。
- v1に存在しないintegrity evidenceを捏造せず、`legacy_source`とmissing guaranteesを記録する。
- migrationはcopy前後のsource inventory hash一致と明示的quiescence、またはlegacy sourceのexclusive
  quarantine/renameを要求し、旧CLI resumeとの混合snapshotを拒否する。
- migrationはtransactional、再実行可能、同じsource hashでidempotent。
- unsupported future versionはgeneric ValidationErrorで偶然読まず、structured errorにする。
- support対象versionと期間はhuman approvalを要する。

### Preconditions / postconditions

Precondition:

- valid run ID、supported version、confined path、expected revision/hash、lock ownership。
- artifact byte reservationとrequired terminal reserveがある。

Postcondition:

- current pointerが参照するimmutable manifestのpayload inventoryは実ファイルと一致する。
- failure時に前verified revisionを壊さず、new transactionはorphan/quarantineとして識別可能。
- completed readerはcompletion marker protocolを全検証している。

### Failure taxonomy / side effects / idempotency

- `RunNotFound`, `RunConflict`, `RunLocked`, `StaleLock`, `LockRecoveryFailed`
- `RunIncomplete`, `RunCorrupt`, `UnsupportedRunVersion`, `LegacyRunUnverified`
- `ArtifactMissing`, `ArtifactHashMismatch`, `ArtifactSchemaError`, `ArtifactBudgetExceeded`
- `TransactionWriteFailed`, `TransactionPublishFailed`, `DurabilityUnconfirmed`

Writerだけがrun artifactを変更する。同じtransaction ID/payload hashは既存結果を返し、異なるpayloadは
conflict。直接JSONL appendは禁止し、event segmentをimmutable fileとして追加してmanifestで参照する。

### Cancellation / timeout / security / lifecycle

- cancellation中もverified checkpointを壊さず、cancel statusをnew revisionへcommitする。
- lock待機とwrite/verificationはRM-011 runtimeに課金する。
- path traversal、symlink/reparse escape、cross-run swap、hash downgradeを拒否する。
- hashはredaction後に保存するcanonical artifact bytesへ適用し、redaction前dataは別分類/承認なしに保存しない。
- SHA-256 chainはaccidental corruptionと非整合の検出であり、filesystem書込権限を持つmalicious writerへの
  authenticity証明ではない。後者をthreat modelへ含める場合はprotected HMAC/signature key、OS ACL、
  またはexternal append-only anchorを別承認・実装する。
- retention、quarantine、orphan cleanupはRM-027 policyを正とする。

### Acceptance criteriaとtest

- **unit**: manifest/marker strict schema、canonical hash、inventory、version dispatch。
- **property**: dependency/event hash chain、revision monotonicity、idempotent migration。
- **integration**: create/run/show/resume、v1 read-only migration、別CWD/platform path。
- **adversarial**: cross-run swap、symlink/reparse、valid-JSON tamper、hash downgrade。
- **fault injection**: 全artifact write、manifest publish、marker publish、flush、lock acquisition/recovery境界。
- markerだけ、manifestだけ、terminal stateだけ、missing/corrupt/hash mismatchをcompletedにしない。
- 2 writer/resumeのlost update、共有temp race、run byte reservation raceを再現し防止する。

### Dependencies / human approval / safe commit units

Dependencies: P2-010A、P2-011A、policy/schemaだけのP2-027Aを経てP2-012Aを実装する。
P2-010B/P2-011B integration後にP2-012Bを完了gateとする。destructive cleanupのP2-027Bと
approval lifecycleのP2-013Bを前提にしない。

Human approval:

- v1 support/migration範囲、support window、in-place禁止方針
- supported platform別durability target
- lock wait/lease/stale policy、quarantine/retention/encryption

Safe commit units:

1. v2 manifest/marker/error schemaとreader（writer behaviorは未変更）。
2. immutable revision writer、payload inventory/hash、current pointer revision CAS。
3. revision-local completion marker last protocolとshow/load/public status mapping verification。
4. cross-process lock/recoveryとconcurrency tests。
5. v1 read-only migrationとRM-027 lifecycle hooks。

---

## RM-013 — Approval and resume hardening

### 目的と非目標

**目的**は、approval actor/authority、request revision、decision idempotency、concurrent conflict、
resume outcome、将来external actionのbindingを監査可能にすること。

**非目標**:

- 現在存在しないexternal action executorを実装しない。
- CLIの自己申告actorを認証済みauthorityとみなさない。
- approvalを能力、正しさ、GTO/均衡の証明にしない。

### Public / backward compatibility

- `resume --approve/--reject/--reason`のlocal safe pathを保持し、actor/expected revision/idempotencyは
  v2 optionまたはversioned inputとして追加する。
- actor未指定のlegacy CLIは自己申告local actorとしてauditできるが、将来external actionをapproveできない。
- 既存pending/reject behaviorとexit codeをcharacterization testで保持し、unknown/duplicate/conflictは
  tracebackではなくstructured errorとexit code 2へ正規化する。
- v1 ledgerはread-only validation/migrationを通し、重複や不正stateを暗黙修復しない。

### 対象moduleとtyped I/O

- `approvals.py`, `orchestrator.resume`, `cli.py`, approval schema、RM-012 transaction/audit event。
- `ApprovalActor`: actor ID/type、authority source/scope、verification status、session/credential reference。
- `ApprovalRequestV2`: request ID/revision、action category、canonical action plan/digest、data、cost/resource
  limits、expiry、required authority、created revision。
- `ApprovalDecisionBatch`: run ID、expected run/ledger revision、actor、decision ID/idempotency key、
  approve/reject item、reason、decision time。
- `ApprovalDecisionOutcome`: committed revision、per-item old/new status、audit event hash、resume result ref。

### Approval actor / decision authority

- rejectは安全側操作のため、local interactive actorの自己申告をauditし許可できる。
- external actionのapproveにはrequired authority scopeをverified sourceから確認する。
- unverified CLI actorはexternal actionをapproveできず`UnauthorizedDecision`。
- actor、authority、verification sourceをdecision後に変更しない。
- approval expiry/revocationはdecision前とexternal action直前の両方で検査する。

### Duplicate / stale / concurrent / approve+reject

- batchは**all-or-nothing**。全IDとrevision、authority、conflictを変更前に検証する。
- unknown ID、同一batch内duplicate、approveとreject両方、既決ID、expired requestをstructured error。
- 検査順を固定する: idempotency key lookup → 保存済みactor/payload hash照合 → same payloadなら既存outcome返却
  → unknown keyだけexpected run/ledger revisionと現在値を比較する。unknown keyのrevision不一致は
  `StaleDecision`でdomain write 0とする。
- 2 processのconcurrent decisionはRM-012 lock/CASで一方だけcommitする。
- 同じrequestへのapprove/reject競合をlast-write-winsにしない。
- malformed/duplicate ledgerをdict構築で上書きせず、reader段階で`ApprovalLedgerCorrupt`。

### Resume idempotency

- idempotency keyはactor、run/request revision、decision payload hashへbindingする。
- same key + same payloadは以前の`ApprovalDecisionOutcome`を返し、artifactを再writeしない。
- same key + different payloadは`IdempotencyConflict`。
- terminal runへの再送もpayloadを検証し、反対decisionを無言で無視しない。
- resumeはdecision commitとreport/state updateを同じRM-012 transaction lineageへ置く。
- request生成にもrun/phase/action digest由来のidempotency keyを使う。attempt IDはlineage fieldとして
  別記しkeyへ含めないため、fresh retryでも同じactionは同じpending requestを返す。payload差替えは
  conflict、新digestによる置換は旧requestを`superseded`へcommitしてから行う。

### Approval and future external action binding

canonical action planに最低限次を含めSHA-256 digestへbindingする。

- exact command/tool/operation、executable/provider/version/hash
- 送信data fieldとcontent hash、destination、retention/trace policy
- cost/resource/time/output上限、working directory、environment allowlist
- expected result type、idempotency/execution ID、expiry

いずれかが変われば既存approvalはstaleとなり、新requestが必要。future executorはapproveされたdigestだけを
実行候補とし、execution receipt/result hashを同じlineageへ記録する。crash後の外部副作用をlocal receiptだけで
exactly-onceとは扱わない。remote idempotency keyまたはauthoritative reconciliationが検証できる場合だけ再実行し、
それ以外は`effect_unknown / manual_reconciliation_required`で停止する。RM-028は
`launch_committed/running/effect_unknown/reconciled`とisolated job IDを同じdigestへbindingする。

### Preconditions / postconditions

Precondition:

- valid manifest/ledger、pending request、expected revision、non-expired action digest。
- actor authorityがaction category/scopeを満たす。
- batch内全decisionがuniqueかつ矛盾しない。

Postcondition:

- 全itemまたは0 itemが変更される。
- new ledger/report/state/audit eventは同じrevisionとdecision hashを参照する。
- pendingが残れば`approval_required`、全rejectはsafe no-action path、approveはexecutor不在なら
  `failed_with_limitations`を正直に返す。

### Failure taxonomy

- `ApprovalUnknown`, `ApprovalDuplicate`, `ApprovalAlreadyDecided`, `ApprovalExpired`
- `ApprovalLedgerCorrupt`, `UnauthorizedDecision`, `StaleDecision`
- `ApproveRejectConflict`, `IdempotencyConflict`, `ActionDigestMismatch`
- `ResumeConflict`, `ResumeTransactionFailed`, `ExternalExecutorUnavailable`

### Artifact / cancellation / concurrency / migration / security

- decision成功/拒否はdomain revisionへcommitする。conflict、unauthorized、validation失敗はledger/state/reportの
  domain mutation 0とし、別のbounded/redacted security audit ledgerへ`audit_sequence` CASで記録する。
  audit記録失敗時は`audit_unconfirmed`を返し、run/decision revisionを進めない。容量上限とrate limitを必須とする。
- reason、actor、old/new state、revision/hash、decision/execution IDを記録する。
- resume cancelはdecision commit前ならwrite 0、commit後ならnew checkpointから安全に再開する。
- resume timeoutはRM-011のactive monotonic budgetへ課金し、decision commit境界の前後をauditする。
- concurrent accessはRM-012 lock/CAS、retention/expiryはRM-027を使う。
- v1 ledgerはread-only validation後に新v2 copyへmigrationし、duplicate/invalid stateを自動修復しない。
  actor/authority/action digestを欠くv1 pending approvalは`historical_only/stale`とし、approvableなV2 requestへ
  昇格させない。継続するactionは新しいV2 requestを再発行する。
- arbitrary inputからstatus/decision/timestamp/authorityを注入できないことを維持する。

### Acceptance criteriaとtest

- **unit**: actor/authority、state/timestamp validator、action digest、idempotency。
- **property**: approval state matrix、batch permutation、same/different payload idempotency。
- **integration**: CLI exit code、pending/approve/reject/resume、v1 migration。
- **adversarial**: actor spoof、stale revision、disk tamper、approve+reject、action plan差替え。
- **fault injection**: decision event、ledger、report、state、marker各write境界。
- concurrent resumeでlost updateやmixed winner artifactが生じない。
- duplicate ledger、unknown ID、partial batch、terminal反対decisionをstructured failureにする。

### Dependencies / human approval / safe commit units

Dependencies: P2-012B後にP2-013A（actor/authority/digest/idempotency/CAS）を実装し、P2-013Aの
destructive authorityを使うP2-027B後にP2-013B（resume/lifecycle）を完了gateとする。
external executionではP2-011B、P2-024A、P2-028Aが必要。

Human approval:

- actor identity/authority provider、approval expiry/revocation
- authority scope taxonomy、audit retention
- 将来external executorを追加するか

Safe commit units:

1. v2 actor/request/decision/error schemaとv1 characterization tests。
2. strict ledger reader、all-or-nothing validation、action digest。
3. RM-012 revision CASによるdecision transaction。
4. resume idempotencyとCLI structured errors。
5. external execution binding interface（executor本体なし）。

---

## Supporting Phase 2 milestone security contracts

### RM-027A / RM-027B split

- P2-027Aはclassification、retention、expiry、quarantine、disposition schema/policyだけをfreezeし、
  filesystemを変更するexecutorを含まない。P2-012Aより前に提供できる。
- P2-027BはP2-012BとP2-013A後に実装する。active/pending判定とdelete間の競合をrun lock/current
  pointer CASで防ぎ、verified actor/authority、承認済みdry-run plan/action digest、receipt/tombstone、
  partial failure reconciliationを必須とする。secure eraseはplatform evidenceなしに主張しない。

### RM-028 isolation boundary

- network egressはdefault denyとし、承認action digestに含むdestination allowlistだけを許可する。
- filesystem read/write allowlistを分け、symlink/reparse/child process escapeを拒否する。
- dedicated最小権限OS identity、stdin/継承handle遮断、secret valueでなくapproved secret reference、
  executableだけでなくinterpreter/dynamic library/container imageのtransitive identityを記録する。
- durable job stateは`launch_committed`, `running`, `cancel_requested`, `remote_cancel_unconfirmed`,
  `effect_unknown`, `reconciled`, `completed`, `failed`を区別する。local process killはremote課金・処理の
  停止証拠ではない。provider idempotency/reconciliationがなければeffect unknownを自動retryしない。

---

## Cross-RM implementation order

1. P2-024A: ContextEnvelope policy/schema/ownership/lineage/allowlist。
2. P2-010A: typed pure phaseをserial・persistence非依存で分割。
3. P2-011A: strict budget/fake clock/serial accounting/retry classification。
4. P2-027A: retention/quarantine/disposition policy/schemaのみ。
5. P2-012A: immutable revision、manifest、transaction、lock/recovery claim、revision CAS。
6. P2-010B/P2-011B: durable transition integration、usage/resume、reservation/concurrency/cancel。
7. P2-012B: terminal marker、verified reader/status mapping、migration/lifecycle hooks。
8. P2-013A: approval actor/authority/action digest、request/decision idempotency、CAS。
9. P2-027B: authorized cleanup executor、dry-run digest、receipt/tombstone/reconciliation。
10. P2-013B: resume/legacy reissue/expiry/revocation/lifecycle integration。
11. P2-028A: isolation、durable external-effect state、cancel/reconciliation。
12. Phase 2全体のfault-injection、backward compatibility、security再監査。

各stepは独立commitとし、4品質ゲート、targeted contract/fault test、独立レビューがgreenになるまで
次へ進めない。

split RMの親をcompletedにする場合、親RMの`completion_evidence`はordered
`commits`/`paths`/`tests`を含めcompletion milestoneのevidenceと完全一致させる。
completion milestone側は、そのmilestoneに明示承認されたexact implementation scopeのproposal順、
tracked repository path、cited commit tree、changed path、append-only historyで検証する。親RMの抽象的な
targets/tests labelへ実在pathをprefix-bindして代用しない。entry milestoneとcompletion milestoneが同一の
RM、およびsplitを持たないcompleted RMの既存binding ruleは変更しない。

## P2-012A completion boundary

P2-012A の完了条件は、専用 root、strict nonterminal schema/canonical bytes、typed provenance、
process/kernel lock、immutable revision、serialized CAS、structural reader、orphan inspection と
metadata-only claim、quota/fault/concurrency evidence までである。`RM-012` 全体は
`in_progress` のまま、`P2-012B` の completion marker、terminal reader/status、product
integration、migration/lifecycle hook は `not_started` のままにする。

capability は `immutable_revision_storage_foundation` だけを implemented にできる。
`product_integrated_durable_run` は planned を維持し、P2-010B/P2-011B/P2-012B の readiness
や approval を暗黙に昇格させない。
