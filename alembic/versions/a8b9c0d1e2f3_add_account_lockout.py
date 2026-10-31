"""add_account_lockout

アカウントロック機構のスキーマ拡張:
- users に failed_login_count（連続ログイン失敗回数, 既定 0）と locked_until
  （ロック解除時刻, NULL可）を追加
- auth_events テーブルを新設（ロック/解除イベントの追記専用監査ログ）

SQLite は既存テーブルへの ALTER ADD CONSTRAINT を持たないため、users への追加列は
物理 FK を張らず単純追加とする。新設テーブルの FK は create_table 内で宣言する。
NOT NULL 列 failed_login_count は既存行のため server_default('0') を付ける。

Revision ID: a8b9c0d1e2f3
Revises: b3c4d5e6f7a8
Create Date: 2026-07-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "failed_login_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "users",
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "auth_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("failed_count", sa.Integer(), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_auth_events_user_id"), "auth_events", ["user_id"])
    op.create_index(op.f("ix_auth_events_action"), "auth_events", ["action"])


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_events_action"), table_name="auth_events")
    op.drop_index(op.f("ix_auth_events_user_id"), table_name="auth_events")
    op.drop_table("auth_events")
    op.drop_column("users", "locked_until")
    op.drop_column("users", "failed_login_count")
