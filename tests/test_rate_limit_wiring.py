"""レート制限の設定と実装が結線されていること。

守る対象は2つある。

- **上限値が `settings` から読まれること。** 以前は `web/auth_routes.py` に
  `@limiter.limit("5/minute")` と直接書かれており、`.env` の `RATE_LIMIT_LOGIN` を
  変えても何も起きなかった。`core/config.py` のアカウントロックの注意書き
  （単一IPからのロックはこの値に依存する）は、そのあいだ嘘だった
- **Limiter がプロセスに1つであること。** 以前は `api/main.py` と
  `web/auth_routes.py` がそれぞれ `Limiter` を作っており、IP 単位のカウンタが
  2つに割れていた。429 を組み立てる `app.state.limiter` に載っているのは前者だけで、
  ログインの制限は別の帳簿で数えられていた

`tests/test_security.py::TestRateLimiting::test_login_rate_limiting` は既定の
5回/分を見ているが、**値をハードコードに戻しても通る**（既定値が同じなので）。
ここでは既定と違う値を設定して、その値で 429 になることを見る。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from kawasemi.api.main import app
from kawasemi.core.config import settings
from tests.test_security import extract_form_csrf_token


def test_the_app_and_the_auth_routes_share_one_limiter() -> None:
    """`app.state.limiter` と `web/auth_routes` の limiter が同一オブジェクトであること。"""
    from kawasemi.core.rate_limit import limiter as shared
    from kawasemi.web import auth_routes

    assert auth_routes.limiter is shared
    assert app.state.limiter is shared


def test_the_login_limit_reads_the_setting() -> None:
    """上限を返す関数が `settings` を読むこと（デコレータ評価時に固定しない）。"""
    from kawasemi.core.rate_limit import login_rate_limit

    assert login_rate_limit() == settings.rate_limit_login


@pytest.mark.asyncio
async def test_changing_the_setting_changes_when_429_is_returned(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**設定を 2/minute にすると、3回目で 429 になること。**

    既定は 5/minute なので、ハードコードに戻すとこのテストは落ちる。
    """
    monkeypatch.setattr(settings, "rate_limit_login", "2/minute")

    statuses: list[int] = []
    for _ in range(3):
        page = await client.get("/login")
        token = extract_form_csrf_token(page.text)
        response = await client.post(
            "/login",
            data={"username": "invalid_user", "password": "invalid_pass", "csrf_token": token},
            headers={"X-CSRF-Token": token},
        )
        statuses.append(response.status_code)

    assert statuses[0] in (200, 303), statuses
    assert statuses[1] in (200, 303), statuses
    assert statuses[2] == 429, f"2/minute を設定したのに3回目が通った: {statuses}"
