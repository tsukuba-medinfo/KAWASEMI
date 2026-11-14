"""ドライバの例外の要約（PostgreSQL 実機）

`KAWASEMI_TEST_POSTGRES_URL` が無ければ skip する。CI の `migrations-postgres` ジョブが
`test_external_db_postgres.py` と同じ起動で回す。このテストは**自分の一時テーブルだけ**を作って消す。

実機の asyncpg・PostgreSQL は、`[parameters: ...]` の外にも行・束縛の値を例外の文面に載せる
（一意制約違反の `DETAIL: キー (col)=(value)`、型変換エラーの `"value"`、`Token "x" is invalid`、
`malformed array literal: "x"`、列挙型・範囲型・tsquery …）。文面の形は数百種あり、正規表現で伏せる方式は
取りこぼす。evidence・trace・DB へ出す文面は、構造から取れるもの（型名・SQLSTATE・制約名や列名）
だけにする。ここでは、実機のさまざまな失敗で、**値が一切出ない**ことと、原因の切り分けに要る
SQLSTATE が出ることを見る。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

import kawasemi.ai_input.db_retry as db_retry
import kawasemi.ai_input.executor as executor_mod
from kawasemi.ai_input.executor import DSLInterpreter, _exc_msg
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry
from kawasemi.core.exception_summary import summarize_exception

PG_URL = os.environ.get("KAWASEMI_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not PG_URL, reason="KAWASEMI_TEST_POSTGRES_URL が未設定")

TABLE = f"exc_probe_{uuid.uuid4().hex[:8]}"
NEEDLE = "PHI-NEEDLE-9137"
BASE = {"op": "external_db_extract", "id": "v"}


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    # hide_parameters を付けない（付け忘れた経路でも、要約が値を出さないことを見る）
    eng = create_async_engine(PG_URL)
    async with eng.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        await conn.execute(text(f"DROP TYPE IF EXISTS {TABLE}_mood"))
        await conn.execute(text(f"CREATE TYPE {TABLE}_mood AS ENUM ('ok', 'sad')"))
        await conn.execute(
            text(
                f"CREATE TABLE {TABLE} (email text UNIQUE, n int NOT NULL, v text, pid text, "
                f"mood {TABLE}_mood, CONSTRAINT {TABLE}_chk CHECK (n < 1000))"
            )
        )
        await conn.execute(
            text(f"INSERT INTO {TABLE} (email, n, v, pid) VALUES (:e, 1, :v, 'REG-1')"),
            {"e": f"{NEEDLE}@example.org", "v": NEEDLE},
        )
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
            await conn.execute(text(f"DROP TYPE IF EXISTS {TABLE}_mood"))
        await eng.dispose()


async def _error(
    engine: AsyncEngine, sql: str, params: dict[str, object] | None = None
) -> Exception:
    try:
        async with engine.begin() as conn:
            await conn.execute(text(sql), params or {})
    except Exception as exc:  # noqa: BLE001 - 例外の文面そのものを見るテスト
        return exc
    raise AssertionError("例外が起きなかった")


# (名前, SQL, 束縛, 期待する SQLSTATE, 要約に残るはずの名前)
def _cases() -> list[tuple[str, str, dict[str, object], str, list[str]]]:
    t = TABLE
    needle_email = f"{NEEDLE}@example.org"
    return [
        (
            "unique",
            f"INSERT INTO {t} (email, n) VALUES (:a, 2)",
            {"a": needle_email},
            "23505",
            [f"constraint={t}_email_key", f"table={t}"],
        ),
        (
            "notnull",
            f"INSERT INTO {t} (email, n, v) VALUES (:e, NULL, :v)",
            {"e": "z@example.org", "v": NEEDLE},
            "23502",
            ["column=n"],
        ),
        (
            "check",
            f"INSERT INTO {t} (email, n, v) VALUES (:e, 5000, :v)",
            {"e": "c@example.org", "v": NEEDLE},
            "23514",
            [f"constraint={t}_chk"],
        ),
        ("int_cast", f"SELECT CAST(v AS int) FROM {t} WHERE pid = 'REG-1'", {}, "22P02", []),
        (
            "bind_type",
            f"SELECT * FROM {t} WHERE n = :a",
            {"a": NEEDLE},
            "",  # asyncpg 側で検出（SQLSTATE が付かないことがある）
            [],
        ),
        ("array_cast", f"SELECT CAST(v AS int[]) FROM {t} WHERE pid = 'REG-1'", {}, "22P02", []),
        ("jsonb_cast", f"SELECT CAST(v AS jsonb) FROM {t} WHERE pid = 'REG-1'", {}, "22P02", []),
        (
            "range_cast",
            f"SELECT CAST(v AS int4range) FROM {t} WHERE pid = 'REG-1'",
            {},
            "22P02",
            [],
        ),
        ("regclass_cast", f"SELECT CAST(v AS regclass) FROM {t} WHERE pid = 'REG-1'", {}, "", []),
        (
            "enum",
            f"INSERT INTO {t} (email, n, mood) VALUES (:e, 1, :m)",
            {"e": "m@example.org", "m": NEEDLE},
            "22P02",
            [],
        ),
        (
            "tsquery",
            f"SELECT to_tsquery('simple', v || ' &') FROM {t} WHERE pid = 'REG-1'",
            {},
            "",
            [],
        ),
        (
            "newline_value",
            f"INSERT INTO {t} (email, n) VALUES (:a, 2)",
            {"a": f"{NEEDLE}@example.org"},
            "23505",
            [],
        ),
        (
            "undefined_table",
            f"SELECT {NEEDLE.replace('-', '_')} FROM nonexistent_t",
            {},
            "42P01",
            [],
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [c[0] for c in _cases()])
async def test_real_driver_failures_show_no_values_but_keep_the_code(
    engine: AsyncEngine, case: str
) -> None:
    name, sql, params, state, kept = next(c for c in _cases() if c[0] == case)
    exc = await _error(engine, sql, params)

    shown = summarize_exception(exc)
    assert NEEDLE not in shown, (name, shown)
    assert NEEDLE not in _exc_msg(exc)
    if state:
        assert f"sqlstate={state}" in shown, (name, shown)
    for token in kept:
        assert token in shown, (name, token, shown)


@pytest.mark.asyncio
async def test_unique_violation_raw_text_has_the_value_so_the_summary_matters(
    engine: AsyncEngine,
) -> None:
    """前提の確認: 生の文面には値が入る（要約が意味を持つ理由）。値の入らないドライバなら、このテストを見直す。"""
    exc = await _error(
        engine,
        f"INSERT INTO {TABLE} (email, n) VALUES (:a, 2)",
        {"a": f"{NEEDLE}@example.org"},
    )
    assert NEEDLE in str(exc)
    assert NEEDLE not in summarize_exception(exc)


@pytest_asyncio.fixture
async def interp(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[DSLInterpreter]:
    registry = ConnectionRegistry({"c": {"kind": "db", "scope": "external", "secret_ref": PG_URL}})
    monkeypatch.setattr(executor_mod, "get_registry", lambda: registry)
    monkeypatch.setattr(executor_mod, "is_select_only_sql", lambda sql: True)
    monkeypatch.setattr(db_retry, "RETRY_WAIT_SECONDS", 0.0)
    it = DSLInterpreter()
    try:
        yield it
    finally:
        await it.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expr",
    [
        "CAST(v AS int)",
        "CAST(v AS int[])",
        "CAST(v AS jsonb)",
        "CAST(v AS int4range)",
        "CAST(v AS regclass)",
        "to_tsquery('simple', v || ' &')",
    ],
)
async def test_external_db_trace_error_does_not_carry_row_values(
    engine: AsyncEngine, interp: DSLInterpreter, expr: str
) -> None:
    """外部 DB の型変換エラーは、行の値（束縛ではない）を載せる。trace の error には出さない。"""
    sql = f"SELECT {expr} FROM {TABLE} WHERE pid = :patient_id"
    output, trace = await interp._op_external_db(
        {**BASE, "connection_id": "c", "sql": sql, "multi": True}, "REG-1", dict(BASE)
    )
    assert output is None
    error = str(trace["error"])
    assert NEEDLE not in error, error
    assert "sqlstate=" in error or "Error" in error
