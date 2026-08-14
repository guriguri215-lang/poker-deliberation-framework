# P3-016B versioned river range-equity bridge

## 対象範囲

P3-016Bは、成功した1個の`RangeValidationResultV1`を既存のheads-up `holdem_equity`へ
接続する加算的なbridgeである。Heroとrange targetだけがeligibleな、retrospective NLHE cash
river判断だけを扱う。rangeは引き続き`USER_CLAIM`または`ASSUMPTION`であり、bridgeはその戦略的
正しさを証明しない。

公開APIの呼出順は次のとおりである。

```python
admission = admit_versioned_range_river_equity(candidate)
report = orchestrator.run_versioned_range_river_equity(admission)
result = build_versioned_range_river_equity_result(admission.case, report.tool_results)
```

`Orchestrator.run()`はbridge markerを拒否する。全new-run経路はtool実行前に共通のper-run kernel
authorityを取得し、product namespaceを予約する。専用APIは同じauthority区間内で、run buffer外の
`.revision-control/range-equity-admissions/<case-folded-run-lock-hash>.json`へbinding/candidate/tool-plan digestを
exclusive-createでflushし、次にbufferを作る。同じadmission bindingは`range_equity_binding.json`として
run payloadにも保存する。orphan recordや予約済みnamespaceと同じrun IDはbuffer作成・tool実行前に拒否する。
case aliasも同じjournal keyへ写像し、record内run ID不一致としてfail closedにする。Legacy copy migrationも
同じterminal authorityで空のproduct namespaceを先に予約する。bridgeとmigrationの先着一方だけが予約でき、
負けた側はbudget作成・tool実行・publicationより前に拒否される。migrationはpublish直前にも、自身の空予約に
current pointerやbufferが追加されていないこととlegacy sourceが変わっていないことを再検証する。
通常の`Orchestrator.run()`が従来から受け付けるcaller-supplied `holdem_equity` inputは別経路であり、
その計算意味を変更しない。

**FACT**: tool実行前の予約では、product namespaceを内側からrootの親まで同期し、admission recordはファイル本体の`fsync`、journal directory、control directoryの順で同期する。POSIXはdirectory `fsync`、Windowsは`FILE_FLAG_BACKUP_SEMANTICS`で開いたdirectory handleへの`FlushFileBuffers`を使用する。いずれかの同期が不能または失敗した場合はrun bufferとtool実行より前にfail closedする。

## Contractとhash domain

`VersionedRangeRiverEquityBindingV1`はcandidate、source range、検証済みcondition、canonical combo、
exact oracle、target、decision index、3-tool plan、990 evaluations capを記録する。
`VersionedRangeRiverEquityResultV1`は保存済みinput、strict binding artifact、tool resultから
再構築できる。markerとbinding artifactはcomplete runと、独立したreplay authorityを持つdurable terminalで
同時に存在して一致しなければならない。再現不能な一時的tool failureや一般budget failureは
`failed_with_limitations`のephemeral reportとして返し、durable current pointerを作らない。terminal publish/readはbuffer外のpre-execution admission
recordも再読し、binding artifactとのdigest一致を要求する。このrecordが存在するrunを全marker・binding
artifact不在のlegacy payloadへ作り替えることは拒否する。namespace確認、product namespace予約、record作成、
buffer作成は同一per-run authorityで直列化するため、同じrun IDの通常runが途中へ割り込んで公開することもない。

次のASCII domain-separated SHA-256を独立して使用する。

- `poker-versioned-range-river-equity-source-range-v1`
- `poker-versioned-range-river-equity-candidate-v1`
- `poker-versioned-range-river-equity-validation-input-v1`
- `poker-versioned-range-river-equity-validation-output-v1`
- `poker-versioned-range-river-equity-combos-input-v1`
- `poker-versioned-range-river-equity-combos-output-v1`
- `poker-versioned-range-river-equity-equity-input-v1`
- `poker-versioned-range-river-equity-equity-output-v1`
- `poker-versioned-range-river-equity-oracle-v1`
- `poker-versioned-range-river-equity-binding-v1`
- `poker-versioned-range-river-equity-admission-record-v1`
- `poker-versioned-range-river-equity-result-v1`

canonical JSONはUTF-8、key sort、compact separator、finite numberに固定し、
`domain + NUL + payload`をhashする。

## 実行順とexactness

runtime順序は固定である。

1. `range_validate`がsource rights、action-prefix condition、blocker、canonical combo、
   integer-millionth weightを検証する。
2. `combos`は成功したcanonical notationだけを`dead_cards=[]`で受け取る。
3. `holdem_equity`は`combos`成功後だけ実行する。Heroのexplicit combo、canonical villain range、
   5-card board、`mode=exact`、`max_exact_evaluations=990`を機械生成する。

bridgeは各合法villain comboについてHeroとvillainのhand rankを比較し、整数weightを次のように
集約する。

```text
numerator = 2 * win_weight_millionths + tie_weight_millionths
denominator = 2 * total_weight_millionths
```

約分済みfractionは`exact`である。legacy `hero_equity`は`math.fsum`によるpair集約を使うbinary64
`floating-verified` projectionであり、integer-millionth oracleのbinary64 projectionからcalculator contractの
128 ULP以内でなければならない。exact outputへMonte Carlo専用metadataを混在させることも拒否する。

exact-only replayはtool outputだけでなく、`ToolResult`最上位のmethod、stochastic、seed、samples、iterations、confidence interval、stopping condition、assumptions、version、model qualifier、warnings、error、reproduce commandもcanonical contractと完全照合する。exact outputをMonte Carlo provenanceで包んだartifactは`REQ_E_CHAIN`で拒否する。

## Fail-closed境界

nonriver、noncash、非canonicalまたは重複known card、0個または複数versioned range、不明target、
追加eligible player、targetのriver bet/raiseで終わらないprefix、all-in、手動tool input、tool順序変更、
Monte Carlo、990超過、source/condition/combo/hash不一致、equity range変更、outcome count変更、数値変更、
失敗した前提tool後の継続実行を拒否する。

immutable revision readerとterminal readerはmarker、`range_equity_binding.json`、case projection、
tool prefixのsemantic replayを繰り返す。terminal publicationはさらにbuffer外のpre-execution admission
recordを要求する。hash同士の一致だけでは受理しない。tool resultの形状だけでは従来の手動exact-equity
runと専用bridgeを識別できないため、形状推測をprovenance authorityにしない。

admissionはdurable canonical storageと同じNFC文字列境界を先に検査する。public verifierはinput、normalized、
reportの3 projectionをbinding artifactへ相関し、schema-invalidまたは非JSONのtool outputをstableな
`REQ_E_CHAIN`として拒否する。

ここで`eligible`は、versioned rangeが拘束したcanonical action prefix内の明示的な`fold`を
差し引いたplayer集合を指す。P3-016B自身はbetting round、stack、uncalled return、side potを
再計算してハンド全体の合法性を証明しない。完全なaction legalityやpot accountingが必要な用途では、
別contractのvalidator/ledgerによる検証が前提である。

## 非対象

P3-016Bはrange syntax、複数range、preflop/flop/turn equity、multiway、all-in、自然言語parse、call EV、
call/fold助言、rake model、external source import、external solver、GTO、均衡を追加しない。
P3-030Cは別の専用admissionとしてこのbridgeを再利用するが、P3-016B自身のcontractや通常経路は
変更しない。P3-030Cの限定統合は`docs/bounded-river-call-ev.md`に記載する。

## 決定論的評価

`tests/fixtures/range_equity/v1/scenarios.json`はrepository-owned MIT synthetic fixtureである。
`scripts/run_range_equity_evaluation.py`は、weightと有理数oracle、admission境界、failure/terminal replayを
7 case・3 exact-evidence metricで検査する。990 comboの極端weightに対する128 ULP境界とexact outputへの
Monte Carlo metadata混入拒否をexact/oracle metricに含める。binding artifact、pre-execution record、
orphan recordのpre-tool run-ID conflictとcase alias拒否、replay authorityのないfailed prefixがephemeralに留まり
durable currentを作らないこと、通常の手動exact equity
互換性もreplay/storage metricに含める。
全metricのthresholdは`1.0`であり、欠落、追加、順序変更、
evidence不一致はfail closedで`0.0`になる。結果artifactは対象commit SHAとtree SHAを必須で記録し、
case/metric/overallの再計算値とともにhashへ拘束する。この評価はrangeの戦略的正しさ、call EV、GTO、均衡を
評価しない。
