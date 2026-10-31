"""ValueSet モデル定義"""

from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from kawasemi.models.base import Base, TimestampMixin


class ValueSet(Base, TimestampMixin):
    """FHIR ValueSet リソースを保存するテーブル"""

    __tablename__ = "valuesets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    fhir_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<ValueSet(id={self.id}, fhir_id='{self.fhir_id}', title='{self.title}')>"
