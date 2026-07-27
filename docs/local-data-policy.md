# Local data policy

## 実装状態

- **FACT**: P2-027A は strict・frozen・extra-forbid の policy/schema と pure evaluator を実装する。
- **FACT**: policy schema は `1.0.0`、canonicalization は
  `poker-local-data-policy-json-v1`、hash は unkeyed SHA-256 である。
- **FACT**: `local_data_lifecycle_policy` は implemented である。
- **FACT**: P2-027B は P2-027A の policy を変更せず、別承認された additive Python API として
  bounded scan、quarantine、staged delete、receipt、tombstone、read-only reconciliation を実装する。
- **FACT**: cleanup CLI、secure erase、暗号化、automatic retry は実装しない。

## 分類と所有権

分類は既存の `public`、既定の `internal`、`sensitive`、`restricted` を再利用する。
既知の current-run artifact も最低 `internal` から始まり、content-bearing source の最大分類へ
単調に昇格する。`public` は信頼済み明示分類と restricted-secret 非検出を必要とする。
credential 検出は常に `restricted` へ昇格し、redaction は downgrade authority にならない。

typed application ownership と logical subject kind が必要である。Git ignore、glob、名前、mtime は
所有権の根拠ではない。`user_materials/`、goal の `tmp/`、review/test output、unowned pytest
session、tracked source/docs、未知 logical name は対象外で、未知 artifact は fail closed になる。
current v1 artifact は `legacy_unverified` として protected/manual review であり、削除候補にならない。
分類結果は canonical な content-source classification vector、trusted explicit source flag、
restricted-secret check の完了状態、credential 検出状態へ束縛する。所有権は boolean ではなく
typed provenance で表し、除外領域の provenance は常に protected になる。

## Retention と expiry

1 retention day は UTC の固定24時間である。承認済み matrix は次のとおり。

| classification | run retention | persistence policy |
|---|---:|---|
| `public` | 365日 | 暗号化 claim をしない |
| `internal` | 90日 | 暗号化 claim をしない |
| `sensitive` | 30日 | persistence 前に encryption capability 必須 |
| `restricted` | 0日 | persistence forbidden |

application cache は profile と7日の小さい方、application temp は profile と1日の小さい方を使う。
lifecycle audit metadata と future disposition receipt は365日、quarantine review window は30日である。
run は future verified terminal publication、cache/temp は typed application creation、audit/receipt は
decision commit、quarantine は quarantine entry を retention anchor とする。filesystem mtime は使わない。
capability availability と subject の encryption state は別の strict enum である。sensitive は
capability `available` と `encrypted_verified` の両方を必要とし、`requirement_mismatch` は
quarantine candidate になる。両方を audit へ記録し、P2-027A 自身は暗号化を実行しない。

`ContextPolicy.expires_at` は provider use-expiry のままであり、storage の
`retention_started_at` / `retention_expires_at` と分離する。`now >= retention_expires_at` は
候補 eligibility にすぎず、action・承認・削除を実行しない。

## Disposition、保護、quarantine

pure evaluator は `deny_persistence`、`retain`、`protected`、`manual_review`、
`quarantine_candidate`、`delete_candidate` の typed value だけを返す。
active、approval pending、legal hold、ownership/integrity/lineage 未検証は destructive eligibility を
blockする。incomplete、corrupt、orphan transaction、および明示された integrity/lineage mismatch、
path-confinement failure、encryption mismatch は保護条件がなければ quarantine candidate になる。
delete candidate は verified terminal または already quarantined で retention boundary 以後の場合だけ
提案できる。P2-027A は filesystem/domain mutation を常にゼロに保つ。
現在の evaluator は manual-review ケースを `protected` +
`manual_review_required=true` で表し、`manual_review` は schema vocabulary の予約値として保持する。

## Failure と audit

未知 schema/policy/classification/artifact、invalid UTC/time、clock rollback、policy hash mismatch、
downgrade、ownership/integrity/lineage failure、encryption/persistence denial は bounded typed
non-retryable failure になる。failure は action を返さない。

audit metadata は policy identity/hash、subject identity/kind/hash、classification source、
retention timestamps、evaluation time、state、proposed disposition、reason、evaluator version を
bindする。raw content と secret は含めず、ledger/receipt/tombstone を永続化しない。
unkeyed SHA-256 は corruption/correlation mismatch の検出用であり、writer authenticity を証明しない。

承認 scope digest は
`c5636cff29547bf40ce800e63776a7de77b234ee3acb68b17b4647f5d5b5e96d` である。
P2-027B は P2-012B と P2-013A の後に別承認され、実行ごとにも exact destructive approval を要する。
詳細は [`local-data-cleanup.md`](local-data-cleanup.md) を参照する。

## P2-012A storage admission

P2-012A は policy を変更せず、各 `RevisionArtifactV1` について complete
`ArtifactClassification`、classification source/evidence、固定 policy digest を pure preflight
と stored reread の両方で再評価する。persist できるのは trusted explicit または source
inheritance により `public` / `internal` と判定され、restricted-secret check が完了して clean な
artifact だけである。attempt context、sensitive/restricted/unknown、default-internal、
policy mismatch、downgrade は拒否する。

storage は retention/disposition を実行せず、P2-027B の scan、quarantine、delete、receipt、
tombstone、secure erase を先取りしない。

## P2-011B budget-state classification

専用producer `p2-011b-durable-budget`の`budget_state.json`だけは、logical subject
`RUN_AUDIT`、classification `internal`、source `default_internal`としてstrict admissionする。
artifactはschema `poker-durable-budget-state-artifact-v1`で、typed ID、hash、unit、status、
provenanceだけを含む。raw `ContextEnvelope`、provider input、tool context、credential、exception、
traceback、secret-shaped valueは拒否する。

この限定例外は他のP2-012A artifactへdefault-internal admissionを拡張しない。budget rootは
product run rootと別であり、P2-027Aのretention evaluatorやP2-027Bのcleanup authorityを実行しない。

## P2-012B terminal lifecycle hook

product terminal revisionは全admitted payloadのclassification/evidenceを再検証し、
`lifecycle_audit.json`をrequired payloadとしてcompletion markerより前に保存する。
P3-014A の documented key-value hand inputでは、typed `normalization.json` も固定
run payloadとして `internal` 以上に分類される。source本文は同artifactへ保存せず、
exact source byte length/SHA-256、parser identity/version、sanitized diagnosticと、
成功時のcanonical normalized-hand length/SHA-256だけを保存する。structured JSON入力と
provenanceを持たないlegacy copyでは、このartifactを新規作成しない。
`retention_started_at`はexact `CompletionMarkerV2.published_at`、verification basisは
`verified_terminal`である。provider contextのuse-expiryやfilesystem mtimeをretention anchorへ
流用しない。

incomplete、corrupt、orphan、path/lineage/encryption mismatchはpure
`quarantine_candidate`/manual-review evaluationへ写像できるが、P2-012Bはscan、move、delete、
quarantine、encryption、receipt、tombstone、secure eraseを行わない。flat-v1とcopy migrationは
`legacy_unverified`かつprotected/manual reviewのままである。
