# 隔離ジョブ制御契約（P2-028A）

## 状態と対象

- **FACT**: P2-028AはWindows上のrepository-owned synthetic helperだけを対象とする入口スライスである。
- **FACT**: `repository_synthetic_isolated_job_control`は`implemented`である。
- **FACT**: 汎用`process_sandbox`、`external_solver`、`codex_python_runtime_bridge`は`unavailable`、
  `openai_agents_outbound`は`disabled`のままである。
- **FACT**: RM-028とP2-028Aは`in_progress`である。任意外部コード、実provider/solver、
  remote cancellation、OS強制network isolationを満たしていないため`completed`ではない。
- **ASSUMPTION**: backendはAMD64版Windowsと、実行時にidentityを取得できるbase CPythonを前提とする。
- **UNKNOWN**: 今回のローカル行以外のWindows/Python組合せ、remote CI、異なるOS設定での結果。

このAPIは通常`Orchestrator`、CLI、provider、solver、Codex/Python bridgeへ接続しない。shell、
自由なexecutable、自由なargv、script/module指定、環境変数、network要求を受け付けない。

## 実証する保証

| 項目 | P2-028Aの保証 |
|---|---|
| 起動対象 | hash・file identityを再検証したbase Pythonと固定`synthetic_child.py`だけ |
| argv | `-I -S -B -u -X utf8`とclosed operation enum／bounded整数だけ |
| process tree | Windows Job Objectへsuspended状態で割当て後にresumeし、`KILL_ON_JOB_CLOSE`と`TerminateJobObject`を使用 |
| resource | process/job CPU time、process/job committed memory、active process数をJob Objectへ設定してexact requery。CPUはJob accountingもpollし、上限到達時にJob全体を停止 |
| wall/output | controller wall clockとstdout／stderr／combined byte capの超過時にJob全体を停止 |
| handles | NUL stdin、stdout pipe、stderr pipe、任意の承認済みinput handle一つだけを明示handle listへ設定 |
| filesystem | 固定working directoryはidentity-bound。任意write surfaceはなく、inputはworkspace配下の単一read handleだけ |
| link/path | absolute Windows path、component、reparse/symlink、hardlink、ADS、reserved name、escapeをfail closed |
| secrets | requestは参照IDとdigestだけ。秘密形状、raw secret値、raw reconciliation evidenceを拒否 |
| durable state | immutable full snapshot、current CAS、hash lineage、固定3 artifact、typed transitionを検証 |

次は保証しない。

- OSレベルのnetwork default-denyまたはdestination allowlist。requestでnetworkを求めた時点で拒否する。
- 任意外部コード、provider、solver、package、container、WSLの隔離。
- reduced token、AppContainer、別ユーザー、ACL boundary、同権限malicious writerからの隔離。
- remote process、remote billing、provider課金、remote cancellationの停止証拠。
- interpreterがloadし得る全OS DLLの完全な推移的attestation。
- distributed filesystem、power-loss durability、exactly-once、writer authenticity、秘密性。

## Versioned contract

`poker_deliberation.isolated_jobs.models`はstrict、frozen、unknown-field拒否のversion
`1.0.0` contractを持つ。

- `IsolatedJobRequestV1`: run/execution/attempt/context/budget lineage、closed operation、
  bounded arguments、secret reference metadata。
- `IsolatedJobPolicyV1`: backend、Job Object limits、execution identity、filesystem/handle policy。
- `ExecutionIdentityV1`: interpreter、Python DLL、encoding files、synthetic helper、Python version、
  architectureと全体digest。helperは標準libraryの非frozen importを3つのencoding fileだけに
  制限し、module-inventory実体試験でidentity集合との対応を確認する。
- `JobEvidenceV1`: process identity、exit/termination、wall/CPU/memory/process/output accounting、
  command-line digest、handle数、tree停止・limit再照合・identity再照合。
- `DurableIsolatedJobStateV1`: request/policy/action/approval/context/budget binding、generation、
  previous hash、effect state、evidence、event chain。

canonical bytesは既存storage canonical JSONを再利用し、SHA-256はcorruptionとcorrelationだけを
検出する。署名、writer authenticity、秘密性は主張しない。

## Admissionとeffect境界

実行順序は次の通りである。

1. request、policy、P2-024A `ContextEnvelope`、P2-011B lineageを検証する。
2. exact request/policy/context/budget/secret-reference digestを持つ
   `CanonicalActionPlanV2(action_category="external_code")`を構築する。
3. P2-012B terminal readerでP2-013B approval chainを読み、live actor/scope/expiry/revocationを
   effectなしで再検証する。
4. P2-011B permitをreserveし、`prepared` snapshotを専用revision rootへpublishする。
5. childをsuspendedで生成し、Job Objectへ割り当て、limitとidentityを再照合する。
6. approvalをもう一度再検証し、同じapproval currentである場合だけ`launch_committed`をpublishする。
7. permitをstartし、primary threadをresumeして`running`をpublishする。
8. outcomeをbudgetへsettleしてからterminal job snapshotとbounded outputをpublishする。

approval、context、budget、identity、path、handleの不一致は起動前に拒否する。同一executionの並行
実行はrevision authorityにより一つだけがeffectへ進み、他方は`run_locked`となる。terminal exact
replayは保存済みresultを返し、子を再起動しない。automatic retryは常に禁止する。

## Durable stateとrecovery

状態は次の意味を持つ。

```text
prepared
  ├─ launch_committed ─ running ─ completed
  │                         ├─ failed
  │                         ├─ cancel_requested ─ cancelled
  │                         └─ effect_unknown
  ├─ failed
  └─ effect_unknown ─ reconciled
```

`completed`はexit code 0、active process 0、完全なoutput evidence、成功settlementを全て要求する。
`cancelled`はJob全体停止、active process 0、cancellation acknowledgement、cancelled settlementを
要求する。`effect_unknown`はsuccess、failed、retryへ変換しない。再起動後は保存済みPIDとcreation
timeを照合し、同じlive processまたはidentity mismatchなら自動回復せず`run_locked`に停止する。
process不在を確認した場合も保守的に`effect_unknown`へlatchする。人間がopaque reference IDと
evidence digestを与え、process不在を再確認した場合だけ`reconciled`へ進むが、これは非successである。

各revisionは`isolated_job_state.json`、UTF-8 `stdout.txt`、`stderr.txt`のfull snapshotを持つ。
partial write、corrupt current/payload、stale CAS、cross-execution replay、transition lineageの改変は
fail closedとなる。

job rootとbudget rootはdistributed transactionではない。terminal job snapshotより先にbudgetを
settleし、terminal publicationが不確実ならbudget settlementが`succeeded`でもjobを
`effect_unknown`にする。budget settlement単独はjob success authorityではなく、自動retryを許可しない。

## テスト境界

repository testは次を実Windows processで検証する。

- normal exit、closed stdin、module inventory、明示input handle。
- wall-clock、CPU、memory、process count、stdout、stderr、combined output cap。
- descendant tree termination、cancel race、同一execution並行起動。
- real approval/context/budget/storageのvertical sliceとterminal exact replay。
- restart/effect-unknown/reconciliation、partial publication、payload tamper。
- workspace escape、hardlink、secret形状、CRLF/BOM、identity改変、argv/env/shell field注入。
- strict contract canonicalizationとproperty-based durable transition。

mockだけでhard-stopやresource isolationを実装済みとは判定しない。
