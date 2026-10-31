"""KeyTypeDrafter（レベル2カタログ化支援, PR④）のユニットテスト。

- 患者キー列（physical_name == patient_key）のみ候補対象、他列・非db kind は除外
- key_type 名称一致でスコア上位（mrn 列 → mrn 候補が top）
- top_n / min_score フィルタ
"""

from __future__ import annotations

from types import SimpleNamespace

from kawasemi_indexer.key_type_drafter import KeyTypeDrafter
from kawasemi_indexer.key_types import KeyType, KeyTypeCatalog
from kawasemi_indexer.schema import ResourceUnit

_CATALOG = KeyTypeCatalog(
    {
        "mrn": KeyType("mrn", "院内MRN"),
        "dwh_serial": KeyType("dwh_serial", "DWH内部連番"),
        "fhir_patient_id": KeyType("fhir_patient_id", "FHIR Patient.id"),
    }
)


def _unit(
    unit_id: str, physical: str, patient_key: str | None, *, kind: str = "db"
) -> ResourceUnit:
    return ResourceUnit(
        unit_id=unit_id,
        kind=kind,
        label=physical,
        search_text=physical,
        snippet=physical,
        payload={
            "physical_name": physical,
            "logical_name": physical,
            "patient_key": patient_key,
            "db_qualified": "DWH.PATIENT",
        },
    )


def _drafter(units: list[ResourceUnit]) -> KeyTypeDrafter:
    store = SimpleNamespace(units=units)
    return KeyTypeDrafter(catalog=_CATALOG, store=store)  # type: ignore[arg-type]


def test_only_patient_key_columns_are_drafted() -> None:
    units = [
        _unit("db:DWH.PATIENT.mrn", "mrn", "mrn"),  # 患者キー列そのもの → 対象
        _unit("db:DWH.PATIENT.name", "name", "mrn"),  # 同テーブルの別列 → 除外
        _unit("db:DWH.PATIENT.no_pk", "x", None),  # patient_key 無し → 除外
        _unit("file:foo", "mrn", "mrn", kind="file"),  # 非db → 除外
    ]
    drafts = _drafter(units).draft()
    assert [d.unit_id for d in drafts] == ["db:DWH.PATIENT.mrn"]


def test_name_match_ranks_top() -> None:
    units = [_unit("db:DWH.PATIENT.mrn", "mrn", "mrn")]
    drafts = _drafter(units).draft()
    assert drafts[0].candidates[0].key_type == "mrn"
    assert drafts[0].candidates[0].score > 0


def test_top_n_and_min_score() -> None:
    units = [_unit("db:DWH.PATIENT.mrn", "mrn", "mrn")]
    drafts = _drafter(units).draft(top_n=1)
    assert len(drafts[0].candidates) == 1

    # 高い min_score では一致しない列は候補ゼロ
    units2 = [_unit("db:DWH.PATIENT.zzz", "zzz", "zzz")]
    drafts2 = _drafter(units2).draft(min_score=0.5)
    assert drafts2[0].candidates == []
