"""`kawasemi.ai_input.schemas` のPydanticスキーマのユニットテスト

検証対象:
- 必須/任意フィールドと既定値
- ネストモデルの dict からの自動コアース
- `DSLProgram` の `return` エイリアス（populate_by_name 含む）
- `DSLStep` の str|int 共用フィールド
- `DSLStep` のキー解決フィールド（`key_type` / `physical_format`）が dump で保たれること
- `EvidenceItem` の「Optional だが必須（既定値なし）」セマンティクス
"""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from kawasemi.ai_input.schemas import (
    AutofillRequest,
    AutofillResponse,
    ClinicalAnnotationIn,
    ClinicalAnnotationSaveRequest,
    DSLProgram,
    DSLStep,
    EvidenceItem,
    GeneratedPlans,
    GeneratedRunbooks,
    LLMLocationChoice,
    RunbookStep,
    SystemLocationCandidate,
    SystemLocationIn,
)

# ---------------------------------------------------------------------------
# ステップ1(役割1): 臨床的コンテキスト
# ---------------------------------------------------------------------------


def test_clinical_annotation_in_defaults() -> None:
    a = ClinicalAnnotationIn(link_id="q1")
    assert a.link_id == "q1"
    assert a.item_text is None
    assert a.annotation is None


def test_clinical_annotation_in_requires_link_id() -> None:
    with pytest.raises(ValidationError):
        ClinicalAnnotationIn()  # type: ignore[call-arg]


def test_clinical_annotation_save_request_coerces_items() -> None:
    req = ClinicalAnnotationSaveRequest(items=[{"link_id": "q1", "annotation": "所見"}])
    assert isinstance(req.items[0], ClinicalAnnotationIn)
    assert req.items[0].annotation == "所見"


# ---------------------------------------------------------------------------
# ステップ1(役割2): データ所在
# ---------------------------------------------------------------------------


def test_system_location_candidate_defaults() -> None:
    c = SystemLocationCandidate()
    assert c.source_category == "manual"
    assert c.location is None
    assert c.unit_id is None
    assert c.connection_id is None
    assert c.payload is None
    assert c.llm_model is None


def test_system_location_in_distinguishes_absent_from_empty_candidates() -> None:
    """`candidates` の**未指定（None）と空配列を区別する**。

    `_normalize_candidates` はこの区別で分岐する——未指定なら旧形式の平坦な
    フィールドを1候補として読み、空配列なら「候補を全部消す」保存として扱う。
    既定を `[]` にすると旧形式が読めなくなる。
    """
    assert SystemLocationIn(link_id="q1").candidates is None
    assert SystemLocationIn(link_id="q1", candidates=[]).candidates == []


def test_system_location_in_coerces_candidates() -> None:
    item = SystemLocationIn(
        link_id="q1",
        candidates=[{"location": "病名テーブル", "unit_id": "u1"}],
    )
    assert item.candidates is not None
    assert isinstance(item.candidates[0], SystemLocationCandidate)
    assert item.candidates[0].location == "病名テーブル"
    # 未指定の source_category は既定 "manual"
    assert item.candidates[0].source_category == "manual"


def test_system_location_in_rejects_too_many_candidates() -> None:
    """候補件数の上限。上限を超えたら受け口で弾く。

    保存できてもコンパイラ（ステップ3）は主候補＋代替2件しか読まないので、
    それを超える候補は記録に残るのに取得計画へ現れない。
    """
    ok = [{"source_category": "local_db", "location": f"表{i}"} for i in range(3)]
    assert len(SystemLocationIn(link_id="q1", candidates=ok).candidates or []) == 3
    with pytest.raises(ValidationError):
        SystemLocationIn(link_id="q1", candidates=[*ok, {"location": "表4"}])


def test_system_location_in_keeps_legacy_flat_fields() -> None:
    """旧形式（`candidates` を持たない平坦な形）が捨てられずに残ること。"""
    item = SystemLocationIn(
        link_id="q1",
        source_category="local_db",
        location="病名テーブル",
        unit_id="u1",
        connection_id="c1",
        payload={"table_logical": "病名"},
    )
    assert item.candidates is None
    assert item.source_category == "local_db"
    assert item.unit_id == "u1"
    assert item.payload == {"table_logical": "病名"}


def test_llm_location_choice_defaults() -> None:
    choice = LLMLocationChoice(link_id="q1")
    assert choice.chosen_cand_id is None
    assert choice.source_category == "manual"


def test_llm_location_choice_requires_link_id() -> None:
    with pytest.raises(ValidationError):
        LLMLocationChoice()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# ステップ2: 手順書
# ---------------------------------------------------------------------------


def test_runbook_step_defaults() -> None:
    step = RunbookStep(order=1, title="病名取得")
    assert step.description == ""
    assert step.action_type is None
    assert step.data_hint is None


def test_runbook_step_requires_order_and_title() -> None:
    with pytest.raises(ValidationError):
        RunbookStep(title="x")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        RunbookStep(order=1)  # type: ignore[call-arg]


def test_runbook_step_order_must_be_int() -> None:
    with pytest.raises(ValidationError):
        RunbookStep(order="first", title="x")  # type: ignore[arg-type]


def test_generated_runbooks_nesting() -> None:
    rbs = GeneratedRunbooks(runbooks=[{"link_id": "q1", "steps": [{"order": 1, "title": "t"}]}])
    assert rbs.runbooks[0].link_id == "q1"
    assert rbs.runbooks[0].steps[0].title == "t"


# ---------------------------------------------------------------------------
# ステップ3: DSL
# ---------------------------------------------------------------------------


def test_dsl_step_minimal_defaults() -> None:
    step = DSLStep(op="compare", id="s1")
    assert step.needs_input is False
    assert step.multi is False
    assert step.header is True
    assert step.sql is None
    assert step.reasoning is None
    assert step.connection_id is None


def test_dsl_step_requires_op_and_id() -> None:
    with pytest.raises(ValidationError):
        DSLStep(op="compare")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        DSLStep(id="s1")  # type: ignore[call-arg]


@pytest.mark.parametrize("col", ["MRN", 3])
def test_dsl_step_key_column_accepts_str_or_int(col) -> None:
    step = DSLStep(op="csv_extract", id="s1", key_column=col, column=col)
    assert step.key_column == col
    assert step.column == col


def test_dsl_step_key_binding_fields() -> None:
    # キー解決用フィールド（executor._key_for_step が参照する）がスキーマに存在する
    step = DSLStep(op="external_db_extract", id="v", key_type="mrn", physical_format={"pad": 10})
    assert step.key_type == "mrn"
    assert step.physical_format == {"pad": 10}


def test_dsl_step_key_binding_fields_default_to_none() -> None:
    # 未宣言なら None（executor は登録番号をそのままバインドする後方互換経路）
    step = DSLStep(op="sql_extract", id="v")
    assert step.key_type is None
    assert step.physical_format is None


def test_dsl_program_dump_preserves_key_binding_fields() -> None:
    # pydantic 経由の保存（routes._program_dump 相当）で key_type/physical_format が
    # 落ちないことを固定する（未定義だと extra として黙って捨てられ、キー解決が壊れる）
    raw = {
        "steps": [
            {
                "op": "external_db_extract",
                "id": "v",
                "connection_id": "dwh",
                "sql": "SELECT 1",
                "key_type": "mrn",
                "physical_format": {"pad": 10},
            }
        ],
        "return": "$v",
    }
    dumped = DSLProgram(**raw).model_dump(by_alias=True, exclude_none=True)
    assert dumped["steps"][0]["key_type"] == "mrn"
    assert dumped["steps"][0]["physical_format"] == {"pad": 10}
    assert dumped["return"] == "$v"


def test_dsl_step_declares_every_field_the_executor_reads() -> None:
    """executor が step から読むキーは全て DSLStep に定義されていること。

    DSLStep は extra="ignore"（pydantic 既定）なので、未定義キーは pydantic 経由の
    保存で黙って捨てられる。executor 側の `step.get("...")` を機械的に集め、
    スキーマとの取りこぼしを1本で固定する（未定義キーの黙った欠落の再発防止）。
    """
    src = (Path(__file__).resolve().parents[2] / "kawasemi/ai_input/executor.py").read_text()
    read_keys = set(re.findall(r'step\.get\(\s*"([a-z_]+)"', src))
    # 実行時にのみ意味を持ち、プランに保存されないキーはここに列挙する（現状なし）
    runtime_only: set[str] = set()
    missing = sorted(read_keys - set(DSLStep.model_fields) - runtime_only)
    assert not missing, f"executor が読むのに DSLStep 未定義のフィールド: {missing}"


def test_dsl_step_declares_every_field_e2e_plans_use() -> None:
    """E2E 台本のプラン（コンパイラの応答）が使う全キーが DSLStep に定義されていること。"""
    from kawasemi.ai_input.e2e.scripted_llm import load_design_script

    used: set[str] = set()
    for item in load_design_script().values():
        for step in item["program"].get("steps", []):
            used |= set(step)
    assert used, "E2E 台本にプランの step が無い（前提が崩れている）"
    missing = sorted(used - set(DSLStep.model_fields))
    assert not missing, f"E2E プランが使うのに DSLStep 未定義のフィールド: {missing}"


def test_dsl_program_dump_preserves_deprecated_connection_alias() -> None:
    # executor が後方互換で読む connection（connection_id の旧名）も落とさない
    raw = {"steps": [{"op": "external_db_extract", "id": "v", "connection": "dwh"}]}
    dumped = DSLProgram(**raw).model_dump(by_alias=True, exclude_none=True)
    assert dumped["steps"][0]["connection"] == "dwh"


def test_dsl_program_dump_preserves_step_llm_model() -> None:
    # executor の 3 段フォールバック中段（step 単位のモデル固定）も落とさない
    raw = {"steps": [{"op": "llm_extract", "id": "v", "llm_model": "model-c:9b"}]}
    dumped = DSLProgram(**raw).model_dump(by_alias=True, exclude_none=True)
    assert dumped["steps"][0]["llm_model"] == "model-c:9b"


def test_dsl_program_dump_preserves_plan_key_binding() -> None:
    """確認済みプラン（dict 直挿し）を pydantic 経由で往復しても、キー解決の宣言が保たれる。

    外部 DB の step は key_type と physical_format（ゼロ詰め）を、CSV の
    step は key_type だけを持つ。どちらも往復後に**値ごと**残ること。
    """
    raw = {
        "steps": [
            {
                "op": "external_db_extract",
                "id": "a",
                "connection_id": "dwh",
                "sql": "SELECT ADM_DATE FROM main.ADMISSION WHERE PT_ID = :patient_id",
                "key_type": "mrn",
                "physical_format": {"pad": 10, "padchar": "0"},
            },
            {
                "op": "csv_extract",
                "id": "b",
                "connection_id": "dpc_ef",
                "path": "ef.csv",
                "key_column": "患者ID",
                "column": "薬効分類",
                "key_type": "billing_id",
            },
        ],
        "return": "$a",
    }
    steps = DSLProgram(**raw).model_dump(by_alias=True, exclude_none=True)["steps"]
    assert steps[0]["key_type"] == "mrn"
    assert steps[0]["physical_format"] == {"pad": 10, "padchar": "0"}
    assert steps[1]["key_type"] == "billing_id"
    assert "physical_format" not in steps[1]


def test_dsl_program_dump_preserves_fhir_key_binding() -> None:
    """physical_format を持たない FHIR の step でも、キー解決の宣言が保たれる。

    `key_type` と `fhir_query` の `{{patient_id}}` プレースホルダ保持を固定する。
    """
    raw = {
        "steps": [
            {
                "op": "fhir_extract",
                "id": "v",
                "connection_id": "hosp_fhir",
                "fhir_query": "/Observation?code=pathogen&patient={{patient_id}}",
                "fhir_path": "valueString",
                "key_type": "fhir_patient_id",
            }
        ],
        "return": "$v",
    }
    (step,) = DSLProgram(**raw).model_dump(by_alias=True, exclude_none=True)["steps"]
    assert step["key_type"] == "fhir_patient_id"
    assert step["fhir_query"] == "/Observation?code=pathogen&patient={{patient_id}}"
    assert "physical_format" not in step


def test_dsl_program_defaults() -> None:
    prog = DSLProgram()
    assert prog.steps == []
    assert prog.return_ is None


def test_dsl_program_return_alias_populate() -> None:
    # エイリアス "return" でも内部名 return_ でも設定できる（populate_by_name）
    by_alias = DSLProgram(**{"return": "$s1"})
    assert by_alias.return_ == "$s1"
    by_name = DSLProgram(return_="$s2")
    assert by_name.return_ == "$s2"


def test_dsl_program_dump_uses_return_alias() -> None:
    dumped = DSLProgram(return_="$s1").model_dump(by_alias=True)
    assert dumped["return"] == "$s1"
    assert "return_" not in dumped


def test_dsl_program_coerces_steps() -> None:
    prog = DSLProgram(
        steps=[{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}],
        **{"return": "$s1"},
    )
    assert isinstance(prog.steps[0], DSLStep)
    assert prog.steps[0].sql == "SELECT 1"
    assert prog.return_ == "$s1"


def test_generated_plans_nesting() -> None:
    plans = GeneratedPlans(plans=[{"link_id": "q1", "program": {"steps": [], "return": "$x"}}])
    assert plans.plans[0].link_id == "q1"
    assert plans.plans[0].program.return_ == "$x"


# ---------------------------------------------------------------------------
# ステップ4: 自動入力
# ---------------------------------------------------------------------------


def test_autofill_request_defaults() -> None:
    req = AutofillRequest(patient_id="P001")
    assert req.study_id == ""
    assert req.model is None


def test_autofill_request_accepts_well_formed_study_id() -> None:
    req = AutofillRequest(patient_id="P001", study_id=" STUDY-01.v2 ")
    assert req.study_id == "STUDY-01.v2"


@pytest.mark.parametrize(
    "bad_study_id",
    [
        "a" * 65,  # 長さ超過
        "study/01",  # 許可外文字（パスセパレータ）
        "study id",  # 空白
        "study;drop",  # 許可外文字
    ],
)
def test_autofill_request_rejects_malformed_study_id(bad_study_id: str) -> None:
    with pytest.raises(ValidationError):
        AutofillRequest(patient_id="P001", study_id=bad_study_id)


def test_autofill_request_has_no_encounter_id() -> None:
    """encounter_id は供給経路が無いので持たない。

    組み込みパラメータとして一度復活させたが、UI もバッチも送っておらず、
    宣言だけあると「コンパイラが案内どおり生成 → 検証は通る → 実行は毎回失敗」を
    再生産する。供給側を用意するまで持たせない。
    """
    assert "encounter_id" not in AutofillRequest.model_fields


def test_autofill_request_requires_patient_id() -> None:
    with pytest.raises(ValidationError):
        AutofillRequest()  # type: ignore[call-arg]


def test_evidence_item_optional_fields_are_required() -> None:
    # str | None でも既定値が無いため明示指定が必須
    with pytest.raises(ValidationError):
        EvidenceItem(source_type="sql")  # type: ignore[call-arg]


def test_evidence_item_accepts_explicit_none() -> None:
    ev = EvidenceItem(
        source_type="sql",
        query_used=None,
        raw_data={"value": 1},
        path_used=None,
        reasoning=None,
    )
    assert ev.source_type == "sql"
    assert ev.raw_data == {"value": 1}


def test_autofill_response_coerces_nested_evidence() -> None:
    resp = AutofillResponse(
        success=True,
        prefill_data={"q1": "値"},
        evidence={
            "q1": {
                "source_type": "sql",
                "query_used": "SELECT 1",
                "raw_data": 42,
                "path_used": None,
                "reasoning": "理由",
            }
        },
        status="completed",
    )
    assert isinstance(resp.evidence["q1"], EvidenceItem)
    assert resp.evidence["q1"].raw_data == 42
    assert resp.execution_id is None
