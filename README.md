# poker-deliberation-framework

監査可能・再現可能なポーカー問題検討MVPです。No-Limit Texas Hold'emのキャッシュと
トーナメントの事後検討を主対象に、Codexカスタムエージェント定義、決定的なPython状態機械、
ローカル計算、承認台帳、run artifactsを組み合わせます。既定の`LocalProvider`は文章的な
専門分析やモデル推論を生成しません。

Codexネイティブ層とPythonオーケストレーター層は別実行面です。Python CLIがCodexのsub-agentを
起動したり、Codexでの実行をPythonのrun artifactsへ自動記録したりする統合bridgeはありません。

RM実装状態の正はpackage resourceとして設定したtracked JSON
[`src/poker_deliberation/roadmap_status.json`](src/poker_deliberation/roadmap_status.json)です。
source/editable checkoutでの読込はcontract test対象ですが、wheel/sdist同梱はRM-018Aまで`UNKNOWN`です。
人間向け一覧は[`docs/roadmap-status.md`](docs/roadmap-status.md)、Phase 2実装前contractは
[`docs/phase2-readiness-contracts.md`](docs/phase2-readiness-contracts.md)を参照してください。
`user_materials/ROADMAP.md`は承認方針・背景説明であり、runtimeやdoctorの入力ではありません。

P2-010AのPython run経路は、strict versioned request/outcomeを使うpure phaseとserialな
Analysis/ToolResearch effect境界へ分割されています。state transitionとartifact writeは引き続き
Orchestratorだけが所有します。内部contractと非目標は
[`docs/phase-services.md`](docs/phase-services.md)を参照してください。

Python providerへのrole別contextは、P2-024Aの
[`Context lifecycle contract`](docs/context-lifecycle.md)に従い、試行ごとのstrict immutable envelopeで
allowlist、UTC期限、classification、integrity、run/assignment/attempt/runtime系譜を検証してからfresh
`AgentContext`として渡します。envelope/payloadの新規永続化、retention期間、削除、cleanup、外部送信、
Codex/Python bridgeは追加していません。

**FACT**: canonical SSOTではRM-024/P2-024AとP2-010Aを`completed`としている。
RM-010はP2-010B待ちの`in_progress`、P2-010Bは別承認がないため`not_started`である。

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
poker-deliberate resume RUN_ID --reject APPROVAL_ID --reason "外部実行を許可しない"
```

各コマンドは `--format json` または `--format markdown` を受け付けます。計算入力と完全な
ToolResultは `runs/<run_id>/tool_results/` に保存され、再現コマンドがレポートに入ります。
新しいcontract version 2では、旧3値`exactness`を互換用に残し、`numeric_exactness`で
`exact` / `exact-under-model` / `floating-verified` / `approximate` / `unavailable`を区別します。
20 toolのstrict input/output schema、前提、上限、単位、toleranceは
[生成済みtool contracts](docs/tool-contracts.md)を参照してください。
自由文ハンドは `key: value`、`player: id, position, stack`、
`action: street, actor, action, amount[, to_amount]` の保守的な形式だけを正規化します。
承認待ちのCLIはレポートを出力して終了コード3を返します。
正常完了は0、入力・計算失敗および`failed_with_limitations`は2です。

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
成果物の保存先だけは`POKER_DELIBERATION_RUNS_DIR`でワークスペース内に変更できます。

## 状態と成果物

状態はINTAKEからCOMPLETEDまたはFAILED_WITH_LIMITATIONSまで明示的に遷移します。実行時間と
artifact/output sizeの上限は通常経路で強制します。討論round、tool retry、concurrencyのbudget
fieldは存在しますが、通常のorchestrator経路には未接続で`disabled`です。

各runには次を保存します。

```text
runs/<run_id>/
  input.json
  normalized_case.json
  assumptions.json
  assignments.json
  agent_reports/
  agent_execution_records.json
  security_events.json
  tool_results/
  evidence.jsonl
  disputes.json
  approvals.json
  state.json
  final_report.md
  final_report.json
```

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
- [Offline public release checklist](docs/public-release-checklist.md)
- [RM status projection](docs/roadmap-status.md)
- [Phase 2 readiness contracts](docs/phase2-readiness-contracts.md)
- [Independent review remediation](docs/review-remediation.md)

## ライセンス

このリポジトリ独自コードはMIT Licenseです。
