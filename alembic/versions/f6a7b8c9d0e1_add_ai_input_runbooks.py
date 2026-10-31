"""add ai_input_runbooks table (mode 3 runbook)

モード3を「項目ごとのデータ取得手順書（runbook）」へ再定義するための新テーブル。
順序付きステップを steps(JSON) に保持し、status(draft/confirmed) で確認状態を管理する。

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-05-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5f6a7b8c9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_input_runbooks",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
        sa.Column("item_text", sa.Text(), nullable=True),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
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
        sa.UniqueConstraint("questionnaire_id", "link_id", name="uq_ai_input_runbook_qn_link"),
    )
    op.create_index(
        op.f("ix_ai_input_runbooks_questionnaire_id"),
        "ai_input_runbooks",
        ["questionnaire_id"],
    )
    op.create_index(op.f("ix_ai_input_runbooks_link_id"), "ai_input_runbooks", ["link_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_input_runbooks_link_id"), table_name="ai_input_runbooks")
    op.drop_index(
        op.f("ix_ai_input_runbooks_questionnaire_id"),
        table_name="ai_input_runbooks",
    )
    op.drop_table("ai_input_runbooks")
