# 確認済み自然言語レビュー入力

P3-030Aは、自然言語からポーカーハンドを推測するparserではありません。呼出側が自然言語資料と
完全な構造化候補を用意し、人間または検証済みapplication authorityが両者の対応を明示確認した
場合だけ、既存のoffline adjudicationへ接続する境界です。

文書化した有限の日本語NLHE cash grammarからcandidateを決定的に抽出する別経路は
[P3-030B限定日本語入力](bounded-natural-language-intake.md)を参照してください。P3-030Aの
caller-supplied candidate契約とartifact familyは変更せず、両経路を混在させません。

## 3段階の利用

1. `prepare-review-intake`はsource bytesとcaller-supplied candidateを検証し、source hashと
   candidate hashを含む準備artifactを生成します。この段階ではrun、role、tool、reportを
   作成しません。
2. 利用者はsourceとcandidateを確認し、表示されたhashを
   `confirm-review-intake`へ明示的に渡します。local CLI authorityは`self_asserted`であり、
   暗号学的な本人確認ではありません。
3. `review-confirmed-intake`はsource、candidate、confirmation、authority、期限、run IDの
   bindingを再検証してから、固定`LocalProvider`と標準tool registryだけで製品runを実行します。

例:

```text
poker-deliberate prepare-review-intake --source hand.txt --candidate candidate.json \
  --output preparation.json --source-id my-review-1

poker-deliberate confirm-review-intake --preparation preparation.json \
  --output confirmation.json --run-id run-my-review-1 --authority-id local-user \
  --confirmation-id confirmation-my-review-1 --idempotency-key idempotency-my-review-1 \
  --expected-source-sha256 <表示されたsource hash> \
  --expected-candidate-sha256 <表示されたcandidate hash>

poker-deliberate review-confirmed-intake --source hand.txt \
  --preparation preparation.json --confirmation confirmation.json --format summary
```

## 対応範囲

- UTF-8、BOMなし、LFのみ、NFCの単一source（最大1,000,000 bytes）。
- candidateと単一artifactは最大1,000,000 bytes、run全体は最大10,000,000 bytes。
- 完全なretrospective NLHE hand。欠落・未解決の曖昧性・512を超えるactionは拒否。
- opponent rangeはversion 1の範囲定義を最大1件。確認後に
  `range_validate`、成功時だけ`combos`を実行。
- `hand_validator`は必須。`hand_pot_ledger`は
  `generic_nlhe_cash_no_rake_v1`、cash、anteなし、明示的zero rakeだけ任意実行。
- source、candidate、confirmation、case、agent/tool evidence、final reportを
  `confirmed_review_provenance.json`で結合し、terminal readerが再計算。

候補のclaimは確認後も`USER_CLAIM`です。計算結果だけがtoolのexactness contractに従って
`CALCULATED`または`ESTIMATE`になり、provider文章は`UNKNOWN`のままです。

## Fail-closed境界

source/candidate/schema/extractor/authority/runの変更、期限切れ、cross-run replay、複数range、
legacy range、real-time支援、secret形状、外部provider、solver request、証拠を超えるreport claim、
artifact欠落または改ざんは拒否します。同一idempotency keyかつ完全に同じpayloadの再実行だけが
read-only replayになります。confirmationの有効期間は最大24時間です。

未実装なのは一般自然言語・site parser、image/PDF取込、外部model、Codex/Python runtime bridge、
外部solver、range equity、multiway range、GTO/均衡、署名/HMAC、revocation UIです。

## 決定論的評価

`tests/fixtures/confirmed_review/v1/scenarios.json`はrepository-owned MIT synthetic fixtureです。
`scripts/run_confirmed_review_evaluation.py`が17件の宣言済みケースを実行し、各ケースのobserved
evidenceがexpected evidenceと完全一致した場合だけexact score `1.0`を与えます。ケースや証拠の
欠落・重複・追加・不一致はfail closedで`0.0`です。この得点は自然言語抽出精度や戦略品質を
測りません。
