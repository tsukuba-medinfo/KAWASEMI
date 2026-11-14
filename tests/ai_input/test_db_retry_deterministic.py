"""外部DBの再試行: やり直しても変わらない失敗は投げ直さず、一時的な失敗は投げ直す。"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError

from kawasemi.ai_input.db_retry import RetriesExhausted, is_deterministic_failure, with_retry


class _PgError(Exception):
    def __init__(self, msg: str, sqlstate: str | None) -> None:
        super().__init__(msg)
        self.sqlstate = sqlstate


def _err(kind: type[Exception], msg: str, sqlstate: str | None = None) -> Exception:
    return kind("SELECT", {}, _PgError(msg, sqlstate))


async def _no_sleep(_s: float) -> None:
    return None


@pytest.mark.parametrize(
    ("exc", "deterministic"),
    [
        (_err(ProgrammingError, "cannot execute INSERT in a read-only transaction", "25006"), True),
        (_err(OperationalError, "canceling statement due to statement timeout", "57014"), True),
        (_err(ProgrammingError, "permission denied for table t", "42501"), True),
        (_err(ProgrammingError, "syntax error", "42601"), True),
        (_err(OperationalError, "connection failure", "08006"), False),
        (_err(OperationalError, "deadlock detected", "40P01"), False),
        (_err(OperationalError, "could not serialize", "40001"), False),
        (_err(OperationalError, "the database system is shutting down", "57P01"), False),
        (_err(OperationalError, "Login timeout expired"), False),
        (_err(OperationalError, "attempt to write a readonly database"), True),
    ],
)
def test_classification(exc: Exception, deterministic: bool) -> None:
    assert is_deterministic_failure(exc) is deterministic


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        _err(ProgrammingError, "read-only", "25006"),
        _err(OperationalError, "statement timeout", "57014"),
        _err(OperationalError, "attempt to write a readonly database"),
    ],
)
async def test_deterministic_failure_is_not_retried(exc: Exception) -> None:
    calls = 0

    async def call() -> int:
        nonlocal calls
        calls += 1
        raise exc

    with pytest.raises(RetriesExhausted) as info:
        await with_retry("t", call, sleep=_no_sleep)
    assert calls == 1
    assert info.value.attempts == 1
    assert info.value.__cause__ is exc


@pytest.mark.asyncio
async def test_transient_failure_is_still_retried() -> None:
    calls = 0

    async def call() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _err(OperationalError, "connection failure", "08006")
        return "ok"

    result, attempt = await with_retry("t", call, sleep=_no_sleep)
    assert (result, attempt, calls) == ("ok", 3, 3)
