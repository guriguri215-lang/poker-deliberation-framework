# 限定日本語NLHEキャッシュ入力

P3-030Bは、一般自然言語parserではありません。version 1として列挙した日本語文型だけを
決定的に解釈し、各semantic fieldを元UTF-8 bytesのhalf-open span `[start_byte, end_byte)`へ
束縛します。抽出結果を利用者が6個の独立hash domainで明示確認した場合だけ、P3-030Aと同じ
local-only adjudication、context lifecycle、role routing、durable terminal reportへ接続します。

## 3段階の利用

1. `prepare-bounded-review-intake`がsource bytesを検証・抽出し、candidate、source bindings、
   focal decision、tool plan、extractor identityを含む準備artifactを作ります。この段階ではrun
   namespace、role、tool、reportを作成しません。
2. 利用者はsourceと全projectionを確認し、source、candidate、source bindings、focal decision、
   tool plan、extractorの6 hashを`confirm-bounded-review-intake`へ明示的に渡します。
3. `review-bounded-confirmed-intake`がsource、candidate、confirmation、authority、run ID、期限を
   再検証し、固定`LocalProvider`と`default_registry()`で製品runを実行します。

例:

```text
poker-deliberate prepare-bounded-review-intake --source hand-ja.txt \
  --output preparation.json --intake-id intake-1 --source-id source-1

poker-deliberate confirm-bounded-review-intake --preparation preparation.json \
  --output confirmation.json --run-id run-1 --authority-id local-user \
  --confirmation-id confirmation-1 --idempotency-key idempotency-1 \
  --expected-source-sha256 <source hash> \
  --expected-candidate-sha256 <candidate hash> \
  --expected-source-bindings-sha256 <source bindings hash> \
  --expected-focal-sha256 <focal decision hash> \
  --expected-tool-plan-sha256 <tool plan hash> \
  --expected-extractor-sha256 <extractor hash>

poker-deliberate review-bounded-confirmed-intake --source hand-ja.txt \
  --preparation preparation.json --confirmation confirmation.json --format summary
```

local CLI authorityは`self_asserted`であり、暗号署名や本人確認ではありません。confirmationの
有効期間は最大24時間です。同じidempotency key、run ID、全bindingが一致するterminal runだけが
read-only replayになります。

## version 1 grammar

入力はUTF-8、BOMなし、LFのみ、NFC、最大65,536 bytesです。空行を除き、行順は次の固定構造です。

- 完了済みNLHEキャッシュ宣言と参加人数（2～6人）。
- `SB/BB`、ante `0`、rake `0`。金額は非負の通常10進表記で、内部では入力から導いたchip unitの
  整数へ変換する。
- 一意なplayer ID、position、正の開始stack。`Hero`を必須とする。
- `Hero`の2 hole cards。
- `プリフロップ`に続く時系列action。必要に応じてflop 3枚、turn 1枚、river 1枚を順番に記す。
- actionはSB/BB post、check、fold、call、bet、または「追加額」と「合計額」を両方記すraise。
  最大64 actionsで、曖昧なraise表現は受理しない。人数ごとのposition集合、blind post順、
  preflop/postflopのactor順、street完了、terminal foldも検証する。
- 任意の検算値として、判断直前pot、call額、call後の争点potを各1回だけ記せる。記した値が
  ledger再計算と違えば拒否する。
- 最後に、同一street上で相手のbet/raiseと直後のHero callまたはfoldが隣接する、単一の
  `コールまたはフォールド` focal decisionを指定する。hand全体はterminal foldまで記録する。

具体的な受理例は
[`tests/fixtures/bounded_natural_language/v1/valid-ja.txt`](../tests/fixtures/bounded_natural_language/v1/valid-ja.txt)
を参照してください。半角/全角slash、ASCII/ideographic space、列挙した丁寧体・常体のvariation
以外は一般化しません。余分な行を無視せず、stableな`BNL_E_*` code、field path、該当する場合は
source byte spanを返してfail closedにします。errorへsource lexemeやsecretをechoしません。

## 実行と証拠

確認後のtool planは次の3件・この順序だけです。

1. `hand_validator`
2. `hand_pot_ledger` (`generic_nlhe_cash_no_rake_v1`)
3. `pot_odds`

`CaseInput.raw_text`は`None`で、raw sourceはagent contextやproviderへ渡しません。sourceの内容は
確認後も`USER_CLAIM`であり、検証済みcalculator結果だけが各tool contractに従う`CALCULATED`
です。`LocalProvider`の文章的出力は`UNKNOWN`のままです。

terminal runは`bounded_nl_source.txt`、`bounded_nl_candidate.json`、
`bounded_nl_confirmation.json`、`bounded_nl_provenance.json`を既存artifact群へ追加します。readerは
parserを再実行し、exact spans、6 hash、focal action adjacency、tool inputs/results、context、role、
report、storage revisionを再相関します。artifact欠落、改ざん、cross-run replayは拒否します。

## 明示的な非対応範囲

一般自然言語、site hand history、画像/PDF/OCR、tournament、ante/rake、straddle、range抽出、
real-time助言、focal all-in、focal side pot、short call、複数focal decision、外部provider/model、
Codex/Python runtime bridge、外部solver、range equity、multiway range、GTO・均衡は未実装です。
P3-030Aのcaller-supplied candidate経路は変更せず併存します。

## 決定論的評価

`tests/fixtures/bounded_natural_language/v1/scenarios.json`はrepository-owned MIT synthetic
fixtureです。次のコマンドが12件の宣言済みcaseを実行します。

```text
python scripts/run_bounded_natural_language_evaluation.py \
  --fixture tests/fixtures/bounded_natural_language/v1/scenarios.json \
  --source tests/fixtures/bounded_natural_language/v1/valid-ja.txt \
  --work-root tmp/bounded-nl-evaluation/work \
  --source-commit <40-or-64-lowercase-hex-commit> \
  --source-tree <40-or-64-lowercase-hex-tree> \
  --output tmp/bounded-nl-evaluation/result.json
```

fixtureはsource、hand、focal decision、tool plan、全source-binding tuple、extractorの固定hash oracleと
binding件数を含みます。source byteが1つでも異なる場合は全caseをfail closedにし、各spanは固定tuple、
元source slice、lexeme hashを再計算します。field extraction、source span、diagnostic、end-to-end tool
evidence、storage replayを別々に採点し、5 metricがすべてexact `1.0`の場合だけ合格します。
result schema v2はcaller-declared source commit/treeを必須でdigestへ拘束し、v1 resultはread-only互換として
引き続き検証できます。
これはversion 1 grammar境界の適合性であり、
一般自然言語精度、戦略品質、GTO、均衡、release readinessの評価ではありません。
