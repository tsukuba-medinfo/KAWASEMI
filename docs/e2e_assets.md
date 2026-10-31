# E2E 試験環境の資産（生成器・固定資産・gold）

E2E 試験環境（様式 → 臨床的意味 → 所在探索 → 手順書 → DSL → 実行 → 回答の保存）のうち、
**ダミーデータの生成器と固定資産**について書く。関門 (a)〜(e) の判定は
[e2e_pipeline.md](e2e_pipeline.md)、実機層と運用は [e2e_guide.md](e2e_guide.md) に書いてある。

## 1. 配置

```
examples/e2e/                             固定資産（追跡する）
  questionnaire_e2e_infection.json         架空の感染症様式（14 項目）。実在の様式は写していない
  knowledge.yaml                           接続 dwh の確認済み知識層（ingest の入力）
  knowledge.billing.yaml                   接続 billing の確認済み知識層（ingest は 1 ファイル＝1 接続）
  definitions/{dwh,billing}_definition.csv 定義書（表・列の論理名。knowledge.yaml から参照）
  file_manifest.jsonl                      レポート・CSV の所在宣言（file 単位）
  fhir_dictionary.jsonl                    FHIR の臨床項目辞書（fhir 単位）
  references/e2e_infection_manual.md       架空の記入要領（典拠。章立てで引ける）
  reference_scopes.yaml                    様式 URL → 典拠にしてよい文書
  connections.e2e.yaml / key_types.e2e.yaml
  gold.jsonl                               項目ごとの期待単位・経路・患者ごとの期待値（生成器が書く）
  script/                                  台本（design.yaml / execution.yaml）
var/e2e/                                  生成物（追跡しない。`kawasemi e2e setup` で再生成）
  dwh.db / billing.db                      外部DB（2 接続）
  reports/{report_key}/                    入院時サマリ txt ＋ 手書き同意書 PDF
  fhir_stub/                               Patient crosswalk ＋ Observation
  csv/ef.csv                               DPC EF 相当
  connections.yaml / key_types.yaml      実行時  に読む複製
kawasemi/ai_input/e2e/
  __init__.py                              定数・E2ESubject・SUBJECTS（seed 固定の患者 10 名）
  sources.py                               生成器と build_gold
  seed.py                                  様式・利用者・データセットのアプリDB投入
  index.py                                 索引の構築（`kawasemi e2e setup --build-index`）
  embedder.py / scripted_llm.py / gates.py  決定的層の embedder・台本 LLM・関門
  pipeline.py                              一気通貫の工程（決定的層と実機層で共有）
tests/ai_input/test_e2e_sources.py       決定性・gold の一致・ingest・seed の検証
```

`kawasemi e2e setup` は `var/e2e/` と `gold.jsonl` を作り、`connections.yaml` / `key_types.yaml` を `var/e2e/` へ複製する。
**アプリDBには触らない**（索引の構築と様式・利用者の投入は決定的層の試験が行う）。

## 2. 散らばり方

患者 10 名（`E2E-0001`〜`E2E-0010`）。各システムの患者キーは別体系で、crosswalk は FHIR Patient.identifier
だけが持つ（KAWASEMI は名寄せの値を保持しない）。

| システム | 患者キー | key_type | 中身 |
|---|---|---|---|
| `dwh.db`（接続 `dwh`） | `patient_no`＝mrn の 10 桁ゼロ詰め | `mrn` | 横持ち `patients` / `admissions` / `infection_cases`、`item_code` 縦持ち `vital_signs`（`value_num`）/ `observations`（`value_text`・`seq`）、`lab_results`（`test_code` → `lab_tests`）、マスタ `sex_codes` / `lab_tests` / `observation_items` / `measurements`、件数表 `item_counts` |
| `billing.db`（接続 `billing`） | `account_no`＝`B000000` 形 | `billing_id` | `billing_stays`（在院日数・抗菌薬投与日数）/ `billing_drugs` |
| `reports/`（接続 `report_fs`） | `RPT-E2E-0000` | `report_key` | `admission_summary.txt`（診断名・発症日は本文にだけある）/ `consent.pdf`（テキスト層なし） |
| `fhir_stub/`（接続 `hosp_fhir`） | `fhirpat-e2e-0000` | `fhir_patient_id` | `Observation-pathogen` / `-severity` / `-specimen-date` |
| `csv/ef.csv`（接続 `dpc_ef`） | 患者ID 列＝mrn の 10 桁ゼロ詰め | （DWH と同じ物理キー） | 薬効分類 ABX の行の有無が抗菌薬投与の有無 |

観察記録には**有効でない行**を混ぜてある。`vital_signs` の `is_deleted='1'` の体温は真の最高体温より 1.5 ℃ 高く、
`observations` の `weight`（体重測定）は seq 1 体重と seq 2 体脂肪率が同居し（体脂肪率の記録日は入院翌日なので、
seq で絞らずに「最新」を採ると体脂肪率になる）、`is_deleted='1'` の行は桁違いの値を持つ。
絞り込み（`item_code` / `seq` / `is_deleted` / `is_active`）を落とすと期待値と合わなくなるので、
関門 (d) が値で検出できる。`item_counts` は項目×測定×フラグ別の件数表で、
患者の行を読まずに項目単位を作るための材料である。`knowledge.yaml` では `table_roles` を `patient_data`
にしつつ `patient_unscoped_tables` にも載せている。前者は「内容（行）を索引に読ませない」（`master` にすると
code→label を読む対象になる）、後者は「患者で絞る表ではない」（`patient_scoped=false` が立ち、
患者キー列が無いことを不足として報告させない）という別々の宣言だからである。

### 物理整形（ゼロ詰め）の置き場所

`mrn` の canonical はゼロ詰めを除いた数値文字列で、DWH の `patient_no` と CSV の `患者ID` 列は 10 桁ゼロ詰めで
格納されている。**この物理整形は knowledge.yaml にも索引にも宣言できない**（indexer に語彙が無い）ので、
接続レジストリ `connections.e2e.yaml` の `key_formats: {mrn: {pad: 10}}`（`dwh` と `dpc_ef`）に宣言する。
file / csv / fhir の接続には既定の `key_type` も書く。コンパイラがこれと索引の `payload.key_type` から
step の `key_type` / `physical_format` を焼き込む。台本の `program` には書かない。

| キー | key_type | 物理整形 |
|---|---|---|
| DWH `patient_no` | `mrn` | **要る**（`{pad: 10}`） |
| CSV `患者ID` | （`mrn` の物理形をそのまま突合） | **要る**（`{pad: 10}`） |
| `account_no` | `billing_id` | 要らない（crosswalk の値をそのまま使う） |
| `reports/{report_key}/` | `report_key` | 要らない |
| FHIR `Patient.id` | `fhir_patient_id` | 要らない |

## 3. 様式と経路

| 項目 | link_id | 経路 | 期待単位（gold.unit_id） |
|---|---|---|---|
| [1] 登録番号 | `patient_id` | 入力（Hub） | gold に無い |
| [2] 生年月日 | `birth_date` | db | `db:patients.birth_date` |
| [3] 性別 | `sex` | db（value_domains → `sex_codes`） | `db:patients.sex_code` |
| [4] 入院年月日 | `admission_date` | db | `db:admissions.admitted_on` |
| [5] 発症日 | `onset_date` | **fallback**（DWH が空なら本文） | `db:infection_cases.onset_date` → `file:patient_data:入院時サマリ` |
| [6] 診断名 | `diagnosis_text` | **llm**（本文を読む） | `file:patient_data:入院時サマリ` |
| [7] 最高体温 | `max_body_temp` | db（**item_code**。有効行の最大） | `db:vital_signs.value_num#body_temp` |
| [8] 入院時体重 | `body_weight` | db（**item_code**・2 seq 同居。最新） | `db:observations.value_text#weight` |
| [9] 入院時 CRP | `crp_on_admission` | db（マスタ結合。最初の行） | `db:lab_results.result_value` |
| [10] 起因微生物 | `pathogen` | fhir | `fhir:Observation:pathogen` |
| [11] 重症度 | `severity` | fhir | `fhir:Observation:severity` |
| [12] 抗菌薬投与の有無 | `antibiotic_given` | csv | `file:patient_data:DPC出来高EFファイル` |
| [13] 抗菌薬投与日数 | `antibiotic_days` | db（第 2 接続） | `db:billing_stays.antibiotic_days` |
| [14] 同意取得日 | `consent_date` | file（手書き PDF） | `file:patient_data:研究参加同意書（手書き）` |

発症日は `E2E-0003` / `0006` / `0009` の 3 名で DWH が NULL（`gold.expected_source` が `text`）。

## 4. gold の導き方

`gold.jsonl` は**生成器と同じ `SUBJECTS` から** `sources.build_gold()` が組む（1 行 1 項目）。データと正解が
別々の手で書かれていないので、食い違いが構造的に起きない。`tests/ai_input/test_e2e_sources.py` の
`test_tracked_gold_matches_fresh_generation` が、追跡ファイルといま生成したものの一致を検査する。

1 行のキー:

- `link_id` / `route`（`db` `file` `fhir` `csv` `llm` `fallback`）/ `connection_id` / `unit_id`
- `expected`: 登録番号 → 期待値。型は様式の `item.type` に従う（date → ISO 文字列、decimal/integer → 数値、
  choice → コード文字列、string → 文字列）
- 任意: `alt_unit_ids`（同じ値へ辿り着く別の単位。性別の `master:` など）、`row_filters`（有効行の条件）、
  `aggregate` / `select` / `order_column`（複数行から 1 値を決める規則）、`fallback_unit_id` と
  `expected_source`（フォールバック項目で患者ごとにどちらの経路で決まるか）、`csv`（患者ID 列と絞り込み）

`unit_id` は indexer の builder が付ける形（`db:<表>.<列>` / `file:patient_data:<label>` /
`fhir:<型>:<code>` / `master:<接続>:<表>:<code>`）に揃えてある。観察項目の単位は
`db:<表>.<列>#<item_code>` の形で、**seq で分かれる単位の命名はまだ決めていない**。
それまでは `row_filters` に `seq` を書き、判定側（関門）は `unit_id` の前方一致で見る。

seed を変えると患者も gold も変わる。`E2E_SEED` を変えたら `kawasemi e2e setup` で `gold.jsonl` を作り直して
コミットする（テストが一致を要求する）。

## 5. 索引の入力

```
kawasemi-indexer ingest examples/e2e/knowledge.yaml \
    --dsn sqlite+aiosqlite:///var/e2e/dwh.db --out <出力先/dwh>
kawasemi-indexer ingest examples/e2e/knowledge.billing.yaml \
    --dsn sqlite+aiosqlite:///var/e2e/billing.db --out <出力先/billing>
```

`ingest` は 1 ファイル＝1 接続で、出力先に `L2/column_index.jsonl` と `value_dictionary.jsonl` を書く
（同じ出力先に 2 接続を書くと上書きされる）。`db_group` は接続 ID と同じにしてある（builder が
`connection_id = db_group` を流用するため）。

**`build_index` の `db_source` は 1 つしか受けない。** 2 接続の取り込み結果をそのまま渡すと片方しか
索引に入らず、`db:billing_stays.antibiotic_days` が無い索引になる（関門 (a) で落ちる）。E2E の索引を
作るときは、両接続の出力を**1 つの DB 定義書ディレクトリへ連結**してから `build` に渡す
（indexer が複数ソースを受けるようになるまでの手順。`kawasemi/ai_input/e2e/index.py` が実装した）:

```
mkdir -p <連結先>/L2
cat <出力先/dwh>/L2/column_index.jsonl <出力先/billing>/L2/column_index.jsonl > <連結先>/L2/column_index.jsonl
cat <出力先/dwh>/value_dictionary.jsonl <出力先/billing>/value_dictionary.jsonl > <連結先>/value_dictionary.jsonl
kawasemi-indexer build --db-source <連結先> \
    --file-source examples/e2e/references --file-manifest examples/e2e/file_manifest.jsonl \
    --fhir-source examples/e2e/fhir_dictionary.jsonl --out <索引>
```

表名は接続をまたいで重ならないようにしてある（unit_id が `db:<表>.<列>` で接続 ID を含まないため）。
観察項目の単位（`#<item_code>`）の追記は `build` の後に行う。E2E では `observation_items` / `measurements` /
`item_counts` から `ItemSpec` を組んで追記する。外部DBの単位の実行用の識別子（`payload.physical`）は
`ingest` が reflection から付ける（[e2e_pipeline.md](e2e_pipeline.md) §2）。

## 6. 項目を足すとき

1. `E2ESubject` に値のフィールドを足し、`make_subjects` で乱数から埋める（**末尾に足す**。途中に挟むと
   以降の乱数が全部ずれて既存の gold が変わる）
2. 値の置き場所を `sources.py` の該当ビルダー（`build_dwh` / `build_billing` / `build_reports` / `build_fhir_stub` /
   `build_dpc_ef_csv`）に足す。DB の列なら `definitions/*.csv` と `knowledge.yaml`（患者キー・表役割）も
3. 様式の `item` と記入要領の章を足す。所在が file / fhir なら `file_manifest.jsonl` / `fhir_dictionary.jsonl` に宣言する
4. `build_gold` に行を足す（経路・期待単位・期待値）
5. `kawasemi e2e setup` で `gold.jsonl` を作り直し、`uv run pytest tests/ai_input/test_e2e_sources.py` を通す
6. 台本（`examples/e2e/script/`）に同じ項目を足し、`make e2e` で関門 (a)〜(e) を通す（[e2e_pipeline.md](e2e_pipeline.md) §4）
