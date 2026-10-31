"""collapse_roles_to_four

役割を7つから4つに畳む:

- `collection_lead` / `plan_designer` / `data_manager` → `designer`
- `verifier` / `data_user` → `reviewer`
- `data_entry` → `collector`
- `ai_designer`（設計を3役割に分ける前の旧役割。分割の移行が役割の行を残していたため、
  古い DB では設計者の利用者がこれだけを保有していることがある）→ `designer`
- `admin` はそのまま（説明文だけ更新）

既存 DB の利用者が移行後も同じ画面に入れるよう、`user_roles` の行を新役割へ写してから
旧役割を消す。同じ利用者が旧3役割を持っていた場合は `designer` 1行に畳む（重複を作らない）。
来歴（`confirmed_by_id` 等）は利用者 ID なので触らない。`input_trial_runs.active_role` の
旧値もそのまま残す（試行時の記録であり、認可には使わない）。

Revision ID: d5e6f7a8b9c0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-22

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: str | None = "b4c5d6e7f8a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# 旧役割 → 新役割
ROLE_MAP: dict[str, str] = {
    "collection_lead": "designer",
    "plan_designer": "designer",
    "data_manager": "designer",
    "data_entry": "collector",
    "verifier": "reviewer",
    "data_user": "reviewer",
    # 3役割へ分割する前の旧役割。downgrade では戻さない（分割後の3役割へ戻す）
    "ai_designer": "designer",
}

DESCRIPTIONS: dict[str, str] = {
    "admin": "管理者（アカウント・役割の付与、接続と索引の状態、ログ、様式のファイル取り込み。患者データは見ない）",
    "designer": "設計者（臨床的意味・所在・手順書・取得プラン・設計キャンバス・様式の内容と ValueSet の編集。回答の入力・送信はしない）",
    "collector": "収集者（データセット・症例の割り当て・自動収集のバッチ起動と本番実行・手動入力・回答送信。設計の確定はしない）",
    "reviewer": "閲覧・検証者（回答の閲覧・チェック・検証・差し戻し・エクスポート・取得メトリクス。入力・設計はしない）",
}

# downgrade 用（新 → 旧。設計者は旧3役割すべてに戻す）
REVERSE_MAP: dict[str, list[str]] = {
    "designer": ["collection_lead", "plan_designer", "data_manager"],
    "collector": ["data_entry"],
    "reviewer": ["verifier", "data_user"],
}

OLD_DESCRIPTIONS: dict[str, str] = {
    "collection_lead": "臨床定義責任者（取りたいデータの臨床的意味＝臨床的コンテキストを定義・編集）",
    "plan_designer": "データ取得計画設計者（取得手順書・取得プランの設計。臨床とシステムの橋渡し）",
    "data_manager": "データ所在管理者（データの所在＝システム場所の定義、索引知識の確認、プランのSQL補完）",
    "data_entry": "データ収集者（割り当て症例の手動入力・autofill本番実行・回答の確定保存。記入責任を負う）",
    "verifier": "品質保証／検証者（回答の閲覧・checkモード・原資料突合。自分の入力は検証不可）",
    "data_user": "データ利用者／解析担当（収集データの閲覧・エクスポート・分析・EHR出力）",
}


def _roles_table() -> sa.Table:
    return sa.table(
        "roles",
        sa.column("id", sa.Integer),
        sa.column("name", sa.String),
        sa.column("description", sa.Text),
    )


def _user_roles_table() -> sa.Table:
    return sa.table(
        "user_roles",
        sa.column("id", sa.Integer),
        sa.column("user_id", sa.Integer),
        sa.column("role_id", sa.Integer),
        sa.column("assigned_by_id", sa.Integer),
    )


def _role_ids(conn: sa.Connection) -> dict[str, int]:
    roles = _roles_table()
    rows = conn.execute(sa.select(roles.c.name, roles.c.id)).all()
    return {str(name): int(rid) for name, rid in rows}


def _ensure_role(conn: sa.Connection, name: str, description: str) -> int:
    roles = _roles_table()
    ids = _role_ids(conn)
    if name in ids:
        conn.execute(sa.update(roles).where(roles.c.name == name).values(description=description))
        return ids[name]
    conn.execute(sa.insert(roles).values(name=name, description=description))
    return _role_ids(conn)[name]


def _remap(
    conn: sa.Connection, mapping: dict[str, list[str]], descriptions: dict[str, str]
) -> None:
    """`mapping` の各 (旧名 → 新名のリスト) について user_roles を写し、旧役割の行と役割を消す。

    新役割の行は (user_id, role_id) が既にあれば作らない（重複を作らない）。
    """
    user_roles = _user_roles_table()
    roles = _roles_table()
    ids = _role_ids(conn)
    for old_name, new_names in mapping.items():
        if old_name not in ids:
            continue
        old_id = ids[old_name]
        held = conn.execute(
            sa.select(user_roles.c.user_id, user_roles.c.assigned_by_id).where(
                user_roles.c.role_id == old_id
            )
        ).all()
        for new_name in new_names:
            new_id = _ensure_role(conn, new_name, descriptions[new_name])
            for user_id, assigned_by_id in held:
                exists = conn.execute(
                    sa.select(user_roles.c.id)
                    .where(user_roles.c.user_id == user_id, user_roles.c.role_id == new_id)
                    .limit(1)
                ).first()
                if exists is None:
                    conn.execute(
                        sa.insert(user_roles).values(
                            user_id=user_id, role_id=new_id, assigned_by_id=assigned_by_id
                        )
                    )
        conn.execute(sa.delete(user_roles).where(user_roles.c.role_id == old_id))
        conn.execute(sa.delete(roles).where(roles.c.id == old_id))


def upgrade() -> None:
    conn = op.get_bind()
    _remap(conn, {old: [new] for old, new in ROLE_MAP.items()}, DESCRIPTIONS)
    # 4役割が揃っていることを保証する（旧役割の保持者が居なくても作る）
    for name, description in DESCRIPTIONS.items():
        _ensure_role(conn, name, description)


def downgrade() -> None:
    conn = op.get_bind()
    _remap(conn, REVERSE_MAP, OLD_DESCRIPTIONS)
