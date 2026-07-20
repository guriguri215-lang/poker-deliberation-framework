# 実装計画

## RM-024 / P2-024A Context lifecycle（2026-07-20）

- **FACT**: `goal-rm024-p2-024a-2026-07-20`を人間承認の一次記録とし、RM-024の
  immutable scopeと11件のpolicy decisionをcanonical approval scopeへbindingした。
- **CALCULATED**: approval scope digestはcanonical JSONからコードで算出した
  `50b14fcf598111d6462d6046ded5ccdd01835a1b71536abe96a55de94c645069`である。
- **FACT**: RM-024は`proposed -> planned`、P2-024Aは`not_started`である。次の独立commitで
  RM-024とP2-024Aを`in_progress`へ進めてから、実装・レビュー・completion evidenceを行う。
- **FACT**: governance validatorは、承認済み`proposed -> planned`時の一度限りのscope freeze、
  HEAD ancestor/tree/change-bound evidence、1対1 RM/milestone evidence parityを要求する。
- **NON-GOAL**: persistence、retention期間の決定、cleanup executor、external provider/solver、
  dependency追加、Codex/Python bridge、RM-010〜013、RM-027本体、parallel executionは実装しない。

## Phase 2 readiness（2026-07-20）

- **FACT**: RM実装状態のsingle source of truthは
  `src/poker_deliberation/roadmap_status.json`とする。生成projectionは
  `docs/roadmap-status.md`、doctorはpackage resourceとして設定したJSONからsummaryを計算する。
  source/editable checkoutは検証対象だが、wheel/sdist同梱はRM-018Aまで`UNKNOWN`とする。
- **FACT**: RM-010〜013本体は今回実装せず、`docs/phase2-readiness-contracts.md`で
  phase/budget/run/approvalの実装前contractを、人間承認事項を条件としてfreezeする。
- **FACT**: RM-018はpre-release前の`RM-018A`とPhase 2後stable tag前の`RM-018B`へ分割する。
- **FACT**: このreadiness履歴ではRM-023を実装し、RM-024〜028を候補として`proposed`に保った。
  現在のRM-024状態は上のContext lifecycle節とcanonical JSONを正とする。
- **FACT**: `user_materials/ROADMAP.md`は承認方針・説明文書であり、runtime、doctor、
  generated docsの入力にしない。編集案はGitへstage/commitしない。
- **NON-GOAL**: Phase 2本体、external provider/solver、dependency追加、fetch、PR、tag、release、
  repository公開は行わない。

現在の工程はこのfileのchecklistではなくgoal planで管理する。RMの現在状態はcanonical JSONを参照する。

## Phase 1再監査時の基準（履歴）

- **FACT**: 再監査開始時のbranchは`codex/phase1-math-contracts`、HEADは
  `ca8c9ab5c24f5a4416ed3bcfde3d0fb9028a3976`で、ローカルupstreamとの差は0/0、
  tracked/untracked差分はなかった。Git fetchは実行していない。
- **USER_CLAIM**: `user_materials/ROADMAP.md`は2026-07-19 JSTに人間承認済み。
- **FACT**: tracked実装ではPhase 0のRM-001〜003・008〜009と、Phase 1のRM-004〜007が
  実装済みである。`user_materials/ROADMAP.md`の全項目PROPOSED表示はこの状態へ未同期である。
- **FACT**: 今回のscopeは、push後HEADに対する能力・文書・CLI・Git状態の再監査と、確認できた
  表示矛盾の最小修正だけである。
- **FACT**: この節が記録するPhase 1再監査scopeでは、外部通信、依存追加、solver/API実行、
  commit、tag、push、PR、release、公開を行わない方針だった。現在のreadiness goalでは承認済みの
  独立commit/pushだけを別工程として行う。

## Phase 0/1実装状態（履歴）

- [x] Phase 0: capability truthfulness、provider境界、canonical quality gate、offline public
  preflight、Markdown ToolResult metadataを実装した。
- [x] Phase 1: 重要数学分岐、contract v2の`numeric_exactness`とtolerance、20 toolのtyped
  definition/manifest parity、ローカルoracle/metamorphic testsを実装した。
- [x] Codex 9定義とPython 7役を別一覧として表示した。
- [ ] Codex実行とPython orchestrator/run artifactsを接続するruntime bridgeは未実装である。

## Phase 1後の再監査（履歴）

- [x] Git基準点、最終Phase 1 commit、tracked/untracked/ignored状態を確認する。
- [x] PLAN/PROGRESS/README/docs、capability/doctor、orchestrator/config/schema/run store、
  agent/Skill定義、tests、evals、関連する承認済みroadmap範囲を照合する。
- [x] 修正前HEADで4品質ゲートとCLI smokeを実測する。
- [x] `phase_1_hardening=planned`等、現在実装と矛盾する表示だけを修正する。
- [x] 修正後の4品質ゲート、CLI、Git差分を再確認する。
- [x] roadmap本文は編集せず、status/release順序/新規候補の変更案を人間へ提示する。

## 品質ゲート

```text
python -m pytest
ruff check .
ruff format --check .
mypy src
```

bare commandは開発用venvを有効化して実行する。venvを有効化しない場合は
`.\.venv\Scripts\python.exe -m pytest`、`.\.venv\Scripts\ruff.exe`、
`.\.venv\Scripts\mypy.exe`を使用する。追加でdoctor/list-tools/list-agents、contract v2の
pot-odds、unavailable solverのCLI smokeを実行する。

**ASSUMPTION**: supported候補は`requires-python >=3.11`に基づくCPython 3.11-3.13、Windowsと
Ubuntu。今回実行しないmatrix行は`UNKNOWN`であり、成功扱いにしない。coverage thresholdは
人間承認値がないため追加しない。

## 非目標

Phase 2以降のorchestrator分割、context lifecycle、Codex/Python runtime bridge、extension SPI、
local data lifecycle、外部provider/solverのprocess隔離、full NLHE solver/CFR、multiway/PLO equity、
dependency変更、履歴書換え、公開操作は対象外とする。

## 履歴の扱い

2026-07-17の初期構築計画とPhase 0/1の過去結果は履歴であり、現在の能力判断には現HEADのコード、
doctor、contract test、今回の実行結果を優先する。過去のtest数やcoverage値を現在の成功として
コピーしない。
