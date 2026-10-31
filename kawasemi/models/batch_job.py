"""バッチAI自動入力ジョブのモデル定義

データセット内の 1 Questionnaire に対する「複数患者の一括AI自動入力」を 1 ジョブとして
表す。バックグラウンドタスクが pending な InputTarget を逐次処理し、各件の InputExecution
（AIドラフト）を生成しながら進捗カウンタを更新する。画面はこのレコードをポーリングして
進捗を表示する（ページを閉じてもジョブは継続する）。
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, false, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from kawasemi.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from kawasemi.models.dataset import Dataset
    from kawasemi.models.questionnaire import Questionnaire
    from kawasemi.models.user import User


class BatchJobStatus:
    """バッチジョブの状態定数"""

    RUNNING = "running"  # 実行中
    COMPLETED = "completed"  # 正常終了（個々の成否は succeeded/partial/failed に集計）
    FAILED = "failed"  # ジョブ自体が致命的エラーで停止
    CANCELED = "canceled"  # 利用者がキャンセル

    ALL = [RUNNING, COMPLETED, FAILED, CANCELED]

    LABELS = {
        RUNNING: "実行中",
        COMPLETED: "完了",
        FAILED: "失敗",
        CANCELED: "キャンセル",
    }


class BatchJob(Base, TimestampMixin):
    """バッチAI自動入力ジョブ

    スコープは (dataset, questionnaire)。対象は実行時点で status=pending の InputTarget 群。
    """

    __tablename__ = "ai_input_batch_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    dataset_id: Mapped[int] = mapped_column(
        ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    questionnaire_id: Mapped[int] = mapped_column(
        ForeignKey("questionnaires.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    model: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="使用するLLMモデル名（任意・未指定はデフォルト）"
    )

    # 試験識別子。(study, subject) で被験者を一意化し、resolver が各システムの患者キーを
    # 解決する際に使う（autofill の study_id と同じ用途）。未指定可（登録番号のみ構成）。
    study_id: Mapped[str] = mapped_column(
        String(255), default="", server_default=text("''"), nullable=False
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default=BatchJobStatus.RUNNING,
        server_default=text("'running'"),
        nullable=False,
        index=True,
    )

    # 進捗カウンタ（processed = succeeded + partial + failed）
    total: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"), nullable=False)
    processed: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    succeeded: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    partial: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
    failed: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )

    # キャンセル要求フラグ。ランナーが各件の処理前に確認して途中停止する。
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )

    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    dataset: Mapped["Dataset"] = relationship("Dataset")
    questionnaire: Mapped["Questionnaire"] = relationship("Questionnaire")
    created_by: Mapped["User | None"] = relationship("User", foreign_keys=[created_by_id])

    @property
    def status_label(self) -> str:
        return BatchJobStatus.LABELS.get(self.status, self.status)

    @property
    def is_active(self) -> bool:
        return self.status == BatchJobStatus.RUNNING
