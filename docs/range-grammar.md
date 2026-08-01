# Versioned NLHE range grammar

P3-016Aは`poker-deliberation.nlhe-range` version `1.0.0`のbounded grammarと、
その入力・検証結果・製品実行のbindingを実装する。この機能は1人のnon-hero playerに対する
1つの明示rangeを扱う。P3-016Bは、このV1 artifactを変更せず、明示的にadmitされたNLHE cash
riverだけを既存`holdem_equity`のexact enumerationへ接続する別contractである。自然言語から
rangeを推測せず、solver、import format、site固有format、GTOまたは均衡の主張を追加しない。

## FACT: versioned envelope

`VersionedRangeDefinitionV1`は次を必須にする。

- `schema_version=1.0.0`
- `grammar_id=poker-deliberation.nlhe-range`
- `grammar_version=1.0.0`
- portableな`range_id`と`target_player_id`
- UTF-8 source bytesとしてhashされる`notation`
- `source_id`、`source_kind`、`content_sha256`、license、usage、content status
- NLHE、cash/tournament、table size、target position、street、stack interval、
  as-of action index、canonical action-prefix SHA-256

version 1で許可するsource rightsの組み合わせは次の2つだけである。

| source kind | license classification | usage classification |
|---|---|---|
| `user_supplied` | `user_supplied_private_analysis` | `local_analysis_only` |
| `repository_fixture` | `repository_owned_mit` | `redistribution_allowed` |

`content_status`は`USER_CLAIM`または`ASSUMPTION`だけを受け入れる。rangeの提供元が
戦略的に正しいこと、母集団を代表すること、solver由来であることは検証結果に含めない。

## FACT: grammar version 1

tokenはcommaで区切り、token前後にはASCII spaceまたはtabだけを許可する。

| form | example | meaning |
|---|---|---|
| explicit combo | `AsKh` | exact two-card combo |
| pair | `QQ` | six pair combos before blockers |
| suited class | `AKs` | four same-suit combos before blockers |
| offsuit class | `AKo` | twelve different-suit combos before blockers |
| optional weight | `AKs@0.25` | weight `250000` millionths |

non-pair classはrankをdescending canonical orderで書く。weightは`(0, 1]`のASCII decimalで、
小数点以下は最大6桁とする。内部値はbinary floatではなく`1..1000000`の整数millionthsである。
canonical outputでは末尾のzeroを除き、weight `1`は省略する。

次の構文はversion 1では未対応でありfail closedになる。

- `+`、interval、exclusion
- colon weight
- sign、exponent、leading decimal point
- non-ASCII notation
- solver-native、site-native、importまたは自然言語range

combo overlapはblocker除去より前に検査する。そのため、blockerで重複comboが消える場合でも
重複tokenは受理しない。hero cardsと指定streetでvisibleなboard cardsをblockerにし、全comboが
消えるrangeは失敗する。

## FACT: exact binding and result

`range_validate`はcontract version `2.0.0`のlocal exact toolであり、
`RangeValidationResultV1`を返す。成功時には次を含む。

- source notation SHA-256
- exact hand/game-condition/blockerのcondition-binding SHA-256
- canonical notation
- canonical combo SHA-256
- ordered comboとinteger-millionth weight
- combo countとinteger total weight

失敗時にはpartial canonical artifactを返さず、diagnosticだけを返す。製品経路で
versioned rangeと`combos`が指定されると、orchestratorは`range_validate`を先に実行する。
成功したcanonical notationだけを既存`combos`へ渡し、blockerはすでに適用済みなので
`dead_cards=[]`に固定する。手動`range_validate`または`combos`入力がこのbindingと競合する場合、
`combos`を実行しない。

immutable revision preflightとterminal readerは`range_validate`を再計算する。成功chainでは
`combos`入力、output、floating verification metadataも再計算する。保存済みhash同士だけが
整合していても、意味上の再計算と異なるartifactは拒否する。

## FACT: P3-016B river equity bridge

P3-016Bは通常の`Orchestrator.run()`から自動発動しない。呼出側はまず
`admit_versioned_range_river_equity()`でcandidateを検証し、そのadmissionを
`run_versioned_range_river_equity()`へ渡す。callerがmarkerまたはtool inputを手動指定する経路は
拒否する。

admissionは次をすべて要求する。

- `kind=calculation`、`analysis_scope=retrospective`、NLHE cash
- canonicalなHero 2 cardsとriver board 5 cards
- 1個の`VersionedRangeDefinitionV1`と1人のnon-Hero target
- bound action prefixの末尾がtargetによるriver betまたはraise（all-inは非対象）
- 判断時点のeligible playerがHeroとtargetだけ
- tool planが`range_validate`, `combos`, `holdem_equity`の順序だけ
- 手動tool input、raw text、realized result、free-form claim/evidence/assumptionがない

実行時は`range_validate`成功後に`combos`を実行し、その成功後だけ`holdem_equity`を実行する。
equity payloadはHero cards、canonical weighted opponent combos、boardから機械生成し、
`game_type=NLHE`、`mode=exact`、`max_exact_evaluations=990`へ固定する。Monte Carlo fieldや
`opponent_ranges`/`villain_ranges`の別経路は使用しない。

7枚の既知cardでblockしたNLHE river opponent rangeの最大合法combo数は990である。bridgeは
各canonical comboについてhand strengthを完全列挙し、integer-millionth weightを次の有理数へ
集約する。

```text
equity = (2 * win_weight + tie_weight) / (2 * total_weight)
```

このfractionとwin/tie/lossのcombo/weight totalsは`exact`である。既存`holdem_equity`の
`hero_equity`はbinary64 accumulationの互換projectionなので、引き続き`floating-verified`である。
exact enumerationを実数表現までexactと読み替えない。

source range、candidate、validation input/output、combos input/output、equity input/output、oracle、
binding、resultは独立したSHA-256 domainでhashする。terminal readerとimmutable revision readerは
V1 range chainに加え、admission、tool順序、derived payload、outcome count、有理数oracle、128 ULP
projectionを再計算する。検証済みrangeと異なるrangeをequityへ渡すartifactはfail closedになる。

## FACT: limits and diagnostics

| resource | hard limit |
|---|---:|
| notation UTF-8 bytes | 16384 |
| tokens | 1326 |
| expanded combos | 1326 |
| blockers | 52 |
| action prefix | 512 |
| diagnostics | 64 |

stable diagnostic codeは次のとおりである。

`RNG_E_UNSUPPORTED_VERSION`, `RNG_E_LIMIT`, `RNG_E_NON_ASCII`, `RNG_E_SYNTAX`,
`RNG_E_CARD`, `RNG_E_CLASS_ORDER`, `RNG_E_WEIGHT_LEXEME`, `RNG_E_WEIGHT_RANGE`,
`RNG_E_OVERLAP`, `RNG_E_BLOCKER`, `RNG_E_EMPTY`, `RNG_E_PROVENANCE`,
`RNG_E_LICENSE`, `RNG_E_TARGET`, `RNG_E_GAME_CONDITION`,
`RNG_E_DIAGNOSTIC_LIMIT`.

repository-owned MIT fixtureとconformance datasetは同じdeclared case setから生成する。

```powershell
.\.venv\Scripts\python.exe scripts\generate_range_fixtures.py --check
```

## Compatibility boundary

既存`RangeDefinition`、`parse_weighted_range`、`combos` direct input、`holdem_equity`の意味は
変更しない。legacy parserが受け入れていたcolon weight、case normalization、rank reversalなどを
version 1 grammarへ暗黙移行しない。P3-016Aの製品sliceは従来どおり`combos`で終了し、P3-016Bは
別markerと専用admissionがある場合だけ発動する。

複数versioned range、preflop/flop/turn bridge、Monte Carlo bridge、multiway、external range取得、
自然言語hand/range intake、call EV、call/fold推奨、adjudicated reportとの新しい統合経路は未実装で
ある。自然言語からcanonical hand/range、calculator、adjudication、reportへ進む将来候補は
RM-030として別にdecision-gateする。
