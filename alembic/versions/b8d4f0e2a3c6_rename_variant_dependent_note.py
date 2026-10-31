"""location_candidate_assessments の所在の可変性の列を汎用の名前にする

`cancer_type_dependent_note` を `variant_dependent_note` へ改名する。中身（値の語彙と
nullable）は変えない。様式の版や対象群によって所在が変わるかを記録する列である。

Revision ID: b8d4f0e2a3c6
Revises: a7c3e9d1f2b4
Create Date: 2026-10-31

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8d4f0e2a3c6"
down_revision: str | None = "a7c3e9d1f2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "location_candidate_assessments"
_NEW_COMMENT = "様式の版や対象群によって所在が変わるか（VariantDependentNote）"
# 5476e155ca8a で付けた注釈。downgrade で戻す（None にすると注釈が消える）。
_OLD_COMMENT = "様式の版や対象群によって所在が変わるか"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            "cancer_type_dependent_note",
            new_column_name="variant_dependent_note",
            existing_type=sa.String(length=16),
            existing_nullable=True,
            comment=_NEW_COMMENT,
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            "variant_dependent_note",
            new_column_name="cancer_type_dependent_note",
            existing_type=sa.String(length=16),
            existing_nullable=True,
            comment=_OLD_COMMENT,
        )
