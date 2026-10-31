"""リフレッシュトークンモデル。

DB 保存＋ローテーション式。ログイン時に1件発行し、`POST /token/refresh` のたびに
旧トークンを revoke して新トークンへ差し替える（再利用検知・失効を可能にする）。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from kawasemi.models.base import Base, TimestampMixin


class RefreshToken(Base, TimestampMixin):
    """発行済みリフレッシュトークン（jti 単位）。"""

    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # JWT の jti（一意）。トークン本体は保存せず jti で照合する。
    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # 失効フラグ（ローテーション・ログアウト・明示失効で True）
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
