"""add_confirmed_by_to_ai_artifacts

AIドラフト確認操作の操作者記録（ALCOA+ 帰属性）:
- ai_input_runbooks / ai_input_plans に confirmed_by_id（確認操作者）と
  confirmed_at（確認日時）を追加。status=confirmed 昇格時に記録する。

SQLite は既存テーブルへの ALTER ADD CONSTRAINT を持たないため、confirmed_by_id は
物理 FK を張らず index のみとし、参照整合性は ORM 宣言（create_all 環境）と
アプリ層で担保する。既存の confirmed 済みレコードは操作者不明のため NULL のまま。

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-07-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c0d1e2f3a4"
down_revision: str | None = "a8b9c0d1e2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("ai_input_runbooks", "ai_input_plans"):
        op.add_column(table, sa.Column("confirmed_by_id", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index(op.f(f"ix_{table}_confirmed_by_id"), table, ["confirmed_by_id"])


def downgrade() -> None:
    for table in ("ai_input_runbooks", "ai_input_plans"):
        op.drop_index(op.f(f"ix_{table}_confirmed_by_id"), table_name=table)
        op.drop_column(table, "confirmed_at")
        op.drop_column(table, "confirmed_by_id")
