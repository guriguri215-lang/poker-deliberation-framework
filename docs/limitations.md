# Current limitations

- P3-017A は repository-owned MIT synthetic fixture の 10 declared case と
  `exact-evidence-match` scorer だけを実装する。threshold `1.0` の pass は宣言済みevidenceの
  完全一致を意味するだけで、戦略品質、GTO、均衡、正確なrange、release readinessを意味しない。
  主観的metric、人間rubric、外部dataset/baseline、provider、external solver、runtime bridge、
  process sandbox、latency/cost計測は未実装である。structured timeoutはsynthetic bound checkで
  実processの強制停止ではない。
- P2-027A itself defines and evaluates local-data policy values only. P2-027B adds an explicitly
  authorized, one-run-at-a-time Python API for bounded ownership verification, same-volume
  quarantine, delayed staged deletion, immutable receipts/tombstones, CAS, and read-only
  reconciliation. It adds no cleanup CLI, encryption/key management, secure erase, automatic
  retry, repair, or broad discovery. `local_data_cleanup_executor` is implemented only for this
  bounded, approval-bound API; it does not imply a general-purpose or secure-erasure facility.
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
- P3-016Aのversioned range grammarは、provenanceとexact game-conditionを持つ1 opponent range
  に限り、explicit combo、pair、canonical descending suited/offsuit class、最大6桁の`@` decimal
  weightを受理する。weightはinteger millionthsで保持し、overlapはblocker前に拒否する。
  `+`、interval、exclusion、colon/sign/exponent weight、複数versioned range、equity integration、
  import/site/solver-native format、自然言語range inferenceは未実装である。legacy
  `RangeDefinition`、legacy parser、既存equity semanticsは変更しない。
- Legacy `hand_validator` does not fully model straddles, returned uncalled bets, site-specific rake
  timing, side pots, or every jurisdictional minimum-raise rule; its existing semantics remain
  unchanged.
- `hand_pot_ledger` implements returned uncalled bets, contribution layers, fold eligibility, and
  full-raise/short-all-in reopening only for `generic_nlhe_cash_no_rake_v1` version `1.0.0`,
  `supported_site=none`, one board, and explicit zero rake. It does not assign winners, evaluate
  hand strength, split payouts, or support rake, straddles, run-it-twice, PLO, tournaments, bounties,
  site-specific rules, or jurisdictional conformance.
- Free-text normalization supports only
  `poker-deliberation.generic-key-value-hand` version `1.0.0`, with `supported_site=none`.
  It is not a natural-language or site-specific parser. Unknown/malformed/duplicate/ambiguous input
  fails closed with stable bounded diagnostic codes; it is neither ignored nor converted into facts.
- Best response uses pure-policy enumeration and is limited to small, finite, acyclic games.
- Matrix support enumeration may use approximate fictitious play for degenerate/large unsupported
  cases; such output is never labeled exact.
- ICM has no future-game simulation, skill edge, risk preference, bounty equity model, or deal model.
  Its conservation tolerance is a binary64 forward-error envelope derived from the current cached
  subset-DP loops and non-negative arithmetic. It is not a proof for a different implementation,
  non-finite aggregate, external solver, GTO strategy, or equilibrium.
- Human approval decisions persist and resume; approved external actions are not auto-executed.
- Evidence records are validated, claim-linked, stored in `evidence.jsonl`, and included in reports.
  Case-specific web retrieval itself still requires a connected agent and explicit recording.
- Local calculators run in-process. Hard size/work/depth caps prevent callers from requesting
  unbounded work, convergent best-response DAGs are memoized, and over-budget results fail closed,
  but the MVP has no OS-level preemptive CPU or memory sandbox. Providers must honor the cooperative
  cancellation contract. Any future external-code executor must use process isolation and true
  time/memory limits.
- Ordinary P2-011A deadline/cancellation is cooperative and in-process. It distinguishes requested,
  acknowledged, and unconfirmed cancellation, but an uncooperative daemon thread may continue after
  the run reports a limitation. There is no process-tree kill, remote cancellation, or durable
  reconciliation.
- Role-specific provider contexts now use a versioned P2-024A attempt envelope with Python-local
  lineage, UTC use-expiry, exact allowlists, and unkeyed SHA-256 integrity. It does not persist the
  envelope, choose a storage retention duration, delete data, run cleanup, provide secure erase, add
  a durable authenticity trust anchor, execute retries, or connect Codex and Python runtimes.
- Ordinary product runs use a confined immutable V2 revision, marker-last terminal publication,
  verified current reader, and pointer/marker-bound durable budget settlement. This is failure
  atomic for cooperating readers; it is not a distributed transaction across the product and budget
  roots. A post-pointer settlement overrun or failure remains `incomplete`, and no automatic orphan
  repair or cleanup is performed. At-rest encryption is not implemented.
- Provider/tool budget accounting remains serial and in-memory during ordinary execution. Terminal
  publication alone reserves one durable slot, remaining runtime, and exact storage bytes in the
  P2-011B root. It does not meter an external provider's actual invoice or persist provider/tool
  attempt usage across ordinary resume. The injected provider's execution class and preflight cost
  estimate are trusted declarations. For
  public API compatibility, a pre-P2-011A injected provider that omits the new class is treated as a
  trusted in-process local-free declaration; callers must not use that legacy form for external work.
  After a sticky runtime or clock-observation failure, failure artifacts remain protected by
  `RunStore` hard caps, but their later physical bytes are not added to the frozen ledger snapshot.
- The v2 retry count describes candidate attempts and classification only. P2-011A has no automatic
  retry loop, backoff, durable retry state, or parallel execution; peak concurrency is fixed at one.
- Redaction covers common structured keys and token forms, not arbitrary personal information or
  every possible secret encoding.
- Japanese retrospective-negation handling is bounded lexical defense in depth. It preserves
  recognized negative spans while leaving explicit questions, double negation, and independent
  live context for blocking, but it is not full semantic language understanding.
- The concise `summary` format is an additive projection of a verified `FinalReport`, not a new
  stored artifact or replacement for the complete JSON/Markdown report. It deliberately omits full
  input, verification observations, execution audit, and unverified agent prose.
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
  long-path support in the OS/process configuration. The checked per-test `tmp_path` uses a short OS
  temporary root; other pytest temporary fixtures and explicit `--basetemp` paths do not establish
  support for arbitrarily deep clones.
- Pytest may leave empty session directories after its own retention cleanup. The repository does not
  recursively remove the shared temp root or other sessions because hook ordering and concurrent runs
  make such cleanup unsafe; empty ignored directories are an intentional local-only trade-off.
- Wheel/sdist contents, clean install, remote CI, and a coverage threshold are not asserted by the
  current Phase 0/1 baseline.
- P2-012A のstructural revisionとP2-010Bの`poker-final-report-artifact-v2`は引き続き
  internal `structural_nonterminal`であり、それ単独ではcompleted/resumable/terminalを意味しない。
  通常product runは、これらのV1意味を変更しない別のP2-012B terminal V2 protocolを使う。
- flat-v1はread-only `legacy_unverified`で、copy migration後もcompleted/resumableへ昇格しない。
  migrationには明示quiescenceが必要で、sourceをlock、repair、rename、deleteしない。
- P2-010B の v2 使用は producer `p2-010b-phase-revision` version `0.2.0` の専用 root と
  initially empty な target-run revision history を前提とする。exact same-process
  `current_committed` replay 以外は例外にしない。
- same-canonical-build、no-mixed-build、no-rolling access は trusted deployment assumption
  である。変更していない ownership/manifest/pointer schema は mixed-build violation を
  attest、detect、prevent できない。
- current の atomic visibility は cooperating local writer/reader に限る。power-loss、
  hardware cache、UNC/SMB/NFS/distributed filesystem、cross-volume rename、Windows directory
  durability、malicious writer authenticity は保証しない。
- P2-012A recovery claim 自体は metadata-only であり、orphan の cleanup、quarantine、repair、
  migration、selection、publish を行わない。P2-027B cleanup executor は別 protocol/root であり、
  P2-012A orphan recovery を自動化しない。secure erase は未実装である。
- Windows adapter は legacy 260 UTF-16 path bound を保守的に適用する。extended-length path と
  arbitrary deep clone は未検証である。POSIX adapter code は存在するが、この Windows
  セッションでの POSIX 実行結果は **UNKNOWN** である。
- P2-010Bは内部revision-only seamだけを実装する。通常のflat-v1 run、CLI、product reader、
  completion marker、durable resume、migration、retention/cleanupへは接続しない。
- same-process authorizationはprocess restart後に再構築できず、storageはplan hashをattestしない。
  structural revision publish後のin-memory apply failureはrollbackせず、terminal completionを
  意味しない。
- validationとsecret scanは承認済みexact payload familyに限定され、任意PIIやすべてのsecret
  encodingを検出する保証ではない。closed failureは診断詳細より非漏洩を優先する。
- P2-011Bの一般purpose bounded executorはinternal opt-inのままである。P2-012Bは通常productの
  terminal publication reservation/settlementだけへ接続する。通常provider/tool実行はparallel
  scheduling、automatic retryを利用しない。
- P2-011Bのexternal costはstrictなcaller-supplied integer estimate/actualと認証済みflagを検証するが、
  provider invoice、billing source、remote effect authenticityをmeterまたはattestしない。外部provider/
  solver自体も実装しない。
- P2-011B cancellationはrequested/acknowledged/cancelled/unconfirmed/effect_unknownをdurableに区別するが、
  cooperative threadを強制停止しない。process-tree kill、remote cancellation保証、CPU/memory/output
  isolationはP2-028Aまで未実装である。
- P2-011B structural resumeはbudget stateだけをverified historyから再構成する。P2-012Bの通常resumeは
  verified `approval_required` checkpointに限定される。P2-013A/Bは
  approval actor/authority/action digest、明示reissue、expiry/revocation recheckを追加するが、
  external action executionとdurable effect resumeはP2-028A以降まで未実装である。
- P2-012Aと同様、Windows directory sync unavailable evidence、power-loss、hardware cache、
  network/distributed filesystem、same-privilege malicious writer authenticityは保証しない。
- terminalization自体が極端に短いruntimeまたはartifact/run hard capを超える場合、ordinary callは
  `failed_with_limitations`を返し得るが、durable reportが存在するとは主張しない。pointer publication後の
  budget overrunはreaderから`incomplete`として扱われ、手動reconciliationが必要である。
- product manifestの`framework_version`はpackage versionを記録するが、runtimeからGit commitを
  authoritativeに取得できない場合の`source_commit_id`は64桁のzero sentinelである。これは
  **UNKNOWN**を表す運用規約で、source provenanceやbuild attestationの証明ではない。
## P2-013A の制限

- P2-013A の approve は exact authority record であり、外部 action を実行しない。結果は常に `external_executor_unavailable` / `failed_with_limitations` である。
- V1 request は historical-only であり、approve、action plan 推定、authority 推定、silent migration
  を行わない。P2-013B の reissue は全 pending V1 source と完全な V2 successor plan の明示を要求する。
- request expiry と authority revocation は decision admission と publication lock 内で検証する。P2-027B local cleanup executor は effect 直前にも再検証するが、remote effect と remote reconciliation は未実装である。
- failure audit は bounded append-only control ledger であり、自動 truncate／delete／repair を行わない。capacity exhaustion は別途承認された lifecycle action まで fail closed となる。
- approval V2 artifact 名は既存 P2-027A artifact-kind table を変更しないため、terminal lifecycle
  metadata 本体の対象集合ではなく、manifest inventory／hash／approval lineage により検証する。
  P2-027B は lifecycle audit 自身と approval V2 の3 control artifact を terminal published_at 起点の
  365日保持 subject として追加評価し、全 inventory の保持満了を exact cleanup plan に拘束する。

## P2-013B の制限

- reissue は request renewal の自動化ではない。V2 source は expiry 時刻以後、V1 source は全 pending
  request の明示 batch に限り、完全な successor action plan を要求する。
- `approval_reissues_v2.jsonl` は reissue を行った run だけに追加する。P2-027B の固定 cleanup
  inventory contract は変更しないため、この新しい control artifact を含む run の cleanup は
  P2-027B API で自動適格化せず fail closed となる。
- pre-execution recheck は pure binding であり、external provider/solver、process、network、
  filesystem effect を開始しない。effect receipt、remote idempotency、exactly-once、
  cancellation、reconciliation、sandbox は提供しない。
- authority provider が unavailable、provider/actor が変化、request/authority が expiry、
  revocationまたはscope不一致なら mutation zero で拒否する。same-privilege malicious writerへの
  authenticity、clock source authenticity、distributed freshness は証明しない。

## P2-027B の制限

- 明示1 runだけを処理し、glob、ignore規則、名前推測、mtime、全root巡回によるcleanup候補発見は行わない。
- quarantine と delete は別 plan・別 destructive approval で、plan source の時刻を live tombstone と
  再照合し、固定30日待機を短縮しない。
- delete は transaction-specific staging と `delete_prepared` を経由する。partial unlink、
  journal後、rename後、pointer replace不確実時は自動retry／resume／repairせず、人間判断を要する。
- current reader は到達可能な revision と対応する standalone journal だけを authority とし、
  pointer 未公開の orphan revision を replay success に採用しない。`delete_prepared` replay は
  staging の exact/partial/absent/unreadable を再観測して effect を保守的に報告する。
  pending control artifact や dangling current link があれば absent/no-effect と推定せず
  `effect_unknown` に停止する。canonical/path/link read failure の execute replay も
  `internal_invariant_error / effect=none` にはしない。
- same-volume local filesystem と cooperating process/kernel lock が前提である。cross-volume、
  distributed atomicity、network filesystem、exactly-once、process-tree cancellationを保証しない。
- `os.replace`、file fsync、利用可能な場合のdirectory syncを用いるが、hardware cache、突然の電源断、
  Windows directory sync unavailable環境における物理媒体への残存を保証しない。
- receipt/tombstone/hash chainはcorruptionと相関違反を検出するが、同権限writerのauthenticity、
  cryptographic erasure、raw block overwrite、復元不能性を証明しない。
  各 tombstone の保持期限は対応 receipt の `committed_at + 365日` として lineage 全体で検証する。
- cooperative cancellation は effect 前の mutation zero と effect 後の停止／reconciliation を
  提供するが、OS process hard-stop、undo、process-tree cancellation は提供しない。
- injected clock/fault/provider callback の後は effect state と controlを再検証するが、
  callback 内の同権限writerを隔離するprocess sandboxやACL境界を提供するものではない。
