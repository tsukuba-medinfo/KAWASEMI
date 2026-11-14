"""PHI レダクションフィルタのユニットテスト。"""

import logging
import sys

from kawasemi.core.logging_config import PHIRedactionFilter, _redact_text, _redact_value


def test_redact_text_masks_sensitive_kv() -> None:
    """patient_id / token 等のキー直後の値を伏せる（キー名は残す）。"""
    out = _redact_text("patient_id=PAT-001 questionnaire_id=5")
    assert "PAT-001" not in out
    assert "***REDACTED***" in out
    assert "questionnaire_id=5" in out  # 非機微は保持

    out2 = _redact_text('token: "abc123"')
    assert "abc123" not in out2
    assert "token" in out2


def test_redact_text_keeps_non_sensitive() -> None:
    assert _redact_text("questionnaire_id=5 status=ok") == "questionnaire_id=5 status=ok"


def test_redact_value_masks_dict_keys() -> None:
    out = _redact_value({"patient_id": "P1", "answers": {"q1": "x"}, "count": 3})
    assert out == {"patient_id": "***REDACTED***", "answers": "***REDACTED***", "count": 3}


def test_filter_redacts_message() -> None:
    f = PHIRedactionFilter()
    record = logging.LogRecord("x", logging.INFO, "p", 1, "fetched patient_id=UC-0009 ok", (), None)
    f.filter(record)
    assert "UC-0009" not in record.getMessage()


def test_filter_redacts_exception_parameters() -> None:
    """SQLAlchemy 例外の [parameters: ...] 等、トレースバック中の PHI をマスクする。"""
    f = PHIRedactionFilter()
    try:
        raise ValueError("bind failed token=abc123secret patient_id=UC-0009")
    except ValueError:
        record = logging.LogRecord("x", logging.ERROR, "p", 1, "boom", (), sys.exc_info())
    f.filter(record)
    assert record.exc_text is not None
    assert "abc123secret" not in record.exc_text
    assert "UC-0009" not in record.exc_text


# --- 再スキャン F4: SQLAlchemy の [parameters: ...] の束縛値 -------------------------

_HASH = "$2b$12$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ0123456"


def _real_integrity_error(hide_parameters: bool) -> Exception:
    """実際の SQLAlchemy の IntegrityError（束縛値にハッシュ・メール・患者 ID を含む）を作る。"""
    import sqlalchemy as sa

    engine = sa.create_engine("sqlite://", hide_parameters=hide_parameters)
    with engine.connect() as conn:
        conn.execute(sa.text("CREATE TABLE u (name TEXT UNIQUE, email TEXT, h TEXT, pid TEXT)"))
        ins = sa.text("INSERT INTO u (name, email, h, pid) VALUES (:n, :e, :h, :p)")
        params = {"n": "alice", "e": "alice@example.org", "h": _HASH, "p": "PAT-777"}
        conn.execute(ins, params)
        try:
            conn.execute(ins, params)
        except sa.exc.IntegrityError as exc:
            return exc
    raise AssertionError("IntegrityError が起きなかった")


def test_real_dbapi_error_text_has_parameters_when_not_hidden() -> None:
    """前提の確認: hide_parameters なしの実例外文字列には束縛値が入る（フィルタが要る理由）。"""
    assert "alice@example.org" in str(_real_integrity_error(False))


def test_filter_strips_bind_values_from_a_real_dbapi_error() -> None:
    """hide_parameters を忘れたエンジンでも、フィルタが束縛値（ハッシュ・メール・患者 ID）を伏せる。"""
    f = PHIRedactionFilter()
    exc = _real_integrity_error(False)
    record = logging.LogRecord(
        "x", logging.ERROR, "p", 1, f"db failed: {exc}", (), (type(exc), exc, exc.__traceback__)
    )
    f.filter(record)
    shown = record.getMessage() + (record.exc_text or "")
    for secret in (_HASH, "alice@example.org", "PAT-777"):
        assert secret not in shown, secret


def test_hidden_parameters_engine_text_has_no_bind_values() -> None:
    assert "alice@example.org" not in str(_real_integrity_error(True))


def test_filter_strips_parameters_with_brackets_and_newlines_in_values() -> None:
    text = (
        "(sqlite3.IntegrityError) boom\n[SQL: INSERT ...]\n"
        "[parameters: ('a]b', 'line1\nline2', '" + _HASH + "')]\n"
        "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
    )
    out = _redact_text(text)
    assert _HASH not in out and "line2" not in out and "a]b" not in out
    assert "(Background on this error at:" in out


def test_truncated_parameters_segment_is_redacted_to_the_end() -> None:
    out = _redact_text("err [parameters: ('x@example.org', '" + _HASH)
    assert "x@example.org" not in out and _HASH not in out


def test_password_hash_and_jti_keys_are_masked() -> None:
    out = _redact_text(f"password_hash={_HASH} jti=abc-123-def")
    assert _HASH not in out and "abc-123-def" not in out


def test_engines_hide_parameters() -> None:
    """アプリの DB エンジンは、例外文字列に束縛値を入れない設定で作られている。"""
    from kawasemi.core.database import engine

    assert engine.sync_engine.hide_parameters is True


def test_setup_logging_attaches_the_filter_to_uvicorn_handlers() -> None:
    """uvicorn は独自のハンドラ＋propagate=False で、ルートの伏せ字を通らない。そのハンドラにも付ける。

    未処理の例外のトレースバック（束縛値を含みうる）は `uvicorn.error` に出る。
    """
    import io

    from kawasemi.core.logging_config import attach_redaction_to_uvicorn

    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(logging.Formatter("%(message)s"))
    uv = logging.getLogger("uvicorn")
    old_handlers, old_propagate = list(uv.handlers), uv.propagate
    uv.handlers[:] = [handler]
    uv.propagate = False
    try:
        attach_redaction_to_uvicorn()
        attach_redaction_to_uvicorn()  # 2 回呼んでも二重に付かない
        assert sum(isinstance(f, PHIRedactionFilter) for f in handler.filters) == 1
        try:
            raise RuntimeError("boom\n[parameters: ('phi@example.org',)]")
        except RuntimeError:
            logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=True)
    finally:
        uv.handlers[:] = old_handlers
        uv.propagate = old_propagate
    assert "phi@example.org" not in sink.getvalue()
    assert "Exception in ASGI application" in sink.getvalue()
