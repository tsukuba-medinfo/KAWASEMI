"""検証ワークフローのテスト

- 検証者が verify / reject（理由必須）でき、サインオフ・監査ログが残る
- SoD: 自分が提出した回答は検証できない
- 検証済は施錠（編集不可）。差戻し後の再編集で未検証へ戻り amended になる
- エクスポートは既定で検証済のみ（include_unverified で全件）。未検証の単体exportは403
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import (
    QuestionnaireResponse,
    ResponseVerificationLog,
    VerificationStatus,
)
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from tests.conftest import TestSessionLocal


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    client.cookies.set(
        settings.jwt_cookie_name, create_access_token(data={"sub": str(user.id), "act": role})
    )


async def _make_response(
    db: AsyncSession,
    *,
    submitted_by_id: int | None,
    verification_status: str = VerificationStatus.UNVERIFIED,
    fhir_id: str = "qr-verif-1",
) -> tuple[Questionnaire, QuestionnaireResponse]:
    q = Questionnaire(
        fhir_id="q-verif",
        title="検証WFテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-verif",
            "item": [{"linkId": "q1", "type": "string", "text": "質問"}],
        },
    )
    db.add(q)
    await db.flush()
    qr = QuestionnaireResponse(
        fhir_id=fhir_id,
        questionnaire_id=q.id,
        content={"resourceType": "QuestionnaireResponse", "status": "completed", "item": []},
        status="completed",
        subject="Patient/p-001",
        submitted_by_id=submitted_by_id,
        verification_status=verification_status,
    )
    db.add(qr)
    await db.commit()
    await db.refresh(qr)
    return q, qr


async def _count_logs(db: AsyncSession, qr_id: int, action: str) -> int:
    return (
        await db.execute(
            select(func.count())
            .select_from(ResponseVerificationLog)
            .where(
                ResponseVerificationLog.questionnaire_response_id == qr_id,
                ResponseVerificationLog.action == action,
            )
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_verifier_verify_signs_off_and_logs(
    reviewer_client: AsyncClient,
    reviewer_user: User,
    collector_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """検証者の verify で検証済・サインオフ・監査ログが残る。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    resp = await reviewer_client.post(
        f"/responses/{qr.id}/verify",
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with TestSessionLocal() as v:
        refreshed = (
            await v.execute(select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr.id))
        ).scalar_one()
        assert refreshed.verification_status == VerificationStatus.VERIFIED
        assert refreshed.verified_by_id == reviewer_user.id
        assert refreshed.verified_at is not None
        assert await _count_logs(v, qr.id, "verified") == 1


@pytest.mark.asyncio
async def test_verifier_cannot_verify_own_submission(
    reviewer_client: AsyncClient,
    reviewer_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """SoD: 自分が提出した回答は検証できない（403）。"""
    _, qr = await _make_response(db_session, submitted_by_id=reviewer_user.id)
    resp = await reviewer_client.post(
        f"/responses/{qr.id}/verify",
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    async with TestSessionLocal() as v:
        refreshed = (
            await v.execute(select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr.id))
        ).scalar_one()
        assert refreshed.verification_status == VerificationStatus.UNVERIFIED


@pytest.mark.asyncio
async def test_verifier_reject_requires_reason(
    reviewer_client: AsyncClient,
    collector_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """差戻しは理由必須（空なら400）。理由ありで rejected + 理由 + ログ。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)

    no_reason = await reviewer_client.post(
        f"/responses/{qr.id}/reject",
        data={"reason": "  "},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert no_reason.status_code == 400

    ok = await reviewer_client.post(
        f"/responses/{qr.id}/reject",
        data={"reason": "値の根拠が不足"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    async with TestSessionLocal() as v:
        refreshed = (
            await v.execute(select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr.id))
        ).scalar_one()
        assert refreshed.verification_status == VerificationStatus.REJECTED
        assert refreshed.rejection_reason == "値の根拠が不足"
        assert await _count_logs(v, qr.id, "rejected") == 1


@pytest.mark.asyncio
async def test_verified_response_is_immutable(
    collector_client: AsyncClient,
    collector_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """検証済は施錠: 編集画面・更新ともに 403。"""
    _, qr = await _make_response(
        db_session,
        submitted_by_id=collector_user.id,
        verification_status=VerificationStatus.VERIFIED,
    )
    get_resp = await collector_client.get(f"/responses/{qr.id}/edit")
    assert get_resp.status_code == 403
    post_resp = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={"q1": "x"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert post_resp.status_code == 403


@pytest.mark.asyncio
async def test_rejected_resubmit_resets_to_unverified(
    collector_client: AsyncClient,
    collector_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """差戻し後の再編集（再提出）で未検証へ戻り、FHIR status=amended、ログに resubmitted。"""
    _, qr = await _make_response(
        db_session,
        submitted_by_id=collector_user.id,
        verification_status=VerificationStatus.REJECTED,
    )
    resp = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={"subject": "Patient/p-001", "q1": "修正値"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with TestSessionLocal() as v:
        refreshed = (
            await v.execute(select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr.id))
        ).scalar_one()
        assert refreshed.verification_status == VerificationStatus.UNVERIFIED
        assert refreshed.rejection_reason is None
        assert refreshed.status == "amended"
        assert await _count_logs(v, qr.id, "resubmitted") == 1


@pytest.mark.asyncio
async def test_single_export_blocks_unverified(
    admin_client: AsyncClient,
    admin_user: User,
    collector_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """未検証回答の単体エクスポートは 403（検証済のみ可）。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    _act_as(admin_client, admin_user, RoleType.REVIEWER)
    resp = await admin_client.post(
        f"/api/v100/questionnaire-responses/{qr.id}/export",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 403
