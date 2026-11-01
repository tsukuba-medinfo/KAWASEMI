"""DSL が書いた SQL をアプリDBへ**読み取り専用で**発行する経路。

`sql_extract`（`_op_sql`）と統計プロファイルの自アプリDB集計は、LLM や索引が組んだ SQL を
アプリDBへ発行する。文字列検査（`is_select_only_sql`）は多層防御の1枚にすぎず、
**SELECT から到達できる書き込み**（`WITH x AS (INSERT ... RETURNING 1) SELECT ...`、
書き込みをする関数の呼び出しなど）を取りこぼしうる。保証は DB 側に置く。

方言ごとの実現:

- **SQLite**: 呼び出し側のセッションに `PRAGMA query_only = ON` を掛け、抜けるときに戻す。
  別コネクションを張らないので、テストの `:memory:` DB でも同じ関門を通る（従来どおり）
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

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
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


def _refuses_without_dsn() -> bool:
    return settings.environment == "production"


def get_readonly_engine() -> AsyncEngine | None:
    """`READONLY_DATABASE_URL` のエンジン。未設定なら None。初回に生成してキャッシュする。"""
    url = settings.readonly_database_url
    if not url:
        return None
    engine = _readonly_engines.get(url)
    if engine is None:
        engine = create_async_engine(url, pool_pre_ping=True)
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
async def _readonly_transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
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
            except Exception:
                # 状態を外せない接続はプールへ返さない（advisory lock を握ったまま使い回さない）
                logger.exception("読み取り専用接続の後始末に失敗したため接続を破棄します")
                await conn.invalidate()


@asynccontextmanager
async def readonly_program_sql(db: AsyncSession) -> AsyncIterator[AsyncSession | AsyncConnection]:
    """DSL 由来の SQL を読み取り専用で発行する相手（`execute` を持つ）を貸し出す。

    呼び出し側は yield された相手に `execute` する。`db` そのものとは限らない。

    Raises:
        ReadOnlySessionUnavailableError: 非 SQLite・READONLY_DATABASE_URL 未設定・production
    """
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(text("PRAGMA query_only = ON"))
        try:
            yield db
        finally:
            await db.execute(text("PRAGMA query_only = OFF"))
        return

    engine = get_readonly_engine()
    if engine is None:
        if _refuses_without_dsn():
            raise ReadOnlySessionUnavailableError(UNCONFIGURED_PRODUCTION_WARNING)
        engine = _app_engine(db)
    async with _readonly_transaction(engine) as conn:
        yield conn
