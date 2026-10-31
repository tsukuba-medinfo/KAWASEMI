"""プログラム SQL の読み取り専用実行

`_op_sql` は LLM が生成した SQL をアプリDBへ発行する。ゲートが `is_select_only_sql`
（接頭辞＋禁止語＋複文禁止）だけだと、**SELECT から到達できる書き込み・副作用**を
取りこぼす（Postgres の `SELECT ... INTO` / `pg_read_file`、MySQL の `INTO OUTFILE`）。
**ブロックリストは取りこぼすので、保証を文字列検査に置かない**——DB 側の権限で閉じる。

ここでは (1) 実行が読み取り専用の関門を通ること、(2) 関門が用意できない環境では
**実行しない**こと（フェイルクローズ）、(3) 文字列検査側の多層防御（集合演算・INTO 等）を
固定する。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.dsl import is_select_only_sql
from kawasemi.ai_input.executor import (
    DSLInterpreter,
    ReadOnlySessionUnavailableError,
    readonly_program_sql,
)
from kawasemi.ai_input.models import FailureCode, InputPlan


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


@pytest.mark.asyncio
async def test_it_fails_closed_when_no_readonly_route_exists(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """関門が用意できない方言では**実行しない**こと（フェイルクローズ）。

    SQLite 以外で `READONLY_DATABASE_URL` が未設定のときに、黙って読み書きセッションで
    実行するのがいちばん危ない。`source_unavailable` として記録し、値は入れない。
    """

    class _FakeDialect:
        name = "postgresql"

    class _FakeBind:
        dialect = _FakeDialect()

    monkeypatch.setattr(db_session, "get_bind", lambda: _FakeBind())

    with pytest.raises(ReadOnlySessionUnavailableError):
        async with readonly_program_sql(db_session):
            pass  # pragma: no cover - 入る前に落ちる


@pytest.mark.asyncio
async def test_the_step_records_source_unavailable_when_it_cannot_read_only(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """フェイルクローズが step の記録として残ること（黙って成功にしない）。"""
    import kawasemi.ai_input.executor as executor_mod

    def _boom(_db: AsyncSession):  # noqa: ANN202
        raise ReadOnlySessionUnavailableError("読み取り専用で実行できません（テスト）")

    monkeypatch.setattr(executor_mod, "readonly_program_sql", _boom)

    program = {
        "steps": [
            {"op": "sql_extract", "id": "s", "sql": "SELECT 1 WHERE :patient_id = :patient_id"}
        ],
        "return": "$s",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("q", program)], "P1", db_session)

    assert "q" not in prefill
    trace = evidence["q"]["trace"][0]
    assert trace["failure_code"] == FailureCode.SOURCE_UNAVAILABLE
    assert "読み取り専用" in trace["error"]


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
