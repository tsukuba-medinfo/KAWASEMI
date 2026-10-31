"""split ai design into two roles (clinical_annotations / system_locations)

既存の ai_input_designs を臨床的コンテキスト(clinical_annotations)とシステム場所(system_locations)
の2テーブルに分離し、レガシーの ai_data_fetch_plans を廃止する。
ai_input_plans は design_id を廃止し source_category を非正規化カラムとして持つ。

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-05-30

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. 臨床的コンテキストアノテーション ---
    op.create_table(
        "clinical_annotations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
        sa.Column("item_text", sa.Text(), nullable=True),
        sa.Column("annotation", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("questionnaire_id", "link_id", name="uq_clinical_annotation_qn_link"),
    )
    op.create_index(
        op.f("ix_clinical_annotations_questionnaire_id"),
        "clinical_annotations",
        ["questionnaire_id"],
    )
    op.create_index(op.f("ix_clinical_annotations_link_id"), "clinical_annotations", ["link_id"])

    # --- 2. システム場所 ---
    op.create_table(
        "system_locations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
        sa.Column("item_text", sa.Text(), nullable=True),
        sa.Column(
            "source_category",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("location", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("questionnaire_id", "link_id", name="uq_system_location_qn_link"),
    )
    op.create_index(
        op.f("ix_system_locations_questionnaire_id"),
        "system_locations",
        ["questionnaire_id"],
    )
    op.create_index(op.f("ix_system_locations_link_id"), "system_locations", ["link_id"])

    # --- 3. 既存 ai_input_designs からデータ移行 ---
    # description + decision_notes → annotation
    # 改行は束縛パラメータで渡す（SQLite の char(10) は PostgreSQL に無い）。
    op.execute(
        sa.text(
            """
        INSERT INTO clinical_annotations
            (questionnaire_id, link_id, item_text, annotation, created_at, updated_at)
        SELECT
            questionnaire_id,
            link_id,
            item_text,
            NULLIF(
                TRIM(
                    COALESCE(description, '')
                    || CASE
                         WHEN COALESCE(decision_notes, '') != ''
                         THEN CASE WHEN COALESCE(description, '') != '' THEN :newline ELSE '' END
                              || '注意事項: ' || decision_notes
                         ELSE ''
                       END
                ),
                ''
            ),
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM ai_input_designs
        """
        ).bindparams(newline="\n")
    )
    # source_category + data_hint → location
    op.execute(
        """
        INSERT INTO system_locations
            (questionnaire_id, link_id, item_text, source_category, location, created_at, updated_at)
        SELECT
            questionnaire_id,
            link_id,
            item_text,
            source_category,
            data_hint,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM ai_input_designs
        """
    )

    # --- 4. ai_input_plans を design_id 廃止 + source_category 追加で作り直す ---
    # （プラン内容はモード3で再生成可能なため再構築する）
    op.drop_index(op.f("ix_ai_input_plans_design_id"), table_name="ai_input_plans")
    op.drop_index(op.f("ix_ai_input_plans_questionnaire_id"), table_name="ai_input_plans")
    op.drop_index(op.f("ix_ai_input_plans_link_id"), table_name="ai_input_plans")
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
        op.f("ix_ai_input_plans_questionnaire_id"), "ai_input_plans", ["questionnaire_id"]
    )
    op.create_index(op.f("ix_ai_input_plans_link_id"), "ai_input_plans", ["link_id"])

    # --- 5. 旧 ai_input_designs を廃止 ---
    op.drop_index(op.f("ix_ai_input_designs_link_id"), table_name="ai_input_designs")
    op.drop_index(op.f("ix_ai_input_designs_questionnaire_id"), table_name="ai_input_designs")
    op.drop_table("ai_input_designs")

    # --- 6. レガシー ai_data_fetch_plans を廃止 ---
    op.drop_index("ix_ai_data_fetch_plans_link_id", table_name="ai_data_fetch_plans")
    op.drop_index("ix_ai_data_fetch_plans_questionnaire_id", table_name="ai_data_fetch_plans")
    op.drop_table("ai_data_fetch_plans")


def downgrade() -> None:
    # レガシー ai_data_fetch_plans を復元
    op.create_table(
        "ai_data_fetch_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
        sa.Column("item_text", sa.Text(), nullable=True),
        sa.Column(
            "data_source_type",
            sa.String(length=64),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("fetch_instruction", sa.Text(), nullable=True),
        sa.Column("fhir_path", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.UniqueConstraint("questionnaire_id", "link_id", name="uq_ai_plan_questionnaire_link"),
    )
    op.create_index(
        "ix_ai_data_fetch_plans_questionnaire_id",
        "ai_data_fetch_plans",
        ["questionnaire_id"],
    )
    op.create_index("ix_ai_data_fetch_plans_link_id", "ai_data_fetch_plans", ["link_id"])

    # ai_input_designs を復元
    op.create_table(
        "ai_input_designs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
        sa.Column("item_text", sa.Text(), nullable=True),
        sa.Column(
            "source_category",
            sa.String(length=32),
            server_default="manual",
            nullable=False,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("data_hint", sa.Text(), nullable=True),
        sa.Column("decision_notes", sa.Text(), nullable=True),
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
        op.f("ix_ai_input_designs_questionnaire_id"),
        "ai_input_designs",
        ["questionnaire_id"],
    )
    op.create_index(op.f("ix_ai_input_designs_link_id"), "ai_input_designs", ["link_id"])

    # data を戻す（annotation→description, location→data_hint）
    op.execute(
        """
        INSERT INTO ai_input_designs
            (questionnaire_id, link_id, item_text, source_category, description,
             data_hint, decision_notes, created_at, updated_at)
        SELECT
            s.questionnaire_id,
            s.link_id,
            s.item_text,
            s.source_category,
            c.annotation,
            s.location,
            NULL,
            CURRENT_TIMESTAMP,
            CURRENT_TIMESTAMP
        FROM system_locations s
        LEFT JOIN clinical_annotations c
          ON c.questionnaire_id = s.questionnaire_id AND c.link_id = s.link_id
        """
    )

    # ai_input_plans を design_id 付きへ戻す（内容は再生成想定で再構築）
    op.drop_index(op.f("ix_ai_input_plans_link_id"), table_name="ai_input_plans")
    op.drop_index(op.f("ix_ai_input_plans_questionnaire_id"), table_name="ai_input_plans")
    op.drop_table("ai_input_plans")
    op.create_table(
        "ai_input_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("design_id", sa.Integer(), nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("link_id", sa.String(length=255), nullable=False),
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

    # 新2テーブルを削除
    op.drop_index(op.f("ix_system_locations_link_id"), table_name="system_locations")
    op.drop_index(op.f("ix_system_locations_questionnaire_id"), table_name="system_locations")
    op.drop_table("system_locations")
    op.drop_index(op.f("ix_clinical_annotations_link_id"), table_name="clinical_annotations")
    op.drop_index(
        op.f("ix_clinical_annotations_questionnaire_id"),
        table_name="clinical_annotations",
    )
    op.drop_table("clinical_annotations")
