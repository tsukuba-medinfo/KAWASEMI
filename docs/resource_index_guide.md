# リソース索引 作成・運用マニュアル

最終更新日: 2026-05-31

ステップ1・役割2（データ所在）の「AIによる案の作成」は、施設のリソース（DB／ファイル／
FHIR）の**所在カタログ**を embedding で検索し、各 Questionnaire 項目に関連する候補を
絞り込んで提示する。このマニュアルは、その検索基盤となる**リソース索引**を作る手順と、
入力データの書式・運用を説明する。

関連: [DB定義書コーパス仕様](./db_definition_corpus.md) ／ [AI支援入力設計の2役割化](./ai_design_two_roles.md)

> **索引の生成・取り込みは indexer の責務です。** indexer はこのリポジトリの `indexer/` にあり、
> uv の workspace メンバーとして入ります（`uv sync --all-packages --all-groups --all-extras`）。索引のビルド／取り込み／
> 知識層下書きは KAWASEMI 本体から独立した SE 向け CLI `kawasemi-indexer` が行います
> （本マニュアルのコマンド例は `kawasemi-indexer build ...` 等で記載）。KAWASEMI 本体に残るのは
> 点検用の `kawasemi index search` のみです（indexer 側 `kawasemi-indexer search` でも点検可能）。

---

## 1. 全体像（3層分離）

リソース索引まわりは独立した3層で構成される。施設が用意するのは(1)と(2)の入力データだけ。

| 層 | 実体 | 役割 | 施設が用意するもの |
|----|------|------|---------------------|
| (1) 接続レジストリ | `connections.yaml` | 外部到達手段・認証・scope | 接続定義（secret は環境変数参照） |
| (2) リソース索引 | `*_index.jsonl` + `embeddings.npy` + `build_info.json` | 各リソースの中身カタログ＋ベクトル | `kawasemi-indexer build` で生成 |
| (3) LLM提示 | （実行時に自動） | 検索hitをLLMへ提示し候補選択 | なし |

**重要な原則 ―「索引＝地図、実行＝患者値」**: 索引には**患者データを載せない**。索引対象は
患者データを含まないメタ・共通知識のみ（DB定義書のスキーマ、FHIR臨床項目辞書、参照文書の
本文）。実際の患者値の取得は、後日の実行工程でのみ行う。

---

## 2. 前提条件

- **Ollama** が起動しており、**embedding モデル**が pull 済みであること。

  ```bash
  ollama pull bge-m3
  ```

  既定モデルは `bge-m3`（多言語、日本語対応）。`EMBEDDING_MODEL` 環境変数または
  `settings.embedding_model` で変更可。**索引生成時とKAWASEMI実行時で同一モデル**にすること
  （異なるとベクトル空間がずれ、KAWASEMIは検索を自動無効化する）。

- 設定（`.env` または環境変数）

  | 変数 | 既定 | 用途 |
  |------|------|------|
  | `EMBEDDING_MODEL` | `bge-m3` | embedding モデル名（索引・クエリ共通） |
  | `RESOURCE_INDEX_DIR` | `var/resource_index` | 索引の出力／読み取りディレクトリ |
  | `DB_DEFINITION_DIR` | `site/db_definition` | DB定義書コーパス |
  | `CONNECTIONS_FILE` | `site/connections.yaml` | 接続レジストリ |
  | `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama エンドポイント |

> 施設固有データ（`site/db_definition/`, `var/resource_index/`,
> `site/connections.yaml`）は `.gitignore` 済み。リポジトリにはコミットしない。

---

## 3. 索引の生成（`kawasemi-indexer build`）

索引の生成は indexer（`kawasemi-indexer`）の責務。kind（db / fhir / file）ごとに入力源を指定して、
1つの索引ディレクトリへ**一括生成**する。

```bash
# db のみ（既定。--db-source 省略時は DB_DEFINITION_DIR を使う）
kawasemi-indexer build --db-source site/db_definition

# 複数 kind を一括（推奨：embeddings.npy の行整合のため必ず1回で生成）
kawasemi-indexer build \
  --db-source     site/db_definition \
  --fhir-source   site/fhir_dictionary \
  --file-source   site/reference_docs \
  --file-manifest site/file_manifest.jsonl \
  --out           var/resource_index \
  --model         bge-m3
```

| オプション | 対象 | 入力 |
|------------|------|------|
| `--db-source` | db | DB定義書ディレクトリ or `column_index.jsonl` |
| `--fhir-source` | fhir | 臨床項目辞書ディレクトリ or `fhir_dictionary.jsonl` |
| `--file-source` | file(reference) | 参照文書ディレクトリ（text/markdown/PDF を走査） |
| `--file-manifest` | file(patient_data) | 所在宣言 `file_manifest.jsonl` |
| `--out` | — | 出力ディレクトリ（既定 `RESOURCE_INDEX_DIR`） |
| `--model` | — | embedding モデル名（既定 `EMBEDDING_MODEL`） |

> **複数 kind は必ず1回の `kawasemi-indexer build` で生成すること。** `embeddings.npy` は全 kind を
> 連結した単一ファイルなので、kind を別々に `build` すると行がずれて索引が壊れる。

### 出力（出力契約）

```
<out>/
  db_index.jsonl       # kind=db のユニット（1行1ユニット）
  fhir_index.jsonl     # kind=fhir（任意）
  file_index.jsonl     # kind=file（任意）
  embeddings.npy       # 全ユニットのベクトル（float32, 行 = 全jsonl連結順）
  build_info.json      # schema_version / embedding_model / dim / built_at 等
```

各ユニット（`ResourceUnit`）の共通形:

```json
{
  "unit_id": "db:DEMO.LAB.BLAST_PCT",
  "kind": "db",
  "label": "芽球割合",
  "search_text": "検査結果 BLAST_PCT 芽球割合 ... DEMO_DB",
  "snippet": "DB=DEMO_DB 表=DEMO.LAB（検査結果）型=NUMBER(5,2) メモ=...",
  "payload": { "connection_id": "DEMO_DB", "...kind固有...": "..." }
}
```

---

## 4. 入力データの書式（kind 別）

### 4.1 db ― DB定義書由来

入力は既存の DB定義書コーパス `L2/column_index.jsonl`（1行1カラム）。書式の詳細は
[DB定義書コーパス仕様](./db_definition_corpus.md) を参照。索引時に**本番DBへは接続しない**
（定義書が真実という前提）。

`payload` に組み立てられる主な値:

| payload キー | 由来 | 用途 |
|--------------|------|------|
| `connection_id` | `db_group` | 接続レジストリ参照キー |
| `db_qualified` / `physical_name` / `logical_name` / `table_logical` | 定義書 | location 文言 |
| `type_raw` / `pk` / `notes` | 定義書 | 候補表示 |
| `patient_key` | PK＋列名ヒューリスティック（`*_pt_id` 等）＋relationships推定 | 患者絞り込み（不明なら null → ステップ3で要補完） |
| `patient_scoped` | **施設の確認を経た宣言**。`false` のときだけ意味を持つ | 患者で絞らない表（マスタ参照・施設固定値）。下記 |
| `physical` | indexer の `ingest`（reflection）か `enrich-physical` | 外部DBで実行に使う `schema` / `table` / `column`。下記 |

**`patient_scoped: false`（患者で絞らない表の宣言）**: 施設マスタ・コード表のように、
患者を識別する列が原理的に無い表には、`patient_key` が決まらない。宣言が無い間は「患者絞り込み
キー列が無い」として不足になり、人の補完へ回る（理由に、この宣言という出口があることを含める）。
宣言があれば `WHERE` を持たない `SELECT <列> FROM <表>` を組み、`:patient_id` のバインドも
要求しない。**守ること**: 宣言は真偽値の `false` のときだけ認める（文字列 `"false"`・`0`・欠落は
認めない）。**表論理名に「マスタ」を含むことなど、名前からは宣言しない**——患者データの表で
キーの推定に失敗したときに、全患者の行を返す SQL ができるため。宣言は施設の確認（導入時の
施設調整。`key_types.yaml` の差し替えと同じ系列）を経たものに限り、それまで宣言は空である。
`patient_key` や `access_path`（患者への到達経路）と同時にあるのは矛盾なので、どちらも信じず
人へ回す。

**`physical`（実行用の識別子）**: `db_qualified` は定義書（元システム側）の名前で、
実行先DBの名前とは限らない（例: 定義書の `SRC.DOCUMENT` が実行先DBでは `ehr.DOCUMENT`）。外部DBの
単位は `physical` の3つ（`schema` / `table` / `column`）が揃うときだけ SQL を組み、揃わなければ
不足として人へ回す（§6.1）。付けるのは indexer で、入口は2つある。

- `kawasemi-indexer ingest`: reflection した表に付ける。スキーマは知識層（knowledge.yaml）の
  `physical_schemas`、無ければ reflection したスキーマ（`--schema` か接続の既定。SQLite は `main`）。
  候補が複数なら、reflection したスキーマが候補に在ればそれ、無ければ実行先の表一覧（`--catalog`、
  無ければ DSN の各候補スキーマの表名）で一意に決まる表だけに付ける。候補の表を1つも列挙できなければ
  ingest は理由を言って止まる
- `kawasemi-indexer enrich-physical --base <索引> --out <書き出し先> --knowledge <知識層…> --catalog <表一覧>`:
  定義書から作った既存の索引に、実行先DBの表一覧（`schema_name|table_name|行数`）と知識層の
  `physical_schemas` を突き合わせて追記する。文書種別の単位（`doc_type_code`）には、知識層の
  `doc_header`（ヘッダ表・結合列・種別の列・有効行の条件・並び順・本文の患者キー列）から `access_path` と
  `patient_key` も足す。
  `search_text` と `embeddings.npy` は変えない。一意に決まらなくなった単位の古い `physical` は外す

**`physical` と文書種別の `access_path` は、追跡済みのコードで再現できる。** 規則は indexer の
`enrich-physical` にあり、対応表とヘッダ表を知識層から読む（施設固有の値はコードに持たない）。
新しく文書種別の単位を作るときは、施設側のスクリプトで同じ `access_path` と
`patient_key` を付ける。

どちらも**一意に決まらない表には付けない**（同名の表が複数の候補スキーマにある等）。推測で付けると
実行できてしまうぶんだけ悪質なので、不足として見える形で残す。**文書種別の単位は `access_path` が
無ければ `physical` を付けない**（本文の表が種別の列を持たない構成では、経路なしで実行すると患者の全文書
——削除済みを含む——を返す）。KAWASEMI 側も、経路の無い文書種別の単位は組み立てず不足として返す（§6.1）。

**識別子は引用符なしで SQL に埋まる。** 大文字小文字を区別して作られた表（PostgreSQL で `"MyTable"` の
ように引用符付きで作ったもの）は、引用符なしでは小文字に畳まれて引けない。そういう接続は現状対象外で
ある（CONTRACT.md の `physical`）。

**項目コードで行を選ぶ単位（`item_code` / `item_name` / `item_filter_column`）**: 経過表のような
縦持ちの表（例: `DEMO.VITALS` に体温・脈拍・SpO2 が同居する）は、何の値かが列名ではなく同じ表の
項目コード（例: `ITEM_CODE`）の値で決まる。1項目を1単位（`unit_id` は `db:<表>.<列>#<項目コード>`）にし、
`access_path.filters` に `{"column": "ITEM_CODE", "equals": "<項目コード>"}`（`via` なし＝同じ表）を持たせる。
有効な行の条件（`DELETED='0'` など）は `item_row_filters` に持つ（値は施設のデータ管理部門に確認する）。
`location` には `/ 項目: <コード>（<名称>） / 絞り込み: <表>.<列>='<コード>' AND DELETED='0'` が付き、
同じ列の単位どうしが同じ文字列にならない（文書種別の単位と同じ理由）。DSL の照合では項目コードと
有効行の条件の列名も必須トークンになり、どれかが現れない SQL は、索引から組んだ SQL で置き換わる。
**トークンは「現れるか」しか見ない**ので、`DELETED <> '0'` や `ITEM_CODE IN (…)` のような誤りは捕まえない。
1項目に測定が複数同居する項目（「体重／体脂肪率」など）は、項目コードだけでは値が混ざるので単位にしていない。

### 4.2 fhir ― 臨床項目辞書（手キュレーション）

`fhir_dictionary.jsonl`（1行1臨床概念）。施設の FHIR プロファイル/サポートコードに合わせて
手で整備する（または CapabilityStatement から骨格生成し別名・コードを付与）。

```json
{"label": "白血球数", "aliases": ["WBC", "白血球"], "resource_type": "Observation", "query_template": "Observation?code=http://loinc.org|6690-2&patient={{patient_id}}", "fhir_path": "valueQuantity.value", "code": "LOINC|6690-2", "connection_id": "hosp_fhir", "notes": "血液一般検査の白血球数"}
```

| フィールド | 必須 | 用途 |
|------------|------|------|
| `label` | ◯ | 候補名・検索語 |
| `resource_type` | ◯ | FHIR リソース型 |
| `aliases` | | 別名（検索語を増やす） |
| `query_template` | | 取得クエリ（`{{patient_id}}` を実行時に置換） |
| `fhir_path` | | 値抽出パス（FHIRPath ではなく簡易ドット記法。下記注意） |
| `code` | | LOINC/SNOMED 等のコード |
| `connection_id` | | 接続レジストリ参照 |
| `notes` | | 検索語＋表示 |

> `label` と `resource_type` が無い行はスキップされる。

> **`fhir_path` は FHIRPath ではない。** 実行側（`executor.resolve_fhir_path`）が解釈するのは
> `.` 区切りの辿りと `name[0]` 形式の整数添字のみで、`where()` / `ofType()` / `as` / `|` などの
> FHIRPath 構文は解釈されず黙って `null` になる。絞り込みは `query_template` の検索パラメータで
> 表現すること。詳細は [dsl_specification.md](dsl_specification.md) §5.2。

### 4.3 file(reference) ― 参照文書

`--file-source` のディレクトリを再帰的に走査し、`.txt` / `.md` / `.pdf` を抽出して
オーバーラップ付きでチャンク化する（PDFはページ単位）。**患者データを含まない共通文書のみ**
を置くこと（治験実施計画書・院内マニュアル等）。`payload` は
`{file_role: "reference", path, page, char_range, mime}`。

### 4.4 file(patient_data) ― 所在宣言マニフェスト

患者ごとのファイル（紹介状・外部検査レポPDF等）は**中身を索引しない**。所在パターンと
抽出方法だけを `file_manifest.jsonl` に宣言する（実際の読み取りは実行時 `file_extract` が行う）。

```json
{"label": "外部検査レポートPDF", "aliases": ["紹介状"], "path_pattern": "/reports/{{patient_id}}/*.pdf", "extract": {"method": "pdf_llm"}, "connection_id": "report_fs", "notes": "患者ごとの外部検査レポート"}
```

| フィールド | 必須 | 用途 |
|------------|------|------|
| `label` | ◯ | 候補名・検索語 |
| `path_pattern` | ◯ | 所在パターン（実行時に `{{patient_id}}` を置換） |
| `extract.method` | | 抽出方法（既定 `pdf_llm`） |
| `connection_id` / `notes` / `aliases` | | 参照・検索語 |

---

## 5. 接続レジストリ `connections.yaml`

索引(2層)は `connection_id` を参照するだけで、接続詳細・秘匿情報は持たない。実際の接続情報は
`connections.yaml` に分離する。サンプルは [`site.example/connections.yaml`](../site.example/connections.yaml)。

```yaml
connections:
  app_db:    {kind: db,   scope: self}                 # KAWASEMI 自身のDB → local_db
  dwh:       {kind: db,   scope: external, host: ..., secret_ref: env:DWH_DSN}  # → external_db
  hosp_fhir: {kind: fhir, scope: external, base_url: https://...}
  report_fs: {kind: file, scope: external, root_path: /mnt/reports, key_type: report_key}
```

- `key_type` / `key_formats`（任意）は患者キーの宣言で、コンパイラが step へ焼き込む。
  `key_type` は file / csv / fhir で `{{patient_id}}` を埋める名前空間、`key_formats` はこの接続が
  名前空間ごとに格納している物理形（例 `{mrn: {pad: 10}}`）。詳細は
  [key_resolution_guide.md](key_resolution_guide.md) §3。
- `scope: self|external` が db の `source_category` を **local_db / external_db** に振り分ける
  （未登録は local_db 既定）。
- `secret_ref` は環境変数/金庫参照（`env:VAR`）。**生のパスワード・DSN を置かない**。
- **`connections.yaml` を置かないと、ステップ3の op 引数の決定的導出（§6.1）は db について
  一切効かず、db の取得 step はすべて `needs_input`（人の補完待ち）になる。** 機能の不具合では
  なく、自アプリDB／外部DBの別を判定できない候補に対して SQL を組み立てないための設計である
  （判定材料が無いまま自アプリDBへ倒すと、任意の表をアプリ本体のセッションで読む SQL になる）。
  導入時は `site.example/connections.yaml` を `site/connections.yaml` へ複製して接続を登録すること。
- 接続の確立・データ取得は実行工程（`external_db_extract` / `file_extract`）の責務。
  いずれも実装済み。`file_extract` は txt/md 等のテキストと PDF のテキスト層（pypdf）を
  読む。**テキスト層を持たない PDF（スキャン・手書き）は自動縮退し、各ページを
  ラスタライズしてマルチモーダル Ollama（`ollama_vision_model`、既定は `ollama_model`）で
  文字起こしする（Vision OCR）**。読取手法・モデル・生出力は step trace に確定保存する。
  `ollama_model` が画像入力に対応しているとは限らないので、`OLLAMA_VISION_MODEL` に画像入力に
  対応したモデルを指定する。

---

## 6. KAWASEMI 側の利用と縮退

1. 起動時に `RESOURCE_INDEX_DIR` の索引をメモリへ読み込む（プロセス内シングルトン）。
2. 「AIによる案の作成」で、項目テキスト＋臨床的コンテキストを embedding 化し、cosine で
   kind横断 top-k を取得 → Ollama に候補を提示。LLM は `cand_id` を選ぶだけ。
3. `location` 文字列・`source_category` は選ばれたユニットの `payload`＋接続 scope から
   KAWASEMI が決定的に組み立てる（存在しない表/カラム/パスの捏造を構造的に防ぐ）。
4. 選ばれたユニットの `unit_id` / `connection_id` / `payload` は `system_locations.candidates`
   へ保存され、**ステップ3（DSLコンパイル）へ伝播する**。ステップ3は同じ `payload` から
   op 引数を決定的に導出する（下記 §6.1）。

### 6.1 ステップ3（DSLコンパイル）での op 引数の決定的導出

索引が持つ**識別子**は KAWASEMI 側が最終決定する
（`resource_index/derive.py: derive_op_args` → `ai_input/compiler.py: enforce_derived_op_args`）。

**この保証の範囲**は「生成物が索引の識別子を必ず含む」ことであり、「索引の識別子**しか**含まない」
ことではない。LLM の SQL が必須識別子をすべて踏んでいれば、時間窓や並び順を含む臨床ロジックごと
採用する（踏んでいなければ上表の引数で丸ごと上書きする）。したがって採用された SQL に索引外の
表が現れる余地は残る。実行時の防御は別に置かれている（`SELECT` 限定検査、確認済みプランのみ実行、
設計者による確認）。

| kind | 導出される op | 決定的に組み立てる引数 | payload の素材 |
|------|---------------|------------------------|----------------|
| db（scope=self） | `sql_extract` | `sql = SELECT <列> FROM <表> WHERE <患者キー列> = :patient_id`、`connection_id` | `db_qualified` / `physical_name` / `patient_key` / `connection_id` |
| db（scope=external） | `external_db_extract` | 同上。ただし `<表>` は `physical` の `<schema>.<table>`（`connection_id` と `physical` 必須。無ければ `needs_input`） | 同上＋`physical` |
| fhir | `fhir_extract` | `fhir_query`（`query_template` を先頭 `/` 付きへ正規化）、`fhir_path` | `resource_type` / `query_template` / `fhir_path` / `code` |
| file（`file_role=patient_data`） | `file_extract` | `path_pattern`（接続 `root_path` 起点の相対へ正規化）、`connection_id` | `path_pattern` / `connection_id` |
| master | （導出しない） | — | コード表の1エントリでコード列・ラベル列の物理名が無い（`WHERE` 句の材料として提示のみ） |
| file（`file_role=reference`） | （導出しない） | — | 参照知識で実行時に値を取らない |

LLM が担うのは**手順書由来の臨床ロジックだけ**（時間窓の `WHERE` 条件・`ORDER BY` /
`LIMIT` ・絞り込み）。生成された SQL / FHIRクエリ / パスが索引の識別子（表・カラム・
`:patient_id`・リソース型・コード・パス）を踏んでいれば臨床ロジックごと採用し、踏んで
いなければ上表の引数で丸ごと上書きする。識別子の照合は**単語境界**で行う（`LAB` が `LABS` に
誤マッチしない）。FHIR は `query_template` が `{{patient_id}}` を含むなら、それも必須識別子に
なる（患者絞り込みを落としたクエリを採用しない）。

次の場合は決定的に組めないものとして、推測を実行させず `needs_input=true` で人の補完へ回す。

- 索引に**患者絞り込みキー列（`patient_key`）が無く、`patient_scoped: false` の宣言も無い**、
  表・カラム・キー列が SQL 識別子の形でない
- **接続が `connections.yaml` に未登録**で自アプリDB／外部DBを判定できない。自アプリDB／外部DB
  の別は**接続レジストリの `scope` だけを根拠に決める**（候補の `source_category` は根拠にしない。
  §5 のとおりドラフト生成時の既定で未登録接続にも `local_db` が付きうるうえ、候補は保存時に
  値の妥当性を検査しないため）。判定できない候補は自アプリDBへ倒さず不足として扱う。ただし候補が
  外部DBを宣言している場合だけは `external_db_extract` を維持する（未登録の外部DBは実行時に
  接続解決で失敗するだけで、アプリ本体の DB には到達しないため）
- 外部DBなのに `connection_id` が無い
- 外部DBなのに `physical`（実行用の識別子 `schema` / `table` / `column`）が揃っていない（§4.1）
- 文書種別の単位（`doc_type_code` 等を持つ）なのに `access_path` が無い（§4.1。組み立てると患者の
  全文書を返す SQL になる）
- **FHIR の `query_template` が `{{patient_id}}` を含まない**。患者絞り込みの無いクエリは全患者を
  対象にし、executor は `multi` 未指定なら Bundle の先頭リソースを採るため、他患者の値が当該患者
  の値として保存されうる（DB の `patient_key` 欠落と同じ扱い）
- **`file_role=patient_data` の `path_pattern` が `{{patient_id}}` を含まない**（全患者で同じ
  ファイルを読むことになる）
- `path_pattern` が**ワイルドカード（`*` `?` `[`）を含む**。executor はパスを `is_file()` で
  リテラルに解決し glob を展開しないため、焼き込むと常に不成立となり「当該患者のデータなし」と
  誤って記録される。具体的なファイル名は人が確定する
- `path_pattern` が `..` を含む（接続 root 外への参照）

同じ所在候補を2つ以上の取得 step へ焼き込むこともしない（2件目以降は `needs_input`）。
術前値・術後値のように意味の異なる step が同じ SQL に潰れるのを防ぐ。**この保護は DB /
FHIR / ファイルの全ファミリに掛かる**（引数はそれぞれ SQL / FHIRクエリ / パスパターン）。
LLM が確定済み引数を両 step へコピペした形（大文字小文字・空白の違いを無視して同一の
生成物になる形）も同じ扱いで人へ回す。ただし**照合できた step が2つ並ぶ場合**（どちらも索引の
識別子を踏んでいて、生成物が同一でない場合）は、術前/術後のように意味の違う取得として通す。

**所在候補の配列順は優先順である。** フォールバック取得計画について**別の性質を2つ**検査し、
満たしていない step は `needs_input` として人へ回す。前者は優先順（どちらを先に試すか）、
後者は取得できた値の保全（結線）を見る。後者は優先順に一切触れない。

| 検査 | 対象 | 内容 |
|---|---|---|
| 取得の順序（優先順）| `guard` の参照先に**空判定**（`is_empty` / `is_not_empty`）があり、**その空判定が「空だった」を意味する向きで**参照されている step。空判定の左辺が辿る取得 step が「前の試行」にあたる | 前の試行より優先順の高い候補を引いていたら後戻り |
| 分岐の結線（値の保全）| `when` が空判定に依存し（否定・真偽ラッパも極性を読んで解決する）、空判定の対象が所在候補へ辿り着き、かつ**「空だったときに走る」代替候補の取得がある** `branch`（`guard` の極性で判別）| ①値があるときに採る側が空判定の対象を指していない、②空だったときに採る側が代替候補を指していない — のいずれかなら誤り |

**2つは別の性質である。** 前者は「どちらを先に試すか」（優先順）、後者は「取得できた値を
捨てていないか」（結線）を見る。したがって `when: "$has_b", then: "$b", else_branch: "$a"`
のように**第2候補の空判定を見る書き方も正しい**（b に値があれば b、無ければ a を採る）。
`is_empty` で書いた場合は `then` と `else_branch` が入れ替わるのが正しい形になる。極性と
「どちらの候補を見ているか」の両方を見ないと、正しい結線と誤った結線を取り違える。

**結線の検査はフォールバックの分岐に限る。** `is_empty` / `is_not_empty` は汎用の比較演算子
なので、値の有無をゲートに使う分岐（`when: "$has_consent", then: "$文書からの抽出値",
else_branch: "該当なし"`）にも現れる。そこでは候補の値は失われていないので対象外にする。
判別は上の表のとおり「空判定の対象が候補へ辿り着く」かつ「**空だったときに走る**代替候補の
取得がある」（`guard` の極性で見る）で行う。ゲート形では代替候補が「値があるときに走る」ので
対象外になる。両側がリテラルの形（`then: "1", else_branch: "2"`）は、**代替候補が無ければ**
値の有無の符号化として対象外だが、「空だったときに走る」代替候補があるなら、その取得値を
捨てているので対象である。**否定の書き方は複数あり、どれでも同じに読む** — `has_a == false` / `has_a != true`
（左右を入れ替えた `false == has_a` / `true != has_a` も同じ）、および**同じ値を見る別の
空判定**（`is_not_empty($a)` に対する `is_empty($a)`。`guard` に直結する、または `== true` で
囲む）。**`is_empty($a) == false` と書いてはいけない** — これは「空でないとき」なので極性が
逆転し、第1候補が成功したときにだけ代替候補の患者データ取得が走る（「第1候補が空だったときにだけ代替候補を取得する」の逆）。

**片側だけがリテラルの形は、リテラルがどちら側にあっても、また誤りがどちらの規則に触れても
対象である。** `then: "実施", else_branch: "$b"` と `then: "$b", else_branch: "該当なし"` は
規則①（第1候補の値が捨てられる）、`then: "$a", else_branch: "該当なし"` は規則②（**実際に
取得した代替候補の値**が捨てられる）に触れる。いずれも項目値が入るので `item_failure_code` が
付かず、取得可能性の集計では充足として数えられてしまう。片側・片規則だけを見ると鏡像を見逃す。

**`when` に否定（`$no_a`）や真偽ラッパ（`$yes_a`）を置いた形も対象である。** 極性を読んで
「値があるときに採る側」を決める。`when: "$no_a"` なら **`else_branch` 側**が「値があるとき
に採る側」になる。ここを直参照の形だけに限ると、`then: "$b", else_branch: "実施済み"` の
ように**取得できた値をリテラルへ置き換える**計画が痕跡なく確定できてしまう
（`item_failure_code` も付かないので集計では充足として数えられる）。

**空振り判定の向きが読めないときは人へ回す。** 極性の読み取りには限界があり、読めない書き方
（数値にできないリテラル、真偽の言い換えを重ねた形、`transform` / `llm_judge` の介在）では
判定できない。
そこで検査を飛ばすと、フォールバックとして正常に動くプランが2つの検査のどちらも通らないまま
確定する。`resource_index/derive.py` の `matches`（「検証できない」を「検証に通った」と扱わない）
と同じ方針で、`needs_input(空振り判定の向き不明)` として人へ回す。**`guard` と `when` の
両方に適用する**（片方だけにすると、もう片方へ同義表記を書くだけで両方の検査が外れる）。
判定の対象は「真偽の言い換えを辿って空判定に届く条件」である。連鎖は **`branch` を境界に**
推移的に辿る（ただし**両側がリテラルの `branch`** は取得した値を運んでいないので、真偽の
言い換えとして `when` を辿る）。境界を置かないと、取れた値の大小で分ける後段の条件まで巻き込む。

真偽リテラルは**実行時と同じ意味になる範囲だけ**読む — `0` / `1` とその文字列表記
（`has_a == 0` / `== "0"` / `!= 1`）である。`_loose_eq` は真偽を `1.0` / `0.0` として数値比較
するので、`2` や `-1` は**常に偽**（`==`）／**常に真**（`!=`）になり、真偽としては読めない
（読むと、常に走る guard を「空だったときに走る」と誤読して両方の検査を通してしまう）。

**読めない書き方は網羅できない**（数値にできないリテラル、`transform` / `llm_judge` の介在、
真偽の言い換えの重ね方は無限にある）。したがって**列挙で健全性を担保しない**。読めない形は
すべて `needs_input` で人へ回すので、**読めない書き方が増えても見逃しにはならない**。
連鎖は `branch` を境界に推移的に辿る。**ただし両側がリテラルの `branch`**
（`then: false, else: true` / `then: 0, else: "該当なし"` / `then: "1", else: "2"`）は取得した値を
運んでいないので、真偽の言い換えとして `when` を辿る。取れた値を使う後段の条件は必ず片側が
`$id` 参照の `branch` を通るので、この境界で両者を分けられる。

なお `when` 側の fail-closed は、**その空判定に `guard` で依存する別の候補の取得が無い
プランには適用しない**（失われる値が無いため）。`guard` を伴わない複数候補の取得は
この門を通らない（下記の「`guard` を付けずに複数候補を無条件に取得する形」が扱う）。

**空判定の対象が所在候補でない分岐は誤検出しうる。** `when` が `llm_judge` の判定や
`select` / `aggregate` を経た値の空判定で、かつその値が候補の取得へ辿り着き、「空だったときに
走る」代替候補がある形では、「値があるときに採る側が空判定の対象を指していない」と判定される。値の有無の符号化と
取得値の取り違えを静的に区別できないためで、`needs_input` として人へ回す側に倒している。

参照の連鎖は `left` / `right` / `when` / `then` / `else_branch` / `input` / `source` /
`match[].value` と **`params[].value` の `$id`** に加えて **`prompt` の `$id`・`{{id}}`** も辿る。`llm_extract` で値を取り出す
形（1候補が複数 step になる推奨形）で連鎖が切れると、検査が丸ごと効かなくなる。

**判定に step の並び順は使わない。** `guard` の無い取得 step は無条件に実行されるので、
並び順は優先順を表さない（どちらの値を採るかは `branch` の結線だけが決める）。並び順で
判定すると、条件付き実行（相互排他な `guard`）や多ソースのプログラム、前段で区分値を
取ってから表を切り替える形が、候補の配列順と並び順が違うだけで人の手待ちになる。

**空判定を経ない `guard` も対象にしない。** 取得値の大小比較で取得元を変える形
（`compare` で `>=` を書く条件付き実行）はフォールバックではない。`llm_judge` で書けば
対象外・`compare` で書けば対象、という書き方の違いで結果が変わってはならない。

裏を返すと、**空振りの判定を `is_empty` / `is_not_empty` 以外で書いたフォールバック**
（`llm_judge` に「空か」を尋ねる形）は、2つの検査のどちらも発火しない。`== ""` で書くことは
できない（`right: ""` を必須フィールド検査が弾く）。空の定義が `is_empty_value`
（`None` / `""` / `[]`）と揃わないため、そう書くべきでないことをプロンプトと
`docs/dsl_specification.md`（§5.8、§8.5）で示している。

次の形は2つの検査の対象外である（いずれも静的に正当な形と区別できない）。

- **同じ `guard` を共有する代替候補が2つ以上ある形**（`b` と `c` がともに `guard: "$no_a"`）。
  `b` が値を取れても `c` の患者データ取得が実行される。プロンプトは入れ子（`c` は `$no_b`）を
  指示しているが、検証では止めていない
- **代替候補が索引候補を持たないファミリで取得される形**。その step は候補として数えないので
  結線の検査が効かない
- **`branch` を書かず `return` を第1候補に固定した形**。代替候補の取得は実行されるのに値は
  捨てられる（`return` の解析は行っていない）
- **片側だけが `$id` 参照の `branch` を「その否定」に使った形**（`branch(when=$has_a,
  then="$a", else_branch=1)` を `guard` に置く）。値を運ぶ `branch` は連鎖の境界なので、
  極性が読めず代替候補も特定できない

**`guard` を付けずに複数候補を無条件に取得する形は、人へ回す。**
条件は「同じファミリの所在候補が2件以上あり、そのファミリの取得 step が2つ以上あって
**いずれにも guard が無い**」で、該当する step は `needs_input`（理由: `guard無しの複数候補取得`）
になり `unit_id` は刻まれない。`guard: true` のリテラルも無条件として扱う（`true` を
1つ書くだけで検査を回避できないようにするため）。

この形を静的に弾けるのは、**どちらの値を採るかが branch の書き方次第で優先順と逆にもなる**
のに痕跡が残らないためである（`fixes` は空、`unit_id` は step ごとに正しく刻まれ、
`needs_input` も立たない）。条件付き実行（`guard`）が「branch だけで書くと両方の取得が実行され、
不要な患者データ取得が起きる」として避けたい形そのものでもある。

**1つでも guard を持つ step があれば従来の検査（優先順・結線）に任せる**——「前段が空だったら
走る」定型を書こうとしている計画を人待ちにしないため。候補が1件しか無い項目の無条件2 step
（術前値・術後値など）も対象外である（どちらの候補を採るかという問題が存在しない）。

したがって**残る対象外は上の一覧の形に限る**。

確定に使った索引ユニットは `step.unit_id` に刻まれ、生成された DSL から所在の典拠を辿れる。
**候補重複・優先順の逆転・空振り判定の向き不明・guard無しの複数候補取得で人へ回した取得 step には `unit_id` を刻まない**（どの候補から取るか
未確定の step に典拠を付けると、監査証跡に偽の典拠が残る）。索引の情報が足りず `needs_input`
にした step（`missing` があるとき）や、引数を下書きとして提示した step には刻む — そこでは
どの候補を指しているかが確定しているためである。
`payload` を持たない所在（手入力など）では、引数・`needs_input` に手を入れない。ただし
`unit_id` は `strip_llm_unit_ids` が**必ず**落とす（LLM が書いた実在風の典拠を残さないため）。

**自動縮退（エラーにしない）**:

| 状況 | 挙動 |
|------|------|
| 索引が未配置 | DB定義書 BM25（従来方式）へフォールバック |
| `build_info.embedding_model` ≠ 設定モデル | 検索を無効化し BM25 へフォールバック（誤検索防止） |
| `schema_version` 非互換 | 検索を無効化（再索引が必要） |

> 索引を作る前でも BM25 で動作する。embedding 索引は段階導入できる。

> **検索が空・想定外のときは [`kawasemi index search`](#9-検索のデバッグkawasemi-index-search) で切り分ける。**
> どの経路（embedding / BM25）を通ったか、検索不可ならその理由（モデル不一致・schema非互換・
> 未配置）がヘッダに表示される。

---

## 7. 更新・バージョン・整合性

- **更新は全再生成**（kind単位の差分更新は未対応）。定義書・辞書・参照文書を更新したら
  `kawasemi-indexer build` をやり直す。
- 索引差し替え後は**プロセス再起動**で再ロードされる。
- `build_info.json` の `schema_version` / `embedding_model` / `dim` / `built_at` を
  起動時に照合する。`embedding_model` を変えたら**必ず全再生成**（旧ベクトルは無効）。
- staleness: `build_info.built_at` が入力（定義書等）の更新時刻より古ければ再索引を推奨。

---

## 8. クイックスタート（最小）

```bash
# 1. embedding モデルを用意
ollama pull bge-m3

# 2. DB定義書から索引を生成（indexer。既定パス）
kawasemi-indexer build --db-source site/db_definition

# 3. 候補探索AIを有効にして起動し、ステップ1役割2「AIによる案の作成」で確認
ENABLE_AI_DRAFTING=true kawasemi serve
```

FHIR辞書・参照文書・患者ファイル宣言も使う場合は §3 の複数 kind 一括コマンドを参照。

---

## 9. 検索のデバッグ（`kawasemi index search`）

「この項目が本番でどこにマッピングされるか」「なぜ候補が出ないか」を、Web/DB を介さず
1クエリ単位で確認するためのコマンド。本番の「データ所在AI案」(`AIResourceDrafter`) と
**同じ検索経路**を再現する（searchable なら embedding、不可なら DB定義書 BM25）。

```bash
# 基本（auto＝本番同様の自動選択）
kawasemi index search "白血球数"

# 臨床的コンテキストを連結（本番は item_text + annotation でクエリを組む）
kawasemi index search "発症日" --annotation "脳血管障害の初発エピソード"

# 経路を強制して A/B 比較（embedding が効いていても BM25 だけ見る）
kawasemi index search "白血球数" --engine bm25
kawasemi index search "白血球数" --engine embedding

# 構造化JSONで出力（jq でパイプ）
kawasemi index search "白血球数" --json | jq '.hits[].location'
```

| オプション | 既定 | 用途 |
|------------|------|------|
| `query`（位置引数） | — | 検索クエリ（項目名など） |
| `--annotation` | なし | クエリへ連結する臨床的コンテキスト |
| `--engine` | `auto` | `auto`（本番同様）／ `embedding` 強制 ／ `bm25` 強制 |
| `--top-k` | `8` | 表示する上位件数 |
| `--min-score` | 設定値 | cosine 足切り（embedding 時。`RESOURCE_INDEX_MIN_SCORE`） |
| `--model` | 設定値 | クエリ側 embedding モデル名（`EMBEDDING_MODEL`） |
| `--index-dir` | 設定値 | 索引ディレクトリ（`RESOURCE_INDEX_DIR`） |
| `--corpus-dir` | 設定値 | BM25 フォールバック用の DB定義書ディレクトリ |
| `--json` | off | 構造化 JSON で出力 |

### 出力の読み方

```
クエリ: '白血球数'
経路  : bm25（DB定義書フォールバック）  [指定: auto]   ← 実際に通った経路 / 指定した経路
索引  : var/resource_index | ロード=True ユニット=1234 索引model=bge-m3 検索可=True
検索  : query_model=bge-m3 top_k=8 min_score=0.5
検索不可の理由: embeddingモデル不一致（索引=bge-m3 / 指定=other） ← auto がBM25へ落ちた理由
```

各ヒットには `score` / 種別（DB・ファイル・FHIR）/ `label` のほか、本番でプランに入る
**導出所在**（`build_location`）と **source_category**（接続 scope から決定）を表示する。

### 「検索が空」のときの切り分け

1. **ヘッダの「検索可」を見る** ― `False` なら `検索不可の理由` を確認。
   - `embeddingモデル不一致` → 索引生成時と `EMBEDDING_MODEL` を揃える（または再索引）。
   - `schema_version 非互換` → `kawasemi-indexer build` で再索引。
   - `索引が未配置/未ロード` → §3 で索引を生成。
2. **`--engine bm25` で DB定義書側のヒットを確認** ― embedding を疑う前に、そもそも
   定義書コーパスに該当カラムがあるかを Ollama 非依存で確かめる。
3. **`--engine embedding --min-score 0` で足切りを外す** ― 候補は存在するが
   `min_score` で切られていないかを確認（既定 0.5）。低スコアしか出ないなら検索語不足。
4. **`--annotation` を足す／`label`・`aliases`・`notes` を充実させる** ― 検索語が薄いと
   cosine/BM25 ともに沈む。索引入力（§4）側の語彙を増やす。

> embedding 経路は Ollama 起動・embedding モデル pull が前提。未起動時はエラー行を
> 表示して BM25 を試す案内に留め、クラッシュしない。
