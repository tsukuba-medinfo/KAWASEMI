"""前段が失敗・空のとき LLM op を呼ばない／回答型に合わない値を prefill しない。

E2E 実機層で、`file_extract` がファイルを見つけられずに失敗した後、
同じ guard の `llm_extract` が空の本文で走り、LLM の断り文（「不明（本文を貼って
いただければ…）」）が date 項目の値として prefill・保存された。**取れなかったことが
「取れた値」に化ける fail-open** である。ここでは次を固定する。

- 前段が失敗した／本文が空（空白のみ）なら LLM を呼ばず、step と項目に failure_code が付く
- 本文が取れていれば従来どおり呼ぶ（DB が空 → 本文を読む、のフォールバックを壊さない）
- 回答型（date 等）に合わない値は prefill せず `answer_type_mismatch` を付ける
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.models import FailureCode, InputPlan
from kawasemi.ai_input.resource_index import connections as conn_mod
from kawasemi.api.v100.questionnaire_responses import answer_conforms, normalize_answer

MISMATCH = object()
REFUSAL = "不明\n\n（ご提示いただいた「入院時サマリ:」の後に本文が記載されていないため…）"


def _plan(link_id: str, program: dict[str, Any]) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id=link_id, program=program, status="confirmed")


def _chat(content: str) -> AsyncMock:
    return AsyncMock(return_value=SimpleNamespace(message=SimpleNamespace(content=content)))


def _use_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    registry = conn_mod.ConnectionRegistry(
        {"fs": {"kind": "file", "scope": "external", "root_path": str(root)}}
    )
    monkeypatch.setattr(conn_mod, "get_registry", lambda: registry)
    monkeypatch.setattr("kawasemi.ai_input.executor.get_registry", lambda: registry)


def _text_program(path: str = "reports/{{patient_id}}/admission_summary.txt") -> dict[str, Any]:
    """file_extract で本文を取り、同じ本文から llm_extract で日付を抜く（E2E の onset_date の形）。"""
    return {
        "steps": [
            {
                "op": "file_extract",
                "id": "summary_text",
                "connection_id": "fs",
                "path_pattern": path,
            },
            {
                "op": "llm_extract",
                "id": "onset",
                "prompt": "入院時サマリ: $summary_text\n発症日を YYYY-MM-DD で答えよ",
            },
        ],
        "return": "$onset",
    }


def _date_questionnaire(link_id: str = "onset_date") -> dict[str, Any]:
    return {"resourceType": "Questionnaire", "item": [{"linkId": link_id, "type": "date"}]}


# --------------------------------------------------------------------------- #
# (a)(b)(c) 入力が使えないときは LLM を呼ばない
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_failed_upstream_file_extract_blocks_llm_extract(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) ファイルが見つからず file_extract が失敗したら、llm_extract は呼ばれない。"""
    (tmp_path / "reports" / "P1").mkdir(parents=True)  # 親はある＝not_found_for_patient
    _use_root(monkeypatch, tmp_path)
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat(REFUSAL)

    prefill, evidence = await interp.execute(
        [_plan("onset_date", _text_program())], "P1", db_session
    )

    interp._ollama_client.chat.assert_not_awaited()
    assert "onset_date" not in prefill
    item = evidence["onset_date"]
    assert item["value"] is None
    fx, llm = item["trace"]
    assert fx["failure_code"] == FailureCode.NOT_FOUND_FOR_PATIENT
    # 前段の失敗コードを伝播する。呼んでいないので llm ブロックは無い
    assert llm["failure_code"] == FailureCode.NOT_FOUND_FOR_PATIENT
    assert "llm" not in llm
    assert llm["inputs"] == {"unusable": {"summary_text": FailureCode.NOT_FOUND_FOR_PATIENT}}
    assert item["failure_code"] == FailureCode.NOT_FOUND_FOR_PATIENT


@pytest.mark.asyncio
async def test_schema_mismatch_upstream_is_propagated_under_a_guard(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """フォールバックの第2候補（guard 付き）でパスを誤ると schema_mismatch が項目に残る。

    E2E 実機層で起きた形そのもの: DWH が空 → 同じ guard の file_extract / llm_extract。
    以前は llm_extract が断り文を返し、それが値になって failure_code が付かなかった。
    """
    _use_root(monkeypatch, tmp_path)  # reports/ が無い＝schema_mismatch
    program = _text_program()
    for st in program["steps"]:
        st["guard"] = "$db_empty"
    # 「DB が空だった」を真の判定で代用する（compare の左右が等しい＝真）
    program["steps"].insert(
        0, {"op": "compare", "id": "db_empty", "left": "x", "cmp": "==", "right": "x"}
    )
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat(REFUSAL)

    prefill, evidence = await interp.execute([_plan("onset_date", program)], "P1", db_session)

    interp._ollama_client.chat.assert_not_awaited()
    assert "onset_date" not in prefill
    assert evidence["onset_date"]["trace"][-1]["failure_code"] == FailureCode.SCHEMA_MISMATCH
    assert evidence["onset_date"]["failure_code"] == FailureCode.SCHEMA_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", "  \n\t "])
async def test_empty_text_blocks_llm_extract(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """(b) 本文が空（空白のみ）なら LLM を呼ばない。前段に失敗コードが無いので not_found。"""
    d = tmp_path / "reports" / "P1"
    d.mkdir(parents=True)
    (d / "admission_summary.txt").write_text(body, encoding="utf-8")
    _use_root(monkeypatch, tmp_path)
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat(REFUSAL)

    prefill, evidence = await interp.execute(
        [_plan("onset_date", _text_program())], "P1", db_session
    )

    interp._ollama_client.chat.assert_not_awaited()
    assert "onset_date" not in prefill
    llm = evidence["onset_date"]["trace"][1]
    assert llm["inputs"] == {"unusable": {"summary_text": "empty"}}
    assert evidence["onset_date"]["failure_code"] == FailureCode.NOT_FOUND_FOR_PATIENT


@pytest.mark.asyncio
async def test_normal_text_still_calls_llm(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) 本文が取れていれば従来どおり呼ぶ（フォールバックの正しい形を止めない）。"""
    d = tmp_path / "reports" / "P1"
    d.mkdir(parents=True)
    (d / "admission_summary.txt").write_text("5月31日から発熱。", encoding="utf-8")
    _use_root(monkeypatch, tmp_path)
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat("2026-05-31")

    prefill, evidence = await interp.execute(
        [_plan("onset_date", _text_program())],
        "P1",
        db_session,
        questionnaire_content=_date_questionnaire(),
    )

    interp._ollama_client.chat.assert_awaited_once()
    assert prefill["onset_date"] == "2026-05-31"
    assert "failure_code" not in evidence["onset_date"]


@pytest.mark.asyncio
async def test_prompt_without_step_refs_still_calls_llm(db_session: AsyncSession) -> None:
    """前段を参照しないプロンプト（`{{patient_id}}` だけ）は従来どおり呼ぶ。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat("男性")
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "患者 {{patient_id}} の性別"}],
        "return": "$v",
    }
    prefill, _ = await interp.execute([_plan("sex", program)], "P1", db_session)
    interp._ollama_client.chat.assert_awaited_once()
    assert prefill["sex"] == "男性"


@pytest.mark.asyncio
async def test_one_usable_input_is_enough(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """入力の一部が使えるなら呼ぶ（止めるのは全ての入力が使えないときだけ）。"""
    (tmp_path / "reports" / "P1").mkdir(parents=True)
    _use_root(monkeypatch, tmp_path)
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat('{"answer": "yes"}')
    program = {
        "steps": [
            {
                "op": "file_extract",
                "id": "a",
                "connection_id": "fs",
                "path_pattern": "reports/{{patient_id}}/missing.txt",
            },
            {"op": "transform", "id": "b", "input": "肺炎", "rule": {}},
            {"op": "llm_judge", "id": "j", "prompt": "$a / $b は感染症か"},
        ],
        "return": "$j",
    }
    prefill, _ = await interp.execute([_plan("q", program)], "P1", db_session)
    interp._ollama_client.chat.assert_awaited_once()
    assert prefill["q"] is True


@pytest.mark.asyncio
async def test_llm_judge_on_param_ref_to_empty_step_is_blocked(
    db_session: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宣言パラメータ経由（`params[].value = "$a"`）の参照も入力として見る。判定 op も対象。"""
    d = tmp_path / "reports" / "P1"
    d.mkdir(parents=True)
    (d / "rx.txt").write_text("", encoding="utf-8")
    _use_root(monkeypatch, tmp_path)
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat('{"answer": "no"}')
    program = {
        "params": [{"name": "txt", "value": "$a"}],
        "steps": [
            {
                "op": "file_extract",
                "id": "a",
                "connection_id": "fs",
                "path_pattern": "reports/{{patient_id}}/rx.txt",
            },
            {"op": "llm_judge", "id": "j", "prompt": "{{txt}} に抗菌薬の記載はあるか"},
        ],
        "return": "$j",
    }
    prefill, evidence = await interp.execute([_plan("q", program)], "P1", db_session)
    interp._ollama_client.chat.assert_not_awaited()
    assert "q" not in prefill
    assert evidence["q"]["trace"][1]["inputs"] == {"unusable": {"a": "empty"}}
    assert evidence["q"]["failure_code"] == FailureCode.NOT_FOUND_FOR_PATIENT


# --------------------------------------------------------------------------- #
# (d) 回答型に合わない値は prefill しない
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_refusal_for_a_date_item_is_not_prefilled(db_session: AsyncSession) -> None:
    """(d) 本文があっても LLM が断り文を返したら、date 項目には prefill しない。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat(REFUSAL)
    program = {
        "steps": [
            {"op": "transform", "id": "t", "input": "現病歴: 詳細不明", "rule": {}},
            {"op": "llm_extract", "id": "v", "prompt": "$t から発症日を答えよ"},
        ],
        "return": "$v",
    }
    prefill, evidence = await interp.execute(
        [_plan("onset_date", program)],
        "P1",
        db_session,
        questionnaire_content=_date_questionnaire(),
    )
    interp._ollama_client.chat.assert_awaited_once()
    assert "onset_date" not in prefill
    item = evidence["onset_date"]
    assert item["value"] is None
    assert item["rejected_value"] == REFUSAL
    assert item["failure_code"] == FailureCode.ANSWER_TYPE_MISMATCH


@pytest.mark.asyncio
async def test_stream_also_rejects_type_mismatch(db_session: AsyncSession) -> None:
    """ストリーム実行でも同じ検査を通る（項目イベントの value が None、コード付き）。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat(REFUSAL)
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "患者 {{patient_id}} の発症日"}],
        "return": "$v",
    }
    items = [
        ev
        async for ev in interp.execute_stream(
            [_plan("onset_date", program)],
            "P1",
            db_session,
            questionnaire_content=_date_questionnaire(),
        )
        if ev["type"] == "item"
    ]
    assert items[0]["value"] is None
    assert items[0]["failure_code"] == FailureCode.ANSWER_TYPE_MISMATCH
    assert items[0]["rejected_value"] == REFUSAL


@pytest.mark.asyncio
async def test_no_questionnaire_means_no_type_check(db_session: AsyncSession) -> None:
    """回答型を渡さない呼び出しは従来どおり（検査しない）。"""
    interp = DSLInterpreter()
    interp._ollama_client.chat = _chat(REFUSAL)
    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "患者 {{patient_id}} の発症日"}],
        "return": "$v",
    }
    prefill, _ = await interp.execute([_plan("onset_date", program)], "P1", db_session)
    assert prefill["onset_date"] == REFUSAL


_SEX = {
    "1": {"system": "s", "code": "1", "display": "男"},
    "2": {"system": "s", "code": "2", "display": "女"},
}


@pytest.mark.parametrize(
    ("value", "item_type", "opts", "expected"),
    [
        # date: 区切り・ゼロ埋め・8桁・時刻つきを YYYY-MM-DD へ。部分日付はそのまま
        ("2026-05-31", "date", None, "2026-05-31"),
        ("2025/8/5", "date", None, "2025-08-05"),
        ("2025-8-5", "date", None, "2025-08-05"),
        ("20250805", "date", None, "2025-08-05"),
        ("２０２５/８/５", "date", None, "2025-08-05"),
        ("2026-05-31 10:00:00", "date", None, "2026-05-31"),
        ("2026/5/31 9:05", "date", None, "2026-05-31"),
        ("2026-05-31T10:00:00+09:00", "date", None, "2026-05-31"),
        ("2026", "date", None, "2026"),
        ("2026-05", "date", None, "2026-05"),
        (date(2025, 8, 5), "date", None, "2025-08-05"),
        (datetime(2025, 8, 5, 10, 0), "date", None, "2025-08-05"),
        ("2026-13", "date", None, MISMATCH),
        ("2026-02-30", "date", None, MISMATCH),
        ("20260599", "date", None, MISMATCH),
        ("77777777", "date", None, MISMATCH),
        ("99999999", "date", None, MISMATCH),
        ("令和7年8月5日", "date", None, MISMATCH),
        (REFUSAL, "date", None, MISMATCH),
        # dateTime: ISO 8601 へ
        ("2026-05-31T10:00:00Z", "dateTime", None, "2026-05-31T10:00:00+00:00"),
        ("2026/5/31 9:05", "dateTime", None, "2026-05-31T09:05:00"),
        ("2026-05-31 10:00:00+0900", "dateTime", None, "2026-05-31T10:00:00+09:00"),
        ("2026/5/31", "dateTime", None, "2026-05-31"),
        ("不明", "dateTime", None, MISMATCH),
        # decimal / integer
        ("44.1", "decimal", None, 44.1),
        (38, "decimal", None, 38),
        ("38,5", "decimal", None, MISMATCH),
        ("38.5℃", "decimal", None, MISMATCH),
        (True, "decimal", None, MISMATCH),
        ("nan", "decimal", None, MISMATCH),
        ("7", "integer", None, 7),
        ("7.0", "integer", None, 7),
        (3.0, "integer", None, 3),
        ("7.5", "integer", None, MISMATCH),
        ("三", "integer", None, MISMATCH),
        # boolean
        (True, "boolean", None, True),
        ("false", "boolean", None, False),
        ("0", "boolean", None, False),
        (1, "boolean", None, True),
        ("はい", "boolean", None, True),
        ("いいえ", "boolean", None, False),
        ("yes", "boolean", None, True),
        ("No", "boolean", None, False),
        ("あり", "boolean", None, True),
        ("なし", "boolean", None, False),
        ("不明", "boolean", None, MISMATCH),
        ("on", "boolean", None, MISMATCH),
        # choice: コード、表示名 → コード
        ("1", "choice", _SEX, "1"),
        (2, "choice", _SEX, "2"),
        ("男", "choice", _SEX, "1"),
        (" 女 ", "choice", _SEX, "2"),
        ("男性", "choice", _SEX, MISMATCH),
        (["1", "女"], "choice", _SEX, ["1", "2"]),
        (["1", "x"], "choice", _SEX, MISMATCH),
        ("自由記述", "choice", None, "自由記述"),
        ("自由記述", "open-choice", _SEX, "自由記述"),
        (REFUSAL, "string", None, REFUSAL),
        (REFUSAL, None, None, REFUSAL),
    ],
)
def test_normalize_answer(value: Any, item_type: str | None, opts: Any, expected: Any) -> None:
    ok, norm = normalize_answer(value, item_type, opts)
    if expected is MISMATCH:
        assert ok is False
        assert answer_conforms(value, item_type, opts) is False
    else:
        assert ok is True
        assert norm == expected and type(norm) is type(expected)


def test_date_items_prefill_iso_dates() -> None:
    """日付項目へ入る値（ゼロ埋めしない `2025/8/5` を含む）が ISO で prefill される。

    様式は E2E の架空の感染症様式（`examples/e2e/questionnaire_e2e_infection.json`）を使い、
    回答型は `answer_types_of` で様式から取る。DWH・LLM が返しうる日付の形を
    `item_evidence` に通す。
    """
    import json

    from kawasemi.ai_input.e2e import QUESTIONNAIRE_JSON
    from kawasemi.ai_input.executor import answer_types_of, item_evidence

    types, options = answer_types_of(json.loads(QUESTIONNAIRE_JSON.read_text("utf-8")))
    date_items = sorted(lid for lid, t in types.items() if t == "date")
    assert date_items == ["admission_date", "birth_date", "consent_date", "onset_date"]
    samples = {"2025/8/5": "2025-08-05", "1948/12/1": "1948-12-01", "2025/10/14": "2025-10-14"}
    for lid in date_items:
        for raw, iso in samples.items():
            ev = item_evidence(raw, [], types[lid], options.get(lid))
            assert ev["value"] == iso, (lid, raw, ev)
            assert ev["normalized_from"] == raw
            assert "failure_code" not in ev
    # 同意書の日付は LLM に YYYY-MM-DD で答えさせている。そのまま通る
    ev = item_evidence("2025-03-17", [], types["consent_date"], None)
    assert ev == {"trace": [], "value": "2025-03-17"}
    # コード化 SQL / FHIR の valueString / branch が返すコードは選択肢のコードとして通る
    for lid, code in (("sex", "1"), ("antibiotic_given", "2"), ("severity", "3")):
        assert types[lid] == "choice"
        assert item_evidence(code, [], types[lid], options.get(lid))["value"] == code


# --------------------------------------------------------------------------- #
# 実行の入口（単発・ストリーム）が回答型を executor へ渡していること
# --------------------------------------------------------------------------- #


async def _typed_questionnaire(db: AsyncSession, collector_id: int) -> int:
    from kawasemi.models.case_assignment import CaseAssignment
    from kawasemi.models.questionnaire import Questionnaire

    q = Questionnaire(
        fhir_id="q-463",
        title="回答型の検査",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-463",
            "item": [
                {"linkId": "onset_date", "type": "date"},
                {"linkId": "memo", "type": "string"},
            ],
        },
    )
    db.add(q)
    await db.flush()
    sql = {"steps": [{"op": "sql_extract", "id": "s", "sql": "SELECT '不明' AS v"}], "return": "$s"}
    for lid in ("onset_date", "memo"):
        db.add(
            InputPlan(
                questionnaire_id=q.id, link_id=lid, item_text=lid, program=sql, status="confirmed"
            )
        )
    db.add(CaseAssignment(questionnaire_id=q.id, patient_id="PAT-1", assigned_to_id=collector_id))
    await db.commit()
    return int(q.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["autofill", "autofill/stream"])
async def test_routes_pass_answer_types_to_the_executor(
    collector_client: Any,
    collector_user: Any,
    db_session: AsyncSession,
    csrf_token: str,
    path: str,
) -> None:
    """date 項目の「不明」は prefill されず、string 項目の同じ値は従来どおり prefill される。"""
    import json

    from sqlalchemy import select

    from kawasemi.ai_input.models import InputExecution

    qid = await _typed_questionnaire(db_session, collector_user.id)
    resp = await collector_client.post(
        f"/ai-input/questionnaires/{qid}/{path}",
        json={"patient_id": "PAT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    if path == "autofill":
        resp.json()  # 応答が JSON であること
    else:
        [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    execution = (await db_session.execute(select(InputExecution))).scalar_one()
    assert execution.prefill_data == {"memo": "不明"}
    ev = execution.evidence["onset_date"]
    assert ev["value"] is None
    assert ev["rejected_value"] == "不明"
    assert ev["failure_code"] == FailureCode.ANSWER_TYPE_MISMATCH


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("item_type", "raw", "expected"),
    [("date", "2025/8/5", "2025-08-05"), ("boolean", "false", False), ("integer", "7.0", 7)],
)
async def test_execute_prefills_the_normalized_value(
    db_session: AsyncSession, item_type: str, raw: str, expected: Any
) -> None:
    """prefill するのは正規化した値（`"false"` が文字列のままだとフォームで真に見える）。"""
    interp = DSLInterpreter()
    program = {
        "steps": [{"op": "transform", "id": "t", "input": raw, "rule": {}}],
        "return": "$t",
    }
    qc = {"resourceType": "Questionnaire", "item": [{"linkId": "x", "type": item_type}]}
    prefill, evidence = await interp.execute(
        [_plan("x", program)], "P1", db_session, questionnaire_content=qc
    )
    assert prefill["x"] == expected and type(prefill["x"]) is type(expected)
    assert evidence["x"]["normalized_from"] == raw
    assert "failure_code" not in evidence["x"]
