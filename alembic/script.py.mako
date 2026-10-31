"""${message}

Revision ID: ${up_revision}
Revises:${" " + down_revision if down_revision else ""}
Create Date: ${create_date}

"""
<%!
def pyrepr(value):
    # revision / down_revision / branch_labels / depends_on は本リポジトリでは
    # 常に str か None（Sequence[str] 分岐が使われたことは無い）。
    # repr() は既定でシングルクォートになり ruff format（ダブルクォート）と食い違うため、
    # ここで直接ダブルクォート表記を組み立てる。
    if value is None:
        return "None"
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return '"' + escaped + '"'
    return repr(value)
%>
## このテンプレートが生成する内容は CI の `ruff check .` の対象になる。
## 変更したら `alembic revision --autogenerate` を空撃ちして lint が通ることを確かめること。
## - 型注釈は py311 の `X | Y`（UP007/UP035 回避）
## - `${imports}`（方言 import）は sqlalchemy と同じ third-party ブロックへ入れる。
##   `alembic` はリポジトリ直下のディレクトリ名と一致するため first-party 扱いになり、
##   注入位置を誤ると I001 になる。
## - 本文が空のスケルトン（`alembic revision` 単独）では sa/op が未使用で F401 が出るが、
##   upgrade()/downgrade() を書けば解消する。
from collections.abc import Sequence

import sqlalchemy as sa
% if imports:
${imports}
% endif

from alembic import op

# revision identifiers, used by Alembic.
revision: str = ${pyrepr(up_revision)}
down_revision: str | Sequence[str] | None = ${pyrepr(down_revision)}
branch_labels: str | Sequence[str] | None = ${pyrepr(branch_labels)}
depends_on: str | Sequence[str] | None = ${pyrepr(depends_on)}


def upgrade() -> None:
    """Upgrade schema."""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """Downgrade schema."""
    ${downgrades if downgrades else "pass"}
