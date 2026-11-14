"""外部DBが返す一時的なエラーの再試行。

**外部DB（DWH など）は一時的なエラーを返すことがある。** 負荷や接続の切断で一度失敗した
クエリも、少し待って投げ直せば通ることが多い。一般的な一時エラーの扱いとして、
一定時間待って上限回数まで投げ直す。

待機は 10 秒（定数 `RETRY_WAIT_SECONDS`。環境に合わせて変えてよい）。回数に上限を置くのは
投げ直しが必ず通るとは限らないためで、上限が無いと落ちているサーバへ延々と投げ続ける。

- **黙って再試行しない。** 何回目で成功したかを呼び出し側が trace へ残す。
  「一発で取れた項目」と「3回目で取れた項目」を後から区別できなくなるので
- **上限まで失敗したら例外をそのまま上げる。** 握り潰すと「一時的なエラーが続いた」と
  「表が存在しない」を区別できなくなる
- **やり直しても結果が変わらない失敗は投げ直さない**。読み取り専用の違反（書き込もうとした）、
  文タイムアウトによる取り消し、権限・構文・データの誤りは、待っても通らない。投げ直すと、
  文タイムアウト（既定60秒）× 待機 × 回数で接続を長く握り続ける。これらは1回目で
  `RetriesExhausted(attempts=1)` として返す（原因は `__cause__`）。接続断・過負荷・
  デッドロック・直列化の失敗のような一時的な失敗だけを投げ直す
- 対象は施設の外部DBだけ。自アプリDB（`sql_extract`）は再試行しない。失敗した
  セッションを同じトランザクションのまま投げ直すと別の失敗を招く
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from sqlalchemy.exc import SQLAlchemyError

from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

RETRY_WAIT_SECONDS = 10.0
RETRY_MAX_ATTEMPTS = 3


# 投げ直さない SQLSTATE（PostgreSQL）。完全一致と、先頭2桁のクラス。
_NON_RETRYABLE_STATES = frozenset(
    {"25006", "57014"}
)  # 読み取り専用の違反、取り消し（タイムアウト）
_NON_RETRYABLE_CLASSES = frozenset({"22", "23", "0A", "42"})  # データ・整合性・未対応・構文/権限


def is_deterministic_failure(e: BaseException) -> bool:
    """投げ直しても同じ結果になる失敗か（読み取り専用の違反・タイムアウト・権限や構文の誤り）。

    判定できないものは False（＝従来どおり投げ直す）。一時的な失敗を取りこぼさないため。
    """
    orig = getattr(e, "orig", None) or e
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if isinstance(state, str) and state:
        return state in _NON_RETRYABLE_STATES or state[:2] in _NON_RETRYABLE_CLASSES
    # SQLite は SQLSTATE を持たない。読み取り専用の違反だけを文言で見分ける
    return "readonly database" in str(orig).lower() or "read-only" in str(orig).lower()


class RetriesExhausted(Exception):  # noqa: N818
    """上限まで失敗した。元の例外は `__cause__`、試行回数は `attempts`。"""

    def __init__(self, attempts: int, cause: Exception) -> None:
        super().__init__(str(cause))
        self.attempts = attempts
        self.__cause__ = cause


async def with_retry[T](
    label: str,
    call: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[T, int]:
    """一時的なエラーを待って投げ直す。戻り値は (結果, 何回目で成功したか)。

    上限まで失敗したら `RetriesExhausted`（元の例外を `__cause__` に持つ）を上げる。
    `sleep` を差し替え可能にしてあるのはテストのためで、待機の実時間を持ち込まない。
    """
    for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
        try:
            return await call(), attempt
        except SQLAlchemyError as e:
            if is_deterministic_failure(e):
                raise RetriesExhausted(attempt, e) from e
            if attempt == RETRY_MAX_ATTEMPTS:
                raise RetriesExhausted(attempt, e) from e
            logger.warning(
                f"{label}: {attempt}回目が失敗した（{type(e).__name__}）。"
                f"{RETRY_WAIT_SECONDS:.0f}秒待って再試行する（上限{RETRY_MAX_ATTEMPTS}回）"
            )
            await sleep(RETRY_WAIT_SECONDS)
    raise AssertionError("unreachable")
