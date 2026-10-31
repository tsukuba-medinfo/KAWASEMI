"""add_subject_hub_and_subject_id_fks

Subject（Hub）テーブルを新設し、case_assignments / input_targets /
ai_input_executions に subject_id FK（nullable・後方互換）を追加する。
既存 patient_id 文字列カラムは当面据え置き（後続 PR で移行・撤去）。

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-06-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c2d3e4f5a6b7"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_TABLES = ("case_assignments", "input_targets", "ai_input_executions")


def upgrade() -> None:
    op.create_table(
        "subjects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("study_id", sa.String(length=255), nullable=False),
        sa.Column("registration_no", sa.String(length=255), nullable=False),
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
        sa.UniqueConstraint("study_id", "registration_no", name="uq_subject_study_registration"),
    )
    op.create_index(op.f("ix_subjects_study_id"), "subjects", ["study_id"])
    op.create_index(op.f("ix_subjects_registration_no"), "subjects", ["registration_no"])

    # 既存テーブルへ nullable な subject_id を追加（index のみ）。
    # 物理 FK 制約は張らない: SQLite は既存テーブルへの ALTER ADD CONSTRAINT を
    # 持たず、batch 再構築は FK 依存テーブルの reflect を要して重い（本リポジトリは
    # コアテーブルが create_all 管理のハイブリッド構成）。移行期は nullable のため、
    # subjects への参照整合性は ORM 宣言（create_all 環境）とアプリ層で担保し、
    # subject_id を主へ昇格する後続 PR で正式 FK を付与する。
    for table in _FK_TABLES:
        op.add_column(table, sa.Column("subject_id", sa.Integer(), nullable=True))
        op.create_index(op.f(f"ix_{table}_subject_id"), table, ["subject_id"])


def downgrade() -> None:
    for table in reversed(_FK_TABLES):
        op.drop_index(op.f(f"ix_{table}_subject_id"), table_name=table)
        op.drop_column(table, "subject_id")
    op.drop_index(op.f("ix_subjects_registration_no"), table_name="subjects")
    op.drop_index(op.f("ix_subjects_study_id"), table_name="subjects")
    op.drop_table("subjects")
