"""add_response_last_edited_by

回答の最後の編集者を記録する（職務分離の判定用）。

- questionnaire_responses に last_edited_by_id を追加（NULL 可、index 付き）
- questionnaire_responses に edit_version を追加（編集ごとに +1。検証・差戻し・編集の
  書き込み条件に使い、状態が変わらない編集の割り込みを弾く）
- 既存の編集の穴埋め: この移行より前の編集は last_edited_by_id も監査ログも残していない。
  Web の編集は `author` に `User/{編集者の id}` を入れていたので、それが `User/<数字>` で
  提出者と違い、実在するユーザーなら last_edited_by_id に写す（保守的な推定。外れても
  「検証できない人が増える」側にしか倒れない）。推定できない過去の編集は残る。

既存の `submitted_by_id` / `verified_by_id` と同じ流儀で、追加カラムのユーザ参照には
物理 FK を張らない（SQLite は既存テーブルへの ALTER ADD CONSTRAINT を持たない）。
参照整合性は ORM 宣言（create_all 環境）とアプリ層で担保する。

Revision ID: f3b5d7e9a1c4
Revises: e2a4c6e8b0d1
Create Date: 2026-11-14

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3b5d7e9a1c4"
down_revision: str | None = "e2a4c6e8b0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _backfill_last_editor() -> None:
    conn = op.get_bind()
    user_ids = {int(r[0]) for r in conn.execute(sa.text("SELECT id FROM users"))}
    rows = conn.execute(
        sa.text(
            "SELECT id, author, submitted_by_id FROM questionnaire_responses "
            "WHERE author LIKE 'User/%' AND last_edited_by_id IS NULL"
        )
    ).fetchall()
    for row_id, author, submitted_by_id in rows:
        tail = str(author)[len("User/") :]
        if not (tail.isascii() and tail.isdigit()):
            continue
        editor = int(tail)
        if editor == submitted_by_id or editor not in user_ids:
            continue
        conn.execute(
            sa.text("UPDATE questionnaire_responses SET last_edited_by_id = :e WHERE id = :i"),
            {"e": editor, "i": row_id},
        )


def upgrade() -> None:
    op.add_column(
        "questionnaire_responses",
        sa.Column("edit_version", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "questionnaire_responses",
        sa.Column("last_edited_by_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_questionnaire_responses_last_edited_by_id"),
        "questionnaire_responses",
        ["last_edited_by_id"],
    )
    _backfill_last_editor()


def downgrade() -> None:
    op.drop_index(
        op.f("ix_questionnaire_responses_last_edited_by_id"),
        table_name="questionnaire_responses",
    )
    op.drop_column("questionnaire_responses", "last_edited_by_id")
    op.drop_column("questionnaire_responses", "edit_version")
