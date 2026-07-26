# Public roadmap status

この文書は`src/poker_deliberation/roadmap_status.json`から生成する公開projectionです。
公開中の実装状態、依存関係、能力scope、受入条件、milestone、decision rationaleを示します。

- schema version: `2.0.0`
- source SHA-256: `e0c6dc2e69a8c71da42951fb60aa7509d1e67c6f81f69e02a5f7b59fef32f13b`
- `ready`は依存関係だけから計算し、decision gateの完了を意味しません。
- release readinessはRM件数から推定せず、candidate固有のbuild/hash/matrix証拠を別途要求します。

## Status vocabulary

| status | meaning | legal next status |
|---|---|---|
| `proposed` | Candidate under consideration. | `planned`, `superseded` |
| `planned` | Public scope is listed and implementation has not started. | `in_progress`, `blocked`, `completed`, `superseded` |
| `in_progress` | Implementation is underway within the published scope. | `planned`, `blocked`, `completed` |
| `blocked` | Implementation cannot proceed for the reason recorded in status_reason. | `planned`, `in_progress`, `superseded` |
| `completed` | The current tree satisfies the published acceptance criteria and tests. | `in_progress`, `blocked` |
| `superseded` | Replaced by another roadmap item. | terminal |

## Milestone status vocabulary

| status | meaning | legal next status |
|---|---|---|
| `not_started` | Milestone implementation has not started. | `in_progress`, `blocked` |
| `in_progress` | Milestone implementation is underway. | `not_started`, `blocked`, `completed` |
| `blocked` | Milestone implementation cannot proceed for the recorded reason. | `not_started`, `in_progress` |
| `completed` | The current tree satisfies the milestone scope. | terminal |

## Phase 2 implementation milestones

| milestone | RM | status | dependencies | scope | status reason |
|---|---|---|---|---|---|
| `P2-024A` | `RM-024` | `completed` | `RM-006`, `RM-023` | Context envelope policy, schema, ownership, lineage, and allowlists. | Context lifecycle contracts are implemented and covered by the declared tests. |
| `P2-010A` | `RM-010` | `completed` | `RM-006`, `RM-007`, `RM-023`, `P2-024A` | Typed pure phase services with serial execution and no persistence integration. | Typed pure phase services are implemented; P2-010B provides the internal durable integration seam. |
| `P2-011A` | `RM-011` | `completed` | `RM-023`, `P2-010A` | Strict budget schema, fake clock, serial accounting, and retry classification. | Strict budget schemas, injected clocks, serial accounting, and retry classification are implemented. |
| `P2-027A` | `RM-027` | `completed` | `RM-023`, `P2-024A` | Classification, retention, expiry, quarantine, and disposition policy/schema only. | The versioned local-data policy and pure lifecycle evaluation are implemented without filesystem mutation. |
| `P2-012A` | `RM-012` | `completed` | `RM-023`, `P2-010A`, `P2-011A`, `P2-027A` | Immutable revision storage, manifest, transaction, lock, recovery claim, and revision CAS foundation. | Immutable structural revisions, manifests, locking, recovery claims, and revision CAS are implemented. |
| `P2-010B` | `RM-010` | `completed` | `P2-012A` | Phase service integration with durable transition ordering and fault injection. | The internal structural-nonterminal revision seam and authorized phase transition ordering are implemented. |
| `P2-011B` | `RM-011` | `completed` | `P2-012A` | Durable usage/resume, reservations, concurrency, cancellation, and RM-028 interface. | Durable budget state, reservations, settlement, bounded execution, cancellation, and retry contracts are implemented. |
| `P2-012B` | `RM-012` | `completed` | `P2-010B`, `P2-011B`, `P2-012A` | Completion marker, verified reader/status mapping, migration, and lifecycle hooks. | Marker-last terminal publication, verified readers, legacy adapters, migration, budget settlement, and lifecycle hooks are implemented. |
| `P2-013A` | `RM-013` | `completed` | `P2-012B` | Approval actor, authority, action digest, request/decision idempotency, and CAS transaction. | Strict approval authority, action binding, decision idempotency, CAS publication, and bounded failure audit are implemented. |
| `P2-027B` | `RM-027` | `completed` | `P2-012B`, `P2-013A` | Authorized cleanup executor, dry-run digest, CAS, receipt, tombstone, and reconciliation. | The additive authorized cleanup API implements quarantine, staged deletion, receipts, tombstones, CAS, and reconciliation. |
| `P2-013B` | `RM-013` | `completed` | `P2-013A`, `P2-027B` | Resume integration, legacy reissue, expiry/revocation, and lifecycle binding. | Explicit historical and expired-request reissue, replay-first CAS resume integration, immutable lifecycle binding, and effect-free expiry/revocation pre-execution rechecks are implemented. |
| `P2-028A` | `RM-028` | `not_started` | `P2-011B`, `P2-012B`, `P2-013B`, `P2-027B` | Approved isolation boundary, durable external-effect state, cancellation, and reconciliation. | Not started. |

## Current RM state

| RM | title | phase | priority | status | dependencies | completion milestone | decision gate |
|---|---|---|---|---|---|---|---|
| `RM-001` | Capability matrix and documentation synchronization | `phase-0` | `P0` | `completed` | none | `n/a` | `none` |
| `RM-002` | Provider capability truthfulness | `phase-0` | `P0` | `completed` | `RM-001` | `n/a` | `none` |
| `RM-003` | Canonical quality gate and temp policy | `phase-0` | `P0` | `completed` | `RM-001` | `n/a` | `none` |
| `RM-004` | Priority mathematical branch coverage | `phase-1` | `P0` | `completed` | `RM-003` | `n/a` | `none` |
| `RM-005` | Exactness and numeric tolerance specification | `phase-1` | `P0` | `completed` | `RM-004` | `n/a` | `none` |
| `RM-006` | Single source for typed tool contracts | `phase-1` | `P1` | `completed` | `RM-005` | `n/a` | `none` |
| `RM-007` | Mathematical oracle and metamorphic test pack | `phase-1` | `P1` | `completed` | `RM-005`, `RM-006` | `n/a` | `none` |
| `RM-008` | Offline public preflight | `phase-0` | `P0` | `completed` | `RM-001`, `RM-003` | `n/a` | `required` |
| `RM-009` | Complete Markdown ToolResult metadata | `phase-0` | `P0` | `completed` | `RM-005` | `n/a` | `none` |
| `RM-010` | Orchestrator phase services | `phase-2` | `P1` | `completed` | `RM-006`, `RM-007`, `RM-023`, `RM-024` | `P2-010B` | `required` |
| `RM-011` | Budget, retry, timeout, cancellation, and concurrency semantics | `phase-2` | `P1` | `completed` | `RM-023`, `RM-024` | `P2-011B` | `required` |
| `RM-012` | Versioned run manifest and failure atomicity | `phase-2` | `P1` | `completed` | `RM-023`, `RM-024` | `P2-012B` | `required` |
| `RM-013` | Approval and resume contract hardening | `phase-2` | `P1` | `completed` | `RM-012`, `RM-024` | `P2-013B` | `required` |
| `RM-014` | Versioned normalization grammar | `phase-3` | `P1` | `planned` | `RM-006`, `RM-012` | `n/a` | `required` |
| `RM-015` | Hand rule profiles and side-pot accounting | `phase-3` | `P2` | `planned` | `RM-014` | `n/a` | `required` |
| `RM-016` | Range grammar and provenance | `phase-3` | `P2` | `planned` | `RM-006`, `RM-014` | `n/a` | `required` |
| `RM-017` | Executable evaluation harness | `phase-3` | `P1` | `planned` | `RM-006`, `RM-007`, `RM-012` | `n/a` | `required` |
| `RM-018A` | Pre-release readiness | `pre-release` | `P1` | `planned` | `RM-001`, `RM-002`, `RM-003`, `RM-004`, `RM-005`, `RM-006`, `RM-007`, `RM-008`, `RM-009`, `RM-023` | `n/a` | `required` |
| `RM-018B` | Stable release gate | `stable-release` | `P1` | `planned` | `RM-018A`, `RM-010`, `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-027` | `n/a` | `required` |
| `RM-019` | Decision-gated OpenAI provider | `phase-4` | `P2` | `planned` | `RM-010`, `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-028` | `n/a` | `required` |
| `RM-020` | External solver qualification adapter | `phase-4` | `P2` | `planned` | `RM-011`, `RM-012`, `RM-013`, `RM-017`, `RM-028` | `n/a` | `required` |
| `RM-021` | Multiway and PLO equity feasibility | `phase-5` | `P3` | `planned` | `RM-006`, `RM-007` | `n/a` | `required` |
| `RM-022` | Small imperfect-information research | `phase-5` | `P3` | `planned` | `RM-007`, `RM-017` | `n/a` | `required` |
| `RM-023` | Roadmap and status single source of truth | `readiness` | `P0` | `completed` | `RM-001` | `n/a` | `required` |
| `RM-024` | Context lifecycle contract | `phase-2` | `P1` | `completed` | `RM-006`, `RM-023` | `P2-024A` | `required` |
| `RM-025` | Codex and Python agent runtime conformance | `post-phase-2` | `P2` | `proposed` | `RM-012`, `RM-013`, `RM-023`, `RM-024` | `n/a` | `required` |
| `RM-026` | Framework extension SPI | `phase-3` | `P2` | `proposed` | `RM-006`, `RM-012`, `RM-023` | `n/a` | `required` |
| `RM-027` | Local data lifecycle | `phase-2` | `P1` | `completed` | `RM-023`, `RM-024` | `P2-027B` | `required` |
| `RM-028` | Isolated solver and provider job control | `phase-2` | `P1` | `proposed` | `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-027` | `P2-028A` | `required` |

## Public item contracts

### RM-001 — Capability matrix and documentation synchronization

- Status: `completed`
- Status reason: Phase 0 capability truthfulness is implemented and contract-tested.
- Objective: Keep implemented, disabled, unavailable, and planned capability claims aligned with executable behavior.
- Capabilities:
  - local_calculators
  - local_provider
  - openai_agents_outbound
  - external_solver
- Targets:
  - src/poker_deliberation/capabilities.py
  - docs/capabilities.md
  - src/poker_deliberation/cli.py
- Acceptance criteria:
  - Doctor, documentation, and code expose the same capability states.
- Tests:
  - tests/integration/test_capability_contract.py

### RM-002 — Provider capability truthfulness

- Status: `completed`
- Status reason: The four package/key combinations are tested and do not overstate capability.
- Objective: Never infer outbound availability from package or key presence alone.
- Capabilities:
  - local_provider
  - openai_agents_outbound
- Targets:
  - src/poker_deliberation/providers
  - src/poker_deliberation/cli.py
- Acceptance criteria:
  - OpenAI provider remains disabled until outbound analyze is implemented and approved.
- Tests:
  - tests/unit/test_provider_availability.py
  - tests/integration/test_capability_contract.py

### RM-003 — Canonical quality gate and temp policy

- Status: `completed`
- Status reason: Canonical commands and workspace-local pytest temp isolation are implemented.
- Objective: Make the four local quality gates reproducible without unsafe shared temp cleanup.
- Capabilities:
  - none
- Targets:
  - pyproject.toml
  - tests/conftest.py
  - scripts/check_quality.ps1
- Acceptance criteria:
  - Pytest, Ruff lint, Ruff format check, and mypy run in the supported local workspace.
- Tests:
  - tests/integration/test_quality_config.py

### RM-004 — Priority mathematical branch coverage

- Status: `completed`
- Status reason: The approved priority branches and regressions are implemented.
- Objective: Exercise high-risk numeric, normalization, evidence, and provider branches with known outcomes.
- Capabilities:
  - phase_1_hardening
- Targets:
  - src/poker_deliberation/tools
  - src/poker_deliberation/normalization.py
  - src/poker_deliberation/research/evidence.py
- Acceptance criteria:
  - Priority failure, fallback, warning, and boundary branches have regression tests.
- Tests:
  - tests/unit/test_phase1_priority_branches.py

### RM-005 — Exactness and numeric tolerance specification

- Status: `completed`
- Status reason: Contract v2 exactness and runtime verification are present.
- Objective: Define exact, exact-under-model, floating-verified, approximate, and unavailable results without false precision.
- Capabilities:
  - phase_1_hardening
- Targets:
  - src/poker_deliberation/schemas.py
  - src/poker_deliberation/tools/verification.py
  - docs/calculation-policy.md
- Acceptance criteria:
  - Contract v2 records numeric exactness, tolerance, model qualifiers, and executable verification.
- Tests:
  - tests/integration/test_tool_contracts.py
  - tests/property/test_phase1_math_oracles.py

### RM-006 — Single source for typed tool contracts

- Status: `completed`
- Status reason: All 20 tools expose contract version 2.0.0 with typed schemas.
- Objective: Keep all registered tool input, output, assumptions, limits, and versions in one typed definition set.
- Capabilities:
  - local_calculators
  - phase_1_hardening
- Targets:
  - src/poker_deliberation/tools/contracts.py
  - tools/manifest.yaml
  - docs/tool-contracts.md
- Acceptance criteria:
  - Registry, generated manifest, and generated documentation have full contract parity.
- Tests:
  - tests/integration/test_tool_contracts.py

### RM-007 — Mathematical oracle and metamorphic test pack

- Status: `completed`
- Status reason: Local oracle and metamorphic regression coverage is implemented.
- Objective: Cross-check calculators with known answers, independent local oracles, invariants, and metamorphic relations.
- Capabilities:
  - phase_1_hardening
- Targets:
  - tests/property/test_phase1_math_oracles.py
  - tests/fixtures/phase1
- Acceptance criteria:
  - Priority calculators have deterministic oracle, boundary, and reproducibility coverage.
- Tests:
  - tests/property/test_phase1_math_oracles.py

### RM-008 — Offline public preflight

- Status: `completed`
- Status reason: The offline scanner and synthetic fixtures are implemented; publication remains a separate decision.
- Objective: Audit tracked worktree and Git history without network access or destructive history changes.
- Capabilities:
  - none
- Targets:
  - src/poker_deliberation/public_preflight.py
  - scripts/public_preflight.py
- Acceptance criteria:
  - Tracked/history secret, PII, license, artifact, and exclusion checks fail closed.
- Tests:
  - tests/unit/test_public_preflight.py
  - tests/integration/test_public_preflight_history.py
- Decision gate rationale:
  - offline audit only; no publication or history rewrite

### RM-009 — Complete Markdown ToolResult metadata

- Status: `completed`
- Status reason: Golden coverage protects the required Markdown result metadata.
- Objective: Render ToolResult status, exactness, assumptions, warnings, uncertainty, errors, and reproduction data without silent loss.
- Capabilities:
  - local_calculators
- Targets:
  - src/poker_deliberation/reporting/markdown.py
- Acceptance criteria:
  - JSON and Markdown preserve the required result metadata for every result state.
- Tests:
  - tests/golden/test_markdown_tool_results.py

### RM-010 — Orchestrator phase services

- Status: `completed`
- Status reason: P2-010A pure phase services and the P2-010B internal durable transition seam are implemented; ordinary product execution remains serial.
- Objective: Split orchestration into typed, independently testable phases while preserving public behavior.
- Capabilities:
  - none
- Targets:
  - src/poker_deliberation/orchestrator.py
  - src/poker_deliberation/phases
  - docs/phase-services.md
- Acceptance criteria:
  - Pure phase services do not mutate state or artifacts; the orchestrator alone requests transitions after durable writes.
  - The P2-010B opt-in seam publishes a verified structural-nonterminal revision before authorizing the same-process final transition.
  - Ordinary run, resume, show, load_report, and report_path use the separate P2-012B terminal protocol without enabling external execution, parallel scheduling, or automatic retry.
- Tests:
  - tests/unit/test_phase_contracts.py
  - tests/unit/test_pure_phase_services.py
  - tests/property/test_phase_properties.py
  - tests/integration/test_phase_orchestration.py
  - tests/adversarial/test_phase_boundaries.py
  - tests/fault/test_phase_failures.py
  - tests/unit/test_phase_revision_coordinator.py
  - tests/property/test_phase_revision_coordinator_properties.py
  - tests/integration/test_phase_revision_coordinator.py
  - tests/adversarial/test_phase_revision_coordinator_security.py
  - tests/fault/test_phase_revision_coordinator_failures.py
  - tests/concurrency/test_phase_revision_coordinator_concurrency.py
  - tests/characterization/test_orchestrator_phase_baseline.py
- Decision gate rationale:
  - Implement typed phase schemas, pure phase services, and only the minimum connection to the current serial orchestrator.
  - Preserve the calculation assignment artifact, including the currently assigned but unexecuted report-writer, without deletion, rename, or schema change.
  - Treat the P2-024A context retention and classification contract as authoritative and add no P2-010A retention, deletion, quarantine, cleanup, or persistence semantics.
  - Preserve the current attempt-memory-only-v1 semantics and defer retention or disposition changes to RM-027A/B.
  - Treat the phase schema as a versioned internal API and do not declare it a stable public API.
  - Preserve Orchestrator.run, resume, load_report, CLI, exit codes, CaseInput, FinalReport, ToolResult, and existing artifact meanings.
  - Pure phases do not directly access filesystem, state machine, provider, tool, network, approval ledger, ambient clock, or ambient randomness.
  - Provide time, IDs, policy, and capability data as typed inputs or explicitly injected values.
  - Isolate Analysis and ToolResearch effects behind explicit executor or adapter boundaries without changing serial execution semantics.
  - Allow phases to return requested next state and artifact intent only as values without executing transitions or writes.
  - Keep existing write and transition responsibility in the orchestrator and do not anticipate RM-012 transactions, manifests, locks, recovery, or CAS.
  - Do not add automatic retry, parallel execution, budget accounting, timeout or cancellation redesign, durable resume, or approval CAS.
  - Do not add dependencies, external providers, external solvers, or a Codex/Python runtime bridge.
- Relations:
  - P2-010B is an internal structural revision seam; the normal product terminal path is P2-012B.

### RM-011 — Budget, retry, timeout, cancellation, and concurrency semantics

- Status: `completed`
- Status reason: P2-011A and the internal-only P2-011B durable budget contracts are implemented; ordinary product execution remains serial, automatic retry remains disabled, and process sandboxing remains unavailable.
- Objective: Make every active budget field finite, enforceable, auditable, and fail-closed or remove it from active configuration.
- Capabilities:
  - parallel_deliberation_and_tool_retry
  - process_sandbox
- Targets:
  - src/poker_deliberation/budgets
  - src/poker_deliberation/config.py
  - src/poker_deliberation/state_machine.py
  - src/poker_deliberation/orchestrator.py
  - docs/budget-execution-contract.md
- Acceptance criteria:
  - Unknown, zero, or over-cap external cost prevents external calls while free local execution remains valid; accounting uses injected clocks and finite bounded schemas.
  - Durable reservations and settlement are revision-bound and idempotent, with typed retry and cancellation outcomes that never promote unconfirmed cancellation to success.
  - The internal bounded executor does not enable ordinary parallel deliberation, automatic retry, an external provider, or an OS process sandbox.
- Tests:
  - tests/unit/test_budget_contracts.py
  - tests/property/test_budget_properties.py
  - tests/integration/test_budget_orchestration.py
  - tests/adversarial/test_budget_boundaries.py
  - tests/fault/test_budget_failures.py
  - tests/unit/test_durable_budget_contracts.py
  - tests/unit/test_durable_budget_store.py
  - tests/unit/test_durable_retry_execution.py
  - tests/property/test_durable_budget_properties.py
  - tests/integration/test_durable_budget_storage.py
  - tests/integration/test_durable_budget_execution.py
  - tests/adversarial/test_durable_budget_security.py
  - tests/fault/test_durable_budget_failures.py
  - tests/concurrency/test_durable_budget_concurrency.py
  - tests/characterization/test_durable_budget_compatibility.py
- Decision gate rationale:
  - whether parallel deliberation is a product capability
  - field removal and deprecation policy
  - cost precision
- Relations:
  - P2-011B provides internal durable accounting contracts while the public capability parallel_deliberation_and_tool_retry remains disabled.

### RM-012 — Versioned run manifest and failure atomicity

- Status: `completed`
- Status reason: P2-012A immutable structural revisions and the P2-012B marker-last product terminal protocol are implemented; flat-v1 copies remain legacy_unverified and no release action is implied.
- Objective: Distinguish completed, incomplete, corrupt, unsupported, and resumable runs with integrity evidence.
- Capabilities:
  - none
- Targets:
  - src/poker_deliberation/storage/run_store.py
  - src/poker_deliberation/storage/revision_store.py
  - src/poker_deliberation/storage/terminal_store.py
  - src/poker_deliberation/storage/legacy_migration.py
  - src/poker_deliberation/storage/lifecycle_hooks.py
  - src/poker_deliberation/orchestrator.py
  - docs/run-revision-storage.md
- Acceptance criteria:
  - Immutable revision artifacts preserve every prior verified revision; a terminal marker is written last inside the revision and an atomic current pointer is published only after readers verify run ID, manifest, payload inventory, sizes, and hashes.
- Tests:
  - tests/unit/test_revision_storage_contracts.py
  - tests/property/test_revision_storage_properties.py
  - tests/integration/test_revision_storage.py
  - tests/adversarial/test_revision_storage_security.py
  - tests/fault/test_revision_storage_failures.py
  - tests/concurrency/test_revision_storage_concurrency.py
  - tests/unit/test_terminal_run_contracts.py
  - tests/property/test_terminal_run_properties.py
  - tests/integration/test_terminal_run_store.py
  - tests/adversarial/test_terminal_run_security.py
  - tests/fault/test_terminal_run_failures.py
  - tests/concurrency/test_terminal_run_concurrency.py
  - tests/integration/test_product_durable_run.py
  - tests/characterization/test_product_run_compatibility.py
  - tests/integration/test_legacy_run_migration.py
- Decision gate rationale:
  - supported old-run versions
  - retention and quarantine
  - Windows durability target
- Relations:
  - P2-012A is the immutable structural foundation; P2-012B is the normal product terminal protocol.

### RM-013 — Approval and resume contract hardening

- Status: `completed`
- Status reason: P2-013A authoritative approval decisions and P2-013B explicit reissue, pre-execution authority rechecks, and resume lifecycle binding are implemented without enabling external effects.
- Objective: Make approval decisions authoritative, conflict-safe, auditable, and idempotently resumable.
- Capabilities:
  - none
- Targets:
  - src/poker_deliberation/approval_models.py
  - src/poker_deliberation/approval_canonical.py
  - src/poker_deliberation/approvals.py
  - src/poker_deliberation/orchestrator.py
  - src/poker_deliberation/cli.py
  - docs/approval-authority-contract.md
- Acceptance criteria:
  - Existing idempotency outcomes are resolved before stale-revision checks; unknown, duplicate, stale, unauthorized, concurrent, and approve/reject conflicts otherwise fail as structured all-or-nothing decisions bound to an action digest.
- Tests:
  - tests/unit/test_approval_v2_contracts.py
  - tests/property/test_approval_v2_properties.py
  - tests/integration/test_approval_v2_transaction.py
  - tests/integration/test_approval_v2_cli.py
  - tests/adversarial/test_approval_v2_security.py
  - tests/fault/test_approval_v2_failures.py
  - tests/concurrency/test_approval_v2_concurrency.py
  - tests/characterization/test_approval_v1_compatibility.py
- Decision gate rationale:
  - actor trust model
  - approval expiry/revocation
  - authority scopes
- Relations:
  - P2-013A supplies authoritative local approval decisions; P2-013B remains the separate lifecycle and reissue milestone.

### RM-014 — Versioned normalization grammar

- Status: `planned`
- Status reason: Accepted roadmap scope; not implemented.
- Objective: Version the supported conservative grammar and preserve parser provenance in run artifacts.
- Capabilities:
  - documented_hand_parser
  - natural_language_or_site_parser
- Targets:
  - src/poker_deliberation/normalization.py
  - input schema documentation
- Acceptance criteria:
  - Supported and unsupported syntax, locale, warnings, and parser version are explicit and round-trip tested.
- Tests:
  - round-trip, malformed, Unicode, unknown-key, and resource-boundary tests
- Decision gate rationale:
  - supported sites and grammar profiles

### RM-015 — Hand rule profiles and side-pot accounting

- Status: `planned`
- Status reason: Accepted roadmap scope; not implemented.
- Objective: Define selected rule profiles for raises, all-ins, uncalled bets, side pots, and rake timing.
- Capabilities:
  - none
- Targets:
  - src/poker_deliberation/schemas.py
  - src/poker_deliberation/tools/hand_validator.py
- Acceptance criteria:
  - Supported rule profiles and unsupported cases match schema, documentation, and golden ledgers.
- Tests:
  - multiway side-pot, short-call, folded-contribution, and rule-profile golden tests
- Decision gate rationale:
  - site, jurisdiction, and game rule profiles

### RM-016 — Range grammar and provenance

- Status: `planned`
- Status reason: Accepted roadmap scope; not implemented.
- Objective: Add only approved range syntax with source and game-condition provenance.
- Capabilities:
  - none
- Targets:
  - src/poker_deliberation/tools/combinations.py
  - range schemas and documentation
- Acceptance criteria:
  - Ambiguity, overlap, blocker, weight, and provenance mismatches fail explicitly.
- Tests:
  - range parser, normalization, blocker, ambiguity, and provenance isolation tests
- Decision gate rationale:
  - range formats and source licenses

### RM-017 — Executable evaluation harness

- Status: `planned`
- Status reason: evals/metrics.json names metrics but has no executable runner or baseline.
- Objective: Turn evaluation metric names into versioned datasets, scorers, thresholds or review protocols, and reproducible result artifacts.
- Capabilities:
  - none
- Targets:
  - evals
  - future evaluation runner
- Acceptance criteria:
  - Every metric records dataset/license/hash, scorer/version, direction, aggregation, denominator policy, threshold or human rubric, commit/config hashes, and per-case outcomes.
- Tests:
  - deterministic scorer
  - threshold boundaries
  - corrupt/missing metric
  - baseline drift
  - unsupported-equilibrium and false-precision fixtures
- Decision gate rationale:
  - dataset rights
  - metric thresholds
  - human review rubric

### RM-018A — Pre-release readiness

- Status: `planned`
- Status reason: The phase order is corrected, but build, matrix, license, and artifact evidence do not yet exist.
- Objective: Establish reproducible build and distribution evidence after Phase 1 and before any pre-release tag.
- Capabilities:
  - none
- Targets:
  - CI
  - build metadata
  - wheel/sdist smoke
  - license inventory
  - release evidence manifest
- Acceptance criteria:
  - Approved OS/Python matrix, clean build/install, wheel/sdist contents, CLI/package-data smoke, license inventory, artifact SHA-256, and offline preflight are tied to one candidate commit.
- Tests:
  - CI matrix
  - isolated install
  - package data
  - dependency/license check
  - artifact hash verification
- Decision gate rationale:
  - supported matrix
  - release channel
  - distribution target
  - tag operation

### RM-018B — Stable release gate

- Status: `planned`
- Status reason: This gate is intentionally after Phase 2 and is not implemented or evaluated.
- Objective: Gate any stable tag on completed Phase 2 compatibility, migration, and source-to-artifact evidence.
- Capabilities:
  - none
- Targets:
  - version/tag/changelog policy
  - migration and deprecation policy
  - stable release manifest
- Acceptance criteria:
  - SemVer, version/tag/changelog parity, migration, deprecation, supported stable matrix, and artifact/source commit mapping are verified for one candidate.
- Tests:
  - version parity
  - migration fixtures
  - deprecation compatibility
  - artifact/source mapping
  - stable release dry-run
- Decision gate rationale:
  - SemVer
  - support window
  - migration guarantees
  - stable tag operation

### RM-019 — Decision-gated OpenAI provider

- Status: `planned`
- Status reason: The outbound provider remains disabled; implementation is decision-gated.
- Objective: Connect an external provider only after data, model, trace, cost, timeout, and approval boundaries are authorized.
- Capabilities:
  - openai_agents_outbound
- Targets:
  - src/poker_deliberation/providers/openai_agents.py
  - provider schemas
  - security and approval boundaries
- Acceptance criteria:
  - Only approved data is sent and model/version/reasoning, trace policy, cost, timeout, structured output, and failure are recorded.
- Tests:
  - no-network mocks
  - redaction canaries
  - timeout/cancel
  - malformed output
  - approval and SDK compatibility
- Decision gate rationale:
  - external transmission
  - provider/model
  - trace retention
  - credentials
  - cost

### RM-020 — External solver qualification adapter

- Status: `planned`
- Status reason: Only an honest unavailable adapter exists; RM-028 is a hard dependency for external execution.
- Objective: Qualify external solver results without promoting partial, mismatched, or non-converged output to GTO or equilibrium.
- Capabilities:
  - external_solver
  - full_nlhe_equilibrium
  - process_sandbox
- Targets:
  - src/poker_deliberation/tools/solver_adapter.py
  - qualification fixtures
  - isolated job control
- Acceptance criteria:
  - Typed status, full game/tree/rake/stack/range/version identity, executable/license and I/O hashes, resource approval, convergence definitions and trajectory, exploitability evidence, and tiny-game comparison are required.
- Tests:
  - unavailable/disabled/failed/partial/cancelled/timed_out
  - version and condition mismatch
  - non-convergence
  - RM-028 hard-stop
  - tiny-game oracle
- Decision gate rationale:
  - solver product/license
  - resource budget
  - redistribution rights
  - convergence threshold

### RM-021 — Multiway and PLO equity feasibility

- Status: `planned`
- Status reason: Research feasibility only; not implemented.
- Objective: Measure algorithm, error, oracle, license, and resource feasibility before any production commitment.
- Capabilities:
  - multiway_or_plo_equity
- Targets:
  - research ADR
  - evaluator interface
  - benchmark fixtures
- Acceptance criteria:
  - Small exact cases, complexity, hard caps, oracle provenance, and expected error support a separate adoption decision.
- Tests:
  - small exact cases
  - symmetry
  - card legality
  - resource caps
- Decision gate rationale:
  - game priority
  - compute budget
  - oracle license

### RM-022 — Small imperfect-information research

- Status: `planned`
- Status reason: Research-only roadmap item; not implemented.
- Objective: Reproduce known toy-game results without claiming general NLHE equilibrium capability.
- Capabilities:
  - full_nlhe_equilibrium
- Targets:
  - research-only modules
  - toy-game fixtures
  - research ADR
- Acceptance criteria:
  - Known equilibrium, regret trend, and best-response/exploitability cross-check are reproducible and labeled experimental.
- Tests:
  - known toy equilibrium
  - regret trend
  - best-response cross-check
- Decision gate rationale:
  - research purpose
  - success criteria
  - compute budget

### RM-023 — Roadmap and status single source of truth

- Status: `completed`
- Status reason: Implemented by the tracked readiness change; its enclosing commit is external evidence and cannot be self-referential in this file.
- Objective: Provide a package-resource-configured, tracked, machine-readable canonical RM state independent of ignored user materials; distribution inclusion remains an RM-018A check.
- Capabilities:
  - none
- Targets:
  - src/poker_deliberation/roadmap_status.json
  - src/poker_deliberation/roadmap.py
  - docs/roadmap-status.md
  - doctor
- Acceptance criteria:
  - Schema, vocabulary, transitions, unique IDs, DAG, evidence, capability references, doctor summary, and generated documentation are contract-tested from any CWD.
- Tests:
  - tests/integration/test_roadmap_status.py
- Decision gate rationale:
  - tracked packaged JSON as canonical RM status

### RM-024 — Context lifecycle contract

- Status: `completed`
- Status reason: P2-024A is implemented on the Python-local provider path with versioned policy, immutable attempt envelopes, exact allowlists, expiry, integrity, and lineage; persistence, cleanup, external runtimes, and later milestones remain out of scope.
- Objective: Implement the P2-024A attempt-scoped context policy, immutable envelope, lineage, expiry, integrity, and provider allowlist contract without persistence, cleanup, external runtime, or later Phase 2 work.
- Capabilities:
  - none
- Targets:
  - src/poker_deliberation/context_lifecycle.py
  - src/poker_deliberation/orchestrator.py
  - src/poker_deliberation/schemas.py
  - src/poker_deliberation/agents/roles.py
  - src/poker_deliberation/providers
  - docs/context-lifecycle.md
  - docs/architecture.md
  - docs/security.md
  - docs/limitations.md
  - docs/agent-protocol.md
- Acceptance criteria:
  - A strict versioned immutable ContextPolicy and ContextEnvelope bind classification, retention-policy ID, UTC expiry, exact allowlist, canonical payload/source/policy hashes, and run/assignment/attempt/runtime lineage.
  - The actual orchestrator-to-provider path validates integrity, expiry, lineage, runtime, assignment allowlist parity, restricted secrets, and provider availability before delivering a fresh AgentContext copy.
  - Unknown fields, versions, runtimes, dotted-path allowlists, tampering, replay, expired context, and restricted provider handoff fail closed without enabling external providers or writing context lifecycle state.
  - Existing AgentContext and provider signatures, LocalProvider behavior, CLI exit codes, run/resume/show, and existing artifact meanings remain compatible.
  - Unit, property/metamorphic, integration, adversarial, CLI, roadmap, doctor, and canonical quality gates pass with no new dependency.
- Tests:
  - tests/unit/test_context_lifecycle.py
  - tests/property/test_context_lifecycle_properties.py
  - tests/integration/test_context_lifecycle_integration.py
  - tests/adversarial/test_context_lifecycle_security.py
  - tests/adversarial/test_review_regressions.py
  - tests/adversarial/test_blind_isolation.py
  - tests/integration/test_cli.py
- Decision gate rationale:
  - Use context classifications public, internal by default, sensitive, and restricted.
  - Treat secrets and credentials as restricted; reject provider handoff fail closed and never use redaction as authorization.
  - Create a fresh immutable ContextEnvelope for every attempt.
  - Do not add persistence, automatic deletion, or a cleanup executor in P2-024A.
  - Require explicit policy expiry, timezone-aware UTC, an injected clock, and reject use when now is at or after expires_at.
  - Defer concrete retention durations, quarantine, deletion, tombstones, and secure erase to RM-027A/B.
  - Bind schema version, context, run, assignment, attempt, parent context, source hash, producer runtime, and consumer runtime lineage.
  - Support Python local runtime only and reject Codex bridge, unknown runtime, and unknown schema versions fail closed.
  - Preserve AgentContext, provider, CLI, run/resume/show, and artifact compatibility; keep P2-024A as additive internal API.
  - Do not implement external providers, solvers, dependencies, RM-010 through RM-013, RM-027 cleanup, or parallel execution.
- Relations:
  - Prerequisite for RM-010 analysis boundaries and RM-025 runtime conformance.

### RM-025 — Codex and Python agent runtime conformance

- Status: `proposed`
- Status reason: Approved as a roadmap candidate only; both runtimes remain separate.
- Objective: Define versioned conformance fixtures for assignment, context, tool allowlist, approval, result, and execution-record semantics across separate runtimes.
- Capabilities:
  - codex_python_runtime_bridge
- Targets:
  - Codex agent definitions
  - Python role/provider catalog
  - future interchange schema
- Acceptance criteria:
  - Version negotiation and fixture conformance cover assignment, context, result, error, approval, and audit semantics; capability remains unavailable until an actual bridge passes the contract.
- Tests:
  - cross-runtime fixtures
  - version mismatch
  - allowlist and approval parity
  - missing execution record
- Decision gate rationale:
  - whether to implement a bridge or retain conformance-only separation
- Relations:
  - Extends the execution-surface boundary recorded by RM-001 without claiming current interoperability.

### RM-026 — Framework extension SPI

- Status: `proposed`
- Status reason: Approved as a roadmap candidate only; no extension SPI exists.
- Objective: Define explicit, versioned extension registration without treating arbitrary imports as a security boundary.
- Capabilities:
  - none
- Targets:
  - future extension registry
  - provider/tool/parser adapter interfaces
- Acceptance criteria:
  - Extension ID, type, API version, capability and data permissions, lifecycle methods, duplicate/incompatible rejection, and health/cancel/close semantics are explicit; auto-import is not required.
- Tests:
  - duplicate/incompatible registration
  - version negotiation
  - permission denial
  - lifecycle and failure isolation
- Decision gate rationale:
  - third-party in-process code policy
  - distribution and trust model
- Relations:
  - May support RM-019/RM-020 adapters but never replaces their approval or isolation requirements.

### RM-027 — Local data lifecycle

- Status: `completed`
- Status reason: P2-027A policy/schema and the P2-027B additive authorized cleanup API are implemented; secure erase, a cleanup CLI, automatic retry, P2-013B, and P2-028A remain outside this item.
- Objective: Provide versioned local-data classification, retention, expiry, quarantine, disposition, and audit contracts, plus explicitly authorized quarantine and staged deletion for one verified run.
- Capabilities:
  - local_data_lifecycle_policy
  - local_data_cleanup_executor
- Targets:
  - src/poker_deliberation/local_data_policy.py
  - src/poker_deliberation/local_data_cleanup_models.py
  - src/poker_deliberation/local_data_cleanup_canonical.py
  - src/poker_deliberation/local_data_cleanup.py
  - src/poker_deliberation/storage/local_data_cleanup_store.py
  - docs/local-data-policy.md
  - docs/local-data-cleanup.md
- Acceptance criteria:
  - P2-027A provides strict versioned policy and pure lifecycle evaluation with exact retention/expiry semantics, protected states, typed failures, and no filesystem mutation.
  - P2-027B accepts one verified terminal run and requires verified ownership, active/pending protection, exact runtime approval binding, a dry-run plan digest, revision CAS, receipts/tombstones, idempotency, and read-only reconciliation.
  - Cleanup remains an additive Python API with no cleanup CLI, automatic repair/retry, broad discovery, direct product-namespace deletion, or secure-erase claim.
- Tests:
  - tests/unit/test_local_data_policy.py
  - tests/property/test_local_data_policy_properties.py
  - tests/integration/test_local_data_policy_contract.py
  - tests/adversarial/test_local_data_policy_security.py
  - tests/fault/test_local_data_policy_failures.py
  - tests/unit/test_local_data_cleanup_contracts.py
  - tests/property/test_local_data_cleanup_properties.py
  - tests/integration/test_local_data_cleanup_executor.py
  - tests/adversarial/test_local_data_cleanup_security.py
  - tests/fault/test_local_data_cleanup_failures.py
  - tests/concurrency/test_local_data_cleanup_concurrency.py
  - tests/characterization/test_local_data_cleanup_compatibility.py
- Decision gate rationale:
  - retention periods
  - quarantine
  - encryption
  - audit tombstones and deletion priority
- Relations:
  - P2-027A provides policy/schema; P2-027B relies on the P2-012B verified run protocol and P2-013A runtime authority.

### RM-028 — Isolated solver and provider job control

- Status: `proposed`
- Status reason: Approved as a roadmap candidate only; cooperative in-process cancellation is not a hard stop.
- Objective: Provide durable, approval-bound process isolation and hard-stop evidence for non-cooperative external jobs.
- Capabilities:
  - process_sandbox
  - external_solver
  - openai_agents_outbound
- Targets:
  - future isolated job runner
  - solver/provider execution adapters
  - job artifacts
- Acceptance criteria:
  - Canonical argv plus transitive interpreter/library/image identity, approved secret references, minimal OS identity, closed stdin/handles, filesystem read/write allowlists with link-escape defense, default-deny network egress with destination allowlists, wall-clock kill, process-tree cleanup, CPU/memory/output caps, remote-cancel state, durable launch/effect_unknown/reconciliation states, and approval action digest are enforced.
- Tests:
  - hung child and process tree
  - output flood
  - cancel race
  - resource caps
  - filesystem and network escape
  - identity/handle/secret isolation
  - partial artifact
  - effect-unknown restart recovery
  - approval mismatch
- Decision gate rationale:
  - supported isolation platform
  - resource limits
  - external executables and licenses
- Relations:
  - Hard dependency for any RM-019/RM-020 execution that claims timeout or cancellation guarantees.

## Synchronization contract

- `poker-deliberate doctor --format json`の`roadmap`はpackage resourceの公開JSONから計算します。
- 公開projection自体はcandidate固有のcommitやtest実行を証明しません。status更新は同一schema更新検証、参照path/testのtracked検証、repository gateを別途要求します。
- `scripts/generate_roadmap_status.py --check`とcontract testがこのprojectionのdriftを検出します。
- wheel/sdistのpackage-dataはartifact smokeで候補ごとに別途検証します。この検証だけではrelease candidate判定とせず、RM-018Aのmatrix・license・artifact条件を別途要求します。
