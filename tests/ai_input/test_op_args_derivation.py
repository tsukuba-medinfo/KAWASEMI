"""ステップ3: 所在の unit_id / payload からの op 引数の決定的導出

3層で検証する:
1. `resource_index.derive.derive_op_args`: payload → op 引数の決定的導出。
2. `compiler.enforce_derived_op_args`: LLM 生成 program の検証・是正（識別子を踏んで
   いれば臨床ロジックごと採用、踏んでいなければ索引由来の引数で上書き）。
3. `routes._compile_inputs`: コンパイラ入力に unit_id / connection_id / payload /
   candidates が含まれること。
加えて E2E 台本のプラン（payload なしの所在）で生成結果が一切変わらないこと（回帰防止）。
"""

from __future__ import annotations

import json
import re
import types

import pytest

from kawasemi.ai_input.compiler import (
    AIPlanCompiler,
    _value_closure,
    candidates_of,
    enforce_derived_op_args,
)
from kawasemi.ai_input.e2e.scripted_llm import load_design_script
from kawasemi.ai_input.models import (
    ClinicalAnnotation,
    InputRunbook,
    OpType,
    SourceCategory,
    SystemLocation,
)
from kawasemi.ai_input.resource_index import derive as derive_module
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry
from kawasemi.ai_input.resource_index.derive import (
    FAMILY_DB,
    FAMILY_FHIR,
    FAMILY_FILE,
    derive_op_args,
    derive_op_args_list,
    infer_kind,
)
from kawasemi.ai_input.routes import _compile_inputs
from kawasemi.ai_input.schemas import DSLProgram

# --------------------------------------------------------------------------- #
# 候補レコードのひな型（索引 payload の実体は docs/resource_index_guide.md §4）
# --------------------------------------------------------------------------- #

DB_PAYLOAD = {
    "connection_id": "DEMO_DB",
    "db_qualified": "DEMO.LAB",
    "table_logical": "検査結果",
    "physical_name": "BLAST_PCT",
    "logical_name": "芽球割合",
    "type_raw": "NUMBER(5,2)",
    "pk": False,
    "patient_key": "PATIENT_ID",
    "key_type": "mrn",
    "notes": "末梢血の芽球割合(%)",
}

FHIR_PAYLOAD = {
    "connection_id": "hosp_fhir",
    "resource_type": "Observation",
    "query_template": "Observation?code=http://loinc.org|6690-2&patient={{patient_id}}",
    "fhir_path": "valueQuantity.value",
    "code": "LOINC|6690-2",
}

# 外部DB（connections.yaml で scope=external として登録済み）を指す payload
# 外部DBのユニットは、定義書の名前（db_qualified）とは別に**実DBで使える識別子**
# （physical.schema/table/column）を持つ。
# 持たない外部DBユニットは SQL を組み立てず不足として返る（下のテスト参照）。
EXTERNAL_DB_PAYLOAD = {
    **DB_PAYLOAD,
    "connection_id": "hosp_dwh",
    "physical": {"schema": "DEMO", "table": "LAB", "column": "BLAST_PCT"},
}

FILE_PAYLOAD = {
    "connection_id": "report_fs",
    "file_role": "patient_data",
    "path_pattern": "/reports/{{patient_id}}/report.pdf",
    "extract": {"method": "pdf_llm"},
}

# 索引のマニフェストはワイルドカードでも宣言できるが、executor は glob を展開しない。
GLOB_FILE_PAYLOAD = {**FILE_PAYLOAD, "path_pattern": "/reports/{{patient_id}}/*.pdf"}


@pytest.fixture(autouse=True)
def _fixed_registry(monkeypatch):
    """接続レジストリを固定する。

    `resolve_db_op` は connections.yaml の scope を唯一の根拠に自アプリDB／外部DBを決める
    （候補の source_category は信用しない）。テストが実環境の connections.yaml の
    有無に左右されないよう、既定のレジストリを差し替える。
    """
    reg = ConnectionRegistry(
        {
            "DEMO_DB": {"kind": "db", "scope": "self"},
            "hosp_dwh": {"kind": "db", "scope": "external"},
            "hosp_fhir": {"kind": "fhir", "scope": "external"},
            "report_fs": {"kind": "file", "scope": "external"},
        }
    )
    monkeypatch.setattr(derive_module, "get_registry", lambda: reg)
    return reg


def _cand(source_category: str, unit_id: str, payload: dict, **extra) -> dict:
    return {
        "source_category": source_category,
        "location": "（人間可読の所在）",
        "unit_id": unit_id,
        "connection_id": payload.get("connection_id"),
        "payload": payload,
        **extra,
    }


# --------------------------------------------------------------------------- #
# 1. payload → op 引数の決定的導出
# --------------------------------------------------------------------------- #


def test_infer_kind_from_unit_id_prefix_and_payload():
    assert infer_kind("db:DEMO.LAB.BLAST_PCT", {}) == "db"
    assert infer_kind("master:DEMO_DB:DEMO.MST:C92.0", {}) == "master"
    # unit_id が無くても payload のキー構成から判定できる
    assert infer_kind(None, FHIR_PAYLOAD) == "fhir"
    assert infer_kind(None, FILE_PAYLOAD) == "file"
    assert infer_kind(None, DB_PAYLOAD) == "db"
    assert infer_kind(None, {}) is None


def test_derive_db_local_builds_sql_deterministically():
    spec = derive_op_args(_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD))
    assert spec is not None
    assert spec.op == OpType.SQL_EXTRACT
    assert spec.family == FAMILY_DB
    assert spec.args["sql"] == ("SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id")
    assert spec.args["connection_id"] == "DEMO_DB"
    assert spec.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert spec.key_type == "mrn"
    assert spec.is_complete is True


def test_derive_db_external_uses_external_op():
    spec = derive_op_args(
        _cand(SourceCategory.EXTERNAL_DB, "db:DEMO.LAB.BLAST_PCT", EXTERNAL_DB_PAYLOAD)
    )
    assert spec is not None
    assert spec.op == OpType.EXTERNAL_DB_EXTRACT
    assert spec.args["connection_id"] == "hosp_dwh"


def test_derive_db_op_is_decided_by_connection_scope():
    """自アプリDB／外部DBの別は接続レジストリの scope だけで決める。

    候補の source_category は根拠にしない（ドラフト生成時の既定で未登録接続にも
    local_db が付くうえ、候補は保存時に値を検査しないため）。
    """
    registry = ConnectionRegistry({"DEMO_DB": {"kind": "db", "scope": "external"}})
    cand = _cand(SourceCategory.MANUAL, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)
    spec = derive_op_args(cand, registry)
    assert spec is not None
    assert spec.op == OpType.EXTERNAL_DB_EXTRACT


def test_derive_db_without_patient_key_reports_missing():
    payload = {**DB_PAYLOAD, "patient_key": None}
    spec = derive_op_args(_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", payload))
    assert spec is not None
    assert "sql" not in spec.args  # 推測でWHERE句を作らない
    assert spec.is_complete is False
    assert any("patient_key" in m for m in spec.missing)


def test_derive_db_external_without_physical_identifiers_reports_missing():
    """外部DBは実識別子（payload.physical）が無ければ SQL を組み立てない。

    索引の db_qualified は元システム側の名前で、実DBには無い表を指す
    （`SRC.DOCUMENT` → `ehr.DOCUMENT`）。組み立てると形は成立するので検証を
    通ってしまい、実行時に「表が存在しない（invalid object name）」で落ちるまで誰も気付かない。
    """
    payload = {k: v for k, v in EXTERNAL_DB_PAYLOAD.items() if k != "physical"}
    spec = derive_op_args(_cand(SourceCategory.EXTERNAL_DB, "db:DEMO.LAB.BLAST_PCT", payload))
    assert spec is not None
    assert spec.op == OpType.EXTERNAL_DB_EXTRACT
    assert "sql" not in spec.args
    assert spec.is_complete is False
    assert any("physical" in m for m in spec.missing)


@pytest.mark.parametrize("physical", [{"schema": "", "table": "LAB", "column": "BLAST_PCT"}, {}])
def test_derive_db_external_with_incomplete_physical_reports_missing(physical: dict):
    """schema / table / column は3つ揃って初めて実DBの1列を指す。"""
    payload = {**EXTERNAL_DB_PAYLOAD, "physical": physical}
    spec = derive_op_args(_cand(SourceCategory.EXTERNAL_DB, "db:DEMO.LAB.BLAST_PCT", payload))
    assert spec is not None
    assert "sql" not in spec.args
    assert spec.is_complete is False


def test_derive_db_external_with_physical_identifiers_builds_sql():
    """実識別子を持つ外部DBユニットは従来どおり組み立てる（既存の挙動を変えない）。"""
    spec = derive_op_args(
        _cand(SourceCategory.EXTERNAL_DB, "db:DEMO.LAB.BLAST_PCT", EXTERNAL_DB_PAYLOAD)
    )
    assert spec is not None
    assert spec.op == OpType.EXTERNAL_DB_EXTRACT
    assert spec.args["sql"] == "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id"
    assert spec.is_complete is True


def test_derive_db_local_does_not_require_physical_identifiers():
    """自アプリDBは読み替えが起きないので、この判定の対象外（既存の挙動を変えない）。"""
    spec = derive_op_args(_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD))
    assert spec is not None
    assert spec.op == OpType.SQL_EXTRACT
    assert "physical" not in DB_PAYLOAD
    assert spec.is_complete is True


def test_derive_db_missing_patient_key_is_reported_before_physical():
    """既存の不足判定の順序を変えない（患者キーの欠落が先に立つ）。"""
    payload = {k: v for k, v in EXTERNAL_DB_PAYLOAD.items() if k != "physical"}
    spec = derive_op_args(
        _cand(SourceCategory.EXTERNAL_DB, "db:DEMO.LAB.BLAST_PCT", {**payload, "patient_key": None})
    )
    assert spec is not None
    assert any("patient_key" in m for m in spec.missing)


def test_derive_db_rejects_unsafe_identifiers():
    """索引由来でも SQL へ連結できない識別子は決定的導出の対象外にする。"""
    payload = {**DB_PAYLOAD, "physical_name": "C; DROP TABLE X"}
    spec = derive_op_args(_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.X", payload))
    assert spec is not None
    assert "sql" not in spec.args
    assert spec.is_complete is False


def test_derive_fhir_builds_query_and_path():
    spec = derive_op_args(_cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD))
    assert spec is not None
    assert spec.op == OpType.FHIR_EXTRACT
    assert spec.family == FAMILY_FHIR
    # 索引の query_template は先頭 "/" を持たない。executor の URL 連結規約へ正規化する。
    assert spec.args["fhir_query"] == (
        "/Observation?code=http://loinc.org|6690-2&patient={{patient_id}}"
    )
    assert spec.args["fhir_path"] == "valueQuantity.value"
    assert spec.is_complete is True
    labels = [t.label for t in spec.required_tokens]
    assert "リソース型 Observation" in labels
    assert "コード 6690-2" in labels


def test_derive_fhir_without_query_template_reports_missing():
    payload = {**FHIR_PAYLOAD, "query_template": None}
    spec = derive_op_args(_cand(SourceCategory.FHIR, "fhir:Observation:x", payload))
    assert spec is not None
    assert "fhir_query" not in spec.args
    assert spec.is_complete is False


def test_derive_file_patient_data_normalizes_path():
    spec = derive_op_args(_cand(SourceCategory.FILE, "file:patient_data:レポート", FILE_PAYLOAD))
    assert spec is not None
    assert spec.op == OpType.FILE_EXTRACT
    assert spec.family == FAMILY_FILE
    # executor は connection の root_path 起点で解決するため先頭 "/" を落とす
    assert spec.args["path_pattern"] == "reports/{{patient_id}}/report.pdf"
    assert spec.args["connection_id"] == "report_fs"
    assert spec.is_complete


def test_derive_file_rejects_glob_pattern():
    """ワイルドカードは確定しない。executor は glob を展開せず is_file() で見るため、
    焼き込むと必ず不成立になり「当該患者のデータなし」と誤記録される。"""
    spec = derive_op_args(
        _cand(SourceCategory.FILE, "file:patient_data:レポート", GLOB_FILE_PAYLOAD)
    )
    assert spec is not None
    assert spec.is_complete is False
    assert "path_pattern" not in spec.args
    assert spec.required_tokens == ()


def test_derive_file_rejects_parent_traversal():
    payload = {**FILE_PAYLOAD, "path_pattern": "../secret/{{patient_id}}/report.pdf"}
    spec = derive_op_args(_cand(SourceCategory.FILE, "file:patient_data:レポート", payload))
    assert spec is not None
    assert spec.is_complete is False
    assert "path_pattern" not in spec.args


def test_derive_returns_none_for_reference_master_and_empty_payload():
    reference = _cand(
        SourceCategory.MANUAL,
        "file:protocol.md:0-100",
        {"file_role": "reference", "path": "protocol.md"},
    )
    master = _cand(
        SourceCategory.LOCAL_DB,
        "master:DEMO_DB:DEMO.MST.DISEASE:C92.0",
        {
            "connection_id": "DEMO_DB",
            "db_qualified": "DEMO.MST.DISEASE",
            "code": "C92.0",
            "label": "急性骨髄性白血病",
        },
    )
    empty = {
        "source_category": SourceCategory.EXTERNAL_DB,
        "location": "DWH: patient_infomation.name",
        "unit_id": None,
        "connection_id": "dwh",
        "payload": {},
    }
    assert derive_op_args(reference) is None  # 参照知識（実行時に値を取らない）
    assert derive_op_args(master) is None  # コード表の1エントリ（列名が無く op にできない）
    assert derive_op_args(empty) is None  # 索引を使っていない所在（手入力など）
    assert derive_op_args_list([reference, master, empty]) == []


def test_derive_op_args_list_keeps_priority_order():
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
        ]
    )
    assert [s.family for s in specs] == [FAMILY_FHIR, FAMILY_DB]


# --------------------------------------------------------------------------- #
# 2. LLM 生成 program の検証・是正
# --------------------------------------------------------------------------- #


def _program(steps: list[dict], ret: str | None = "$v") -> DSLProgram:
    return DSLProgram(steps=steps, **{"return": ret})


def test_enforce_replaces_fabricated_sql():
    """索引に無い表・カラムを推測したSQLは、索引由来のSQLで丸ごと置き換える。"""
    program = _program(
        [{"op": "sql_extract", "id": "v", "sql": "SELECT blast FROM lab_results WHERE pid = 1"}]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    step = program.steps[0]
    assert step.sql == "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id"
    assert step.needs_input is False
    assert step.unit_id == "db:DEMO.LAB.BLAST_PCT"  # 所在の典拠を辿れる
    assert "SQLを索引の構造化情報から決定的に組み立て" in (step.reasoning or "")
    assert fixes == ["v:sql確定"]


def test_enforce_keeps_clinical_logic_when_identifiers_match():
    """索引の表・カラム・:patient_id を踏んだSQLは、時間窓・並び順ごと採用する。"""
    sql = (
        "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id "
        "ORDER BY SAMPLING_DATE DESC LIMIT 1"
    )
    program = _program([{"op": "sql_extract", "id": "v", "sql": sql}])
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[0].sql == sql  # 臨床ロジックは残す
    assert program.steps[0].unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert fixes == []


def test_enforce_corrects_op_variant_and_connection_id():
    """自アプリDB/外部DBの別と connection_id はコード側が確定する。"""
    sql = "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id"
    program = _program([{"op": "sql_extract", "id": "v", "sql": sql, "connection_id": "guessed"}])
    specs = derive_op_args_list(
        [_cand(SourceCategory.EXTERNAL_DB, "db:DEMO.LAB.BLAST_PCT", EXTERNAL_DB_PAYLOAD)]
    )

    enforce_derived_op_args(program, specs)

    assert program.steps[0].op == OpType.EXTERNAL_DB_EXTRACT
    assert program.steps[0].connection_id == "hosp_dwh"


def test_enforce_does_not_collapse_two_db_steps():
    """同一ファミリの複数stepが同じSQLへ潰れないこと（術前/術後の別を壊さない）。"""
    pre = "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id ORDER BY SAMPLING_DATE ASC"
    post = (
        "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id ORDER BY SAMPLING_DATE DESC"
    )
    program = _program(
        [
            {"op": "sql_extract", "id": "pre", "sql": pre},
            {"op": "sql_extract", "id": "post", "sql": post},
        ],
        ret="$post",
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    enforce_derived_op_args(program, specs)

    assert program.steps[0].sql == pre
    assert program.steps[1].sql == post


def test_enforce_needs_input_when_patient_key_unknown():
    """索引に患者絞り込みキー列が無ければ、推測SQLを実行させず人の補完へ回す。"""
    payload = {**DB_PAYLOAD, "patient_key": None}
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "v",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PT_NO = :patient_id",
            }
        ]
    )
    specs = derive_op_args_list([_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", payload)])

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[0].needs_input is True
    assert "決定的に組み立てられない" in (program.steps[0].reasoning or "")
    assert fixes == ["v:needs_input"]


def test_fhir_query_template_without_patient_placeholder_is_not_confirmed():
    """患者絞り込みの無いクエリ雛形は確定しない。

    確定してしまうと、LLM が正しく書いた患者絞り込み付きクエリを索引の非絞り込み
    クエリで上書きし、executor が Bundle の先頭リソースを採るため他患者の値が
    当該患者の値として確定保存されうる。
    """
    payload = {"connection_id": "hosp_fhir", "query_template": "Patient/everything"}
    spec = derive_op_args(_cand(SourceCategory.FHIR, "fhir:unknown", payload))
    assert spec is not None
    assert spec.is_complete is False
    assert "fhir_query" not in spec.args

    # LLM が書いた患者絞り込み付きクエリは上書きされず、人の確認へ回る
    program = _program(
        [{"op": "fhir_extract", "id": "v", "fhir_query": "/Patient?name={{patient_id}}"}]
    )
    enforce_derived_op_args(program, [spec])
    assert program.steps[0].fhir_query == "/Patient?name={{patient_id}}"
    assert program.steps[0].needs_input is True


def test_fhir_accepts_spaced_placeholder_written_by_llm():
    """executor は {{ patient_id }} も置換するので、その綴りも「踏んだ」とみなす。"""
    spec = derive_op_args(_cand(SourceCategory.FHIR, "fhir:obs", FHIR_PAYLOAD))
    assert spec is not None
    assert spec.is_complete is True
    assert (
        spec.matches("/Observation?code=http://loinc.org|6690-2&patient={{ patient_id }}") is True
    )


def test_file_path_without_patient_placeholder_is_not_confirmed():
    payload = {**FILE_PAYLOAD, "path_pattern": "/shared/all_patients.pdf"}
    spec = derive_op_args(_cand(SourceCategory.FILE, "file:patient_data:共有", payload))
    assert spec is not None
    assert spec.is_complete is False
    assert "path_pattern" not in spec.args


def test_enforce_drops_sql_not_referencing_index_when_incomplete():
    payload = {**DB_PAYLOAD, "patient_key": None}
    program = _program([{"op": "sql_extract", "id": "v", "sql": "SELECT x FROM other_table"}])
    specs = derive_op_args_list([_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", payload)])

    enforce_derived_op_args(program, specs)

    assert program.steps[0].sql is None
    assert program.steps[0].needs_input is True


def test_enforce_replaces_fhir_query_and_forces_fhir_path():
    """コード絞り込みを落としたFHIRクエリは索引の雛形で置き換え、FHIRPathは索引の値。"""
    program = _program(
        [
            {
                "op": "fhir_extract",
                "id": "v",
                "fhir_query": "/Observation?patient={{patient_id}}",
                "fhir_path": "valueString",
            }
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD)]
    )

    enforce_derived_op_args(program, specs)

    step = program.steps[0]
    assert step.fhir_query == "/Observation?code=http://loinc.org|6690-2&patient={{patient_id}}"
    assert step.fhir_path == "valueQuantity.value"
    assert step.connection_id == "hosp_fhir"
    assert step.unit_id == "fhir:Observation:LOINC|6690-2"


def test_enforce_normalizes_fhir_query_leading_slash():
    program = _program(
        [
            {
                "op": "fhir_extract",
                "id": "v",
                "fhir_query": "Observation?code=http://loinc.org|6690-2&patient={{patient_id}}&date=ge2024",
            }
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD)]
    )

    enforce_derived_op_args(program, specs)

    # 識別子（リソース型・コード）を踏んでいるので日付絞り込みは残し、先頭 "/" だけ補う
    assert program.steps[0].fhir_query.startswith("/Observation?")
    assert "date=ge2024" in program.steps[0].fhir_query


def test_enforce_forces_file_path_pattern():
    program = _program(
        [
            {
                "op": "file_extract",
                "id": "doc",
                "path_pattern": "/reports/{{patient_id}}/other.pdf",
            },
            {"op": "llm_extract", "id": "v", "prompt": "$doc から抽出"},
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.FILE, "file:patient_data:レポート", FILE_PAYLOAD)]
    )

    enforce_derived_op_args(program, specs)

    assert program.steps[0].path_pattern == "reports/{{patient_id}}/report.pdf"
    assert program.steps[0].connection_id == "report_fs"
    assert program.steps[1].unit_id is None  # 取得系でない step に典拠は刻まない


def test_enforce_discards_llm_written_unit_id():
    """典拠はコード側が刻む。LLM が書いた unit_id は採用しない。"""
    program = _program([{"op": "llm_judge", "id": "v", "prompt": "判定", "unit_id": "db:捏造"}])

    enforce_derived_op_args(program, [])

    assert program.steps[0].unit_id is None


def test_enforce_leaves_csv_extract_untouched():
    """csv_extract は索引側に成果物契約が無いため是正対象外（CSV プランを壊さない）。"""
    program = _program(
        [
            {
                "op": "csv_extract",
                "id": "v",
                "path": "ef.csv",
                "key_column": "患者ID",
                "column": "実施回数",
                "connection_id": "dpc_ef",
            }
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.FILE, "file:patient_data:レポート", FILE_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[0].path == "ef.csv"
    assert program.steps[0].connection_id == "dpc_ef"
    assert fixes == []


def test_enforce_matches_step_to_its_own_candidate_in_multi_source_program():
    """多ソース（FHIR優先・DB代替）のプログラムは step ごとに典拠を対応づける。"""
    program = _program(
        [
            {
                "op": "fhir_extract",
                "id": "f",
                "fhir_query": "/Observation?code=http://loinc.org|6690-2&patient={{patient_id}}",
            },
            {
                "op": "sql_extract",
                "id": "d",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "branch", "id": "v", "when": "$f", "then": "$f", "else_branch": "$d"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[0].unit_id == "fhir:Observation:LOINC|6690-2"
    assert program.steps[1].unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert program.steps[2].unit_id is None
    assert fixes == []


# --------------------------------------------------------------------------- #
# 2.5 フォールバック取得計画（同ファミリの候補が優先順で複数）
#
# 「優先順の複数候補から fallback 取得計画を生成できる」という性質は、
# 手書き DSL が検証を通ることではなく、複数候補の所在からコンパイルした結果が
# **step ごとに別の候補を引く**ことで初めて成立する。
# --------------------------------------------------------------------------- #

# 第2候補（同じ FHIR サーバの別コード）。フォールバック先として第1候補と区別できる。
FHIR_PAYLOAD_ALT = {
    **FHIR_PAYLOAD,
    "query_template": "Observation?code=http://loinc.org|718-7&patient={{patient_id}}",
    "fhir_path": "valueQuantity.value",
    "code": "LOINC|718-7",
}

# 第3候補（`_MAX_ALT_CANDIDATES` の下限を測るのに使う）
FHIR_PAYLOAD_ALT2 = {
    **FHIR_PAYLOAD,
    "query_template": "Observation?code=http://loinc.org|30313-1&patient={{patient_id}}",
    "code": "LOINC|30313-1",
}

_FHIR_Q1 = "/Observation?code=http://loinc.org|6690-2&patient={{patient_id}}"
_FHIR_Q2 = "/Observation?code=http://loinc.org|718-7&patient={{patient_id}}"
_FHIR_Q3 = "/Observation?code=http://loinc.org|30313-1&patient={{patient_id}}"


def _fhir_fallback_program(
    second_query: str | None,
    *,
    first_query: str = _FHIR_Q1,
    llm_needs_input: bool = False,
) -> DSLProgram:
    """プロンプトのフォールバック定型（2候補）。各候補のクエリを差し替えられる。

    `llm_needs_input` は第2 step に LLM 自身が付けた `needs_input` である。**既定は
    False**（＝コード側が立てたことを測れる形）。フィクスチャが True を渡すと、
    「コードが needs_input を立てたか」を測れないテストになる。
    """
    return _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": first_query},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {
                "op": "fhir_extract",
                "id": "b",
                "guard": "$no_a",
                "fhir_query": second_query,
                "needs_input": llm_needs_input,
            },
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )


def _two_fhir_candidates():
    return derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
        ]
    )


def test_enforce_gives_each_fallback_step_its_own_candidate():
    """2候補のフォールバックは、step ごとに自分の候補の引数と典拠を持つ（肯定側）。"""
    program = _fhir_fallback_program(_FHIR_Q2)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "fhir:Observation:LOINC|6690-2"
    assert b.unit_id == "fhir:Observation:LOINC|718-7"  # 第1候補を焼き直していない
    assert "718-7" in (b.fhir_query or "")
    assert not a.needs_input
    assert not b.needs_input  # 両候補が確定しているので人の補完は要らない
    assert fixes == []


@pytest.mark.parametrize("llm_needs_input", [False, True])
def test_enforce_does_not_bake_the_first_candidate_into_the_second_step(llm_needs_input: bool):
    """第2候補の引数を LLM が書けなかった step に、第1候補を焼き込んで確定させない。

    `llm_needs_input=False` は**コードが needs_input を立てること**を測る（LLM が付け
    忘れた形）。`True` は **LLM が立てた needs_input を消さないこと**を測る（以前は
    消されていた）。フィクスチャが True だけだと前者を測れない。
    """
    program = _fhir_fallback_program(None, llm_needs_input=llm_needs_input)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "fhir:Observation:LOINC|6690-2"  # 第1候補は確定する
    assert b.needs_input is True
    assert "6690-2" not in (b.fhir_query or "")  # 第1候補の二度引きにしない
    assert b.unit_id is None  # 特定できていない step に偽の典拠を残さない
    assert "b:needs_input(候補重複)" in fixes


def test_enforce_rejects_the_second_step_that_copies_the_first_candidate_query():
    """確定済み引数を両 step へコピペした形（Aを試して空ならAをもう一度）を止める。"""
    program = _fhir_fallback_program(_FHIR_Q1)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "fhir:Observation:LOINC|6690-2"
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(候補重複)" in fixes


def test_enforce_reserves_the_candidate_even_when_the_earlier_step_matched():
    """先行 step が識別子照合に成功していても、その候補は使用済みとして扱う。

    予約を「索引から焼き込んだとき」だけにすると、LLM が第1候補の SQL を自分で書いた
    フォールバックで、第2 step に第1候補の SQL が黙って焼き込まれる。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "a",
                "sql": (
                    "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id"
                    " ORDER BY TAKEN_AT DESC"
                ),
            },
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            # needs_input はコード側が立てることを測る（フィクスチャでは渡さない）。
            {"op": "sql_extract", "id": "b", "guard": "$no_a"},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert "ORDER BY TAKEN_AT DESC" in (a.sql or "")  # 臨床ロジックは残す
    assert b.needs_input is True
    assert b.sql is None
    assert b.unit_id is None
    assert "b:needs_input(候補重複)" in fixes
    # 人に出す理由が「候補重複」のものであること（優先順の逆転の文言が出てはいけない）
    assert "同じ所在候補を先行 step が使用済み" in (b.reasoning or "")
    assert "優先順" not in (b.reasoning or "")


_FILE_PATH1 = "reports/{{patient_id}}/report.pdf"


def _file_fallback_program(second_path: str | None) -> DSLProgram:
    """ファイル取得のフォールバック。`needs_input` はフィクスチャで渡さない。"""
    return _program(
        [
            {
                "op": "file_extract",
                "id": "a",
                "path_pattern": _FILE_PATH1,
                "connection_id": "report_fs",
            },
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {
                "op": "file_extract",
                "id": "b",
                "guard": "$no_a",
                "path_pattern": second_path,
                "connection_id": "report_fs",
            },
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )


def _one_file_candidate():
    return derive_op_args_list(
        [_cand(SourceCategory.FILE, "file:patient_data:レポート", FILE_PAYLOAD)]
    )


def test_enforce_blocks_the_second_file_step_that_reuses_the_candidate():
    """ファイル取得でも候補重複を人へ回す（DB だけの保護では層が揃わない）。"""
    program = _file_fallback_program(None)

    fixes = enforce_derived_op_args(program, _one_file_candidate())

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "file:patient_data:レポート"
    assert b.needs_input is True  # コード側が立てること（フィクスチャは渡していない）
    assert b.path_pattern is None  # 第1候補のパスを焼き込まない
    assert b.unit_id is None
    assert "b:needs_input(候補重複)" in fixes
    assert "ファイルパスは確定しない" in (b.reasoning or "")


def test_enforce_rejects_the_second_file_step_that_copies_the_first_path():
    """ファイル取得でも、確定済みパスを両 step へコピペした形を人へ回すこと。"""
    program = _file_fallback_program(_FILE_PATH1)

    fixes = enforce_derived_op_args(program, _one_file_candidate())

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "file:patient_data:レポート"
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(候補重複)" in fixes


# --------------------------------------------------------------------------- #
# 2.6 優先順の逆転（候補の配列順は優先順である。プロンプトの厳守事項3）
#
# 代替候補の確定済み引数をプロンプトへ出したので、LLM は**第1 step に第2候補を、
# 第2 step に第1候補を**書けるようになった。逆転を止めないと、両候補に値がある被験者で
# **運用者が指定した優先順と逆の値が確定保存される**（`branch` は第1 step の値を優先する）。
# step ごとの unit_id は「正しく」刻まれ fixes も空なので、痕跡がどこにも残らない。
# --------------------------------------------------------------------------- #


def test_enforce_blocks_priority_inversion_within_a_family():
    """第1 step が第2候補を、代替 step が第1候補を使う形を人へ回すこと。"""
    program = _fhir_fallback_program(_FHIR_Q1, first_query=_FHIR_Q2)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "fhir:Observation:LOINC|718-7"  # LLM が書いたとおり第2候補
    assert b.needs_input is True  # 優先順を後戻りする step を確定させない
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes
    assert "優先順（候補の配列順）を後戻りする計画" in (b.reasoning or "")
    assert "FHIRクエリは確定しない" in (b.reasoning or "")


def test_enforce_blocks_priority_inversion_across_families():
    """ファミリを跨いだ逆転（候補1=FHIR・候補2=DB を DB→FHIR の順に試す形）も止めること。

    同ファミリ内の単調性だけを見る実装では、各ファミリに候補が1件しか無いこの形を
    検出できない。プロンプト自身が例示している「FHIR を第1、院内DB を第2」の構成である。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "a",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_blocks_inversion_even_when_the_later_step_wrote_no_args():
    """後戻りする step が引数を書いていなくても逆転として止めること。

    逆転検査を「照合できた step」だけに限ると、第1 step が第2候補を使い、代替 step は
    厳守事項1に従って引数を書かなかった形（LLM 出力として最もありふれた形）で、システムが
    第1候補を焼き込んで **確定** させてしまう。
    """
    program = _fhir_fallback_program(None, first_query=_FHIR_Q2)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "fhir:Observation:LOINC|718-7"
    assert b.needs_input is True
    assert b.unit_id is None
    assert b.fhir_query is None  # 第1候補を焼き込んで確定させない
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_drops_the_fabricated_fhir_query_of_a_blocked_step():
    """人へ回す FHIR 取得 step に、索引の識別子を踏んでいないクエリを残さないこと。

    残すと、人が `needs_input` を外したときに索引に無いクエリが実行される（DB・FILE と
    同じ扱いにする）。
    """
    program = _fhir_fallback_program(
        "/Observation?code=http://loinc.org|9999-9&patient={{patient_id}}"
    )

    fixes = enforce_derived_op_args(program, _two_fhir_candidates())

    b = program.steps[3]
    assert b.needs_input is True
    assert b.fhir_query is None  # 捏造クエリを下書きに残さない
    assert b.unit_id is None
    assert "b:needs_input(候補重複)" in fixes


def test_enforce_does_not_block_conditional_fetch_with_mutually_exclusive_guards():
    """条件付き取得（患者の文脈で取得元が変わる形）を逆転扱いにしないこと。

    guard が判定（llm_judge）由来で、前段の取得の空振りに依存していない場合、step の
    並び順は優先順を表さない（どちらか一方しか実行されない）。ここを並び順だけで見ると、
    プロンプトが例示している形が丸ごと人の手待ちになる。
    """
    program = _program(
        [
            {"op": "llm_judge", "id": "is_inpatient", "prompt": "患者 {{patient_id}} は入院中か"},
            {
                "op": "sql_extract",
                "id": "a",
                "guard": "$is_inpatient",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {
                "op": "compare",
                "id": "is_outpatient",
                "left": "$is_inpatient",
                "cmp": "==",
                "right": False,
            },
            {"op": "fhir_extract", "id": "b", "guard": "$is_outpatient", "fhir_query": _FHIR_Q1},
            {
                "op": "branch",
                "id": "v",
                "when": "$is_inpatient",
                "then": "$a",
                "else_branch": "$b",
            },
        ]
    )
    # 候補の優先順は FHIR → DB。プログラムでは DB 側の step が先に来る。
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[1], program.steps[3]
    assert a.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert b.unit_id == "fhir:Observation:LOINC|6690-2"
    assert not a.needs_input
    assert not b.needs_input
    assert fixes == []


def test_enforce_does_not_block_unguarded_steps_out_of_candidate_order():
    """guard の無い取得 step の並び順は優先順を表さないので、逆転扱いにしないこと。

    guard が無ければ両方が無条件に実行され、どちらの値を採るかは `branch` の結線が決める。
    並び順で判定すると、多ソースのプログラム（既存テストが `fixes == []` を固定している形）
    が step を入れ替えただけで人の手待ちになる。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "d",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "fhir_extract", "id": "f", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$f", "then": "$f", "else_branch": "$d"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[0].unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert program.steps[1].unit_id == "fhir:Observation:LOINC|6690-2"
    assert not program.steps[0].needs_input
    assert not program.steps[1].needs_input
    assert fixes == []


def test_enforce_blocks_every_step_below_the_highest_priority_already_used():
    """逆転が起きたあと、**優先順を追い越された全ての step**を人へ回すこと。

    直前の step とだけ比較する実装だと、第3候補 → 第1候補 → 第2候補という並びで
    3つ目（第2候補）が「直前より進んだ」と見えて確定してしまう。優先順が崩れた計画は
    どこを先に試すのかが決まっていないので、追い越された側はすべて人が確定する。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q3},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_b", "guard": "$no_a", "left": "$b", "cmp": "is_not_empty"},
            {
                "op": "compare",
                "id": "no_b",
                "guard": "$no_a",
                "left": "$has_b",
                "cmp": "==",
                "right": False,
            },
            {"op": "fhir_extract", "id": "c", "guard": "$no_b", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v2", "when": "$has_b", "then": "$b", "else_branch": "$c"},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$v2"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|30313-1", FHIR_PAYLOAD_ALT2),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    a, b, c = program.steps[0], program.steps[3], program.steps[6]
    assert a.unit_id == "fhir:Observation:LOINC|30313-1"  # LLM が書いたとおり第3候補
    assert b.needs_input is True
    assert c.needs_input is True  # 直前（第1候補）より進んでいても追い越されている
    assert b.unit_id is None
    assert c.unit_id is None
    # 取得の順序は逆転しているが、branch の結線そのもの（空判定で見た値を採る）は正しい。
    # 結線の検査と優先順の検査は別の性質なので、ここでは branch は止めない。
    assert fixes == ["b:needs_input(優先順の逆転)", "c:needs_input(優先順の逆転)"]
    assert not program.steps[8].needs_input


def test_enforce_blocks_the_branch_that_discards_the_fetched_value():
    """空判定で見た値に値があるのに、別の候補側を採る `branch` を止めること。

    `when: "$has_a"` は「a に値があるか」を見ている。真のときに採る側（`then`）が `$b` だと、
    a が値を取れているのに **skip されて None の第2候補側**が採られ、取得できた値が失われる。
    取得 step 側の検査はこの形を検出できない（並び順・引数・典拠はすべて正しい）。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            # then と else_branch が入れ替わっている
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b, v = program.steps[0], program.steps[3], program.steps[4]
    assert a.unit_id == "fhir:Observation:LOINC|6690-2"  # 取得側は正しい
    assert b.unit_id == "fhir:Observation:LOINC|718-7"
    assert not a.needs_input
    assert not b.needs_input
    assert v.needs_input is True  # 結線が誤っている
    assert fixes == ["v:needs_input(分岐の結線)"]


@pytest.mark.parametrize(
    "middle",
    [
        pytest.param({"op": "select", "id": "m", "source": "$a", "pick": "first"}, id="select"),
        pytest.param(
            {
                "op": "transform",
                "id": "m",
                "input": "$a",
                "rule": {"type": "unit_conversion", "formula": "value/18.01", "decimals": 2},
            },
            id="transform",
        ),
        pytest.param(
            {"op": "aggregate", "id": "m", "source": "$a", "func": "first"}, id="aggregate"
        ),
        pytest.param(
            {
                "op": "csv_extract",
                "id": "m",
                "path": "ef.csv",
                "connection_id": "report_fs",
                "key_column": "PID",
                "column": "V",
                "match": [{"column": "CODE", "value": "$a"}],
            },
            id="csv_match_value",
        ),
    ],
)
def test_enforce_keeps_the_chain_through_value_passing_steps(middle: dict):
    """`input` / `source` / `match[].value` を経由しても参照の連鎖が切れないこと。

    プロンプトは「起点取得 → 絞り込み(select) → 変換(transform)」の並びを指示している。
    これらのスロットを辿らないと `guard` の連鎖（`$no_a` → `$has_a` → `$m` → `$a`）が
    途中で切れ、取得の順序の検査が丸ごと効かなくなる。
    """
    program = _program(
        [
            # 第2候補を先に試している
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2, "multi": True},
            middle,
            {"op": "compare", "id": "has_a", "left": "$m", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$m", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    b = program.steps[4]
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_keeps_the_chain_through_a_branch_when_slot_only():
    """`branch` の `when` だけを経由する連鎖でも切れないこと。

    `then` / `else_branch` がリテラルの `branch` は、取得 step へ辿れる経路が `when` しか
    ない。`when` を辿らない実装だと、その `branch` の値を空判定してフォールバックする形で
    取得の順序の検査が黙って無効になる。
    """
    program = _program(
        [
            # 第2候補を先に試している
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            # then / else_branch はリテラル（連鎖は when 経由だけ）
            {"op": "branch", "id": "flag", "when": "$has_a", "then": "1", "else_branch": "0"},
            {"op": "compare", "id": "has_flag", "left": "$flag", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_flag", "left": "$has_flag", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_flag", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_flag", "then": "$flag", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    b = program.steps[5]
    assert b.needs_input is True  # 第2候補のあとに第1候補へ戻っている
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_keeps_the_chain_through_a_fetch_without_index_candidates():
    """索引由来の候補を持たない取得 step を間に挟んでも、試行の連鎖が切れないこと。

    候補の無いファミリの取得（手入力所在など）で記録を切ると、その1 step を挟んだだけで
    後続の逆転が見えなくなる。
    """
    program = _program(
        [
            # 第2候補（FHIR の別コード）を先に試す
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            # 候補の無いファミリ（FILE）の取得を挟む
            {
                "op": "file_extract",
                "id": "m",
                "guard": "$no_a",
                "path_pattern": "manual/{{patient_id}}.txt",
                "connection_id": "report_fs",
            },
            {"op": "compare", "id": "has_m", "guard": "$no_a", "left": "$m", "cmp": "is_not_empty"},
            {
                "op": "compare",
                "id": "no_m",
                "guard": "$no_a",
                "left": "$has_m",
                "cmp": "==",
                "right": False,
            },
            {"op": "fhir_extract", "id": "b", "guard": "$no_m", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v2", "when": "$has_m", "then": "$m", "else_branch": "$b"},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$v2"},
        ]
    )
    specs = _two_fhir_candidates()  # FHIR の2候補のみ（FILE の候補は無い）

    fixes = enforce_derived_op_args(program, specs)

    b = program.steps[6]
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_detects_inversion_even_with_a_backward_reference():
    """後方参照を含むプログラムでも参照の連鎖を辿れること。

    `enforce_derived_op_args` は `validate_program` より前に走るので、この時点では
    「参照は前方のみ」を前提にできない。1パスで閉包を組むと連鎖が切れ、検査が黙って
    無効になる。
    """
    program = _program(
        [
            # no_a が後方（has_a より前）にある
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[3].needs_input is True
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_does_not_block_switching_tables_by_a_fetched_code():
    """区分値を取ってから参照表を切り替える形（プロンプトが例示）を逆転扱いにしないこと。"""
    program = DSLProgram(
        params=[
            {
                "name": "ref_tbl",
                "kind": "identifier",
                "value": "$kubun",
                "allowed": ["DEMO.LAB", "DEMO.LAB2"],
            }
        ],
        steps=[
            {
                "op": "sql_extract",
                "id": "kubun",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "fhir_extract", "id": "v", "fhir_query": _FHIR_Q1},
        ],
        **{"return": "$v"},
    )
    # 候補の優先順は FHIR → DB だが、区分値（DB）を先に取る
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert not program.steps[0].needs_input
    assert not program.steps[1].needs_input
    assert fixes == []


@pytest.mark.parametrize("ref", ["$a", "{{a}}"])
def test_enforce_blocks_inversion_through_an_llm_extract_step(ref: str):
    """取得 → `llm_extract` で値を取り出す**推奨形**でも逆転を検出すること。

    `llm_extract` は前段を `prompt` の `$id`（互換の `{{id}}`）で参照する。その参照を
    辿らないと `guard`（`$no_a` → `$has_a` → `$xa`）の連鎖が `llm_extract` で切れ、
    「1候補が複数 step になる推奨形」で検査が丸ごと
    効かなくなる。`{{id}}` は検証が受理する書き方なので両方で測る。
    """
    program = _program(
        [
            # 第2候補を先に試している
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "llm_extract", "id": "xa", "prompt": f"{ref} から芽球割合を取り出す"},
            {"op": "compare", "id": "has_a", "left": "$xa", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$xa", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[4]
    assert a.unit_id == "fhir:Observation:LOINC|718-7"
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_does_not_block_a_guard_that_compares_a_fetched_value():
    """`guard` が取得値の**大小比較**に依存する条件付き実行を逆転扱いにしないこと。

    プロンプトの厳守事項3 は「判定(llm_judge/compare) → guard 付き取得 → branch」の並びを
    指示している。`guard` が前段の取得値に依存していても、**空判定を経ていなければ
    フォールバックではない**。ここを区別しないと、`compare` で書いたか `llm_judge` で
    書いたかという違いだけで正当なプランが人の手待ちになる。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "a",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "high", "left": "$a", "cmp": ">=", "right": 20},
            {"op": "fhir_extract", "id": "b", "guard": "$high", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$high", "then": "$b", "else_branch": "$a"},
        ]
    )
    # 候補の優先順は FHIR → DB。DB 側の step が先に来る。
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[2]
    assert a.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert b.unit_id == "fhir:Observation:LOINC|6690-2"
    assert not a.needs_input
    assert not b.needs_input
    assert fixes == []


def test_enforce_does_not_block_a_downstream_branch_on_the_fetched_value():
    """フォールバックの後段にある**別目的の分岐**を優先順の判定対象にしないこと。

    定型は `when` に空判定の結果を直に置く。推移的に依存を見ると、取れた値の大小で
    分けるだけの分岐まで「優先順の逆転」と判定されてしまう。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v0", "when": "$has_a", "then": "$a", "else_branch": "$b"},
            # 取れた値が閾値を超えるかで表示値を変える（優先順とは無関係の分岐）
            {"op": "compare", "id": "over", "left": "$v0", "cmp": ">", "right": 100},
            {"op": "branch", "id": "v", "when": "$over", "then": "$b", "else_branch": "$v0"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not program.steps[6].needs_input
    assert fixes == []


def test_enforce_sees_through_a_transform_when_checking_the_branch_routing():
    """`branch` の片側が `transform` を経由していても、その先の step で結線を判定すること。

    「起点取得 → 変換」はプロンプトが指示する並びなので、変換を挟んだ側を「何を指しているか
    分からない」として見送ると、定型どおりに書かれたプランで誤った結線を検出できなくなる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {
                "op": "transform",
                "id": "t",
                "input": "$a",
                "rule": {"type": "unit_conversion", "formula": "value/18.01", "decimals": 2},
            },
            # then と else_branch が入れ替わっている（第1候補は変換を経由している）
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "$t"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[5].needs_input is True
    assert fixes == ["v:needs_input(分岐の結線)"]


def test_enforce_allows_the_branch_that_tests_the_second_candidate():
    """`when` が**第2候補**の空判定を見る書き方も正しい結線として通すこと。

    `when: "$has_b", then: "$b", else_branch: "$a"` は、b に値があれば b、無ければ a を採る。
    第1候補が値を取れているときは b が skip され `has_b` も skip（None）になるので
    `else_branch` の a が採られる。**プロンプトの規範をどれも破っていない正当な形**である
    （guard を併用し、`when` に `is_not_empty` の結果を置き、判定 step にも guard を伝播）。
    「then 側が優先順の低い候補だから逆転」と判定すると、この形が人の手待ちになる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "has_b", "guard": "$no_a", "left": "$b", "cmp": "is_not_empty"},
            {"op": "branch", "id": "v", "when": "$has_b", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[0].unit_id == "fhir:Observation:LOINC|6690-2"
    assert program.steps[3].unit_id == "fhir:Observation:LOINC|718-7"
    assert not any(st.needs_input for st in program.steps)
    assert fixes == []


@pytest.mark.parametrize(
    ("cmp", "then_side", "else_side", "blocked"),
    [
        # is_empty は極性が逆になるので、then が代替候補・else_branch が第1候補で正しい
        ("is_empty", "$b", "$a", False),
        ("is_empty", "$a", "$b", True),
        ("is_not_empty", "$a", "$b", False),
        ("is_not_empty", "$b", "$a", True),
    ],
)
def test_enforce_checks_the_branch_routing_for_both_polarities(
    cmp: str, then_side: str, else_side: str, blocked: bool
):
    """`is_empty` / `is_not_empty` のどちらで書いても、結線の正しさを同じ基準で見ること。

    `is_empty` は後から足した演算子で、検証も通る。極性を見ずに「then 側が第1候補か」で
    判定すると、正しい `is_empty` の結線を弾き、誤った結線を見逃す（判定が反転する）。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "t", "left": "$a", "cmp": cmp},
            {
                "op": "compare",
                "id": "no_a",
                "left": "$t",
                "cmp": "==",
                # 「空だったとき」に発火する guard にする（is_not_empty なら偽・is_empty なら真）
                "right": cmp == "is_empty",
            },
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$t", "then": then_side, "else_branch": else_side},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    v = program.steps[4]
    if blocked:
        assert v.needs_input is True
        assert fixes == ["v:needs_input(分岐の結線)"]
    else:
        assert not v.needs_input
        assert fixes == []


def test_enforce_allows_the_branch_routing_through_a_transform():
    """`then` 側が `transform` を経由して空判定の値に届いていれば正しい結線とすること。

    「起点取得 → 変換」はプロンプトが指示する並びである。参照の連鎖を辿らずに直接一致だけを
    見ると、この正当な形が結線の誤りとして人の手待ちになる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {
                "op": "transform",
                "id": "t",
                "input": "$a",
                "rule": {"type": "unit_conversion", "formula": "value/18.01", "decimals": 2},
            },
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$t", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any(st.needs_input for st in program.steps)
    assert fixes == []


def test_enforce_blocks_inversion_when_the_guard_uses_is_empty_directly():
    """`guard` を `is_empty` の結果に直結した短い書き方でも逆転を検出すること。

    `has_a` → `no_a` の2段を踏まず `{"left":"$a","cmp":"is_empty"}` を `guard` に直結する
    書き方は検証を通る（後から足した演算子）。`is_not_empty` だけを空判定として扱うと、
    この書き方では検査が丸ごと効かない。
    """
    program = _program(
        [
            # 第2候補を先に試している
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "empty_a", "left": "$a", "cmp": "is_empty"},
            {"op": "fhir_extract", "id": "b", "guard": "$empty_a", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$empty_a", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    b = program.steps[2]
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_follows_the_chain_when_the_compare_operands_are_reversed():
    """`compare` の左右を入れ替えて書かれていても参照の連鎖を辿れること。

    `{"left": false, "cmp": "==", "right": "$has_a"}` は定型と同じ意味で、検証も通る。
    `right` を辿らないとこの書き方で連鎖が切れる。
    """
    program = _program(
        [
            # 第2候補を先に試している
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": False, "cmp": "==", "right": "$has_a"},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    b = program.steps[3]
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes


def test_enforce_allows_the_branch_that_encodes_presence_as_a_literal():
    """値の有無をリテラルで符号化する分岐（`then: "1"`）を結線の誤りとしないこと。

    値を捨てているのではなく、値の有無そのものを記録する正当な書き方である。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "1", "else_branch": "2"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not program.steps[2].needs_input
    assert fixes == []


@pytest.mark.parametrize(
    ("cmp", "then_side", "else_side"),
    [
        # 取得した値がリテラルに置き換わる形。正側・空側のどちらがリテラルでも同じ害である。
        ("is_not_empty", "実施", "$b"),
        ("is_not_empty", "$b", "該当なし"),
        ("is_empty", "$b", "実施"),
        ("is_empty", "該当なし", "$b"),
    ],
)
def test_enforce_blocks_the_branch_that_replaces_the_fetched_value_with_a_literal(
    cmp: str, then_side: str, else_side: str
):
    """フォールバックなのに、片側がリテラルで取得した値が捨てられる形を止めること。

    第1候補に値があると取得した値がリテラルに置き換わる。しかも項目値が入るので
    `item_failure_code` が付かず、取得可能性の集計では充足として数えられる。
    **リテラルがどちら側にあっても同じ害**なので、片側だけを見て見送ると鏡像を見逃す。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "t", "left": "$a", "cmp": cmp},
            {
                "op": "compare",
                "id": "no_a",
                "left": "$t",
                "cmp": "==",
                # 「空だったとき」に発火する guard にする（is_not_empty なら偽・is_empty なら真）
                "right": cmp == "is_empty",
            },
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$t", "then": then_side, "else_branch": else_side},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    v = program.steps[4]
    assert v.needs_input is True
    assert fixes == ["v:needs_input(分岐の結線)"]
    # 人に出す説明が事実と合っていること（誤った説明は監査証跡に残る）
    assert "$a" in (v.reasoning or "")
    assert "失敗としても記録され" in (v.reasoning or "")


def test_enforce_blocks_the_inner_branch_of_a_nested_fallback():
    """入れ子の**内側**の分岐が誤っている形を止めること。

    誤結線のテストがどれも `branch` 1個のプログラムだと、検査を「最初の branch だけ」に
    限定する退行に気づけない。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "has_b", "guard": "$no_a", "left": "$b", "cmp": "is_not_empty"},
            {
                "op": "compare",
                "id": "no_b",
                "guard": "$no_a",
                "left": "$has_b",
                "cmp": "==",
                "right": False,
            },
            {"op": "fhir_extract", "id": "c", "guard": "$no_b", "fhir_query": _FHIR_Q3},
            # 内側だけ then と else_branch が入れ替わっている
            {"op": "branch", "id": "v2", "when": "$has_b", "then": "$c", "else_branch": "$b"},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$v2"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|30313-1", FHIR_PAYLOAD_ALT2),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    v2, v = program.steps[7], program.steps[8]
    assert v2.needs_input is True
    assert not v.needs_input  # 外側は正しい
    assert fixes == ["v2:needs_input(分岐の結線)"]
    # 説明には**その分岐が見ている候補**（第2候補）を出す（第1候補に固定してはいけない）
    assert "$b" in (v2.reasoning or "")


def test_enforce_takes_the_highest_priority_already_tried_when_several_are_gated():
    """空判定が複数の試行を見ているとき、**その中の最も優先順の高い候補**と比べること。

    平坦な `branch` 連鎖では `is_not_empty($v2)` が第1候補と第3候補の両方を見る。ここで
    最小（先に試した候補）を採ると、第2候補を最後に試す計画（第3候補より後）を見逃す。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},  # 第1候補
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q3},  # 第3候補
            {"op": "branch", "id": "v2", "when": "$has_a", "then": "$a", "else_branch": "$b"},
            {"op": "compare", "id": "has_v2", "left": "$v2", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_v2", "left": "$has_v2", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "c", "guard": "$no_v2", "fhir_query": _FHIR_Q2},  # 第2候補
            {"op": "branch", "id": "v", "when": "$has_v2", "then": "$v2", "else_branch": "$c"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|30313-1", FHIR_PAYLOAD_ALT2),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    c = program.steps[7]
    assert c.needs_input is True  # 第3候補のあとに第2候補を試している
    assert c.unit_id is None
    assert "c:needs_input(優先順の逆転)" in fixes


def test_enforce_blocks_a_later_branch_when_the_first_one_is_correct():
    """**後段**の分岐が誤っている形を止めること（平坦な連鎖の2段目）。

    誤結線のテストで誤った分岐が常に最初の `branch` だと、検査を「最初の branch だけ」に
    限定する退行に気づけない。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            # 1段目は正しい
            {"op": "branch", "id": "v2", "when": "$has_a", "then": "$a", "else_branch": "$b"},
            {"op": "compare", "id": "has_v2", "left": "$v2", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_v2", "left": "$has_v2", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "c", "guard": "$no_v2", "fhir_query": _FHIR_Q3},
            # 2段目は then と else_branch が入れ替わっている
            {"op": "branch", "id": "v", "when": "$has_v2", "then": "$c", "else_branch": "$v2"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|30313-1", FHIR_PAYLOAD_ALT2),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    v2, v = program.steps[4], program.steps[8]
    assert not v2.needs_input
    assert v.needs_input is True
    assert fixes == ["v:needs_input(分岐の結線)"]


def test_enforce_reserves_the_candidate_even_when_the_step_is_blocked():
    """人へ回した step が使った候補も**予約**すること。

    予約しないと、後続の未照合 step が同じ第1候補を黙って焼き込んで確定してしまう
    （3つ並べたときに2つ目だけが人へ回り、3つ目が第1候補の三度引きになる）。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "a",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            # 引数を書いていない（第1候補が予約済みなので人へ回る）
            {"op": "sql_extract", "id": "b", "guard": "$no_a"},
            {"op": "sql_extract", "id": "c", "guard": "$no_a"},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    b, c = program.steps[3], program.steps[4]
    assert b.needs_input is True
    assert c.needs_input is True  # 予約が効いていれば3つ目も人へ回る
    assert b.sql is None
    assert c.sql is None
    assert fixes == ["b:needs_input(候補重複)", "c:needs_input(候補重複)"]


def test_enforce_does_not_block_when_the_tested_value_is_not_a_candidate():
    """空判定の対象が所在候補でないなら、`when` 側の fail-closed を適用しないこと。

    候補と無関係な値の空判定をゲートに使う分岐は、候補の値を失っていない。
    """
    program = _program(
        [
            # 候補ではない別表のフラグ
            {
                "op": "sql_extract",
                "id": "flag",
                "sql": "SELECT STATUS FROM DEMO.FLAGS WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_flag", "left": "$flag", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_flag", "left": "$has_flag", "cmp": "==", "right": "なし"},
            {"op": "fhir_extract", "id": "b", "guard": "$no_flag", "fhir_query": _FHIR_Q2},
            {
                "op": "branch",
                "id": "v",
                "when": "$no_flag",
                "then": "$b",
                "else_branch": "該当なし",
            },
        ]
    )
    # 候補は FHIR のみ（flag の sql_extract は DB 候補が無いので候補にならない）
    specs = derive_op_args_list(
        [_cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert not any("向き不明" in f for f in fixes), fixes


def test_enforce_does_not_open_the_gate_via_a_non_candidate_emptiness():
    """**候補でない値**の空判定で、fail-closed の門が開かないこと。

    `when` が複数の空判定を参照していると、そのうち候補と無関係なもの（別表のフラグ）を
    根拠に「代替候補がある」と判定してしまう余地がある。門の中でも空判定の対象が候補で
    あることを確かめないと、`guard` がそのフラグに依存する正当な条件付き取得
    （フォールバックではない）が人の手待ちになる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            # 候補ではない別表のフラグ（DB 候補が無いので候補にならない）
            {
                "op": "sql_extract",
                "id": "flag",
                "sql": "SELECT STATUS FROM DEMO.FLAGS WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_flag", "left": "$flag", "cmp": "is_not_empty"},
            # 向きが読めない条件（両方の空判定を参照する）
            {"op": "llm_judge", "id": "cond", "prompt": "$has_a と $has_flag から判定する"},
            # 走る条件はフラグの有無（a の空振りとは無関係）
            {"op": "fhir_extract", "id": "b", "guard": "$has_flag", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$cond", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any("向き不明" in f for f in fixes), fixes
    assert not program.steps[6].needs_input


def test_enforce_does_not_treat_the_tested_candidate_itself_as_the_alternative():
    """代替候補は「空判定の対象とは別の候補」であること。

    自分自身を代替候補と数えると、代替が無いプランでも門が開いて人の手待ちになる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": "なし"},
            # 空だったときに走るのは a 自身の後段（別候補の取得は無い）
            {"op": "llm_extract", "id": "x", "guard": "$no_a", "prompt": "$a から取り出す"},
            {"op": "branch", "id": "v", "when": "$no_a", "then": "$x", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any("向き不明" in f for f in fixes), fixes


def test_enforce_follows_only_when_of_a_paraphrase_branch():
    """真偽の言い換えとみなした `branch` からは **`when` だけ**を辿ること。

    その `branch` の他のスロット（`input` 等）まで辿ると、条件と無関係な参照で連鎖がつながり、
    正当なプランが「向き不明」で止まる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {
                "op": "sql_extract",
                "id": "flag",
                "sql": "SELECT STATUS FROM DEMO.FLAGS WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_flag", "left": "$flag", "cmp": "is_not_empty"},
            # 条件は flag の有無（候補の空判定ではない）。`input` は条件に関与しない
            {
                "op": "branch",
                "id": "cond",
                "when": "$has_flag",
                "then": 1,
                "else_branch": 0,
                "input": "$has_a",
            },
            {"op": "fhir_extract", "id": "b", "guard": "$cond", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$has_flag", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any("向き不明" in f for f in fixes), fixes


def test_enforce_does_not_block_a_threshold_guard_when_the_value_is_on_the_else_side():
    """値を運ぶ側が `else_branch` でも、その `branch` は境界のままであること。

    片側だけでリテラル判定すると、値の運搬を「真偽の言い換え」と誤認して連鎖がつながり、
    閾値で取得元を変える正当なプランが人の手待ちになる（`then` 側だけを見る実装で起きる）。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            # 値は else_branch 側に来る
            {"op": "branch", "id": "v0", "when": "$has_a", "then": "0", "else_branch": "$a"},
            {"op": "compare", "id": "gt", "left": "$v0", "cmp": ">", "right": 100},
            {"op": "fhir_extract", "id": "b", "guard": "$gt", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$gt", "then": "$b", "else_branch": "$v0"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any("向き不明" in f for f in fixes), fixes
    assert not any(st.needs_input for st in program.steps)


def test_enforce_does_not_block_a_threshold_guard_downstream_of_a_branch():
    """取れた値の大小で取得元を変える `guard` を「向き不明」で止めないこと。

    連鎖を `branch` を越えて辿ると、`branch` で選んだ値の大小を見る条件まで「空判定に
    依存している」と判定され、プロンプトの厳守事項3 に従った正当なプランが人の手待ちになる。
    真偽の言い換えは `branch` を通らないので、そこが境界になる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "branch", "id": "v0", "when": "$has_a", "then": "$a", "else_branch": "0"},
            {"op": "compare", "id": "gt", "left": "$v0", "cmp": ">", "right": 100},
            # 値が閾値を超えるときだけ別ソースを引く（フォールバックではない）
            {"op": "fhir_extract", "id": "b", "guard": "$gt", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$gt", "then": "$b", "else_branch": "$v0"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any("向き不明" in f for f in fixes), fixes
    assert not any(st.needs_input for st in program.steps)


def test_enforce_rejects_a_copy_paste_of_the_second_candidate_between_unguarded_steps():
    """**第2候補**の引数を無条件の2 step へコピペした形も人へ回すこと。

    コピペ検出を「使用済みの候補と一致したときだけ」にすると、この形では2つ目の step が
    「引数を書かなかった step」と区別できず、**第1候補の引数が黙って焼き込まれる**
    （LLM が一度も書いていない別のコードの取得に静かに差し替わる）。術前値・術後値のように
    無条件の2 step になるのは、この保護が本来守ろうとしている形である。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "fhir_extract", "id": "b", "fhir_query": _FHIR_Q2},
        ],
        ret="$b",
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[1]
    # **この形は両 step とも人へ回る。** 複数候補のある項目で
    # 取得 step が2つとも無条件なら、どちらの値を採るかは branch の書き方次第で優先順と
    # 逆にもなる（しかも痕跡が残らない）ので、コピペかどうかに依らず確定しない。
    # コピペの保護（`候補重複`）は、少なくとも一方が guard を持つ形——`_fhir_fallback_program`
    # を使う一連のテスト——で引き続き効いている。
    assert a.needs_input is True
    assert a.unit_id is None
    assert b.needs_input is True
    assert b.unit_id is None
    # コピペされた生成物を下書きとして残さない（残すと、人が needs_input を外した瞬間に
    # 「A を試して、空なら A をもう一度試す」が実行される）
    assert b.fhir_query is None
    assert fixes == [
        "a:needs_input(guard無しの複数候補取得)",
        "b:needs_input(guard無しの複数候補取得)",
    ]


def test_enforce_reserves_the_candidate_of_a_step_blocked_by_inversion():
    """**優先順の逆転で**人へ回した step が使った候補も予約すること。

    逆転で止めた step の候補を予約しないと、後続の未照合 step がその候補を黙って焼き込んで
    確定してしまう（逆転は人へ回ったのに、三度目の取得だけが素通りする）。
    """
    program = _program(
        [
            # 第2候補を先に試している
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            # 第1候補（逆転として人へ回る）
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_b", "guard": "$no_a", "left": "$b", "cmp": "is_not_empty"},
            {
                "op": "compare",
                "id": "no_b",
                "guard": "$no_a",
                "left": "$has_b",
                "cmp": "==",
                "right": False,
            },
            # 引数を書いていない（第1候補が予約済みなので人へ回るべき）
            {"op": "fhir_extract", "id": "c", "guard": "$no_b"},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    b, c = program.steps[3], program.steps[6]
    assert b.needs_input is True
    assert c.needs_input is True  # 予約が効いていれば第1候補を焼き込まない
    assert c.fhir_query is None
    assert c.unit_id is None
    # **理由まで見る。** 逆転が先に発火すると予約の有無を観測できない（上位層が先に止めると
    # 下位層が無検証になる形）。`c` は guard が第2候補の空判定に依存しており逆転ではない。
    assert fixes == ["b:needs_input(優先順の逆転)", "c:needs_input(候補重複)"]


def test_enforce_counts_a_blocked_step_as_a_candidate_fetch():
    """人へ回した取得 step も「候補の取得」として数えること。

    そうしないと、片側が block された step・もう片側がリテラルの分岐で結線の検査が
    働かなくなる（block と誤結線が同居するプログラム）。
    """
    program = _program(
        [
            {
                "op": "fhir_extract",
                "id": "a",
                "fhir_query": _FHIR_Q1,
            },
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            # 第1候補のクエリをコピペしている（候補重複で人へ回る）
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "該当なし"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    b, v = program.steps[3], program.steps[4]
    assert b.needs_input is True  # 候補重複
    assert v.needs_input is True  # 結線も誤っている
    assert fixes == ["b:needs_input(候補重複)", "v:needs_input(分岐の結線)"]


def test_enforce_does_not_block_a_branch_between_non_candidate_fetches():
    """どちらの側も所在候補の取得へ辿り着かない分岐は対象外にすること。

    空判定を含んでいても、候補の値を選んでいないならフォールバックではない。ここを
    「取得 step でありさえすればフォールバック」と広げると、索引に無い所在（手入力・
    別項目由来）を扱う分岐まで人の手待ちになる。
    """
    program = _program(
        [
            {
                "op": "file_extract",
                "id": "d1",
                "path_pattern": "notes/{{patient_id}}/a.txt",
                "connection_id": "report_fs",
            },
            {"op": "compare", "id": "has_d1", "left": "$d1", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_d1", "left": "$has_d1", "cmp": "==", "right": False},
            {
                "op": "file_extract",
                "id": "d2",
                "guard": "$no_d1",
                "path_pattern": "notes/{{patient_id}}/b.txt",
                "connection_id": "report_fs",
            },
            # 結線としては誤っているが、どちらの側も所在候補ではない
            {"op": "branch", "id": "v", "when": "$has_d1", "then": "$d2", "else_branch": "$d1"},
        ]
    )
    # 候補は FHIR だけ（FILE ファミリには候補が無い）
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any(st.needs_input for st in program.steps)
    assert fixes == []


@pytest.mark.parametrize(
    ("cmp", "then_side", "else_side"),
    [
        # 空側がリテラルなので、代替候補を取得しているのにその値が捨てられる
        ("is_not_empty", "$a", "該当なし"),
        ("is_empty", "該当なし", "$a"),
    ],
)
def test_enforce_blocks_the_branch_that_discards_the_alternative_candidate(
    cmp: str, then_side: str, else_side: str
):
    """空側がリテラルで、代替候補の取得値が捨てられる形を止めること。

    正側（値があるときに採る側）は正しく第1候補を指しているが、第1候補が空のときに採るのが
    リテラルなので、**実際に外部アクセスして取得した第2候補の値が失われる**。しかも項目値が
    入るので `item_failure_code` が付かず、取得可能性の集計では充足として数えられる。
    正側だけを検査すると、この鏡像を見逃す。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "t", "left": "$a", "cmp": cmp},
            {"op": "compare", "id": "no_a", "left": "$t", "cmp": "==", "right": cmp == "is_empty"},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$t", "then": then_side, "else_branch": else_side},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    v = program.steps[4]
    assert v.needs_input is True
    assert fixes == ["v:needs_input(分岐の結線)"]
    assert "代替候補" in (v.reasoning or "")
    assert "b" in (v.reasoning or "")


# 「第1候補が空だったか」の書き方を **`OpType.ALL` から駆動して**並べる。
#
# 12周かけて分かったのは「同じ意味の綴りは非有界で、列挙で追う限り必ず次の1形が出る」こと
# である。そこで**真偽を運べる op それぞれについて1段包む形**を機械的に用意し、`OpType.ALL`
# との突き合わせで**op が増えたら分類を強制**する（下の
# `test_every_op_is_classified_for_the_negation_forms`）。値を運べない取得系は None にする。
#
# この検査が約束している不変条件は「**この生成規則が並べるどの形でも、極性が読めるか
# `needs_input` で止まるかのどちらかである**」（読めないのに素通りする形が無い）。
_TRUTH_WRAPS: dict[str, dict[str, list[dict]] | None] = {
    OpType.COMPARE: {
        # 定型とその同義表記（左右の入れ替え・`!=`・数値・数値文字列）
        "eq_false": [
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False}
        ],
        "false_eq": [
            {"op": "compare", "id": "no_a", "left": False, "cmp": "==", "right": "$has_a"}
        ],
        "ne_true": [{"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "!=", "right": True}],
        "eq_zero": [{"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": 0}],
        "eq_zero_str": [
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": "0"}
        ],
        "ne_one": [{"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "!=", "right": 1}],
        # 同じ値を見る別の空判定
        "is_empty": [{"op": "compare", "id": "no_a", "left": "$a", "cmp": "is_empty"}],
        # 実行時に真偽と一致しないリテラル（常に偽／常に真になる）
        "eq_two": [{"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": 2}],
        "ne_two": [{"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "!=", "right": 2}],
        # 数値にできないリテラル
        "eq_text": [
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": "なし"}
        ],
        # 真偽の言い換えを2段重ねた形
        "double": [
            {"op": "compare", "id": "yes_a", "left": "$has_a", "cmp": "==", "right": True},
            {"op": "compare", "id": "no_a", "left": "$yes_a", "cmp": "==", "right": False},
        ],
        "in_list": [
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "in", "right": [False]}
        ],
    },
    OpType.BRANCH: {
        # 両側がリテラルの `branch` は取得した値を運んでいないので、真偽の言い換えとして
        # `when` を辿る（下の4形すべてが対象）。値を運ぶ `branch`（片側が `$id` 参照）は
        # 連鎖の境界なので、取れた値の大小で分ける後段の条件は巻き込まない。
        "branch_negate": [
            {"op": "branch", "id": "no_a", "when": "$has_a", "then": False, "else_branch": True}
        ],
        "branch_negate_01": [
            {"op": "branch", "id": "no_a", "when": "$has_a", "then": 0, "else_branch": 1}
        ],
        # 片側だけが真偽リテラルの形。実行時は `_truthy(0)=False` / `_truthy("該当なし")=True`
        # なので否定として動く（判定はリテラル性だけで行い、真偽の差は見ない）
        "branch_negate_mixed": [
            {"op": "branch", "id": "no_a", "when": "$has_a", "then": 0, "else_branch": "該当なし"}
        ],
        # 両側の真偽が同じ形。後段の比較（`== "2"`）で極性が取り出せるので、これも辿る
        "branch_constant_then_compare": [
            {"op": "branch", "id": "code", "when": "$has_a", "then": "1", "else_branch": "2"},
            {"op": "compare", "id": "no_a", "left": "$code", "cmp": "==", "right": "2"},
        ],
        # branch で真偽を通したうえで比較する形
        "branch_then_eq_false": [
            {"op": "branch", "id": "same_a", "when": "$has_a", "then": True, "else_branch": False},
            {"op": "compare", "id": "no_a", "left": "$same_a", "cmp": "==", "right": False},
        ],
        "branch_then_judge": [
            {"op": "branch", "id": "same_a", "when": "$has_a", "then": True, "else_branch": False},
            {"op": "llm_judge", "id": "no_a", "prompt": "$same_a は偽か"},
        ],
    },
    OpType.LLM_JUDGE: {
        "judge1": [{"op": "llm_judge", "id": "no_a", "prompt": "$has_a は偽か"}],
        "judge2": [
            {"op": "llm_judge", "id": "j1", "prompt": "$has_a は偽か"},
            {"op": "llm_judge", "id": "no_a", "prompt": "$j1 は真か"},
        ],
    },
    OpType.TRANSFORM: {
        "transform": [
            {
                "op": "transform",
                "id": "no_a",
                "input": "$has_a",
                "rule": {"type": "code_map", "map": {"True": "false", "False": "true"}},
            }
        ],
        "transform2": [
            {
                "op": "transform",
                "id": "t1",
                "input": "$has_a",
                "rule": {"type": "code_map", "map": {"True": "1", "False": "0"}},
            },
            {
                "op": "transform",
                "id": "no_a",
                "input": "$t1",
                "rule": {"type": "code_map", "map": {"1": "false", "0": "true"}},
            },
        ],
    },
    OpType.SELECT: {
        "select_wrap": [
            {"op": "select", "id": "no_a", "source": "$has_a", "pick": "first"},
        ]
    },
    OpType.AGGREGATE: {
        "aggregate_wrap": [
            {"op": "aggregate", "id": "no_a", "source": "$has_a", "func": "first"},
        ]
    },
    OpType.LLM_EXTRACT: {
        "extract_wrap": [{"op": "llm_extract", "id": "no_a", "prompt": "$has_a を反転して"}],
    },
    # 取得系は前段の真偽を運べない（引数へ差し込むには params が要る）
    OpType.SQL_EXTRACT: None,
    OpType.FHIR_EXTRACT: None,
    OpType.EXTERNAL_DB_EXTRACT: None,
    OpType.FILE_EXTRACT: None,
    OpType.CSV_EXTRACT: None,
}

_NEGATION_FORMS: dict[str, list[dict]] = {
    name: steps for wraps in _TRUTH_WRAPS.values() if wraps for name, steps in wraps.items()
}


def test_every_op_is_classified_for_the_negation_forms():
    """**op が増えたら「真偽を運べるか」の分類を強制する。**

    否定の書き方を手書きの列挙で持つと、op が増えたときに盲点になる（`branch` を真偽の
    言い換えの連鎖に含めていなかったため、`branch` を境界にした変更が見逃しを作った）。
    """
    assert set(_TRUTH_WRAPS) == set(OpType.ALL)


def _fallback_with_negation(form: str, *, then_side: str, else_side: str) -> DSLProgram:
    """否定の書き方を差し替えたフォールバック（`branch` の結線も指定できる）。"""
    return _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            *_NEGATION_FORMS[form],
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {
                "op": "branch",
                "id": "v",
                "when": "$has_a",
                "then": then_side,
                "else_branch": else_side,
            },
        ]
    )


@pytest.mark.parametrize("form", sorted(_NEGATION_FORMS))
def test_no_negation_form_silently_passes_a_miswired_fallback(form: str):
    """**どの書き方でも、誤結線が黙って通ることは無い**（読めるか、止まるか）。

    これがこの検査の健全性の担保である。表記の列挙で追いかける形（読めない書き方は素通り）
    では、同じ意味の綴りが非有界なので必ず抜けが出る。読めない書き方は
    `needs_input(空振り判定の向き不明)` で人へ回すことで、**見逃しにならない**構造にする。
    """
    # 値があるときにリテラルを採る誤結線
    program = _fallback_with_negation(form, then_side="実施", else_side="$b")

    fixes = enforce_derived_op_args(program, _two_fhir_candidates())

    assert fixes, f"{form}: 誤結線が素通りした"
    assert any(st.needs_input for st in program.steps), f"{form}: 人へ回っていない"
    assert all(
        f.endswith("needs_input(分岐の結線)") or f.endswith("needs_input(空振り判定の向き不明)")
        for f in fixes
    ), f"{form}: {fixes}"


@pytest.mark.parametrize("form", sorted(_NEGATION_FORMS))
def test_a_single_candidate_plan_is_not_blocked_by_an_unreadable_condition(form: str):
    """代替候補が無いプランは、判定の書き方が非定型でも止めないこと。

    失われる値が無いので止める理由が無い。ここを門で絞らないと、候補を1件しか送れない UI
    で判定を非定型に書いただけの項目が人の手待ちになる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            *_NEGATION_FORMS[form],
            # `when` に否定を置く（向きが読めない形では fail-closed の経路を通る）
            {"op": "branch", "id": "v", "when": "$no_a", "then": "該当なし", "else_branch": "$a"},
        ]
    )
    # 候補は1件だけ（代替候補の取得が無い）
    specs = derive_op_args_list(
        [_cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert not any("向き不明" in f for f in fixes), f"{form}: {fixes}"
    assert not program.steps[-1].needs_input, form


@pytest.mark.parametrize("form", sorted(_NEGATION_FORMS))
def test_no_negation_form_is_reported_as_miswired_when_it_is_correct(form: str):
    """正しい結線が「結線の誤り」として報告されることは無いこと（誤検出の方向）。

    向きが読めない書き方では「向き不明」で止まりうる（fail-safe）が、**誤結線という
    事実と違う理由**を付けてはいけない。理由文は監査証跡に残る。
    """
    program = _fallback_with_negation(form, then_side="$a", else_side="$b")

    fixes = enforce_derived_op_args(program, _two_fhir_candidates())

    assert not any("分岐の結線" in f for f in fixes), f"{form}: {fixes}"


def _negation_step(form: str) -> dict:
    """「第1候補が空だったか」を表す step を、検証を通る3つの書き方で作る。"""
    return {
        "eq_false": {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
        "ne_true": {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "!=", "right": True},
        "is_empty": {"op": "compare", "id": "no_a", "left": "$a", "cmp": "is_empty"},
    }[form]


@pytest.mark.parametrize("form", ["eq_false", "ne_true", "is_empty"])
@pytest.mark.parametrize(
    ("then_side", "else_side"),
    [("$b", "$a"), ("実施", "$b"), ("$a", "該当なし")],
)
def test_enforce_checks_the_routing_for_every_negation_form(
    form: str, then_side: str, else_side: str
):
    """「その否定」の書き方が変わっても結線の検査が効くこと。

    `guard` の極性を「`has_a == false`」の字面だけで読むと、`is_empty($a)` を直結した形や
    `!=` で書いた形で**検査が丸ごと切れる**（フォールバックと認識されなくなる）。定型の
    「取得 → is_not_empty → その否定 → guard 付きで次の候補」に従っていても、否定の書き方は
    1つに決まらない。3形 × 誤結線3形の直積で測る。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            _negation_step(form),
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {
                "op": "branch",
                "id": "v",
                "when": "$has_a",
                "then": then_side,
                "else_branch": else_side,
            },
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[4].needs_input is True
    assert fixes == ["v:needs_input(分岐の結線)"]


@pytest.mark.parametrize("form", ["eq_false", "ne_true", "is_empty"])
def test_enforce_allows_the_correct_routing_for_every_negation_form(form: str):
    """否定の書き方が変わっても、正しい結線を誤検出しないこと（肯定側）。"""
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            _negation_step(form),
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any(st.needs_input for st in program.steps)
    assert fixes == []


@pytest.mark.parametrize("reverse_candidates", [False, True])
def test_enforce_does_not_block_a_gate_regardless_of_candidate_order(reverse_candidates: bool):
    """ゲート形（非空のときに走る guard）を、候補の配列順で逆転扱いにしないこと。

    取得の順序の検査が `guard` の向きを見ないと、**プログラムを1文字も変えずに候補配列の順を
    入れ替えるだけで判定が反転する**。しかも理由文（「第1候補より先に代替候補を試す形」）は
    事実と違う（この計画は代替候補を試していない。2つの候補は別のものを指している）。
    """
    gate = _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)
    doc = _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD)
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "consent",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_consent", "left": "$consent", "cmp": "is_not_empty"},
            # 同意があるときだけ FHIR から取る（フォールバックではない）
            {"op": "fhir_extract", "id": "doc", "guard": "$has_consent", "fhir_query": _FHIR_Q1},
            {
                "op": "branch",
                "id": "v",
                "when": "$has_consent",
                "then": "$doc",
                "else_branch": "該当なし",
            },
        ]
    )
    specs = derive_op_args_list([doc, gate] if reverse_candidates else [gate, doc])

    fixes = enforce_derived_op_args(program, specs)

    assert not any(st.needs_input for st in program.steps)
    assert not any("優先順の逆転" in f for f in fixes)


@pytest.mark.parametrize(
    ("when_ref", "then_side", "else_side", "blocked"),
    [
        # when に否定（$no_a）を置くと、値があるときに採るのは else_branch 側になる
        ("$no_a", "$b", "$a", False),
        # 空のときに採るのが then 側なので、ここがリテラルだと代替候補の値が捨てられる
        ("$no_a", "実施済み", "$a", True),
        ("$no_a", "$b", "実施済み", True),
        ("$no_a", "$a", "$b", True),
        # when に真偽ラッパ（$yes_a = has_a == true）を置いた形は $has_a と同じ
        ("$yes_a", "$a", "$b", False),
        ("$yes_a", "実施済み", "$b", True),
    ],
)
def test_enforce_checks_the_routing_when_the_condition_is_negated_or_wrapped(
    when_ref: str, then_side: str, else_side: str, blocked: bool
):
    """`when` に否定・真偽ラッパを置いた形でも結線を検査すること。

    `when: "$no_a"` は「a が空のとき」に `then` を採る。したがって**値があるときに採るのは
    `else_branch` 側**である。`when` が空判定を直参照する形だけを対象にすると、この形で
    検査が丸ごと飛び、`then: "$b", else_branch: "実施済み"` のように**取得できた値をリテラルへ
    置き換える**計画が痕跡なく確定できてしまう（`item_failure_code` も付かない）。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "compare", "id": "yes_a", "left": "$has_a", "cmp": "==", "right": True},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {
                "op": "branch",
                "id": "v",
                "when": when_ref,
                "then": then_side,
                "else_branch": else_side,
            },
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    v = program.steps[5]
    if blocked:
        assert v.needs_input is True
        assert fixes == ["v:needs_input(分岐の結線)"]
    else:
        assert not v.needs_input
        assert fixes == []


@pytest.mark.parametrize("negation", [0, "0", False])
def test_enforce_reads_a_numeric_negation_as_the_template(negation: object):
    """`has_a == 0` / `== "0"` は実行時 `== false` と同じなので、同じに読むこと。

    `_loose_eq` は数値へ落として比べる（`executor`）。真偽リテラルだけを読むと、この表記で
    極性が読めず2つの検査が丸ごと外れる（フォールバックとして正常に動くのに素通りする）。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": negation},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            # 誤結線（値があるときにリテラルを採る）
            {"op": "branch", "id": "v", "when": "$has_a", "then": "実施", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[4].needs_input is True
    assert fixes == ["v:needs_input(分岐の結線)"]


@pytest.mark.parametrize("slot", ["guard", "when"])
def test_enforce_blocks_when_the_negation_polarity_is_unreadable(slot: str):
    """空振り判定の**向きが読めない**ときは人へ回すこと（判定不能を合格にしない）。

    `guard` と `when` の**両方**で同じ扱いにする。片方だけ fail-closed にすると、もう片方の
    スロットに同義表記を書くだけで2つの検査のどちらも通らないまま確定できる。
    `resource_index/derive.py` の `matches` と同じ方針（判定できないものを合格にしない）。
    """
    # 数値にも真偽にも読めないリテラル（`_loose_eq` でも定型と同じにはならない）
    negation = {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": "なし"}
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            negation,
            {
                "op": "fhir_extract",
                "id": "b",
                "guard": "$no_a" if slot == "guard" else "$has_a",
                "fhir_query": _FHIR_Q2,
            },
            {
                "op": "branch",
                "id": "v",
                "when": "$has_a" if slot == "guard" else "$no_a",
                "then": "$a",
                "else_branch": "$b",
            },
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    blocked = program.steps[3] if slot == "guard" else program.steps[4]
    assert blocked.needs_input is True
    assert fixes == [f"{blocked.id}:needs_input(空振り判定の向き不明)"]
    assert "向き" in (blocked.reasoning or "")


def test_enforce_blocks_the_presence_encoding_branch_when_a_fallback_exists():
    """両側リテラルでも、代替候補を取得しているならその値を捨てないこと。

    `then: "1", else_branch: "2"` は、代替候補が無ければ値の有無の符号化として正当だが、
    「空だったときに走る」代替候補があるなら、その取得値を捨てている。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "1", "else_branch": "2"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[4].needs_input is True
    assert fixes == ["v:needs_input(分岐の結線)"]


def test_enforce_treats_is_empty_eq_false_as_a_gate_not_a_fallback():
    """`is_empty($a) == false` は「空でないとき」＝ゲートであり、フォールバックではないこと。

    正本が一時これを「否定の書き方」として認めていたが、極性が逆で、**第1候補が成功した
    ときにだけ代替候補の患者データ取得が走る**（本来の「空だったときにだけ取得する」の逆）。この形を
    「空だったときに走る」と読んでしまうと、結線の検査が誤った側を「値があるときに採る側」
    として扱う。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "empty_a", "left": "$a", "cmp": "is_empty"},
            # 「空でないとき」に走る（ゲート）
            {"op": "compare", "id": "not_empty", "left": "$empty_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$not_empty", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    # b は「a が非空のとき」に走るので、a の空振りの代替候補ではない。
    # したがって結線の検査（代替候補の値を捨てていないか）の対象にならない。
    assert not any("分岐の結線" in f for f in fixes), fixes
    # 取得の順序の検査も、b を「前の試行のあとの候補」と数えない（逆転扱いにしない）
    assert not any("優先順の逆転" in f for f in fixes), fixes


def test_enforce_reads_a_double_negated_gate_as_a_gate():
    """`is_empty($a) == false`（二重否定）で書いたゲートを、フォールバックと誤認しないこと。

    この guard は「a が空でないとき」に発火する。内側の空判定の向きを反映しないと、
    「空だったときに走る」＝フォールバックの代替候補と読み違え、ゲート形の正当なプランを
    人へ回してしまう。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "empty_a", "left": "$a", "cmp": "is_empty"},
            {"op": "compare", "id": "not_empty", "left": "$empty_a", "cmp": "==", "right": False},
            # a が空でないときに走る（ゲート）
            {"op": "fhir_extract", "id": "b", "guard": "$not_empty", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "該当なし"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any("分岐の結線" in f for f in fixes)
    assert not program.steps[5].needs_input


def test_enforce_does_not_treat_another_values_emptiness_as_this_fallback():
    """**別の値**の空判定で走る step を、この空判定のフォールバックと見なさないこと。

    空判定同士を左辺を見ずに突き合わせると、無関係な2つの空判定が同じものとして扱われる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {
                "op": "sql_extract",
                "id": "x",
                "sql": "SELECT STATUS FROM DEMO.FLAGS WHERE PATIENT_ID = :patient_id",
            },
            # 向きの違う空判定にする（同じ向きだと、左辺を見ない実装でも「代替候補ではない」
            # と判定されてしまい、境界を測れない）
            {"op": "compare", "id": "empty_x", "left": "$x", "cmp": "is_empty"},
            # 走る条件は「x が空」で、a の空振りとは無関係
            {"op": "fhir_extract", "id": "b", "guard": "$empty_x", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert not any("分岐の結線" in f for f in fixes)
    assert not program.steps[5].needs_input


def test_enforce_does_not_block_a_branch_whose_tested_value_is_not_a_candidate():
    """空判定の対象が所在候補でない分岐は、代替候補があっても対象外にすること。

    「ゲートが空なら候補から取る」形では、`then` 側にゲート由来の値やリテラルを置くのが
    自然である。空判定の対象が候補の値でないなら、そこに「候補の値を採れ」と要求する筋合いは
    ない。
    """
    program = _program(
        [
            # ゲート（この項目の所在候補ではない別表）
            {
                "op": "sql_extract",
                "id": "gate",
                "sql": "SELECT STATUS FROM DEMO.FLAGS WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_gate", "left": "$gate", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_gate", "left": "$has_gate", "cmp": "==", "right": False},
            # ゲートが空のときだけ候補から取る
            {"op": "fhir_extract", "id": "b", "guard": "$no_gate", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_gate", "then": "対象外", "else_branch": "$b"},
        ]
    )
    # 候補は FHIR のみ（gate の sql_extract は DB 候補が無いので候補にならない）
    specs = derive_op_args_list(
        [_cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert not program.steps[4].needs_input
    assert not any("分岐の結線" in f for f in fixes)


def test_enforce_reads_the_guard_polarity_with_reversed_operands():
    """`guard` の否定が左右逆に書かれていても極性を読めること。

    `{"left": false, "cmp": "==", "right": "$has_a"}` は定型と同じ意味で検証も通る。
    片方の並びしか読まないと、この書き方のフォールバックで結線の検査が丸ごと効かない。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": False, "cmp": "==", "right": "$has_a"},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            # then と else_branch が入れ替わっている
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[4].needs_input is True
    assert "v:needs_input(分岐の結線)" in fixes


def test_enforce_does_not_block_a_gate_branch_whose_document_is_also_a_candidate():
    """ゲート形の分岐は、文書側が所在候補であっても対象外にすること。

    `when: "$has_consent"` で判定し、`guard: "$has_consent"` で文書を取る形では、代替候補は
    「空だったときに走る」のではなく「値があるときに走る」。`guard` の極性を見ずに「どちらかの
    側が候補に届くか」だけで判別すると、プロンプトの厳守事項3 に従った正当なプランが
    人の手待ちになる（候補の値は失われていない）。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "consent",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_consent", "left": "$consent", "cmp": "is_not_empty"},
            {
                "op": "file_extract",
                "id": "doc",
                "guard": "$has_consent",
                "path_pattern": "reports/{{patient_id}}/report.pdf",
                "connection_id": "report_fs",
            },
            {
                "op": "llm_extract",
                "id": "val",
                "guard": "$has_consent",
                "prompt": "$doc から所見を抽出",
            },
            {
                "op": "branch",
                "id": "v",
                "when": "$has_consent",
                "then": "$val",
                "else_branch": "該当なし",
            },
        ]
    )
    # ゲート（DB）も文書（FILE）も所在候補である
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
            _cand(SourceCategory.FILE, "file:patient_data:レポート", FILE_PAYLOAD),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert not program.steps[4].needs_input
    assert not any("分岐の結線" in f for f in fixes)


def test_enforce_does_not_block_an_existence_gate_branch():
    """値の有無をゲートに使う分岐（フォールバックでない形）を結線の誤りとしないこと。

    `when: "$has_a"`（同意日があるか）で文書からの抽出値を採る形では、`a` は項目値では
    なくゲートであり、失われている値は無い。`is_empty` / `is_not_empty` は汎用の比較
    演算子なので、フォールバック以外でも使われる。「空だったときに走る」代替候補の取得が
    無い分岐は対象外にする。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "a",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {
                "op": "file_extract",
                "id": "doc",
                "guard": "$has_a",
                "path_pattern": "reports/{{patient_id}}/report.pdf",
                "connection_id": "report_fs",
            },
            {"op": "llm_extract", "id": "val", "guard": "$has_a", "prompt": "$doc から所見を抽出"},
            {
                "op": "branch",
                "id": "v",
                "when": "$has_a",
                "then": "$val",
                "else_branch": "該当なし",
            },
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert not program.steps[4].needs_input
    assert fixes == []  # 索引の SQL を踏んでいるので是正も無い


def test_enforce_keeps_the_chain_through_a_flat_branch_sequence():
    """3候補を入れ子でなく `branch` の連鎖で書いた形でも参照の連鎖が切れないこと。

    `v2 = branch(...)` → `is_not_empty($v2)` → 第3候補の `guard` という並びは、入れ子に
    しない自然な別解である。`when` / `then` / `else_branch` を辿らないと連鎖がここで切れ、
    取得の順序の検査が黙って無効になる。
    """
    program = _program(
        [
            # 第2候補 → 第3候補 → 第1候補（最後が後戻り）
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q2},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q3},
            {"op": "branch", "id": "v2", "when": "$has_a", "then": "$a", "else_branch": "$b"},
            {"op": "compare", "id": "has_v2", "left": "$v2", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_v2", "left": "$has_v2", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "c", "guard": "$no_v2", "fhir_query": _FHIR_Q1},
            {"op": "branch", "id": "v", "when": "$has_v2", "then": "$v2", "else_branch": "$c"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|30313-1", FHIR_PAYLOAD_ALT2),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    c = program.steps[7]
    assert c.needs_input is True  # 第2・第3候補のあとに第1候補へ戻っている
    assert c.unit_id is None
    assert "c:needs_input(優先順の逆転)" in fixes


def test_enforce_allows_a_guarded_retry_of_the_same_candidate_with_other_conditions():
    """同じ候補を条件を変えて取り直すフォールバック（時間窓を広げる等）は止めないこと。

    「同じ優先順に留まる」のは後戻りではない。ここを `>=` で弾くと、第1候補を条件を変えて
    2回試す正当な計画が人の手待ちになる。生成物が**同一**のときは別途コピペとして止まる。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "a",
                "sql": (
                    "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id"
                    " AND TAKEN_AT >= :window_start"
                ),
            },
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {
                "op": "sql_extract",
                "id": "b",
                "guard": "$no_a",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[3]
    assert a.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert b.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert not a.needs_input
    assert not b.needs_input
    assert "AND TAKEN_AT >= :window_start" in (a.sql or "")
    assert fixes == []


def test_enforce_drops_the_fabricated_path_of_a_blocked_file_step():
    """人へ回すファイル取得 step に、索引の宣言を踏んでいないパスを残さないこと。"""
    program = _file_fallback_program("secret/{{patient_id}}/guessed.pdf")

    fixes = enforce_derived_op_args(program, _one_file_candidate())

    b = program.steps[3]
    assert b.needs_input is True
    assert b.path_pattern is None  # 捏造パスを下書きに残さない
    assert b.unit_id is None
    assert "b:needs_input(候補重複)" in fixes


def test_enforce_does_not_block_the_conditional_branch_choosing_by_context():
    """条件付き実行の `branch` は then/else の順を優先順として見ないこと。

    `when` が判定（llm_judge）由来で空判定を経由していない分岐は、文脈でどちらを採るかが
    決まる。ここを優先順として扱うと、プロンプトが例示している形が人の手待ちになる。
    """
    program = _program(
        [
            {"op": "llm_judge", "id": "is_inpatient", "prompt": "患者 {{patient_id}} は入院中か"},
            {
                "op": "sql_extract",
                "id": "d",
                "guard": "$is_inpatient",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {
                "op": "compare",
                "id": "is_outpatient",
                "left": "$is_inpatient",
                "cmp": "==",
                "right": False,
            },
            {"op": "fhir_extract", "id": "f", "guard": "$is_outpatient", "fhir_query": _FHIR_Q1},
            # then が第2候補（DB）・else が第1候補（FHIR）だが、これは優先順ではない
            {"op": "branch", "id": "v", "when": "$is_inpatient", "then": "$d", "else_branch": "$f"},
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD),
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert not program.steps[4].needs_input
    assert fixes == []


def test_enforce_records_corrections_and_drops_the_fabricated_sql_when_blocking():
    """人へ回す step でも、適用した書き換えを是正一覧に残し、捏造SQLは残さないこと。

    `op` / `connection_id` は索引由来の値で書き換わる。それを記録しないと、監査で
    「なぜ外部DB取得が自アプリDB取得になったのか」を追えない。逆に、索引の識別子を
    踏んでいない SQL を下書きに残すと、人が needs_input を外したときに実行される。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "a",
                "sql": "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id",
            },
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {
                "op": "external_db_extract",  # 索引は自アプリDB（scope=self）なので是正される
                "id": "b",
                "guard": "$no_a",
                "connection_id": "hosp_dwh",
                "sql": "SELECT blast FROM guessed_table WHERE pid = :patient_id",
            },
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$b"},
        ]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    b = program.steps[3]
    assert b.needs_input is True
    assert b.sql is None  # 索引に無い表への SELECT を下書きに残さない
    assert b.unit_id is None
    assert fixes == ["b:needs_input(候補重複)・op→sql_extract・connection_id→DEMO_DB"]


def test_enforce_allows_the_same_candidate_used_by_two_steps_in_order():
    """同じ候補を2つの step が別の SQL で正当に使う形（術前/術後）は止めないこと。

    逆転検査は「後戻り」だけを見る。同じ優先順に留まるのは後戻りではない。
    """
    program = _program(
        [
            {
                "op": "sql_extract",
                "id": "pre",
                "sql": (
                    "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id"
                    " AND TAKEN_AT < :op_date"
                ),
            },
            {
                "op": "sql_extract",
                "id": "post",
                "sql": (
                    "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id"
                    " AND TAKEN_AT >= :op_date"
                ),
            },
        ],
        ret="$post",
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)]
    )

    fixes = enforce_derived_op_args(program, specs)

    pre, post = program.steps[0], program.steps[1]
    assert pre.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert post.unit_id == "db:DEMO.LAB.BLAST_PCT"  # 同じ所在を指すのは正しい
    assert not pre.needs_input
    assert not post.needs_input
    assert "TAKEN_AT >= :op_date" in (post.sql or "")  # 取得条件を潰さない
    assert fixes == []


@pytest.mark.parametrize(
    "copied",
    [
        "/OBSERVATION?CODE=http://loinc.org|6690-2&PATIENT={{patient_id}}",
        "/Observation?code=http://loinc.org|6690-2&patient = {{patient_id}}",
    ],
)
def test_enforce_detects_the_copied_query_despite_trivial_differences(copied: str):
    """コピペ検出が大文字小文字・空白の差で回避されないこと。

    `DerivedOpArgs.matches` は識別子を大文字小文字を無視して照合するので、比較側が
    バイト一致だと「同じ取得を二度書いた」形をこの程度の差ですり抜けられる。
    """
    program = _fhir_fallback_program(copied)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    b = program.steps[3]
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(候補重複)" in fixes


# --------------------------------------------------------------------------- #
# 3. コンパイラ入力（_compile_inputs）とプロンプト提示
# --------------------------------------------------------------------------- #


def test_compile_inputs_propagates_unit_id_connection_id_payload():
    cand = _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.BLAST_PCT", DB_PAYLOAD)
    runbooks = {
        "q1": InputRunbook(questionnaire_id=1, link_id="q1", item_text="芽球割合", steps=[])
    }
    annotations = {"q1": ClinicalAnnotation(questionnaire_id=1, link_id="q1", annotation="末梢血")}
    locations = {
        "q1": SystemLocation(
            questionnaire_id=1,
            link_id="q1",
            candidates=[cand, _cand(SourceCategory.FHIR, "fhir:Observation:x", FHIR_PAYLOAD)],
        )
    }

    items = _compile_inputs(["q1"], runbooks, annotations, locations)

    assert items[0]["unit_id"] == "db:DEMO.LAB.BLAST_PCT"
    assert items[0]["connection_id"] == "DEMO_DB"
    assert items[0]["payload"]["physical_name"] == "BLAST_PCT"
    assert len(items[0]["candidates"]) == 2  # 候補全体（多ソースの典拠づけに使う）
    assert items[0]["source_category"] == SourceCategory.LOCAL_DB
    assert items[0]["annotation"] == "末梢血"


def test_compile_inputs_without_location_is_empty_but_shaped():
    runbooks = {"q1": InputRunbook(questionnaire_id=1, link_id="q1", item_text="x", steps=[])}
    items = _compile_inputs(["q1"], runbooks, {}, {})
    assert items[0]["unit_id"] is None
    assert items[0]["payload"] is None
    assert items[0]["candidates"] == []


def test_candidates_of_accepts_flat_legacy_item():
    item = {"link_id": "q1", "source_category": "local_db", "location": "DB: X / 表: T / カラム: C"}
    assert candidates_of(item) == [
        {
            "source_category": "local_db",
            "location": "DB: X / 表: T / カラム: C",
            "unit_id": None,
            "connection_id": None,
            "payload": None,
        }
    ]
    assert candidates_of({"link_id": "q1"}) == []


def test_user_message_presents_structured_location_info():
    compiler = AIPlanCompiler()
    item = {
        "link_id": "q1",
        "item_text": "芽球割合",
        "source_category": SourceCategory.LOCAL_DB,
        "location": "DB: DEMO_DB / 表: DEMO.LAB / カラム: BLAST_PCT",
        "candidates": [
            _cand(
                SourceCategory.LOCAL_DB,
                "db:DEMO.LAB.BLAST_PCT",
                {**DB_PAYLOAD, "value_dict": [{"code": "1", "label": "x"}] * 10},
            ),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
        ],
        "steps": [],
    }

    message = compiler._build_user_message([item])

    assert "unit_id=db:DEMO.LAB.BLAST_PCT" in message
    assert "接続ID=DEMO_DB" in message
    assert "db_qualified=DEMO.LAB" in message
    assert "patient_key=PATIENT_ID" in message
    assert "確定済みop引数" in message
    assert "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id" in message
    assert "必須識別子" in message
    assert "代替の所在候補2" in message
    assert "value_dict" not in message  # 肥大するキーはプロンプトへ出さない


def test_user_message_shows_missing_for_incomplete_index():
    compiler = AIPlanCompiler()
    item = {
        "link_id": "q1",
        "item_text": "芽球割合",
        "candidates": [
            _cand(
                SourceCategory.LOCAL_DB,
                "db:DEMO.LAB.BLAST_PCT",
                {**DB_PAYLOAD, "patient_key": None},
            )
        ],
        "steps": [],
    }

    message = compiler._build_user_message([item])

    assert "不足（needs_input=true にすること）" in message


def test_user_message_shows_derived_args_for_alternative_candidates():
    """代替候補にも確定済みop引数を出す（要約だけでは第2候補の step を具体的に書けない）。"""
    compiler = AIPlanCompiler()
    item = {
        "link_id": "q1",
        "item_text": "芽球割合",
        "candidates": [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
        ],
        "steps": [],
    }

    message = compiler._build_user_message([item])

    assert "代替の所在候補2" in message
    alt = message.split("代替の所在候補2")[1]
    assert "unit_id=fhir:Observation:LOINC|718-7" in alt
    # 要約（unit_id / op）だけでは第2候補の step を書けない。**引数そのもの**が出ていること。
    # unit_id にもコードの字面が入るので、"718-7" を探すだけでは要約で満たされてしまう。
    assert "確定済みop引数" in alt
    assert "fhir_query=/Observation?code=http://loinc.org|718-7&patient={{patient_id}}" in alt
    assert "必須識別子" in alt


def test_user_message_shows_derived_args_for_the_third_candidate():
    """**第3候補**の確定済み引数も出ること（3候補の定型を書くのに要る）。

    `_MAX_ALT_CANDIDATES` を1に下げると第3候補が消えるが、2候補のテストだけでは
    気づけない。厳守事項1に従うと引数の示されていない候補の step は needs_input=true に
    なるので、提示が減ると3候補の定型そのものが成立しなくなる。
    """
    compiler = AIPlanCompiler()
    item = {
        "link_id": "q1",
        "item_text": "芽球割合",
        "candidates": [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|30313-1", FHIR_PAYLOAD_ALT2),
        ],
        "steps": [],
    }

    message = compiler._build_user_message([item])

    assert "代替の所在候補3" in message
    third = message.split("代替の所在候補3")[1]
    assert "確定済みop引数" in third
    assert "fhir_query=/Observation?code=http://loinc.org|30313-1&patient={{patient_id}}" in third


def test_user_message_shows_missing_and_key_type_for_alternative_candidates():
    """代替候補の「不足」と「患者キー種別」も提示すること。

    どちらも欠けると、LLM はその候補の step を書けるかどうか判断できない（不足がある候補は
    厳守事項1により needs_input=true にすべきで、患者キー種別は SQL の書き方に効く）。
    """
    compiler = AIPlanCompiler()
    item = {
        "link_id": "q1",
        "item_text": "芽球割合",
        "candidates": [
            _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
            # 患者絞り込みキー列が無い＝不足がある DB 候補
            _cand(
                SourceCategory.LOCAL_DB,
                "db:DEMO.LAB.BLAST_PCT",
                {**DB_PAYLOAD, "patient_key": None},
            ),
            _cand(SourceCategory.LOCAL_DB, "db:DEMO.LAB.HGB", DB_PAYLOAD),
        ],
        "steps": [],
    }

    message = compiler._build_user_message([item])

    second = message.split("代替の所在候補2")[1].split("代替の所在候補3")[0]
    assert "不足（この候補を使う step は needs_input=true）" in second
    third = message.split("代替の所在候補3")[1]
    assert "患者キー種別: mrn" in third


# --------------------------------------------------------------------------- #
# 4. コンパイル経路の統合（Ollama はモック）と索引を使わない所在の回帰
# --------------------------------------------------------------------------- #


def _resp(content):
    return types.SimpleNamespace(message=types.SimpleNamespace(content=content))


def _patch_client(compiler: AIPlanCompiler, program_by_link: dict[str, dict]):
    async def fake_chat(**kwargs):
        message = kwargs["messages"][1]["content"]
        link_ids = re.findall(r"### linkId: (\S+)", message)
        payload = {
            "plans": [
                {"link_id": lid, "program": program_by_link.get(lid, {"steps": [], "return": None})}
                for lid in link_ids
            ]
        }
        return _resp(json.dumps(payload))

    compiler._client = types.SimpleNamespace(chat=fake_chat)


@pytest.mark.asyncio
async def test_compile_enforces_derived_args_end_to_end():
    compiler = AIPlanCompiler()
    _patch_client(
        compiler,
        {
            "q1": {
                "steps": [
                    {"op": "sql_extract", "id": "v", "sql": "SELECT blast FROM guess WHERE id = 1"}
                ],
                "return": "$v",
            }
        },
    )
    items = [
        {
            "link_id": "q1",
            "item_text": "芽球割合",
            "candidates": [
                _cand(SourceCategory.EXTERNAL_DB, "db:DEMO.LAB.BLAST_PCT", EXTERNAL_DB_PAYLOAD)
            ],
            "steps": [],
        }
    ]

    plans = await compiler.compile(items)

    step = plans[0].program.steps[0]
    assert step.op == OpType.EXTERNAL_DB_EXTRACT
    assert step.sql == "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id"
    assert step.unit_id == "db:DEMO.LAB.BLAST_PCT"


@pytest.mark.asyncio
async def test_compile_passes_every_candidate_to_the_enforcement():
    """コンパイル経路が**候補全体**を是正へ渡すこと（第1候補だけに切り詰めていない）。

    ここが第1候補だけになると、フォールバックの第2 step は候補重複と判定されて黙って
    `needs_input` になり、機能が丸ごと死ぬ。プロンプト提示側だけを固定しても検出できない。
    """
    compiler = AIPlanCompiler()
    _patch_client(
        compiler,
        {
            "q1": {
                "steps": [
                    {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
                    {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
                    {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
                    {
                        "op": "fhir_extract",
                        "id": "b",
                        "guard": "$no_a",
                        "fhir_query": _FHIR_Q2,
                    },
                    {
                        "op": "branch",
                        "id": "v",
                        "when": "$has_a",
                        "then": "$a",
                        "else_branch": "$b",
                    },
                ],
                "return": "$v",
            }
        },
    )
    items = [
        {
            "link_id": "q1",
            "item_text": "芽球割合",
            "candidates": [
                _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|6690-2", FHIR_PAYLOAD),
                _cand(SourceCategory.FHIR, "fhir:Observation:LOINC|718-7", FHIR_PAYLOAD_ALT),
            ],
            "steps": [],
        }
    ]

    plans = await compiler.compile(items)

    steps = {s.id: s for s in plans[0].program.steps}
    assert steps["a"].unit_id == "fhir:Observation:LOINC|6690-2"
    assert steps["b"].unit_id == "fhir:Observation:LOINC|718-7"  # 第2候補が確定している
    assert not steps["a"].needs_input
    assert not steps["b"].needs_input


@pytest.mark.asyncio
async def test_compile_leaves_program_untouched_without_index_payload():
    """索引由来の payload が無い所在（手入力など）では生成物に手を入れない。"""
    compiler = AIPlanCompiler()
    program = {
        "steps": [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "dwh",
                "sql": "SELECT name FROM patient_infomation WHERE CAST(patient_id AS TEXT) = :patient_id",
            }
        ],
        "return": "$v",
    }
    _patch_client(compiler, {"q1": program})
    items = [
        {
            "link_id": "q1",
            "item_text": "患者氏名",
            "source_category": SourceCategory.EXTERNAL_DB,
            "location": "DWH: patient_infomation.name",
            "unit_id": None,
            "connection_id": "dwh",
            "payload": {},
            "candidates": [
                {
                    "source_category": SourceCategory.EXTERNAL_DB,
                    "location": "DWH: patient_infomation.name",
                    "unit_id": None,
                    "connection_id": "dwh",
                    "payload": {},
                }
            ],
            "steps": [],
        }
    ]

    plans = await compiler.compile(items)

    # 期待値は「是正を通さない素の DSLProgram パース結果」（既定値の付与は従来挙動）
    expected = DSLProgram(**program).model_dump(by_alias=True, exclude_none=True)
    assert plans[0].program.model_dump(by_alias=True, exclude_none=True) == expected


def _payloadless_candidate(source_category: str, connection_id: str | None) -> dict:
    """索引を使わない所在の候補（手入力など。payload は空）。"""
    return {
        "source_category": source_category,
        "location": "（人間可読の所在）",
        "unit_id": None,
        "connection_id": connection_id,
        "payload": {},
    }


# 取得 op → その所在の source_category（E2E 台本はプランだけを持ち、所在の種類は書かない）。
_FETCH_OP_SOURCE_CATEGORY = {
    OpType.EXTERNAL_DB_EXTRACT: SourceCategory.EXTERNAL_DB,
    OpType.SQL_EXTRACT: SourceCategory.LOCAL_DB,
    OpType.FHIR_EXTRACT: SourceCategory.FHIR,
    OpType.FILE_EXTRACT: SourceCategory.FILE,
    OpType.CSV_EXTRACT: SourceCategory.FILE,
}

_E2E_PROGRAMS = {link_id: item["program"] for link_id, item in load_design_script().items()}


@pytest.mark.parametrize("link_id", sorted(_E2E_PROGRAMS))
def test_e2e_plans_unchanged_by_enforcement(link_id: str) -> None:
    """E2E 台本のプランは、payload なしの所在に対する op 引数の是正で1文字も変わらない。

    **`assert specs == []` は payload の無い候補に対して恒真である**。
    ここで固定したいのは「payload の無い候補では是正が一切走らない」という性質
    （DSLProgram のパース可否・unit_id 非付与・dump の往復安定性）である。外部 DB・
    FHIR・ファイル・CSV と compare / branch / guard を含む実プラン一式で見る。
    """
    program_dict = _E2E_PROGRAMS[link_id]
    first_fetch = next(
        step["op"] for step in program_dict["steps"] if step["op"] in _FETCH_OP_SOURCE_CATEGORY
    )
    program = DSLProgram(**program_dict)
    before = program.model_dump(by_alias=True, exclude_none=True)
    specs = derive_op_args_list(
        [_payloadless_candidate(_FETCH_OP_SOURCE_CATEGORY[first_fetch], None)]
    )

    fixes = enforce_derived_op_args(program, specs)

    assert specs == [], f"{link_id}: payload なしの所在から op 引数は導出されない"
    assert fixes == [], f"{link_id}: 是正は行われない"
    assert program.model_dump(by_alias=True, exclude_none=True) == before, link_id
    assert all(step.unit_id is None for step in program.steps), link_id


# --------------------------------------------------------------------------- #
# 安全側に倒すべき境界の退行検知
# --------------------------------------------------------------------------- #


def test_db_op_fails_closed_when_connection_unregistered():
    """接続が未登録で自アプリDB/外部DBを判定できないときは SQL を確定しない。

    フェイルオープンで sql_extract に倒すと、候補 payload が指す任意の表を
    アプリ本体の DB セッションで読む SQL を「決定的に導出した」ラベル付きで
    生成してしまう（candidates はクライアント送信値をそのまま保存する）。
    """
    payload = {
        "db_qualified": "users",
        "physical_name": "password_hash",
        "patient_key": "id",
        "connection_id": "not_registered",
    }
    # 実運用の候補形状: derive_source_category は未登録接続を local_db と既定するため、
    # 候補は source_category=local_db を持ちうる（クライアント送信値をそのまま保存する
    # 経路もある）。source_category を信用すると任意表の読み出しに繋がるので、
    # レジストリで判定できないことを理由に確定しないこと。
    cand = _cand(SourceCategory.LOCAL_DB, "db:unknown", payload)
    spec = derive_op_args(cand, registry=ConnectionRegistry({}))

    assert spec is not None
    assert spec.is_complete is False
    assert "sql" not in spec.args

    # source_category を持たない古い候補でも同じ
    spec2 = derive_op_args(
        {"unit_id": "db:unknown", "payload": payload}, registry=ConnectionRegistry({})
    )
    assert spec2 is not None
    assert spec2.is_complete is False


def test_external_db_requires_connection_id():
    payload = {k: v for k, v in DB_PAYLOAD.items() if k != "connection_id"}
    cand = _cand(SourceCategory.EXTERNAL_DB, "db:DEMO_DB:DEMO.LAB:BLAST_PCT", payload)
    spec = derive_op_args(cand)

    assert spec is not None
    assert spec.is_complete is False
    assert "sql" not in spec.args


def test_fhir_requires_patient_placeholder_in_generated_query():
    """患者絞り込みを落としたクエリは採用しない（他患者の値が入るため）。"""
    payload = {
        "connection_id": "hosp_fhir",
        "resource_type": "Observation",
        "query_template": "Observation?patient={{patient_id}}&code=6690-2",
        "fhir_path": "Observation.valueQuantity.value",
    }
    spec = derive_op_args(_cand(SourceCategory.FHIR, "fhir:obs:6690-2", payload))
    assert spec is not None

    # リソース型とコードは踏んでいるが patient 絞り込みが無いクエリ
    assert spec.matches("/Observation?code=6690-2") is False

    program = _program(
        [{"op": "fhir_extract", "id": "v", "fhir_query": "/Observation?code=6690-2"}]
    )
    enforce_derived_op_args(program, [spec])
    assert program.steps[0].fhir_query == "/Observation?patient={{patient_id}}&code=6690-2"


def test_required_token_matches_on_word_boundary():
    """部分文字列一致だと別表（LABS 等）を踏んだ SQL を検証済みと誤認する。"""
    spec = derive_op_args(_cand(SourceCategory.EXTERNAL_DB, "db:x", DB_PAYLOAD))
    assert spec is not None
    assert spec.matches("SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id") is True
    assert spec.matches("SELECT BLAST_PCT FROM DEMO.LABS WHERE PATIENT_ID = :patient_id") is False


def test_compile_strips_llm_unit_id_when_no_derived_args():
    """payload の無い項目でも LLM が書いた unit_id は残さない（偽の典拠を作らない）。"""
    from kawasemi.ai_input.compiler import strip_llm_unit_ids

    program = _program([{"op": "llm_judge", "id": "v", "prompt": "判定", "unit_id": "db:捏造"}])
    strip_llm_unit_ids(program)
    assert program.steps[0].unit_id is None


def test_enforce_keeps_needs_input_when_llm_declared_insufficient():
    """LLM が情報不足と判断した step は、導出SQLを下書きにしつつ needs_input を維持する。

    導出SQLは WHERE が患者キーのみで ORDER BY / LIMIT を持たないため、複数行返る列では
    rows[0][0] が DB の並び次第の非決定値になる。黙って確定させない。
    """
    program = _program([{"op": "external_db_extract", "id": "v", "sql": None, "needs_input": True}])
    specs = derive_op_args_list([_cand(SourceCategory.EXTERNAL_DB, "db:x", DB_PAYLOAD)])

    enforce_derived_op_args(program, specs)

    assert program.steps[0].needs_input is True
    assert program.steps[0].sql is not None  # 下書きとして提示はする


def test_enforce_does_not_collapse_two_unmatched_db_steps():
    """同じ候補を2つ以上の step に焼き込まない（術前/術後の取り違えを防ぐ）。"""
    program = _program(
        [
            {"op": "external_db_extract", "id": "a", "sql": "SELECT 1 FROM NOWHERE"},
            {"op": "external_db_extract", "id": "b", "sql": "SELECT 2 FROM NOWHERE"},
        ]
    )
    specs = derive_op_args_list([_cand(SourceCategory.EXTERNAL_DB, "db:x", DB_PAYLOAD)])

    enforce_derived_op_args(program, specs)

    assert program.steps[0].needs_input is False
    assert program.steps[1].needs_input is True
    assert program.steps[0].sql != program.steps[1].sql


def test_enforce_file_glob_pattern_requires_human_input():
    """glob は executor が展開しないので、確定せず人へ回す。"""
    program = _program(
        [{"op": "file_extract", "id": "doc", "path_pattern": "reports/{{patient_id}}/x.pdf"}]
    )
    specs = derive_op_args_list(
        [_cand(SourceCategory.FILE, "file:patient_data:レポート", GLOB_FILE_PAYLOAD)]
    )

    enforce_derived_op_args(program, specs)

    assert program.steps[0].needs_input is True


# --- guard を伴わない複数候補の無条件取得 -----------------------------------------


def test_enforce_blocks_unguarded_multi_candidate_fetches():
    """**両方が無条件の複数候補取得**は人へ回すこと。

    優先順の検査も分岐の結線の検査も「プロンプトが指示する定型」——`guard` で前段の
    空振りに依存させ、`branch.when` に空判定を置く——を前提にしている。複数候補を
    **無条件に**取って `branch` で選ぶ形はどちらにも掛からず、`then` 側に第2候補・
    `else_branch` 側に第1候補を書いても検出されない。両候補に値がある被験者では
    **運用者が指定した優先順と逆の値が確定保存される**のに、`fixes` は空、`unit_id` は
    step ごとに正しく刻まれ、`needs_input` も立たないので痕跡が残らない。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "fhir_extract", "id": "b", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "pick", "when": "$b", "then": "$b", "else_branch": "$a"},
        ],
        ret="$pick",
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[1]
    assert a.needs_input is True and b.needs_input is True
    assert a.unit_id is None and b.unit_id is None, "特定できない step に典拠を刻んでいる"
    assert fixes == [
        "a:needs_input(guard無しの複数候補取得)",
        "b:needs_input(guard無しの複数候補取得)",
    ]


def test_enforce_allows_a_guarded_fallback_of_two_candidates():
    """**guard のある定型は従来どおり通す**こと（正当なフォールバックを止めない）。

    ここが落ちると「フォールバックを書くと必ず人待ちになる」＝運用中ずっと手が止まる。
    """
    program = _fhir_fallback_program(None)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert program.steps[0].unit_id == "fhir:Observation:LOINC|6690-2"
    assert not any("guard無し" in f for f in fixes), fixes


def test_enforce_ignores_a_single_candidate_item_with_two_fetches():
    """候補が1件しか無い項目の2 step（術前値・術後値など）は巻き込まないこと。

    無条件の2 step が正当になるのは、**どちらの候補を採るかという問題が存在しない**
    ときである。候補が1件なら優先順の逆転はそもそも起こらない。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "pre", "fhir_query": _FHIR_Q1},
            {"op": "fhir_extract", "id": "post", "fhir_query": _FHIR_Q1 + "&date=gt2026"},
        ],
        ret="$post",
    )
    specs = _two_fhir_candidates()[:1]  # 候補は1件だけ

    fixes = enforce_derived_op_args(program, specs)

    assert not any("guard無し" in f for f in fixes), fixes


def test_enforce_treats_a_literal_true_guard_as_unconditional():
    """`guard: true` は無条件と同じ扱いにすること。

    プロンプトは `guard` に `true` / `false` のリテラルを許しているので LLM 出力に現れる。
    `true` を「guard がある」と数えると、リテラルを1つ書くだけでこの検査を回避できる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1, "guard": True},
            {"op": "fhir_extract", "id": "b", "fhir_query": _FHIR_Q2, "guard": True},
        ],
        ret="$b",
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert fixes == [
        "a:needs_input(guard無しの複数候補取得)",
        "b:needs_input(guard無しの複数候補取得)",
    ]


# --- 到達経路から SQL を組む --------------------------------------------------------
#
# 索引の `db_qualified` は定義書＝元システム側の名前で、実DBには無い表を指す。
# 文書種別つきの所在は、種別で絞る経路を持たないと**その患者の全文書**を読む SQL になる。

from kawasemi.ai_input.resource_index.derive import build_access_sql  # noqa: E402

# 架空の形: 本文表とヘッダ表を2列で結び、ヘッダ側の日付だけで並べる
_NOTE_INDEX = {"schema": "ehr", "table": "NOTE_INDEX", "on": ["PID", "NOTE_NO"]}
_DOC_PAYLOAD = {
    "physical": {"schema": "ehr", "table": "DOCUMENT", "column": "NOTE_TEXT"},
    "physical_name": "NOTE_TEXT",
    "patient_key": "PID",
    "access_path": {
        "filters": [
            {"via": _NOTE_INDEX, "column": "NOTE_TYPE", "equals": "NOTE"},
            {"via": _NOTE_INDEX, "column": "VOID_FLAG", "equals": 0},
        ],
        "order_by": [
            {"via": _NOTE_INDEX, "column": "NOTE_DATE", "dir": "desc"},
        ],
    },
}


def test_the_sql_uses_the_real_schema_not_the_definition_name():
    """**実DBの名前で組む。** 定義書の名前で組むと「表が存在しない」エラーになる。"""
    sql = build_access_sql({"physical_name": "ID", "patient_key": "ID"}, "mart.PATIENT")
    assert sql == "SELECT ID FROM mart.PATIENT WHERE ID = :patient_id"


def test_the_document_type_is_actually_filtered():
    """**種別で絞る。** 絞らないとその患者の全文書が対象になる。"""
    sql = build_access_sql(_DOC_PAYLOAD, "ehr.DOCUMENT")
    assert "JOIN ehr.NOTE_INDEX" in sql
    assert "NOTE_TYPE = 'NOTE'" in sql
    assert "VOID_FLAG = 0" in sql
    assert "ORDER BY" in sql and "NOTE_DATE DESC" in sql
    # 結合は絞り込みのためだけで、値は本文表から取る（別表の値を持ってくる結合はしない）
    assert sql.startswith("SELECT c.NOTE_TEXT FROM ehr.DOCUMENT c")


def test_a_unit_without_a_path_is_unchanged():
    """経路が無いユニットは従来どおり単表で組む。"""
    sql = build_access_sql({"physical_name": "CHEMO_FLAG", "patient_key": "PID"}, "dwh.CLAIMS")
    assert sql == "SELECT CHEMO_FLAG FROM dwh.CLAIMS WHERE PID = :patient_id"


def test_no_identifiers_means_no_sql():
    assert build_access_sql({"patient_key": "PID"}, "S.T") is None
    assert build_access_sql({"physical_name": "C", "patient_key": "PID"}, "") is None


def test_a_broken_path_is_not_silently_dropped():
    """**壊れた経路で単表へ落とさない。** 落とすと、経路を黙って捨てて患者の全文書を読む SQL になる。"""
    broken = {
        "physical": {"schema": "ehr", "table": "DOCUMENT", "column": "NOTE_TEXT"},
        "patient_key": "PID",
        "access_path": {
            "filters": [
                {
                    "via": {"schema": "ehr", "table": "E; DROP", "on": ["PID"]},
                    "column": "NOTE_TYPE",
                    "equals": "NOTE",
                }
            ]
        },
    }
    assert build_access_sql(broken, "ehr.DOCUMENT") is None


def test_a_filter_value_that_is_not_a_code_is_refused():
    """リテラルは索引由来のコードだけ。形が合わなければ組み立てない。"""
    bad = {
        "physical": {"schema": "ehr", "table": "DOCUMENT", "column": "NOTE_TEXT"},
        "patient_key": "PID",
        "access_path": {"filters": [{"column": "NOTE_TYPE", "equals": "NOTE' OR '1'='1"}]},
    }
    assert build_access_sql(bad, "ehr.DOCUMENT") is None


def test_the_required_tokens_accept_the_real_table_name():
    """compiler が実表名で書かれた SQL を「索引を踏んでいない」と誤判定しないこと。"""
    derived = derive_op_args(
        {
            "unit_id": "db:SRC.DOCUMENT.NOTE_TEXT#NOTE",
            "connection_id": "ehr",
            "payload": {
                **_DOC_PAYLOAD,
                "db_qualified": "SRC.DOCUMENT",
                "physical_name": "NOTE_TEXT",
                "connection_id": "ehr",
            },
        },
        registry=ConnectionRegistry({"ehr": {"kind": "db", "scope": "external"}}),
    )
    table_token = next(t for t in derived.required_tokens if t.label.startswith("表"))
    assert "ehr.DOCUMENT" in table_token.accepted
    # **定義書上の名前は受け入れない。** 受け入れると、実DBに無い表を指す LLM の SQL が
    # 「索引を踏んでいる」と判定されてそのまま残る（実際に起きた）。
    assert "SRC.DOCUMENT" not in table_token.accepted
    # **素の表名も受け入れない。** スキーマの無い SQL は実DBで「表が存在しない」エラーになる
    assert "DOCUMENT" not in table_token.accepted
    assert table_token.accepted == ("ehr.DOCUMENT",)


# --------------------------------------------------------------------------- #
# 2.10 患者で絞らない表の宣言
#
# マスタ参照・施設固定値（施設マスタ、コード表）の表には、患者を識別する列が原理的に無い。
# **「患者キーが無ければ WHERE を付けない」は採ってはいけない**（患者データの表でキーの推定に
# 失敗したときに、全患者の行を返す SQL ができる）。必要なのは**宣言**で、宣言は
# `payload.patient_scoped` が真偽値の `False` のときだけ認める。名前（「マスタ」）から推測しない。
# --------------------------------------------------------------------------- #

_MASTER_PAYLOAD = {
    "db_qualified": "SRC.FACILITY",
    "physical_name": "FACILITY_NAME",
    "connection_id": "hosp_dwh",
    "physical": {"schema": "MST_DB", "table": "FACILITY", "column": "FACILITY_NAME"},
    "patient_key": None,
}


def _derive_master(payload: dict):
    return derive_op_args(
        _cand(SourceCategory.EXTERNAL_DB, "db:SRC.FACILITY.FACILITY_NAME", payload)
    )


def test_a_declared_unscoped_table_gets_a_select_without_a_patient_filter():
    derived = _derive_master({**_MASTER_PAYLOAD, "patient_scoped": False})

    assert derived is not None and derived.missing == ()
    assert derived.args["sql"] == "SELECT FACILITY_NAME FROM MST_DB.FACILITY"
    # SQL に :patient_id が無いので、そのバインドを要求するトークンも付けない
    assert all(":patient_id" not in t.accepted for t in derived.required_tokens)


@pytest.mark.parametrize(
    "declaration",
    [
        {},
        {"patient_scoped": True},
        {"patient_scoped": "false"},
        {"patient_scoped": 0},
        {"patient_scoped": None},
    ],
    ids=["absent", "true", "string-false", "zero", "none"],
)
def test_without_a_real_false_declaration_the_missing_key_still_stops_the_derivation(declaration):
    """宣言が無い（または `False` でない）なら、従来どおり不足として返す。WHERE を省かない。"""
    derived = _derive_master({**_MASTER_PAYLOAD, **declaration})

    assert derived is not None
    assert "sql" not in derived.args, "宣言なしに患者で絞らない SQL を組んだ"
    assert derived.missing, "不足が報告されていない"
    # 理由に、宣言という出口があることを含める（「患者キーが無い」と「絞らない表である」を混ぜない）
    assert "patient_scoped" in derived.missing[0]


def test_a_contradictory_declaration_is_not_trusted_either_way():
    """宣言と患者キー・到達経路が両立しないときは、どちらも信じず人へ回す。"""
    with_key = _derive_master({**_MASTER_PAYLOAD, "patient_scoped": False, "patient_key": "PID"})
    with_path = _derive_master(
        {
            **_MASTER_PAYLOAD,
            "patient_scoped": False,
            "access_path": {"via": [{"schema": "MST_DB", "table": "T2", "on": ["K"]}]},
        }
    )
    for derived in (with_key, with_path):
        assert derived is not None
        assert "sql" not in derived.args
        assert "矛盾しない" in derived.missing[0]


def test_the_declaration_does_not_relax_the_other_checks():
    """宣言があっても、外部DBの実識別子（`payload.physical`）が無ければ組まない。"""
    payload = {**_MASTER_PAYLOAD, "patient_scoped": False}
    del payload["physical"]
    derived = _derive_master(payload)

    assert derived is not None and "sql" not in derived.args
    assert "payload.physical" in derived.missing[0]


def test_an_unscoped_sql_is_enforced_as_confirmed_for_a_matching_llm_step():
    """索引を踏んだ SQL（患者で絞らない）は確定でき、needs_input にならない。"""
    program = _program(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "hosp_dwh",
                "sql": "SELECT FACILITY_NAME FROM MST_DB.FACILITY",
            }
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(
                SourceCategory.EXTERNAL_DB,
                "db:SRC.FACILITY.FACILITY_NAME",
                {**_MASTER_PAYLOAD, "patient_scoped": False},
            )
        ]
    )

    fixes = enforce_derived_op_args(program, specs)

    # 索引を踏んでいる LLM の SQL はそのまま採用される（是正なし）。needs_input にならない
    assert program.steps[0].needs_input is not True
    assert program.steps[0].sql == "SELECT FACILITY_NAME FROM MST_DB.FACILITY"
    assert fixes == []


def test_an_unscoped_derivation_replaces_a_sql_that_misses_the_index():
    """索引の表を踏んでいない SQL は、宣言された「患者で絞らない」SQL へ置き換わる。"""
    program = _program(
        [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "hosp_dwh",
                "sql": "SELECT NAME FROM SOMEWHERE_ELSE",
            }
        ]
    )
    specs = derive_op_args_list(
        [
            _cand(
                SourceCategory.EXTERNAL_DB,
                "db:SRC.FACILITY.FACILITY_NAME",
                {**_MASTER_PAYLOAD, "patient_scoped": False},
            )
        ]
    )

    enforce_derived_op_args(program, specs)

    assert program.steps[0].sql == "SELECT FACILITY_NAME FROM MST_DB.FACILITY"


# --------------------------------------------------------------------------- #
# 2.11 params[].value 経由の参照
#
# プロンプトは `params` の `value` に `"$前段id"` を書くことを明示的に許可している
# （`COMPILE_SYSTEM_PROMPT`）。「まず区分値を取得し、それで第2候補のクエリを組み立てる」
# 形が書けるので、**参照の連鎖が params を通る**。ここを辿らないと `_value_closure` が
# 切れ、2.5 の2つの検査（候補の優先順・フォールバックの結線）が**どちらも効かない**。
# --------------------------------------------------------------------------- #


def _param_chained_program(
    *,
    first_query: str,
    second_query: str,
    then: str = "$w",
    else_branch: str = "$b",
) -> DSLProgram:
    """連鎖が `params[].value` を通るフォールバック。

    `w` は取得値 `a` を **`{{code}}` 経由でだけ**参照する（`params: code = "$a"`）。
    したがって `w` の空判定を見る `b` の guard から `a` へ辿れるかどうかは、
    params を辿るかどうかで決まる。
    """
    return DSLProgram(
        **{
            "params": [{"name": "code", "kind": "value", "value": "$a", "allowed": ["x"]}],
            "steps": [
                {"op": "fhir_extract", "id": "a", "fhir_query": first_query},
                {"op": "llm_extract", "id": "w", "prompt": "{{code}} から値を取り出せ"},
                {"op": "compare", "id": "has_w", "left": "$w", "cmp": "is_not_empty"},
                {"op": "compare", "id": "no_w", "left": "$has_w", "cmp": "==", "right": False},
                {"op": "fhir_extract", "id": "b", "guard": "$no_w", "fhir_query": second_query},
                {
                    "op": "branch",
                    "id": "v",
                    "when": "$has_w",
                    "then": then,
                    "else_branch": else_branch,
                },
            ],
            "return": "$v",
        }
    )


def test_the_value_closure_follows_a_param_value_reference():
    """`{{name}}` を使う step が、`params[name].value` の `$id` へ到達すること。"""
    program = _param_chained_program(first_query=_FHIR_Q1, second_query=_FHIR_Q2)

    closure = _value_closure(program)

    assert "a" in closure["w"], closure["w"]
    # 連鎖はそのまま guard 側まで伸びる（`no_w` → `has_w` → `w` → （params）→ `a`）
    assert "a" in closure["no_w"], closure["no_w"]


def test_priority_inversion_is_caught_through_a_param_reference():
    """params 経由の連鎖でも優先順の逆転を止めること。

    第1 step が第2候補を、代替 step が第1候補を引いている。params を辿らないと
    `b` の guard から `a` へ到達できず、**逆転が検出されないまま確定保存される**
    （実測: `fixes == []` / `b.needs_input is False`）。
    """
    program = _param_chained_program(first_query=_FHIR_Q2, second_query=_FHIR_Q1)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    a, b = program.steps[0], program.steps[4]
    assert a.unit_id == "fhir:Observation:LOINC|718-7"  # LLM が書いたとおり第2候補
    assert b.needs_input is True
    assert b.unit_id is None
    assert "b:needs_input(優先順の逆転)" in fixes


def test_branch_routing_is_caught_through_a_param_reference():
    """params 経由の連鎖でも分岐の結線の誤りを止めること。

    `when: $has_w` が真（＝第1系統に値がある）なのに `then` が代替の `b` を採っている。
    params を辿らないと検出されない（実測: `fixes == []`）。
    """
    program = _param_chained_program(
        first_query=_FHIR_Q1, second_query=_FHIR_Q2, then="$b", else_branch="$w"
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert "v:needs_input(分岐の結線)" in fixes
    assert program.steps[5].needs_input is True


def test_a_correct_param_chained_fallback_is_not_blocked():
    """params 経由でも、正しく書かれたフォールバックは人へ回さないこと。

    連鎖を足したことで**正当なプランまで止まる**（過検出）ようになっていないかを見る。
    """
    program = _param_chained_program(first_query=_FHIR_Q1, second_query=_FHIR_Q2)
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert fixes == []
    assert program.steps[0].unit_id == "fhir:Observation:LOINC|6690-2"
    assert program.steps[4].unit_id == "fhir:Observation:LOINC|718-7"
    assert not any(s.needs_input for s in program.steps)


def test_a_literal_param_value_adds_no_edge():
    """`params[].value` がリテラルなら、どの step へも辺が伸びないこと。

    何でも辿る実装（`value` を無条件に参照名として扱う等）だと、リテラルのパラメータでも
    前段の取得へ辺が生え、**フォールバックでないプランが逆転として止まる**。

    `{{code}}` 自体は `_value_refs` が従来から「`{{id}}` で前段を指す互換形」として
    拾うので、参照名 `code` は残る。見るのは**実在の step へ届いていない**ことである。
    """
    program = DSLProgram(
        **{
            "params": [{"name": "code", "kind": "value", "value": "NOTE"}],
            "steps": [
                {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
                {"op": "llm_extract", "id": "w", "prompt": "{{code}} から値を取り出せ"},
            ],
            "return": "$w",
        }
    )

    closure = _value_closure(program)

    step_ids = {s.id for s in program.steps}
    assert closure["w"] & step_ids == set(), closure["w"]


# --------------------------------------------------------------------------- #
# 2.12 薄かった検査の補強
#
# 単一テスト1件でしか検出されていなかったミューテーションを、
# **落ちる理由が別々になる2件ずつ**で押さえる。1件だけだと、そのテストが別の理由で
# 書き換わった瞬間に規則が無検査へ戻る。
# --------------------------------------------------------------------------- #


def test_the_negation_written_as_not_equal_one_is_read_as_a_fallback():
    """`has_a != 1` と書いた否定を「空だったときに走る」と読むこと。

    `_loose_eq`（executor）は真偽を 1.0 / 0.0 として数値比較するので、`!= 1` は実行時に
    `!= true` と**同じに動く**。ここを真偽リテラルだけで読むと極性が読めず、
    **結線の誤り（取得できた値を捨てる形）が名指しされないまま通る**。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "!=", "right": 1},
            {"op": "fhir_extract", "id": "b", "guard": "$no_a", "fhir_query": _FHIR_Q2},
            # then と else_branch が入れ替わっている
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert "v:needs_input(分岐の結線)" in fixes
    assert program.steps[4].needs_input is True


def test_a_gate_written_as_equal_one_is_not_mistaken_for_an_unreadable_condition():
    """`has_a == 1` と書いたゲートを「向きが読めない」として止めないこと。

    上のテストと**同じ規則の逆側**である。`1` を真として読まないと、この正当なプランが
    `空振り判定の向き不明` で人の手待ちになる（過検出）。落ちる理由が違うので、
    片方が書き換わってももう片方が規則を守る。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "yes_a", "left": "$has_a", "cmp": "==", "right": 1},
            # 値があるときに走るゲート（フォールバックではない）
            {"op": "fhir_extract", "id": "b", "guard": "$yes_a", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert fixes == [], fixes
    assert not any(s.needs_input for s in program.steps)


def test_a_negation_of_the_is_empty_side_is_read_as_a_fallback():
    """否定を `is_empty($a) == true` と書いても「空だったときに走る」と読むこと。

    `_guard_polarity` は「参照先が真のとき空判定が真か」（`inner`）を見て向きを反転する。
    `is_empty` 側の判定を経由すると `inner` は偽になるので、この反転を落とすと
    **フォールバックがゲートと読み替えられ、結線の検査が丸ごと飛ぶ**。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "empty_a", "left": "$a", "cmp": "is_empty"},
            {"op": "compare", "id": "go_b", "left": "$empty_a", "cmp": "==", "right": True},
            {"op": "fhir_extract", "id": "b", "guard": "$go_b", "fhir_query": _FHIR_Q2},
            # then と else_branch が入れ替わっている
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert "v:needs_input(分岐の結線)" in fixes
    assert program.steps[5].needs_input is True


def test_a_gate_written_on_the_is_empty_side_is_not_blocked():
    """`is_empty($a) == false` と書いたゲートを止めないこと（上の逆側）。

    同じ反転を落とすと、こちらは**正当なプランが結線の誤りとして止まる**（過検出）。
    上のテストとは落ちる理由が違う。

    このテストは「`guard` を読む」と「`when` を読む」の区別も押さえる。`when`（`$has_a`）
    と代替取得の `guard`（`$gate`）が**別の判定を指しており、極性も逆**なので、
    どちらを読むかで結論が変わる。
    """
    program = _program(
        [
            {"op": "fhir_extract", "id": "a", "fhir_query": _FHIR_Q1},
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "empty_a", "left": "$a", "cmp": "is_empty"},
            # 値が**あるとき**に走るゲート（フォールバックではない）
            {"op": "compare", "id": "gate", "left": "$empty_a", "cmp": "==", "right": False},
            {"op": "fhir_extract", "id": "b", "guard": "$gate", "fhir_query": _FHIR_Q2},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$b", "else_branch": "$a"},
        ]
    )
    specs = _two_fhir_candidates()

    fixes = enforce_derived_op_args(program, specs)

    assert fixes == [], fixes
    assert program.steps[5].needs_input is False


# 経過表のように、何の値かが同じ表の項目コードで決まる単位（体温も脈拍も VITALS.NUM）。
# 形は経過表の項目単位（`kawasemi_indexer.item_units`）に合わせる。
ITEM_PAYLOAD = {
    **EXTERNAL_DB_PAYLOAD,
    "db_qualified": "SRC.OBSERVATIONS",
    "physical_name": "VALUE",
    "patient_key": "PID",
    "physical": {"schema": "dwh", "table": "OBSERVATIONS", "column": "VALUE"},
    "item_code": "OBS001",
    "item_name": "Spo2",
    "item_filter_column": "ITEM_CODE",
    "item_row_filters": [
        {"column": "DELETED", "equals": "0"},
        {"column": "ACTIVE", "equals": "1"},
    ],
    "access_path": {
        "filters": [
            {"column": "ITEM_CODE", "equals": "OBS001"},
            {"column": "DELETED", "equals": "0"},
            {"column": "ACTIVE", "equals": "1"},
        ],
        "order_by": [{"column": "VALID_TO", "dir": "asc"}],
    },
}
ITEM_UNIT_ID = "db:SRC.OBSERVATIONS.VALUE#OBS001"


def test_derive_item_code_unit_filters_by_item_code():
    spec = derive_op_args(_cand(SourceCategory.EXTERNAL_DB, ITEM_UNIT_ID, ITEM_PAYLOAD))
    assert spec is not None and spec.is_complete
    assert spec.args["sql"] == (
        "SELECT c.VALUE FROM dwh.OBSERVATIONS c WHERE c.PID = :patient_id"
        " AND c.ITEM_CODE = 'OBS001' AND c.DELETED = '0' AND c.ACTIVE = '1'"
        " ORDER BY c.VALID_TO ASC"
    )


def _item_program(sql: str) -> DSLProgram:
    return _program(
        [{"op": "external_db_extract", "id": "v", "connection_id": "hosp_dwh", "sql": sql}]
    )


@pytest.mark.parametrize(
    "sql",
    [
        # 項目コードで絞らない（全項目の値が混ざる）
        "SELECT VALUE FROM dwh.OBSERVATIONS WHERE PID = :patient_id",
        # 項目コードでは絞るが、有効な行の条件が無い（削除行・履歴行が混ざる）
        "SELECT VALUE FROM dwh.OBSERVATIONS WHERE PID = :patient_id AND ITEM_CODE = 'OBS001'",
    ],
)
def test_enforce_replaces_sql_missing_item_code_or_row_filters(sql: str):
    program = _item_program(sql)
    specs = derive_op_args_list([_cand(SourceCategory.EXTERNAL_DB, ITEM_UNIT_ID, ITEM_PAYLOAD)])

    enforce_derived_op_args(program, specs)

    out = program.steps[0].sql or ""
    assert "ITEM_CODE = 'OBS001'" in out and "ACTIVE = '1'" in out


def test_enforce_keeps_item_sql_that_names_every_condition():
    """条件の列が全部現れる SQL は臨床ロジックごと採る（列名が現れるかしか見ない限界もここで固定）。"""
    sql = (
        "SELECT VALUE FROM dwh.OBSERVATIONS WHERE PID = :patient_id AND ITEM_CODE = 'OBS001'"
        " AND DELETED = '0' AND ACTIVE = '1' ORDER BY VALID_TO DESC"
    )
    program = _item_program(sql)
    specs = derive_op_args_list([_cand(SourceCategory.EXTERNAL_DB, ITEM_UNIT_ID, ITEM_PAYLOAD)])

    enforce_derived_op_args(program, specs)

    assert program.steps[0].sql == sql


# --- 経路の無い文書種別の単位は組み立てない --------------------------------------------


@pytest.mark.parametrize("key", ["doc_type_code", "doc_filter_table", "doc_filter_column"])
def test_a_doc_unit_without_an_access_path_is_reported_missing(key: str):
    """文書種別の単位で `access_path` が無いと、組み立てた SQL は患者の全文書（削除済みを含む）を
    返す。`physical` が揃っていても組み立てず不足として返す（indexer 側の防御と二重）。"""
    payload = {
        **EXTERNAL_DB_PAYLOAD,
        "physical": {"schema": "ehr", "table": "DOCUMENT", "column": "NOTE_TEXT"},
        "physical_name": "NOTE_TEXT",
        key: {"doc_type_code": "NOTE", "doc_filter_table": "ehr.NOTE_INDEX"}.get(key, "NOTE_TYPE"),
    }
    spec = derive_op_args(
        _cand(SourceCategory.EXTERNAL_DB, "db:SRC.DOCUMENT.NOTE_TEXT#NOTE", payload)
    )
    assert spec is not None
    assert "sql" not in spec.args
    assert spec.is_complete is False
    assert any("access_path" in m for m in spec.missing)


def test_a_doc_unit_with_an_access_path_is_built():
    """経路があれば従来どおり結合して種別で絞る SQL を組む（既存の挙動を変えない）。"""
    payload = {
        **EXTERNAL_DB_PAYLOAD,
        **_DOC_PAYLOAD,
        "doc_type_code": "NOTE",
        "doc_filter_table": "ehr.NOTE_INDEX",
        "doc_filter_column": "NOTE_TYPE",
    }
    spec = derive_op_args(
        _cand(SourceCategory.EXTERNAL_DB, "db:SRC.DOCUMENT.NOTE_TEXT#NOTE", payload)
    )
    assert spec is not None
    assert spec.is_complete is True
    assert "NOTE_TYPE = 'NOTE'" in spec.args["sql"]
