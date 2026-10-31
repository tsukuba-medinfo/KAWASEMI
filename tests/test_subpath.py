"""リバースプロキシのサブパス配下（例 /kawasemi）での動作を検証する。

nginx がプレフィックスを strip して転送する構成を ASGITransport の
root_path で模す。検証の要点:
  - ルーティングが root_path の有無に関わらず成立する
  - テンプレートの内部絶対パスに base_path() プレフィックスが付く
  - 内部リダイレクトの Location にプレフィックスが補われる
  - root_path 無し（従来）の場合はプレフィックスが付かない
"""

from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.api.main import app
from kawasemi.core.database import get_db

ROOT = "/kawasemi"


async def _make_client(db_session: AsyncSession, root_path: str) -> AsyncClient:
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    return AsyncClient(
        transport=ASGITransport(app=app, root_path=root_path),
        base_url="http://test",
    )


@pytest.mark.asyncio
async def test_login_page_links_are_prefixed_under_subpath(db_session: AsyncSession) -> None:
    """サブパス配下ではログイン画面のリンク/フォームにプレフィックスが付く。"""
    client = await _make_client(db_session, ROOT)
    try:
        resp = await client.get("/login")
        assert resp.status_code == 200
        body = resp.text
        # フォーム action と static 参照がプレフィックス化されている
        assert f'action="{ROOT}/login"' in body
        # 素の "/login"（プレフィックス無し）は残っていない
        assert 'action="/login"' not in body
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_login_page_links_not_prefixed_without_subpath(db_session: AsyncSession) -> None:
    """root_path 無し（ルート直下）では従来どおりプレフィックスは付かない。"""
    client = await _make_client(db_session, "")
    try:
        resp = await client.get("/login")
        assert resp.status_code == 200
        assert 'action="/login"' in resp.text
        assert "/kawasemi/" not in resp.text
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_redirect_location_is_prefixed_under_subpath(db_session: AsyncSession) -> None:
    """内部リダイレクト（/logout -> /login）の Location にプレフィックスが補われる。"""
    client = await _make_client(db_session, ROOT)
    try:
        resp = await client.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"{ROOT}/login"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_redirect_location_not_prefixed_without_subpath(db_session: AsyncSession) -> None:
    """root_path 無しでは Location は素のパスのまま。"""
    client = await _make_client(db_session, "")
    try:
        resp = await client.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_health_routes_under_subpath(db_session: AsyncSession) -> None:
    """root_path を strip した転送でもルーティングが成立する。"""
    client = await _make_client(db_session, ROOT)
    try:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy"}
    finally:
        app.dependency_overrides.clear()
