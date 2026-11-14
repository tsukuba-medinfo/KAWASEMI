"""users_sqlite_autoincrement

SQLite の `users.id` が、最大の行を消すと再利用されるのを止める。

`INTEGER PRIMARY KEY`（AUTOINCREMENT なし）は max(rowid)+1 を採るので、いちばん新しい
ユーザーを削除して作り直すと同じ id になる。トークンは数値の `sub` だけで本人を引くため、
削除済みユーザーの未失効のトークンが、新しいアカウントのものとして通る恐れがあった。
AUTOINCREMENT にすると、削除済みの id は二度と割り当てられない。

SQLite でだけ表を作り直す。PostgreSQL のシーケンスは id を再利用しないので何もしない。
移行用の接続は外部キーの強制が無い（`PRAGMA foreign_keys` が既定の OFF）ので、
`users` を作り直しても子表の行は消えない。

Revision ID: e2a4c6e8b0d1
Revises: d1f3b5a7c9e2
Create Date: 2026-11-14

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a4c6e8b0d1"
down_revision: str | None = "d1f3b5a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    with op.batch_alter_table(
        "users", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ):
        pass


def downgrade() -> None:
    # AUTOINCREMENT のままでも動作に害は無いので、戻さない（表の作り直しを避ける）。
    pass
