"""DSL が書いた SQL をアプリDBへ**読み取り専用で**発行する経路。

`sql_extract`（`_op_sql`）と統計プロファイルの自アプリDB集計は、LLM や索引が組んだ SQL を
アプリDBへ発行する。文字列検査（`is_select_only_sql`）は多層防御の1枚にすぎず、
**SELECT から到達できる書き込み**（`WITH x AS (INSERT ... RETURNING 1) SELECT ...`、
書き込みをする関数の呼び出しなど）を取りこぼしうる。保証は DB 側に置く。

方言ごとの実現:

- **SQLite**: 呼び出し側のセッションに `PRAGMA query_only = ON` と authorizer（下の「読める表」）を
  掛け、抜けるときに戻す。別コネクションを張らないので、テストの `:memory:` DB でも同じ関門を通る（従来どおり）
- **その他（PostgreSQL 等）**: 呼び出し側のセッションは使わず、**別の接続**を取り、
  **読み取り専用トランザクション**（PostgreSQL は `postgresql_readonly` ＝ `BEGIN READ ONLY`、
  それ以外は `SET TRANSACTION READ ONLY`）の中で読み、**必ずロールバックする**。
  接続元は次の順:

  1. `READONLY_DATABASE_URL` があれば、その DSN のエンジン（読み取り専用ロール。遅延生成・
     URL 単位でキャッシュし、シャットダウン時に破棄）
  2. 無ければ、`ENVIRONMENT=production` では**実行しない**（`ReadOnlySessionUnavailableError`。
     フェイルクローズ）。読み取り専用トランザクションは書き込みを止めるが、
     `users.password_hash` のような読み取りまでは止めず、それを閉じるのは専用ロールだけだから。
     production 以外ではアプリ本体のエンジン（読み書きロール）から別接続を取り、
     読み取り専用トランザクションで実行する。どちらも起動時と `check_schema` で警告する

  別接続にするのは、同じトランザクションを読み取り専用にすると後続の evidence の保存まで
  止まるからである。アプリのエンジンを使う経路では、実行のたびにアプリの接続プールから
  **2本目の接続**を借りる（セッションが持つ1本とは別）。

**読める表の範囲を実行時に絞る**（F8/F9）。読み取り専用は書き込みを止めるだけで、
`users.password_hash` のような**読み取りは止めない**。そこで、プログラム SQL が触れる表を
実行の側でも検査する:

- **拒否する表**: アプリ自身が持つ表（`Base.metadata` の全表。認証・権限・回答・割当・
  実行記録）と `alembic_version`、SQLite のスキーマ表・`pragma_*`、PostgreSQL の
  `pg_catalog` / `information_schema`。`sql_extract` が読むのは**臨床データの表**で、
  アプリの内部表ではない
- **許す表を限る**（`allowed_tables`）: 呼び出し側が渡すと、読める表がその表だけになる
  （現在は呼び出し側が渡していない。SQL の患者の範囲は、アプリでは保証しない。docs/DEPLOYMENT.md）
- SQLite は接続の `set_authorizer` で、**文を準備する時点**に DB 側が検査する（文字列検査では
  ない。ビュー・CTE・サブクエリ経由でも、実際に読む表で判定される）。
  PostgreSQL は `EXPLAIN (VERBOSE, FORMAT JSON)` の計画に出る実表で検査する（`assert_relations_permitted`。
  ビューは基表まで展開される）。**PostgreSQL の境界は引き続きロールの権限**で、計画の検査は
  「計画に出る表の読み取り」を塞ぐ追加の層である。**関数が表を読む形**（`query_to_xml` や
  `dblink` など）は計画に実表として出ないので、`is_select_only_sql` の禁止語の一覧とロールの
  権限に頼る。PostgreSQL・SQLite 以外の方言は表を検査できないので実行しない

PostgreSQL では加えて:

- トランザクションの先頭で `statement_timeout` を `SET LOCAL` 相当で掛ける
  （`READONLY_STATEMENT_TIMEOUT_SECONDS`、既定 60 秒、0 で無効）。長い SELECT や `pg_sleep` で
  接続を握り続けさせない
- 抜けるときにロールバックしてから `pg_advisory_unlock_all()` を呼ぶ。advisory lock は
  セッションに残り、ロールバックでは外れないので、プールへ返す前に必ず外す。外せなかった接続は
  プールへ返さず破棄する（`invalidate`）
- `SET` / `set_config` で変えた設定はトランザクションと一緒にロールバックされるので、
  プールへ持ち越されない
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import AsyncIterator, Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# READONLY_DATABASE_URL のエンジン。URL 単位で1つだけ持つ（設定の差し替えに追従する）。
_readonly_engines: dict[str, AsyncEngine] = {}

_SET_THE_DSN = (
    "読み取り専用ロールの DSN を READONLY_DATABASE_URL に設定してください"
    "（docs/DEPLOYMENT.md の「読み取り専用ロール」）。"
)
UNCONFIGURED_WARNING = (
    "READONLY_DATABASE_URL が未設定です。ENVIRONMENT が production ではないため"
    "（未設定時の既定は development）、sql_extract と統計プロファイルの SQL は"
    "アプリ本体のロールで、読み取り専用トランザクションの中で実行します"
    "（書き込みは止まるが、users.password_hash を含め、アプリのロールが読める表はすべて読める）。"
    "本番では ENVIRONMENT=production を設定してください。" + _SET_THE_DSN
)
UNCONFIGURED_PRODUCTION_WARNING = (
    "READONLY_DATABASE_URL が未設定のため、ENVIRONMENT=production では sql_extract と"
    "統計プロファイルの自アプリDB集計を実行しません（source_unavailable）。" + _SET_THE_DSN
)


class ReadOnlySessionUnavailableError(RuntimeError):
    """プログラム SQL を読み取り専用ロールで実行する手段が用意できない（production）。"""

    kawasemi_authored = True


class ProgramSqlDeniedError(RuntimeError):
    """プログラム SQL が、読むことを許されていない表に触れた（ポリシー拒否）。"""

    kawasemi_authored = True


_CATALOG_SCHEMAS = {"pg_catalog", "information_schema"}


def denied_tables() -> frozenset[str]:
    """プログラム SQL に読ませない表（小文字）。アプリ自身の内部表すべて。

    `Base.metadata` から引くので、モデルに表が増えれば自動で入る（認証の表だけを列挙すると、
    回答や実行記録のような PHI を持つ表が漏れる）。
    """
    # 遅延 import: モデルの登録を import 時の副作用に頼るので、循環を避けて呼ぶ時に読む
    from kawasemi.ai_input import models as _ai_input_models  # noqa: F401
    from kawasemi.models import Base

    return frozenset(name.lower() for name in Base.metadata.tables) | {"alembic_version"}


def _clean(identifier: str) -> str:
    return identifier.strip().strip('"`[]').lower()


def _normalize_allowed(
    allowed_tables: Iterable[str] | None, *, keep_schema: bool = False
) -> frozenset[str] | None:
    """許す表の集合。SQLite の authorizer は表名だけを見るので既定ではスキーマを落とす。"""
    if allowed_tables is None:
        return None
    out: set[str] = set()
    for t in allowed_tables:
        parts = [_clean(p) for p in str(t).split(".")]
        out.add(".".join(parts[-2:] if keep_schema else parts[-1:]))
    return frozenset(out)


def table_read_permitted(
    table: str | None,
    allowed_tables: frozenset[str] | None = None,
    schema: str | None = None,
) -> bool:
    """`table` を読んでよいか（SQLite の authorizer と PostgreSQL の計画検査が共有する規則）。

    `allowed_tables` の要素が `schema.table` なら、計画が `schema` を与えているとき一致を見る
    （同名の別スキーマの表を通さない）。`table` だけなら表名で見る。
    """
    name = _clean(table or "")
    if not name:
        return True
    if name in denied_tables() or name.startswith(("sqlite_", "pragma_", "pg_")):
        return False
    if schema and _clean(schema) in {"pg_catalog", "information_schema"}:
        return False
    if allowed_tables is None:
        return True
    return name in allowed_tables or (
        bool(schema) and f"{_clean(schema or '')}.{name}" in allowed_tables
    )


def _sqlite_authorizer(allowed_tables: frozenset[str] | None) -> Callable[..., int]:
    """SQLite の `set_authorizer` 用コールバック。SELECT が読む表だけを通し、ほかは全部拒否する。"""
    denied = denied_tables()
    permitted_actions = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }

    def authorizer(action: int, arg1: str | None, arg2: str | None, *_: object) -> int:
        if action == sqlite3.SQLITE_READ:
            name = (arg1 or "").lower()
            if name in denied or name.startswith(("sqlite_", "pragma_")):
                return sqlite3.SQLITE_DENY
            if allowed_tables is not None and name not in allowed_tables:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_OK if action in permitted_actions else sqlite3.SQLITE_DENY

    return authorizer


async def _set_sqlite_authorizer(db: AsyncSession, callback: Callable[..., int] | None) -> None:
    """セッションの SQLite 接続へ authorizer を掛ける（None で外す）。

    aiosqlite は接続を専用スレッドで持つので、そのスレッドの中で呼ぶ必要がある
    （SQLAlchemy のアダプタは `set_authorizer` を公開していない）。掛けられないときは
    **実行しない**（読ませてはならない表を読ませるより失敗させる）。
    """
    conn = await db.connection()
    raw = await conn.get_raw_connection()
    driver = raw.driver_connection
    execute = getattr(driver, "_execute", None)
    inner = getattr(driver, "_conn", None)
    if execute is None or inner is None or not hasattr(inner, "set_authorizer"):
        raise ReadOnlySessionUnavailableError(
            "SQLite の接続に authorizer を掛けられないため、プログラム SQL を実行しません"
        )
    await execute(inner.set_authorizer, callback)


def is_authorizer_denial(exc: BaseException) -> bool:
    """SQLite の authorizer が文を拒否したときの例外か（`not authorized`）。"""
    message = str(getattr(exc, "orig", None) or exc).lower()
    return isinstance(exc, DBAPIError) and (
        "not authorized" in message or "is prohibited" in message
    )


def _plan_relations(node: Any) -> set[tuple[str | None, str]]:
    """`EXPLAIN (VERBOSE, FORMAT JSON)` の計画から、読まれる実表 `(schema, 表名)` を集める。"""
    found: set[tuple[str | None, str]] = set()
    if isinstance(node, dict):
        name = node.get("Relation Name")
        if isinstance(name, str):
            found.add((node.get("Schema"), name))
        for value in node.values():
            found |= _plan_relations(value)
    elif isinstance(node, list):
        for item in node:
            found |= _plan_relations(item)
    return found


# カタログの関数の呼び出し（`pg_stat_get_activity(...)`、`pg_get_viewdef(...)`、
# `pg_relation_size(...)`）。結合の省略で表の読み取りが消えても、計画の関数ノードや出力式に残る
_CATALOG_CALL = re.compile(r"\bpg_[a-z0-9_]+\s*\(|\b(?:pg_catalog|information_schema)\s*\.", re.I)


def _plan_catalog_references(node: Any) -> list[str]:
    """計画の中の、カタログ（`pg_catalog` / `information_schema` / `pg_*` 関数）への参照。

    `Relation Name` を持たない読み取り（`pg_stat_activity` などの関数で作るビューは、
    結合の省略で `Function Scan on pg_stat_get_activity` だけが残る）と、出力式に現れる
    関数呼び出しを拾う。関数ノード（`Function Name`）と、計画の文字列値（`Output`・
    `Function Call`・`Filter` など）を見る。

    拒否するのは**名前が `pg_` で始まる関数**（大文字小文字を区別しない）だけで、`pg_catalog` に
    ある組み込みの集合を返す関数（`generate_series`・`unnest`・`jsonb_array_elements`・
    `regexp_split_to_table` など）は通す（正当な SQL が使う）。計画の文字列値の走査は
    リテラルの中身も見るので、`'pg_x('` を含む定数があると誤って拒否する（安全側の誤検知）。
    """
    found: list[str] = []
    if isinstance(node, dict):
        func = node.get("Function Name")
        if isinstance(func, str) and func.lower().startswith("pg_"):
            found.append(func)
        for value in node.values():
            found.extend(_plan_catalog_references(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_plan_catalog_references(item))
    elif isinstance(node, str) and _CATALOG_CALL.search(node):
        found.append(node)
    return found


async def assert_relations_permitted(
    reader: AsyncSession | AsyncConnection,
    sql: str,
    binds: dict[str, Any] | None = None,
    *,
    allowed_tables: Iterable[str] | None = None,
) -> None:
    """PostgreSQL: SQL の**計画に出る実表**を実行前に調べ、許されない表なら拒否する。

    調べるのは計画の `Relation Name`（ビューは基表まで展開される）と、カタログへの参照
    （名前が `pg_` で始まる関数ノードと `pg_*` 関数の呼び出し。`pg_stat_activity`、
    `pg_settings`、`pg_get_viewdef(...)`、`pg_relation_size(...)` など）である。**ユーザー定義や
    拡張の関数が表を読む形**（`query_to_xml('select ...')`、`dblink` など）は計画に実表として
    現れないので、ここでは捕まえない。そちらは `is_select_only_sql` の禁止語の一覧（補助の層）と、
    ロールの権限（境界）に頼る。

    SQLite は接続の authorizer が文の準備時に見るので何もしない。PostgreSQL・SQLite 以外の
    方言は、表の検査ができないので**実行しない**（フェイルクローズ）。SELECT / WITH 以外は計画を
    取らない（そのまま読み取り専用トランザクションが止める）。

    Raises:
        ProgramSqlDeniedError: 許されない表（またはスキーマ）を読む、または方言が不明
    """
    dialect = getattr(getattr(reader, "dialect", None), "name", None)
    if dialect is None and isinstance(reader, AsyncSession):
        dialect = reader.get_bind().dialect.name
    if dialect == "sqlite":
        return
    if dialect != "postgresql":
        raise ProgramSqlDeniedError(
            f"この DB 方言（{dialect}）では読む表を検査できないため、プログラム SQL を実行しません"
        )
    if not sql.lstrip().lower().startswith(("select", "with")):
        return
    allowed = await _qualified_allowance(reader, allowed_tables)
    # VERBOSE にすると、計画の各表が `Schema`（実際に解決されたスキーマ）を持つ
    result = await reader.execute(text(f"EXPLAIN (VERBOSE, FORMAT JSON) {sql}"), binds or {})
    plan = result.scalar_one()
    if isinstance(plan, str | bytes):
        plan = json.loads(plan)
    for schema, table in _plan_relations(plan):
        if not table_read_permitted(table, allowed, schema):
            raise ProgramSqlDeniedError(f"このSQLが読む表は許されていません: {table}")
    catalog = _plan_catalog_references(plan)
    if catalog:
        raise ProgramSqlDeniedError(f"このSQLはカタログを参照しています: {catalog[0]}")


async def _qualified_allowance(
    reader: AsyncSession | AsyncConnection, allowed_tables: Iterable[str] | None
) -> frozenset[str] | None:
    """許す表を `schema.table` にそろえる。スキーマ無しの表は、接続の現在のスキーマの表とみなす。

    SQL の中のスキーマ無しの表名は検索パスで解決される。許可の側もそれと同じ解決にしないと、
    同名の別スキーマの表（`archive.master`）が、スキーマ無しの許可（`master`）で通ってしまう。
    """
    if allowed_tables is None:
        return None
    entries = [str(t) for t in allowed_tables]
    current: str | None = None
    if any("." not in e for e in entries):
        current = _clean(str((await reader.execute(text("SELECT current_schema()"))).scalar_one()))
    out: set[str] = set()
    for e in entries:
        parts = [_clean(p) for p in e.split(".")]
        out.add(".".join(parts[-2:]) if len(parts) > 1 else f"{current}.{parts[0]}")
    return frozenset(out)


def _refuses_without_dsn() -> bool:
    return settings.environment == "production"


def get_readonly_engine() -> AsyncEngine | None:
    """`READONLY_DATABASE_URL` のエンジン。未設定なら None。初回に生成してキャッシュする。"""
    url = settings.readonly_database_url
    if not url:
        return None
    engine = _readonly_engines.get(url)
    if engine is None:
        engine = create_async_engine(url, pool_pre_ping=True, hide_parameters=True)
        _readonly_engines[url] = engine
    return engine


async def dispose_readonly_engine() -> None:
    """生成した読み取り専用エンジンを破棄する（アプリのシャットダウン時）。"""
    engines = list(_readonly_engines.values())
    _readonly_engines.clear()
    for engine in engines:
        await engine.dispose()


def readonly_route_warning(app_dialect: str) -> str | None:
    """読み取り専用ロールが無いまま非 SQLite で動いているときの警告文。問題なければ None。"""
    if app_dialect == "sqlite" or settings.readonly_database_url:
        return None
    return UNCONFIGURED_PRODUCTION_WARNING if _refuses_without_dsn() else UNCONFIGURED_WARNING


def readonly_execution_options(dialect: str) -> dict[str, Any]:
    """読み取り専用トランザクションを張るための `execution_options`。"""
    if dialect == "postgresql":
        return {"postgresql_readonly": True}
    return {}


def _app_engine(db: AsyncSession) -> AsyncEngine:
    bind = db.bind
    if isinstance(bind, AsyncEngine):
        return bind
    if isinstance(bind, AsyncConnection):
        return bind.engine
    from kawasemi.core.database import engine

    return engine


async def _release_session_state(conn: AsyncConnection, dialect: str) -> None:
    """ロールバックでは戻らないセッション状態を外す（プールへ返す前）。"""
    if dialect == "sqlite":
        await conn.execute(text("PRAGMA query_only = OFF"))
    elif dialect == "postgresql":
        await conn.execute(text("SELECT pg_advisory_unlock_all()"))
    else:
        return
    await conn.rollback()


@asynccontextmanager
async def readonly_transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """別接続で読み取り専用トランザクションを張り、抜けるときに必ずロールバックする。"""
    dialect = engine.dialect.name
    async with engine.connect() as conn:
        conn = await conn.execution_options(**readonly_execution_options(dialect))
        try:
            if dialect == "sqlite":
                # 読み取り専用 DSN に SQLite を置いた場合（主にテスト）。接続スコープなので戻す
                await conn.execute(text("PRAGMA query_only = ON"))
            elif dialect == "postgresql":
                timeout_ms = settings.readonly_statement_timeout_seconds * 1000
                if timeout_ms > 0:
                    # is_local=true ＝ SET LOCAL。このトランザクションの中だけで効く
                    await conn.execute(
                        text("SELECT set_config('statement_timeout', :v, true)"),
                        {"v": str(timeout_ms)},
                    )
            else:
                await conn.execute(text("SET TRANSACTION READ ONLY"))
            yield conn
        finally:
            try:
                await conn.rollback()
                await _release_session_state(conn, dialect)
            except BaseException as e:
                # 状態を外せない接続はプールへ返さない（advisory lock を握ったまま使い回さない）。
                # **`CancelledError` も拾う**（`Exception` ではない）。後始末の途中で取り消されると
                # unlock が走らないまま接続がプールへ戻るため。破棄したうえで取り消しは伝える
                logger.exception("読み取り専用接続の後始末に失敗したため接続を破棄します")
                await conn.invalidate()
                if not isinstance(e, Exception):
                    raise


@asynccontextmanager
async def readonly_program_sql(
    db: AsyncSession, *, allowed_tables: Iterable[str] | None = None
) -> AsyncIterator[AsyncSession | AsyncConnection]:
    """DSL 由来の SQL を読み取り専用で発行する相手（`execute` を持つ）を貸し出す。

    呼び出し側は yield された相手に `execute` する。`db` そのものとは限らない。
    SQLite では実行の間、アプリ自身の表（`denied_tables`）を読む文を DB が拒否する。
    `allowed_tables` を渡すと、読める表がその表だけになる。PostgreSQL の表の検査は
    実行前に `assert_relations_permitted` を呼ぶ側が行う（文を知っているのは呼び出し側だけ）。

    Raises:
        ReadOnlySessionUnavailableError: 非 SQLite・READONLY_DATABASE_URL 未設定・production
    """
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(text("PRAGMA query_only = ON"))
        try:
            await _set_sqlite_authorizer(db, _sqlite_authorizer(_normalize_allowed(allowed_tables)))
            yield db
        finally:
            # authorizer を先に外す（PRAGMA 自体を拒否する規則なので、外さないと戻せない）
            try:
                await _set_sqlite_authorizer(db, None)
            finally:
                await db.execute(text("PRAGMA query_only = OFF"))
        return

    engine = get_readonly_engine()
    if engine is None:
        if _refuses_without_dsn():
            raise ReadOnlySessionUnavailableError(UNCONFIGURED_PRODUCTION_WARNING)
        engine = _app_engine(db)
    async with readonly_transaction(engine) as conn:
        yield conn
