"""「患者で絞らない表」の宣言を索引へ出す。

マスタ参照・施設固定値（施設情報マスタなど）の表には患者を識別する列が原理的に無い。クエリ側は
`payload.patient_scoped` が真偽値の `False` のときだけ、WHERE 句の無い SQL を組む。

**宣言は人（施設）が確認した知識層からだけ出す。** 表論理名に「マスタ」を含む等から推測しない
——患者データの表でキーの推定に失敗したとき、全患者の行を返す SQL ができるため。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from kawasemi_indexer.builder import build_db_units
from kawasemi_indexer.ingestion.knowledge import load_knowledge, save_knowledge
from kawasemi_indexer.ingestion.merge import merge
from kawasemi_indexer.ingestion.models import (
    ColumnSnapshot,
    KnowledgeLayer,
    PatientKeyBinding,
    SchemaSnapshot,
    TableSnapshot,
)


def _snapshot() -> SchemaSnapshot:
    cols = [
        ColumnSnapshot(physical_name="CODE", type_raw="CHAR(8)", type_base="CHAR", pk=True),
        ColumnSnapshot(physical_name="NAME", type_raw="VARCHAR(40)", type_base="VARCHAR"),
    ]
    return SchemaSnapshot(
        connection_id="dwh",
        db_group="MASTER",
        tables=[
            TableSnapshot(
                table_id="FACILITY_INFO", db_qualified="MASTER.FACILITY_INFO", columns=cols
            ),
            TableSnapshot(table_id="TB_LAB", db_qualified="MASTER.TB_LAB", columns=cols),
        ],
    )


def _knowledge(**over: object) -> KnowledgeLayer:
    return KnowledgeLayer(connection_id="dwh", db_group="MASTER", **over)  # type: ignore[arg-type]


def test_a_declared_table_is_marked_unscoped_in_the_column_records() -> None:
    records, _ = merge(_snapshot(), [], _knowledge(patient_unscoped_tables=["FACILITY_INFO"]))
    by_table = {r.table_id: r.patient_scoped for r in records}
    assert by_table == {"FACILITY_INFO": False, "TB_LAB": None}, "宣言した表だけが False になる"


def test_a_contradictory_declaration_is_not_adopted(caplog: pytest.LogCaptureFixture) -> None:
    """`patient_keys` にも宣言された表は矛盾。患者で絞る側へ倒し、警告する。"""
    knowledge = _knowledge(
        patient_unscoped_tables=["FACILITY_INFO"],
        patient_keys={"FACILITY_INFO": PatientKeyBinding(physical_name="CODE", key_type="mrn")},
    )
    with caplog.at_level(logging.WARNING):
        records, _ = merge(_snapshot(), [], knowledge)
    assert all(r.patient_scoped is None for r in records if r.table_id == "FACILITY_INFO")
    assert any("矛盾" in r.getMessage() for r in caplog.records)


def _rows(*, scoped: bool | None, patient_key_column: bool) -> list[dict]:
    rows = [
        {
            "table_id": "FACILITY_INFO",
            "db_qualified": "MASTER.FACILITY_INFO",
            "db_group": "MASTER",
            "physical_name": name,
            "logical_name": name,
            "type_raw": "VARCHAR(40)",
            "pk": name == "CODE",
            "patient_scoped": scoped,
        }
        for name in (["CODE", "NAME"] + (["PATIENT_ID"] if patient_key_column else []))
    ]
    return rows


def test_a_declared_table_gets_the_declaration_on_every_unit_and_drops_the_key() -> None:
    # 列名ヒューリスティックが患者キーを推定しうる表（PATIENT_ID がある）でも、宣言が正になる
    units = build_db_units(_rows(scoped=False, patient_key_column=True))
    assert units
    for unit in units:
        assert unit.payload["patient_scoped"] is False
        assert unit.payload["patient_key"] is None


@pytest.mark.parametrize("scoped", [None, True])
def test_an_undeclared_table_keeps_the_payload_exactly_as_before(scoped: bool | None) -> None:
    """宣言の無い表は payload に `patient_scoped` のキーを**足さない**（既存の索引と同じ形）。

    `True` を書き出さないのは、クエリ側が `False` のときだけ意味を読むから。
    """
    units = build_db_units(_rows(scoped=scoped, patient_key_column=True))
    for unit in units:
        assert "patient_scoped" not in unit.payload
    assert any(u.payload["patient_key"] == "PATIENT_ID" for u in units), "推定は従来どおり"


def test_the_knowledge_layer_round_trips_the_declaration(tmp_path: Path) -> None:
    path = tmp_path / "knowledge.yaml"
    save_knowledge(_knowledge(patient_unscoped_tables=["FACILITY_INFO"]), path)
    assert load_knowledge(path).patient_unscoped_tables == ["FACILITY_INFO"]
    save_knowledge(_knowledge(), path)
    assert load_knowledge(path).patient_unscoped_tables == [], "既定は空（宣言は人が足す）"
