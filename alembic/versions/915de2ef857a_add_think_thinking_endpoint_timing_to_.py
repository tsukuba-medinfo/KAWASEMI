"""ai_input_design_llm_calls に think / thinking / endpoint / timing を足す

Revision ID: 915de2ef857a
Revises: a1b2c3d4e5f7
Create Date: 2026-08-15 09:55:40.659142

**設計ロールの来歴に、これまで捨てていた3種を残す。**

1. `think` / `thinking` — thinking を要求したかと、モデルが返した推論本文。
   `think` は `options` ではなく `/api/chat` のトップレベル引数で、渡さないとモデルの
   既定に従う＝**Ollama やテンプレートの更新で挙動が黙って動く**。既定値は
   `llm_json.DEFAULT_THINK`。`thinking` は content と別フィールドで返るので JSON は
   汚れないが、時間と `num_ctx` を消費し出力に影響するので捨てない。
   **両方要る**——`think=True` でもモデルが何も考えないことはありうるので、
   `thinking` が空であることだけでは「切っていた」と区別できない。

2. `endpoint` — 呼び出し先の base URL。所要時間は**マシン依存**なので時間だけ
   残しても比較できない（別マシン・別モデルの実測はそのまま当てはまらない）。

3. `timing` — 自動生成にかかった時間（1項目あたりの秒数を、使ったモデルごとに比べる）の
   **唯一の出所**。行は生成後に1度だけ作られるので
   `created_at`/`updated_at` の差からも出せず、**生成の瞬間にしか捕まえられない**。
   JSON にしてあるのは Ollama の内訳（`total_duration` / `load_duration` /
   `eval_count` / `eval_duration` / `prompt_eval_count` / `prompt_eval_duration`）を
   列を足さずに残すため。**`load_duration` が別に取れるのが要点**で、初回コールの
   モデルロードを差し引けないと逐次実行の1本目だけ遅く見える。

**すべて nullable。** 既存行（この版より前の記録）は null になり、それは
「記録していなかった窓の記録」を正しく表す。`model_digest` と同じ扱いである。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "915de2ef857a"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ai_input_design_llm_calls"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        _TABLE,
        sa.Column(
            "think",
            sa.Boolean(),
            nullable=True,
            comment="thinking を要求したか（null は未記録＝この列より前の窓の記録）",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "thinking",
            sa.Text(),
            nullable=True,
            comment="モデルが返した推論本文（think=False なら空。null は未記録）",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "endpoint",
            sa.String(length=255),
            nullable=True,
            comment="呼び出し先の base URL（取れなければ null）",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "timing",
            sa.JSON(),
            nullable=True,
            comment="所要時間（elapsed_ms とサーバ側の内訳。null は未記録）",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column(_TABLE, "timing")
    op.drop_column(_TABLE, "endpoint")
    op.drop_column(_TABLE, "thinking")
    op.drop_column(_TABLE, "think")
