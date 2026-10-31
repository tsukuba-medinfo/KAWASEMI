"""add_valueset_table

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "valuesets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fhir_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("is_deleted", sa.Boolean(), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_valuesets_fhir_id"), "valuesets", ["fhir_id"], unique=True)
    op.create_index(op.f("ix_valuesets_is_deleted"), "valuesets", ["is_deleted"], unique=False)
    op.create_index(op.f("ix_valuesets_status"), "valuesets", ["status"], unique=False)
    op.create_index(op.f("ix_valuesets_title"), "valuesets", ["title"], unique=False)
    op.create_index(op.f("ix_valuesets_url"), "valuesets", ["url"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_valuesets_url"), table_name="valuesets")
    op.drop_index(op.f("ix_valuesets_title"), table_name="valuesets")
    op.drop_index(op.f("ix_valuesets_status"), table_name="valuesets")
    op.drop_index(op.f("ix_valuesets_is_deleted"), table_name="valuesets")
    op.drop_index(op.f("ix_valuesets_fhir_id"), table_name="valuesets")
    op.drop_table("valuesets")
