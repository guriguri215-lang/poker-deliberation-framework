# poker-deliberation-framework

監査可能・再現可能なポーカー問題検討MVPです。No-Limit Texas Hold'emのキャッシュと
トーナメントの事後検討を主対象に、Codexカスタムエージェント定義、決定的なPython状態機械、
ローカル計算、承認台帳、run artifactsを組み合わせます。既定の`LocalProvider`は文章的な
専門分析やモデル推論を生成しません。

Codexネイティブ層とPythonオーケストレーター層は別実行面です。Python CLIがCodexのsub-agentを
起動したり、Codexでの実行をPythonのrun artifactsへ自動記録したりする統合bridgeはありません。
P2-025Aは、この分離を維持したまま役割inventory、assignment/context/result、許可、承認、
execution auditをversioned schemaで比較し、検証済みPython productを加算的にprojectionします。
詳細は[`docs/runtime-conformance-contract.md`](docs/runtime-conformance-contract.md)を参照してください。

P3-017Aはrepository-owned MIT synthetic fixture、strict canonical dataset/scorer/result、
deterministic exact-evidence scoringを使うoffline integrated evaluation harnessです。
network、provider、external solver、runtime bridge、product run storageを起動・変更しません。
詳細は[`docs/evaluation-contract.md`](docs/evaluation-contract.md)を参照してください。

RM実装状態の正はpackage resourceとして設定したtracked JSON
[`src/poker_deliberation/roadmap_status.json`](src/poker_deliberation/roadmap_status.json)です。

P2-027A は versioned local-data classification/retention/expiry policy と pure disposition
evaluation を提供します。P2-027B の additive Python API は、明示1 run・verified ownership・
exact destructive approval・CAS を条件に、same-volume quarantine と30日後の別承認による
staged delete、receipt/tombstone、read-only reconciliation を提供します。cleanup CLI、
secure erase、automatic retry は実装しません。詳細は
[`docs/local-data-policy.md`](docs/local-data-policy.md)と
[`docs/local-data-cleanup.md`](docs/local-data-cleanup.md)を参照してください。
source/editable checkoutはcontract test対象です。wheel/sdistの収載・runtime読込はpackage-data
artifact smokeで候補ごとに別途検証し、その結果だけでrelease candidate判定とはしません。
matrix・licenseを含む全体判定はRM-018Aのままです。
人間向け一覧は[`docs/roadmap-status.md`](docs/roadmap-status.md)、Phase 2実装前contractは
[`docs/phase2-readiness-contracts.md`](docs/phase2-readiness-contracts.md)を参照してください。

P2-010AのPython run経路は、strict versioned request/outcomeを使うpure phaseとserialな
Analysis/ToolResearch effect境界へ分割されています。state transitionとartifact writeは引き続き
Orchestratorだけが所有します。内部contractと非目標は
[`docs/phase-services.md`](docs/phase-services.md)を参照してください。

Python providerへのrole別contextは、P2-024Aの
[`Context lifecycle contract`](docs/context-lifecycle.md)に従い、試行ごとのstrict immutable envelopeで
allowlist、UTC期限、classification、integrity、run/assignment/attempt/runtime系譜を検証してからfresh
`AgentContext`として渡します。envelope/payloadの新規永続化、retention期間、削除、cleanup、外部送信、
Codex/Python bridgeは追加していません。

P2-011Aでは、strictなbudget schema、明示的v1 migration、注入可能なmonotonic clock、serial usage
accounting、retry classification、typed deadline/cancellationを追加しています。external cost cap 0でも
free local providerと決定論calculatorは利用でき、parallel実行とautomatic retryは行いません。詳細は
[`docs/budget-execution-contract.md`](docs/budget-execution-contract.md)を参照してください。

P2-011Bでは、P2-012Aのimmutable revision/CASを利用する専用rootに、strictな
`budget_state.json`、resource reservation、settlement、resume検査、bounded concurrency、
typed retry、cooperative cancellation、RM-028 evidence interfaceを追加しています。これは
`poker_deliberation.budgets.durable_*`の内部APIです。P2-012Bの通常product経路は、この専用budget
rootとterminal revision rootを束縛し、publication前のreservationとpointer publication後の
settlementを検証します。通常経路のprovider/tool実行は引き続きserial、automatic retry 0です。

**FACT**: milestone/RMの公開状態と技術契約の正は、schema 5.0のpublic projectionである
[`src/poker_deliberation/roadmap_status.json`](src/poker_deliberation/roadmap_status.json)です。
このprojection単体はcandidate固有のcommitやtest実行を証明しません。status更新は同一schema
更新検証、参照path/testのtracked検証、repository gateを別途要求します。
RM-010、RM-011、RM-012、RM-013、RM-024、RM-027とP2-013Bは`completed`です。
RM-029/P2-029Aはoffline Python product pathの安全性・数値検証・利用者向けsummary・dogfoodを
完了し、`completed`です。RM-025は外部作用前のruntime意味整合を優先するためP1ですが、
P2-025Aのconformance-only contract完了後も実bridgeは未実装のため`in_progress`とdecision gateを
維持します。RM-017はP3-017A offline exact-evidence harnessを完了しましたが、主観的metric、
外部dataset、人間rubricが未実装のため`in_progress`を維持します。
RM-028は`proposed`、P2-028Aは`not_started`であり、
RM-019/RM-020の外部provider/solver実行は開始していません。
P2-029Aの詳細contractは
[`docs/offline-product-path.md`](docs/offline-product-path.md)を参照してください。

P2-010Bは、すでに計算済みのphase traceを再検証し、専用revision rootへ
`structural_nonterminal` revisionをpublishしてから、同一processの非直列化authorizationで
`FINAL_SYNTHESIS`から`COMPLETED`へのin-memory transitionを適用する内部opt-in seamである。
このP2-010B seam自体は通常経路へ接続しません。通常の`run`、`resume`、`show`、`load_report`、
`report_path`はP2-012Bの別terminal protocolを使用します。cleanup、external provider/solver、
GTO・均衡・正確なrangeの主張は追加しません。

APIキーなしで、doctor、スキーマ検証、ポットオッズ、ポット再構成、コンボ、heads-up equity、EV tree、ICM、
小規模ゼロ和行列ゲーム、固定相手戦略へのbest response、ハンド検証、感度分析、品質テストが
動きます。外部ソルバーがなければ、偽の均衡結果ではなく明示的なUnavailableを返します。
effective stack、SPR、MDF、レーキ、レーキ込みcall EV、bluff EV、polar river bluff fraction、
Bayes更新も、仮定をToolResultへ明記する決定論ツールとして利用できます。

## セットアップ

Python 3.11以上を用意し、PowerShellで次を実行します。パッケージ取得は外部操作なので、
組織の承認・ネットワーク方針に従ってください。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
```

次の任意依存は将来のOpenAI Agents SDK adapter開発用です。現在の
`OpenAIAgentsProvider.analyze`は未実装であり、SDKとAPI keyが存在してもproviderは`disabled`、
`available=false`です。MVPはユーザーデータを外部送信しません。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[agents]"
```

## 最初の確認

```powershell
poker-deliberate doctor
poker-deliberate list-tools
poker-deliberate list-agents
```

現在の開発環境でeditable install前に実行する場合は、`$env:PYTHONPATH="src"` を設定し、
`python -m poker_deliberation` を `poker-deliberate` の代わりに使えます。

`doctor`の最上位`status=ok`は診断が完了した意味です。個別能力の`implemented / disabled /
unavailable / planned`は[能力matrix](docs/capabilities.md)と`capabilities`出力を確認してください。

## CLI

```powershell
poker-deliberate review-hand --file examples\valid_hand.json
poker-deliberate review-hand --file examples\free_text_hand.txt
poker-deliberate review-strategy --file examples\wrong_pot_odds_case.json
poker-deliberate audit-claim "この主張は検証条件なしでは真偽不明です"
poker-deliberate calculate pot_odds --analysis-scope retrospective --input examples\pot_odds_input.json
poker-deliberate calculate icm --analysis-scope retrospective --input examples\icm_input.json
poker-deliberate calculate fixed_strategy_best_response --analysis-scope retrospective --input examples\best_response_input.json
poker-deliberate show RUN_ID
poker-deliberate show RUN_ID --format summary
poker-deliberate resume RUN_ID --reject APPROVAL_ID --reason "外部実行を許可しない"
```

各コマンドは `--format json` または `--format markdown` を受け付けます。
`review-hand`、`review-strategy`、`audit-claim`、`show`、`resume`では、保存artifactを増やさない
追加projectionとして`--format summary`も選択できます。計算入力と完全な
ToolResultは `runs/<run_id>/tool_results/` に保存され、再現コマンドがレポートに入ります。
新しいcontract version 2では、旧3値`exactness`を互換用に残し、`numeric_exactness`で
`exact` / `exact-under-model` / `floating-verified` / `approximate` / `unavailable`を区別します。
20 toolのstrict input/output schema、前提、上限、単位、toleranceは
[生成済みtool contracts](docs/tool-contracts.md)を参照してください。
自由文ハンドは `key: value`、`player: id, position, stack`、
`action: street, actor, action, amount[, to_amount]` のversion 1保守形式だけを正規化します。
exact source bytesとcanonical handのSHA-256、parser ID/version、安定診断コードは
[`normalization.json` contract](docs/normalization-contract.md)に従います。対応siteは`none`で、
自然言語およびsite-specific hand historyは解析しません。
承認待ちのCLIはレポートを出力して終了コード3を返します。
正常完了は0、入力・計算失敗および`failed_with_limitations`は2です。

P3-017A offline evaluationは、固定したcommit/tree IDとignored `tmp/` outputを明示して実行します。

```powershell
.\.venv\Scripts\python.exe scripts\generate_offline_evaluation_fixtures.py --check
.\.venv\Scripts\python.exe scripts\run_offline_evaluation.py `
  --source-commit COMMIT_SHA `
  --source-tree TREE_SHA `
  --output tmp/goals/P3-017A/evaluation-runs/result.json
```

score `1.0` / threshold `1.0`は宣言済み10 caseのexact evidence一致だけを意味し、未実装の
主観的戦略metricやGTO・均衡品質を評価しません。

## 主張の数値検証

CaseInputの `metadata.claim_checks` はUSER_CLAIMの値を計算出力と比較します。
`examples/wrong_pot_odds_case.json` は「pot 100に50 bet、50 callの必要equityが33.3%」という
誤りを、tool固有ULP policyで検証済みの浮動小数計算25%で訂正する例です。

```powershell
poker-deliberate review-strategy --file examples\wrong_pot_odds_case.json --format markdown
```

## Codex統合

Codexでこのリポジトリをワークスペースとして開くと、`AGENTS.md`の恒常ルールが適用されます。
最初は、次のプロンプトの`{ここにハンド履歴を貼り付ける}`を実際の履歴へ置き換えて実行できます。

```text
このプロジェクトのAGENTS.md、同梱Skill、計算ツール、エージェント構成を理解し、
$review-poker-handを使って、レビューに必要なエージェントだけを呼び出してください。
次のポーカーのハンド履歴を正規化し、カード、スタック、アクション、ポットを再構成したうえで、
私の判断とプレイ内容が妥当だったかレビューしてください。

【ハンド履歴】
{ここにハンド履歴を貼り付ける}

入力にない情報やレンジを捏造せず、数値はテスト済みのローカル計算ツールで検証してください。
ハンドレンジが用意されておらず、レビューに外部レンジが本当に必要な場合は、GitHubまたはWeb上の
取得候補を調査し、出典URL、ライセンス、対象ゲーム、スタック、レーキ、更新日、現在のハンドとの
条件差、保存予定ファイルを先に提示してください。ライセンス上利用可能であることを確認し、
私が明示的に承認するまではダウンロード、外部送信、パッケージ導入を行わないでください。
適切な外部レンジがない場合は、その理由を示し、ASSUMPTIONとして複数レンジの感度分析を提案してください。

FACT / CALCULATED / INFERENCE / ESTIMATE / ASSUMPTION / USER_CLAIM / UNKNOWNを区別し、
ソルバー実行と収束確認なしにGTO、均衡、正確なレンジと断定しないでください。
結論、根拠、仮定、不明点、使用したツールとエージェント、再現コマンドを日本語で報告してください。
```

短く依頼する場合は「`$review-poker-hand`を使い、不明な情報を捏造せずこのハンドをレビューして」
でも実行できます。計算だけなら`$run-poker-calculation`、主張監査なら`$audit-poker-claim`を指定します。

すぐにコピーして試せる日本語プロンプトは
[Codex実行プロンプト例](docs/example-prompts.md)にまとめています。サンプルハンドのレビュー、
決定的計算、主張監査、不完全なハンドへの質問、手持ちハンド用テンプレートを収録しています。

### ユーザー資料

公開リポジトリへ含めたくない手持ちの資料や自作ツールは、`user_materials/`へ置けます。Codexは依頼に関連する資料がある場合に限り、必要な範囲を参照します。このフォルダでは`README.md`と`.gitignore`だけが追跡対象で、後から追加したファイルとサブフォルダはすべてGitから無視されます。

資料は未検証のユーザー提供情報として扱われます。コード、マクロ、実行ファイルは自動実行せず、内容を外部サービスへ送信せず、公開情報として引用・再配布しません。APIキーなどの秘密情報は、Gitから無視されていても置かないでください。

- `AGENTS.md`: Codexが自動適用する短い恒常ルール
- `.codex/config.toml`: プロジェクト設定
- `.codex/agents/*.toml`: 9体のエージェント定義。`developer_instructions`が各役の指示本体
- `.agents/skills/*/SKILL.md`: 3つの再利用可能な手順書
- `.agents/skills/*/agents/openai.yaml`: UI表示情報とコピー可能な`default_prompt`

`.codex/config.toml` は直接の子エージェントだけを許可する `max_depth = 1`、同時上限5を設定します。
分析役はread-onlyで、`calculator-builder` だけworkspace-writeです。

`poker-deliberate list-agents`はCodexの9体ではなく、Pythonオーケストレーターが内部配分する
7役を表示します。対応は次のとおりです。

| Codexエージェント | Python役割 | 備考 |
|---|---|---|
| `poker-orchestrator` | `Orchestrator` | Pythonでは配分主体であり、分析役一覧には含まれない |
| `intake-reconstructor` | `intake` | 入力再構成 |
| `strategy-analyst` | `strategy-analyst` | 戦略分析 |
| `math-tool-auditor` | `math-auditor` | 数学・ツール監査 |
| `evidence-researcher` | `evidence-researcher` | 証拠調査 |
| `skeptic-falsifier` | `skeptic` | 反証 |
| `adjudicator` | `adjudicator` | 裁定 |
| `report-writer` | `report-writer` | レポート生成 |
| `calculator-builder` | なし | 開発時だけ使うコード変更役 |

この表は役割名の対応であり、同一ランタイムの証明ではありません。Codex側はCodexのagent機構で、
Python側は`AgentProvider`境界でそれぞれ実行されます。現在のPython既定経路は`LocalProvider`です。

Python MVPは常に`LocalProvider`を使い、モデルへ外部送信せず、文章的な専門分析を生成しません。
`POKER_DELIBERATION_PROVIDER`は`local`だけを許可し、モデル名・推論強度の環境設定は未対応として
エラーにします。外部providerは、承認と統合テスト後に`Orchestrator(provider=...)`へ明示注入します。
`from_env()`ではlegacy、product revision、durable budgetの保存先をそれぞれ
`POKER_DELIBERATION_RUNS_DIR`、`POKER_DELIBERATION_REVISION_RUNS_DIR`、
`POKER_DELIBERATION_DURABLE_BUDGET_RUNS_DIR`で変更できます。3 rootはすべてワークスペース内かつ
相互に非重複でなければなりません。

## 状態と成果物

状態はINTAKEからCOMPLETEDまたはFAILED_WITH_LIMITATIONSまで明示的に遷移します。strict v2
budgetはactive runtime、external micro-USD、provider/tool/artifact/runのbyte上限、analysis batch、
serial peak concurrencyを通常経路で検証します。tool retry数は分類上の候補上限であり、通常経路は
automatic retryを実行しません。

通常の新規runは、flat-v1 rootではなく専用terminal revision rootへ保存します。

```text
.poker-run-revisions/
  ownership.json
  runs/<run_id>/.terminal-store/
    current.json
    transactions/
    revisions/r<revision>-<transaction_id>/
      transaction.json
      manifest.json
      completion.json
      payload/
        input.json
        state.json
        final_report.json
        final_report.md
        ...
```

`completion.json`はterminal revision内の最後のdata artifactであり、全payload・manifest・markerを
再検証してから`current.json`をCAS更新します。`succeeded`だけがpublic
`run_status=completed`になります。`approval_required` checkpointだけがresume可能です。
`failed`、`cancelled`、`cancel_unconfirmed`もmarker付きterminal revisionとして保存しますが、
public successにはなりません。

P2-013B は expired V2 request または historical V1 pending request に対する明示的な
`ApprovalReissueBatchV2` を受け付け、元 request を superseded/reissued projection、
完全な successor を pending とする新しい `approval_required` revision を CAS 公開します。
`poker-deliberate resume RUN_ID --reissue-file REISSUE.json` は decision construction option と
併用できません。reissue がある run だけ `approval_reissues_v2.jsonl` を追加し、reissue がない
既存 V2 checkpoint の3 control artifact byte contractは維持します。

`recheck_approval_for_execution` は approval run revision/pointer/manifest、exact approved action、
decision record、live provider/actor/scope/expiry/revocationを effect なしで再検証し、短命の immutable
bindingを返します。external action、provider/solver、process isolation、durable effect lifecycle は
実装しません。

既存の`runs/<run_id>/`はread-only flat-v1 namespaceです。exact `b"v1\n"` sentinelと現行schemaを
検証できた場合も`legacy_unverified`であり、completedやresumableへ昇格しません。
`Orchestrator.migrate_legacy_run`は明示的quiescence確認の下で別run IDへexact byte copyを作りますが、
copy先もmissing guaranteesを保持した`legacy_unverified`です。元runは変更しません。

`agent_execution_records.json`の既存`context_sha256`は従来の完全な`AgentContext` hash計算を維持します。
P2-024Aはcontext/attemptとsparse payload/source/policy/integrity/runtime/expiryの監査metadataを
任意fieldとして追加し、旧artifactの読込・hash値互換性を維持します。

既定では共通の秘密キーとAPI-key/token形式をredactします。任意の個人情報まで完全検出する
ものではないため、入力へ秘密情報を含めないでください。

## 品質チェック

次のbare commandは開発用venvを有効化してから実行します。venvを有効化しない場合は、READMEの
セットアップ例と同様に`.\.venv\Scripts\python.exe`等の明示パスを使用してください。

```text
python -m pytest
ruff check .
ruff format --check .
mypy src
```

テストはunit、property、integration、golden、adversarialに分かれます。pytestのtempは
`tests/conftest.py`がワークスペース内のignoredなセッション固有ディレクトリへ設定します。
自動設定名はpath長を抑えた`.pytest-tmp/s-<pid-hex>-<nonce>/`で、呼出側が指定した
`--basetemp`は変更しません。
PowerShellでは次で同じ4 gateを順番に実行できます。system-wide ExecutionPolicyは変更しません。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check_quality.ps1
```

**ASSUMPTION**としてsupported候補はCPython 3.11-3.13、WindowsとUbuntuです。今回ローカルで
実行していないmatrix行は`UNKNOWN`であり、成功とは扱いません。coverage thresholdは人間承認値が
ないため設定していません。

Windowsのpytest結果は、Python versionだけでなくclone先とtemp rootを合わせたpathの深さ、および
OS/processのlong-path設定にも左右されます。今回の確認対象は現在のclone先と短い自動temp、または
明示した短い`--basetemp`に限ります。深いclone/temp pathを常にサポートするとは判断せず、未実行の
組合せは`UNKNOWN`です。

## 公開前のローカル監査

`scripts/public_preflight.py`はtracked worktree、非ignoredなuntracked候補、到達可能なGit履歴の
blobとcommit/tag/ref metadataを外部通信なしで検査し、秘密・PII候補の値を表示せずredacted指紋だけの
JSON/Markdown報告を生成します。objectの読取・parse・decodeまたはref列挙が不完全なら`UNKNOWN`を
保持します。author/committer/tagger identityは機械的候補であり、個人情報の確定判定ではありません。
ignoredな`user_materials/`と`runs/`の内容は自動走査しません。実行方法と人間判断項目は
[公開前チェックリスト](docs/public-release-checklist.md)を参照してください。

## 外部依存関係

- Pydantic 2.x — MIT — 実行時の型付きスキーマ。
- OpenAI Agents SDK — MIT — 任意依存。outbound analyzeは未実装・disabled・未送信。
- pytest / pytest-cov / PyYAML / Ruff / mypy — MIT、Hypothesis — MPL-2.0 — 開発時のみ。
- setuptools / wheel — MIT — 固定されたbuild時依存。

依存関係の固定結果は `requirements.lock` に記録します。外部ソルバー、カード評価バイナリ、
有料レンジは同梱していません。

## 文書

- [Architecture](docs/architecture.md)
- [Capabilities](docs/capabilities.md)
- [Agent protocol](docs/agent-protocol.md)
- [Calculation policy](docs/calculation-policy.md)
- [Source policy](docs/source-policy.md)
- [Security](docs/security.md)
- [Limitations](docs/limitations.md)
- [Budget execution contract](docs/budget-execution-contract.md)
- [Offline evaluation contract](docs/evaluation-contract.md)
- [Offline public release checklist](docs/public-release-checklist.md)
- [RM status projection](docs/roadmap-status.md)
- [Phase 2 readiness contracts](docs/phase2-readiness-contracts.md)
- [Correctness and security hardening](docs/review-remediation.md)

## ライセンス

このリポジトリ独自コードはMIT Licenseです。
