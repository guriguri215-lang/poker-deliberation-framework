# 限定リバー検討の再開可能workflowとverified report view

P3-030Dは、既存のP3-030C限定日本語river call-EV reviewと、既存のP2-025B固定5役bridgeを
1つのlocal-first workflowとして構成します。parser、range、poker calculator、role controllerを
再実装せず、明示確認、canonicalな状態保存、status、resume、replay、両実行面のlinkageを追加します。
P3-030Eは、このworkflowへpure/read-onlyなreport表示を加えます。新しいreportを生成せず、既存の
verified `FinalReport`とworkflow/bridgeのhash・stateだけを表示します。

ここでP3-030Eの`verified FinalReport`とreport projectionは、repository-ownedなschema、hash、
workflow/bridge linkage、state、numeric-contractの検査に合格したことを意味します。戦略品質、
実戦rangeの正確性、GTO/equilibrium、外部solver一致、第三者認証、release readinessは証明しません。

P3-030Fは、nonlocal modeで次の固定roleだけを、request preview、利用者による全fieldの明示確認、
workflow-owned canonical confirmation receipt、1回のexecuteへ進めるworkflow-level wrapperを追加します。

P3-030Gは、このproduction workflowをrepository-owned fixtureで端から端まで通すfirst-class
deterministic production-workflow qualification harnessと、sanitized self-hashed canonical manifestを
追加します。実際のP3-030D prepare/12-hash confirm/runとP3-030F show/17-field confirm/execute wrapperを
再利用し、別の簡略workflowをqualification対象にはしません。

ここでrole confirmationは、previewされた全fieldへの利用者の明示的一致をworkflow receiptへ
hash束縛する手続です。利用者の本人認証、戦略判断の承認、外部第三者検証、model/providerの
現在資格を意味しません。

## 実装範囲

- P3-030Cが受理する有限な日本語NLHE cash grammar、単一opponent range、river call/foldだけを扱う。
- workflow planを先に保存し、plan hashとP3-030Cの12個の独立hashを利用者が確認する。
- P3-030C terminal manifest/inventoryと、同じcommit/tree・auth modeに固定したP2-025B bridge
  manifest/inventoryを1つのcanonical linkageへ結ぶ。
- P3 terminal作成後またはbridge準備後に中断しても、検証済みstorageから`resume`できる。
- `status`と`replay`は、plan、preparation、confirmation、P3 terminal、bridge source projection、
  linkageを再検証する。replayはparser、calculator、provider、modelを再実行しない。
- `show-bounded-river-review`は同じverified artifact chainから`json`、`summary`、`markdown`を
  投影する。report-writerが表示できるのは保存済みconclusion codeとevidence hashだけであり、
  新しい計算、range、戦略判断を追加しない。
- P3-030Fのstatusは`next_role`と`role_state`を表示し、workflow plan、linkage、current bridge
  revision/manifest/inventory/pointer、次のrole、request、runtime policyを1回の確認へcross-bindする。
- 確認成功時はexact P2 confirmationとpreview/confirmed lineageを、`binding_sha256`付きのrole別
  canonical receiptとしてworkflow側へ保存する。receipt検証後だけそのroleを実行可能にする。
- 1 roleごとにshow、全field confirm、executeを直列に繰り返す。自動確認、一括・並列実行、retry、
  skip、mode/model/provider fallbackは行わない。
- P3-030Gはfresh previewのexact 17 fieldをfixture管理のlocal authorityからproduction confirmへ渡し、
  confirm自体のzero executionと、private deterministic read-only executor seamを通した1回のexecuteを
  固定順の5 roleすべてで検査する。terminal status、replay、report、lineage、hashも再検証する。
- P3-030Gのsanitized manifestはsafe code、hash、count、固定metric、runtime inventoryだけを保持し、raw
  source、prompt/outbound bytes、credential、narrative、model trace、`user_materials/`を含めない。

一般自然言語・site固有history・OCR、複数range、multiwayまたはearlier-street equity、rake、all-in、
side pot、外部solver、GTO/equilibrium、一般Codex/Python bridgeは範囲外です。

## Runtime mode

`--auth-mode`の既定値は`local_only`です。この場合、P3-030Cのローカル計算とterminal保存を完了し、
固定5役のbridge planだけを準備して`completed_local_only`になります。model runtime directory、API key、
保存済みChatGPT login、networkは使わず、model/nonlocal runtimeを開始しません。P3-030Eの表示も
同じlocal-only read境界内で、parser、calculator、provider、modelを起動しません。

P3-030Fのrole用`show-bounded-river-review-role-request`、
`confirm-bounded-river-review-role-request`、`execute-bounded-river-review-role`は、`local_only`では
すべてtyped error `BRW_E_LOCAL_ONLY`で拒否されます。offlineのparser、calculator、storage、replay、
verified report表示は引き続き利用できますが、role transportやruntime directoryは開始しません。

`codex_subscription`を明示したworkflowはbridge準備後に`awaiting_role_review`となります。
role transportは自動開始せず、次節のP3-030F wrapperを利用者がroleごとに操作します。現在の
subscription qualificationは`UNKNOWN`です。`openai_api` planには正の
`--api-max-cost-micro-usd`が必要で、現在のadapterはlive-unqualifiedかつdefault-disabledです。

## CLIの流れ

`--workflow-root`には、候補commitの追跡済み`.gitignore`によって無視されるrepository配下の
専用ディレクトリを指定します。例は`tmp/runs/river-review-001`です。`--repository-commit`と
`--repository-tree`は、bridgeを準備するclean checkoutの候補commit/treeに一致させます。

```powershell
poker-deliberate prepare-bounded-river-review `
  --source .\hand-ja.txt --range .\range.json `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001 `
  --intake-id intake-001 --source-run-id source-run-001 --bridge-run-id bridge-run-001 `
  --source-id local-hand-001 --repository-commit <commit> --repository-tree <tree>
```

prepareのJSONには`plan_sha256`と`expected_hashes`が出ます。内容を確認した後、表示された値を
省略せず`confirm-bounded-river-review`の`--expected-plan-sha256`と12個の
`--expected-*-sha256`へ渡します。確認後の実行と状態確認は次の通りです。

```powershell
poker-deliberate run-bounded-river-review `
  --source .\hand-ja.txt --workflow-root .\tmp\runs\river-review-001 `
  --workflow-id river-review-001

poker-deliberate status-bounded-river-review `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001

poker-deliberate resume-bounded-river-review `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001

poker-deliberate replay-bounded-river-review `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001

poker-deliberate show-bounded-river-review `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001 `
  --repository-root . --format markdown
```

P3 terminal作成前の`resume`には元の`--source`が必要です。terminal作成後はraw sourceをworkflowへ
複製せず、検証済みstorageから再開できます。`show-bounded-river-review`はterminal verification済みの
workflowだけを受理し、raw sourceや未検証のflat fileからreportを再構成しません。

## P3-030F supervised role lifecycle

この手順は、既存例のsource、range、ID形式を再利用し、`--auth-mode codex_subscription`を明示して
新規準備したworkflowがbridge linkageまで完了した場合のrole loopです。各roleで最初に実行するコマンドは
`show-bounded-river-review-role-request`です。previewはexact outbound UTF-8/base64、bytes/hash、
context envelope、runtime policy/identity、auth mode、model/provider、credential reference、retentionと、
確認に必要な17個の`confirmation_fields`を返します。show自体はroleを実行せず、bridge stateも変更しません。

```powershell
$rolePreviewJson = poker-deliberate show-bounded-river-review-role-request `
  --workflow-root .\tmp\runs\river-review-001 `
  --workflow-id river-review-001 `
  --repository-root . `
  --format json
$rolePreview = ($rolePreviewJson -join "`n") | ConvertFrom-Json
$fields = $rolePreview.confirmation_fields
$rolePreviewJson | Out-Host
```

利用者は`request`、`next_role`、`next_role_state`と`confirmation_fields`を確認します。
`authority-id`、`confirmation-id`、`idempotency-key`も利用者が明示し、previewから得た全fieldを
次の全`--expected-*`へ省略せず渡します。`...`でflagを省略せず、利用者の確認なしにconfirmを
自動実行しません。

```powershell
$authorityId = Read-Host "authority-id"
$confirmationId = Read-Host "confirmation-id"
$idempotencyKey = Read-Host "idempotency-key"
$null = Read-Host "全preview fieldを確認後にEnter"

poker-deliberate confirm-bounded-river-review-role-request `
  --workflow-root .\tmp\runs\river-review-001 `
  --workflow-id river-review-001 `
  --repository-root . `
  --authority-id $authorityId `
  --confirmation-id $confirmationId `
  --idempotency-key $idempotencyKey `
  --expected-plan-sha256 $fields.expected_plan_sha256 `
  --expected-linkage-sha256 $fields.expected_linkage_sha256 `
  --expected-bridge-revision $fields.expected_bridge_revision `
  --expected-bridge-manifest-sha256 $fields.expected_bridge_manifest_sha256 `
  --expected-bridge-inventory-sha256 $fields.expected_bridge_inventory_sha256 `
  --expected-bridge-pointer-sha256 $fields.expected_bridge_pointer_sha256 `
  --expected-role $fields.expected_role `
  --expected-auth-mode $fields.expected_auth_mode `
  --expected-request-sha256 $fields.expected_request_sha256 `
  --expected-request-bytes-sha256 $fields.expected_request_bytes_sha256 `
  --expected-envelope-sha256 $fields.expected_envelope_sha256 `
  --expected-runtime-policy-sha256 $fields.expected_runtime_policy_sha256 `
  --expected-runtime-identity $fields.expected_runtime_identity `
  --expected-model-provider $fields.expected_model_provider `
  --expected-model $fields.expected_model `
  --expected-credential-reference $fields.expected_credential_reference `
  --expected-remote-retention-policy $fields.expected_remote_retention_policy `
  --format json
```

confirm成功時、workflowは`role-confirmation-binding-<ordinal>-<role>.json`をexclusive-createします。
このcanonical receiptの`binding_sha256`は、workflow plan/confirmation、linkage、auth mode、role順、
17 confirmation fieldが表すrequest/runtime policy、authority/confirmation/idempotency ID、P2 confirmation
hashとexpiry、preview時とconfirm後のbridge revision/manifest/inventory/pointerを結びます。receiptが検証できる
ことがworkflow-levelの実行許可であり、lower-level P2 confirmationだけでは代用できません。

既存の`confirm-bounded-codex-role-request`を直接実行した直後や、P2 confirmationの保存後・workflow receiptの
保存前に中断した場合、statusはそのroleを`awaiting_confirmation` / `show_role_request`として表示します。
fresh `show-bounded-river-review-role-request`で現在のlineageを取り直し、既存P2 confirmationと同じ
`authority-id`、`confirmation-id`、`idempotency-key`と、fresh previewの17 fieldをwrapper confirmへ明示すると、
P2 confirmationを再発行せず不足receiptを作成できます。値が一致しなければ`BRW_E_ROLE_BINDING`です。
すでにadmission済みまたはcompletedのroleにreceiptがない場合はstatus、resume/replay、report viewが
`BRW_E_ROLE_BINDING`でfail closedし、自動生成・修復しません。

確認成功後のstatusは同じ`next_role`、`role_state=executable`、
`next_action=execute_role`と`role_confirmation_expires_at`を表示します。次のコマンドは、確認済みの
そのroleを期限内に1回だけ実行します。
`--runtime-root`は必須です。候補commitの`.gitignore`により無視されるrepository内で、role実行ごとに
別の、まだ存在しないuntracked scratchを指定します。runtime rootはsingle-useで、1回目の実行後に残る
同じdirectoryを次roleへ再利用できません。

```powershell
$runtimeToken = [Guid]::NewGuid().ToString("N")
$runtimeRoot = ".\tmp\bridge-runtime-$($rolePreview.next_role)-$runtimeToken"
if (Test-Path -LiteralPath $runtimeRoot) { throw "runtime root must not exist" }
poker-deliberate execute-bounded-river-review-role `
  --workflow-root .\tmp\runs\river-review-001 `
  --workflow-id river-review-001 `
  --repository-root . `
  --runtime-root $runtimeRoot `
  --format json
```

成功後、残りroleがあればstatusは次の`next_role`、`role_state=awaiting_confirmation`、
`next_action=show_role_request`へ進みます。同じshow → 全field confirm → 1回executeを繰り返します。
固定5 roleの完了後は`next_role=null`、`role_state=terminal`、`next_action=none`です。実行済みroleを
再実行したり、roleをskipしたりするcommandはありません。

statusは次roleの`role_request_expires_at`と、P2 confirmationが存在する場合の
`role_confirmation_expires_at`をUTCで表示します。どちらかの有効期限に達すると`role_state=expired`、
`next_action=none`となり、show/confirm/executeは`BRW_E_ROLE_EXPIRED`で停止します。期限切れを自動再確認せず、
同じworkflowを別modeへ切り替えず、自動retryやfallbackも行いません。

`reconciliation_required=true`はadmission後・audit確定前の`role_state=in_progress`と、
`cancel_unconfirmed` / `effect_unknown`のterminal stateで優先されます。role wrapperは
`BRW_E_ROLE_RECONCILIATION`で停止し、同じroleの再実行、再確認、skip、新しいmodeでの実行を行いません。
`failed` / `timed_out` / `cancelled`など、reconciliationを要求しないterminal stateも自動retryしません。

## P3-030G deterministic qualification

`BoundedRiverReviewWorkflowFixtureV2`を使うV2 evaluatorは、production workflowを次の順で検査します。

1. P3-030Dのprepare、12-hash confirm、terminal runを実行する。
2. 各roleでfresh `show-bounded-river-review-role-request`相当のpreviewを取得し、exact 17 fieldを
   fixture管理のlocal authorityでproduction confirmへ渡す。
3. confirmだけではrole execution countが増えないことを検証する。
4. production single-role execute wrapperをprivate `_role_executor` evaluation seam経由で1回だけ呼ぶ。
5. 固定5 roleをserial orderで完了後、terminal replay、verified report、lineage、hashを再検証する。

callbackはshared `DeterministicReadOnlyTransport`だけを使い、live model/provider、network、credentialを
使いません。fixture管理の確認は人間の本人認証や判断ではなく、exact field bindingを検査する決定論的な
test actionです。したがって`SanitizedBoundedRiverReviewWorkflowQualificationManifestV1`が
`qualification_status="passed"`でも、`transport_qualification="deterministic_fixture"`、
`live_qualification_status="UNKNOWN"`、`api_live_executed=false`、
`api_production_qualified=false`です。

actual-live/provider qualificationはこのharnessとは別です。固定5 roleそれぞれについてfresh previewを
人間が確認し、全fieldを明示確認してから1 roleずつ実行する5 cycleが必要です。deterministic manifest、
historical live evidence、API keyの存在をcurrent live資格へ昇格しません。strategy quality、human usefulness、
range accuracy、GTO/equilibrium、外部solver一致もqualification対象外です。

repository runnerはV2を既定とし、canonicalな`v2/scenarios.json`と、hash-boundされた既存V1
`source-ja.txt` / compact `range.json`を使います。`--output`はself-hashed V2 evaluation resultを
exclusive-createし、全case/metricが合格した場合だけ、任意の`--manifest-output`へsanitized manifestを
exclusive-createします。既存pathの上書きや不合格resultからのmanifest生成は行いません。

`--work-root`はproduction workflowのconfined namespaceとなるため、repository内のGit-ignoredかつuntrackedな
まだ存在しないpathだけを受理します。`--output`と任意の`--manifest-output`はrepository外、または
repository内ならGit-ignoredかつuntrackedな、まだ存在しないpathを受理します。3 pathは互いにdistinctかつ
non-overlappingでなければならず、tracked/unignored path、fixture/source/range path、それらの親子、または
互いの親子は受理しません。runnerはこのpath preflightをevaluationやdirectory/file作成より先に行い、拒否時は
これら3 pathのいずれも作成しません。preflight例外についてcanonical failure artifactは生成しません。

```powershell
python scripts\run_bounded_river_review_workflow_evaluation.py `
  --source-commit <commit> --source-tree <tree> `
  --work-root .\tmp\codex-goals\p3-030g\work `
  --output .\tmp\codex-goals\p3-030g\evaluation.json `
  --manifest-output .\tmp\codex-goals\p3-030g\manifest.json
```

## 状態

| state | 意味 | next action |
|---|---|---|
| `awaiting_confirmation` | planとpreparationは保存済み、確認前 | `confirm` |
| `ready_to_run` | 確認済み、P3 terminal未作成 | `run` |
| `ready_to_resume` | P3 terminalまたはbridgeまで作成後に中断 | `resume` |
| `completed_local_only` | local計算、terminal、bridge plan、linkageが検証済み | `none` |
| `awaiting_role_review` | nonlocal modeのbridge planを準備済み | `show_role_request` |
| `role_review_in_progress` | role確認済み、一部role結果保存済み、またはreconciliation待ち | `show_role_request` / `execute_role` / `none` |
| `completed` / `failed` | bridge replayでterminal状態を確認済み | `none` |

bridge linkage後は、同じstatusに次のrole単位fieldが入ります。

| `role_state` | `next_role` | `next_action` | 意味 |
|---|---|---|---|
| `awaiting_confirmation` | 次の固定role | `show_role_request` | previewとworkflow receiptを作る明示確認が必要。direct P2 confirmationだけの場合もこの状態 |
| `executable` | 確認済みの次の固定role | `execute_role` | receipt検証済みかつ期限内のroleを1回だけ実行可能 |
| `expired` | 期限切れの次role | `none` | requestまたはconfirmation期限切れ。自動再確認しない |
| `in_progress` | admission済みrole | `none` | 自動retryせずreconciliationが必要 |
| `terminal` | `null` | `none` | local-onlyまたはbridge terminal。reconciliation requiredなら再実行しない |

主なtyped refusalは、local-only role wrapperの`BRW_E_LOCAL_ONLY`、古いまたは不一致のpreview fieldの
`BRW_E_ROLE_BINDING`、固定順外roleの`BRW_E_ROLE_ORDER`、未確認・完了後などの
`BRW_E_ROLE_STATE`、request/confirmation期限切れの`BRW_E_ROLE_EXPIRED`、admission後またはterminalの
reconciliation要求の`BRW_E_ROLE_RECONCILIATION`です。通常のrole transport失敗は
bridgeの`failed` / `timed_out` / `cancelled` / `cancel_unconfirmed` / `effect_unknown`として永続化され、
CLIはstatusを表示して終了code 2を返します。`BRW_E_ROLE_EXECUTION`はproduct boundary、storage、runtime準備
などの例外をworkflowが縮約したcodeです。いずれも自動再確認、retry、skip、新mode、fallbackで回避しません。

## Local dataと評価

workflow plan、preparation、confirmation、role confirmation receipt、linkage、bridge artifactは指定した
Git管理外rootにだけ保存し、
raw日本語sourceをworkflow/bridge namespaceへ複製しません。認証情報の値はplan、status、errorへ保存・表示
しません。report viewはsource terminalのraw source bytesを検証とhash相関のために読みますが、表示、複製、
bridge投影はせず、既存FinalReportのverified projectionと相関hash/stateに限定します。credentialとmodel
traceも表示・複製しません。元sourceとrangeの保管・削除方針は呼出側が管理します。

repository-owned評価は、confirmation binding、exact decision math、runtime modeと固定role、
resume/replay、local-data separationの5 metricを独立に採点します。外部modelとsolverは実行しません。
P3-030Eのtestは、表示のpure/read-only性、artifact verification、format parity、report-writerの
conclusion/evidence allowlist、local_onlyでmodel/nonlocal runtimeを開始しないことを検査します。
P3-030Fのtestは、次roleのpreviewがread-onlyであること、17 fieldとcanonical receiptのcross-binding、
direct P2 confirmationからの明示復旧、receipt欠落・expiry・reconciliationのfail-closed性、固定role順、
1 roleずつの実行、local-only typed拒否、no fallback、既存`FinalReport`とexact計算結果の不変性を検査します。
P3-030GのV2 testは、production prepare/confirm/run、5回のfresh preview/fixture確認/単発execute、confirm時の
zero execution、terminal replay/report、sanitized canonical manifestを検査します。外部model、provider、
network、credential、live qualification、security scanは実行しません。

CLI引数と既定fixture pathだけを確認する場合は
`python scripts\run_bounded_river_review_workflow_evaluation.py --help`を使います。
