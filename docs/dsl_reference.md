# DSL リファレンス（op 表とその意味論）

**このファイルが DSL の op とその意味論の正本である**。起動して動かす手順は [QUICKSTART_AI.md](../QUICKSTART_AI.md)、アーキテクチャ・データモデル・各ステップの操作・通しの作例は [ai_mode_guide.md](ai_mode_guide.md) を参照。

---

ステップ3が生成する DSL は、**限定プリミティブ（op）の線形 SSA + branch** で表現されます。任意コード実行を含まず、安全なインタプリタが実行します。

## 使用可能な op 一覧

「差し込み先」は、その op のどこに `params`（後述）や組み込みパラメータの値を埋め込めるかを示します。

| op | 説明 | 差し込み先 |
|---|---|---|
| `sql_extract` | 自アプリ DB から SELECT で抽出 | 値 `:name` / 識別子 `{{name}}` |
| `fhir_extract` | FHIR サーバから取得 + `fhir_path`（**簡易ドット記法**。下記注意）で値抽出 | `fhir_query` の `{{name}}` |
| `external_db_extract` | 外部 DB（`connections.yaml` の接続）から SELECT 限定で抽出 | 値 `:name` / 識別子 `{{name}}` |
| `csv_extract` | 全患者横断 CSV を患者ID列＋複合等値 WHERE で決定的に抽出 | `path` の `{{name}}` / `match` の value の `{{name}}` と `"$id"`（**列指定は不可**） |
| `file_extract` | 患者データファイル（txt/md/csv/json/xml/pdf）から抽出。画像PDFは Vision OCR に縮退 | `path_pattern` の `{{name}}` |
| `llm_extract` | LLM で自由記述から値を抽出 | `prompt` の `{{name}}` / `$id` |
| `llm_judge` | LLM で**真偽**を判定（**分類には使えない**。応答は `yes` / `no` / `unknown` に閉じられ、真偽か「判定不能」に正規化される。判定不能は値を出さず `extraction_low_confidence` が付く。区分や値が欲しいときは `llm_extract`）| `prompt` の `{{name}}` / `$id` |
| `compare` | left cmp right → bool。`cmp` は `< <= > >= == != in contains is_empty is_not_empty`。**`is_empty` / `is_not_empty` は `right` を取らない**（左辺が空＝値なし・空文字・0件 かどうかだけを見る） | `left` / `right` の `"$id"` |
| `branch` | when/then/else_branch → **値の選択**（実行は分岐しない。実行を分けるなら `guard`） | `when` / `then` / `else_branch` の `"$id"` |
| `transform` | code_map / unit_conversion | `input` の `"$id"` |
| `select` | 複数候補（rows）から `pick`（`first` / `last`）で1件選ぶ | `source` の `"$id"` |
| `aggregate` | 複数候補（rows）を集計してスカラ化 | `source` の `"$id"` |

### `fhir_path` は FHIRPath ではない（簡易ドット記法）

`fhir_extract` の `fhir_path` という名前に反して、実装（`executor.resolve_fhir_path`）は
FHIRPath 評価エンジンではなく**独自の簡易ドット記法パーサ**です。

- **対応する記法**: `.` 区切りの辿り（`Observation.valueQuantity.value`）、
  `name[0]` 形式の整数リテラル添字（`Patient.name[0].text`）、
  先頭要素が大文字始まりならリソース型として読み飛ばす（`Observation.status` ≡ `status`）
- **対応しない記法**（書いても効かず、黙って `null` になります）:
  `where(...)` / `ofType(...)` / `first()` / `exists()` / `resolve()` などの FHIRPath 関数、
  型演算子 `as` / `is`、union `|`、`and` / `or` や `=` 等の演算子、
  式・範囲・ワイルドカード添字（`[*]`、`[$this]` 等）、コレクション意味論

絞り込みは `fhir_query` の検索パラメータ側で表現してください。
（FHIRPath 評価エンジンは未導入です）

### `select` の `where` / `order` は実行されないヒント

`select` で実行されるのは `pick`（`first` / `last`）だけです。`where` と `order` は
**人間可読の設計メモ**であり、実行時には評価されません。

- `where`: 行の絞り込みには使われず、監査証跡に `hints_ignored`（無視したヒント）として
  記録されるのみ
- `order`: 並び替えを一切行わない（`pick` は抽出時点の行順に適用される）

どちらかが書かれた `select` step は実行時に警告ログを出します。決定的な絞り込み・並び順は
抽出側（SQL の `WHERE` / `ORDER BY`、`csv_extract` の `match`、FHIR クエリの検索パラメータ）で
表現してください。

なお `pick` に `first` / `last` 以外（`latest` などの綴り違い）を書くと、黙って `first` に
なるのではなくプラン検証エラーになります。

### `csv_extract` の差し込み（`match` の value と列指定）

`match` の value には `{{name}}`（params）と `"$id"`（前段 step の値）の両方を差し込めます。
value は CSV のセル値と**そのまま等値比較されるだけ**なので、サニタイズも `allowed` も
要求しません（SQL の `:name` バインドと同じ線引きです）。

一方 **`key_column` / `column` / `match` の `column`（列指定）に `{{name}}` は書けません。**
プラン検証エラーになります。列指定は「どの列を読むか」を決めるスロットで、`header=false`
では整数 index を要求する規則とも両立しないためです。

以前は `match` の value に書いた `{{name}}` が展開されず、リテラル文字列 `"{{name}}"` として
CSV と比較されていました。一致しないので結果は例外ではなく「該当なし」になり、
**パラメータ化が効いていないことに気づけませんでした**。現在はどちらの記法も
実行時に解決され、未宣言の名前はプラン検証で弾かれます。

### 患者キーの解決（`key_type` / `physical_format`）

データ取得系の op（`external_db_extract` / `fhir_extract` / `file_extract` / `csv_extract` /
`sql_extract`）は、step に `key_type` を宣言すると、実行前に resolver が登録番号から
その名前空間の患者キーへ一括解決し、`physical_format` で格納形式へ整形した値をバインドします。

```json
{
  "op": "external_db_extract",
  "id": "v",
  "connection_id": "dwh",
  "sql": "SELECT name FROM patient_infomation WHERE CAST(patient_id AS TEXT) = :patient_id",
  "key_type": "mrn",
  "physical_format": { "pad": 10 }
}
```

- `key_type`: `key_types.yaml` のカタログ ID（例 `mrn` / `fhir_patient_id`）
- `physical_format`: 列ごとの格納形式。`pad`（総桁数・左詰め） / `padchar`（既定 `0`） /
  `prefix` / `suffix`
- 未宣言なら登録番号をそのままバインドします（後方互換）。詳細は
  [docs/key_resolution_guide.md](key_resolution_guide.md)

**この2つはコンパイラが焼き込みます**（LLM に書かせません）。

| op | `key_type` の出どころ | `physical_format` の出どころ |
|---|---|---|
| `external_db_extract` / `sql_extract` | step の典拠の索引単位の `payload.key_type` | 接続の `key_formats.<key_type>` |
| `file_extract` / `fhir_extract` | 索引単位の `payload.key_type`、無ければ接続の既定 `key_type` | 同上 |
| `csv_extract` | 接続の既定 `key_type` | 同上 |

- LLM が**違う値**を書いていたら、宣言の値に置き換えたうえで `needs_input: true` にします
  （所在の取り違えの徴候でありうるので、黙って上書きしません）。同じ値・未記入なら何もしません。
- 索引にも接続にも宣言が無ければ、LLM が書いた `key_type` がキー種別カタログ（`key_types.yaml`）に
  あるときだけ残します。カタログに無い名前（`patient_id` / `value` 等）は外して `needs_input` にします
  （実行時に resolver が引けず項目ごと飛ぶため）。カタログが未配置なら外しません。
- DB の step に接続の既定 `key_type` は使いません（患者で絞らない表に要らない解決を走らせないため）。
- 「患者で絞らない表」（`payload.patient_scoped=false`）の step には付けず、LLM が書いた指定は外して
  `needs_input` にします。`patient_scoped=false` と `key_type` を両方持つ単位は宣言の矛盾として人へ回します。
- 自アプリDBの `sql_extract` で anchor（`KEY_RESOLVER_ANCHOR`、既定 `trial_subject_no`）を指すものは
  `key_type` を付けず、登録番号をそのままバインドします。
- 焼き込むのはコンパイル時だけです。**保存済みのプランは再コンパイルするまで変わりません。**

## 共通フィールド（全 op）

| フィールド | 説明 |
|---|---|
| `id` | step の一意名。後続 step は `"$id"` で結果を参照する（前方参照のみ） |
| `needs_input` | `true` なら情報不足で人間による補完が必要（実行時はスキップ） |
| `reasoning` | 設計時の判断理由。実行トレースにも残り監査で参照できる |
| `guard` | **条件付き実行**。真のときだけこの step を実行する（`"$id"` 参照か `true`/`false`） |

## 条件付き実行（`guard`）

`guard` が偽の step は**実行されない**。当該 step の外部 DB / FHIR / ファイルへのアクセスも
LLM 呼び出しも行わず、トレースに `"skipped": true` として記録される。スキップは失敗ではないため
その step に `failure_code` は付かない。

ただし**項目単位の失敗理由には影響する**。**判定 op（`compare` / `llm_judge`）が
実行され、その判定を guard が指した結果として取得系 step が全てスキップされた項目は
`not_applicable`**（患者の文脈上そもそも該当しない）に分類され、**取得困難度の自動充足率の
分母から除かれる**。判定を経ないスキップ（`guard: false` のリテラル、判定 op 以外を指す guard）は
`unclassified` に残る。定義は `kawasemi/ai_input/metrics.py`。

書き方の規則:

- `guard` に書けるのは `"$id"` 参照か `true` / `false` のみ。`"is_inpatient"` のように `$` を
  落とした文字列は**検証エラー**になる（黙って常に真＝無条件実行になるのを防ぐため）。
- 自分自身を参照する `guard`（`{"id":"x","guard":"$x"}`）も検証エラー。
- 参照先が空値（`null` / 空文字 / 空リスト）なら偽。0 件の `multi` 抽出結果を `guard` に
  使えば、後続の抽出・LLM 呼び出しは実行されない。
- **`guard` 付き step の結果を使う後続 step にも同じ `guard` を付ける**。付けないと未実行で
  空になった値のまま後続が実行される。

`guard` と `branch` は役割が異なる。

- `guard` = **実行するか否か**（取得の前段で外部アクセスそのものを抑止する）
- `branch` = **計算済みの値のどちらを採るか**（決定的な後処理）

取得元が患者の文脈で変わる項目（入院なら病院DB、外来なら FHIR など）を `branch` だけで書くと、
両方の抽出が実行され不要な患者データ取得が起きる。判定 → `guard` 付き取得 → `branch` の順に並べる。

```json
{
  "steps": [
    { "op": "llm_judge", "id": "is_inpatient", "prompt": "患者 {{patient_id}} は入院中か" },
    { "op": "external_db_extract", "id": "from_hosp", "guard": "$is_inpatient",
      "connection_id": "hospital", "sql": "SELECT ..." },
    { "op": "compare", "id": "is_outpatient", "left": "$is_inpatient", "cmp": "==", "right": false },
    { "op": "fhir_extract", "id": "from_fhir", "guard": "$is_outpatient",
      "fhir_query": "/Observation?patient={{patient_id}}" },
    { "op": "branch", "id": "value", "when": "$is_inpatient",
      "then": "$from_hosp", "else_branch": "$from_fhir" }
  ],
  "return": "$value"
}
```

取得フロー図では、`guard` の依存は点線（`guard`）、実行されなかった step は破線グレーの
「⏭ 未実行」ノードとして描かれる。

## LLM op の入力が使えないとき

`llm_extract` / `llm_judge` は、プロンプトが参照する前段 step（`$id` / `{{id}}`、値が `"$id"` の
`params`）が**全て**「失敗した（`failure_code` が付いた）」か「空（null・空文字・空白のみ・空リスト）」
なら、**LLM を呼ばずに失敗する**。`failure_code` は前段のものを伝播し（前段にコードが無ければ
`not_found_for_patient`）、項目は値なしで失敗コード付きになる。空の本文を渡された LLM は
「不明（本文を貼っていただければ…）」のような断り文を返し、それが値として入ってしまうためである。

- 前段を参照しないプロンプト（`{{patient_id}}` だけ等）は従来どおり呼ぶ
- 入力の一部でも使えれば呼ぶ
- 「DB が空 → 本文を読む」のフォールバックは、本文の取得に成功していれば従来どおり LLM を呼ぶ

また、プランの最終値は**項目の回答型**（Questionnaire の `item.type`）へ正規化してから prefill する
（`2025/8/5` → `2025-08-05`、選択肢の表示名 → コード、`"はい"` → `True`）。正規化できない値は
prefill しない（`answer_type_mismatch`）。型ごとに受ける値と限界は
[autofill 実行仕様](./autofill_execution_spec.md)「取れなかったことを値にしない」。

## フォールバック（所在候補が複数あるとき）

上の例は**文脈で取得元が決まる**場合である。これに対し「まず第1候補から取り、**空だったら**
第2候補」というフォールバックは、取ってみるまでどちらになるか分からない。データ所在に
優先順の候補が複数あるときはこの形にする。

空振りの判定には `compare` の **`is_not_empty`**（`right` を取らない単項演算子）を使う。
空とは「値なし・空文字・0件」で、これは `guard` の評価と同じ規則である。`== null` や
`!= ""` では代用できない — 前者は `right: null` を必須フィールド検査が弾き、後者は
`right: ""` を同じ検査が弾く（`right` を省略すると「必須の right が未設定」になる）。仮に
通したとしても `_loose_eq` は `str()` 比較に落ちるので、**0件の multi 抽出（`[]`）を
「値あり」と誤判定してフォールバックが発火しない**。

```json
{
  "steps": [
    { "op": "fhir_extract", "id": "a",
      "fhir_query": "/Observation?patient={{patient_id}}&code=1234-5" },
    { "op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty" },
    { "op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": false },
    { "op": "sql_extract", "id": "b", "guard": "$no_a",
      "sql": "SELECT v FROM t WHERE patient_id = :patient_id" },
    { "op": "branch", "id": "value", "when": "$has_a", "then": "$a", "else_branch": "$b" }
  ],
  "return": "$value"
}
```

`guard` を付けるのが要点である。付けないと第1候補が成功しても第2候補の患者データ取得が走る。

`branch` の `when` には**空判定の結果**（`$has_a`）を置く。取得結果そのもの（`$a`）を置くと、
`branch` の真偽化は `guard` と規則が違うため 0件（`[]`）が真・文字列 `"0"` が偽になり、
取り直した値が捨てられる。`is_empty` を使う場合は `then` と `else_branch` が入れ替わる
（`when: "$empty_a", then: "$b", else_branch: "$a"`）。第2候補の空判定を見る書き方
（`when: "$has_b", then: "$b", else_branch: "$a"`）も正しい。

**候補が3つ以上あるときは、2段目以降の判定 step にも同じ `guard` を付ける。**

```json
{ "op": "sql_extract", "id": "b", "guard": "$no_a", "sql": "..." },
{ "op": "compare", "id": "has_b", "guard": "$no_a", "left": "$b", "cmp": "is_not_empty" },
{ "op": "compare", "id": "no_b", "guard": "$no_a", "left": "$has_b", "cmp": "==", "right": false },
{ "op": "file_extract", "id": "c", "guard": "$no_b", "connection_id": "docs",
  "path_pattern": "{{patient_id}}/report.txt" },
{ "op": "branch", "id": "v2", "when": "$has_b", "then": "$b", "else_branch": "$c" },
{ "op": "branch", "id": "value", "when": "$has_a", "then": "$a", "else_branch": "$v2" }
```

`has_b` / `no_b` の `guard` を付け忘れると、skip された `b` の値は `null` なので `has_b` が
「空だった」と出て `no_b` が真になり、**第1候補が成功しているのに第3候補の患者データ取得が
走る**。`branch` も入れ子にする。

全候補が空振りしたときの根本原因は、**最後に失敗した候補**の失敗として集計される。先頭の
失敗は設計どおりの空振りなので原因に採らない。同じ `guard` を共有する step 列は1つの候補の
試行とみなし、その中では最初の失敗（取得の失敗）を採る（`kawasemi/ai_input/metrics.py`）。

各候補の取得 step には、**その候補の確定済み op 引数**を書く。第1候補の引数を第2候補の
step にも書くと「A を試して、空なら A をもう一度試す」計画になるため、コンパイル時に
その step は `needs_input=true` にされ、典拠（`unit_id`）も刻まれない。

## プログラム構造

```json
{
  "steps": [
    {
      "op": "sql_extract",
      "id": "pathology_text",
      "sql": "SELECT observation FROM pathology_report WHERE patient_id = :patient_id ORDER BY examination_date DESC LIMIT 1"
    },
    {
      "op": "llm_extract",
      "id": "result",
      "input": "$pathology_text",
      "prompt": "以下の病理報告書から、がんの組織型のみを簡潔に抽出してください。\n組織型名のみを回答してください。\n\n報告書:\n$前段id"
    }
  ],
  "return": "$result"
}
```

各 step は `id` を持ち、後続の step が `"$id"` で参照します。  
`needs_input: true` が付いた step は情報不足のため人間による補完が必要です。  
`guard` が付いた step は条件が真のときだけ実行され、偽なら未実行（`skipped`）として記録されます。

## プログラム引数（`params`）

患者ID 以外の値を SQL・FHIR クエリ・ファイルパスへ差し込むための宣言です。`value` に `"$前段id"` を書くと **前段 step の取得結果を後段の引数にできる** ため、「まず区分値を取得し、その値で参照テーブルや FHIR の `code=` を組み立てる」という段階的な取得が書けます。

```json
{
  "params": [
    { "name": "ref_tbl", "kind": "identifier", "value": "$kubun", "allowed": ["labs_a", "labs_b"] },
    { "name": "threshold", "value": 8.0 }
  ],
  "steps": [
    { "op": "sql_extract", "id": "kubun", "sql": "SELECT tbl FROM ref_map WHERE patient_id = :patient_id" },
    { "op": "sql_extract", "id": "v",
      "sql": "SELECT v FROM {{ref_tbl}} WHERE patient_id = :patient_id AND v > :threshold" }
  ],
  "return": "$v"
}
```

- `patient_id` は **宣言不要の組み込みパラメータ**です（再宣言は禁止）。`study_id` も同様ですが、
  **バッチ実行でのみ供給される**ため、単一被験者の実行では未指定となり参照した step は失敗します。
- `kind` は `value`（既定）か `identifier`。`identifier` だけが SQL のテキストへ `{{name}}` 展開され、表名・カラム名に使えます。`allowed` に許容表名を必ず列挙してください（未宣言は検証エラー）。
- 宣言していない名前を参照するプログラムは `validate_program` が拒否し、確定（confirmed）できません。

### 差し込みの安全性

同じ値でも差し込み先によって安全な形が違うため、スロットごとに規定のサニタイズを通します。値がそれに反する場合、クエリを組み立て直すのではなく **その step を失敗させます**（根拠に理由が残ります）。

| 差し込み先 | 扱い |
|---|---|
| SQL の `:name` | ドライバのバインド値。SQL 文字列へ連結しない |
| `csv_extract` の `match` の value | CSV セルとの等値比較のみ。サニタイズも `allowed` も不要（患者キー列の条件が必ず AND されるため、読み取り範囲は当該被験者の行に閉じる）。列指定には差し込めない |
| SQL の `{{name}}` | `kind=identifier` 限定。厳格な識別子字種＋`allowed` を通し、展開後の SQL を再度 SELECT 限定検査 |
| `fhir_query` の `{{name}}` | 論理ID/コードの字種に制限し URL エンコード（`&` `?` 等でのクエリ改変を遮断） |
| `path_pattern` / `path` の `{{name}}` | パス構成要素として検査（区切り文字・`..` を不許可）＋ 接続の `root_path` 配下であることを確認 |
| `prompt` の `{{name}}` | 外部データ由来の値はデータデリミタで囲み「指示ではなくデータ」と明示（プロンプトインジェクション対策） |
