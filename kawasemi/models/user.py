"""ユーザーモデル定義"""

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.role import Role


class User(Base, TimestampMixin):
    """ユーザーモデル"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(100), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # アカウントロック: 連続ログイン失敗回数と、ロック解除時刻。
    # locked_until が未来なら「ロック中」。時間経過 or admin 操作（None化）で解除。
    failed_login_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # リレーション
    user_roles: Mapped[list["UserRole"]] = relationship(
        "UserRole",
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="[UserRole.user_id]",
    )

    # 現在「どの役割として」操作しているか（active_role）。
    # 永続列ではなくリクエストごとに deps が JWT から設定する非マップ属性。
    # 既定 None。設定前でもテンプレートが安全に参照できるよう既定値を持つ。
    #
    # 設計上の不変条件（意図的に ORM インスタンスへ載せている）:
    #   - これは「クラス属性 = None」で、リクエスト処理中に
    #     `user.active_role = ...`（インスタンス属性への代入）で上書きされる。
    #     マップ外属性なので DB 列にならず flush/commit の対象にもならない。
    #   - get_current_user_optional は毎リクエスト DB から User を新規ロードするため、
    #     同一インスタンスが複数リクエストで共有されることはない（＝漏えいしない）。
    #   - 将来 selectinload 済みインスタンスのキャッシュ/使い回しを導入する場合は、
    #     この前提が崩れるため request スコープ（contextvar / request.state）へ
    #     退避すること。test_active_role_not_shared_across_instances が番人。
    _active_role = None

    @property
    def active_role(self) -> str | None:
        """現在アクティブな役割名（未設定なら既定の最高権限役割）"""
        if self._active_role is not None:
            return self._active_role
        return self.default_active_role()

    @active_role.setter
    def active_role(self, value: str | None) -> None:
        self._active_role = value

    def switchable_role_names(self) -> list[str]:
        """切替可能な役割名（優先順）＝保有する役割。

        **管理者は他の役割のスーパーセットではない**。管理者はシステムの運用だけを持ち、
        設計・収集・検証の画面には、その役割を明示的に付与されたときにしか入れない。

        **この入り口（`ENTRY_ROLES`）で使える役割に限る。** 既定の役割・`can_act_as`・
        ヘッダの切替・403 の案内はすべてここを通るので、入り口の制限はこの1か所で効く。
        別の入り口の Cookie（同じホストの別 port は Cookie を共有する）が運んできた
        `act` も、ここで弾かれて既定の役割へ落ちる。
        """
        from kawasemi.core.entry import current_entry
        from kawasemi.models.role import RoleType

        held = set(self.role_names)
        names = [r for r in RoleType.PRIORITY if r in held]
        return current_entry().filter_roles(names)

    def held_role_names_outside_entry(self) -> list[str]:
        """保有しているが、この入り口では使えない役割（ログイン拒否の案内に使う）。"""
        from kawasemi.core.entry import current_entry

        entry = current_entry()
        return [r for r in self.role_names if not entry.allows(r)]

    def default_active_role(self) -> str | None:
        """既定のアクティブ役割（最高権限）。役割が無ければ None。"""
        names = self.switchable_role_names()
        return names[0] if names else None

    def can_act_as(self, role_name: str) -> bool:
        """その役割としてアクティブになれるか"""
        return role_name in self.switchable_role_names()

    @property
    def roles(self) -> list["Role"]:
        """ユーザーが持つロールのリストを取得"""
        return [ur.role for ur in self.user_roles]

    @property
    def role_names(self) -> list[str]:
        """ユーザーが持つロール名のリストを取得"""
        return [ur.role.name for ur in self.user_roles]

    def has_role(self, role_name: str) -> bool:
        """指定されたロールを持っているか確認"""
        return role_name in self.role_names

    def has_any_role(self, role_names: list[str]) -> bool:
        """指定されたロールのいずれかを持っているか確認"""
        return any(role in self.role_names for role in role_names)

    # --- アカウントロック ---

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        """naive datetime（SQLite 由来）を UTC aware として扱う。"""
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)

    def is_locked(self, now: datetime | None = None) -> bool:
        """現在ロック中か（locked_until が未来かどうか）。"""
        if self.locked_until is None:
            return False
        now = now or datetime.now(UTC)
        return self._as_utc(self.locked_until) > now

    def register_failed_login(self, max_attempts: int, lock_minutes: int) -> bool:
        """ログイン失敗を1回記録し、閾値到達でロックする。ロックしたら True。"""
        from datetime import timedelta

        self.failed_login_count += 1
        if self.failed_login_count >= max_attempts:
            self.locked_until = datetime.now(UTC) + timedelta(minutes=lock_minutes)
            return True
        return False

    def reset_lockout(self) -> None:
        """失敗カウントとロックを解除する（ログイン成功・時間経過・admin解除で使用）。"""
        self.failed_login_count = 0
        self.locked_until = None


class UserRole(Base):
    """ユーザーとロールの中間テーブル"""

    __tablename__ = "user_roles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role_id: Mapped[int] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    assigned_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # リレーション
    user: Mapped["User"] = relationship("User", back_populates="user_roles", foreign_keys=[user_id])
    role: Mapped["Role"] = relationship("Role", back_populates="user_roles")
    assigned_by: Mapped["User | None"] = relationship("User", foreign_keys=[assigned_by_id])
