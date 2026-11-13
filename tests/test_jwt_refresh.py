"""JWTリフレッシュトークンのテスト。

- ログインでリフレッシュトークンが Cookie 発行＆DB保存される
- /token/refresh で新アクセストークンが発行され、ローテーション（旧失効・新発行）される
- 失効/不正なリフレッシュトークンは 401
- CSRF なしの /token/refresh は 403
- ログアウトでリフレッシュトークンが失効する
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.config import settings
from kawasemi.models.refresh_token import RefreshToken
from kawasemi.models.user import User
from kawasemi.web.auth_routes import limiter
from tests.e2e.helpers import extract_form_csrf_token

_PW = "AdminPass123!"


async def _login(client: AsyncClient, username: str = "admin_test", password: str = _PW) -> str:
    """ログインして csrf raw トークンを返す（以降の POST 用）。"""
    limiter.reset()
    page = await client.get("/login")
    token = extract_form_csrf_token(page.text)
    resp = await client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 303
    return token


@pytest.mark.asyncio
async def test_login_issues_refresh_token(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """ログインでリフレッシュ Cookie が発行され、DB に未失効の1件が保存される。"""
    await _login(client)
    assert client.cookies.get(settings.jwt_refresh_cookie_name) is not None

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == admin_user.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].revoked is False


@pytest.mark.asyncio
async def test_refresh_rotates_and_issues_new_access(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """/token/refresh は新アクセスを発行し、旧リフレッシュを失効・新規発行（ローテーション）。"""
    token = await _login(client)
    old_refresh = client.cookies.get(settings.jwt_refresh_cookie_name)

    resp = await client.post(
        "/token/refresh",
        data={"csrf_token": token},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    # 新しいアクセス／リフレッシュ Cookie が設定される
    assert resp.cookies.get(settings.jwt_cookie_name) is not None
    new_refresh = resp.cookies.get(settings.jwt_refresh_cookie_name)
    assert new_refresh is not None and new_refresh != old_refresh

    # DB: 旧トークンは失効、未失効はちょうど1件（新トークン）
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == admin_user.id)
    )
    rows = result.scalars().all()
    assert len(rows) == 2
    assert sum(1 for r in rows if not r.revoked) == 1


@pytest.mark.asyncio
async def test_refresh_rejects_revoked_token(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """失効済みリフレッシュトークンでは 401。"""
    token = await _login(client)
    # DB 上の当該トークンを失効させる
    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == admin_user.id)
    )
    rt = result.scalars().one()
    rt.revoked = True
    await db_session.commit()

    resp = await client.post(
        "/token/refresh",
        data={"csrf_token": token},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_without_csrf_rejected(client: AsyncClient, admin_user: User) -> None:
    """CSRF なしの /token/refresh は 403。"""
    await _login(client)
    resp = await client.post("/token/refresh")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_refresh_token_not_usable_as_access_token(
    client: AsyncClient, admin_user: User
) -> None:
    """リフレッシュトークンを認証Cookieに入れても認証されない（失効迂回の防止）。"""
    await _login(client)
    refresh = client.cookies.get(settings.jwt_refresh_cookie_name)
    # リフレッシュトークンを access Cookie に詰めて admin 専用ルートへ
    client.cookies.set(settings.jwt_cookie_name, refresh)
    resp = await client.get("/admin/users")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_refresh_reuse_triggers_family_revocation(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """ローテ済み（失効）トークンの再利用検知で、当該ユーザーの全トークンを失効させる。"""
    token = await _login(client)
    old_refresh = client.cookies.get(settings.jwt_refresh_cookie_name)

    # 正常ローテーション（old_refresh を revoke、新トークン発行）
    resp1 = await client.post(
        "/token/refresh", data={"csrf_token": token}, headers={"X-CSRF-Token": token}
    )
    assert resp1.status_code == 200

    # 盗まれた old_refresh を再提示（再利用）→ 401 かつ家系失効
    client.cookies.set(settings.jwt_refresh_cookie_name, old_refresh)
    resp2 = await client.post(
        "/token/refresh", data={"csrf_token": token}, headers={"X-CSRF-Token": token}
    )
    assert resp2.status_code == 401

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == admin_user.id)
    )
    rows = result.scalars().all()
    # 全トークンが失効（家系失効）
    assert all(r.revoked for r in rows)


@pytest.mark.asyncio
async def test_logout_revokes_refresh_token(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """ログアウトでリフレッシュトークンが失効する。"""
    csrf = await _login(client)
    resp = await client.post("/logout", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 303

    result = await db_session.execute(
        select(RefreshToken).where(RefreshToken.user_id == admin_user.id)
    )
    rt = result.scalars().one()
    assert rt.revoked is True
