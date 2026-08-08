# Public qualification artifacts

このdirectoryには、repository-owned public synthetic fixtureを用いた、秘密を含まないqualification manifestだけを置きます。

- `p2-025b-codex-subscription-v1.json`: P2-025Bの`codex_subscription` actual-live qualificationに成功したcanonical sanitized manifestです。
- `p2-025b-deterministic-evaluation-v1.json`: 同じqualification commit/treeに束縛したno-network exact-evidence評価結果です。
- raw CLI JSONL、raw model trace、reasoning trace、認証cache、token、API key、private hand historyは公開しません。
- manifestはqualification実行commit/treeとruntime source inventoryを固定し、最終公開commitでは同じinventoryを再検証します。
- `openai_api`はこのmilestoneではlive qualificationを行わず、production-qualifiedとは表現しません。
