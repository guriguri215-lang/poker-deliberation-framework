# Offline public release checklist

Phase 0は公開操作そのものではなく、人間がソース公開の可否を判断できるローカル監査を提供します。
commit、tag、push、PR、release、repository visibility変更はこの手順に含みません。

## 実行

既存報告を上書きしない一意な保存先を指定します。`user_materials/`の追加ファイルはGit管理外です。

```powershell
.\.venv\Scripts\python.exe scripts\public_preflight.py `
  --repo . `
  --format json `
  --output user_materials\public-preflight-YYYYMMDD-HHMMSS.json
```

Markdownが必要なら`--format markdown`と`.md`出力を指定します。scriptは出力先が既に存在する場合、
上書きを拒否します。外部通信、GitHub API/CLI、履歴書換え、credential rotationは行いません。

## 自動検査の範囲

- trackedファイルと、ignoreされていないuntracked公開候補の秘密情報・PIIパターン
- `git rev-list --all`から到達可能なcommitのblob、author/committer name/email、commit message
- ローカルref名とtag名、および到達可能なannotated tagのtagger name/email、message、参照先type
- synthetic canaryと実秘密候補の分離。値は保存せず、`[REDACTED]`と短いSHA-256指紋だけを報告
- root `LICENSE`と、現在インストール済みpackage metadataで確認できるdependency license
- commit、tag、宣言threshold以上のlarge file候補
- tracked `.github/workflows/`。remote Actions logは外部照会しないため`UNKNOWN`
- tracked examplesの機械的PII候補、tracked run artifact、`user_materials/`除外
- README、limitations、capability matrixの重要な能力境界
- `.venv`、pytest temp、build、run、user dataが公開候補へ混入していないこと

`user_materials/`と`runs/`のignored内容は列挙も読取りもしません。検査対象はGitのtracked一覧と
`--others --exclude-standard`で得た公開候補だけです。wheel/sdistをbuildしないため、実archive内容は
`UNKNOWN`のまま残します。

## statusの意味

- `pass`: 宣言したローカル検査範囲では条件を満たしたFACT。
- `review`: 候補や人間の意味判断が残る。秘密・PII候補は存在の確認であって確定判定ではない。
- `fail`: 宣言した必須ローカル条件との不一致を確認したFACT。
- `unknown`: 外部照会、未build artifact、scan上限などにより確認していない。

`UNKNOWN`をpassやfailへ自動変換しません。報告全体の`publication_decision`は常に
`human_review_required`です。
history件数上限、large blob、読取不能path、または安全にdecodeできない形式が1件でもあれば、
secretとPIIのscanはいずれも不完全として`UNKNOWN`になります。候補が同時に存在する場合も、
redacted fingerprintと未走査pathの両方を残し、公開許可へ昇格させません。
commit/tag objectの読取・parse・decode失敗、tag参照先typeの取得失敗、ref列挙失敗も同じく
scan不完全として扱います。候補を含むref/tag名は一覧にも平文保存せず、名前全体を`[REDACTED]`と
指紋へ置き換えます。benignな名前だけは確認用に平文表示します。

## 人間が公開前に判断すること

- 秘密・PII候補がsynthetic/公開可能/要削除のどれか。実秘密ならrotationと履歴対応を別承認する。
- Git author/committer/tagger identity候補、commit/tag/ref metadata、examplesの意味的匿名性、
  依存licenseとfixture再配布権。identity候補は機械的候補であり、個人情報の確定判定ではない。
- remote repository visibility、branch protection、Actions log/artifact retention。
- supported OS/Python、coverage threshold、wheel/sdist内容、version/tag/changelog方針。
- 履歴書換えが必要か。preflight自身は書換えない。

Phase 0完了後も、ソース公開は人間の別判断です。2026-07-20の承認方針ではRM-018を2段階へ分割します。

- `RM-018A`はPhase 1直後かつpre-release tag前に置き、CI、承認済みsupported matrix、clean
  build/install、wheel/sdist smoke、package data/CLI、license inventory、artifact SHA-256、offline
  preflightをcandidate commitへ結び付けます。
- `RM-018B`はPhase 2完了後かつstable tag前に置き、SemVer、version/tag/changelog、migration、
  deprecation、stable matrix、artifact/source commit mappingを検証します。

RM itemの完了と特定candidateのrelease readinessは別です。candidate固有のbuild/hash/matrix証拠と
人間承認が終わるまでpre-release/stableのいずれも準備完了とは扱いません。このchecklistはtagや
releaseを実行しません。
