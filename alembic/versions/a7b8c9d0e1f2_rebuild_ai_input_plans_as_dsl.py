"""rebuild ai_input_plans as DSL program (mode 4/5)

モード4/5をDSLベースに再設計。InputPlan を「1項目=1平坦レコード」から
「実行可能な限定プリミティブのDSLプログラム」へ作り替える。

旧 planner（モード3）は削除済みで ai_input_plans に行は存在しないため、
drop+create の破壊的マイグレーションで作り直す。

新スキーマ: program(JSON) + status(draft/confirmed) + llm_model + item_text。
廃止: source_category / fhir_resource_type / fhir_query_template / fhir_path /
      local_sql / llm_prompt_template / transform_rule。

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-05-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 旧 ai_input_plans を破棄（行は存在しない前提）して新スキーマで再作成
    op.drop_table("ai_input_plans")

    op.create_table(
        "ai_input_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
        sa.Column("item_text", sa.Text(), nullable=True),
        sa.Column("program", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
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
        sa.ForeignKeyConstraint(["questionnaire_id"], ["questionnaires.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("questionnaire_id", "link_id", name="uq_ai_input_plan_qn_link"),
    )
    op.create_index(
        op.f("ix_ai_input_plans_questionnaire_id"),
        "ai_input_plans",
        ["questionnaire_id"],
    )
    op.create_index(op.f("ix_ai_input_plans_link_id"), "ai_input_plans", ["link_id"])


def downgrade() -> None:
    # 旧フラットスキーマへ戻す
    op.drop_index(op.f("ix_ai_input_plans_link_id"), table_name="ai_input_plans")
    op.drop_index(op.f("ix_ai_input_plans_questionnaire_id"), table_name="ai_input_plans")
    op.drop_table("ai_input_plans")

    op.create_table(
        "ai_input_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
        sa.Column(
            "source_category",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("fhir_resource_type", sa.String(length=64), nullable=True),
        sa.Column("fhir_query_template", sa.Text(), nullable=True),
        sa.Column("fhir_path", sa.String(length=255), nullable=True),
        sa.Column("local_sql", sa.Text(), nullable=True),
        sa.Column("llm_prompt_template", sa.Text(), nullable=True),
        sa.Column("transform_rule", sa.Text(), nullable=True),
        sa.Column("llm_model", sa.String(length=128), nullable=True),
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
        sa.ForeignKeyConstraint(["questionnaire_id"], ["questionnaires.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("questionnaire_id", "link_id", name="uq_ai_input_plan_qn_link"),
    )
    op.create_index(
        op.f("ix_ai_input_plans_questionnaire_id"),
        "ai_input_plans",
        ["questionnaire_id"],
    )
    op.create_index(op.f("ix_ai_input_plans_link_id"), "ai_input_plans", ["link_id"])
