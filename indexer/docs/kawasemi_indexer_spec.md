# kawasemi-indexer 仕様（DB定義書メタ情報の取り込みと索引化）

最終更新: 2026-06-16
ステータス: 要件確定＋取り込みモジュール（builder宣言キー優先／
value_dictionary畳み込み／LLM下書きドラフタ）まで実装済み。

## 1. 背景と課題

KAWASEMI のデータ自動収集では、設問（Questionnaire 項目）に対して
「どの接続先のどの表のどのカラムから取るか」を決める必要がある。
この決定の土台が **リソース索引（`resource_index`）** であり、DB 定義書を
索引化して NL（設問テキスト・注釈）から関連カラム候補を検索する。

しかし索引ビルダ（`indexer/kawasemi_indexer/builder.py`）は、正規化済みの
`site/db_definition/L2/column_index.jsonl` を **入力前提**にしている。
施設ごとにバラバラな定義書（Excel のシート構成・ヘッダ位置の差異、紙ベース、
そもそも定義書が無く DB しか無い等）から、この `column_index.jsonl` を
**どう作るか**は空白だった。

kawasemi-indexer は、この **取り込み（ingestion）レイヤ**を仕様化する。
「自然言語から適切な SQL を可能にするための検索基盤を、異種の定義書から
信頼できる形で構築する」ことが目的である。

### 位置づけ（出力境界）

kawasemi-indexer の責務は **候補（テーブル/カラム）のランキングまで**。
NL を受けて関連カラム候補とメタ情報を返すところで終わる。SQL 本文の組み立ては
利用側（KAWASEMI の `compiler` 等）に委ねる。これは既存の **反捏造設計**
（LLM に SQL を書かせず、書けないときは `needs_input=true` / `sql=null`）と
整合する。「indexer」は適切な SQL を **可能にする検索基盤**であって、
SQL そのものを出力するシステムではない。

したがって索引にテーブル間リレーション（FK/JOIN 情報）は **持たせない**
（JOIN 生成を担わないため）。

## 2. 正規化先（canonical 契約）

`column_index.jsonl` を **公式契約**に昇格する。全取り込みアダプタの出力先であり、
バージョン管理する。1 行 = 1 カラムのフラットなレコード（既存実体）:

```json
{"table_id": "LAB", "db_qualified": "DEMO.LAB", "table_logical": "検査結果",
 "db_group": "DEMO_DB", "physical_name": "BLAST_PCT", "logical_name": "芽球割合",
 "type_raw": "NUMBER(5,2)", "type_base": "NUMBER", "pk": false, "notes": "..."}
```

`builder.py`（および embedder/store/derive）は本契約の **消費側**として現状のまま
据え置く。kawasemi-indexer は契約より上流（生ソース → 契約）のみを担う。

## 3. アーキテクチャ（5 層）

```
1. 生ソース        : information_schema スナップショット ＋ Excel/CSV 定義書
2. 確認済知識層    : マッピング設定 / 論理名補間 / 表役割 / 患者キー+key_type紐付け / コード辞書
                     （いずれも LLM 下書き → 人確認 → 永続・版管理）
3. 導出（契約）    : column_index.jsonl ＝ merge(骨格=info_schema, 知識層)
4. 索引            : embeddings.npy + *_index.jsonl + build_info.json（既存 builder/store）
5. 問い合わせ      : NL → 候補（テーブル/カラム）ランキング（既存 store.search 再利用）
```

層 1（生ソース）と層 2（確認済知識層）を **分離**することが要。
層 1 は機械的に再取得でき、層 2 は人の確認コストがかかる資産である。
両者を分けることで再構築時に知識層を再適用でき、確認コストを償却できる（§7）。

## 4. 取り込みソース（2 系統）と統合方針

対象は **Excel/CSV 定義書** と **生きDBの information_schema** の 2 系統。
両者は補完的:

- **information_schema** = 物理構造の真実（table/column/type/pk は正確だが論理名・メモが欠ける）
- **Excel/CSV 定義書** = 意味の源（論理名・メモ・コード表は豊富だが実 DB とズレうる）

### 統合方針: information_schema を骨格真実 → Excel で意味補完

1. 生きDBから `information_schema` を抽出し、**物理構造（table/column/type/pk）を正**とする。
   - DB エンジン差（sqlite/oracle/postgres 等）は SQLAlchemy の Inspector（reflection）で
     エンジン非依存に取得する。接続は `connections.yaml` の `connection_id`/`driver`/`secret_ref` を利用。
2. Excel の `logical_name`・`notes`・コード表を、**physical_name（＋表識別子）で結合**して補完する。
3. 不一致（Excel にあるが DB に無いカラム、型不一致等）は **定義書側を棄却**し、骨格を優先する。

## 5. Excel レイアウトの解釈（施設別マッピング）

施設ごとに異なる Excel レイアウト（どの列が physical_name か / 論理名か、ヘッダ行位置、
シート構成）を、**施設別マッピング設定**で canonical フィールドに対応づける。

- マッピングは宣言的に持つ（例: `physical_name=列C, logical_name=列D, header_row=5,
  sheet=...`、および Excel 上の表識別子をどう `info_schema` の表に対応づけるか）。
- 初回は **LLM がシートを見てマッピングを下書き** → **人が確認**して確定。
- 確定後は **決定的・再利用・監査可**（既存の「LLM 下書き → 確認」パターンと同型、
  `llm_json` / drafter 系インフラを流用）。
- マッピングは層 2（確認済知識層）に永続する。

## 6. 不変条件（医療ドメインの安全制約）

### 6.1 PHI 不混入: 患者データ行は見ない

- `information_schema` は **スキーマ定義のみ**抽出（データ行の SELECT サンプリング禁止）。
- LLM は **物理名 / 論理名 / 型 / notes / 定義書記載のコード表例**のみを参照する。
- 「索引 ＝ 地図（患者データ非投入）」の既存不変条件を維持する。

### 6.2 例外: マスタ（参照表/コード表）の内容は取り込み可

マスタ（性別コード→意味、検査項目コード→和名 等）は PHI を含まず、内容を読むことで
索引・値解釈が大幅に向上する。そこで:

- **表役割（table_role = master / patient_data）を明示宣言**（LLM 下書き → 人確認）。
- **master かつ患者キー無し**と人が確認した表のみ、`code → label` の対応を
  **統制語彙（コード辞書）として取り込む**。
- patient_data / トランザクション表は **メタのみ**、内容を一切読まない。
- 誤分類は PHI 混入に直結するため、role 確定は **人確認必須**（ヒューリスティック自動判定に依存しない）。

#### コード辞書の格納（推奨・実装時詳細）

`column_index.jsonl` の隣に `value_dictionary.jsonl` を置き、
`(connection_id, db_qualified, physical_name)` をキーに `code→label` を保持する。
取り込み方は **マスタ規模で二系統に分岐**する:

- **小コード表（FK 列の値域；性別・血液型・各種区分）**: `physical_name` 付きエントリとして
  当該カラムの `search_text` に畳み込み（例: 「男性」で SEX コード列が surface）、
  候補メタとして利用側に返す（利用側が `WHERE SEX='1'` を組み立てられる）。
  `builder.fold_value_dictionary`、上限 `_VALUE_FOLD_LABEL_CAP=200`/`_VALUE_DICT_PAYLOAD_CAP=500`。
- **大規模臨床マスタ（病名/検査/薬剤等）**: `physical_name` なし（マスタ表自身）エントリを
  **1エントリ=1ユニット（`kind=master`）** に展開する（`builder.build_master_units`、
  `master_index.jsonl`）。各ラベルが独立 embedding を持つため「急性骨髄性白血病」のような語が
  200件畳込上限に潰されず直接 hit する。payload はマスタ表の場所＋`code/label` を持ち、
  利用側が `WHERE <code列>='...'` を組める。`SCHEMA_VERSION=2` で追加（CONTRACT 同期）。

### 6.3 論理名・メモなしカラムの扱い: LLM で補間（推定印付き・人確認）

`information_schema` に存在するが Excel 補完の無いカラム（物理名のみ）は、
NL 検索でほぼ拾えない（検索品質＝補完カバレッジ）。そこで:

- 物理名＋表コンテキストから **LLM が論理名/説明を下書き**し、**`inferred=true` を付与**して収録。
- 推定を正と見せない運用（印の保持・監査）と **人確認**を前提とする。
- 実在カラムを隠さない（カタログの完全性）。`inferred` 補間も層 2 に永続する。

#### 補間対象の絞り込み（オンデマンド化・優先度付け）

定義書レス施設では未補間列が数千〜数万に及び、全列一括の LLM 補間はローカル LLM では
非現実的な時間になる。`draft-knowledge` は補間対象を絞る手段を持つ（既定は従来どおり全列）:

- `--only-missing` / `--resume`: 既存 knowledge YAML を土台に、**未補間の列だけ**を追補する
  （冪等・再開）。`--resume` は分類・患者キー等の構造下書きも再利用し、補間のみ再実行する
  （再実行コストを抑える）。人が `confirmed` にした補間は保持し上書きしない。
- `--enrich-tables GLOB`: 表名グロブ（例 `DEMO.*`）で補間対象表を限定。
- `--enrich-limit N`: 補間列数の上限。優先度上位から採る。
- `--near TERM`: 物理名・表名がこの語に一致する列を優先（検索で拾えなかった項目起点の近傍補間）。
- `--dry-run-enrich`: 対象の内訳（全列 / 定義書被覆 / 既補間 / 表フィルタ除外 / 候補 → 補間列数）と
  推定 LLM 呼び出し回数のみ表示し、LLM を呼ばない（コスト見積り）。

優先度は「患者キー保持表・patient_data 表の列を先に」＋「`--near` 一致列を加点」。実行後は
採用件数・LLM 呼び出し回数・所要時間をログ出力する。補間結果は従来どおり `inferred=true`
（人確認前提）で、絞り込みの有無で意味論は変わらない。

### 6.4 患者キー列の確定: 明示宣言＋key_types 紐付け（人確認必須）

取り違え（別患者データ混入）は致命的。各表の患者キー列を:

- **施設別マッピングで表ごとに明示宣言**し、その `key_type`（mrn 等、`key_resolver` の
  `key_types.yaml` 参照）を紐付ける。
- LLM が論理名/notes から下書きし、**人が確認して確定**（ヒューリスティック自動判定に依存しない）。
- 索引と `key_resolver` の責務分離は維持（索引は「どの列が患者キーで、どの key_type か」を
  記録するのみ。値の名寄せは resolver）。

#### フォーマット指紋による候補補強（--from-live）

「列名だけでは mrn か dwh_serial か決まらない」ため、`draft-key-types --from-live` は
読み取り専用接続で各患者キー列を上限付き（既定 1,000 行）サンプリングし、フォーマット指紋
（桁数分布・ゼロ詰め率・文字クラス・ユニーク率、既知列との値集合重なり率）を計算して
名称類似スコアと合成する（`key_fingerprint.py`）。列名が無意味（例 `COL_017`）でも値の形で
候補を上位提示できる。**最終確定は引き続き人手**（自動 confirm しない）。

PHI 保護: 生値は保持しない（指紋計算後に破棄）。重なり率はソルト付きハッシュ集合で計算し、
成果物・ログのいずれにも生値を出さない。CLI:
`kawasemi-indexer draft-key-types [--from-live] [--dsn DSN] [--sample-limit N] [--out JSONL]`。

## 7. drift（スキーマ変化）と再構築

スキーマは変化し（列追加/改名/削除）、Excel も更新され、索引は陳腐化する。一方で
層 2（確認済知識層）は再構築のたびに失いたくない。

- **確認済知識層を生ソースから分離して永続**する。
- 再構築時は `information_schema` を **再取得**し、知識層（マッピング/補間/表役割/
  キー紐付け/コード辞書）を **再適用**する。
- 前回スナップショットと比較し、**新規/変更/消失カラムだけを drift 差分として人に提示**して
  再確認を依頼する（確認コストを償却）。
- embedding モデル / schema_version の不一致は既存 `build_info` の互換性チェックで
  `is_searchable=false` に縮退（store 既存挙動）。

## 8. 問い合わせ（クエリ）インターフェース

NL（設問テキスト・注釈等のコンテキスト）を受け、`ResourceIndexStore.search` を再利用して
**候補（テーブル/カラム）を top-k でランキング**して返す（embedding 類似度、BM25 フォールバック）。
返却にはカラムのメタ（physical/logical/type/notes/patient_key/key_type/inferred/コード辞書）を含め、
利用側が SQL を組み立てられる材料を提供する。SQL 生成・JOIN は indexer の責務外。

## 9. 成果物と実装

1. 本仕様書（docs/kawasemi_indexer_spec.md）。
2. **ingestion モジュール**: `indexer/kawasemi_indexer/ingestion/`
   | ファイル | 役割 | 状態 |
   |---|---|---|
   | `models.py` | 全データモデル（生ソース/知識層/契約/drift） | 実装済 |
   | `knowledge.py` | 確認済知識層 YAML の永続 | 実装済 |
   | `info_schema.py` | reflection（メタのみ）＋ master code 読取 | 実装済 |
   | `tabular.py` | Excel/CSV ＋ 施設別マッピング ＋ 検品正規化 | 実装済 |
   | `lint.py` | 定義書検品（全角混入・区切り誤り等の検出／qaレポート） | 実装済 |
   | `pdf_doc.py` | 紙・スキャンPDF定義書 → 正準CSV（テキスト層/vision OCR） | 実装済 |
   | `merge.py` | 骨格＋補完 → `column_index.jsonl` / `value_dictionary.jsonl` | 実装済 |
   | `drift.py` | スナップショット比較・差分 | 実装済 |
   | `pipeline.py` | オーケストレーション | 実装済 |
   | （LLM ドラフタ） | マッピング/補間/役割/患者キーの初回下書き | **後続** |

   テスト: `indexer/tests/test_ingestion.py`（一時 sqlite で契約適合・PHI 境界・
   補完欠落計上・棄却・マスタ取込・drift・builder 消費を検証）。

### CLI

```
kawasemi-indexer ingest <knowledge.yaml> [--out DIR] [--dsn DSN] [--schema NAME] [--fix] [--force]
kawasemi-indexer lint   <knowledge.yaml> [--out DIR] [--fix]
```

確認済知識層 YAML から `column_index.jsonl` 一式を生成する。出力後、既存
`kawasemi-indexer build --db-source <DIR>` が embedding 索引を作る。

### 紙・スキャンPDF定義書の取り込み

定義書が紙（スキャンPDF）しか無い施設のため、`ingestion/pdf_doc.py` が PDF から
カラム定義（table_id/physical_name/logical_name/notes）を復元し、既存の CSV 定義書と
同じ正準列を持つ CSV へ書き出す。人が確認・修正してから通常の `--doc` として
`draft-knowledge` / `ingest` へ合流させる（OCR/LLM 由来は confirmed 化しない＝反捏造）。

- テキスト層あり: `pypdf` 抽出 → LLM で表構造復元。
- スキャン画像: `pypdfium2` でページ画像化 → Ollama vision で OCR＋構造復元（新規依存なし）。
- ページ別に処理結果（text/ocr/failed）を記録し、**OCR 不能ページを黙って落とさない**。
- 患者データ誤投入ガード: 物理名が「値」に見える（長い数字列・日付）割合が高い、または
  生年月日らしき記述を検出したら警告し、CLI は非ゼロ終了（要人確認）。
- CLI: `kawasemi-indexer extract-pdf-doc <pdf> --out <csv> [--vision-model M] [--no-ocr]`。

### 定義書の検品（lint）

院内の定義書には機械処理を阻む不備（全角の混入・区切りの誤り・表IDのコピペ誤りなど）が
よくある。`ingestion/lint.py` がこれを類型ごとのルールにして検出し、確度別レポート
（`<out>/qa/qa_report.md`＝人向け、`qa_report.jsonl`＝機械向け）を出力する。

検出ルール（初期セット。対応クラスは不備の類型 A〜F の記号）:

| ルール | 重大度 | 自動修正 | 対応クラス |
|---|---|---|---|
| `fullwidth`（物理識別子への全角混入） | error | 可（NFKC） | A |
| `separator`（db_qualified に区切り '.' 無し） | error | 不可 | B |
| `table_id_reuse`（同一表IDが別表に再利用） | error | 不可 | C |
| `create_prefix`（'CREATE_' 接頭辞の紛れ込み） | warning | 可 | E |
| `empty_required`（必須列空欄で行除外） | warning | 不可 | F |
| `logical_missing`（論理名空。論理名補間の対象） | info | — | — |

- `ingest` は既定で検品を実行し、**error があれば停止**（`qa_report.md` 参照を促す）。`--force` で続行。
- `--fix` は物理識別子を NFKC 正規化して取り込み（全角→半角）、適用内容を provenance として
  `qa_report.jsonl` に記録する（`read_tabular(normalize=True)`。冪等）。
- JOIN 句解析は tabular アダプタの対象外のため、
  「結合条件数とJOIN句数の不一致」は本レイヤの検出対象外（別レイヤの課題）。

### 継ぎ目の解消状況

- ✅ **宣言患者キーの builder 採用（実装済）**: `build_db_units` は `resolve_patient_key`
  で `is_patient_key`/`key_type` 宣言を最優先し、無い場合のみ `infer_patient_key`
  ヒューリスティックへフォールバック。payload に `key_type` も付与。
- ✅ **コード辞書の畳み込み（実装済, §6.2）**: 知識層の `value_domains`（FK列→マスタ表の
  値ドメイン注釈。JOIN ではない）で、マスタの code→label を当該 FK 列の `search_text` に
  畳み込み（「男性」で当該列が surface）、`payload['value_dict']` に候補メタとして格納。
  巨大コード表は上限で切り詰め。`fold_value_dictionary` が `build_index` 内で適用。
- ✅ **LLM 下書きドラフタ（実装済, §5/§6）**: `KnowledgeDrafter`（`ingestion/drafter.py`）が
  information_schema＋定義書ヘッダから、マッピング/表役割＋master列/患者キー＋key_type/
  論理名補間（inferred）の **下書き** を `llm_json.chat_json` で生成し、value_domains は
  名称ヒューリスティックで下書く。出力はそのまま使わず人が確認・修正して確定（draft→confirmed）。
  ガード: 実在しないヘッダ/列、未知 key_type は弾く（捏造防止）。患者データ行は見ない。
  client 注入可でテストは Ollama 非依存。CLI: `kawasemi-indexer draft-knowledge
  --connection <id> --db-group <g> --out <knowledge.yaml> [--doc <file> ...] [--dsn] [--schema]`。
  → 人が確認後 `kawasemi-indexer ingest` へ。
  コスト管理: 論理名補間は既定で定義書未カバー列を全件 LLM へ投げる（`_CHUNK=8` 区切り）。
  大規模スキーマ（数千〜数万列）では `--only-missing` / `--resume` / `--enrich-tables` /
  `--enrich-limit` / `--near` / `--dry-run-enrich` で対象を絞れる（§6.3「補間対象の絞り込み」）。
  失敗時はクリーン失敗（部分 YAML を残さない）＝人が再実行する前提。

## 10. 確定した設計判断

| # | 論点 | 決定 |
|---|---|---|
| 1 | 位置づけ | 索引ビルドを WebUI から独立したツールとして標準化（NL→SQL 生成は範囲外） |
| 2 | 正規化先の契約 | `column_index.jsonl` を公式契約に昇格 |
| 3 | 対応ソース | Excel/CSV ＋ information_schema |
| 4 | 2 系統の関係 | information_schema を骨格真実 → Excel で意味補完（ズレは定義書棄却） |
| 5 | 出力境界 | 候補（テーブル/カラム）のランキングまで（リレーション不要） |
| 6 | Excel レイアウト解釈 | 施設別マッピング設定（LLM 下書き → 人確認） |
| 7 | 補完欠落カラム | LLM で論理名補間（`inferred` 印・人確認）、実在カラムは隠さない |
| 8 | 患者キー確定 | マッピングで明示宣言＋key_types 紐付け（LLM 下書き → 人確認） |
| 9 | PHI 境界 | メタのみ。実データ行は見ない |
| 10 | マスタ取込 | 表役割を明示宣言（LLM 下書き → 人確認）、master のみ code→label を取込 |
| 11 | drift/再構築 | 確認済知識層を分離・再適用＋drift 差分を人に提示 |
| 12 | 成果物 | 仕様書＋ingestion モジュール実装 |
