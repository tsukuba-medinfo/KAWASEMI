"""入り口：使う人ごとに画面と権限の範囲を分ける。

画面と権限は使う人ごとに分け、コードとデータは分けない。`ENTRY_ROLES` で絞った入り口では、
役割切替・既定の役割・ナビ・403 の案内・ログインがその範囲に閉じる。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.core.entry import current_entry, parse_entry, reset_entry_cache
from kawasemi.models.role import RoleType
from kawasemi.models.user import User, UserRole
from kawasemi.web.template_globals import nav_items


def _entry(monkeypatch: pytest.MonkeyPatch, roles: str, urls: str = "", name: str = "設計") -> None:
    monkeypatch.setattr(settings, "entry_name", name)
    monkeypatch.setattr(settings, "entry_roles", roles)
    monkeypatch.setattr(settings, "entry_urls", urls)
    reset_entry_cache()


async def _load(db_session: AsyncSession, user: User) -> User:
    """役割を読み込み済みの User（fixture は役割を後から足すので遅延ロードになる）。"""
    db_session.expunge_all()  # fixture の instance を捨て、関係ごと読み直す
    row = await db_session.execute(
        select(User)
        .where(User.id == user.id)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .execution_options(populate_existing=True)
    )
    return row.scalar_one()


def _act(client: AsyncClient, user: User, act: str) -> None:
    client.cookies.set(
        settings.jwt_cookie_name, create_access_token(data={"sub": str(user.id), "act": act})
    )


# --------------------------------------------------------------------------- #
# 設定
# --------------------------------------------------------------------------- #


def test_an_empty_setting_means_no_restriction() -> None:
    e = parse_entry("", "", "")
    assert not e.restricted and e.allows("admin") and e.allows(None) is True


def test_roles_and_urls_are_parsed() -> None:
    e = parse_entry(
        " 設計 ",
        " designer ",
        "collector=https://h/collect/, admin=https://h/admin/",
    )
    assert e.name == "設計" and e.roles == ("designer",)
    assert e.allows("designer") and not e.allows("admin")
    assert e.url_for(["reviewer", "admin"]) == ("admin", "https://h/admin/")
    assert e.url_for(["reviewer"]) is None


@pytest.mark.parametrize(
    "roles,urls",
    [
        ("ai_designer", ""),
        ("collection_lead", ""),  # 旧7役割の名前は通らない（互換の別名は残さない）
        ("admin", "collector"),
        ("admin", "nobody=https://h/"),
    ],
)
def test_a_wrong_role_name_stops_at_startup(roles: str, urls: str) -> None:
    """黙って全役割に倒さない（入り口の制限が消えるため）。"""
    with pytest.raises(ValueError):
        parse_entry("", roles, urls)


# --------------------------------------------------------------------------- #
# 役割の絞り込み（1か所で効く）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_switchable_roles_are_limited_to_the_entry(
    multi_role_user: User, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user = await _load(db_session, multi_role_user)
    assert set(user.switchable_role_names()) == {
        RoleType.DESIGNER,
        RoleType.COLLECTOR,
        RoleType.REVIEWER,
    }
    _entry(monkeypatch, "designer")
    assert user.switchable_role_names() == ["designer"]
    assert user.default_active_role() == "designer"
    assert not user.can_act_as("collector")
    assert set(user.held_role_names_outside_entry()) == {"collector", "reviewer"}


@pytest.mark.asyncio
async def test_an_act_claim_from_another_entry_falls_back_to_this_entry(
    client: AsyncClient, multi_role_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同じホストの別 port は Cookie を共有する。別の入り口の `act` はここでは通らない。"""
    _entry(monkeypatch, "designer")
    _act(client, multi_role_user, "collector")
    resp = await client.get("/input-targets", headers={"Accept": "application/json"})
    assert resp.status_code == 403
    assert resp.json()["active_role"] == "designer", "別の入り口の act がそのまま通っている"


# --------------------------------------------------------------------------- #
# 403 の案内・ナビ・ログイン
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_403_points_to_the_other_entry_only_for_held_roles(
    client: AsyncClient,
    multi_role_user: User,
    designer_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry(monkeypatch, "designer", "collector=https://h/collect/")
    # 収集者も保有する利用者には、収集者の入り口へ案内する
    _act(client, multi_role_user, "designer")
    resp = await client.get("/input-targets", headers={"Accept": "text/html"})
    assert resp.status_code == 403
    assert 'href="https://h/collect/"' in resp.text and "の入り口へ" in resp.text
    assert "切り替えて続ける" not in resp.text, "入り口の外の役割へ切替を勧めている"
    resp = await client.get("/input-targets", headers={"Accept": "application/json"})
    body = resp.json()
    assert body["outside_entry"] is True
    assert body["other_entry"] == {"role": "collector", "url": "https://h/collect/"}

    # 設計の役割しか持たない利用者には、持たない役割の入り口を案内しない（そこで拒否される）
    _act(client, designer_user, "designer")
    resp = await client.get("/input-targets", headers={"Accept": "application/json"})
    assert resp.json()["outside_entry"] is True and resp.json()["other_entry"] is None
    resp = await client.get("/input-targets", headers={"Accept": "text/html"})
    assert "https://h/collect/" not in resp.text and "入り口からログインしてください" in resp.text


@pytest.mark.asyncio
async def test_nav_shows_other_entries_only_for_held_roles(
    designer_user: User,
    multi_role_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _entry(
        monkeypatch,
        "designer",
        "collector=https://h/collect/,admin=https://h/admin/",
    )
    user = await _load(db_session, designer_user)
    user.active_role = "designer"
    items = {i["label"]: i for i in nav_items(user)}
    assert items["データ自動収集"]["locked"] is False
    # 設計の役割しか持たない利用者には、収集者・管理者の入り口を出さない（届かない）
    assert "入力対象一覧" not in items and "ユーザー管理" not in items

    multi = await _load(db_session, multi_role_user)
    multi.active_role = "designer"
    items = {i["label"]: i for i in nav_items(multi)}
    # 収集者も保有するので、URL のある収集者の入り口は案内になる。管理者は持たないので出さない
    assert items["入力対象一覧"]["other_entry"] == "https://h/collect/"
    assert "ユーザー管理" not in items


@pytest.mark.asyncio
async def test_login_is_refused_when_the_user_has_no_role_at_this_entry(
    client: AsyncClient, collector_user: User, csrf_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entry(monkeypatch, "designer", "collector=https://h/collect/")
    resp = await client.post(
        "/login",
        data={
            "username": collector_user.username,
            "password": "Collector123!",
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.status_code
    assert "この入り口「設計」では" in resp.text and "https://h/collect/" in resp.text
    assert settings.jwt_cookie_name not in resp.cookies


@pytest.mark.asyncio
async def test_switch_role_outside_the_entry_is_refused(
    client: AsyncClient, multi_role_user: User, csrf_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    _entry(monkeypatch, "designer")
    _act(client, multi_role_user, "designer")
    resp = await client.post(
        "/switch-role",
        data={"role": "collector", "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    assert current_entry().name == "設計"


@pytest.mark.asyncio
async def test_a_shared_cookie_without_a_usable_role_is_unauthenticated(
    client: AsyncClient, collector_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    """別の入り口でログインした収集者のトークンは、設計の入り口では未認証と同じ。"""
    _entry(monkeypatch, "designer")
    _act(client, collector_user, "collector")
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"].endswith("/login")
    resp = await client.get("/api/v100/questionnaires/")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_startup_stops_on_a_wrong_entry_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from kawasemi.api.main import startup_event

    _entry(monkeypatch, "ai_designer")
    # 例外の出所を固定する（入り口の検査の後ろで別の理由で落ちても通ってしまわないように）
    with pytest.raises(ValueError, match="ENTRY_ROLES"):
        await startup_event()


@pytest.mark.asyncio
async def test_refresh_is_refused_without_a_usable_role_at_this_entry(
    client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自分の入り口で得たリフレッシュトークンでも、この入り口で使える役割が無ければ更新しない。"""
    from kawasemi.web.auth_routes import _issue_refresh_token

    token = await _issue_refresh_token(collector_user.id, db_session)
    await db_session.commit()
    _entry(monkeypatch, "designer")
    client.cookies.set(settings.jwt_refresh_cookie_name, token)
    resp = await client.post("/token/refresh", headers={"X-CSRF-Token": csrf_token})
    assert resp.status_code == 401
    assert "この入り口で使える役割がありません" in resp.json()["message"]


@pytest.mark.asyncio
async def test_a_user_with_no_roles_stays_authenticated_at_a_restricted_entry(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """役割を1つも持たない利用者は従来どおり認証済み（役割なし）のまま。

    未認証に倒すのは「別の入り口で使える役割を持つ」利用者だけである。役割ゼロの利用者を
    未認証にすると、どの入り口にも入れず、管理者が役割を付けるまで何も見えない状態の説明が無くなる。
    """
    from kawasemi.core.auth import get_password_hash

    user = User(
        username="noroles",
        email="noroles@example.com",
        password_hash=get_password_hash("NoRoles123!"),
        full_name="役割なし",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    _entry(monkeypatch, "designer")
    _act(client, user, "designer")
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code == 200
