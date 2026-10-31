"""「規約文書に該当箇所が無い」の記録のテスト

**記述が空・典拠0件・`status=draft` という状態は、次の2つで完全に同じだった。**

| | 記述 | 典拠 | status |
|---|---|---|---|
| まだ書いていない | 空 | 0件 | draft |
| 探したが該当箇所が無いので空欄にした | 空 | 0件 | draft |

`save_clinical` は記述の空な項目を confirmed へ昇格させないので、確定ワークフローでも
区別が付かない。後者は「規約文書を引いても意味が決まらなかった項目」として記録したいが、
**それを LLM 経路の結果から数えることはできない**。人の判断の置き場が要る。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import AnnotationStatus, ClinicalAnnotation
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

_REASON = "様式の定義書に定義が無く、記入の手引きにも記載が見当たらない"


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-no-citation-reason",
        title="典拠なしの理由のテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-no-citation-reason",
            "item": [
                {"linkId": "item1", "text": "診断日", "type": "string"},
                {"linkId": "item2", "text": "進行度", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


def _url(qid: int) -> str:
    return f"/ai-input/questionnaires/{qid}/source-survey/clinical"


async def _row(db_session: AsyncSession, link_id: str) -> ClinicalAnnotation:
    result = await db_session.execute(
        select(ClinicalAnnotation).where(ClinicalAnnotation.link_id == link_id)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_the_reason_is_saved_with_an_empty_annotation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """記述を空欄のまま「該当箇所が無い」を保存できること（典拠が見つからない項目を人が記録する運用そのもの）。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        _url(q.id),
        json={
            "items": [
                {
                    "link_id": "item1",
                    "annotation": None,
                    "citations": [],
                    "no_citation_reason": _REASON,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200
    row = await _row(db_session, "item1")
    assert row.no_citation_reason == _REASON
    assert row.annotation is None, "記述は空欄のまま（推測で補わない）"
    assert row.status == AnnotationStatus.DRAFT, "記述が空なので確定はしない"


@pytest.mark.asyncio
async def test_the_untouched_item_and_the_no_citation_item_are_distinguishable(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """**未着手と「該当箇所が無い」が API の応答で区別できること。**

    どちらも記述が空・典拠0件・draft なので、この値が無いと同じに見える。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _url(q.id),
        json={
            "items": [
                {"link_id": "item1", "annotation": None, "no_citation_reason": _REASON},
                {"link_id": "item2", "annotation": None},
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    searched = await _row(db_session, "item1")
    untouched = await _row(db_session, "item2")

    assert searched.no_citation_reason == _REASON
    assert untouched.no_citation_reason is None
    assert searched.annotation == untouched.annotation is None, "前提: 記述はどちらも空"
    assert searched.status == untouched.status, "前提: status では区別できない"


@pytest.mark.asyncio
async def test_a_request_without_the_key_keeps_the_saved_reason(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """**キーを持たない保存で既存の値が消えないこと**（`citations` / `llm_model` と同じ規則）。

    項目単位の保存や、この欄を知らない経路からの保存で人の記録が消えると、
    「探したが無かった」という判断が黙って失われる。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _url(q.id),
        json={"items": [{"link_id": "item1", "no_citation_reason": _REASON}]},
        headers={"X-CSRF-Token": csrf_token},
    )

    # 同じ項目を、キーを含めずに保存し直す
    await designer_client.post(
        _url(q.id),
        json={"items": [{"link_id": "item1", "annotation": "後から書いた記述"}]},
        headers={"X-CSRF-Token": csrf_token},
    )

    row = await _row(db_session, "item1")
    assert row.no_citation_reason == _REASON
    assert row.annotation == "後から書いた記述"


@pytest.mark.asyncio
async def test_an_explicit_empty_value_clears_the_reason(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """明示的に空で送れば消せること（「やはり典拠が見つかった」を書き戻せる）。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _url(q.id),
        json={"items": [{"link_id": "item1", "no_citation_reason": _REASON}]},
        headers={"X-CSRF-Token": csrf_token},
    )

    await designer_client.post(
        _url(q.id),
        json={"items": [{"link_id": "item1", "no_citation_reason": None}]},
        headers={"X-CSRF-Token": csrf_token},
    )

    row = await _row(db_session, "item1")
    assert row.no_citation_reason is None


@pytest.mark.asyncio
async def test_the_screen_shows_the_field_and_marks_the_recorded_item(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """画面に欄が出て、記録済みの項目が「未入力」と別に見えること。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _url(q.id),
        json={"items": [{"link_id": "item1", "no_citation_reason": _REASON}]},
        headers={"X-CSRF-Token": csrf_token},
    )

    resp = await designer_client.get(_url(q.id))
    body = resp.text

    assert resp.status_code == 200
    assert "no-citation-reason" in body, "入力欄が描画されていない"
    assert _REASON in body, "保存済みの理由が復元されていない"
    assert "該当箇所なし（記録済み）" in body, "記録済みの項目が未入力と同じに見える"


@pytest.mark.asyncio
async def test_an_existing_row_without_the_column_value_still_renders(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
) -> None:
    """値が NULL の既存行でも画面と初期状態が壊れないこと。"""
    q = await _make_questionnaire(db_session)
    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="item1",
            annotation="既存の記述",
            citations=[],
            status=AnnotationStatus.DRAFT,
        )
    )
    await db_session.commit()
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get(_url(q.id))

    assert resp.status_code == 200
    assert '"no_citation_reason": null' in resp.text or '"no_citation_reason":null' in resp.text
