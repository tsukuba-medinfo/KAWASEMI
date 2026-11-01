"""AIドラフト確認操作の操作者記録のテスト。

InputRunbook / InputPlan を confirmed へ昇格する際、確認操作者（confirmed_by_id）と
日時（confirmed_at）が記録されること、内容が同一の保存（confirm=false）で保持され、draft では
記録されないことを検証する。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import InputPlan, InputRunbook
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

# needs_input なし・LLM op なしの妥当プログラム（confirm 可能）
_SQL_PROGRAM = {
    "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT v FROM t WHERE pid = :patient_id"}],
    "return": "$s1",
}


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-confirmed-by",
        title="確認者記録テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-confirmed-by",
            "item": [{"linkId": "item1", "text": "項目1", "type": "string"}],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


async def _get_runbook(db: AsyncSession, qid: int, link_id: str) -> InputRunbook:
    result = await db.execute(
        select(InputRunbook).where(
            InputRunbook.questionnaire_id == qid, InputRunbook.link_id == link_id
        )
    )
    return result.scalar_one()


async def _get_plan(db: AsyncSession, qid: int, link_id: str) -> InputPlan:
    result = await db.execute(
        select(InputPlan).where(InputPlan.questionnaire_id == qid, InputPlan.link_id == link_id)
    )
    return result.scalar_one()


@pytest.mark.asyncio
async def test_runbook_confirm_records_confirmed_by(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """手順書の confirm 昇格で confirmed_by_id / confirmed_at が記録される。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1?confirm=true",
        json={"base_version": None, "steps": []},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"

    rb = await _get_runbook(db_session, q.id, "item1")
    assert rb.status == "confirmed"
    assert rb.confirmed_by_id == designer_user.id
    assert rb.confirmed_at is not None


@pytest.mark.asyncio
async def test_runbook_draft_save_does_not_record_confirmed_by(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """confirm しない保存（draft）では confirmed_by は記録されない。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1",
        json={"steps": []},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"

    rb = await _get_runbook(db_session, q.id, "item1")
    assert rb.confirmed_by_id is None
    assert rb.confirmed_at is None


@pytest.mark.asyncio
async def test_runbook_edit_after_confirm_preserves_confirmer(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """内容が同一の保存（confirm=false）では確認済みと元の確認者を保持する。

    内容を変えた保存は draft へ戻る（test_plan_confirmation_integrity を参照）。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1?confirm=true",
        json={"base_version": None, "steps": []},
        headers={"X-CSRF-Token": csrf_token},
    )
    rb = await _get_runbook(db_session, q.id, "item1")
    original_confirmer = rb.confirmed_by_id
    original_at = rb.confirmed_at

    # confirm=false で同一内容を保存（status は confirmed のまま）
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1",
        json={"steps": []},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.json()["status"] == "confirmed"
    await db_session.refresh(rb)
    assert rb.confirmed_by_id == original_confirmer
    assert rb.confirmed_at == original_at


@pytest.mark.asyncio
async def test_plan_confirm_records_confirmed_by(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """取得プランの confirm 昇格で confirmed_by_id / confirmed_at が記録される。"""
    q = await _make_questionnaire(db_session)
    # プラン確認には確認済みの手順書が前提（未確認の手順書では確認を拒否する）
    db_session.add(
        InputRunbook(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="項目1",
            steps=[],
            status="confirmed",
        )
    )
    await db_session.commit()

    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1?confirm=true",
        json={"base_version": None, "program": _SQL_PROGRAM},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirmed"

    plan = await _get_plan(db_session, q.id, "item1")
    assert plan.status == "confirmed"
    assert plan.confirmed_by_id == designer_user.id
    assert plan.confirmed_at is not None


@pytest.mark.asyncio
async def test_plan_draft_save_does_not_record_confirmed_by(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """プランの draft 保存（confirm=false）では confirmed_by は記録されない。"""
    q = await _make_questionnaire(db_session)
    db_session.add(
        InputRunbook(questionnaire_id=q.id, link_id="item1", item_text="項目1", steps=[])
    )
    await db_session.commit()

    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1",
        json={"program": _SQL_PROGRAM},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "draft"

    plan = await _get_plan(db_session, q.id, "item1")
    assert plan.confirmed_by_id is None
    assert plan.confirmed_at is None


@pytest.mark.asyncio
async def test_runbook_edit_by_other_user_preserves_original_confirmer(
    designer_client: AsyncClient,
    designer_user: User,
    multi_role_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """別ユーザーが confirm=false で同一内容を保存しても、元の確認者・確認日時が保持される。"""
    q = await _make_questionnaire(db_session)

    # ユーザーA（ai_designer）が確認
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1?confirm=true",
        json={"base_version": None, "steps": []},
        headers={"X-CSRF-Token": csrf_token},
    )
    rb = await _get_runbook(db_session, q.id, "item1")
    confirmer_a = rb.confirmed_by_id
    at_a = rb.confirmed_at
    assert confirmer_a == designer_user.id

    # ユーザーB（設計者も保有する別の利用者）が confirm=false で同一内容を保存
    _act_as(designer_client, multi_role_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1",
        json={"steps": []},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.json()["status"] == "confirmed"
    await db_session.refresh(rb)
    # 元の確認者A のまま（編集者B に書き換わらない）
    assert rb.confirmed_by_id == confirmer_a
    assert rb.confirmed_at == at_a
