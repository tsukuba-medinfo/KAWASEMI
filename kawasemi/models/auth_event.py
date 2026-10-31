"""認証・アカウントロックの監査ログモデル。

ResponseVerificationLog と同じ「追記専用・actor_id 付き」の監査ログ様式に倣う。
アカウントのロック（自動）と解除（時間経過 / admin 操作）を証跡として残す。
"""

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from kawasemi.models.base import Base, TimestampMixin


class AuthEventAction:
    """認証監査イベントの種別。"""

    LOCKED = "locked"  # 連続失敗によりロック（自動、actor なし）
    UNLOCKED = "unlocked"  # ロック解除（admin 操作 or 時間経過）

    ALL = [LOCKED, UNLOCKED]


class AuthEvent(Base, TimestampMixin):
    """認証・アカウントロックの監査ログ（追記専用）。"""

    __tablename__ = "auth_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 対象ユーザー。ユーザー削除後も証跡を残すため SET NULL。
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 操作者（admin 解除時）。自動ロック・時間経過解除では None。
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 補足（連続失敗回数・解除経路など）。PHI は含めない。
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 失敗回数の記録（ロック時点の連続失敗数など）
    failed_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
