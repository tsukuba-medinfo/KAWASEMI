"""ドライバの例外文面の伏せ処理（PostgreSQL 実機）

`KAWASEMI_TEST_POSTGRES_URL` が無ければ skip する。CI の `migrations-postgres` ジョブが
`test_external_db_postgres.py` と同じ起動で回す。このテストは**自分の一時テーブルだけ**を作って消す。

実機の asyncpg・PostgreSQL は、`[parameters: ...]` の外にも行・束縛の値を例外の文面に載せる
（一意制約違反の `DETAIL: キー (col)=(value)`、型変換エラーの `"value"`、束縛の型違反の
`invalid input for query argument $1: 'value'`、NOT NULL 違反の `失敗した行は(...)`）。
`hide_parameters=True` では隠れないので、evidence へ出す `_exc_msg` が値を伏せることを見る。
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

PG_URL = os.environ.get("KAWASEMI_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not PG_URL, reason="KAWASEMI_TEST_POSTGRES_URL が未設定")

TABLE = f"exc_probe_{uuid.uuid4().hex[:8]}"
BASE = {"op": "external_db_extract", "id": "v"}


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    # hide_parameters を付けない（付け忘れた経路でも、フィルタ・サニタイザが伏せることを見る）
    eng = create_async_engine(PG_URL)
    async with eng.begin() as conn:
        await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        await conn.execute(
            text(f"CREATE TABLE {TABLE} (email text UNIQUE, n int NOT NULL, v text, pid text)")
        )
        await conn.execute(
            text(
                f"INSERT INTO {TABLE} VALUES "
                "('phi-victim@example.org', 1, 'PHI-DIAGNOSIS-XYZ', 'REG-1')"
            )
        )
    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "secrets", "kept"),
    [
        ("unique", ["phi-victim@example.org"], ["(email)", "UniqueViolation"]),
        ("notnull", ["z@example.org", "PHI-ROW-VALUE"], ["NotNullViolation"]),
        ("syntax", ["PHI-DIAGNOSIS-XYZ"], ["InvalidTextRepresentation"]),
        ("arg", ["PHI-PATIENT-NAME-TARO"], ["invalid input for query argument $1"]),
    ],
)
async def test_real_driver_messages_are_masked_in_evidence_text(
    engine: AsyncEngine, case: str, secrets: list[str], kept: list[str]
) -> None:
    sqls: dict[str, tuple[str, dict[str, object]]] = {
        "unique": (
            f"INSERT INTO {TABLE} VALUES (:a, 2, 'y', 'p')",
            {"a": "phi-victim@example.org"},
        ),
        "notnull": (
            f"INSERT INTO {TABLE} VALUES (:e, NULL, :v, 'p')",
            {"e": "z@example.org", "v": "PHI-ROW-VALUE"},
        ),
        "syntax": (f"SELECT CAST(v AS int) FROM {TABLE} WHERE pid = :a", {"a": "REG-1"}),
        "arg": (f"SELECT * FROM {TABLE} WHERE n = :a", {"a": "PHI-PATIENT-NAME-TARO"}),
    }
    sql, params = sqls[case]
    exc = await _error(engine, sql, params)

    raw = str(exc)
    # 前提: 伏せる前の実機の文面には、値が入っている（このテストが意味を持つ理由）
    for secret in secrets:
        assert secret in raw, f"前提が崩れた（ドライバが値を載せなくなった）: {secret!r}"

    shown = _exc_msg(exc)
    for secret in secrets:
        assert secret not in shown, (secret, shown)
    for token in kept:
        assert token in shown, (token, shown)


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
async def test_external_db_trace_error_does_not_carry_row_values(
    engine: AsyncEngine, interp: DSLInterpreter
) -> None:
    """外部 DB の型変換エラーは行の値（`"PHI-DIAGNOSIS-XYZ"`）を載せる。trace の error は伏せる。"""
    sql = f"SELECT CAST(v AS int) FROM {TABLE} WHERE pid = :patient_id"
    output, trace = await interp._op_external_db(
        {**BASE, "connection_id": "c", "sql": sql, "multi": True}, "REG-1", dict(BASE)
    )
    assert output is None
    error = str(trace["error"])
    assert "PHI-DIAGNOSIS-XYZ" not in error, error
    assert "InvalidTextRepresentation" in error or "入力構文" in error or "invalid input" in error
