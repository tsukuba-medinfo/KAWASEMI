# DB定義書コーパス仕様（ステップ1役割2「AIによる案の作成」の検索基盤）

最終更新日: 2026-05-31

## 目的と位置づけ

ステップ1・役割2（データ所在 = `SystemLocation`）の入力を支援する
「AIによる案の作成」機能は、施設のDB定義書コーパスを根拠に、各Questionnaire項目の
`source_category` と `location`（接続先・表・カラム）を下書きする。

このドキュメントは、**コーパス本体がコミットされない**（施設固有のため `.gitignore` で
`site/db_definition/` を除外）状況でも、データ構造とKAWASEMIの読み取り仕様を
リポジトリに残すために存在する。施設はディレクトリを差し替えるだけで利用できる。

関連: [AI支援入力設計の2役割化](./ai_design_two_roles.md)

## 配置とパス

- 既定パス: `site/db_definition/`（`core/config.py` の `db_definition_dir`、
  環境変数 `DB_DEFINITION_DIR` で上書き可）。
- コーパス未配置の場合、AI案作成は「候補ゼロ」として安全に縮退する（エラーにしない）。
- データ本体は施設固有のため **コミットしない**（`.gitignore` 済）。

## ディレクトリ構成（例）

KAWASEMI が読むのは `L2/column_index.jsonl` の1ファイルだけである
（`kawasemi/ai_input/db_definition.py` の `DbDefinitionIndex.load`）。
施設の DB 定義書を `kawasemi-indexer ingest` で正規化すると、このファイルが生成される。
同じディレクトリに他のファイルがあっても読まれない。

```
site/db_definition/
└── L2/
    └── column_index.jsonl   # 全カラムを平坦化した索引（1行=1カラム）。検索対象
```

検索の索引（BM25）は KAWASEMI がこのファイルから起動時に自前で組み立てる。
外部で作った索引ファイル（pickle 等）は、内部形式への密結合・ロード時の安全性・再現性の
理由で読まない。

## `L2/column_index.jsonl` のスキーマ（v1が読む唯一のファイル）

1行1カラムのJSON。KAWASEMIが参照するフィールド:

```json
{
  "table_id": "LAB",
  "db_qualified": "DEMO.LAB",
  "table_logical": "検査結果",
  "db_group": "DEMO_DB",
  "physical_name": "BLAST_PCT",
  "logical_name": "芽球割合",
  "type_raw": "NUMBER(5,2)",
  "pk": false,
  "notes": "末梢血の芽球割合(%)"
}
```

| フィールド | 用途 |
|------------|------|
| `db_group` | 所属DB群。`location` 文言とログに使用 |
| `db_qualified` | スキーマ修飾テーブル名。`location` 文言に使用 |
| `table_logical` | テーブル論理名。検索ドキュメント＋`location`文言 |
| `physical_name` | カラム物理名。検索ドキュメント＋`location`文言 |
| `logical_name` | カラム論理名。検索ドキュメント＋`location`文言 |
| `notes` | カラムメモ。検索ドキュメントに含める |
| `type_raw` | 型。LLM候補表示に使用 |
| `pk` | 主キーか。LLM候補表示に使用 |

未知フィールドは無視される。上記が欠けても可能な範囲で動作する。

## 検索とAI案生成のフロー

1. **検索（`db_definition.py`）**: 各項目の `item_text` ＋ 役割1 `annotation` を
   クエリに、`column_index.jsonl` 全カラムへのBM25で上位 k 件（既定8）を取得。
   - トークナイザ: NFKC/lower のうえ「ascii-token ＋ 非ascii文字の char-bigram」。
   - 索引はプロセス内シングルトンに遅延ロード（コーパスは頻繁に変わらない前提。
     差し替え後はプロセス再起動、またはテストでは `reset_index_cache()`）。
2. **選定（`location_drafter.py`）**: 候補リストを Ollama に渡し、各項目について
   `chosen_cand_id`（該当なしは null）と `source_category` を構造化出力で得る。
   - LLMは **候補IDを選ぶだけ**。所在文字列は KAWASEMI が定型文で組み立てる
     （存在しないカラム名の捏造を構造的に防止）:
     `DB: {db_group} / 表: {db_qualified}（{table_logical}）/ カラム: {physical_name}（{logical_name}）`
   - 候補ゼロの項目はLLMに渡さずスキップ（件数のみフィードバック）。
3. **差し込み**: 案はフォームへJSで差し込まれる。**保存はされない**。担当者が確認・修正のうえ
   既存の保存ボタンで確定する（最終成果物の作者は常に人）。

## source_category の決め方

v1の検索ソースは `column_index.jsonl`（= 院内DB = `local_db`）のみ。LLMは候補の有無と
項目意味から5カテゴリ（`fhir`/`local_db`/`external_db`/`llm`/`manual`）を判断する。
候補を選んだのに `manual` を返した場合は `local_db` に補正する。
将来、FHIR定義書等を別ソースとして追加すれば、カテゴリ判断の根拠を拡張できる。

## テスト

施設の定義書に依存しないよう、架空の最小フィクスチャ
`tests/fixtures/db_definition/L2/column_index.jsonl` と Ollama モックで
`tests/ai_input/test_location_drafter.py` が検索→選定→組み立て→候補ゼロ集計を検証する。
