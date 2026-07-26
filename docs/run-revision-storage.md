# Immutable revision storage foundation

## 境界

P2-012A は、明示的に初期化した専用 root へ immutable な
`structural_nonterminal` revision を保存する内部 foundation である。
P2-012A完了時点では既存の `RunStore`、`Orchestrator`、CLI、
`run/resume/show/load_report/report_path`、flat-v1 の bytes・layout・write order を変更しなかった。
新しい型は
`poker_deliberation.storage.revision_*` から内部利用し、package-root API には公開しない。

P2-012A は completion marker、terminal manifest/pointer、completed status mapping、
migration、retention、cleanup、resume integration を実装しない。これらは別承認が必要な
P2-012B 以降の範囲である。したがって revision の検証成功は「run 完了」や「再開可能」を意味しない。

P2-012BはこのfoundationのV1 schemaを変更せず、別の
`terminal_models.py`、`terminal_canonical.py`、`terminal_store.py`でproduct terminal V2を実装する。
以下のP2-012A節はimmutable foundationの契約であり、末尾のP2-012B節が通常product経路の追加層を
定義する。

## 専用 root

`initialize_revision_root`、`inspect_root_initialization`、`reconcile_revision_root`、
`RunRevisionStore` は、明示的な `revision_root` と既存の `legacy_runs_root` を要求する。
両 root は同一・祖先・子孫関係を禁止し、legacy root の resolved path と filesystem identity
から得た domain-separated SHA-256 のみを ownership marker に保存する。生の絶対 path は
control file に保存しない。

初期化だけが空の専用 root を作成できる。constructor と通常の inspection/read は
side-effect-free である。root-level の stable authority、ownership marker、
`.revision-control/locks`、`runs` 以外の entry、異なる ownership、legacy identity の変化、
flat-v1 の同一または ASCII-case alias は fail closed になる。

## revision layout

```text
<revision_root>/
  .revision-init.authority.lock
  ownership.json
  .revision-control/locks/
    <case-folded-run-key>.authority.lock
    <case-folded-run-key>.metadata.json
  runs/<run_id>/.revision-store/
    current.json
    transactions/<transaction_id>/
    revisions/r<revision>-<transaction_id>/
      transaction.json
      manifest.json
      payload/<logical-name>
    recovery-claims/
      .tmp/
      <transaction_id>.json
```

authority file は identity を保った 1 byte の file であり、置換・unlink・truncate をしない。
Windows は offset 0、length 1 の `msvcrt` nonblocking lock、POSIX は whole-file
`flock(LOCK_EX|LOCK_NB)` を使う。metadata は lock authority ではなく、lock 保持中にだけ
atomic replace される last-owner evidence である。待機、lease、heartbeat、stale timer、
lock stealing、自動 retry はない。

## canonical transaction

pure preflight は filesystem mutation より前に、strict schema/version、portable ID/path、
UTC、producer identity、P2-027A classification replay、media/schema table、canonical bytes、
typed provenance、source graph、per-artifact size、ordered inventory、transaction/manifest/pointer
の exact bytes を確定する。

control JSON は UTF-8、BOM/newline なし、sorted-key compact JSON、NFC、finite value、
exact six-digit UTC `Z` 形式である。JSONL は各 canonical object の末尾に LF を必須とし、
text は NFC・LF-only の caller bytes を保持する。accepted payload は deserialize 後の
strict domain schema と canonical bytes の一致を再検証し、writer は accepted payload を
再 serialize しない。

各 payload は exactly one `LocalDataBindingV1` を持つ。P2-027A policy/evidence、
user input、same-revision の earlier payload、external evidence、approval decision、
context、phase、budget、tool、report の binding は、対応する typed record と hash domain を
相関検証する。future/self/circular/cross-run/duplicate/conflicting source、未登録 tool contract、
不一致の final-report ledger/markdown renderer は拒否する。

### final-report artifact schema

`final_report.json` は inventory の `artifact_schema_version` だけで意味を dispatch する。
reader はその未信頼値を canonical table に渡し、media type、serialization、schema version、
origin kind の完全な tuple を選択してから inventory と比較する。未知、欠落、downgrade、
cross-label は fail closed であり、path・inventory・manifest・source graph の順序は引き続き
UTF-8 lexical/canonical order である。

`poker-final-report-artifact-v1` の canonical bytes、lexical `ToolResult` 比較、final report に
`ContextBindingV1` を必須とする規則は不変で、既存 v1 revision はそのまま readable である。
`poker-final-report-artifact-v2` だけが、各 tool input/result pair の exactly one かつ
byte-identical な `ToolBindingV1`、ordinal の unique contiguous `0..n-1`、ordinal 順の
`FinalReport.tool_results` 一致を要求する。v2 の final-report context は
`agent_reports` または `agent_execution_records` が非空なら一つ以上必要で execution ledger
と一致し、両方が空なら exactly zero でなければならない。

v2 は内部 `structural_nonterminal` artifact contract であり、product の terminal/read/resume
format ではない。old v1-only build は v2 schema version を unknown として拒否する。
P2-010B で v2 を使用する場合、ownership marker が
`producer_id=p2-010b-phase-revision`、`producer_version=0.2.0` の専用 revision root と、
初回 publication 前に revision が存在しない target run を要求する。例外は同一 process で
freeze した元の request/plan bundle の exact `current_committed` replay だけである。
same-canonical-build、no-mixed-build、no-rolling access は trusted deployment assumption であり、
変更していない ownership/manifest/pointer schema はこれを attest、detect、prevent しない。
これはP2-012A/P2-010B foundation単独の境界である。terminal reader/status、completion marker、
migration、resume integrationは、後段のP2-012B product protocolだけが所有する。

## publish と read

publish は process registry と kernel lock の下で次の順序を取る。

1. ownership、legacy/revision sibling、known-entry grammar、link/reparse/hardlink、current lineage、
   現在の physical bytes を検証する。
2. idempotency と expected revision/manifest/pointer の CAS を判定する。
3. metadata temp、staging revision、pointer temp が既存 bytes と共存する exact peak を admission
   する。equality は許可し、1 byte over は拒否する。
4. transaction、payload、manifest を exclusive create・flush/fsync・close・reread し、
   complete staging を immutable revision へ rename する。
5. current-to-genesis lineage を再検証し、pointer temp を atomic replace する直前にも CAS と
   ownership を再確認する。
6. new current、manifest、inventory、lineage を reconciliation read し、対応 platform の
   directory sync を試みて lock を解放する。

`read_current` は lock を作成・取得せず、pointer-read、complete lineage verification、
pointer-reread を行う。並行する cooperating writer がある場合は complete old または complete
new pointer のみを返す。reachable history は current から revision 1 まで exact decrement と
manifest hash で連結される。

同一 transaction/digest が current または reachable history にあれば domain write なしで
idempotent outcome を返す。staging または unreachable revision の replay は自動採用せず
reconciliation を要求する。recovery claim は authoritative lock 下の metadata-only evidence
であり、orphan の移動・削除・修復・publish を行わない。reachable revision と path-only partial
staging は claim できない。

## failure、durability、limits

failure は redacted な `RunStorageFailureV1` として、code、stage、filesystem/domain effect、
previous-revision effect、reconciliation requirement、durability evidence を返す。
blind retry は禁止され、semantic retryable は overlapping `run_locked` だけである。

default limit は payload/control artifact 1,000,000 bytes、dedicated revision run
10,000,000 bytes である。run total は reachable history、staging、published orphan、
claim final/temp、current/temp、attributable authority/metadata を含む。filesystem allocation、
compression、snapshot、flat-v1、P2-011B reservation は含まない。

- **FACT**: new regular file は flush と `os.fsync` を試みる。POSIX directory fsync は
  supported な場合に試みる。
- **FACT**: Windows directory sync は `unavailable` evidence であり、それだけでは publish
  failure にしない。
- **UNKNOWN**: 現セッションで未実行の POSIX/Python 行、power-loss、hardware write cache、
  network/distributed filesystem、cross-volume rename の耐久性。
- **ASSUMPTION**: atomic current visibility は同じ protocol を守る cooperating writer/reader
  間に限る。malicious same-privilege writer、完全な TOCTOU 防止、authenticity は保証しない。

## 運用上の注意

dedicated root は product `runs_dir` と共有しない。P2-012A の内部 API を通常 CLI に接続しない。
reconciliation-required または effect-unknown を受けた caller は、typed inspection と同一 request
による明示的 reconciliation を行い、directory を手動削除・上書き・silent adoption しない。

## P2-010B coordinator boundary

P2-010Bはproducer `p2-010b-phase-revision` version `0.2.0`の専用rootだけを使い、対象runの
初回publish前historyが空であることを確認する。完全なphase traceとfinal-report-v2 source graphを
再検証し、artifact intentのnonnull content hashをexact admitted bytesへ一致させる。
`state.json` intentはprovenanceに残るがpayload化しない。

transition authorizationは`published`またはoriginal same-process bundleのexact
`current_committed`だけから生成する。historical replay、reconstructed bundle、release failure、
reconciliation-required、effect-unknown、current ambiguityはtransition authorityにならない。
structural revisionはterminal/product-readable stateではなく、P2-012Bのcompletion marker、
verified reader、resume、migrationを先取りしない。

## P2-011B dedicated budget-state root

P2-011Bはproducer `p2-011b-durable-budget` version `0.1.0`の専用rootだけを使う。
`budget_state.json`はartifact schema `poker-durable-budget-state-artifact-v1`、storage protocol
`poker-run-revision-v1`のstrict internal structural artifactである。flat-v1 product root、
P2-010B phase-revision rootと共有せず、既存runをadoptしない。

内部structural history readerはcurrent-to-genesis chain、manifest/payload identity、size/hash/schema、
stable current pointerを全revisionで検証してexact bytesを返す。さらにdurable layerはgeneration、
previous-state hash、append-only operation/event/settlement、permit/attempt/cancellation lineage、
monotonic policy tightening、usage非減少、typed transitionを再検証する。cross-run replay、history gap、
policy/activation substitution、semantic successor forgeryは`reconciliation_required`でfail closedになる。

このreaderは`poker_deliberation.storage` package rootからexportせず、terminal、completed、
resumable-product、migrated statusへmappingしない。P2-012Aのphysical quotaとP2-011Bのlogical
resource reservationは独立しており、どちらか厳しい側がfail closedになる。

## P2-012B product terminal V2

通常の`Orchestrator.run`はflat-v1 `runs_dir`へ書かず、`revision_runs_dir`の
`runs/<run_id>/.terminal-store/`へimmutable revisionをpublishする。既定rootは
`.poker-run-revisions`、対応するdurable budget rootは`.poker-budget-revisions`である。3 rootは
同一・祖先・子孫関係を拒否し、初回run時だけ明示ownershipで初期化する。既存rootのproducer、
legacy-root identity、schema、ownershipが一致しなければ自動adoptやrepairをしない。

product revisionはcanonical payload inventory、`RunManifestV2`、terminalの場合だけ
`CompletionMarkerV2`を持つ。payloadとmanifestをexclusive create・fsync・rereadした後、
`completion.json`をrevision-localの最後のdata artifactとして書く。marker/manifest/payloadを
再検証後にだけ、expected previous pointerを束縛した`current.json` CASを行う。CAS lossは
last-write-winsにせずunreferenced revisionとしてreconciliationを要求する。

readerはpointerから選択したrevisionについてrun/revision/transaction/path、inventory、
size/hash/schema、required payload、state/report/status、approval/context/execution/event lineage、
lifecycle audit、completion marker、RM-011 settlementを再計算し、pointerを再読してbyte identityを
確認する。返すstatusは`in_progress`、`approval_required`、`succeeded`、`failed`、`cancelled`、
`cancel_unconfirmed`、`incomplete`、`corrupt`、`unsupported_version`、`legacy_unverified`である。
public `completed`へ写像するのはverified `succeeded`だけで、resume可能なのはstableな
`in_progress`または`approval_required` checkpointだけである。

flat-v1 adapterはexact LF sentinel、portable ID/path、既知artifact、bounded canonical schemaだけを
read-onlyで検査する。integrity evidenceが存在しないため常に`legacy_unverified`である。
Windowsのtext newline変換に依存せず、legacy writerもsentinelをexact `b"v1\n"`で保存する。
copy migrationは異なるdestination run ID、明示quiescence、copy前後のsource inventory hash一致、
deterministic identityを要求し、元bytesを変更しない。copy先もcompletion markerを持たず
`legacy_unverified`かつnon-resumableである。

terminal publicationはrevision mutation前に専用RM-011 rootへremaining active runtime、1 slot、
最大artifact bytes、persistent deltaをatomic reserveし、pointer publication後にpointer/marker hashへ
exact settleする。missing、overrun、conflict、effect-unknown settlementではreaderがterminal statusを
返さない。短すぎるruntimeやartifact hard capによってfailure report自体をdurable publishできない
場合、ordinary callはsuccessを返さず固定code付き`failed_with_limitations`にdowngradeする。

`lifecycle_audit.json`はmarker前のrequired payloadである。retention anchorは同じtransactionでfreezeした
`CompletionMarkerV2.published_at`で、P2-027A pure evaluatorのbounded metadataだけを保存する。
P2-012B自身はscan、move、quarantine、delete、encryption、receipt、tombstone、secure eraseを実行しない。
後続P2-027Bはこのverified terminal currentとlifecycle auditを入力にする別protocolである。
## P2-013A approval successor

approval decision は、検証済み current revision から ledger、decision log、domain audit、V1 projection、state、JSON/Markdown report を memory 上で構築する。decision record は authority snapshot とその digest を保持し、run/ledger revision の連続鎖と最後の decision revision を successor manifest revision へ拘束する。deterministic transaction ID、expected revision、manifest hash、pointer hash を `TerminalRunStore.publish_approval_decision` に渡し、RM-012 lock 内で actor と authority provider identity/version を再検証してから manifest と current pointer を公開する。

CAS winner は 1 つだけである。loser は current を再読し、同一 idempotency key／actor／batch の完全 replay なら保存済み outcome を返す。それ以外では domain current を変更せず、別の bounded control ledger に失敗 event を追加する。

失敗監査は `.terminal-store/approval-failure-audit/` に immutable event と `current.json` pointer を持つ。event は sequence と previous hash を拘束し、最大 1024 件、1 件 16,384 bytes、run 合計 1,048,576 bytes、actor/run の rolling 60 秒で 32 failure events に制限する。自動 cleanup は P2-027B より前には行わない。

## P2-027B detached-run authority と cleanup history

quarantine の rename 後は product namespace から run が消えるため、delete 段階は
`RunRevisionStore.acquire_detached_run_authority` で同じ P2-012A stable lock authority を取得する。
このAPIは既存 ownership/control marker と run storage の完全一致を要求し、root/runを初期化、
adopt、repairしない。`inspect_run_authority_binding` は lock metadata を作らない read-only binding
を返す。detached 判定は `Path.exists()` に依存せず runs namespace を列挙し、dangling
symlink/junction/reparse と case alias を拒否する。lock 内で immutable product tree と cleanup
current を再検証してからだけ staging rename または unlink を許可する。

cleanup root は product revision root と共有せず、run hash ごとの
`runs/<run-hash>/transactions/`、`runs/<run-hash>/revisions/`、`current.json` に独立した immutable
historyを持つ。各 revision は transaction、manifest、receipt、tombstone を保持する。current
readerは pointer、transaction、payload location、plan/approval hash、state matrix、previous
manifest hashを revision 1まで再計算する。idempotency lookup は current から到達可能な revision
だけを対象とし、pointer未公開の orphan revisionを成功として採用しない。standalone journal集合は
到達可能な revision 1/2 と exact canonical bytesで一致させ、authority内で未公開の次 transaction
を読む場合だけ internal pending transactionとして明示検証する。product terminal stateは
書き換えず、direct product delete APIも公開しない。
