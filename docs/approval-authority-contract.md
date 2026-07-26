# 承認主体・権限・再発行契約（P2-013A / P2-013B）

## 状態と範囲

- **FACT**: P2-013A は RM-013 の入口マイルストーンであり、承認主体、検証済み権限、正規 action digest、request/decision idempotency、全件一括検証、RM-012 CAS 公開、失敗監査を実装する。
- **FACT**: P2-013B は、P2-013A の decision semantics を変更せず、request reissue、
  resume/lifecycle binding、汎用の effect-free pre-execution recheck を追加する。
  P2-027B cleanup executor の既存 inventory と独立した effect 直前 recheck は変更しない。
- **FACT**: P2-028A の process isolation、durable external-effect state、executor 起動、
  cancellation、reconciliation は P2-013B の範囲外である。
- **FACT**: P2-013A は外部 executor、provider、solver を起動しない。承認は実行・正しさ・収束・GTO・均衡・exploitability・正確な range の証拠ではない。

## 信頼境界

`DecisionAuthorityProvider` が process 内で注入された唯一の信頼済み権限源である。decision batch 内の `ApprovalActor` は照合対象であり、それ自体を信用しない。

- 既定の `LocalCliAuthorityProvider` は `verification_status=unverified`、`authority_source=local_cli`、scope は `reject:any` のみ。
- approve には provider が返した `verified` actor、対象 category と完全一致する `approve:<category>`、未失効かつ `not_revoked` の権限が必要。
- claimed actor と provider snapshot の不一致は `actor_spoof`、scope 不足は `unauthorized_decision`、失効は `authority_revoked`。
- provider ID、provider version、resolved UTC、canonical actor を含む authority snapshot は decision record に保存し、専用 digest を outcome、record、domain audit へ拘束する。lock 内の再検証では actor に加えて provider ID/version も一致しなければならない。
- session、credential、verification reference は SHA-256 の参照だけを保存し、token、secret、certificate、provider payload は保存しない。

## V2 request と action digest

`ApprovalProposalV2` は proposal と `CanonicalActionPlanV2` の入力だけを持つ。request ID、revision、state、actor、decision、audit は application-owned である。

action digest は operation、category、executor identity/version/hash/availability、送信 field の分類と content hash、destination、retention/trace policy、cost/runtime/memory/output/process 上限、working directory、environment 名 allowlist、result type、execution ID、remote idempotency key、UTC expiry の全項目を domain-separated SHA-256 で拘束する。値または順序が変われば別 action である。

request idempotency key は run ID、phase ID、stable proposal ID、category、action digest から決定し、attempt ID は lineage にだけ記録する。同じ key と同じ canonical payload は同じ pending request、異なる payload は `idempotency_conflict` である。

## authoritative artifact

新しい V2 checkpoint は次を一組として持つ。

- `approval_ledger_v2.json`
- `approval_decisions_v2.jsonl`
- `approval_audit_v2.jsonl`
- reissue を1回以上行った run に限り `approval_reissues_v2.jsonl`
- V1 compatibility projection の `approvals.json`

V2 ledger、decision/domain-audit chain、任意の reissue chain を lookup より先に完全検証する。
decision と reissue を合わせた run/ledger mutation timeline、source/successor projection、
terminal manifest revision の端点、V1 projection の一致を要求する。不一致なら run は corrupt とする。
reissue がない既存 run の3 control artifact byte contract は変えず、`FinalReport.approvals` と
`approvals.json` に V2 field は追加しない。

## 固定検証順序

1. current revision と ledger/log chain の strict read
2. idempotency key lookup、保存済み actor hash／batch hash 照合、完全 replay
3. expected run revision、expected ledger revision
4. decision ID／request ID の重複、approve/reject conflict
5. unknown、already decided、request revision、action digest、expiry
6. provider による actor 解決と完全一致
7. exact authority scope、revocation、authority expiry
8. RM-012 lock 内で expiry と authority を再検証
9. 1 個の immutable successor を current-pointer CAS で公開

同一 idempotency key と同一 canonical batch は保存済み `ApprovalDecisionOutcome` をそのまま返し、artifact、report、manifest、marker、pointer、audit を書き換えない。CAS 敗者は winner を再読し、完全 replay でなければ `stale_decision` または `resume_conflict` となる。

## domain 結果

- pending が残る reject batch: 新しい `approval_required` checkpoint
- 全件 reject: 外部操作なしの `completed`
- approve を含む batch: 承認は記録するが `failed_with_limitations` / `external_executor_unavailable`

ledger、decision log、domain audit、V1 projection、state、JSON/Markdown report、manifest、terminal marker は同一 revision にまとめて公開する。partial batch と mixed revision は認めない。

## 失敗監査

domain decision に失敗した試行は domain current を変更せず、同じ per-run RM-012 authority の control ledger に redacted event を追加する。

- 最大 1024 events/run
- 最大 16,384 bytes/event
- 最大 1,048,576 event bytes/run
- actor/run ごとに rolling 60 秒で最大 32 failure events
- 上限到達時は window ごとに 1 個だけ rate-limit marker を記録
- 自動削除、truncate、overwrite は行わない

監査には actor、decision、idempotency、batch の hash、failure code、観測 revision だけを残す。raw reason、action content、credential、traceback は残さない。監査の durable reread を確認できなければ `audit_unconfirmed` とし、decision revision を進めない。

## request reissue と V1 compatibility

V1 `ApprovalRequest` は historical-only であり、action plan、authority、expiry を推定して V2 approve
へ昇格させない。local actor の V1 approve は引き続き `legacy_approval_historical_only` である。
P2-013B reissue は pending V1 request 全件を同じ batch へ明示し、各 successor の完全な
`CanonicalActionPlanV2` と表示情報を caller が与えた場合だけ受理する。元 V1 canonical snapshot と
hash を record に保存し、元 request は compatibility projection 上で reissued/rejected、
successor は pending となる。silent migration と一部だけの V1 repair は拒否する。

V2 reissue は exact pending source の expiry 時刻以後だけ受理する。source request revision と
action digest が一致し、新しい action expiry が reissue 時刻より後でなければならない。
1 transaction で source を `superseded`、明示 successor を `pending` にし、previous
manifest/pointer/ledger hash、run/ledger revision、source/successor request hash を immutable
reissue record へ拘束する。idempotency replay は stale check より前に解決して write zero、
異なる payload、live request、部分 batch、CAS loser は structured failure となる。

## pre-execution recheck

`recheck_approval_for_execution` は effect を開始しない pure admission seam である。strict reader を
通過した approval state と、approval run revision/pointer/manifest、exact action plan、
injected authority provider、評価 UTC を入力とする。

- request が exact action digest を持つ approved state であること
- 唯一の immutable approve decision/result と record/outcome hash が一致すること
- approval decision revision が指定した approval run revision と一致すること
- request expiry 前であること
- provider ID/version と canonical actor が保存 snapshot と一致すること
- live actor が verified、not revoked、未失効で exact scope を持つこと

全条件を満たした場合だけ、ledger、decision、actor/authority、execution ID、valid-until を含む
hashed `ApprovalExecutionRecheckBindingV2` を返す。失敗は mutation zero の
`ApprovalExecutionValidationError` であり、binding は実行権限の再利用 token でも effect receipt
でもない。executor は effect 直前にこの条件を評価し、binding expiry 後に使用してはならない。

## API と CLI

- `Orchestrator.resume(run_id, *, approve_ids, reject_ids, reason, decision_batch=None,
  reissue_batch=None)` は既存引数を保ち、reissue を keyword-only で追加する。
- `Orchestrator.decide_approvals(batch)` は committed outcome を返し、失敗は redacted `ApprovalDecisionValidationError.failure` として返す。
- `Orchestrator.reissue_approvals(batch)` は committed reissue outcome を返し、同じ bounded failure
  audit と RM-012 CAS を使用する。
- CLI は既存 `resume --approve/--reject/--reason` と exit 0/2/3 を保ち、`--decision-file`、`--actor-id`、`--decision-id`、`--idempotency-key`、`--expected-run-revision`、`--expected-ledger-revision`、`--decision-at` を追加する。
- `--decision-file` は canonical V2 JSON だけを受け付け、legacy construction option と併用できない。
- `--reissue-file` は canonical `ApprovalReissueBatchV2` JSON だけを受け付け、decision file／construction
  option と併用できない。
- CLI は verified flag や任意 authority scope を受け付けない。

## セキュリティ上の非主張

hash chain は corruption と correlation を検出するが、同一権限の malicious writer に対する authenticity を証明しない。remote identity、network verification、HMAC/signature、secure erase、distributed filesystem の durability は本契約の保証外である。

## P2-027B execution binding

P2-027B は `CanonicalActionPlanV2` の category `destructive_change` に、cleanup module inventory、
cleanup plan digest、cleanup root identity、policy/trace ID、resource limits、execution/idempotency
identity、expiry を拘束する。executor は approval run current の全 ledger/log chain、
approved request/result revision、record/outcome hash、actor/authority snapshot を照合し、effect直前に
providerへ再解決する。revoked、expired、scope mismatch、actor/provider mismatch は mutation zero。
`external_executor_unavailable` は承認時に effect がなかった証拠で、cleanup result には転用しない。
