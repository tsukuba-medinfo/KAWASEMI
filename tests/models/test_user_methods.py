"""User モデルのメソッド/プロパティの単体テスト。DB 不要の純粋検証。"""

from datetime import UTC, datetime, timedelta

from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole


def _user(*role_names: str) -> User:
    """指定ロールを持つ User をインメモリ構築する（DB 未使用）。"""
    u = User(username="u", email="u@x", password_hash="h", full_name="U")
    u.user_roles = [UserRole(role=Role(name=r)) for r in role_names]
    return u


# 注: admin スーパーセット / default_active_role / active_role setter・インスタンス間
# 非共有は tests/test_role_switch.py::TestActiveRoleModel が既にカバーしている。
# ここでは重複を避け、そこに無い挙動（非admin の順序保持・has_any_role・ロック系）に絞る。


def test_switchable_roles_non_admin_subset_in_priority_order() -> None:
    u = _user(RoleType.REVIEWER, RoleType.COLLECTOR)
    result = u.switchable_role_names()
    assert set(result) == {RoleType.REVIEWER, RoleType.COLLECTOR}
    # PRIORITY の順序が保たれる
    assert result == [r for r in RoleType.PRIORITY if r in result]


def test_has_role_and_has_any_role() -> None:
    u = _user(RoleType.REVIEWER)
    assert u.has_role(RoleType.REVIEWER)
    assert not u.has_role(RoleType.ADMIN)
    assert u.has_any_role([RoleType.ADMIN, RoleType.REVIEWER])
    assert not u.has_any_role([RoleType.ADMIN, RoleType.COLLECTOR])


def test_is_locked() -> None:
    u = _user()
    assert u.is_locked() is False  # locked_until None
    u.locked_until = datetime.now(UTC) + timedelta(minutes=5)
    assert u.is_locked() is True  # 未来
    u.locked_until = datetime.now(UTC) - timedelta(minutes=5)
    assert u.is_locked() is False  # 過去


def test_is_locked_handles_naive_datetime() -> None:
    """SQLite 由来の naive datetime でも _as_utc で UTC 扱いし比較できる。"""
    u = _user()
    # naive（tz なし）UTC 時刻。SQLite から読み戻した状態を模す
    u.locked_until = (datetime.now(UTC) + timedelta(minutes=5)).replace(tzinfo=None)
    assert u.is_locked() is True


def test_register_failed_login_boundary() -> None:
    u = _user()
    u.failed_login_count = 0  # DB 既定（INSERT 時適用）を明示。DB からロードした状態を模す
    assert u.register_failed_login(max_attempts=3, lock_minutes=30) is False
    assert u.failed_login_count == 1
    assert u.register_failed_login(3, 30) is False
    assert u.failed_login_count == 2
    # 3回目で閾値到達 → ロック
    assert u.register_failed_login(3, 30) is True
    assert u.failed_login_count == 3
    assert u.locked_until is not None and u.is_locked()


def test_reset_lockout() -> None:
    u = _user()
    u.failed_login_count = 5
    u.locked_until = datetime.now(UTC) + timedelta(minutes=30)
    u.reset_lockout()
    assert u.failed_login_count == 0
    assert u.locked_until is None
