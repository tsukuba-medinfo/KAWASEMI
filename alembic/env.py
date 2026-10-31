"""Alembic マイグレーション環境設定（非同期対応）"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Alembic Config オブジェクト
config = context.config

# ロギング設定
if config.config_file_name is not None:
    # アプリやテストが先に作ったロガーを無効化しない（既定の True だと、プロセス内で
    # 移行を走らせたあとにそれらのログが消える）
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# モデルのメタデータをインポート。
# fileConfig によるロギング設定の後に import する（Alembic テンプレートの構造）。
#
# kawasemi.ai_input.models は kawasemi/models/ パッケージの外にあり、kawasemi.models
# の __init__ からは import できない（そちらは kawasemi.models.base 経由で
# ai_input.models から参照される側であり、逆方向の import は循環importになる）。
# Base は共有しているため、ここで import するだけで target_metadata に含まれる
# ようになる。これを import し忘れると、alembic は ai_input 由来の
# 全テーブルを「モデルに存在しない＝削除対象」と誤検出する。
from kawasemi.ai_input import models as ai_input_models  # noqa: E402, F401
from kawasemi.core.config import settings  # noqa: E402
from kawasemi.models import Base  # noqa: E402

target_metadata = Base.metadata

# データベースURLを設定から取得
config.set_main_option("sqlalchemy.url", str(settings.database_url))


def run_migrations_offline() -> None:
    """
    オフラインモードでマイグレーションを実行

    DBへの接続なしでSQLを生成する
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """マイグレーションの実行"""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """非同期でマイグレーションを実行"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """オンラインモードでマイグレーションを実行"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
