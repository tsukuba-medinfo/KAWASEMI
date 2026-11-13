"""セッションの失効と、リフレッシュ・ログインの同時実行（PostgreSQL）。

SQLite は書き込みを DB 全体で直列化し `FOR UPDATE` を無視するので、**行ロックの順序と効き**は
ここでしか確かめられない。`KAWASEMI_TEST_POSTGRES_URL` が無ければ skip する。その DB の
テーブルは作り直す（使い捨ての DB を渡すこと）。CI の `migrations-postgres` ジョブが、
`tests/ai_input/test_plan_concurrency_postgres.py` と同じ起動で回す。

見ること（要求ごとに**別の接続**を使う。1つのセッションを共有しない）:

- リフレッシュと再設定（`revoke_user_sessions`）を、ずらした時刻で同時に走らせる。
  再設定が commit されたあとに未失効のリフレッシュトークンが残らない／デッドロックしない。
  `revoke_user_sessions` が refresh_tokens を先に UPDATE し世代の +1 を commit まで遅らせる
  古い順序に戻すと、スナップショットの食い違いで生き残るトークンと、users 行と refresh_tokens 行の
  ロック順の逆転によるデッドロック（500）が出て、このテストは落ちる。
- 再設定が users 行を握っている間は、ログインもリフレッシュも待ち、commit のあとに断られる。
- 認証の直後（発行の最中）に再設定が commit されたログインは、トークンを残さず失敗として返る。
"""

from __future__ import annotations

import asyncio
import os
import random
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi_csrf_protect import CsrfProtect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kawasemi.api.main import app
from kawasemi.core.auth import create_refresh_token, get_password_hash
from kawasemi.core.config import settings
from kawasemi.core.database import get_db
from kawasemi.core.login_throttle import login_throttle
from kawasemi.core.rate_limit import limiter
from kawasemi.core.sessions import revoke_user_sessions
from kawasemi.models.base import Base
from kawasemi.models.refresh_token import RefreshToken
from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole
from kawasemi.web import auth_routes

PG_URL = os.environ.get("KAWASEMI_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="KAWASEMI_TEST_POSTGRES_URL が無い（PostgreSQL の同時実行テスト）"
)

_ITERATIONS = 60  # 古い順序だと数回に1回は落ちる回数。全体で数秒
_MAX_OFFSET = 0.03  # 要求と再設定の開始のずれ（秒）


@pytest.fixture
async def pg() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    assert PG_URL is not None
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    # 本番（`core/database.py: AsyncSessionLocal`）と同じ設定。**autoflush=False が肝**で、
    # 既定の True だと、UPDATE の前に保留中の変更が flush され、ロックの順序の問題が隠れる
    factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        # 要求ごとに別の接続（同時実行を本物にする）
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _seed_user(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as s:
        role = Role(name=RoleType.COLLECTOR, description=RoleType.DESCRIPTIONS[RoleType.COLLECTOR])
        user = User(
            username="pg_collector",
            email="pg_collector@test.com",
            password_hash=get_password_hash("unused-hash"),
            full_name="PG 収集者",
            is_active=True,
        )
        s.add_all([role, user])
        await s.flush()
        s.add(UserRole(user_id=user.id, role_id=role.id))
        await s.commit()
        return user.id


async def _live_tokens(factory: async_sessionmaker[AsyncSession], user_id: int) -> list[str]:
    async with factory() as s:
        rows = await s.execute(
            select(RefreshToken.jti).where(
                RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False)
            )
        )
        return list(rows.scalars())


async def _give_refresh_token(factory: async_sessionmaker[AsyncSession], user_id: int) -> str:
    """未失効のリフレッシュトークンを1件入れ、その Cookie 値を返す。"""
    jti = uuid.uuid4().hex
    async with factory() as s:
        s.add(
            RefreshToken(
                jti=jti, user_id=user_id, expires_at=datetime.now(UTC) + timedelta(days=14)
            )
        )
        await s.commit()
    return create_refresh_token(user_id, jti)


async def _reset(factory: async_sessionmaker[AsyncSession], user_id: int, delay: float) -> None:
    """管理者のパスワード再設定と同じ失効（別の接続）。"""
    await asyncio.sleep(delay)
    async with factory() as s:
        user = (await s.execute(select(User).where(User.id == user_id))).scalar_one()
        await revoke_user_sessions(s, user)
        await asyncio.sleep(random.random() * 0.005)
        await s.commit()


def _client() -> tuple[AsyncClient, str]:
    """CSRF のクッキーを確立した、別の端末役のクライアントと raw トークン。"""
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    csrf = CsrfProtect()
    raw, signed = csrf.generate_csrf_tokens()
    client.cookies.set(csrf._cookie_key, signed)
    return client, raw


@pytest.mark.asyncio
async def test_refresh_racing_a_reset_leaves_no_live_token_and_no_deadlock(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    """リフレッシュと再設定を同時に走らせても、再設定の commit 後に生きたトークンは残らず、
    どちらもデッドロックで失敗しない。"""
    user_id = await _seed_user(pg)
    violations: list[str] = []
    errors: list[str] = []
    for i in range(_ITERATIONS):
        cookie = await _give_refresh_token(pg, user_id)
        client, raw = _client()
        client.cookies.set(settings.jwt_refresh_cookie_name, cookie)
        limiter.reset()

        async def refresh(c: AsyncClient = client, token: str = raw) -> None:
            await asyncio.sleep(random.random() * _MAX_OFFSET)
            try:
                resp = await c.post("/token/refresh", headers={"X-CSRF-Token": token})
                if resp.status_code not in (200, 401):
                    errors.append(f"#{i} refresh status {resp.status_code}")
            except Exception as exc:  # デッドロックは 500 の例外として届く
                errors.append(f"#{i} refresh raised {type(exc).__name__}: {str(exc)[:80]}")

        async def reset() -> None:
            try:
                await _reset(pg, user_id, random.random() * _MAX_OFFSET)
            except Exception as exc:
                errors.append(f"#{i} reset raised {type(exc).__name__}: {str(exc)[:80]}")

        await asyncio.gather(refresh(), reset())
        live = await _live_tokens(pg, user_id)
        if live:
            violations.append(f"#{i} 再設定の後に未失効のトークンが残った: {len(live)} 件")
        await client.aclose()

    assert not errors, "\n".join(errors[:5])
    assert not violations, "\n".join(violations[:5])


@pytest.mark.asyncio
async def test_refresh_waits_for_a_reset_in_progress_and_is_then_refused(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    """再設定が users 行を握っている間、リフレッシュは待つ。commit のあとは断られ、何も残らない。"""
    user_id = await _seed_user(pg)
    cookie = await _give_refresh_token(pg, user_id)
    client, raw = _client()
    client.cookies.set(settings.jwt_refresh_cookie_name, cookie)

    async with pg() as rs:
        user = (await rs.execute(select(User).where(User.id == user_id))).scalar_one()
        await revoke_user_sessions(rs, user)  # users 行のロックを握ったまま（commit しない）
        task = asyncio.create_task(client.post("/token/refresh", headers={"X-CSRF-Token": raw}))
        await asyncio.sleep(0.5)
        assert not task.done(), "再設定の最中なのにリフレッシュが待たずに進んだ"
        await rs.commit()
    resp = await asyncio.wait_for(task, timeout=10)
    assert resp.status_code == 401
    assert await _live_tokens(pg, user_id) == []
    await client.aclose()


@pytest.mark.asyncio
async def test_login_waits_for_a_reset_in_progress_and_leaves_no_token(
    pg: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """再設定が users 行を握っている間、ログインはトークンの発行で待ち、commit のあとに
    失敗として返る（再設定の前の状態で認証したログインが、トークンを残さない）。"""
    user_id = await _seed_user(pg)
    monkeypatch.setattr(auth_routes, "verify_password", lambda plain, hashed: plain == "pw-ok")
    client, raw = _client()
    limiter.reset()
    login_throttle.clear()

    async with pg() as rs:
        user = (await rs.execute(select(User).where(User.id == user_id))).scalar_one()
        await revoke_user_sessions(rs, user)
        task = asyncio.create_task(
            client.post(
                "/login",
                data={"username": "pg_collector", "password": "pw-ok", "csrf_token": raw},
                headers={"X-CSRF-Token": raw},
            )
        )
        await asyncio.sleep(0.5)
        assert not task.done(), "再設定の最中なのにログインが待たずに進んだ"
        await rs.commit()
    resp = await asyncio.wait_for(task, timeout=10)
    assert resp.status_code == 200  # 303（成功）ではなく、失敗の画面
    assert settings.jwt_cookie_name not in resp.headers.get("set-cookie", "")
    assert await _live_tokens(pg, user_id) == []
    await client.aclose()


@pytest.mark.asyncio
async def test_login_whose_reset_commits_between_auth_and_issue_leaves_no_token(
    pg: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """認証のあと、トークンの発行の前に再設定が commit された場合も、トークンは残らない。"""
    user_id = await _seed_user(pg)
    monkeypatch.setattr(auth_routes, "verify_password", lambda plain, hashed: plain == "pw-ok")
    original = auth_routes._issue_session_refresh_token
    reached = asyncio.Event()
    release = asyncio.Event()

    async def gated(user: User, db: AsyncSession) -> str | None:
        reached.set()
        await release.wait()
        return await original(user, db)

    monkeypatch.setattr(auth_routes, "_issue_session_refresh_token", gated)
    client, raw = _client()
    limiter.reset()
    login_throttle.clear()
    task = asyncio.create_task(
        client.post(
            "/login",
            data={"username": "pg_collector", "password": "pw-ok", "csrf_token": raw},
            headers={"X-CSRF-Token": raw},
        )
    )
    await asyncio.wait_for(reached.wait(), timeout=10)
    await _reset(pg, user_id, 0)
    release.set()
    resp = await asyncio.wait_for(task, timeout=10)
    assert resp.status_code == 200
    assert settings.jwt_cookie_name not in resp.headers.get("set-cookie", "")
    assert await _live_tokens(pg, user_id) == []
    await client.aclose()


@pytest.mark.asyncio
async def test_username_with_a_nul_byte_is_a_plain_failure(
    pg: async_sessionmaker[AsyncSession],
) -> None:
    """NUL バイトを含むユーザー名は PostgreSQL の text に入らない。500 にならず、他の失敗と同じ応答。"""
    await _seed_user(pg)
    client, raw = _client()
    limiter.reset()
    login_throttle.clear()
    resp = await client.post(
        "/login",
        data={"username": "pg_coll\x00ector", "password": "x", "csrf_token": raw},
        headers={"X-CSRF-Token": raw},
    )
    assert resp.status_code == 200
    assert "ユーザー名またはパスワードが正しくありません" in resp.text
    await client.aclose()
