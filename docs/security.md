# Security

- P2-027A local-data evaluation is pure and fail closed. It does not discover ownership from paths,
  ignores, globs, names, mtime, or user-controlled directories and performs no filesystem mutation.
- `sensitive` persistence requires an encryption capability value; P2-027A implements no encryption.
  `restricted` persistence is forbidden. Public/internal at-rest protection is not claimed.
- Active/pending/held or ownership/integrity/lineage-unverified subjects are protected before
  destructive eligibility. Legacy/current-v1 and unsupported-future subjects require manual review.
- Classification audit binds a canonical source vector and strict trust/check booleans. Ownership,
  integrity, and lineage use typed provenance/evidence states; path-like non-run names and explicit
  excluded provenance cannot become destructive candidates.
- Opaque local-data identifiers reject Windows reserved-device names, including extension variants
  and trailing-dot aliases. Run artifact names remain on an exact logical-name allowlist.
- Lifecycle audit/failure values do not copy raw subject IDs, run IDs, approval references, or
  non-run logical names. They bind those values with purpose-separated SHA-256 digests; raw run
  logical names are retained only when they match the fixed artifact allowlist. Known credential
  shapes are also rejected at the input boundary as defense in depth.
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

## Revision storage threat boundary

P2-012A は dedicated root と legacy root の重複を拒否し、ownership/legacy filesystem identity、
portable run/path grammar、ASCII-case alias、NFC、reserved device、ADS、link/reparse/hardlink、
unknown namespace entry、cross-run lineage を fail closed で検査する。payload は P2-027A の
trusted classification evidence と clean restricted-secret check を再実行し、control/provenance
には raw path、payload excerpt、credential、自由形式 error message を保存しない。

authority は stable file に対する process registry + nonblocking kernel lock である。
advisory metadata、mtime、PID、claim file は authority ではない。SHA-256 は corruption と
correlation の検出用であり writer authenticity を証明しない。同権限の malicious writer、
完全な syscall 間 TOCTOU、ACL hardening、network filesystem、secure erase は非保証である。

## P2-010B validation and authority boundary

revision coordinatorはpublish前に各admitted payloadをsize制限、strict UTF-8、media別parse、
recursive restricted-secret pattern、P2-027A classification evidenceで検査する。phase trace、
context envelope、tool request/result、final-report-v2 provenanceをraw preimageから再検証するが、
raw trace、provider input、context payload、matched value、例外、tracebackはartifactにも返却値にも
含めない。失敗はclosed `phase-revision-failure-v1` codeだけに変換する。

transition authorityは同一process factoryがoriginal bundle identityへ発行する非直列化値であり、
storage上の権限や認証tokenではない。publish uncertainty、reconciliation requirement、stale plan、
reconstructed authorityはCOMPLETEDを許可しない。この境界はmalicious same-process code、
process restart、distributed writer、secure eraseを保証しない。

## Durable budget threat boundary

P2-011Bはportable identifier grammarとsecret-shape rejection、strict canonical request hash、
exclusive producer/artifact admission、current-to-genesis verification、revision CAS、run lock、
append-only semantic successor validationでcross-run replay、policy/activation substitution、
duplicate attempt/context/idempotency/ordinal、owner/role/phase/assignment substitutionを拒否する。
unknownまたはunauthenticated external costはeffect前/settlement時にfail closedになる。

committed reserved permitはeffect開始証拠ではなく、明示no-effect releaseだけが許される。
started-unsettled、ambiguous current replace、unconfirmed cancellation、callable exceptionは
`effect_unknown`または`reconciliation_required`であり、successやautomatic retryに変換しない。
failure latchは最初のunsafe observationを保持する。

in-process cooperative tokenはprocess-tree kill、remote billing停止、OS resource isolationの証拠ではない。
それらを要求するtaskは、実装されていないRM-028 boundary evidenceなしに開始しない。unkeyed SHA-256は
corruption/correlation検出であり、same-privilege malicious writerに対するauthenticityではない。

## P2-012B product terminal threat boundary

product writer/readerはlegacy/product両rootのcase-insensitive alias、mixed namespace、traversal、
absolute/drive/URI/ADS、非NFC、Windows reserved name、symlink/reparse/hardlink、cross-run substitution、
duplicate JSON key、unknown version、hash/algorithm downgradeをtrust前に拒否する。run ID validationは
root初期化より先に行い、不正IDでproduct/budget rootを作らない。

`RunManifestV2`はpayload inventoryとinput/state/event/approval/context/execution lineage、
budget settlement、lifecycle metadataを束縛する。terminal statusはrevision-local marker-last、
manifest/payload reread、current pointer CAS、pointer/marker-bound budget settlementの全てが確認できた
場合だけ返す。manifest-only、marker-only、terminal state-only、missing payload、CAS ambiguity、
settlement uncertaintyはsuccessにならない。

flat-v1はexact bytesをread-onlyで扱い、欠けているintegrity guaranteeを捏造しない。
copy migrationは元root/run IDをhashだけで束縛し、raw pathやpayload excerptをcontrol metadataへ
保存しない。source inventoryがcopy中に変化した場合、destination currentをpublishせず
staging orphanとしてreconciliationを要求する。

この境界もunkeyed SHA-256とcooperating local process/kernel lockに依存する。同権限のmalicious
writer authenticity、OS crash後のpower-loss durability、network/distributed filesystem、
Windows directory fsync、ACL/signature/HMAC、automatic orphan cleanupは保証しない。
manifestのall-zero `source_commit_id`はruntimeでauthoritative commitが得られない場合の
**UNKNOWN** sentinelであり、Git/build provenanceのattestationとして信頼してはならない。
## Approval authority threat boundary

P2-013A は claimed actor を信用せず、注入された `DecisionAuthorityProvider` の canonical snapshot と全 field を照合する。snapshot の provider ID/version、resolved UTC、actor と専用 digest は immutable decision record、outcome、domain audit に拘束し、publication lock 内の再検証でも provider ID/version と actor の一致を要求する。既定 CLI actor は unverified で `reject:any` しか持たない。approve は verified actor、exact category scope、未失効、not-revoked の全条件を必要とする。

action plan と outbound content は raw payload ではなく bounded identifier／classification／SHA-256 binding として保存する。failure audit は actor、decision、idempotency、batch の hash と failure code だけを保存し、raw reason、credential、token、provider input、traceback を保存しない。failure 制限は actor/run ごとの真の rolling 60 秒窓として再構成し、窓内 32 件と単一 marker を strict pointer state へ拘束する。

SHA-256 と hash chain は accidental corruption と substitution を検出するためのものであり、same-privilege malicious writer の authenticity は保証しない。default CLI は remote identity を検証せず、external effect、network verification、signature/HMAC、secure erase を実装しない。
