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
