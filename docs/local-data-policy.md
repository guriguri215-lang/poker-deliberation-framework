# Local data policy

## 実装状態

- **FACT**: P2-027A は strict・frozen・extra-forbid の policy/schema と pure evaluator を実装する。
- **FACT**: policy schema は `1.0.0`、canonicalization は
  `poker-local-data-policy-json-v1`、hash は unkeyed SHA-256 である。
- **FACT**: `local_data_lifecycle_policy` は implemented である。
- **FACT**: `local_data_cleanup_executor` は unavailable である。
- **FACT**: filesystem の探索・読込・書込・scan・move・rename・quarantine・delete、
  cleanup CLI、secure erase、暗号化、receipt、tombstone、reconciliation は実装しない。

## 分類と所有権

分類は既存の `public`、既定の `internal`、`sensitive`、`restricted` を再利用する。
既知の current-run artifact も最低 `internal` から始まり、content-bearing source の最大分類へ
単調に昇格する。`public` は信頼済み明示分類と restricted-secret 非検出を必要とする。
credential 検出は常に `restricted` へ昇格し、redaction は downgrade authority にならない。

typed application ownership と logical subject kind が必要である。Git ignore、glob、名前、mtime は
所有権の根拠ではない。`user_materials/`、goal の `tmp/`、review/test output、unowned pytest
session、tracked source/docs、未知 logical name は対象外で、未知 artifact は fail closed になる。
current v1 artifact は `legacy_unverified` として protected/manual review であり、削除候補にならない。

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
P2-027B は P2-012B と P2-013A の後に別承認を要する。
