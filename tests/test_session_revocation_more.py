"""発行済みトークンの失効のテスト（続き）: 無効化・役割変更・リフレッシュの競合・CLI。

`tests/test_session_revocation.py` の続き。Claude Security F20 / F23 のレビューで出た
取りこぼし（無効化を取り消したときの復活、役割の変更、ローテーション最中の再設定、
E2E のパスワード入れ替え）を押さえる。
"""

import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.e2e.seed import seed_users
from kawasemi.api.main import app
from kawasemi.cli import _reset_e2e_passwords
from kawasemi.core.config import settings
from kawasemi.core.sessions import revoke_user_sessions
from kawasemi.models.refresh_token import RefreshToken
from kawasemi.models.role import Role
from kawasemi.models.user import User
from kawasemi.web import auth_routes
from kawasemi.web.auth_routes import limiter
from tests.e2e.helpers import extract_form_csrf_token

_PAGE = "/datasets"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _real_login(client: AsyncClient, username: str, password: str) -> str:
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
    return (await client.get(_PAGE, follow_redirects=False)).status_code == 200


async def _admin_edit(
    admin_client: AsyncClient,
    user: User,
    csrf_token: str,
    *,
    active: bool = True,
    roles: list[int] | None = None,
):
    data: dict[str, object] = {
        "username": user.username,
        "email": user.email,
        "full_name": user.full_name,
        "password": "",
        "csrf_token": csrf_token,
    }
    if active:
        data["is_active"] = "on"
    if roles is not None:
        data["roles"] = [str(r) for r in roles]
    return await admin_client.post(
        f"/admin/users/{user.id}/edit", data=data, headers={"X-CSRF-Token": csrf_token}
    )


async def _refresh_rows(db: AsyncSession, user_id: int) -> list[RefreshToken]:
    result = await db.execute(select(RefreshToken).where(RefreshToken.user_id == user_id))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_deactivate_then_reactivate_does_not_revive_old_tokens(
    admin_client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """L3: 無効化して有効に戻しても、無効化の前に発行されたトークンは生き返らない。"""
    async with _new_client() as victim:
        await _real_login(victim, "collector_test", "Collector123!")
        assert await _can_open(victim)

        resp = await _admin_edit(
            admin_client, collector_user, csrf_token, active=False, roles=[collector_role.id]
        )
        assert resp.status_code == 303
        assert not await _can_open(victim)

        resp = await _admin_edit(
            admin_client, collector_user, csrf_token, active=True, roles=[collector_role.id]
        )
        assert resp.status_code == 303
        assert not await _can_open(victim), "有効に戻したら古いトークンが生き返った"

    rows = await _refresh_rows(db_session, collector_user.id)
    assert rows and all(r.revoked for r in rows)


@pytest.mark.asyncio
async def test_role_change_revokes_old_tokens(
    admin_client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    reviewer_role: Role,
    csrf_token: str,
) -> None:
    """L3: 役割を変えたら、変更前のトークンは使えない（戻しても生き返らない）。"""
    async with _new_client() as victim:
        await _real_login(victim, "collector_test", "Collector123!")
        assert await _can_open(victim)
        await _admin_edit(
            admin_client, collector_user, csrf_token, roles=[collector_role.id, reviewer_role.id]
        )
        assert not await _can_open(victim)
        await _admin_edit(admin_client, collector_user, csrf_token, roles=[collector_role.id])
        assert not await _can_open(victim)


@pytest.mark.asyncio
async def test_refresh_leaves_no_live_token_when_a_reset_lands_mid_rotation(
    client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1: ローテーションの最中に再設定が入っても、生き残るリフレッシュトークンは残らない。"""
    csrf = await _real_login(client, "collector_test", "Collector123!")
    original = auth_routes._issue_refresh_token

    async def racing_issue(user_id: int, db: AsyncSession) -> str:
        # 管理者の再設定が、新しいトークンの発行の直前にコミットされた状況。再設定の
        # UPDATE は、これから入る新しい行を失効できない
        row = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        await revoke_user_sessions(db, row)
        await db.commit()
        return await original(user_id, db)

    monkeypatch.setattr(auth_routes, "_issue_refresh_token", racing_issue)
    resp = await client.post("/token/refresh", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 401
    assert settings.jwt_cookie_name not in resp.headers.get("set-cookie", "")

    live = [r for r in await _refresh_rows(db_session, collector_user.id) if not r.revoked]
    assert live == [], "再設定のあとに生きているリフレッシュトークンが残っている"


@pytest.mark.asyncio
async def test_login_leaves_no_live_token_when_a_reset_lands_mid_login(
    client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """L1: 認証の直後（発行の最中）に再設定が入ったログインは、失敗として返し何も残さない。"""
    original = auth_routes._issue_refresh_token

    async def racing_issue(user_id: int, db: AsyncSession) -> str:
        row = (await db.execute(select(User).where(User.id == user_id))).scalar_one()
        await revoke_user_sessions(db, row)
        await db.commit()
        return await original(user_id, db)

    monkeypatch.setattr(auth_routes, "_issue_refresh_token", racing_issue)
    limiter.reset()
    page = await client.get("/login")
    token = extract_form_csrf_token(page.text)
    resp = await client.post(
        "/login",
        data={"username": "collector_test", "password": "Collector123!", "csrf_token": token},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 200  # 303 で入れてしまわない
    assert settings.jwt_cookie_name not in resp.headers.get("set-cookie", "")
    live = [r for r in await _refresh_rows(db_session, collector_user.id) if not r.revoked]
    assert live == []


def test_e2e_password_reset_bumps_token_version(tmp_path: Path) -> None:
    """L4: E2E の app.db のパスワードを入れ替えるときも、発行済みトークンを無効にする。"""
    db = tmp_path / "app.db"
    names = list(seed_users())
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT, "
            "token_version INTEGER NOT NULL DEFAULT 0)"
        )
        con.execute(
            "CREATE TABLE refresh_tokens (id INTEGER PRIMARY KEY, user_id INTEGER, "
            "revoked INTEGER NOT NULL DEFAULT 0)"
        )
        for i, name in enumerate(names, start=1):
            con.execute("INSERT INTO users VALUES (?, ?, 'old', 0)", (i, name))
            con.execute("INSERT INTO refresh_tokens (user_id) VALUES (?)", (i,))
    _reset_e2e_passwords(db)
    with sqlite3.connect(db) as con:
        assert {r[0] for r in con.execute("SELECT token_version FROM users")} == {1}
        assert {r[0] for r in con.execute("SELECT revoked FROM refresh_tokens")} == {1}
        assert all(r[0] != "old" for r in con.execute("SELECT password_hash FROM users"))


def test_e2e_password_reset_tolerates_an_older_schema(tmp_path: Path) -> None:
    """token_version 列の無い古い実行結果の app.db でも、パスワードの入れ替えは落ちない。"""
    db = tmp_path / "app.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT)"
        )
        for i, name in enumerate(seed_users(), start=1):
            con.execute("INSERT INTO users VALUES (?, ?, 'old')", (i, name))
    _reset_e2e_passwords(db)
    with sqlite3.connect(db) as con:
        assert all(r[0] != "old" for r in con.execute("SELECT password_hash FROM users"))
