"""エクスポートした JSON が、HTML として解釈されても害にならないこと（再スキャン F1）。

回答には、収集者が入力した文字列（値・linkId）やデザイナーが付けた文字列が入る。
エクスポートの応答を取り違えて HTML として扱う実装（ブラウザの種別推測、取得した本文の
ページへの書き込み）があっても、`<` `>` `&` がタグの材料にならないようにする。
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import QuestionnaireResponse, VerificationStatus
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from kawasemi.utils.file_export import dumps_html_safe

HOSTILE = "x'\"><img src=x onerror=alert(1)> & <svg/onload=alert(2)>"


def test_dumps_html_safe_has_no_markup_characters_and_round_trips() -> None:
    content = {"item": [{"linkId": HOSTILE, "answer": [{"valueString": HOSTILE}]}], "日本語": "値"}
    text = dumps_html_safe(content)
    for ch in "<>&":
        assert ch not in text
    assert json.loads(text) == content
    assert "日本語" in text  # ensure_ascii=False は保つ（可読性）


def test_dumps_html_safe_leaves_ordinary_json_alone() -> None:
    content = {"a": [1, 2, {"b": "c"}], "d": None}
    assert dumps_html_safe(content) == json.dumps(content, ensure_ascii=False, indent=2)


async def _verified_hostile_response(db: AsyncSession) -> QuestionnaireResponse:
    q = Questionnaire(
        fhir_id="q-xss",
        title="XSS テスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-xss", "item": []},
    )
    db.add(q)
    await db.flush()
    qr = QuestionnaireResponse(
        fhir_id="qr-xss-1",
        questionnaire_id=q.id,
        content={
            "resourceType": "QuestionnaireResponse",
            "status": "completed",
            "questionnaire": HOSTILE,
            "item": [{"linkId": HOSTILE, "answer": [{"valueString": HOSTILE}]}],
        },
        status="completed",
        subject="Patient/p-001",
        verification_status=VerificationStatus.VERIFIED,
    )
    db.add(qr)
    await db.commit()
    await db.refresh(qr)
    return qr


def _act_as_reviewer(client: AsyncClient, user: User) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": RoleType.REVIEWER})
    client.cookies.set(settings.jwt_cookie_name, token)


@pytest.mark.asyncio
async def test_single_export_body_has_no_markup_and_is_nosniff(
    admin_client: AsyncClient,
    multi_role_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    qr = await _verified_hostile_response(db_session)
    _act_as_reviewer(admin_client, multi_role_user)
    resp = await admin_client.post(
        f"/api/v100/questionnaire-responses/{qr.id}/export",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith("attachment")
    for ch in "<>&":
        assert ch not in resp.text
    assert json.loads(resp.text)["item"][0]["answer"][0]["valueString"] == HOSTILE


@pytest.mark.asyncio
async def test_bulk_export_entries_have_no_markup_and_response_is_nosniff(
    admin_client: AsyncClient,
    multi_role_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    await _verified_hostile_response(db_session)
    _act_as_reviewer(admin_client, multi_role_user)
    resp = await admin_client.post(
        "/api/v100/questionnaire-responses/bulk-export",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.headers["x-content-type-options"] == "nosniff"
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        names = zf.namelist()
        assert names
        for name in names:
            body = zf.read(name).decode("utf-8")
            for ch in "<>&":
                assert ch not in body, name
            assert json.loads(body)["item"][0]["linkId"] == HOSTILE
