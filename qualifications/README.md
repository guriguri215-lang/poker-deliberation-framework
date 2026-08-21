# Public qualification artifacts

このdirectoryには、repository-owned public synthetic fixtureを用いた、秘密を含まないqualification evidenceを置きます。

P3-030Gは、P3-030D/Fの実際のproduction preview、17-field confirm、single-role execute wrapperを
固定5 roleの順で通すfirst-class deterministic production-workflow qualification harnessです。role実行には
評価専用のdeterministic read-only executor seamを使い、外部model、provider、network、credentialを
使用しません。`SanitizedBoundedRiverReviewWorkflowQualificationManifestV1`はschema `1.0.0`の
self-hashed canonical manifestであり、`qualification_status="passed"`の場合も
`transport_qualification="deterministic_fixture"`、`live_qualification_status="UNKNOWN"`、
`api_live_executed=false`、`api_production_qualified=false`を保持します。

このmanifestはsafe code、hash、count、固定metric、runtime inventoryだけへ限定し、raw source、prompt/
outbound bytes、credential値、narrative、reasoning/model trace、`user_materials/`を含めません。
deterministic fixtureの合格はactual-live/provider qualificationではありません。live qualificationは別手順として
固定5 roleそれぞれのfresh previewと人間による明示確認を必要とします。

`scripts/run_bounded_river_review_workflow_evaluation.py`はV2 harnessを既定で実行します。self-hashed
evaluation resultは必須`--output`へ保存し、全case/metric合格時だけ、任意の`--manifest-output`へ
sanitized manifestをexclusive-createします。このdirectoryにP3-030G manifestが存在することや
deterministic合格を、current live evidenceとして扱いません。

`--work-root`はrepository内のGit-ignoredかつuntrackedな、まだ存在しないpathに限定します。`--output`と
`--manifest-output`はrepository外、またはrepository内ならGit-ignoredかつuntrackedな、まだ存在しないpathを
受理します。3 pathの一致・親子関係と、tracked/unignored pathやfixture/source/range pathとの一致・親子関係を
拒否します。この検査はwriteより前に完了し、拒否時にcanonical failure artifactは生成しません。このgoalの
正規pathは`tmp/codex-goals/p3-030g/`配下です。

- `historical/3b8772a587f270acccee32e33f3df68187dda418/`: そのcandidate commit/treeへ束縛された
  strict canonical V2 sealed live manifestとno-network exact-evidence評価結果です。bytesは変更せず、
  historical evidenceとして保存します。
- current qualificationの唯一の正は、current canonical pathのstrict canonical V2
  `p2-025b-codex-subscription-v1.json`と、それへ束縛された
  `p2-025b-deterministic-evaluation-v1.json`のpairに対するpublic preflight結果です。両方欠落は
  `UNKNOWN`、片方だけの欠落、noncanonical、invalid、untrackedまたはcurrent-tree binding不一致は`FAIL`、
  pairが揃い全binding checkに合格した場合だけ`subscription_live_qualified=true`です。historical manifestや
  文書上の状態記述をcurrent authorityにしません。
- raw CLI JSONL、raw model trace、reasoning trace、認証cache、token、API key、private hand historyは公開しません。
- current manifestはqualification実行commit/tree、runtime source inventory、role conformanceを固定し、
  current treeに対してpublic preflightで再検証します。historical evidenceの再hashでは置き換えません。
- `openai_api`はこのmilestoneではlive qualificationを行わず、production-qualifiedとは表現しません。
