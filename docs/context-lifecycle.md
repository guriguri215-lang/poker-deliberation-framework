# Context lifecycle contract

## P2-027A との分離

P2-027A の local-data policy は `ContextPolicy` / `ContextEnvelope` 1.0.0 を変更しない。
`ContextPolicy.expires_at` は provider handoff の use-expiry、`attempt-memory-only-v1` は現在の
非永続 attempt 契約である。local-data policy の `retention_started_at` と
`retention_expires_at` は future storage lifecycle 用の別 field であり、use-expiry を retention
anchor に流用しない。P2-027A evaluator は pure value だけを返し、context、RunStore、
orchestrator、provider、CLI に接続しない。

## Scope

P2-024Aは、Python local orchestratorから既存`AgentProvider.analyze`へ渡す1回の試行単位の
context boundaryを定義する。`AgentContext`とproviderの3引数signatureは変更しない。
contextの永続化、保存期間、削除、quarantine、tombstone、secure erase、cleanup executor、
自動retry、Codex/Python bridge、external providerはこの契約に含まれない。

## Versioned models

`ContextPolicy`と`ContextEnvelope`はPydanticのstrict・extra-forbid・frozen modelである。
現在認識する値は次に限定する。

- envelope/policy schema: `1.0.0`
- canonicalization: `poker-context-json-v1`
- hash algorithm: `sha256`
- producer/consumer runtime: `python-local`
- retention-policy ID: `attempt-memory-only-v1`

未知のfield、schema、canonicalization、hash algorithm、runtime、retention-policy IDはfail closedと
する。`attempt-memory-only-v1`はP2-024Aが新しいcontext artifactや保管処理を作らないことを表す
識別子であり、保存期間、memory消去時刻、secure deletionを保証しない。

## Policy and classification

classificationは`public`、既定の`internal`、`sensitive`、`restricted`の4種類である。
credentialらしいstructured keyまたは既知token patternがpayload内で検出された場合、呼出側の
指定にかかわらず`restricted`になる。`restricted`はproviderへ渡さない。redactionはartifactの
defense in depthであり、context handoffを許可する根拠には使用しない。

policyはtimezone-aware UTCの`expires_at`を必須とする。境界使用時刻`now`は注入されたclockから
得て、`now >= expires_at`なら拒否する。通常orchestratorでは既存のprovider attempt timeoutを
use-expiryとして明示する。これはstorage retention期間ではない。

`allowed_fields`はcanonical sorted orderの重複しないtop-level identifierだけを許可する。
dotted pathは許可しない。payloadの実field集合、policy allowlist、`AgentAssignment.context_keys`は
完全一致しなければならない。

## Immutable envelope and lineage

payloadは意味のある`AgentContext` top-level fieldだけをcanonical JSON文字列にし、nested mutable
objectをenvelopeへ保持しない。providerへ渡すたびにcanonical JSONから新しい`AgentContext`を生成
する。このため、元contextまたはprovider側copyのnested mutationはenvelopeへ逆流しない。

各試行は新しい`context_id`と`attempt_id`を持ち、次をhashへbindingする。

- `run_id`、`assignment_id`、assignment全体のhash
- `context_id`、`attempt_id`、任意の`parent_context_id`
- root payloadを示す`source_sha256`
- producer/consumer runtime
- policy、canonical payload、その各SHA-256
- schema/canonicalization/hash version、作成UTC時刻

初回試行では`source_sha256 == payload_sha256`を要求する。retry envelopeを明示的に構築する場合は、
新しいcontext/attempt ID、直前のparent context ID、root source hashを要求する。P2-024Aはretryを
実行せず、系譜を構築・検証するpure APIだけを提供する。

canonical JSONはUTF-8、key sort、空白なし、non-ASCII保持、non-finite number拒否で固定する。
SHA-256は偶発的な破損、stale hash、相関違いを検出するintegrity mechanismである。秘密鍵や外部
trust anchorを使わないため、同じprocess/storageへ書込み可能な悪意ある主体に対する真正性証明では
ない。durable trust anchorはP2-024Aの範囲外である。

## Provider boundary

実経路はprovider呼出し前に、strict schema、UTC、expiry、run/assignment/attempt/parent/source、
runtime、policy/payload/envelope hash、canonical payload、exact allowlist、restricted data、provider
availabilityを検証する。全条件が通った場合だけfresh `AgentContext`とfresh assignment copyを既存の
`analyze(context, assignment, control)`へ渡す。

provider reportの`agent_role`と`task`も元assignmentへ完全一致させる。不一致report、期限切れ、
改ざん、別run/assignment/context/attemptへのreplay、未知runtime/schema、restricted context、
unavailable providerは採用しない。同じ有効なenvelopeからfresh copyを複数回materializeすること自体を
single-use保証とはしていない。

## Artifacts and compatibility

raw `ContextEnvelope`、canonical payload、policyの新規artifactは保存しない。既存
`agent_execution_records.json`の`context_sha256`は従来どおりdefaults/Noneを含む完全な
`AgentContext.model_dump(mode="json")`のcanonical SHA-256を意味し、同一contextの値互換性を維持する。
新しい任意fieldとしてcontext/attempt/parent ID、schema、classification、sparse envelopeの
payload/source hash、policy/envelope hash、expiry、producer/consumer runtimeを記録する。任意fieldなので
旧artifactのvalidation、`show`、`resume`との互換性を維持する。

`LocalProvider`、CLI exit code、既存run artifact名、calculation caseのprovider非実行、blind decision
isolationは維持する。外部送信は追加しない。
