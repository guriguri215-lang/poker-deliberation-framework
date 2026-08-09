# 限定リバー検討の再開可能workflow

P3-030Dは、既存のP3-030C限定日本語river call-EV reviewと、既存のP2-025B固定5役bridgeを
1つのlocal-first workflowとして構成します。parser、range、poker calculator、role controllerを
再実装せず、明示確認、canonicalな状態保存、status、resume、replay、両実行面のlinkageを追加します。

## 実装範囲

- P3-030Cが受理する有限な日本語NLHE cash grammar、単一opponent range、river call/foldだけを扱う。
- workflow planを先に保存し、plan hashとP3-030Cの12個の独立hashを利用者が確認する。
- P3-030C terminal manifest/inventoryと、同じcommit/tree・auth modeに固定したP2-025B bridge
  manifest/inventoryを1つのcanonical linkageへ結ぶ。
- P3 terminal作成後またはbridge準備後に中断しても、検証済みstorageから`resume`できる。
- `status`と`replay`は、plan、preparation、confirmation、P3 terminal、bridge source projection、
  linkageを再検証する。replayはparser、calculator、provider、modelを再実行しない。

一般自然言語・site固有history・OCR、複数range、multiwayまたはearlier-street equity、rake、all-in、
side pot、外部solver、GTO/equilibrium、一般Codex/Python bridgeは範囲外です。

## Runtime mode

`--auth-mode`の既定値は`local_only`です。この場合、P3-030Cのローカル計算とterminal保存を完了し、
固定5役のbridge planだけを準備して`completed_local_only`になります。model runtime directory、API key、
保存済みChatGPT login、networkは使いません。

`codex_subscription`または`openai_api`を明示しても、このworkflowはrole transportを開始しません。
bridge準備後は`awaiting_role_review`となり、request表示、role別確認、実行、reconcileはP2-025Bの
既存CLIを別途使います。`openai_api` planには正の`--api-max-cost-micro-usd`が必要で、現在のadapterは
live-unqualifiedかつdefault-disabledです。

## CLIの流れ

`--workflow-root`には、候補commitの追跡済み`.gitignore`によって無視されるrepository配下の
専用ディレクトリを指定します。例は`tmp/runs/river-review-001`です。`--repository-commit`と
`--repository-tree`は、bridgeを準備するclean checkoutの候補commit/treeに一致させます。

```powershell
poker-deliberate prepare-bounded-river-review `
  --source .\hand-ja.txt --range .\range.json `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001 `
  --intake-id intake-001 --source-run-id source-run-001 --bridge-run-id bridge-run-001 `
  --source-id local-hand-001 --repository-commit <commit> --repository-tree <tree>
```

prepareのJSONには`plan_sha256`と`expected_hashes`が出ます。内容を確認した後、表示された値を
省略せず`confirm-bounded-river-review`の`--expected-plan-sha256`と12個の
`--expected-*-sha256`へ渡します。確認後の実行と状態確認は次の通りです。

```powershell
poker-deliberate run-bounded-river-review `
  --source .\hand-ja.txt --workflow-root .\tmp\runs\river-review-001 `
  --workflow-id river-review-001

poker-deliberate status-bounded-river-review `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001

poker-deliberate resume-bounded-river-review `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001

poker-deliberate replay-bounded-river-review `
  --workflow-root .\tmp\runs\river-review-001 --workflow-id river-review-001
```

P3 terminal作成前の`resume`には元の`--source`が必要です。terminal作成後はraw sourceをworkflowへ
複製せず、検証済みstorageから再開できます。

## 状態

| state | 意味 | next action |
|---|---|---|
| `awaiting_confirmation` | planとpreparationは保存済み、確認前 | `confirm` |
| `ready_to_run` | 確認済み、P3 terminal未作成 | `run` |
| `ready_to_resume` | P3 terminalまたはbridgeまで作成後に中断 | `resume` |
| `completed_local_only` | local計算、terminal、bridge plan、linkageが検証済み | `none` |
| `awaiting_role_review` | nonlocal modeのbridge planを準備済み | 既存bridge CLI |
| `role_review_in_progress` | 一部role結果を保存済み | 既存bridge CLI |
| `completed` / `failed` | bridge replayでterminal状態を確認済み | `none` |

## Local dataと評価

workflow plan、preparation、confirmation、linkage、bridge artifactは指定したGit管理外rootにだけ保存し、
raw日本語sourceをworkflow/bridge namespaceへ複製しません。認証情報の値はplan、status、errorへ保存・表示
しません。元sourceとrangeの保管・削除方針は呼出側が管理します。

repository-owned評価は、confirmation binding、exact decision math、runtime modeと固定role、
resume/replay、local-data separationの5 metricを独立に採点します。外部modelとsolverは実行しません。

```powershell
python scripts\run_bounded_river_review_workflow_evaluation.py --help
```
