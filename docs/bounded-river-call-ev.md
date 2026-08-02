# Bounded river call-EV review

## 実装境界

P3-030Cは、P3-030Bの有限日本語grammar、P3-015Aのexact no-rake ledger、P3-016Bの単一range
exact river equity、既存`pot_odds`と`raked_call_ev`、P2-024A context lifecycleを統合する
ローカル専用contractである。入力は完了済みretrospective NLHE cashで、riverのfacing bet/raise直後に
Heroが実際にfoldした最終actionをcall/foldのfocal decisionとする。board 5枚、Hero hole 2枚、
ante/rake 0、no all-in/side pot/future action、focal時点のeligible playerがHeroとfacing actorだけ、
別入力の`VersionedRangeDefinitionV1`がちょうど1個でtargetもそのactor、という条件をすべて要求する。

P3-030Bのterminal-fold grammarは変更しない。そのため実際のcallで終了した履歴はこのversionでは
受理せず、実際にfoldした履歴に対するcounterfactual call EVを計算する。rangeは自然言語から推測せず、
source/license/usage/content status/hash、grammar/version、integer-millionth weights、game conditions、
action-prefix、blockersをP3-016B admissionで再検証する。

## Confirmationと実行

candidate schema、confirmation、binding、result、provenanceはいずれもversion `1.0.0`の加算的contractで
ある。confirmationは次の12 hashを独立domainで完全一致させる。

1. raw source
2. P3-030B bounded candidate
3. source bindings
4. focal decision
5. extractor
6. combined tool plan
7. range definition
8. range target/action-prefix binding
9. P3-016B range-equity binding
10. exact equity model
11. exact call-EV model
12. complete P3-030C candidate

authority scope、run ID、confirmation/idempotency ID、UTC confirmation/expiryも拘束する。最大有効期間は
24時間である。source/range変更、期限切れ、cross-run/case-fold alias replay、manual tool inputとの競合は
fail closedになる。P3-016B recordとP3-030C recordをbuffer/tool executionより先にexclusive-createし、
preflightはcalculatorを走らせない。各toolは次の順で一度だけ実行する。

```text
hand_validator
hand_pot_ledger
pot_odds
range_validate
combos
holdem_equity
raked_call_ev
```

`raked_call_ev`には`rake_percent=0`、capなしを渡す。存在しない`call_ev` toolは使用しない。

## Exact oracleと主張範囲

ledgerのinteger chip unitsとP3-016B reduced rational equityを正とする。独立`Fraction` oracleは次である。

```text
required_equity = call_cost / (pot_after_bet + call_cost)
call_ev = equity * (pot_after_bet + call_cost) - call_cost
fold_ev = 0  # focal decision時点
call_minus_fold_ev = call_ev
```

`pot_odds.required_equity`、`holdem_equity.hero_equity`、`raked_call_ev.ev`はexact oracleのbinary64
projectionとして、それぞれ16、128、32 ULP以内だけを保存する。exact equity、required equity、call EV、
fold EV、delta、`call`/`fold`/`tie`比較をtyped resultに保持する。

比較は「確認済みの明示range、zero rake、no future betting」というmodel内だけの`CALCULATED`である。
range sourceは`USER_CLAIM`または`ASSUMPTION`、戦略的解釈は`INFERENCE`、実戦rangeの正確性は
`UNKNOWN`である。一般戦略、無条件の推奨、GTO、均衡、solver-derived strategyは主張しない。

## Contextとdurable replay

`CaseInput.raw_text`は`None`で、raw sourceをagent contextへ渡さない。exact Python `LocalProvider`、既存role
assignment、P2-024A `ContextEnvelope`、attempt lineage、expiry、runtime/classification/hash/budgetを再利用する。
math-auditorだけが上記7 toolを完全一致allowlistとして持ち、他roleは空である。実Codex/Python runtime
bridgeやproduct runtimeのCodex sub-agent実行ではない。

terminal runは次の7 artifactを既存case、assignment、context、agent、tool、report、P3-016B bindingへ相関する。

- `bounded_river_call_ev_source.txt`
- `bounded_river_call_ev_range.json`
- `bounded_river_call_ev_candidate.json`
- `bounded_river_call_ev_confirmation.json`
- `bounded_river_call_ev_binding.json`
- `bounded_river_call_ev_result.json`
- `bounded_river_call_ev_provenance.json`

readerはsource parserと両admission、tool input/output、exact oracle、ULP、failed prefix、role/context、storage
authorityを再実行する。missing/extra/reordered/tampered artifact、source/range mutation、record欠落、context
runtime/role/allowlist/expiry/lineage mismatchを拒否する。SHA-256はcorruption/correlation検出用であり、同じ
OS権限のwriterに対する暗号学的authenticityを提供しない。

## CLIと評価

CLIは次の3段階だけを提供する。

```text
prepare-bounded-river-call-ev-intake
confirm-bounded-river-call-ev-intake
review-bounded-river-call-ev-confirmed-intake
```

`tests/fixtures/bounded_river_call_ev/v1/scenarios.json`と
`scripts/run_bounded_river_call_ev_evaluation.py`は、exact decision math、admission/security、runtime/replayの
3 metricをexact evidence setで採点する。全metric、全case、overallが`1.0`の場合だけ合格し、resultは
対象commit SHA/tree SHAとcanonical result hashへ拘束する。

## 非目標

一般自然言語、model/site parser、OCR、range推測、複数range、multiwayまたはpreflop/flop/turn equity、
Monte Carlo、all-in、side pot、rake、ante、tournament/ICM、外部provider、実Codex/Python bridge、外部solver、
GTO、均衡、新規package、外部data取得は非目標である。
