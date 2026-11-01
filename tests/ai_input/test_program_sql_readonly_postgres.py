"""プログラム SQL の読み取り専用実行（PostgreSQL 実機）

`KAWASEMI_TEST_POSTGRES_URL`（例 `postgresql+asyncpg://kawasemi:kawasemi@localhost:5432/kawasemi`）
が無ければ skip する。CI では `migrations-postgres` ジョブが設定して回す。

見ること:

- 文字列検査（`is_select_only_sql`）を通らない形を**直接** reader へ投げても、
  読み取り専用トランザクションが書き込みを止めること（CTE 内の INSERT / UPDATE、
  書き込みをする VOLATILE 関数、トランザクションの読み書きモードへの切り戻し）
- 文字列検査を**通る**形（関数呼び出しの SELECT）を `sql_extract` で流しても書けないこと
- READONLY_DATABASE_URL を設定すると、その DSN のロールで読むこと
- production で READONLY_DATABASE_URL が無ければ実行しないこと
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kawasemi.ai_input.dsl import is_select_only_sql
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.models import InputPlan
from kawasemi.core import readonly_db
from kawasemi.core.config import settings
from kawasemi.core.readonly_db import readonly_program_sql

PG_URL = os.environ.get("KAWASEMI_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(not PG_URL, reason="KAWASEMI_TEST_POSTGRES_URL が未設定")

# 25006 = read_only_sql_transaction。サーバのロケールに依らず SQLSTATE で見る。
READ_ONLY_SQLSTATE = "25006"

_SUFFIX = uuid.uuid4().hex[:8]
TABLE = f"ro_probe_{_SUFFIX}"
FUNC = f"ro_probe_write_{_SUFFIX}"
RO_ROLE = f"kawasemi_ro_{_SUFFIX}"
RO_PASSWORD = "ro-test-password"


def _sqlstate(exc: BaseException) -> str | None:
    orig = getattr(exc, "orig", None)
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


@pytest.fixture
async def pg_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.execute(text(f"CREATE TABLE {TABLE} (patient_id TEXT, v TEXT)"))
        await conn.execute(text(f"INSERT INTO {TABLE} VALUES ('P1', 'original')"))
        await conn.execute(
            text(
                f"CREATE FUNCTION {FUNC}() RETURNS integer LANGUAGE sql VOLATILE AS "
                f"$$ INSERT INTO {TABLE} VALUES ('P9', 'smuggled') RETURNING 1 $$"
            )
        )
    try:
        yield engine
    finally:
        await readonly_db.dispose_readonly_engine()
        async with engine.begin() as conn:
            await conn.execute(text(f"DROP FUNCTION IF EXISTS {FUNC}()"))
            await conn.execute(text(f"DROP TABLE IF EXISTS {TABLE}"))
        await engine.dispose()


@pytest.fixture
async def app_session(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncSession]:
    monkeypatch.setattr(settings, "readonly_database_url", "")
    monkeypatch.setattr(settings, "environment", "development")
    async with async_sessionmaker(pg_engine, expire_on_commit=False)() as session:
        yield session


async def _rows(engine: AsyncEngine) -> list[tuple[str, str]]:
    async with engine.connect() as conn:
        result = await conn.execute(text(f"SELECT patient_id, v FROM {TABLE} ORDER BY 1"))
        return [(r[0], r[1]) for r in result.fetchall()]


SMUGGLED = [
    f"WITH x AS (INSERT INTO {TABLE} VALUES ('P2', 'cte') RETURNING 1) SELECT * FROM x",
    f"WITH x AS (UPDATE {TABLE} SET v = 'cte' RETURNING 1) SELECT * FROM x",
    f"SELECT {FUNC}()",
]
# 名前に乱数の接尾辞が入るので、id は固定する（xdist の worker 間で収集を一致させる）
SMUGGLED_IDS = ["cte_insert", "cte_update", "volatile_function"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", SMUGGLED, ids=SMUGGLED_IDS)
async def test_smuggled_writes_fail_in_the_readonly_transaction(
    app_session: AsyncSession, pg_engine: AsyncEngine, sql: str
) -> None:
    """READONLY_DATABASE_URL 無し（production 以外。アプリのロール）でも、
    書き込みは 25006 で止まること。"""
    with pytest.raises(DBAPIError) as exc:
        async with readonly_program_sql(app_session) as reader:
            assert reader is not app_session
            await reader.execute(text(sql))
    assert _sqlstate(exc.value) == READ_ONLY_SQLSTATE, exc.value
    assert await _rows(pg_engine) == [("P1", "original")]


@pytest.mark.asyncio
async def test_the_transaction_cannot_be_switched_back_to_read_write(
    app_session: AsyncSession, pg_engine: AsyncEngine
) -> None:
    """`set_config('transaction_read_only', 'off')` で読み書きへ戻して書く手は通らないこと。"""
    with pytest.raises(DBAPIError):
        async with readonly_program_sql(app_session) as reader:
            assert (await reader.execute(text("SHOW transaction_read_only"))).scalar() == "on"
            await reader.execute(text("SELECT set_config('transaction_read_only', 'off', true)"))
            await reader.execute(text(f"SELECT {FUNC}()"))
    assert await _rows(pg_engine) == [("P1", "original")]


@pytest.mark.asyncio
async def test_the_app_session_can_still_write_after_the_guard(
    app_session: AsyncSession, pg_engine: AsyncEngine
) -> None:
    """関門は別接続なので、アプリのセッション自身のトランザクションは読み書きのままであること。"""
    await app_session.execute(text(f"INSERT INTO {TABLE} VALUES ('P3', 'before')"))
    async with readonly_program_sql(app_session) as reader:
        assert (await reader.execute(text("SHOW transaction_read_only"))).scalar() == "on"
    await app_session.execute(text(f"INSERT INTO {TABLE} VALUES ('P4', 'after')"))
    await app_session.commit()
    assert [r[0] for r in await _rows(pg_engine)] == ["P1", "P3", "P4"]


def _plan(sql: str) -> InputPlan:
    program = {"steps": [{"op": "sql_extract", "id": "s", "sql": sql}], "return": "$s"}
    return InputPlan(questionnaire_id=1, link_id="q", program=program, status="confirmed")


@pytest.mark.asyncio
async def test_sql_extract_with_a_writing_function_does_not_write(
    app_session: AsyncSession, pg_engine: AsyncEngine
) -> None:
    """文字列検査を通る形（関数呼び出し）を `sql_extract` で流しても書けないこと。"""
    sql = f"SELECT {FUNC}() WHERE :patient_id = :patient_id"
    assert is_select_only_sql(sql), "前提: この形は文字列検査を通る"

    prefill, evidence = await DSLInterpreter().execute([_plan(sql)], "P1", app_session)

    assert "q" not in prefill
    assert evidence["q"]["trace"][0].get("error"), evidence
    assert await _rows(pg_engine) == [("P1", "original")]


@pytest.fixture
async def readonly_role(
    pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    """DEPLOYMENT.md の手順どおりの読み取り専用ロールを作り、その DSN を設定する。"""
    url = make_url(PG_URL)
    try:
        async with pg_engine.begin() as conn:
            await conn.execute(text(f"CREATE ROLE {RO_ROLE} LOGIN PASSWORD '{RO_PASSWORD}'"))
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {url.database} TO {RO_ROLE}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {RO_ROLE}"))
            await conn.execute(text(f"GRANT SELECT ON {TABLE} TO {RO_ROLE}"))
    except DBAPIError as e:  # pragma: no cover - CREATEROLE の無い環境
        pytest.skip(f"読み取り専用ロールを作れない: {e}")
    ro_url = url.set(username=RO_ROLE, password=RO_PASSWORD)
    monkeypatch.setattr(
        settings, "readonly_database_url", ro_url.render_as_string(hide_password=False)
    )
    try:
        yield RO_ROLE
    finally:
        await readonly_db.dispose_readonly_engine()
        async with pg_engine.begin() as conn:
            await conn.execute(text(f"REVOKE ALL ON {TABLE} FROM {RO_ROLE}"))
            await conn.execute(text(f"REVOKE USAGE ON SCHEMA public FROM {RO_ROLE}"))
            await conn.execute(text(f"REVOKE CONNECT ON DATABASE {url.database} FROM {RO_ROLE}"))
            await conn.execute(text(f"DROP ROLE {RO_ROLE}"))


@pytest.mark.asyncio
async def test_the_readonly_dsn_role_is_used(
    app_session: AsyncSession, pg_engine: AsyncEngine, readonly_role: str
) -> None:
    """READONLY_DATABASE_URL のロールで、読み取り専用トランザクションの中で読むこと。"""
    async with readonly_program_sql(app_session) as reader:
        assert (await reader.execute(text("SELECT current_user"))).scalar() == readonly_role
        assert (await reader.execute(text("SHOW transaction_read_only"))).scalar() == "on"

    prefill, _ = await DSLInterpreter().execute(
        [_plan(f"SELECT v FROM {TABLE} WHERE patient_id = :patient_id")], "P1", app_session
    )
    assert prefill["q"] == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", SMUGGLED, ids=SMUGGLED_IDS)
async def test_smuggled_writes_fail_on_the_readonly_dsn_route(
    app_session: AsyncSession, pg_engine: AsyncEngine, readonly_role: str, sql: str
) -> None:
    """読み取り専用ロールの経路でも、書き込みは止まること（トランザクションとロールの二重）。"""
    with pytest.raises(DBAPIError):
        async with readonly_program_sql(app_session) as reader:
            await reader.execute(text(sql))
    assert await _rows(pg_engine) == [("P1", "original")]


@pytest.mark.asyncio
async def test_production_without_readonly_dsn_does_not_run(
    app_session: AsyncSession, pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """production で DSN が無ければ、`sql_extract` は実行されず source_unavailable になること。"""
    from kawasemi.ai_input.models import FailureCode

    monkeypatch.setattr(settings, "environment", "production")
    prefill, evidence = await DSLInterpreter().execute(
        [_plan(f"SELECT v FROM {TABLE} WHERE patient_id = :patient_id")], "P1", app_session
    )

    assert "q" not in prefill
    assert evidence["q"]["trace"][0]["failure_code"] == FailureCode.SOURCE_UNAVAILABLE


@pytest.mark.asyncio
async def test_production_with_readonly_dsn_reads(
    app_session: AsyncSession, readonly_role: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """production でも DSN があれば読み取り専用ロールで読めること。"""
    monkeypatch.setattr(settings, "environment", "production")
    async with readonly_program_sql(app_session) as reader:
        assert (await reader.execute(text("SELECT current_user"))).scalar() == readonly_role
    prefill, _ = await DSLInterpreter().execute(
        [_plan(f"SELECT v FROM {TABLE} WHERE patient_id = :patient_id")], "P1", app_session
    )
    assert prefill["q"] == "original"


# --- statement_timeout と advisory lock の後始末 -----------------------------
#
# 文字列検査は `pg_sleep` / `pg_advisory_lock` を拒否するが、ここでは関門の側の保証を見るため
# reader へ直接投げる。


@pytest.mark.asyncio
async def test_statement_timeout_cancels_long_statements(
    app_session: AsyncSession, pg_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`READONLY_STATEMENT_TIMEOUT_SECONDS` を超える文は 57014 で止まり、設定は持ち越されないこと。"""
    monkeypatch.setattr(settings, "readonly_statement_timeout_seconds", 1)
    with pytest.raises(DBAPIError) as exc:
        async with readonly_program_sql(app_session) as reader:
            assert (await reader.execute(text("SHOW statement_timeout"))).scalar() == "1s"
            await reader.execute(text("SELECT pg_sleep(5)"))
    assert _sqlstate(exc.value) == "57014", exc.value  # query_canceled

    # プールへ返った接続に timeout が残っていないこと（SET LOCAL 相当なので外れている）
    async with pg_engine.connect() as conn:
        assert (await conn.execute(text("SHOW statement_timeout"))).scalar() == "0"


@pytest.mark.asyncio
async def test_advisory_locks_are_released_before_returning_the_connection(
    app_session: AsyncSession, pg_engine: AsyncEngine
) -> None:
    """セッションレベルの advisory lock はロールバックで外れないので、関門が外すこと。"""
    key = 0x6B617761  # 他のテストと被らない値
    async with readonly_program_sql(app_session) as reader:
        assert (await reader.execute(text(f"SELECT pg_try_advisory_lock({key})"))).scalar()

    other = create_async_engine(PG_URL)
    try:
        async with other.connect() as conn:
            got = (await conn.execute(text(f"SELECT pg_try_advisory_lock({key})"))).scalar()
            assert got is True, "関門を抜けた後も advisory lock が残っている"
            await conn.execute(text(f"SELECT pg_advisory_unlock({key})"))
    finally:
        await other.dispose()


# --- DEPLOYMENT.md の SQL をそのまま当てる -----------------------------------


@pytest.fixture
async def documented_role(pg_engine: AsyncEngine) -> AsyncIterator[AsyncEngine]:
    """DEPLOYMENT.md の「読み取り専用ロール」の SQL を、名前だけ置き換えて当てる。

    認証の表が無ければ（`alembic upgrade` していない DB）モデルから作り、後で消す。
    """
    from pathlib import Path

    from sqlalchemy import inspect as sa_inspect

    from kawasemi.ai_input import models as _ai_input_models  # noqa: F401 - 全表を登録する
    from kawasemi.models import Base

    url = make_url(PG_URL)
    role = f"kawasemi_ro_doc_{_SUFFIX}"
    doc = (Path(__file__).resolve().parents[2] / "docs" / "DEPLOYMENT.md").read_text("utf-8")
    start = doc.index("CREATE ROLE kawasemi_ro")
    block = doc[start : doc.index("```", start)]
    statements = [
        line.replace("kawasemi_ro", role)
        .replace("PASSWORD '...'", f"PASSWORD '{RO_PASSWORD}'")
        .replace("ON DATABASE kawasemi ", f"ON DATABASE {url.database} ")
        for line in block.splitlines()
        if line.strip() and not line.startswith("--")
    ]

    async with pg_engine.connect() as conn:
        before = set(await conn.run_sync(lambda c: sa_inspect(c).get_table_names()))
    async with pg_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    created = [t for t in reversed(Base.metadata.sorted_tables) if t.name not in before]
    try:
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO users (username, email, password_hash, "
                    "full_name, is_active, failed_login_count, created_at, "
                    f"updated_at) VALUES ('doc_{_SUFFIX}', 'doc_{_SUFFIX}@example.org', "
                    "'not-a-real-hash', 'x', true, 0, now(), now())"
                )
            )
            for stmt in statements:
                await conn.execute(text(stmt))
    except DBAPIError as e:  # pragma: no cover - CREATEROLE の無い環境
        pytest.skip(f"読み取り専用ロールを作れない: {e}")
    ro = create_async_engine(url.set(username=role, password=RO_PASSWORD))
    try:
        yield ro
    finally:
        await ro.dispose()
        async with pg_engine.begin() as conn:
            await conn.execute(text(f"DELETE FROM users WHERE username = 'doc_{_SUFFIX}'"))
            await conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}"))
            await conn.execute(text(f"REVOKE USAGE ON SCHEMA public FROM {role}"))
            await conn.execute(text(f"REVOKE CONNECT ON DATABASE {url.database} FROM {role}"))
            await conn.execute(text(f"DROP ROLE {role}"))
            for table in created:
                await conn.execute(text(f'DROP TABLE IF EXISTS "{table.name}" CASCADE'))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT password_hash FROM users",
        "SELECT jti FROM refresh_tokens",
        "SELECT * FROM auth_events",
        "SELECT * FROM user_roles",
    ],
)
async def test_documented_role_cannot_read_auth_tables(
    documented_role: AsyncEngine, sql: str
) -> None:
    """手順書どおりのロールでは、認証の表が 42501（権限なし）で読めないこと。"""
    with pytest.raises(DBAPIError) as exc:
        async with documented_role.connect() as conn:
            await conn.execute(text(sql))
    assert _sqlstate(exc.value) == "42501", exc.value


@pytest.mark.asyncio
async def test_documented_role_reads_other_tables_read_only(documented_role: AsyncEngine) -> None:
    """手順書どおりのロールでも、認証以外の表は読め、書き込みは止まること。"""
    async with documented_role.connect() as conn:
        assert (await conn.execute(text(f"SELECT v FROM {TABLE}"))).scalar() == "original"
        assert (await conn.execute(text("SHOW transaction_read_only"))).scalar() == "on"
    with pytest.raises(DBAPIError):
        async with documented_role.connect() as conn:
            await conn.execute(text(f"INSERT INTO {TABLE} VALUES ('P5', 'x')"))


# --- dblink: 読み取り専用トランザクションの外へ出る口 ----------------------------


@pytest.fixture
async def dblink(pg_engine: AsyncEngine) -> AsyncIterator[str]:
    """dblink 拡張を入れ、自分自身へつなぐ接続文字列を返す。入れられなければ skip。"""
    async with pg_engine.connect() as conn:
        existed = bool(
            (
                await conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'dblink'"))
            ).scalar()
        )
    if not existed:
        try:
            async with pg_engine.begin() as conn:
                await conn.execute(text("CREATE EXTENSION dblink"))
        except DBAPIError as e:  # pragma: no cover - contrib が無い・権限が無い環境
            pytest.skip(f"dblink を入れられない: {e}")
    url = make_url(PG_URL)
    try:
        yield (
            f"host={url.host or 'localhost'} port={url.port or 5432} dbname={url.database} "
            f"user={url.username} password={url.password}"
        )
    finally:
        if not existed:
            async with pg_engine.begin() as conn:
                await conn.execute(text("DROP EXTENSION IF EXISTS dblink"))


@pytest.mark.asyncio
async def test_dblink_escapes_the_readonly_transaction_but_sql_extract_rejects_it(
    app_session: AsyncSession, pg_engine: AsyncEngine, dblink: str
) -> None:
    """dblink は別セッションを開くので、読み取り専用トランザクションの中からでも書ける
    （関門だけでは止まらない）。だから文字列検査で拒否する。Unicode エスケープで
    `dblink_exec` を字面から消した形（レビューの再現）も `sql_extract` で拒否されること。
    """
    insert = f"INSERT INTO {TABLE} VALUES (''P7'', ''via-dblink'')"
    escaped = (
        f"SELECT U&\"\\0064blink_exec\"('{dblink}', '{insert}') WHERE :patient_id = :patient_id"
    )

    # 対照: 文字列検査を通さずに関門へ直接投げると、dblink 経由で書けてしまう
    async with readonly_program_sql(app_session) as reader:
        await reader.execute(text(f"SELECT dblink_exec('{dblink}', '{insert}')"))
    assert ("P7", "via-dblink") in await _rows(pg_engine)
    async with pg_engine.begin() as conn:
        await conn.execute(text(f"DELETE FROM {TABLE} WHERE patient_id = 'P7'"))

    assert not is_select_only_sql(escaped)
    prefill, evidence = await DSLInterpreter().execute([_plan(escaped)], "P1", app_session)
    assert "q" not in prefill
    # プログラム検証（`validate_program`）の段で落ち、step は1つも実行されない
    assert evidence["q"]["trace"] == []
    assert "SELECT" in evidence["q"]["error"], evidence
    assert await _rows(pg_engine) == [("P1", "original")]


# レビューで PostgreSQL 17 の `sql_extract` から実際に書けた形。`{db}` に dblink_exec を入れる
_COMMENT_BYPASSES = {
    # リテラルの中の `--` / `/*` から後ろをコメントとして消していた（1回目）
    "line_comment_in_literal": "SELECT '--' AS x, {db} WHERE :patient_id = :patient_id",
    "block_comment_in_literal": "SELECT '/*' AS x, {db} WHERE :patient_id = :patient_id",
    # PostgreSQL は `--` を `\r` でも終える（2回目）
    "line_comment_ended_by_cr": "SELECT 1 --\r, {db} WHERE :patient_id = :patient_id\n",
    # PostgreSQL の入れ子の開き `/*/` が閉じ `*/` と重なる（2回目）
    "overlapping_nested_opener": (
        "SELECT 1 /* /*/ -- */ */ , {db} WHERE :patient_id = :patient_id\n"
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("template", list(_COMMENT_BYPASSES.values()), ids=list(_COMMENT_BYPASSES))
async def test_comment_tricks_do_not_hide_dblink_exec(
    app_session: AsyncSession, pg_engine: AsyncEngine, dblink: str, template: str
) -> None:
    """コメントで `dblink_exec` を禁止語の検査から隠し、開発用フォールバック
    （READONLY_DATABASE_URL 無し）で書けていた形（レビューの再現）。コメントは引用の外では
    一切拒否するので、`sql_extract` で拒否され、何も書かれないこと。"""
    insert = f"INSERT INTO {TABLE} VALUES (''P8'', ''via-comment'')"
    sql = template.format(db=f"dblink_exec('{dblink}', '{insert}')")

    assert not is_select_only_sql(sql)
    prefill, evidence = await DSLInterpreter().execute([_plan(sql)], "P1", app_session)
    assert "q" not in prefill
    assert evidence["q"]["trace"] == []
    assert "SELECT" in evidence["q"]["error"], evidence
    assert await _rows(pg_engine) == [("P1", "original")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "glue",
    ["$$a$$=$$b$$union", "$t$a$t$=$t$b$t$union all"],
    ids=["empty_tag", "named_tag"],
)
async def test_a_keyword_right_after_a_dollar_quote_is_not_hidden(
    app_session: AsyncSession, glue: str
) -> None:
    """閉じたドル引用の直後の `union` が禁止語の境界に掛からず、`sql_extract` で
    `pg_authid` が読めていた（レビューの再現）。検証で拒否され、何も読まれないこと。"""
    sql = (
        f"SELECT v FROM {TABLE} WHERE patient_id = :patient_id AND {glue} "
        "SELECT rolname FROM pg_authid"
    )

    assert not is_select_only_sql(sql)
    prefill, evidence = await DSLInterpreter().execute([_plan(sql)], "NOPE", app_session)
    assert "q" not in prefill, prefill
    assert evidence["q"]["trace"] == []
    assert "SELECT" in evidence["q"]["error"], evidence
