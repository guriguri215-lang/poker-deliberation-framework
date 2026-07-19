# Progress

## 2026-07-19 — Phase 0

- **FACT**: `main` / `b149600e422ad2404a74348650a234f9b8de03bb`、clean worktreeから着手。
- **FACT**: RM-001としてcanonical capability state、doctor出力、能力matrix、文書contractを追加。
- **FACT**: RM-002としてOpenAI outbound providerをSDK/keyの有無にかかわらず`disabled`、
  `available=false`とし、`analyze`を明示的なNotImplemented pathに統一。
- **FACT**: RM-003としてpytest tempをignoredなworkspace-local session directoryへ分離し、
  pathを短縮したsession固有名、明示`--basetemp`尊重、canonical 4 gateとPowerShell runnerを追加。
- **FACT**: RM-008としてtracked/non-ignored worktreeと全Git historyを対象にするoffline preflight、
  解決後worktree path境界、scan不完全時のsecret/PII `UNKNOWN`保持、UTF-16/非対応形式方針、
  redacted fingerprint、synthetic canary分類、ignored-path/history fixture testを追加。
- **FACT**: RM-009としてMarkdown ToolResultのstatus/exactness/assumptions/warnings/seed/samples/
  confidence interval/version/duration/error/reproduction表示と4状態のgolden testを追加。
- **FACT**: provider/Markdown/pytest temp/capability contract/preflightのtargeted testsは38件成功。
- **FACT**: 追加`--basetemp`なしのcanonical `python -m pytest`は164件成功。
- **FACT**: 一意なignored pathを明示`--basetemp`に指定したfull suiteも164件成功し、呼出側の
  指定値を上書きしないcontractを確認。
- **FACT**: `ruff check .`、`ruff format --check .`、`mypy src`が成功。mypy対象は41 source files。
- **FACT**: quality runnerの直接`.ps1`起動はWindows ExecutionPolicyで拒否されたが、system設定を
  変更しないprocess限定`-ExecutionPolicy Bypass`でrunnerと4 gateが成功。READMEへ実行形を反映。
- **FACT**: CLI smokeはdoctor `ok` / OpenAI provider `disabled`、20 tools、exact pot odds 0.25、
  hand review `completed`、`runs/<generated-run-id>/final_report.md`生成を確認。
- **FACT**: Git metadata修正後のpublic preflight unit/history targeted testsは21件成功。
- **FACT**: 修正後のcanonical `python -m pytest`は170件成功し、短い一意なignored path
  `.pytest-tmp/m-019f796b`を明示したfull suiteも170件成功。
- **FACT**: 最初に試した長い一意なignored `user_materials/` basetempではWindows path深度により
  1件が`FileNotFoundError`、169件が成功した。既存の短いworkspace-local temp方針で再検証した。
- **FACT**: 修正後の`ruff check .`、67 filesの`ruff format --check .`、`mypy src` 41 source filesが成功。
- **FACT**: 修正後のCLI smokeはdoctor `ok` / OpenAI provider `disabled`、20 tools、exact pot odds
  0.25、hand review `completed`、`runs/<generated-run-id>/final_report.md`生成を確認。
- **FACT**: 最新offline preflightは
  `user_materials/public-preflight-20260719-metadata-final-f21bdd34.json`へGit ignoredで保存。fail 0、pass 8、
  review 2、unknown 3、実秘密候補0、説明付きsynthetic canary 4、PII候補10、metadata skipped 0。
- **FACT**: 最新報告はcommit/ref metadataの完全性がtrue、tag 0件を正常処理し、14 findingsすべて
  `[REDACTED]`、再抽出した候補値5種の平文一致0件、publication decisionは
  `human_review_required`のまま。
- **UNKNOWN**: setuptools/wheelのlicense metadata、remote Actions/log、未build wheel/sdist、
  Windows CPython 3.12以外のOS/Python matrix、深いclone/temp pathとlong-path設定の組合せ、
  coverage threshold。

## 2026-07-18

- 当時の再レビュー反例を追加し、当時の126テストがworkspace-local basetempで成功。
- 当時のRuff lint、Ruff format check、mypy strictが成功。現在のgate結果ではない。
- scope fail-closed、blind isolation、provider入力境界、side-pot回帰を強化。

## 2026-07-17

- 目標ファイル全1,036行をUTF-8で確認。
- 空の作業ディレクトリ、実行環境、APIキー、利用可能依存関係を調査。
- Codex Manualを公式URLから取得し、設定・エージェント・Skills配置を確認。
- Developer Docs MCP登録はCodex実行ファイルのアクセス拒否により未完了。
- OpenAI公式のAgents SDK、HITL、Responses API、現行モデル資料を確認。
- API非依存コア＋任意Agents SDK Providerの二層設計を採用。
- Pydantic型付きスキーマ、有限状態機械、承認台帳、再開可能なrun store、Markdown/JSONレポートを実装。
- ポットオッズ、コンボ、Hold'em equity、EV tree、ICM、ゼロ和行列ゲーム、固定戦略best response、ハンド検証、感度分析を実装。
- 外部ソルバーとOpenAI Agents SDKがない場合は、推測結果を返さず明示的なUnavailableとして扱う境界を実装。
- Codexカスタムエージェント9種、プロジェクトSkills 3種、設定、ADR、運用・安全・制限文書、再現用examplesを追加。
- unit/property/integration/golden/adversarialの51テストが成功。カバレッジ85%。
- Ruff lint、Ruff format check、mypy strictがすべて成功。
- `doctor` と主要CLIを実行し、誤った33.333%ポットオッズ主張を厳密値25%で訂正できることを確認。
- 数学、オーケストレーション、セキュリティ、テスト/再現性の4独立レビューを完了。
- ブラインド状態、player 1 best response、MC区間、ICM境界、感度設定の数学的指摘を修正。
- 未検証FACT/A・provider結論・事前承認注入を遮断し、証拠・争点・信頼度・承認表示を接続。
- hard compute/I/O cap、redaction、JSON argv、排他的run、workspace内run rootを実装。
- 再確認で見つかった組合せ仕事量、DAG再訪、最終redaction、provider変異/取消境界も修正。
- レビュー反例と再現性契約を回帰テスト化し、85テスト・カバレッジ87%が成功。
- golden 6、adversarial 23、property 3、integration 9を分離再実行して成功。
- 最終再確認で数学・オーケストレーション・セキュリティの残存High/Mediumがゼロ。
- Ruff lint/format、mypy strict、pip check、3 Skills validator、主要CLI実動確認がすべて成功。
