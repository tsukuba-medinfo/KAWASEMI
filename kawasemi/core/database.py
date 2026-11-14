"""データベース接続管理"""

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kawasemi.core.config import settings

_is_sqlite = str(settings.database_url).startswith("sqlite")

# 非同期エンジンの作成。pool_size/max_overflow は QueuePool 前提で、SQLite(aiosqlite)では
# 無効なため非SQLiteでのみ渡す。本番は Postgres を推奨（DEPLOYMENT 参照）。
_engine_kwargs: dict[str, Any] = {
    "echo": settings.debug,  # デバッグモードでSQLをログ出力
    "pool_pre_ping": True,  # 接続の健全性チェック
}
if not _is_sqlite:
    _engine_kwargs["pool_size"] = 5  # 接続プールサイズ
    _engine_kwargs["max_overflow"] = 10  # プールを超える最大接続数

engine = create_async_engine(str(settings.database_url), **_engine_kwargs)


def set_sqlite_pragmas(dbapi_conn: Any, _rec: Any = None) -> None:
    """SQLite 接続ごとの PRAGMA。本番エンジンのリスナーで、テストからも直接使う。

    WAL でリーダとライタの競合を減らし、busy_timeout でロック待ちを許容して
    "database is locked" を緩和する。外部キーも強制する: SQLite は既定で強制しないので、
    そのままだと ON DELETE CASCADE / SET NULL が効かず、ユーザーを削除してもセッション・
    担当・リースの行が宙に浮く。（マイグレーションは別の接続で、この設定を受けない）
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


if _is_sqlite:
    # aiosqlite は sync_engine 経由で connect イベントを拾える。
    event.listen(engine.sync_engine, "connect", set_sqlite_pragmas)


# 非同期セッションファクトリ
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    データベースセッションの依存性注入用ジェネレータ

    FastAPIのDependsで使用する

    トランザクション方針（意図的に commit-on-success を採用）:
        - 正常終了時に commit、例外時に rollback、最後に必ず close する標準イディオム。
        - 読み取り専用ハンドラでも末尾 commit が走るが、autoflush=False かつ
          リクエストスコープ状態（User.active_role 等）は非マップ属性で flush 対象に
          ならないため、意図しない書き込みは発生しない。
        - 書き込みハンドラは可読性のため明示 commit していることが多いが、その後の
          二重 commit は no-op で無害。
        この前提が崩れる変更（暗黙のダーティ状態を持ち込む等）を入れる場合は、
        ハンドラ側 commit + get_db は rollback/close のみ、へ方針転換すること。

    Yields:
        AsyncSession: 非同期データベースセッション
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


def _alembic_config() -> Any:
    """リポジトリ直下の alembic.ini を指す Alembic 設定を返す。"""
    from alembic.config import Config

    root = Path(__file__).resolve().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    return cfg


async def init_db() -> None:
    """
    データベースの初期化

    `alembic upgrade head` と同じことをする。スキーマの正本はマイグレーションであり、
    `Base.metadata.create_all` は使わない。create_all は import 済みのモデルしか
    作らないため、以前は kawasemi.ai_input.models の表が欠けて /ai-input の画面が
    500 になった。また alembic_version も記録されず、後から upgrade できなかった。

    env.py が asyncio.run を呼ぶので、実行中のイベントループとは別スレッドで動かす。
    """
    from alembic import command

    await asyncio.to_thread(command.upgrade, _alembic_config(), "head")


async def drop_db() -> None:
    """
    データベースの削除

    実 DB にある全テーブルを反映して削除する（デモの初期化・テスト用）。
    alembic_version も消えるので、続く init_db() はマイグレーションを最初から当てる。
    """

    def _drop_all(sync_conn: Any) -> None:
        meta = MetaData()
        meta.reflect(bind=sync_conn)
        meta.drop_all(bind=sync_conn)

    async with engine.begin() as conn:
        await conn.run_sync(_drop_all)
