"""add index identity to ai_input_design_llm_calls

設計ロールの LLM 呼び出し来歴に **検索に使った索引の同一性** を残す列を足す。

典拠の付与率は永続化された `clinical_annotations.citations` から計算するので、
**下書きを作った時点の索引と、測定した時点の索引が別物でも値は出る**。索引は版管理外
（`.gitignore`）なので、差し替えは diff に現れない。

それまでの担保は2つだけだった。(1) `store.get_store` がロード時に1度だけ出す WARNING
（プロセスごとに1回。キャッシュ後は再照合しない）、(2) テストによるディスク上の索引との
照合（**開発機でしか走らない**＝索引は版管理外）。どちらも
「その典拠がどの索引から作られたか」を後から辿れるようにはしない。

そこで **索引が実際に使われた瞬間に作られる記録**（設計ロールの来歴）へ残す。これで
**典拠 → その典拠を作った DesignLlmCall → 索引の同一性** を辿れる。「指標を記録に
残すときは、その時点の `build_info.json` を併記する」という**人への指示が、機械が担保する
事実になる**（人への指示は忘れられる）。

測定 API の応答に「いまの `build_info`」を貼る形は採らない（来歴の偽装になる）。

既存行は NULL のまま（**この列が無かった窓の記録には索引の同一性が無い**という事実を
そのまま残す。既定値で埋めると「照合した」ように見えてしまう）。

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: str | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ai_input_design_llm_calls",
        sa.Column(
            "index_build_info",
            sa.JSON(),
            nullable=True,
            comment="検索に使った索引の build_info（索引を読まないロールでは null）",
        ),
    )
    op.add_column(
        "ai_input_design_llm_calls",
        sa.Column(
            "index_freeze_mismatches",
            sa.JSON(),
            nullable=True,
            comment=(
                "その索引と照合元の記録の食い違い（空配列なら一致）。**空と NULL は違う**——"
                "NULL は照合していない（索引を読まないロール、または列が無かった窓）"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column("ai_input_design_llm_calls", "index_freeze_mismatches")
    op.drop_column("ai_input_design_llm_calls", "index_build_info")
