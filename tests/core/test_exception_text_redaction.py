"""例外・ログの文面の伏せ処理（再スキャン 3 回目 F4 の追加分）。

ドライバの例外は、`[parameters: ...]` の外にも行や束縛の値を載せる（`DETAIL: Key (col)=(value)` など）。
`redact_exception_text` が、evidence・trace・DB・API の応答・ログの共通の窓口として、これらを
ベストエフォートで伏せる。文面は実機（PostgreSQL 17・asyncpg・日本語ロケール）で採ったもの。
"""

from __future__ import annotations

import logging
import time

import pytest

from kawasemi.ai_input.executor import _exc_msg, _raw_exc_msg, classify_exception
from kawasemi.core.logging_config import (
    PHIRedactionFilter,
    _redact_text,
    redact_exception_text,
    redact_urls,
)

# --- 実機で採ったドライバの文面 ---------------------------------------------------------

UNIQUE_JA = (
    "(sqlalchemy.dialects.postgresql.asyncpg.IntegrityError) <class "
    "'asyncpg.exceptions.UniqueViolationError'>: 重複したキー値は一意性制約\"pr_t_email_key\"違反となります\n"
    "DETAIL:  キー (email)=(phi-victim@example.org) はすでに存在します。\n"
    "[SQL: INSERT INTO pr_t VALUES ($1, 2, 'y')]\n"
    "[parameters: ('phi-victim@example.org',)]\n"
    "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
)
NOTNULL_JA = (
    '<class \'asyncpg.exceptions.NotNullViolationError\'>: リレーション"pr_t"の列"n"のNULL値が'
    "非NULL制約に違反しています\n"
    "DETAIL:  失敗した行は(z@example.org, null, PHI-VALUE)を含みます\n"
    "[SQL: INSERT INTO pr_t VALUES ($1, $2, $3)]"
)
SYNTAX_JA = '<class \'asyncpg.exceptions.InvalidTextRepresentationError\'>: "integer"型の入力構文が不正です: "PHI-DIAGNOSIS-XYZ"'
ARG_EN = (
    "<class 'asyncpg.exceptions.DataError'>: invalid input for query argument $1: "
    "'PHI-PATIENT-NAME-TARO' ('str' object cannot be interpreted as an integer)"
)
SYNTAX_EN = 'invalid input syntax for type integer: "PHI-DIAGNOSIS-XYZ"'
UNIQUE_EN = "DETAIL:  Key (email)=(phi-victim@example.org) already exists."
NOTNULL_EN = "DETAIL:  Failing row contains (z@example.org, null, PHI-VALUE)."


@pytest.mark.parametrize(
    ("text", "secrets"),
    [
        (UNIQUE_JA, ["phi-victim@example.org"]),
        (NOTNULL_JA, ["z@example.org", "PHI-VALUE"]),
        (SYNTAX_JA, ["PHI-DIAGNOSIS-XYZ"]),
        (ARG_EN, ["PHI-PATIENT-NAME-TARO"]),
        (SYNTAX_EN, ["PHI-DIAGNOSIS-XYZ"]),
        (UNIQUE_EN, ["phi-victim@example.org"]),
        (NOTNULL_EN, ["z@example.org", "PHI-VALUE"]),
        ("could not convert string to float: 'PHI-LAB-VALUE'", ["PHI-LAB-VALUE"]),
        ("invalid literal for int() with base 10: 'PHI-ID-9'", ["PHI-ID-9"]),
    ],
)
def test_driver_echoed_values_are_masked(text: str, secrets: list[str]) -> None:
    out = redact_exception_text(text)
    for secret in secrets:
        assert secret not in out, (secret, out)


def test_error_kind_and_column_names_are_kept_for_debugging() -> None:
    out = redact_exception_text(UNIQUE_JA)
    assert "UniqueViolationError" in out
    assert "(email)" in out  # 列名は残す
    assert "pr_t_email_key" in out  # 制約名は残す
    assert "(Background on this error at:" in out
    out2 = redact_exception_text(NOTNULL_JA)
    assert 'リレーション"pr_t"の列"n"' in out2
    out3 = redact_exception_text(SYNTAX_JA)
    assert '"integer"型の入力構文が不正です' in out3
    out4 = redact_exception_text(ARG_EN)
    assert "invalid input for query argument $1" in out4
    assert "cannot be interpreted as an integer" in out4


def test_logging_text_uses_the_same_pipeline() -> None:
    assert "phi-victim@example.org" not in _redact_text(UNIQUE_JA)


def test_exc_msg_masks_but_classification_still_sees_the_raw_text() -> None:
    """evidence に出す文面は伏せるが、失敗の分類は伏せる前の文面で判定する。"""
    err = Exception('relation "labs" does not exist; DETAIL: Key (email)=(a@b.example) bad')
    assert "a@b.example" not in _exc_msg(err)
    assert "a@b.example" in _raw_exc_msg(err)
    assert classify_exception(err) == "schema_mismatch"


def test_exc_msg_never_returns_empty() -> None:
    assert _exc_msg(TimeoutError()) == "TimeoutError"


# --- [parameters: ...] の線形走査 ----------------------------------------------------------


def test_parameters_segment_that_is_not_a_bind_dump_is_kept() -> None:
    """束縛値の形（`(` `[` `{` で始まる）をしていない `[parameters: ...]` は、そのまま残す。"""
    text = "Model fit [parameters: lr=0.1] finished"
    assert redact_exception_text(text) == text


def test_parameters_dump_forms_are_masked() -> None:
    for dump in ("('a@x.example', 'b')", "{'p': 'a@x.example'}", "[('a@x.example',), ('c',)]"):
        out = redact_exception_text(
            f"boom\n[SQL: x]\n[parameters: {dump}]\n(Background on this error at: https://sqlalche.me/e/20/gkpj)"
        )
        assert "a@x.example" not in out, dump
        assert "(Background on this error at:" in out


def test_text_after_a_closed_parameters_segment_is_kept() -> None:
    out = redact_exception_text("err [parameters: ('x@y.example',)] and then more context")
    assert "x@y.example" not in out
    # 閉じて、後ろに文字が続く形は、閉じ括弧の位置を決められないので、残りも伏せる（安全側）
    assert "[parameters: ***REDACTED***]" in out


@pytest.mark.parametrize(
    "unit",
    [
        "[parameters: x ",
        "[parameters: (((( ",
        "[parameters: ('a', ",
        "] ] ] ",
        "@" * 50,
        "https://a:b@",
    ],
)
def test_adversarial_large_inputs_are_bounded_in_time(unit: str) -> None:
    text = unit * (3_000_000 // len(unit))  # 約 3 MB
    started = time.monotonic()
    out = redact_exception_text(text)
    elapsed = time.monotonic() - started
    assert elapsed < 2.0, (unit, elapsed)
    assert len(out) < 70_000  # 上限で切る


def test_the_filter_survives_a_huge_message() -> None:
    f = PHIRedactionFilter()
    record = logging.LogRecord("x", logging.ERROR, "p", 1, "[parameters: ('a'," * 200_000, (), None)
    started = time.monotonic()
    f.filter(record)
    assert time.monotonic() - started < 2.0


# --- URL ----------------------------------------------------------------------------------


def test_urls_in_text_lose_userinfo_and_query_secrets() -> None:
    out = redact_urls(
        "GET https://svc:pw@fhir.example/fhir/Patient?access_token=TKN&_count=5 -> 500"
    )
    assert "svc:pw" not in out and "TKN" not in out
    assert "fhir.example/fhir/Patient" in out
    assert "_count=5" in out  # 秘匿でないパラメータは残す


def test_userinfo_with_a_slash_in_the_password_is_best_effort_masked() -> None:
    out = redact_urls("failed https://user:p/ss@host.example/x")
    assert "p/ss" not in out and "host.example" in out


def test_a_legit_url_with_an_email_in_the_query_is_not_mangled() -> None:
    url = "https://example.com:8080/search?email=a@b.com"
    assert redact_urls(url) == url
    url2 = "https://example.com:8080?email=a@b.com"
    assert redact_urls(url2) == url2


def test_whole_url_mode_handles_scheme_less_userinfo() -> None:
    assert redact_urls("user:pw@host.example/fhir", whole_url=True) == "host.example/fhir"
    assert redact_urls("//user:pw@host.example/fhir", whole_url=True) == "host.example/fhir"
    # メールアドレスのような、コロンの無い形は触らない
    assert redact_urls("a@b.example", whole_url=True) == "a@b.example"


@pytest.mark.parametrize("name", ["access_token", "API_KEY", "Signature", "client_secret", "key"])
def test_sensitive_query_names_are_masked_case_insensitively(name: str) -> None:
    out = redact_urls(f"https://h.example/x?a=1&{name}=SECRETVAL&b=2")
    assert "SECRETVAL" not in out and "a=1" in out and "b=2" in out


def test_fhir_search_code_parameter_is_kept() -> None:
    """`code=`（LOINC など）は FHIR の検索の通常のパラメータで、秘匿値ではないので残す。"""
    out = redact_urls("https://h.example/fhir/Observation?code=http://loinc.org|6690-2&patient=P1")
    assert "6690-2" in out
