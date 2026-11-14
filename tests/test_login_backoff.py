"""再スキャン（2026-11-14）F8: ログインの総当たりに対する、IP に依らない制限とパスワードの方針。

- `AccountBackoff`: ユーザー名だけで試行を直列に並べる段階的な遅延（ロックしない）
- IPv6 は /64 にまとめて数える（アドレスを変えるだけで数え直しにならない）
- bcrypt はイベントループの外（スレッド）で走らせる
- パスワードの方針（管理画面・CLI の入口の検査）
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.cli import create_admin_user
from kawasemi.core.config import settings
from kawasemi.core.login_throttle import (
    AccountBackoff,
    account_backoff,
    aggregate_ip,
    throttle_key,
    username_digest,
)
from kawasemi.core.password_policy import password_policy_error
from kawasemi.models.user import User
from kawasemi.web import auth_routes
from kawasemi.web.auth_routes import limiter
from tests.e2e.helpers import extract_form_csrf_token

_GENERIC = "ユーザー名またはパスワードが正しくありません"
_KW: dict[str, Any] = {"free": 3, "base": 1.0, "cap": 8.0, "max_wait": 20.0}


# ===== AccountBackoff（単体）=====


def test_first_attempts_are_free_then_the_interval_doubles_up_to_the_cap() -> None:
    b = AccountBackoff()
    now = 1000.0
    waits = [b.reserve("k", now=now, **_KW) for _ in range(9)]

    # 同じ瞬間に9回並べる: 最初の3回は待たず、以降は枠が 1, 2, 4, 8, 8, 8 秒ずつ後ろへ伸びる
    # （1つ目の遅延の枠は4回目から。枠の位置は、前の試行の枠＋その間隔）
    assert waits[:4] == [0.0, 0.0, 0.0, 0.0]
    assert waits[4:] == [1.0, 3.0, 7.0, 15.0, None]


def test_a_long_queue_is_refused_instead_of_waiting_forever() -> None:
    b = AccountBackoff()
    waits = [b.reserve("k", now=0.0, **_KW) for _ in range(12)]

    assert None in waits
    first_none = waits.index(None)
    assert all(w is None for w in waits[first_none:])
    # 拒否された試行は枠を使わない（拒否が続いても、後から来た試行の待ちは伸びない）
    assert b.reserve("k", now=0.0, **_KW) is None


def test_parallel_attempts_cannot_beat_the_serial_rate() -> None:
    """同じ瞬間に何千回送っても、枠は1つずつしか空かない（IP の数で推測の速さが伸びない）。"""
    b = AccountBackoff()
    granted = [w for w in (b.reserve("k", now=0.0, **_KW) for _ in range(5000)) if w is not None]

    assert len(granted) <= 3 + 1 + 5  # 無料 3 回＋待ちが max_wait 以内に収まる分だけ
    assert max(granted) <= _KW["max_wait"]


def test_the_queue_drains_as_time_passes_and_success_forgets_the_account() -> None:
    b = AccountBackoff()
    for _ in range(8):
        b.reserve("k", now=0.0, **_KW)

    assert b.reserve("k", now=0.0, **_KW) is None
    assert b.reserve("k", now=100.0, **_KW) == 0.0  # 時間が経てば枠が空く
    b.reset("k")
    assert [b.reserve("k", now=100.0, **_KW) for _ in range(3)] == [0.0, 0.0, 0.0]


def test_an_idle_account_is_forgotten_after_the_window() -> None:
    b = AccountBackoff(window_seconds=900.0)
    for _ in range(8):
        b.reserve("k", now=0.0, **_KW)

    assert b.reserve("k", now=1000.0, **_KW) == 0.0
    assert b.reserve("k", now=1000.0, **_KW) == 0.0


def test_accounts_are_counted_separately_and_zero_base_disables_the_delay() -> None:
    b = AccountBackoff()
    for _ in range(8):
        b.reserve("a", now=0.0, **_KW)

    assert b.reserve("b", now=0.0, **_KW) == 0.0
    off = {**_KW, "base": 0.0}
    assert all(b.reserve("a", now=0.0, **off) == 0.0 for _ in range(20))


def test_memory_is_bounded_by_the_entry_cap() -> None:
    b = AccountBackoff(max_entries=50)
    for i in range(500):
        b.reserve(f"user-{i}", now=float(i), **_KW)

    assert len(b._entries) <= 50


# ===== IPv6 の /64 =====


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("203.0.113.9", "203.0.113.9"),
        ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
        ("2001:db8:1:2::1", "2001:db8:1:2::/64"),
        ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
        ("::ffff:203.0.113.9", "203.0.113.9"),
        ("fe80::1%eth0", "fe80::/64"),
        ("not-an-ip", "not-an-ip"),
    ],
)
def test_ipv6_sources_are_grouped_by_the_64(raw: str, expected: str) -> None:
    assert aggregate_ip(raw) == expected


def test_throttle_key_groups_a_64_but_separates_other_prefixes() -> None:
    a = throttle_key("admin", "2001:db8:1:2::1")
    b = throttle_key("admin", "2001:db8:1:2:ffff:ffff:ffff:ffff")
    c = throttle_key("admin", "2001:db8:1:3::1")

    assert a == b
    assert a != c
    assert a[0] == username_digest("admin")


def test_the_rate_limit_key_also_groups_the_64() -> None:
    from starlette.requests import Request

    from kawasemi.core.rate_limit import client_key

    def req(host: str) -> Request:
        return Request({"type": "http", "client": (host, 1234), "headers": [], "path": "/"})

    assert client_key(req("2001:db8:9:9::1")) == client_key(req("2001:db8:9:9::2"))
    assert client_key(req("2001:db8:9:9::1")) != client_key(req("2001:db8:9:8::1"))


# ===== ログイン経路 =====


async def _attempt(client: AsyncClient, username: str, password: str):
    limiter.reset()
    page = await client.get("/login")
    token = extract_form_csrf_token(page.text)
    return await client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        headers={"X-CSRF-Token": token},
    )


@pytest.fixture
def backoff_on(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """遅延を有効にし、`asyncio.sleep` の呼び出しを記録する（実際には待たない）。"""
    monkeypatch.setattr(settings, "login_backoff_base_seconds", 1.0)
    monkeypatch.setattr(settings, "login_backoff_free_attempts", 2)
    monkeypatch.setattr(settings, "login_backoff_cap_seconds", 4.0)
    monkeypatch.setattr(settings, "login_backoff_max_wait_seconds", 6.0)
    monkeypatch.setattr(
        settings, "lockout_max_attempts", 1000
    )  # (ユーザー名, IP) のブロックと分ける
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    # `asyncio.sleep` 全体ではなく、この経路が使う `asyncio` だけを差し替える（他の箇所の
    # `sleep(0)` を記録しないため）
    monkeypatch.setattr(
        auth_routes,
        "asyncio",
        SimpleNamespace(sleep=fake_sleep, to_thread=asyncio.to_thread),
    )
    # 時計を止める（bcrypt の実時間で枠が空いて、結果が揺れないように）
    real_reserve = account_backoff.reserve
    monkeypatch.setattr(
        account_backoff, "reserve", lambda key, **kw: real_reserve(key, now=1000.0, **kw)
    )
    return slept


@pytest.mark.asyncio
async def test_the_delay_pattern_is_identical_for_real_and_unknown_usernames(
    client: AsyncClient, admin_user: User, backoff_on: list[float]
) -> None:
    patterns: dict[str, list[float]] = {}
    for name in ("admin_test", "ghost_user"):
        account_backoff.clear()
        backoff_on.clear()
        for _ in range(5):
            await _attempt(client, name, "wrong-pass")
        patterns[name] = list(backoff_on)

    # 無料2回のあと、枠の間隔が 1, 2 秒と伸びる（4回目は1秒、5回目は3秒待たされる）
    assert patterns["admin_test"] == patterns["ghost_user"] == [1.0, 3.0]


@pytest.mark.asyncio
async def test_a_queue_that_is_too_long_fails_without_checking_the_password(
    client: AsyncClient,
    admin_user: User,
    backoff_on: list[float],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    real = auth_routes.verify_password

    def spy(plain: str, hashed: str) -> bool:
        calls.append(plain)
        return real(plain, hashed)

    monkeypatch.setattr(auth_routes, "verify_password", spy)
    for _ in range(12):
        resp = await _attempt(client, "admin_test", "wrong-pass")
        assert resp.status_code == 200
        assert _GENERIC in resp.text

    # 待ち行列が長すぎる試行（6回目以降）は、パスワードを調べずに同じ失敗応答にする
    assert len(calls) == 5
    # **ロックではない**: 行列が空けば、正しいパスワードですぐに入れる
    account_backoff.clear()
    ok = await _attempt(client, "admin_test", "AdminPass123!")
    assert ok.status_code == 303


@pytest.mark.asyncio
async def test_success_forgets_the_delay_for_that_username(
    client: AsyncClient, admin_user: User, backoff_on: list[float]
) -> None:
    for _ in range(4):
        await _attempt(client, "admin_test", "wrong-pass")
    assert backoff_on == [1.0]  # 4回目に遅延が掛かった

    ok = await _attempt(client, "admin_test", "AdminPass123!")
    assert ok.status_code == 303
    client.cookies.clear()  # ログイン済みのままだと /login が転送される

    backoff_on.clear()
    for _ in range(2):
        await _attempt(client, "admin_test", "wrong-pass")
    assert backoff_on == []  # 無料の回数まで戻っている


@pytest.mark.asyncio
async def test_bcrypt_runs_off_the_event_loop(
    client: AsyncClient, admin_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    threads: list[int] = []
    real = auth_routes.verify_password

    def spy(plain: str, hashed: str) -> bool:
        threads.append(threading.get_ident())
        return real(plain, hashed)

    monkeypatch.setattr(auth_routes, "verify_password", spy)
    main = threading.get_ident()

    await _attempt(client, "admin_test", "wrong-pass")  # 実在するユーザー
    await _attempt(client, "ghost_user", "wrong-pass")  # ダミーのハッシュ

    assert len(threads) == 2
    assert all(t != main for t in threads)


# ===== パスワードの方針 =====


@pytest.mark.parametrize(
    ("password", "username", "expected_substring"),
    [
        ("short1!", None, "12文字以上"),
        ("a" * 11, None, "12文字以上"),
        ("password1234", None, "ありふれた"),
        ("Password1234", None, "ありふれた"),
        ("xxxxxxxxxxxx", None, "同じ文字"),
        ("SomeUserName1", "someusername1", "ユーザー名"),
    ],
)
def test_weak_passwords_are_rejected(
    password: str, username: str | None, expected_substring: str
) -> None:
    error = password_policy_error(password, username=username)

    assert error is not None and expected_substring in error


@pytest.mark.parametrize(
    "password",
    ["correct horse battery", "AdminPass123!!", "日本語のパスフレーズです長め", "a" * 12 + "b"],
)
def test_reasonable_passwords_pass(password: str) -> None:
    assert password_policy_error(password, username="admin") is None


def _form(**extra: str) -> dict[str, str]:
    base = {
        "username": "newuser",
        "email": "newuser@example.com",
        "full_name": "新規",
        "password": "correct horse battery",
    }
    base.update(extra)
    return base


@pytest.mark.asyncio
async def test_admin_cannot_create_a_user_with_a_weak_password(
    admin_client: AsyncClient, db_session: AsyncSession, csrf_token: str
) -> None:
    for weak in ("short", "password1234"):
        resp = await admin_client.post(
            "/admin/users/new",
            data=_form(password=weak, csrf_token=csrf_token),
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 200
        assert re.search(r"12文字以上|ありふれた", resp.text)
    created = (
        await db_session.execute(select(User).where(User.username == "newuser"))
    ).scalar_one_or_none()
    assert created is None

    ok = await admin_client.post(
        "/admin/users/new",
        data=_form(csrf_token=csrf_token),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert ok.status_code == 303


@pytest.mark.asyncio
async def test_admin_cannot_reset_a_password_to_a_weak_one_but_blank_means_unchanged(
    admin_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    before = collector_user.password_hash

    def data(password: str) -> dict[str, str]:
        return {
            "username": collector_user.username,
            "email": collector_user.email,
            "full_name": collector_user.full_name,
            "password": password,
            "is_active": "on",
            "csrf_token": csrf_token,
        }

    weak = await admin_client.post(
        f"/admin/users/{collector_user.id}/edit",
        data=data("short"),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert weak.status_code == 200
    await db_session.refresh(collector_user)
    assert collector_user.password_hash == before

    blank = await admin_client.post(
        f"/admin/users/{collector_user.id}/edit",
        data=data(""),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert blank.status_code == 303
    await db_session.refresh(collector_user)
    assert collector_user.password_hash == before


@pytest.mark.asyncio
async def test_the_user_form_asks_for_the_same_minimum_as_the_server(
    admin_client: AsyncClient,
) -> None:
    page = await admin_client.get("/admin/users/new")

    assert 'minlength="12"' in page.text
    assert "12文字以上" in page.text
    assert settings.password_min_length == 12


@pytest.mark.asyncio
async def test_the_cli_refuses_a_weak_admin_password(capsys: pytest.CaptureFixture[str]) -> None:
    await create_admin_user("cliadmin", "cliadmin@example.com", "short", "CLI 管理者")

    out = capsys.readouterr().out
    assert "12文字以上" in out


def test_the_cli_exits_non_zero_when_the_admin_password_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """方針違反で作らなかったのに終了コード 0 だと、スクリプトが失敗を見落とす。"""
    import sys

    from kawasemi import cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "kawasemi",
            "create_admin",
            "-u",
            "cliadmin2",
            "-e",
            "cliadmin2@example.com",
            "-p",
            "short",
            "-n",
            "CLI 管理者",
        ],
    )

    with pytest.raises(SystemExit) as raised:
        cli.main()

    assert raised.value.code == 1
    assert "12文字以上" in capsys.readouterr().out
