"""文書種別の所在記述に種別と絞り込みを入れ直す

Revision ID: c1f4a2b7d8e3
Revises: 85159bbd9a3e
Create Date: 2026-08-17 09:40:00.000000

`build_location` が組む `location` は、文書種別を所在にした候補では
**すべて同一の文字列**になっていた。本文はどの種別も同じ表・列に入っており、
種別で分かれるのは文書ヘッダ側の絞り込み条件だけなので、種別を書かないと区別が消える。

**人の判断は書き換えない。** 直すのは `location`（機械が payload から組み立てた表示用の
文字列）だけで、`unit_id` / `payload` / `source_category` / `connection_id` は触らない。
`status` / `confirmed_at` / `confirmed_by_id` も保持する——人が選んだ所在そのものは
変わっておらず、その表示が不足していただけである。

`proposed_candidates` 側も同じ規則で書き換える。片方だけ直すと、画面の復元
（`sameCandidate` が `location` を見る）で決着が提示スレートと結びつかなくなる。

**索引は読まない。** 候補の `payload` が `doc_type_code` / `doc_type_name` /
`doc_filter_table` / `doc_filter_column` を持っているので、そこから組み直せる。
索引は版管理外なので、移行が索引の有無に依存しないほうがよい。

`compute_cand_key` は `location` を含むのでキーが変わる。`location_candidate_assessments`
に行がある DB では、移行の前に孤児が出ないか確かめること。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f4a2b7d8e3"
down_revision: str | Sequence[str] | None = "85159bbd9a3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "system_locations"
_COLUMNS = ("candidates", "proposed_candidates")


def _with_doc_type(location: str, payload: dict) -> str:
    """`build_location`（`resource_index/derive.py`）の文書種別分岐と同じ文字列を作る。

    **本体の関数を import しない。** マイグレーションは書かれた時点の規則を再現する
    ものであり、あとから本体が変わっても過去の移行の意味は変わってはいけない。
    """
    code = payload.get("doc_type_code")
    if not code:
        return location
    if "文書種別:" in location:
        return location  # 既に入っている（再実行しても壊さない）
    name = payload.get("doc_type_name") or ""
    out = f"{location} / 文書種別: {code}（{name}）"
    ftable, fcolumn = payload.get("doc_filter_table"), payload.get("doc_filter_column")
    if ftable and fcolumn:
        out += f" / 絞り込み: {ftable}.{fcolumn}='{code}'"
    return out


def _rewrite(candidates: list | None) -> tuple[list | None, int]:
    if not isinstance(candidates, list):
        return candidates, 0
    changed = 0
    out = []
    for c in candidates:
        if not isinstance(c, dict):
            out.append(c)
            continue
        payload = c.get("payload")
        location = c.get("location")
        if isinstance(payload, dict) and isinstance(location, str):
            new = _with_doc_type(location, payload)
            if new != location:
                c = {**c, "location": new}
                changed += 1
        out.append(c)
    return out, changed


def upgrade() -> None:
    import json

    conn = op.get_bind()
    rows = conn.execute(
        sa.text(f"SELECT id, {', '.join(_COLUMNS)} FROM {_TABLE}")  # noqa: S608
    ).fetchall()
    total = 0
    for row in rows:
        updates = {}
        for i, col in enumerate(_COLUMNS, start=1):
            raw = row[i]
            value = json.loads(raw) if isinstance(raw, str) else raw
            new, changed = _rewrite(value)
            if changed:
                updates[col] = json.dumps(new, ensure_ascii=False)
                total += changed
        if updates:
            sets = ", ".join(f"{c} = :{c}" for c in updates)
            conn.execute(
                sa.text(f"UPDATE {_TABLE} SET {sets} WHERE id = :id"),  # noqa: S608
                {**updates, "id": row[0]},
            )
    print(f"文書種別の所在記述を書き直した: {total} 件")


def downgrade() -> None:
    """`location` の表示を戻す（人の判断は元から触っていない）。"""
    import json
    import re

    conn = op.get_bind()
    pattern = re.compile(r" / 文書種別: .*$")
    rows = conn.execute(
        sa.text(f"SELECT id, {', '.join(_COLUMNS)} FROM {_TABLE}")  # noqa: S608
    ).fetchall()
    for row in rows:
        updates = {}
        for i, col in enumerate(_COLUMNS, start=1):
            raw = row[i]
            value = json.loads(raw) if isinstance(raw, str) else raw
            if not isinstance(value, list):
                continue
            out, changed = [], 0
            for c in value:
                if isinstance(c, dict) and isinstance(c.get("location"), str):
                    stripped = pattern.sub("", c["location"])
                    if stripped != c["location"]:
                        c = {**c, "location": stripped}
                        changed += 1
                out.append(c)
            if changed:
                updates[col] = json.dumps(out, ensure_ascii=False)
        if updates:
            sets = ", ".join(f"{c} = :{c}" for c in updates)
            conn.execute(
                sa.text(f"UPDATE {_TABLE} SET {sets} WHERE id = :id"),  # noqa: S608
                {**updates, "id": row[0]},
            )
