"""add_ai_input_tables

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-04-19

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: str | None = "b2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_input_designs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column(
            "link_id",
            sa.String(length=255),
            nullable=False,
            comment="Questionnaire item の linkId",
        ),
        sa.Column("item_text", sa.Text(), nullable=True, comment="表示用テキスト"),
        sa.Column(
            "source_category",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
            comment="データソース種別: fhir/local_db/llm/manual",
        ),
        sa.Column("description", sa.Text(), nullable=True, comment="この項目が表すものの説明"),
        sa.Column("data_hint", sa.Text(), nullable=True, comment="データ取得先の自然言語ヒント"),
        sa.Column("decision_notes", sa.Text(), nullable=True, comment="判断時の注意点・特記事項"),
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
        sa.UniqueConstraint("questionnaire_id", "link_id", name="uq_ai_input_design_qn_link"),
    )
    op.create_index(
        op.f("ix_ai_input_designs_questionnaire_id"), "ai_input_designs", ["questionnaire_id"]
    )
    op.create_index(op.f("ix_ai_input_designs_link_id"), "ai_input_designs", ["link_id"])

    op.create_table(
        "ai_input_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("design_id", sa.Integer(), nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
        sa.Column(
            "fhir_resource_type",
            sa.String(length=64),
            nullable=True,
            comment="Patient / Encounter / Observation / Condition など",
        ),
        sa.Column(
            "fhir_query_template",
            sa.Text(),
            nullable=True,
            comment="FHIRクエリURLテンプレート (例: /Patient/{{patient_id}})",
        ),
        sa.Column(
            "fhir_path",
            sa.String(length=255),
            nullable=True,
            comment="FHIRパス記法 (例: Patient.name[0].text)",
        ),
        sa.Column(
            "local_sql",
            sa.Text(),
            nullable=True,
            comment=":patient_id などのプレースホルダー付きSQLクエリ",
        ),
        sa.Column(
            "llm_prompt_template",
            sa.Text(),
            nullable=True,
            comment="LLM呼び出し用プロンプトテンプレート",
        ),
        sa.Column(
            "transform_rule",
            sa.Text(),
            nullable=True,
            comment="データ変換ルールのJSON文字列",
        ),
        sa.Column(
            "llm_model",
            sa.String(length=128),
            nullable=True,
            comment="計画生成に使用したLLMモデル名",
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
        sa.ForeignKeyConstraint(["design_id"], ["ai_input_designs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["questionnaire_id"], ["questionnaires.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("questionnaire_id", "link_id", name="uq_ai_input_plan_qn_link"),
    )
    op.create_index(op.f("ix_ai_input_plans_design_id"), "ai_input_plans", ["design_id"])
    op.create_index(
        op.f("ix_ai_input_plans_questionnaire_id"), "ai_input_plans", ["questionnaire_id"]
    )
    op.create_index(op.f("ix_ai_input_plans_link_id"), "ai_input_plans", ["link_id"])

    op.create_table(
        "ai_input_executions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column(
            "patient_id",
            sa.String(length=255),
            nullable=False,
            comment="実行対象の患者ID",
        ),
        sa.Column("executed_by_id", sa.Integer(), nullable=True),
        sa.Column(
            "prefill_data",
            sa.JSON(),
            nullable=True,
            comment="{link_id: value} 形式の補完結果",
        ),
        sa.Column(
            "evidence",
            sa.JSON(),
            nullable=True,
            comment="{link_id: {source_type, query_used, raw_data, path_used, reasoning}} 形式の根拠",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="success",
            comment="success / partial / failed",
        ),
        sa.Column("error_summary", sa.Text(), nullable=True, comment="エラー概要（失敗時）"),
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
        sa.ForeignKeyConstraint(["executed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ai_input_executions_questionnaire_id"),
        "ai_input_executions",
        ["questionnaire_id"],
    )
    op.create_index(
        op.f("ix_ai_input_executions_patient_id"), "ai_input_executions", ["patient_id"]
    )
    op.create_index(
        op.f("ix_ai_input_executions_executed_by_id"),
        "ai_input_executions",
        ["executed_by_id"],
    )


def downgrade() -> None:
    op.drop_table("ai_input_executions")
    op.drop_table("ai_input_plans")
    op.drop_table("ai_input_designs")
