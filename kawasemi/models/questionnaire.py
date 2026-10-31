"""Questionnaire モデル定義"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.questionnaire_response import QuestionnaireResponse


class Questionnaire(Base, TimestampMixin):
    """
    Questionnaire（質問票テンプレート）モデル

    FHIR Questionnaire リソースを保存するテーブル
    """

    __tablename__ = "questionnaires"

    # プライマリキー
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # FHIR リソース ID（FHIR仕様のID）
    fhir_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    # Questionnaire の完全なJSON内容（JSONB型で保存）
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # 検索用に抽出したメタデータ
    title: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # draft, active, retired, etc.
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True, unique=True, index=True)

    # 論理削除フラグ
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

    # リレーション
    responses: Mapped[list[QuestionnaireResponse]] = relationship(
        "QuestionnaireResponse", back_populates="questionnaire", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Questionnaire(id={self.id}, fhir_id='{self.fhir_id}', title='{self.title}')>"
