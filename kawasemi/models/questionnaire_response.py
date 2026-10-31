"""QuestionnaireResponse モデル定義"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.questionnaire import Questionnaire
    from kawasemi.models.user import User
    from kawasemi.models.validation_log import ValidationLog


class VerificationStatus:
    """回答の検証ワークフロー状態。

    FHIR の `status`（in-progress / completed / amended）とは独立した、検証者による
    サインオフのライフサイクル。
      unverified: 提出済・未検証（既定）
      verified:   検証者が承認（施錠＝以後編集不可）
      rejected:   検証者が差戻し（収集者の再編集→再提出で unverified に戻る）
    """

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    REJECTED = "rejected"

    ALL = [UNVERIFIED, VERIFIED, REJECTED]

    LABELS: dict[str, str] = {
        UNVERIFIED: "未検証",
        VERIFIED: "検証済み",
        REJECTED: "差戻し",
    }

    @classmethod
    def label(cls, value: str | None) -> str:
        return cls.LABELS.get(value or "", value or "")


class VerificationAction:
    """検証監査ログ（ResponseVerificationLog）のイベント種別。"""

    VERIFIED = "verified"  # 検証者が承認
    REJECTED = "rejected"  # 検証者が差戻し
    RESUBMITTED = "resubmitted"  # 差戻し後に収集者が再編集・再提出

    ALL = [VERIFIED, REJECTED, RESUBMITTED]


class QuestionnaireResponse(Base, TimestampMixin):
    """
    QuestionnaireResponse（質問票回答）モデル

    FHIR QuestionnaireResponse リソースを保存するテーブル
    """

    __tablename__ = "questionnaire_responses"

    # プライマリキー
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # FHIR リソース ID
    fhir_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    # 参照する Questionnaire
    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # QuestionnaireResponse の完全なJSON内容（JSONB型で保存）
    content: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    # 検索用に抽出したメタデータ
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, index=True
    )  # in-progress, completed, amended, etc.
    subject: Mapped[str | None] = mapped_column(
        Text, nullable=True, index=True
    )  # Patient参照（例: "Patient/123"）
    authored: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )  # 作成日時（FHIR dateTime形式）
    author: Mapped[str | None] = mapped_column(Text, nullable=True)  # 作成者参照

    # --- 検証ワークフロー ---
    # 提出者（記入責任者）。SoD（自己検証禁止）の判定に使う。author はフリーテキストの
    # FHIR 参照で信頼できないため、ユーザ FK を別に持つ。
    submitted_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # 検証状態（unverified / verified / rejected）。FHIR status とは独立。
    verification_status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
        server_default=VerificationStatus.UNVERIFIED,
        index=True,
    )
    # サインオフ: 誰がいつ検証(verified)したか（rejected/unverified では null）。
    verified_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 差戻し理由（rejected のとき）。
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # リレーション
    questionnaire: Mapped[Questionnaire] = relationship("Questionnaire", back_populates="responses")
    validation_logs: Mapped[list[ValidationLog]] = relationship(
        "ValidationLog", back_populates="questionnaire_response", cascade="all, delete-orphan"
    )
    submitted_by: Mapped[User | None] = relationship("User", foreign_keys=[submitted_by_id])
    verified_by: Mapped[User | None] = relationship("User", foreign_keys=[verified_by_id])
    verification_logs: Mapped[list[ResponseVerificationLog]] = relationship(
        "ResponseVerificationLog",
        back_populates="questionnaire_response",
        cascade="all, delete-orphan",
        order_by="ResponseVerificationLog.id",
    )

    def __repr__(self) -> str:
        return f"<QuestionnaireResponse(id={self.id}, fhir_id='{self.fhir_id}', subject='{self.subject}')>"


class ResponseVerificationLog(Base, TimestampMixin):
    """検証イベントの追記専用監査ログ（誰が・いつ・どの操作・理由）。

    検証ワークフローの状態遷移（verified / rejected / resubmitted）を時系列で残す。
    現在状態は QuestionnaireResponse 側のカラムが持ち、本表は履歴（監査証跡）を担う。
    """

    __tablename__ = "response_verification_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    questionnaire_response_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaire_responses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    actor_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # verified/rejected/resubmitted
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    questionnaire_response: Mapped[QuestionnaireResponse] = relationship(
        "QuestionnaireResponse", back_populates="verification_logs"
    )
    actor: Mapped[User | None] = relationship("User", foreign_keys=[actor_id])
