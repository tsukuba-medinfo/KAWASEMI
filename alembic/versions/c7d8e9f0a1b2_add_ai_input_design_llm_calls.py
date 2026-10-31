"""add ai_input_design_llm_calls

設計ロール（臨床的意味 / データ所在 / 取得手順書 / 取得プラン）の LLM 呼び出しについて、
プロンプト全文と生応答を保存するテーブルを追加する。実行ロールが evidence trace に
残しているのと同じ粒度の来歴を、設計ロールにも持たせるため。

患者データを保存しない前提と、前提が崩れる経路に対するガードは
`docs/design_llm_provenance.md` および `kawasemi/ai_input/design_provenance.py` を参照。

Revision ID: c7d8e9f0a1b2
Revises: 5476e155ca8a
Create Date: 2026-08-03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | None = "5476e155ca8a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ai_input_design_llm_calls",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("questionnaire_id", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.String(length=32),
            nullable=False,
            comment="設計ロールの区分（clinical / system / runbook / plan）",
        ),
        sa.Column(
            "label",
            sa.String(length=100),
            nullable=False,
            comment="呼び出しの処理名（ログ・例外と同じラベル。例「手順書生成」）",
        ),
        sa.Column("model", sa.String(length=100), nullable=False, comment="生成に使ったモデル名"),
        sa.Column(
            "options",
            sa.JSON(),
            nullable=False,
            comment="Ollama options（temperature / num_ctx 等）。再現性の記録",
        ),
        sa.Column("system_prompt", sa.Text(), nullable=False, comment="システムプロンプト全文"),
        sa.Column(
            "user_message",
            sa.Text(),
            nullable=False,
            comment="ユーザメッセージ全文（項目名・所在記述・規約文書の抜粋など）",
        ),
        sa.Column(
            "raw_output",
            sa.Text(),
            nullable=True,
            comment="モデルの生応答（パース前）。失敗時も残す",
        ),
        sa.Column("error", sa.Text(), nullable=True, comment="失敗した場合の理由（成功時は null）"),
        sa.Column(
            "chunk_index",
            sa.Integer(),
            nullable=False,
            comment="分割呼び出しの何回目か（chat_json_chunked）",
        ),
        sa.Column(
            "item_count",
            sa.Integer(),
            nullable=False,
            comment="このチャンクで渡した項目数",
        ),
        sa.Column(
            "truncated",
            sa.Boolean(),
            nullable=False,
            comment="保存上限で切り詰めたか（切り詰めた事実を残す）",
        ),
        sa.Column(
            "content_sources",
            sa.JSON(),
            nullable=False,
            comment=(
                "プロンプトへ中身を入れた索引ユニットの出所（'kind:file_role' の一覧）。"
                "ファイルは file_role=reference に限る（design_provenance のガード2）"
            ),
        ),
        sa.Column("created_by_id", sa.Integer(), nullable=True, comment="生成を実行した操作者"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["questionnaire_id"], ["questionnaires.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_ai_input_design_llm_calls_questionnaire_id"),
        "ai_input_design_llm_calls",
        ["questionnaire_id"],
    )
    op.create_index(
        op.f("ix_ai_input_design_llm_calls_role"), "ai_input_design_llm_calls", ["role"]
    )
    op.create_index(
        op.f("ix_ai_input_design_llm_calls_created_by_id"),
        "ai_input_design_llm_calls",
        ["created_by_id"],
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_ai_input_design_llm_calls_created_by_id"),
        table_name="ai_input_design_llm_calls",
    )
    op.drop_index(op.f("ix_ai_input_design_llm_calls_role"), table_name="ai_input_design_llm_calls")
    op.drop_index(
        op.f("ix_ai_input_design_llm_calls_questionnaire_id"),
        table_name="ai_input_design_llm_calls",
    )
    op.drop_table("ai_input_design_llm_calls")
