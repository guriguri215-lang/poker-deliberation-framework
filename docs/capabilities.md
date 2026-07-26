# 能力と実行状態

この文書は Phase 0/1 完了後の能力表示契約です。`poker-deliberate doctor --format json` の
`capabilities`、provider health、登録済みtool、追跡済みエージェント定義と照合します。
能力状態の正は`capabilities.py`、RM作業状態の正は`roadmap_status.json`であり、doctorは両者を
別fieldとして表示します。RM completedをruntime capability availableと同一視しません。

## 状態の定義

- **implemented**: 現在のローカル実行経路と回帰テストが存在する。
- **disabled**: 境界や設定項目は存在するが、通常経路で意図的に実行不能にしている。
- **unavailable**: 要求された能力を実行する実装・adapter・対象gameが同梱されていない。
- **planned**: ロードマップ候補であり、現在の実装済み能力ではない。

Providerの`available`は、現在`analyze`を実行できる場合だけ`true`です。providerの
`disabled`と`unavailable`はどちらも`available=false`ですが、前者は意図的な実行停止、後者は
実行前提の欠落を表します。CLI全体のdoctor `status=ok`は診断処理が完了したという意味で、
全能力がavailableという意味ではありません。

## Capability matrix

| capability ID | 状態 | 実行上の意味 |
|---|---|---|
| `local_calculators` | **implemented** | 登録済みローカルtoolをtyped `ToolResult`として実行する。 |
| `local_provider` | **implemented** | 境界検証用。文章的な専門分析やモデル推論は生成しない。 |
| `openai_agents_outbound` | **disabled** | `OpenAIAgentsProvider.analyze`は未実装。SDK/API keyの有無にかかわらず外部送信しない。 |
| `external_solver` | **unavailable** | `solver_status`は正直なUnavailableを返すだけで、外部solverを実行しない。 |
| `full_nlhe_equilibrium` | **unavailable** | full NLHE game tree、CFR、node locking、検証済み均衡計算はない。 |
| `heads_up_nlhe_equity` | **implemented** | heads-up NLHEに限り、上限付きexact enumerationまたはseed付きMonte Carloを実行する。 |
| `multiway_or_plo_equity` | **unavailable** | multiway equityとPLO equityは未対応。 |
| `documented_hand_parser` | **implemented** | 文書化したkey-value/player/action形式だけを保守的に正規化する。 |
| `natural_language_or_site_parser` | **unavailable** | 自然言語およびsite-specific hand history parserはない。 |
| `process_sandbox` | **unavailable** | 構造的hard capはあるがOS-level CPU/memory sandboxはない。 |
| `parallel_deliberation_and_tool_retry` | **disabled** | budget fieldは存在するが、通常のorchestrator経路は並列round/retryを実行しない。 |
| `runtime_conformance_contract` | **implemented** | P2-025Aの役割inventory、assignment/context/resultのversioned contract、pure比較、verified Python product projectionを提供する。実行bridgeではない。 |
| `codex_python_runtime_bridge` | **unavailable** | Codexネイティブ層とPythonオーケストレーター層は別実行面であり、Codex実行をPython runへ記録するbridgeはない。 |
| `local_data_lifecycle_policy` | **implemented** | P2-027Aのstrict versioned policy、canonical hash、pure lifecycle evaluationを実装する。filesystem mutationは行わない。 |
| `local_data_cleanup_executor` | **implemented** | P2-027BのPython APIは、承認済み1 runに対するbounded quarantine、遅延staged delete、immutable receipt/tombstone、revision CAS、idempotency、read-only reconciliationを実装する。cleanup CLI、automatic retry、secure eraseは実装しない。 |
| `immutable_revision_storage_foundation` | **implemented** | P2-012Aのimmutable revision、manifest、transaction、lock、recovery claim、revision CAS基盤と、P2-010Bの内部revision-only phase transition authorization seamを実装済み。通常のproduct runには未接続。 |
| `product_integrated_durable_run` | **implemented** | P2-012Bのmarker-last terminal publication、verified product reader/status、approval-checkpoint resume、read-only flat-v1 adapter、copy-only migration、durable budget settlement、lifecycle metadata integrationを実装済み。 |
| `phase_1_hardening` | **implemented** | typed tool contract、contract v2の数値区分、実行時verification、ローカルoracle/metamorphic testを実装済み。 |

## 20 tools、Codex 9役、Python 7役

- **FACT**: `default_registry()`と`tools/manifest.yaml`には`20`個のtool名があり、計算または
  capability照会の実行単位を表す。
- **FACT**: `.codex/agents/`の`9`定義はCodexネイティブの役割である。orchestratorと開発専用
  calculator builderを含む。
- **FACT**: `ROLE_CATALOG`の`7`役はPython orchestratorが分析を配分する役であり、Codexの
  9定義と同じ一覧ではない。
- **FACT**: `LocalProvider`はこれら7役へ文章的な専門分析を供給せず、空の結論と制限を返す。
  明示的に注入する`DeterministicMockProvider`はテスト用であり、外部モデル能力ではない。
- **FACT**: 名前の対応表はありますが、Pythonは`.codex/agents/*.toml`を起動しません。Codex側の
  sub-agent実行も`AgentExecutionRecord`やPython run artifactsへ自動的には取り込まれません。
- **FACT**: P2-025Aは両実行面の意味を別schemaで比較できるが、片方を起動したり、他方の監査記録を
  捏造したりしない。Codex側のtool catalogが宣言されていない場合は、空の権限を含め
  `undeclared`として保持する。

この数は品質指標ではありません。contract testは実装から件数を再計算し、文書との差を検出します。

## Game、parser、sandbox境界

- 主対象は事後のNLHE cash/tournament review。リアルタイム助言はfail-closedで拒否する。
- equityはheads-up NLHEだけ。ICMは指定したpayout model、小規模gameは明示した有限modelだけを扱う。
- free-text parserは文書化grammarだけを扱い、不明行を警告として保存する。
- toolはpayload/work/output capを持つが、in-process実行を強制停止するOS sandboxではない。
- solver実行、収束、対象game/rake/stackの一致がない結果をGTO・均衡・正確なrangeと表示しない。

## 品質とplatform

開発用venvを有効化した環境でのcanonical quality gateは次の4コマンドです。

```text
python -m pytest
ruff check .
ruff format --check .
mypy src
```

pytestの既定tempは`tests/conftest.py`により、ワークスペース内のignoredな
`.pytest-tmp/s-<process-hex>-<nonce>/`へ分離します。呼出側の明示`--basetemp`は上書きしません。
固定共有ディレクトリを再利用しないため、並行sessionが互いのtempを開始時に削除しません。

- **ASSUMPTION**: `requires-python >=3.11`を根拠に、候補matrixはCPython 3.11-3.13、Windowsと
  Ubuntuとする。
- **FACT**: 今回ローカルで実行する環境はWindows / CPython 3.12である。
- **FACT**: 自動temp名はWindowsのpath消費を抑えるため短縮し、session固有性を維持する。
- **UNKNOWN**: 深いclone先・深い明示`--basetemp`・long-path設定の異なるWindows環境。これらは
  `FileNotFoundError`等のOS path制約に影響され得るため、常時対応とは表示しない。
- **UNKNOWN**: ローカルで実行していないOS/Python行、release candidate全体、remote CIの結果。
- **UNKNOWN**: coverage thresholdは人間承認値がないため、現在のbaselineでは設定しない。

公開判断は[公開前チェックリスト](public-release-checklist.md)を参照してください。
