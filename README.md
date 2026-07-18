# poker-deliberation-framework

監査可能・再現可能なポーカー問題検討MVPです。No-Limit Texas Hold'emのキャッシュと
トーナメントを主対象に、Codexカスタムエージェント、決定的なPython状態機械、ローカル計算、
承認台帳、run artifactsを組み合わせます。

APIキーなしで、doctor、スキーマ検証、ポットオッズ、ポット再構成、コンボ、heads-up equity、EV tree、ICM、
小規模ゼロ和行列ゲーム、固定相手戦略へのbest response、ハンド検証、感度分析、全テストが
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

任意のOpenAI Agents SDK連携を後で実装・試験する場合だけ、次を使います。MVPはユーザーデータを
外部送信しません。

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
自由文ハンドは `key: value`、`player: id, position, stack`、
`action: street, actor, action, amount[, to_amount]` の保守的な形式だけを正規化します。
承認待ちのCLIはレポートを出力して終了コード3を返します。
正常完了は0、入力・計算失敗および`failed_with_limitations`は2です。

## 主張の数値検証

CaseInputの `metadata.claim_checks` はUSER_CLAIMの値を計算出力と比較します。
`examples/wrong_pot_odds_case.json` は「pot 100に50 bet、50 callの必要equityが33.3%」という
誤りを、厳密計算25%で訂正する例です。

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

Python MVPは常に`LocalProvider`を使い、モデルへ外部送信しません。
`POKER_DELIBERATION_PROVIDER`は`local`だけを許可し、モデル名・推論強度の環境設定は未対応として
エラーにします。外部providerは、承認と統合テスト後に`Orchestrator(provider=...)`へ明示注入します。
成果物の保存先だけは`POKER_DELIBERATION_RUNS_DIR`でワークスペース内に変更できます。

## 状態と成果物

状態はINTAKEからCOMPLETEDまたはFAILED_WITH_LIMITATIONSまで明示的に遷移します。無制限ループは
なく、討論回数、ツール再試行、実行時間、並列数を設定で制限します。

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

既定では共通の秘密キーとAPI-key/token形式をredactします。任意の個人情報まで完全検出する
ものではないため、入力へ秘密情報を含めないでください。

## 品質チェック

```powershell
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe src
```

テストはunit、property、integration、golden、adversarialに分かれます。

## 外部依存関係

- Pydantic 2.x — MIT — 実行時の型付きスキーマ。
- OpenAI Agents SDK — MIT — 任意。MVP既定経路では未導入・未送信。
- pytest / pytest-cov / PyYAML / Ruff / mypy — MIT、Hypothesis — MPL-2.0 — 開発時のみ。
- setuptools / wheel — MIT — 固定されたbuild時依存。

依存関係の固定結果は `requirements.lock` に記録します。外部ソルバー、カード評価バイナリ、
有料レンジは同梱していません。

## 文書

- [Architecture](docs/architecture.md)
- [Agent protocol](docs/agent-protocol.md)
- [Calculation policy](docs/calculation-policy.md)
- [Source policy](docs/source-policy.md)
- [Security](docs/security.md)
- [Limitations](docs/limitations.md)
- [Independent review remediation](docs/review-remediation.md)

## ライセンス

このリポジトリ独自コードはMIT Licenseです。
