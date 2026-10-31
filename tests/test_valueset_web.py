"""ValueSet 管理画面（web/routes.py）の CRUD 回帰テスト。

様式の内容の一部なので設計者が管理する。正常系（登録→詳細→編集→論理削除）と
異常系（無効JSON・FHIR検証失敗・CSRF欠落）を固定する。認可（未認証401 / 非designer403）は
tests/test_security.py::TestWebRouteAuthorization を参照。
"""

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.valueset import ValueSet

_SAMPLE_VS = {
    "resourceType": "ValueSet",
    "status": "active",
    "url": "http://kawasemi.example/ValueSet/test-vs",
    "version": "1.0.0",
    "title": "テストValueSet",
    "compose": {
        "include": [
            {
                "system": "http://kawasemi.example/CodeSystem/test",
                "concept": [
                    {"code": "1", "display": "選択肢1"},
                    {"code": "2", "display": "選択肢2"},
                ],
            }
        ]
    },
}


async def _create_valueset(db_session: AsyncSession, **overrides: object) -> ValueSet:
    """DB に ValueSet を1件用意する（画面テストの前提データ）。"""
    content = {**_SAMPLE_VS, **overrides}
    vs = ValueSet(
        fhir_id="vs-fixture-1",
        content=content,
        title=content.get("title"),
        status=content.get("status", "draft"),
        version=content.get("version"),
        url=content.get("url"),
    )
    db_session.add(vs)
    await db_session.commit()
    await db_session.refresh(vs)
    return vs


@pytest.mark.asyncio
async def test_list_valuesets_empty(designer_client: AsyncClient) -> None:
    """一覧: 0件でも 200 で空状態を表示する。"""
    resp = await designer_client.get("/valuesets")
    assert resp.status_code == 200
    assert "ValueSet管理" in resp.text


@pytest.mark.asyncio
async def test_create_valueset_success(
    designer_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    """新規登録: 妥当な ValueSet JSON は 303 で詳細へ、DB に保存され fhir_id が採番される。"""
    resp = await designer_client.post(
        "/valuesets/new",
        data={"resource_json": json.dumps(_SAMPLE_VS), "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/valuesets/")

    result = await db_session.execute(select(ValueSet).where(~ValueSet.is_deleted))
    saved = result.scalars().all()
    assert len(saved) == 1
    assert saved[0].title == "テストValueSet"
    assert saved[0].status == "active"
    assert saved[0].url == "http://kawasemi.example/ValueSet/test-vs"
    # サーバ側で FHIR ID が採番され content にも反映される
    assert saved[0].fhir_id
    assert saved[0].content["id"] == saved[0].fhir_id


@pytest.mark.asyncio
async def test_create_valueset_invalid_json(designer_client: AsyncClient, csrf_token: str) -> None:
    """新規登録: 壊れた JSON は 200 でフォーム再描画＋エラー表示（保存しない）。"""
    resp = await designer_client.post(
        "/valuesets/new",
        data={"resource_json": "{ぶっ壊れ", "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert "無効なJSON形式です" in resp.text


@pytest.mark.asyncio
async def test_create_valueset_fhir_validation_error(
    designer_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    """新規登録: FHIR 必須項目（status）を欠く JSON は登録失敗として再描画される。"""
    bad = {"resourceType": "ValueSet", "url": "http://x/vs"}  # status 欠落
    resp = await designer_client.post(
        "/valuesets/new",
        data={"resource_json": json.dumps(bad), "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert "登録に失敗しました" in resp.text
    result = await db_session.execute(select(ValueSet))
    assert result.scalars().first() is None


@pytest.mark.asyncio
async def test_create_valueset_without_csrf_rejected(designer_client: AsyncClient) -> None:
    """新規登録: CSRF トークン無しの POST は 403。"""
    resp = await designer_client.post(
        "/valuesets/new", data={"resource_json": json.dumps(_SAMPLE_VS)}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_show_valueset_lists_options(
    designer_client: AsyncClient, db_session: AsyncSession
) -> None:
    """詳細: compose.include[].concept から選択肢を抽出して表示する。"""
    vs = await _create_valueset(db_session)
    resp = await designer_client.get(f"/valuesets/{vs.id}")
    assert resp.status_code == 200
    assert "選択肢 (2件)" in resp.text
    assert "選択肢1" in resp.text
    assert "選択肢2" in resp.text


@pytest.mark.asyncio
async def test_show_valueset_not_found(designer_client: AsyncClient) -> None:
    """詳細: 存在しない ID は 404。"""
    resp = await designer_client.get("/valuesets/9999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_valueset_success(
    designer_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    """更新: タイトル変更が 303 で保存され、fhir_id は維持される。"""
    vs = await _create_valueset(db_session)
    original_fhir_id = vs.fhir_id
    updated = {**_SAMPLE_VS, "title": "更新後タイトル"}
    resp = await designer_client.post(
        f"/valuesets/{vs.id}/edit",
        data={"resource_json": json.dumps(updated), "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 303

    await db_session.refresh(vs)
    assert vs.title == "更新後タイトル"
    assert vs.fhir_id == original_fhir_id
    assert vs.content["id"] == original_fhir_id


@pytest.mark.asyncio
async def test_update_valueset_fhir_validation_error_rerenders(
    designer_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    """更新: FHIR 検証失敗は 500 でなく 200 で再描画される（rollback 後の expire で500化しない）。"""
    vs = await _create_valueset(db_session)
    bad = {"resourceType": "ValueSet"}  # status 欠落
    resp = await designer_client.post(
        f"/valuesets/{vs.id}/edit",
        data={"resource_json": json.dumps(bad), "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert "更新に失敗しました" in resp.text
    # 入力した JSON が textarea に保持される
    assert "resourceType" in resp.text
    # 元データは変更されない
    await db_session.refresh(vs)
    assert vs.status == "active"


@pytest.mark.asyncio
async def test_update_valueset_duplicate_url_rerenders(
    designer_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    """更新: 既存の別 ValueSet と url が重複（unique制約違反）しても 500 でなく 200 で再描画。"""
    other = ValueSet(
        fhir_id="vs-other",
        content={**_SAMPLE_VS, "url": "http://kawasemi.example/ValueSet/other"},
        title="他ValueSet",
        status="active",
        url="http://kawasemi.example/ValueSet/other",
    )
    db_session.add(other)
    await db_session.commit()

    vs = await _create_valueset(db_session)
    # vs の url を other と同じに変更 → commit で IntegrityError
    dup = {**_SAMPLE_VS, "url": "http://kawasemi.example/ValueSet/other"}
    resp = await designer_client.post(
        f"/valuesets/{vs.id}/edit",
        data={"resource_json": json.dumps(dup), "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert "更新に失敗しました" in resp.text


@pytest.mark.asyncio
async def test_delete_valueset_soft(
    designer_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    """削除: 論理削除され、一覧クエリ（~is_deleted）から外れる。"""
    vs = await _create_valueset(db_session)
    resp = await designer_client.post(
        f"/valuesets/{vs.id}/delete",
        data={"csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 303

    await db_session.refresh(vs)
    assert vs.is_deleted is True
    # 論理削除後は詳細も 404
    resp2 = await designer_client.get(f"/valuesets/{vs.id}")
    assert resp2.status_code == 404
