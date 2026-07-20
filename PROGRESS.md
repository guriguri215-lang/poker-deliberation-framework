# Progress

## 2026-07-20 — Phase 1 push後再監査

- **FACT**: 再監査開始時は`codex/phase1-math-contracts` / `ca8c9ab5c24f5a4416ed3bcfde3d0fb9028a3976`、
  `origin/codex/phase1-math-contracts`との差0/0、tracked/untracked差分なしだった。Git fetchは
  実行していない。
- **FACT**: 修正前の`.venv` / CPython 3.12.13でfull pytestは312件成功、Ruff lint、74 filesの
  format check、mypy 44 source filesが成功した。
- **FACT**: bare `python`、`ruff`、`mypy`、`poker-deliberate`は今回のshellのPATHに存在せず失敗した。
  workspaceの`.venv\Scripts`を明示した同一entry pointでは4 gateとCLIが成功した。
- **FACT**: 修正前doctorは`phase_1_hardening=planned`を返したが、20 tool全ての
  `contract_version=2.0.0`、typed input/output、runtime verification、Phase 1 oracle/property testsが
  現HEADに存在したため、能力表示は実装と矛盾していた。
- **FACT**: Codexの9 agent定義とPythonの7 roleは別実行面であり、PythonがCodex agentを起動する
  runtime bridgeやCodex実行をPython runへ同期する実装は存在しない。
- **FACT**: `user_materials/ROADMAP.md`はignore対象の未検証ユーザー資料として必要箇所だけ参照し、
  編集していない。本文の全RM PROPOSED表示、旧HEAD、Phase 5のRM-018は現実装・承認済みtag順序と
  未同期である。
- **INFERENCE**: RM-018のrelease readiness部分はpre-release前へ移し、stable release部分は
  Phase 2完了ゲートへ置く必要がある。context/data lifecycle、runtime conformance、extension SPI、
  isolated job controlは別roadmap項目または既存RMの明示的な拡張が必要である。
- **FACT**: 能力・文書・contract test修正後のfull pytestは314件成功、Ruff lint、74 filesの
  format check、mypy 44 source filesが成功した。doctorは`phase_1_hardening=implemented`、
  `codex_python_runtime_bridge=unavailable`、全体`status=ok`を返した。
- **FACT**: 修正後CLIは20 tools全て`contract_version=2.0.0`、Python 7 roles、pot oddsの
  `required_equity=0.25` / `numeric_exactness=floating-verified` / verification成功、solverの
  `unavailable` / `available=false`を返した。
- **USER_CLAIM**: この未コミット修正を作成した前セッションでは、外部通信、依存追加、
  外部solver/API、sub-agent、commit、push、PR、tag、release、公開は実行していない。

## 2026-07-19 — Phase 1

- **FACT**: `origin/main`のmerge commit `2a62e5cc6b7b8df8ae93e8efd638d80a0db217fd`、
  clean worktreeから`codex/phase1-math-contracts`を作成した。
- **FACT**: RM-004としてpriority 5 modulesの入力境界、fallback、failure、locator、warningを
  unit/property/adversarial testで覆った。52 targeted testsでbranchを含む対象240 statements / 90
  branchesが100%だった。global coverage thresholdは追加していない。
- **FACT**: RM-005として旧3値`exactness`を互換projectionとして維持し、contract v2の
  `numeric_exactness`に`exact` / `exact-under-model` / `floating-verified` / `approximate` /
  `unavailable`を追加した。approximate successのmethod、stochastic/seed、samples/iterations、
  interval/error、stopping conditionをschemaで強制した。
- **FACT**: universal epsilonは導入せず、tool/field/unitごとのULP、caller-supplied matrix tolerance、
  Monte Carlo interval、ICM conservation bound、chip magnitude由来toleranceを明示した。
- **FACT**: RM-006として20 toolsのstrict typed input/output、前提、precondition、limit、unit、
  failure、version、exactnessをcanonical definitionへ集約し、manifestとtool contracts文書を生成した。
  round-trip、extra/missing field、version mismatch、registry/manifest/docs parity testsが成功した。
- **FACT**: RM-007としてFraction known-answer、invariant、scale/order/symmetry、invalid boundary、
  deterministic/seeded reproducibility、matrix sensitivityをlocal oracle/metamorphic packへ追加した。
  外部oracle、package、solver、provider、datasetは使用していない。
- **FACT**: final candidateのcanonical full pytestと一意なworkspace-local `--basetemp` full pytestは
  ともに291件成功した。Ruff lint、72 filesのformat check、mypy 42 source filesも成功した。
- **FACT**: CLI smokeはdoctor `ok`、20 tools、exact combos、floating-verified pot odds、seeded
  approximate equity、unavailable solverを確認し、JSON/Markdown metadata parityも成功した。
- **FACT**: offline public preflightはfail 0 / pass 8 / review 2 / unknown 3、secret候補0だった。
  PII review候補は既存fixtureとGit identity metadataに限定され、Phase 1ファイル由来ではなかった。
- **UNKNOWN**: dependency license metadataの一部、remote Actions/log、未build package内容、未実行の
  OS/Python matrix、coverage release thresholdはPhase 1でも確定していない。
- **FACT**: dependency追加、外部送信/取得、sub-agent、push、PR、tag、releaseは実行していない。

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
