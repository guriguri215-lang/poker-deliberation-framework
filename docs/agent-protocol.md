# Agent protocol

## Assignment

Each assignment contains a role, one bounded task, minimum context keys, and read-only status.
The router selects roles by task type; it does not start every role for every run.
The Python provider receives a typed, role-specific context allowlist. Unnormalized hand-history
`raw_text` is sent only to intake. For strategy cases, normalized `strategy_text` is sent to the
strategy analyst, skeptic, and adjudicator, but not the math auditor. Arbitrary metadata is not sent
to providers; only allowlisted calculator inputs are included in the math-auditor context.

## Report contract

An `AgentReport` includes conclusions, claims, assumptions, evidence IDs, tool-result IDs, formulas,
uncertainties, objections, falsification conditions, confidence, and unresolved questions.
Private chain-of-thought is neither requested nor stored.
Provider conclusions are untrusted arguments: the final report labels them UNKNOWN, records disputes,
and excludes them from the adjudicated conclusion until evidence or tools support them.

## Deliberation

1. Normalize and choose the objective.
2. Detect missing decision-relevant information.
3. Run independent specialist passes without seeing each other's conclusions.
4. Use deterministic calculations and primary evidence.
5. Ask the skeptic for concrete falsification.
6. Adjudicate by evidence strength, not vote count.
7. Preserve unresolved disputes.
8. Render Japanese output without adding facts.

## Epistemic labels

Use FACT, CALCULATED, INFERENCE, ESTIMATE, ASSUMPTION, USER_CLAIM, and UNKNOWN. Confidence A is
primary evidence or clear calculation; B has limited uncertainty; C has material assumptions or
missing information; D is speculative.

## Japanese report structure

`src/poker_deliberation/reporting/markdown.py` is the executable renderer for this 15-section
contract. Agents must return the same headings without inventing fields or conclusions:

1. 結論
2. 入力の再構成
3. データ品質と不足情報
4. ユーザー主張の判定
5. 論点別分析
6. 数学的計算
7. 使用したツール
8. GTOベースラインとexploitative adjustment
9. 代替戦略
10. 感度分析
11. 反証と未解決争点
12. 出典
13. 再現手順
14. 人間に必要な質問または承認
15. 信頼度と主要な制限
