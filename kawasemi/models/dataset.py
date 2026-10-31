"""データセットモデル定義"""

from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.input_target import InputTarget
    from kawasemi.models.questionnaire import Questionnaire
    from kawasemi.models.user import User


class Dataset(Base, TimestampMixin):
    """データセットモデル

    複数のQuestionnaireと複数のPatient IDの組み合わせを管理する。
    作成時にInputTargetをデカルト積で一括生成する。
    """

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    created_by: Mapped["User | None"] = relationship("User", foreign_keys=[created_by_id])
    dataset_questionnaires: Mapped[list["DatasetQuestionnaire"]] = relationship(
        "DatasetQuestionnaire",
        back_populates="dataset",
        cascade="all, delete-orphan",
        order_by="DatasetQuestionnaire.display_order",
    )
    input_targets: Mapped[list["InputTarget"]] = relationship(
        "InputTarget",
        back_populates="dataset",
        cascade="all, delete-orphan",
    )


class DatasetQuestionnaire(Base):
    """データセットとQuestionnaireの関連モデル"""

    __tablename__ = "dataset_questionnaires"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"), nullable=False, index=True
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    dataset: Mapped["Dataset"] = relationship("Dataset", back_populates="dataset_questionnaires")
    questionnaire: Mapped["Questionnaire"] = relationship("Questionnaire")

    __table_args__ = (
        UniqueConstraint("dataset_id", "questionnaire_id", name="uq_dataset_questionnaire"),
    )
