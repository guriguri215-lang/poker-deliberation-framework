# ADR 0001: Codexネイティブ層と決定的Python制御層

- Status: Accepted
- Date: 2026-07-17

## Context

フレームワークには、Codexからの直接利用、複数専門家、ローカル厳密計算、承認後の再開、
監査可能な成果物、APIキーなしのテストが必要である。現在の環境にはOpenAI Agents SDKと
APIキーがない。

OpenAI公式Agents SDKは、エージェントループ、tools/handoffs、guardrails、sessions、tracing、
HITLを提供し、OpenAIモデルではResponses APIを既定で利用する。一方、公式資料も、ループ・
ツール配分・状態を自分で所有したい場合はResponses API直利用を選べるとしている。

## Decision

二層構成を採用する。

1. Codexネイティブ層: `.codex/config.toml`、`.codex/agents/*.toml`、
   `.agents/skills/`、ローカルCLIを提供する。
2. Python制御層: 状態遷移、予算、承認、保存、計算、裁定入力、レポートを決定的に管理する。
3. Providerプロトコルを定義し、ローカルMockProviderを既定とする。
4. Agents SDK Providerは任意依存とし、SDKまたはAPIキーがなければUnavailableを返す。
5. SDKを導入しても、状態機械・承認台帳・run artifactsはアプリケーション側を正とする。

## Consequences

- APIキーなしでdoctor、計算、検証、テストが動く。
- SDK導入前でも偽のモデル応答や均衡結果を返さない。
- SDK固有のtracing/HITLとの完全統合はMVP後の追加作業となる。
- ローカル成果物とSDK RunStateを併用する場合、バージョン対応を明示する必要がある。

## Alternatives

- Agents SDKへ全面委任: 現環境では実行不能で、状態・予算・承認の監査境界が不明瞭になる。
- LLMのみで統合: 数学的再現性、APIキーなしテスト、失敗時の安全性を満たさない。
- 大規模ソルバー内製: MVPの範囲と計算資源を超える。

## Sources

- <https://openai.github.io/openai-agents-python/>
- <https://openai.github.io/openai-agents-python/human_in_the_loop/>
- <https://developers.openai.com/codex/codex-manual.md>
