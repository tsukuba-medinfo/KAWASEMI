"""add_ai_input_trial_runs

ロール再整理: autofill を実行コンテキストで区別する。
設計クラスタ（収集責任者／取得計画設計者／データ管理者）による「試行(trial)」実行は
結果（prefill_data / evidence）を永続しない揮発プレビューとし、本テーブルには
「誰がいつどの患者で試したか」のメタ情報のみを残す（PHI を蓄積しない）。
本番実行（収集者）は従来どおり ai_input_executions に永続する。

subject_id 追加（c2d3e4f5a6b7）等と同様、SQLite は ALTER ADD CONSTRAINT を持たないため
executed_by_id は物理 FK を張らず index のみ付与し、参照整合性は ORM 宣言（create_all 環境）
とアプリ層で担保する。questionnaire_id の FK は create_table 内で宣言する。

Revision ID: a2b3c4d5e6f7
Revises: f7a8b9c0d1e2
Create Date: 2026-06-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_input_trial_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.String(length=255), nullable=False),
        sa.Column("executed_by_id", sa.Integer(), nullable=True),
        sa.Column("acted_as", sa.String(length=50), nullable=True),
        sa.Column(
            "status", sa.String(length=32), server_default=sa.text("'success'"), nullable=False
        ),
        sa.Column("filled_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.ForeignKeyConstraint(["questionnaire_id"], ["questionnaires.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ai_input_trial_runs_questionnaire_id"),
        "ai_input_trial_runs",
        ["questionnaire_id"],
    )
    op.create_index(
        op.f("ix_ai_input_trial_runs_patient_id"), "ai_input_trial_runs", ["patient_id"]
    )
    op.create_index(
        op.f("ix_ai_input_trial_runs_executed_by_id"),
        "ai_input_trial_runs",
        ["executed_by_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_input_trial_runs_executed_by_id"), table_name="ai_input_trial_runs")
    op.drop_index(op.f("ix_ai_input_trial_runs_patient_id"), table_name="ai_input_trial_runs")
    op.drop_index(op.f("ix_ai_input_trial_runs_questionnaire_id"), table_name="ai_input_trial_runs")
    op.drop_table("ai_input_trial_runs")
