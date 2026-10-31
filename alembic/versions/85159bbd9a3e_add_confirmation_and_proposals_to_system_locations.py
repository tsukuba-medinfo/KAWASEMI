"""system_locations に確認の状態と提示された候補を足す

Revision ID: 85159bbd9a3e
Revises: e65aa8ad61ce
Create Date: 2026-08-17 03:10:00.000000

**確認の状態（`status` / `confirmed_at` / `confirmed_by_id`）**
`system_locations` には確認ワークフローが無く、「人が見て決めた項目」と「AI 推奨のまま
素通りした項目」が DB 上で同じ状態だった。保存は一括なので `updated_at` も揃い、
1項目あたりの工数も後から復元できない。`ClinicalAnnotation` /
`InputRunbook` / `InputPlan` は同じ3列を持つ（ALCOA+ 帰属性）ので、
所在の側にだけ無いという非対称を埋める。

**提示された候補（`proposed_candidates`）**
`candidates` は**人が決着させた所在**であり、ステップ3のコンパイラが読んで取得計画を
組み立てる（`compiler._MAX_ALT_CANDIDATES`、上限は `MAX_LOCATION_CANDIDATES`=3）。
ここへ AI が提示した8件をそのまま入れると、**選ばれなかった7件がフォールバック取得計画
に化ける**。提示（AI が何を並べたか）と決着（人がどれを選んだか）は別物なので列を分ける。
「候補は優先順の配列とする」は `candidates` が満たしており、
本列はその手前の**提示の記録**である。提示と選択の関係はこの2列から算出する。

**既存行はそのまま移る。** `status` だけが NOT NULL なので `server_default` を付ける
（既定値の無い NOT NULL 列を足すと既存の INSERT 経路が壊れる）。
既存行は `draft` へ寄せる——**`confirmed` へ寄せない**のは、この列を入れる目的が
「人が確認した」を他と区別することだからで、遡って確認済みを名乗らせては意味が消える。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "85159bbd9a3e"
down_revision: str | Sequence[str] | None = "e65aa8ad61ce"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "system_locations"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "proposed_candidates",
            sa.JSON(),
            nullable=True,
            comment="AI が提示した候補の全件（提示順。人の決着は candidates 側）",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="draft",
            comment="draft（AI 案のまま・未確認） / confirmed（人が確認済み）",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "confirmed_by_id",
            sa.Integer(),
            nullable=True,
            comment="確認（confirmed 昇格）操作者",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "confirmed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="確認日時（UTC）。項目単位に記録するので工数の復元に使える",
        ),
    )
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.create_index(
            "ix_system_locations_confirmed_by_id", ["confirmed_by_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_system_locations_confirmed_by_id_users",
            "users",
            ["confirmed_by_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint("fk_system_locations_confirmed_by_id_users", type_="foreignkey")
        batch_op.drop_index("ix_system_locations_confirmed_by_id")
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("confirmed_by_id")
        batch_op.drop_column("status")
        batch_op.drop_column("proposed_candidates")
