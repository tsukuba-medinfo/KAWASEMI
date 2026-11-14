"""ロギング設定"""

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

from kawasemi.core.config import settings
from kawasemi.core.url_redaction import MAX_TEXT_LEN, TRUNCATED, strip_userinfo

# PHI/秘匿値のマスク対象キー（小文字・完全一致で構造化フィールドを、
# "key=value"/"key: value"/'"key":"value"' 形式でテキストをマスクする）。
_SENSITIVE_KEYS = (
    "password",
    "password_hash",
    "passwd",
    "token",
    "jti",
    "refresh_token",
    "access_token",
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


# SQLAlchemy の DBAPIError 文字列にある "[parameters: (...)]" に続く束縛値。
# 位置指定のタプルにはキーが無く、キー名の照合では伏せられないので、セグメントごと取り除く。
# 正規表現の再試行（開始位置ごとに末尾まで走査する二次時間）を避けるため、手で一度だけ走査する。
_PARAMETERS_OPEN = re.compile(r"\[parameters:\s*(?=[(\[{])", re.IGNORECASE)
_BACKGROUND = "(Background on this error"


def _strip_sqlalchemy_parameters(text: str) -> str:
    """`[parameters: (...)]` の束縛値のセグメントを伏せる（線形時間）。

    値の中に ']' や改行があっても取りこぼさないよう、次の "(Background on this error"
    か文字列の終わりまでを範囲とし、範囲の最後が ']' ならそこまで、無ければ（途中で切れた
    文字列）範囲の残り全体を伏せる。`[parameters: lr=0.1]` のように、束縛値の形（`(` `[` `{`
    で始まる）をしていないものは、そのまま残す。
    """
    out: list[str] = []
    pos = 0
    while True:
        m = _PARAMETERS_OPEN.search(text, pos)
        if m is None:
            break
        region_end = text.find(_BACKGROUND, m.end())
        if region_end == -1:
            region_end = len(text)
        stop = region_end
        while stop > m.end() and text[stop - 1].isspace():
            stop -= 1
        end = stop if stop > m.end() and text[stop - 1] == "]" else region_end
        out.append(text[pos : m.start()])
        out.append("[parameters: " + _REDACTED + "]")
        pos = end
    out.append(text[pos:])
    return "".join(out)


def redact_log_text(text: str) -> str:
    """ログの文面から、秘匿値・PHI・資格情報を**ベストエフォートで**取り除く（線形時間）。

    取り除くもの: SQLAlchemy の `[parameters: ...]`、URL の userinfo、機微なキーの値
    （`token=...` など）。**ドライバが文面に載せる行の値（`DETAIL: Key (col)=(value)`）は
    取り除かない**: その形は多様で、パターンの列挙は必ず取りこぼすため、evidence・DB・API の
    応答へは構造化した要約（`summarize_exception`）だけを出し、生の文面はログにだけ残す。
    ログは機微な情報として扱う（アクセス制御・保存期間。docs/DEPLOYMENT.md）。
    掛ける長さは `MAX_TEXT_LEN` までで、超えた分は捨てる。
    """
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + TRUNCATED
    text = strip_userinfo(text)
    text = _strip_sqlalchemy_parameters(text)
    return _KV_PATTERN.sub(lambda m: m.group(1) + _REDACTED, text)


def _redact_text(text: str) -> str:
    return redact_log_text(text)


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


def attach_redaction_to_uvicorn() -> None:
    """uvicorn のハンドラ（`uvicorn`・`uvicorn.error`・`uvicorn.access`）に伏せ字フィルタを付ける。"""
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        for handler in logging.getLogger(name).handlers:
            if not any(isinstance(f, PHIRedactionFilter) for f in handler.filters):
                handler.addFilter(PHIRedactionFilter())


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
    # （アプリ全般・sqlalchemy.engine 等）の全レコードに適用される。
    console_handler.addFilter(PHIRedactionFilter())
    logger.addHandler(console_handler)
    # uvicorn は独自のハンドラ＋propagate=False なので、上の経路を通らない（未処理の例外の
    # トレースバックが `uvicorn.error` に出る）。uvicorn は、アプリを読み込む前にロギングを
    # 設定するので、ここで、そのハンドラにも同じフィルタを付ける。
    attach_redaction_to_uvicorn()

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
