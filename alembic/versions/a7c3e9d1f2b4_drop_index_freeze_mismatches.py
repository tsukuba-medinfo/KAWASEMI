"""ai_input_design_llm_calls から index_freeze_mismatches を落とす

索引を固定した記録との照合をやめたため、その結果を入れていた列を外す。

Revision ID: a7c3e9d1f2b4
Revises: d5e6f7a8b9c0
Create Date: 2026-10-31

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c3e9d1f2b4"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ai_input_design_llm_calls") as batch_op:
        batch_op.drop_column("index_freeze_mismatches")


def downgrade() -> None:
    with op.batch_alter_table("ai_input_design_llm_calls") as batch_op:
        batch_op.add_column(sa.Column("index_freeze_mismatches", sa.JSON(), nullable=True))
