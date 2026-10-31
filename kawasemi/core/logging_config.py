"""ロギング設定"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from kawasemi.core.config import settings

# PHI/秘匿値のマスク対象キー（小文字・完全一致で構造化フィールドを、
# "key=value"/"key: value"/'"key":"value"' 形式でテキストをマスクする）。
_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "token",
    "secret",
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "session",
    "subject",
    "answers",
    "patient_id",
    "patient_name",
    "prefill_data",
    "evidence",
)
_REDACTED = "***REDACTED***"

# key=val / key: val / "key":"val" 形式（キー直後に区切り）を捕捉する。
# 値側の停止文字に & を含め、クエリ文字列の後続パラメータまで巻き込まないようにする。
_KV_PATTERN = re.compile(
    r'(?i)(["\']?(?:' + "|".join(re.escape(k) for k in _SENSITIVE_KEYS) + r')["\']?\s*[:=]\s*)'
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&)}\]]+)"
)


def _redact_text(text: str) -> str:
    return _KV_PATTERN.sub(lambda m: m.group(1) + _REDACTED, text)


def _redact_value(value: object) -> object:
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, dict):
        return {
            k: (_REDACTED if str(k).lower() in _SENSITIVE_KEYS else _redact_value(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v) for v in value)
    return value


class PHIRedactionFilter(logging.Filter):
    """秘匿値・PHI をログ出力前にマスクする（JSON/テキスト両フォーマッタ共通）。

    フォーマット確定前の filter 段で record.msg を確定メッセージへ畳み込み、args を
    消費してから正規表現マスクをかける。extra 経由の構造化フィールドもキー名でマスクする。
    「うっかり漏れ」への二重防御であり、値をログに出さない一次対策（呼び出し側）を代替しない。
    """

    _RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            try:
                message = record.getMessage()
            except Exception:
                message = str(record.msg)
            record.msg = _redact_text(message)
            record.args = ()

            # 例外トレースバックもマスクする。SQLAlchemy 例外は [parameters: ...] に
            # バインド値（=PHI）を含みうるため、確定文字列化してから伏せる（主要漏洩ベクタ）。
            if record.exc_info:
                if not record.exc_text:
                    record.exc_text = logging.Formatter().formatException(record.exc_info)
                record.exc_info = None
            if record.exc_text:
                record.exc_text = _redact_text(record.exc_text)

            extra = getattr(record, "extra", None)
            if isinstance(extra, dict):
                record.extra = _redact_value(extra)

            for key in list(record.__dict__):
                if key in self._RESERVED or key == "extra":
                    continue
                try:
                    if key.lower() in _SENSITIVE_KEYS:
                        record.__dict__[key] = _REDACTED
                    else:
                        record.__dict__[key] = _redact_value(record.__dict__[key])
                except Exception:
                    # 個別属性のマスク失敗でログ経路全体を巻き込まない（保守的に握りつぶす）。
                    continue
        except Exception:
            # フィルタ自体の失敗でアプリのログ呼び出しを壊さない。
            return True
        return True


class JSONFormatter(logging.Formatter):
    """JSON形式のログフォーマッター"""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # エラー情報の追加（PHIRedactionFilter が exc_text を確定・マスク済みなら優先）。
        if record.exc_text:
            log_data["exception"] = record.exc_text
        elif record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # 追加フィールドの追加
        if hasattr(record, "extra"):
            log_data.update(record.extra)

        return json.dumps(log_data, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """テキスト形式のログフォーマッター"""

    def __init__(self) -> None:
        fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        super().__init__(fmt=fmt, datefmt="%Y-%m-%d %H:%M:%S")


def setup_logging() -> None:
    """ロギングのセットアップ"""
    # ルートロガーの取得
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, settings.log_level))

    # 既存のハンドラーをクリア
    logger.handlers.clear()

    # コンソールハンドラーの作成
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, settings.log_level))

    # フォーマッターの設定
    formatter: logging.Formatter
    if settings.log_format == "json":
        formatter = JSONFormatter()
    else:
        formatter = TextFormatter()

    console_handler.setFormatter(formatter)
    # PHI/秘匿値のマスク。root へ伝播し独自ハンドラを持たないロガー
    # （アプリ全般・sqlalchemy.engine 等）の全レコードに適用される。独自ハンドラ＋
    # propagate=False の uvicorn 系はこの経路を通らない点に留意（アクセスログは別管理）。
    console_handler.addFilter(PHIRedactionFilter())
    logger.addHandler(console_handler)

    # Uvicorn や SQLAlchemy のログレベル調整
    logging.getLogger("uvicorn").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(
        logging.INFO if settings.debug else logging.WARNING
    )


# アプリケーション起動時にロギングをセットアップ
def get_logger(name: str) -> logging.Logger:
    """
    ロガーの取得

    Args:
        name: ロガー名（通常は __name__ を使用）

    Returns:
        Logger: ロガーインスタンス
    """
    return logging.getLogger(name)
