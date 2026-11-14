"""例外の要約（構造化・許可リスト方式）と、ログの伏せ処理（線形時間）。

evidence・trace・DB（`InputExecution.error_summary` など）・API の応答・SSE へ出す文面は、
ドライバの例外文面（`DETAIL: Key (col)=(value)`、`Token "x" is invalid`、型変換エラーの値…）を
**一切含まない**。文面を正規表現で伏せる方式は、形の取りこぼしが出る（PostgreSQL のメッセージは
数百種ある）ので、型名・SQLSTATE・構造化された名前（制約名・列名）・HTTP の接続先だけを出す。
ここでは、どんな文面でも値が出ないことを、文面の形に依存しない形で確かめる。
"""

from __future__ import annotations

import logging
import sqlite3
import time

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from kawasemi.ai_input.db_retry import RetriesExhausted
from kawasemi.ai_input.dsl import ParamError
from kawasemi.ai_input.executor import (
    PathOutsideRootError,
    StepConfigError,
    _exc_msg,
    _raw_exc_msg,
    classify_exception,
)
from kawasemi.core.exception_summary import safe_message, summarize_exception
from kawasemi.core.exceptions import DatabaseException, FHIRServerException
from kawasemi.core.logging_config import PHIRedactionFilter, _redact_text, redact_log_text

NEEDLE = "PHI-NEEDLE-9137"


class _FakeDriverError(Exception):
    """asyncpg の例外のように、構造化した属性（sqlstate・制約名・列名）を持つ例外。"""

    def __init__(self, message: str, **attrs: str) -> None:
        super().__init__(message)
        for k, v in attrs.items():
            setattr(self, k, v)


# 値を文面に載せるドライバのメッセージの形（再スキャンの Reality Check で採った 57 種のうち、
# 取りこぼしが出た形を含む。どれも要約には出ない）。
DRIVER_MESSAGES = [
    f"DETAIL:  Key (email)=({NEEDLE}@example.org) already exists.",
    f"DETAIL:  キー (email)=({NEEDLE}) はすでに存在します。",
    f"DETAIL:  Key (email)=(山田\n{NEEDLE}) already exists.",  # 値に改行
    f"Failing row contains ({NEEDLE}, null, x).",
    f"失敗した行は({NEEDLE}, null)を含みます",
    f"invalid input for query argument $1: '{NEEDLE}' ('str' object cannot be interpreted)",
    f'invalid input syntax for type integer: "{NEEDLE}"',
    f'"integer"型の入力構文が不正です: "{NEEDLE}"',
    f'malformed array literal: "{NEEDLE}"',
    f'DETAIL: Token "{NEEDLE}" is invalid.',
    f'malformed range literal: "{NEEDLE}"',
    f'relation "{NEEDLE}" does not exist',
    f'invalid input value for enum t.col: "{NEEDLE}"',
    f'no operand in tsquery: "{NEEDLE} &"',
    f'invalid size: "{NEEDLE}"',
    f"invalid XML document DETAIL: ... <a>{NEEDLE}",
    f"could not convert string to float: '{NEEDLE}'",
    f"1 validation error for Model\n  Input should be a valid integer [input_value='{NEEDLE}']",
    f"free-form message nobody has a pattern for: {NEEDLE}",
]


@pytest.mark.parametrize("message", DRIVER_MESSAGES)
def test_no_driver_message_shape_reaches_the_summary(message: str) -> None:
    """どの文面でも、要約に値が出ない（形に依存しない: 文面そのものを使わない）。"""
    plain = Exception(message)
    assert NEEDLE not in summarize_exception(plain)
    assert NEEDLE not in _exc_msg(plain)
    # SQLSTATE と構造化した名前があれば、それは出る（原因の切り分けに要る）
    coded = _FakeDriverError(
        message,
        sqlstate="23505",
        constraint_name="users_email_key",
        column_name="email",
        table_name="users",
    )
    out = summarize_exception(coded)
    assert NEEDLE not in out
    assert "sqlstate=23505" in out
    assert "constraint=users_email_key" in out and "column=email" in out and "table=users" in out
    # SQLAlchemy の包み（orig に実体）でも同じ
    wrapped = IntegrityError("INSERT ...", {"p": NEEDLE}, coded)
    out2 = summarize_exception(wrapped)
    assert NEEDLE not in out2
    assert "IntegrityError" in out2 and "sqlstate=23505" in out2


def test_structured_names_must_look_like_identifiers() -> None:
    """名前の属性に値が混ざっていても（識別子の形でないものは）出さない。"""
    e = _FakeDriverError("x", sqlstate="23505", constraint_name=f"has space {NEEDLE}")
    out = summarize_exception(e)
    assert NEEDLE not in out and "constraint=" not in out
    e2 = _FakeDriverError("x", sqlstate="NOT-A-CODE", column_name="c")
    assert "sqlstate" not in summarize_exception(e2)


def test_a_real_sqlite_integrity_error_names_the_error_not_the_value() -> None:
    engine = sa.create_engine("sqlite://", hide_parameters=False)
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE u (email TEXT UNIQUE)"))
        conn.execute(sa.text("INSERT INTO u VALUES (:e)"), {"e": f"{NEEDLE}@x.example"})
        with pytest.raises(IntegrityError) as info:
            conn.execute(sa.text("INSERT INTO u VALUES (:e)"), {"e": f"{NEEDLE}@x.example"})
    assert NEEDLE in str(info.value)  # 前提: 生の文面には値がある
    out = summarize_exception(info.value)
    assert NEEDLE not in out
    assert out.startswith("IntegrityError")
    if hasattr(sqlite3.Error, "sqlite_errorname") or hasattr(info.value.orig, "sqlite_errorname"):
        assert "sqlite=SQLITE_CONSTRAINT" in out


def test_third_party_exceptions_show_only_their_type() -> None:
    assert summarize_exception(ValueError(f"bad value {NEEDLE}")) == "ValueError"
    assert summarize_exception(KeyError(NEEDLE)) == "KeyError"
    assert summarize_exception(TimeoutError()) == "TimeoutError"
    assert summarize_exception(RuntimeError(f"DETAIL: Key (a)=({NEEDLE})")) == "RuntimeError"


def test_authored_exceptions_keep_their_own_message() -> None:
    assert summarize_exception(ParamError("パラメータ x が未指定")) == (
        "ParamError: パラメータ x が未指定"
    )
    assert summarize_exception(StepConfigError("列名が見つかりません: 'a'")) == (
        "StepConfigError: 列名が見つかりません: 'a'"
    )
    assert (
        safe_message(PathOutsideRootError("絶対パスは指定できません")) == "絶対パスは指定できません"
    )
    # こちらが書いていない例外は、`safe_message` でも文面を出さない（要約になる）
    assert safe_message(ValueError(f"DETAIL: Key (a)=({NEEDLE})")) == "ValueError"
    # こちらが書いた文面でも、URL の資格情報は落とす
    assert "pw" not in summarize_exception(ParamError("接続 https://svc:pw@h.example/x が不正"))
    # 型名しか無い（空の文面）の例外は型名
    assert summarize_exception(ParamError("")) == "ParamError"
    assert summarize_exception(FHIRServerException(message="FHIRサーバエラー")) == (
        "FHIRServerException: FHIRサーバエラー"
    )


def test_database_exception_details_use_the_summary() -> None:
    exc = DatabaseException("失敗", original_error=Exception(f"DETAIL: Key (a)=({NEEDLE})"))
    assert NEEDLE not in repr(exc.details)


def test_retries_exhausted_is_summarised_through_its_cause() -> None:
    cause = _FakeDriverError(f"DETAIL: Key (a)=({NEEDLE})", sqlstate="40001")
    out = summarize_exception(RetriesExhausted(3, cause))
    assert NEEDLE not in out and "sqlstate=40001" in out


def test_http_errors_show_status_and_target_without_userinfo_path_or_query() -> None:
    request = httpx.Request(
        "GET", f"https://svc:{NEEDLE}@fhir.example:8443/fhir/Patient?t={NEEDLE}"
    )
    out = summarize_exception(httpx.ConnectError(f"boom {NEEDLE}", request=request))
    assert NEEDLE not in out
    assert "target=https://fhir.example:8443" in out
    # path（FHIR では `/Patient/<id>` のように患者 ID が入る）は含めない
    assert "/fhir/Patient" not in out
    response = httpx.Response(503, request=request)
    out2 = summarize_exception(httpx.HTTPStatusError("bad", request=request, response=response))
    assert "status=503" in out2 and NEEDLE not in out2
    # request が未設定の httpx 例外（RuntimeError を出す）でも落ちない
    assert summarize_exception(httpx.ReadTimeout("slow")) == "ReadTimeout"


def test_the_summary_is_bounded_and_never_empty() -> None:
    assert summarize_exception(Exception())
    long_authored = ParamError("x" * 100_000)
    assert len(summarize_exception(long_authored)) < 1100


def test_classification_still_uses_the_raw_text() -> None:
    """evidence に出す文面は要約だが、失敗の分類は生の文面で判定する（FailureCode は定数）。"""
    err = Exception('relation "labs" does not exist; DETAIL: Key (email)=(a@b.example) bad')
    assert "a@b.example" not in _exc_msg(err)
    assert "a@b.example" in _raw_exc_msg(err)
    assert classify_exception(err) == "schema_mismatch"
    assert classify_exception(Exception("Login timeout expired")) == "timeout"
    assert classify_exception(Exception("connection refused")) == "source_unavailable"
    assert classify_exception(Exception("boom")) == "unclassified"


# --- ログの伏せ処理: 線形時間 ---------------------------------------------------------------

_TIMING_UNITS = [
    "http://",
    "a:",
    "a.",
    "a:b/",
    "Key (a)=(x)y",
    "[parameters: ",
    "[parameters: ((((",
    "] ] ] ",
    "@",
    "word-",
    'token="',
    "token='",
    "token ",
    "https://a:b@",
]


@pytest.mark.parametrize("unit", _TIMING_UNITS)
def test_log_redaction_is_bounded_in_time_at_64kib(unit: str) -> None:
    for text in (
        unit * (64 * 1024 // len(unit)),
        "http://" + unit * (64 * 1024 // len(unit)),
        unit * (64 * 1024 // len(unit)) + "://x",
    ):
        started = time.perf_counter()
        redact_log_text(text)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.2, (unit, elapsed)


@pytest.mark.parametrize("unit", _TIMING_UNITS)
def test_log_redaction_is_bounded_in_time_at_3mb(unit: str) -> None:
    text = unit * (3_000_000 // len(unit))
    started = time.perf_counter()
    out = redact_log_text(text)
    assert time.perf_counter() - started < 0.5, unit
    # 上限（64 KiB）で切る（伏せ字への置き換えで多少は伸びる）
    assert len(out) < 3 * 64 * 1024


def test_the_filter_survives_a_huge_message() -> None:
    f = PHIRedactionFilter()
    record = logging.LogRecord("x", logging.ERROR, "p", 1, "[parameters: ('a'," * 200_000, (), None)
    started = time.perf_counter()
    f.filter(record)
    assert time.perf_counter() - started < 0.5


def test_parameters_segment_that_is_not_a_bind_dump_is_kept() -> None:
    text = "Model fit [parameters: lr=0.1] finished"
    assert redact_log_text(text) == text


def test_parameters_dump_forms_are_masked() -> None:
    for dump in ("('a@x.example', 'b')", "{'p': 'a@x.example'}", "[('a@x.example',), ('c',)]"):
        out = redact_log_text(
            f"boom\n[SQL: x]\n[parameters: {dump}]\n(Background on this error at: https://sqlalche.me/e/20/gkpj)"
        )
        assert "a@x.example" not in out, dump
        assert "(Background on this error at:" in out


def test_log_text_masks_userinfo_and_sensitive_keys_but_keeps_the_rest() -> None:
    out = _redact_text("GET https://svc:pw@fhir.example/fhir/Patient?_count=5 token=ABC123 ok")
    assert "svc:pw" not in out and "ABC123" not in out
    assert "fhir.example/fhir/Patient?_count=5" in out


def test_a_url_with_an_email_in_the_query_is_not_mangled() -> None:
    url = "https://example.com:8080?email=a@b.com"
    assert redact_log_text(url) == url


def test_userinfo_with_a_slash_in_the_password_is_best_effort_masked() -> None:
    out = redact_log_text("failed https://user:p/ss@host.example/x")
    assert "p/ss" not in out and "host.example" in out
