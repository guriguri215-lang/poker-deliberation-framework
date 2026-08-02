# Runtime conformance contract

## 目的と状態

P2-025Aは、Codexネイティブ実行面とPythonオーケストレーター実行面の意味を、実行bridgeなしで
比較するstrict versioned contractである。`runtime_conformance_contract`は**implemented**だが、
`codex_python_runtime_bridge`は**unavailable**のままである。PythonはCodex roleを起動せず、
Codex実行をPython `AgentExecutionRecord`やrun artifactとして捏造しない。
P3-030Cは既存Python `LocalProvider`、assignment、P2-024A context、tool allowlistを利用するだけで、
この状態を変更せず、実Codex/Python bridgeや並列Codex product runtimeを実装したとは扱わない。

## Schemaとcanonicalization

- schema version: `1.0.0`
- canonicalization: `poker-runtime-conformance-json-v1`
- hash algorithm: `sha256`
- record domain: `poker-runtime-conformance-record-v1`
- inventory domain: `poker-runtime-conformance-inventory-v1`
- JSON: strict UTF-8、BOMなし、末尾改行なし、重複keyなし、NFC、key順、compact encoding
- time: timezone-aware UTCを`Z`で表現

このfamilyは`poker-run-storage-json-v1`、P2-012B terminal schema、既存artifact schemaとは別である。
conformance valueは既存artifactへ保存されず、既存readerやorchestratorの意味を変更しない。

## Role inventory

`.codex/agents/*.toml`は、追跡済みの7 fieldが完全一致する場合だけdataとしてparseする。9 roleを
inventory化し、instruction本文を実行しない。Python側は`ROLE_CATALOG`の7 analysis roleと、
catalog外のruntime owner `python-orchestrator`を別にinventory化する。

7 analysis roleは`semantic-peer`として明示対応する。両orchestratorは同じ処理の実行記録ではなく、
別surfaceのownerなので`runtime-specific`である。`calculator-builder`はrepository開発roleであり、
Python analysis roleを発明せず`intentionally-unmapped`とする。

Codex native tool catalogは追跡role TOMLに宣言されていないため`null`であり、検証時も
`undeclared`として扱う。Pythonのtool/capability catalogは、呼出時の機械的snapshotに限る。

## Assignment、context、approval

`AssignmentV1`はruntime/role/semantic role、objectiveとdomain hash、parent、role inventory hashを
束縛する。`ContextReferenceV1`はclassification、payload/policy/envelope hash、作成・期限、
producer/consumer/parent provenance、budget policy hashと既知limitを保持する。context envelopeは
期限とenvelope hashが必須で、verified product/fixtureがそれらを発明することは禁止される。

`ToolCapabilityAllowlistV1`の意味は`exact`だけであり、target runtimeでのtool/capability追加を
許可しない。`ApprovalBindingV1`はrequired/not-requiredのclosed matrixでrequest、action digest、
decision、decision time、expiry、authority snapshotを束縛する。外部作用は有効なapproved binding
なしで実行済みにできない。

## Resultとexecution audit

`ResultV1`はterminal status、epistemic label、strategy claim、hash-bound tool/evidence、
unverified provider reference、qualified solver evidence、bounded limitationを分離する。

- `CALCULATED`には成功したtool resultが必要
- `FACT`にはverified evidenceが必要
- provider conclusionは`unverified`のまま
- `gto`、`equilibrium`、`exact-range`にはqualified solver evidenceが必要
- timeout/cancel/failureには対応する`StructuredErrorV1`が必要

実行済みrecordは`ExecutionAuditV1`を必須とし、context/allowlist/approval hash、terminal status、
agent/tool lineage、UTC timing、再現version、既知または明示UNKNOWNのsource commitを保持する。

## 検証API

`validate_record`は1 runtime内のinventory、role、catalog、expiry、approval、audit hash、lineage、
terminal状態をpureに検査する。`compare_records`はsource/target双方を検査したうえで、明示role mapping、
objective、parent、context分類・来歴・policy・budget、exact allowlist、approval action binding、
structured result/evidence/error意味の変更を検出する。検査時刻はtimezone-aware UTCを明示注入する。

`project_python_product_run`は、`VerifiedRunReadV2`が`succeeded`でcompletion markerを持ち、
渡された`FinalReport`と各`ToolResult`がverified payload bytesと一致する場合だけprojectionする。
承認付きrun、外部provider、失敗・非terminal run、欠損payload、tool catalog不一致を拒否する。
product artifactを変更せず、provider/solver/networkを呼び出さない。

## Fixtureと非目標

`tests/fixtures/runtime_conformance/v1/scenarios.json`は、正常pair、unknown role/capability、
allowlist拡張、classification/provenance変更、approval digest変更、audit hash変更を列挙する。
unit/property/adversarial/integration testはcanonical round-trip、version/duplicate/unknown-field拒否、
context/approval expiry、structured timeout/cancel、実行時secret canary、unsupported solver limitation、
既存offline productのprojectionを検証する。

非目標は、Codex/Python bridge、Codex role起動、外部provider/solver、GTO/均衡/range生成、
既存artifact migration、P2-025B以降の双方向transport、RM-028実行である。
