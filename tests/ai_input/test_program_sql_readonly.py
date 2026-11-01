"""プログラム SQL の読み取り専用実行

`_op_sql` は LLM が生成した SQL をアプリDBへ発行する。ゲートが `is_select_only_sql`
（接頭辞＋禁止語＋複文禁止）だけだと、**SELECT から到達できる書き込み・副作用**を
取りこぼす（Postgres の `SELECT ... INTO` / `pg_read_file`、MySQL の `INTO OUTFILE`）。
**ブロックリストは取りこぼすので、保証を文字列検査に置かない**——DB 側の権限で閉じる。

ここでは (1) 実行が読み取り専用の関門を通ること、(2) SQLite 以外では
`READONLY_DATABASE_URL` のエンジン（無ければ production 以外に限りアプリのエンジン）から
別接続を取り、読み取り専用トランザクションの中で読むこと、production で DSN が無ければ
**実行しない**こと（フェイルクローズ）、(3) 文字列検査側の多層防御（集合演算・INTO 等）を
固定する。PostgreSQL 上の実挙動は `test_program_sql_readonly_postgres.py` が見る。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kawasemi.ai_input.dsl import is_select_only_sql
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.models import FailureCode, InputPlan
from kawasemi.core import readonly_db
from kawasemi.core.config import settings
from kawasemi.core.readonly_db import readonly_program_sql


def _plan(link_id: str, program: dict) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id=link_id, program=program, status="confirmed")


@pytest.mark.asyncio
async def test_writes_are_refused_inside_the_guard(db_session: AsyncSession) -> None:
    """関門の中では**書き込みが DB 側で拒否される**こと（文字列検査に依存しない証拠）。

    `is_select_only_sql` を迂回できたとしても、接続が読み取り専用なら書けない。
    ここでは検査を通さずに直接 INSERT を投げて、**DB が拒否する**ことを見る。
    """
    async with readonly_program_sql(db_session):
        with pytest.raises(Exception) as exc:  # noqa: PT011 - 方言ごとに例外型が違う
            await db_session.execute(text("CREATE TABLE evil_117 (x INTEGER)"))
    assert "readonly" in str(exc.value).lower() or "read-only" in str(exc.value).lower(), exc.value


@pytest.mark.asyncio
async def test_the_guard_is_released_after_use(db_session: AsyncSession) -> None:
    """関門を抜けたら**書き込みが戻る**こと（evidence の保存を止めない）。

    `PRAGMA query_only` は接続スコープなので、戻さないと同じセッションの後続の書き込み
    （`InputExecution` や evidence の保存）が全部落ちる。**関門は step の実行だけを包む。**
    """
    async with readonly_program_sql(db_session):
        await db_session.execute(text("SELECT 1"))

    await db_session.execute(text("CREATE TABLE ok_117 (x INTEGER)"))
    await db_session.execute(text("DROP TABLE ok_117"))


@pytest.mark.asyncio
async def test_sql_extract_still_reads_through_the_guard(db_session: AsyncSession) -> None:
    """正常系: `sql_extract` は関門越しに値を読めること（関門が読みを壊していない）。"""
    await db_session.execute(text("CREATE TABLE t_117 (patient_id TEXT, v TEXT)"))
    await db_session.execute(text("INSERT INTO t_117 VALUES ('P1', '42')"))
    await db_session.commit()

    program = {
        "steps": [
            {
                "op": "sql_extract",
                "id": "s",
                "sql": "SELECT v FROM t_117 WHERE patient_id = :patient_id",
            }
        ],
        "return": "$s",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("q", program)], "P1", db_session)

    assert prefill["q"] == "42"
    assert evidence["q"]["trace"][0].get("failure_code") is None


# --- SQLite 以外の経路 -------------------------------------------------------
#
# PostgreSQL を立てずに経路を見るため、アプリのセッションの方言だけを "postgresql" に
# 見せかける。読み取り専用 DSN 側には SQLite のファイル DB を置く（関門は `query_only`）。


def _pretend_postgresql(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeDialect:
        name = "postgresql"

    class _FakeBind:
        dialect = _FakeDialect()

    monkeypatch.setattr(session, "get_bind", lambda: _FakeBind())


@pytest.fixture
async def readonly_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
    """値 'from-readonly-dsn' を持つ別の DB を作り、READONLY_DATABASE_URL に設定する。"""
    url = f"sqlite+aiosqlite:///{tmp_path / 'readonly.db'}"
    seed = create_async_engine(url)
    async with seed.begin() as conn:
        await conn.execute(text("CREATE TABLE t_ro (patient_id TEXT, v TEXT)"))
        await conn.execute(text("INSERT INTO t_ro VALUES ('P1', 'from-readonly-dsn')"))
    await seed.dispose()
    monkeypatch.setattr(settings, "readonly_database_url", url)
    yield url
    await readonly_db.dispose_readonly_engine()


_RO_PROGRAM = {
    "steps": [
        {"op": "sql_extract", "id": "s", "sql": "SELECT v FROM t_ro WHERE patient_id = :patient_id"}
    ],
    "return": "$s",
}


@pytest.mark.asyncio
async def test_sql_extract_reads_through_the_readonly_dsn_engine(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, readonly_dsn: str
) -> None:
    """READONLY_DATABASE_URL があれば、`sql_extract` は**その DSN のエンジン**で読むこと。

    `t_ro` は読み取り専用 DSN 側にだけあり、アプリのセッション側には無い。値が取れれば
    読み書きセッションではなく読み取り専用エンジンを通った証拠になる。
    """
    _pretend_postgresql(db_session, monkeypatch)

    prefill, evidence = await DSLInterpreter().execute([_plan("q", _RO_PROGRAM)], "P1", db_session)

    assert prefill["q"] == "from-readonly-dsn", evidence
    engine = readonly_db.get_readonly_engine()
    assert engine is not None
    assert str(engine.url) == readonly_dsn
    # 遅延生成したエンジンはキャッシュされる（実行のたびに作らない）
    assert readonly_db.get_readonly_engine() is engine


@pytest.mark.asyncio
async def test_writes_are_refused_on_the_readonly_dsn_route(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, readonly_dsn: str
) -> None:
    """読み取り専用 DSN の経路でも、書き込みは DB 側で拒否されること。"""
    _pretend_postgresql(db_session, monkeypatch)

    async with readonly_program_sql(db_session) as reader:
        assert reader is not db_session
        with pytest.raises(Exception) as exc:  # noqa: PT011 - 方言ごとに例外型が違う
            await reader.execute(text("INSERT INTO t_ro VALUES ('P2', 'x')"))
    assert "readonly" in str(exc.value).lower(), exc.value

    engine = readonly_db.get_readonly_engine()
    assert engine is not None
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM t_ro"))).scalar_one()
    assert count == 1


@pytest.mark.asyncio
async def test_dispose_drops_the_cached_engine(readonly_dsn: str) -> None:
    """シャットダウン時の破棄でキャッシュが空になり、次の利用で作り直されること。"""
    first = readonly_db.get_readonly_engine()
    await readonly_db.dispose_readonly_engine()
    second = readonly_db.get_readonly_engine()
    assert first is not None and second is not None
    assert first is not second


@pytest.mark.asyncio
async def test_without_readonly_dsn_it_uses_a_separate_readonly_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """production 以外で READONLY_DATABASE_URL が無い非 SQLite では、アプリのエンジンから
    **別接続**を取り、読み取り専用トランザクションの中で読むこと。

    同じセッションのトランザクションを読み取り専用にしないので、後続の書き込みは止まらない。
    """
    monkeypatch.setattr(settings, "readonly_database_url", "")
    monkeypatch.setattr(settings, "environment", "development")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t_ro (patient_id TEXT, v TEXT)"))
        await conn.execute(text("INSERT INTO t_ro VALUES ('P1', 'from-app-engine')"))
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            _pretend_postgresql(session, monkeypatch)
            async with readonly_program_sql(session) as reader:
                assert reader is not session
                value = (await reader.execute(text("SELECT v FROM t_ro"))).scalar_one()
                with pytest.raises(Exception, match="readonly"):
                    await reader.execute(text("INSERT INTO t_ro VALUES ('P2', 'x')"))
            assert value == "from-app-engine"
            # 関門の外の、セッション自身の書き込みは通る
            await session.execute(text("INSERT INTO t_ro VALUES ('P3', 'ok')"))
            await session.commit()
            count = (await session.execute(text("SELECT COUNT(*) FROM t_ro"))).scalar_one()
            assert count == 2
    finally:
        await engine.dispose()


class _SpyConn:
    def __init__(self) -> None:
        self.options: dict[str, Any] = {}
        self.statements: list[str] = []
        self.rolled_back = False

    async def execution_options(self, **opts: Any) -> _SpyConn:
        self.options.update(opts)
        return self

    async def execute(self, stmt: Any, *_args: Any) -> None:
        self.statements.append(str(stmt))

    async def rollback(self) -> None:
        self.rolled_back = True


class _SpyEngine:
    def __init__(self, dialect: str) -> None:
        class _D:
            name = dialect

        self.dialect = _D()
        self.conn = _SpyConn()

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[_SpyConn]:
        yield self.conn


@pytest.mark.asyncio
async def test_postgresql_route_opens_a_readonly_transaction_and_rolls_back(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgreSQL では読み取り専用 DSN のエンジンに `postgresql_readonly=True` を付けて
    接続し（`BEGIN READ ONLY`）、抜けるときに必ずロールバックすること。"""
    spy = _SpyEngine("postgresql")
    monkeypatch.setattr(readonly_db, "get_readonly_engine", lambda: spy)
    _pretend_postgresql(db_session, monkeypatch)

    with pytest.raises(RuntimeError):
        async with readonly_program_sql(db_session) as reader:
            assert reader is spy.conn
            raise RuntimeError("SQL の失敗")

    assert spy.conn.options == {"postgresql_readonly": True}
    assert spy.conn.statements == []
    assert spy.conn.rolled_back


@pytest.mark.asyncio
async def test_other_dialects_set_the_transaction_read_only(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgreSQL 以外の非 SQLite では `SET TRANSACTION READ ONLY` を先に発行すること。"""
    spy = _SpyEngine("mysql")
    monkeypatch.setattr(readonly_db, "get_readonly_engine", lambda: spy)
    _pretend_postgresql(db_session, monkeypatch)

    async with readonly_program_sql(db_session):
        pass

    assert spy.conn.statements == ["SET TRANSACTION READ ONLY"]
    assert spy.conn.rolled_back


def test_the_warning_fires_only_for_non_sqlite_without_readonly_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """起動時・check_schema の警告は「非 SQLite かつ DSN 未設定」のときだけ出ること。
    production では「実行しない」旨の文面になること。"""
    monkeypatch.setattr(settings, "readonly_database_url", "")
    for env in ("development", "staging", "production"):
        monkeypatch.setattr(settings, "environment", env)
        assert readonly_db.readonly_route_warning("sqlite") is None
    monkeypatch.setattr(settings, "environment", "development")
    assert readonly_db.readonly_route_warning("postgresql") == readonly_db.UNCONFIGURED_WARNING
    monkeypatch.setattr(settings, "environment", "production")
    assert (
        readonly_db.readonly_route_warning("postgresql")
        == readonly_db.UNCONFIGURED_PRODUCTION_WARNING
    )
    monkeypatch.setattr(settings, "readonly_database_url", "postgresql+asyncpg://ro@h/db")
    assert readonly_db.readonly_route_warning("postgresql") is None


# --- production で読み取り専用ロールが無いとき: 実行しない（フェイルクローズ） ---------


@pytest.fixture
def production_without_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "readonly_database_url", "")
    monkeypatch.setattr(settings, "environment", "production")


@pytest.mark.asyncio
@pytest.mark.usefixtures("production_without_dsn")
async def test_production_refuses_without_readonly_dsn(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """production の非 SQLite で DSN が無ければ、関門は接続を取らずに拒否すること。"""
    spy = _SpyEngine("postgresql")
    monkeypatch.setattr(readonly_db, "_app_engine", lambda _db: spy)
    _pretend_postgresql(db_session, monkeypatch)

    with pytest.raises(readonly_db.ReadOnlySessionUnavailableError):
        async with readonly_program_sql(db_session):
            pass  # pragma: no cover - 入る前に落ちる
    assert spy.conn.statements == [] and not spy.conn.options


@pytest.mark.asyncio
@pytest.mark.usefixtures("production_without_dsn")
async def test_production_sql_extract_records_source_unavailable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """拒否は `sql_extract` の step に source_unavailable として残ること（黙って成功にしない）。"""
    _pretend_postgresql(db_session, monkeypatch)

    prefill, evidence = await DSLInterpreter().execute([_plan("q", _RO_PROGRAM)], "P1", db_session)

    assert "q" not in prefill
    trace = evidence["q"]["trace"][0]
    assert trace["failure_code"] == FailureCode.SOURCE_UNAVAILABLE
    assert "READONLY_DATABASE_URL" in trace["error"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("production_without_dsn")
async def test_production_statistical_profile_is_not_applicable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """統計プロファイルの自アプリDB集計も production では実行しないこと。"""
    import kawasemi.ai_input.statistical_profile as sp
    from kawasemi.ai_input.models import OpType, SourceCategory

    _pretend_postgresql(db_session, monkeypatch)
    monkeypatch.setattr(sp, "resolve_db_op", lambda *_a: OpType.SQL_EXTRACT)
    candidate = {
        "source_category": SourceCategory.LOCAL_DB,
        "payload": {"db_qualified": "t_sp", "physical_name": "v"},
    }
    profile = await sp.generate_statistical_profile(candidate, db_session, min_cell_size=1)

    assert profile["applicable"] is False
    assert "READONLY_DATABASE_URL" in profile["reason"]


@pytest.mark.asyncio
async def test_production_with_readonly_dsn_reads(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, readonly_dsn: str
) -> None:
    """production でも DSN があれば読み取り専用ロールの経路で読めること。"""
    monkeypatch.setattr(settings, "environment", "production")
    _pretend_postgresql(db_session, monkeypatch)

    prefill, evidence = await DSLInterpreter().execute([_plan("q", _RO_PROGRAM)], "P1", db_session)
    assert prefill["q"] == "from-readonly-dsn", evidence


@pytest.mark.asyncio
async def test_sqlite_is_unaffected_by_production(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite は production でも DSN 不要のまま（`PRAGMA query_only`）であること。"""
    monkeypatch.setattr(settings, "readonly_database_url", "")
    monkeypatch.setattr(settings, "environment", "production")
    async with readonly_program_sql(db_session) as reader:
        assert reader is db_session
        assert (await reader.execute(text("SELECT 1"))).scalar() == 1


@pytest.mark.asyncio
async def test_statistical_profile_aggregates_go_through_the_guard(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """統計プロファイルの自アプリDB集計も同じ関門を通ること。"""
    import kawasemi.ai_input.statistical_profile as sp
    from kawasemi.ai_input.models import OpType, SourceCategory

    await db_session.execute(text("CREATE TABLE t_sp (v TEXT)"))
    await db_session.execute(text("INSERT INTO t_sp VALUES ('a')"))

    entered: list[AsyncSession] = []
    real = sp.readonly_program_sql

    @asynccontextmanager
    async def _spy(db: AsyncSession) -> AsyncIterator[Any]:
        entered.append(db)
        async with real(db) as reader:
            yield reader

    monkeypatch.setattr(sp, "readonly_program_sql", _spy)
    monkeypatch.setattr(sp, "resolve_db_op", lambda *_a: OpType.SQL_EXTRACT)

    candidate = {
        "source_category": SourceCategory.LOCAL_DB,
        "payload": {"db_qualified": "t_sp", "physical_name": "v"},
    }
    profile = await sp.generate_statistical_profile(candidate, db_session, min_cell_size=1)

    assert entered == [db_session]
    assert profile["applicable"] is True
    assert profile["total_count"] == 1


@pytest.mark.parametrize(
    "sql",
    [
        # 実測ケース（索引の必須識別子を含むので「索引由来」として採用されていた）
        "SELECT BLAST_PCT FROM DEMO.LAB WHERE PATIENT_ID = :patient_id "
        "UNION SELECT password_hash FROM users",
        "SELECT a FROM t WHERE id = :patient_id EXCEPT SELECT b FROM u",
        "SELECT a FROM t WHERE id = :patient_id INTERSECT SELECT b FROM u",
        # SELECT から到達できる書き込み・ファイル読み書き
        "SELECT * INTO evil FROM users",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT a FROM t INTO OUTFILE '/tmp/x'",
    ],
)
def test_forbidden_sql_shapes_are_rejected(sql: str) -> None:
    """文字列検査側の多層防御（集合演算・INTO・ファイル関数）。"""
    assert is_select_only_sql(sql) is False


def test_ordinary_selects_still_pass() -> None:
    """正当な SELECT / WITH を拒否していないこと（誤検出で作業が止まらないこと）。"""
    assert is_select_only_sql("SELECT v FROM t WHERE patient_id = :patient_id") is True
    assert is_select_only_sql("WITH x AS (SELECT 1 AS a) SELECT a FROM x") is True
