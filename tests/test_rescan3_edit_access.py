"""再スキャン 3 回目の F5: 回答の編集の認可を、患者 × 様式のリースに揃える。

以前は「その患者のどの入力対象でもリースがあれば」編集できたので、同じ患者の別の様式の
リースを持つ収集者が、他の収集者が入力した回答を読み書きできた。保存の経路
（`require_leased_target`）と同じ粒度（患者 × この回答の様式）にそろえる。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.input_target import InputTargetStatus
from kawasemi.models.role import Role
from kawasemi.models.user import User
from tests.conftest import _make_user
from tests.lease_helpers import make_target
from tests.test_verification_workflow import _make_response


async def _other_collector(db: AsyncSession, collector_role: Role) -> User:
    return await _make_user(
        db, "other_collector", "別の収集者", "pw-other-collector", [collector_role]
    )


@pytest.mark.asyncio
async def test_leaseholder_for_another_questionnaire_cannot_read_or_write_the_response(
    collector_client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """同じ患者の別の様式のリースを持つだけの収集者は、他の収集者の回答を開けず、書けない。"""
    other = await _other_collector(db_session, collector_role)
    q, qr = await _make_response(db_session, submitted_by_id=other.id)
    # collector_user が持つのは (p-001, 別の様式) のリース
    await make_target(
        db_session,
        "p-001",
        questionnaire_id=q.id + 100,
        holder_id=collector_user.id,
        lease_minutes=30,
        status=InputTargetStatus.IN_PROGRESS,
    )
    await db_session.commit()

    got = await collector_client.get(f"/responses/{qr.id}/edit")
    assert got.status_code == 403

    posted = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={"q1": "書き換え"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert posted.status_code == 403
    await db_session.refresh(qr)
    assert "書き換え" not in str(qr.content)


@pytest.mark.asyncio
async def test_leaseholder_for_the_same_patient_and_questionnaire_can_edit(
    collector_client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """患者 × この様式のリースを持つ収集者は、従来どおり読み書きできる。"""
    other = await _other_collector(db_session, collector_role)
    q, qr = await _make_response(db_session, submitted_by_id=other.id)
    await make_target(
        db_session,
        "p-001",
        questionnaire_id=q.id,
        holder_id=collector_user.id,
        lease_minutes=30,
        status=InputTargetStatus.IN_PROGRESS,
    )
    await db_session.commit()

    assert (await collector_client.get(f"/responses/{qr.id}/edit")).status_code == 200
    posted = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={"q1": "担当者による修正"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert posted.status_code == 303


@pytest.mark.asyncio
async def test_expired_lease_for_the_questionnaire_does_not_grant_access(
    collector_client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    db_session: AsyncSession,
) -> None:
    other = await _other_collector(db_session, collector_role)
    q, qr = await _make_response(db_session, submitted_by_id=other.id)
    await make_target(
        db_session,
        "p-001",
        questionnaire_id=q.id,
        holder_id=collector_user.id,
        lease_minutes=-5,
        status=InputTargetStatus.IN_PROGRESS,
    )
    await db_session.commit()
    assert (await collector_client.get(f"/responses/{qr.id}/edit")).status_code == 403


@pytest.mark.asyncio
async def test_submitter_can_still_edit_without_a_lease(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """提出者本人は、リースが完了で切れた後も、自分の回答（差戻しなど）を編集できる。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    assert (await collector_client.get(f"/responses/{qr.id}/edit")).status_code == 200
    posted = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={"q1": "本人の修正"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert posted.status_code == 303


@pytest.mark.asyncio
async def test_reviewer_still_reads_the_detail_page(
    reviewer_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
) -> None:
    """検証者は、リースなしで詳細ページを読める（編集の認可の変更は、詳細ページに及ばない）。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    assert (await reviewer_client.get(f"/responses/{qr.id}")).status_code == 200
