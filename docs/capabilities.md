# 能力と実行状態

この文書は Phase 0/1 完了後の能力表示契約です。`poker-deliberate doctor --format json` の
`capabilities`、provider health、登録済みtool、追跡済みエージェント定義と照合します。
能力状態の正は`capabilities.py`、RM作業状態の正は`roadmap_status.json`であり、doctorは両者を
別fieldとして表示します。RM completedをruntime capability availableと同一視しません。

## 状態の定義

- **implemented**: 現在のローカル実行経路と回帰テストが存在する。
- **disabled**: 境界や設定項目は存在するが、通常経路で意図的に実行不能にしている。
- **unavailable**: 要求された能力を実行する実装・adapter・対象gameが同梱されていない。
- **planned**: ロードマップ候補であり、現在の実装済み能力ではない。

Providerの`available`は、現在`analyze`を実行できる場合だけ`true`です。providerの
`disabled`と`unavailable`はどちらも`available=false`ですが、前者は意図的な実行停止、後者は
実行前提の欠落を表します。CLI全体のdoctor `status=ok`は診断処理が完了したという意味で、
全能力がavailableという意味ではありません。

## Capability matrix

| capability ID | 状態 | 実行上の意味 |
|---|---|---|
| `local_calculators` | **implemented** | 登録済みローカルtoolをtyped `ToolResult`として実行する。 |
| `local_provider` | **implemented** | 境界検証用。文章的な専門分析やモデル推論は生成しない。 |
| `openai_agents_outbound` | **disabled** | `OpenAIAgentsProvider.analyze`は未実装。SDK/API keyの有無にかかわらず外部送信しない。 |
| `external_solver` | **unavailable** | `solver_status`は正直なUnavailableを返すだけで、外部solverを実行しない。 |
| `full_nlhe_equilibrium` | **unavailable** | full NLHE game tree、CFR、node locking、検証済み均衡計算はない。 |
| `heads_up_nlhe_equity` | **implemented** | heads-up NLHEに限り、上限付きexact enumerationまたはseed付きMonte Carloを実行する。 |
| `multiway_or_plo_equity` | **unavailable** | multiway equityとPLO equityは未対応。 |
| `documented_hand_parser` | **implemented** | version 1のstrictかつprovenance-boundなkey-value/player/action形式だけを保守的に正規化する。対応siteは`none`。 |
| `versioned_nlhe_range_grammar` | **implemented** | provenance、game condition、blocker、整数millionth weightを検証し、1 opponent rangeをcanonical comboへ変換するbounded grammar v1。 |
| `versioned_nlhe_river_equity_bridge` | **implemented** | P3-016B専用admissionで、検証済み単一rangeをper-run authorityで直列化したproduct namespace予約、buffer外のexclusive-create pre-execution commitment、strict binding artifact、riverのexact-only heads-up enumeration、有理数oracle、failed-prefixを含むterminal replay、3 metricのexact-evidence評価へ拘束する。all-inや一般equity自動接続ではない。 |
| `profiled_nlhe_side_pot_ledger` | **implemented** | `generic_nlhe_cash_no_rake_v1`に限り、整数単位のcontribution、uncalled return、side pot、eligibilityを独立oracle付きで計算する。 |
| `natural_language_or_site_parser` | **unavailable** | 一般自然言語およびsite-specific hand history parserはない。独立したbounded Japanese grammarをこの能力へ拡張しない。 |
| `bounded_japanese_nlhe_cash_parser` | **implemented** | version 1の文書化済み日本語retrospective NLHE cash grammarを、exact UTF-8 span、6 hash確認、固定LocalProvider、限定tool、durable replayに接続する。一般自然言語・site parserではない。 |
| `bounded_japanese_river_call_ev_review` | **implemented** | P3-030C専用admissionで、P3-030Bのriver fold完了履歴と明示確認済み単一rangeを、calculator-free source semantic replay、P3-015A ledger、P3-016B exact equity、既存`pot_odds`/`raked_call_ev`へ一度ずつ固定順で接続し、exact Fraction oracle、ULP検証、model限定call/fold比較、typed terminal replay、checkout/module/callable-origin拘束済み3 metric評価へ拘束する。tool budget拒否は独立external recordへ先行拘束する。一般戦略やGTOではない。 |
| `bounded_river_review_workflow` | **implemented** | P3-030Dは1つの明示確認済みP3-030C reviewと1つのmode-bound P2-025B固定5役bridge planを、canonical plan、linkage、status、resume、replayで接続する。P3-030Eは既存のverified `FinalReport`とworkflow/bridgeのhash・stateだけをpure/read-only表示する。P3-030Fはnonlocal modeで次の1 roleだけをrequest preview、17 fieldの明示確認、workflow-owned canonical hash receipt、1回のexecuteへ拘束する。direct P2 confirmationだけではworkflow上は未確認で、期限切れとreconciliationはtyped停止する。P3-030Gは同じproduction wrapperを固定5 roleで通すfirst-class deterministic production-workflow qualification harnessとsanitized self-hashed canonical manifestを提供する。deterministic fixtureはactual-live/provider qualificationではない。current qualificationはcurrent canonical V2 live manifest、bound deterministic evaluation、public preflightの共通規則だけで判定する。`local_only`はprocessを開始せず、自動確認・再確認、一括・並列、retry、skip、fallbackはない。 |
| `confirmed_natural_language_review_intake` | **implemented** | 呼出側が作成した完全な候補を利用者がsource/candidate hashで明示確認した場合に限り、固定LocalProvider・限定tool・検証済みterminal reportへ接続する。自然言語の意味抽出やsite parserを実装したという意味ではない。 |
| `process_sandbox` | **unavailable** | 構造的hard capはあるがOS-level CPU/memory sandboxはない。 |
| `parallel_deliberation_and_tool_retry` | **disabled** | budget fieldは存在するが、通常のorchestrator経路は並列round/retryを実行しない。 |
| `runtime_conformance_contract` | **implemented** | P2-025Aの役割inventory、assignment/context/resultのversioned contract、pure比較、verified Python product projectionを提供する。実行bridgeではない。 |
| `local_only_runtime_mode` | **implemented** | API key、ChatGPT/Codex login、外部model、networkなしでdeterministic parser/calculator、LocalProvider、storage、replay、evaluation、verified report projectionを利用し、model/nonlocal runtimeを開始しない。 |
| `bounded_codex_river_review_bridge` | **implemented** | P2-025B実装はterminal verification済みP3-030C run、固定5 role、fresh serial read-only turn、strict result、durable replayに限定される。candidate-bound historical live evidenceはbytes不変で保存するがcurrent authorityではない。current qualificationの唯一の正は、current canonical pathのstrict canonical V2 live manifestとbound deterministic evaluationのpairに対するpublic preflight結果である。 |
| `codex_subscription_bounded_river_review` | **implemented** | 保存済みChatGPT loginを使う明示経路はconfigured provider `openai` / auth boundary `chatgpt`を分離し、API keyやfallbackを使わない。canonical pairの両方欠落は`UNKNOWN`、片方だけの欠落、noncanonical、invalid、untrackedまたはcurrent-tree binding不一致は`FAIL`、pairが揃い全binding checkに合格した場合だけ`subscription_live_qualified=true`である。 |
| `openai_api_bounded_river_review_adapter` | **disabled** | **Qualification: deterministic contract only / live-unqualified.** optional `openai_api` adapterはno-network contract test済みだが、price authority/hard cost stopがなく、process/network起動前にfail closedする。subscription qualificationをAPIへ拡張しない。 |
| `codex_python_runtime_bridge` | **unavailable** | 一般Codex/Python bridgeはない。P2-025Bの別名bounded river-only bridgeを広いinteroperabilityへ拡張しない。 |
| `local_data_lifecycle_policy` | **implemented** | P2-027Aのstrict versioned policy、canonical hash、pure lifecycle evaluationを実装する。filesystem mutationは行わない。 |
| `local_data_cleanup_executor` | **implemented** | P2-027BのPython APIは、承認済み1 runに対するbounded quarantine、遅延staged delete、immutable receipt/tombstone、revision CAS、idempotency、read-only reconciliationを実装する。cleanup CLI、automatic retry、secure eraseは実装しない。 |
| `immutable_revision_storage_foundation` | **implemented** | P2-012Aのimmutable revision、manifest、transaction、lock、recovery claim、revision CAS基盤と、P2-010Bの内部revision-only phase transition authorization seamを実装済み。通常のproduct runには未接続。 |
| `product_integrated_durable_run` | **implemented** | P2-012Bのmarker-last terminal publication、verified product reader/status、approval-checkpoint resume、read-only flat-v1 adapter、copy-only migration、durable budget settlement、lifecycle metadata integrationを実装済み。 |
| `offline_evaluation_harness` | **implemented** | P3-017Aのstrictなoffline dataset、決定的exact-evidence scorer、provenance binding、再現可能なresult artifactを、外部実行なしで提供する。 |
| `phase_1_hardening` | **implemented** | typed tool contract、contract v2の数値区分、実行時verification、ローカルoracle/metamorphic testを実装済み。 |

ここでrole confirmationは、previewされた全fieldへの利用者の明示的一致をworkflow receiptへ
hash束縛する手続です。利用者の本人認証、戦略判断の承認、外部第三者検証、model/providerの
現在資格を意味しません。

P3-030Eの`verified FinalReport`とreport projectionは、repository-ownedなschema、hash、
workflow/bridge linkage、state、numeric-contractの検査に合格したことを意味します。戦略品質、
実戦rangeの正確性、GTO/equilibrium、外部solver一致、第三者認証、release readinessは証明しません。

P3-030Gは、fresh production previewの17 fieldをfixture管理のlocal authorityでproduction confirmへ
渡し、confirm時のzero executionと、その後のsingle-role executeを固定順の5 roleすべてで検査します。
role実行だけをprivateなdeterministic read-only executor seamへ差し替え、外部model/provider/network/
credentialを使いません。生成する`SanitizedBoundedRiverReviewWorkflowQualificationManifestV1`は
`transport_qualification="deterministic_fixture"`、`live_qualification_status="UNKNOWN"`、
`api_live_executed=false`、`api_production_qualified=false`を保持します。これは人間の本人確認、
actual-live/provider qualification、戦略品質、GTO/equilibriumを証明しません。別のlive qualificationには
固定5 roleそれぞれのfresh previewと人間による明示確認が必要です。

## 22 tools、Codex 9役、Python 7役

- **FACT**: `default_registry()`と`tools/manifest.yaml`には`22`個のtool名があり、計算または
  capability照会の実行単位を表す。
- **FACT**: `.codex/agents/`の`9`定義はCodexネイティブの役割である。orchestratorと開発専用
  calculator builderを含む。
- **FACT**: `ROLE_CATALOG`の`7`役はPython orchestratorが分析を配分する役であり、Codexの
  9定義と同じ一覧ではない。
- **FACT**: `LocalProvider`はこれら7役へ文章的な専門分析を供給せず、空の結論と制限を返す。
  明示的に注入する`DeterministicMockProvider`はテスト用であり、外部モデル能力ではない。
- **FACT**: 通常のPython orchestratorは`.codex/agents/*.toml`を起動しません。開発用Codex
  sub-agent実行も`AgentExecutionRecord`やPython run artifactsへ自動的には取り込まれません。
- **FACT**: P2-025Aは両実行面の意味を別schemaで比較できるが、片方を起動したり、他方の監査記録を
  捏造したりしない。Codex側のtool catalogが宣言されていない場合は、空の権限を含め
  `undeclared`として保持する。
- **FACT**: P2-025B限定bridgeだけは、terminal verification済みP3-030C river run、固定5 role、
  empty tool allowlist、fresh serial thread、mode-bound confirmation/resultへ限定した別artifact familyを
  提供する。candidate-bound historical evidenceは保存するがcurrent authorityではない。current qualificationの
  唯一の正はcurrent canonical V2 live manifest、bound deterministic evaluation、public preflightであり、両方欠落は
  `UNKNOWN`、incomplete/invalid/binding不一致は`FAIL`、全binding check合格時だけ
  `subscription_live_qualified=true`である。通常経路は別実行面のままであり、
  一般bridgeではない。P3-030Fは、この既存bridgeの次のroleだけをworkflow plan、linkage、current bridge
  lineageへcross-bindして監督実行するwrapperであり、P2-025Bのrole順、request bytes、confirmation、result、
  replayの意味を変更しない。wrapper確認はexact P2 confirmationとそのexpiryを
  `binding_sha256`付きのworkflow-owned canonical receiptへ結び、receiptのないlower-level direct確認を
  workflow実行許可として扱わない。

この数は品質指標ではありません。contract testは実装から件数を再計算し、文書との差を検出します。

## Game、parser、sandbox境界

- 主対象は事後のNLHE cash/tournament review。リアルタイム助言はfail-closedで拒否する。
- equityはheads-up NLHEだけ。P3-016B bridgeはNLHE cash river、Heroと単一range target、最大990 evaluationsに限定する。ICMは指定したpayout model、小規模gameは明示した有限modelだけを扱う。
- generic free-text parserは文書化key-value grammarだけを扱い、不明行を警告として保存する。
- bounded Japanese parserはP3-030B version 1の有限文型だけをexact matchし、余分な行、曖昧性、
  欠落、矛盾、未対応scopeをfail closedで拒否する。
- P3-030Cはそのgrammarをriverのterminal fold履歴だけに絞り、別入力の単一rangeに対する
  heads-up exact equityとzero-rake single-decision call EVだけを比較する。複数range、multiway、
  earlier street、all-in、side pot、rake、ante、将来actionは拒否する。
- P3-030DはP3-030CとP2-025Bを再実装せず、明示確認、状態保存、部分完了からのresume、provider/modelを
  再実行しないartifact replayだけを追加する。P3-030Eの表示も既存artifactのpure projectionである。
  P3-030Fのnonlocal wrapperはstatusの`next_role` / `role_state`に従い、各roleをpreview、全field確認、
  canonical confirmation receipt、1回のexecuteへ直列化する。statusはrequest/confirmation expiryと
  `expired` / reconciliation停止も投影する。admission・完了済みroleにreceiptがなければfail closedし、
  既存P3 `FinalReport`、parser/calculator、単一range、7-tool exact semantics、P2-025B authorityは不変である。
- P3-030GはP3-030D/Fのproduction compositionを、repository-owned deterministic fixtureとread-only
  transport seamだけでqualificationする。固定case/metric、terminal status、replay、report、lineage、hash、
  sanitized canonical manifestを検査するが、live model/providerを実行またはqualifyせず、deterministic
  evidenceをcurrent live資格へ昇格しない。
- default canonical toolはpayload/work/output capに加え、1つのdirect childへ隔離したwall deadlineと
  terminate/kill確認を持つ。ただしprocess-tree、CPU、memoryのOS sandboxではなく、明示的な
  `phase_isolated=False` custom diagnostic toolはin-processのままである。
- solver実行、収束、対象game/rake/stackの一致がない結果をGTO・均衡・正確なrangeと表示しない。

## 品質とplatform

user Quickstartの`pip install -e .`とは別に、CI-equivalentな開発環境を次のlocked installで準備します。
作成済みvenvのPythonを明示し、別のglobal Pythonへdependencyを入れないようにします。

```powershell
$python = ".\.venv\Scripts\python.exe"
& $python -m pip install --disable-pip-version-check -r requirements.lock
& $python -m pip install --disable-pip-version-check --no-deps -e .
```

setup後のcanonical quality gateは次の4コマンドです。

```powershell
& $python -m pytest
& $python -m ruff check .
& $python -m ruff format --check .
& $python -m mypy src
```

POSIXでは同じvenvを次のように指定します。

```bash
python="./.venv/bin/python"
"$python" -m pip install --disable-pip-version-check -r requirements.lock
"$python" -m pip install --disable-pip-version-check --no-deps -e .
"$python" -m pytest
"$python" -m ruff check .
"$python" -m ruff format --check .
"$python" -m mypy src
```

pytestの既定tempは`tests/conftest.py`により、ワークスペース内のignoredな
`.pytest-tmp/s-<process-hex>-<nonce>/`へ分離します。呼出側の明示`--basetemp`は上書きしません。
固定共有ディレクトリを再利用しないため、並行sessionが互いのtempを開始時に削除しません。

- **FACT**: package metadataは`requires-python >=3.11`である。これは単独では検証済みmatrixを意味しない。
- **FACT**: tracked GitHub Actions matrixはWindows/Ubuntu × CPython 3.12/3.13の各組合せを
  3つのdeterministic file shardへ分け、各test fileをfresh pytest processで実行する。3 shardの和が
  そのOS/Python組合せのfull suiteを重複・欠落なく覆う。Windows 3.13でstatic gate、Ubuntu 3.13で
  reproducible package evidenceも実行する。
  2026-08-12 20:06:17 JSTのfresh read-backでは、commit
  `ad3f267345491651153a18be12e854632366e34a`のActions run `31583851426`で、Windows/Ubuntu ×
  Python 3.12/3.13のfull-test 4 row、static quality、package evidenceの全6 jobが成功した。
  これは同commitだけに対する証拠であり、public releaseまたはproduction readinessを証明しない。
  run `31462799513`の6-job成功はcommit `fcbf4b8eb51a1a3e91a11313ed85f481f631f0bf`だけに対する
  historicalかつcommit-specificな証拠である。
- **FACT**: RM-018Aのbuild/install、license inventory、artifact SHA-256、offline preflightを
  candidate commitへ結ぶ実装とworkflowが存在する。任意checkoutの`doctor`はcandidate evidenceではない。
- **FACT**: 自動temp名はWindowsのpath消費を抑えるため短縮し、session固有性を維持する。
- **UNKNOWN**: 深いclone先・深い明示`--basetemp`・long-path設定の異なるWindows環境。これらは
  `FileNotFoundError`等のOS path制約に影響され得るため、常時対応とは表示しない。
- **UNKNOWN**: この文書を読む任意のworking treeに対応するcandidate-specific run結果、公開artifact、release、tag。
- **UNKNOWN**: coverage thresholdは人間承認値がないため、現在のbaselineでは設定しない。

公開判断は[公開前チェックリスト](public-release-checklist.md)を参照してください。

## P2-028A Windows限定capability

| capability ID | 状態 | 実行上の意味 |
|---|---|---|
| `repository_synthetic_isolated_job_control` | **implemented** | Windows Job Objectで、固定repository synthetic helperだけをapproval/context/budget/identityに拘束し、hard stop、resource/output cap、durable cancellation/reconciliationを提供する。汎用process sandboxではない。 |

`process_sandbox`は引き続き**unavailable**である。通常tool、任意外部コード、provider、solver、
remote process、network isolationへこの限定capabilityを拡張しない。詳細は
[隔離ジョブ制御契約](isolated-job-control.md)を参照する。
