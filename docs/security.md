# Security

- P2-027A local-data evaluation is pure and fail closed. It does not discover ownership from paths,
  ignores, globs, names, mtime, or user-controlled directories and performs no filesystem mutation.
- `sensitive` persistence requires an encryption capability value; P2-027A implements no encryption.
  `restricted` persistence is forbidden. Public/internal at-rest protection is not claimed.
- Active/pending/held or ownership/integrity/lineage-unverified subjects are protected before
  destructive eligibility. Legacy/current-v1 and unsupported-future subjects require manual review.
- Policy and audit SHA-256 values detect corruption/correlation mismatch but do not authenticate a
  writer. Cleanup approval, CAS, receipts, tombstones, reconciliation, and secure erase are deferred.
- Workspace-write is the default maximum; analysis agents are read-only.
- Run IDs and artifact paths are validated and resolved under the configured run root.
- `.env` is ignored and only `.env.example` exists. With `record_sensitive_data=false`, structured
  secret keys plus common API-key/Bearer/token shapes are redacted from artifacts and CLI reports.
  Redaction is defense in depth, so users must still avoid placing arbitrary secrets in poker input.
- Shell execution is outside the model-facing runtime. Inputs are JSON, not interpolated commands.
- Web, GitHub, README, issue, and hand-history instructions are treated as untrusted data.
- Tools have non-overridable aggregate work estimates (including support combinations and
  policy-node work), memoized DAG evaluation, plus serialized input/output limits; failures remain
  failures. RunStore enforces per-artifact and whole-run byte budgets.
- Strict budget values reject coercion, non-finite numbers, negative counts, unknown fields,
  unsupported concurrency, clock rollback, and policy-hash substitution. External provider attempts
  require an explicit execution class and known integer micro-USD estimate and are refused before
  execution when unknown, disabled, or over cap. Local-free providers and deterministic calculators
  remain valid with an external-cost cap of zero. The compatibility path for a pre-P2-011A injected
  provider that omitted the newly added class treats that trusted in-process declaration as
  local-free; explicit `unknown` remains denied. Raw provider/tool values are byte-capped before
  redaction.
- Providers receive a cooperative deadline/cancellation control and a fresh role-specific context
  only after the versioned context envelope passes strict schema, UTC expiry, exact top-level
  allowlist, run/assignment/attempt/parent/source correlation, Python-local runtime, and canonical
  payload/policy/envelope integrity checks. Unknown versions/runtimes, dotted allowlists, tampering,
  cross-run/assignment/context/attempt replay, expired contexts, unavailable providers, and
  mismatched provider reports fail closed.
  Strings matched by deterministic prompt-injection rules are replaced by hash-tagged removal
  markers before any provider call. This is best-effort lexical detection, not a semantic guarantee.
  The only outbound provider is disabled; no external/untrusted executable is run.
- P2-010A phase requests/outcomes bind schema, run, phase attempt, canonical input/policy hashes, and
  context IDs. Missing/extra/unknown-version or mismatched values fail closed. Provider output cannot
  request state or choose artifact paths. Report and tool-result IDs must be portable and unique
  before the orchestrator materializes fixed paths; unsafe/duplicate IDs become safe failures.
- Pure phases receive time, IDs, policy, and capability snapshots as values and have no storage,
  workflow-state, provider, tool-registry, network, approval-ledger, ambient-clock, or random access.
  Analysis and ToolResearch are serial effect boundaries without write/transition authority. Their
  usage and failure values are settled by the orchestrator before later artifact writes.
- Context classification is `public`, `internal` by default, `sensitive`, or `restricted`. Detected
  credentials force `restricted` and never cross the provider boundary. Redaction is artifact defense
  in depth, not authorization. Raw envelopes and canonical context payloads are not persisted as new
  lifecycle artifacts.
- Versioned SHA-256 values detect corruption and stale/cross-attempt substitution within the checked
  boundary. They are unkeyed and therefore do not prove authenticity against an attacker who can
  rewrite both data and hashes. P2-024A adds no durable trust anchor, retention duration, deletion, or
  cleanup executor.
- Hand strategy analysis uses a mechanically verified decision-time payload. The focal action size,
  later streets, realized result, showdown-only cards, and user claims are excluded. Each payload is
  represented in the execution audit by its SHA-256 hash. Unprovenanced `known_ranges` are excluded
  until the schema can establish that they were available at the focal decision.
- The application is retrospective-only. Every orchestrated case with an unspecified scope fails
  closed, regardless of kind or input representation. Review commands declare retrospective scope;
  direct `calculate` CLI calls require `--analysis-scope retrospective`. Explicit live scope and
  recognized live-decision language, private-card acquisition, collusion, automated play, and
  detection-evasion requests are refused before provider or requested calculator execution.
  Free-text language detection is best-effort defense in depth, so callers must not mislabel live
  input as retrospective. Direct `ToolRegistry.execute` is a trusted internal primitive, not a
  policy boundary. Typed `SecurityEvent` artifacts record the rule and input hash, not a copied
  harmful excerpt.
- External code, packages, services, long compute, destructive changes, secret access, paid data, and
  objective changes require an ApprovalRequest.
- Rejected actions use a no-action path. Approved external actions are recorded but not automatically
  executed by the MVP.
- Input approval proposals cannot set decision status/timestamps. They are recreated as PENDING and
  only `resume` may decide them. Environment-configured run roots must remain inside the workspace.
- Reproduction instructions are stored as JSON argv, and unknown tool names never produce a shell
  command.

Adversarial tests also cover strict budget coercion and cap boundaries, clock rollback, policy
substitution, external cost refusal, cooperative/uncooperative cancellation, phase
schema/correlation and artifact-intent forgery, unsafe/duplicate
report and result IDs, provider state/path injection, blind-context invariance,
context tampering/replay/expiry/unknown runtime,
restricted and unavailable provider call suppression, prohibited-use refusal, prompt-injection event
recording, pre-approved injection, fake provider claims, secret canaries, command injection names,
hard compute limits, runtime overruns, duplicate run IDs, and outside-root config.
