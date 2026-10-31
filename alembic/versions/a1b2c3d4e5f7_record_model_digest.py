"""record the model digest in the design-role provenance

LLM の再現性の記録では「使用したモデル（**タグとダイジェスト**）が実行のたびに
記録される」ことを求めるが、これまで記録していたのはタグだけだった。

タグだけでは実装が特定できない。モデル名の設定（`.env`）を書き換える経路は
タグの記録で捕まえられるが、**同じタグの中身が差し替わる**経路は残っていた。

- `ollama pull` で上流のタグが更新された場合
- ローカルで同名タグを作り直した場合

どちらも記録は同じ文字列（`model-a:7b` 等）のままで、**2回の実行を記録だけでは区別できない**。

2つの列を足す。**null の意味が2つあるので理由を別の列に持つ**（「空配列と null は
意味が違う」と同じ規則）。

- `model_digest`: 取れたダイジェスト
- `model_digest_unavailable`: 引いたが取れなかった理由。**両方 null は「宣言していない」**
  （この列が無かった窓の記録、またはダイジェストを宣言しないロール）

フェイルクローズにしない判断は `kawasemi/ai_input/llm_provider.py` の `model_digest_of` に
書いた（来歴の記録が生成そのものを壊してはならない。`provider_of` と同じ理由）。

Revision ID: a1b2c3d4e5f7
Revises: f1b2c3d4e5f6
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | None = "f1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_input_design_llm_calls",
        sa.Column(
            "model_digest",
            sa.String(length=128),
            nullable=True,
            comment="モデルのダイジェスト（取れなかった場合は null。理由は model_digest_unavailable）",
        ),
    )
    op.add_column(
        "ai_input_design_llm_calls",
        sa.Column(
            "model_digest_unavailable",
            sa.String(length=255),
            nullable=True,
            comment="ダイジェストが取れなかった理由（取れた場合は null。両方 null は未宣言）",
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_input_design_llm_calls", "model_digest_unavailable")
    op.drop_column("ai_input_design_llm_calls", "model_digest")
