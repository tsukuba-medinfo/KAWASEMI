"""clinical_annotations に典拠カラムと来歴カラムを追加

設計ロールの4成果物のうち、来歴フィールドがゼロなのは clinical_annotations だけ
だった。以下を追加して他3成果物と揃える。

- citations（JSON配列）: 記述の典拠。各要素は {unit_id, payload, llm_model}。
  unit_id は索引側のアドレス（reference 文書なら
  `file:{パス}#p{ページ}:{開始文字}-{終了文字}`）。
- llm_model / status / confirmed_by_id / confirmed_at: InputRunbook・InputPlan と
  同形の来歴。「下書き → 人間確認 → confirmed 昇格」の確定ワークフローに乗せる。

既存行の移行:
- annotation はそのまま。citations は既存行すべてに空配列を入れる。
- status は「記述あり → confirmed / 記述なし → draft」。本カラム導入前の画面は臨床的
  コンテキストに確認ステップを持たず、記述の存在だけで設計キャンバスの当該セルを
  「確認済み」として表示していた。記述は収集責任者が直接入力したもの（AI下書き機構は
  存在しなかった）なので、confirmed へ寄せることで移行前後の表示・ゲート挙動を保つ。
  既存 confirmed 相当レコードの確認操作者は不明のため confirmed_by_id / confirmed_at は
  NULL のまま（b9c0d1e2f3a4 と同じ扱い）。

SQLite は既存テーブルへの ALTER ADD CONSTRAINT / ALTER COLUMN を持たないため、
- confirmed_by_id は物理 FK を張らず index のみとする（b9c0d1e2f3a4 と同じ方針）
- citations / status は既存テーブル上では nullable のまま追加する（b1c2d3e4f5a6 で
  system_locations.candidates を追加したときと同じ）。NULL が入らないことは、既存行の
  データ移行 ＋ status の server_default ＋ ORM の default（models.py の
  nullable=False / default=list）で担保する。create_all 環境（テスト・新規構築）では
  ORM 宣言どおり NOT NULL になる。
参照整合性・非NULL 性はアプリ層と ORM 宣言で担保する。

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-01

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1f2a3b4c5d6"
down_revision: str | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "clinical_annotations"


def _table() -> sa.Table:
    """データ移行用の軽量テーブル定義（JSON型で dialect 別シリアライズを委ねる）。"""
    return sa.table(
        _TABLE,
        sa.column("id", sa.Integer),
        sa.column("annotation", sa.Text),
        sa.column("citations", sa.JSON),
        sa.column("status", sa.String),
    )


def upgrade() -> None:
    # 典拠。既存行は下のデータ移行で空配列を入れる
    op.add_column(_TABLE, sa.Column("citations", sa.JSON(), nullable=True))
    # 来歴。status は server_default を持たせ、ORM 以外の経路でも NULL にならないようにする
    op.add_column(
        _TABLE,
        sa.Column("status", sa.String(length=32), nullable=True, server_default="draft"),
    )
    op.add_column(_TABLE, sa.Column("llm_model", sa.String(length=128), nullable=True))
    op.add_column(_TABLE, sa.Column("confirmed_by_id", sa.Integer(), nullable=True))
    op.add_column(_TABLE, sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f(f"ix_{_TABLE}_confirmed_by_id"), _TABLE, ["confirmed_by_id"])

    bind = op.get_bind()
    ca = _table()
    rows = bind.execute(sa.select(ca.c.id, ca.c.annotation)).fetchall()
    for row in rows:
        has_annotation = bool((row.annotation or "").strip())
        bind.execute(
            ca.update()
            .where(ca.c.id == row.id)
            .values(
                citations=[],  # 既存行に典拠は無い（空配列）
                status="confirmed" if has_annotation else "draft",
            )
        )


def downgrade() -> None:
    op.drop_index(op.f(f"ix_{_TABLE}_confirmed_by_id"), table_name=_TABLE)
    op.drop_column(_TABLE, "confirmed_at")
    op.drop_column(_TABLE, "confirmed_by_id")
    op.drop_column(_TABLE, "llm_model")
    op.drop_column(_TABLE, "status")
    op.drop_column(_TABLE, "citations")
