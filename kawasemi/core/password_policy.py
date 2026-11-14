"""パスワードの方針（管理者がアカウントを作る・パスワードを変えるときの入口の検査）。

ログイン画面の `minlength` は HTML の属性にすぎず、サーバーは何も検査していなかった。
管理画面（`/admin/users`）と CLI（`create_admin`）が、保存の前にこの検査を通す。

- 長さは `settings.password_min_length`（既定 12）以上。bcrypt は先頭 72 バイトしか見ないので、
  72 バイトを超える部分は効かない（`core/auth.py::_bcrypt_bytes`）。上限は設けないが、
  最小の長さは**文字数**で数える（日本語のパスフレーズも通す）。
- ありふれた弱いパスワード（下の拒否リスト）、同じ文字だけの並び、ユーザー名と同じものは拒否する。
  拒否リストは小さな下限であって、強さの保証ではない（長さが主な防御である）。
- デモ・E2E の自動生成パスワード（`core/demo_secrets.py`）は、この検査を通らない。
  CLI や管理画面から入力された値だけが対象である。
"""

from __future__ import annotations

from kawasemi.core.config import settings

# ありふれたパスワードの一部（小文字で比較する）。長さの下限を超える形のものを中心に挙げる。
_DENY = frozenset(
    {
        "password1234",
        "password12345",
        "passwordpassword",
        "password123456",
        "123456789012",
        "1234567890123",
        "12345678901234",
        "qwertyuiopas",
        "qwertyuiop12",
        "qwerty123456",
        "abcdefghijkl",
        "abcd12345678",
        "iloveyou1234",
        "administrator",
        "admin1234567",
        "admin12345678",
        "adminadminadmin",
        "changeme1234",
        "changemechangeme",
        "welcome12345",
        "letmein12345",
        "kawasemi1234",
        "kawasemikawasemi",
    }
)


def password_policy_error(password: str, *, username: str | None = None) -> str | None:
    """方針に合わなければ、利用者に見せる理由を返す（合えば None）。"""
    minimum = settings.password_min_length
    if len(password) < minimum:
        return f"パスワードは{minimum}文字以上にしてください"
    lowered = password.lower()
    if lowered in _DENY:
        return "ありふれたパスワードは使えません。別のものにしてください"
    if len(set(password)) == 1:
        return "同じ文字だけのパスワードは使えません"
    if username and lowered == username.lower():
        return "ユーザー名と同じパスワードは使えません"
    return None
