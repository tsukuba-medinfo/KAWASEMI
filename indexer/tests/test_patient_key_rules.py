"""患者キー推定の施設固有の規則（`indexer_rules.yaml` の `patient_key`）。

表名・接続名はすべて架空。規則が無ければ施設に依らない振る舞い（論理名の完全一致と
物理名パターンだけ）になり、規則を書いた施設だけ条件付きの論理名・曖昧な表が効く。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kawasemi_indexer.builder import build_db_units, infer_patient_key
from kawasemi_indexer.site_rules import PatientKeyRules

RULES = PatientKeyRules(
    conditional_logical_names={"ID": ["claims_demo"]},
    ambiguous_tables=["DEMO.PATIENT_LINK"],
)
EMPTY = PatientKeyRules()


def _col(
    physical: str,
    logical: str,
    *,
    table: str = "DEMO.T",
    conn: str = "claims_demo",
    pk: bool = False,
):
    return {
        "physical_name": physical,
        "logical_name": logical,
        "db_qualified": table,
        "connection_id": conn,
        "pk": pk,
    }


def test_conditional_logical_name_only_on_listed_connection_and_pk() -> None:
    assert infer_patient_key([_col("ID", "ID", pk=True)], RULES) == "ID"
    # PK でなければ認めない
    assert infer_patient_key([_col("ID", "ID", pk=False)], RULES) is None
    # 一覧に無い接続（別の命名体系のマスタ表など）には適用しない
    assert infer_patient_key([_col("ID", "ID", conn="ehr", pk=True)], RULES) is None


def test_without_rules_an_id_column_is_not_a_patient_key() -> None:
    assert infer_patient_key([_col("ID", "ID", pk=True)], EMPTY) is None


def test_ambiguous_table_gets_no_patient_key() -> None:
    cols = [_col("PT_ID", "患者ID", table="DEMO.PATIENT_LINK")]
    assert infer_patient_key(cols, RULES) is None
    # 規則が無ければ論理名の完全一致がそのまま効く
    assert infer_patient_key(cols, EMPTY) == "PT_ID"
    # 一覧に無い表は影響を受けない
    assert infer_patient_key([_col("PT_ID", "患者ID", table="DEMO.VISIT")], RULES) == "PT_ID"


def test_generic_rules_do_not_depend_on_site_file() -> None:
    """論理名の完全一致・順序・重複の扱いは施設の規則に依らない。"""
    both = [_col("PT_NO", "患者番号"), _col("PT_ID", "患者ID")]
    assert infer_patient_key(both, EMPTY) == "PT_NO"
    dup = [_col("H_PT", "患者ID"), _col("B_PT", "患者ID")]
    assert infer_patient_key(dup, EMPTY) is None
    qualified = [_col("PT_ID", "患者ID"), _col("SRC_PT_ID", "他院患者ID")]
    assert infer_patient_key(qualified, EMPTY) == "PT_ID"


def test_additional_logical_names_come_after_the_generic_ones() -> None:
    """施設が足した論理名は、既定の論理名がどれも無い表でだけ採る。"""
    rules = PatientKeyRules(additional_logical_names=["受診者番号"])
    alone = [_col("USER_NO", "受診者番号"), _col("X", "その他")]
    assert infer_patient_key(alone, rules) == "USER_NO"
    # 規則が無ければ患者キーと認めない
    assert infer_patient_key(alone, EMPTY) is None
    # 既定の論理名と並べば既定のほうを採る（2件として落とさない）
    both = [_col("PT_ID", "患者ID"), _col("USER_NO", "受診者番号")]
    assert infer_patient_key(both, rules) == "PT_ID"


def _rows(table: str, logical: str, physical: str) -> list[dict[str, Any]]:
    return [
        {
            "db_group": "claims_demo",
            "db_qualified": table,
            "physical_name": physical,
            "logical_name": logical,
            "pk": True,
            "type_raw": "VARCHAR",
        }
    ]


def test_build_db_units_reads_the_site_rules_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`rules` 省略時は設定の `indexer_rules_file` を読み、無ければ空の規則で動く。"""
    from kawasemi_indexer.config import settings

    rows = _rows("DEMO.PATIENT_LINK", "患者ID", "PT_ID")
    monkeypatch.setattr(settings, "indexer_rules_file", str(tmp_path / "missing.yaml"))
    (unit,) = build_db_units(rows)
    assert unit.payload["patient_key"] == "PT_ID"

    site = tmp_path / "indexer_rules.yaml"
    site.write_text("patient_key:\n  ambiguous_tables: [DEMO.PATIENT_LINK]\n", encoding="utf-8")
    monkeypatch.setattr(settings, "indexer_rules_file", str(site))
    (unit,) = build_db_units(rows)
    assert unit.payload["patient_key"] is None
