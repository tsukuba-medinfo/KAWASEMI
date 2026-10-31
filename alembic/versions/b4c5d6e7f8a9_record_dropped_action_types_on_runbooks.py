"""手順書に、語彙外として落とした action_type の記録を持たせる

保存要求が何を送ってきたかは、保存済みの `steps` には残らない（検証が語彙外の
`action_type` を `None` にするため）。弾かずに落とすのは ALCOA+ が要求するのが拒否では
なく可視性だからで、そのぶん**落としたことの記録**が要る。

記録先を手順書の行にしたのは、これが**人の保存**であって LLM 呼び出しではないからである
（`ai_input_design_llm_calls` の行は LLM 呼び出しの記録で、role / model / options を持つ）。

**落ちたタグが無い保存では null になる。** 空配列を残さないので、`null` は「この保存では
落ちていない」を意味する。

Revision ID: b4c5d6e7f8a9
Revises: c1f4a2b7d8e3
Create Date: 2026-09-06 06:40:49.582321

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c5d6e7f8a9"
down_revision: str | Sequence[str] | None = "c1f4a2b7d8e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_input_runbooks",
        sa.Column(
            "dropped_action_types",
            sa.JSON(),
            nullable=True,
            comment=(
                "語彙外として None へ落とした action_type の記録。"
                "[{order, given}] の配列。落ちたタグが無い保存では null"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_input_runbooks", "dropped_action_types")
