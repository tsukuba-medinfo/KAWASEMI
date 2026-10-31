"""部分結果が API に別掲されることのテスト。

`chat_json_chunked` は失敗したチャンクを飛ばして次を試す。その結果、
**1回の実行の中に「応答があった項目」「送ったが返らなかった項目」「送っていない項目」が
同時に存在しうる**。ここで測るのは、それがルート層で**別掲される**ことである。

**混ぜてはいけない理由**は経路ごとに違う。所在（段階判定の入力）では「妥当な候補なし」に
混ざると所在の確からしさの段階（rubric）の分布が動く。プランでは「LLM op を含むので保存しなかった」
（手順書を直さないと埋まらない）と混ざる。**押し直せば埋まる項目かどうか**が読めなくなる。

ルート層のハーネス（認証・CSRF・索引の差し替え）は `test_design_provenance_routes` が
持っているものを使う。**同じ結線を2つ持つと片方だけ腐る**ので、ヘルパーは複製しない。
"""

from __future__ import annotations

import json
import pathlib
import re
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import compiler as compiler_mod
from kawasemi.ai_input import location_drafter as ld_mod
from kawasemi.ai_input import resource_drafter as rd_mod
from kawasemi.ai_input import runbook as runbook_mod
from kawasemi.ai_input.models import InputPlan, InputRunbook, PlanStatus, RunbookStatus
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from tests.ai_input.test_design_provenance_routes import (
    _act_as,
    _fake_client,
    _questionnaire_with_items,
    _resp,
    _use_resource_index,
)


def _link_ids(user_message: str) -> list[str]:
    return re.findall(r"### linkId: (\S+)", user_message)


def _failing_chunk(chunk_number: int, ok: Any) -> Any:
    """`chunk_number` 番目（1始まり）のチャンクだけ空応答にする応答関数を作る。

    空応答は `chat_json` が失敗にする経路のうち**最も起きやすいもの**である（実測。
    thinking モデルの思考トークンが `num_ctx` を食うと content が空になりうる）。
    """
    seen = 0

    def response_fn(user_message: str) -> Any:
        nonlocal seen
        seen += 1
        if seen == chunk_number:
            return _resp("")
        return ok(user_message)

    return response_fn


def _choices(user_message: str) -> Any:
    payload = {
        "choices": [
            {"link_id": lid, "chosen_cand_id": 0, "source_category": "local_db"}
            for lid in _link_ids(user_message)
        ]
    }
    return _resp(json.dumps(payload, ensure_ascii=False))


def _choices_with_duplicate(duplicated_link_id: str) -> Any:
    """`duplicated_link_id` にだけ2エントリを返す応答関数（実測で起きた形）。

    実際の生成で起きた形は「1エントリ目が有効・2エントリ目が両方 null」で、最初のエントリを
    採るので**下書きは入る**。矛盾していた事実は別に出さないと消える。
    """

    def response_fn(user_message: str) -> Any:
        choices: list[dict[str, Any]] = []
        for lid in _link_ids(user_message):
            choices.append({"link_id": lid, "chosen_cand_id": 0, "source_category": "local_db"})
            if lid == duplicated_link_id:
                # 2エントリ目は「該当候補なし」（`chosen_cand_id=null`）。実測形と同じく、
                # 後勝ちにすると有効な下書きが消える組み合わせにする
                choices.append(
                    {"link_id": lid, "chosen_cand_id": None, "source_category": "manual"}
                )
        return _resp(json.dumps({"choices": choices}, ensure_ascii=False))

    return response_fn


def _runbooks(user_message: str) -> Any:
    payload = {
        "runbooks": [
            {"link_id": lid, "steps": [{"order": 1, "title": "参照", "description": "台帳"}]}
            for lid in _link_ids(user_message)
        ]
    }
    return _resp(json.dumps(payload, ensure_ascii=False))


def _plans(user_message: str) -> Any:
    payload = {
        "plans": [
            {
                "link_id": lid,
                "program": {
                    "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}],
                    "return": "$s1",
                },
            }
            for lid in _link_ids(user_message)
        ]
    }
    return _resp(json.dumps(payload, ensure_ascii=False))


_ITEMS = [(f"item{i}", f"項目{i}") for i in range(12)]


async def _questionnaire_with_confirmed_runbooks(
    db: AsyncSession, *, fhir_id: str
) -> Questionnaire:
    """12項目すべてに確認済み手順書がある票（プラン作成の前段ゲートを通す）。"""
    q = await _questionnaire_with_items(db, fhir_id=fhir_id, items=_ITEMS, annotate=True)
    for link_id, text in _ITEMS:
        db.add(
            InputRunbook(
                questionnaire_id=q.id,
                link_id=link_id,
                item_text=text,
                steps=[{"order": 1, "title": "参照", "description": "台帳を見る"}],
                status=RunbookStatus.CONFIRMED,
            )
        )
    await db.commit()
    return q


# --------------------------------------------------------------------------- #
# データ所在（所在の確からしさの判定の入力）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_location_route_reports_unattempted_items_separately(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """**所在AI案は部分結果を成功として返し、未試行を別掲する**。

    12項目・既定幅4 で3チャンク、2番目だけ空応答。以前は失敗として 500 を返し、
    **1・3番目のチャンクの一推しも捨てていた**（完了済みチャンクを捨てない修正と同じ算術が当てはまる経路）。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_resource_index(monkeypatch, tmp_path, searchable=True)
    q = await _questionnaire_with_items(db_session, fhir_id="q-partial-system", items=_ITEMS)
    _fake_client(rd_mod, monkeypatch, _failing_chunk(2, _choices))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/ai-draft",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True, "部分結果を失敗として返している"
    # **未試行が別掲される**（件数と一覧の両方。件数だけでは押し直す対象が分からない）
    assert body["unattempted"] == 4
    assert body["unattempted_link_ids"] == ["item4", "item5", "item6", "item7"]
    # **候補なしと混ざっていない**（探索は全項目で成功している）
    assert body["no_hit"] == 0
    assert body["filled"] == 12
    # **原因を1つだけ名指ししない**（打ち切り・チャンクの失敗・応答漏れのどれかである）
    assert "制限時間" not in (body["message"] or "")
    assert "未試行" in body["message"]
    # 一推しは応答のあったチャンクの分だけ付いている
    recommended = {
        d["link_id"] for d in body["drafts"] if any(c["recommended"] for c in d["candidates"])
    }
    assert recommended == {f"item{i}" for i in range(12)} - {f"item{i}" for i in range(4, 8)}


@pytest.mark.asyncio
async def test_the_location_route_reports_no_unattempted_on_the_normal_path(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """**正常系では未試行が 0 で `message` が出ない**こと。

    三状態の別掲は「正常系検出器」になりやすい（何かあれば必ず出る形にすると、脅威の側を
    測っていなくても緑になる）。正常系そのもので固定する。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_resource_index(monkeypatch, tmp_path, searchable=True)
    q = await _questionnaire_with_items(db_session, fhir_id="q-partial-system-ok", items=_ITEMS)
    calls = _fake_client(rd_mod, monkeypatch, _choices)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/ai-draft",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert len(calls) == 3, "前提: チャンクが3つできている"
    assert body["unattempted"] == 0
    assert body["unattempted_link_ids"] == []
    assert body["duplicated"] == 0
    assert body["message"] is None
    assert body["llm_model"], "呼んだモデル名が出ていない"


@pytest.mark.asyncio
async def test_the_location_unattempted_ids_follow_the_item_order_not_the_dictionary_order(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """**未試行の並びは対象項目の順**（4経路で揃える）。

    既存のテストは失敗させるチャンクが2番目で、期待値 `["item4"..."item7"]` が**すでに
    辞書順**だった——`list(...)` を `sorted(...)` へ変える変異が素通りする（実測）。
    **辞書順と項目順が食い違う組**（`item8, item9, item10, item11` は辞書順だと
    `item10, item11, item8, item9`）で測る。臨床側には同型のテストがある。

    3番目（最後）のチャンクを落とすので、**位置の変異の鏡像**（先頭だけ／末尾だけ）も
    既存テストと合わせて両端が当たる。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_resource_index(monkeypatch, tmp_path, searchable=True)
    q = await _questionnaire_with_items(db_session, fhir_id="q-partial-system-order", items=_ITEMS)
    _fake_client(rd_mod, monkeypatch, _failing_chunk(3, _choices))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/ai-draft",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    unattempted = body["unattempted_link_ids"]
    # 前提: この組は辞書順と項目順が食い違う（食い違わないと `sorted()` を区別できない）
    assert unattempted != sorted(unattempted), "前提が崩れている（辞書順と項目順が同じ）"
    assert unattempted == ["item8", "item9", "item10", "item11"]
    assert body["unattempted"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("searchable", [True, False], ids=["索引経路", "BM25 経路"])
async def test_the_location_route_reports_duplicated_responses_apart(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    searchable: bool,
) -> None:
    """**矛盾した応答（同じ項目に複数のエントリ）を所在ルートも別掲する**。

    所在候補の側も矛盾した応答が API に別掲されると説明していたが、**drafter → ルート → API の最後の1ホップが無検証**だった
    （実測: `duplicated = list(result.duplicated_link_ids)` を `[]` へ変える変異が
    `tests/` 全体 1839 件を素通りした）。臨床側には鏡像のテストがあり、**同じ直しを2箇所に
    入れて片方だけ測っていた**形である。

    最初のエントリを採るので**下書きは入る**。だからこそ、矛盾していたことを別に出さないと
    利用者は「矛盾した応答は無かった」と読む——取り逃がしが「無かった」に化ける。

    既存の正常系テスト（`..._reports_no_unattempted_on_the_normal_path` の
    `duplicated == 0`）は**結線が死んでいても通る**ので、脅威の側をここで測る。

    **索引経路と BM25 経路の両方**に当てる（`LocationDraftResult` は共通だが、ルートが
    どちらの結果を受けても別掲することを固定する）。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_resource_index(monkeypatch, tmp_path, searchable=searchable)
    # **両経路で候補が出る項目名を使う**（BM25 経路は語が DB 定義に当たらないと候補が0件になり、
    # LLM を1回も呼ばない＝矛盾も起こせない）
    q = await _questionnaire_with_items(
        db_session,
        fhir_id=f"q-partial-system-dup-{searchable}",
        items=[("item1", "芽球割合"), ("item2", "芽球割合")],
    )
    # BM25 経路（索引が使えない環境）は別モジュールが LLM を呼ぶ。**ルートがどちらの
    # 結果を受けても別掲する**ことを固定するので、両方を当てる
    calls = _fake_client(
        rd_mod if searchable else ld_mod, monkeypatch, _choices_with_duplicate("item1")
    )

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/ai-draft",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert calls, "前提: LLM を呼んでいる（呼ばなければ矛盾も起きない）"
    assert body["duplicated"] == 1
    assert body["duplicated_link_ids"] == ["item1"]
    # **未試行とは別の枠**（混ぜると原因が追えない）。矛盾した応答は送って返ってきている
    assert body["unattempted"] == 0
    assert body["unattempted_link_ids"] == []
    # **採用したことと、矛盾していたことは別**（最初のエントリを採るので下書きは入る）
    recommended = {
        d["link_id"] for d in body["drafts"] if any(c["recommended"] for c in d["candidates"])
    }
    assert "item1" in recommended, "前提: 矛盾していても最初のエントリで下書きは入る"


@pytest.mark.asyncio
async def test_the_location_route_hides_the_model_name_when_the_llm_was_never_called(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """**LLM を1回も呼んでいない実行でモデル名を返さない**こと。

    ルートは以前 `model or settings.ollama_model` を自分で組み立てていたので、**候補が0件で
    LLM を呼んでいなくてもモデル名を返していた**（使っていないモデルを「使った」と開示する）。
    ドラフタ側のテストだけでは**ルートが自分で組み立てる形へ戻す退行が素通りする**
    （実測: `used_model = model or settings.ollama_model` へ戻す変異が生き残った）。

    項目名も臨床的コンテキストも無いとクエリが空になり、探索そのものを実行しない。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_resource_index(monkeypatch, tmp_path, searchable=True)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-partial-system-nohit", items=[("item0", "")]
    )
    calls = _fake_client(rd_mod, monkeypatch, _choices)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/ai-draft",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert calls == [], "前提: 候補0件なので LLM は呼ばれない"
    assert body["no_hit"] == 1
    assert body["llm_model"] is None, "呼んでいないモデル名を開示している"
    assert body["unattempted"] == 0, "候補なしは未試行ではない"


# --------------------------------------------------------------------------- #
# 手順書
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_runbook_route_reports_unattempted_items_separately(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**手順書の一括生成も部分結果を保存し、未試行を別掲する**。

    12項目・幅3 で4チャンク、2番目だけ空応答 → 9件保存・3件未試行。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-partial-runbook", items=_ITEMS, annotate=True
    )
    _fake_client(runbook_mod, monkeypatch, _failing_chunk(2, _runbooks))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/generate",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["generated"] == 9, "応答のあったチャンクの手順書が保存されていない"
    assert body["unattempted"] == 3
    assert body["unattempted_link_ids"] == ["item3", "item4", "item5"]
    # **再実行の案内が対象集合と一致していること**。この経路の対象は
    # 「設計済み かつ（未生成 **または draft**）」なので、押し直すと**保存済みの draft も
    # 再生成される**。「未生成の項目だけ」と書くと、人の手直しが消えることが読めない。
    assert "draft" in body["message"], body["message"]
    assert "確認済みは対象外" in body["message"]
    # 保存されたのは応答があった項目だけ（未試行の項目に空の手順書を作らない）
    rows = list(
        (
            await db_session.execute(
                select(InputRunbook).where(InputRunbook.questionnaire_id == q.id)
            )
        )
        .scalars()
        .all()
    )
    assert {r.link_id for r in rows} == {f"item{i}" for i in range(12)} - {
        "item3",
        "item4",
        "item5",
    }


@pytest.mark.asyncio
async def test_the_runbook_route_reports_no_unattempted_on_the_normal_path(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**正常系では未試行 0・`message` が None**（手順書）。

    3エンドポイントのうち所在だけがこれを測っていたので、`message` を無条件に出す変異が
    手順書とプランでは素通りした（全12項目が成功しても「12項目は手順書が返りませんでした」と
    出す実装が緑になる）。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-partial-runbook-ok", items=_ITEMS, annotate=True
    )
    calls = _fake_client(runbook_mod, monkeypatch, _runbooks)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/generate",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert len(calls) == 4, "前提: チャンクが4つできている"
    assert body["generated"] == 12
    assert body["unattempted"] == 0
    assert body["unattempted_link_ids"] == []
    assert body["message"] is None


@pytest.mark.asyncio
async def test_regenerating_one_runbook_does_not_overwrite_it_with_an_empty_answer(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**応答にこの項目が含まれなかったら、既存の手順書を空で上書きしない**。

    以前は `g is None` のときに `steps = []` を保存していたので、**人が直した手順書が
    応答漏れ1回で消えた**。未試行を「結果」として記録する型そのものである。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-partial-regen", items=_ITEMS[:1], annotate=True
    )
    kept = [{"order": 1, "title": "人が書いた起点", "description": "台帳を見る"}]
    db_session.add(
        InputRunbook(
            questionnaire_id=q.id,
            link_id="item0",
            item_text="項目0",
            steps=kept,
            status=RunbookStatus.CONFIRMED,
        )
    )
    await db_session.commit()
    # **別の link_id を返す**（応答漏れ。空応答だと素の失敗になるので、この形で測る）
    _fake_client(
        runbook_mod,
        monkeypatch,
        lambda _msg: _resp(
            json.dumps(
                {
                    "runbooks": [
                        {
                            "link_id": "別の項目",
                            "steps": [{"order": 1, "title": "x", "description": "y"}],
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ),
    )

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item0/regenerate",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 502, resp.text
    assert resp.json()["success"] is False
    assert "未試行" in resp.json()["message"]
    row = (
        (
            await db_session.execute(
                select(InputRunbook).where(InputRunbook.questionnaire_id == q.id)
            )
        )
        .scalars()
        .one()
    )
    await db_session.refresh(row)
    assert row.steps == kept, "既存の手順書が空で上書きされている"
    assert row.status == RunbookStatus.CONFIRMED, "確認済みが draft に落とされている"


# --------------------------------------------------------------------------- #
# 取得プラン
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_compile_route_separates_unattempted_from_rejected(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**未試行と「LLM op を含むので保存しなかった」を別に数える**。

    前者は押し直せば埋まり、後者は手順書を直さないと埋まらない。同じ数に混ぜると、
    操作者はどちらなのか判断できない。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-partial-plan")
    _fake_client(compiler_mod, monkeypatch, _failing_chunk(2, _plans))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    # コンパイラの刻み幅は 2（`COMPILE_CHUNK_SIZE`。12項目なら6チャンク）
    assert body["compiled"] == 10, "応答のあったチャンクのプランが保存されていない"
    assert body["unattempted"] == 2
    assert body["unattempted_link_ids"] == ["item2", "item3"]
    assert body["rejected_llm"] == [], "未試行が LLM op 拒否に混ざっている"
    assert "未試行" in body["message"]
    # **再実行の案内が対象集合と一致していること**
    assert "draft のプラン" in body["message"], body["message"]


@pytest.mark.asyncio
async def test_the_compile_route_reports_no_unattempted_on_the_normal_path(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**正常系では未試行 0・`message` が None**（プラン）。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-partial-plan-ok")
    calls = _fake_client(compiler_mod, monkeypatch, _plans)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert len(calls) == 6, "前提: 12項目・幅2 で6チャンクできている"
    assert body["compiled"] == 12
    assert body["unattempted"] == 0
    assert body["unattempted_link_ids"] == []
    assert body["rejected_llm"] == []
    assert body["validation_errors"] == {}
    assert body["message"] is None


@pytest.mark.asyncio
async def test_the_compile_route_reports_validation_errors_but_still_saves_draft(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**生成直後に検証結果を見せるが、エラーがあっても draft の保存は続ける**。

    以前は `compile_plans` が `validate_program` を一切通さず draft 保存していたため、
    検証エラーが確定操作の時点まで見えなかった。修正の起点として draft は残しつつ、
    どの項目が検証エラーを持つかをレスポンスから読めることを固定する。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-partial-plan-invalid")

    def response_fn(user_message: str) -> Any:
        payload = {
            "plans": [
                {
                    "link_id": lid,
                    "program": (
                        # sql_extract に必須の "sql" を欠いた不正な program（validate_program
                        # が拾う）。LLM が structured output のスキーマ上は妥当だが構造的に
                        # 不完全な出力を返す形を模す。
                        {"steps": [{"op": "sql_extract", "id": "s1"}], "return": "$s1"}
                        if lid == "item0"
                        else {
                            "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}],
                            "return": "$s1",
                        }
                    ),
                }
                for lid in _link_ids(user_message)
            ]
        }
        return _resp(json.dumps(payload, ensure_ascii=False))

    _fake_client(compiler_mod, monkeypatch, response_fn)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["compiled"] == 12, "検証エラーがあっても draft の保存自体は続ける"
    assert list(body["validation_errors"].keys()) == ["item0"]
    assert body["validation_errors"]["item0"], "エラー内容が空でないこと"
    assert "検証エラー" in body["message"]

    from kawasemi.ai_input.models import InputPlan, PlanStatus

    row = (
        await db_session.execute(
            select(InputPlan).where(
                InputPlan.questionnaire_id == q.id, InputPlan.link_id == "item0"
            )
        )
    ).scalar_one()
    assert row.status == PlanStatus.DRAFT, "検証エラーがあっても draft として保存されている"


@pytest.mark.asyncio
async def test_the_compile_route_keeps_unattempted_and_rejected_apart_when_both_happen(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**未試行と「LLM op を含むので保存しなかった」が両方非空のとき**に分かれること。

    従来のフィクスチャは `rejected_llm == []` だったので、**rejected を未試行へ混ぜる側の
    退行が測れなかった**（`fixture-must-enable-the-competition` の型）。あるチャンクを空応答に
    し、別のチャンクに `llm_extract` を含むプランを返させて**競合を成立させる**。

    `ENABLE_AI_EXTRACTION` は既定 False なので、LLM op を含むプランは保存されない
    （二段ガードの1段目）。押し直しても埋まらないので、未試行とは別に数える必要がある。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-partial-plan-both")

    seen = 0

    def response_fn(user_message: str) -> Any:
        nonlocal seen
        seen += 1
        if seen == 2:
            return _resp("")  # 2チャンク目は空応答（未試行になる）
        if seen == 3:
            # 3チャンク目は LLM op を含むプラン（生成はできたが保存しない）
            # 刻み幅は 2 なので、2チャンク目＝item2/item3、3チャンク目＝item4/item5
            payload = {
                "plans": [
                    {
                        "link_id": lid,
                        "program": {
                            "steps": [
                                {
                                    "op": "llm_extract",
                                    "id": "s1",
                                    "prompt": "抽出してください",
                                    "input": "$s0",
                                }
                            ],
                            "return": "$s1",
                        },
                    }
                    for lid in _link_ids(user_message)
                ]
            }
            return _resp(json.dumps(payload, ensure_ascii=False))
        return _plans(user_message)

    _fake_client(compiler_mod, monkeypatch, response_fn)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # **どちらも非空**（競合が成立している。これが無いと片方向の混同しか測れない）
    assert body["unattempted_link_ids"] == ["item2", "item3"]
    assert body["rejected_llm"] == ["item4", "item5"]
    assert body["compiled"] == 8
    assert body["unattempted"] == 2
    # メッセージは両方を別に述べる
    assert "llm_extract" in body["message"] and "未試行" in body["message"]


@pytest.mark.asyncio
async def test_recompiling_one_plan_does_not_overwrite_it_with_an_empty_program(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**応答にこの項目が含まれなかったら、空のプログラムを保存しない**。

    以前は `{"steps": [], "return": None}` を draft 保存しており、**確認済みプランを空で
    上書きしていた**。生成結果ではなく応答漏れなのに、結果として記録される形である。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-partial-replan")
    _fake_client(
        compiler_mod,
        monkeypatch,
        lambda _msg: _resp(
            json.dumps(
                {"plans": [{"link_id": "別の項目", "program": {"steps": [], "return": None}}]},
                ensure_ascii=False,
            )
        ),
    )

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item0/recompile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 502, resp.text
    assert "未試行" in resp.json()["message"]
    # プランは1件も作られていない（空の draft を残さない）
    from kawasemi.ai_input.models import InputPlan

    rows = list(
        (await db_session.execute(select(InputPlan).where(InputPlan.questionnaire_id == q.id)))
        .scalars()
        .all()
    )
    assert rows == [], "空のプログラムが draft 保存されている"


@pytest.mark.asyncio
async def test_recompiling_one_plan_reports_validation_errors_but_still_saves_draft(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**単一項目の再作成でも生成直後に検証結果を見せる**。

    `compile_plans` と同じ理由: 手動保存の `save_plan` は `validate_program` を通すのに
    生成側（`recompile_plan`）は通さず、検証エラーが確定操作の時点まで見えなかった。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-partial-replan-invalid")
    _fake_client(
        compiler_mod,
        monkeypatch,
        lambda _msg: _resp(
            json.dumps(
                {
                    "plans": [
                        {
                            "link_id": "item0",
                            # sql_extract に必須の "sql" を欠いた不正な program。
                            "program": {
                                "steps": [{"op": "sql_extract", "id": "s1"}],
                                "return": "$s1",
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            )
        ),
    )

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item0/recompile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["errors"], "検証エラーがレスポンスに乗っていない"

    from kawasemi.ai_input.models import InputPlan, PlanStatus

    row = (
        await db_session.execute(
            select(InputPlan).where(
                InputPlan.questionnaire_id == q.id, InputPlan.link_id == "item0"
            )
        )
    ).scalar_one()
    assert row.status == PlanStatus.DRAFT, "検証エラーがあっても draft として保存されている"


# --------------------------------------------------------------------------- #
# 画面
#
# JS のテストランナーが無いのでレンダリング結果への検査で代替する（臨床側の
# `test_clinical_page_shows_unattempted_apart_from_no_citation` と同じ方式）。「その文字列が
# ページのどこかに在る」だけを見ると同じ文言を持つ別の箇所に助けられるので、**関数単位で
# 切り出して**固定する。三状態の判定を Python 側へ寄せるのは別の課題である。
# --------------------------------------------------------------------------- #


def _js_function(body: str, name: str) -> str:
    """レンダリング結果から関数 `name` の本体を切り出す（列0で始まり列0の `}` で閉じる）。"""
    m = re.search(rf"^(?:async )?function {re.escape(name)}\(.*?^\}}$", body, re.M | re.S)
    assert m, f"{name} が見つからない"
    return m.group(0)


@pytest.mark.asyncio
async def test_the_plan_page_shows_param_binding_source_and_allowed(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """確定UI（plan.html）が program.params の束縛元と allowed を表示すること。

    確定UIが program.params を表示しない不具合の修正が、テストで
    固定されていなかった（plan.html を参照するテストはリポジトリ全体で0件だった）。
    「宣言が画面に出ないと確認者は値の出所を見ずに確定する」という論拠を固定する。
    JS のテストランナーが無いので、レンダリング結果から関数本体を切り出して文字列で
    検査する（画面の JS を静的ファイルへ切り出した際の制約は構文検査の話で、ここは既存の分岐を
    読む検査なので該当しない）。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-plan-params-summary")

    resp = await designer_client.get(f"/ai-input/questionnaires/{q.id}/plan")
    assert resp.status_code == 200, resp.text
    fn = _js_function(resp.text, "paramsSummaryHtml")

    # 束縛元: "$id" 参照（前段ステップの取得結果）とリテラルを区別して表示する
    # （枝の中身を入れ替える変異を殺すため、三項演算子の両枝を別々に見る）
    src_marker = "p.value.startsWith('$')"
    assert src_marker in fn
    q_idx = fn.index("?", fn.index(src_marker))
    colon_idx = fn.index(":", q_idx)
    semi_idx = fn.index(";", colon_idx)
    dollar_branch, literal_branch = fn[q_idx:colon_idx], fn[colon_idx:semi_idx]
    assert "前段ステップ" in dollar_branch and "の取得結果" in dollar_branch
    assert "リテラル" not in dollar_branch
    assert "リテラル" in literal_branch and "前段ステップ" not in literal_branch

    # allowed: 宣言があれば列挙し、kind=identifier で未宣言なら警告を出す
    # （外側の三項の真branchの文字列自身が "allowed: " というコロンを含むので、
    # コロン探索ではなく次のブロック（kind === 'identifier'）を境界に使う）
    allowed_marker = "Array.isArray(p.allowed) && p.allowed.length"
    identifier_marker = "kind === 'identifier'"
    assert allowed_marker in fn and identifier_marker in fn
    q2 = fn.index("?", fn.index(allowed_marker))
    kind_idx = fn.index(identifier_marker)
    has_allowed_branch = fn[q2:kind_idx]
    assert "allowed:" in has_allowed_branch and "params-warn" not in has_allowed_branch

    q3 = fn.index("?", kind_idx)
    colon3 = fn.index(":", q3)
    close_paren = fn.index(")", colon3)
    identifier_warn_branch, non_identifier_branch = fn[q3:colon3], fn[colon3:close_paren]
    assert "allowed 未宣言" in identifier_warn_branch and "params-warn" in identifier_warn_branch
    assert "allowed なし" in non_identifier_branch and "params-warn" not in non_identifier_branch


@pytest.mark.asyncio
async def test_the_location_page_shows_unattempted_apart_from_unresolved(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所在画面が**未試行を「未解決」と混ぜない**こと。

    **所在の確からしさの段階（rubric）を付けるのは人であり、その人が読むのはこの画面である。**
    API の内訳だけ正しくしても、ここで混ざれば段階の分布が汚染される。しかもこの経路は
    従来 500 だったので、**部分結果を 200 で返すようにした結果「緑の成功」に化ける**退行を
    作りうる（臨床側で先に見つかった型）。

    **書き込み → 判定 → 表示の3つの結線を条件式そのもので固定する**。
    最初の版は「その文字列が関数の中にあるか」しか見ていなかったので、**書き込みの削除・
    引数の削除・カウンタ更新の削除・分岐の条件を `false &&` にする**の7通りが全部素通りした
    （臨床側は条件式そのものを assert しているので同じ変異で落ちる）。

    **限界（実測して書く）**: 条件式全体を pin しているので `false &&` の挿入は
    落ちる（臨床側は断片を pin しているので通る——「臨床側と同じ」は誤りだった）。
    **この検査で届かないのは次の形である**。(a) `hidden` やクラスの切り替えは実行時の挙動なので
    レンダリング結果には出ない。(b) **pin していない行の追加・削除**（出現回数を数えている
    箇所だけは後置の上書きも落ちる）。(c) 関数の外に同名の変数を作る形。
    **三状態の判定を API 側へ寄せる**のが構造的な直しで、まだ入っていない。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_items(db_session, fhir_id="q-partial-page", items=_ITEMS)

    resp = await designer_client.get(f"/ai-input/questionnaires/{q.id}/source-survey/system")
    # **画面の JS は静的ファイルへ切り出した**。ブラウザが受け取るのは
    # ページ本文とこのファイルの合計なので、検査の対象も合計にする。関数の切り出し
    # （`_js_function`）は切り出し先の整形に依存するが、列0で始まり列0の `}` で閉じる
    # 形は変わっていない。
    static_js = (
        pathlib.Path(__file__).resolve().parents[2]
        / "kawasemi"
        / "web"
        / "static"
        / "js"
        / "design_system.js"
    ).read_text(encoding="utf-8")
    body = resp.text + static_js

    # **未試行のカウンタが「未解決」と別にある**（強調の CSS も別に持つ）
    assert 'id="sb-unattempted"' in body and 'id="sb-unresolved"' in body

    # **説明文（画面の hint）も pin する**（この1文だけが無検証だった）。
    # 数字を正しく出しても、読み手が「AI では所在が特定できなかった」と読めば同じ誤りになる
    assert "「未試行」はAIの一推しが返っていない項目" in body
    assert "「AIでは所在が特定できなかった」ではありません" in body

    # (1) 書き込み: 未試行集合を項目の状態へ入れる**条件式そのもの**
    apply_fn = _js_function(body, "applyDrafts")
    assert "s.unattempted = !!(unattemptedSet && unattemptedSet.has(d.link_id));" in apply_fn, (
        "未試行の書き込みが無い（削除・引数落ちを検出できない）"
    )
    # **代入は1箇所**（後置で `s.unattempted = false;` を足す形を殺す）
    assert apply_fn.count("s.unattempted =") == 1, "未試行の代入が1箇所でない（後置の上書き）"
    # (2) 判定: 未試行を「未解決」と別の分類にする条件式
    classify = _js_function(body, "classify")
    assert "if (s.selected === null && s.unattempted) return 'unattempted';" in classify
    assert "if (s.selected === null) return 'unresolved';" in classify
    # **順序**（未試行の枝が未解決より後にあると到達しない。この assert を一度落として
    # しまい、順序を入れ替える変異が素通りした）
    assert classify.index("return 'unattempted'") < classify.index("return 'unresolved'"), (
        "未試行の枝が未解決より後にあると到達しない"
    )
    # (3) 表示: カウンタの更新と強調。**出現回数で絞る**ので、後置の上書き
    # （`set('sb-unattempted', 0);` の追加）も落ちる
    refresh = _js_function(body, "refreshStatusbar")
    assert refresh.count("set('sb-unattempted'") == 1, "カウンタの代入が1箇所でない（後置の上書き）"
    assert "set('sb-unattempted', c.unattempted);" in refresh, "カウンタが更新されない"
    assert refresh.count("toggle('warn', c.unattempted") == 1
    assert "p.classList.toggle('warn', c.unattempted > 0);" in refresh, "強調が付かない"
    # **強調の CSS が実在すること**（JS だけ測っても色が付かない）
    css = (Path(__file__).resolve().parents[2] / "kawasemi/web/static/css/app.css").read_text(
        encoding="utf-8"
    )
    assert ".sb-unattempted.warn" in css, "未試行の強調の CSS が無い"
    # **フラグの文言が2つの状態で違い、枝の本体が入れ替わっていないこと**
    flag = _js_function(body, "updateFlag")
    head = "if (s.selected === null && s.unattempted) {"
    tail = "} else if (s.selected === null) {"
    assert head in flag and tail in flag
    pending_branch = flag[flag.index(head) + len(head) : flag.index(tail)]
    unresolved_branch = flag[flag.index(tail) + len(tail) :]
    assert "未試行（AIの一推しが返っていません）" in pending_branch, pending_branch
    assert "要確認（未選択）" in unresolved_branch, unresolved_branch
    assert "未試行" not in unresolved_branch, "未解決の枝が未試行の文言を出している"

    # 一括: 未試行の一覧を渡し、あるときは緑にしない
    bulk = _js_function(body, "aiDraftAll")
    assert "const unattempted = new Set(data.unattempted_link_ids || []);" in bulk
    assert "applyDrafts(data.drafts, unattempted);" in bulk, "一括で未試行集合を渡していない"
    # **件数を API から読む行そのもの**（`const pending = 0;` へ無害化する形を殺す。
    # 以前はプラン画面だけが同じ行を pin していて、**段階判定の入力である
    # この画面が pin していなかった**。いまは3画面すべてが pin している）
    assert "const pending = data.unattempted || 0;" in bulk, "未試行の件数を API から読んでいない"
    assert "setStatus(msg, !pending);" in bulk, "未試行があっても緑のままになっている"
    # **マークの向き**（`? '✓' : '⚠'` へ反転する形を殺す）
    assert "${pending ? '⚠' : '✓'}" in bulk, "未試行のときのマークが逆になっている"
    # **件数を必ず添える**（この行を削ると人が押し直す対象を読めない）
    assert "${pending}件はAIの一推しが返りませんでした（未試行。もう一度実行してください）" in bulk
    # 項目単位も同じ区別をする（一括だけ測ると、こちらの枝を丸ごと削っても通る）
    single = _js_function(body, "aiDraftItem")
    assert "const unattempted = new Set(data.unattempted_link_ids || []);" in single
    assert "applyDrafts(data.drafts, unattempted);" in single
    # **2つの枝の本体を切り出して各々 assert する**（`updateFlag` と同じ道具。本体を
    # 入れ替える変異が素通りしていた）
    s_head = "if (unattempted.has(linkId)) {"
    s_tail = "} else {"
    assert s_head in single and s_tail in single, "項目単位で未試行を見ていない"
    s_pending = single[single.index(s_head) + len(s_head) : single.index(s_tail)]
    s_else = single[single.index(s_tail) + len(s_tail) :]
    assert "未試行" in s_pending and "false)" in s_pending, s_pending
    assert "候補を提示しました" in s_else, s_else
    assert "未試行" not in s_else, "未試行でない枝が未試行の文言を出している"


@pytest.mark.asyncio
async def test_the_plan_page_does_not_report_unattempted_as_a_green_success(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """プラン画面が**未試行を緑の成功として出さない**こと。

    この経路も 500 → 200 + 部分結果に変えたので、手順書画面で直したのと同じ退行が起きうる。
    既存の `rejected_llm` 用の結線に相乗りしていたので**偶然動いていた**——`!data.message` を
    `true` にする変異も `note` を空にする変異も素通りした（実測）。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-partial-plan-page")

    resp = await designer_client.get(f"/ai-input/questionnaires/{q.id}/plan")
    body = resp.text

    compile_fn = _js_function(body, "compileAll")
    assert "const pending = data.unattempted || 0;" in compile_fn, "未試行の件数を読んでいない"
    assert "const note = data.message ? ` ${data.message}` : '';" in compile_fn
    assert "setStatus(`${mark} ${data.compiled} 件を作成しました。${note} 再読込します...`, " in (
        compile_fn
    )
    # 警告にするのは未試行・LLM op 拒否・検証エラーのいずれかがあるとき（確認済みで
    # 作り直さなかった項目は意図した振る舞いなので、緑のまま注記を出す）
    assert "const warn = pending ||" in compile_fn, "未試行が警告の条件に入っていない"
    assert "!warn);" in compile_fn, "未試行があっても緑のままになっている"
    assert "(data.message || pending) ? 4000 : 800" in compile_fn, "読み手が読む時間が無い"
    # **実際に変わったのはマークである**。ルートは未試行があれば必ず
    # `message` を入れるので、`pending` の項は**到達しうる応答では結果を変えない**——
    # `mark` を `'✓'` 固定にする変異が素通りしていた。`pending` を残すのは、将来 route が
    # `message` を入れなくなっても画面が緑にならないための多重防御である。
    assert "const mark = warn ? '⚠' : '✓';" in compile_fn, (
        "未試行のときのマークが緑の ✓ のままになっている"
    )


@pytest.mark.asyncio
async def test_the_runbook_page_does_not_report_unattempted_as_a_green_success(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手順書画面が**未試行を緑の成功として出さない**こと。

    以前は `data.message` を捨てて 800ms で `location.reload()` していたので、**10分待って
    1件も生成されなかった実行が「✓ 0 件を生成しました」で終わっていた**。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-partial-rb-page", items=_ITEMS, annotate=True
    )

    resp = await designer_client.get(f"/ai-input/questionnaires/{q.id}/runbook")
    body = resp.text

    gen = _js_function(body, "generateAll")
    # **件数を API から読む行そのもの**（`data.unattempted` の部分一致では
    # `const pending = 0;` への無害化は落ちるが、**他2画面と同じ行を pin していなかった**——
    # 「3画面が同じ行を pin している」と書いた主張が片方向だった）
    assert "const pending = data.unattempted || 0;" in gen, "未試行の件数を API から読んでいない"
    assert gen.count("const pending =") == 1, "件数の代入が1箇所でない（後置の上書き）"
    assert "未試行" in gen
    # **緑にしない枝がある**（`setStatus(..., false)` を未試行の枝で使う）
    pending_branch = gen[gen.index("if (pending)") :]
    assert "false)" in pending_branch.split("} else {")[0], "未試行でも緑のままになっている"


# --------------------------------------------------------------------------- #
# 対象に入らなかった項目
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_compile_route_reports_the_items_it_left_out_by_reason(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**対象から外した項目を、理由ごとに別掲する**。

    確認済みの plan は人の確認を尊重して上書きしない／手順書が未確認の項目は対象外。
    どちらも意図した振る舞いだが、応答に出ないと `compiled` を全体の件数と読んで数が
    食い違い、表に残った前回の行を今回の結果として数える（プランの一括生成で3回続けて同じ
    数字が出て、原因を2度誤診した）。**2つの条件を混ぜない**。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-skip-plan")
    db_session.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id="item0",
            item_text="項目0",
            program={"params": [], "steps": [], "return": None},
            status=PlanStatus.CONFIRMED,
        )
    )
    rb1 = (
        await db_session.execute(
            select(InputRunbook).where(
                InputRunbook.questionnaire_id == q.id, InputRunbook.link_id == "item1"
            )
        )
    ).scalar_one()
    rb1.status = RunbookStatus.DRAFT
    # 手順書の行そのものが無い項目（`runbooks` を走るループには現れない）
    rb11 = (
        await db_session.execute(
            select(InputRunbook).where(
                InputRunbook.questionnaire_id == q.id, InputRunbook.link_id == "item11"
            )
        )
    ).scalar_one()
    await db_session.delete(rb11)
    await db_session.commit()
    _fake_client(compiler_mod, monkeypatch, _plans)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["compiled"] == 9
    assert sorted(body["compiled_link_ids"]) == sorted(f"item{i}" for i in range(2, 11))
    assert body["skipped_confirmed_plan"] == ["item0"]
    assert body["skipped_runbook_unconfirmed"] == ["item1"]
    assert body["skipped_no_runbook"] == ["item11"]
    # 表にあって今回書き換えなかった行（古い行）。事後の検算に使える
    assert body["rows_not_rewritten"] == ["item0"]
    assert "確認済みのため作り直していません" in body["message"], body["message"]
    assert "手順書が未確認" in body["message"], body["message"]
    assert "手順書が無い" in body["message"], body["message"]
    # 対象の中の件数に、外した件数を足すと表の項目数になる（数が食い違わない）
    assert (
        body["compiled"]
        + body["unattempted"]
        + len(body["rejected_llm"])
        + len(body["skipped_confirmed_plan"])
        + len(body["skipped_runbook_unconfirmed"])
        + len(body["skipped_no_runbook"])
        == 12
    )


@pytest.mark.asyncio
async def test_the_compile_route_reports_nothing_skipped_on_the_normal_path(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """外した項目が無いときは空のリストで、`message` は None のまま。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-skip-plan-none")
    _fake_client(compiler_mod, monkeypatch, _plans)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert body["skipped_confirmed_plan"] == []
    assert body["skipped_runbook_unconfirmed"] == []
    assert body["skipped_no_runbook"] == []
    assert body["rows_not_rewritten"] == []
    assert body["message"] is None


@pytest.mark.asyncio
async def test_the_compile_route_reports_the_skipped_items_even_when_nothing_is_left(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """対象が1件も無くて 400 を返すときも、なぜ空なのかを理由別に出す。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-skip-plan-all")
    for link_id, text in _ITEMS:
        db_session.add(
            InputPlan(
                questionnaire_id=q.id,
                link_id=link_id,
                item_text=text,
                program={"params": [], "steps": [], "return": None},
                status=PlanStatus.CONFIRMED,
            )
        )
    await db_session.commit()

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert resp.status_code == 400
    assert len(body["skipped_confirmed_plan"]) == 12
    assert body["skipped_runbook_unconfirmed"] == []


@pytest.mark.asyncio
async def test_the_runbook_route_reports_the_items_it_left_out_by_reason(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """手順書の一括生成も、確認済みで外した項目を応答に出す。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-skip-runbook", items=_ITEMS, annotate=True
    )
    db_session.add(
        InputRunbook(
            questionnaire_id=q.id,
            link_id="item0",
            item_text="項目0",
            steps=[{"order": 1, "title": "参照", "description": "台帳を見る"}],
            status=RunbookStatus.CONFIRMED,
        )
    )
    await db_session.commit()
    _fake_client(runbook_mod, monkeypatch, _runbooks)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/generate",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["generated"] == 11
    assert "item0" not in body["generated_link_ids"]
    assert body["skipped_confirmed"] == ["item0"]
    assert body["skipped_not_designed"] == []
    assert body["rows_not_rewritten"] == ["item0"]
    assert "確認済みのため作り直していません" in body["message"], body["message"]
    assert body["generated"] + len(body["skipped_confirmed"]) == 12


def _plans_with_empty_programs(empty_ids: set[str]) -> Any:
    """`empty_ids` の項目だけ、step が空のプログラムを返すモデルの応答。"""

    def responder(user_message: str) -> Any:
        plans = []
        for lid in _link_ids(user_message):
            program = (
                {"steps": [], "return": None}
                if lid in empty_ids
                else {
                    "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}],
                    "return": "$s1",
                }
            )
            plans.append({"link_id": lid, "program": program})
        return _resp(json.dumps({"plans": plans}, ensure_ascii=False))

    return responder


@pytest.mark.asyncio
async def test_the_compile_route_counts_empty_programs_apart(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """step が1つも無いプログラムを、取得の設計とは別に数えて返す。

    `validate_program` は空の `steps` を通す（手入力への縮退という正当な形でもある）ので、
    検証エラーには出ない。**保存はするが、「確定できる」に混ぜて数えない**ために別掲する。
    以前は空のプログラムも「確定できる」に数えられていた。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-empty-program")
    _fake_client(compiler_mod, monkeypatch, _plans_with_empty_programs({"item3", "item7"}))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["compiled"] == 12, "空のプログラムも保存はされる"
    assert body["empty_program_link_ids"] == ["item3", "item7"]
    assert body["validation_errors"] == {}, "空は検証エラーではない"
    assert "step が空のプログラム" in body["message"], body["message"]


@pytest.mark.asyncio
async def test_the_compile_route_reports_no_empty_programs_on_the_normal_path(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id="q-empty-program-none")
    _fake_client(compiler_mod, monkeypatch, _plans)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert body["empty_program_link_ids"] == []
    assert body["message"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("program", "warned"),
    [
        ({"params": [], "steps": [], "return": None}, True),
        (
            {"steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}], "return": "$s1"},
            False,
        ),
    ],
    ids=["empty-program", "program-with-steps"],
)
async def test_confirming_an_empty_program_succeeds_but_warns(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    program: dict[str, Any],
    warned: bool,
) -> None:
    """step が空のプランは確定できる（手入力への縮退という正当な形）が、**警告を返す**。

    止めると manual 縮退のプランを確定できなくなる。かといって黙って確定すると、人が
    気付かずに「取得の設計ができた」かのように母集団へ入る。**止めずに告げる**。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbooks(db_session, fhir_id=f"q-confirm-{warned}")

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item0?confirm=true",
        json={"program": program},
        headers={"X-CSRF-Token": csrf_token},
    )

    body = resp.json()
    assert resp.status_code == 200, resp.text
    assert body["status"] == PlanStatus.CONFIRMED
    assert ("warning" in body) is warned, body
    assert body.get("empty_program", False) is warned
