# Current limitations

- P2-027A defines and evaluates local-data policy values only. There is no filesystem discovery,
  ownership scan, quarantine move, deletion, cleanup CLI, encryption/key management, secure erase,
  durable lifecycle audit, receipt, tombstone, CAS, or partial-failure reconciliation. All
  dispositions are candidates; `local_data_cleanup_executor` remains unavailable.
- `OpenAIAgentsProvider` outbound analyze is not implemented. It reports `disabled` and `available=false`
  whether the optional SDK/API key is absent or present; the probes never imply outbound capability.
- Codex-native agents/Skills and the Python role/provider catalog are separate execution surfaces.
  Python does not launch Codex agents, and Codex execution is not automatically captured in Python
  state, approvals, `AgentExecutionRecord`, or run artifacts.
- The workspace is an initialized Git repository. Local history can be audited offline, but remote
  repository visibility, branch protection, and Actions logs remain UNKNOWN without an approved
  external check.
- No external poker solver, external card library, paid range, or private dataset is bundled.
- No full NLHE game-tree equilibrium, CFR/CFR+, node locking, or solver exploitability computation.
- Equity is heads-up NLHE only; multiway and PLO equity are unsupported.
- Range syntax supports explicit combos, pairs, suited/offsuit classes, and weights; `+`, intervals,
  exclusions, and solver-native range formats are not implemented.
- Hand validation does not fully model straddles, returned uncalled bets, site-specific rake timing,
  side pots, or every jurisdictional minimum-raise rule.
- Free-text normalization supports the documented key-value/player/action format. It is not a
  natural-language or site-specific parser; unrecognized lines are preserved as warnings, not facts.
- Best response uses pure-policy enumeration and is limited to small, finite, acyclic games.
- Matrix support enumeration may use approximate fictitious play for degenerate/large unsupported
  cases; such output is never labeled exact.
- ICM has no future-game simulation, skill edge, risk preference, bounty equity model, or deal model.
- Human approval decisions persist and resume; approved external actions are not auto-executed.
- Evidence records are validated, claim-linked, stored in `evidence.jsonl`, and included in reports.
  Case-specific web retrieval itself still requires a connected agent and explicit recording.
- Local calculators run in-process. Hard size/work/depth caps prevent callers from requesting
  unbounded work, convergent best-response DAGs are memoized, and over-budget results fail closed,
  but the MVP has no OS-level preemptive CPU or memory sandbox. Providers must honor the cooperative
  cancellation contract. Any future external-code executor must use process isolation and true
  time/memory limits.
- P2-011A deadline/cancellation is cooperative and in-process. It distinguishes requested,
  acknowledged, and unconfirmed cancellation, but an uncooperative daemon thread may continue after
  the run reports a limitation. There is no process-tree kill, remote cancellation, or durable
  reconciliation.
- Role-specific provider contexts now use a versioned P2-024A attempt envelope with Python-local
  lineage, UTC use-expiry, exact allowlists, and unkeyed SHA-256 integrity. It does not persist the
  envelope, choose a storage retention duration, delete data, run cleanup, provide secure erase, add
  a durable authenticity trust anchor, execute retries, or connect Codex and Python runtimes.
- Run artifacts are confined. JSON and text replacement writes use a temporary file per artifact,
  but JSONL evidence is appended directly and is not crash-atomic. There is no versioned run
  manifest, whole-run atomic completion protocol, retention/deletion enforcement, migration
  contract, or at-rest encryption. P2-027A provides the pure policy only; RunStore does not apply it.
- Budget accounting is serial and in-memory. It does not reserve concurrent work, meter an external
  provider's actual invoice, persist a usage manifest, or settle usage across durable resume. The
  injected provider's execution class and preflight cost estimate are trusted declarations. For
  public API compatibility, a pre-P2-011A injected provider that omits the new class is treated as a
  trusted in-process local-free declaration; callers must not use that legacy form for external work.
  After a sticky runtime or clock-observation failure, failure artifacts remain protected by
  `RunStore` hard caps, but their later physical bytes are not added to the frozen ledger snapshot.
- The v2 retry count describes candidate attempts and classification only. P2-011A has no automatic
  retry loop, backoff, durable retry state, or parallel execution; peak concurrency is fixed at one.
- Redaction covers common structured keys and token forms, not arbitrary personal information or
  every possible secret encoding.
- `audit-claim` without structured calculation inputs preserves a USER_CLAIM as unverified rather
  than guessing its truth.
- The offline public preflight uses bounded pattern matching and package metadata; it is not proof that
  every possible secret, PII encoding, or license issue is absent. It intentionally does not scan
  ignored `user_materials/` or ignored runs. It locally scans reachable commit blobs and commit/tag/ref
  metadata, redacts matched values and identity candidates to fingerprints, and keeps secret/PII status
  UNKNOWN when an object, ref inventory, or supported text encoding cannot be read completely. Git
  author/committer/tagger identities are review candidates, not confirmed personal information.
- CPython 3.11-3.13 on Windows/Ubuntu is a candidate matrix inferred from `requires-python`, not a set
  of verified rows. Rows not executed locally or in CI remain UNKNOWN.
- On Windows, pytest path viability depends on the combined depth of the clone and temp paths and on
  long-path support in the OS/process configuration. The short workspace-local automatic temp reduces
  this risk but does not establish support for arbitrarily deep clone or explicit `--basetemp` paths.
- Pytest may leave empty session directories after its own retention cleanup. The repository does not
  recursively remove the shared temp root or other sessions because hook ordering and concurrent runs
  make such cleanup unsafe; empty ignored directories are an intentional local-only trade-off.
- Wheel/sdist contents, clean install, remote CI, and a coverage threshold are not asserted by the
  current Phase 0/1 baseline.
- P2-012A は opt-in の structural revision foundation であり、既存 CLI/product run を durable
  revision に切り替えない。verified structural revision は completed、resumable、terminal を
  意味しない。
- current の atomic visibility は cooperating local writer/reader に限る。power-loss、
  hardware cache、UNC/SMB/NFS/distributed filesystem、cross-volume rename、Windows directory
  durability、malicious writer authenticity は保証しない。
- recovery claim は metadata-only であり、orphan の cleanup、quarantine、repair、migration、
  selection、publish を行わない。retention/cleanup executor と secure erase は未実装である。
- Windows adapter は legacy 260 UTF-16 path bound を保守的に適用する。extended-length path と
  arbitrary deep clone は未検証である。POSIX adapter code は存在するが、この Windows
  セッションでの POSIX 実行結果は **UNKNOWN** である。
