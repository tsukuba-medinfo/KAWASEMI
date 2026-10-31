"""system_locations を candidates 配列へ移行

ステップ1役割2「データ所在」を「単一の source_category + location」から
「優先順の候補配列 candidates(JSON)」へ作り替える。1項目が複数ソース候補を
持てるようにし、各候補に索引由来の機械可読情報(unit_id/connection_id/payload)を
載せられるようにする（ステップ3が op 引数を決定的に導出する素材）。

既存行は1要素の候補へ移行する（location があるか source_category が manual 以外の
場合のみ。両方とも空相当なら空配列）。

Revision ID: b1c2d3e4f5a6
Revises: a7b8c9d0e1f2
Create Date: 2026-05-31

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table() -> sa.Table:
    """データ移行用の軽量テーブル定義（JSON型で dialect 別シリアライズを委ねる）。"""
    return sa.table(
        "system_locations",
        sa.column("id", sa.Integer),
        sa.column("source_category", sa.String),
        sa.column("location", sa.Text),
        sa.column("candidates", sa.JSON),
    )


def upgrade() -> None:
    op.add_column("system_locations", sa.Column("candidates", sa.JSON(), nullable=True))

    bind = op.get_bind()
    sl = _table()
    rows = bind.execute(sa.select(sl.c.id, sl.c.source_category, sl.c.location)).fetchall()
    for row in rows:
        sc = row.source_category or "manual"
        loc = (row.location or "").strip() or None
        candidates: list[dict] = []
        if loc or sc != "manual":
            candidates = [
                {
                    "source_category": sc,
                    "location": loc,
                    "unit_id": None,
                    "connection_id": None,
                    "payload": None,
                }
            ]
        bind.execute(sl.update().where(sl.c.id == row.id).values(candidates=candidates))

    # SQLite 3.35+ / PostgreSQL は ALTER TABLE DROP COLUMN をネイティブ対応
    op.drop_column("system_locations", "source_category")
    op.drop_column("system_locations", "location")


def downgrade() -> None:
    op.add_column(
        "system_locations",
        sa.Column(
            "source_category",
            sa.String(length=32),
            nullable=False,
            server_default="manual",
        ),
    )
    op.add_column("system_locations", sa.Column("location", sa.Text(), nullable=True))

    bind = op.get_bind()
    sl = _table()
    rows = bind.execute(sa.select(sl.c.id, sl.c.candidates)).fetchall()
    for row in rows:
        cands = row.candidates or []
        primary = cands[0] if cands else {}
        bind.execute(
            sl.update()
            .where(sl.c.id == row.id)
            .values(
                source_category=primary.get("source_category") or "manual",
                location=primary.get("location"),
            )
        )

    op.drop_column("system_locations", "candidates")
