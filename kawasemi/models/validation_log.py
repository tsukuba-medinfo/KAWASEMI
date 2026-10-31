"""ValidationLog モデル定義"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.questionnaire_response import QuestionnaireResponse


class ValidationLog(Base, TimestampMixin):
    """
    ValidationLog（バリデーションログ）モデル

    Questionnaire および QuestionnaireResponse のバリデーション結果を保存
    """

    __tablename__ = "validation_logs"

    # プライマリキー
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # リソースタイプ（questionnaire または questionnaire_response）
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # リソースID（QuestionnaireResponseのみ外部キー制約）
    questionnaire_response_id: Mapped[int | None] = mapped_column(
        ForeignKey("questionnaire_responses.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # バリデーション結果（JSON形式）
    validation_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # バリデーション成功フラグ
    is_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)

    # エラーメッセージ（日本語）
    error_summary: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # リレーション
    questionnaire_response: Mapped[QuestionnaireResponse | None] = relationship(
        "QuestionnaireResponse", back_populates="validation_logs"
    )

    def __repr__(self) -> str:
        return f"<ValidationLog(id={self.id}, resource_type='{self.resource_type}', is_valid={self.is_valid})>"
