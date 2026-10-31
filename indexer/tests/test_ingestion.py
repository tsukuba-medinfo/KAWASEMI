"""kawasemi-indexer 取り込みレイヤのテスト（実Ollama非依存）。

一時 sqlite を生きDBに見立て、information_schema reflection（骨格真実）＋ CSV/Excel
定義書（意味補完）＋ 知識層 から column_index.jsonl / value_dictionary.jsonl を生成し、
契約・PHI境界・補完欠落・マスタ取込・drift を検証する。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from kawasemi_indexer.ingestion.info_schema import _type_base, to_sync_dsn
from kawasemi_indexer.ingestion.knowledge import (
    load_knowledge,
    save_knowledge,
)
from kawasemi_indexer.ingestion.models import (
    Enrichment,
    KnowledgeLayer,
    MasterTableSpec,
    PatientKeyBinding,
    TabularSource,
)
from kawasemi_indexer.ingestion.pipeline import ingest_connection

# --------------------------------------------------------------------------- #
# フィクスチャ: 一時 sqlite（患者データ表 LAB ＋ マスタ表 SEX_MASTER）
# --------------------------------------------------------------------------- #


@pytest.fixture
def live_db(tmp_path: Path) -> str:
    db_path = tmp_path / "dwh.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE LAB (
            PT_ID     INTEGER PRIMARY KEY,
            BLAST_PCT NUMERIC,
            WBC       INTEGER,
            SEX_CD    TEXT
        );
        CREATE TABLE SEX_MASTER (
            CODE  TEXT PRIMARY KEY,
            LABEL TEXT
        );
        INSERT INTO LAB VALUES (1, 12.5, 6800, '1');
        INSERT INTO SEX_MASTER VALUES ('1', '男性'), ('2', '女性');
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db_path}"


def _csv_doc(tmp_path: Path) -> Path:
    """LAB の一部カラムだけ論理名を持つ定義書（WBC は欠落させ unenriched を作る）。
    EXTINCT は実DBに無い古い行（棄却対象）。"""
    path = tmp_path / "lab_def.csv"
    path.write_text(
        "表物理名,表論理名,カラム物理名,カラム論理名,備考\n"
        "LAB,検査結果,PT_ID,患者番号,\n"
        "LAB,検査結果,BLAST_PCT,芽球割合,末梢血の芽球割合\n"
        "LAB,検査結果,SEX_CD,性別コード,SEX_MASTER参照\n"
        "LAB,検査結果,EXTINCT,廃止列,旧定義\n",
        encoding="utf-8",
    )
    return path


def _knowledge(tmp_path: Path, csv_path: Path) -> Path:
    k = KnowledgeLayer(
        connection_id="dwh",
        db_group="DEMO_DB",
        tabular_sources=[
            TabularSource(
                path=str(csv_path),
                header_row=1,
                columns={
                    "table_id": "表物理名",
                    "table_logical": "表論理名",
                    "physical_name": "カラム物理名",
                    "logical_name": "カラム論理名",
                    "notes": "備考",
                },
                table_match="table_id",
            )
        ],
        table_roles={"LAB": "patient_data", "SEX_MASTER": "master"},
        patient_keys={"LAB": PatientKeyBinding(physical_name="PT_ID", key_type="mrn")},
        master_tables={"SEX_MASTER": MasterTableSpec(code_column="CODE", label_column="LABEL")},
        enrichments={
            # WBC は定義書に無いので知識層で補間（inferred 印）
            "LAB.WBC": Enrichment(logical_name="白血球数", inferred=True, confirmed=True),
        },
    )
    path = tmp_path / "knowledge_dwh.yaml"
    save_knowledge(k, path)
    return path


def _rows(out_dir: Path) -> list[dict]:
    path = out_dir / "L2" / "column_index.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# テスト
# --------------------------------------------------------------------------- #


def test_type_base_and_sync_dsn() -> None:
    assert _type_base("NUMBER(5,2)") == "NUMBER"
    assert _type_base("CHAR(8)") == "CHAR"
    assert _type_base("datetime") == "DATETIME"
    assert to_sync_dsn("sqlite+aiosqlite:///x.db") == "sqlite:///x.db"
    assert to_sync_dsn("postgresql+asyncpg://h/d") == "postgresql+psycopg2://h/d"


def test_ingest_contract_and_enrichment(tmp_path: Path, live_db: str) -> None:
    out_dir = tmp_path / "corpus"
    csv_path = _csv_doc(tmp_path)
    kpath = _knowledge(tmp_path, csv_path)

    result = ingest_connection(kpath, out_dir, dsn=live_db)

    rows = _rows(out_dir)
    by_phys = {(r["table_id"], r["physical_name"]): r for r in rows}

    # 契約フィールドが揃っている（builder が消費できる）
    contract = {
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
    }
    assert contract <= set(rows[0].keys())

    # 骨格真実 = info_schema の全カラムが索引化される（LAB 4 + SEX_MASTER 2 = 6）
    assert result.column_count == 6
    assert result.table_count == 2

    # Excel 補完: 論理名が結合される
    assert by_phys[("LAB", "BLAST_PCT")]["logical_name"] == "芽球割合"
    assert by_phys[("LAB", "BLAST_PCT")]["db_group"] == "DEMO_DB"
    assert by_phys[("LAB", "BLAST_PCT")]["table_logical"] == "検査結果"

    # 知識層 enrichment（inferred 印付き）
    wbc = by_phys[("LAB", "WBC")]
    assert wbc["logical_name"] == "白血球数"
    assert wbc["logical_name_inferred"] is True

    # 患者キー宣言が反映（取り違え防止）
    pt = by_phys[("LAB", "PT_ID")]
    assert pt["is_patient_key"] is True
    assert pt["key_type"] == "mrn"
    assert pt["pk"] is True

    # 表役割が反映
    assert by_phys[("SEX_MASTER", "CODE")]["table_role"] == "master"
    assert by_phys[("LAB", "PT_ID")]["table_role"] == "patient_data"


def test_skeleton_truth_discards_stale_doc_rows(tmp_path: Path, live_db: str) -> None:
    """定義書にあって info_schema に無い列（EXTINCT）は棄却される。"""
    out_dir = tmp_path / "corpus"
    kpath = _knowledge(tmp_path, _csv_doc(tmp_path))
    result = ingest_connection(kpath, out_dir, dsn=live_db)

    rows = _rows(out_dir)
    physicals = {r["physical_name"] for r in rows}
    assert "EXTINCT" not in physicals  # 古い定義は索引に載らない
    assert result.discarded_doc_rows == 1


def test_unenriched_counted(tmp_path: Path, live_db: str) -> None:
    """補完の無いカラムは unenriched として計上される（検索品質の可視化）。

    SEX_MASTER の LABEL/CODE は定義書にも enrichment にも無いので欠落。"""
    out_dir = tmp_path / "corpus"
    kpath = _knowledge(tmp_path, _csv_doc(tmp_path))
    result = ingest_connection(kpath, out_dir, dsn=live_db)
    # SEX_MASTER.CODE, SEX_MASTER.LABEL の2件が論理名なし
    assert result.unenriched_count == 2


def test_phi_boundary_only_master_codes_read(tmp_path: Path, live_db: str) -> None:
    """患者データ表(LAB)の行は読まず、master(SEX_MASTER)の code→label のみ取り込む。"""
    out_dir = tmp_path / "corpus"
    kpath = _knowledge(tmp_path, _csv_doc(tmp_path))
    result = ingest_connection(kpath, out_dir, dsn=live_db)

    vd_path = out_dir / "value_dictionary.jsonl"
    entries = [json.loads(ln) for ln in vd_path.read_text("utf-8").splitlines() if ln.strip()]
    assert result.code_entry_count == 2
    assert {e["code"]: e["label"] for e in entries} == {"1": "男性", "2": "女性"}
    # 患者データ表からは1件も読み出していない
    assert all(e["db_qualified"] == "SEX_MASTER" for e in entries)


def test_phi_master_with_patient_key_is_not_read(tmp_path: Path, live_db: str) -> None:
    """誤分類耐性: role=master でも patient_keys にも宣言された表は読まない。"""
    out_dir = tmp_path / "corpus"
    k = load_knowledge(_knowledge(tmp_path, _csv_doc(tmp_path)))
    # 矛盾状態を作る: SEX_MASTER を master かつ患者キー宣言
    k.patient_keys["SEX_MASTER"] = PatientKeyBinding(physical_name="CODE", key_type="mrn")
    kpath = tmp_path / "k2.yaml"
    save_knowledge(k, kpath)

    result = ingest_connection(kpath, out_dir, dsn=live_db)
    # 矛盾表は読まれず、コード辞書は空（PHI 行を読まない）
    assert result.code_entry_count == 0
    assert (out_dir / "value_dictionary.jsonl").read_text("utf-8").strip() == ""


def test_master_spec_missing_column_skips(tmp_path: Path, live_db: str) -> None:
    """master_tables の列名が実在しないと警告スキップ（例外を投げない）。"""
    out_dir = tmp_path / "corpus"
    k = load_knowledge(_knowledge(tmp_path, _csv_doc(tmp_path)))
    k.master_tables["SEX_MASTER"] = MasterTableSpec(code_column="NOPE", label_column="LABEL")
    kpath = tmp_path / "k3.yaml"
    save_knowledge(k, kpath)

    result = ingest_connection(kpath, out_dir, dsn=live_db)
    assert result.code_entry_count == 0


def test_mapping_header_not_in_doc_raises(tmp_path: Path, live_db: str) -> None:
    """マッピングの必須列が定義書ヘッダに無い（タイプミス）と早期エラー（黙って欠落しない）。"""
    csv_path = tmp_path / "bad.csv"
    csv_path.write_text("表物理名,カラム物理名\nLAB,WBC\n", encoding="utf-8")
    k = KnowledgeLayer(
        connection_id="dwh",
        db_group="DEMO_DB",
        tabular_sources=[
            TabularSource(
                path=str(csv_path),
                columns={"table_id": "表物理名", "physical_name": "存在しない列"},
            )
        ],
    )
    kpath = tmp_path / "k4.yaml"
    save_knowledge(k, kpath)
    with pytest.raises(ValueError, match="physical_name"):
        ingest_connection(kpath, tmp_path / "corpus", dsn=live_db)


def test_drift_diff_direct() -> None:
    """diff_snapshots を直接検証: added/removed/type_changed と表記揺れ誤検知の抑制。"""
    from kawasemi_indexer.ingestion.drift import diff_snapshots
    from kawasemi_indexer.ingestion.models import (
        ColumnSnapshot,
        SchemaSnapshot,
        TableSnapshot,
    )

    def snap(cols: list[tuple[str, str]]) -> SchemaSnapshot:
        return SchemaSnapshot(
            connection_id="dwh",
            db_group="G",
            tables=[
                TableSnapshot(
                    table_id="T",
                    db_qualified="T",
                    columns=[
                        ColumnSnapshot(physical_name=n, type_raw=t, type_base=t) for n, t in cols
                    ],
                )
            ],
        )

    prev = snap([("A", "INTEGER"), ("B", "TEXT"), ("C", "TEXT")])
    new = snap(
        [("A", "integer"), ("B", "NUMERIC"), ("D", "TEXT")]
    )  # A=表記揺れ, B=型変更, C削除, D追加
    report = diff_snapshots(prev, new)
    changes = {(i.physical_name, i.change) for i in report.items}
    assert ("D", "added") in changes
    assert ("C", "removed") in changes
    assert ("B", "type_changed") in changes
    assert ("A", "type_changed") not in changes  # INTEGER vs integer は誤検知しない


def test_drift_detects_added_column(tmp_path: Path, live_db: str) -> None:
    out_dir = tmp_path / "corpus"
    kpath = _knowledge(tmp_path, _csv_doc(tmp_path))

    first = ingest_connection(kpath, out_dir, dsn=live_db)
    assert first.drift is not None and not first.drift.has_changes  # 初回は差分なし

    # 生きDBに列追加 → 再取り込みで drift 検出
    conn = sqlite3.connect(live_db.replace("sqlite:///", ""))
    conn.execute("ALTER TABLE LAB ADD COLUMN HGB NUMERIC")
    conn.commit()
    conn.close()

    second = ingest_connection(kpath, out_dir, dsn=live_db)
    assert second.drift is not None and second.drift.has_changes
    added = [i for i in second.drift.items if i.change == "added"]
    assert any(i.physical_name == "HGB" and i.needs_enrichment for i in added)


def test_builder_consumes_generated_column_index(tmp_path: Path, live_db: str) -> None:
    """生成した column_index.jsonl を既存 builder がそのまま消費できる（契約適合）。"""
    from kawasemi_indexer.builder import build_db_units

    out_dir = tmp_path / "corpus"
    kpath = _knowledge(tmp_path, _csv_doc(tmp_path))
    ingest_connection(kpath, out_dir, dsn=live_db)

    columns = _rows(out_dir)
    units = build_db_units(columns)
    assert len(units) == 6
    lab_pt = next(u for u in units if u.unit_id.endswith("LAB.PT_ID"))
    # builder のヒューリスティックも宣言キー(PT_ID)を患者キーと判定できる
    assert lab_pt.payload["patient_key"] == "PT_ID"


def test_builder_honors_declared_patient_key() -> None:
    """宣言 is_patient_key/key_type を builder が最優先で採用（ヒューリスティック上書き）。"""
    from kawasemi_indexer.builder import build_db_units, resolve_patient_key

    # ヒューリスティックなら PT_ID（患者IDパターン一致）を選ぶが、宣言は SERIAL を指す。
    cols = [
        {
            "db_group": "G",
            "db_qualified": "T",
            "physical_name": "SERIAL",
            "pk": True,
            "is_patient_key": True,
            "key_type": "dwh_serial",
        },
        {"db_group": "G", "db_qualified": "T", "physical_name": "PT_ID", "pk": True},
    ]
    pk, key_type = resolve_patient_key(cols)
    assert pk == "SERIAL" and key_type == "dwh_serial"  # 宣言が勝つ

    units = build_db_units(cols)
    assert all(u.payload["patient_key"] == "SERIAL" for u in units)
    assert {u.payload["key_type"] for u in units} == {"dwh_serial"}


def test_builder_falls_back_to_heuristic_without_declaration() -> None:
    """宣言が無ければ従来の infer_patient_key へフォールバック（後方互換）。"""
    from kawasemi_indexer.builder import resolve_patient_key

    cols = [{"db_group": "G", "db_qualified": "T", "physical_name": "PT_ID", "pk": True}]
    pk, key_type = resolve_patient_key(cols)
    assert pk == "PT_ID" and key_type is None


def test_value_dictionary_fk_folding(tmp_path: Path, live_db: str) -> None:
    """value_domains で FK 列にマスタ語彙を畳み込み、search_text と候補メタに反映。"""
    from kawasemi_indexer.builder import (
        _read_column_index,
        _read_value_dictionary,
        build_db_units,
        build_master_units,
        fold_value_dictionary,
    )
    from kawasemi_indexer.schema import Kind

    out_dir = tmp_path / "corpus"
    k = load_knowledge(_knowledge(tmp_path, _csv_doc(tmp_path)))
    k.value_domains = {"LAB.SEX_CD": "SEX_MASTER"}  # SEX_CD の値域は SEX_MASTER
    kpath = tmp_path / "k_vd.yaml"
    save_knowledge(k, kpath)

    result = ingest_connection(kpath, out_dir, dsn=live_db)
    # master 自身(2) + FK 列 LAB.SEX_CD(2) = 4
    assert result.code_entry_count == 4

    entries = _read_value_dictionary(out_dir)
    units = build_db_units(_read_column_index(out_dir))
    folded = fold_value_dictionary(units, entries)
    assert folded >= 1

    sex = next(u for u in units if u.unit_id.endswith("LAB.SEX_CD"))
    # 「男性」「女性」が search_text に畳み込まれ NL 検索で surface する
    assert "男性" in sex.search_text and "女性" in sex.search_text
    # 候補メタ: 利用側が WHERE SEX_CD='1' を組み立てられる code→label
    assert {(p["code"], p["label"]) for p in sex.payload["value_dict"]} == {
        ("1", "男性"),
        ("2", "女性"),
    }
    # マスタ表自身（physical_name なしエントリ）は 1エントリ=1ユニット(kind=master)になる
    masters = build_master_units(entries)
    assert all(u.kind == Kind.MASTER for u in masters)
    labels = {u.label for u in masters}
    assert "男性" in labels and "女性" in labels
    # payload からマスタ表の場所と code が辿れる（利用側が WHERE <code列>='1' を組める）
    male = next(u for u in masters if u.label == "男性")
    assert male.payload["code"] == "1" and male.payload["db_qualified"]


def test_fold_value_dictionary_truncates_at_cap() -> None:
    """巨大コード表は value_dict が上限で切り詰められ search_text も上限まで。"""
    from kawasemi_indexer import builder as bmod
    from kawasemi_indexer.builder import build_db_units, fold_value_dictionary

    cols = [{"db_group": "G", "db_qualified": "T", "physical_name": "ITEM_CD", "pk": False}]
    units = build_db_units(cols)
    n = bmod._VALUE_DICT_PAYLOAD_CAP + 50
    entries = [
        {"db_qualified": "T", "physical_name": "ITEM_CD", "code": str(i), "label": f"項目{i}"}
        for i in range(n)
    ]
    fold_value_dictionary(units, entries)
    item = next(u for u in units if u.unit_id.endswith("T.ITEM_CD"))
    assert len(item.payload["value_dict"]) == bmod._VALUE_DICT_PAYLOAD_CAP  # payload上限
    # search_text の畳み込みラベルは label 上限まで
    assert item.search_text.count("項目") <= bmod._VALUE_FOLD_LABEL_CAP


def test_knowledge_yaml_roundtrip(tmp_path: Path) -> None:
    kpath = _knowledge(tmp_path, _csv_doc(tmp_path))
    loaded = load_knowledge(kpath)
    assert loaded.connection_id == "dwh"
    assert loaded.table_roles["SEX_MASTER"] == "master"
    assert loaded.patient_keys["LAB"].key_type == "mrn"


def test_xlsx_source(tmp_path: Path, live_db: str) -> None:
    """Excel(.xlsx) 定義書も同じマッピングで取り込める。"""
    openpyxl = pytest.importorskip("openpyxl")
    xlsx = tmp_path / "lab_def.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["表物理名", "カラム物理名", "カラム論理名"])
    ws.append(["LAB", "BLAST_PCT", "芽球割合"])
    wb.save(xlsx)

    k = KnowledgeLayer(
        connection_id="dwh",
        db_group="DEMO_DB",
        tabular_sources=[
            TabularSource(
                path=str(xlsx),
                columns={
                    "table_id": "表物理名",
                    "physical_name": "カラム物理名",
                    "logical_name": "カラム論理名",
                },
            )
        ],
    )
    kpath = tmp_path / "k.yaml"
    save_knowledge(k, kpath)

    out_dir = tmp_path / "corpus"
    ingest_connection(kpath, out_dir, dsn=live_db)
    rows = _rows(out_dir)
    blast = next(r for r in rows if r["physical_name"] == "BLAST_PCT")
    assert blast["logical_name"] == "芽球割合"


# --------------------------------------------------------------------------- #
# 検品（lint）の ingest 統合
# --------------------------------------------------------------------------- #


def _fullwidth_doc(tmp_path: Path) -> Path:
    """カラム物理名に全角が混入した定義書（ＷＢＣ → NFKC で WBC に一致）。"""
    path = tmp_path / "fw_def.csv"
    path.write_text(
        "表物理名,カラム物理名,カラム論理名\nLAB,ＷＢＣ,白血球数\n",
        encoding="utf-8",
    )
    return path


def _fullwidth_knowledge(tmp_path: Path, csv_path: Path) -> Path:
    k = KnowledgeLayer(
        connection_id="dwh",
        db_group="DEMO_DB",
        tabular_sources=[
            TabularSource(
                path=str(csv_path),
                columns={
                    "table_id": "表物理名",
                    "physical_name": "カラム物理名",
                    "logical_name": "カラム論理名",
                },
            )
        ],
        table_roles={"LAB": "patient_data", "SEX_MASTER": "master"},
    )
    kpath = tmp_path / "k_fw.yaml"
    save_knowledge(k, kpath)
    return kpath


def test_ingest_stops_on_lint_error(tmp_path: Path, live_db: str) -> None:
    """検品 error（全角混入）があれば ingest は停止し、qa レポートが残る。"""
    kpath = _fullwidth_knowledge(tmp_path, _fullwidth_doc(tmp_path))
    out_dir = tmp_path / "corpus"
    with pytest.raises(ValueError, match="検品"):
        ingest_connection(kpath, out_dir, dsn=live_db)
    assert (out_dir / "qa" / "qa_report.md").exists()


def test_ingest_force_bypasses_lint_error(tmp_path: Path, live_db: str) -> None:
    """--force なら error があっても続行する（列は全角のまま突合できず補完欠落）。"""
    kpath = _fullwidth_knowledge(tmp_path, _fullwidth_doc(tmp_path))
    out_dir = tmp_path / "corpus"
    result = ingest_connection(kpath, out_dir, dsn=live_db, force=True)
    assert result.column_count == 6  # 骨格は全カラム索引化される


def test_ingest_fix_normalizes_and_matches(tmp_path: Path, live_db: str) -> None:
    """--fix は全角を正規化し、実DBの WBC 列に論理名が結合される。"""
    kpath = _fullwidth_knowledge(tmp_path, _fullwidth_doc(tmp_path))
    out_dir = tmp_path / "corpus"
    result = ingest_connection(kpath, out_dir, dsn=live_db, lint_fix=True)
    assert result.column_count == 6
    rows = _rows(out_dir)
    wbc = next(r for r in rows if r["physical_name"] == "WBC")
    assert wbc["logical_name"] == "白血球数"  # 正規化後に突合できた
