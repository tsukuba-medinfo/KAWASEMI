"""add_user_token_version

アクセストークンの失効（世代番号）:
- users に token_version（既定 0）を追加する。アクセストークンの `tv` クレームと
  照合し、パスワード再設定・ログアウトで +1 して発行済みトークンを無効にする。

既存行は server_default('0') で 0 になる。既存トークンには `tv` が無いが、`tv` が無い
ものは 0 として扱うので、デプロイ直後に全員が締め出されることはない。

Revision ID: d1f3b5a7c9e2
Revises: c9e1a5b7d3f2
Create Date: 2026-11-13

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d1f3b5a7c9e2"
down_revision: str | None = "c9e1a5b7d3f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("users", "token_version")
