"""ログイン失敗のスロットルのテスト（Claude Security F13 / F14 / F16）。

アカウント単位のロックは廃止した。認証前の第三者が既知のアカウントを締め出せ、
さらにロック済みを示す応答の違いで有効なユーザー名が分かったためである。
今は失敗を (ユーザー名, クライアント IP) の組で数え、閾値に達した組だけを受け付けない。

- 失敗応答は、ユーザー不在・非アクティブ・パスワード違い・ブロック中のすべてで同じ
- ブロックされるのは試行元の IP だけで、別の IP からの本人のログインは通る
- 存在しないユーザー名も同じように数える（数え方で実在が分からない）
- ブロックは延長されない・窓を過ぎれば忘れる・件数に上限がある
- 管理者向けの証跡（AuthEvent=locked）は残るが、応答には出ない
- 旧方式の locked_until が残っている行も、応答では区別しない／admin が解除できる

IP 単位のレート制限（5回/分）とは別機構。テストでは各試行前に limiter.reset() で 429 干渉を避ける。
"""

import re
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.api.main import app
from kawasemi.core.config import settings
from kawasemi.core.login_throttle import LoginThrottle, login_throttle
from kawasemi.models.auth_event import AuthEvent, AuthEventAction
from kawasemi.models.user import User
from kawasemi.web.auth_routes import limiter
from tests.e2e.helpers import extract_form_csrf_token

_PW = "AdminPass123!"
_GENERIC = "ユーザー名またはパスワードが正しくありません"


async def _attempt_login(client: AsyncClient, username: str, password: str):
    """GET /login で CSRF を取り、POST /login を1回試行する（rate limit を都度リセット）。"""
    limiter.reset()
    page = await client.get("/login")
    token = extract_form_csrf_token(page.text)
    return await client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        headers={"X-CSRF-Token": token},
    )


def _normalized(html: str) -> str:
    """応答ごとに変わる CSRF トークンを除いた本文（応答の同一性の比較用）。"""
    return re.sub(r'value="[^"]*"', 'value=""', html)


async def _count_events(db: AsyncSession, user_id: int, action: str) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(AuthEvent)
        .where(AuthEvent.user_id == user_id, AuthEvent.action == action)
    )
    return int(result.scalar_one())


# ===== ログイン経路 =====


@pytest.mark.asyncio
async def test_repeated_failures_block_this_pair_even_for_the_right_password(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, monkeypatch
) -> None:
    """閾値回数の失敗で、同じ (ユーザー名, IP) は正しいパスワードでも入れない。"""
    monkeypatch.setattr(settings, "lockout_max_attempts", 3)

    for _ in range(3):
        await _attempt_login(client, "admin_test", "wrong-pass")

    resp = await _attempt_login(client, "admin_test", _PW)
    assert resp.status_code == 200
    assert _GENERIC in resp.text
    assert settings.jwt_cookie_name not in resp.cookies
    # 管理者向けの証跡は1件だけ残る
    assert await _count_events(db_session, admin_user.id, AuthEventAction.LOCKED) == 1
    # アカウント自体はロックされない（旧方式の列は触らない）
    await db_session.refresh(admin_user)
    assert admin_user.locked_until is None
    assert admin_user.failed_login_count == 0


@pytest.mark.asyncio
async def test_failure_response_is_identical_whether_or_not_the_user_exists_or_is_blocked(
    client: AsyncClient, admin_user: User, db_session: AsyncSession, monkeypatch
) -> None:
    """F13/F14/F16: 不在・パスワード違い・非アクティブ・ブロック中で、状態も本文も同じ。"""
    monkeypatch.setattr(settings, "lockout_max_attempts", 3)

    ghost = await _attempt_login(client, "ghost_user", "whatever")
    wrong = await _attempt_login(client, "admin_test", "wrong-pass")
    for _ in range(2):  # 3回目でブロックに入る（このあとの応答も含めて比較する）
        await _attempt_login(client, "admin_test", "wrong-pass")
    blocked_wrong = await _attempt_login(client, "admin_test", "wrong-pass")
    blocked_right = await _attempt_login(client, "admin_test", _PW)

    admin_user.is_active = False
    await db_session.commit()
    inactive = await _attempt_login(client, "admin_test", _PW)

    responses = [ghost, wrong, blocked_wrong, blocked_right, inactive]
    assert {r.status_code for r in responses} == {200}
    bodies = {_normalized(r.text) for r in responses}
    assert len(bodies) == 1, (
        "失敗応答が状態によって違う（ユーザー名の列挙・ブロックの確認ができる）"
    )
    assert _GENERIC in ghost.text
    assert "ロック" not in ghost.text
    assert all(settings.jwt_cookie_name not in r.cookies for r in responses)


@pytest.mark.asyncio
async def test_unknown_usernames_are_counted_like_real_ones(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """存在しないユーザー名も同じ数え方でブロックに入る（数え方で実在が分からない）。"""
    monkeypatch.setattr(settings, "lockout_max_attempts", 2)
    for _ in range(2):
        await _attempt_login(client, "ghost_user", "whatever")
    assert login_throttle.is_blocked(("ghost_user", "127.0.0.1"))
    # 実在しないので監査イベントは作らない（user_id が無い）
    total = await db_session.execute(select(func.count()).select_from(AuthEvent))
    assert int(total.scalar_one()) == 0


@pytest.mark.asyncio
async def test_a_third_party_cannot_lock_the_owner_out_from_another_ip(
    client: AsyncClient, admin_user: User, monkeypatch
) -> None:
    """F16: 攻撃者の IP のブロックは、別の IP から来る本人のログインに影響しない。"""
    monkeypatch.setattr(settings, "lockout_max_attempts", 3)

    for _ in range(3):  # 攻撃者（127.0.0.1）が既知のユーザー名で失敗を積む
        await _attempt_login(client, "admin_test", "wrong-pass")
    assert login_throttle.is_blocked(("admin_test", "127.0.0.1"))

    transport = ASGITransport(app=app, client=("10.9.8.7", 4321))
    async with AsyncClient(transport=transport, base_url="http://test") as owner:
        resp = await _attempt_login(owner, "admin_test", _PW)
    assert resp.status_code == 303
    assert resp.cookies.get(settings.jwt_cookie_name) is not None


@pytest.mark.asyncio
async def test_successful_login_forgets_the_failures_of_that_pair(
    client: AsyncClient, admin_user: User, monkeypatch
) -> None:
    """成功すると、その組の失敗の積み上げは消える。"""
    monkeypatch.setattr(settings, "lockout_max_attempts", 3)
    for _ in range(2):
        await _attempt_login(client, "admin_test", "wrong-pass")
    assert (await _attempt_login(client, "admin_test", _PW)).status_code == 303
    client.cookies.clear()  # ログイン済みのままだと /login が転送されるので、未ログインに戻す
    for _ in range(2):  # 積み直し。成功で消えていなければここでブロックに入る
        await _attempt_login(client, "admin_test", "wrong-pass")
    assert not login_throttle.is_blocked(("admin_test", "127.0.0.1"))


# ===== 旧方式の locked_until が残っている行 =====


@pytest.mark.asyncio
async def test_legacy_lock_in_the_future_looks_like_any_other_failure(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """旧ロックが期限内でも、応答は他の失敗と同じ（ロック済みを示す文面を返さない）。"""
    admin_user.locked_until = datetime.now(UTC) + timedelta(minutes=30)
    admin_user.failed_login_count = 5
    await db_session.commit()

    resp = await _attempt_login(client, "admin_test", _PW)
    assert resp.status_code == 200
    assert _GENERIC in resp.text
    assert "ロック" not in resp.text
    assert settings.jwt_cookie_name not in resp.cookies


@pytest.mark.asyncio
async def test_legacy_lock_auto_expires_and_allows_login(
    client: AsyncClient, admin_user: User, db_session: AsyncSession
) -> None:
    """旧ロックの期限切れは自動解除され、正しいパスワードでログイン成功＆カウントリセット。"""
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
async def test_admin_can_unlock_account(
    admin_client: AsyncClient, admin_user: User, db_session: AsyncSession, csrf_token: str
) -> None:
    """admin が旧ロックを解除でき、監査に unlocked（actor_id 付き）が残る。"""
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


# ===== LoginThrottle 単体 =====


def _trip(t: LoginThrottle, key: tuple[str, str], now: float, n: int = 3, window: float = 60.0):
    tripped = False
    for _ in range(n):
        tripped = t.register_failure(key, max_attempts=n, window_seconds=window, now=now)
    return tripped


def test_throttle_trips_on_the_nth_failure_only() -> None:
    t = LoginThrottle()
    key = ("u", "1.1.1.1")
    assert [
        t.register_failure(key, max_attempts=3, window_seconds=60, now=0) for _ in range(3)
    ] == [
        False,
        False,
        True,
    ]
    assert t.is_blocked(key, now=1)


def test_throttle_does_not_extend_the_block_and_unblocks_after_the_window() -> None:
    """ブロック中の失敗は数えない（延長されない）。窓を過ぎれば受け付ける。"""
    t = LoginThrottle()
    key = ("u", "1.1.1.1")
    assert _trip(t, key, now=0)
    for now in (10, 30, 59):  # ブロック中に何度失敗しても期限は動かない
        assert t.register_failure(key, max_attempts=3, window_seconds=60, now=now) is False
        assert t.is_blocked(key, now=now)
    assert not t.is_blocked(key, now=61)


def test_throttle_forgets_old_failures() -> None:
    t = LoginThrottle()
    key = ("u", "1.1.1.1")
    t.register_failure(key, max_attempts=3, window_seconds=60, now=0)
    t.register_failure(key, max_attempts=3, window_seconds=60, now=1)
    # 窓を過ぎてからの失敗は、数え直し（3回目にならない）
    assert t.register_failure(key, max_attempts=3, window_seconds=60, now=100) is False
    assert not t.is_blocked(key, now=101)


def test_throttle_pairs_are_independent() -> None:
    t = LoginThrottle()
    assert _trip(t, ("u", "1.1.1.1"), now=0)
    assert not t.is_blocked(("u", "2.2.2.2"), now=1)
    assert not t.is_blocked(("v", "1.1.1.1"), now=1)


def test_throttle_is_bounded_and_keeps_active_blocks() -> None:
    """ユーザー名を変えながらの大量の試行でも件数が上限を超えない。ブロック中の組は捨てない。"""
    t = LoginThrottle(max_entries=50)
    blocked = ("victim", "9.9.9.9")
    assert _trip(t, blocked, now=0, window=1000)
    for i in range(500):
        t.register_failure((f"u{i}", "1.1.1.1"), max_attempts=3, window_seconds=1000, now=1 + i)
    assert len(t._entries) <= 50
    assert t.is_blocked(blocked, now=600)
