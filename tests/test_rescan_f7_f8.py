"""再スキャンの指摘 F7 / F8 のテスト（一般的な修正のみ。施設固有の内容は含まない）。

- F7: 職務分離（SoD）が提出者しか見ておらず、収集者と検証者を兼ねるユーザーが他人の回答を
  編集してから検証できた。編集者も記録し、検証・差戻しを拒否する。
- F8: 検証済み（施錠）の確認と書き込みが別の手順で、その間に検証が割り込むと、検証済みの
  回答の内容が書き換わった。書き込みの条件に「検証済みでない」を含めて原子的にする。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from kawasemi.models.questionnaire_response import (
    QuestionnaireResponse,
    ResponseVerificationLog,
    VerificationStatus,
)
from kawasemi.models.role import Role
from kawasemi.models.user import User
from tests.conftest import TestSessionLocal, _make_user
from tests.lease_helpers import lease_patient
from tests.test_verification_workflow import _act_as, _make_response


async def _load(qr_id: int) -> QuestionnaireResponse:
    async with TestSessionLocal() as v:
        return (
            await v.execute(select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr_id))
        ).scalar_one()


async def _actions(qr_id: int) -> list[str]:
    async with TestSessionLocal() as v:
        rows = await v.execute(
            select(ResponseVerificationLog.action)
            .where(ResponseVerificationLog.questionnaire_response_id == qr_id)
            .order_by(ResponseVerificationLog.id)
        )
        return [r[0] for r in rows]


async def _dual_role_user(
    db: AsyncSession, collector_role: Role, reviewer_role: Role, name: str = "dual"
) -> User:
    return await _make_user(db, name, "兼任", "Passw0rd!x", [collector_role, reviewer_role])


# ===== F7 =====


@pytest.mark.asyncio
async def test_edit_records_the_editor_and_an_audit_entry(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    resp = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={"q1": "値"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    saved = await _load(qr.id)
    assert saved.last_edited_by_id == collector_user.id
    assert await _actions(qr.id) == ["edited"]


@pytest.mark.asyncio
async def test_dual_role_user_cannot_edit_then_verify(
    client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    reviewer_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """他人が提出した回答を編集した兼任ユーザーは、その回答を検証できない。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    dual = await _dual_role_user(db_session, collector_role, reviewer_role)
    await lease_patient(db_session, dual.id, "p-001")
    headers = {"X-CSRF-Token": csrf_token}

    _act_as(client, dual, "collector")
    edit = await client.post(
        f"/responses/{qr.id}/edit", data={"q1": "改変"}, headers=headers, follow_redirects=False
    )
    assert edit.status_code == 303

    _act_as(client, dual, "reviewer")
    verify = await client.post(
        f"/responses/{qr.id}/verify", headers=headers, follow_redirects=False
    )
    assert verify.status_code == 403
    assert (await _load(qr.id)).verification_status == VerificationStatus.UNVERIFIED


@pytest.mark.asyncio
async def test_dual_role_user_cannot_edit_then_reject(
    client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    reviewer_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    dual = await _dual_role_user(db_session, collector_role, reviewer_role)
    await lease_patient(db_session, dual.id, "p-001")
    headers = {"X-CSRF-Token": csrf_token}

    _act_as(client, dual, "collector")
    await client.post(
        f"/responses/{qr.id}/edit", data={"q1": "改変"}, headers=headers, follow_redirects=False
    )
    _act_as(client, dual, "reviewer")
    reject = await client.post(
        f"/responses/{qr.id}/reject",
        data={"reason": "理由"},
        headers=headers,
        follow_redirects=False,
    )
    assert reject.status_code == 403
    assert (await _load(qr.id)).verification_status == VerificationStatus.UNVERIFIED


@pytest.mark.asyncio
async def test_an_earlier_editor_stays_barred_after_someone_else_edits(
    client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    reviewer_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """最後の編集者でなくなっても、一度でも編集したユーザーは検証できない（監査ログで判定）。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    dual = await _dual_role_user(db_session, collector_role, reviewer_role)
    await lease_patient(db_session, dual.id, "p-001")
    headers = {"X-CSRF-Token": csrf_token}

    _act_as(client, dual, "collector")
    await client.post(
        f"/responses/{qr.id}/edit", data={"q1": "一回目"}, headers=headers, follow_redirects=False
    )
    _act_as(client, collector_user, "collector")
    await client.post(
        f"/responses/{qr.id}/edit", data={"q1": "二回目"}, headers=headers, follow_redirects=False
    )
    assert (await _load(qr.id)).last_edited_by_id == collector_user.id

    _act_as(client, dual, "reviewer")
    verify = await client.post(
        f"/responses/{qr.id}/verify", headers=headers, follow_redirects=False
    )
    assert verify.status_code == 403


@pytest.mark.asyncio
async def test_a_separate_reviewer_can_still_verify_an_edited_response(
    client: AsyncClient,
    collector_user: User,
    reviewer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    headers = {"X-CSRF-Token": csrf_token}
    _act_as(client, collector_user, "collector")
    await client.post(
        f"/responses/{qr.id}/edit", data={"q1": "値"}, headers=headers, follow_redirects=False
    )
    _act_as(client, reviewer_user, "reviewer")
    verify = await client.post(
        f"/responses/{qr.id}/verify", headers=headers, follow_redirects=False
    )
    assert verify.status_code == 303
    saved = await _load(qr.id)
    assert saved.verification_status == VerificationStatus.VERIFIED
    assert saved.verified_by_id == reviewer_user.id


# ===== F8 =====


@pytest.mark.asyncio
async def test_edit_is_refused_when_a_verify_lands_between_the_check_and_the_write(
    collector_client: AsyncClient,
    collector_user: User,
    reviewer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """施錠の確認の後、書き込みの前に検証が確定しても、編集は書き込まれない。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    qr_id = qr.id
    original_content = (await _load(qr_id)).content
    real_execute = AsyncSession.execute
    fired = False

    async def verify_elsewhere() -> None:
        async with TestSessionLocal() as other:
            row = (
                await other.execute(
                    select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr_id)
                )
            ).scalar_one()
            row.verification_status = VerificationStatus.VERIFIED
            row.verified_by_id = reviewer_user.id
            await other.commit()

    async def execute_after_verify(
        self: AsyncSession, statement: Any, *args: Any, **kw: Any
    ) -> Any:
        # 編集の処理が施錠を確認した後、書き込みの直前に、別の検証者の検証が確定する状況を作る。
        nonlocal fired
        if not fired and isinstance(statement, Update):
            fired = True
            await verify_elsewhere()
        return await real_execute(self, statement, *args, **kw)

    with monkeypatch.context() as patch:
        patch.setattr(AsyncSession, "execute", execute_after_verify)
        resp = await collector_client.post(
            f"/responses/{qr_id}/edit",
            data={"q1": "検証後の改変"},
            headers={"X-CSRF-Token": csrf_token},
            follow_redirects=False,
        )
    assert fired
    saved = await _load(qr_id)
    assert resp.status_code != 303, resp.text[:200]
    assert saved.verification_status == VerificationStatus.VERIFIED
    assert saved.content == original_content
    assert "検証後の改変" not in str(saved.content)
    assert "edited" not in await _actions(qr_id)


@pytest.mark.asyncio
async def test_a_verified_response_cannot_be_edited(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    _, qr = await _make_response(
        db_session,
        submitted_by_id=collector_user.id,
        verification_status=VerificationStatus.VERIFIED,
    )
    resp = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={"q1": "値"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert (await _load(qr.id)).last_edited_by_id is None
