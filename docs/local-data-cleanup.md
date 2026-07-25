# Authorized local-data cleanup executor

## 実装範囲

- **FACT**: P2-027B は、明示された1つの verified product run、またはその run から
  P2-027B 自身が作成した1つの quarantine payload だけを扱うローカル Python API である。
- **FACT**: 対象探索に glob、ignore、名前推測、mtime を使わない。product root と cleanup root の
  ownership marker、root identity、terminal current、manifest、inventory、lifecycle audit、
  pending approval、failure-audit 保持期限、legal hold を strict read する。
- **FACT**: CLI、外部 provider、network、solver、process sandbox、parallel execution、
  automatic retry は追加しない。
- **FACT**: テストで mutation を行う対象は、各テストが作成した disposable temp root だけである。

主要 API は `poker_deliberation.local_data_cleanup.LocalDataCleanupExecutor` にある。

- `initialize_cleanup_root(...)`
- `dry_run_quarantine(...)`
- `execute(...)`
- `execute_quarantine(...)`
- `dry_run_delete(...)`
- `execute_delete(...)`
- `inspect_reconciliation(...)`

構築と inspect は root を暗黙作成しない。cleanup root の初期化だけが control-only mutation を
許可される。repository/workspace 配下、home root、`.git`、`user_materials`、`tmp/goals`、
product/legacy root と重なる場所は cleanup root にできない。
cleanup root の marker は、各 dry-run/execute/read で actual product authority の root identity と
ownership marker に再照合する。別 repository の配下、path component に symlink/junction/reparse
を含む root、unknown root entry がある root も fail closed とする。
executor 経由の root inspection と reconciliation inspection も同じ live product-root binding を
必須とし、marker が別 product root に属する場合は initialized/committed と報告しない。

## 2段階の状態遷移

product run を直接削除する API はない。

1. `dry_run_quarantine` は explicit run ID を read-only 評価し、exactly one action の
   `CleanupPlanV1` と domain-separated plan SHA-256 を返す。
2. P2-013A の別 run で、その plan に対応する `CanonicalActionPlanV2` を
   `approve:destructive_change` scope で承認する。
3. `execute_quarantine` は product run の current/tree と live authority を lock 内で再検証し、
   same-volume の単一 `os.replace` で cleanup root の `quarantine/<run_id>` へ移す。
4. quarantine entry から固定30日が経過した後、`dry_run_delete` で新しい plan を作り、
   新しい P2-013A destructive approval を得る。
5. `execute_delete` は quarantine payload を transaction-specific `deleting/` staging へ
   renameし、`delete_prepared` current を公開してから、linkを追わず bottom-up unlinkする。
   完了後だけ `deleted` current を公開する。

許可される cleanup current の遷移は
`quarantined -> delete_prepared -> deleted` だけである。各 revision は transaction、plan、
approval binding、receipt、tombstone、前 manifest hash、期待 pointer hash を immutable に持つ。
reader は current pointer から revision 1 まで到達可能な lineage だけを再計算する。到達不能な
revision は成功 replay に採用しない。standalone transaction journal は到達可能な revision 1/2 と
canonical bytes が完全一致しなければならず、delete authority 内で作成中の1 journalだけを
transaction-local な strict read に明示して検証する。
delete plan の `quarantine_entered_at` は live tombstone と一致し、
`delete_eligible_at` はその時刻に policy の固定30日を加えた値と完全一致しなければならない。
正の待機時間であるだけの forged plan や、承認済みでも短縮された時刻は effect 前に拒否する。
各 revision の tombstone 保持期限は、その revision 自身の receipt `committed_at + 365日` であり、
current-to-genesis reader が全到達 lineage について再検証する。

## 承認 binding

`cleanup_approval_action_plan(plan)` は次を P2-013A action digest へ拘束する。

- category `destructive_change`
- executor kind `local_process`
- cleanup module inventory SHA-256、version、availability
- internal field `cleanup_plan_sha256`
- cleanup root ID、P2-027A retention policy、P2-027B trace policy
- cost/runtime/memory/output/process 上限
- result type、execution ID、idempotency key、UTC expiry

cleanup module inventory は `local_data_cleanup_models.py`、`local_data_cleanup_canonical.py`、
`local_data_cleanup.py`、`storage/local_data_cleanup_store.py`、
`storage/revision_store.py` の exact bytes を含む。

実行時は approval run の current immutable ledger/decision/audit chain を完全検証し、approved
request、request revision、action digest、decision record/outcome hash、verified actor、
authority provider ID/version、`approve:destructive_change`、revocation、authority expiry を照合する。
さらに product/detached-run authority を保持した effect 直前に同じ検証を繰り返す。

P2-013A の `failed_with_limitations / external_executor_unavailable` は「approval transaction が
effect を起動していない」ことを示す承認証拠であり、cleanup 成功証拠ではない。

## idempotency と reconciliation

manifest と receipt は execution ID、idempotency key、plan hash、approval binding hash、
actor hash、authority snapshot hash、source tree hash を持つ。完全一致する成功 replay は保存済み
pointer/receipt/tombstone を再読して byte-equivalent result を返し、write zero である。同じ
execution ID または key に異なる plan/approval を与えると `idempotency_conflict` になる。
保存済み成功の照合は新しい effect admission より先に行うため、plan/request/actor authority の
期限後や delete 完了後でも、同一 plan と approval identity の historical replay は保存済み revision
を返す。ただし新しい filesystem effect を許可するものではない。

effect 前の失敗は mutation zero とする。journal、rename、unlink、pointer replace の後に結果を
確定できない場合は `reconciliation_required` または `effect_unknown` で停止する。
`inspect_reconciliation(plan)` は source/destination/staging/current/receipt/tombstone を
read-only 分類するだけで、repair、resume、retry、lock stealing は行わない。
partial delete の `delete_prepared` は新しい人間判断なしに自動再開しない。
`delete_prepared` の exact replay も bounded reconciliation を行い、staging が exact の場合だけ
`delete_staging_moved`、partial/absent の場合は `partial_delete`、unreadable の場合は
`effect_unknown` と報告する。quarantine の committed 判定には destination exact に加えて
product source absent を要求する。
standalone journal、未公開 revision、一時 pointer、dangling current link などが control namespace に
存在して strict current を読めない場合、current absent/no-effect とは推定せず
`current=unreadable / effect_unknown` とする。再実行も保存済み成功へ昇格させない。
cooperative cancellation は lock 前、journal 前、rename 直前、各 unlink 前に確認する。journal 後
かつ effect 前の cancellation は exact journal/scaffold だけを巻き戻し、effect 開始後は
`reconciliation_required` として停止する。

## filesystem と容量境界

- portable ID/path、NFC、case-fold alias、reserved device stem、root escape を拒否する。
- symlink、reparse point、hardlink、Windows alternate data stream、unknown entry kind を拒否する。
- durable journal を公開し fault/cancellation hook を通過した後、effect 時刻で authority と hold を
  再解決する。その外部 callback 完了後、effect 直前に authority、current、tree identity、
  destination absence、same-volume をローカル再検証する。失効は journal を exact rollback し、
  rename/staging/unlink に進まない。
- journal directory 作成後から publish 完了前までの write/fsync fault は、部分 file を含む exact
  transaction root を巻き戻す。巻き戻しを確認できない場合は成功にせず reconciliation を要求する。
- plan は1 action、tree 10,000 entries、target 100,000,000 bytes、control artifact
  1,000,000 bytes、control/run 10,000,000 bytes、lifetime 86,400秒を上限とする。
- capacity admission と cancellation は effect 前に fail closed とする。

terminal lifecycle audit は lifecycle audit 自身と approval V2 の3 control artifact を除く exact
inventory bytes/hash と再構築結果を照合する。除外した4 control artifact は terminal
`published_at` を起点とする365日保持の synthetic lifecycle subjects として件数・満了時刻へ加え、
全 product inventory が delete candidate になるまで quarantine を許可しない。

## 非保証

- **FACT**: secure erase、暗号鍵破棄、raw block overwrite は実装しない。
- **FACT**: unkeyed SHA-256 は corruption/correlation mismatch の検出用であり、
  same-privilege malicious writer authenticity を証明しない。
- **UNKNOWN**: Windows directory sync が unavailable の環境、hardware cache、突然の電源断後に
  どの rename/fsync が物理媒体へ残るかは platform evidence なしに保証しない。
- **FACT**: distributed atomicity、cross-volume atomicity、exactly-once、automatic retry、
  external cancellation、process-tree cleanup は主張しない。
- **FACT**: cleanup approval は計算、solver 実行、GTO、均衡、exact range、
  exploitability の証拠ではない。
