"""ai_input_design_llm_calls の created_at / updated_at に server_default を与える

Revision ID: a6fceb154051
Revises: 915de2ef857a
Create Date: 2026-08-15 13:09:00.733346

**alembic で作った DB では設計ロールの来歴が1行も保存できなかった。**

`TimestampMixin` は両列に `server_default=func.now()` を宣言している
（`kawasemi/models/base.py`）ので、ORM は INSERT に値を載せない。しかし表を作った
移行（`c7d8e9f0a1b2`）は `sa.Column("created_at", sa.DateTime(timezone=True),
nullable=False)` と書いており、**既定値の無い NOT NULL 列**になっていた。結果として

    IntegrityError: NOT NULL constraint failed: ai_input_design_llm_calls.created_at

で全行が落ちる。`save_design_llm_calls_safely` は「来歴の失敗で成果物を落とさない」
設計なので、**保存が全滅しても呼び出しは成功として返る**——長時間の生成が
完走したのに来歴は0行、という形で現れた。

**テストでは見つからない欠陥である。** テストは `Base.metadata.create_all` で表を作り、
そちらはモデル側の `server_default` を反映するので緑になる。同じ穴は
`tests/test_stage_vocabulary_migration.py` の docstring が既に名指ししていた。
検出側は `tests/test_timestamp_server_defaults_migration.py` で足す。

SQLite は `ALTER TABLE ... ALTER COLUMN` を持たないので `batch_alter_table`
（表の作り直し）で与える。**既存行は動かさない**——値の入っている行はそのまま移り、
既定値はこれ以降の INSERT にだけ効く。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a6fceb154051"
down_revision: str | Sequence[str] | None = "915de2ef857a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ai_input_design_llm_calls"
_COLUMNS = ("created_at", "updated_at")


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for column in _COLUMNS:
            batch_op.alter_column(
                column,
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
                server_default=sa.func.now(),
            )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        for column in _COLUMNS:
            batch_op.alter_column(
                column,
                existing_type=sa.DateTime(timezone=True),
                existing_nullable=False,
                server_default=None,
            )
