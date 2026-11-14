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


# 伏せ処理に掛ける文字数の上限。これを超える分は捨てる（巨大な文字列で処理時間が伸びるのを
# 防ぐ。捨てるので、上限の先にある値が漏れることもない）。
_MAX_REDACT_LEN = 64 * 1024
_TRUNCATED = "…[truncated]"

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


# URL の資格情報。`scheme://user:pw@host` の userinfo と、クエリ文字列の秘匿値。運用者が URL に
# 埋め込んだ資格情報を、どの呼び出し箇所のログ・例外の文面からも伏せる（個別の呼び出し側が
# 表示用 URL を作る一次対策の、二重防御）。
_URL_RE = re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s\'\"<>]+")
# 標準の userinfo（`/` `?` `#` を含まない）。パスワードに `/` を含む不正な形は、ポート番号に
# 見える `数字/` で始まるもの（`host:8080/x?e=a@b`）を除いて、できる範囲で扱う。
_USERINFO = re.compile(
    r"^([a-z][a-z0-9+.\-]*://)(?:[^/?#\s]*@|[^\s/@?#]+:(?!\d+[/?#])[^\s@]*?/[^\s@]*@)", re.I
)
_SENSITIVE_QUERY_NAMES = (
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "api_key",
    "apikey",
    "api-key",
    "key",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "pwd",
    "sig",
    "signature",
    "auth",
    "authorization",
    "auth_code",
    "authorization_code",
    "oauth_code",
    "sas",
)
_QUERY_SECRET = re.compile(
    r"(?i)([?&;](?:" + "|".join(re.escape(n) for n in _SENSITIVE_QUERY_NAMES) + r")=)[^&#\s]*"
)
# `user:pw@host/...` / `//user:pw@host/...`（スキームなし）。文字列全体が URL のときだけ使う。
_SCHEMELESS_USERINFO = re.compile(r"^(?://)?[^/?#\s@]+:[^/?#\s]*@")


def _redact_one_url(url: str) -> str:
    url = _USERINFO.sub(lambda m: m.group(1) + "", url)
    return _QUERY_SECRET.sub(lambda m: m.group(1) + _REDACTED, url)


def redact_urls(text: str, *, whole_url: bool = False) -> str:
    """文字列中の URL から資格情報（userinfo・クエリの秘匿値）を取り除く。

    whole_url=True のときは、文字列全体を 1 つの URL として扱う（スキームなしの
    `user:pw@host/x` や、相対 URL のクエリの秘匿値も取り除く）。
    """
    text = _URL_RE.sub(lambda m: _redact_one_url(m.group(0)), text)
    if whole_url:
        text = _SCHEMELESS_USERINFO.sub("", text)
        text = _QUERY_SECRET.sub(lambda m: m.group(1) + _REDACTED, text)
    return text


# PostgreSQL・asyncpg・SQLite などのドライバが、エラー文に行や束縛の値を載せる形。
# hide_parameters では隠れない（[parameters: ...] の外にある）ので、値だけを伏せ、
# エラーの種類・列名・SQLSTATE は残す（原因の切り分けに要る）。ベストエフォートで、
# 珍しい形のドライバ文面まで保証するものではない（DB ロールの権限が境界）。
_DRIVER_ECHOES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Key (email)=(a@b) already exists. / キー (email)=(a@b) はすでに存在します。
    (
        re.compile(r"((?:Key|キー)\s*\([^()]*\)\s*=\s*)\(.*?\)(?=\s|[.。]|$)", re.IGNORECASE),
        r"\1(" + _REDACTED + ")",
    ),
    # Failing row contains (1, x, ...). / 失敗した行は(1, x, ...)を含みます。
    (
        re.compile(r"(Failing row contains\s*)\(.*\)", re.IGNORECASE),
        r"\1(" + _REDACTED + ")",
    ),
    (re.compile(r"(失敗した行は\s*)\(.*\)"), r"\1(" + _REDACTED + ")"),
    # asyncpg: invalid input for query argument $1: 'value' (...)
    (
        re.compile(
            r"(invalid input for query argument\s*\$\d+:\s*)('(?:[^'\\]|\\.)*'|\"[^\"]*\"|\S+)",
            re.IGNORECASE,
        ),
        r"\1'" + _REDACTED + "'",
    ),
    # invalid input syntax for type integer: "value" / "integer"型の入力構文が不正です: "value"
    (
        re.compile(r"(invalid input syntax for[^:\n]*:\s*)\"[^\"\n]*\"", re.IGNORECASE),
        r'\1"' + _REDACTED + '"',
    ),
    (re.compile(r"(入力構文が不正です:\s*)\"[^\"\n]*\""), r'\1"' + _REDACTED + '"'),
    # Python/SQLite の型変換エラー。
    (
        re.compile(
            r"(could not convert string to \w+:\s*|invalid literal for \w+\(\) with base \d+:\s*)"
            r"'[^'\n]*'",
            re.IGNORECASE,
        ),
        r"\1'" + _REDACTED + "'",
    ),
)


def redact_exception_text(text: str) -> str:
    """例外・ログの文面から、秘匿値・PHI・資格情報をできる範囲で取り除く。

    取り除くもの: SQLAlchemy の `[parameters: ...]`、ドライバが載せる行・束縛の値
    （`DETAIL: Key (col)=(value)` など）、URL の userinfo とクエリの秘匿値、機微なキーの値。
    ベストエフォートであり、珍しいドライバ文面まで保証しない。境界は DB ロールの権限である。
    evidence・DB・API の応答に載せる例外文字列と、ログの両方で使う。
    """
    if len(text) > _MAX_REDACT_LEN:
        text = text[:_MAX_REDACT_LEN] + _TRUNCATED
    text = redact_urls(text)
    text = _strip_sqlalchemy_parameters(text)
    for pattern, repl in _DRIVER_ECHOES:
        text = pattern.sub(repl, text)
    return _KV_PATTERN.sub(lambda m: m.group(1) + _REDACTED, text)


def _redact_text(text: str) -> str:
    return redact_exception_text(text)


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
