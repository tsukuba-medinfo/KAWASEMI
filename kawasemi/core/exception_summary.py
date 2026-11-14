"""例外の要約（evidence・trace・DB・API の応答・SSE へ出す文面）。

ドライバや外部ライブラリの例外は、文面に行や束縛の値（`DETAIL: Key (col)=(value)`、
`invalid input syntax for type integer: "value"`、`Token "x" is invalid` …）を載せうる。
**文面を正規表現で伏せる方式は、形の取りこぼしが必ず出る**ので採らない。出す文面は、構造から
取れる安全な部分だけに絞る（許可リスト方式）:

- 例外の型名
- SQLSTATE / ドライバのエラーコード（`23505` など）
- ドライバが構造化して持つ**名前**（制約名・列名・表名・スキーマ名。値ではない）
- HTTP クライアントの例外は、接続先の host（userinfo・クエリは落とす）
- **こちらで文面を書いた例外**（`kawasemi_authored = True` を持つ型）は、その文面

第三者の例外（ドライバ・httpx・標準ライブラリ）の `str(e)` は出さない。分類（`classify_exception`）は
生の文面を内部で使うが、結果は `FailureCode` の定数で、文面は外へ出ない。サーバのログには、
`logger.exception` の形で完全な例外が残る（そちらは `PHIRedactionFilter` がベストエフォートで伏せる）。
"""

from __future__ import annotations

import re
from typing import Any

from kawasemi.core.url_redaction import display_url, strip_userinfo

# 構造化された名前（制約名・列名など）として出してよい形。識別子は値を載せられない
# （長さと字種を絞ってあるので、行の値がここから漏れることはない）。
_IDENT = re.compile(r"[\w$.\-]{1,128}")
_NAME_ATTRS = ("constraint_name", "column_name", "table_name", "schema_name", "data_type_name")
_MAX_CHAIN = 6
# 文面を書いた（作者が文面を制御している）例外の、文面の長さの上限。
_MAX_AUTHORED_LEN = 1000


def _chain(e: BaseException) -> list[BaseException]:
    """`e` と、その原因（`orig`・`__cause__`・`__context__`）を、深さを限って集める。"""
    seen: list[BaseException] = []
    todo: list[BaseException] = [e]
    while todo and len(seen) < _MAX_CHAIN:
        cur = todo.pop(0)
        if any(cur is s for s in seen):
            continue
        seen.append(cur)
        orig = getattr(cur, "orig", None)
        if isinstance(orig, BaseException):
            todo.append(orig)
        for nxt in (cur.__cause__, cur.__context__):
            if nxt is not None:
                todo.append(nxt)
    return seen


def _sqlstate(chain: list[BaseException]) -> str | None:
    for exc in chain:
        for attr in ("sqlstate", "pgcode"):
            v = getattr(exc, attr, None)
            if isinstance(v, str) and re.fullmatch(r"[0-9A-Z]{5}", v):
                return v
    return None


def _names(chain: list[BaseException]) -> dict[str, str]:
    found: dict[str, str] = {}
    for exc in chain:
        holders: list[Any] = [exc, getattr(exc, "diag", None)]
        for holder in holders:
            if holder is None:
                continue
            for attr in _NAME_ATTRS:
                if attr in found:
                    continue
                v = getattr(holder, attr, None)
                if isinstance(v, str) and _IDENT.fullmatch(v):
                    found[attr.removesuffix("_name")] = v
    return found


def _sqlite_name(chain: list[BaseException]) -> str | None:
    for exc in chain:
        v = getattr(exc, "sqlite_errorname", None)
        if isinstance(v, str) and re.fullmatch(r"SQLITE_[A-Z_]{1,40}", v):
            return v
    return None


def _http_target(e: BaseException) -> str | None:
    """httpx の例外から、接続先（scheme://host:port/path）を取る。無ければ None。"""
    try:
        request = getattr(e, "request", None)
    except RuntimeError:  # httpx: request が未設定の例外
        return None
    url = getattr(request, "url", None)
    if url is None:
        return None
    try:
        return display_url(f"{url.scheme}://{url.netloc.decode('ascii', 'replace')}{url.path}")
    except Exception:
        return None


def _http_status(e: BaseException) -> int | None:
    """HTTP ステータス（整数）。`status_code` を持つ例外（ollama・openai 系）か、`response.status_code`。"""
    for holder in (e, getattr(e, "response", None)):
        v = getattr(holder, "status_code", None)
        if isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 599:
            return v
    return None


def is_authored(e: BaseException) -> bool:
    """こちらで文面を書いた例外か（型に `kawasemi_authored = True` がある）。"""
    return bool(getattr(type(e), "kawasemi_authored", False))


def safe_message(e: BaseException) -> str:
    """表に出してよい文面（型名を付けない）。

    こちらが書いた例外はその文面（URL の userinfo だけ落とす）、第三者の例外は `summarize_exception` の
    構造化した要約。
    """
    if is_authored(e):
        msg = strip_userinfo(str(e))
        if len(msg) > _MAX_AUTHORED_LEN:
            msg = msg[:_MAX_AUTHORED_LEN] + "…"
        return msg or type(e).__name__
    return summarize_exception(e)


def summarize_exception(e: BaseException) -> str:
    """evidence・DB・API・SSE へ出す、例外の要約（型名＋構造化した安全な属性）。

    ドライバの文面（DETAIL・行の値・束縛の値）は含めない。
    """
    # 再試行の包み（`RetriesExhausted`）は、原因を要約する。
    cause = getattr(e, "__cause__", None)
    if type(e).__name__ == "RetriesExhausted" and isinstance(cause, BaseException):
        return summarize_exception(cause)

    name = type(e).__name__
    if is_authored(e):
        msg = safe_message(e)
        return f"{name}: {msg}" if msg and msg != name else name

    chain = _chain(e)
    parts: list[str] = []
    state = _sqlstate(chain)
    if state:
        parts.append(f"sqlstate={state}")
    sqlite_name = _sqlite_name(chain)
    if sqlite_name:
        parts.append(f"sqlite={sqlite_name}")
    for key, value in _names(chain).items():
        parts.append(f"{key}={value}")
    status = _http_status(e)
    if status is not None:
        parts.append(f"status={status}")
    target = _http_target(e)
    if target:
        parts.append(f"target={target}")
    # 根本原因の型名（SQLAlchemy の包みの中身。`UniqueViolationError` など）
    for exc in chain[1:]:
        inner = type(exc).__name__
        if inner != name and inner not in ("Exception", "BaseException"):
            parts.insert(0, f"cause={inner}")
            break
    return f"{name} [{', '.join(parts)}]" if parts else name
