"""persist duplicated LLM responses per item

「同じ項目に2エントリ返った応答は**集計の分子から外して別掲する**」という集計の規則が
あるが、`duplicated_link_ids` は**どこにも永続化されていなかった**。
出るのはプロセス内の戻り値・その HTTP 応答・ログの3つだけである。

したがって**永続化された `clinical_annotations.citations` から数える経路**——
`citation_metrics.citation_coverage` と集計エンドポイント——は、矛盾した応答を外せない。
集計は「**いつ測っても同じ値が出る**こと」が要件なので、**矛盾を外した分子は
「測定時の run の記録が手元にある間だけ再現できる」**という非対称が残っていた。

2つの列を足す。

- `ai_input_design_llm_calls.duplicated_link_ids`: **サーバ側の真実**。下書きを作った
  その場（LLM 応答をマージした直後）に記録する。クライアントの申告に依存しない
  （クライアントが申告する `origin` は信用できない）
- `clinical_annotations.duplicated_response`: **項目に紐づいた事実**。保存時に上の記録から
  引いて立てる。測定の記録を持たない環境でも `SELECT` 一発で分かる

**集計関数の分子の定義は変えない。** 矛盾した応答は**別掲**として同じ器から
出す。既存の集計結果を変えずに、「いつ測っても同じ値が出る」要件を満たせる。

Revision ID: f1b2c3d4e5f6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1b2c3d4e5f6"
down_revision: str | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_input_design_llm_calls",
        sa.Column(
            "duplicated_link_ids",
            sa.JSON(),
            nullable=True,
            comment="同じ項目に複数のエントリが返った link_id（空なら重複なし。null は未記録）",
        ),
    )
    op.add_column(
        "clinical_annotations",
        sa.Column(
            "duplicated_response",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="矛盾した応答（同じ項目に複数エントリ）由来の下書きか（別掲用）",
        ),
    )


def downgrade() -> None:
    op.drop_column("clinical_annotations", "duplicated_response")
    op.drop_column("ai_input_design_llm_calls", "duplicated_link_ids")
