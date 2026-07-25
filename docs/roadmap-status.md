# RM status

この文書は`src/poker_deliberation/roadmap_status.json`から生成する追跡済みprojectionです。
RM実装状態の正はJSONであり、`user_materials/ROADMAP.md`やPROGRESS履歴ではありません。

- schema version: `1.1.0`
- source SHA-256: `38892fa5a8be2fed97c403e4f95082612c25adbab319f5217931b5b709f0e838`
- history baseline: Histories before the enclosing genesis commit are migrated status claims backed by listed evidence; append-only previous/current validation starts with that commit.
- `ready`は保存statusではなく、依存関係と人間承認から計算する派生表示です。
- release readinessはRM件数から推定せず、candidate固有のbuild/hash/matrix証拠を別途要求します。

## Status vocabulary

| status | meaning | legal next status |
|---|---|---|
| `proposed` | Candidate not yet accepted into an implementation plan. | `planned`, `superseded` |
| `planned` | Accepted scope with no implementation work in progress. | `in_progress`, `blocked`, `completed`, `superseded` |
| `in_progress` | An explicitly scoped implementation is active. | `planned`, `blocked`, `completed` |
| `blocked` | Progress is stopped by a recorded concrete blocker. | `planned`, `in_progress`, `superseded` |
| `completed` | Acceptance criteria have tracked code, test, or artifact evidence. | `in_progress`, `blocked` |
| `superseded` | Replaced by another RM and terminal unless governance is amended. | terminal |

Completedの再openは`in_progress`または`blocked`への遷移とし、理由と直前evidence digestをappend-only eventで同じ変更に記録します。再completedには全旧evidenceを保持したうえで新しいcommit/test/artifact evidenceを要求します。scope変更はschema amendmentを要します。`superseded`はgovernance amendmentなしにはterminalです。

The semantic scoped reapprovals admitted by this governance version are the exact chain `goal-rm010-p2-010b-2026-07-24` -> `goal-rm010-p2-010b-scope-revision-1-2026-07-24` -> `goal-rm010-p2-010b-scope-revision-2-2026-07-24` -> `goal-rm010-p2-010b-scope-revision-3-2026-07-24`. Every prior record remains immutable, each replacement must be newly appended with its pair-specific explicit-human-reapproval label and valid full-scope digest, and neither binding commit may change P2-010B progress or evidence.

## Phase 2 implementation milestones

RM-010〜013/024/027/028の実装順はitem-level依存ではなく、次の非循環milestone DAGを正とします。
entry milestoneとcompletion milestoneが異なるsplit RMをcompletedにする場合、親RMのordered completion evidenceはcompletion milestoneのevidenceと完全一致し、そのmilestoneの承認済みexact implementation scopeで検証されます。

| milestone | RM | state | dependencies | scope |
|---|---|---|---|---|
| `P2-024A` | `RM-024` | `completed` | `RM-006`, `RM-023` | Context envelope policy, schema, ownership, lineage, and allowlists. |
| `P2-010A` | `RM-010` | `completed` | `RM-006`, `RM-007`, `RM-023`, `P2-024A` | Typed pure phase services with serial execution and no persistence integration. |
| `P2-011A` | `RM-011` | `completed` | `RM-023`, `P2-010A` | Strict budget schema, fake clock, serial accounting, and retry classification. |
| `P2-027A` | `RM-027` | `completed` | `RM-023`, `P2-024A` | Classification, retention, expiry, quarantine, and disposition policy/schema only. |
| `P2-012A` | `RM-012` | `completed` | `RM-023`, `P2-010A`, `P2-011A`, `P2-027A` | Immutable revision storage, manifest, transaction, lock, recovery claim, and revision CAS foundation. |
| `P2-010B` | `RM-010` | `completed` | `P2-012A` | Phase service integration with durable transition ordering and fault injection. |
| `P2-011B` | `RM-011` | `completed` | `P2-012A` | Durable usage/resume, reservations, concurrency, cancellation, and RM-028 interface. |
| `P2-012B` | `RM-012` | `completed` | `P2-010B`, `P2-011B`, `P2-012A` | Completion marker, verified reader/status mapping, migration, and lifecycle hooks. |
| `P2-013A` | `RM-013` | `completed` | `P2-012B` | Approval actor, authority, action digest, request/decision idempotency, and CAS transaction. |
| `P2-027B` | `RM-027` | `not_started` | `P2-012B`, `P2-013A` | Authorized cleanup executor, dry-run digest, CAS, receipt, tombstone, and reconciliation. |
| `P2-013B` | `RM-013` | `not_started` | `P2-013A`, `P2-027B` | Resume integration, legacy reissue, expiry/revocation, and lifecycle binding. |
| `P2-028A` | `RM-028` | `not_started` | `P2-011B`, `P2-012B`, `P2-013B`, `P2-027B` | Approved isolation boundary, durable external-effect state, cancellation, and reconciliation. |

## Current RM state

| RM | title | phase | priority | status | dependencies | completion milestone | human approval |
|---|---|---|---|---|---|---|---|
| `RM-001` | Capability matrix and documentation synchronization | `phase-0` | `P0` | `completed` | none | `n/a` | `not_required` |
| `RM-002` | Provider capability truthfulness | `phase-0` | `P0` | `completed` | `RM-001` | `n/a` | `not_required` |
| `RM-003` | Canonical quality gate and temp policy | `phase-0` | `P0` | `completed` | `RM-001` | `n/a` | `not_required` |
| `RM-004` | Priority mathematical branch coverage | `phase-1` | `P0` | `completed` | `RM-003` | `n/a` | `not_required` |
| `RM-005` | Exactness and numeric tolerance specification | `phase-1` | `P0` | `completed` | `RM-004` | `n/a` | `not_required` |
| `RM-006` | Single source for typed tool contracts | `phase-1` | `P1` | `completed` | `RM-005` | `n/a` | `not_required` |
| `RM-007` | Mathematical oracle and metamorphic test pack | `phase-1` | `P1` | `completed` | `RM-005`, `RM-006` | `n/a` | `not_required` |
| `RM-008` | Offline public preflight | `phase-0` | `P0` | `completed` | `RM-001`, `RM-003` | `n/a` | `approved_scope` |
| `RM-009` | Complete Markdown ToolResult metadata | `phase-0` | `P0` | `completed` | `RM-005` | `n/a` | `not_required` |
| `RM-010` | Orchestrator phase services | `phase-2` | `P1` | `completed` | `RM-006`, `RM-007`, `RM-023`, `RM-024` | `P2-010B` | `approved_scope` |
| `RM-011` | Budget, retry, timeout, cancellation, and concurrency semantics | `phase-2` | `P1` | `completed` | `RM-023`, `RM-024` | `P2-011B` | `approved_scope` |
| `RM-012` | Versioned run manifest and failure atomicity | `phase-2` | `P1` | `completed` | `RM-023`, `RM-024` | `P2-012B` | `approved_scope` |
| `RM-013` | Approval and resume contract hardening | `phase-2` | `P1` | `in_progress` | `RM-012`, `RM-024` | `P2-013B` | `approved_scope` |
| `RM-014` | Versioned normalization grammar | `phase-3` | `P1` | `planned` | `RM-006`, `RM-012` | `n/a` | `pending` |
| `RM-015` | Hand rule profiles and side-pot accounting | `phase-3` | `P2` | `planned` | `RM-014` | `n/a` | `pending` |
| `RM-016` | Range grammar and provenance | `phase-3` | `P2` | `planned` | `RM-006`, `RM-014` | `n/a` | `pending` |
| `RM-017` | Executable evaluation harness | `phase-3` | `P1` | `planned` | `RM-006`, `RM-007`, `RM-012` | `n/a` | `pending` |
| `RM-018A` | Pre-release readiness | `pre-release` | `P1` | `planned` | `RM-001`, `RM-002`, `RM-003`, `RM-004`, `RM-005`, `RM-006`, `RM-007`, `RM-008`, `RM-009`, `RM-023` | `n/a` | `pending` |
| `RM-018B` | Stable release gate | `stable-release` | `P1` | `planned` | `RM-018A`, `RM-010`, `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-027` | `n/a` | `pending` |
| `RM-019` | Decision-gated OpenAI provider | `phase-4` | `P2` | `planned` | `RM-010`, `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-028` | `n/a` | `pending` |
| `RM-020` | External solver qualification adapter | `phase-4` | `P2` | `planned` | `RM-011`, `RM-012`, `RM-013`, `RM-017`, `RM-028` | `n/a` | `pending` |
| `RM-021` | Multiway and PLO equity feasibility | `phase-5` | `P3` | `planned` | `RM-006`, `RM-007` | `n/a` | `pending` |
| `RM-022` | Small imperfect-information research | `phase-5` | `P3` | `planned` | `RM-007`, `RM-017` | `n/a` | `pending` |
| `RM-023` | Roadmap and status single source of truth | `readiness` | `P0` | `completed` | `RM-001` | `n/a` | `approved_scope` |
| `RM-024` | Context lifecycle contract | `phase-2` | `P1` | `completed` | `RM-006`, `RM-023` | `P2-024A` | `approved_scope` |
| `RM-025` | Codex and Python agent runtime conformance | `post-phase-2` | `P2` | `proposed` | `RM-012`, `RM-013`, `RM-023`, `RM-024` | `n/a` | `pending` |
| `RM-026` | Framework extension SPI | `phase-3` | `P2` | `proposed` | `RM-006`, `RM-012`, `RM-023` | `n/a` | `pending` |
| `RM-027` | Local data lifecycle | `phase-2` | `P1` | `in_progress` | `RM-023`, `RM-024` | `P2-027B` | `approved_scope` |
| `RM-028` | Isolated solver and provider job control | `phase-2` | `P1` | `proposed` | `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-027` | `P2-028A` | `pending` |

## Synchronization contract

- `poker-deliberate doctor --format json`の`roadmap`はpackage resourceとして設定したJSONから計算します。source/editable checkoutは検証済みですが、wheel/sdist同梱はRM-018Aで候補ごとに検証します。
- `scripts/generate_roadmap_status.py --check`とcontract testがこのprojectionのdriftを検出します。
- PLANは現在の実行scopeを示し、PROGRESSは履歴だけを記録し、RM statusを再定義しません。
- ignoredの`user_materials/ROADMAP.md`は承認方針・背景説明でありruntime入力ではありません。
