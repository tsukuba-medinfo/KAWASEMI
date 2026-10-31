"""設計ロールの来歴が**ルート経由で実際に保存される**ことの検証

`test_design_provenance.py` はガードと保存内容をユニットで固定するが、それだけでは
「recorder が4つの設計ロールへ通っていて、ルートが保存する」という本機能の主目的を
一切検証できない。実際にミューテーションを当てると、

- `runbook.py` の `recorder=recorder` を `None` に変える
- `chat_json` の冒頭で recorder を捨てる
- ルートの保存呼び出しをノーオペにする

のいずれも**全テストが緑のまま**だった（＝来歴が1件も保存されなくなる変更を許す）。
さらに、プロンプトから患者データのマーカーを探す旧ガードは `{{patient_id}}` の部分文字列
一致で正常系を 100% 落としていたが、ユニットテストは通っていた。

そこで Ollama をフェイクにしてルートを叩き、**行ができること・role が正しいこと・
失敗時も行が残ること**を固定する。ここが緑であれば、配線が外れる改修とガードの誤発火は
どちらも検出できる。
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from kawasemi_indexer.builder import build_db_index
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import compiler as compiler_mod
from kawasemi.ai_input import location_drafter as ld_mod
from kawasemi.ai_input import resource_drafter as rd_mod
from kawasemi.ai_input import runbook as runbook_mod
from kawasemi.ai_input.db_definition import reset_index_cache
from kawasemi.ai_input.design_provenance import DesignLlmRecorder
from kawasemi.ai_input.models import (
    ClinicalAnnotation,
    DesignLlmCall,
    DesignRole,
    InputRunbook,
    RunbookStatus,
)
from kawasemi.ai_input.resource_index.store import reset_store_cache
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from tests.ai_input.conftest import HashingEmbedder

_DB_FIXTURE_DIR = str(Path(__file__).resolve().parents[1] / "fixtures" / "db_definition")


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


def _resp(content: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(message=types.SimpleNamespace(content=content))


def _is_hint_call(kwargs: Any) -> bool:
    """所在検索ヒントのコールか。

    データ所在の提案は**2種類のコールを打つ**（ヒント→選択）。`format` に載る JSON schema
    のトップレベルのキーで見分ける。ヒントは施策であって提案そのものではないので、
    件数を数えるテストの `calls` には入れない（空の `hints` で答えて縮退させる）。
    """
    return "hints" in (kwargs.get("format") or {}).get("properties", {})


def _empty_hints() -> Any:
    return types.SimpleNamespace(message=types.SimpleNamespace(content=json.dumps({"hints": []})))


def _fake_client(module: Any, monkeypatch: pytest.MonkeyPatch, response_fn: Any) -> list[dict]:
    """モジュールの `make_design_llm_client` をフェイクへ差し替え、呼び出しを記録する。

    ルートの中でクライアントを生成するので、インスタンスではなくファクトリを差し替える。
    **差し替える先はファクトリである**（以前は `module.ollama.AsyncClient` だった）——
    プロバイダの選択がファクトリに閉じたので、テストもそこを掴む。
    """
    calls: list[dict] = []

    async def fake_chat(**kwargs: Any) -> Any:
        if _is_hint_call(kwargs):
            return _empty_hints()
        calls.append(kwargs)
        return response_fn(kwargs["messages"][1]["content"])

    monkeypatch.setattr(
        module,
        "make_design_llm_client",
        lambda: types.SimpleNamespace(chat=fake_chat),
    )
    return calls


def _fake_client_on(obj: Any, response_fn: Any) -> None:
    """生成器インスタンスの `_client` をフェイクへ差し替える。"""

    async def fake_chat(**kwargs: Any) -> Any:
        if _is_hint_call(kwargs):
            return _empty_hints()
        return response_fn(kwargs["messages"][1]["content"])

    obj._client = types.SimpleNamespace(chat=fake_chat)


def _plans_from_message(user_message: str) -> types.SimpleNamespace:
    """user メッセージの linkId ごとに手動 program を返す（LLM op を含めない）。"""
    link_ids = re.findall(r"### linkId: (\S+)", user_message)
    payload = {
        "plans": [
            {
                "link_id": lid,
                "program": {
                    "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}],
                    "return": "$s1",
                },
            }
            for lid in link_ids
        ]
    }
    return _resp(json.dumps(payload))


def _runbooks_from_message(user_message: str) -> types.SimpleNamespace:
    link_ids = re.findall(r"### linkId: (\S+)", user_message)
    payload = {
        "runbooks": [
            {"link_id": lid, "steps": [{"order": 1, "title": "起点", "description": "説明"}]}
            for lid in link_ids
        ]
    }
    return _resp(json.dumps(payload))


async def _questionnaire_with_confirmed_runbook(db: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-prov-routes",
        title="来歴ルートテスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-prov-routes"},
    )
    db.add(q)
    await db.flush()
    db.add(
        InputRunbook(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="診断日",
            steps=[{"order": 1, "title": "参照", "description": "台帳を見る"}],
            status=RunbookStatus.CONFIRMED,
        )
    )
    await db.commit()
    return q


async def _rows(db: AsyncSession) -> list[DesignLlmCall]:
    return list((await db.execute(select(DesignLlmCall))).scalars().all())


@pytest.mark.asyncio
async def test_plan_compile_persists_provenance(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取得プランのコンパイルで来歴が1件以上保存され、プランも保存されること。

    旧ガード（`{patient_id}` のマーカー走査）は `COMPILE_SYSTEM_PROMPT` に7箇所ある
    `{{patient_id}}` に部分一致して**必ず**発火し、プランも来歴も両方失われていた。
    このテストはその状態では落ちる。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbook(db_session)
    calls = _fake_client(compiler_mod, monkeypatch, _plans_from_message)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True
    assert len(calls) == 1  # LLM は呼ばれている

    rows = await _rows(db_session)
    assert len(rows) == 1
    row = rows[0]
    assert row.role == DesignRole.PLAN
    assert row.questionnaire_id == q.id
    assert row.created_by_id == designer_user.id
    assert "{{patient_id}}" in row.system_prompt  # 正常系の綴りで拒否されない
    assert row.raw_output is not None
    assert row.error is None
    # 取得プランは索引の中身を読まないので出所は空（項目名・所在記述・手順書のみ）
    assert row.content_sources == []


@pytest.mark.asyncio
async def test_runbook_generator_feeds_the_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    """手順書生成が recorder へ来歴を渡すこと（`recorder=None` への改変を検出する）。

    ルート経由にすると設計済み項目（所在＋臨床的意味）の作り込みが必要になるので、
    ここは生成器の入口で固定する。ルート側の保存は `plan/compile` の統合テストで
    見ているので、この2つで「recorder が通る」と「ルートが保存する」の両方が
    load-bearing になる。
    """
    gen = runbook_mod.AIRunbookGenerator()
    _fake_client_on(gen, _runbooks_from_message)
    recorder = DesignLlmRecorder(DesignRole.RUNBOOK)

    await gen.generate(
        [
            {
                "link_id": "item1",
                "item_text": "診断日",
                "source_category": "manual",
                "annotation": None,
                "location": None,
            }
        ],
        recorder=recorder,
    )

    assert len(recorder.records) == 1
    assert recorder.records[0].raw_output is not None
    assert "### linkId: item1" in recorder.records[0].user_message


@pytest.mark.asyncio
async def test_failed_call_is_recorded_and_error_message_survives(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """失敗した呼び出しも来歴に残り、AI のエラー内容が応答に残ること。

    来歴の保存で例外を投げる実装だと、失敗パスでは元の `AIServiceException.message` が
    generic 500 に化けて操作者が原因を追えなくなる。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbook(db_session)
    _fake_client(compiler_mod, monkeypatch, lambda _msg: _resp("{壊れたJSON"))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 500
    message = resp.json()["message"]
    # 元の AIServiceException の内容が残っている（generic 500 に化けていない）
    assert "パースに失敗" in message, message
    assert "Internal" not in message

    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].error is not None
    assert rows[0].raw_output == "{壊れたJSON"


# =========================================================================== #
# 5経路 × 成功時/失敗時 の保存呼び出し
#
# ルートは AI 呼び出しの成功時と失敗時の両方で `save_design_llm_calls_safely` を呼ぶ。
# 実測では、`plans/compile` 以外の8箇所はノーオペ化してもフルスイートが緑の
# まま通った（＝来歴が保存されなくなる改修を許す）。ここで全箇所を load-bearing にする。
# =========================================================================== #

_BROKEN_JSON = "{壊れたJSON"


def _choices_from_message(user_message: str) -> types.SimpleNamespace:
    """データ所在AI案の応答（linkId ごとに先頭候補を選ぶ）。索引経路・BM25経路で共通。"""
    link_ids = re.findall(r"### linkId: (\S+)", user_message)
    payload = {
        "choices": [
            {"link_id": lid, "chosen_cand_id": 0, "source_category": "local_db"} for lid in link_ids
        ]
    }
    return _resp(json.dumps(payload, ensure_ascii=False))


async def _questionnaire_with_items(
    db: AsyncSession, *, fhir_id: str, items: list[tuple[str, str]], annotate: bool = False
) -> Questionnaire:
    """項目つき Questionnaire を作る（annotate=True で臨床的コンテキストも入れる）。

    臨床的コンテキストがあると `_is_resolved` / `_is_designed` が真になり、手順書生成の
    後段ゲートを通る。
    """
    q = Questionnaire(
        fhir_id=fhir_id,
        title="来歴ルートテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": fhir_id,
            "item": [{"linkId": lid, "text": text, "type": "string"} for lid, text in items],
        },
    )
    db.add(q)
    await db.flush()
    if annotate:
        for lid, text in items:
            db.add(
                ClinicalAnnotation(
                    questionnaire_id=q.id,
                    link_id=lid,
                    item_text=text,
                    annotation=f"{text}の臨床的意味",
                )
            )
    await db.commit()
    return q


def _use_resource_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, searchable: bool
) -> None:
    """データ所在AI案の検索経路を選ぶ（索引あり / 索引未配置＝BM25フォールバック）。

    索引未配置は施設が索引を作るまでの縮退経路であり、`location_drafter` へ委譲する。
    **`recorder` はこの委譲を越えて渡らなければならない**（`location_drafter.py` の
    `recorder=recorder`）。索引経路のテストだけではその配線を検証できない。
    """
    reset_store_cache()
    reset_index_cache()
    index_dir = tmp_path / "resource_index"
    if searchable:
        build_db_index(source=_DB_FIXTURE_DIR, out_dir=index_dir, embedder=HashingEmbedder())
        monkeypatch.setattr(settings, "embedding_model", HashingEmbedder().model)
        monkeypatch.setattr(rd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    else:
        index_dir.mkdir()  # 空＝未構築（is_searchable が False）
    monkeypatch.setattr(settings, "resource_index_dir", str(index_dir))
    monkeypatch.setattr(settings, "db_definition_dir", _DB_FIXTURE_DIR)


def _system_draft_url(qid: int) -> str:
    return f"/ai-input/questionnaires/{qid}/source-survey/system/ai-draft"


# --------------------------------------------------------------------------- #
# データ所在（system）: 索引経路 / BM25 フォールバック経路
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("searchable", [True, False], ids=["index", "bm25_fallback"])
async def test_system_draft_persists_provenance(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    searchable: bool,
) -> None:
    """データ所在AI案の成功時に来歴が保存されること（索引経路・BM25経路の両方）。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_resource_index(monkeypatch, tmp_path, searchable=searchable)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-prov-system", items=[("item1", "芽球割合")]
    )
    _fake_client(rd_mod if searchable else ld_mod, monkeypatch, _choices_from_message)

    resp = await designer_client.post(
        _system_draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    rows = await _rows(db_session)
    # **索引経路は2回 LLM を呼ぶ**（検索ヒント→選択）。BM25 フォールバックは
    # ヒントを入れていないので1回のままである。**両方が来歴に残ること**を測る——1項目あたりの
    # LLM の生成時間は2本の合計で数えるので、片方が落ちると過小に出る。
    assert [r.label for r in rows] == (
        ["所在検索ヒント", "データ所在AI案"] if searchable else ["データ所在AI案"]
    ), "データ所在ロールの来歴が保存されていない"
    assert {r.role for r in rows} == {DesignRole.SYSTEM}
    assert {r.created_by_id for r in rows} == {designer_user.id}
    assert all("芽球割合" in r.user_message for r in rows)
    assert all(r.error is None for r in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("searchable", [True, False], ids=["index", "bm25_fallback"])
async def test_system_draft_persists_provenance_on_failure(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    searchable: bool,
) -> None:
    """データ所在AI案の失敗時も来歴（生応答）が残ること（索引経路・BM25経路の両方）。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_resource_index(monkeypatch, tmp_path, searchable=searchable)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-prov-system-ng", items=[("item1", "芽球割合")]
    )
    _fake_client(rd_mod if searchable else ld_mod, monkeypatch, lambda _msg: _resp(_BROKEN_JSON))

    resp = await designer_client.post(
        _system_draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 500
    rows = await _rows(db_session)
    # 索引経路はヒント（このスタブでは壊れた JSON を返すので失敗）と選択の2本になる。
    assert len(rows) == (2 if searchable else 1), "失敗した呼び出しの来歴が保存されていない"
    # **選択のコールは最後の1本**である（索引経路ではその前に検索ヒントが入る）。
    # 測りたいのは「壊れた応答が来歴に残ること」なので、選択の行を見る。
    failed = rows[-1]
    assert failed.role == DesignRole.SYSTEM
    assert failed.raw_output == _BROKEN_JSON
    assert failed.error is not None


# --------------------------------------------------------------------------- #
# 手順書（runbook）: 一括生成 / 単一項目の再生成
# --------------------------------------------------------------------------- #


def _runbook_urls(qid: int) -> dict[str, str]:
    return {
        "generate": f"/ai-input/questionnaires/{qid}/runbook/generate",
        "regenerate": f"/ai-input/questionnaires/{qid}/runbook/item1/regenerate",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["generate", "regenerate"])
async def test_runbook_routes_persist_provenance(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """手順書の一括生成・再生成の成功時に来歴が保存されること。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_items(
        db_session, fhir_id=f"q-prov-rb-{route}", items=[("item1", "診断日")], annotate=True
    )
    _fake_client(runbook_mod, monkeypatch, _runbooks_from_message)

    resp = await designer_client.post(
        _runbook_urls(q.id)[route], json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    rows = await _rows(db_session)
    assert len(rows) == 1, f"手順書({route})の来歴が保存されていない"
    assert rows[0].role == DesignRole.RUNBOOK
    assert rows[0].created_by_id == designer_user.id
    assert "### linkId: item1" in rows[0].user_message
    # 手順書ロールは索引の中身を読まない（項目名・所在記述・臨床的意味のみ）
    assert rows[0].content_sources == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["generate", "regenerate"])
async def test_runbook_routes_persist_provenance_on_failure(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    """手順書の一括生成・再生成の失敗時も来歴（生応答）が残ること。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_items(
        db_session, fhir_id=f"q-prov-rb-ng-{route}", items=[("item1", "診断日")], annotate=True
    )
    _fake_client(runbook_mod, monkeypatch, lambda _msg: _resp(_BROKEN_JSON))

    resp = await designer_client.post(
        _runbook_urls(q.id)[route], json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 500
    rows = await _rows(db_session)
    assert len(rows) == 1, f"手順書({route})の失敗時の来歴が保存されていない"
    assert rows[0].role == DesignRole.RUNBOOK
    assert rows[0].raw_output == _BROKEN_JSON
    assert rows[0].error is not None


# --------------------------------------------------------------------------- #
# 取得プラン（plan）: 単一項目の再コンパイル（一括は上のテストが押さえている）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_plan_recompile_persists_provenance(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単一項目の取得プラン再作成の成功時に来歴が保存されること。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbook(db_session)
    _fake_client(compiler_mod, monkeypatch, _plans_from_message)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1/recompile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].role == DesignRole.PLAN
    assert rows[0].created_by_id == designer_user.id
    assert rows[0].error is None


@pytest.mark.asyncio
async def test_plan_recompile_persists_provenance_on_failure(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """単一項目の取得プラン再作成の失敗時も来歴（生応答）が残ること。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _questionnaire_with_confirmed_runbook(db_session)
    _fake_client(compiler_mod, monkeypatch, lambda _msg: _resp(_BROKEN_JSON))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1/recompile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 500
    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].role == DesignRole.PLAN
    assert rows[0].raw_output == _BROKEN_JSON
    assert rows[0].error is not None


# --------------------------------------------------------------------------- #
# 臨床的意味（clinical）
# --------------------------------------------------------------------------- #
#
# 臨床的意味の drafter も `DesignRole.CLINICAL` として来歴を残す（設計ロール4段の4段目）。


def _clinical_draft_url(qid: int) -> str:
    return f"/ai-input/questionnaires/{qid}/source-survey/clinical/ai-draft"


def _use_reference_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """規約文書（reference）だけを持つ索引を実際に作って使わせる。

    臨床的意味の典拠探索は索引が検索可能でないと LLM を1回も呼ばないので、
    実ビルダで索引を作る必要がある（フェイクでは配線を通せない）。
    """
    from kawasemi_indexer.builder import build_index

    from kawasemi.ai_input import clinical_drafter as cd_mod

    reset_store_cache()
    docs = tmp_path / "manuals"
    docs.mkdir()
    (docs / "registry_manual.txt").write_text(
        "診断日は、組織学的または細胞学的に悪性と確定した日を記載する。"
        "臨床診断のみの場合は画像診断で悪性と判断した日を診断日とする。\n",
        encoding="utf-8",
    )
    # **患者データの所在宣言も同じ索引に入れる。** 規約文書しか無い索引だと、
    # 述語（`is_reference_unit`）を外しても reference しか返らず、「非 reference は
    # 提示も宣言もされない」という不変条件が来歴側から無検証のまま残る。
    manifest = tmp_path / "file_manifest.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "connection_id": "files",
                "file_role": "patient_data",
                "path_pattern": "{patient_id}/pathology.txt",
                # **項目名と同一にして cosine を最大化する。** 「病理レポート（診断日の
                # 記載あり）」のような長いラベルだと足切り（`_MIN_SCORE`）に届かず、
                # 述語を外しても reference しか返らない＝防御ではなく足切りが結果を
                # 作る状態になる。
                "label": "診断日",
                "extract": "text",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    index_dir = tmp_path / "resource_index"
    build_index(
        out_dir=index_dir,
        embedder=HashingEmbedder(),
        file_source=docs,
        file_manifest=manifest,
    )
    monkeypatch.setattr(settings, "embedding_model", HashingEmbedder().model)
    monkeypatch.setattr(cd_mod, "OllamaEmbedder", lambda **kw: HashingEmbedder())
    monkeypatch.setattr(settings, "resource_index_dir", str(index_dir))


def _clinical_drafts_from_message(user_message: str) -> types.SimpleNamespace:
    """提示された最初の cand_id を選ぶ応答を返す。"""
    link_ids = re.findall(r"### linkId: (\S+)", user_message)
    cand_ids = [int(m) for m in re.findall(r"cand_id=(\d+)", user_message)]
    return types.SimpleNamespace(
        message=types.SimpleNamespace(
            content=json.dumps(
                {
                    "drafts": [
                        {
                            "link_id": lid,
                            "chosen_cand_id": cand_ids[0] if cand_ids else None,
                            "annotation": "悪性と確定した日を記載する。",
                        }
                        for lid in link_ids
                    ]
                }
            )
        )
    )


@pytest.mark.asyncio
async def test_clinical_draft_persists_provenance(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """臨床的意味のAI下書きで来歴が保存されること（設計ロール4段の4段目）。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_reference_index(monkeypatch, tmp_path)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-prov-clinical", items=[("item1", "診断日")]
    )
    from kawasemi.ai_input import clinical_drafter as cd_mod

    _fake_client(cd_mod, monkeypatch, _clinical_drafts_from_message)

    resp = await designer_client.post(
        _clinical_draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["success"] is True

    rows = await _rows(db_session)
    assert len(rows) == 1, "臨床的意味ロールの来歴が保存されていない"
    assert rows[0].role == DesignRole.CLINICAL
    assert rows[0].created_by_id == designer_user.id
    # **項目名の行まで見る。** `"診断日" in user_message` だと、候補本文（規約文書の
    # 「診断日は、組織学的または…」）に含まれるので、プロンプトから項目名を落としても
    # 落ちない。項目名が載る経路は tests/ai_input/ のどこでも検証されていなかった。
    assert "項目名: 診断日" in rows[0].user_message
    assert rows[0].error is None
    # ガード2 の出所は規約文書のチャンクだけ（述語で reference に絞ってある）
    assert rows[0].content_sources == ["file:reference"]
    # **宣言した範囲と、プロンプトへ実際に載せた範囲が一致していること。**
    # 項目名（「診断日」）は候補本文を落としても残るので、規約文書側にしか出ない語で
    # 「中身を見せた」という `content_sources` の主張の裏を取る。
    assert "組織学的" in rows[0].user_message
    # **返した典拠が規約文書のチャンクであること。** 索引には項目名と同一ラベルの
    # 患者データの所在宣言も入れてあるので、述語（`is_reference_unit`）を外すと
    # そちらが1位に来て典拠に据えられる。
    # なお `"{patient_id}" not in user_message` のような検査はここでは意味を持たない。
    # 臨床的意味は `snippet`（所在宣言では `path_pattern`）ではなく `search_text`
    # （所在宣言では label ＋ notes）を載せるので、その綴りは元々現れない。
    citation = resp.json()["drafts"][0]["citation"]
    assert citation is not None
    assert citation["payload"]["file_role"] == "reference"


@pytest.mark.asyncio
async def test_clinical_draft_persists_provenance_on_failure(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """臨床的意味のAI下書きが失敗しても、生応答が来歴に残ること。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _use_reference_index(monkeypatch, tmp_path)
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-prov-clinical-ng", items=[("item1", "診断日")]
    )
    from kawasemi.ai_input import clinical_drafter as cd_mod

    _fake_client(
        cd_mod,
        monkeypatch,
        lambda _msg: types.SimpleNamespace(
            message=types.SimpleNamespace(content="これはJSONではない")
        ),
    )

    resp = await designer_client.post(
        _clinical_draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 500
    rows = await _rows(db_session)
    assert len(rows) == 1, "失敗時の来歴が残っていない"
    assert rows[0].role == DesignRole.CLINICAL
    assert rows[0].error is not None
    assert rows[0].raw_output == "これはJSONではない"
    # **失敗しても出所は残る。** 出所の宣言を LLM 呼び出しの後ろへ動かすと、例外で
    # 宣言に到達せず「プロンプト本文はあるのに出所のラベルだけが無い行」ができる。
    # 監査記録としては最悪の形なので、宣言が呼び出しより前にあることをここで固定する。
    assert rows[0].content_sources == ["file:reference"]


@pytest.mark.asyncio
async def test_clinical_draft_records_nothing_when_index_unsearchable(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """索引が検索不可なら LLM を呼ばず、来歴も残さないこと（`clinical` 固有の経路）。

    行が0件であることは確認書の上では「そのロールで AI を使っていない」と等価であり、
    ここで空の行や `error` つきの行ができると**使っていない AI を使ったと開示する**。
    `save` を 409 の早期 return より前に置いている並びは本ロール固有なので、ルート
    合成として固定しておく。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    reset_store_cache()
    empty_index = tmp_path / "resource_index"
    empty_index.mkdir()  # 空＝未構築（is_searchable が False）
    monkeypatch.setattr(settings, "resource_index_dir", str(empty_index))
    q = await _questionnaire_with_items(
        db_session, fhir_id="q-prov-clinical-noindex", items=[("item1", "診断日")]
    )

    resp = await designer_client.post(
        _clinical_draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["index_unsearchable"] is True
    assert await _rows(db_session) == [], "LLM を呼んでいないのに来歴の行ができている"


# --- 検索に使った索引の同一性 ----------------------------------------------------


async def test_the_recorder_carries_the_index_identity(tmp_path: Path) -> None:
    """`declare_index_identity` が build_info を来歴へ運ぶこと。

    索引は版管理外（`.gitignore`）なので、差し替えは diff にも文書の sha256 にも
    現れない。典拠の付与率は永続化された `clinical_annotations.citations` から計算するため、
    **下書きを作った時点の索引と測定した時点の索引が別物でも値は出てしまう**。
    索引が実際に使われた瞬間に作られる記録（この来歴）だけが同一性を残せる。
    """

    class _FakeBuildInfo:
        def model_dump(self, mode: str = "python") -> dict[str, object]:
            return {"embedding_model": "bge-m3", "unit_count": 1234, "schema_version": 2}

    class _FakeStore:
        build_info = _FakeBuildInfo()

    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    assert recorder.index_build_info is None, "宣言前は null（未照合）であること"

    recorder.declare_index_identity(_FakeStore())

    assert recorder.index_build_info == {
        "embedding_model": "bge-m3",
        "unit_count": 1234,
        "schema_version": 2,
    }
