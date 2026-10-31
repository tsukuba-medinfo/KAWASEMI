"""add_ai_data_fetch_plans

Revision ID: a1b2c3d4e5f6
Revises: 42b8172a7712
Create Date: 2026-04-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "42b8172a7712"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_data_fetch_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column(
            "link_id",
            sa.String(length=255),
            nullable=False,
            comment="Questionnaire item の linkId",
        ),
        sa.Column(
            "item_text",
            sa.Text(),
            nullable=True,
            comment="item の表示テキスト（ログ・表示用）",
        ),
        sa.Column(
            "data_source_type",
            sa.String(length=64),
            nullable=False,
            comment="データソースタイプ（DataSourceType定数）",
        ),
        sa.Column(
            "fetch_instruction",
            sa.Text(),
            nullable=True,
            comment="AI agent 向けのデータ取得指示テキスト",
        ),
        sa.Column(
            "fhir_path",
            sa.String(length=255),
            nullable=True,
            comment="FHIRパス (例: Patient.name[0].text)",
        ),
        sa.Column(
            "notes",
            sa.Text(),
            nullable=True,
            comment="補足メモ（AI設計者向けの注意事項など）",
        ),
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
        sa.ForeignKeyConstraint(
            ["questionnaire_id"],
            ["questionnaires.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "questionnaire_id",
            "link_id",
            name="uq_ai_plan_questionnaire_link",
        ),
    )
    op.create_index(
        "ix_ai_data_fetch_plans_questionnaire_id",
        "ai_data_fetch_plans",
        ["questionnaire_id"],
    )
    op.create_index(
        "ix_ai_data_fetch_plans_link_id",
        "ai_data_fetch_plans",
        ["link_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ai_data_fetch_plans_link_id", table_name="ai_data_fetch_plans")
    op.drop_index("ix_ai_data_fetch_plans_questionnaire_id", table_name="ai_data_fetch_plans")
    op.drop_table("ai_data_fetch_plans")
