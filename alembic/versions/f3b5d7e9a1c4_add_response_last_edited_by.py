"""add_response_last_edited_by

回答の最後の編集者を記録する（職務分離の判定用）。

- questionnaire_responses に last_edited_by_id を追加（NULL 可、index 付き）

既存の `submitted_by_id` / `verified_by_id` と同じ流儀で、追加カラムのユーザ参照には
物理 FK を張らない（SQLite は既存テーブルへの ALTER ADD CONSTRAINT を持たない）。
参照整合性は ORM 宣言（create_all 環境）とアプリ層で担保する。

Revision ID: f3b5d7e9a1c4
Revises: e2a4c6e8b0d1
Create Date: 2026-11-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b5d7e9a1c4"
down_revision: str | None = "e2a4c6e8b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "questionnaire_responses",
        sa.Column("last_edited_by_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_questionnaire_responses_last_edited_by_id"),
        "questionnaire_responses",
        ["last_edited_by_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_questionnaire_responses_last_edited_by_id"),
        table_name="questionnaire_responses",
    )
    op.drop_column("questionnaire_responses", "last_edited_by_id")
