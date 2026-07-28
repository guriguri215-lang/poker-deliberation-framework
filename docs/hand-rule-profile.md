# P3-015A hand rule profile

## 実装済み境界

- **FACT**: `hand_pot_ledger`が受理するprofileは
  `generic_nlhe_cash_no_rake_v1` version `1.0.0`だけで、`supported_site=none`である。
- **FACT**: input、profile、ledger action、uncalled return、pot layer、player eligibilityは
  schema `1.0.0`のstrict・frozen modelである。unknown version、unknown field、暗黙defaultは
  受理しない。
- **FACT**: `CanonicalHand`とfree-text normalization grammar/version `1.0.0`は変更しない。
  profileはcalculator inputで明示し、product pathは`CaseInput.hand`をtool inputへ束縛する。
- **FACT**: legacy `hand_validator`は従来のbinary64・`floating-verified` validatorとして残る。
  新calculatorだけがside-pot ledgerを`exact-under-model`として返す。

## 対応profile

| field | 唯一の受理値 |
|---|---|
| profile schema | `1.0.0` |
| profile ID | `generic_nlhe_cash_no_rake_v1` |
| profile version | `1.0.0` |
| supported site | `none` |
| game / format | NLHE / cash |
| board | single board |
| rake | inputで明示した0 |

`chip_unit`はcallerが明示する正のcanonical decimal文字列である。整数部は最大18桁、小数部は
最大9桁で、leading zeroや末尾zeroを含む別表現は受理しない。既存`CanonicalHand`のbinary64値を
Pythonのshortest decimal表現へ戻し、`Decimal`で`chip_unit`の整数倍であることを要求する。
内部値は`0..2^63-1`の整数単位であり、丸め・epsilon・toleranceは使わない。

例:

```json
{
  "schema_version": "1.0.0",
  "rule_profile": {
    "schema_version": "1.0.0",
    "profile_id": "generic_nlhe_cash_no_rake_v1",
    "profile_version": "1.0.0",
    "supported_site": "none",
    "chip_unit": "0.1"
  },
  "hand": {"CanonicalHand": "完全な既存schema値"}
}
```

上の`hand`表記は説明用であり、実inputでは完全な`CanonicalHand` objectを渡す。

## betting model

- forced postはpreflopのsmall blind 1件、big blind 1件だけを要求する。3件目のblindはstraddle
  としてfail closedになる。
- ante 0ではante postを拒否する。正のanteでは全playerが同じanteを1回ずつpostする形だけを
  受理する。
- `bet`と`raise`はfull actionであり、stackを使い切る場合は`all_in`を使う。
- `all_in`が直前のfull raise increment未満ならshort raiseであり、それ単独では、すでにactionした
  playerのraise権を再開しない。
- 複数short all-inの累積により、そのplayerが最後にactionしたlevelから現在levelまでの差が
  直前のfull raise以上になればraise権が再開する。
- street終了時に、action可能なactive playerの未処理actionまたは未コール額が残れば
  `incomplete-betting-round-*`で失敗する。

このreopen規約は
[Poker TDA Rules 43/47](https://www.pokertda.com/view-poker-tda-rules/)を参照して明文化した
repository-owned generic cash profileである。特定site、cardroom、jurisdictionへの適合を主張しない。

## ledger と side pot

各chip commitmentはaction index、street、actor、commit後のstreet/total contribution、
remaining stack、pot、current bet、raise rights、full-raise状態へ記録する。

street終了時に最高contributionが1人だけなら、2番目のlevelとの差をそのplayerへ1回だけ返す。
返却playerがactiveで、返却部分と重なるsource actionが再構成できる場合だけ
`uncalled_returns`へ追加する。曖昧な場合は推測せず失敗する。

最終net contributionの正のlevelを昇順に分割し、各layerを次で構成する。

```text
amount = (upper_bound - lower_bound) * contributor_count
contributors = net contribution >= upper_bound
eligible_players = contributorsのうちfoldしていないplayer
```

foldしたplayerの既投入chipはcontributionに残るが、eligibilityは失う。各layerは、その区間へ
chipを投入したroot action indexを`evidence_action_indexes`として持つ。

## 検証とexactness

成功前に次をすべて要求する。

1. `gross committed = final pot + uncalled returns`
2. `starting chips = remaining stacks + final pot`
3. `sum(pot layer amounts) = final pot`
4. main builderとは別に`Fraction`でdecimal-to-unit変換、street return、net contribution、
   layer、eligibility、conservationを再構成し、全値が一致

- **CALCULATED**: 上の条件を満たした個別resultは`conservation_verified=true`、
  `oracle_verified=true`で、`numeric_exactness=exact-under-model`となる。
- **INFERENCE**: これは選択profileと入力action列の下での会計の正確性を意味する。
  現実のsite rule、winner、hand strength、payout splitの正確性までは意味しない。

既存terminal product pathを使い、tool inputと`ToolResult`を同じresult IDの
`tool_results/<id>.input.json` / `.json`としてpair保存する。新artifact kindは追加しない。
summaryはprofile/version/unit、final pot、layers、returns、conservation/oracle flagだけを
明示allowlistから投影し、完全input、remaining stack、未検証文章を展開しない。

## fail-closedな非対応範囲

- rakeが未指定または0以外
- straddle、run-it-twice、multi-board、split pot
- PLO、OTHER、tournament、bounty
- site-specificまたはjurisdiction-specific rule
- unknown profile/schema/version/site
- winner assignment、hand strength、payout
- 非整数chip unit、overflow、incomplete/illegal action、曖昧なuncalled return

これらを追加する場合はRM-015の別decision gate、versioned profile、golden ledgerが必要である。
