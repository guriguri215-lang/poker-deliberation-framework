# Local data policy

## 文書状態

- **FACT**: P2-027Aのexact policy/schema scopeは人間承認済みである。
- **FACT**: 現段階はscope freezeであり、pure policy評価はまだ実装していない。
- **FACT**: P2-027Aはfilesystemの探索、読込、書込、移動、rename、quarantine、削除、
  secure erase、receipt、tombstone、reconciliationを実行しない。
- **FACT**: cleanup executorはP2-012BとP2-013Aに依存するP2-027Bで別承認を要する。

## 承認済み境界

分類は既存の`public`、`internal`、`sensitive`、`restricted`を再利用する。既定は
`internal`、credential検出は`restricted`へ昇格し、derived artifactはsourceの最大分類を
継承する。redactionは分類downgradeの根拠にしない。

storage retentionはprovider handoffの`ContextPolicy.expires_at`と分離する。承認済みの
run retentionはpublic 365日、internal 90日、sensitive 30日、restricted 0日・永続化禁止である。
1日はUTCの固定24時間とし、cacheは最大7日、tempは最大1日、bounded lifecycle audit metadata
とfuture disposition receiptは365日、quarantine review windowは30日とする。

`now >= retention_expires_at`は候補eligibilityだけを意味し、自動actionや承認を意味しない。
active、approval pending、legal hold、ownership/integrity/lineage未検証はdestructive eligibilityを
blockする。current v1 artifactsは`legacy_unverified`であり、削除候補にしない。

at-rest encryptionはP2-027Aで実装しない。public/internalには暗号化を要求せず安全性も主張しない。
sensitiveは永続化前の暗号化を必須とし、能力がなければ拒否する。restrictedは永続化を禁止する。

実装するdisposition vocabularyは`deny_persistence`、`retain`、`protected`、`manual_review`、
`quarantine_candidate`、`delete_candidate`である。candidateはpure valueであり、filesystem
actionではない。

承認scope digestは
`c5636cff29547bf40ce800e63776a7de77b234ee3acb68b17b4647f5d5b5e96d`である。
