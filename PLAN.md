# 実装計画

## 現在の基準

- **FACT**: Phase 0着手時の基準は`main`、HEAD
  `b149600e422ad2404a74348650a234f9b8de03bb`、tracked/untracked差分なし。
- **USER_CLAIM**: `user_materials/ROADMAP.md`は2026-07-19 JSTに人間承認済み。
- **FACT**: 今回のscopeはRM-001、RM-002、RM-003、RM-008、RM-009だけ。
- **FACT**: 外部通信、依存追加、solver/API実行、commit、tag、push、PR、release、公開は行わない。

## Phase 0作業順

- [x] AGENTS.md、承認済みroadmap、Git初期状態を確認し、既存差分がないことを確認する。
- [x] RM-001: capability stateをコード/doctor/docsへ集約し、20 tools・Codex 9役・Python 7役を区別する。
- [x] RM-002: providerのavailable/unavailable/disabled契約とSDK/key 4組合せをテストする。
- [x] RM-003: pytest tempをworkspace-localかつsession固有にし、4 quality gateを統一する。
- [x] RM-008: tracked/public候補/historyを対象とするoffline preflightと安全境界テストを追加する。
- [x] RM-009: MarkdownへToolResult metadataを欠落なく表示し、4状態のgolden testを追加する。
- [x] targeted/full pytest、Ruff lint/format、mypy、CLI smoke、実repository preflightを完走する。
- [x] 最終Git状態とignored成果物を確認し、Phase 0完了可否を判定する。

## 品質ゲート

```text
python -m pytest
ruff check .
ruff format --check .
mypy src
```

追加でprovider/doctor、capability contract、pytest temp、public preflight、Markdown rendererの
targeted tests、doctor/list-tools/pot_odds/reviewのCLI smoke、offline preflightを実行する。

**ASSUMPTION**: supported候補は`requires-python >=3.11`に基づくCPython 3.11-3.13、Windowsと
Ubuntu。今回実行しないmatrix行は`UNKNOWN`であり、成功扱いにしない。coverage thresholdは
人間承認値がないため追加しない。

## 非目標

Phase 1以降、full NLHE solver/CFR、multiway/PLO equity、外部provider/solver、dependency変更、
履歴書換え、公開操作は対象外。実装に不可欠でない変更はPhase 1候補として残す。

## 履歴の扱い

2026-07-17の初期構築計画は当時の履歴であり、現在の能力判断にはコード、doctor、contract test、
今回の実行結果を優先する。過去のtest数やcoverage値を現在の成功としてコピーしない。
