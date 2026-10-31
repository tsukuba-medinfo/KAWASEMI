"""add_location_candidate_assessments

データ所在の確からしさの判定を記録するための変更。

- location_candidate_assessments テーブルを新設する。SystemLocation の候補
  （candidates[] の1要素）を cand_key で参照し、段階・判断根拠・判定者・判定日時
  などを追記のみで保存する（既存行は更新しない。訂正は新しい round として残す）。
- system_locations.updated_by_id を追加する（最後に保存した利用者。判定者との
  職務分離を検出するために使う）。

SQLite は既存テーブルへの ALTER ADD CONSTRAINT を持たないため、
system_locations.updated_by_id は物理FKを張らずindexのみ付与する
（b9c0d1e2f3a4・e1f2a3b4c5d6 と同じ方針）。参照整合性はORM宣言（models.py の
ForeignKey）とアプリ層で担保する。

このマイグレーションは所在の確からしさの判定に関係する箇所のみを対象にする。`--autogenerate` は
このほかに既存カラムのFK/NOT NULL関連の差分（10+3箇所）を検出するが、これらは
この変更と無関係かつ意図的な既存設計のため含めていない。

Revision ID: 5476e155ca8a
Revises: e1f2a3b4c5d6
Create Date: 2026-08-02

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5476e155ca8a"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "location_candidate_assessments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("system_location_id", sa.Integer(), nullable=False),
        sa.Column(
            "cand_key",
            sa.String(length=32),
            nullable=False,
            comment="判定対象の候補を指す参照キー（compute_cand_key の出力）",
        ),
        sa.Column(
            "candidate_snapshot",
            sa.JSON(),
            nullable=False,
            comment="判定時点の候補内容の複製（「提示した所在」）",
        ),
        sa.Column(
            "round_number",
            sa.Integer(),
            nullable=False,
            comment="同一候補への判定回次（1=1回目、2以降=再判定）",
        ),
        sa.Column(
            "stage",
            sa.Integer(),
            nullable=True,
            comment="AssessmentStage の値。判定不能の場合は null",
        ),
        sa.Column(
            "is_undeterminable",
            sa.Boolean(),
            nullable=False,
            comment="段階を付けず「判定不能」と回答された（推測で段階を付けない）",
        ),
        sa.Column(
            "rationale",
            sa.Text(),
            nullable=False,
            comment="判断の根拠（自由記述、判定不能の場合も理由を残す）",
        ),
        sa.Column(
            "unreachable_reason",
            sa.Text(),
            nullable=True,
            comment="到達不能の理由（該当する場合のみ。手書きスキャン・他院紙文書・非標準記載など）",
        ),
        sa.Column(
            "cancer_type_dependent_note",
            sa.String(length=16),
            nullable=True,
            comment="様式の版や対象群によって所在が変わるか",
        ),
        sa.Column(
            "materials",
            sa.JSON(),
            nullable=True,
            comment="判定材料のスナップショット（スキーマ定義・データディクショナリ・統計プロファイル等）",
        ),
        sa.Column(
            "is_self_assessment",
            sa.Boolean(),
            nullable=False,
            comment="判定時点で判定者=SystemLocation.updated_by だったか（職務分離の検出用）",
        ),
        sa.Column("assessed_by_id", sa.Integer(), nullable=True, comment="判定者"),
        sa.Column(
            "assessed_at", sa.DateTime(timezone=True), nullable=False, comment="判定日時（UTC）"
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
        sa.ForeignKeyConstraint(["assessed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["system_location_id"], ["system_locations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "system_location_id",
            "cand_key",
            "round_number",
            name="uq_location_candidate_assessment_round",
        ),
    )
    op.create_index(
        op.f("ix_location_candidate_assessments_assessed_by_id"),
        "location_candidate_assessments",
        ["assessed_by_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_location_candidate_assessments_cand_key"),
        "location_candidate_assessments",
        ["cand_key"],
        unique=False,
    )
    op.create_index(
        op.f("ix_location_candidate_assessments_system_location_id"),
        "location_candidate_assessments",
        ["system_location_id"],
        unique=False,
    )
    # system_locations.updated_by_id（index のみ。物理 FK は張らない。上記docstring参照）
    op.add_column(
        "system_locations",
        sa.Column(
            "updated_by_id",
            sa.Integer(),
            nullable=True,
            comment="最後に保存した利用者（職務分離の判定に使う）",
        ),
    )
    op.create_index(
        op.f("ix_system_locations_updated_by_id"),
        "system_locations",
        ["updated_by_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_system_locations_updated_by_id"), table_name="system_locations")
    op.drop_column("system_locations", "updated_by_id")
    op.drop_index(
        op.f("ix_location_candidate_assessments_system_location_id"),
        table_name="location_candidate_assessments",
    )
    op.drop_index(
        op.f("ix_location_candidate_assessments_cand_key"),
        table_name="location_candidate_assessments",
    )
    op.drop_index(
        op.f("ix_location_candidate_assessments_assessed_by_id"),
        table_name="location_candidate_assessments",
    )
    op.drop_table("location_candidate_assessments")
