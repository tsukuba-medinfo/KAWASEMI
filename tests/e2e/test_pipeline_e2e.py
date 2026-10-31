"""E2E 決定的層: 散らばったダミーデータを索引で探し出して取得し、回答として保存するまで。

Ollama を使わない。CI で skip せずに走る（docs/e2e_pipeline.md）。

1. 索引: 生成器の DB から `ingest → 連結 → build_index → 項目単位の追記` を indexer の
   実コードで通す（`HashingEmbedder`）。**関門 (a)**
2. 設計: HTTP（ASGITransport）で臨床的意味の下書き → 所在の下書き → 手順書 → DSL の
   コンパイル → 確認済み。LLM は台本（`kawasemi/ai_input/e2e/scripted_llm.py`）で、
   **所在探索の順位は索引検索が決める**。**関門 (b)(c)**
3. 実行: 収集者が 10 名に autofill（本番）→ 回答を送信して QuestionnaireResponse を保存。
   外部 DB は SQLite 2 接続、FHIR は E2E のスタブをプロセス内で、ファイルと CSV は接続の
   root 配下。**関門 (d)(e)**

全工程はモジュールで1回だけ走らせ（`pipeline` fixture。工程そのものは実機層と共有する
`kawasemi/ai_input/e2e/pipeline.py`）、関門ごとのテストが結果を見る。
落ちたときは項目×関門の表を出し、JSON を一時ディレクトリへ書く。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from kawasemi.ai_input import e2e
from kawasemi.ai_input.e2e import SUBJECTS, sources
from kawasemi.ai_input.e2e.embedder import HashingEmbedder
from kawasemi.ai_input.e2e.gates import GATES, PASS, GateReport
from kawasemi.ai_input.e2e.pipeline import PipelineConfig, PipelineResult, run_pipeline
from kawasemi.ai_input.e2e.scripted_llm import (
    STAGE_CLINICAL,
    STAGE_COMPILE,
    STAGE_EXTRACT,
    STAGE_HINT,
    STAGE_LOCATION,
    STAGE_RUNBOOK,
    STAGE_VISION,
    ScriptedLLM,
    ScriptMiss,
    build_vision_transcripts,
    load_design_script,
)
from kawasemi.core.config import settings


@dataclass
class PipelineRun:
    """決定的層の1回分（共有の `PipelineResult` に台本 LLM と表の出力先を添える）。"""

    result: PipelineResult
    llm: ScriptedLLM
    report_path: Path

    @property
    def report(self) -> GateReport:
        return self.result.report

    @property
    def gold(self) -> dict[str, dict[str, Any]]:
        return self.result.gold

    @property
    def programs(self) -> dict[str, dict[str, Any]]:
        return self.result.programs

    @property
    def evidence(self) -> dict[str, dict[str, Any]]:
        return self.result.evidence

    @property
    def notes(self) -> dict[str, Any]:
        return self.result.notes


@pytest.fixture(scope="module")
async def pipeline(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[PipelineRun, None]:
    """工程は `kawasemi/ai_input/e2e/pipeline.py` が持つ（実機層と共有）。ここは差し込む物だけ。"""
    tmp = tmp_path_factory.mktemp("e2e_pipeline")
    llm = ScriptedLLM(
        {subj.reg_no: subj.name for subj in SUBJECTS},
        design=load_design_script(),
        # Vision の文字起こしは生成物の PDF から作るので、生成を先に済ませる
        # （run_pipeline も同じ場所へ同じ物を作り直す。決定的なので同一になる）
        vision_transcripts=_vision_transcripts(tmp / "generated"),
    )
    result = await run_pipeline(
        PipelineConfig(
            work_dir=tmp,
            embedder=HashingEmbedder(model=settings.embedding_model),
            script=load_design_script(),
            llm=llm,
            query_embedder=lambda **kw: HashingEmbedder(model=kw["model"]),
        )
    )
    report = result.report
    report.notes.append(f"台本 LLM の呼び出し: {llm.stage_counts()}")
    report.notes.extend(f"台本に無い入力（ScriptMiss）: {m}" for m in llm.misses)
    report.notes.append(f"典拠つきの臨床下書き: {result.notes.get('clinical_draft')}")
    # 検索での提示順（0始まり）。画面への応答は一推しを先頭へ繰り上げるので、台本 LLM が
    # 選んだ cand_id（＝提示順）から取る
    result.notes["location_rank"] = {
        lid: rank for c in llm.calls if c.stage == STAGE_LOCATION for lid, rank in c.detail.items()
    }
    report.notes.append(f"所在の提示順（0始まり・検索の順位）: {result.notes['location_rank']}")
    report_path = tmp / "gate_report.json"
    report.write(report_path)
    print(f"\n関門の表（{report_path}）\n{report.table()}")
    # **台本の引き損ないは症状ではなく原因で落とす。** 製品の経路は LLM の失敗を握って縮退する
    # ので、関門の表には「上位8件に無い」のような別の症状でしか出ない
    assert llm.misses == [], f"台本に無い入力があった: {llm.misses}\n{report.table()}"
    yield PipelineRun(result, llm, report_path)


def _vision_transcripts(generated: Path) -> dict[str, str]:
    sources.generate_all(generated, gold_path=None)
    return build_vision_transcripts(generated / e2e.REPORTS_DIR.name, SUBJECTS)


def _assert_gate(result: PipelineRun, gate: str) -> None:
    failed = result.report.failures(gate)
    assert not failed, (
        f"関門 ({gate}) で落ちた項目: {[it.link_id for it in failed]}\n{result.report.table()}"
    )


# --------------------------------------------------------------------------- #
# 関門
# --------------------------------------------------------------------------- #


def test_gate_a_index_has_every_gold_unit(pipeline: PipelineRun) -> None:
    _assert_gate(pipeline, "a")
    assert pipeline.notes["index"]["item_units"], "項目単位が追記されていない"


def test_gate_b_location_search_reaches_gold_in_top_k(pipeline: PipelineRun) -> None:
    _assert_gate(pipeline, "b")


def test_gate_c_compiles_without_needs_input(pipeline: PipelineRun) -> None:
    _assert_gate(pipeline, "c")


def test_gate_d_executed_values_match_gold(pipeline: PipelineRun) -> None:
    _assert_gate(pipeline, "d")


def test_gate_e_answers_are_saved(pipeline: PipelineRun) -> None:
    _assert_gate(pipeline, "e")


def test_every_item_passes_every_gate(pipeline: PipelineRun) -> None:
    """表の全セルが PASS（XFAIL も無い）。落ちた項目は最初に落ちた関門と一緒に出す。"""
    bad = {
        it.link_id: it.first_failure
        for it in pipeline.report.items.values()
        if any(it.gates[g] != PASS for g in GATES)
    }
    assert not bad, f"{bad}\n{pipeline.report.table()}"
    assert (
        json.loads(pipeline.report_path.read_text("utf-8"))["items"].keys() == pipeline.gold.keys()
    )


# --------------------------------------------------------------------------- #
# 経路と台本の検査
# --------------------------------------------------------------------------- #


def test_fallback_item_reads_text_only_for_patients_without_dwh_value(
    pipeline: PipelineRun,
) -> None:
    """発症日: DWH が空の 3 名だけ本文（入院時サマリ）を読み、他の 7 名は DB で決まる。"""
    gold = pipeline.gold["onset_date"]
    for reg, ev in pipeline.evidence.items():
        trace = {t["id"]: t for t in ev["onset_date"]["trace"]}
        if gold["expected_source"][reg] == "text":
            assert trace["a"]["output"] is None, reg
            assert trace["doc"]["skipped"] is False and trace["b"]["skipped"] is False, reg
        else:
            assert trace["doc"]["skipped"] is True and trace["b"]["skipped"] is True, reg
    texts = sorted(r for r, v in gold["expected_source"].items() if v == "text")
    assert texts == ["E2E-0003", "E2E-0006", "E2E-0009"]
    read = sorted(
        c.detail["subject"]
        for c in pipeline.llm.calls
        if c.stage == STAGE_EXTRACT and c.detail["task"] == "onset_date"
    )
    assert read == texts


# 本文読みの経路の患者のうち1名の入院時サマリを消した変種
MISSING_SUMMARY = "E2E-0003"


@pytest.fixture(scope="module")
async def pipeline_missing_summary(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncGenerator[PipelineRun, None]:
    """`MISSING_SUMMARY` の入院時サマリを実行の直前に消して、もう1回通す。

    本体の `pipeline` とは別の一時ディレクトリで走らせるので、gold も本体の関門の結果も
    変わらない。設計（索引・所在・DSL）は消す前に済んでいるので、本体と同じプランで実行する。
    """
    tmp = tmp_path_factory.mktemp("e2e_pipeline_missing_summary")
    llm = ScriptedLLM(
        {subj.reg_no: subj.name for subj in SUBJECTS},
        design=load_design_script(),
        vision_transcripts=_vision_transcripts(tmp / "generated"),
    )
    subj = next(s for s in SUBJECTS if s.reg_no == MISSING_SUMMARY)

    def _remove_summary(generated: Path) -> None:
        target = generated / e2e.REPORTS_DIR.name / subj.report_key / "admission_summary.txt"
        assert target.exists(), target
        target.unlink()

    result = await run_pipeline(
        PipelineConfig(
            work_dir=tmp,
            embedder=HashingEmbedder(model=settings.embedding_model),
            script=load_design_script(),
            llm=llm,
            query_embedder=lambda **kw: HashingEmbedder(model=kw["model"]),
            before_execute=_remove_summary,
        )
    )
    yield PipelineRun(result, llm, tmp / "gate_report.json")


def test_missing_report_file_does_not_reach_llm_extract(
    pipeline_missing_summary: PipelineRun,
) -> None:
    """ファイルが無い患者では本文読みの llm_extract が呼ばれず、項目は失敗コード付きで空。

    E2E 実機層の初回では、`file_extract` が失敗した後の `llm_extract` が空の本文で走り、
    LLM の断り文が date 項目の値として保存された。台本 LLM は空の本文に値を返さない
    （`ScriptMiss`）ので、**呼ばれたかどうかは台本の呼び出し記録で見る**。
    """
    run = pipeline_missing_summary
    gold = run.gold["onset_date"]
    assert gold["expected_source"][MISSING_SUMMARY] == "text"

    item = run.evidence[MISSING_SUMMARY]["onset_date"]
    trace = {t["id"]: t for t in item["trace"]}
    assert trace["doc"]["skipped"] is False and trace["doc"]["failure_code"], trace["doc"]
    assert trace["b"]["skipped"] is False
    assert "llm" not in trace["b"], "LLM が呼ばれた"
    assert trace["b"]["failure_code"] == trace["doc"]["failure_code"]
    assert item["value"] is None
    assert item["failure_code"] == trace["doc"]["failure_code"]

    # 台本 LLM へ届いた発症日の本文読みは、ファイルが残っている2名だけ
    read = sorted(
        c.detail["subject"]
        for c in run.llm.calls
        if c.stage == STAGE_EXTRACT and c.detail["task"] == "onset_date"
    )
    assert read == ["E2E-0006", "E2E-0009"]
    assert run.llm.misses == [], run.llm.misses

    # 落ちるのは消した1名だけ（他の患者の発症日は本体と同じく期待値どおり）
    detail = run.report.items["onset_date"].detail.get("d", [])
    assert detail and all(d.startswith(f"{MISSING_SUMMARY}:") for d in detail), detail

    # 回答の保存（関門 e の食い違い）でも、発症日が入らないのは消した1名だけ。(d) で落ちた
    # 項目の (e) は表では skip なので、パイプラインが残した食い違いの記録で見る
    saved = run.notes["mismatch_e"].get("onset_date", [])
    assert saved == [f"{MISSING_SUMMARY}: 期待 {gold['expected'][MISSING_SUMMARY]!r} / 保存 None"]


def test_mrn_keyed_steps_carry_zero_padding(pipeline: PipelineRun) -> None:
    """mrn で引く DWH と CSV の step は `physical_format: {pad: 10}` を持つ。

    台本は key_type も physical_format も書かない。コンパイラが索引と接続の宣言から焼き込む。
    """
    seen = 0
    for lid, program in pipeline.programs.items():
        for step in program.get("steps", []):
            if step.get("key_type") == e2e.KT_MRN:
                assert step.get("physical_format") == {"pad": 10}, (lid, step["id"])
                seen += 1
            elif step.get("key_type") in (e2e.KT_BILLING, e2e.KT_REPORT, e2e.KT_FHIR):
                assert not step.get("physical_format"), (lid, step["id"])
    assert seen >= 7


def test_unscoped_db_units_get_no_patient_key(pipeline: PipelineRun) -> None:
    """患者で絞らない表（item_counts・マスタ）の取得 step に患者キーが付かない。

    台本の13項目はどれも患者で絞る表を引くので、索引に実在する unscoped の単位を直接コンパイラの
    是正と焼き込みに通す。`dwh` は接続の既定 `key_type: mrn` を宣言しているので、DB の step に
    接続の既定を当てる実装だとここで落ちる（付くと未登録の被験者で項目ごと飛ぶ）。
    """
    from kawasemi.ai_input.compiler import bake_patient_keys, enforce_derived_op_args
    from kawasemi.ai_input.resource_index.connections import ConnectionRegistry
    from kawasemi.ai_input.resource_index.derive import derive_op_args_list, is_patient_unscoped
    from kawasemi.ai_input.resource_index.key_types import KeyTypeCatalog
    from kawasemi.ai_input.resource_index.store import ResourceIndexStore
    from kawasemi.ai_input.schemas import DSLProgram

    registry = ConnectionRegistry.load(pipeline.notes["connections_file"])
    assert registry.key_type_of(e2e.CONN_DWH) == e2e.KT_MRN  # 罠が張られていること
    catalog = KeyTypeCatalog.load(e2e.KEY_TYPES_E2E)
    store = ResourceIndexStore.load(pipeline.notes["index"]["index_dir"])
    units = [u for u in store.units if u.kind == "db" and is_patient_unscoped(u.payload)]
    assert units, "索引に患者で絞らない db 単位が無い（knowledge.yaml の patient_unscoped_tables）"
    for unit in units:
        cand = {
            "unit_id": unit.unit_id,
            "connection_id": unit.payload.get("connection_id"),
            "payload": unit.payload,
        }
        derived = derive_op_args_list([cand], registry)
        assert derived and derived[0].is_complete, (unit.unit_id, derived)
        spec = derived[0]
        program = DSLProgram(steps=[{"op": spec.op, "id": "v", **spec.args}], **{"return": "$v"})
        enforce_derived_op_args(program, derived)
        bake_patient_keys(program, derived, registry, catalog)
        step = program.steps[0]
        assert step.key_type is None and step.physical_format is None, unit.unit_id
        assert not step.needs_input, (unit.unit_id, step.reasoning)


def test_every_design_stage_and_runtime_read_went_through_the_script(
    pipeline: PipelineRun,
) -> None:
    counts = pipeline.llm.stage_counts()
    assert pipeline.llm.misses == []
    # **全項目が各段階の応答を受け取った**（段階ごとに1回以上、では1チャンクの脱落を見逃す）
    for stage in (STAGE_HINT, STAGE_LOCATION, STAGE_RUNBOOK, STAGE_COMPILE):
        answered = {lid for c in pipeline.llm.calls if c.stage == stage for lid in c.link_ids}
        assert answered == set(pipeline.gold), (stage, sorted(set(pipeline.gold) - answered))
    # 臨床的意味は、典拠候補が足切りを越えた項目があるときだけ LLM が呼ばれる
    assert counts.get(STAGE_CLINICAL, 0) >= 1, counts
    # Vision（手書き同意書）は全患者、本文読みは診断名10＋同意日10＋発症日3
    assert counts.get(STAGE_VISION) == len(SUBJECTS), counts
    assert counts.get(STAGE_EXTRACT) == 2 * len(SUBJECTS) + 3, counts


# --------------------------------------------------------------------------- #
# 台本 LLM 単体: 台本に無い入力は失敗する
# --------------------------------------------------------------------------- #


async def test_scripted_llm_rejects_unknown_inputs() -> None:
    from kawasemi.ai_input.runbook import RUNBOOK_SYSTEM_PROMPT

    llm = ScriptedLLM({subj.reg_no: subj.name for subj in SUBJECTS}, vision_transcripts={})
    with pytest.raises(ScriptMiss, match="system プロンプト"):
        await llm.chat(
            model="m",
            messages=[{"role": "system", "content": "知らない"}, {"role": "user", "content": "x"}],
        )
    with pytest.raises(ScriptMiss, match="台本に無い項目"):
        await llm.chat(
            model="m",
            messages=[
                {"role": "system", "content": RUNBOOK_SYSTEM_PROMPT},
                {"role": "user", "content": "### linkId: no_such_item\n  項目名: x\n"},
            ],
        )
    with pytest.raises(ScriptMiss, match="画像"):
        await llm.chat(model="m", messages=[{"role": "user", "content": "読め", "images": [b"x"]}])
    with pytest.raises(ScriptMiss, match="タスク"):
        await llm.chat(model="m", messages=[{"role": "user", "content": "何かを答えよ"}])
    # 別の患者の本文が渡ったら値を返さない
    other = SUBJECTS[1]
    with pytest.raises(ScriptMiss, match="本文ではない"):
        await llm.chat(
            model="m",
            messages=[
                {
                    "role": "user",
                    "content": f"被験者 {SUBJECTS[0].reg_no} の感染症の診断名を答えよ。\n=== 本文 ===\n"
                    + sources.admission_summary_text(other),
                }
            ],
        )
    # 台本の節が欠けた項目も ScriptMiss（素の KeyError にしない）
    broken = ScriptedLLM(
        {}, design={"x": {"runbook": [{"title": "t"}]}}, execution=[], vision_transcripts={}
    )
    from kawasemi.ai_input.location_hint import HINT_SYSTEM_PROMPT

    with pytest.raises(ScriptMiss, match="'hint' が無い"):
        await broken.chat(
            model="m",
            messages=[
                {"role": "system", "content": HINT_SYSTEM_PROMPT},
                {"role": "user", "content": "### linkId: x\n"},
            ],
        )
    assert broken.misses and "'hint' が無い" in broken.misses[0]
    with pytest.raises(ScriptMiss, match="llm_judge"):
        await llm.chat(
            model="m", messages=[{"role": "user", "content": "x"}], format={"type": "object"}
        )


def test_cited_clinical_drafts_point_at_the_manual_section(pipeline: PipelineRun) -> None:
    """典拠つきの下書き（2項目）は記入要領のチャンクを指し、そのチャンクは該当の節を含む。"""
    cited = pipeline.notes["clinical_citations"]
    assert sorted(cited) == ["antibiotic_given", "pathogen"], cited
    for lid, c in cited.items():
        assert c["unit_id"].startswith("file:e2e_infection_manual.md"), (lid, c)
        assert c["cites_section"], (lid, c)
