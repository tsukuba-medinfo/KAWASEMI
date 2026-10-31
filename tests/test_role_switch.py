"""役割切替（active_role ハード・スコープ）のテスト

- User モデルの switchable_role_names / default_active_role / can_act_as / active_role
- require_roles がアクティブ役割で判定する（ハード・スコープ）こと
- データ自動収集ダッシュボードのバケツ判定ロジック
"""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from kawasemi.ai_input.routes import _collection_status
from kawasemi.core.auth import create_access_token, decode_access_token
from kawasemi.core.config import settings
from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole


def _user_with_roles(*role_names: str) -> User:
    """role_names を持つ User をインメモリ構築（DB 非依存）"""
    u = User()
    # SQLAlchemy の instrumented collection には実マップ済みインスタンスが必要
    u.user_roles = [UserRole(role=Role(name=name)) for name in role_names]
    return u


def _set_active(client: AsyncClient, user: User, act: str | None = None) -> None:
    data: dict[str, str] = {"sub": str(user.id)}
    if act is not None:
        data["act"] = act
    client.cookies.set(settings.jwt_cookie_name, create_access_token(data=data))


# ===== User モデル =====


class TestActiveRoleModel:
    def test_admin_is_not_a_superset(self):
        """管理者は他の役割のスーパーセットではない。管理者としてしか動けない"""
        u = _user_with_roles(RoleType.ADMIN)
        assert u.switchable_role_names() == [RoleType.ADMIN]
        assert u.default_active_role() == RoleType.ADMIN
        assert not u.can_act_as(RoleType.DESIGNER)
        assert not u.can_act_as(RoleType.COLLECTOR)
        assert not u.can_act_as(RoleType.REVIEWER)

    def test_admin_who_also_holds_designer_can_switch_to_it(self):
        """同じ人が設計もするなら設計者を明示的に付与する（付与した役割にだけ切り替えられる）"""
        u = _user_with_roles(RoleType.ADMIN, RoleType.DESIGNER)
        assert u.switchable_role_names() == [RoleType.ADMIN, RoleType.DESIGNER]
        assert u.can_act_as(RoleType.DESIGNER)
        assert not u.can_act_as(RoleType.COLLECTOR)

    def test_single_role_user_cannot_switch(self):
        """単一役割ユーザーはその役割のみ"""
        u = _user_with_roles(RoleType.DESIGNER)
        assert u.switchable_role_names() == [RoleType.DESIGNER]
        assert u.default_active_role() == RoleType.DESIGNER
        assert not u.can_act_as(RoleType.ADMIN)

    def test_default_active_role_uses_priority(self):
        """複数役割の既定は優先順位の先頭（最高権限）"""
        u = _user_with_roles(RoleType.COLLECTOR, RoleType.DESIGNER)
        assert u.default_active_role() == RoleType.DESIGNER

    def test_active_role_not_shared_across_instances(self):
        """active_role の代入が別 User インスタンスへ漏えいしないこと（番人）。

        _active_role はクラス属性既定 None をインスタンス属性で上書きする設計。
        あるリクエストの active_role 代入が、別インスタンス（=別リクエストで
        ロードされた User）に波及しないという不変条件を固定する。
        """
        u1 = _user_with_roles(RoleType.ADMIN)
        u2 = _user_with_roles(RoleType.ADMIN)
        u1.active_role = RoleType.COLLECTOR
        assert u1.active_role == RoleType.COLLECTOR
        # u2 は影響を受けず既定（最高権限）のまま
        assert u2.active_role == RoleType.ADMIN

    def test_active_role_property(self):
        """未設定なら既定、設定後はその値を返す"""
        u = _user_with_roles(RoleType.ADMIN)
        assert u.active_role == RoleType.ADMIN  # 既定
        u.active_role = RoleType.DESIGNER
        assert u.active_role == RoleType.DESIGNER

    def test_no_role_user(self):
        u = _user_with_roles()
        assert u.switchable_role_names() == []
        assert u.default_active_role() is None


# ===== ハード・スコープ（require_roles がアクティブ役割で判定） =====


class TestHardScope:
    @pytest.mark.asyncio
    async def test_admin_mode_accesses_admin_route(self, client: AsyncClient, admin_user: User):
        """管理者モード（既定）は管理者ルートに入れる"""
        _set_active(client, admin_user)  # act 無し → 既定 admin
        resp = await client.get("/admin/users")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_designer_blocked_from_admin_route(
        self, client: AsyncClient, multi_role_user: User
    ):
        """設計者として動いているときは管理者ルートが 403（ハード・スコープ）"""
        _set_active(client, multi_role_user, act=RoleType.DESIGNER)
        resp = await client.get("/admin/users")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_multi_role_user_acting_as_designer_accesses_dashboard(
        self, client: AsyncClient, multi_role_user: User
    ):
        """設計者へ切替中はデータ自動収集ダッシュボードに入れる"""
        _set_active(client, multi_role_user, act=RoleType.DESIGNER)
        resp = await client.get("/ai-input/dashboard")
        assert resp.status_code == 200
        assert "データ自動収集" in resp.text

    @pytest.mark.asyncio
    async def test_admin_blocked_from_dashboard(self, client: AsyncClient, admin_user: User):
        """管理者はダッシュボードに入れない（切替先も無い）"""
        _set_active(client, admin_user)
        resp = await client.get("/ai-input/dashboard")
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_act_claim_for_designer_is_ignored(
        self, client: AsyncClient, admin_user: User
    ):
        """管理者が JWT の `act` で設計者を名乗っても、保有していないので管理者に落ちる"""
        _set_active(client, admin_user, act=RoleType.DESIGNER)
        resp = await client.get("/ai-input/dashboard")
        assert resp.status_code == 403
        assert resp.json()["active_role"] == RoleType.ADMIN

    @pytest.mark.asyncio
    async def test_data_entry_cannot_act_as_design_role(
        self, client: AsyncClient, collector_user: User
    ):
        """collector は設計者を要求しても既定にフォールバックし 403"""
        _set_active(client, collector_user, act=RoleType.DESIGNER)
        resp = await client.get("/ai-input/dashboard")
        assert resp.status_code == 403


# ===== 役割切替フォーム / POST /switch-role（CSRF） =====


class TestSwitchRoleForm:
    """base.html ナビの役割切替フォームと POST エンドポイントの CSRF 連携。

    回帰: 切替フォームに hidden csrf_token が無く、POST /switch-role が常に
    CSRF_TOKEN_INVALID になっていた不具合を防ぐ。
    """

    @pytest.mark.asyncio
    async def test_nav_form_renders_csrf_hidden_field(
        self, client: AsyncClient, multi_role_user: User, csrf_token: str
    ):
        """複数役割の利用者のナビ切替フォームに raw CSRF トークンの hidden が出る"""
        _set_active(client, multi_role_user)  # 既定 designer（切替可能役割が複数）
        resp = await client.get("/ai-input/dashboard")
        assert resp.status_code == 200
        # 切替フォームと、その中に埋め込まれた raw トークンの hidden 入力
        assert 'action="/switch-role"' in resp.text
        assert 'name="csrf_token"' in resp.text
        assert csrf_token in resp.text

    @pytest.mark.asyncio
    async def test_switch_role_succeeds_with_csrf(
        self, client: AsyncClient, multi_role_user: User, csrf_token: str
    ):
        """csrf 付き POST は 303 で成功し、JWT の act が切り替わる

        本番 JS と同様、トークンは X-CSRF-Token ヘッダで送る（ヘッダ方式）。
        """
        _set_active(client, multi_role_user)
        resp = await client.post(
            "/switch-role",
            data={"role": RoleType.COLLECTOR, "csrf_token": csrf_token},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 303
        new_jwt = resp.cookies.get(settings.jwt_cookie_name)
        assert new_jwt is not None
        payload = decode_access_token(new_jwt)
        assert payload is not None
        assert payload.get("act") == RoleType.COLLECTOR

    @pytest.mark.asyncio
    async def test_switch_role_without_csrf_rejected(self, client: AsyncClient, admin_user: User):
        """csrf 欠落の POST は CSRF エラーで拒否される（保護が効いている）"""
        _set_active(client, admin_user)
        resp = await client.post("/switch-role", data={"role": RoleType.DESIGNER})
        assert resp.status_code >= 400
        assert resp.json()["error_code"] == "CSRF_TOKEN_INVALID"


# ===== ダッシュボードのバケツ判定 =====


class TestCollectionStatus:
    def _ann(self):
        return SimpleNamespace(annotation="臨床的コンテキスト")

    def test_todo_when_no_designed_field(self):
        s = _collection_status([{"link_id": "a"}], {}, {}, {}, {})
        assert s["bucket"] == "todo"
        assert s["next_path"] == "source-survey"

    def test_in_progress_when_plan_missing(self):
        fields = [{"link_id": "a"}, {"link_id": "b"}]
        s = _collection_status(fields, {"a": self._ann()}, {}, {}, {})
        assert s["bucket"] == "in_progress"
        assert s["designed_count"] == 1
        assert s["plan_done"] == 0

    def test_done_when_all_designed_have_plan(self):
        fields = [{"link_id": "a"}]
        s = _collection_status(fields, {"a": self._ann()}, {}, {}, {"a": SimpleNamespace()})
        assert s["bucket"] == "done"
        assert s["plan_done"] == s["designed_count"] == 1
