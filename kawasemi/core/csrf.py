"""CSRFトークンのセッション内安定化ヘルパー

fastapi-csrf-protect の `generate_csrf_tokens()` は呼ぶたびに新しいランダムトークンを生成し、
`set_csrf_cookie()` で cookie を上書きする。各 GET ページでこれを行うと、同一セッションでも
ページを開くたびに cookie 内トークンが変わってしまう。

AI入力支援の画面は AJAX でトークンを使い回すため、ページ読込後に別の GET（別タブ・
バックグラウンド取得など）が走ると、開いたままのページが持つトークンが無効化され、POST が
CSRF エラーになる。これを避けるため、有効な CSRF cookie が既にある場合はそれを再利用し、
無い場合のみ新規生成する。
"""

from __future__ import annotations

from fastapi import Request
from fastapi_csrf_protect import CsrfProtect
from itsdangerous import BadData, URLSafeTimedSerializer

from kawasemi.core.config import settings

# fastapi-csrf-protect が署名に使う固定 salt（ライブラリ内部値）
_CSRF_SALT = "fastapi-csrf-token"
_DEFAULT_COOKIE_NAME = "kawasemi_csrf_token"


def raw_csrf_token(
    request: Request,
    *,
    secret_key: str,
    cookie_key: str = _DEFAULT_COOKIE_NAME,
    max_age: int | None = None,
) -> str | None:
    """既存の CSRF cookie から raw（未署名）トークンを取り出す。

    cookie が無い・署名不正・期限切れの場合は None を返す。
    `max_age=None` のときは期限チェックを行わない（最終的な鮮度検証は
    POST 時の `validate_csrf` に委ねる）。
    """
    existing = request.cookies.get(cookie_key)
    if not existing:
        return None
    serializer = URLSafeTimedSerializer(secret_key, salt=_CSRF_SALT)
    try:
        raw: str = serializer.loads(existing, max_age=max_age)
        return raw
    except BadData:
        return None


def stable_csrf_tokens(request: Request, csrf_protect: CsrfProtect) -> tuple[str, str | None]:
    """セッション内で安定した CSRF トークンを返す。

    既存の有効な CSRF cookie があればその raw トークンを再利用し、cookie は据え置く。
    無い／不正な場合のみ新規トークンを生成する。

    Returns:
        (csrf_token, signed_token):
            - csrf_token: テンプレートに渡す raw トークン
            - signed_token: 新規生成した場合の署名済みトークン。再利用時は None
              （None のとき呼び出し側は `set_csrf_cookie` を呼ばない）
    """
    secret_key = getattr(csrf_protect, "_secret_key", None) or settings.csrf_secret_key
    cookie_key = getattr(csrf_protect, "_cookie_key", None) or _DEFAULT_COOKIE_NAME
    max_age = getattr(csrf_protect, "_max_age", None)

    raw_token = raw_csrf_token(
        request, secret_key=secret_key, cookie_key=cookie_key, max_age=max_age
    )
    if raw_token is not None:
        return raw_token, None

    # cookie 無し／署名不正／期限切れは新規生成にフォールバック
    return csrf_protect.generate_csrf_tokens()
