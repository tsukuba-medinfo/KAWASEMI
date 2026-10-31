"""取り込みレイヤのデータモデル（生ソース・確認済知識層・導出契約・drift）。

公式契約 ``ColumnRecord`` のフィールドは ``column_index.jsonl`` の1行と一致させる
（builder._read_column_index / build_db_units が消費する）。契約を壊さないため、
取り込み固有の追加情報（is_patient_key / key_type / inferred 等）は別フィールドとして
持ち、現 builder は無視する（将来の取り込み側拡張で参照）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 表役割。master のみ内容（code→label）取込可、patient_data はメタのみ。
TableRole = Literal["master", "patient_data"]

# 契約フィールド（column_index.jsonl の1行が必ず持つキー）
CONTRACT_FIELDS = (
    "table_id",
    "db_qualified",
    "table_logical",
    "db_group",
    "physical_name",
    "logical_name",
    "type_raw",
    "type_base",
    "pk",
    "notes",
)


# --------------------------------------------------------------------------- #
# 層1: 生ソース（information_schema スナップショット）
# --------------------------------------------------------------------------- #


class ColumnSnapshot(BaseModel):
    """information_schema 由来の1カラム（物理構造の真実。メタのみ）。"""

    physical_name: str
    type_raw: str
    type_base: str
    pk: bool = False


class TableSnapshot(BaseModel):
    """1テーブルの物理スナップショット。"""

    table_id: str = Field(description="テーブル物理名（reflection 由来）")
    db_qualified: str = Field(description="schema.table 形式の修飾名")
    columns: list[ColumnSnapshot] = Field(default_factory=list)


class SchemaSnapshot(BaseModel):
    """接続1つの information_schema スナップショット（drift 比較の基準）。"""

    connection_id: str
    db_group: str
    schema_name: str | None = Field(
        default=None,
        description="reflection したスキーマ（`--schema` か接続の既定スキーマ。SQLite は main）。"
        "payload.physical の既定のスキーマになる",
    )
    tables: list[TableSnapshot] = Field(default_factory=list)

    def table(self, table_id: str) -> TableSnapshot | None:
        for t in self.tables:
            if t.table_id == table_id:
                return t
        return None


# --------------------------------------------------------------------------- #
# 層2: 確認済知識層（LLM 下書き → 人確認 → 永続）
# --------------------------------------------------------------------------- #


class TabularSource(BaseModel):
    """1つの Excel/CSV 定義書と、その施設別マッピング。"""

    path: str = Field(description="定義書ファイル（.xlsx / .csv）")
    sheet: str | None = Field(default=None, description="シート名（未指定で先頭。CSVは無視）")
    header_row: int = Field(default=1, description="ヘッダ行（1始まり）")
    columns: dict[str, str] = Field(
        default_factory=dict,
        description="canonicalフィールド→定義書のヘッダ名 のマッピング"
        "（table_id/table_logical/physical_name/logical_name/notes）",
    )
    table_match: Literal["table_id", "db_qualified"] = Field(
        default="table_id",
        description="定義書の表識別子を info_schema のどのキーへ突き合わせるか",
    )


class PatientKeyBinding(BaseModel):
    """1テーブルの患者キー列宣言（取り違え防止・人確認必須）。"""

    physical_name: str
    key_type: str | None = Field(default=None, description="key_types.yaml の key_type（mrn等）")


class MasterTableSpec(BaseModel):
    """マスタ表のコード辞書取込指定（master のみ内容読取可）。"""

    code_column: str
    label_column: str


class Enrichment(BaseModel):
    """論理名・メモの補間（Excel欠落分。LLM下書きは inferred=true）。"""

    logical_name: str | None = None
    notes: str | None = None
    inferred: bool = True
    confirmed: bool = False


class DocFilter(BaseModel):
    """ヘッダ表の有効行の条件（`IS_DELETED = 0` など）。"""

    column: str
    equals: str | int


class DocOrder(BaseModel):
    """文書の既定の並び順。どれを採るか（最新/初回/全部）は利用側の DSL が決める。"""

    column: str
    dir: Literal["asc", "desc"] = "asc"
    on_header: bool = Field(default=False, description="真ならヘッダ表の列、偽なら本文の表の列")


class DocHeaderSpec(BaseModel):
    """文書ヘッダ表（実行先DBの名前）。文書種別の単位の `access_path` を組む材料。"""

    header_schema: str
    header_table: str
    join_on: list[str] = Field(description="本文とヘッダを結ぶ列（両表で同名）")
    type_column: str = Field(description="文書種別のコードが入るヘッダの列")
    filters: list[DocFilter] = Field(default_factory=list, description="ヘッダの有効行の条件")
    order_by: list[DocOrder] = Field(default_factory=list)
    body_patient_key: str | None = Field(
        default=None,
        description="本文の表の患者キー列（文書種別の単位の payload.patient_key）。無ければ付けない"
        "（KAWASEMI は患者キー列の無い単位を組み立てず不足として返す）",
    )
    body_key_type: str | None = Field(
        default=None,
        description="その患者キーの key_type（key_types.yaml の id）。宣言したときだけ付ける",
    )


class KnowledgeLayer(BaseModel):
    """接続1つの確認済知識層（YAML 永続）。

    生ソースから分離して永続し、再構築のたびに再適用する（drift 差分のみ人へ）。
    """

    connection_id: str
    db_group: str
    tabular_sources: list[TabularSource] = Field(default_factory=list)
    table_roles: dict[str, TableRole] = Field(default_factory=dict)
    patient_keys: dict[str, PatientKeyBinding] = Field(default_factory=dict)
    patient_unscoped_tables: list[str] = Field(
        default_factory=list,
        description="患者で絞らない表（マスタ参照・施設固定値）の table_id。**人（施設）が確認した"
        "ものだけ**を書く。表論理名に「マスタ」を含む等からは推測しない"
        "——患者データの表でキーの推定に失敗したとき、全患者の行を返す SQL ができるため。"
        "patient_keys と同時に宣言された表は矛盾なので宣言を採らない（警告）。",
    )
    master_tables: dict[str, MasterTableSpec] = Field(default_factory=dict)
    physical_schemas: list[str] = Field(
        default_factory=list,
        description="実行先DBでこの接続の表が置かれるスキーマ（payload.physical.schema）。"
        "空なら reflection したスキーマを使う。1つならその名前を全表に付ける"
        "（reflection に使ったレプリカとスキーマ名が違うとき）。複数なら、reflection したスキーマが"
        "候補に在ればそれ（表はそこに実在する）、無ければ実行先の表一覧（ingest の `--catalog`、"
        "無ければ DSN の各候補スキーマの表名）で一意に決まる表だけに付ける。決まらない表には付けない"
        "（推測しない）。候補の表名を1つも列挙できなければ ingest は止まる（黙って全表を落とさない）。",
    )
    doc_header: DocHeaderSpec | None = Field(
        default=None,
        description="文書の本文を種別で絞るためのヘッダ表（文書種別の単位の access_path）。"
        "本文の表が種別の列を持たない構成では、ヘッダ表を結合して種別と有効行で絞る。",
    )
    value_domains: dict[str, str] = Field(
        default_factory=dict,
        description="値ドメイン注釈。キー='table_id.physical_name'(コード値が入る列)、"
        "値=master の table_id。当該列の値域がどのマスタかを示す（JOINではなく列の値域メタ）。"
        "宣言するとマスタの code→label がその列の search_text/候補メタに畳み込まれる。",
    )
    enrichments: dict[str, Enrichment] = Field(
        default_factory=dict,
        description="キー='db_qualified.physical_name'、値=補間内容",
    )

    def role_of(self, table_id: str) -> TableRole | None:
        return self.table_roles.get(table_id)


# --------------------------------------------------------------------------- #
# 層3: 導出（契約 column_index.jsonl の1行）
# --------------------------------------------------------------------------- #


class ColumnRecord(BaseModel):
    """column_index.jsonl の1行（公式契約）。

    契約フィールド（CONTRACT_FIELDS）＋取り込み固有メタを持つ。dump_row() は両方を
    書き出すが、現 builder は契約フィールドのみ .get() で参照するためメタ混在は無害。
    """

    # --- 契約フィールド ---
    table_id: str
    db_qualified: str
    table_logical: str | None = None
    db_group: str | None = None
    physical_name: str
    logical_name: str | None = None
    type_raw: str | None = None
    type_base: str | None = None
    pk: bool = False
    notes: str | None = None
    # --- 取り込み固有メタ（現 builder は無視。将来拡張・監査用） ---
    is_patient_key: bool = False
    key_type: str | None = None
    # 「患者で絞らない表」の宣言。**`False` のときだけ意味を持つ**（それ以外は None）。
    patient_scoped: bool | None = None
    # 実行用の識別子 `{"schema", "table", "column"}`。決まらなければ None
    physical: dict[str, str] | None = None
    table_role: TableRole | None = None
    logical_name_inferred: bool = False
    enrichment_confirmed: bool = True

    def dump_row(self) -> dict[str, object]:
        """column_index.jsonl へ書き出す1行（契約＋メタ）。"""
        return self.model_dump()


class CodeEntry(BaseModel):
    """value_dictionary.jsonl の1行（マスタの code→label）。"""

    connection_id: str
    db_qualified: str
    physical_name: str | None = Field(
        default=None, description="このコードが値として入る参照元カラム（任意）"
    )
    code: str
    label: str


# --------------------------------------------------------------------------- #
# drift / 取り込み結果
# --------------------------------------------------------------------------- #


class DriftItem(BaseModel):
    table_id: str
    physical_name: str
    change: Literal["added", "removed", "type_changed"]
    detail: str | None = None
    needs_enrichment: bool = False


class DriftReport(BaseModel):
    """前回スナップショットとの差分（人へ提示して再確認を依頼）。"""

    connection_id: str
    items: list[DriftItem] = Field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.items)


class IngestResult(BaseModel):
    """1回の取り込みの結果サマリ。"""

    connection_id: str
    column_count: int = 0
    table_count: int = 0
    code_entry_count: int = 0
    unenriched_count: int = Field(default=0, description="論理名なしカラム数（検索品質に影響）")
    discarded_doc_rows: int = Field(
        default=0, description="info_schemaに無く棄却した定義書行数（古い定義）"
    )
    drift: DriftReport | None = None
