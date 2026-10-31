"""location_candidate_assessments.stage に語彙の CHECK 制約を足す

所在の確からしさの段階（`AssessmentStage.ALL == [4, 3, 2, 1]`）を守っていたのは、保存ルートの
`if payload.stage not in AssessmentStage.ALL: → 400` という**1行だけ**だった。DB 直書き・
移行スクリプト・将来の別エンドポイントは素通りし、集計（`export_location_assessments`）は
`str(latest.stage)` を集計キーにするので、**語彙外の値は `"0"` や `"None"` という新しいキーを
作る**——集計の目盛りが黙って増える。語彙の**値**を固定していても、
その集合外が保存できるなら固定は集計を守れない。

**既存行を先に検査し、語彙外があれば移行を中止する**（黙って作れない制約を作りに行って
`IntegrityError` で落ちるのではなく、**何件が語彙外かを先に報告する**）。SQLite は
`ALTER TABLE ... ADD CONSTRAINT` を持たないので `batch_alter_table`（表の作り直し）で足す。

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import context, op
from kawasemi.ai_input.models import AssessmentStage

revision: str = "d8e9f0a1b2c3"
down_revision: str | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "location_candidate_assessments"


def _assert_existing_rows_are_in_vocabulary() -> None:
    """既存行が語彙内であることを確認する（**適用時に働く機構**）。

    **これは記録ではない。** ここは**語彙内であることの確認をどの環境でも成立させる機構**で、
    語彙外の行がある環境では移行を止めて件数を報告する。

    **語彙は `AssessmentStage.check_sql()` から組む**（ここに条件を書き直すと、語彙や規則を
    変えたときにこの検査だけが古い規則を見る）。**制約が拒否する行と、この検査が数える行は
    同じ集合でなければならない**——違うと「検査は 0 件と言ったのに `IntegrityError` で落ちる」
    （あるいはその逆）が起きる。以前この検査は `stage` 単体しか見ておらず、
    `stage IS NULL` かつ `is_undeterminable = 0` の行を**語彙内として通していた**。
    """
    violating = f"NOT ({AssessmentStage.check_sql()})"
    if context.is_offline_mode():
        # **`--sql` では既存行を読めない**（接続が無い）。検査を飛ばして制約を作りに行くと、
        # 語彙外の行がある DB では適用時に `IntegrityError` で落ちる——**何件が語彙外かを
        # 先に報告する**という設計が失われる。
        #
        # **この移行はオフラインでは出力できない。** 以前はここで
        # 「0 件を確認してから `--sql` の出力を当てること」と案内していたが、この枝は無条件に
        # 中止するので **`--sql` の出力は永久に得られない**——実行できない指示だった。
        # 事前に確認するための SQL は示す（接続のある環境で `alembic upgrade` を走らせる前に
        # 語彙外の件数を知りたいときに使える）。
        raise RuntimeError(
            "オフラインモード（--sql）では既存行を検査できないので中止した。"
            "この移行は接続のある環境で `alembic upgrade` として実行すること"
            "（--sql での DDL 出力には対応しない）。"
            f"適用前に語彙外の件数を知りたい場合は次を実行する: "
            f"SELECT COUNT(*) FROM {_TABLE} WHERE {violating};"
        )
    bind = op.get_bind()
    rows = list(
        bind.execute(
            sa.text(
                f"SELECT id, stage, is_undeterminable FROM {_TABLE} "  # noqa: S608 - 定数
                f"WHERE {violating}"
            )
        )
    )
    if rows:
        detail = ", ".join(
            f"id={r[0]} stage={r[1]!r} is_undeterminable={r[2]!r}" for r in rows[:20]
        )
        raise RuntimeError(
            f"{_TABLE}.stage に語彙外の値が {len(rows)} 件ある（語彙: {AssessmentStage.ALL}）。"
            f"移行を中止した。判定記録は追記のみなので、値を直すか行を削除するかは"
            f"記録の管理者による判断が必要である: {detail}"
        )


def upgrade() -> None:
    _assert_existing_rows_are_in_vocabulary()
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.create_check_constraint(AssessmentStage.CHECK_NAME, AssessmentStage.check_sql())


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_constraint(AssessmentStage.CHECK_NAME, type_="check")
