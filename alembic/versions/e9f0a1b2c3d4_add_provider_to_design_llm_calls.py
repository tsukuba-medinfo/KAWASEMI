"""add provider to ai_input_design_llm_calls

設計ロールの LLM 呼び出し来歴に **推論プロバイダ** を残す列を足す。

モデル名だけでは、どの実装が生成したかを特定できない。`OLLAMA_MODEL` は環境変数で
上書きでき、同じモデル名を別の推論エンジンが名乗ることもできる。Anthropic 互換 API
などの別経路でモデルを使う場合、`provider` が無いと記録上 Ollama と区別がつかず、
生成結果の比較可能性を後から検証できない。

既存行は `server_default="ollama"` で埋める。この列が無かった窓の記録は**すべて Ollama で
生成されている**（それ以外の経路が存在しなかった）ので、既定値は事実と一致する。

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-08-11
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_input_design_llm_calls",
        sa.Column(
            "provider",
            sa.String(length=32),
            nullable=False,
            server_default="ollama",
            comment=("推論プロバイダ（ollama 等）。モデル名だけでは実装が特定できないため別に持つ"),
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_input_design_llm_calls", "provider")
