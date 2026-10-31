"""実行用の識別子（db payload の `physical`）のテスト。

- ingest: SQLite の reflection（既定スキーマ `main`）から付く／知識層の明示が優先する
- 定義書と実行先でスキーマ名が違う知識層（接続ごとのスキーマ対応、生きDBなし）
- 既存の索引への追記（`enrich_physical`）が、接続ごとの知識層（`physical_schemas`）と実行先の
  表一覧から一意に決まる表だけに付けること。接続・スキーマ・表の名前はすべて架空。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest
import yaml

from kawasemi_indexer.builder import build_db_units
from kawasemi_indexer.cli import main as cli_main
from kawasemi_indexer.ingestion.knowledge import load_knowledge
from kawasemi_indexer.ingestion.merge import merge
from kawasemi_indexer.ingestion.models import (
    ColumnSnapshot,
    DocFilter,
    DocHeaderSpec,
    DocOrder,
    KnowledgeLayer,
    SchemaSnapshot,
    TableSnapshot,
)
from kawasemi_indexer.ingestion.pipeline import ingest_connection
from kawasemi_indexer.physical import (
    GroupConfig,
    doc_access_path,
    enrich_physical,
    load_groups,
    read_catalog,
    table_schemas,
)


def _rows(out_dir: Path) -> list[dict]:
    path = out_dir / "L2" / "column_index.jsonl"
    return [json.loads(ln) for ln in path.read_text("utf-8").splitlines() if ln.strip()]


@pytest.fixture
def sqlite_dsn(tmp_path: Path) -> str:
    db = tmp_path / "dwh.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE LAB (PT_ID TEXT PRIMARY KEY, WBC INTEGER);"
        "CREATE TABLE VITAL (PT_ID TEXT, BT NUMERIC);"
    )
    conn.close()
    return f"sqlite+aiosqlite:///{db}"


def _knowledge_file(tmp_path: Path, **extra: object) -> Path:
    data = {"connection_id": "dwh", "db_group": "dwh", **extra}
    path = tmp_path / "knowledge.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# ingest（reflection）
# --------------------------------------------------------------------------- #


def test_ingest_sqlite_uses_reflected_main_schema(tmp_path: Path, sqlite_dsn: str) -> None:
    out = tmp_path / "out"
    ingest_connection(_knowledge_file(tmp_path), out, dsn=sqlite_dsn)
    rows = _rows(out)
    assert {(r["db_qualified"], r["physical_name"]) for r in rows} == {
        ("LAB", "PT_ID"),
        ("LAB", "WBC"),
        ("VITAL", "PT_ID"),
        ("VITAL", "BT"),
    }
    for r in rows:
        assert r["physical"] == {
            "schema": "main",
            "table": r["table_id"],
            "column": r["physical_name"],
        }

    # build が db payload へ写す
    units = {u.unit_id: u.payload for u in build_db_units(rows)}
    assert units["db:VITAL.BT"]["physical"] == {"schema": "main", "table": "VITAL", "column": "BT"}


def test_ingest_explicit_single_schema_overrides_reflection(
    tmp_path: Path, sqlite_dsn: str
) -> None:
    """reflection に使ったレプリカとスキーマ名が違うときは、知識層の1つの名前を付ける。"""
    out = tmp_path / "out"
    ingest_connection(_knowledge_file(tmp_path, physical_schemas=["DWH"]), out, dsn=sqlite_dsn)
    assert {r["physical"]["schema"] for r in _rows(out)} == {"DWH"}


def test_ingest_reflected_schema_among_candidates_is_used(tmp_path: Path, sqlite_dsn: str) -> None:
    """候補が複数でも、reflection したスキーマ（`main`）が候補に在ればそれ。表はそこに実在する。"""
    out = tmp_path / "out"
    knowledge = _knowledge_file(tmp_path, physical_schemas=["EXEC_A", "main"])
    ingest_connection(knowledge, out, dsn=sqlite_dsn)
    assert {r["physical"]["schema"] for r in _rows(out)} == {"main"}


def test_ingest_multiple_schemas_without_listing_fails_clearly(
    tmp_path: Path, sqlite_dsn: str
) -> None:
    """reflection の DSN から候補スキーマを列挙できなければ、理由を言って止まる（落ちる・黙らない）。"""
    knowledge = _knowledge_file(tmp_path, physical_schemas=["EXEC_A", "EXEC_B"])
    with pytest.raises(ValueError, match="--catalog"):
        ingest_connection(knowledge, tmp_path / "out", dsn=sqlite_dsn)


def test_ingest_multiple_schemas_with_catalog_file(tmp_path: Path, sqlite_dsn: str) -> None:
    """実行先の表一覧を渡せば、そこで一意に決まる表だけに付ける。"""
    catalog = tmp_path / "tables.txt"
    catalog.write_text(
        "schema_name|table_name|row_count\n---|---|---\n"
        "EXEC_A|LAB|10\nEXEC_A|VITAL|3\nEXEC_B|VITAL|4\n",
        encoding="utf-8",
    )
    knowledge = _knowledge_file(tmp_path, physical_schemas=["EXEC_A", "EXEC_B"])
    out = tmp_path / "out"
    ingest_connection(knowledge, out, dsn=sqlite_dsn, catalog_path=catalog)
    got = {(r["table_id"], r["physical_name"]): r["physical"] for r in _rows(out)}
    assert got[("LAB", "WBC")] == {"schema": "EXEC_A", "table": "LAB", "column": "WBC"}
    assert got[("VITAL", "BT")] is None  # 2つのスキーマにある（推測しない）


def test_ingest_catalog_missing_a_candidate_fails(tmp_path: Path, sqlite_dsn: str) -> None:
    catalog = tmp_path / "tables.txt"
    catalog.write_text("EXEC_A|LAB|10\n", encoding="utf-8")
    knowledge = _knowledge_file(tmp_path, physical_schemas=["EXEC_A", "EXEC_B"])
    with pytest.raises(ValueError, match="EXEC_B"):
        ingest_connection(knowledge, tmp_path / "out", dsn=sqlite_dsn, catalog_path=catalog)


def test_table_schemas_rules() -> None:
    tables = ["A", "B", "C"]
    assert table_schemas(tables, [], reflected="main") == {"A": "main", "B": "main", "C": "main"}
    assert table_schemas(tables, [], reflected=None) == {"A": None, "B": None, "C": None}
    assert table_schemas(tables, ["X"], reflected="main") == {"A": "X", "B": "X", "C": "X"}
    catalog = {"A": {"S1"}, "B": {"S1", "S2"}, "C": {"OTHER"}}
    # A は一意、B は2つにある（推測しない）、C は候補に無い
    assert table_schemas(tables, ["S1", "S2"], reflected="main", catalog=catalog) == {
        "A": "S1",
        "B": None,
        "C": None,
    }
    # reflection したスキーマが候補に在れば、表はそこに実在する
    assert table_schemas(tables, ["S1", "S2"], reflected="S2", catalog=catalog) == {
        "A": "S2",
        "B": "S2",
        "C": "S2",
    }


def test_build_omits_physical_when_incomplete() -> None:
    """揃わない physical は payload にキーごと出さない（KAWASEMI が不足として拾う）。"""
    base = {"db_group": "g", "db_qualified": "T", "physical_name": "C", "table_id": "T"}
    rows = [
        {**base, "physical": {"schema": "", "table": "T", "column": "C"}},
        {**base, "physical_name": "D"},
    ]
    for u in build_db_units(rows):
        assert "physical" not in u.payload


# --------------------------------------------------------------------------- #
# 定義書と実行先でスキーマ名が違う（生きDBなし）
# --------------------------------------------------------------------------- #


def test_knowledge_maps_schema_over_reflection(tmp_path: Path) -> None:
    """定義書の名前（`DEMO.DEPT_VISIT`）と実行先のスキーマ（`DWH`）が違う接続。

    reflection は既定スキーマ（`dbo`）を返したが、実行先では `DWH` にある。
    知識層の `physical_schemas` が reflection より優先し、表名・列名は reflection のものを使う。
    """
    k = tmp_path / "knowledge.dwh.yaml"
    k.write_text("connection_id: dwh\ndb_group: dwh\nphysical_schemas: [DWH]\n", encoding="utf-8")
    knowledge = load_knowledge(k)
    snapshot = SchemaSnapshot(
        connection_id="dwh",
        db_group="dwh",
        schema_name="dbo",
        tables=[
            TableSnapshot(
                table_id="DEPT_VISIT",
                db_qualified="dbo.DEPT_VISIT",
                columns=[
                    ColumnSnapshot(physical_name="DEPT_CD", type_raw="CHAR(3)", type_base="CHAR")
                ],
            )
        ],
    )
    schemas = table_schemas(
        ["DEPT_VISIT"], knowledge.physical_schemas, reflected=snapshot.schema_name
    )
    records, _ = merge(snapshot, [], knowledge, physical_schemas=schemas)
    assert records[0].physical == {"schema": "DWH", "table": "DEPT_VISIT", "column": "DEPT_CD"}

    # 明示が無ければ reflection の `dbo`
    plain = KnowledgeLayer(connection_id="dwh", db_group="dwh")
    schemas = table_schemas(["DEPT_VISIT"], plain.physical_schemas, reflected="dbo")
    records, _ = merge(snapshot, [], plain, physical_schemas=schemas)
    assert records[0].physical == {"schema": "dbo", "table": "DEPT_VISIT", "column": "DEPT_CD"}


# --------------------------------------------------------------------------- #
# 既存の索引への追記（enrich_physical）
# --------------------------------------------------------------------------- #

# 接続ごとの知識層の `physical_schemas`（架空）。
KNOWLEDGE = {
    "dwh": ["DWH"],
    "ehr": ["EHR"],
    "dpc": ["DPC_SCH"],  # 定義書はスキーマ無し（`PATIENTS`）、実行先は別名のスキーマ
    "multi": ["UNIT_A_SCH", "UNIT_B_SCH", "UNIT_C_SCH"],  # 1接続が3スキーマに分かれる
}

CATALOG = """schema_name|table_name|row_count
-----------|----------|---------
DWH|DEPT_VISIT|120
EHR|LAB|5
EHR|DOC_BODY|1000000
DPC_SCH|PATIENTS|42
UNIT_A_SCH|T_EVENT_REC|10
UNIT_B_SCH|T_CARE|7
UNIT_C_SCH|T_CARE|3
DWH|T_CARE|1

schema_name|view_name
-----------|---------
UNIT_B_SCH|V_EVENT_REC
"""

# (unit_id, connection_id, db_qualified, physical_name) → 期待する physical
SAMPLES = [
    (
        ("db:DEMO.DEPT_VISIT.DEPT_CD", "dwh", "DEMO.DEPT_VISIT", "DEPT_CD"),
        ("DWH", "DEPT_VISIT", "DEPT_CD"),
    ),
    (
        ("db:APP.LAB.BLAST_PCT", "ehr", "APP.LAB", "BLAST_PCT"),
        ("EHR", "LAB", "BLAST_PCT"),
    ),
    (
        ("db:APP.DOC_BODY.BODY_TEXT", "ehr", "APP.DOC_BODY", "BODY_TEXT"),
        ("EHR", "DOC_BODY", "BODY_TEXT"),
    ),
    (("db:PATIENTS.ID", "dpc", "PATIENTS", "ID"), ("DPC_SCH", "PATIENTS", "ID")),
    (
        ("db:dbo.T_EVENT_REC.REC_ID", "multi", "dbo.T_EVENT_REC", "REC_ID"),
        ("UNIT_A_SCH", "T_EVENT_REC", "REC_ID"),
    ),
    # 同名の表が2つのスキーマ（UNIT_B_SCH と UNIT_C_SCH）にある（DWH にもあるが接続の候補外）→ 付けない
    (("db:dbo.T_CARE.CARE_ID", "multi", "dbo.T_CARE", "CARE_ID"), None),
    # カタログに無い表 → 付けない
    (("db:DEMO.GONE.X", "dwh", "DEMO.GONE", "X"), None),
    # ビューの節にしか無い → 付けない
    (("db:dbo.V_EVENT_REC.REC_ID", "multi", "dbo.V_EVENT_REC", "REC_ID"), None),
]


# 文書ヘッダ（本文の表が種別の列を持たない構成）。名前は架空
DOC_HEADER = DocHeaderSpec(
    header_schema="EHR",
    header_table="NOTE_INDEX",
    join_on=["PT_ID", "NOTE_NO"],
    type_column="NOTE_TYPE",
    filters=[DocFilter(column="VOID_FLAG", equals=0)],
    order_by=[DocOrder(column="NOTE_DATE", dir="desc", on_header=True)],
    body_patient_key="PT_ID",
)
_VIA = {"schema": "EHR", "table": "NOTE_INDEX", "on": ["PT_ID", "NOTE_NO"]}
# 文書種別 X001 の単位に付く経路
EXPECTED_DOC_PATH = {
    "filters": [
        {"via": _VIA, "column": "NOTE_TYPE", "equals": "X001"},
        {"via": _VIA, "column": "VOID_FLAG", "equals": 0},
    ],
    "order_by": [{"via": _VIA, "column": "NOTE_DATE", "dir": "desc"}],
}
DOC_UNIT = {
    "unit_id": "db:APP.DOC_BODY.BODY_TEXT#X001",
    "kind": "db",
    "search_text": "プログレスノート",
    "payload": {
        "connection_id": "ehr",
        "db_qualified": "APP.DOC_BODY",
        "physical_name": "BODY_TEXT",
        "doc_type_code": "X001",
        "doc_filter_table": "EHR.NOTE_INDEX",
        "doc_filter_column": "NOTE_TYPE",
    },
}


def _write_base(base: Path, extra: list[dict] | None = None) -> None:
    base.mkdir()
    lines = []
    for (uid, conn, dq, col), _ in SAMPLES:
        payload = {"connection_id": conn, "db_qualified": dq, "physical_name": col}
        lines.append(
            json.dumps({"unit_id": uid, "kind": "db", "search_text": uid, "payload": payload})
        )
    lines += [json.dumps(u, ensure_ascii=False) for u in extra or []]
    (base / "db_index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    np.save(base / "embeddings.npy", np.eye(len(lines), dtype=np.float32))
    (base / "build_info.json").write_text("{}", encoding="utf-8")


def _knowledge_files(tmp_path: Path) -> list[Path]:
    paths = []
    for i, (group, schemas) in enumerate(KNOWLEDGE.items()):
        data: dict = {"connection_id": group, "db_group": group, "physical_schemas": schemas}
        if group == "ehr":
            data["doc_header"] = DOC_HEADER.model_dump()
        p = tmp_path / f"knowledge.{i}.yaml"
        p.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        paths.append(p)
    return paths


def _read_out(out: Path) -> dict[str, dict]:
    rows = map(json.loads, (out / "db_index.jsonl").read_text("utf-8").splitlines())
    return {r["unit_id"]: r["payload"] for r in rows}


def _catalog(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.txt"
    path.write_text(CATALOG, encoding="utf-8")
    return path


def test_enrich_maps_schemas_from_knowledge(tmp_path: Path) -> None:
    base, out = tmp_path / "base", tmp_path / "out"
    _write_base(base, [DOC_UNIT])
    groups = load_groups(_knowledge_files(tmp_path))
    assert groups.schemas == KNOWLEDGE
    assert groups.doc_headers == {"ehr": DOC_HEADER}
    catalog = read_catalog(_catalog(tmp_path))
    assert "V_EVENT_REC" not in catalog  # ビューは読まない
    result = enrich_physical(base, out, groups, catalog)

    got = _read_out(out)
    for (uid, *_), want in SAMPLES:
        expect = dict(zip(("schema", "table", "column"), want)) if want else None
        assert got[uid].get("physical") == expect, uid
    # 文書種別の単位: ヘッダ表を結合する経路が付き、そのうえで physical が付く
    doc = got[DOC_UNIT["unit_id"]]
    assert doc["access_path"] == EXPECTED_DOC_PATH
    assert doc["physical"] == {"schema": "EHR", "table": "DOC_BODY", "column": "BODY_TEXT"}
    assert doc["patient_key"] == "PT_ID" and "key_type" not in doc
    assert (result.units, result.added, result.unresolved, result.access_paths) == (9, 6, 3, 1)
    # 埋め込みと他のファイルは1バイトも動かない
    assert (out / "embeddings.npy").read_bytes() == (base / "embeddings.npy").read_bytes()
    assert (out / "build_info.json").exists()


def test_enrich_never_gives_physical_to_a_doc_unit_without_access_path(tmp_path: Path) -> None:
    """経路の無い文書種別の単位に physical を付けると、患者の全文書（削除済みを含む）を返す SQL が
    実行できてしまう。ヘッダの宣言が無ければ physical を付けず、古いものは外す。"""
    stale = json.loads(json.dumps(DOC_UNIT))
    stale["payload"]["physical"] = {"schema": "EHR", "table": "DOC_BODY", "column": "BODY_TEXT"}
    base, out = tmp_path / "base", tmp_path / "out"
    _write_base(base, [stale])
    groups = GroupConfig(schemas=KNOWLEDGE, doc_headers={})  # doc_header の宣言なし
    result = enrich_physical(base, out, groups, read_catalog(_catalog(tmp_path)))
    doc = _read_out(out)[DOC_UNIT["unit_id"]]
    assert "access_path" not in doc and "physical" not in doc and "patient_key" not in doc
    assert result.removed == 1


def test_enrich_removes_stale_physical_when_ambiguous(tmp_path: Path) -> None:
    """付け直しで一意に決まらなくなった単位の古い physical は外す（残すと実行できてしまう）。
    知識層に無い接続の単位は触らない。"""
    old = {"schema": "UNIT_B_SCH", "table": "T_CARE", "column": "CARE_ID"}
    other = {"schema": "X", "table": "Y", "column": "Z"}
    units = [
        {
            "unit_id": "db:dbo.T_CARE.OLD",
            "kind": "db",
            "search_text": "t",
            "payload": {
                "connection_id": "multi",
                "db_qualified": "dbo.T_CARE",
                "physical_name": "CARE_ID",
                "physical": old,
            },
        },
        {
            "unit_id": "db:OTHER.T.C",
            "kind": "db",
            "search_text": "o",
            "payload": {
                "connection_id": "NOT_IN_KNOWLEDGE",
                "db_qualified": "OTHER.T",
                "physical_name": "C",
                "physical": other,
            },
        },
    ]
    base, out = tmp_path / "base", tmp_path / "out"
    _write_base(base, units)
    groups = GroupConfig(schemas=KNOWLEDGE, doc_headers={})
    result = enrich_physical(base, out, groups, read_catalog(_catalog(tmp_path)))
    got = _read_out(out)
    assert "physical" not in got["db:dbo.T_CARE.OLD"]
    assert got["db:OTHER.T.C"]["physical"] == other
    assert result.removed == 1


def test_enrich_cli(tmp_path: Path) -> None:
    base, out = tmp_path / "base", tmp_path / "out"
    _write_base(base, [DOC_UNIT])
    argv = ["enrich-physical", "--base", str(base), "--out", str(out)]
    argv += ["--catalog", str(_catalog(tmp_path))]
    argv += ["--knowledge", *map(str, _knowledge_files(tmp_path))]
    assert cli_main(argv) == 0
    got = _read_out(out)
    assert got["db:DEMO.DEPT_VISIT.DEPT_CD"]["physical"] == {
        "schema": "DWH",
        "table": "DEPT_VISIT",
        "column": "DEPT_CD",
    }
    assert got[DOC_UNIT["unit_id"]]["access_path"] == EXPECTED_DOC_PATH


def test_enrich_refuses_in_place(tmp_path: Path) -> None:
    base = tmp_path / "base"
    _write_base(base)
    with pytest.raises(ValueError):
        enrich_physical(base, base, GroupConfig(schemas=KNOWLEDGE, doc_headers={}), {})


def test_enrich_doc_patient_key_keeps_existing_and_writes_declared_key_type(tmp_path: Path) -> None:
    """患者キーが既にあれば触らない。無ければ doc_header から付け、key_type は宣言したときだけ。"""
    has_key = json.loads(json.dumps(DOC_UNIT))
    has_key["unit_id"] += "_KEEP"
    has_key["payload"]["patient_key"] = "OTHER_PT_ID"
    base, out = tmp_path / "base", tmp_path / "out"
    _write_base(base, [DOC_UNIT, has_key])
    header = DOC_HEADER.model_copy(update={"body_key_type": "pid"})
    groups = GroupConfig(schemas=KNOWLEDGE, doc_headers={"ehr": header})
    enrich_physical(base, out, groups, read_catalog(_catalog(tmp_path)))
    got = _read_out(out)
    assert got[DOC_UNIT["unit_id"]]["patient_key"] == "PT_ID"
    assert got[DOC_UNIT["unit_id"]]["key_type"] == "pid"
    assert got[has_key["unit_id"]]["patient_key"] == "OTHER_PT_ID"
    assert "key_type" not in got[has_key["unit_id"]]


def test_doc_access_path_orders_on_the_body_table_unless_declared_on_header() -> None:
    """並び順の列は `on_header` が偽なら本文の表の列（`via` を付けない）。"""
    spec = DOC_HEADER.model_copy(update={"order_by": [DocOrder(column="LINE_NO", dir="asc")]})
    assert doc_access_path("X001", spec)["order_by"] == [{"column": "LINE_NO", "dir": "asc"}]
