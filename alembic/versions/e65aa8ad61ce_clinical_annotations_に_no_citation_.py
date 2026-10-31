"""clinical_annotations に no_citation_reason を足す

Revision ID: e65aa8ad61ce
Revises: a6fceb154051
Create Date: 2026-08-15 21:24:32.820435

**「まだ書いていない」項目と「規約文書に該当箇所が無いので空欄にした」項目が
DB 上で同じ状態だった**（記述が空・典拠0件・`status=draft`）。`save_clinical` は記述の空な
項目を confirmed へ昇格させないので、確定ワークフローでも区別が付かない。

典拠が見つからない項目は「推測で補わない。臨床的意味を空欄のまま次に進む」扱いにし、
その項目数を数えられるようにしたい。数えるのは人である（LLM 経路で典拠が付かなかった
ことは、規約文書に該当箇所が無いことを意味しない）ので、人の判断を書き残す先が要る。

**nullable なので既存行はそのまま移る。** 既定値の無い NOT NULL 列を足すと
INSERT が壊れる（`ai_input_design_llm_calls.created_at` で起きた）——`server_default` が要るのは
NOT NULL の列である。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e65aa8ad61ce"
down_revision: str | Sequence[str] | None = "a6fceb154051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "clinical_annotations"
_COLUMN = "no_citation_reason"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            _COLUMN,
            sa.Text(),
            nullable=True,
            comment="典拠が見つからなかった理由（人が記録する。AIは書き込まない。評価用）",
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
