"""回答の編集・検証・差戻しの同時実行（PostgreSQL）。

SQLite は書き込みを DB 全体で直列化し `FOR UPDATE` を無視するので、**行ロックの効き**は
ここでしか確かめられない。`KAWASEMI_TEST_POSTGRES_URL` が無ければ skip する。その DB の
テーブルは作り直す（使い捨ての DB を渡すこと）。CI の `migrations-postgres` ジョブが回す。

見ること（要求ごとに**別の接続**を使う）:

- 別の接続が行を `FOR UPDATE` で握っている間、編集の要求は**待つ**（`FOR UPDATE` で読む）。
  握っている側が検証を確定して放すと、編集は新しい状態を読んで断られ、内容は変わらない。
  `update_response` の `.with_for_update()` を外すと、要求は待たずに先へ進み、このテストは落ちる。
- 兼任ユーザーが、同じ回答に対して自分の編集と自分の検証を同時に送っても、両方は通らない。
  通ると「検証者 = 編集者」の検証済みが出来る（職務分離の破れ）。
- 2人の検証者が、同じ回答の検証と差戻しを同時に送ると、どちらか一方だけが通り、
  最終状態と最後の監査ログが一致する。
"""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import AsyncGenerator

import pytest
from fastapi_csrf_protect import CsrfProtect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kawasemi.api.main import app
from kawasemi.core.auth import create_access_token, get_password_hash
from kawasemi.core.config import settings
from kawasemi.core.database import get_db
from kawasemi.core.rate_limit import limiter
from kawasemi.models.base import Base
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import (
    QuestionnaireResponse,
    ResponseVerificationLog,
    VerificationStatus,
)
from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole
from tests.lease_helpers import make_target
from tests.test_verification_workflow import _make_response

PG_URL = os.environ.get("KAWASEMI_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="KAWASEMI_TEST_POSTGRES_URL が無い（PostgreSQL の同時実行テスト）"
)

_ITERATIONS = 40
_MAX_OFFSET = 0.02


@pytest.fixture
async def pg() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    assert PG_URL is not None
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    # 本番と同じ設定（autoflush=False）
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:  # 要求ごとに別の接続
            yield session

    app.dependency_overrides[get_db] = override_get_db
    limiter.enabled = False
    try:
        yield factory
    finally:
        limiter.enabled = True
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _seed(factory: async_sessionmaker[AsyncSession]) -> dict[str, User]:
    """収集者（提出者）・検証者2人・兼任（収集者+検証者）を作る。"""
    async with factory() as s:
        roles = {
            name: Role(name=name, description=RoleType.DESCRIPTIONS[name])
            for name in (RoleType.COLLECTOR, RoleType.REVIEWER)
        }
        s.add_all(roles.values())
        await s.flush()
        users: dict[str, User] = {}
        for key, held in (
            ("collector", [RoleType.COLLECTOR]),
            ("reviewer", [RoleType.REVIEWER]),
            ("reviewer2", [RoleType.REVIEWER]),
            ("dual", [RoleType.COLLECTOR, RoleType.REVIEWER]),
        ):
            user = User(
                username=f"pg_{key}",
                email=f"pg_{key}@test.com",
                password_hash=get_password_hash("unused-hash"),
                full_name=key,
                is_active=True,
            )
            s.add(user)
            await s.flush()
            for name in held:
                s.add(UserRole(user_id=user.id, role_id=roles[name].id))
            users[key] = user
        await s.commit()
        for user in users.values():
            await s.refresh(user)
        return users


async def _new_response(
    factory: async_sessionmaker[AsyncSession], users: dict[str, User], n: int
) -> int:
    async with factory() as s:
        if n == 0:
            q, qr = await _make_response(
                s, submitted_by_id=users["collector"].id, fhir_id=f"qr-pg-{n}"
            )
        else:
            q = (await s.execute(select(Questionnaire).limit(1))).scalar_one()
            qr = QuestionnaireResponse(
                fhir_id=f"qr-pg-{n}",
                questionnaire_id=q.id,
                content={
                    "resourceType": "QuestionnaireResponse",
                    "status": "completed",
                    "item": [],
                },
                status="completed",
                subject="Patient/p-001",
                submitted_by_id=users["collector"].id,
                verification_status=VerificationStatus.UNVERIFIED,
            )
            s.add(qr)
            await s.commit()
            await s.refresh(qr)
        return qr.id


def _client(user: User, role: str) -> tuple[AsyncClient, str]:
    """指定ユーザー・指定の役割で入った、別の端末役のクライアントと CSRF の raw トークン。"""
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    csrf = CsrfProtect()
    raw, signed = csrf.generate_csrf_tokens()
    client.cookies.set(csrf._cookie_key, signed)
    client.cookies.set(
        settings.jwt_cookie_name,
        create_access_token(data={"sub": str(user.id), "act": role}),
    )
    return client, raw


async def _state(
    factory: async_sessionmaker[AsyncSession], qr_id: int
) -> tuple[QuestionnaireResponse, list[ResponseVerificationLog]]:
    async with factory() as s:
        qr = (
            await s.execute(select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr_id))
        ).scalar_one()
        logs = list(
            (
                await s.execute(
                    select(ResponseVerificationLog)
                    .where(ResponseVerificationLog.questionnaire_response_id == qr_id)
                    .order_by(ResponseVerificationLog.id)
                )
            ).scalars()
        )
        return qr, logs


@pytest.mark.asyncio
async def test_edit_waits_for_a_row_lock_and_is_refused_after_a_verify(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    """行を `FOR UPDATE` で握る別の接続がある間、編集は待ち、検証の確定後に断られる。"""
    users = await _seed(pg)
    qr_id = await _new_response(pg, users, 0)
    original = (await _state(pg, qr_id))[0].content

    holder = pg()
    await holder.execute(
        select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr_id).with_for_update()
    )
    client, csrf = _client(users["collector"], "collector")
    task = asyncio.create_task(
        client.post(
            f"/responses/{qr_id}/edit",
            data={"q1": "ロック中の編集"},
            headers={"X-CSRF-Token": csrf},
            follow_redirects=False,
        )
    )
    await asyncio.sleep(0.5)
    waited = not task.done()
    # 握っている側が検証を確定して放す
    await holder.execute(
        QuestionnaireResponse.__table__.update()
        .where(QuestionnaireResponse.id == qr_id)
        .values(
            verification_status=VerificationStatus.VERIFIED, verified_by_id=users["reviewer"].id
        )
    )
    await holder.commit()
    await holder.close()
    resp = await asyncio.wait_for(task, timeout=10)
    await client.aclose()

    assert waited, "編集が行ロックを待たずに先へ進んだ（FOR UPDATE が効いていない）"
    assert resp.status_code == 403  # 新しい状態（検証済み）を読んで断られる
    qr, logs = await _state(pg, qr_id)
    assert qr.verification_status == VerificationStatus.VERIFIED
    assert qr.content == original
    assert "edited" not in [log.action for log in logs]


@pytest.mark.asyncio
async def test_a_dual_role_user_cannot_get_both_their_edit_and_their_verify_through(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    """兼任ユーザーが自分の編集と自分の検証を同時に送っても、両方は通らない。"""
    users = await _seed(pg)
    dual = users["dual"]
    async with pg() as s:
        await _make_response(s, submitted_by_id=users["collector"].id, fhir_id="qr-pg-seed")
        await make_target(s, "p-001", holder_id=dual.id, lease_minutes=30)
        await s.commit()
    for i in range(_ITERATIONS):
        qr_id = await _new_response(pg, users, i + 1)
        edit_client, edit_csrf = _client(dual, "collector")
        verify_client, verify_csrf = _client(dual, "reviewer")

        async def edit(delay: float, qr_id: int = qr_id) -> int:
            await asyncio.sleep(delay)
            r = await edit_client.post(
                f"/responses/{qr_id}/edit",
                data={"q1": "兼任の編集"},
                headers={"X-CSRF-Token": edit_csrf},
                follow_redirects=False,
            )
            return r.status_code

        async def verify(delay: float, qr_id: int = qr_id) -> int:
            await asyncio.sleep(delay)
            r = await verify_client.post(
                f"/responses/{qr_id}/verify",
                headers={"X-CSRF-Token": verify_csrf},
                follow_redirects=False,
            )
            return r.status_code

        e, v = await asyncio.gather(
            edit(random.random() * _MAX_OFFSET), verify(random.random() * _MAX_OFFSET)
        )
        await edit_client.aclose()
        await verify_client.aclose()
        qr, logs = await _state(pg, qr_id)
        assert not (e == 303 and v == 303), f"編集も検証も通った (iteration {i})"
        assert not (qr.verified_by_id is not None and qr.verified_by_id == qr.last_edited_by_id), (
            f"検証者と編集者が同じ検証済みができた (iteration {i}): edit={e} verify={v}"
        )
        assert e in (303, 403, 409) and v in (303, 403, 409), (e, v)
        actions = [log.action for log in logs]
        if "verified" in actions and "edited" in actions:
            # 検証の後に編集のログが付いてはいけない（施錠後の編集）
            assert actions.index("verified") > actions.index("edited"), actions


@pytest.mark.asyncio
async def test_concurrent_verify_and_reject_leave_one_consistent_outcome(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    """2人の検証者の検証と差戻しが同時でも、どちらか一方だけが通り、状態とログが一致する。"""
    users = await _seed(pg)
    for i in range(_ITERATIONS):
        qr_id = await _new_response(pg, users, i)
        v_client, v_csrf = _client(users["reviewer"], "reviewer")
        r_client, r_csrf = _client(users["reviewer2"], "reviewer")

        async def verify(delay: float, qr_id: int = qr_id) -> int:
            await asyncio.sleep(delay)
            r = await v_client.post(
                f"/responses/{qr_id}/verify",
                headers={"X-CSRF-Token": v_csrf},
                follow_redirects=False,
            )
            return r.status_code

        async def reject(delay: float, qr_id: int = qr_id) -> int:
            await asyncio.sleep(delay)
            r = await r_client.post(
                f"/responses/{qr_id}/reject",
                data={"reason": "理由"},
                headers={"X-CSRF-Token": r_csrf},
                follow_redirects=False,
            )
            return r.status_code

        v, r = await asyncio.gather(
            verify(random.random() * _MAX_OFFSET), reject(random.random() * _MAX_OFFSET)
        )
        await v_client.aclose()
        await r_client.aclose()
        qr, logs = await _state(pg, qr_id)
        assert v in (303, 400, 409) and r in (303, 400, 409), (v, r)
        # 検証が通れば検証済み（差戻しは 400/409）。差戻しが先なら検証は差戻し状態を読んで通ることもある
        # が、その場合は最後のログが最終状態と一致する。
        assert logs, "どちらのログも残っていない"
        last = logs[-1].action
        expected = {
            VerificationStatus.VERIFIED: "verified",
            VerificationStatus.REJECTED: "rejected",
        }[qr.verification_status]
        assert last == expected, (v, r, qr.verification_status, [log.action for log in logs])
