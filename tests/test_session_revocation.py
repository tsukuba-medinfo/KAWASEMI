"""発行済みトークンの失効のテスト（Claude Security F20 / F23 / F25）。

- F20: 管理者がパスワードを再設定したら、そのユーザーの発行済みのリフレッシュトークンと
  アクセストークンはすべて使えなくなる
- F23: アクセストークンは世代番号（tv クレーム）で取り消せる。/switch-role は有効期限を延ばさない
- F25: ログアウトは POST（CSRF 検証あり）。GET では何も起きない

アクセストークンの `tv` が無いもの（世代導入前に発行）は 0 として扱うので、`create_access_token` に
`tv` を載せない既存テストの認証済みクライアントは、世代 0 のユーザーでそのまま通る。
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.api.main import app
from kawasemi.core.auth import create_access_token, decode_access_token
from kawasemi.core.config import settings
from kawasemi.models.refresh_token import RefreshToken
from kawasemi.models.user import User
from kawasemi.web.auth_routes import limiter
from tests.e2e.helpers import extract_form_csrf_token

_PAGE = "/datasets"  # 管理者・収集者・閲覧検証者が開ける画面（認証済みなら 200）


def _new_client() -> AsyncClient:
    """DB を差し替え済みの app へつなぐ別のクライアント（別の利用者・別の端末役）。"""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _real_login(client: AsyncClient, username: str, password: str) -> str:
    """実際に /login して Cookie（アクセス＋リフレッシュ）を得る。CSRF の raw トークンを返す。"""
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


async def _can_open(client: AsyncClient) -> bool:
    resp = await client.get(_PAGE, follow_redirects=False)
    return resp.status_code == 200


# ===== F20: パスワード再設定 =====


@pytest.mark.asyncio
async def test_password_reset_revokes_refresh_and_access_tokens(
    admin_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """F20/F23: 再設定の前に盗まれた Cookie は、再設定のあと更新も使用もできない。"""
    async with _new_client() as victim, _new_client() as thief:
        victim_csrf = await _real_login(victim, "collector_test", "Collector123!")
        assert await _can_open(victim)
        # 盗まれた Cookie（アクセス＋リフレッシュ）を別の端末へ写す
        stolen_access = victim.cookies.get(settings.jwt_cookie_name)
        stolen_refresh = victim.cookies.get(settings.jwt_refresh_cookie_name)
        assert stolen_access and stolen_refresh
        thief.cookies.set(settings.jwt_cookie_name, stolen_access)
        thief.cookies.set(settings.jwt_refresh_cookie_name, stolen_refresh)
        thief.cookies.set("kawasemi_csrf_token", victim.cookies.get("kawasemi_csrf_token") or "")
        assert await _can_open(thief)

        # 管理者がパスワードを再設定する
        resp = await admin_client.post(
            f"/admin/users/{collector_user.id}/edit",
            data={
                "username": collector_user.username,
                "email": collector_user.email,
                "full_name": collector_user.full_name,
                "password": "NewPassword123!",
                "is_active": "on",
                "csrf_token": csrf_token,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 303

        # 本人のセッションも、盗まれた Cookie も、期限前のアクセストークンが使えない
        assert not await _can_open(victim)
        assert not await _can_open(thief)
        # リフレッシュも失効している（更新で新しいアクセストークンを得られない）
        refreshed = await thief.post("/token/refresh", headers={"X-CSRF-Token": victim_csrf})
        assert refreshed.status_code == 401

    rows = (
        (
            await db_session.execute(
                select(RefreshToken).where(RefreshToken.user_id == collector_user.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows and all(r.revoked for r in rows)
    await db_session.refresh(collector_user)
    assert collector_user.token_version == 1


@pytest.mark.asyncio
async def test_edit_without_a_password_keeps_sessions(
    admin_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """パスワードを変えない編集では、利用者のセッションを切らない。"""
    async with _new_client() as victim:
        await _real_login(victim, "collector_test", "Collector123!")
        resp = await admin_client.post(
            f"/admin/users/{collector_user.id}/edit",
            data={
                "username": collector_user.username,
                "email": collector_user.email,
                "full_name": "改名",
                "password": "",
                "is_active": "on",
                "csrf_token": csrf_token,
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 303
        assert await _can_open(victim)
    await db_session.refresh(collector_user)
    assert collector_user.token_version == 0


# ===== F23: 世代番号 =====


@pytest.mark.asyncio
async def test_access_token_generation_is_enforced(
    client: AsyncClient, collector_user: User, db_session: AsyncSession
) -> None:
    """世代が合わないトークン（古い・不正な型）は使えない。`tv` の無いものは 0 として通る。"""
    sub = str(collector_user.id)
    client.cookies.set(settings.jwt_cookie_name, create_access_token(data={"sub": sub}))
    assert await _can_open(client)  # tv なし＝0 ＝ ユーザーの世代 0

    collector_user.token_version = 3
    await db_session.commit()
    assert not await _can_open(client)  # 世代が進んだので、tv なしの古いトークンは無効

    for bad in (2, 4, "3", True, None):
        client.cookies.set(
            settings.jwt_cookie_name, create_access_token(data={"sub": sub, "tv": bad})
        )
        assert not await _can_open(client), f"tv={bad!r} が通った"

    client.cookies.set(settings.jwt_cookie_name, create_access_token(data={"sub": sub, "tv": 3}))
    assert await _can_open(client)


@pytest.mark.asyncio
async def test_switch_role_does_not_extend_the_token_lifetime(
    client: AsyncClient, multi_role_user: User, csrf_token: str
) -> None:
    """F23: 役割切替で発行されるトークンの期限は、元のトークンの期限のまま（延びない）。"""
    short = timedelta(minutes=5)
    old = create_access_token(data={"sub": str(multi_role_user.id), "tv": 0}, expires_delta=short)
    client.cookies.set(settings.jwt_cookie_name, old)
    old_exp = decode_access_token(old)["exp"]  # type: ignore[index]

    resp = await client.post(
        "/switch-role",
        data={"role": "collector", "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    new = resp.cookies.get(settings.jwt_cookie_name)
    assert new
    payload = decode_access_token(new)
    assert payload is not None
    assert payload["act"] == "collector"
    assert payload["tv"] == 0
    # 期限は元のまま（秒の丸めの1秒だけ許す）。新規発行なら 24 時間先になる
    assert abs(payload["exp"] - old_exp) <= 1
    assert datetime.fromtimestamp(payload["exp"], UTC) < datetime.now(UTC) + timedelta(minutes=6)


@pytest.mark.asyncio
async def test_switch_role_refuses_a_revoked_token(
    client: AsyncClient, multi_role_user: User, db_session: AsyncSession, csrf_token: str
) -> None:
    """失効済み（世代が古い）のトークンでは、役割切替で新しいトークンを得られない。"""
    client.cookies.set(
        settings.jwt_cookie_name,
        create_access_token(data={"sub": str(multi_role_user.id), "tv": 0}),
    )
    multi_role_user.token_version = 1
    await db_session.commit()

    resp = await client.post(
        "/switch-role",
        data={"role": "collector", "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert settings.jwt_cookie_name not in resp.cookies


# ===== F25: ログアウト =====


@pytest.mark.asyncio
async def test_get_logout_changes_nothing(
    client: AsyncClient, collector_user: User, db_session: AsyncSession
) -> None:
    """GET /logout はログアウトしない（外部ページからの遷移で勝手にログアウトさせられない）。"""
    async with _new_client() as c:
        await _real_login(c, "collector_test", "Collector123!")
        resp = await c.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert settings.jwt_cookie_name not in resp.headers.get("set-cookie", "")
        assert await _can_open(c)  # まだログイン中

    rows = (
        (
            await db_session.execute(
                select(RefreshToken).where(RefreshToken.user_id == collector_user.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows and not any(r.revoked for r in rows)
    await db_session.refresh(collector_user)
    assert collector_user.token_version == 0


@pytest.mark.asyncio
async def test_post_logout_requires_csrf(
    client: AsyncClient, collector_user: User, db_session: AsyncSession
) -> None:
    """CSRF トークンの無い POST /logout は拒否され、何も失効しない。"""
    async with _new_client() as c:
        await _real_login(c, "collector_test", "Collector123!")
        resp = await c.post("/logout", follow_redirects=False)
        assert resp.status_code == 403
        assert await _can_open(c)
    await db_session.refresh(collector_user)
    assert collector_user.token_version == 0


@pytest.mark.asyncio
async def test_post_logout_revokes_every_token_of_the_user(
    client: AsyncClient, collector_user: User, db_session: AsyncSession
) -> None:
    """ログアウトで、リフレッシュトークンは全件失効し、控えておいたアクセストークンも使えなくなる。"""
    async with _new_client() as c, _new_client() as replay:
        csrf = await _real_login(c, "collector_test", "Collector123!")
        replay.cookies.set(settings.jwt_cookie_name, c.cookies.get(settings.jwt_cookie_name) or "")
        assert await _can_open(replay)

        resp = await c.post("/logout", headers={"X-CSRF-Token": csrf}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"].endswith("/login")

        assert not await _can_open(c)
        assert not await _can_open(replay), "ログアウト後も控えたアクセストークンが使える"

    rows = (
        (
            await db_session.execute(
                select(RefreshToken).where(RefreshToken.user_id == collector_user.id)
            )
        )
        .scalars()
        .all()
    )
    assert rows and all(r.revoked for r in rows)
    await db_session.refresh(collector_user)
    assert collector_user.token_version == 1


@pytest.mark.asyncio
async def test_logout_only_touches_the_owner_of_the_verified_cookie(
    client: AsyncClient, collector_user: User, reviewer_user: User, db_session: AsyncSession
) -> None:
    """ログアウトが失効するのは、署名を検証した Cookie の持ち主だけ。ほかの利用者には及ばない。"""
    async with _new_client() as a, _new_client() as b:
        csrf_a = await _real_login(a, "collector_test", "Collector123!")
        await _real_login(b, "reviewer_test", "Reviewer123!")
        await a.post("/logout", headers={"X-CSRF-Token": csrf_a})
        assert await _can_open(b)
    await db_session.refresh(reviewer_user)
    assert reviewer_user.token_version == 0
