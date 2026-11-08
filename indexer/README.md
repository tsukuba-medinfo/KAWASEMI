# kawasemi-indexer

> KAWASEMI = **K**nowledge-**A**ided **W**orkflow for **A**I-assisted **S**tructured **E**HR **M**apping & **I**nput

KAWASEMI のリソース索引を構築する **バックエンド前処理ツール**。異種の DB 定義書
（Excel/CSV）と生きDBの `information_schema` を取り込み、自然言語から関連カラム候補を
検索するための索引成果物を生成する。

このリポジトリの `indexer/` に置かれた独立パッケージ（`kawasemi_indexer`）で、
KAWASEMI 本体（`kawasemi/`）とはコードを共有しない。SE がサーバ側で事前に実行する
ツールであり、ログインして使う WebUI とは用途・ネットワークが別。両者は**索引ファイルの
授受**で協働する（接点は [`CONTRACT.md`](../CONTRACT.md)）。

## 位置づけ

indexer の責務は **候補（テーブル/カラム）のランキングまで**。SQL 本文の組み立ては
利用側（KAWASEMI）に委ねる（反捏造設計）。索引にテーブル間リレーション（FK/JOIN）は
持たせない。詳細仕様は [`docs/kawasemi_indexer_spec.md`](./docs/kawasemi_indexer_spec.md)。

## 安全制約（医療ドメイン）

- **PHI 不混入**: `information_schema` はスキーマ定義のみ抽出。患者データ行は見ない。
- **例外**: master（参照表/コード表）の内容のみ、人確認のうえ `code→label` を取り込む。
- 患者キー列・key_type の確定は **人確認必須**（ヒューリスティック自動判定に依存しない）。

## インストール

indexer はリポジトリ直下の `pyproject.toml` で uv workspace のメンバー
（`[tool.uv.workspace] members = ["indexer"]`）として宣言されている。
**リポジトリ直下で**次を実行すると、本体と indexer（`kawasemi-indexer` コマンドを含む）が
同じ仮想環境に入る。

```bash
uv sync --all-packages --all-groups --all-extras
```

施設の外部DBのドライバ（`postgres` / `oracle`）や索引診断 WebUI（`webui`）は indexer 側の
extras である。`--all-extras` だけではワークスペースの他メンバーの extras までは入らないので、
`--all-packages` を付けている。

設定は本体と共通の `.env` で行う（リポジトリ直下の [`.env.example`](../.env.example) を
`.env` にコピーする）。indexer は `.env` をカレントディレクトリから読むので、リポジトリ直下で実行する。

## 使い方

```bash
# 1) information_schema＋定義書から知識層 YAML を下書き（LLM）→ 人が確認・修正
kawasemi-indexer draft-knowledge --connection <id> --db-group <g> --out knowledge.yaml [--doc FILE ...]

# 2) 確認済み知識層 YAML から column_index.jsonl 一式を生成
#    実行用の識別子 physical（schema/table/column）も付く。スキーマは知識層の physical_schemas、
#    無ければ reflection したスキーマ（CONTRACT.md「kind=db の payload」）
#    physical_schemas が複数で reflection したスキーマが候補に無いときは --catalog で実行先の表一覧を渡す
#    （無ければ DSN から列挙し、列挙できなければ止まる）
kawasemi-indexer ingest knowledge.yaml --out var/resource_index [--catalog tables.txt]

# 3) embedding 索引（embeddings.npy 等）を生成
kawasemi-indexer build --db-source site/db_definition --out var/resource_index

# 3') 定義書から作った既存の索引へ physical を追記（実行先DBの表一覧と知識層の physical_schemas から）
#     文書種別の単位（doc_type_code）には知識層の doc_header から access_path も足す。経路の無い文書種別の
#     単位には physical を付けない（全文書を返す SQL になるため）
kawasemi-indexer enrich-physical --base <索引> --out <書き出し先> --knowledge k1.yaml k2.yaml --catalog tables.txt

# 4) クエリ側成果物（任意）: 標準コード変換テーブル＋参照リンク（列→値域マスタ）
kawasemi-indexer build-synonyms --index-dir var/resource_index
kawasemi-indexer draft-ref-links --index-dir var/resource_index  # 下書き→人が status を confirmed に
#    リンク先のマスタ表は施設の site/indexer_rules.yaml の ref_link_masters から読む（下記）

# 5) 索引の点検（NL → 候補ランキング。字面＋embedding＋参照リンクのハイブリッド）
kawasemi-indexer search "芽球割合" --index-dir var/resource_index

# 6) 検索精度の評価（recall@k / MRR。embedding/hybrid/rewrite/ref の4モード比較）
kawasemi-indexer eval --gold site/eval/gold.jsonl --index-dir var/resource_index
```

生成された `var/resource_index/` 一式を KAWASEMI 本体のクエリ側へ授受する。

### 施設の索引規則（`site/indexer_rules.yaml`）

施設の表名・接続名に依る判断はコードに持たず、`<SITE_DIR>/indexer_rules.yaml`
（設定 `INDEXER_RULES_FILE` で変更可）から読む。架空の例が
[`site.example/indexer_rules.yaml`](../site.example/indexer_rules.yaml) にある。
**ファイルが無ければ空の規則**として動き、エラーにしない。

- `ref_link_masters`: 参照リンクの概念（`byomei` = 病名、`exam` = 検体検査項目）→ 値域マスタの表
  （`connection_id` / `db_qualified`）。書いた概念だけ `draft-ref-links` が下書きする。
  無ければ参照リンクは下書きしない。
- `patient_key.additional_logical_names`: 既定の論理名（`患者番号`・`患者ID` など）に足す、施設に
  固有の論理名。既定のどれも無い表でだけ、書いた順に採る。
- `patient_key.conditional_logical_names`: 曖昧な論理名（`ID` など）→ 患者キーと認める
  `connection_id` の一覧（PK の列に限る）。
- `patient_key.ambiguous_tables`: 論理名の完全一致でも患者キーを付けない表（`db_qualified`）。

### 項目単位の追記（ライブラリ）

何の値かが列名ではなく同じ表の項目コード列の値で決まる表（経過表など縦持ちの表）は、
1項目を `db:<表>.<列>#<項目コード>` の1単位にして既存の索引へ**追記**する
（CONTRACT.md「項目コードで行を選ぶ単位」）。入口は `kawasemi_indexer.item_units` で、
embedder は引数で受ける（テストや E2E では決定的な embedder を注入する）。

```python
from kawasemi_indexer.item_units import ItemSpec, append_item_units, build_item_units, read_db_units

specs = [
    ItemSpec(
        db_qualified="DEMO.VITAL_LOG", value_column="VALUE_NUM",  # 列の単位 db:DEMO.VITAL_LOG.VALUE_NUM
        item_column="ITEM_CODE", item_code="X001", item_name="体温",
        item_source="dwh.ITEM_MASTER",                     # 項目コードの出どころ（項目マスタ）
        row_filters={"IS_DELETED": "0"}, order_by=(("RECORDED_AT", "asc"),),
        measurements=["T"], units=["度"], patients=100, numeric_share=0.9,
    ),
]
units = build_item_units(specs, read_db_units("var/resource_index"))
append_item_units("var/resource_index", "var/resource_index_items", units, embedder)
```

既存のベクトルは動かさず、新しい単位のぶんだけ `embeddings.npy` の db ブロックの末尾へ
挿し込む。既に索引に在る `unit_id` は飛ばす。項目コード列（`item_column`）と項目マスタ
（`item_source`）は施設ごとに違うので既定値を持たない。施設の表名や回答形式を知っているのは
呼び出し側で、それを読んで `ItemSpec` に写す。

## ライセンス

Apache License 2.0（[`LICENSE`](../LICENSE)）。
