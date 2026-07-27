# P3-017A offline evaluation contract

## 実装範囲

P3-017A は、repository-owned の synthetic fixture を使う deterministic な offline integrated
evaluation harness である。`poker-offline-evaluation-json-v1` を dataset、manifest、scorer、
suite、result の専用 canonicalization family とし、storage、approval、runtime conformance の
schema と hash domain を再利用しない。

- **FACT**: dataset は root `LICENSE` に束縛された MIT fixture で、外部 dataset を含まない。
- **FACT**: scorer は `exact-evidence-match` version `1.0.0`、direction
  `higher-is-better`、aggregation `micro-mean`、denominator `all-declared-cases`、
  invalid/missing count `fail-closed` である。
- **FACT**: 承認済み threshold は `1.0`。各 case の expected evidence tuple と actual evidence
  tuple が byte-order を含め完全一致した場合だけ、その case の numerator は 1 になる。
- **CALCULATED**: suite score は `matched_case_count / declared_case_count`。primary evidence は
  integer numerator/denominator であり、binary floating-point を判定根拠にしない。
- **UNKNOWN**: `evals/metrics.json` に列挙された主観的戦略 metric、人間 rubric、外部 dataset
  baseline は未実装である。

この milestone は provider、network、外部 solver、Codex/Python runtime bridge、process sandbox、
product run storage を起動または変更しない。solver evidence がない GTO、均衡、正確な range の
主張は unsupported fixture として拒否する。

## Versioned inputs

`evals/datasets/p3_017a/v1/manifest.json` は dataset ID/version、repository ownership、
SPDX license、license path/hash、case path/count、domain-separated content hash を記録する。
`cases.json` の各 case は ID、kind、strict input、expected evidence を持つ。

`evals/scorers/exact_evidence_match_v1.json` は scorer/version、metric、direction、aggregation、
denominator、invalid/missing count policy、threshold、human-rubric absence を記録する。
`evals/suites/p3_017a_v1.json` は dataset manifest と scorer の exact file hash、固定された
evaluation time、および network/provider/solver/bridge がすべて `false` であることを束縛する。

generator は canonical byte 列を生成し、改行、BOM、duplicate key、unknown field、非有限数、
unknown version、hash/count/license mismatch を reader が fail closed で拒否する。

```powershell
.\.venv\Scripts\python.exe scripts\generate_offline_evaluation_fixtures.py --check
```

## Integrated evidence

normal case は同じ evaluation outcome 内で次を束縛する。

- repository から機械的に構築した Codex/Python runtime inventory hash
- Python runtime routing と context/provenance の cross-runtime conformance
- `runtime_bridge=false` と `external_effect=false`
- registry から実行した typed `ToolResult`
- tool contract version、input/output domain hash、numeric exactness、verification status、
  reproduction command
- `pot_odds` の verified floating output と独立した rational oracle `1/4`

negative case は context/provenance、role/allowlist、calculator oracle、missing denominator、
missing scorer、missing version、unsupported solver claim、synthetic secret-shaped metadata、
bounded timeout を structured failure と expected evidence に変換する。expected failure であっても
evidence が一致しなければ scorer は 0 を与える。timeout は実 sleep や process kill を行わない
synthetic bound check であり、OS sandbox の実装を意味しない。

## Result and reproduction

result は source Git commit/tree ID、suite/manifest/dataset/scorer hash、全 tool contract hash と
version、Codex/Python runtime inventory hash、各 case outcome、structured failure、summary を
含む。動的な calculator timestamp/result ID は score evidence に使わず、同じ source binding と
suite から同一 canonical result bytes を得る。

output は repository-relative の ignored `tmp/` 配下に限定する。

```powershell
.\.venv\Scripts\python.exe scripts\run_offline_evaluation.py `
  --source-commit COMMIT_SHA `
  --source-tree TREE_SHA `
  --output tmp/goals/P3-017A/evaluation-runs/result.json
```

終了コード 0 は exact threshold pass、1 は valid evaluation result の threshold fail、2 は
invocationまたはsuite load failureである。この判定は repository release readiness や
未実装 metric の合格を意味しない。
