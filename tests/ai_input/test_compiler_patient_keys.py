"""コンパイルが step の key_type / physical_format を焼き込む。

実機で回すと LLM は key_type を1つも書かず、DWH の mrn がゼロ詰めされずに
全員 not_found、ファイルのパスは登録番号で組まれた。値は索引の `payload.key_type` と
接続レジストリ（`key_type` / `key_formats`）にあるので、コンパイラが決定的に付ける。
"""

from __future__ import annotations

from typing import Any

from kawasemi.ai_input.compiler import (
    _location_lines,
    bake_patient_keys,
    enforce_derived_op_args,
)
from kawasemi.ai_input.resource_index import connections as conn_mod
from kawasemi.ai_input.resource_index import derive as derive_module
from kawasemi.ai_input.resource_index import key_types as kt_mod
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry
from kawasemi.ai_input.resource_index.derive import derive_op_args_list
from kawasemi.ai_input.resource_index.key_types import KeyType, KeyTypeCatalog
from kawasemi.ai_input.schemas import DSLProgram

CATALOG = KeyTypeCatalog(
    {
        k: KeyType(id=k, label=k)
        for k in ("trial_subject_no", "mrn", "billing_id", "report_key", "fhir_patient_id")
    }
)

REGISTRY = ConnectionRegistry(
    {
        "app_db": {"kind": "db", "scope": "self"},
        "dwh": {"kind": "db", "scope": "external", "key_formats": {"mrn": {"pad": 10}}},
        # DB に接続単位の key_type を書いても、DB の step には使わない（列ごとに索引が決める）
        "billing": {"kind": "db", "scope": "external", "key_type": "billing_id"},
        "hosp_fhir": {"kind": "fhir", "scope": "external", "key_type": "fhir_patient_id"},
        "report_fs": {"kind": "file", "scope": "external", "key_type": "report_key"},
        "dpc_ef": {
            "kind": "file",
            "scope": "external",
            "key_type": "mrn",
            "key_formats": {"mrn": {"pad": 10}},
        },
        "bare_fs": {"kind": "file", "scope": "external"},
    }
)

DWH_PAYLOAD = {
    "connection_id": "dwh",
    "db_qualified": "PATIENT",
    "physical_name": "BIRTH_DATE",
    "patient_key": "PT_ID",
    "key_type": "mrn",
    "physical": {"schema": "main", "table": "PATIENT", "column": "BIRTH_DATE"},
}
BILLING_PAYLOAD = {
    "connection_id": "billing",
    "db_qualified": "BILLING_SUMMARY",
    "physical_name": "ANTIBIOTIC_DAYS",
    "patient_key": "BILL_PT_ID",
    "key_type": "billing_id",
    "physical": {"schema": "main", "table": "BILLING_SUMMARY", "column": "ANTIBIOTIC_DAYS"},
}
FILE_PAYLOAD = {
    "connection_id": "report_fs",
    "file_role": "patient_data",
    "path_pattern": "{{patient_id}}/admission_summary.txt",
}
FHIR_PAYLOAD = {
    "connection_id": "hosp_fhir",
    "resource_type": "Observation",
    "query_template": "Observation?code=pathogen&patient={{patient_id}}",
    "fhir_path": "valueString",
}

DWH_SQL = "SELECT BIRTH_DATE FROM main.PATIENT WHERE PT_ID = :patient_id"


def _cand(unit_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "location": "（所在）",
        "unit_id": unit_id,
        "connection_id": payload["connection_id"],
        "payload": payload,
    }


def _compile(steps: list[dict[str, Any]], candidates: list[dict[str, Any]]):
    """コンパイラと同じ順（是正 → 焼き込み）で通した program と是正一覧を返す。"""
    program = DSLProgram(steps=steps, **{"return": f"${steps[-1]['id']}"})
    derived = derive_op_args_list(candidates, REGISTRY)
    fixes = enforce_derived_op_args(program, derived) if derived else []
    fixes += bake_patient_keys(program, derived, REGISTRY, CATALOG)
    return program, fixes


def test_db_step_gets_key_type_from_payload_and_format_from_connection() -> None:
    """LLM が key_type を書かなかった DWH の step に mrn と10桁ゼロ詰めが付く（実機の birth_date）。"""
    program, fixes = _compile(
        [{"op": "external_db_extract", "id": "v", "connection_id": "dwh", "sql": DWH_SQL}],
        [_cand("db:PATIENT.BIRTH_DATE", DWH_PAYLOAD)],
    )
    step = program.steps[0]
    assert step.key_type == "mrn"
    assert step.physical_format == {"pad": 10}
    assert not step.needs_input
    assert fixes == []  # 未記入からの補完は是正ではない


def test_db_step_without_declared_format_stays_canonical() -> None:
    program, _ = _compile(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "billing",
                "sql": "SELECT ANTIBIOTIC_DAYS FROM main.BILLING_SUMMARY "
                "WHERE BILL_PT_ID = :patient_id",
            }
        ],
        [_cand("db:BILLING_SUMMARY.ANTIBIOTIC_DAYS", BILLING_PAYLOAD)],
    )
    assert program.steps[0].key_type == "billing_id"
    assert program.steps[0].physical_format is None


def test_db_step_does_not_take_the_connection_default() -> None:
    """典拠の無い DB step に接続単位の key_type を当てない（患者で絞らない表などを巻き込む）。"""
    program, _ = _compile(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "billing",
                "sql": "SELECT CODE FROM main.MASTER",
            }
        ],
        [],
    )
    assert program.steps[0].key_type is None


def test_file_step_gets_key_type_from_connection() -> None:
    """ファイルの `{{patient_id}}` は登録番号ではなく report_key で埋まる（実機の report_fs）。"""
    program, _ = _compile(
        [
            {
                "op": "file_extract",
                "id": "doc",
                "connection_id": "report_fs",
                "path_pattern": "{{patient_id}}/admission_summary.txt",
            }
        ],
        [_cand("file:patient_data:入院時サマリ", FILE_PAYLOAD)],
    )
    assert program.steps[0].key_type == "report_key"
    assert program.steps[0].physical_format is None
    assert not program.steps[0].needs_input


def test_fhir_step_gets_key_type_from_connection() -> None:
    program, _ = _compile(
        [
            {
                "op": "fhir_extract",
                "id": "v",
                "connection_id": "hosp_fhir",
                "fhir_query": "/Observation?code=pathogen&patient={{patient_id}}",
                "fhir_path": "valueString",
            }
        ],
        [_cand("fhir:Observation:pathogen", FHIR_PAYLOAD)],
    )
    assert program.steps[0].key_type == "fhir_patient_id"


def test_csv_step_gets_key_type_and_format_from_connection_without_index_unit() -> None:
    """csv_extract は索引の是正の対象外でも、接続の宣言から key_type と物理形が付く。"""
    program, _ = _compile(
        [
            {
                "op": "csv_extract",
                "id": "rows",
                "connection_id": "dpc_ef",
                "path": "ef.csv",
                "key_column": "患者ID",
                "column": "薬効分類",
            }
        ],
        [],
    )
    assert program.steps[0].key_type == "mrn"
    assert program.steps[0].physical_format == {"pad": 10}


def test_llm_key_type_that_disagrees_is_replaced_and_sent_to_a_human() -> None:
    """LLM が別の key_type を書いたら、索引の値に置き換えて needs_input にする（黙って通さない）。"""
    program, fixes = _compile(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "dwh",
                "sql": DWH_SQL,
                "key_type": "billing_id",
            }
        ],
        [_cand("db:PATIENT.BIRTH_DATE", DWH_PAYLOAD)],
    )
    step = program.steps[0]
    assert step.key_type == "mrn"
    assert step.physical_format == {"pad": 10}
    assert step.needs_input is True
    assert "billing_id→mrn" in (step.reasoning or "")
    assert fixes == ["v:患者キー不一致(needs_input)"]


def test_llm_physical_format_that_disagrees_is_sent_to_a_human() -> None:
    program, fixes = _compile(
        [
            {
                "op": "file_extract",
                "id": "doc",
                "connection_id": "report_fs",
                "path_pattern": "{{patient_id}}/admission_summary.txt",
                "key_type": "report_key",
                "physical_format": {"pad": 10},
            }
        ],
        [_cand("file:patient_data:入院時サマリ", FILE_PAYLOAD)],
    )
    step = program.steps[0]
    assert step.physical_format is None
    assert step.needs_input is True
    assert fixes == ["doc:患者キー不一致(needs_input)"]


def test_llm_key_type_that_agrees_is_not_a_correction() -> None:
    program, fixes = _compile(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "dwh",
                "sql": DWH_SQL,
                "key_type": "mrn",
                "physical_format": {"pad": 10},
            }
        ],
        [_cand("db:PATIENT.BIRTH_DATE", DWH_PAYLOAD)],
    )
    assert fixes == []
    assert not program.steps[0].needs_input


def _bare_file_step(key_type: str) -> list[dict[str, Any]]:
    return [
        {
            "op": "file_extract",
            "id": "doc",
            "connection_id": "bare_fs",
            "path_pattern": "{{patient_id}}/a.txt",
            "key_type": key_type,
        }
    ]


def test_undeclared_connection_keeps_a_known_key_type() -> None:
    """索引にも接続にも宣言が無ければ、カタログにある名前は残す（推測で否定しない）。"""
    program, fixes = _compile(_bare_file_step("report_key"), [])
    assert program.steps[0].key_type == "report_key"
    assert not program.steps[0].needs_input
    assert fixes == []


def test_undeclared_connection_drops_a_key_type_missing_from_the_catalog() -> None:
    """カタログに無い名前（運用中の DB の保存済みプランに `patient_id` / `value` 等が実在した）は外す。

    残すと実行時に resolver が引けず not_found になり、項目ごと飛ばされる。
    """
    program, fixes = _compile(_bare_file_step("patient_id"), [])
    step = program.steps[0]
    assert step.key_type is None
    assert step.needs_input is True
    assert "patient_id" in (step.reasoning or "")
    assert fixes == ["doc:患者キー除去(needs_input)"]


def test_unknown_key_type_is_kept_when_no_catalog_is_loaded() -> None:
    """カタログが未配置なら何がカタログに無いかを言えないので、外さない。"""
    program = DSLProgram(steps=_bare_file_step("patient_id"), **{"return": "$doc"})
    assert bake_patient_keys(program, [], REGISTRY, KeyTypeCatalog()) == []
    assert program.steps[0].key_type == "patient_id"


APP_PAYLOAD = {
    "connection_id": "app_db",
    "db_qualified": "SUBJECT",
    "physical_name": "AGE",
    "patient_key": "SUBJECT_NO",
    "key_type": "trial_subject_no",
}


def test_app_db_sql_extract_keeps_using_the_registration_number() -> None:
    """自アプリDBの anchor（登録番号）の表は key_type を付けない（resolver を通さない）。"""
    program, fixes = _compile(
        [
            {
                "op": "sql_extract",
                "id": "v",
                "sql": "SELECT AGE FROM SUBJECT WHERE SUBJECT_NO = :patient_id",
                "key_type": "trial_subject_no",
            }
        ],
        [_cand("db:SUBJECT.AGE", APP_PAYLOAD)],
    )
    step = program.steps[0]
    assert step.op == "sql_extract"
    assert step.key_type is None
    assert not step.needs_input
    assert fixes == []


def test_app_db_sql_extract_with_another_key_type_is_sent_to_a_human() -> None:
    program, fixes = _compile(
        [
            {
                "op": "sql_extract",
                "id": "v",
                "sql": "SELECT AGE FROM SUBJECT WHERE SUBJECT_NO = :patient_id",
                "key_type": "mrn",
            }
        ],
        [_cand("db:SUBJECT.AGE", APP_PAYLOAD)],
    )
    assert program.steps[0].key_type is None
    assert program.steps[0].needs_input is True
    assert fixes == ["v:患者キー除去(needs_input)"]


UNSCOPED_PAYLOAD = {
    "connection_id": "dwh",
    "db_qualified": "ITEM_COUNT",
    "physical_name": "CNT",
    "patient_scoped": False,
    "physical": {"schema": "main", "table": "ITEM_COUNT", "column": "CNT"},
}


def test_unscoped_table_gets_no_patient_key_and_llm_key_type_is_dropped() -> None:
    """患者で絞らない表の step に患者キーを付けない。LLM が書いた分は外して人へ回す。"""
    program, fixes = _compile(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "dwh",
                "sql": "SELECT CNT FROM main.ITEM_COUNT",
                "key_type": "mrn",
                "physical_format": {"pad": 10},
            }
        ],
        [_cand("db:ITEM_COUNT.CNT", UNSCOPED_PAYLOAD)],
    )
    step = program.steps[0]
    assert step.key_type is None and step.physical_format is None
    assert step.needs_input is True
    assert "v:患者キー除去(needs_input)" in fixes


def test_unscoped_table_without_llm_key_type_stays_runnable() -> None:
    program, fixes = _compile(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "dwh",
                "sql": "SELECT CNT FROM main.ITEM_COUNT",
            }
        ],
        [_cand("db:ITEM_COUNT.CNT", UNSCOPED_PAYLOAD)],
    )
    assert program.steps[0].key_type is None
    assert not program.steps[0].needs_input
    assert fixes == []


def test_unscoped_unit_that_also_declares_a_key_type_is_a_contradiction() -> None:
    """`patient_scoped=false` と `key_type` が両方ある単位は、宣言の矛盾として人へ回す。

    derive が patient_key / access_path との矛盾を扱うのと同じ（どちらを信じても事故になりうる）。
    """
    program, _ = _compile(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "dwh",
                "sql": "SELECT CNT FROM main.ITEM_COUNT",
            }
        ],
        [_cand("db:ITEM_COUNT.CNT", {**UNSCOPED_PAYLOAD, "key_type": "mrn"})],
    )
    step = program.steps[0]
    assert step.key_type is None
    assert step.needs_input is True
    assert "key_type" in (step.reasoning or "")


def test_prompt_wording_for_the_key_type_follows_the_op_family() -> None:
    """`:patient_id` の指示は DB だけ。FHIR / ファイルには `{{patient_id}}` の説明を出す。"""
    db = derive_op_args_list([_cand("db:PATIENT.BIRTH_DATE", DWH_PAYLOAD)], REGISTRY)
    db_lines = "\n".join(_location_lines({}, db))
    assert "患者キー種別: mrn（実行時に被験者IDから解決される。SQLでは :patient_id を使う）" in (
        db_lines
    )
    fhir = derive_op_args_list(
        [_cand("fhir:Observation:pathogen", {**FHIR_PAYLOAD, "key_type": "fhir_patient_id"})],
        REGISTRY,
    )
    fhir_lines = "\n".join(_location_lines({}, fhir))
    assert "患者キー種別: fhir_patient_id" in fhir_lines
    assert ":patient_id" not in fhir_lines.replace("{{patient_id}}", "")


def test_bake_uses_the_shared_registry_by_default(monkeypatch) -> None:
    """引数を省くと接続レジストリの共有インスタンスを使う（コンパイラ本体の呼び方）。"""
    monkeypatch.setattr(conn_mod, "get_registry", lambda: REGISTRY)
    monkeypatch.setattr(derive_module, "get_registry", lambda: REGISTRY)
    monkeypatch.setattr(kt_mod, "get_catalog", lambda: CATALOG)
    program = DSLProgram(
        steps=[{"op": "fhir_extract", "id": "v", "connection_id": "hosp_fhir"}],
        **{"return": "$v"},
    )
    bake_patient_keys(program, [])
    assert program.steps[0].key_type == "fhir_patient_id"


def test_registry_reads_key_type_and_formats() -> None:
    assert REGISTRY.key_type_of("report_fs") == "report_key"
    assert REGISTRY.key_type_of("dwh") is None
    assert REGISTRY.key_type_of("unknown") is None
    assert REGISTRY.key_format_of("dwh", "mrn") == {"pad": 10}
    assert REGISTRY.key_format_of("dwh", "billing_id") is None
    assert REGISTRY.key_format_of("dwh", None) is None
