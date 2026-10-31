"""アカウントロック機構のテスト。

- 連続ログイン失敗で locked_until が設定される（監査 AuthEvent=locked 記録）
- ロック中は正しいパスワードでもログイン不可
- ロック期限切れは自動解除され、成功時にカウントがリセットされる
- admin がロック解除でき、監査 AuthEvent=unlocked（actor_id 付き）が残る
- 認証成功で失敗カウントがリセットされる

レート制限（5回/分, IP単位）とロック（アカウント単位）は別機構。テストでは
lockout_max_attempts を小さくし、各試行前に limiter.reset() で 429 干渉を避ける。
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.config import settings
from kawasemi.models.auth_event import AuthEvent, AuthEventAction
from kawasemi.models.user import User
from kawasemi.web.auth_routes import limiter
from tests.e2e.helpers import extract_form_csrf_token

_PW = "AdminPass123!"


async def _attempt_login(client: AsyncClient, username: str, password: str) -> object:
    """GET /login で CSRF を取り、POST /login を1回試行する（rate limit を都度リセット）。"""
    limiter.reset()
    page = await client.get("/login")
    token = extract_form_csrf_token(page.text)
    return await client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        headers={"X-CSRF-Token": token},
    )


async def _count_events(db: AsyncSession, user_id: int, action: str) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(AuthEvent)
        .where(AuthEvent.user_id == user_id, AuthEvent.action == action)
    )
    return int(result.scalar_one())


@pytest.mark.asyncio
async def test_account_locks_after_max_failures(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, monkeypatch
) -> None:
    """閾値回数の連続失敗で locked_until が設定され、監査に locked が残る。"""
    monkeypatch.setattr(settings, "lockout_max_attempts", 3)

    for _ in range(3):
        await _attempt_login(client, "admin_test", "wrong-pass")

    await db_session.refresh(admin_user)
    assert admin_user.failed_login_count == 3
    assert admin_user.locked_until is not None
    assert admin_user.is_locked()
    assert await _count_events(db_session, admin_user.id, AuthEventAction.LOCKED) == 1


@pytest.mark.asyncio
async def test_locked_account_rejects_correct_password(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """ロック中は正しいパスワードでもログインできない（Cookie も発行されない）。"""
    admin_user.locked_until = datetime.now(UTC) + timedelta(minutes=30)
    admin_user.failed_login_count = 5
    await db_session.commit()

    resp = await _attempt_login(client, "admin_test", _PW)
    assert resp.status_code == 200
    assert "ロック" in resp.text
    assert settings.jwt_cookie_name not in resp.cookies


@pytest.mark.asyncio
async def test_lock_auto_expires_and_allows_login(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """ロック期限切れは自動解除され、正しいパスワードでログイン成功＆カウントリセット。"""
    admin_user.locked_until = datetime.now(UTC) - timedelta(minutes=1)
    admin_user.failed_login_count = 5
    await db_session.commit()

    resp = await _attempt_login(client, "admin_test", _PW)
    assert resp.status_code == 303
    assert resp.cookies.get(settings.jwt_cookie_name) is not None

    await db_session.refresh(admin_user)
    assert admin_user.locked_until is None
    assert admin_user.failed_login_count == 0
    # 時間経過による自動解除が監査に残る
    assert await _count_events(db_session, admin_user.id, AuthEventAction.UNLOCKED) == 1


@pytest.mark.asyncio
async def test_successful_login_resets_failed_count(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """ロック前でも、認証成功で失敗カウントが 0 に戻る。"""
    admin_user.failed_login_count = 2
    await db_session.commit()

    resp = await _attempt_login(client, "admin_test", _PW)
    assert resp.status_code == 303
    await db_session.refresh(admin_user)
    assert admin_user.failed_login_count == 0


@pytest.mark.asyncio
async def test_admin_can_unlock_account(
    admin_client: AsyncClient, admin_user: User, db_session: AsyncSession, csrf_token: str
) -> None:
    """admin がロック解除でき、監査に unlocked（actor_id 付き）が残る。"""
    admin_user.locked_until = datetime.now(UTC) + timedelta(minutes=30)
    admin_user.failed_login_count = 5
    await db_session.commit()

    resp = await admin_client.post(
        f"/admin/users/{admin_user.id}/unlock",
        data={"csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 303

    await db_session.refresh(admin_user)
    assert admin_user.locked_until is None
    assert admin_user.failed_login_count == 0

    result = await db_session.execute(
        select(AuthEvent).where(
            AuthEvent.user_id == admin_user.id, AuthEvent.action == AuthEventAction.UNLOCKED
        )
    )
    ev = result.scalar_one()
    assert ev.actor_id == admin_user.id  # admin_client の操作者


@pytest.mark.asyncio
async def test_nonexistent_user_does_not_create_events(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """存在しないユーザーへの失敗は誰もロックせず、監査イベントも作らない。"""
    monkeypatch.setattr(settings, "lockout_max_attempts", 2)
    for _ in range(3):
        await _attempt_login(client, "ghost_user", "whatever")
    total = await db_session.execute(select(func.count()).select_from(AuthEvent))
    assert int(total.scalar_one()) == 0
