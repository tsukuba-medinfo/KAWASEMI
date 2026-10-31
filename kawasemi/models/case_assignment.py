"""症例割り当てモデル定義"""

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.questionnaire import Questionnaire
    from kawasemi.models.user import User


class AssignmentStatus:
    """割り当てステータスの定数"""

    PENDING = "pending"  # 未着手
    IN_PROGRESS = "in_progress"  # 入力中
    COMPLETED = "completed"  # 完了
    CANCELLED = "cancelled"  # キャンセル

    ALL = [PENDING, IN_PROGRESS, COMPLETED, CANCELLED]

    LABELS = {
        PENDING: "未着手",
        IN_PROGRESS: "入力中",
        COMPLETED: "完了",
        CANCELLED: "キャンセル",
    }


class CaseAssignment(Base, TimestampMixin):
    """症例割り当てモデル

    どの患者（patient_id）のどのQuestionnaire（questionnaire_id）を
    誰（assigned_to）が入力するかを管理する
    """

    __tablename__ = "case_assignments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # 割り当て対象
    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"), nullable=False, index=True
    )
    patient_id: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True, comment="FHIR Patient ID または識別子"
    )
    subject_id: Mapped[int | None] = mapped_column(
        ForeignKey("subjects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="被験者（Hub）。patient_id 文字列からの移行先。当面 nullable",
    )
    patient_name: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="患者名（表示用）"
    )

    # 担当者
    assigned_to_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # 割り当て者
    assigned_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # ステータス
    status: Mapped[str] = mapped_column(
        String(20), default=AssignmentStatus.PENDING, nullable=False, index=True
    )

    # 期限
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # メモ
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # 完了日時
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # リレーション
    questionnaire: Mapped["Questionnaire"] = relationship("Questionnaire")
    assigned_to: Mapped["User"] = relationship("User", foreign_keys=[assigned_to_id])
    assigned_by: Mapped["User | None"] = relationship("User", foreign_keys=[assigned_by_id])

    @property
    def status_label(self) -> str:
        """ステータスの日本語ラベルを取得"""
        return AssignmentStatus.LABELS.get(self.status, self.status)

    @property
    def is_overdue(self) -> bool:
        """期限切れかどうかを判定"""
        if self.due_date is None:
            return False
        if self.status in [AssignmentStatus.COMPLETED, AssignmentStatus.CANCELLED]:
            return False
        return date.today() > self.due_date
