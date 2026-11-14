"""external_db_extract の読み取り専用・文タイムアウト・接続状態の復元（PostgreSQL 実機）

`KAWASEMI_TEST_POSTGRES_URL` が無ければ skip する。CI の `migrations-postgres` ジョブが、
`test_program_sql_readonly_postgres.py` と同じ起動で回す。このテストは**自分の一時テーブル・
シーケンスだけ**を作って消す（全テーブルを drop しない）。

見ること（SELECT 限定の文字列検査は素通しにして、**データベース側の防御だけ**を見る）:

- 書き込み（INSERT・CTE 内の書き込み・シーケンスを進める関数）が読み取り専用トランザクションで止まる
- 文タイムアウトが `pg_sleep` を取り消し、**取り消しは投げ直さない**（`attempts == 1`）
- 抜けたあとの接続（プールへ戻る）に、advisory lock・セッションの設定・読み取り専用が残らない
"""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import kawasemi.ai_input.db_retry as db_retry
import kawasemi.ai_input.executor as executor_mod
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry
from kawasemi.core.config import settings

PG_URL = os.environ.get("KAWASEMI_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not PG_URL, reason="KAWASEMI_TEST_POSTGRES_URL が未設定")

_SUFFIX = uuid.uuid4().hex[:8]
TABLE = f"ext_probe_{_SUFFIX}"
SEQ = f"ext_probe_seq_{_SUFFIX}"
BASE = {"op": "external_db_extract", "id": "v"}


@pytest_asyncio.fixture
async def admin() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        await conn.execute(text(f"DROP SEQUENCE IF EXISTS {SEQ}"))
        await conn.execute(text(f"CREATE TABLE {TABLE} (pid text, val text)"))
        await conn.execute(text(f"INSERT INTO {TABLE} VALUES ('REG-1', 'x')"))
        await conn.execute(text(f"CREATE SEQUENCE {SEQ}"))
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
            await conn.execute(text(f"DROP SEQUENCE IF EXISTS {SEQ}"))
        await engine.dispose()


@pytest_asyncio.fixture
async def interp(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DSLInterpreter]:
    registry = ConnectionRegistry({"c": {"kind": "db", "scope": "external", "secret_ref": PG_URL}})
    monkeypatch.setattr(executor_mod, "get_registry", lambda: registry)
    # 文字列検査を素通しにして、データベース側の防御だけを見る
    monkeypatch.setattr(executor_mod, "is_select_only_sql", lambda sql: True)
    monkeypatch.setattr(db_retry, "RETRY_WAIT_SECONDS", 0.0)
    it = DSLInterpreter()
    try:
        yield it
    finally:
        await it.aclose()


async def _run(it: DSLInterpreter, sql: str) -> tuple[object, dict]:
    return await it._op_external_db(
        {**BASE, "connection_id": "c", "sql": sql, "multi": True}, "REG-1", dict(BASE)
    )


async def _scalar(engine: AsyncEngine, sql: str) -> object:
    async with engine.connect() as conn:
        return (await conn.execute(text(sql))).scalar()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "template",
    [
        "INSERT INTO {t} VALUES ('REG-2', 'y')",
        "WITH w AS (INSERT INTO {t} VALUES ('REG-3', 'z') RETURNING 1) SELECT * FROM w",
        "UPDATE {t} SET val = 'changed'",
        "SELECT nextval('{s}')",
    ],
)
async def test_writes_are_refused_by_the_readonly_transaction(
    admin: AsyncEngine, interp: DSLInterpreter, template: str
) -> None:
    output, trace = await _run(interp, template.format(t=TABLE, s=SEQ))
    assert output is None and trace["error"]
    # 読み取り専用の違反は投げ直さない
    assert trace["attempts"] == 1
    assert await _scalar(admin, f"SELECT count(*) FROM {TABLE}") == 1
    assert await _scalar(admin, f"SELECT val FROM {TABLE}") == "x"
    # nextval が通っていれば is_called が true になる
    assert await _scalar(admin, f"SELECT is_called FROM {SEQ}") is False


@pytest.mark.asyncio
async def test_statement_timeout_cancels_and_is_not_retried(
    admin: AsyncEngine, interp: DSLInterpreter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "readonly_statement_timeout_seconds", 1)
    started = time.monotonic()
    output, trace = await _run(interp, "SELECT pg_sleep(8)")
    elapsed = time.monotonic() - started
    assert output is None and trace["error"]
    assert trace["attempts"] == 1
    # 取り消されたので 8 秒待たず、3 回投げ直した（約 3 秒以上）わけでもない
    assert elapsed < 6, elapsed
    # 取り消された接続が、サーバ側に残っていない
    assert (
        await _scalar(
            admin,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE query LIKE '%pg_sleep(8)%' AND pid <> pg_backend_pid()",
        )
        == 0
    )


@pytest.mark.asyncio
async def test_pooled_connection_state_is_restored(
    admin: AsyncEngine, interp: DSLInterpreter
) -> None:
    # セッションに残る状態を作る文（advisory lock・セッションの設定）。検査を素通しにしている
    await _run(interp, "SELECT pg_advisory_lock(424242)")
    await _run(interp, "SELECT set_config('search_path', 'pg_temp', false)")
    await _run(interp, "SELECT set_config('application_name', 'leaked', false)")

    assert await _scalar(admin, "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") == 0, (
        "advisory lock がプールへ戻る接続に残った"
    )

    # 同じエンジン（プール）で次の抽出を行い、前の文の設定を引き継いでいないことを見る
    engine = interp._external_engine(PG_URL)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SHOW application_name"))).scalar() != "leaked"
        assert "pg_temp" not in (await conn.execute(text("SHOW search_path"))).scalar()
        # 読み取り専用も残っていない（通常の接続として書ける）
        await conn.execute(text(f"INSERT INTO {TABLE} VALUES ('REG-9', 'w')"))
        await conn.rollback()

    # 通常の SELECT は従来どおり取れる
    output, trace = await _run(interp, f"SELECT val FROM {TABLE} WHERE pid = :patient_id")
    assert output == ["x"] and trace["attempts"] == 1
