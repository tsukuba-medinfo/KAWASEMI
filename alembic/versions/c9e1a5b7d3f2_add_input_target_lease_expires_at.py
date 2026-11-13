"""input_targets に排他リースの失効時刻 lease_expires_at を追加する

入力対象は管理者が登録した対象リストから先着で自動割り当てし、担当には期限付きの排他リースを
付ける。started_by_id が保持者、lease_expires_at が期限である。NULL は未保持を意味する。
既存行は NULL のまま（リースなし）で、本番データは無いのでデータ移行は不要。

SQLite は既存テーブルへの ALTER ADD CONSTRAINT を持たないが、本列は外部キーを持たない
nullable な DateTime と index だけなので ADD COLUMN / CREATE INDEX で足りる。

Revision ID: c9e1a5b7d3f2
Revises: b8d4f0e2a3c6
Create Date: 2026-11-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9e1a5b7d3f2"
down_revision: str | None = "b8d4f0e2a3c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "input_targets",
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        op.f("ix_input_targets_lease_expires_at"), "input_targets", ["lease_expires_at"]
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_input_targets_lease_expires_at"), table_name="input_targets")
    op.drop_column("input_targets", "lease_expires_at")
