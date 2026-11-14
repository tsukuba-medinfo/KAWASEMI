"""レート制限の Limiter を1つに集約する。

**Limiter はプロセスに1つでなければならない。** 以前は `api/main.py` と
`web/auth_routes.py` がそれぞれ `Limiter(key_func=get_remote_address)` を作っており、
IP 単位のカウンタが2つに割れていた。`app.state.limiter`（429 応答を組み立てる側）に
載っているのは前者だけなので、ログインの制限だけが別の帳簿で数えられていた。

**上限値は設定から読む。** 以前は `web/auth_routes.py` に `"5/minute"` が直接書かれて
おり、`.env` の `RATE_LIMIT_LOGIN` を変えても何も起きなかった（設定できるという嘘）。
`core/config.py` のアカウントロックの注意書きは、この値が効くことを前提に書かれている。
"""

from __future__ import annotations

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from kawasemi.core.config import settings
from kawasemi.core.login_throttle import aggregate_ip


def client_key(request: Request) -> str:
    """レート制限の単位。IPv6 は /64 にまとめる（アドレスを変えるだけで制限を回避させない）。"""
    return aggregate_ip(get_remote_address(request))


# アプリ全体で共有する唯一の Limiter。`api/main.py` が `app.state.limiter` に載せ、
# 各ルータはここから import して使う（`web/auth_routes.py` など）。
limiter = Limiter(key_func=client_key)


def login_rate_limit() -> str:
    """ログインの上限（`RATE_LIMIT_LOGIN`）。

    文字列ではなく**関数**を `@limiter.limit()` に渡すのは、slowapi が引数なしの
    callable をリクエストのたびに呼ぶためである。デコレータの評価時に値を固定すると、
    テストや運用で `settings` を差し替えても効かない。
    """
    return settings.rate_limit_login
