# 承認主体・権限契約（P2-013A）

## 状態と範囲

- **FACT**: P2-013A は RM-013 の入口マイルストーンであり、承認主体、検証済み権限、正規 action digest、request/decision idempotency、全件一括検証、RM-012 CAS 公開、失敗監査を実装する。
- **FACT**: P2-013B（再発行・完全な lifecycle）と P2-028A（process isolation）は別承認である。
  P2-027B cleanup executor は P2-013A の immutable approval outcome を承認証拠として利用するが、
  P2-013A 自身の decision semantics は変更しない。
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
- V1 compatibility projection の `approvals.json`

V2 ledger と 2 本の hash chain を先に完全検証し、run/ledger revision の decision 間連続性、最後の decision revision と terminal manifest revision の端点一致、V1 projection の一致を要求する。不一致なら run は corrupt とする。`FinalReport.approvals` と `approvals.json` に V2 field は追加しない。

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

## V1 compatibility

V1 `ApprovalRequest` は厳密に読み取り可能だが historical-only であり、action plan や authority を推定して V2 approve に昇格させない。local actor の V1 approve は `legacy_approval_historical_only`。safe reject は prior V1 canonical bytes の `HistoricalApprovalV1Binding` hash を decision reason に結び、前 revision を不変のまま successor を作る。完全な reissue／expiry repair は P2-013B に残る。

## API と CLI

- `Orchestrator.resume(run_id, *, approve_ids, reject_ids, reason, decision_batch=None)` は既存引数を保つ。
- `Orchestrator.decide_approvals(batch)` は committed outcome を返し、失敗は redacted `ApprovalDecisionValidationError.failure` として返す。
- CLI は既存 `resume --approve/--reject/--reason` と exit 0/2/3 を保ち、`--decision-file`、`--actor-id`、`--decision-id`、`--idempotency-key`、`--expected-run-revision`、`--expected-ledger-revision`、`--decision-at` を追加する。
- `--decision-file` は canonical V2 JSON だけを受け付け、legacy construction option と併用できない。
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
