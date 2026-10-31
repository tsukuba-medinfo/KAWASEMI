"""drop_resource_key_bindings

患者キー列への key_type 付与（下書き＋確定）は indexer のワークフロー
（draft → 人確認 → ingest）へ一本化し、確定済み key_type は索引成果物
（column_index.jsonl の key_type）に焼き込む方針に変更した。
WebUI 側の永続層 resource_key_bindings は未使用となったため削除する。
ランタイムのキー解決は DSL step.physical_format を用いるため影響しない。

downgrade は d3e4f5a6b7c8 と同一スキーマでテーブルを再作成する（可逆）。

Revision ID: e7f8a9b0c1d2
Revises: d3e4f5a6b7c8
Create Date: 2026-06-20

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7f8a9b0c1d2"
down_revision: str | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        op.f("ix_resource_key_bindings_confirmed_by_id"), table_name="resource_key_bindings"
    )
    op.drop_index(op.f("ix_resource_key_bindings_unit_id"), table_name="resource_key_bindings")
    op.drop_table("resource_key_bindings")


def downgrade() -> None:
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
