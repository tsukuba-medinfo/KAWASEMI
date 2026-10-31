"""add_resource_key_bindings

リソースunit（索引 unit_id）への患者キー宣言（レベル2アノテーション）を
索引から分離した永続層 resource_key_bindings として新設する。複合キーは
同一 unit_id の ordinal 違い複数行で表す。値（名寄せ）は持たない。

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-06-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3e4f5a6b7c8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_key_bindings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("unit_id", sa.String(length=512), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("key_type", sa.String(length=128), nullable=False),
        sa.Column("physical_format", sa.JSON(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("confirmed_by_id", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fingerprint_ref", sa.String(length=255), nullable=True),
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
        sa.ForeignKeyConstraint(["confirmed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unit_id", "ordinal", name="uq_resource_key_binding_unit_ordinal"),
    )
    op.create_index(op.f("ix_resource_key_bindings_unit_id"), "resource_key_bindings", ["unit_id"])
    op.create_index(
        op.f("ix_resource_key_bindings_confirmed_by_id"),
        "resource_key_bindings",
        ["confirmed_by_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_resource_key_bindings_confirmed_by_id"), table_name="resource_key_bindings"
    )
    op.drop_index(op.f("ix_resource_key_bindings_unit_id"), table_name="resource_key_bindings")
    op.drop_table("resource_key_bindings")
