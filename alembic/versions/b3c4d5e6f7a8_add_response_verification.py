"""add_response_verification

検証ワークフローのスキーマ拡張:
- questionnaire_responses に検証状態・サインオフ・提出者・差戻し理由を追加
  - submitted_by_id（記入責任者。SoD 判定用）
  - verification_status（unverified / verified / rejected, 既定 unverified）
  - verified_by_id / verified_at（検証サインオフ）
  - rejection_reason（差戻し理由）
- response_verification_logs テーブルを新設（検証イベントの追記専用監査ログ）

SQLite は既存テーブルへの ALTER ADD CONSTRAINT を持たないため、追加カラムの
ユーザ参照（submitted_by_id / verified_by_id）は物理 FK を張らず index のみとし、
参照整合性は ORM 宣言（create_all 環境）とアプリ層で担保する。新設テーブルの FK は
create_table 内で宣言する。

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-06-27

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: str | None = "a2b3c4d5e6f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "questionnaire_responses",
        sa.Column("submitted_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "questionnaire_responses",
        sa.Column(
            "verification_status",
            sa.String(length=32),
            server_default=sa.text("'unverified'"),
            nullable=False,
        ),
    )
    op.add_column(
        "questionnaire_responses",
        sa.Column("verified_by_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "questionnaire_responses",
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "questionnaire_responses",
        sa.Column("rejection_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        op.f("ix_questionnaire_responses_submitted_by_id"),
        "questionnaire_responses",
        ["submitted_by_id"],
    )
    op.create_index(
        op.f("ix_questionnaire_responses_verification_status"),
        "questionnaire_responses",
        ["verification_status"],
    )
    op.create_index(
        op.f("ix_questionnaire_responses_verified_by_id"),
        "questionnaire_responses",
        ["verified_by_id"],
    )

    op.create_table(
        "response_verification_logs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_response_id", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_response_verification_logs_questionnaire_response_id"),
        "response_verification_logs",
        ["questionnaire_response_id"],
    )
    op.create_index(
        op.f("ix_response_verification_logs_actor_id"),
        "response_verification_logs",
        ["actor_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_response_verification_logs_actor_id"),
        table_name="response_verification_logs",
    )
    op.drop_index(
        op.f("ix_response_verification_logs_questionnaire_response_id"),
        table_name="response_verification_logs",
    )
    op.drop_table("response_verification_logs")

    op.drop_index(
        op.f("ix_questionnaire_responses_verified_by_id"),
        table_name="questionnaire_responses",
    )
    op.drop_index(
        op.f("ix_questionnaire_responses_verification_status"),
        table_name="questionnaire_responses",
    )
    op.drop_index(
        op.f("ix_questionnaire_responses_submitted_by_id"),
        table_name="questionnaire_responses",
    )
    op.drop_column("questionnaire_responses", "rejection_reason")
    op.drop_column("questionnaire_responses", "verified_at")
    op.drop_column("questionnaire_responses", "verified_by_id")
    op.drop_column("questionnaire_responses", "verification_status")
    op.drop_column("questionnaire_responses", "submitted_by_id")
