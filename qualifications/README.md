# Public qualification artifacts

このdirectoryには、repository-owned public synthetic fixtureを用いた、秘密を含まないqualification evidenceを置きます。

- `historical/3b8772a587f270acccee32e33f3df68187dda418/`: そのcandidate commit/treeへ束縛された
  strict canonical V2 sealed live manifestとno-network exact-evidence評価結果です。bytesは変更せず、
  historical evidenceとして保存します。
- current canonical pathの`p2-025b-codex-subscription-v1.json`と
  `p2-025b-deterministic-evaluation-v1.json`は現在存在しません。このためcurrent qualificationは
  `UNKNOWN`、`subscription_live_qualified=false`であり、historical manifestをcurrent authorityにしません。
- raw CLI JSONL、raw model trace、reasoning trace、認証cache、token、API key、private hand historyは公開しません。
- fresh current manifestを将来公開する場合は、qualification実行commit/tree、runtime source inventory、
  role conformanceを固定し、current treeに対してpublic preflightで再検証します。非canonicalまたはinvalidな
  current manifestは`FAIL`であり、historical evidenceの再hashでは置き換えません。
- `openai_api`はこのmilestoneではlive qualificationを行わず、production-qualifiedとは表現しません。
