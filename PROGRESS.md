# Progress

## 2026-07-18

- 再レビュー反例を追加し、現在の全126テストが成功。
- Ruff lint、Ruff format check、mypy strictがすべて成功。
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
