# Repository guidance

- ユーザー向け説明とレポートは日本語、コード識別子は英語を使う。
- 数値は文章推論で捏造せず、`poker-deliberate calculate` またはテスト済み関数で計算する。
- FACT / CALCULATED / INFERENCE / ESTIMATE / ASSUMPTION / USER_CLAIM / UNKNOWN を区別する。
- ソルバー実行と収束確認なしに、GTO・均衡・正確なレンジと断定しない。
- 外部コード取得、パッケージ導入、外部送信、破壊的操作は承認後に行う。
- 実装後は `python -m pytest`、`ruff check .`、`ruff format --check .`、`mypy src` を実行する。
- 長い手順は `.agents/skills/` と `docs/` に置き、このファイルは短く保つ。
