# Security

- P3-015A `hand_pot_ledger`はstrict/frozen schemaと明示profile/version/site/chip unitを要求し、
  unknown field/version、非整数unit、overflow、rake、straddle、PLO、tournament、run-it-twiceを
  fail closedにする。unknown profile値はclosed error codeで拒否し、caller contentをerrorへecho
  しない。product pathはmetadataのhand置換を拒否し、既存canonical case handだけをtool requestへ
  束縛する。整数conservationと独立`Fraction` oracleの両方が一致しないresultは成功にならない。
- P3-014A normalization accepts exact bytes only through a strict/frozen version `1.0.0` request.
  BOM、invalid UTF-8、mixed/bare-CR newline、non-NFC、control/format character、secret shape、
  unknown key、duplicate scalar、ambiguous numeric syntax、resource overflowをfail closedにする。
  diagnosticsはclosed code/field/lineだけを保存し、raw value、secret、CSV/Pydantic errorをecho
  しない。`normalization.json`はsource byte hashとcanonical hand hashを持ち、terminal readerが
  `input.json`/`normalized_case.json`と再相関する。SHA-256は署名やwriter authenticityではない。
- P3-017A evaluation valuesはstrict・frozen・unknown-field拒否で、NFC、portable ID、bounded
  text、canonical JSON、purpose-separated SHA-256を検証する。suite/manifest/dataset/scorer/
  licenseのhashとcase countが不一致ならcase実行前にfail closedとなる。
- fixtureはrepository-owned MIT synthetic dataだけで、network/provider/external solver/
  runtime bridgeを起動しない。solver availability caseは既存のhonest unavailable adapterだけを
  実行し、solver evidenceなしの均衡claimを拒否する。
- evaluation metadataは既知credential形状を拒否し、Pydantic errorからinput valueを隠す。
  synthetic canaryは実行時に分割片から組み立て、fixture/resultへ値を保存しない。prompt-likeな
  extra fieldはstrict schemaで拒否する。
- result outputはrepository-relative `tmp/` JSONへ限定する。hashはcorruption/correlation検出用で
  writer authenticity、署名、秘密性を提供せず、synthetic timeoutはOS process isolationではない。
- P2-025A runtime conformance valuesはstrict・frozen・unknown-field拒否であり、NFC、UTC、
  portable ID、bounded text、canonical JSON、purpose-separated SHA-256を検証する。hashは
  corruption/correlation検出用であり、writer authenticityや署名を提供しない。
- 単一runtime検証はrole inventory hash、role/semantic mapping、宣言済みtool/capability、
  context expiry、approval expiry、execution audit hash、tool lineage、terminal statusを照合する。
  runtime間比較はobjective、classification、payload/policy/budget、provenance、exact allowlist、
  approval action digest、epistemic/strategy/evidence/error意味の変更をfail closedで検出する。
- Codex role TOMLは追跡済みfieldだけをdataとしてparseし、instructionを実行しない。Codexのambient
  tool catalogは推測せず`undeclared`、Python tool/capabilityは呼出側が与えた機械的snapshotだけを
  使用する。意図的に未対応のroleを同等roleとして扱わない。
- verified Python product projectionは既存terminal readerのbyte検証を前提にし、外部provider、
  approval-bearing run、非terminal/失敗run、report/tool-resultのbyte差を拒否する。unavailable
  solverはlimitationのままで、solver evidenceなしにGTO・均衡・正確なrangeへ昇格しない。
- conformance metadataは既知credential形状とcontrol characterを拒否する。テストcanaryは
  実行時に組み立て、追跡fixtureへcredential値を保存しない。
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
  writer. P2-027B adds exact cleanup approval, CAS, receipts, tombstones, and read-only
  reconciliation; secure erase remains unimplemented.
- Workspace-write is the default maximum; analysis agents are read-only.
- Run IDs and artifact paths are validated and resolved under the configured run root.
- `.env` is ignored and only `.env.example` exists. With `record_sensitive_data=false`, structured
  secret keys plus common API-key/Bearer/token shapes are redacted from artifacts and CLI reports.
  If multiple source mapping keys project to the same redacted key, a deterministic ordinal
  collision suffix preserves every entry without exposing a source-secret digest; nested mappings
  use the same rule.
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
  Each blocked rule has user-facing guidance; unspecified scope explicitly requests
  `analysis_scope="retrospective"`. Bounded Japanese negation forms such as
  `今プレイ中ではありません` and `今プレイ中ではなく` are treated as retrospective context,
  while questions, double negation, separate live clauses, and separate live instructions remain
  fail closed.
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

## P2-027B cleanup threat boundary

cleanup root は repository/workspace、home root、`.git`、`user_materials`、`tmp/goals`、
product/legacy rootとの同一・祖先・子孫関係を拒否する。対象は verified terminal current と
P2-027A lifecycle audit が destructive candidate とした明示1 runだけである。portable path、
NFC/case alias、reserved name、root escape、symlink/reparse、hardlink、Windows ADS、unknown entry、
cross-volume、tree/current substitution は effect 前に fail closed となる。

approval は cleanup plan digest、module inventory、cleanup root identity、policy/trace ID、
resource limits、execution/idempotency/expiry を P2-013A `CanonicalActionPlanV2` に拘束する。
executor は immutable approval chain と actor/provider authority を product または detached-run
lock 内の effect 直前に再検証する。失効、revocation、scope/digest/actor/provider mismatch は
filesystem effect zero である。
final authority/hold admission は durable journal 公開と fault/cancellation hook の後に行い、
失効時は pre-effect journal を exact rollback する。
外部 authority/hold/cancellation/clock callback の後に product/cleanup current、source tree、
destination absence、same-volume を再読してから rename する。cleanup root の read/inspect は
保存 marker だけでなく live product-root ownership/root identity と再照合し、別 root への
marker 再利用を initialized/committed と判定しない。
quarantine の final provider callback 後には cleanup marker、live root binding、control 許可集合、
standalone journal の canonical bytes も再読する。effect 後の clock/fault callback の後にも
payload namespace と current/control を再検証し、矛盾した current や成功結果を公開しない。
delete admission は plan の quarantine entry と fixed policy review window を live tombstone に
再拘束する。control namespace に dangling link や pending journal/revision/temp があって strict
current を読めない場合は no-effect と推定せず `effect_unknown` に停止する。
canonical/path/link failure を伴う execute replay も同じ reconciliation に正規化する。

control artifact は bounded identifiers と purpose-separated SHA-256 binding を保存し、raw payload、
credential、approval reason、provider input、traceback を保存しない。ただし同権限の malicious
writer、完全な syscall 間 TOCTOU 排除、ACL/signature/HMAC、power-loss durability、
distributed filesystem、secure erase は保証しない。

## P2-028A isolated-job threat boundary

P2-028AはAMD64 Windows上でbase Pythonと固定repository synthetic helperのfile identity/hashを
effect-admission approval再検証の直前に再検証し、`ResumeThread`直前にはapprovalの
`valid_until`をlive clockで再確認する。resource取得前に登録したpreparation leaseと非daemon workerを
使い、明示`HANDLE_LIST`と`JOB_LIST`を一つの`CreateProcessW`へ渡すため、childは生成時からCPU time、
committed memory、active process、`KILL_ON_JOB_CLOSE`を設定したJob Objectへ所属するsuspended
processである。leaseはbackend return handoff中もcleanup ownershipを保持し、exact requery後にだけ
resumeする。Job CPU
accountingとprocess CPU timeをcontrollerが独立にpollし、上限到達時は`TerminateJobObject`でtree全体を停止する。stdinはNUL、
stdout/stderrはbounded pipe、追加handleはidentity-boundなworkspace内input一つだけである。

request schemaはshell、任意executable/argv/environment、network要求、raw secret値を受け付けない。
path component、reserved name、ADS、workspace escape、reparse/symlink、approved inputのhardlink、
2 MiB超過、open後identity変更をfail closedにする。approvalは`external_code` action digestとlive authorityへ
effect直前まで拘束し、context、budget、secret-reference setはhash/provenanceだけを保存する。

Job終了、wall/output/cancel超過時とpreparation worker／return handoff／resume／running publication／
waitのcontroller abort時はtree全体を停止し、active process 0を再観測する。coordinatorもterminal
settlement前にtree停止証拠を独立に検証し、未確認ならworker-liveの`effect_unknown`としてpermitを
閉じない。exit code 0でもprocess/job CPU evidenceがhard cap以上なら
`cpu_limit`であり、successにしない。ただしlocal Job
terminationはremote provider、remote billing、remote cancellation、network isolationの証拠ではない。
`effect_unknown`はsuccess/failed/retryへ変換せず、保存済みPID/creation time不在と別のopaque
reconciliation evidence digestを確認しても`reconciled`は非successのままである。

同権限malicious writer、完全なTOCTOU排除、reduced token/AppContainer、全OS DLL attestation、
distributed/power-loss durability、writer authenticity、秘密性は非保証である。詳細は
[`isolated-job-control.md`](isolated-job-control.md)を参照する。
repository-owned backend同士のprocess creationはinheritable handle存続中に直列化するが、
backend外の未調整process spawnerまで同期するOS全体の保証ではない。
