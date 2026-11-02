# 成果物契約（CONTRACT）

kawasemi-indexer（ビルド側）と KAWASEMI 本体（クエリ側／WebUI）の **唯一の接点**は、
このファイルに定義する成果物ファイルである。両者は同じリポジトリ（`indexer/` と `kawasemi/`）に
置くが、別ネットワークで動き、実行時にコードを import で共有しない。本契約に従う限り、
各側はコード（`embedder` / `schema`）を独立に複製（vendoring）してよい。

## 1. 成果物ファイル

indexer が出力ディレクトリ（既定 `var/resource_index/`）に生成し、WebUI が読む:

| ファイル | 内容 |
|---|---|
| `column_index.jsonl` | 公式契約。1 行 = 1 カラムのフラットレコード（下記スキーマ） |
| `value_dictionary.jsonl` | `(connection_id, db_qualified, physical_name)` をキーに code→label |
| `db_index.jsonl` | 索引ユニット（kind=db。payload を含む検索対象レコード） |
| `file_index.jsonl` | 索引ユニット（kind=file。規約文書のチャンクと患者データファイルの所在宣言。任意） |
| `fhir_index.jsonl` | 索引ユニット（kind=fhir。リソース種別ごとの取得テンプレート。任意） |
| `master_index.jsonl` | 索引ユニット（kind=master。マスタ 1エントリ=1ユニット。任意） |
| `embeddings.npy` | 索引ユニットの埋め込み行列（float32, l2 正規化済み） |
| `build_info.json` | 埋め込みモデル名・次元・`schema_version` 等のメタ |
| `synonym_index.jsonl` | 標準コード変換テーブル（クエリ書き換え用。任意・**embedding 非依存**） |
| `ref_links.jsonl` | 参照リンク（列→値域マスタ。RRF 第3チャネル用。任意・**embedding 非依存**） |
| `column_fill.jsonl` | 列の充填状況（**測定側が作る任意成果物**。所在候補の足切りに使う） |

> 索引ユニットの kind と行連結順（`embeddings.npy`）は `db, file, fhir, master` の順で固定。
> 新 kind は末尾に追加し既存オフセットを保つ。`schema_version=2` で `master` を追加。

### synonym_index.jsonl のレコード（標準コード変換テーブル）

`master_index.jsonl` を再編成した **クエリ側の任意成果物**（`kawasemi-indexer build-synonyms`
で生成）。1 行 = マスタ 1 エントリ。クエリが `names`/`codes` の語に **完全一致** したら、
同一エントリの別名・標準コードでクエリを一段展開する（製品名⇔一般名、標準コード⇔名称群）。

```json
{"unit_id": "master:dwh:DEMO.DRUG_MASTER:10001", "label": "架空薬甲錠 15mg",
 "connection_id": "dwh", "db_qualified": "DEMO.DRUG_MASTER", "internal_code": "10001",
 "names": ["架空薬甲錠 15mg", "架空成分甲塩酸塩"],
 "codes": ["9999999000001", "9999999X9999", "999999999"]}
```

- `names`（名称類）と `codes`（**標準**コード類: HOT/YJ/レセ電/ICD10/JLAC10。`aliases` 由来）
  のみを横断展開に使う。`internal_code`（院内オーダ/病名コード＝`payload.code`）は接続ローカルで
  衝突しうるため展開キーにしない（表示・特定用）。
- **embedding 契約に非依存**なため `schema_version` は上げない（v2 のまま）。存在すればクエリ側が
  使う任意成果物で、無ければクエリ書き換えを行わないだけ（機能縮退）。再生成は `build` 後に単独実行可。

### column_fill.jsonl のレコード（列の充填状況）

**この成果物だけは indexer ではなく測定側が作る**（施設の DB で列の充填状況を数える
スクリプト）。施設の DB を実測した結果で、索引の生成物ではない。
1 行 = 1 列で、鍵は索引の payload と同じ `(connection_id, db_qualified, physical_name)` である。

```json
{"connection_id": "dwh", "db_qualified": "DEMO.DEPT_VISIT", "physical_name": "DEPT_CD",
 "exists": true, "total_count": 100, "filled_count": 100, "filled_rate": 100.0,
 "measured_at": "2026-01-01"}
```

- `exists`: 実行先の DB にその列が在るか。**在るが空（`filled_count=0`）と、そもそも無い
  （`exists=false`）を区別する。** どちらも所在の候補から外すが、理由が違う
- `filled_count`: NULL でも空白だけでもない行数。`filled_rate` はその百分率
- **落とすのは0件のときだけである。** 充填率が低いことは「使えない」の証拠にならない
  （測ったのは表全体の充填率で、欲しいのは対象集団での充填率である）。閾値は置かない
- **embedding 契約に非依存**なため `schema_version` は上げない。無ければ足切りをしない
  だけで、索引の再構築は要らない（機能縮退）

#### 文書種別ユニットの到達可能性

鍵が `unit_id` の行を同じファイルへ足してよい（文書種別ユニット `db:…#<種別コード>` の到達可能性）。
列の行（鍵が `connection_id` / `db_qualified` / `physical_name`）とは、`unit_id` を持つかで区別する。

```json
{"unit_id": "db:APP.DOC_BODY.BODY_TEXT#X001", "has_header": true, "has_body": false,
 "header_count": 1200, "body_count": null, "measured_at": "2026-01-01"}
```

- `has_header`: 文書ヘッダ表（`access_path` の `via` の表）に、種別の列がその種別で有効行の条件を
  満たす行が**1件以上ある**か。**`false` は「定義はあるが複製された文書が無い」**
- `has_body`: そのうち本文の表とヘッダの結合列で結べて本文が空でない行が**1件以上ある**か。
  **`has_header` が `true` で `has_body` が `false` は「所在は特定できるが現在の取得経路では
  引けない」**（本文が別システムにあり、実行先の DB へ連携されていない場合など）
- `header_count` / `body_count`: 件数。**度数が閾値（10）未満のセルは実数を出さず `null`**
  （小さいセルの秘匿。`has_*` は件数に依らず常に付く。0 件は `has_*=false`、1〜9 件は
  `has_*=true` かつ件数 `null`）
- クエリ側が**落とすのは `has_header=false` のユニットだけ**である。本文が無くても落とさない
  ——所在の特定と到達可能性は別の問題で、後者は施設の意思決定に属する。
  測っていないユニットは通す
- 測定は施設の DB を実測する側が行う（`column_fill` と同じ）
- **embedding 契約に非依存**。`schema_version` は上げず、再構築も要らない

### ref_links.jsonl のレコード（参照リンク: 列→値域マスタ）

列とその値域マスタ表のリンク（`kawasemi-indexer draft-ref-links` で下書き生成）。
クエリ時に二段展開する: クエリの臨床概念からマスタ表を特定（書き換えの展開元 or
上位 kind=master ヒット）し、その表を参照する列を RRF 第3チャネルでブーストする。
シグナルは**メタデータのみ**（列名・論理名・メモ）。患者データの値プロファイリングは行わない。

```json
{"column_unit_id": "db:DEMO.CLAIM.DX_CODE", "master_connection_id": "ehr",
 "master_db_qualified": "MASTER.DISEASE", "status": "rule", "role": "code",
 "source": "rule:byomei_code", "note": null}
```

- `status`: `confirmed`（人手確認済み）> `rule`（ルール推定）> `inferred`（LLM 推論）。
  この順がチャネル内順位になる。下書きは rule/inferred で出力され、**人が status を
  confirmed に書き換えて確定**する（既存 drafter 思想）。確認前リンクも使うが、ヒットには
  `ref_status` が付き、debug/WebUI は `⚑ref:rule` / `⚑ref:inferred` と明示する。
- `role`: 列に入るのがマスタのコード（`code`）か名称文字列（`name`）か（WHERE 組み立て用）。
- リンク先のマスタ表（`master_connection_id` / `master_db_qualified`）は施設の
  `indexer_rules.yaml` の `ref_link_masters` から来る。無ければ下書きは空になる。
- **embedding 契約に非依存**なため `schema_version` は上げない。無ければ第3チャネルが
  不発になるだけ（機能縮退）。再生成は `build` 後に単独実行可。

#### FK 逆引き（マスタ → 参照する患者表FK列）

同じ `ref_links.jsonl` を逆向きにも引く。列→マスタ（上記の第3チャネル）に加え、
**マスタ表 → それを参照する患者表の FK 列一覧**を返す（`RefLinkIndex.referencing_columns`）。
別成果物は作らず、`ref_links.jsonl` を読み込み時に転置したビューで提供する（冗長な逆引き
ファイルを持たない）。検索結果に含まれる master hit は、この逆引きで得た FK 列
`[(column_unit_id, role, status)]` で注釈される（`SearchOutcome.ref_columns_by_master`、
`search` CLI 表示）。「マスタ表の場所＋code まで」に加え、利用側が
`WHERE <code列>='...'` を組む対象列（role=code 優先）を示す
（`master_ingestion_agreement.md` の v2 ギャップを解消）。反捏造原則は不変
（候補列の提示までで、JOIN/SQL 本文の組み立ては利用側）。

### 索引ユニットのレコード（`*_index.jsonl` 共通）

`db` / `file` / `fhir` / `master` のいずれも、1 行が 1 ユニットで形は共通である。
検索は `search_text` の埋め込みで行い、`payload` は**所在を決定的に導出するための材料**
（クエリ側は payload からしか接続先・表・列・パスを組まない＝反捏造）。

```json
{"unit_id": "db:DEMO.LAB.BLAST_PCT", "kind": "db", "label": "芽球割合",
 "search_text": "検査結果 BLAST_PCT 芽球割合 … DEMO_DB",
 "snippet": "DB=DEMO_DB 表=DEMO.LAB （検査結果） 型=NUMBER(5,2) メモ=…",
 "payload": { ... kind 別（下記） ... }}
```

`unit_id` は kind ごとに接頭辞を持つ（`db:` / `file:` / `fhir:` / `master:`）。
**クエリ側は `unit_id` を解析して意味を取り出してはならない**。判定は `kind` と `payload` で行う。

#### kind=db の payload

| キー | 内容 |
|---|---|
| `connection_id` | 接続 id |
| `db_qualified` | スキーマ修飾表名 |
| `table_logical` | 表の論理名 |
| `physical_name` / `logical_name` | 列の物理名・論理名 |
| `type_raw` | 定義書どおりの型文字列（`CHAR(8)` 等） |
| `pk` | 主キーか |
| `patient_key` / `key_type` | 患者キー列か、そのキー種別（`key_types.yaml` の id） |
| `patient_scoped` | **患者で絞らない表**（マスタ参照・施設固定値）の宣言。**`false` のときだけキーが在る**（無ければ患者で絞る）。人（施設）が確認した知識層（`patient_unscoped_tables`）からだけ出し、表論理名の「マスタ」等からは推測しない（患者データの表でキーの推定に失敗したとき、全患者の行を返す SQL ができるため）。宣言された表は `patient_key` を持たない。クエリ側は宣言があるときだけ WHERE 句の無い SELECT を組む |
| `notes` | 定義書のメモ |
| `inferred` | 論理名等を LLM 推論で補完したか（人手確認済みなら `false`） |
| `physical` | **実行用の識別子** `{"schema", "table", "column"}`。`db_qualified` は定義書（元システム側）の名前で、実行先DBの名前とは限らない（定義書の `APP.ORDERS` が実DBでは `ehr.ORDERS`）ので、実行に使う名前を別に持つ。**3つが揃うときだけキーが在る。** クエリ側は外部DBの単位について、揃っていなければ SQL を組まず不足として人へ回す。付け方は2つ: `ingest` が reflection した表に付ける（スキーマは知識層の `physical_schemas`、無ければ reflection したスキーマ。SQLite は `main`）か、既存の索引へ `enrich-physical` で追記する（実行先DBの表一覧と `physical_schemas` を突き合わせる）。`physical_schemas` が複数のときは、reflection したスキーマが候補に在ればそれ、無ければ実行先の表一覧で一意に決まる表だけに付け、**推測では付けない**（候補の表を1つも列挙できなければ ingest は止まる）。**文書種別の単位（下記）は `access_path` が無ければ付けない**。識別子は**引用符なし**で SQL に埋まるので、大文字小文字を区別して作られた名前（PostgreSQL の `"MyTable"` 等）は引けない（そういう接続は対象外）。付加的なキーなので `schema_version` は上げない |

**項目コードで行を選ぶ単位**（`kawasemi_indexer.item_units`）: 経過表など縦持ちの表は、
何の値かが列名ではなく同じ表の項目コード列の値で決まる。1項目を `db:<表>.<列>#<項目コード>` の1単位にし、
列の単位の payload に次を足す。KAWASEMI は `item_code` があれば所在の文字列に項目と絞り込みを書き、
DSL の照合で項目コードと `item_row_filters` の列名を必須トークンにする。
（DSL とその照合の規則は [docs/dsl_specification.md](docs/dsl_specification.md) §9.3。）

| キー | 内容 |
|---|---|
| `item_code` / `item_name` | 項目コードと名称（項目マスタから） |
| `item_source` | 項目コードの出どころ（項目マスタ。`<接続>.<表>`） |
| `item_filter_column` | 項目で絞る列（施設の項目コード列） |
| `item_row_filters` | 有効な行の条件（`[{"column": "IS_DELETED", "equals": "0"}, …]`）。**施設の定義で確かめるまで未確定として扱う** |
| `item_measurements` / `item_units` | 測定名と単位（測定マスタから）。1項目に測定が複数ある項目は単位にしない |
| `item_patients` / `item_numeric_share` | 患者数（測定ごとの最大＝下限値）と、数値として読める行の割合 |
| `access_path` | `filters` に項目コード列と有効行の条件（`via` なし＝同じ表）、`order_by` に時刻の列 |

**文書種別で本文を絞る単位**: 文書の本文の表が種別の列を持たない構成のための単位。
文書種別1件を `db:<本文の表>.<本文の列>#<種別コード>` の1単位にし、列の単位の payload に次を足す。
**経路が無いまま実行すると、その患者の全文書（削除済みを含む）を返す SQL になる**ので、`access_path` の
無い文書種別の単位には `physical` を付けず、クエリ側も組み立てない。

| キー | 内容 |
|---|---|
| `doc_type_code` / `doc_type_name` | 文書種別のコードと名称 |
| `doc_filter_table` / `doc_filter_column` | 種別で絞るヘッダ表（実行先の名前）と種別の列 |
| `access_path` | `filters` にヘッダ表（`via`: `schema` / `table` / 結合列 `on`）の種別の列と有効行の条件、`order_by` に既定の並び順。知識層の `doc_header` から作る |
| `patient_key` / `key_type` | 本文の表の患者キー列とキー種別。知識層の `doc_header.body_patient_key` / `body_key_type` から付ける（`key_type` は宣言したときだけ）。宣言が無ければ付けず、クエリ側は患者キー列の欠落として人へ回す |

#### kind=file の payload

**`file_role` で2種類に分かれ、索引される中身が違う。**

| `file_role` | 索引されるもの | payload |
|---|---|---|
| `reference` | **本文**。規約文書・入力マニュアル等をチャンク化して `search_text` に載せる | `connection_id`, `file_role`, `path`, `page`, `char_range`, `mime` |
| `patient_data` | **所在の宣言だけ**。中身は索引時に読まない | `connection_id`, `file_role`, `path_pattern`, `extract`（`{"method": ...}`）, `mime` |

> **`reference` は「意味の典拠」であって「データの所在」ではない。**
> クエリ側が所在（データがどこにあるか）を探すときは `file_role=reference` を**除外する**
> 責務を負う。除外しないと、項目名をそのまま含むマニュアルの定義文が常に上位を占め、
> 略号の列名しか持たない DB 側が負ける（提示候補の大半がマニュアルのチャンクで占められる）。
> 逆に「臨床的意味の典拠」を探すときは `reference` だけを対象にする。
> **この振り分けは payload の `file_role` だけが根拠**であり、ファイル名や `kind` からは判定できない。

#### kind=fhir の payload

`connection_id`, `resource_type`, `query_template`, `fhir_path`, `code`。

#### kind=master の payload

`connection_id`, `db_qualified`, `code`, `label`, `aliases`（任意）。
**マスタは「コード表の 1 エントリ」であって値の取得先ではない。**
クエリ側はここから取得 op を組まず、`WHERE <code列>='...'` の材料として使う
（対象列は `ref_links.jsonl` の FK 逆引きで得る）。

### column_index.jsonl のレコード（既存実体）

```json
{"table_id": "LAB", "db_qualified": "DEMO.LAB", "table_logical": "検査結果",
 "db_group": "DEMO_DB", "physical_name": "BLAST_PCT", "logical_name": "芽球割合",
 "type_raw": "NUMBER(5,2)", "type_base": "NUMBER", "pk": false, "notes": "...",
 "is_patient_key": false, "key_type": null, "patient_scoped": null, "inferred": false,
 "physical": {"schema": "DEMO", "table": "LAB", "column": "BLAST_PCT"}}
```

`physical` は実行用の識別子で、決まらない列では `null`（`build` は db payload へ写さない）。

## 2. 埋め込み（契約クリティカル）

- `embedding_model`: 既定 `bge-m3`（Ollama）。**ビルド側とクエリ側で必ず一致**させる。
- 次元・`l2_normalize` の有無は `build_info.json` に記録し、両側で一致させる。
- `embedding_digest`（省略可能）: 索引ビルド時の埋め込みモデルのダイジェスト
  （Ollama `/api/tags` の `digest`）。**モデル名（タグ）だけでは重みの同一性を保証できない**
  （`ollama pull` で上流が更新されても、別のサーバが同名タグで違う重みを持っていても名前は同じ）
  ので `build_info.json` に焼く。クエリ側は起動時に、問い合わせに使う `embedding_host` から得た
  ダイジェストと照合し、**違えば検索を無効化して理由を出す**（黙って再現率を落とさない）。
  `null` は**未検証**（この欄が増える前の索引、ダイジェストを取れなかったビルド）で、
  無効化はしない。付加的な欄なので `schema_version` は上げない。
- クエリ側は問い合わせベクトルを **同一モデル・同一正規化**で生成すること。

## 3. schema_version

- 契約（ファイル形式・レコードスキーマ・埋め込み仕様）を変えたら **必ず version を上げる**。
- クエリ側 store は `build_info.json` の `embedding_model` / `schema_version` 不一致時に
  `is_searchable=false` へ縮退する（drift 防止 B＝実行時防御）。

## 4. connection_id 名前空間

- 成果物は `connection_id` を埋め込む（取得先・value_dictionary のキー）。
- WebUI 側 executor は **同一 connection_id** を自分の connections.yaml で解決する。
- よって `connection_id` ＋ kind/scope/driver/base_url/root_path（接続の **論理定義**）は
  両側で一致させること。物理的な宛先は **レプリカ前提**（indexer はスキーマ読取用、
  executor は本番データ取得用で別インスタンスでも可。論理スキーマと id が一致すればよい）。
- secret 実値（DSN 等）は環境ごと（`secret_ref: env:NAME`）。git にはコミットしない。

## 5. key_types カタログ

- `key_types.yaml`（key_type → label/system/canonical）も両側で一致させる参照データ。
- 患者キー列の `key_type` は indexer 側ワークフロー（draft → 人確認 → ingest）で確定し、
  成果物（`column_index.jsonl` の `key_type` / 知識層 YAML）に焼き込む。WebUI は読むだけ。

## 6. drift 防止（B+C）

- **B（実行時防御）**: `build_info.json` 互換チェックで不一致を `is_searchable=false` に縮退。
- **C（CI 検知）**: ゴールデンテスト（`tests/test_contract.py`）が、ビルド側
  （`kawasemi_indexer`）とクエリ側（`kawasemi`）の `embedder` / `schema` の**両方の複製**を
  同じ期待値で検査する。固定入力 → 既知のレコード・正規化結果を照合し、どちらかが契約から
  ズレたら CI が落ちる。
