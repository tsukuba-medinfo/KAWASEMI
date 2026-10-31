"""add_batch_jobs_and_ai_drafted

バッチAI自動入力とチェックモードのためのスキーマ拡張:
- ai_input_batch_jobs テーブルを新設（(dataset, questionnaire) スコープの一括実行ジョブ）
- input_targets.needs_attention（チェックの「要確認」フラグ）を追加
- ai_input_executions.batch_job_id（どのバッチが生成したか）を追加（index のみ）

input_targets.status の "ai_drafted"（AIドラフト済み・レビュー待ち）は文字列値であり
列定義（String(20)）の変更を伴わないため DDL 変更はない。

subject_id 追加（c2d3e4f5a6b7）と同様、SQLite は既存テーブルへの ALTER ADD CONSTRAINT を
持たないため batch_job_id は物理 FK を張らず index のみ付与し、参照整合性は ORM 宣言
（create_all 環境）とアプリ層で担保する。新設 ai_input_batch_jobs テーブルの FK は
create_table 内で宣言する。

Revision ID: f7a8b9c0d1e2
Revises: e7f8a9b0c1d2
Create Date: 2026-06-26

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: str | None = "e7f8a9b0c1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_input_batch_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", sa.Integer(), nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("created_by_id", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("study_id", sa.String(length=255), server_default=sa.text("''"), nullable=False),
        sa.Column(
            "status", sa.String(length=20), server_default=sa.text("'running'"), nullable=False
        ),
        sa.Column("total", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("processed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("succeeded", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("partial", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["dataset_id"], ["datasets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["questionnaire_id"], ["questionnaires.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ai_input_batch_jobs_dataset_id"), "ai_input_batch_jobs", ["dataset_id"]
    )
    op.create_index(
        op.f("ix_ai_input_batch_jobs_questionnaire_id"),
        "ai_input_batch_jobs",
        ["questionnaire_id"],
    )
    op.create_index(
        op.f("ix_ai_input_batch_jobs_created_by_id"), "ai_input_batch_jobs", ["created_by_id"]
    )
    op.create_index(op.f("ix_ai_input_batch_jobs_status"), "ai_input_batch_jobs", ["status"])

    # input_targets.needs_attention（要確認フラグ）
    op.add_column(
        "input_targets",
        sa.Column("needs_attention", sa.Boolean(), server_default=sa.false(), nullable=False),
    )

    # ai_input_executions.batch_job_id（index のみ。物理 FK は張らない）
    op.add_column("ai_input_executions", sa.Column("batch_job_id", sa.Integer(), nullable=True))
    op.create_index(
        op.f("ix_ai_input_executions_batch_job_id"), "ai_input_executions", ["batch_job_id"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_input_executions_batch_job_id"), table_name="ai_input_executions")
    op.drop_column("ai_input_executions", "batch_job_id")

    op.drop_column("input_targets", "needs_attention")

    op.drop_index(op.f("ix_ai_input_batch_jobs_status"), table_name="ai_input_batch_jobs")
    op.drop_index(op.f("ix_ai_input_batch_jobs_created_by_id"), table_name="ai_input_batch_jobs")
    op.drop_index(op.f("ix_ai_input_batch_jobs_questionnaire_id"), table_name="ai_input_batch_jobs")
    op.drop_index(op.f("ix_ai_input_batch_jobs_dataset_id"), table_name="ai_input_batch_jobs")
    op.drop_table("ai_input_batch_jobs")
