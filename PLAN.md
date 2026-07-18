# 実装計画

## 調査結果（2026-07-17 JST）

- 作業ディレクトリは空で、Gitは未初期化。
- Windows / PowerShell 5.1。Codex Desktopパッケージは
  `OpenAI.Codex_26.715.2305.0_x64`。CLI実行はアクセス拒否のためバージョン文字列を
  直接取得できなかった。
- Codex同梱Python 3.12.13を利用可能。`pydantic 2.13.4` は同梱済み。
- `OPENAI_API_KEY`、OpenAI Python SDK、OpenAI Agents SDKは未導入。
- Codex Manualは公式URLから2026-07-17に取得。Developer Docs MCPの登録は
  `codex.exe` のアクセス拒否で失敗したため、Responses API / Agents SDKはOpenAI公式
  ドキュメントをウェブ参照した。
- 公式Manualで、プロジェクト設定は `.codex/config.toml`、カスタムエージェントは
  `.codex/agents/*.toml`、リポジトリSkillsは `.agents/skills/*/SKILL.md` と確認した。
- 現行モデルカタログと実行環境の両方で `gpt-5.6-sol` / `gpt-5.6-terra` を確認した。

## 設計方針

1. APIキーなしで動く決定的なPythonコアを先に実装する。
2. アプリケーション側が状態、予算、終了条件、承認、run artifactsを所有する。
3. Agents SDKは任意Providerとし、未導入時は明示的なUnavailableを返す。
4. 厳密計算、Monte Carlo、推定をToolResultのメタデータで区別する。
5. 小規模best responseは純粋方策列挙でinformation-set制約を厳守する。
6. 大規模NLHE均衡は解かず、外部SolverAdapterのUnavailableを返す。
7. 外部パッケージ導入は承認後に専用 `.venv` へ限定する。

## 実装順序

- [x] 環境・公式仕様調査
- [x] PLAN / ADR /プロジェクト骨格
- [ ] Pydanticスキーマ、状態機械、保存、承認
- [ ] ローカル計算ツール
- [ ] Provider、役割選択、オーケストレーター、レポート
- [ ] CLIとrun再開・表示
- [ ] CodexエージェントとSkills
- [ ] docs / examples / evals
- [ ] unit / property / integration / golden / adversarial tests
- [ ] lint / format / type check
- [ ] 4観点の独立read-onlyレビュー
- [ ] 指摘修正と全検証再実行

## 完了ゲート

次を完了チェックリストとして使用する。

- 対象入力、仮定、制限、承認要否が明示されている。
- exactとapproximateが分離され、再現コマンドとToolResultが保存される。
- unit / property / integration / golden / adversarialテストが成功する。
- `ruff check .`、`ruff format --check .`、`mypy src`が成功する。
- レビュー指摘が修正済み、明示的に見送り、または制限事項として記録されている。

品質チェックが未実行、またはレビュー指摘が未処理の状態では完了としない。

## 公式資料

- Codex Manual: <https://developers.openai.com/codex/codex-manual.md>
- OpenAI Agents SDK: <https://openai.github.io/openai-agents-python/>
- Agents SDK human-in-the-loop:
  <https://openai.github.io/openai-agents-python/human_in_the_loop/>
- OpenAI model catalog: <https://developers.openai.com/api/docs/models>
- Responses API reference: <https://platform.openai.com/docs/api-reference/responses>
