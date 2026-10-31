"""add_program_snapshot_to_executions

実行監査証跡のバージョン固定（CSV / ALCOA+ Original・Consistent）:
- ai_input_executions に program_snapshot（JSON）を追加。実行時に使用した各プランの
  DSL program（各 step の reasoning 込み）と plan_id / plan_updated_at を固定保存する。

confirmed プランは版管理なしで in-place 編集され得るため、実行記録がどの版の・どの
設計根拠のプログラムで動いたかを、後日の監査で確定的に復元できるようにする。既存
レコードは実行時スナップショットを持たないため NULL のまま。

Revision ID: d0e1f2a3b4c5
Revises: c0d1e2f3a4b5
Create Date: 2026-07-23

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d0e1f2a3b4c5"
down_revision: str | None = "c0d1e2f3a4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_input_executions",
        sa.Column("program_snapshot", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_input_executions", "program_snapshot")
