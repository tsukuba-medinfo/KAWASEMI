"""実スキーマとモデル宣言の食い違いを検出する。

**この検査は CI では発火しない。** CI もテストも DB を新規に作る（`alembic upgrade head`
か `Base.metadata.create_all`）ので、モデルどおりの表と列が必ず揃う。食い違いが起きるのは
**移行を当て損ねた既存 DB** だけであり、そこを見ているものが今まで無かった。

`/input-targets` が 500 で開かなくなったのがその実例である。
`alembic_version` は `c1f4a2b7d8e3` を主張していたが、祖先の `f7a8b9c0d1e2` が当たって
おらず、`input_targets.needs_attention` が存在しなかった。**`alembic upgrade head` では
直らない**（alembic は当該版を適用済みと信じて何もしない）。欠けは報告されてからも
数週間気付かれないまま運用されていた。

見るもの:

- モデルにあって DB に無い**表**
- モデルにあって DB に無い**列**
- `alembic_version` の記録と、移行スクリプトの head の食い違い

**見ないもの（意図的）**:

- 型・`nullable`・`server_default` の違い。`created_at` に `server_default` が
  無い欠けはこの類型だが、列の有無では検出できない。SQLite の型親和性を挟むと方言ごとに
  誤検出が出るため、ここでは扱わない。移行で入れる既定値は
  `tests/test_timestamp_server_defaults_migration.py` が移行を実際に走らせて見ている。
- DB にあってモデルに無い表・列。`alembic_version` や過去の残骸が混ざり、追いたい欠陥
  （モデルが要求する列が無い）とは別の話になる。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import inspect, text

# **モデルの全モジュールを読み込まないと表が揃わない**（alembic/env.py と同じ理由）。
# `kawasemi.models` だけでは 16 表しか登録されず、`kawasemi.ai_input.models` を足して
# 24 表になる。16 表のままだと `ai_input_executions.batch_job_id` の欠けを
# 見落とす。この import を消すと検査は「差分なし」と答えるようになる。
from kawasemi.ai_input import models as _ai_input_models  # noqa: F401
from kawasemi.models import Base

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection
    from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class SchemaDrift:
    """モデル宣言に対して実スキーマに足りないもの。"""

    missing_tables: tuple[str, ...] = ()
    missing_columns: tuple[tuple[str, str], ...] = ()  # (表名, 列名)
    recorded_revision: str | None = None
    head_revision: str | None = None

    @property
    def revision_disagrees(self) -> bool:
        """移行の記録が head と食い違っているか（どちらも判っているときだけ真）。"""
        if self.recorded_revision is None or self.head_revision is None:
            return False
        return self.recorded_revision != self.head_revision

    def __bool__(self) -> bool:
        return bool(self.missing_tables or self.missing_columns or self.revision_disagrees)

    def messages(self) -> list[str]:
        """人が読む1行ずつの報告。食い違いが無ければ空。"""
        lines: list[str] = []
        for name in self.missing_tables:
            lines.append(f"表が無い: {name}")
        for table, column in self.missing_columns:
            lines.append(f"列が無い: {table}.{column}")
        if self.revision_disagrees:
            lines.append(
                f"移行の記録が head と違う: alembic_version={self.recorded_revision} / "
                f"head={self.head_revision}"
            )
        return lines


def _head_revision() -> str | None:
    """移行スクリプトの head。取り出せなければ None（検査は続ける）。"""
    try:
        from pathlib import Path

        from alembic.config import Config
        from alembic.script import ScriptDirectory

        repo_root = Path(__file__).resolve().parents[2]
        config = Config(str(repo_root / "alembic.ini"))
        config.set_main_option("script_location", str(repo_root / "alembic"))
        heads = ScriptDirectory.from_config(config).get_heads()
    except Exception:  # noqa: BLE001 - 版が読めないことを理由に列の検査を止めない
        return None
    return heads[0] if len(heads) == 1 else None


def _recorded_revision(conn: Connection) -> str | None:
    """DB が主張している版。表が無ければ None（`create_all` で作った DB など）。"""
    try:
        row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except Exception:  # noqa: BLE001 - alembic_version が無い DB は正常な状態でありうる
        return None
    return str(row[0]) if row else None


def inspect_schema(conn: Connection) -> SchemaDrift:
    """同期接続を受け取り、モデル宣言との差分を返す。

    `sqlalchemy.inspect` を使うので SQLite でも Postgres でも同じように動く。
    """
    inspector = inspect(conn)
    present_tables = set(inspector.get_table_names())

    missing_tables: list[str] = []
    missing_columns: list[tuple[str, str]] = []
    for name, table in sorted(Base.metadata.tables.items()):
        if name not in present_tables:
            missing_tables.append(name)
            continue  # 表ごと無いなら列は数えない（同じ欠けを2度報告しない）
        actual = {c["name"] for c in inspector.get_columns(name)}
        for column in table.columns:
            if column.name not in actual:
                missing_columns.append((name, column.name))

    return SchemaDrift(
        missing_tables=tuple(missing_tables),
        missing_columns=tuple(missing_columns),
        recorded_revision=_recorded_revision(conn),
        head_revision=_head_revision(),
    )


async def inspect_schema_async(engine: AsyncEngine) -> SchemaDrift:
    """非同期エンジンから `inspect_schema` を呼ぶ。"""
    async with engine.connect() as conn:
        return await conn.run_sync(inspect_schema)
