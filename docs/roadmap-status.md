# Public roadmap status

この文書は`src/poker_deliberation/roadmap_status.json`から生成する公開projectionです。
公開中の実装状態、依存関係、能力scope、受入条件、milestone、decision rationaleを示します。

- schema version: `12.0.0`
- source SHA-256: `41246b9f1d34fae0600a3265e45fef60d31673287eb18257f0be46c5765e49dc`
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

## Implementation milestones

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
| `P2-028A` | `RM-028` | `in_progress` | `P2-011B`, `P2-012B`, `P2-013B`, `P2-027B` | Approved isolation boundary, durable external-effect state, cancellation, and reconciliation. | The approved Windows-first repository-owned synthetic helper slice is implemented and under qualification; arbitrary external code, provider/solver adapters, remote cancellation, and OS-enforced network isolation remain outside the implemented boundary. |
| `P2-029A` | `RM-029` | `completed` | `P2-012B`, `P2-013B`, `P2-024A`, `P2-027B` | Offline input safety, redaction integrity, verified ICM tolerance, concise adjudicated reporting, and ordinary product-path dogfood. | The offline Python product path vertical slice is implemented, contract-tested, and dogfooded through verified terminal storage without external provider or solver execution. |
| `P2-025A` | `RM-025` | `completed` | `P2-012B`, `P2-013B`, `P2-024A`, `P2-029A` | Versioned cross-runtime role, assignment, context, tool allowlist, approval, result, error, execution-audit, canonical fixture, and offline projection conformance without an execution bridge. | The strict conformance-only contract, versioned fixtures, and verified offline Python product projection are implemented without a runtime bridge. |
| `P3-014A` | `RM-014` | `completed` | `RM-006`, `RM-012` | Repository-owned generic key-value grammar version 1, strict byte and Unicode behavior, bounded stable diagnostics, exact source and normalized-hand provenance, typed product persistence and reader verification, compatibility projection, canonical fixtures, and CLI-to-hand_validator integration; supported site none, with no natural-language or site-specific parser. | The approved strict versioned normalization vertical slice, typed provenance, compatibility boundaries, product reader verification, fixtures, and declared gates are implemented. |
| `P3-015A` | `RM-015` | `completed` | `P3-014A` | Repository-owned generic_nlhe_cash_no_rake_v1, strict profile selection, exact caller-unit integer ledger, uncalled returns, contribution layers, eligibility, full-raise reopening, conservation, and an independent oracle through the existing ToolResult product path; supported site none. | The approved generic no-rake NLHE cash profile, exact side-pot ledger, independent oracle, product integration, documentation, and declared tests are implemented. |
| `P3-016A` | `RM-016` | `completed` | `RM-006`, `P3-014A` | poker-deliberation.nlhe-range grammar version 1.0.0 for one provenance-qualified opponent range using explicit combos, pairs, canonical descending suited or offsuit classes, optional decimal @ weights represented as integer millionths, pre-blocker overlap rejection, exact hand and action-prefix binding, automatic range_validate then combos execution, and immutable/terminal semantic replay; no plus, intervals, exclusions, natural-language inference, external source import, equity integration, solver, or GTO claim. | The approved bounded grammar, provenance and game-condition binding, exact validation tool, canonical combo product slice, reader replay, documentation, fixtures, and declared tests are implemented. |
| `P3-016B` | `RM-016` | `completed` | `P3-016A` | One explicitly admitted retrospective NLHE cash river decision with canonical Hero cards, a five-card board, one provenance-qualified VersionedRangeDefinitionV1 target, only Hero and target eligible, range_validate then combos then holdem_equity ordering, exact-only enumeration capped at 990, exact integer-millionth win/tie/loss aggregation and reduced rational equity, floating-verified legacy projection, domain-separated binding/result hashes, immutable/terminal replay, and deterministic exact-evidence evaluation; no natural-language intake, all-in, call EV or recommendation, multiple ranges, multiway, earlier streets, Monte Carlo, external solver, GTO, or equilibrium claim. | The approved opt-in river range-equity bridge, exact integer/rational oracle, derived exact-only tool chain, per-run-authority-serialized product namespace reservation and buffer-external pre-execution admission commitment, provenance hashes, durable semantic replay, exact-evidence evaluation, documentation, and declared tests are implemented. |
| `P3-017A` | `RM-017` | `completed` | `RM-006`, `RM-007`, `RM-012`, `P2-025A` | Strict versioned offline dataset, scorer, provenance, runtime-inventory, per-case outcome, structured-failure, and summary contracts with a repository-owned synthetic MIT fixture and deterministic exact-evidence scoring; no provider, solver, bridge, or external dataset execution. | The canonical synthetic fixture, deterministic runner and scorer, provenance-bound result, documentation, and declared tests are implemented. |
| `P3-030A` | `RM-030` | `completed` | `P3-014A`, `P3-016A`, `P3-017A` | Versioned source, candidate, confirmation, authority, provenance, CLI, durable replay, exact-evidence evaluation, and LocalProvider-only adjudication for one complete retrospective NLHE hand and at most one versioned opponent range; no general natural-language or site parser, external provider or model, runtime bridge, external solver, range equity, multiopponent range, GTO, or equilibrium claim. | The approved caller-supplied candidate, explicit hash-bound confirmation, local-only adjudication, provenance replay, deterministic evaluation, documentation, and declared tests are implemented. |
| `P3-030B` | `RM-030` | `completed` | `P3-030A`, `P3-015A` | Version 1 deterministic parsing for one documented finite Japanese retrospective 2-6 player NLHE cash grammar with integer chip units, zero ante and rake, at most 64 actions, exact UTF-8 half-open source spans, explicit focal call-or-fold decision, hand_validator then hand_pot_ledger then pot_odds execution, six independently confirmed hash domains, LocalProvider-only adjudication, durable provenance replay, and exact-evidence evaluation; no general natural-language or site parser, OCR, tournament, focal all-in or side-pot decision, range inference, external provider or model, runtime bridge, external solver, GTO, or equilibrium claim. | The approved bounded Japanese grammar, exact source-span evidence, six-hash confirmation contract, fixed local product path, durable provenance replay, evaluation fixture, documentation, and declared tests are implemented and passed the canonical local acceptance gates. |
| `P3-030C` | `RM-030` | `completed` | `P2-024A`, `P3-015A`, `P3-016B`, `P3-017A`, `P3-030B` | One confirmed P3-030B river focal decision and one separate provenance-qualified VersionedRangeDefinitionV1 for the facing actor, exact P3-015A no-rake ledger, pot_odds, P3-016B exact-only heads-up river equity, Fraction required-equity and call-EV oracles, raked_call_ev with rake_percent zero, model-limited call/fold comparison, exact LocalProvider role/context/tool controls, typed terminal replay, and deterministic exact-evidence evaluation; no general natural language, actual Codex/Python runtime bridge, multiple ranges, multiway or earlier-street equity, Monte Carlo, all-in, side pot, rake, ante, external solver, GTO, equilibrium, or unconditional recommendation. | The approved bounded river call-or-fold integration, twelve-hash confirmation, exact seven-tool local path, Fraction and ULP oracles, context and provenance controls, typed terminal replay, deterministic evaluation, documentation, and declared tests are implemented and passed the canonical local acceptance gates. |

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
| `RM-014` | Versioned normalization grammar | `phase-3` | `P1` | `completed` | `RM-006`, `RM-012` | `P3-014A` | `required` |
| `RM-015` | Hand rule profiles and side-pot accounting | `phase-3` | `P2` | `in_progress` | `RM-014` | `n/a` | `required` |
| `RM-016` | Range grammar and provenance | `phase-3` | `P2` | `in_progress` | `RM-006`, `RM-014` | `n/a` | `required` |
| `RM-017` | Executable evaluation harness | `phase-3` | `P1` | `in_progress` | `RM-006`, `RM-007`, `RM-012` | `n/a` | `required` |
| `RM-018A` | Pre-release readiness | `pre-release` | `P1` | `planned` | `RM-001`, `RM-002`, `RM-003`, `RM-004`, `RM-005`, `RM-006`, `RM-007`, `RM-008`, `RM-009`, `RM-023` | `n/a` | `required` |
| `RM-018B` | Stable release gate | `stable-release` | `P1` | `planned` | `RM-018A`, `RM-010`, `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-027` | `n/a` | `required` |
| `RM-019` | Decision-gated OpenAI provider | `phase-4` | `P2` | `planned` | `RM-010`, `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-028` | `n/a` | `required` |
| `RM-020` | External solver qualification adapter | `phase-4` | `P2` | `planned` | `RM-011`, `RM-012`, `RM-013`, `RM-017`, `RM-028` | `n/a` | `required` |
| `RM-021` | Multiway and PLO equity feasibility | `phase-5` | `P3` | `planned` | `RM-006`, `RM-007` | `n/a` | `required` |
| `RM-022` | Small imperfect-information research | `phase-5` | `P3` | `planned` | `RM-007`, `RM-017` | `n/a` | `required` |
| `RM-023` | Roadmap and status single source of truth | `readiness` | `P0` | `completed` | `RM-001` | `n/a` | `required` |
| `RM-024` | Context lifecycle contract | `phase-2` | `P1` | `completed` | `RM-006`, `RM-023` | `P2-024A` | `required` |
| `RM-025` | Codex and Python agent runtime conformance | `post-phase-2` | `P1` | `in_progress` | `RM-012`, `RM-013`, `RM-023`, `RM-024` | `P2-025A` | `required` |
| `RM-026` | Framework extension SPI | `phase-3` | `P2` | `proposed` | `RM-006`, `RM-012`, `RM-023` | `n/a` | `required` |
| `RM-027` | Local data lifecycle | `phase-2` | `P1` | `completed` | `RM-023`, `RM-024` | `P2-027B` | `required` |
| `RM-028` | Isolated solver and provider job control | `phase-2` | `P1` | `in_progress` | `RM-011`, `RM-012`, `RM-013`, `RM-024`, `RM-027` | `P2-028A` | `required` |
| `RM-029` | Offline Python product path safety and usability completion | `phase-2` | `P1` | `completed` | `RM-012`, `RM-013`, `RM-024`, `RM-027` | `P2-029A` | `none` |
| `RM-030` | Natural-language canonical intake and adjudicated report integration | `phase-4` | `P2` | `in_progress` | `RM-014` | `n/a` | `required` |

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
- Status reason: All 22 tools expose contract version 2.0.0 with typed schemas.
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

- Status: `completed`
- Status reason: P3-014A is complete with the approved generic grammar version 1, supported site none, typed provenance, verified persistence, compatibility coverage, and passing completion gates.
- Objective: Version the supported conservative grammar and preserve parser provenance in run artifacts.
- Capabilities:
  - documented_hand_parser
  - natural_language_or_site_parser
- Targets:
  - src/poker_deliberation/normalization.py
  - src/poker_deliberation/cli.py
  - src/poker_deliberation/orchestrator.py
  - src/poker_deliberation/phases/models.py
  - src/poker_deliberation/phases/services.py
  - src/poker_deliberation/storage/terminal_canonical.py
  - docs/normalization-contract.md
  - scripts/generate_normalization_fixtures.py
- Acceptance criteria:
  - Strict frozen version-1 request, result, diagnostic, and provenance contracts define supported syntax, UTF-8, BOM, newline, NFC, invariant numeric, duplicate, unknown-key, malformed, and resource-boundary behavior.
  - Exact source-byte and canonical normalized-hand SHA-256 values persist in a typed artifact and are recomputed by the verified product reader without trusting untyped metadata.
  - The legacy normalize_hand_text shape, structured JSON and legacy runs, role context allowlists, approval/runtime boundaries, and unavailable natural-language/site parser claims remain compatible and truthful.
  - The documented free-text CLI path persists and reads the typed record and executes hand_validator without external provider, solver, site adapter, or runtime bridge execution.
  - Canonical fixtures and unit, property, integration, adversarial, characterization, roadmap, doctor, evaluation, and quality gates pass.
- Tests:
  - tests/unit/test_normalization_contracts.py
  - tests/property/test_normalization_properties.py
  - tests/integration/test_normalization_product_path.py
  - tests/integration/test_normalization_fixtures.py
  - tests/adversarial/test_normalization_security.py
  - tests/integration/test_capability_contract.py
  - tests/integration/test_roadmap_status.py
- Decision gate rationale:
  - approved repository-owned generic key-value grammar version 1
  - supported site none and continued natural-language/site-parser unavailability
- Relations:
  - P3-014A supports no poker site and does not start RM-015 hand-rule profiles or RM-016 range grammar work.

### RM-015 — Hand rule profiles and side-pot accounting

- Status: `in_progress`
- Status reason: P3-015A completed the approved generic no-rake NLHE cash vertical slice; raked and site-specific profiles remain future RM-015 work.
- Objective: Implement the approved repository-owned generic no-rake NLHE cash profile for raises, all-ins, uncalled returns, eligibility, and exact side-pot accounting while retaining fail-closed boundaries for all other profiles.
- Capabilities:
  - profiled_nlhe_side_pot_ledger
- Targets:
  - src/poker_deliberation/tools/hand_pot_ledger.py
  - src/poker_deliberation/tools/contracts.py
  - src/poker_deliberation/tools/registry.py
  - docs/hand-rule-profile.md
- Acceptance criteria:
  - Only generic_nlhe_cash_no_rake_v1 version 1.0.0 with supported site none and explicit zero rake is accepted.
  - Caller-declared canonical chip units produce bounded integer ledgers, uncalled returns, contribution layers, eligibility, full-raise reopening, conservation, and independently verified oracle evidence.
  - CanonicalHand and normalization grammar version 1 remain unchanged while rake, straddle, run-it-twice, PLO, tournament, bounty, site-specific, split-pot, and unknown-version inputs fail closed.
- Tests:
  - tests/unit/test_hand_pot_ledger.py
  - tests/property/test_hand_pot_ledger_properties.py
  - tests/integration/test_hand_pot_ledger_product_path.py
  - tests/adversarial/test_hand_pot_ledger_security.py
  - tests/characterization/test_hand_pot_ledger_compatibility.py
- Decision gate rationale:
  - future site, jurisdiction, rake, and game rule profiles require a separate approved decision
- Relations:
  - P3-015A is limited to the repository-owned generic no-rake NLHE cash profile with supported site none.
  - Raked and site-specific rule profiles remain future RM-015 work.
  - P3-015A does not implement RM-016, RM-019, RM-020, RM-025 bridge work, or P2-028A external execution.

### RM-016 — Range grammar and provenance

- Status: `in_progress`
- Status reason: P3-016A implements the bounded version 1 grammar and provenance-bound combos product slice, and P3-016B implements the approved opt-in exact-only cash-river bridge for one range; additional syntax, multiple ranges, earlier-street or multiway equity, imports, and natural-language inference remain unimplemented.
- Objective: Provide a bounded versioned NLHE range grammar with explicit source rights, exact game-condition provenance, deterministic canonical combos, and reader-verifiable product artifacts.
- Capabilities:
  - versioned_nlhe_range_grammar
  - versioned_nlhe_river_equity_bridge
- Targets:
  - src/poker_deliberation/range_models.py
  - src/poker_deliberation/range_grammar.py
  - src/poker_deliberation/range_equity_models.py
  - src/poker_deliberation/range_equity.py
  - src/poker_deliberation/range_equity_evaluation.py
  - src/poker_deliberation/tools/contracts.py
  - src/poker_deliberation/orchestrator.py
  - scripts/generate_range_fixtures.py
  - scripts/run_range_equity_evaluation.py
  - tests/fixtures/range/v1/cases.json
  - tests/fixtures/range_equity/v1/scenarios.json
  - evals/datasets/p3_016a/v1/cases.json
  - docs/range-grammar.md
  - docs/range-equity-bridge.md
- Acceptance criteria:
  - Grammar and result version 1.0.0 accept only explicit combos, pairs, canonical descending suited or offsuit classes, comma separators with ASCII horizontal whitespace, and optional @ decimal weights of at most six places.
  - Weights use exact integer millionths; plus, intervals, exclusions, colon weights, signs, exponent notation, leading decimal points, non-ASCII notation, overlap, limits, empty post-blocker ranges, and ambiguous or unsupported syntax fail with stable diagnostics.
  - One non-hero target range is bound to declared source/license/usage/content status, notation SHA-256, NLHE format/table/position/street/stack, as-of action-prefix SHA-256, and hero plus visible-board blockers.
  - The P3-016A product path runs exact range_validate before canonical weighted combos, refuses conflicting manual inputs, and immutable and terminal readers deterministically replay both artifacts.
  - The separately admitted P3-016B path accepts only retrospective NLHE cash river input with canonical Hero and board cards, one VersionedRangeDefinitionV1 target, only Hero and target eligible, a target bet or raise rather than all-in, and the fixed range_validate then combos then holdem_equity exact-only order capped at 990 evaluations.
  - P3-016B preserves integer-millionth weights in exact win, tie, and loss totals and a reduced rational equity oracle, labels the unchanged legacy binary64 hero_equity projection floating-verified, domain-hashes source/candidate/tool/oracle/binding/result artifacts, and replays the complete chain in immutable and terminal readers.
  - The repository-owned P3-016B fixture scores exact weight/oracle, admission boundaries, and replay/storage evidence separately; all three deterministic metrics must equal 1.0.
  - Legacy RangeDefinition, legacy parse_weighted_range, P3-016A behavior, and existing holdem_equity semantics remain unchanged; all-in, multiple versioned ranges, earlier-street or multiway bridges, Monte Carlo bridge execution, imports, natural-language inference, call EV, external solvers, and GTO claims remain outside P3-016B.
- Tests:
  - tests/unit/test_range_grammar.py
  - tests/property/test_range_grammar_properties.py
  - tests/integration/test_range_product_path.py
  - tests/integration/test_range_fixtures.py
  - tests/adversarial/test_range_grammar_adversarial.py
  - tests/characterization/test_range_grammar_compatibility.py
  - tests/integration/test_tool_contracts.py
  - tests/unit/test_range_equity.py
  - tests/property/test_range_equity_properties.py
  - tests/integration/test_range_equity_product_path.py
  - tests/integration/test_range_equity_evaluation.py
  - tests/adversarial/test_range_equity_security.py
  - tests/characterization/test_range_equity_compatibility.py
- Decision gate rationale:
  - future range syntax, multiple-range semantics, external source/license classes, import formats, all-in, earlier-street, Monte Carlo, multiway, call-EV, or natural-language equity integration beyond the approved P3-016B bridge requires a separate approved decision
- Relations:
  - P3-016A is additive to the legacy RangeDefinition and parser and does not change existing equity semantics.
  - P3-016B is additive to P3-016A and the legacy holdem_equity contract; it is reachable only through its dedicated deterministic admission and does not widen ordinary range or confirmed-review paths.
  - Natural-language hand or range intake and end-to-end report integration are deferred to RM-030.

### RM-017 — Executable evaluation harness

- Status: `in_progress`
- Status reason: P3-017A is implemented for deterministic offline exact-evidence scoring; the broader subjective metric set, external datasets, and human rubrics remain unimplemented.
- Objective: Turn evaluation metric names into versioned datasets, scorers, thresholds or review protocols, and reproducible result artifacts.
- Capabilities:
  - offline_evaluation_harness
- Targets:
  - evals
  - src/poker_deliberation/evaluation
  - scripts/run_offline_evaluation.py
- Acceptance criteria:
  - Every implemented metric records dataset/license/content hash, scorer/version, direction, aggregation, denominator policy, threshold or human rubric, source commit/tree/config/tool-contract/runtime-inventory bindings, per-case outcomes, structured failures, and a summary.
  - P3-017A implements only deterministic exact-evidence matching with micro-mean aggregation, all declared cases in the denominator, fail-closed invalid or missing counts, and an approved threshold of 1.0.
  - The synthetic fixture is repository-owned and MIT-licensed; subjective strategy metrics, external datasets, providers, solvers, runtime bridges, and equilibrium claims remain outside this milestone.
- Tests:
  - tests/unit/test_evaluation_contracts.py
  - tests/property/test_evaluation_properties.py
  - tests/integration/test_offline_evaluation.py
  - tests/adversarial/test_evaluation_security.py
  - tests/characterization/test_evaluation_compatibility.py
- Decision gate rationale:
  - rights for future non-synthetic datasets beyond the approved repository-owned MIT fixture
  - thresholds for future metrics beyond the approved exact-evidence threshold
  - human review rubrics for subjective strategy metrics

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

- Status: `in_progress`
- Status reason: P2-025A conformance-only contracts are complete; both runtimes remain separate and the actual bridge remains unavailable and separately decision-gated.
- Objective: Define versioned conformance fixtures for assignment, context, tool allowlist, approval, result, and execution-record semantics across separate runtimes.
- Capabilities:
  - codex_python_runtime_bridge
- Targets:
  - .codex/agents
  - src/poker_deliberation/agents/roles.py
  - src/poker_deliberation/runtime_conformance
  - docs/runtime-conformance-contract.md
- Acceptance criteria:
  - A strict versioned contract and minimal tracked fixtures cover runtime-specific roles, assignment and parent lineage, context provenance/classification/expiry/budget references, exact tool/capability allowlists, approval binding and expiry, epistemic result references, structured error/timeout/cancellation, and execution audit semantics.
  - Unknown versions, roles, capabilities, allowlist expansion, approval weakening, missing execution evidence, context mismatch, secret-bearing public values, and unsupported solver/provider claims fail closed.
  - Offline Python product runs project into the contract without launching Codex, external providers, external solvers, or representing Python execution as Codex execution.
  - The actual Codex/Python bridge remains unavailable and existing final-report, reader, resume, migration, storage, and canonical artifact meanings remain compatible.
- Tests:
  - tests/unit/test_runtime_conformance_contracts.py
  - tests/property/test_runtime_conformance_properties.py
  - tests/characterization/test_runtime_conformance_compatibility.py
  - tests/adversarial/test_runtime_conformance_security.py
  - tests/integration/test_runtime_conformance.py
- Decision gate rationale:
  - whether a future actual bridge candidate should be registered after the conformance-only contract
- Relations:
  - Extends the execution-surface boundary recorded by RM-001 without claiming current interoperability.
  - A future actual bridge would require a separately approved P2-025B candidate; P2-025B is not registered or implemented by P2-025A.

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

- Status: `in_progress`
- Status reason: P2-028A implements the approved Windows-first repository-owned synthetic helper slice with Job Object hard stops and durable effect state; the broader solver/provider, remote-cancel, and network-isolation scope remains incomplete.
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

### RM-029 — Offline Python product path safety and usability completion

- Status: `completed`
- Status reason: Retrospective input safety, lossless redaction, derived ICM tolerance, concise adjudicated reporting, and ordinary product-path dogfood are implemented and verified; external providers and solvers remain disabled or unavailable.
- Objective: Complete one auditable offline Python product path spanning retrospective input safety, lossless redaction, verified numeric tolerance, concise adjudicated reporting, and ordinary-run dogfood before any external provider or solver execution.
- Capabilities:
  - phase_1_hardening
  - product_integrated_durable_run
- Targets:
  - src/poker_deliberation/security.py
  - src/poker_deliberation/tools/icm.py
  - src/poker_deliberation/reporting/summary.py
  - docs/offline-product-path.md
- Acceptance criteria:
  - Japanese retrospective negation is accepted without weakening fail-closed live-assistance detection; redaction preserves every nested mapping entry under deterministic non-secret collision keys; unspecified analysis scope gives rule-specific retrospective guidance before provider or tool execution; the cached-subset-DP ICM implementation, verification metadata, and manifest share a conservative floating-point error bound; an opt-in concise Japanese projection uses only adjudicated FinalReport content and verified tool results while existing stored report artifacts, canonical bytes, readers, resume, and migration retain their meanings; ordinary product storage dogfood verifies pot-odds correction, structured hand validation, and an honest unsupported-strategy limitation.
- Tests:
  - tests/adversarial/test_review_security_extensions.py
  - tests/adversarial/test_review_regressions.py
  - tests/unit/test_icm.py
  - tests/property/test_phase1_math_oracles.py
  - tests/unit/test_report_summary.py
  - tests/integration/test_offline_product_path.py
  - tests/characterization/test_product_run_compatibility.py
- Relations:
  - Uses the completed P2-012B terminal reader, P2-013B authority lifecycle, P2-024A context boundary, and P2-027B local-data boundary without starting RM-019, RM-020, or P2-028A.
  - Raises RM-025 priority to P1 because cross-runtime semantic drift must be decided before external effects; P2-025A is now the approved conformance-only milestone while the actual bridge remains unavailable and separately decision-gated.

### RM-030 — Natural-language canonical intake and adjudicated report integration

- Status: `in_progress`
- Status reason: P3-030A implements the caller-supplied candidate path, P3-030B implements the bounded Japanese grammar, and P3-030C is an implementation candidate for one confirmed river range/equity/no-rake call-EV product slice; general natural-language or site parsing and actual runtime or solver integrations remain unimplemented. The milestone table remains authoritative for current status.
- Objective: Provide separately approved, bounded flows from confirmed review material through canonical hand/range artifacts, deterministic calculators, adjudication, and provenance-bound reports without implying a general natural-language parser.
- Capabilities:
  - confirmed_natural_language_review_intake
  - bounded_japanese_nlhe_cash_parser
  - bounded_japanese_river_call_ev_review
  - natural_language_or_site_parser
  - versioned_nlhe_range_grammar
- Targets:
  - src/poker_deliberation/confirmed_review_models.py
  - src/poker_deliberation/confirmed_review.py
  - src/poker_deliberation/confirmed_review_evaluation.py
  - src/poker_deliberation/bounded_natural_language_models.py
  - src/poker_deliberation/bounded_natural_language.py
  - src/poker_deliberation/bounded_natural_language_provenance.py
  - src/poker_deliberation/bounded_natural_language_evaluation.py
  - src/poker_deliberation/bounded_river_call_ev_models.py
  - src/poker_deliberation/bounded_river_call_ev.py
  - src/poker_deliberation/bounded_river_call_ev_provenance.py
  - src/poker_deliberation/bounded_river_call_ev_evaluation.py
  - src/poker_deliberation/orchestrator.py
  - src/poker_deliberation/cli.py
  - src/poker_deliberation/storage/terminal_canonical.py
  - tests/fixtures/confirmed_review/v1/scenarios.json
  - tests/fixtures/bounded_natural_language/v1/scenarios.json
  - tests/fixtures/bounded_river_call_ev/v1/scenarios.json
  - scripts/run_confirmed_review_evaluation.py
  - scripts/run_bounded_natural_language_evaluation.py
  - scripts/run_bounded_river_call_ev_evaluation.py
  - docs/confirmed-review-intake.md
  - docs/bounded-natural-language-intake.md
  - docs/bounded-river-call-ev.md
- Acceptance criteria:
  - P3-030A admits only one bounded UTF-8 retrospective source and a caller-supplied complete NLHE candidate after explicit source and candidate hash confirmation by a scoped authority for at most 24 hours.
  - P3-030A uses the exact LocalProvider and default tool registry with hand_validator, the approved optional no-rake cash ledger, and automatic range_validate then combos for at most one versioned range.
  - Source, candidate, confirmation, case input, agent and tool evidence, final report, and typed provenance are persisted and replayed fail closed within the existing terminal product path.
  - The repository-owned 17-case exact-evidence evaluation scores 1.0 only when every expected evidence tuple matches exactly.
  - Caller claims remain USER_CLAIM, provider narrative remains UNKNOWN, calculator outputs follow their exactness contracts, and no general parser, external provider, runtime bridge, solver, range equity, multiopponent range, GTO, or equilibrium capability is claimed.
  - P3-030B accepts only its documented finite Japanese retrospective 2-6 player NLHE cash grammar with zero ante and rake, integer units, at most 64 actions, and one adjacent facing-bet call-or-fold focal decision; ambiguity, missing data, contradictions, extra lines, all-in or side-pot focal decisions, tournament, site input, OCR, ranges, and general language fail closed.
  - Every P3-030B extracted semantic field is bound to exact UTF-8 half-open source byte spans, and source, candidate, source bindings, focal decision, tool plan, and extractor hash domains must all be explicitly confirmed before any run namespace is created.
  - P3-030B runs exactly hand_validator, hand_pot_ledger, then pot_odds through the existing LocalProvider, role/context lifecycle, durable storage, terminal report, and provenance replay path without passing raw source text into agent context.
  - The repository-owned P3-030B scenario fixture gives separate exact scores for field extraction, source spans, diagnostics, end-to-end tool evidence, and storage replay, with acceptance only when all five metrics equal 1.0.
  - P3-030C accepts only a final river call-or-fold focal response from P3-030B, a five-card board, two Hero cards, zero ante and rake, no all-in or side pot, no later action, exactly one separate VersionedRangeDefinitionV1 for the facing actor, and exactly Hero plus that actor eligible at the focal point.
  - P3-030C binds source, bounded candidate, source bindings, focal decision, extractor, tool plan, range definition, range target, P3-016B range binding, equity model, call-EV model, and the complete candidate to independent confirmation hashes with scoped authority, run identity, expiry, and idempotency.
  - P3-030C commits its admission before executing each tool once in the exact hand_validator, hand_pot_ledger, pot_odds, range_validate, combos, holdem_equity, raked_call_ev order; manual conflicts, reordered or tampered prefixes, non-exact equity metadata, and binary64 projections outside the declared ULP bounds fail closed.
  - P3-030C stores exact reduced rational equity, required equity call_cost divided by pot_after_bet plus call_cost, no-rake call EV equity times contestable pot minus call_cost, zero focal fold EV, call-minus-fold EV, and only a model-limited CALCULATED comparison; range accuracy stays USER_CLAIM or ASSUMPTION and UNKNOWN, and strategic interpretation stays INFERENCE.
  - P3-030C uses exact LocalProvider assignments and P2-024A context envelopes without raw source text, persists seven typed integration artifacts plus the reused P3-016B binding, and requires all three deterministic evaluation metrics to equal 1.0.
- Tests:
  - tests/unit/test_confirmed_review_contract.py
  - tests/integration/test_confirmed_review_cli.py
  - tests/integration/test_confirmed_review_product_path.py
  - tests/integration/test_confirmed_review_evaluation.py
  - tests/adversarial/test_confirmed_review_storage_security.py
  - tests/unit/test_bounded_natural_language_contract.py
  - tests/integration/test_bounded_natural_language_cli.py
  - tests/integration/test_bounded_natural_language_product_path.py
  - tests/integration/test_bounded_natural_language_evaluation.py
  - tests/adversarial/test_bounded_natural_language_security.py
  - tests/property/test_bounded_natural_language_metamorphic.py
  - tests/fault/test_bounded_natural_language_replay.py
  - tests/unit/test_bounded_river_call_ev_contract.py
  - tests/integration/test_bounded_river_call_ev_cli.py
  - tests/integration/test_bounded_river_call_ev_product_path.py
  - tests/integration/test_bounded_river_call_ev_evaluation.py
  - tests/adversarial/test_bounded_river_call_ev_security.py
  - tests/adversarial/test_bounded_river_call_ev_terminal_security.py
  - tests/fault/test_bounded_river_call_ev_durability.py
- Decision gate rationale:
  - Any expansion beyond the P3-030B bounded Japanese grammar into general natural-language, model-assisted, site-specific, or OCR parsing requires a new Decision gate covering ambiguity, source rights, privacy, and parser-quality evaluation
  - P2-025B actual Codex or cross-runtime execution bridge, outbound effects, authentication, budgets, and reconciliation
  - Any wider range/equity/call-EV scope beyond P3-030C, including multiple ranges, multiway or earlier streets, nonzero rake, all-in or side pots, external solver, GTO, or equilibrium semantics
- Completion-time relations (historical; not current status assertions):
  - P3-030A depends explicitly on completed P3-014A, P3-016A, and P3-017A milestones without implying that the broader RM-016 or RM-017 scopes are complete, and consumes their artifacts without weakening strict grammars, evidence rules, or the prohibition on inventing missing poker facts.
  - The approved P3-030B grammar remains finite, and P3-030C consumes it only for one confirmed river range/equity/no-rake call-EV slice; any general natural-language, model-assisted, site-specific, or OCR parser remains a separate future Decision gate.
  - P2-025B remains a separate Decision gate for an actual Codex or cross-runtime execution bridge.
  - At P3-030A completion, P2-028A had not started and was not activated by that local-only path.
  - P3-030C reuses P3-015A and P3-016B without widening them; multiple ranges, multiway or earlier-street equity, external solver, GTO, and equilibrium remain outside this milestone.

## Synchronization contract

- `poker-deliberate doctor --format json`の`roadmap`はpackage resourceの公開JSONから計算します。
- 公開projection自体はcandidate固有のcommitやtest実行を証明しません。status更新は同一schema更新検証、参照path/testのtracked検証、repository gateを別途要求します。
- `scripts/generate_roadmap_status.py --check`とcontract testがこのprojectionのdriftを検出します。
- wheel/sdistのpackage-dataはartifact smokeで候補ごとに別途検証します。この検証だけではrelease candidate判定とせず、RM-018Aのmatrix・license・artifact条件を別途要求します。
