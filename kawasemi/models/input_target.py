"""入力対象モデル定義"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, false
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.dataset import Dataset
    from kawasemi.models.questionnaire import Questionnaire
    from kawasemi.models.user import User


class InputTargetStatus:
    """入力対象ステータスの定数"""

    PENDING = "pending"  # 未着手
    IN_PROGRESS = "in_progress"  # 入力中（人がロックして手入力中）
    AI_DRAFTED = "ai_drafted"  # AIがバッチでドラフト生成済み・人のレビュー待ち
    COMPLETED = "completed"  # 完了

    ALL = [PENDING, IN_PROGRESS, AI_DRAFTED, COMPLETED]

    LABELS = {
        PENDING: "未着手",
        IN_PROGRESS: "入力中",
        AI_DRAFTED: "AIドラフト済み",
        COMPLETED: "完了",
    }


class InputTarget(Base, TimestampMixin):
    """入力対象モデル

    データセット内のQuestionnaire × Patient IDの1組み合わせを表す。
    入力担当者が選んで入力する単位。
    1件につき1人がロックして入力する（started_by_idが設定されたらロック済み）。
    """

    __tablename__ = "input_targets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
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

    status: Mapped[str] = mapped_column(
        String(20), default=InputTargetStatus.PENDING, nullable=False, index=True
    )

    # 入力開始者（ロック兼記録）。チェックモードでレビューを開いた人もここに記録する
    # （ソフトロック: 他者には「レビュー中」を表示、開くこと自体は妨げない）。
    started_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # チェックで「要確認（差し戻し/後回し）」と人が印を付けた状態。状態は ai_drafted の
    # まま、後でフラグ付きだけ絞り込んで見直すために使う。
    needs_attention: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # リレーション
    dataset: Mapped["Dataset"] = relationship("Dataset", back_populates="input_targets")
    questionnaire: Mapped["Questionnaire"] = relationship("Questionnaire")
    started_by: Mapped["User | None"] = relationship("User", foreign_keys=[started_by_id])

    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "questionnaire_id",
            "patient_id",
            name="uq_input_target_dataset_questionnaire_patient",
        ),
    )

    @property
    def status_label(self) -> str:
        return InputTargetStatus.LABELS.get(self.status, self.status)
