"""add_core_crf_tables

CRF 中核テーブルの初期スキーマ:
- questionnaires（Questionnaire テンプレート）
- questionnaire_responses（QuestionnaireResponse 回答。検証ワークフロー系カラム
  submitted_by_id / verification_status / verified_by_id / verified_at /
  rejection_reason は後発の b3c4d5e6f7a8 が add_column するため、ここには含めない）
- validation_logs（バリデーション結果ログ）

これらは ORM モデルに定義されていながら create_table するマイグレーションが欠落しており、
空DBへの `alembic upgrade head` が失敗していた。本 revision を新しいルート（down_revision=None）
として挿入し、後続の 42b8172a7712 が張る case_assignments -> questionnaires.id の FK を解決可能にする。

作成順序（FK 依存）: questionnaires -> questionnaire_responses -> validation_logs。
これら3表は users に依存しない（検証ワークフロー系カラム追加前のため）。

Revision ID: d9e8f7a6b5c4
Revises:
Create Date: 2026-07-12

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d9e8f7a6b5c4"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "questionnaires",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fhir_id", sa.String(length=64), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
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
    op.create_index(op.f("ix_questionnaires_fhir_id"), "questionnaires", ["fhir_id"], unique=True)
    op.create_index(op.f("ix_questionnaires_title"), "questionnaires", ["title"])
    op.create_index(op.f("ix_questionnaires_status"), "questionnaires", ["status"])
    op.create_index(op.f("ix_questionnaires_url"), "questionnaires", ["url"], unique=True)
    op.create_index(op.f("ix_questionnaires_is_deleted"), "questionnaires", ["is_deleted"])

    op.create_table(
        "questionnaire_responses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fhir_id", sa.String(length=64), nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column("content", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("authored", sa.String(length=64), nullable=True),
        sa.Column("author", sa.Text(), nullable=True),
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
    )
    op.create_index(
        op.f("ix_questionnaire_responses_fhir_id"),
        "questionnaire_responses",
        ["fhir_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_questionnaire_responses_questionnaire_id"),
        "questionnaire_responses",
        ["questionnaire_id"],
    )
    op.create_index(
        op.f("ix_questionnaire_responses_status"),
        "questionnaire_responses",
        ["status"],
    )
    op.create_index(
        op.f("ix_questionnaire_responses_subject"),
        "questionnaire_responses",
        ["subject"],
    )
    op.create_index(
        op.f("ix_questionnaire_responses_authored"),
        "questionnaire_responses",
        ["authored"],
    )

    op.create_table(
        "validation_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("resource_type", sa.String(length=32), nullable=False),
        sa.Column("questionnaire_response_id", sa.Integer(), nullable=True),
        sa.Column("validation_result", sa.JSON(), nullable=False),
        sa.Column("is_valid", sa.Boolean(), nullable=False),
        sa.Column("error_summary", sa.String(length=500), nullable=True),
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
            ["questionnaire_response_id"], ["questionnaire_responses.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_validation_logs_resource_type"),
        "validation_logs",
        ["resource_type"],
    )
    op.create_index(
        op.f("ix_validation_logs_questionnaire_response_id"),
        "validation_logs",
        ["questionnaire_response_id"],
    )
    op.create_index(
        op.f("ix_validation_logs_is_valid"),
        "validation_logs",
        ["is_valid"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_validation_logs_is_valid"), table_name="validation_logs")
    op.drop_index(
        op.f("ix_validation_logs_questionnaire_response_id"), table_name="validation_logs"
    )
    op.drop_index(op.f("ix_validation_logs_resource_type"), table_name="validation_logs")
    op.drop_table("validation_logs")

    op.drop_index(op.f("ix_questionnaire_responses_authored"), table_name="questionnaire_responses")
    op.drop_index(op.f("ix_questionnaire_responses_subject"), table_name="questionnaire_responses")
    op.drop_index(op.f("ix_questionnaire_responses_status"), table_name="questionnaire_responses")
    op.drop_index(
        op.f("ix_questionnaire_responses_questionnaire_id"), table_name="questionnaire_responses"
    )
    op.drop_index(op.f("ix_questionnaire_responses_fhir_id"), table_name="questionnaire_responses")
    op.drop_table("questionnaire_responses")

    op.drop_index(op.f("ix_questionnaires_is_deleted"), table_name="questionnaires")
    op.drop_index(op.f("ix_questionnaires_url"), table_name="questionnaires")
    op.drop_index(op.f("ix_questionnaires_status"), table_name="questionnaires")
    op.drop_index(op.f("ix_questionnaires_title"), table_name="questionnaires")
    op.drop_index(op.f("ix_questionnaires_fhir_id"), table_name="questionnaires")
    op.drop_table("questionnaires")
