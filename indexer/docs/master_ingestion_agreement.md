# マスタ取り込み 取り決め（優先マスタ一覧と CSV フォーマット）

最終更新: 2026-06-20
ステータス: チーム取り決め（draft）。索引仕様 §6.2 / `docs/kawasemi_indexer_debug_spec.md` /
`CONTRACT.md`（`schema_version=2`, `kind=master`）と整合。

## 1. 目的

NL（臨床的コンテキスト）から「データの場所」を引けるようにするには、**マスタの中身
（code→label）** を索引に入れる必要がある。カラムのメタ情報（物理名・論理名）だけでは
「急性骨髄性白血病」「アスピリン」といった臨床語が対応コード列に結びつかないため。

大規模臨床マスタ（病名/検査/薬剤等）は **1エントリ=1検索ユニット（`kind=master`）** として
索引化する（`builder.build_master_units` → `master_index.jsonl`）。小区分（性別・血液型等）は
従来どおり FK 列へ畳み込む（`fold_value_dictionary`）。本書は**どのマスタを優先するか**と
**提供 CSV のフォーマット**を取り決める。

## 2. 優先マスタ一覧（取り決め）

「どのマスタを優先するか」と「取り込み方式」を定める。**接続ID・実テーブル名（db_qualified）・
実列名は施設特有情報のため本書には載せない**。実マッピングは施設側の抽出メモ
（例: `site/master_extraction_sql.md`。`site/` は **git 管理外**）で管理する。

| 優先 | マスタ | 規模 | 取り込み方式 |
|---|---|---|---|
| **P0** | 病名（診断） | 大 | `kind=master` |
| **P0** | 検査項目 | 大 | `kind=master` |
| **P0** | 薬剤（医薬品） | 大 | `kind=master` |
| **P1** | 診療行為・手術/処置 | 大 | `kind=master` |
| **P1** | 診療科 | 中 | `kind=master` |
| **P1** | DPC 診断群分類 | 中 | `kind=master` |
| **P2** | 転帰区分 | 小〜中 | `kind=master` |
| **P2** | 病棟/病室 | 中 | `kind=master` |
| **P2** | 細菌/微生物・病理組織診断 | 中 | `kind=master` |
| **P3** | 性別・血液型・入退院区分・保険種別・単位 | 小 | **FK 畳み込み**（`physical_name` 付き）|

- **P0 を最優先**で揃える（病名/検査/薬剤）。次に「コードで患者表を絞る」P1（診療行為/診療科/DPC）。
- 小区分（P3）は `kind=master` ではなく **FK 列への畳み込み**にする（後述 §3.3）。
- 各マスタの実 connection_id / db_qualified / code・label 列は施設側の抽出メモ（上記）を参照。

## 3. CSV フォーマット（取り決め）

### 3.1 共通規約

- 文字コード **UTF-8**（BOM 可）。1 行目は**ヘッダ必須**。区切りは `,`（カンマ）。
- **1 ファイル 1 マスタ**を推奨。ファイル名規約: `master_<種別>_<connection>.csv`
  （例 `master_byomei_<conn>.csv`, `master_exam_<conn>.csv`）。
- **PHI 厳禁**: マスタ（参照表）かつ患者キー無しの表のみ。患者個人データ（氏名・ID・日付等の
  行データ）は**絶対に入れない**。誤混入は PHI 漏洩に直結する（索引＝地図の不変条件）。

### 3.2 大規模マスタ（`kind=master`）の列

| 列名 | 必須 | 内容 |
|---|---|---|
| `connection_id` | ✓ | 接続ID（`connections.yaml` と一致）|
| `db_qualified` | ✓ | マスタ表の修飾名（`schema.table`）|
| `code` | ✓ | コード値（患者表の絞り込みに使う実値。例 `C920`）|
| `label` | ✓ | 名称（NL 検索の主対象。例 `急性骨髄性白血病`）|
| `aliases` | 任意 | 別名・略語・読み。`;`（セミコロン）区切り（例 `AML;急性骨髄芽球性白血病`）|

例（`master_byomei_<conn>.csv`。`<conn>`/`<病名マスタ表>` は施設の実値に置換）:
```csv
connection_id,db_qualified,code,label,aliases
<conn>,<病名マスタ表>,C920,急性骨髄性白血病,AML;急性骨髄芽球性白血病
<conn>,<病名マスタ表>,C910,急性リンパ性白血病,ALL
<conn>,<病名マスタ表>,E1190,2型糖尿病,DM;糖尿病(2型)
```

- `aliases` は `search_text` に取り込まれ、略語（AML 等）でも hit するようになる。
- `code` は索引には**コードとして**入り、利用側が `WHERE <code列>='C920'` を組む材料になる。

### 3.3 小区分（FK 畳み込み）の列

性別・血液型など数〜数百件の小区分は、`kind=master` にせず**対象 FK 列**へ畳み込む。
上記に加えて `physical_name`（畳み込み先の列物理名）を付ける:

| 列名 | 必須 | 内容 |
|---|---|---|
| `connection_id` / `db_qualified` / `code` / `label` | ✓ | （§3.2 と同じ。`db_qualified` は **FK 列が属する患者表**）|
| `physical_name` | ✓ | 畳み込み先の FK 列物理名（例 `SEX_CD`）|

例（`master_sex_<conn>.csv`。`<conn>`/`<患者表>` は施設の実値に置換）:
```csv
connection_id,db_qualified,physical_name,code,label
<conn>,<患者表>,SEX_CD,1,男性
<conn>,<患者表>,SEX_CD,2,女性
```

## 4. 取り込み手順（CSV → 索引）

CSV を `value_dictionary.jsonl`（1行1エントリ）に変換し、`site/db_definition/` 直下に置いて
再ビルドする。`physical_name` 列が**無い**行は `kind=master` ユニットに、**有る**行は FK 畳み込みに
自動で振り分けられる。

```bash
# 1) CSV 群 → value_dictionary.jsonl（全 CSV を連結変換）
python - <<'PY'
import csv, json, glob
with open("site/db_definition/value_dictionary.jsonl", "w", encoding="utf-8") as out:
    for path in glob.glob("site/masters/*.csv"):
        with open(path, encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                rec = {k: v for k, v in row.items() if v not in (None, "")}
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
PY

# 2) 再ビルド（master_index.jsonl が生成される）
uv run kawasemi-indexer build --db-source site/db_definition --out var/resource_index
```

> 生DBレプリカがある場合は CSV の代わりに正式ルート（`draft-knowledge` →人確認→ `ingest`）でも
> 同じ `value_dictionary.jsonl` を生成できる（`info_schema.read_master_codes`。1表 `MASTER_CODE_LIMIT=5000` 件上限）。

## 5. 検証（debug コンソール）

```bash
uv run kawasemi-indexer debug --index-dir var/resource_index
```
- 起動ヘッダの `kind={... 'master': N}` でマスタ件数を確認。
- 臨床語（例「急性骨髄性白血病」）を入力 → `master / <マスタ表> / code=C920` が surface すれば成功。
- `:kind master` でマスタのみに絞って点検可能。

## 6. 既知の制約

- `kind=master` で 200 件畳込上限（`_VALUE_FOLD_LABEL_CAP`）の問題は解消（各エントリが独立ベクトル）。
- 生DB読取の `MASTER_CODE_LIMIT=5000`（1表上限）は大規模マスタで要調整（引上げ/ページング）。
- FK 逆引き（マスタユニット → それを参照する患者表の FK 列一覧）は未実装（v2 候補）。現状は
  マスタ表の場所＋code までを返す。
