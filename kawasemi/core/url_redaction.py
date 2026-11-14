"""URL の伏せ処理（設定に依存しない軽いモジュール）。

URL に埋め込まれた資格情報（`user:pw@host`、`?access_token=...`）を、ログや API の応答に返す
文面から取り除く。**正規表現で秘匿値の名前を列挙しない**: 名前の一覧は必ず取りこぼす（`X-Amz-Signature`、
`jwt`、`accessToken` …）ので、表示用の URL は userinfo・クエリ・フラグメントを丸ごと落とす。
実装は `str.find` と一度きりの走査だけで、入力の長さに対して線形時間（正規表現の再試行による
二次時間を持ち込まない）。
"""

from __future__ import annotations

from collections.abc import Iterator

# URL の1語として扱う範囲の終わり。空白・引用符・山括弧・制御文字。
_URL_STOP = frozenset(" \t\r\n\f\v\"'<>`\x00")
# URL の直後に付く句読点（文章の中の URL）。URL には含めない。
_TRAILING = ".,;:!?)]}"
# 1 つの URL として処理する長さの上限。これを超える URL は、超えた分を捨てる。
_MAX_URL_LEN = 4096
# 文字列全体を走査する長さの上限。これを超える分は捨てる。
MAX_TEXT_LEN = 64 * 1024
TRUNCATED = "…[truncated]"
UNPARSEABLE = "<unparseable url>"


def _find_urls(text: str) -> Iterator[tuple[int, int]]:
    """`scheme://...` の範囲 (start, end) を、左から順に返す（線形時間）。"""
    n = len(text)
    i = 0
    while True:
        j = text.find("://", i)
        if j == -1:
            return
        s = j
        while s > i and j - s < 33:
            c = text[s - 1]
            if c.isascii() and (c.isalnum() or c in "+.-"):
                s -= 1
            else:
                break
        while s < j and not (text[s].isascii() and text[s].isalpha()):
            s += 1
        if s == j:
            i = j + 3
            continue
        e = j + 3
        while e < n and text[e] not in _URL_STOP:
            e += 1
        yield s, e
        i = e


def _split_trailing(token: str) -> tuple[str, str]:
    end = len(token)
    while end > 0 and token[end - 1] in _TRAILING:
        end -= 1
    return token[:end], token[end:]


def display_url(url: str) -> str:
    """表示用の URL（scheme・host・port・path だけ）。userinfo・クエリ・フラグメントを落とす。

    パスワードに `/` を含む不正な形も、最後の `@` までを userinfo とみなして落とす（安全側）。
    解釈できないもの（空・空白や制御文字を含む）は `<unparseable url>` にする。
    """
    if len(url) > _MAX_URL_LEN:
        url = url[:_MAX_URL_LEN]
    if not url or any(c in _URL_STOP for c in url):
        return UNPARSEABLE
    url = url.split("#", 1)[0].split("?", 1)[0]
    if "://" in url:
        scheme, _, rest = url.partition("://")
        prefix = scheme + "://"
    elif url.startswith("//"):
        rest = url[2:]
        prefix = "//"
    else:
        # スキームなし（`user:pw@host/x`）。最初の `/` までに `@` があれば userinfo。
        head, slash, tail = url.partition("/")
        if "@" in head:
            head = head.rpartition("@")[2]
        return head + slash + tail
    if "@" in rest:
        rest = rest.rpartition("@")[2]
    return prefix + rest


def display_urls(text: str) -> str:
    """文字列中の URL を、すべて表示用（`display_url`）に置き換える。"""
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + TRUNCATED
    out: list[str] = []
    pos = 0
    for start, end in _find_urls(text):
        body, tail = _split_trailing(text[start:end])
        out.append(text[pos:start])
        out.append(display_url(body))
        out.append(tail)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def strip_userinfo(text: str) -> str:
    """文字列中の `scheme://user:pw@host` から userinfo だけを落とす（ログの二重防御）。

    クエリやパスはそのまま残す。`host:8080?email=a@b` のように、`@` が権限部（authority）の
    外にあるものは触らない。
    """
    out: list[str] = []
    pos = 0
    for start, end in _find_urls(text):
        token = text[start:end]
        scheme, _, rest = token.partition("://")
        cut = len(rest)
        for ch in "/?#":
            k = rest.find(ch)
            if k != -1 and k < cut:
                cut = k
        authority = rest[:cut]
        # パスワードに `/` を含む不正な形を、できる範囲で扱う: 最初の `/` の前に `:` があり
        # （ポート番号ではなく）、後ろの `@` が同じ語にあるなら、最後の `@` まで userinfo とみなす。
        if "@" not in authority and cut < len(rest) and rest[cut] == "/" and ":" in authority:
            port = authority.rpartition(":")[2]
            if not port.isdigit():
                at = rest.rfind("@")
                if at > cut and "?" not in rest[:at] and "#" not in rest[:at]:
                    out.append(text[pos:start])
                    out.append(scheme + "://" + rest[at + 1 :])
                    pos = end
                    continue
        if "@" in authority:
            out.append(text[pos:start])
            out.append(scheme + "://" + rest[authority.rfind("@") + 1 :])
            pos = end
    out.append(text[pos:])
    return "".join(out)
