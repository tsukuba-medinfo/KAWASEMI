"""users_sqlite_autoincrement

SQLite の `users.id` が、最大の行を消すと再利用されるのを止める。

`INTEGER PRIMARY KEY`（AUTOINCREMENT なし）は max(rowid)+1 を採るので、いちばん新しい
ユーザーを削除して作り直すと同じ id になる。トークンは数値の `sub` だけで本人を引くため、
削除済みユーザーの未失効のトークンが、新しいアカウントのものとして通る恐れがあった。
AUTOINCREMENT にすると、削除済みの id は二度と割り当てられない。

SQLite でだけ表を作り直す。PostgreSQL のシーケンスは id を再利用しないので何もしない。
移行用の接続は外部キーの強制が無い（`PRAGMA foreign_keys` が既定の OFF）ので、
`users` を作り直しても子表の行は消えない。

既存の SQLite は、ユーザー削除のとき外部キーが強制されていなかったので、削除済みユーザーを
指す子表の行（リフレッシュトークンなど）が残っていることがある。AUTOINCREMENT の採番は
`sqlite_sequence` の値から始まるので、これを「いま存在する最大の id」ではなく、子表が
まだ指している最大の id より上に置く。あわせて、宙に浮いた子表の行を片付ける
（ON DELETE CASCADE の表は行を削除、SET NULL の表は NULL に戻す）。そうしないと、新しい
アカウントが削除済みユーザーの id を取り、その残りのトークンが通ってしまう。

Revision ID: e2a4c6e8b0d1
Revises: d1f3b5a7c9e2
Create Date: 2026-11-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2a4c6e8b0d1"
down_revision: str | None = "d1f3b5a7c9e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _referencing_columns(conn: sa.Connection) -> list[tuple[str, str, str]]:
    """users を指す外部キーを (表, 列, on_delete) で返す（自己参照も含む）。"""
    found: list[tuple[str, str, str]] = []
    tables = [
        r[0]
        for r in conn.execute(
            sa.text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
    ]
    for table in tables:
        for fk in conn.execute(sa.text(f"PRAGMA foreign_key_list({_quote(table)})")).mappings():
            if fk["table"] == "users":
                found.append((table, fk["from"], str(fk["on_delete"]).upper()))
    return found


def upgrade() -> None:
    conn = op.get_bind()
    if conn.dialect.name != "sqlite":
        return

    refs = _referencing_columns(conn)
    # 宙に浮いた子表の行を片付ける前に、子表が指している最大の id を控える。
    highest = conn.execute(sa.text("SELECT COALESCE(MAX(id), 0) FROM users")).scalar_one()
    for table, column, _ in refs:
        top = conn.execute(
            sa.text(f"SELECT COALESCE(MAX({_quote(column)}), 0) FROM {_quote(table)}")
        ).scalar_one()
        highest = max(highest, int(top))
    for table, column, on_delete in refs:
        t, c = _quote(table), _quote(column)
        orphan = f"{c} IS NOT NULL AND {c} NOT IN (SELECT id FROM users)"
        if on_delete == "CASCADE":
            conn.execute(sa.text(f"DELETE FROM {t} WHERE {orphan}"))
        else:
            # SET NULL / 指定なし。NOT NULL の列だと失敗するので、列が NULL を許す場合だけ戻す。
            info = conn.execute(sa.text(f"PRAGMA table_info({t})")).mappings()
            nullable = {r["name"]: not r["notnull"] for r in info}
            if nullable.get(column):
                conn.execute(sa.text(f"UPDATE {t} SET {c} = NULL WHERE {orphan}"))
            else:
                conn.execute(sa.text(f"DELETE FROM {t} WHERE {orphan}"))

    with op.batch_alter_table(
        "users", recreate="always", table_kwargs={"sqlite_autoincrement": True}
    ):
        pass

    # 採番の起点を、子表がまだ指していた最大の id に置く（以後はそれより大きい id だけが出る）。
    if highest > 0:
        conn.execute(sa.text("DELETE FROM sqlite_sequence WHERE name = 'users'"))
        conn.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES ('users', :h)"), {"h": highest}
        )


def downgrade() -> None:
    # AUTOINCREMENT のままでも動作に害は無いので、戻さない（表の作り直しを避ける）。
    pass
