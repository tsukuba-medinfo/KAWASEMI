"""実行ロールの外部DB取得の再試行。

外部DBは一時的なエラーを返すことがある（`db_retry` を参照）。
10秒 × 上限3回で投げ直し、**何回目で取れたかを trace へ残す**。上限まで失敗したら
例外にせず失敗の trace を返し、失敗の理由（`failure_code`）は元の例外から分類する。
待機の実時間はテストへ持ち込まない（`RETRY_WAIT_SECONDS` を 0 にする）。
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

import kawasemi.ai_input.db_retry as db_retry
import kawasemi.ai_input.executor as executor_mod
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.models import FailureCode
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry

_BASE = {"op": "external_db_extract", "id": "v"}
_STEP = {
    "op": "external_db_extract",
    "id": "v",
    "connection_id": "labs_db",
    "sql": "SELECT val FROM labs WHERE pid = :patient_id",
}


class _FlakyEngine:
    """最初の `fail_times` 回の connect() が一時的なエラーで落ちる。以降は本物へ委譲する。"""

    def __init__(
        self,
        real,  # noqa: ANN001
        fail_times: int,
        message: str = "Login timeout expired",
        kind: type[Exception] = OperationalError,
    ) -> None:
        self._real = real
        self._left = fail_times
        self._message = message
        self._kind = kind
        self.connects = 0

    def connect(self):  # noqa: ANN201
        self.connects += 1
        if self._left > 0:
            self._left -= 1
            raise self._kind("SELECT", {}, Exception(self._message))
        return self._real.connect()


@pytest.fixture
def interp_and_engine(tmp_path, monkeypatch):  # noqa: ANN001, ANN201
    db_file = tmp_path / "ext.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE labs (pid TEXT, val TEXT)")
    conn.execute("INSERT INTO labs (pid, val) VALUES ('REG-1', '陽性')")
    conn.commit()
    conn.close()
    dsn = f"sqlite+aiosqlite:///{db_file}"
    registry = ConnectionRegistry({"labs_db": {"scope": "external", "secret_ref": dsn}})
    monkeypatch.setattr(executor_mod, "get_registry", lambda: registry)
    monkeypatch.setattr(db_retry, "RETRY_WAIT_SECONDS", 0.0)
    interp = DSLInterpreter()
    real = interp._external_engine(dsn)

    def install(
        fail_times: int,
        message: str = "Login timeout expired",
        kind: type[Exception] = OperationalError,
    ) -> _FlakyEngine:
        flaky = _FlakyEngine(real, fail_times, message, kind)
        monkeypatch.setattr(interp, "_external_engine", lambda _dsn: flaky)
        return flaky

    return interp, install


@pytest.mark.asyncio
async def test_a_first_try_success_records_one_attempt(interp_and_engine) -> None:  # noqa: ANN001
    interp, install = interp_and_engine
    flaky = install(0)
    try:
        output, trace = await interp._op_external_db(_STEP, "REG-1", dict(_BASE))
    finally:
        await interp.aclose()
    assert output == "陽性"
    assert trace["attempts"] == 1, "一発で取れたことが残っていない"
    assert flaky.connects == 1


@pytest.mark.asyncio
async def test_a_transient_error_is_retried_and_the_attempt_count_is_kept(
    interp_and_engine,  # noqa: ANN001
) -> None:
    """2回落ちて3回目で取れた項目は、一発で取れた項目と trace で区別できる。"""
    interp, install = interp_and_engine
    flaky = install(2)
    try:
        output, trace = await interp._op_external_db(_STEP, "REG-1", dict(_BASE))
    finally:
        await interp.aclose()
    assert output == "陽性"
    assert trace["attempts"] == 3
    assert flaky.connects == 3
    assert "failure_code" not in trace and "error" not in trace


@pytest.mark.asyncio
async def test_giving_up_returns_a_failed_trace_with_the_real_error(
    interp_and_engine,  # noqa: ANN001
) -> None:
    """上限まで失敗したら例外にせず、元の例外から分類した失敗を attempts つきで返す。

    握り潰すと「一時的なエラーが続いた」と「表が存在しない」を区別できなくなるので、
    **最後の例外の内容が error に残る**（`PendingRollbackError` などに化けない）。
    分類は**元の例外**から行う（`RetriesExhausted` ではなく）——例外の型で分かれる
    2例（タイムアウトと表なし）で確かめる。
    """
    interp, install = interp_and_engine
    flaky = install(99)
    try:
        output, trace = await interp._op_external_db(_STEP, "REG-1", dict(_BASE))
    finally:
        await interp.aclose()
    assert output is None
    assert flaky.connects == db_retry.RETRY_MAX_ATTEMPTS
    assert trace["attempts"] == db_retry.RETRY_MAX_ATTEMPTS
    assert "Login timeout expired" in trace["error"]
    assert trace["failure_code"] == FailureCode.TIMEOUT
    assert trace["output"] is None


@pytest.mark.asyncio
async def test_a_permanent_error_is_classified_from_the_original_exception(
    interp_and_engine,  # noqa: ANN001
) -> None:
    """表が無い（恒久的）エラーも同じ回数だけ試し、failure_code は従来どおり schema_mismatch。

    再試行は恒久的エラーも対象にする（判定材料側と同じ方針。例外の型で絞ると、一時的な
    エラーの取りこぼしが出る）。**待つ代わりに回数を残す**ので、`attempts` で読み分けられる。
    """
    interp, install = interp_and_engine
    install(99, message="no such table: labs", kind=ProgrammingError)
    try:
        output, trace = await interp._op_external_db(_STEP, "REG-1", dict(_BASE))
    finally:
        await interp.aclose()
    assert output is None
    assert trace["attempts"] == db_retry.RETRY_MAX_ATTEMPTS
    assert trace["failure_code"] == FailureCode.SCHEMA_MISMATCH


@pytest.mark.asyncio
async def test_an_empty_result_is_not_retried(interp_and_engine) -> None:  # noqa: ANN001
    """行が0件なのは失敗ではなく取得の結果。待たず、`not_found_for_patient` になる。"""
    interp, install = interp_and_engine
    flaky = install(0)
    try:
        output, trace = await interp._op_external_db(_STEP, "NO-SUCH-PATIENT", dict(_BASE))
    finally:
        await interp.aclose()
    assert output is None
    assert flaky.connects == 1
    assert trace["attempts"] == 1
    assert trace["failure_code"] == FailureCode.NOT_FOUND_FOR_PATIENT


@pytest.mark.asyncio
async def test_the_raw_patient_key_is_masked_in_the_error_of_a_failed_fetch(
    interp_and_engine,  # noqa: ANN001
) -> None:
    """失敗の `error` に生の被験者キーを残さない。

    SQLAlchemy の例外文字列はバインド値（`[parameters: ('REG-12345',)]`）を含む。
    `inputs` だけ伏せても、`error` から生の名寄せ値が trace へ永続化される。
    """
    interp, install = interp_and_engine
    install(99, message="boom for REG-12345 happened")
    step = {**_STEP, "sql": "SELECT val FROM labs WHERE pid = :patient_id"}
    try:
        output, trace = await interp._exec_step(step, {}, "REG-12345", None, {}, None, {})
    finally:
        await interp.aclose()
    assert output is None
    assert "boom for" in trace["error"]
    assert "REG-12345" not in trace["error"], trace["error"]
    assert "REG-12345" not in str(trace.get("inputs"))


@pytest.mark.asyncio
async def test_an_invalid_sql_is_not_retried(interp_and_engine) -> None:  # noqa: ANN001
    """SELECT 以外は接続の前で拒否される。再試行にも `attempts` にも入らない。"""
    interp, install = interp_and_engine
    flaky = install(0)
    step = {**_STEP, "sql": "DELETE FROM labs"}
    try:
        output, trace = await interp._op_external_db(step, "REG-1", dict(_BASE))
    finally:
        await interp.aclose()
    assert output is None
    assert flaky.connects == 0
    assert "attempts" not in trace
