"""役割不足の 403 を「切替の導線」にする（行き止まりにしない）テスト

- 画面（Accept: text/html）には切替ボタン付きの案内を 403 で返す
- API（それ以外）には従来どおり detail の JSON を返す（応答互換）
- 保有していない役割は切替ボタンを出さず、依頼先を案内する
- 切替後は元の画面へ戻す。外部URLの next は握りつぶす（オープンリダイレクト）
- ナビはアクティブ役割で項目を消さず、切替えれば届く項目を 🔒 付きで見せる
"""

import pytest
from httpx import AsyncClient

from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from kawasemi.web.template_globals import nav_items

HTML = {"Accept": "text/html,application/xhtml+xml"}


def _set_active(client: AsyncClient, user: User, act: str | None = None) -> None:
    data: dict[str, str] = {"sub": str(user.id)}
    if act is not None:
        data["act"] = act
    client.cookies.set(settings.jwt_cookie_name, create_access_token(data=data))


class TestRoleGatePage:
    @pytest.mark.asyncio
    async def test_html_403_offers_switch_when_user_holds_the_role(
        self, client: AsyncClient, multi_role_user: User
    ):
        """設計者も保有する人が収集者として設計の画面を叩くと、切替ボタン付きの案内が出る"""
        _set_active(client, multi_role_user, act=RoleType.COLLECTOR)
        resp = await client.get("/ai-input/dashboard", headers=HTML)

        assert resp.status_code == 403
        body = resp.text
        assert "この操作は別の役割で行います" in body
        # どの役割へ切り替えればよいかを名指しする
        assert RoleType.display_name(RoleType.DESIGNER) in body
        assert f'value="{RoleType.DESIGNER}"' in body
        # 戻り先（元の画面）を持ち回る
        assert 'name="next" value="/ai-input/dashboard"' in body

    @pytest.mark.asyncio
    async def test_html_403_without_the_role_shows_who_to_ask(
        self, client: AsyncClient, collector_user: User
    ):
        """保有していない役割の画面では、切替ボタンではなく依頼先を出す"""
        _set_active(client, collector_user, act=RoleType.COLLECTOR)
        resp = await client.get("/ai-input/dashboard", headers=HTML)

        assert resp.status_code == 403
        body = resp.text
        assert "依頼してください" in body
        assert 'name="role"' not in body or "切り替えて続ける" not in body

    @pytest.mark.asyncio
    async def test_api_403_keeps_json_contract(self, client: AsyncClient, multi_role_user: User):
        """API 呼び出し（HTML を要求しない）には従来どおり detail を返す"""
        _set_active(client, multi_role_user, act=RoleType.COLLECTOR)
        resp = await client.get("/ai-input/dashboard")

        assert resp.status_code == 403
        payload = resp.json()
        assert payload["detail"] == "この操作を行う権限がありません"
        # どの役割が要るかを機械可読でも返す（画面と同じ情報）
        assert payload["required_roles"] == [RoleType.DESIGNER]
        assert payload["active_role"] == RoleType.COLLECTOR


class TestSwitchBack:
    @pytest.mark.asyncio
    async def test_switch_role_returns_to_next(
        self, client: AsyncClient, multi_role_user: User, csrf_token: str
    ):
        """next を渡すと切替後にその画面へ戻る"""
        _set_active(client, multi_role_user, act=RoleType.COLLECTOR)
        resp = await client.post(
            "/switch-role",
            data={"role": RoleType.DESIGNER, "next": "/ai-input/dashboard"},
            headers={"X-CSRF-Token": csrf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/ai-input/dashboard"

    @pytest.mark.asyncio
    async def test_switch_role_ignores_external_next(
        self, client: AsyncClient, multi_role_user: User, csrf_token: str
    ):
        """外部URLの next はトップに握りつぶす（オープンリダイレクト対策）"""
        _set_active(client, multi_role_user, act=RoleType.COLLECTOR)
        resp = await client.post(
            "/switch-role",
            data={"role": RoleType.DESIGNER, "next": "https://evil.example/steal"},
            headers={"X-CSRF-Token": csrf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"


class TestLockedNav:
    def test_single_role_user_sees_only_its_own_items(self):
        """単一役割のユーザーのナビは従来どおり（施錠項目で溢れさせない）"""
        from types import SimpleNamespace

        user = SimpleNamespace(
            active_role=RoleType.COLLECTOR,
            switchable_role_names=lambda: [RoleType.COLLECTOR],
        )
        items = nav_items(user)
        assert [i["label"] for i in items] == ["データセット管理", "症例割り当て", "入力対象一覧"]
        assert all(not i["locked"] for i in items)

    def test_multi_role_user_sees_reachable_items_locked(self):
        """切替えれば届く項目は消さずに 🔒 で見せ、担当役割を添える"""
        from types import SimpleNamespace

        # 管理者に加えて収集者・設計者を**明示的に**付与された利用者（管理者は自動では持たない）
        user = SimpleNamespace(
            active_role=RoleType.ADMIN,
            switchable_role_names=lambda: list(RoleType.PRIORITY),
        )
        items = nav_items(user)
        by_label = {i["label"]: i for i in items}
        assert by_label["ユーザー管理"]["locked"] is False
        assert by_label["入力対象一覧"]["locked"] is True
        assert by_label["入力対象一覧"]["owner"] == RoleType.COLLECTOR
        assert by_label["取得メトリクス"]["locked"] is True

    @pytest.mark.asyncio
    async def test_nav_renders_locked_item(self, client: AsyncClient, multi_role_user: User):
        """画面にも 🔒 と担当役割が出る"""
        _set_active(client, multi_role_user)
        resp = await client.get("/ai-input/dashboard", headers=HTML)
        assert resp.status_code == 200
        assert "nav-locked" in resp.text
        assert "入力対象一覧" in resp.text
